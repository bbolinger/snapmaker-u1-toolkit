"""Script-parsed one-line decision form — v2.1.0 Phases B/C.

Per the decided model (plan §6): **the model never parses operator decisions —
this module does.** The workflow emits a consolidated form after analysis; the
operator answers in ONE compact line; the model relays that line verbatim into
``--form-answers``; this module parses + validates it against the offered
options and returns either a structured decision set or a clear, actionable
error. The readiness card echoes the parse back so the human verifies it before
the Stage-1 photo gate.

Parsing is **order-independent and forgiving** (the operator may type fields in
any order with loose punctuation) because the script is the powerhouse and the
relay — a small model — must not be trusted to normalize. Each field is
classified by shape, not position:

    auto | parts 1,3,5 | T0 | PLA | profile 2 | no-supports | start

Field grammar (all order-independent, ``|`` / ``;`` / newline separated):
  - orient   : ``auto`` | ``as-authored`` (default: as-authored)
  - parts    : ``all`` | ``1,3,5`` | ``1-4`` (1-based into the offered list;
               default: all)
  - tool     : ``T0``..``T3``                         (REQUIRED)
  - material : a token from the offered materials      (REQUIRED)
  - profile  : ``profile N`` / ``preset N`` / bare ``N`` (1-based index), or a
               profile name substring                  (REQUIRED)
  - supports : ``supports`` | ``no-supports`` (default: no-supports).
               ``overhangs`` is recognized but rejected as not-offered —
               enable_support is binary in the profile patch; a distinct
               overhangs-only mode is not implemented (do not offer it).
  - action   : ``start`` | ``upload-only``             (default: start; nothing
               physically starts without the separate Stage-1 photo + yes)

Spec shape (assembled by the workflow from analysis):
  {
    'parts':     [{'id': '01_x', 'label': 'x (80x164mm)'}, ...],   # optional (kits)
    'tools':     ['T0', 'T1', ...],
    'materials': ['PLA', 'PETG', ...],
    'profiles':  [{'idx': 1, 'label': '0.20 Standard ...'}, ...],
    'supports':  ['supports', 'no-supports'],
    'actions':   ['start', 'upload-only'],
  }
"""
from __future__ import annotations

import re
from typing import Any

REQUIRED_FIELDS = ("tool", "material", "profile")

_ORIENT_AUTO = {"auto", "auto-orient", "autoorient"}
_ORIENT_ASIS = {"as-authored", "asauthored", "as authored", "authored", "as-is", "asis"}
_SUPPORTS = {
    "supports": "supports",
    "support": "supports",
    "no-supports": "no-supports",
    "nosupports": "no-supports",
    "no-support": "no-supports",
    "no supports": "no-supports",
    "none": "no-supports",
    "overhangs": "overhangs",
    "overhang": "overhangs",
}
_ACTIONS = {
    "start": "start",
    "upload+start": "start",
    "upload-start": "start",
    "print": "start",
    "go": "start",
    # v2.2: the button verb is "Slice & review" — the value stays "start"
    # (the bed-clear yes/no is the real start decision). Accept the new
    # phrasing from text-mode typers too.
    "slice & review": "start",
    "slice and review": "start",
    "slice-review": "start",
    "slice review": "start",
    "review": "start",
    "upload-only": "upload-only",
    "uploadonly": "upload-only",
    "upload only": "upload-only",
    "upload": "upload-only",
}

# Advanced settings (v2.3): optional per-run slicer overrides, reached from
# the form's Review screen via the Advanced button. "default" = no override
# (the picked profile's own value stands). Each entry: (field_id, label,
# [(option_id, option_label), ...], orca_key, {option_id: orca_value}).
# Option labels are SELF-DESCRIBING on purpose: the five fields render on ONE
# shared screen, so a bare "2" or "Profile default" button is unreadable
# (operator feedback, live 2026-07-06 — "you feel blind").
ADVANCED_FIELDS = (
    ("infill", "Infill density",
     [("default", "Infill: profile default"), ("10", "Infill 10%"),
      ("15", "Infill 15%"), ("20", "Infill 20%"), ("30", "Infill 30%"),
      ("40", "Infill 40%"), ("50", "Infill 50%")],
     "sparse_infill_density",
     {"10": "10%", "15": "15%", "20": "20%", "30": "30%",
      "40": "40%", "50": "50%"}),
    ("infill_pattern", "Infill pattern",
     [("default", "Pattern: profile default"), ("grid", "Pattern: grid"),
      ("gyroid", "Pattern: gyroid"), ("honeycomb", "Pattern: honeycomb"),
      ("triangles", "Pattern: triangles"), ("cubic", "Pattern: cubic")],
     "sparse_infill_pattern",
     {"grid": "grid", "gyroid": "gyroid", "honeycomb": "honeycomb",
      "triangles": "triangles", "cubic": "cubic"}),
    ("walls", "Wall loops",
     [("default", "Walls: profile default"), ("2", "Walls: 2"),
      ("3", "Walls: 3"), ("4", "Walls: 4")],
     "wall_loops", {"2": "2", "3": "3", "4": "4"}),
    ("brim", "Brim",
     [("default", "Brim: profile default"), ("off", "Brim: off"),
      ("auto", "Brim: auto")],
     "brim_type", {"off": "no_brim", "auto": "auto_brim"}),
    ("fuzzy", "Fuzzy skin",
     [("default", "Fuzzy skin: profile default"), ("off", "Fuzzy skin: off"),
      ("on", "Fuzzy skin: on (outer walls)")],
     "fuzzy_skin", {"off": "none", "on": "external"}),
    # Only takes effect when Supports is ON (the setup-screen toggle);
    # Orca ignores support_type when enable_support is 0.
    ("support_style", "Support style",
     [("default", "Support style: profile default"),
      ("tree", "Support style: tree"),
      ("grid", "Support style: grid")],
     "support_type", {"tree": "tree(auto)", "grid": "normal(auto)"}),
)

