"""
Drive Hermes through the 3d-printer-slicing-automation skill end-to-end
against gemma4-26b-64k, auto-answering need_input prompts, capturing every
event, and scoring against the 8 acceptance criteria in
docs/DESIGN-CONTRACT.md.

NOT a pytest test — runs `docker exec hermes-agent-stack hermes chat ...`
across multiple turns to simulate a Telegram conversation.

Usage from dev-container (host: /appdata/hermes/...):

    python3 /appdata/hermes/workspaces/snapmaker-u1-toolkit/tests/harness/drive_via_hermes.py

Default config replays the wall_mount_laid_on_back walkthrough (the run
Brent has been doing manually). Pass --stl / --tool / --material /
--profile / --supports / --orient / --upload-mode / --collision to override.

The harness STOPS at Stage 1 (after the agent surfaces the bed photo path
+ approval token). Operator approval of the photo is NOT machine-driven.

Outputs:
- transcript.jsonl: one entry per turn — sent message, received text,
  parsed events, parsed agent question, auto-answer chosen
- score.json: pass/fail per acceptance criterion + reasons
- summary.txt: human-readable summary of what failed and where
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

HERMES_CONTAINER = 'hermes-agent-stack'
HERMES_BIN = '/opt/hermes/.venv/bin/hermes'
MODEL = 'gemma4-26b-64k'
PROVIDER = 'ollama'
SKILL = '3d-printer-slicing-automation'
TURN_TIMEOUT = 600   # 10 minutes — slice can take ~100s + LLM latency
MAX_TURNS = 25       # hard ceiling to prevent runaway loops

# Workflow mirrors every emit() to <out_dir>/events.jsonl. We scan multiple
# possible roots to find them:
#   - v2.0+: <data_dir>/requests/<request_id>/events.jsonl  (Print Request Objects)
#   - v1.5-v1.6 legacy: <ROOT>/artifacts/slice_workflow/<stem>/<ts>/events.jsonl
# Both trees may coexist during the v2.0 migration window. The harness
# walks both — the per-run scan picks up whichever the workflow wrote to.
#
# H4 (Phase 2 cold review): the candidates list is fixed at import; the
# .exists() check happens INSIDE scan_events_jsonl at scan time, so a
# request dir created AFTER the harness imports is found on the very next
# scan (not silently missed forever).
ARTIFACT_TREES = [
    # v2.0 Print Request Objects (primary)
    Path('/appdata/hermes/snapmaker_u1/requests'),
    Path('/opt/data/snapmaker_u1/requests'),
    # v1.5-v1.6 legacy slice_workflow tree (kept for in-flight runs)
    Path('/appdata/hermes/artifacts/slice_workflow'),
    Path('/opt/data/artifacts/slice_workflow'),
]
# Back-compat alias for any code that still imports ARTIFACT_TREE
ARTIFACT_TREE = ARTIFACT_TREES[0]


@dataclass
class ExpectedAnswers:
    """Slugs the harness sends back when each known prompt fires. These are
    the EXACT slug strings expected from the workflow's need_input options
    (the `value` field), not paraphrased labels."""
    orient: str = 'asauthored'                # source as authored (no rotation)
    tool: str = 'T1'
    material: str = 'Snapmaker PETG @U1'      # display name, agent should resolve to slug
    profile: str = '0.20mm Strength @Snapmaker U1 (0.4 nozzle)'  # paraphrase-tolerant; agent should resolve via picker
    supports: str = 'no_supports'
    upload_mode: str = 'Upload + start gate'
    collision: str = 'overwrite'              # if filename collision fires


@dataclass
class TurnRecord:
    turn_idx: int
    sent: str
    raw_stdout: str
    raw_stderr: str
    returncode: int
    session_id_after: str | None
    agent_text: str
    parsed_events: list[dict[str, Any]]
    detected_prompt: str | None
    auto_answer: str | None
    elapsed_seconds: float


@dataclass
class CriterionResult:
    name: str
    passed: bool | None  # None = not applicable / not reached
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


def docker_exec_hermes_chat(message: str, session_id: str | None, log_dir: Path) -> tuple[subprocess.CompletedProcess, float]:
    """One hermes chat turn. Returns (CompletedProcess, elapsed_seconds)."""
    cmd = [
        'docker', 'exec', HERMES_CONTAINER,
        HERMES_BIN, 'chat',
        '-q', message,
        '-m', MODEL,
        '--provider', PROVIDER,
        '-s', SKILL,
        '-Q',                # quiet — suppress banners
        '--yolo',            # don't prompt on shell hooks
        '--pass-session-id', # print session id to stdout for capture
        '--source', 'harness',
        '--max-turns', '20',
    ]
    if session_id:
        cmd.extend(['-r', session_id])

    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TURN_TIMEOUT)
    elapsed = time.monotonic() - t0
    return proc, elapsed


def parse_session_id(stdout: str) -> str | None:
    """`hermes chat --pass-session-id` prints e.g. `session_id: 20260626_083144_661c2c`
    on its own line. Match that shape primarily, with looser fallbacks for older
    Hermes versions."""
    patterns = [
        r'^\s*session_id\s*[:=]\s*([0-9]{8}_[0-9]{6}_[0-9a-f]+)\s*$',  # observed format
        r'session_id["\']?\s*[:=]\s*["\']?([0-9A-Za-z_-]{12,})',
        r'Session(?:\s+ID)?[:\s]+([0-9A-Za-z_-]{12,})',
    ]
    for pat in patterns:
        m = re.search(pat, stdout, re.IGNORECASE | re.MULTILINE)
        if m:
            return m.group(1)
    return None


def parse_json_events(stdout: str) -> list[dict[str, Any]]:
    """Scan stdout for JSON event lines emitted by the workflow when called
    with --json-events. Each event is a single-line dict with at least a
    `stage` field."""
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not (stripped.startswith('{') and stripped.endswith('}')):
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and 'stage' in obj:
            events.append(obj)
    return events


def scan_events_jsonl(start_time: float, already_read_bytes: dict[Path, int]) -> tuple[list[dict[str, Any]], dict[Path, int]]:
    """Walk every ARTIFACT_TREES root for events.jsonl files modified since
    start_time. Read only the new bytes since the last scan (incremental tail).
    Returns (newly_parsed_events, updated_already_read_bytes).

    This is the fallback for when `hermes chat -Q` strips tool stdout from
    its own output. The workflow writes every emit() to events.jsonl, so
    this file is the ground-truth event stream.

    v2.0 Phase 2: scans BOTH the v2.0 requests/ tree AND the legacy
    slice_workflow tree, so the harness keeps working through the migration."""
    new_events: list[dict[str, Any]] = []
    updated = dict(already_read_bytes)
    found_paths: list[Path] = []
    # H4 fix: check .exists() at scan time, not at import time. A request
    # directory created mid-conversation needs to be picked up on the very
    # next scan call, not silently missed because it didn't exist when
    # the harness module loaded.
    for tree in ARTIFACT_TREES:
        if tree.exists():
            found_paths.extend(tree.rglob('events.jsonl'))
    if not found_paths:
        return new_events, updated
    for events_path in found_paths:
        try:
            mtime = events_path.stat().st_mtime
        except FileNotFoundError:
            continue
        if mtime < start_time - 60:  # 60s grace for clock skew
            continue
        offset = updated.get(events_path, 0)
        try:
            with events_path.open('rb') as f:
                f.seek(offset)
                chunk = f.read()
            updated[events_path] = offset + len(chunk)
        except OSError:
            continue
        for raw_line in chunk.decode('utf-8', errors='replace').splitlines():
            line = raw_line.strip()
            if not line.startswith('{'):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and 'stage' in obj:
                obj['_source_file'] = str(events_path)
                new_events.append(obj)
    return new_events, updated


PROMPT_PATTERNS = {
    'orient':         re.compile(r'orient(?:ation)?[\s\S]{0,200}(asauthored|auto|source)', re.I),
    'tool':           re.compile(r'(?:toolhead|tool(?:head)?(?:\s*&\s*filament)?)\??', re.I),
    'preset':         re.compile(r'(?:print preset|profile|preset)\??', re.I),
    'supports':       re.compile(r'support', re.I),
    'upload_mode':    re.compile(r'(?:upload[?\s]+|upload\s*(?:only|\+|and|or)|start\s+gate)', re.I),
    'collision':      re.compile(r'collision|already exists|filename', re.I),
    'bed_clear':      re.compile(r'(?:bed clear|review the (?:attached )?photo|start[?\s]+\(yes/no\))', re.I),
}


_WORKFLOW_KEY_TO_KIND = {
    'orient': 'orient', 'tool': 'tool', 'preset': 'preset',
    'supports': 'supports', 'upload': 'upload_mode',
    'filename_collision': 'collision',
}


def match_question_to_event(agent_text: str, events: list[dict[str, Any]]) -> str | None:
    """Find which need_input event the agent is asking about, by matching
    the event's `prompt` field (strong signal) or option labels (fallback)
    against the agent's reply text.

    Order of preference:
      1. Event's `prompt` text appears in agent_text → that's the prompt
         being asked (this is the cleanest signal — e.g. "Filename
         collision?" / "Toolhead & filament?" / "Supports?").
      2. Otherwise, match against option labels — but use the SHORTEST
         distinctive substring per event, and prefer events whose key was
         emitted LATER in the workflow (collision > upload > supports > ...)
         so generic shared labels like "Cancel" don't false-match.
    """
    text_lower = agent_text.lower()
    candidates = [ev for ev in events if ev.get('stage') == 'need_input']

    # Pass 1: match by event's `prompt` field — unambiguous.
    for ev in candidates:
        prompt = str(ev.get('prompt', '')).lower().strip()
        if prompt and len(prompt) >= 5 and prompt in text_lower:
            return _WORKFLOW_KEY_TO_KIND.get(ev.get('key', ''))

    # Pass 2: option-label fallback. Iterate in REVERSE workflow order so
    # later/more-specific prompts win on generic-label collisions ("Cancel"
    # appears in both upload_mode and filename_collision).
    workflow_order = {'orient': 0, 'tool': 1, 'preset': 2, 'supports': 3, 'upload': 4, 'filename_collision': 5}
    candidates_reversed = sorted(candidates, key=lambda e: -workflow_order.get(e.get('key', ''), 99))
    for ev in candidates_reversed:
        opts = ev.get('options', [])
        # Require at least one DISTINCTIVE label match (≥8 chars, not a
        # common word like "Cancel"). The shorter the label, the more
        # likely it's generic.
        for opt in opts:
            label = str(opt.get('label', '')).lower().strip()
            if not label or len(label) < 8:
                continue
            if label[:25] in text_lower:
                return _WORKFLOW_KEY_TO_KIND.get(ev.get('key', ''))
    return None


def detect_prompt_kind(agent_text: str, events: list[dict[str, Any]]) -> str | None:
    """Return one of the PROMPT_PATTERNS keys, or 'photo_first' if a Stage 1
    photo path appears, or None if no prompt is being asked."""
    # need_input events are the authoritative source
    for ev in reversed(events):
        if ev.get('stage') == 'need_input':
            key = ev.get('key', '')
            if key == 'orient':
                return 'orient'
            if key == 'tool':
                return 'tool'
            if key == 'preset':
                return 'preset'
            if key == 'supports':
                return 'supports'
            if key == 'filename_collision':
                return 'collision'
    # Photo path = Stage 1 succeeded
    if re.search(r'/opt/data/snapmaker_u1/[^\s`"\']*\.(jpg|png)', agent_text):
        return 'photo_first'
    # Fallback to text patterns
    if PROMPT_PATTERNS['upload_mode'].search(agent_text):
        return 'upload_mode'
    if PROMPT_PATTERNS['bed_clear'].search(agent_text):
        return 'bed_clear'
    return None


def choose_answer(prompt_kind: str | None, expected: ExpectedAnswers) -> str | None:
    if prompt_kind is None:
        return None
    mapping = {
        'orient':       expected.orient,
        'tool':         expected.tool,
        'preset':       expected.profile,
        'supports':     expected.supports,
        'upload_mode':  expected.upload_mode,
        'collision':    expected.collision,
    }
    return mapping.get(prompt_kind)


_WARNING_LEAD = re.compile(r'^\s*(warning|info|debug|note)\b', re.IGNORECASE)
_SESSION_TAIL = re.compile(r'^\s*session_id\s*[:=]', re.IGNORECASE)


def extract_agent_text(stdout: str) -> str:
    """`hermes chat -Q` layout: leading `Warning: ...` lines, blank, agent
    response (possibly multi-line, including tool-call output), blank,
    `session_id: <id>` tail.

    Strip leading warning/info lines and the session_id tail. What's left
    is the agent's response (which may itself contain workflow stdout
    interleaved with the LLM's narrative)."""
    lines = stdout.splitlines()
    # Drop leading warnings/info
    while lines and (not lines[0].strip() or _WARNING_LEAD.match(lines[0])):
        lines.pop(0)
    # Drop trailing session_id (+ any blank line above it)
    while lines and (not lines[-1].strip() or _SESSION_TAIL.match(lines[-1])):
        lines.pop()
    return '\n'.join(lines).strip()


def score_acceptance_criteria(
    turns: list[TurnRecord],
    all_events: list[dict[str, Any]],
) -> list[CriterionResult]:
    """Score the 8 acceptance criteria from docs/DESIGN-CONTRACT.md.
    Criteria 5–8 may be NotApplicable (None) when the harness stopped
    before Stage 2 (which is by design — operator approval required)."""
    results: list[CriterionResult] = []

    # 1. Walkthrough: 4 distinct prompts asked + answered, one per turn.
    # Data source = the harness's own `detected_prompt` per turn (which is
    # backed by match_question_to_event — looks at the option labels of
    # need_input events and finds which one's options appear in agent_text).
    # That's far less noisy than keyword anchors.
    prompts_per_turn = [t.detected_prompt for t in turns if t.detected_prompt]
    distinct_required = {'orient', 'tool', 'preset', 'supports'}
    distinct_asked = set(prompts_per_turn) & distinct_required
    # Duplicates: the agent re-asked the same prompt on multiple turns
    duplicates = [p for p in prompts_per_turn if prompts_per_turn.count(p) > 1]
    results.append(CriterionResult(
        name='1_walkthrough',
        passed=(distinct_asked == distinct_required and not duplicates),
        reason=(f'distinct prompts answered: {sorted(distinct_asked)} '
                f'(need {sorted(distinct_required)}); '
                f'duplicate-prompt turns: {sorted(set(duplicates))}'),
        evidence={'prompts_per_turn': prompts_per_turn},
    ))

    # 2. Slice + preview: render event with kind=preview AND summary event with width_mm/depth_mm
    has_preview = any(ev.get('stage') == 'render' and ev.get('kind') == 'preview' for ev in all_events)
    has_summary = any(ev.get('stage') == 'summary' and 'first_layer_width_mm' in ev for ev in all_events)
    results.append(CriterionResult(
        name='2_slice_preview',
        passed=(has_preview and has_summary),
        reason=f'preview event: {has_preview}, summary with width_mm: {has_summary}',
    ))

    # 3. Collision short-circuit: only applicable if a filename_collision prompt fired
    collision_fired = any(ev.get('stage') == 'need_input' and ev.get('key') == 'filename_collision' for ev in all_events)
    slice_reused = any(ev.get('stage') == 'slice_reused' for ev in all_events)
    re_slice_count = sum(1 for ev in all_events if ev.get('stage') == 'slicing')
    if collision_fired:
        # Should see slice_reused on the re-run AND should NOT have called slicing twice
        passed_3 = slice_reused and re_slice_count == 1
        reason_3 = f'collision fired, slice_reused: {slice_reused}, slicing-stage count: {re_slice_count}'
    else:
        passed_3 = None
        reason_3 = 'no collision fired — criterion not applicable this run'
    results.append(CriterionResult(
        name='3_collision_short_circuit',
        passed=passed_3,
        reason=reason_3,
    ))

    # 4. Readiness card present
    readiness = next((ev for ev in all_events if ev.get('stage') == 'readiness_card'), None)
    results.append(CriterionResult(
        name='4_readiness_card_present',
        passed=bool(readiness),
        reason='readiness_card event captured' if readiness else 'readiness_card MISSING — agent cannot legitimately advance to Stage 1',
        evidence={'readiness_card_keys': sorted(readiness.keys()) if readiness else []},
    ))

    # 5. Stage 1 photo first: photo path bare in reply BEFORE any verdict text
    photo_turn = None
    for t in turns:
        if re.search(r'/opt/data/snapmaker_u1/[^\s`"\']*\.(jpg|png)', t.agent_text):
            photo_turn = t
            break
    if photo_turn is None:
        results.append(CriterionResult(
            name='5_stage1_photo_first',
            passed=None,
            reason='no Stage 1 photo path appeared in any turn',
        ))
    else:
        # Photo path should appear EARLY in the text — before words like "ok", "proceed", "ready"
        text = photo_turn.agent_text
        photo_idx = text.find('/opt/data/snapmaker_u1/')
        verdict_words = ['ready to print', 'proceed', 'ok to start', 'good to go', 'looks clear']
        first_verdict = min((text.lower().find(w) for w in verdict_words if w in text.lower()), default=-1)
        photo_first = (photo_idx >= 0 and (first_verdict < 0 or photo_idx < first_verdict))
        results.append(CriterionResult(
            name='5_stage1_photo_first',
            passed=photo_first,
            reason=f'photo path index={photo_idx}, first verdict word index={first_verdict}',
        ))

    # 6. Stage 1 refusal logic: only scorable when we have a snapshot dict
    snapshot_events = [ev for ev in all_events if ev.get('stage') == 'preflight' and 'snapshot' in ev]
    if not snapshot_events:
        results.append(CriterionResult(
            name='6_stage1_refusal_logic',
            passed=None,
            reason='no Stage 1 preflight event with snapshot captured — criterion not scorable',
        ))
    else:
        # This criterion is honored by behavior, not asserted by harness — flag only obvious fails
        snap = snapshot_events[-1].get('snapshot', {})
        if snap.get('brightness_check') == 'deferred':
            # Agent should NOT have refused on deferred — check if it stopped vs. continued
            stopped_at_photo = any(t for t in turns if 'refuse' in t.agent_text.lower() and 'deferred' in t.agent_text.lower())
            results.append(CriterionResult(
                name='6_stage1_refusal_logic',
                passed=not stopped_at_photo,
                reason=f'brightness deferred; agent refused on it: {stopped_at_photo}',
            ))
        else:
            results.append(CriterionResult(
                name='6_stage1_refusal_logic',
                passed=True,
                reason='brightness_check was measured — refusal logic not stress-tested this run',
            ))

    # 7. Stage 2 dispatch: not reached by harness (by design)
    results.append(CriterionResult(
        name='7_stage2_dispatch',
        passed=None,
        reason='harness stops at Stage 1 — Stage 2 requires operator approval; not machine-driven',
    ))

    # 8. Cancel before bed heat: operator-side action, not harness-driven
    results.append(CriterionResult(
        name='8_cancel_before_heat',
        passed=None,
        reason='operator action, not machine-driven',
    ))

    return results


def run_harness(stl_path: Path, expected: ExpectedAnswers, log_dir: Path) -> tuple[list[TurnRecord], list[CriterionResult]]:
    log_dir.mkdir(parents=True, exist_ok=True)

    initial_prompt = (
        f'Slice this using our 3d-printer-slicing-automation skill {stl_path}'
    )

    turns: list[TurnRecord] = []
    all_events: list[dict[str, Any]] = []
    session_id: str | None = None
    next_message = initial_prompt
    harness_start = time.time()
    file_offsets: dict[Path, int] = {}

    for turn_idx in range(MAX_TURNS):
        print(f'[harness] turn {turn_idx + 1} — sending: {next_message[:80]!r}...', flush=True)
        try:
            proc, elapsed = docker_exec_hermes_chat(next_message, session_id, log_dir)
        except subprocess.TimeoutExpired:
            print(f'[harness] TURN TIMEOUT ({TURN_TIMEOUT}s) — aborting', flush=True)
            turns.append(TurnRecord(
                turn_idx=turn_idx, sent=next_message, raw_stdout='', raw_stderr='TIMEOUT',
                returncode=-1, session_id_after=session_id, agent_text='',
                parsed_events=[], detected_prompt=None, auto_answer=None,
                elapsed_seconds=TURN_TIMEOUT,
            ))
            break

        # `hermes chat --pass-session-id` writes the session line to STDERR,
        # not stdout (verified against real run). Check both.
        new_session = parse_session_id(proc.stdout) or parse_session_id(proc.stderr) or session_id
        # Two event sources: (a) stdout — present if hermes didn't strip tool
        # output, (b) <out_dir>/events.jsonl — ground truth always. Merge.
        stdout_events = parse_json_events(proc.stdout)
        disk_events, file_offsets = scan_events_jsonl(harness_start, file_offsets)
        events = stdout_events + disk_events
        agent_text = extract_agent_text(proc.stdout)
        # The workflow emits all 4 need_input events in ONE call (the
        # ANALYSIS+DECISION phase), but the agent surfaces them ONE PER TURN
        # across multiple turns — re-asking from memory without re-invoking
        # the workflow. So match against the ROLLING set of events seen so
        # far, not just this turn's events.
        rolling_events = all_events + events
        prompt_kind = match_question_to_event(agent_text, rolling_events) or detect_prompt_kind(agent_text, rolling_events)
        # Skip auto-answer if the prompt we matched is one we've ALREADY
        # answered — the agent may have re-cited an old question by accident
        # and we don't want to loop.
        already_answered = {t.detected_prompt for t in turns if t.detected_prompt}
        if prompt_kind in already_answered:
            prompt_kind = None
        auto_answer = choose_answer(prompt_kind, expected)

        record = TurnRecord(
            turn_idx=turn_idx, sent=next_message, raw_stdout=proc.stdout,
            raw_stderr=proc.stderr, returncode=proc.returncode,
            session_id_after=new_session, agent_text=agent_text,
            parsed_events=events, detected_prompt=prompt_kind,
            auto_answer=auto_answer, elapsed_seconds=elapsed,
        )
        turns.append(record)
        all_events.extend(events)
        session_id = new_session

        print(f'[harness] turn {turn_idx + 1} done in {elapsed:.1f}s — '
              f'rc={proc.returncode}, events={len(events)}, '
              f'detected_prompt={prompt_kind!r}, auto_answer={auto_answer!r}',
              flush=True)

        # Stop conditions
        if prompt_kind == 'photo_first':
            print('[harness] STOP — Stage 1 photo path surfaced. Human review required.', flush=True)
            break
        if prompt_kind == 'bed_clear':
            print('[harness] STOP — bed-clear yes/no question reached. NOT auto-answering.', flush=True)
            break
        if proc.returncode != 0 and not events:
            print(f'[harness] STOP — non-zero rc with no events. stderr tail: {proc.stderr[-400:]!r}', flush=True)
            break
        if auto_answer is None:
            print('[harness] STOP — no recognized prompt and no auto-answer. Likely terminal state or agent stuck.', flush=True)
            break

        next_message = auto_answer

    # Score
    results = score_acceptance_criteria(turns, all_events)

    # Write artifacts
    (log_dir / 'transcript.jsonl').write_text(
        '\n'.join(json.dumps(asdict(t), default=str) for t in turns) + '\n'
    )
    (log_dir / 'score.json').write_text(
        json.dumps([asdict(r) for r in results], indent=2)
    )
    summary_lines = ['=== Acceptance criteria ===']
    for r in results:
        flag = '✓' if r.passed is True else ('✗' if r.passed is False else '-')
        summary_lines.append(f'  {flag} {r.name}: {r.reason}')
    summary_lines.append('')
    summary_lines.append(f'Turns: {len(turns)}')
    summary_lines.append(f'Total events captured: {len(all_events)}')
    (log_dir / 'summary.txt').write_text('\n'.join(summary_lines) + '\n')

    print('\n'.join(summary_lines), flush=True)
    print(f'\n[harness] Artifacts: {log_dir}/transcript.jsonl, score.json, summary.txt', flush=True)

    return turns, results


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--stl', type=Path,
                    default=Path('/opt/data/snapmaker_u1/artifacts/caulk_holder_wall_20260624-121524/wall_mount_laid_on_back.stl'),
                    help='STL path INSIDE the Hermes container (default: Brent\'s wall-mount test fixture)')
    ap.add_argument('--orient', default='asauthored')
    ap.add_argument('--tool', default='T1')
    ap.add_argument('--material', default='Snapmaker PETG @U1')
    ap.add_argument('--profile', default='0.20mm Strength @Snapmaker U1 (0.4 nozzle)')
    ap.add_argument('--supports', default='no_supports')
    ap.add_argument('--upload-mode', default='Upload + start gate')
    ap.add_argument('--collision', default='overwrite')
    ap.add_argument('--log-dir', type=Path,
                    default=Path('/appdata/hermes/snapmaker_u1/harness-runs') / time.strftime('%Y%m%d-%H%M%S'),
                    help='Where to write transcript.jsonl, score.json, summary.txt')
    args = ap.parse_args()

    expected = ExpectedAnswers(
        orient=args.orient, tool=args.tool, material=args.material,
        profile=args.profile, supports=args.supports,
        upload_mode=args.upload_mode, collision=args.collision,
    )
    print(f'[harness] target model: {MODEL} via {PROVIDER}', flush=True)
    print(f'[harness] skill: {SKILL}', flush=True)
    print(f'[harness] STL: {args.stl}', flush=True)
    print(f'[harness] log dir: {args.log_dir}', flush=True)
    print(f'[harness] expected answers: {asdict(expected)}', flush=True)

    turns, results = run_harness(args.stl, expected, args.log_dir)

    # Exit code: 0 if no criterion failed, 1 if any explicit fail (None doesn't count)
    any_fail = any(r.passed is False for r in results)
    sys.exit(1 if any_fail else 0)


if __name__ == '__main__':
    main()
