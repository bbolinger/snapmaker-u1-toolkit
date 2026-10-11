"""The operator's print-ready message, built from the workflow's own events.

At the bed-clear moment the model used to paraphrase the readiness card and
the yes/no prompt into one long message: filler first, a request id nobody
needs, and the decision as one plain sentence above the photos, so it
scrolled out of view behind them (operator 2026-10-11: "almost missed the
key is to approve or not"). This builds the message deterministically
instead: a short card (what will print, how long, how much filament, anything
to watch for) and the bed-clear question on its own, which the caller sends
as the last message, after the photos and the review doc.

Pure functions over event dicts; no I/O.
"""
from __future__ import annotations

import re
from typing import Any

_SUPPORTS = {
    "supports": "supports",
    "no_supports": "no supports",
}

QUESTION = ("\U0001F449 **Bed clear?** Reply **YES** to print.\n"
            "NO keeps it on the printer, unprinted.")


def _humanize_profile(slug: str) -> str:
    """'0_20_standard_snapmaker_u1_0_4_nozzle' -> '0.20 Standard'. Only a
    fallback: the workflow sends the profile's own label when it has one."""
    s = re.sub(r"_?snapmaker_u1.*$", "", str(slug or ""))
    m = re.match(r"^(\d+)_(\d{2})_?(.*)$", s)
    if m:
        rest = m.group(3).replace("_", " ").strip()
        return f"{m.group(1)}.{m.group(2)}" + (f" {rest.title()}" if rest else "")
    return s.replace("_", " ").strip()


def _grams(value: Any) -> str | None:
    try:
        g = float(value)
    except (TypeError, ValueError):
        return None
    if g <= 0:
        return None
    return f"{round(g)} g filament" if g >= 1 else f"{g:.1f} g filament"


def _time(value: Any) -> str | None:
    """'25m' -> '25 min'; '1h 5m' stays; empty -> None."""
    if not value:
        return None
    text = str(value).strip()
    m = re.fullmatch(r"(\d+)m", text)
    return f"{m.group(1)} min" if m else text


def compose(readiness: dict[str, Any], need: dict[str, Any]) -> tuple[str, str]:
    """Return ``(card_text, question_text)`` for the bed-clear turn.

    ``readiness`` is the workflow's ``kit_readiness_card`` event and ``need``
    its ``need_input`` event keyed ``bed_clear_start``. Missing fields are
    skipped rather than guessed."""
    lines: list[str] = []
    title = str(readiness.get("title") or "").strip()
    lines.append(f"\U0001F5A8️ **Ready to print: {title}**" if title
                 else "\U0001F5A8️ **Ready to print**")

    bits: list[str] = []
    for key in ("tool", "material"):
        if readiness.get(key):
            bits.append(str(readiness[key]))
    profile = (readiness.get("profile_label")
               or _humanize_profile(readiness.get("profile") or ""))
    if profile:
        bits.append(str(profile))
    supports = str(readiness.get("supports") or "").replace("-", "_")
    if supports in _SUPPORTS:
        bits.append(_SUPPORTS[supports])
    if str(readiness.get("orient") or "") == "auto":
        bits.append("auto-oriented")
    if bits:
        lines.append(" · ".join(bits))

    part_count = readiness.get("part_count")
    selected = readiness.get("selected_parts") or []
    if isinstance(part_count, int) and part_count > 1 and selected \
            and len(selected) < part_count:
        lines.append(f"{len(selected)} of {part_count} parts")

    est = readiness.get("estimates") or {}
    when = [x for x in (_time(est.get("total_time_str")),
                        _grams(est.get("total_grams"))) if x]
    if when:
        lines.append("⏱ " + " · ".join(when))

    plates = readiness.get("plate_count")
    if isinstance(plates, int) and plates > 1:
        lines.append(f"\U0001F4E6 {plates} plates. Plate 1 starts on YES; plates "
                     f"2–{plates} are on the printer to start from the "
                     f"Snapmaker app.")

    buckets = readiness.get("overhang_buckets") or {}
    recommended = buckets.get("recommended_indices") or []
    if supports == "no_supports" and recommended:
        which = ", ".join(str(i) for i in recommended)
        noun = "part" if len(recommended) == 1 else "parts"
        lines.append(f"⚠️ Supports are off, but {noun} {which} may need them.")

    if not need.get("bed_snapshot_path"):
        lines.append("\U0001F4F7 No bed photo this time. Check the bed yourself "
                     "before you reply YES.")

    return "\n".join(lines), QUESTION