_ADVANCED_BY_ID = {fid: (orca_key, mapping)
                   for fid, _lbl, _opts, orca_key, mapping in ADVANCED_FIELDS}


_TOOL_RE = re.compile(r"^t([0-9])$", re.IGNORECASE)
_PARTS_PREFIX_RE = re.compile(r"^(?:parts?|p)\s+(.+)$", re.IGNORECASE)
_PROFILE_PREFIX_RE = re.compile(r"^(?:profile|preset)\s+(.+)$", re.IGNORECASE)
# Advanced-override tokens (v2.3), text mode: "infill 30%", "gyroid",
# "walls 3", "brim off", "fuzzy". Prefixed or uniquely-named so they can't
# collide with parts/profile numbers.
_ADV_INFILL_RE = re.compile(r"^infill\s*(\d{1,3})\s*%?$", re.IGNORECASE)
_ADV_WALLS_RE = re.compile(r"^walls?\s*(\d)$", re.IGNORECASE)
_ADV_BRIM_RE = re.compile(r"^brim\s*(off|auto|on)$", re.IGNORECASE)
_ADV_FUZZY_RE = re.compile(r"^fuzzy(?:[\s-]*skin)?(?:\s+(on|off))?$", re.IGNORECASE)
# NOTE: bare "grid" stays an infill-pattern token (pre-existing grammar);
# support style needs the word: "tree supports" / "grid supports" / "tree".
_ADV_PATTERN_IDS = {"grid", "gyroid", "honeycomb", "triangles", "cubic"}
_ADV_SUPSTYLE_RE = re.compile(r"^(tree|grid)[\s-]+supports?$|^tree$", re.IGNORECASE)
_INT_RE = re.compile(r"^\d+$")
_INT_LIST_RE = re.compile(r"^\d+(?:\s*[,\s-]\s*\d+)*$")  # 1,3,5 or 1-4 or "1 3 5"


def _split_fields(line: str) -> list[str]:
    """Split the answer line into trimmed field tokens. Separators: | ; newline."""
    raw = re.split(r"[|;\n]+", line or "")
    return [t.strip() for t in raw if t.strip()]


def _set(values: dict, key: str, val: Any, errors: list, tok: str) -> None:
    """Conflict-checked field assignment. The same field given twice with the
    SAME value is harmless repetition; a different value is a conflict and
    must fail loudly — silent last-wins is how a relayed correction-plus-
    original picks the wrong one."""
    if key in values and values[key] != val:
        errors.append(
            f"{key} given twice with different values "
            f"({values[key]!r} then {tok!r}) — send one value per field"
        )
        return
    values[key] = val


def _expand_selection(text: str, n_parts: int) -> tuple[list[int] | None, str | None]:
    """Expand a selection token to 1-based indices. Returns (indices, error).

    Bounds are validated BEFORE expansion — `parts 1-30000000` must be a
    cheap error, not a multi-hundred-MB set + error string."""
    text = text.strip().lower()
    if text in ("all", "*", "everything"):
        return list(range(1, n_parts + 1)), None
    picked: set[int] = set()
    # comma- or space-separated atoms; each atom is N or a range A-B
    atoms = re.split(r"[,\s]+", text)
    for atom in atoms:
        if not atom:
            continue
        if "-" in atom:
            m = re.match(r"^(\d+)-(\d+)$", atom)
            if not m:
                return None, f"bad part range {atom!r}"
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo > hi:
                lo, hi = hi, lo
            if lo < 1 or hi > n_parts:
                return None, f"part range {atom!r} out of range 1-{n_parts}"
            picked.update(range(lo, hi + 1))
        elif atom.isdigit():
            idx = int(atom)
            if idx < 1 or idx > n_parts:
                return None, f"part number out of range 1-{n_parts}: {idx}"
            picked.add(idx)
        else:
            return None, f"bad part token {atom!r}"
    if not picked:
        return None, "no parts selected"
    return sorted(picked), None


