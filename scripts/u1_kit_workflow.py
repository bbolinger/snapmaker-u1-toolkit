#!/usr/bin/env python3
"""Multi-part kit workflow orchestrator — v2.1.0 (Option 2: separate seam).

Drives the kit path end-to-end WITHOUT touching the single-STL workflow:
  ingest (u1_kit) -> consolidated form (u1_form) -> arrange-slice (u1_arrange)
  -> upload all plates -> kit readiness card -> gate PLATE 1 through the
  existing Stage 1/2 moat. Plates 2..N are upload-only; the operator starts
  them from the Snapmaker app.

Why a separate orchestrator (not threaded through run_workflow): the single-STL
flow is the hardened v2.0 path; keeping it untouched means every v2.0 test stays
green by construction, and a kit handler is exactly the shape the eventual
gate-detecting router (plan §0) dispatches to. Shared logic is REUSED by import
(_real_upload, list_profiles, query_material_options, write_request, the gate
command builder), not duplicated.

Gate-detection principle: the script owns the state machine. The model relays
the operator's verbatim form line into --form-answers; this orchestrator parses
+ validates it (via u1_form) and emits exactly one next action. The model never
decides anything.

Two triggers, no session growth:
  1. operator answers the form  -> slice + upload + readiness photo (Stage 1)
  2. operator approves the photo -> start plate 1 (Stage 2, existing gate)
"""
from __future__ import annotations

# Bootstrap: env check happens BEFORE the heavy numpy/PIL-dependent imports
# below (via u1_kit -> u1_orient -> numpy). Mirrors u1_slice_workflow's
# _ensure_compat_python so the kit workflow is just as robust when invoked
# via a `python3` that lacks deps (e.g. Hermes' /usr/bin/python3). Without
# this, calling `python3 u1_kit_workflow.py ...` fails on the numpy import.
import os, sys, subprocess
from pathlib import Path


