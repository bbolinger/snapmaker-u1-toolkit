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
  - supports : ``supports`` | ``no-supports`` | ``overhangs`` (default: no-supports)
  - action   : ``start`` | ``upload-only``             (default: start; nothing
               physically starts without the separate Stage-1 photo + yes)

Spec shape (assembled by the workflow from analysis):
  {
    'parts':     [{'id': '01_x', 'label': 'x (80x164mm)'}, ...],   # optional (kits)
    'tools':     ['T0', 'T1', ...],
    'materials': ['PLA', 'PETG', ...],
    'profiles':  [{'idx': 1, 'label': '0.20 Standard ...'}, ...],
    'supports':  ['supports', 'no-supports', 'overhangs'],
    'actions':   ['start', 'upload-only'],
  }
"""
from __future__ import annotations

import re
from typing import Any

REQUIRED_FIELDS = ("tool", "material", "profile")

_ORIENT_AUTO = {"auto", "auto-orient", "autoorient", "orient"}
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
    "upload-only": "upload-only",
    "uploadonly": "upload-only",
    "upload only": "upload-only",
    "upload": "upload-only",
}

_TOOL_RE = re.compile(r"^t([0-9])$", re.IGNORECASE)
_PARTS_PREFIX_RE = re.compile(r"^(?:parts?|p)\s+(.+)$", re.IGNORECASE)
_PROFILE_PREFIX_RE = re.compile(r"^(?:profile|preset)\s+(.+)$", re.IGNORECASE)
_INT_RE = re.compile(r"^\d+$")
_INT_LIST_RE = re.compile(r"^\d+(?:\s*[,\s-]\s*\d+)*$")  # 1,3,5 or 1-4 or "1 3 5"


def _split_fields(line: str) -> list[str]:
    """Split the answer line into trimmed field tokens. Separators: | ; newline."""
    raw = re.split(r"[|;\n]+", line or "")
    return [t.strip() for t in raw if t.strip()]


def _expand_selection(text: str, n_parts: int) -> tuple[list[int] | None, str | None]:
    """Expand a selection token to 1-based indices. Returns (indices, error)."""
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
            picked.update(range(lo, hi + 1))
        elif atom.isdigit():
            picked.add(int(atom))
        else:
            return None, f"bad part token {atom!r}"
    bad = [i for i in picked if i < 1 or i > n_parts]
    if bad:
        return None, f"part number(s) out of range 1-{n_parts}: {sorted(bad)}"
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
    supports_allowed = set(spec.get("supports", ["supports", "no-supports", "overhangs"]))
    actions_allowed = set(spec.get("actions", ["start", "upload-only"]))

    values: dict[str, Any] = {}
    errors: list[str] = []
    unrecognized: list[str] = []

    for tok in _split_fields(line):
        low = tok.lower()

        # orient
        if low in _ORIENT_AUTO:
            values["orient"] = "auto"
            continue
        if low in _ORIENT_ASIS:
            values["orient"] = "as-authored"
            continue

        # supports
        if low in _SUPPORTS:
            mapped = _SUPPORTS[low]
            if mapped not in supports_allowed:
                errors.append(f"supports option {mapped!r} not offered")
            else:
                values["supports"] = mapped
            continue

        # action
        if low in _ACTIONS:
            mapped = _ACTIONS[low]
            if mapped not in actions_allowed:
                errors.append(f"action {mapped!r} not offered")
            else:
                values["action"] = mapped
            continue

        # tool (T0..T3)
        m = _TOOL_RE.match(tok)
        if m:
            tool = f"T{m.group(1)}"
            if tools and tool not in tools:
                errors.append(f"tool {tool} not offered (have {', '.join(tools)})")
            else:
                values["tool"] = tool
            continue

        # bare selection keyword: "all" / "*" / "everything"
        if low in ("all", "*", "everything"):
            if n_parts:
                values["parts"] = list(range(1, n_parts + 1))
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
                    values["parts"] = idxs
            continue

        # explicit profile prefix: "profile 2" / "preset slug"
        mpr = _PROFILE_PREFIX_RE.match(tok)
        if mpr:
            _assign_profile(mpr.group(1).strip(), profiles, values, errors)
            continue

        # bare material token
        if low in materials_lower:
            values["material"] = materials_lower[low]
            continue

        # bare integer -> profile index (NOT a part list; lists carry , or -)
        if _INT_RE.match(tok):
            _assign_profile(tok, profiles, values, errors)
            continue

        # multi-number list/range with no prefix -> part selection
        if n_parts and _INT_LIST_RE.match(tok) and re.search(r"[,\-\s]", tok):
            idxs, err = _expand_selection(tok, n_parts)
            if err:
                errors.append(err)
            else:
                values["parts"] = idxs
            continue

        # profile name substring (last resort, before giving up)
        if _match_profile_name(tok, profiles, values):
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
    supports_allowed = set(spec.get("supports", ["supports", "no-supports", "overhangs"]))
    actions_allowed = set(spec.get("actions", ["start", "upload-only"]))

    values.setdefault("orient", "as-authored")
    if n_parts:
        values.setdefault("parts", list(range(1, n_parts + 1)))
    values.setdefault("supports", "no-supports" if "no-supports" in supports_allowed else next(iter(supports_allowed)))
    values.setdefault("action", "start" if "start" in actions_allowed else next(iter(actions_allowed)))

    for f in REQUIRED_FIELDS:
        if f not in values:
            errors.append(f"missing required field: {f}")

    if unrecognized:
        errors.append("unrecognized: " + ", ".join(repr(u) for u in unrecognized))

    return {"ok": not errors, "values": values, "errors": errors,
            "unrecognized": unrecognized or []}


def _assign_profile(text: str, profiles: list, values: dict, errors: list) -> None:
    """Resolve a profile token (index or name) into values['profile']."""
    text = text.strip()
    if _INT_RE.match(text):
        idx = int(text)
        valid = {int(p.get("idx", i + 1)) for i, p in enumerate(profiles)}
        if profiles and idx not in valid:
            errors.append(f"profile index {idx} out of range 1-{len(profiles)}")
            return
        values["profile"] = {"idx": idx}
        return
    if not _match_profile_name(text, profiles, values):
        errors.append(f"profile {text!r} not found in the offered list")


def _match_profile_name(text: str, profiles: list, values: dict) -> bool:
    """Substring (case-insensitive) match against profile labels. Returns hit."""
    low = text.strip().lower()
    if not low or not profiles:
        return False
    hits = [p for p in profiles if low in str(p.get("label", "")).lower()]
    if len(hits) == 1:
        p = hits[0]
        values["profile"] = {"idx": int(p.get("idx", profiles.index(p) + 1)), "label": p.get("label")}
        return True
    return False


def build_form(spec: dict[str, Any]) -> str:
    """Render the human-facing form text after analysis."""
    lines: list[str] = ["Decide all at once, one line (any order, separated by |):", ""]
    parts = spec.get("parts", [])
    if parts:
        lines.append(f"PARTS ({len(parts)}) — `all` or e.g. `parts 1,3,5` / `parts 1-{len(parts)}`:")
        for i, p in enumerate(parts, 1):
            lines.append(f"  {i}. {p.get('label', p.get('id'))}")
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
            lines.append(f"  {p.get('idx')}. {p.get('label')}")
    lines.append("SUPPORTS: " + " | ".join(spec.get("supports", ["no-supports"])))
    lines.append("ACTION: " + " | ".join(spec.get("actions", ["start", "upload-only"])))
    lines.append("")
    ex_parts = "parts 1,3 | " if parts else ""
    lines.append(f"Example: `{ex_parts}auto | T0 | PLA | profile 1 | no-supports | start`")
    return "\n".join(lines)


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
    if parts:
        fields.append({
            "id": "parts", "type": "multi_select", "label": "Parts",
            "options": [{"id": p["id"], "label": p.get("label", p["id"])} for p in parts],
            "default": "all", "required": False,
        })
    fields.append({"id": "orient", "type": "single_select", "label": "Orientation",
                   "options": ["as-authored", "auto"], "default": "as-authored"})
    tools = [str(t).upper() for t in spec.get("tools", [])]
    if tools:
        fields.append({"id": "tool", "type": "single_select", "label": "Toolhead",
                       "options": [{"id": t, "label": t} for t in tools], "required": True})
    mats = spec.get("materials", [])
    if mats:
        fields.append({"id": "material", "type": "single_select", "label": "Material",
                       "options": [{"id": m, "label": m} for m in mats], "required": True})
    profiles = spec.get("profiles", [])
    if profiles:
        fields.append({"id": "profile", "type": "single_select", "label": "Print profile",
                       "options": [{"id": p.get("idx"), "label": p.get("label")} for p in profiles],
                       "required": True})
    fields.append({"id": "supports", "type": "single_select", "label": "Supports",
                   "options": list(spec.get("supports", ["supports", "no-supports", "overhangs"])),
                   "default": "no-supports"})
    fields.append({"id": "action", "type": "single_select", "label": "Action",
                   "options": list(spec.get("actions", ["start", "upload-only"])), "default": "start"})

    schema: dict[str, Any] = {
        "version": FORM_SCHEMA_VERSION,
        "fields": fields,
        "text_fallback": build_form(spec),
        "answer_grammar": "pipe-separated one-liner: parts 1,3 | T0 | PLA | profile 2 | no-supports | start",
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
    supports_allowed = set(spec.get("supports", ["supports", "no-supports", "overhangs"]))
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
