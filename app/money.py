"""Decimal / integer helpers.

Conventions used across the whole code base
-------------------------------------------
* Money is stored as INTEGER **paise** (1 rupee = 100 paise). No floats.
* Quantities are stored as INTEGER **thousandths** of the item's base unit
  (``*_m`` columns).  40 kg flour -> 40000, 10.5 L oil -> 10500.
* Percentages / rates are Decimal (tax rates are stored as basis points).
"""
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

Q0 = Decimal("1")
Q2 = Decimal("0.01")


def D(x) -> Decimal:
    """Convert user input to Decimal safely (never via float arithmetic)."""
    if isinstance(x, Decimal):
        return x
    if x is None or str(x).strip() == "":
        return Decimal(0)
    try:
        return Decimal(str(x).strip().replace(",", ""))
    except InvalidOperation:
        raise ValueError(f"Invalid number: {x!r}")


def round_div(n: int, d: int) -> int:
    """Integer n/d rounded half away from zero (exact, no floats)."""
    return int((Decimal(n) / Decimal(d)).quantize(Q0, ROUND_HALF_UP))


def to_paise(rupees) -> int:
    return int((D(rupees) * 100).quantize(Q0, ROUND_HALF_UP))


def to_milli(qty) -> int:
    return int((D(qty) * 1000).quantize(Q0, ROUND_HALF_UP))


def milli_to_dec(m: int) -> Decimal:
    return Decimal(m) / 1000


def paise_to_dec(p: int) -> Decimal:
    return Decimal(p) / 100


def mul_qty_price(qty_m: int, price_paise: int) -> int:
    """qty (thousandths) x price (paise per unit) -> paise, rounded half-up."""
    return round_div(qty_m * price_paise, 1000)


def pct(part: Decimal, whole: Decimal):
    """(part/whole)*100 to 2 dp, or None when whole is zero."""
    if not whole:
        return None
    return (Decimal(part) * 100 / Decimal(whole)).quantize(Q2, ROUND_HALF_UP)


def fmt_qty(m) -> str:
    if m is None:
        return ""
    d = (Decimal(int(m)) / 1000).normalize()
    s = format(d, "f")
    return s


def fmt_inr(p, symbol=True) -> str:
    if p is None:
        return ""
    p = int(p)
    sign = "-" if p < 0 else ""
    rupees, paisa = divmod(abs(p), 100)
    s = str(rupees)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups + [tail])
    return f"{sign}{'₹' if symbol else ''}{s}.{paisa:02d}"


def fmt_pct(x) -> str:
    if x is None:
        return "–"
    x = Decimal(x)
    return f"{x:+.2f}%"


def plain_money(p: int) -> str:
    """Machine friendly rupees string, e.g. 787500 -> '7875.00'."""
    return f"{Decimal(p) / 100:.2f}"
