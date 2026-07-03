"""Pre-print review document — the operator's flight plan (v2.2).

Generates ``requests/<id>/review.md`` before the start gate so the operator
can read exactly what is about to print — in a medium a human actually
reads — and say yes with confidence.

Three rules make this a trust artifact instead of trust theater:

1. **Ground truth, not intent.** Key settings come from the config block
   OrcaSlicer writes into the sliced gcode — what the printer will
   execute — never from the flags the workflow meant to pass. A doc
   sourced from intent can lie; one sourced from the output file can't.
2. **Curated, not dumped.** ~12 settings that decide whether a print
   succeeds, plus an explicit "operator decisions & overrides" section.
   Three hundred rows create false confidence; nobody reads them.
3. **Bound to the moat, not a new gate.** The doc header carries
   ``request_id`` + ``request_revision`` + plate-1 ``gcode_hash`` and an
   audit row records the doc's own sha256 — ``can_start()``'s existing
   drift check then guarantees the document reviewed describes the plan
   that prints. Reading it is NOT enforced: the yes/no flow is unchanged,
   and generation failures must never block a print (callers wrap
   ``generate()`` fail-soft).
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Curated settings, in display order. Keys are Orca config-block names;
# several have per-printer synonyms, listed as fallbacks. Values render raw
# (Orca emits per-filament lists like "220,220" — showing them un-mangled
# beats guessing which slot applies).
_KEY_SETTINGS: list[tuple[str, list[str]]] = [
    ("Layer height (mm)", ["layer_height"]),
    ("Wall loops", ["wall_loops"]),
    ("Infill density", ["sparse_infill_density"]),
    ("Infill pattern", ["sparse_infill_pattern"]),
    ("Nozzle temp (°C)", ["nozzle_temperature"]),
    ("Nozzle temp, first layer (°C)", ["nozzle_temperature_initial_layer"]),
    ("Bed temp (°C)", ["textured_plate_temp", "hot_plate_temp",
                       "bed_temperature"]),
    ("Bed temp, first layer (°C)", ["textured_plate_temp_initial_layer",
                                    "hot_plate_temp_initial_layer",
                                    "first_layer_bed_temperature"]),
    ("Supports enabled", ["enable_support"]),
    ("Support type", ["support_type"]),
    ("Brim", ["brim_type"]),
    ("Outer wall speed (mm/s)", ["outer_wall_speed"]),
    ("Seam position", ["seam_position"]),
    ("Fuzzy skin", ["fuzzy_skin"]),
]

_CONFIG_LINE_RE = re.compile(r"^;\s*([A-Za-z0-9_ \[\]()]+?)\s*=\s*(.*)$")


def _num_canon(x: Any) -> str:
    """Canonicalize a numeric token so equal numbers compare equal:
    ``0.20`` → ``0.2``, ``1.0`` → ``1``, ``1`` → ``1``. Non-numeric tokens
    pass through unchanged. Without this the deviation sweep flagged
    ``0.2`` vs ``0.20`` and ``1`` vs ``1.0`` as differences (operator
    feedback 2026-07-02: the review doc filled with false ⚠ noise)."""
    s = str(x).strip()
    try:
        f = float(s)
    except (ValueError, TypeError):
        return s
    if f == int(f):
        return str(int(f))
    return ("%.6f" % f).rstrip("0").rstrip(".")


def _norm(v: Any) -> str:
    """Normalize a profile/config value for comparison. Profiles store
    per-filament lists (["240"] / ["240","240"]); gcode config emits the
    joined form ("240,240"). Collapse both to a canonical string, and
    canonicalize numeric tokens (0.20≡0.2, 1.0≡1) so equal values don't
    read as deviations."""
    if isinstance(v, list):
        parts = [_num_canon(x) for x in v]
    else:
        v = str(v).strip()
        parts = [_num_canon(x) for x in v.split(",")] if "," in v else [_num_canon(v)]
    if parts and all(x == parts[0] for x in parts):
        return parts[0]
    return ",".join(parts)


def build_reference(profile_slug: str | None, material: str | None,
                    nozzle: str = "0.4",
                    out_dir: Path | None = None) -> dict[str, str]:
    """Resolve the CHOSEN preset's values (process + filament, inheritance
    flattened) so the settings table can flag where the gcode deviates —
    the settings people tweak (temps, supports) are exactly where distrust
    concentrates, so a tweaked value gets a visible marker with the preset's
    own number next to it.

    Best-effort by contract: any resolution problem returns what it has
    (possibly {}) rather than raising — a missing reference only removes
    markers, never the document."""
    ref: dict[str, str] = {}
    try:
        from u1_slice_workflow import (
            profile_path, filament_path,
            _flatten_process_profile, _materialize_flat_filament,
        )
    except Exception:
        return ref
    if profile_slug:
        try:
            for k, v in _flatten_process_profile(profile_path(profile_slug)).items():
                if not str(k).startswith("_"):
                    ref[str(k)] = _norm(v)
        except Exception:
            pass
    if material and out_dir is not None:
        try:
            import json as _json
            flat = _materialize_flat_filament(
                filament_path(material, nozzle=nozzle), Path(out_dir))
            for k, v in _json.loads(Path(flat).read_text()).items():
                if not str(k).startswith("_"):
                    ref.setdefault(str(k), _norm(v))
        except Exception:
            pass
    return ref


def build_material_envelope(material: str | None, nozzle: str = "0.4",
                            out_dir: Path | None = None) -> dict[str, Any]:
    """Resolve the MATERIAL's declared temperature envelope from its
    filament profile (``nozzle_temperature_range_low`` / ``_high``).

    This powers the third trust layer: preset integrity says "the gcode
    matches what you picked"; the envelope says "what you picked is sane
    for this material" — which is the question a Reddit speed profile
    can't answer about itself. Only DECLARED ranges are used; where the
    profile format declares no range (bed temps), no norm is invented.
    Best-effort: returns {} on any resolution problem."""
    if not material or out_dir is None:
        return {}
    try:
        import json as _json
        from u1_slice_workflow import filament_path, _materialize_flat_filament
        flat = _materialize_flat_filament(
            filament_path(material, nozzle=nozzle), Path(out_dir))
        data = _json.loads(Path(flat).read_text())
        low = _norm(data.get("nozzle_temperature_range_low"))
        high = _norm(data.get("nozzle_temperature_range_high"))
        if not low or not high:
            return {}
        return {"material": str(material),
                "nozzle_low": float(low.split(",")[0]),
                "nozzle_high": float(high.split(",")[0])}
    except Exception:
        return {}


def _temps_outside(value: str, low: float, high: float) -> list[float]:
    """Parse a config temp value ("240" / "240,245") and return any parts
    outside [low, high]. Unparseable parts are skipped, not flagged."""
    out = []
    for part in str(value).split(","):
        try:
            t = float(part.strip())
        except ValueError:
            continue
        if t < low or t > high:
            out.append(t)
    return out


def _read_bounded(path: Path, chunk: int = 512_000) -> str:
    """Head+tail read so a 200MB gcode never becomes a memory bomb. The
    config block lives at the tail; header metadata at the head."""
    size = path.stat().st_size
    with path.open("rb") as f:
        head = f.read(chunk)
        tail = b""
        if size > chunk:
            f.seek(max(0, size - chunk))
            tail = f.read(chunk)
    return (head + b"\n" + tail).decode("utf-8", "replace")


def parse_gcode_config(path: Path) -> dict[str, str]:
    """Parse ``; key = value`` comment lines from a sliced gcode.

    Prefers the ``; CONFIG_BLOCK_START .. ; CONFIG_BLOCK_END`` section Orca
    writes at the end of the file (the authoritative full config); falls
    back to scanning every comment line when the markers are absent (older
    Orca builds / truncated tails). First occurrence wins inside the block
    so bounded-read duplication can't flip values.
    """
    text = _read_bounded(Path(path))
    lines = text.splitlines()
    start = end = None
    for i, l in enumerate(lines):
        if "CONFIG_BLOCK_START" in l and start is None:
            start = i
        elif "CONFIG_BLOCK_END" in l and start is not None:
            end = i
            break
    scan = lines[start + 1:end] if (start is not None and end is not None) else lines
    out: dict[str, str] = {}
    for raw in scan:
        m = _CONFIG_LINE_RE.match(raw.strip())
        if m:
            key = m.group(1).strip()
            if key not in out:
                out[key] = m.group(2).strip()
    return out


# Keys that legitimately differ between a profile file and the gcode's
# config block without meaning anything changed about the PRINT — ids,
# provenance, display metadata. Comparing them would bury real deviations
# in noise.
_SWEEP_IGNORE_EXACT = {
    "name", "from", "inherits", "version", "notes", "is_custom_defined",
    "print_settings_id", "filament_settings_id", "printer_settings_id",
    "printer_model", "printer_variant", "setting_id",
}
_SWEEP_IGNORE_SUFFIXES = ("_id", "_ids", "_settings_id", "_notes")
_SWEEP_IGNORE_PREFIXES = ("compatible_", "_u1_")


def _sweep_deviations(config, reference, skip_keys):
    """Full-config sweep: every key present in BOTH the gcode config and the
    preset reference (minus curated keys already shown and known-noisy
    metadata) whose normalized values differ. This is what catches the
    "little nuances" — ironing, retraction, flow tweaks — that the curated
    table doesn't display. Deviations-only output is self-curating: a
    normal print yields zero to a handful of rows."""
    out = []
    for key in sorted(config):
        if key in skip_keys or key in _SWEEP_IGNORE_EXACT:
            continue
        if key.endswith(_SWEEP_IGNORE_SUFFIXES) or key.startswith(_SWEEP_IGNORE_PREFIXES):
            continue
        ref_v = reference.get(key)
        if ref_v is None:
            continue
        got = _norm(config[key])
        ref_norm = _norm(ref_v)
        # Both-empty is not a deviation (e.g. an unset start_gcode on each side).
        if got != ref_norm and not (got == "" and ref_norm == ""):
            out.append((key, got, ref_norm))
    return out


def _first(config: dict[str, str], keys: list[str]) -> str | None:
    for k in keys:
        v = config.get(k)
        if v not in (None, ""):
            return v
    return None


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def generate(
    request_id: str,
    out_dir: Path,
    plates: list[dict[str, Any]],
    *,
    state: dict[str, Any] | None = None,
    decisions: dict[str, Any] | None = None,
    overrides: list[str] | None = None,
    operator: str | None = None,
    reference: dict[str, str] | None = None,
    envelope: dict[str, Any] | None = None,
) -> Path:
    """Write ``<out_dir>/review.md`` and audit its sha256. Returns the path.

    ``plates``: one dict per plate with ``plate_idx``, ``gcode_path``,
    ``printer_storage_filename``, ``gcode_hash`` and optional ``metadata``
    (the parse_gcode_metadata dict) + ``partition_parts``.
    ``decisions``: operator-chosen fields to echo (orient/tool/material/
    profile/supports/parts...). ``overrides``: human-readable lines for
    anything that changed the preset (e.g. the supports override note).

    Raises on failure — the CALLER decides fail-soft; nothing here should
    ever be load-bearing for the print flow.
    """
    state = state or {}
    decisions = decisions or {}
    out_dir = Path(out_dir)
    revision = state.get("request_revision", 1)
    plate1 = plates[0] if plates else {}

    L: list[str] = []
    L.append(f"# Pre-print review — `{request_id}`")
    L.append("")
    L.append(f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} "
             f"· plan revision **{revision}** · gated plate sha256 "
             f"`{plate1.get('gcode_hash', 'n/a')}`")
    L.append("")
    L.append("> This document is bound to the plan above. If anything "
             "plan-affecting changes after you review it, `can_start()` "
             "refuses the start and you get a fresh card — the plan you "
             "read here is the plan that prints.")
    L.append("")

    # ── What will print ──
    L.append("## What will print")
    L.append("")
    L.append("| Plate | File on printer | Est. time | Filament | sha256 |")
    L.append("|---|---|---|---|---|")
    for p in plates:
        md = p.get("metadata") or {}
        est = (md.get("estimated printing time (normal mode)")
               or md.get("estimated printing time") or "—")
        fil = (md.get("total filament used [g]")
               or md.get("filament used [g]") or "—")
        fil = f"{fil} g" if fil != "—" else fil
        h = str(p.get("gcode_hash", ""))
        h_short = h.replace("sha256:", "")[:12] or "—"
        L.append(f"| {p.get('plate_idx', '?')} | "
                 f"`{p.get('printer_storage_filename', '?')}` | "
                 f"{est} | {fil} | `{h_short}…` |")
        parts = p.get("partition_parts")
        if parts:
            L.append(f"| | ↳ parts: {', '.join(parts)} | | | |")
    if len(plates) > 1:
        L.append("")
        L.append(f"Only **plate 1** goes through the camera-gated start. "
                 f"Plates 2–{len(plates)} are uploaded; start them from the "
                 f"Snapmaker app after plate 1 finishes.")
    L.append("")

    # ── Key settings from the gcode itself ──
    L.append("## Key settings")
    L.append("")
    L.append("Read from the config block inside the sliced gcode — what the "
             "printer will execute, not what the workflow intended.")
    L.append("")
    gcode_path = plate1.get("gcode_path")
    config: dict[str, str] = {}
    if gcode_path and Path(gcode_path).is_file():
        config = parse_gcode_config(Path(gcode_path))
    if config:
        L.append("| Setting | Value |")
        L.append("|---|---|")
        deviations = 0
        for label, keys in _KEY_SETTINGS:
            v = _first(config, keys)
            if v is None:
                continue
            cell = f"`{v}`"
            if reference:
                # Flag values that differ from the chosen preset — the
                # tweaked-and-forgotten temp is the classic trust-killer.
                ref_v = next((reference[k] for k in keys if k in reference), None)
                if ref_v is not None and _norm(v) != _norm(ref_v):
                    cell = f"`{v}` ⚠ *preset: `{ref_v}`*"
                    deviations += 1
            L.append(f"| {label} | {cell} |")
        others: list = []
        if reference:
            curated_keys = {k for _, keys in _KEY_SETTINGS for k in keys}
            others = _sweep_deviations(config, reference, curated_keys)
            total = deviations + len(others)
            L.append("")
            L.append(f"_{total} setting(s) differ from the chosen "
                     f"preset (marked ⚠)._" if total else
                     "_Every setting in the sliced gcode matches the chosen "
                     "preset — no deviations detected._")
        if others:
            L.append("")
            L.append("**Other deviations from the preset** (full-config "
                     "sweep — every remaining setting matches):")
            L.append("")
            L.append("| Setting | In this print | Preset |")
            L.append("|---|---|---|")
            shown = others[:20]
            for key, got, ref_v in shown:
                L.append(f"| `{key}` | `{got}` ⚠ | `{ref_v}` |")
            if len(others) > len(shown):
                L.append(f"| _…and {len(others) - len(shown)} more_ | | |")
        # Material-envelope sanity (v2.2): a custom preset can match itself
        # perfectly and still be wild for the material. Check the gcode's
        # nozzle temps against the range the material's own filament
        # profile declares. In-range prints a quiet confirmation; out-of-
        # range gets its own distinctly-worded flag. No declared range =
        # no section (norms are never invented here).
        if envelope and envelope.get("nozzle_low") is not None:
            lo, hi = envelope["nozzle_low"], envelope["nozzle_high"]
            mat = envelope.get("material", "this material")
            bad: list[str] = []
            for cfg_key, label in (("nozzle_temperature", "Nozzle temp"),
                                   ("nozzle_temperature_initial_layer",
                                    "First-layer nozzle temp")):
                v = config.get(cfg_key)
                if v is None:
                    continue
                outside = _temps_outside(v, lo, hi)
                if outside:
                    bad.append(f"{label} `{v}`°C")
            L.append("")
            if bad:
                L.append(f"⚠ **Material sanity:** {'; '.join(bad)} — "
                         f"outside {mat}'s declared range "
                         f"({lo:.0f}–{hi:.0f}°C). This can be intentional "
                         f"(speed profiles run hot), but it is exactly the "
                         f"kind of thing to notice before saying yes.")
            else:
                L.append(f"_Material sanity: nozzle temps are within "
                         f"{mat}'s declared range ({lo:.0f}–{hi:.0f}°C)._")
    else:
        L.append("_Config block not found in the gcode — settings table "
                 "unavailable for this slice (older Orca build?). The "
                 "decisions below still describe the operator's choices._")
    L.append("")

    # ── Operator decisions & overrides ──
    L.append("## Your decisions")
    L.append("")
    label_map = [
        ("tool", "Toolhead"), ("material", "Material"),
        ("profile", "Profile"), ("orient", "Orientation"),
        ("supports", "Supports"), ("parts", "Parts selected"),
    ]
    any_dec = False
    for key, label in label_map:
        v = decisions.get(key) if key in decisions else state.get(key)
        if v not in (None, "", []):
            L.append(f"- **{label}:** {v}")
            any_dec = True
    if not any_dec:
        L.append("- _(none recorded)_")
    if overrides:
        L.append("")
        L.append("**Overrides applied to the preset:**")
        for o in overrides:
            L.append(f"- {o}")
    L.append("")

    # ── What happens next ──
    L.append("## Before you say yes")
    L.append("")
    L.append("1. Stage 1 captures a **fresh bed photo** — you approve the "
             "photo, not a description of it.")
    L.append("2. After your yes, the gate re-runs every safety check, then "
             "holds a **grace window** before any command reaches the "
             "printer. Reply `CANCEL` in that window to abort — no LLM in "
             "that path.")
    L.append("3. The material you chose is re-verified against what is "
             "PHYSICALLY loaded in the toolhead at start time — a mismatch "
             "blocks the print unless you explicitly accept it (audited).")
    L.append("4. Cancelling costs nothing: the slice and upload stay valid; "
             "restarting is one fresh photo + one fresh yes.")
    L.append("")

    text = "\n".join(L) + "\n"
    doc_path = out_dir / "review.md"
    out_dir.mkdir(parents=True, exist_ok=True)
    doc_path.write_text(text)

    # Forensic binding: the audit trail records exactly which document the
    # operator had available at review time.
    try:
        import u1_audit
        u1_audit.append(request_id, "review_doc_generated",
                        operator=operator or state.get("operator"),
                        doc_sha256=_sha256_text(text),
                        path=str(doc_path),
                        request_revision=revision,
                        gcode_hash=plate1.get("gcode_hash"))
    except Exception:
        pass  # audit is best-effort here; the doc itself already exists

    return doc_path