def _check_python_has_deps(python_path: str, deps: tuple = ("numpy", "PIL")) -> bool:
    try:
        proc = subprocess.run(
            [python_path, "-c", f"import {', '.join(deps)}"],
            capture_output=True, text=True, timeout=10,
        )
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _ensure_compat_python() -> None:
    try:
        import numpy  # noqa: F401
        import PIL    # noqa: F401
        return
    except ImportError:
        pass
    missing = []
    for dep in ("numpy", "PIL"):
        try:
            __import__(dep)
        except ImportError:
            missing.append("pillow" if dep == "PIL" else dep)
    here = Path(__file__).resolve().parent
    root = here.parent
    candidates: list[str] = []
    env_override = os.environ.get("U1_TOOLKIT_PYTHON")
    if env_override:
        candidates.append(env_override)
    candidates.extend([
        "/opt/hermes/.venv/bin/python",
        str(root / "venv" / "bin" / "python"),
        str(root / ".venv" / "bin" / "python"),
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
    ])
    for cand in candidates:
        if not Path(cand).exists():
            continue
        if _check_python_has_deps(cand):
            print(f"[env] current python lacks {', '.join(missing)}; switching to {cand}",
                  file=sys.stderr)
            os.execv(cand, [cand, __file__, *sys.argv[1:]])
    msg = [
        "ERROR: u1_kit_workflow.py needs numpy + PIL (Pillow).",
        f"Missing on the current interpreter ({sys.executable}): {', '.join(missing)}",
        "",
        "Tried these alternative Python interpreters (none had the deps):",
    ]
    for c in candidates:
        msg.append(f"  - {c}  ({'exists' if Path(c).exists() else 'not found'})")
    msg += [
        "",
        "Fix one of these:",
        f"  1. {sys.executable} -m pip install numpy pillow",
        "  2. export U1_TOOLKIT_PYTHON=/path/to/python",
    ]
    print("\n".join(msg), file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    _ensure_compat_python()

# === Heavy imports (numpy/PIL via u1_kit -> u1_orient) ===
import json
from typing import Any

import u1_kit
import u1_form
import u1_arrange
import u1_request
from u1_print_start_gate import build_stage1_command
from u1_slice_workflow import (
    _resolve_operator,
    _shell_quote,
    _real_upload,
    list_profiles,
    profile_path,
    apply_supports_override,
    _tool_to_index,
)

DEFAULT_TOOLS = ["T0", "T1", "T2", "T3"]
DEFAULT_MATERIALS = ["PLA", "PETG", "ABS", "TPU", "ASA", "PLA-CF", "PETG-CF"]
# Maps the form's supports vocabulary to the slice override vocabulary.
_SUPPORTS_TO_OVERRIDE = {"supports": "supports", "no-supports": "no_supports", "overhangs": "overhangs"}


def _emit(events_file: Path | None, obj: dict[str, Any], json_events: bool) -> None:
    """Emit one event to stdout + mirror to events.jsonl (local, no globals)."""
    if json_events:
        print(json.dumps(obj), flush=True)
    else:
        stage = obj.get("stage", "event")
        print(f"[{stage}] " + ", ".join(f"{k}={v}" for k, v in obj.items() if k != "stage"))
    if events_file is not None:
        try:
            with events_file.open("a") as f:
                f.write(json.dumps(obj, default=str) + "\n")
        except Exception:
            pass


def _audit(request_id: str, event: str, operator: str, **details: Any):
    try:
        import u1_audit
        return u1_audit.append(request_id, event, operator=operator, **details)
    except Exception:
        return None


def _build_form_spec(kit: dict[str, Any], nozzle: str,
                     persisted_profiles: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Assemble the u1_form spec from analysis (parts + offered options).

    ``profile N`` is resolved by INDEX, and form-emit / answer-parse happen in
    SEPARATE invocations. To keep index N stable even if print-history or the
    on-disk profiles change between the two calls, the form's profile list is
    persisted at emit time and replayed here on the answer call
    (``persisted_profiles``). Without that, a recommendation re-sort between
    calls could silently shift what ``profile 2`` means. Verified 2026-06-28:
    list_profiles order changes when history_print_settings_id changes.
    """
    if persisted_profiles:
        profiles_full = [
            {"idx": int(p["idx"]), "value": p["value"], "label": p.get("label", p["value"])}
            for p in persisted_profiles
        ]
    else:
        prof_opts = list_profiles(nozzle=nozzle)
        profiles_full = [
            {"idx": i + 1, "value": o["value"], "label": o.get("label", o["value"])}
            for i, o in enumerate(prof_opts)
        ]
    parts = [
        {"id": p["part_id"], "label": f"{p['filename']} ({p['footprint_mm'][0]:.0f}x{p['footprint_mm'][1]:.0f}mm)"}
        for p in kit["parts"]
    ]
    return {
        "parts": parts,
        "tools": DEFAULT_TOOLS,
        "materials": DEFAULT_MATERIALS,
        "profiles": [{"idx": p["idx"], "label": p["label"]} for p in profiles_full],
        "supports": ["supports", "no-supports", "overhangs"],
        "actions": ["start", "upload-only"],
        "_prof_opts": [{"value": p["value"]} for p in profiles_full],  # idx -> resolution
        "_profiles_full": profiles_full,  # persisted at form-emit for index stability
    }


def run_kit_workflow(args) -> dict[str, Any]:
    """Orchestrate the kit path. See module docstring for the two-trigger flow."""
    operator = _resolve_operator(args)
    archive = Path(args.model).resolve()
    json_events = bool(getattr(args, "json_events", False))

    # --- request id (content-hash recovery on the archive bytes) ---
    request_id, was_resumed = u1_request.resolve_request_id(
        cli_request_id=getattr(args, "request_id", None),
        cli_fresh=bool(getattr(args, "fresh", False)),
        stl=archive,
    )
    out_dir = Path(args.out_dir) if getattr(args, "out_dir", None) else u1_request.ensure_request_dir(request_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    events_file = out_dir / "events.jsonl"

    # --- ANALYSIS: ingest the kit ---
    parts_dir = out_dir / "parts"
    stls = u1_kit.extract_all_stls(archive, parts_dir)
    kit = u1_kit.build_kit(stls)
    _emit(events_file, {
        "stage": "kit_ingested",
        "request_id": request_id,
        "part_count": kit["part_count"],
        "multi": kit["multi"],
        "oversized_part_ids": kit["oversized_part_ids"],
    }, json_events)
    _audit(request_id, "kit_ingested", operator,
           part_count=kit["part_count"], oversized=kit["oversized_part_ids"])

    if kit["oversized_part_ids"]:
        _emit(events_file, {
            "stage": "warning", "kind": "oversized_parts",
            "message": (f"Parts too big for the bed even rotated: {kit['oversized_part_ids']}. "
                        "Deselect them or split the model; the slice will fail otherwise."),
        }, json_events)

    # Reuse the profile list persisted at form-emit so `profile N` means the
    # same thing on the answer call (index stability — see _build_form_spec).
    existing = u1_request.read_request(request_id) or {}
    spec = _build_form_spec(kit, getattr(args, "nozzle", "0.4"),
                            persisted_profiles=existing.get("form_profiles"))
    if not spec["profiles"]:
        _emit(events_file, {"stage": "setup_required", "kind": "no_profiles",
                            "message": "No profiles found. Run tools/fetch_snapmaker_profiles.py."}, json_events)
        return {"phase": "setup_required", "request_id": request_id, "out_dir": str(out_dir)}

    # Persist the kit record (additive fields; no schema bump). model_hash is
    # the recovery key — without it, re-sending the same zip (no --request-id)
    # can't resume this request (find_recent_request_for_model matches on it).
    u1_request.write_request(
        request_id, model_file=archive.name, model_path=str(archive),
        model_hash=u1_request.compute_model_hash(archive) if archive.exists() else None,
        out_dir=str(out_dir), operator=operator,
        kit={"parts": kit["parts"], "part_count": kit["part_count"]},
        form_profiles=spec["_profiles_full"],
        phase="kit_analysis",
    )

    # --- DECISION: emit the form, or parse the relayed answer ---
    answers = getattr(args, "form_answers", None)
    answers_json = getattr(args, "form_answers_json", None)
    if not answers and not answers_json:
        submit = {
            "text": (f"python3 /opt/data/scripts/u1_kit_workflow.py {_shell_quote(str(archive))} "
                     f"--json-events --request-id {request_id} --form-answers '<operator answer line>'"),
            "json": (f"python3 /opt/data/scripts/u1_kit_workflow.py {_shell_quote(str(archive))} "
                     f"--json-events --request-id {request_id} --form-answers-json '<json>'"),
        }
        schema = u1_form.build_form_schema(spec, submit=submit)
        event: dict[str, Any] = {
            "stage": "need_input", "key": "kit_form", "request_id": request_id,
            "form": schema["text_fallback"],   # back-compat: text form stays present
            "form_schema": schema,             # form-protocol: declarative schema for native renderers
            "next_command": submit["text"],
            "instruction": ("Show the operator this form. Relay their reply VERBATIM into "
                            "--form-answers (one quoted line) — OR, if you rendered form_schema "
                            "as a native control, submit the result via --form-answers-json. "
                            "Do not interpret the answer yourself; the script parses it."),
        }
        # Sidecar deep link (Level 3 escape hatch — adapters/telegram/sidecar.py).
        # If a sidecar bot is configured, emit a tappable t.me link so the
        # operator can switch to the sidecar DM and fill the form with native
        # inline keyboards. The operator returns to the main chat for the
        # Stage-1 photo gate; safety pipeline is identical to the typed path.
        sidecar_bot = os.environ.get("U1_SIDECAR_BOT_USERNAME", "").strip().lstrip("@")
        if sidecar_bot:
            event["form_url"] = f"https://t.me/{sidecar_bot}?start={request_id}"
            event["instruction"] += (
                f"\n\nALSO: a tappable form is available — post this URL in chat and "
                f"the operator can tap to fill the form with buttons in @{sidecar_bot}: "
                f"{event['form_url']}"
            )
        _emit(events_file, event, json_events)
        return {"phase": "awaiting_form", "request_id": request_id, "out_dir": str(out_dir)}

    if answers_json:
        try:
            obj = json.loads(answers_json) if isinstance(answers_json, str) else answers_json
        except (ValueError, TypeError) as exc:
            _emit(events_file, {"stage": "form_rejected", "key": "kit_form",
                                "request_id": request_id, "errors": [f"invalid --form-answers-json: {exc}"]},
                  json_events)
            return {"phase": "form_rejected", "request_id": request_id,
                    "errors": [f"invalid --form-answers-json: {exc}"]}
        parsed = u1_form.parse_answers_json(obj, spec)
    else:
        parsed = u1_form.parse_answers(answers, spec)
    if not parsed["ok"]:
        _emit(events_file, {
            "stage": "form_rejected", "key": "kit_form", "request_id": request_id,
            "errors": parsed["errors"], "form": u1_form.build_form(spec),
            "instruction": "The answer didn't validate. Show the errors + form and ask the operator again.",
        }, json_events)
        return {"phase": "form_rejected", "request_id": request_id, "errors": parsed["errors"]}

    values = parsed["values"]
    _emit(events_file, {"stage": "form_accepted", "request_id": request_id,
                        "parsed": u1_form.echo_parse(values, spec)}, json_events)

    # --- COMMIT: arrange-slice -> upload all -> readiness (gate plate 1) ---
    return _commit_kit(args, request_id, operator, out_dir, events_file, archive, kit, spec, values)


def _commit_kit(args, request_id, operator, out_dir, events_file, archive, kit, spec, values) -> dict[str, Any]:
    json_events = bool(getattr(args, "json_events", False))
    nozzle = getattr(args, "nozzle", "0.4")

    # Resolve selected parts (1-based indices into kit['parts']).
    sel_idx = values.get("parts") or list(range(1, kit["part_count"] + 1))
    selected = [kit["parts"][i - 1] for i in sel_idx]
    selected_paths = [p["path"] for p in selected]

    tool = values["tool"]
    material = values["material"]
    auto_orient = values.get("orient") == "auto"

    # Resolve profile (idx or name already validated by the parser).
    prof = values["profile"]
    prof_opts = spec["_prof_opts"]
    prof_idx = int(prof.get("idx", 1))
    profile_slug = prof_opts[prof_idx - 1]["value"]
    process = profile_path(profile_slug)

    # Supports override (same temp-profile mechanism as the single path).
    supports = values.get("supports", "no-supports")
    override = _SUPPORTS_TO_OVERRIDE.get(supports, "no_supports")
    if override in ("supports", "no_supports"):
        process = apply_supports_override(process, override == "supports", out_dir)

    slice_out = out_dir / "slice"
    _emit(events_file, {"stage": "kit_slicing", "request_id": request_id,
                        "parts": len(selected_paths), "auto_orient": auto_orient}, json_events)
    try:
        arr = u1_arrange.arrange_slice(
            selected_paths, slice_out,
            tool=tool, material=material, profile=profile_slug, nozzle=nozzle,
            auto_orient=auto_orient, allow_rotations=True,
            process_path_override=process,
        )
    except Exception as exc:
        # Surface a clean event instead of a stack trace — e.g. an oversized
        # part Orca refuses to arrange, or a slicer error. The operator can
        # deselect parts and re-answer the form.
        _emit(events_file, {
            "stage": "kit_slice_failed", "request_id": request_id,
            "error": str(exc)[:600],
            "instruction": "Slice failed. If a part is too big, deselect it and re-answer the form.",
        }, json_events)
        _audit(request_id, "kit_slice_failed", operator, error=str(exc)[:300])
        return {"phase": "slice_failed", "request_id": request_id, "error": str(exc)[:600]}
    _emit(events_file, {"stage": "kit_sliced", "request_id": request_id,
                        "plate_count": arr["plate_count"]}, json_events)
    _audit(request_id, "kit_sliced", operator, plate_count=arr["plate_count"],
           parts=len(selected_paths), tool=tool, material=material, profile=profile_slug)

    # Name + upload every plate. Plate 1 is the gated one.
    kit_stem = u1_kit._sanitize(archive.stem)
    live = bool(getattr(args, "live_upload", False))
    plates_state: list[dict[str, Any]] = []
    for pl in arr["plates"]:
        idx = pl["plate_idx"]
        src = Path(pl["gcode_path"])
        named = src.with_name(f"{kit_stem}_plate{idx}.gcode")
        if named != src:
            src.replace(named)
        up = _real_upload(named, on_collision=getattr(args, "on_collision", None)) if live else {
            "dry_run": True, "uploaded_filename": named.name, "moonraker_upload_ok": None}
        plates_state.append({
            "plate_idx": idx,
            "gcode_path": str(named),
            "gcode_hash": pl["gcode_hash"],
            "printer_storage_filename": up.get("uploaded_filename") or named.name,
            "uploaded": up,
            "started": False,
        })
    _emit(events_file, {"stage": "kit_uploaded", "request_id": request_id,
                        "plates": [p["printer_storage_filename"] for p in plates_state],
                        "live": live}, json_events)

    # Plate 1 is gated through the existing moat: bind top-level gcode_hash to it.
    plate1 = plates_state[0]
    # Toolhead naming MUST match the single workflow (u1_slice_workflow ~2264):
    # T0 -> 'extruder', T1 -> 'extruder1', T2 -> 'extruder2', T3 -> 'extruder3'.
    # This string drives the gate's tool-match safety check — a mismatch heats
    # the wrong toolhead. Verified against the shipped v2.0 mapping 2026-06-28.
    _tidx = _tool_to_index(tool)
    extruder = "extruder" if _tidx == 0 else f"extruder{_tidx}"
    stage1_cmd = build_stage1_command(
        printer_filename=plate1["printer_storage_filename"],
        intended_tool=extruder, material=material, request_id=request_id,
    )

    action = values.get("action", "start")
    readiness = {
        "stage": "kit_readiness_card",
        "request_id": request_id,
        "part_count": kit["part_count"],
        "selected_parts": [p["part_id"] for p in selected],
        "plate_count": len(plates_state),
        "plates": [{"plate_idx": p["plate_idx"],
                    "printer_storage_filename": p["printer_storage_filename"],
                    "gcode_hash": p["gcode_hash"]} for p in plates_state],
        "tool": tool, "material": material, "profile": profile_slug,
        "orient": values.get("orient"), "supports": supports,
        "parsed_echo": u1_form.echo_parse(values, spec),
        "gated_plate": plate1["printer_storage_filename"],
        "start_gate_stage1_command": stage1_cmd,
        "operator_guidance": (
            f"{len(plates_state)} plate(s). Stage 1 gates ONLY plate 1 "
            f"({plate1['printer_storage_filename']}). After it prints, start plates "
            f"2..{len(plates_state)} from the Snapmaker app — they're already uploaded."
            if len(plates_state) > 1 else
            "Single plate. Stage 1 captures the bed photo + approval token."
        ),
    }
    _emit(events_file, readiness, json_events)

    persist_phase = "awaiting_start_approval" if action == "start" else "complete"
    next_action = None
    if action == "start":
        next_action = {
            "stage": "next_action_required",
            "reason": "Run Stage 1 to capture a real bed photo + approval token for plate 1.",
            "command": stage1_cmd,
        }
        _emit(events_file, next_action, json_events)
    else:
        _emit(events_file, {"stage": "complete", "request_id": request_id,
                            "reason": "Upload-only: all plates on the printer; start from the Snapmaker app."}, json_events)

    u1_request.write_request(
        request_id,
        phase=persist_phase,
        kit={"parts": kit["parts"], "part_count": kit["part_count"],
             "selected": [p["part_id"] for p in selected], "orient_mode": values.get("orient")},
        plates=plates_state,
        tool=tool, material=material, profile=profile_slug, supports=override,
        gcode_hash=plate1["gcode_hash"],
        printer_storage_filename=plate1["printer_storage_filename"],
        start_gate_stage1_command=stage1_cmd,
        readiness_card_event=readiness,
        next_action_required_event=next_action,
    )
    _audit(request_id, "kit_readiness_card_emitted", operator,
           plate_count=len(plates_state), gated_plate=plate1["printer_storage_filename"],
           gcode_hash=plate1["gcode_hash"], request_revision=(u1_request.read_request(request_id) or {}).get("request_revision", 1))

    return {
        "phase": persist_phase, "request_id": request_id, "out_dir": str(out_dir),
        "plate_count": len(plates_state),
        "gated_plate": plate1["printer_storage_filename"],
        "start_gate_stage1_command": stage1_cmd,
    }


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Multi-part kit slice workflow (Snapmaker U1)")
    ap.add_argument("model", help="zip of STLs (a kit) or a single model file")
    ap.add_argument("--json-events", action="store_true")
    ap.add_argument("--form-answers", default=None, help="operator's one-line answer, relayed verbatim")
    ap.add_argument("--form-answers-json", default=None,
                    help="structured answer (JSON) from a native-widget gateway; same validation as --form-answers")
    ap.add_argument("--request-id", default=None)
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--operator", default=None)
    ap.add_argument("--nozzle", default="0.4")
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--live-upload", action="store_true")
    ap.add_argument("--on-collision", choices=["rename", "overwrite", "cancel"], default=None)
    a = ap.parse_args(argv)
    res = run_kit_workflow(a)
    if not a.json_events:
        print(json.dumps(res, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
