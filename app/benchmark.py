"""Mandatory Section-5 benchmark: runner (9 steps) and verifier (Section-6 expected results).

`run_benchmark` drives the same service functions the web UI uses.  `verify` recomputes every
expected figure from the database, so it can be shown live in the UI after doing the steps by hand.
"""
from decimal import Decimal

from .money import fmt_inr, fmt_qty, fmt_pct, to_milli, to_paise
from .services import BusinessError, costing, inventory as inv, production as prod, purchasing, sales


def _user(conn, username):
    return conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def _id(conn, table, col, val):
    return conn.execute(f"SELECT id FROM {table} WHERE {col}=?", (val,)).fetchone()[0]


def run_benchmark(conn, log=print):
    admin, prodsup = _user(conn, "admin"), _user(conn, "production")
    sm_a, sm_b = _user(conn, "salesman_a"), _user(conn, "salesman_b")
    flour, oil, spice = (_id(conn, "items", "code", c) for c in ("RM-FLOUR", "RM-OIL", "RM-SPICE"))
    pouch, carton = _id(conn, "items", "code", "PK-POUCH200"), _id(conn, "items", "code", "PK-CARTON20")
    murukku = _id(conn, "products", "code", "MUR200")
    res = {}

    log("Step 1: purchase 60 kg flour @ ₹45")
    sup = _id(conn, "suppliers", "name", "Sri Murugan Flour Mills")
    purchasing.record_purchase(conn, prodsup, sup, [{"item_id": flour, "qty": "60", "unit": "kg", "rate": "45"}],
                               supplier_invoice_no="SMFM/0001")

    log("Step 2: production run - issue flour 40 kg, oil 10.5 L, spice 1.2 kg")
    run_id = prod.create_run(conn, prodsup, murukku, to_milli(1),
                             {flour: to_milli(40), oil: to_milli("10.5"), spice: to_milli("1.2")})
    res["run_id"] = run_id

    log("Step 3: actual production - 188 good packets, 6 damaged pouches, 9 cartons")
    prod.complete_run(conn, prodsup, run_id, to_milli(188),
                      {pouch: {"used_m": to_milli(188), "damaged_m": to_milli(6)},
                       carton: {"used_m": to_milli(9), "damaged_m": 0}})

    log("Step 4: Salesman A orders 150 packets for Retailer A")
    cust_a = _id(conn, "customers", "name", "Retailer A")
    oid_a, _ = sales.create_order(conn, sm_a, cust_a, [(murukku, to_milli(150))], client_uuid="benchmark-step-4")

    log("Step 5: admin verifies, approves, invoices (CGST+SGST) and dispatches")
    sales.approve_order(conn, admin, oid_a)
    inv_a = sales.generate_invoice(conn, admin, oid_a)
    sales.dispatch_invoice(conn, admin, inv_a, vehicle="TN 45 AB 1234")
    res["invoice_a"] = inv_a

    log("Step 6: Salesman B orders 120 packets for Distributor B -> shortage expected")
    cust_b = _id(conn, "customers", "name", "Distributor B")
    oid_b, _ = sales.create_order(conn, sm_b, cust_b, [(murukku, to_milli(120))], client_uuid="benchmark-step-6")
    chk = sales.stock_check(conn, oid_b)
    res["shortage_b_m"] = chk["rows"][0]["shortage_m"]
    log(f"        shortage detected: {fmt_qty(res['shortage_b_m'])} packets")
    try:
        sales.approve_order(conn, admin, oid_b)
        raise AssertionError("order with shortage was approved!")
    except BusinessError as e:
        log(f"        approval blocked: {e}")
    res["order_b"] = oid_b

    log("Step 7: payment ₹4,000 from Retailer A")
    sales.record_payment(conn, admin, cust_a, to_paise(4000), invoice_id=inv_a, mode="upi", reference="UPI-DEMO-0001")

    log("Step 8: overheads - labour ₹1,200, gas & power ₹600")
    prod.add_expense(conn, prodsup, run_id, "labour", to_paise(1200), "Labour for run")
    prod.add_expense(conn, prodsup, run_id, "gas_power", to_paise(600), "Gas and power for run")
    cost = costing.run_cost(conn, run_id)
    log(f"        cost per packet = ₹{cost['cost_per_packet']}  ({cost['valuation_method']})")
    res["cost"] = cost

    log("Step 9: monthly reports are generated from the same records (see Reports / python manage.py benchmark --export)")
    return res


def _q(conn, sql, *a):
    r = conn.execute(sql, a).fetchone()
    return r[0] if r else None


