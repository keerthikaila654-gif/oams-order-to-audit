"""Unit tests for money, units, GST and ledger valuation."""
from decimal import Decimal
import pytest
from app.money import fmt_inr, mul_qty_price, round_div, to_milli, to_paise, pct
from app.units import convert_m
from app.services import sales, inventory as inv, InsufficientStock
from app.services.pdf import amount_in_words


def test_money_is_exact():
    assert to_paise("0.1") + to_paise("0.2") == to_paise("0.3")       # no float drift
    assert fmt_inr(787500) == "₹7,875.00" and fmt_inr(123456789) == "₹12,34,567.89"
    assert round_div(5, 2) == 3 and round_div(-5, 2) == -3


def test_unit_conversion():
    assert convert_m(to_milli(40), "kg", "g") == to_milli(40000)
    assert convert_m(to_milli(500), "g", "kg") == to_milli("0.5")
    assert convert_m(to_milli(2), "L", "ml") == to_milli(2000)
    assert convert_m(to_milli(5), "carton", "packet") == to_milli(100)
    with pytest.raises(ValueError):
        convert_m(1000, "kg", "L")


def test_gst_intra_state_cgst_sgst():
    l = sales.compute_lines([{"qty_m": to_milli(150), "price_paise": 5000, "gst_rate_bp": 500}], intra=True)[0]
    assert (l["taxable_paise"], l["cgst_paise"], l["sgst_paise"], l["igst_paise"], l["total_paise"]) == (750000, 18750, 18750, 0, 787500)


def test_gst_inter_state_igst():
    l = sales.compute_lines([{"qty_m": to_milli(88), "price_paise": 5000, "gst_rate_bp": 500}], intra=False)[0]
    assert (l["taxable_paise"], l["cgst_paise"], l["igst_paise"], l["total_paise"]) == (440000, 0, 22000, 462000)


def test_financial_year():
    assert sales.financial_year("2026-03-31") == "2025-26" and sales.financial_year("2026-04-01") == "2026-27"


def test_variance_pct():
    assert pct(to_milli("0.5"), to_milli(10)) == Decimal("5.00") and pct(-12, 200) == Decimal("-6.00")


def test_amount_in_words():
    assert amount_in_words(787500) == "Rupees Seven Thousand Eight Hundred Seventy Five Only"


def test_weighted_average_and_no_negative_stock(conn):
    flour = conn.execute("SELECT id FROM items WHERE code='RM-FLOUR'").fetchone()[0]
    inv.post_receipt(conn, flour, "PURCHASE", to_milli(60), to_paise(2700), "purchase", 1, "T")
    _, v = inv.post_issue(conn, flour, "PRODUCTION_ISSUE", to_milli(40), "production_run", 1, "T")
    assert v == 172500                      # 40 x (6900/160 = 43.125) = 1725.00
    with pytest.raises(InsufficientStock):
        inv.post_issue(conn, flour, "PRODUCTION_ISSUE", to_milli(1000), "production_run", 1, "T")
    assert inv.balance(conn, flour)[0] == to_milli(120)


def test_ledger_is_immutable(conn):
    import sqlite3
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM inventory_movements")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE inventory_movements SET qty_in_m=1")
