"""End-to-end benchmark (Section 5/6) through the service layer, plus business-rule checks."""
from decimal import Decimal
import sqlite3
import pytest
from app import benchmark
from app.money import to_milli, to_paise
from app.services import BusinessError, sales, costing, inventory as inv, production as prod


@pytest.fixture()
def done(conn):
    return benchmark.run_benchmark(conn, log=lambda *_: None)


def test_all_expected_results_pass(conn, done):
    checks = benchmark.verify(conn)
    assert checks and all(c["status"] == "pass" for c in checks), [c for c in checks if c["status"] != "pass"]


def test_cost_per_packet(conn, done):
    c = costing.run_cost(conn, done["run_id"])
    assert (c["rm_paise"], c["pkg_paise"], c["overhead_paise"], c["total_paise"]) == (343500, 62000, 180000, 585500)
    assert c["cost_per_packet"] == Decimal("31.14") and c["margin_pct"] == Decimal("37.71")


def test_shortage_blocks_approval_and_dispatch(conn, done):
    admin = conn.execute("SELECT * FROM users WHERE username='admin'").fetchone()
    with pytest.raises(BusinessError, match="shortage 32"):
        sales.approve_order(conn, admin, done["order_b"])
    assert sales.stock_check(conn, done["order_b"])["rows"][0]["shortage_m"] == to_milli(32)
    assert sales.tax_preview(conn, done["order_b"])["tax_type"] == "INTER"


def test_every_movement_traceable_and_ledger_reconciles(conn, done):
    assert conn.execute("SELECT COUNT(*) FROM inventory_movements WHERE ref_no IS NULL OR ref_no='' OR src_type IS NULL").fetchone()[0] == 0
    assert inv.reconcile(conn) == []


def test_invoice_cannot_be_deleted_or_edited(conn, done):
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM invoices")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE invoices SET total_paise=1")


def test_cancel_invoice_returns_stock_with_credit_note(conn, done):
    admin = conn.execute("SELECT * FROM users WHERE username='admin'").fetchone()
    sales.cancel_invoice(conn, admin, done["invoice_a"], "Wrong customer")
    fg = conn.execute("SELECT id FROM items WHERE code='FG-MUR200'").fetchone()[0]
    assert inv.balance(conn, fg)[0] == to_milli(238)
    assert conn.execute("SELECT COUNT(*) FROM credit_notes").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM invoices").fetchone()[0] == "cancelled"


def test_overpayment_rejected(conn, done):
    admin = conn.execute("SELECT * FROM users WHERE username='admin'").fetchone()
    cust = conn.execute("SELECT customer_id FROM invoices").fetchone()[0]
    with pytest.raises(BusinessError):
        sales.record_payment(conn, admin, cust, to_paise(99999), invoice_id=done["invoice_a"])


def test_failed_dispatch_rolls_back(conn, done):
    """Dispatch of goods the factory does not hold must be refused atomically."""
    admin = conn.execute("SELECT * FROM users WHERE username='admin'").fetchone()
    sm = conn.execute("SELECT * FROM users WHERE username='salesman_b'").fetchone()
    p = conn.execute("SELECT id FROM products WHERE code='MUR200'").fetchone()[0]
    oid, _ = sales.create_order(conn, sm, conn.execute("SELECT id FROM customers WHERE name='Distributor B'").fetchone()[0], [(p, to_milli(80))])
    sales.approve_order(conn, admin, oid)
    iid = sales.generate_invoice(conn, admin, oid)
    # drain stock by a manual adjustment, then dispatching must fail and leave no partial records
    fg = conn.execute("SELECT id FROM items WHERE code='FG-MUR200'").fetchone()[0]
    prodsup = conn.execute("SELECT * FROM users WHERE username='production'").fetchone()
    inv.adjust_stock(conn, prodsup, fg, -to_milli(50), "count correction")
    before = conn.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0]
    with pytest.raises(BusinessError):
        sales.dispatch_invoice(conn, admin, iid)
    assert conn.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == before


def test_bom_second_pack_size_shares_bom(conn, done):
    """Interview-style change: a 500 g pack reusing the same BOM needs only master data."""
    admin = conn.execute("SELECT * FROM users WHERE username='admin'").fetchone()
    from app import web  # noqa
    item = conn.execute("INSERT INTO items(code,name,kind,base_unit) VALUES ('FG-MUR500','Murukku 500 g','FG','packet')").lastrowid
    bom = conn.execute("SELECT bom_id FROM products WHERE code='MUR200'").fetchone()[0]
    pid = conn.execute("INSERT INTO products(code,name,category_id,pack_size,hsn,gst_rate_bp,item_id,bom_id,base_price_paise) "
                       "VALUES ('MUR500','Murukku 500 g',1,'500 g','2106',500,?,?,12000)", (item, bom)).lastrowid
    req = prod.requirements(conn, pid, to_milli(1))
    assert [l["planned_m"] for l in req["lines"] if l["name"] == "Flour"] == [to_milli(40)]
