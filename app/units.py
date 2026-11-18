"""Unit conversion.

Every unit belongs to a *dimension*; conversion is only allowed inside one
dimension.  ``factor`` is the multiple of the dimension's reference unit.

    mass:    kg (ref), g = 0.001 kg
    volume:  L  (ref), ml = 0.001 L
    count:   pc (ref)                       -- pouches, cartons (packing material), labels
    packet:  packet (ref), carton = 20 packets  -- finished goods only
"""
from decimal import Decimal, ROUND_HALF_UP

UNITS = {
    "kg": ("mass", Decimal(1)),
    "g": ("mass", Decimal("0.001")),
    "L": ("volume", Decimal(1)),
    "ml": ("volume", Decimal("0.001")),
    "pc": ("count", Decimal(1)),
    "packet": ("packet", Decimal(1)),
    "carton": ("packet", Decimal(20)),
}

UNIT_CHOICES = list(UNITS)


def dimension(unit: str) -> str:
    try:
        return UNITS[unit][0]
    except KeyError:
        raise ValueError(f"Unknown unit '{unit}'")


def convert_m(qty_m: int, from_unit: str, to_unit: str) -> int:
    """Convert a quantity in thousandths from one unit to another (exact Decimal)."""
    if from_unit == to_unit:
        return int(qty_m)
    for u in (from_unit, to_unit):
        if u not in UNITS:
            raise ValueError(f"Unknown unit '{u}'")
    (fd, ff), (td, tf) = UNITS[from_unit], UNITS[to_unit]
    if fd != td:
        raise ValueError(f"Cannot convert {from_unit} ({fd}) to {to_unit} ({td})")
    return int((Decimal(qty_m) * ff / tf).quantize(Decimal(1), ROUND_HALF_UP))


def compatible(unit_a: str, unit_b: str) -> bool:
    return unit_a in UNITS and unit_b in UNITS and UNITS[unit_a][0] == UNITS[unit_b][0]
