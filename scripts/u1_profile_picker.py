#!/usr/bin/env python3
"""Scan profile sources, list/score them, and surface supports state.

The toolkit gets profiles from three sources, scanned in priority order:

  1. profiles/from-printer/   — extracted from the user's own recent prints
                                via tools/extract_profiles_from_printer.py.
                                These reflect what the user has actually
                                successfully printed with; rank highest.
  2. profiles/user/           — operator's hand-tuned profiles + overrides.
  3. profiles/snapmaker-stock/ — Snapmaker's official upstream profiles
                                fetched via tools/fetch_snapmaker_profiles.py.

Each scanned profile is annotated with:
  - source: 'from-printer' | 'user' | 'snapmaker-stock'
  - has_supports: bool, read from the profile JSON's `enable_support`
    field (Orca convention — string "1" means supports enabled).

Backward-compatible API: `list_profiles()` still returns a list of dicts
with `label`, `value`, `path`, `recommended` — plus the new `source` and
`has_supports` fields.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

PROFILES_ROOT = Path(__file__).resolve().parent.parent / 'profiles'

# Source-dir order also defines priority: earlier sources win when the
# same profile value would otherwise collide.
DEFAULT_SOURCES: tuple[tuple[str, Path], ...] = (
    ('from-printer', PROFILES_ROOT / 'from-printer'),
    ('user', PROFILES_ROOT / 'user'),
    ('snapmaker-stock', PROFILES_ROOT / 'snapmaker-stock'),
)


_SLUG_RE = re.compile(r'[^a-z0-9]+')


def normalize_value(name: str) -> str:
    """Canonical slug form used in profile dict 'value' fields.

    Lowercase + replace all non-alphanumerics with underscores + strip
    leading/trailing underscores. Used by BOTH `profile_id()` (when
    building the picker's value field from a file stem) AND by callers
    looking up a profile by user-typed name — so the two paths can't
    drift."""
    return _SLUG_RE.sub('_', name.lower()).strip('_') or 'profile'


def profile_id(path: Path) -> str:
    """Return a shell-safe slug for the profile value field.

    Snapmaker stock names have spaces / @ / parens (e.g.
    `0.20 Strength @Snapmaker U1 (0.4 nozzle).json`) which would break
    CLI flag parsing."""
    return normalize_value(path.stem)


def _read_supports_flag(path: Path) -> bool:
    """Inspect a profile JSON for OrcaSlicer's `enable_support` field.

    Orca encodes booleans as strings ('0' or '1'). Returns True iff the
    field is present and equals '1'. Missing or unreadable JSON → False
    (fail-closed: don't claim supports for something we can't verify)."""
    try:
        with path.open('r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    val = data.get('enable_support')
    if isinstance(val, str):
        return val.strip() == '1'
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return int(val) == 1
    return False


def _scan_dir(source_label: str, source_dir: Path) -> list[dict]:
    """One source dir → list of profile dicts. Subdirs are recursed (Snapmaker
    upstream layout puts machine/process/filament/ as subdirs)."""
    if not source_dir.exists():
        return []
    opts: list[dict] = []
    for path in sorted(source_dir.rglob('*.json')):
        # Skip the README marker some sources drop.
        if path.name.lower() == 'readme.json':
            continue
        # Filter to PROCESS profiles only — machine and filament profiles
        # aren't picker candidates, but they live in the same source tree
        # (snapmaker-stock has machine/, process/, filament/ subdirs).
        if not _is_process_profile(path):
            continue
        pid = profile_id(path)
        # from-printer profiles are named after the source G-code file; show the
        # profile's OWN name (its print_settings_id) instead so the picker reads
        # "0.20 Strength Gyroid", not "Phillips_Hue_..._process". `value` stays
        # file-derived so profile resolution is unchanged.
        label = path.stem
        if source_label == 'from-printer':
            label = _extracted_profile_label(path) or path.stem
        opts.append({
            'label': label,
            'value': pid,
            'path': str(path),
            'source': source_label,
            'has_supports': _read_supports_flag(path),
            'mtime': _safe_mtime(path),
        })
    return opts


def _is_process_profile(path: Path) -> bool:
    """Cheap process-vs-machine/filament check.

    For snapmaker-stock the parent dir name tells us. For from-printer the
    extractor writes `<stem>_process.json` and `<stem>_filament.json`. For
    user profiles we fall back to reading the JSON `type` field."""
    parent = path.parent.name.lower()
    if parent == 'process':
        return True
    if parent in ('machine', 'filament'):
        return False
    stem = path.stem.lower()
    if stem.endswith('_filament') or stem.endswith('_machine'):
        return False
    if stem.endswith('_process'):
        return True
    # Fall back to reading the JSON `type` field. Worth the I/O hit for the
    # `profiles/user/` case where filenames don't follow a convention.
    try:
        with path.open('r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    return data.get('type') == 'process'


def _nozzle_matches(label: str, nozzle: str) -> bool:
    """Heuristic: does this profile's label name a matching nozzle?

    Snapmaker stock labels follow `... @Snapmaker U1 (0.4 nozzle)`. Extracted +
    user profiles may or may not encode nozzle — when unencoded, keep them
    (don't filter aggressively, the user explicitly populated those dirs)."""
    low = label.lower()
    nz = nozzle.strip().lower()
    if f'({nz} nozzle)' in low or f'_{nz.replace(".", "_")}_nozzle' in low:
        return True
    # Profile label doesn't encode a nozzle at all → keep it (user/extracted)
    return 'nozzle' not in low


_HEIGHT_PREFIX_RE = re.compile(r'^\s*(\d+\.\d+)')


_HISTORY_DECORATION_PREFIXES = ('community ', 'hermes ', 'custom ')


def _strip_decorations(s: str) -> str:
    """Strip common name decorations from a profile label or print_settings_id.

    The match between a picker label and a `print_settings_id` from
    `print_history.json` has to tolerate the toolkit / community renaming
    conventions: 'Community ', 'Hermes ', or 'Custom ' added as a leading
    word; ' @<printer> [<surface>]' appended at the end. Strip both so
    comparison is based on the layer-height + profile-class core only.

    Operates on the already-lowercased form. Returns the stripped core."""
    out = s.strip()
    for pfx in _HISTORY_DECORATION_PREFIXES:
        if out.startswith(pfx):
            out = out[len(pfx):]
            break
    at_idx = out.find(' @')
    if at_idx > 0:
        out = out[:at_idx]
    return out.strip()


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except Exception:
        return 0.0


def _clean_extracted_name(name: str) -> str:
    """Human-facing form of an extracted profile's print_settings_id: strip a
    leading Community/Hermes/Custom word and the ' @<printer>' suffix, keep case.
    'Community 0.20 Strength Gyroid @Snapmaker U1 Textured PEI' -> '0.20 Strength
    Gyroid'."""
    out = name.strip()
    low = out.lower()
    for pfx in _HISTORY_DECORATION_PREFIXES:
        if low.startswith(pfx):
            out = out[len(pfx):]
            break
    at = out.find(' @')
    if at > 0:
        out = out[:at]
    return out.strip() or name.strip()


def _extracted_profile_label(path: Path) -> str | None:
    """Display name for a from-printer profile: the process profile's own `name`
    (its print_settings_id), cleaned. None if unreadable → caller falls back to
    the file stem."""
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    name = data.get('name') or data.get('print_settings_id')
    if isinstance(name, str) and name.strip():
        return _clean_extracted_name(name.strip())
    return None


def _dedupe_by_name(opts: list[dict], source_priority: dict[str, int]) -> list[dict]:
    """Collapse profiles that resolve to the same NAME to one entry. Covers two
    cases: (1) from-printer reprints of the same object (many timestamps), and
    (2) a captured print whose name matches a hand-saved user profile or a stock
    one. Keep the highest-priority source (from-printer > user > stock), newest as
    tiebreak — so the picker never shows the same profile twice. Distinct names
    (e.g. '0.20 Strength' vs '0.20 Strength Gyroid') are untouched."""
    best: dict[str, dict] = {}
    order: list[str] = []
    for o in opts:
        key = _strip_decorations(str(o.get('label', '')).lower())
        cur = best.get(key)
        if cur is None:
            best[key] = o
            order.append(key)
            continue
        cp = (source_priority.get(o.get('source'), 99), -float(o.get('mtime', 0)))
        pp = (source_priority.get(cur.get('source'), 99), -float(cur.get('mtime', 0)))
        if cp < pp:
            best[key] = o
    return [best[k] for k in order]


def _extract_layer_height_mm(label: str) -> float | None:
    """Snapmaker convention: process profile labels start with a layer-height
    prefix like '0.20 Strength @Snapmaker U1 (0.4 nozzle)'. Returns the
    leading float, or None if the label doesn't follow the convention.

    Used by the layer-height priority score so workhorse heights
    (0.20 / 0.16) surface above specialty extra-fine (0.06/0.08) for a 0.4
    nozzle user. Falls back to reading the profile JSON's `layer_height`
    field (see _read_layer_height) when the label is off-convention (cold
    review F2)."""
    m = _HEIGHT_PREFIX_RE.match(label)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _read_layer_height(path_str: str | None) -> float | None:
    """Cold-review F2 fallback: read `layer_height` from a process profile's
    JSON when the filename doesn't carry a prefix. Off-convention names
    (user-renamed files like 'my_quality_profile.json') still get correct
    height-tier scoring. Fail-soft on any read error — caller treats None
    as 'unknown height' (mid-tier rank)."""
    if not path_str:
        return None
    try:
        with open(path_str, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    v = data.get('layer_height')
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    if isinstance(v, list) and v:
        try:
            return float(v[0])
        except (ValueError, TypeError):
            return None
    return None


# Layer-height priority tier per nozzle. Lower index = higher priority. For
# 0.4 nozzle this puts 0.20 first, 0.16 second, then larger heights, then
# specialty extra-fine (0.06/0.08/0.10) last. 0.3 / 0.5 included with
# reasonable workhorse heights for printers outside Snapmaker's main four,
# even though Snapmaker's own machines only ship 0.2/0.4/0.6/0.8.
_NOZZLE_HEIGHT_PRIORITY: dict[str, tuple[float, ...]] = {
    '0.2': (0.06, 0.08, 0.10, 0.12, 0.16, 0.20),
    '0.3': (0.15, 0.12, 0.20, 0.10, 0.24, 0.08),
    '0.4': (0.20, 0.16, 0.24, 0.12, 0.28, 0.32, 0.08, 0.10, 0.06),
    '0.5': (0.25, 0.20, 0.30, 0.15, 0.35, 0.40),
    '0.6': (0.30, 0.24, 0.36, 0.20, 0.40, 0.16),
    '0.8': (0.40, 0.32, 0.48, 0.24),
}


def _layer_height_tier(label_or_opt, nozzle: str | None) -> int:
    """Rank a profile's layer height for the given nozzle. Lower = preferred.

    Cold-review F2: caller may pass an `opt` dict (preferred — enables the
    JSON layer_height fallback for off-convention names) or a bare label
    string (backward-compat). Falls through to mid-tier (50) when no height
    can be derived; unknown heights for a known nozzle get tier 100.

    Cold-review F4: nozzles outside the priority table get a generic
    workhorse score: closer to nozzle * 0.5 = preferred. Avoids the prior
    flat mid-tier-50-for-everything fallback that gave no useful ordering
    for, e.g., a 0.5 nozzle without a table entry."""
    if not nozzle:
        return 50
    if isinstance(label_or_opt, dict):
        label = label_or_opt.get('label', '')
        height = _extract_layer_height_mm(label)
        if height is None:
            height = _read_layer_height(label_or_opt.get('path'))
    else:
        height = _extract_layer_height_mm(label_or_opt)
    if height is None:
        return 50
    priorities = _NOZZLE_HEIGHT_PRIORITY.get(nozzle.strip().lower())
    if priorities is not None:
        for i, h in enumerate(priorities):
            if abs(h - height) < 1e-6:
                return i
        return 100  # unknown height for a known nozzle
    # Unknown nozzle: derive workhorse = nozzle * 0.5, rank by distance.
    try:
        target = float(nozzle) * 0.5
    except (TypeError, ValueError):
        return 50
    # Distance in 0.01mm units, clamped to [0, 99] so the table-driven
    # tiers (0-99) and the generic ones share a meaningful scale. `round`
    # not `int` so float-precision ties (abs(0.20-0.35) = 0.14999...) don't
    # asymmetrically truncate down — caught by the cold-review test pass.
    return min(99, max(0, round(abs(height - target) * 100)))


def list_profiles(profile_dir: Path | None = None,
                  class_hint: str | None = None,
                  sources: tuple[tuple[str, Path], ...] | None = None,
                  nozzle: str | None = None,
                  history_print_settings_id: str | None = None) -> list[dict]:
    """Return all process profiles from the configured sources.

    Backward-compat: `profile_dir`, if provided, is used as the sole source
    (legacy single-dir behavior, preserves tests that point at a fixture).
    Otherwise, defaults to scanning DEFAULT_SOURCES.

    `nozzle` (e.g. '0.4') filters out profiles whose label encodes a
    different nozzle size — the v1.5.0 snapmaker-stock fetch surfaces 217
    profiles across every nozzle, and a 0.4-nozzle user shouldn't have to
    wade through 0.2 / 0.6 / 0.8 options that don't apply.

    `history_print_settings_id` (e.g. 'Community 0.20 Strength Gyroid
    @Snapmaker U1 Textured PEI'): the user's most recently used preset on
    the chosen tool/nozzle, as read from print_history.json. When a profile
    in the picker's label matches this id, it gets `previously_used: True`
    and dominates the recommendation score — so the picker surfaces the
    operator's most recently working preset on top.
    """
    if profile_dir is not None:
        opts = _scan_dir('legacy', profile_dir)
    else:
        opts = []
        seen_values: set[str] = set()
        for label, source_dir in (sources or DEFAULT_SOURCES):
            for opt in _scan_dir(label, source_dir):
                # First source to define a value wins (priority order).
                if opt['value'] in seen_values:
                    continue
                seen_values.add(opt['value'])
                opts.append(opt)

    # Collapse profiles that resolve to the same name to one entry (from-printer
    # reprints, and a captured print matching a saved user/stock profile), with
    # the highest-priority source kept — the picker never shows a profile twice.
    _srcpri = {label: i for i, (label, _) in enumerate(sources or DEFAULT_SOURCES)}
    _srcpri['legacy'] = 0
    opts = _dedupe_by_name(opts, _srcpri)

    if nozzle:
        opts = [o for o in opts if _nozzle_matches(o['label'], nozzle)]

    # Mark previously-used profile (from print_history) BEFORE scoring so the
    # score function can prefer it. Label = file stem; history_id = the
    # print_settings_id from the gcode metadata. These don't always equal —
    # Live observation: operator renamed a user profile JSON to '0.20 Strength Gyroid.json' but
    # the file's print_settings_id is 'Community 0.20 Strength Gyroid
    # @Snapmaker U1 Textured PEI'.
    #
    # Match strategy (after cold review 2026-06-25): strip both label and
    # history_id of common decorations (Community/Hermes prefix, @printer
    # suffix), then compare the cores for EXACT equality. The pre-fix
    # substring-either-direction logic false-flagged any short label that
    # appeared as a substring of history_id — e.g. a profile literally named
    # 'Strength' would match 'Community 0.20 Strength Gyroid @Snapmaker U1
    # Textured PEI' and steal the recommendation. Core equality avoids that.
    history_id = (history_print_settings_id or '').strip().lower()
    if history_id:
        history_core = _strip_decorations(history_id)
        for o in opts:
            if _strip_decorations(o['label'].lower()) == history_core:
                o['previously_used'] = True

    # Score: (history_match, class_match, layer_height_tier). Lower wins.
    # history_match: 0 if this is the user's last-used preset, else 1
    # class_match: 0 if strength/cosmetic/etc keyword matches the hint, else 1
    # layer_height_tier: per-nozzle preference (workhorse heights first)
    hint = (class_hint or '').lower()

    def score(o: dict) -> tuple[int, int, int]:
        v = o['label'].lower()
        history_rank = 0 if o.get('previously_used') else 1
        class_rank = 1
        if any(w in hint for w in ['strength', 'bracket', 'holder', 'fixture', 'utility']) and 'strength' in v:
            class_rank = 0
        elif any(w in hint for w in ['cosmetic', 'pretty', 'fine']) and ('optimal' in v or 'fine' in v):
            class_rank = 0
        height_rank = _layer_height_tier(o, nozzle)
        return (history_rank, class_rank, height_rank)

    if opts:
        # Sort the full list by score so callers that take a prefix slice
        # (workflow's prof_opts[:8]) get the most relevant entries first.
        # Tie-breaker: source priority, then label alphabetical.
        source_priority = {label: i for i, (label, _) in enumerate(sources or DEFAULT_SOURCES)}
        source_priority['legacy'] = 0
        opts.sort(key=lambda o: (score(o), source_priority.get(o['source'], 99), o['label']))
        opts[0]['recommended'] = True
    return opts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--class-hint')
    ap.add_argument('--nozzle', default=None, help="Filter to profiles matching this nozzle size (e.g. '0.4').")
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args(argv)
    opts = list_profiles(class_hint=a.class_hint, nozzle=a.nozzle)
    if a.json:
        print(json.dumps(opts, indent=2))
    else:
        for o in opts:
            tag = ' [supports]' if o.get('has_supports') else ''
            print(f"  [{o['source']:<14}] {o['label']}{tag}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