def parse_answers(line: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Parse + validate a one-line answer against ``spec``.

    Returns ``{'ok': True, 'values': {...}, 'unrecognized': [...]}`` on success,
    or ``{'ok': False, 'errors': [...], 'values': {...}}`` otherwise. ``values``
    is always populated as far as parsing got, so the caller can echo partial
    progress in a re-prompt.
    """
    tools = [str(t).upper() for t in spec.get("tools", [])]
    materials = [str(m) for m in spec.get("materials", [])]
    materials_lower = {m.lower(): m for m in materials}
    profiles = spec.get("profiles", [])
    parts = spec.get("parts", [])
    n_parts = len(parts)
    supports_allowed = set(spec.get("supports", ["supports", "no-supports"]))
    actions_allowed = set(spec.get("actions", ["start", "upload-only"]))

    values: dict[str, Any] = {}
    errors: list[str] = []
    unrecognized: list[str] = []

    for tok in _split_fields(line):
        low = tok.lower()

        # orient
        if low in _ORIENT_AUTO:
            _set(values, "orient", "auto", errors, tok)
            continue
        if low in _ORIENT_ASIS:
            _set(values, "orient", "as-authored", errors, tok)
            continue

        # supports
        if low in _SUPPORTS:
            mapped = _SUPPORTS[low]
            if mapped not in supports_allowed:
                errors.append(f"supports option {mapped!r} not offered")
            else:
                _set(values, "supports", mapped, errors, tok)
            continue

        # action
        if low in _ACTIONS:
            mapped = _ACTIONS[low]
            if mapped not in actions_allowed:
                errors.append(f"action {mapped!r} not offered")
            else:
                _set(values, "action", mapped, errors, tok)
            continue

        # tool (T0..T3)
        m = _TOOL_RE.match(tok)
        if m:
            tool = f"T{m.group(1)}"
            if tools and tool not in tools:
                errors.append(f"tool {tool} not offered (have {', '.join(tools)})")
            else:
                _set(values, "tool", tool, errors, tok)
            continue

        # bare selection keyword: "all" / "*" / "everything"
        if low in ("all", "*", "everything"):
            if n_parts:
                _set(values, "parts", list(range(1, n_parts + 1)), errors, tok)
            # single-part job: 'all' is harmless and means the one part — consume it
            continue

        # explicit parts prefix: "parts 1,3,5"
        mp = _PARTS_PREFIX_RE.match(tok)
        if mp:
            if n_parts == 0:
                errors.append("part selection given but this is a single-part job")
            else:
                idxs, err = _expand_selection(mp.group(1), n_parts)
                if err:
                    errors.append(err)
                else:
                    _set(values, "parts", idxs, errors, tok)
            continue

        # Advanced overrides (v2.3), only when the spec offers them. Each maps
        # an operator token to the Orca profile key/value; a conflicting
        # repeat fails loudly like every other field.
        if spec.get("offer_advanced"):
            def _adv_set(field_id: str, option_id: str) -> bool:
                orca_key, mapping = _ADVANCED_BY_ID[field_id]
                mapped = mapping.get(option_id)
                if mapped is None:
                    errors.append(
                        f"{field_id} option {option_id!r} not offered "
                        f"(have {', '.join(sorted(mapping))})")
                    return True
                ov = values.setdefault("overrides", {})
                if orca_key in ov and ov[orca_key] != mapped:
                    errors.append(
                        f"{field_id} given twice with different values — "
                        f"send one value per field")
                    return True
                ov[orca_key] = mapped
                return True
            m = _ADV_INFILL_RE.match(tok)
            if m:
                _adv_set("infill", m.group(1))
                continue
            if low in _ADV_PATTERN_IDS:
                _adv_set("infill_pattern", low)
                continue
            m = _ADV_WALLS_RE.match(tok)
            if m:
                _adv_set("walls", m.group(1))
                continue
            m = _ADV_BRIM_RE.match(tok)
            if m:
                _adv_set("brim", "auto" if m.group(1).lower() == "on"
                         else m.group(1).lower())
                continue
            m = _ADV_FUZZY_RE.match(tok)
            if m:
                _adv_set("fuzzy", "off" if (m.group(1) or "on").lower() == "off"
                         else "on")
                continue
            m = _ADV_SUPSTYLE_RE.match(tok)
            if m:
                style = (m.group(1) or "tree").lower()
                _adv_set("support_style", style)
                # "tree supports" means supports ON too — don't make the
                # operator say it twice.
                if "support" in low:
                    _set(values, "supports", "supports", errors, tok)
                continue

        # explicit profile prefix: "profile 2" / "preset slug"
        mpr = _PROFILE_PREFIX_RE.match(tok)
        if mpr:
            _assign_profile(mpr.group(1).strip(), profiles, values, errors, tok=tok)
            continue

        # bare material token
        if low in materials_lower:
            _set(values, "material", materials_lower[low], errors, tok)
            continue

        # bare integer: on a single-part job it can only mean a profile index.
        # On a multi-part kit it's genuinely ambiguous — the staged flow reads
        # "3" as part 3, so silently reading it as PROFILE 3 here would print
        # ALL parts at a different profile. Ambiguity fails loudly.
        if _INT_RE.match(tok):
            if n_parts:
                errors.append(
                    f"bare number {tok!r} is ambiguous on a kit (part or "
                    f"profile?) — say 'profile {tok}' or 'parts {tok}'"
                )
            else:
                _assign_profile(tok, profiles, values, errors, tok=tok)
            continue

        # multi-number list/range with no prefix -> part selection
        if n_parts and _INT_LIST_RE.match(tok) and re.search(r"[,\-\s]", tok):
            idxs, err = _expand_selection(tok, n_parts)
            if err:
                errors.append(err)
            else:
                _set(values, "parts", idxs, errors, tok)
            continue

        # material-looking token that isn't offered: fail loudly as a material
        # problem. Without this, a mistyped/unoffered material (e.g. "PETG"
        # when only PLA is loaded) falls through to the profile-substring
        # matcher and silently becomes a PROFILE choice ("0.20 PETG Strong").
        if _looks_like_material(low, materials):
            errors.append(
                f"material {tok!r} not offered (have {', '.join(materials) or 'none'})"
            )
            continue

        # profile name substring (last resort, before giving up)
        if _match_profile_name(tok, profiles, values, errors, tok=tok):
            continue

        unrecognized.append(tok)

    return _finalize(values, spec, errors, unrecognized)


def _finalize(values: dict[str, Any], spec: dict[str, Any], errors: list[str],
              unrecognized: list[str] | None = None) -> dict[str, Any]:
    """Shared validation core: apply defaults, check required fields, package
    the result. Used by BOTH the text parser and the JSON parser so the two
    intakes validate identically (form-protocol §2). ``values`` is the
    partially-parsed decision set; mutated in place with defaults.
    """
    n_parts = len(spec.get("parts", []))
    supports_allowed = set(spec.get("supports", ["supports", "no-supports"]))
    actions_allowed = set(spec.get("actions", ["start", "upload-only"]))

    # Defaults fall back to the FIRST OFFERED option (spec list order) so a
    # spec without the standard default gets a deterministic one, not
    # whatever a set iterator yields this run.
    supports_offered = list(spec.get("supports", ["supports", "no-supports"]))
    actions_offered = list(spec.get("actions", ["start", "upload-only"]))
    values.setdefault("orient", "as-authored")
    if n_parts:
        values.setdefault("parts", list(range(1, n_parts + 1)))
    values.setdefault("supports", "no-supports" if "no-supports" in supports_allowed
                      else (supports_offered[0] if supports_offered else "no-supports"))
    values.setdefault("action", "start" if "start" in actions_allowed
                      else (actions_offered[0] if actions_offered else "start"))

    # Merged head/material: when the head carries its loaded filament (live
    # tool map), picking the head sets the material — no Material screen. Derive
    # it here, in the shared core, so BOTH intakes satisfy the required-field
    # check and downstream slice. The start gate still physically re-verifies
    # loaded material at print time.
    tool_materials = spec.get("tool_materials") or {}
    if "material" not in values and values.get("tool") in tool_materials:
        values["material"] = tool_materials[values["tool"]]

    for f in REQUIRED_FIELDS:
        if f not in values:
            errors.append(f"missing required field: {f}")

    if unrecognized:
        errors.append("unrecognized: " + ", ".join(repr(u) for u in unrecognized))

    return {"ok": not errors, "values": values, "errors": errors,
            "unrecognized": unrecognized or []}


# Common filament family names. A bare token that leads with one of these is
# operator material intent — it must never silently become a profile match.
_MATERIAL_FAMILIES = {
    "pla", "petg", "pet", "pctg", "abs", "asa", "tpu", "tpe", "pc", "pa",
    "nylon", "pva", "hips", "ppa", "pp",
}


def _looks_like_material(low: str, materials: list[str]) -> bool:
    """Whether an (unmatched) bare token reads as a material name — either a
    known filament family or related by substring to an offered material."""
    lead = re.split(r"[^a-z]", low, maxsplit=1)[0]
    if lead in _MATERIAL_FAMILIES:
        return True
    for m in materials:
        ml = m.lower()
        if ml and (low in ml or ml in low):
            return True
    return False


def _assign_profile(text: str, profiles: list, values: dict, errors: list,
                    *, tok: str | None = None) -> None:
    """Resolve a profile token (index or name) into values['profile']."""
    text = text.strip()
    tok = tok if tok is not None else text
    if _INT_RE.match(text):
        idx = int(text)
        valid = {int(p.get("idx", i + 1)) for i, p in enumerate(profiles)}
        if profiles and idx not in valid:
            errors.append(f"profile index {idx} out of range 1-{len(profiles)}")
            return
        label = None
        for i, p in enumerate(profiles):
            if int(p.get("idx", i + 1)) == idx:
                label = p.get("label")
                break
        prof = {"idx": idx, "label": label} if label else {"idx": idx}
        _set(values, "profile", prof, errors, tok)
        return
    if not _match_profile_name(text, profiles, values, errors, tok=tok):
        errors.append(f"profile {text!r} not found in the offered list")


def _match_profile_name(text: str, profiles: list, values: dict,
                        errors: list, *, tok: str | None = None) -> bool:
    """Substring (case-insensitive) match against profile labels. Returns hit."""
    low = text.strip().lower()
    if not low or not profiles:
        return False
    hits = [p for p in profiles if low in str(p.get("label", "")).lower()]
    if len(hits) == 1:
        p = hits[0]
        prof = {"idx": int(p.get("idx", profiles.index(p) + 1)), "label": p.get("label")}
        _set(values, "profile", prof, errors, tok if tok is not None else text)
        return True
    return False


_COLOR_SWATCH = {
    "white": "⚪", "black": "⚫", "gray": "⚫", "silver": "⚪", "beige": "⚪",
    "red": "🔴", "orange": "🟠", "yellow": "🟡", "green": "🟢", "cyan": "🔵",
    "blue": "🔵", "purple": "🟣", "pink": "🩷", "brown": "🟤",
}


def _head_label(head: dict[str, Any]) -> str:
    """One button label for a print head: 'Head 2 (T1) — PETG ⚫ black'.
    Channel is 0-based on the wire (T0..T3); operators count from 1."""
    swatch = _COLOR_SWATCH.get(str(head.get("color", "")).lower(), "⬤")
    ch = head.get("channel", 0)
    bits = f"Head {ch + 1} ({head.get('tool', f'T{ch}')}) — {head.get('material', '?')}"
    color = head.get("color")
    if color and color != "unknown":
        bits += f" {swatch} {color}"
    return _clean_label(bits)


_PROFILE_SUFFIX_RE = re.compile(r"\s*@\s*Snapmaker\s*U1\s*\([^)]*\)\s*$", re.IGNORECASE)


def _strip_profile_suffix(label: Any) -> str:
    """Drop the '@Snapmaker U1 (0.4 nozzle)' noise every profile label carries —
    it's on all of them, so it distinguishes nothing and just eats width."""
    return _PROFILE_SUFFIX_RE.sub("", str(label)).strip() or str(label)


def _clean_label(label: Any, max_len: int = 96) -> str:
    """Sanitize an operator-facing label. Part labels come from zip entry
    names — a filename containing `|`, `;`, or newlines could otherwise
    inject fake form lines (a spoofed ACTION: row) into the text the
    operator reads and the answer grammar splits on."""
    text = str(label)
    text = re.sub(r"[|;\r\n\t]+", " ", text)
    text = "".join(ch for ch in text if ch.isprintable())
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text[:max_len] if len(text) > max_len else text


def build_form(spec: dict[str, Any]) -> str:
    """Render the human-facing form text after analysis."""
    lines: list[str] = ["Decide all at once, one line (any order, separated by |):", ""]
    parts = spec.get("parts", [])
    if parts:
        lines.append(f"PARTS ({len(parts)}) — `all` or e.g. `parts 1,3,5` / `parts 1-{len(parts)}`:")
        for i, p in enumerate(parts, 1):
            lines.append(f"  {i}. {_clean_label(p.get('label', p.get('id')))}")
    lines.append("ORIENT: `as-authored` (default) | `auto`")
    tools = spec.get("tools", [])
    if tools:
        lines.append("TOOL: " + " | ".join(str(t).upper() for t in tools))
    mats = spec.get("materials", [])
    if mats:
        lines.append("MATERIAL: " + " | ".join(mats))
    profiles = spec.get("profiles", [])
    if profiles:
        lines.append("PROFILE (`profile N`):")
        for p in profiles:
            lines.append(f"  {p.get('idx')}. {_clean_label(p.get('label'))}")
    lines.append("SUPPORTS: " + " | ".join(spec.get("supports", ["no-supports"])))
    lines.append("ACTION: " + " | ".join(spec.get("actions", ["start", "upload-only"])))
    lines.append("")
    ex_parts = "parts 1,3 | " if parts else ""
    lines.append(f"Example: `{ex_parts}auto | T0 | PLA | profile 1 | no-supports | start`")
    return "\n".join(lines)


# ── Form-answers file handoff (v2.2) ────────────────────────────────────────
#
# The button UX collects answers at the GATEWAY (no LLM sees them) and
# writes them to a file keyed by form_id; the workflow redeems the file via
# --form-answers-from <form_id>. The model's only job is relaying the
# emitted command verbatim — the same trust level as --pending-nonce.

FORM_ANSWERS_DIR_ENV = "U1_FORM_ANSWERS_DIR"
_FORM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


def new_form_id() -> str:
    """Opaque single-form token (filename-safe, unguessable).

    Alphanumeric only: token_urlsafe's alphabet includes '-', and a LEADING
    dash turns ``--form-answers-from <id>`` into an argparse flag (live
    v2.2 finding: '-hNjdN7HGrTf'). Prefix guarantees a letter first.
    """
    import secrets
    return "f" + secrets.token_hex(5)


def answers_dir() -> "Path":
    """Where answer files live. Env override first (the Hermes gateway sets
    it to a path both containers can see); else <data_dir>/form_answers."""
    from pathlib import Path
    import os
    env = os.environ.get(FORM_ANSWERS_DIR_ENV, "").strip()
    if env:
        return Path(env)
    from u1_config import get_data_dir
    return Path(get_data_dir()) / "form_answers"


def _answers_path(form_id: str) -> "Path":
    if not _FORM_ID_RE.match(str(form_id or "")):
        raise ValueError(f"invalid form_id: {form_id!r}")
    return answers_dir() / f"{form_id}.json"


FORM_SCHEMAS_DIR_ENV = "U1_FORM_SCHEMAS_DIR"


def schemas_dir() -> "Path":
    """Where persisted form schemas live (sibling of form_answers).

    The agent no longer relays the schema through its tool call — a 26B
    local model (gemma4) reproduced the nested JSON as template-token soup
    (finish=stop, special-token leaks) and the flow stranded. The workflow
    persists the schema here keyed by form_id; the agent passes ONLY the
    flat form_id and the form plugin loads the schema from disk."""
    from pathlib import Path
    import os
    env = os.environ.get(FORM_SCHEMAS_DIR_ENV, "").strip()
    if env:
        return Path(env)
    from u1_config import get_data_dir
    return Path(get_data_dir()) / "form_schemas"


def persist_schema(form_id: str, schema: dict) -> "Path":
    """Write the schema JSON for ``form_id``; returns the path. Raises on
    an invalid form_id (same filename-safety rule as answer files)."""
    import json as _json
    if not _FORM_ID_RE.match(str(form_id or "")):
        raise ValueError(f"invalid form_id: {form_id!r}")
    d = schemas_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{form_id}.json"
    p.write_text(_json.dumps(schema, ensure_ascii=False))
    return p


# --------------------------------------------------------------------------- #
# Bed-clear confirm tokens
# --------------------------------------------------------------------------- #
# The bed-clear yes-command used to be a ~200-char command the model relayed
# verbatim (full path + --request-id + all answer flags + --pending-nonce). A
# 26B local model (gemma4) mangled the request_id mid-string
# ('u1_2026_...' -> 'u1_202rad_...') and the gate refused. Same medicine as the
# form: the model now relays ONE short opaque token; the workflow resolves the
# request from it. Single-use (consumed on redeem); the nonce it maps to still
# does all the real auth (single-use + revision/gcode binding).

CONFIRM_TOKENS_DIR_ENV = "U1_CONFIRM_TOKENS_DIR"


def confirm_tokens_dir() -> "Path":
    from pathlib import Path
    import os
    env = os.environ.get(CONFIRM_TOKENS_DIR_ENV, "").strip()
    if env:
        return Path(env)
    from u1_config import get_data_dir
    return Path(get_data_dir()) / "confirm_tokens"


def new_confirm_token() -> str:
    """Short, filename-safe, unguessable. ``c`` prefix guarantees a leading
    letter (a leading '-' would turn ``--confirm-start <tok>`` into a flag) and
    distinguishes it from a form id."""
    import secrets
    return "c" + secrets.token_hex(5)


def persist_confirm_token(token: str, request_id: str) -> "Path":
    """Map a confirm token to a request id on disk. Raises on a bad token."""
    import json as _json
    if not _FORM_ID_RE.match(str(token or "")):
        raise ValueError(f"invalid confirm token: {token!r}")
    d = confirm_tokens_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{token}.json"
    p.write_text(_json.dumps({"request_id": request_id}, ensure_ascii=False))
    return p


def resolve_confirm_token(token: str, consume: bool = True):
    """Return the request_id a confirm token maps to (or None). When
    ``consume`` (default), delete the token file so it can't be replayed —
    single-use, like the nonce it fronts for. Strict pattern also blocks
    traversal."""
    import json as _json, os
    if not _FORM_ID_RE.match(str(token or "")):
        return None
    p = confirm_tokens_dir() / f"{token}.json"
    if not consume:
        try:
            return _json.loads(p.read_text()).get("request_id")
        except Exception:
            return None
    # v2.2.1 #3: atomic single-use CLAIM. Rename the token file to a unique path
    # BEFORE reading it. os.replace is atomic on one filesystem, so of N
    # concurrent callers (double-click, gateway retry, duplicate Telegram
    # delivery, two workers) exactly ONE wins the rename; the losers see the
    # source already gone and return None. The old read-then-unlink let two
    # callers both read the same token before either deleted it, and both
    # returned the same request_id (and unlink failures were swallowed).
    claimed = p.with_name(f".claim.{os.getpid()}.{token}.json")
    try:
        os.replace(p, claimed)
    except OSError:
        return None  # already claimed by another caller, or never existed
    try:
        rid = _json.loads(claimed.read_text()).get("request_id")
    except Exception:
        rid = None
    try:
        claimed.unlink()
    except OSError:
        pass
    return rid


def write_answers_file(form_id: str, obj: dict) -> "Path":
    """Atomically persist a structured answer set for later redemption."""
    import json as _json
    import os
    p = _answers_path(form_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(_json.dumps(obj, indent=2))
    os.replace(tmp, p)
    return p


def read_and_consume_answers(form_id: str) -> dict:
    """Read an answer file and consume it (single-use). Raises FileNotFoundError
    when absent/already consumed and ValueError on a bad id or bad JSON.

    v2.2.2: CLAIM-before-read. The file is atomically renamed to a pid-unique
    path BEFORE it is read, so two concurrent redeems (double delivery / retry)
    cannot both read the same file and both act on it (the old read-then-rename
    let both in). Only the process whose ``os.replace`` wins owns the answers;
    the loser sees the source gone and raises FileNotFoundError."""
    import json as _json
    import os
    p = _answers_path(form_id)
    claimed = p.with_suffix(f".claimed.{os.getpid()}")
    try:
        os.replace(p, claimed)
    except OSError:
        raise FileNotFoundError(
            f"no pending answers for form {form_id!r} (missing or already used)")
    try:
        text = claimed.read_text()
    finally:
        try:
            os.replace(claimed, p.with_suffix(".json.consumed"))
        except OSError:
            pass
    obj = _json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError("answers file must contain a JSON object")
    return obj


FORM_SCHEMA_VERSION = 1


def build_form_schema(spec: dict[str, Any], *, submit: dict[str, str] | None = None) -> dict[str, Any]:
    """Build the declarative, platform-neutral form schema (form-protocol §3).

    Consumers (Telegram/Discord/canvas adapters, or a big model) render the
    fields as native controls and submit via ``--form-answers-json``; any
    consumer can fall back to ``text_fallback`` + ``--form-answers``. Option ids
    are stable for the life of the request (parts use ``part_id``; profiles use
    their 1-based ``idx``).
    """
    fields: list[dict[str, Any]] = []
    parts = spec.get("parts", [])
    # A single-part kit has nothing to choose — the workflow still renders the
    # thumbnail, but a one-option "which parts?" screen is pure friction. Skip
    # the field; _finalize defaults parts to the whole (one-item) list.
    if len(parts) > 1:
        fields.append({
            "id": "parts", "type": "multi_select", "label": "Parts",
            "options": [{"id": p["id"], "label": _clean_label(p.get("label", p["id"]))} for p in parts],
            "default": "all", "required": False,
        })
    # Setup screen (form UX, shipped in v2.2.0): print head + orientation + supports render TOGETHER
    # on one screen (group="setup"). Head first, then the two toggles. When the
    # live tool map is available each head option carries its loaded material +
    # colour, so picking the head IS picking the filament and the separate
    # Material screen is dropped; offline it falls back to generic T0–T3 + a
    # Material control (still inside the setup group).
    _GROUP = "setup"
    _GLABEL = "Print head & layout"
    heads = spec.get("heads") or []
    if heads:
        fields.append({"id": "tool", "type": "single_select", "label": "Print head",
                       "group": _GROUP, "group_label": _GLABEL,
                       "options": [{"id": h["tool"], "label": _head_label(h)} for h in heads],
                       "required": True})
    else:
        tools = [str(t).upper() for t in spec.get("tools", [])]
        if tools:
            fields.append({"id": "tool", "type": "single_select", "label": "Toolhead",
                           "group": _GROUP, "group_label": _GLABEL,
                           "options": [{"id": t, "label": t} for t in tools], "required": True})
        mats = spec.get("materials", [])
        if mats:
            fields.append({"id": "material", "type": "single_select", "label": "Material",
                           "group": _GROUP,
                           "options": [{"id": m, "label": m} for m in mats], "required": True})
    # Orientation. For a single model the caller may pass Orca's real verdict
    # (spec["orient_recommendation"] = 'auto'|'asauthored' + spec["orient_note"])
    # so the button recommends the pose Orca prefers, with the reason. Default
    # follows the recommendation; absent a verdict it stays 'as-authored'.
    _o_rec = spec.get("orient_recommendation")
    _rec_id = {"auto": "auto", "asauthored": "as-authored"}.get(_o_rec)
    _o_opts = []
    for _oid, _olbl in (("as-authored", "As-authored"), ("auto", "Auto-rotate")):
        _opt = {"id": _oid,
                "label": _olbl + (" (recommended)" if _oid == _rec_id else "")}
        if _oid == _rec_id:
            _opt["recommended"] = True
        _o_opts.append(_opt)
    _orient_field = {"id": "orient", "type": "single_select",
                     "label": "Orientation", "group": _GROUP, "compact": True,
                     "options": _o_opts, "default": _rec_id or "as-authored"}
    if spec.get("orient_note"):
        _orient_field["note"] = spec["orient_note"]
    fields.append(_orient_field)
    # Humanize + put the default (no-supports) first so the toggle reads
    # left-to-right with the safe choice on the left.
    _sup = list(spec.get("supports", ["supports", "no-supports"]))
    _sup_lbl = {"no-supports": "No supports", "supports": "Add supports"}
    _sup_ordered = sorted(_sup, key=lambda s: 0 if s == "no-supports" else 1)
    fields.append({"id": "supports", "type": "single_select", "label": "Supports",
                   "group": _GROUP, "compact": True,
                   "options": [{"id": s, "label": _sup_lbl.get(s, s)} for s in _sup_ordered],
                   "default": "no-supports"})
    profiles = spec.get("profiles", [])
    if profiles:
        fields.append({"id": "profile", "type": "single_select", "label": "Print profile",
                       "options": [{"id": p.get("idx"), "label": _clean_label(_strip_profile_suffix(p.get("label")))} for p in profiles],
                       "required": True})
    # Advanced overrides (v2.3): optional, EXCLUDED from the main screen flow —
    # the renderer only reaches them via the Review screen's Advanced button.
    # "default" = no override; skipping the screen is today's behavior.
    if spec.get("offer_advanced"):
        for _fid, _lbl, _opts, _orca_key, _mapping in ADVANCED_FIELDS:
            fields.append({
                "id": _fid, "type": "single_select", "label": _lbl,
                "options": [{"id": oid, "label": olbl} for oid, olbl in _opts],
                "default": "default", "required": False,
                "advanced": True, "group": "advanced",
                "group_label": "Advanced settings",
            })
    # v2.2 (kit refinement): NO action field. The form only collects the PLAN.
    # The single print/keep-staged decision happens AFTER slice + a FRESH bed
    # photo (you decide with the real bed in view) — not up front, before the
    # photo even exists. `_finalize` still defaults action="start" so the commit
    # always slices + uploads + captures the bed + offers the one decision.
    schema: dict[str, Any] = {
        "version": FORM_SCHEMA_VERSION,
        "fields": fields,
        "submit_label": "\U0001f52a Slice it",
        "text_fallback": build_form(spec),
        "answer_grammar": "pipe-separated one-liner: parts 1,3 | T0 | PLA | profile 2 | no-supports",
    }
    if submit:
        schema["submit"] = submit
    return schema


def parse_answers_json(obj: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Parse a STRUCTURED answer (from a native-widget gateway) against ``spec``.

    Mirrors ``parse_answers`` (text) but takes a dict. Normalizes to the SAME
    internal decision set and runs the SAME ``_finalize`` validation core, so
    both intakes are validated identically (form-protocol §4). ``parts`` are
    stable ids or the literal ``"all"`` (NOT indices — a widget has the ids);
    ``profile`` is the option id (1-based idx) or a name substring.
    """
    if not isinstance(obj, dict):
        return {"ok": False, "values": {}, "errors": ["answer JSON must be an object"], "unrecognized": []}

    parts_spec = spec.get("parts", [])
    n_parts = len(parts_spec)
    id_to_index = {p["id"]: i + 1 for i, p in enumerate(parts_spec)}
    tools = [str(t).upper() for t in spec.get("tools", [])]
    materials = {str(m).lower(): str(m) for m in spec.get("materials", [])}
    profiles = spec.get("profiles", [])
    supports_allowed = set(spec.get("supports", ["supports", "no-supports"]))
    actions_allowed = set(spec.get("actions", ["start", "upload-only"]))

    values: dict[str, Any] = {}
    errors: list[str] = []

    # parts: list of ids | "all"
    if obj.get("parts") not in (None, ""):
        pv = obj["parts"]
        if pv == "all" or pv == ["all"]:
            if n_parts:
                values["parts"] = list(range(1, n_parts + 1))
        elif isinstance(pv, list):
            idxs: list[int] = []
            for item in pv:
                if item in id_to_index:
                    idxs.append(id_to_index[item])
                else:
                    errors.append(f"unknown part id: {item!r}")
            if idxs and not any("part id" in e for e in errors):
                values["parts"] = sorted(set(idxs))
        else:
            errors.append("parts must be a list of ids or 'all'")

    # orient
    if obj.get("orient"):
        o = str(obj["orient"]).lower()
        if o in _ORIENT_AUTO:
            values["orient"] = "auto"
        elif o in _ORIENT_ASIS:
            values["orient"] = "as-authored"
        else:
            errors.append(f"unknown orient {obj['orient']!r}")

    # tool
    if obj.get("tool"):
        t = str(obj["tool"]).upper()
        if tools and t not in tools:
            errors.append(f"tool {t} not offered")
        else:
            values["tool"] = t

    # material
    if obj.get("material"):
        m = str(obj["material"]).lower()
        if m in materials:
            values["material"] = materials[m]
        else:
            errors.append(f"material {obj['material']!r} not offered")

    # profile: id (int idx) or name
    if obj.get("profile") not in (None, ""):
        _assign_profile(str(obj["profile"]), profiles, values, errors)

    # supports
    if obj.get("supports"):
        s = _SUPPORTS.get(str(obj["supports"]).lower())
        if s and s in supports_allowed:
            values["supports"] = s
        else:
            errors.append(f"unknown/unsupported supports {obj['supports']!r}")

    # action
    if obj.get("action"):
        a = _ACTIONS.get(str(obj["action"]).lower())
        if a and a in actions_allowed:
            values["action"] = a
        else:
            errors.append(f"unknown action {obj['action']!r}")

    # Advanced overrides (v2.3) — honored only when the spec offered them.
    # "default" (or absent) = no override; anything else must be an offered
    # option id, mapped here to the Orca profile key/value.
    if spec.get("offer_advanced"):
        for _fid, (_orca_key, _mapping) in _ADVANCED_BY_ID.items():
            raw = obj.get(_fid)
            if raw in (None, "", "default"):
                continue
            mapped = _mapping.get(str(raw))
            if mapped is None:
                errors.append(f"unknown {_fid} option {raw!r}")
            else:
                values.setdefault("overrides", {})[_orca_key] = mapped

    return _finalize(values, spec, errors)


def echo_parse(values: dict[str, Any], spec: dict[str, Any]) -> str:
    """Human-readable echo of the parsed decisions, for the readiness card."""
    bits: list[str] = []
    parts = spec.get("parts", [])
    if parts and "parts" in values:
        sel = values["parts"]
        if len(sel) == len(parts):
            bits.append(f"parts=all ({len(parts)})")
        else:
            labels = [parts[i - 1].get("id", str(i)) for i in sel]
            bits.append("parts=" + ",".join(labels))
    bits.append(f"orient={values.get('orient')}")
    if "tool" in values:
        bits.append(f"tool={values['tool']}")
    if "material" in values:
        bits.append(f"material={values['material']}")
    prof = values.get("profile")
    if prof:
        label = prof.get("label")
        if not label:
            # Resolve the human-readable name from the spec by index so the
            # operator's verification line shows e.g. "0.20 Standard", not "#2".
            for p in spec.get("profiles", []):
                if p.get("idx") == prof.get("idx"):
                    label = p.get("label")
                    break
        bits.append("profile=" + (label or f"#{prof.get('idx')}"))
    bits.append(f"supports={values.get('supports')}")
    bits.append(f"action={values.get('action')}")
    return "I read: " + ", ".join(bits)
