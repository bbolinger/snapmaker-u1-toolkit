"""Per-material temperature envelopes for the optional bed/nozzle overrides.

Every bound here is SOURCED, not guessed:

- Nozzle ranges start from Snapmaker's OWN U1 filament presets' declared
  ``nozzle_temperature_range_low``/``high`` (manufacturer data, read off the
  box's Orca vendor tree), widened by the union with published material guides
  so a value that either source blesses is allowed. TPU's preset carries no
  range, so its bounds come from the guides.
- The U1 hardware caps the hotend at 300 C and the heated bed at 100 C
  (Snapmaker U1 published spec). Nothing is ever offered or accepted above the
  hotend cap; the bed cap keeps a little headroom (110) so ABS/ASA aren't
  rejected against Snapmaker's own 110 C profiles, since the firmware caps the
  real bed temp at its physical limit regardless.
- BED HAS NO PER-MATERIAL MINIMUM. A cool/cold plate (or simply running the bed
  off) is a legitimate setup, and a low bed only risks adhesion, never the
  hardware. Only the bed MAXIMUM is gated; bed-off (0) is always allowed.

Sources: Snapmaker U1 spec (snapmaker.com/snapmaker-u1/specs); Snapmaker on-box
filament presets; sovol3d and filamentcheatsheet material temperature guides.
"""
from __future__ import annotations

# U1 hardware limits (published spec). Hard backstops the gate never exceeds.
HOTEND_MAX_C = 300
# Bed headroom: the U1 spec bed max is 100 C, but Snapmaker's own ABS/ASA U1
# profiles request 110 C, so the gate allows up to 110 and lets the firmware
# cap the real temp. Keeps the gate from rejecting a stock profile value.
BED_MAX_C = 110

# (nozzle_min, nozzle_max, bed_max) per material. bed_min is always 0.
# Union of Snapmaker's on-box preset ranges and the published guides.
_RANGES: dict[str, tuple[int, int, int]] = {
    "PLA":     (190, 240, 70),
    "PETG":    (230, 270, 90),
    "ABS":     (230, 280, 110),
    "ASA":     (240, 280, 110),
    "TPU":     (200, 250, 60),
    "PLA-CF":  (200, 250, 70),
    "PETG-CF": (235, 275, 90),
}
# An unrecognized filament still gets a conservative envelope bounded only by the
# hardware caps (and a 170 C melt floor) so it can be nudged but never fried.
_FALLBACK = (170, HOTEND_MAX_C, BED_MAX_C)


def _key(material: str | None) -> str:
    return (material or "").strip().upper()


def nozzle_range(material: str | None) -> tuple[int, int]:
    """(min, max) nozzle temperature in C for a material, hardware-capped."""
    lo, hi, _bed = _RANGES.get(_key(material), _FALLBACK)
    return (max(0, lo), min(hi, HOTEND_MAX_C))


def bed_range(material: str | None) -> tuple[int, int]:
    """(min, max) bed temperature in C. min is ALWAYS 0 (cold plate / bed off)."""
    _lo, _hi, bed = _RANGES.get(_key(material), _FALLBACK)
    return (0, min(bed, BED_MAX_C))


def _clamp(val: float, lo: int, hi: int) -> int:
    return max(lo, min(int(round(float(val))), hi))


def clamp_nozzle(material: str | None, val: float) -> int:
    lo, hi = nozzle_range(material)
    return _clamp(val, lo, hi)


def clamp_bed(material: str | None, val: float) -> int:
    lo, hi = bed_range(material)
    return _clamp(val, lo, hi)


def nozzle_in_range(material: str | None, val: float) -> bool:
    lo, hi = nozzle_range(material)
    return lo <= float(val) <= hi


def bed_in_range(material: str | None, val: float) -> bool:
    lo, hi = bed_range(material)
    return lo <= float(val) <= hi


def _steps(lo: int, hi: int, step: int) -> list[int]:
    """Inclusive step list from the first multiple of ``step`` at or above lo,
    always including hi so the top of the range is reachable."""
    start = ((lo + step - 1) // step) * step
    out = list(range(start, hi + 1, step))
    if not out or out[-1] != hi:
        out.append(hi)
    return out


def offered_nozzle(material: str | None) -> list[int]:
    """Curated absolute nozzle values to offer in the form (10 C steps)."""
    lo, hi = nozzle_range(material)
    return _steps(lo, hi, 10)


def offered_bed(material: str | None) -> list[int]:
    """Curated absolute bed values to offer: 0 (off) plus 10 C steps up to the
    material's max. A cool/cold plate user picks a low value or off here."""
    _lo, hi = bed_range(material)
    vals = [0] + [v for v in _steps(10, hi, 10) if v > 0]
    # de-dup + sort (steps may re-add hi)
    return sorted(set(vals))
