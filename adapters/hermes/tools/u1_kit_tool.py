#!/usr/bin/env python3
"""
u1_kit Tool — slice a multi-part 3D-print kit (.zip of STLs) for Snapmaker U1.

The OUTER tool for kit prints. The LLM picks this ONCE when the operator
supplies a kit zip; everything downstream — form rendering, answer
collection, re-invocation, readiness card — happens deterministically inside
this handler.

This is the deterministic-dispatch counterpart to ``tools/terminal_tool.py``
calling ``approval_callback`` when ``detect_dangerous_command`` fires. The
LLM doesn't have to pick ``form`` or relay the kit_form event — the u1_kit
handler does it itself, via the same ``form_gateway`` session-keyed notify
mechanism the rest of Hermes uses (see adapters/hermes/README.md for the
verified 8-piece pattern).

Flow:
  1. LLM calls ``u1_kit(model_path="...")``.
  2. Handler invokes ``u1_kit_workflow.py --json-events`` as a subprocess.
  3. Parses the ``kit_form`` event (need_input) from its stdout, extracts
     ``form_schema`` + ``request_id``.
  4. Calls ``form_gateway.invoke_form(session_key, form_schema)`` — blocks
     until the operator submits via Telegram inline buttons (or the text
     fallback). The platform send + user-tap routing all happen via the
     monkey-patched ``TelegramAdapter.send_form`` / ``_handle_callback_query``.
  5. Re-invokes the workflow with ``--form-answers-json <answer>
     --request-id <id>``.
  6. Returns the ``kit_readiness_card`` (request_id, plate count, gated
     plate, Stage 1 command) to the LLM.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from gateway.session_context import get_session_env  # type: ignore
from tools import form_gateway  # type: ignore

logger = logging.getLogger(__name__)

# Path to the kit workflow on the runtime. KEEP IN SYNC with the chain in
# tools/hermes_hooks/u1_confirm_start/handler.py (_scripts_dir): explicit
# env > $HERMES_HOME/scripts (probed) > the Linux deploy default.
def _default_workflow_script() -> str:
    explicit = os.environ.get("U1_KIT_WORKFLOW", "").strip()
    if explicit:
        return explicit
    scripts = os.environ.get("U1_RUNTIME_SCRIPTS_DIR", "").strip()
    if scripts:
        return str(Path(scripts) / "u1_kit_workflow.py")
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    if hermes_home:
        cand = Path(hermes_home) / "scripts" / "u1_kit_workflow.py"
        if cand.is_file():
            return str(cand)
    return "/opt/data/scripts/u1_kit_workflow.py"


DEFAULT_WORKFLOW_SCRIPT = _default_workflow_script()
# sys.executable, not a bare python3: this tool runs inside the gateway
# interpreter (guaranteed present); stock native Windows has no python3.
DEFAULT_PYTHON = os.environ.get("U1_KIT_PYTHON", "").strip() or sys.executable

# 25 min covers a slow multi-plate slice; analysis phase is seconds.
SUBPROCESS_TIMEOUT_SEC = int(os.environ.get("U1_KIT_TIMEOUT_SEC", "1500"))


def _run_workflow(args: List[str], *, timeout: float) -> Dict[str, Any]:
    """Run u1_kit_workflow.py with --json-events; collect events + stderr.

    The workflow exits naturally at phase boundaries (after kit_form in the
    analyze phase, after kit_readiness_card in the slice phase), so a simple
    communicate() is enough — no need to stream while it runs.
    """
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"_error": f"u1_kit_workflow timed out after {timeout:.0f}s"}
    except FileNotFoundError as exc:
        return {"_error": f"u1_kit_workflow not found: {exc}"}

    events: List[Dict[str, Any]] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
            if isinstance(ev, dict):
                events.append(ev)
        except json.JSONDecodeError:
            # Workflow occasionally emits a non-event indented summary line.
            pass
    return {
        "returncode": proc.returncode,
        "events": events,
        "stderr": (proc.stderr or "").strip(),
    }


def _find_event(events: List[Dict[str, Any]], stage: str,
                key: Optional[str] = None) -> Optional[Dict[str, Any]]:
    for ev in events:
        if ev.get("stage") != stage:
            continue
        if key is not None and ev.get("key") != key:
            continue
        return ev
    return None


def u1_kit_tool(
    model_path: str,
    *,
    request_id: Optional[str] = None,
    workflow_script: str = DEFAULT_WORKFLOW_SCRIPT,
    python: str = DEFAULT_PYTHON,
    timeout: float = SUBPROCESS_TIMEOUT_SEC,
) -> str:
    """LLM-facing handler — returns a JSON string with phase + payload."""
    if not model_path or not isinstance(model_path, str):
        return json.dumps({"error": "model_path is required"}, ensure_ascii=False)
    path = Path(model_path).expanduser()
    try:
        path = path.resolve()
    except OSError as exc:
        return json.dumps({"error": f"bad model_path: {exc}"}, ensure_ascii=False)
    if not path.exists():
        return json.dumps({"error": f"model not found: {path}"}, ensure_ascii=False)

    session_key = get_session_env("HERMES_SESSION_KEY", "")
    if not session_key:
        return json.dumps(
            {"error": "u1_kit requires a gateway session (no session_key in context)."},
            ensure_ascii=False)
    if form_gateway.get_notify(session_key) is None:
        return json.dumps(
            {"error": "form notify callback not registered for this session — "
                      "run adapters/hermes/install.py to wire the patch, then restart Hermes."},
            ensure_ascii=False)

    # --- Phase 1: analyze + emit kit_form -----------------------------------
    cmd1 = [python, workflow_script, str(path), "--json-events"]
    if request_id:
        cmd1 += ["--request-id", request_id]
    logger.info("u1_kit phase 1: %s",
                " ".join(shlex.quote(a) for a in cmd1))
    res1 = _run_workflow(cmd1, timeout=timeout)
    if "_error" in res1:
        return json.dumps({"error": res1["_error"], "phase": "analysis"},
                          ensure_ascii=False)
    if res1.get("returncode", 0) != 0:
        return json.dumps({
            "phase": "analysis",
            "error": f"u1_kit_workflow analysis failed (rc={res1['returncode']})",
            "stderr": res1.get("stderr", "")[:2000],
        }, ensure_ascii=False)

    setup_required = _find_event(res1["events"], "setup_required")
    if setup_required:
        return json.dumps({
            "phase": "setup_required",
            "kind": setup_required.get("kind"),
            "message": setup_required.get("message"),
        }, ensure_ascii=False)

    kit_form = _find_event(res1["events"], "need_input", key="kit_form")
    if kit_form is None:
        # No form needed — maybe a single-STL happy path or analysis-failed.
        readiness = _find_event(res1["events"], "kit_readiness_card")
        if readiness:
            return json.dumps({"phase": "ready", "readiness_card": readiness},
                              ensure_ascii=False)
        return json.dumps({
            "phase": "analysis",
            "error": "no kit_form event emitted (and no readiness card)",
            "events_tail": res1["events"][-5:],
        }, ensure_ascii=False)

    form_schema = kit_form.get("form_schema") or {}
    wf_request_id = kit_form.get("request_id") or request_id
    if not isinstance(form_schema, dict) or not form_schema.get("fields"):
        return json.dumps({
            "phase": "analysis",
            "error": "kit_form event missing valid form_schema",
            "kit_form": kit_form,
        }, ensure_ascii=False)

    # --- Phase 2: render the form natively, block on operator submit -------
    answer = form_gateway.invoke_form(session_key, form_schema)
    if not isinstance(answer, dict):
        return json.dumps({
            "phase": "form",
            "error": "form_gateway.invoke_form returned non-dict",
            "got": str(answer)[:200],
            "request_id": wf_request_id,
        }, ensure_ascii=False)
    if answer.get("_error"):
        return json.dumps({"phase": "form", "error": answer["_error"],
                           "request_id": wf_request_id}, ensure_ascii=False)
    if answer.get("_cancelled"):
        return json.dumps({"phase": "cancelled", "request_id": wf_request_id},
                          ensure_ascii=False)
    if answer.get("_timeout"):
        return json.dumps({"phase": "form_timeout", "request_id": wf_request_id},
                          ensure_ascii=False)

    # --- Phase 3: re-invoke workflow with the answer ------------------------
    cmd2 = [
        python, workflow_script, str(path), "--json-events",
        "--request-id", str(wf_request_id),
        "--form-answers-json", json.dumps(answer, ensure_ascii=False),
    ]
    logger.info("u1_kit phase 3: %s",
                " ".join(shlex.quote(a) for a in cmd2))
    res2 = _run_workflow(cmd2, timeout=timeout)
    if "_error" in res2:
        return json.dumps({"error": res2["_error"], "phase": "slice",
                           "request_id": wf_request_id}, ensure_ascii=False)
    if res2.get("returncode", 0) != 0:
        return json.dumps({
            "phase": "slice",
            "error": f"u1_kit_workflow slice failed (rc={res2['returncode']})",
            "stderr": res2.get("stderr", "")[:2000],
            "request_id": wf_request_id,
        }, ensure_ascii=False)

    rejected = _find_event(res2["events"], "form_rejected")
    if rejected:
        return json.dumps({
            "phase": "form_rejected",
            "request_id": wf_request_id,
            "errors": rejected.get("errors", []),
            "user_message": ("Form answer didn't validate. Re-call u1_kit "
                             "(same request_id will resume) to try again."),
        }, ensure_ascii=False)

    readiness = _find_event(res2["events"], "kit_readiness_card")
    if readiness is None:
        return json.dumps({
            "phase": "slice",
            "error": "no readiness card emitted",
            "request_id": wf_request_id,
            "events_tail": res2["events"][-5:],
        }, ensure_ascii=False)

    return json.dumps({
        "phase": "ready",
        "request_id": wf_request_id,
        "readiness_card": readiness,
        "user_answer": answer,
    }, ensure_ascii=False)


def check_u1_kit_requirements() -> bool:
    """u1_kit needs the workflow script at the expected deploy path."""
    return Path(DEFAULT_WORKFLOW_SCRIPT).exists()


# =============================================================================
# Function-calling tool schema
# =============================================================================

U1_KIT_SCHEMA = {
    "name": "u1_kit",
    "description": (
        "Slice a multi-part 3D PRINT KIT (a .zip of STLs intended to print "
        "together) for the Snapmaker U1 3D printer.\n\n"
        "Call this ONCE when the operator supplies a kit zip — everything "
        "after (form rendering with native inline buttons, operator answer "
        "collection, slice, readiness card) happens deterministically inside "
        "this tool. The native form is shown automatically; do NOT also "
        "call the `form` tool yourself.\n\n"
        "Returns a JSON object with `phase` and (on success) a "
        "`readiness_card` containing the request_id, plate count, gated "
        "plate filename, and the Stage-1 photo-gate command for plate 1."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "model_path": {
                "type": "string",
                "description": (
                    "Absolute path to the kit .zip on the Hermes filesystem "
                    "(e.g. an attachment saved to /tmp/parts.zip)."
                ),
            },
            "request_id": {
                "type": "string",
                "description": (
                    "Optional explicit request id to resume a prior kit "
                    "session. Usually omit — the workflow content-hashes "
                    "the archive bytes to resume automatically."
                ),
            },
        },
        "required": ["model_path"],
    },
}


# --- Registry ----------------------------------------------------------------
from tools.registry import registry  # type: ignore

registry.register(
    name="u1_kit",
    toolset="u1_kit",
    schema=U1_KIT_SCHEMA,
    handler=lambda args, **_kw: u1_kit_tool(
        model_path=args.get("model_path", ""),
        request_id=args.get("request_id") or None,
    ),
    check_fn=check_u1_kit_requirements,
    emoji="🖨️",
)