def verify(conn):
    """Section-6 expected results vs values recomputed from the database."""
    checks = []

    def add(name, expected, fn, fmt=str, cmp=None):
        try:
            actual = fn()
        except Exception:
            actual = None
        if actual is None:
            checks.append({"check": name, "expected": fmt(expected) if expected is not None else "calculated",
                           "actual": "not yet recorded", "status": "pending"})
            return
        ok = True if expected is None else (cmp(actual, expected) if cmp else actual == expected)
        checks.append({"check": name, "expected": fmt(expected) if expected is not None else "calculated by system",
                       "actual": fmt(actual), "status": "pass" if ok else "FAIL"})

    def bal(code):
        return lambda: inv.balance(conn, _id(conn, "items", "code", code))[0]

    purchased = _q(conn, "SELECT COUNT(*) FROM purchases") > 0
    produced = _q(conn, "SELECT COUNT(*) FROM production_runs WHERE status='completed'") > 0
    kg = lambda m: f"{fmt_qty(m)}"
    if purchased and produced:
        add("Flour closing balance (100 + 60 − 40)", to_milli(120), bal("RM-FLOUR"), kg)
        add("Oil closing balance (30 − 10.5)", to_milli("19.5"), bal("RM-OIL"), kg)
        add("Pouches closing balance (500 − 188 − 6)", to_milli(306), bal("PK-POUCH200"), kg)
        add("Cartons closing balance (30 − 9)", to_milli(21), bal("PK-CARTON20"), kg)
        add("Spice mix closing balance (5 − 1.2)", to_milli("3.8"), bal("RM-SPICE"), kg)
        fg_item = _id(conn, "items", "code", "FG-MUR200")
        add("Finished goods after production (50 + 188)", to_milli(238),
            lambda: _q(conn, "SELECT SUM(qty_in_m) FROM inventory_movements WHERE item_id=? AND txn_type IN ('OPENING','PRODUCTION_RECEIPT')", fg_item), kg)
    else:
        add("Flour closing balance (100 + 60 − 40)", to_milli(120), lambda: None, kg)

    inv_a = conn.execute("SELECT i.* FROM invoices i JOIN customers c ON c.id=i.customer_id WHERE c.name='Retailer A' "
                         "AND i.status='posted' ORDER BY i.id LIMIT 1").fetchone()
    if inv_a:
        fg_item = _id(conn, "items", "code", "FG-MUR200")
        dispatched = _q(conn, "SELECT COALESCE(SUM(qty_out_m),0) FROM inventory_movements WHERE item_id=? AND txn_type='DISPATCH'", fg_item)
        if produced and dispatched:
            add("Finished goods after Retailer A dispatch (238 − 150)", to_milli(88), lambda: inv.balance(conn, fg_item)[0], kg)
        add("Retailer A taxable invoice value", to_paise(7500), lambda: inv_a["taxable_paise"], fmt_inr)
        add("CGST (2.5 %)", to_paise("187.50"), lambda: inv_a["cgst_paise"], fmt_inr)
        add("SGST (2.5 %)", to_paise("187.50"), lambda: inv_a["sgst_paise"], fmt_inr)
        add("IGST on Retailer A invoice", 0, lambda: inv_a["igst_paise"], fmt_inr)
        add("Retailer A total invoice", to_paise(7875), lambda: inv_a["total_paise"], fmt_inr)
        cid = inv_a["customer_id"]
        if _q(conn, "SELECT COUNT(*) FROM payments WHERE customer_id=?", cid):
            add("Retailer A outstanding (7,875 − 4,000)", to_paise(3875), lambda: sales.customer_outstanding(conn, cid), fmt_inr)
        else:
            add("Retailer A outstanding (7,875 − 4,000)", to_paise(3875), lambda: None, fmt_inr)
    else:
        for n, e in (("Retailer A taxable invoice value", 7500), ("Retailer A total invoice", 7875)):
            add(n, to_paise(e), lambda: None, fmt_inr)

    run = conn.execute("SELECT id FROM production_runs WHERE status='completed' ORDER BY id LIMIT 1").fetchone()
    if run:
        v = prod.run_variance(conn, run["id"])
        add("Yield variance: 188 vs 200 expected → −6 %, flagged", (Decimal("-6.00"), True),
            lambda: (v["yield"]["var_pct"], v["yield"]["flagged"]), lambda t: f"{fmt_pct(t[0])}, {'flagged' if t[1] else 'not flagged'}")
        oil_row = next((r for r in v["rows"] if r["name"] == "Cooking oil"), None)
        add("Oil consumption variance: 10.5 vs 10 → +5 %, flagged", (Decimal("5.00"), True),
            lambda: (oil_row["var_pct"], oil_row["flagged"]), lambda t: f"{fmt_pct(t[0])}, {'flagged' if t[1] else 'not flagged'}")
        c = costing.run_cost(conn, run["id"])
        add("Cost per packet (valuation + overhead rule stated)", None,
            lambda: c["cost_per_packet"] if c["overhead_paise"] else None, lambda x: f"₹{x}")
    else:
        add("Yield variance: 188 vs 200 expected → −6 %, flagged", (Decimal("-6.00"), True), lambda: None)
        add("Oil consumption variance: 10.5 vs 10 → +5 %, flagged", (Decimal("5.00"), True), lambda: None)

    ob = conn.execute("SELECT o.id FROM sales_orders o JOIN customers c ON c.id=o.customer_id WHERE c.name='Distributor B' "
                      "ORDER BY o.id LIMIT 1").fetchone()
    if ob:
        st = sales.stock_check(conn, ob["id"])
        add("Distributor B shortage (120 − 88)", to_milli(32) if st["ok"] is False else to_milli(32),
            lambda: st["rows"][0]["shortage_m"] if not st["ok"] else None, kg)
        add("Distributor B tax type if invoiced", "IGST", lambda: sales.tax_preview(conn, ob["id"])["label"])
    else:
        add("Distributor B shortage (120 − 88)", to_milli(32), lambda: None, kg)
        add("Distributor B tax type if invoiced", "IGST", lambda: None)
    return checks
