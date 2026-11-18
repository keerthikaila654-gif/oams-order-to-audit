"""Sales workflow: order -> verification -> GST invoice -> dispatch -> payment."""
from datetime import date

from ..db import tx, next_seq, get_setting
from ..money import mul_qty_price, round_div
from . import BusinessError, InsufficientStock
from . import inventory as inv
from .audit import log

ORDER_FLOW = ["submitted", "approved", "invoiced", "dispatched"]


# ───────────────────────── pricing ─────────────────────────
def price_for(conn, customer_id, product_id) -> int:
    """Customer-specific price > customer's price list > product base price."""
    r = conn.execute("SELECT price_paise FROM customer_prices WHERE customer_id=? AND product_id=?",
                     (customer_id, product_id)).fetchone()
    if r:
        return r[0]
    r = conn.execute("SELECT pli.price_paise FROM customers c JOIN price_list_items pli "
                     "ON pli.price_list_id=c.price_list_id AND pli.product_id=? WHERE c.id=?",
                     (product_id, customer_id)).fetchone()
    if r:
        return r[0]
    return conn.execute("SELECT base_price_paise FROM products WHERE id=?", (product_id,)).fetchone()[0]


# ───────────────────────── GST (pure functions, unit-tested) ─────────────────────────
def compute_lines(lines, intra: bool):
    """lines: [{qty_m, price_paise, gst_rate_bp}] -> same dicts + taxable/cgst/sgst/igst/total (paise).

    Intra-state supply  -> CGST + SGST, each half of the GST rate.
    Inter-state supply  -> IGST at the full rate.
    All arithmetic is integer paise with half-up rounding per line.
    """
    out = []
    for l in lines:
        taxable = mul_qty_price(l["qty_m"], l["price_paise"])
        bp = l["gst_rate_bp"]
        if intra:
            half = round_div(taxable * bp, 20000)
            cg, sg, ig = half, half, 0
        else:
            cg = sg = 0
            ig = round_div(taxable * bp, 10000)
        out.append({**l, "taxable_paise": taxable, "cgst_paise": cg, "sgst_paise": sg, "igst_paise": ig,
                    "total_paise": taxable + cg + sg + ig})
    return out


def financial_year(d: str) -> str:
    y, m = int(d[:4]), int(d[5:7])
    start = y if m >= 4 else y - 1
    return f"{start}-{str(start + 1)[-2:]}"


def seller(conn):
    keys = ["company_name", "company_address", "company_gstin", "company_state", "company_state_code",
            "company_phone", "company_email", "company_bank"]
    return {k: get_setting(conn, k, "") for k in keys}


# ───────────────────────── orders ─────────────────────────
def create_order(conn, user, customer_id, items, client_uuid=None, notes=None, order_date=None):
    """items: [(product_id, qty_m)].  Idempotent on client_uuid (offline sync can safely retry)."""
    with tx(conn):
        if client_uuid:
            ex = conn.execute("SELECT id FROM sales_orders WHERE client_uuid=?", (client_uuid,)).fetchone()
            if ex:
                return ex["id"], False
        cust = conn.execute("SELECT * FROM customers WHERE id=? AND active=1", (customer_id,)).fetchone()
        if not cust:
            raise BusinessError("Select a valid customer")
        if user["role"] == "salesman" and cust["salesman_user_id"] != user["id"]:
            raise BusinessError("This customer is not assigned to you")
        merged = {}
        for pid, q in items:
            if q <= 0:
                raise BusinessError("Quantities must be greater than zero")
            merged[pid] = merged.get(pid, 0) + q
        if not merged:
            raise BusinessError("Add at least one product to the order")
        n = next_seq(conn, "SO")
        ono = f"SO-{n:05d}"
        cur = conn.execute("INSERT INTO sales_orders(order_no,client_uuid,customer_id,salesman_user_id,order_date,notes) "
                           "VALUES (?,?,?,?,?,?)", (ono, client_uuid, customer_id, user["id"], order_date or inv.today(), notes))
        oid = cur.lastrowid
        for pid, q in merged.items():
            p = conn.execute("SELECT id FROM products WHERE id=? AND active=1", (pid,)).fetchone()
            if not p:
                raise BusinessError("Unknown or inactive product in order")
            conn.execute("INSERT INTO sales_order_items(order_id,product_id,qty_m,price_paise) VALUES (?,?,?,?)",
                         (oid, pid, q, price_for(conn, customer_id, pid)))
        log(conn, user, "create_order", "sales_order", oid, new={"items": merged, "customer_id": customer_id}, ref=ono)
        return oid, True


def get_order(conn, order_id):
    o = conn.execute("SELECT o.*, c.name AS customer_name, c.type AS customer_type, c.state AS customer_state, "
                     "c.state_code AS customer_state_code, c.gstin AS customer_gstin, u.full_name AS salesman_name "
                     "FROM sales_orders o JOIN customers c ON c.id=o.customer_id JOIN users u ON u.id=o.salesman_user_id "
                     "WHERE o.id=?", (order_id,)).fetchone()
    if not o:
        raise BusinessError("Order not found")
    return o


def order_items(conn, order_id):
    return conn.execute("SELECT oi.*, p.name AS product_name, p.item_id, p.hsn, p.gst_rate_bp "
                        "FROM sales_order_items oi JOIN products p ON p.id=oi.product_id WHERE oi.order_id=? ORDER BY oi.id",
                        (order_id,)).fetchall()


def reserved_qty(conn, product_id, exclude_order_id=None) -> int:
    """Stock promised to approved/invoiced orders that has not been dispatched yet."""
    r = conn.execute(
        "SELECT COALESCE(SUM(oi.qty_m),0) FROM sales_order_items oi JOIN sales_orders o ON o.id=oi.order_id "
        "WHERE oi.product_id=? AND o.status IN ('approved','invoiced') AND o.id != COALESCE(?,-1)",
        (product_id, exclude_order_id)).fetchone()[0]
    disp = conn.execute(
        "SELECT COALESCE(SUM(di.qty_m),0) FROM dispatch_items di JOIN dispatches d ON d.id=di.dispatch_id "
        "JOIN invoices inv ON inv.id=d.invoice_id JOIN sales_orders o ON o.id=inv.order_id "
        "WHERE di.product_id=? AND o.status IN ('approved','invoiced') AND inv.status='posted' AND o.id != COALESCE(?,-1)",
        (product_id, exclude_order_id)).fetchone()[0]
    return max(r - disp, 0)


def availability(conn, product_id, exclude_order_id=None):
    item_id = conn.execute("SELECT item_id FROM products WHERE id=?", (product_id,)).fetchone()[0]
    on_hand, _ = inv.balance(conn, item_id)
    reserved = reserved_qty(conn, product_id, exclude_order_id)
    return {"on_hand_m": on_hand, "reserved_m": reserved, "available_m": on_hand - reserved}


def stock_check(conn, order_id):
    """Per line: ordered vs available, shortage (thousandths).  `ok` is False when any shortage exists."""
    rows, ok = [], True
    for it in order_items(conn, order_id):
        a = availability(conn, it["product_id"], exclude_order_id=order_id)
        shortage = max(it["qty_m"] - a["available_m"], 0)
        ok = ok and shortage == 0
        rows.append({"product_id": it["product_id"], "product_name": it["product_name"], "ordered_m": it["qty_m"],
                     "on_hand_m": a["on_hand_m"], "reserved_m": a["reserved_m"], "available_m": a["available_m"],
                     "shortage_m": shortage})
    return {"rows": rows, "ok": ok}


def tax_preview(conn, order_id):
    o = get_order(conn, order_id)
    intra = o["customer_state_code"] == get_setting(conn, "company_state_code")
    lines = compute_lines([{"qty_m": i["qty_m"], "price_paise": i["price_paise"], "gst_rate_bp": i["gst_rate_bp"]}
                           for i in order_items(conn, order_id)], intra)
    keys = ("taxable_paise", "cgst_paise", "sgst_paise", "igst_paise", "total_paise")
    return {"tax_type": "INTRA" if intra else "INTER", "label": "CGST + SGST" if intra else "IGST",
            **{k: sum(l[k] for l in lines) for k in keys}}


def update_order_items(conn, user, order_id, changes):
    """Admin edit before approval. changes = {product_id: (qty_m, price_paise)}; qty 0 removes the line."""
    with tx(conn):
        o = get_order(conn, order_id)
        if o["status"] != "submitted":
            raise BusinessError("Only submitted orders can be edited")
        old = {i["product_id"]: [i["qty_m"], i["price_paise"]] for i in order_items(conn, order_id)}
        for pid, (q, price) in changes.items():
            if pid not in old:
                continue
            if price < 0 or q < 0:
                raise BusinessError("Quantity and price cannot be negative")
            if q == 0:
                conn.execute("DELETE FROM sales_order_items WHERE order_id=? AND product_id=?", (order_id, pid))
            else:
                conn.execute("UPDATE sales_order_items SET qty_m=?, price_paise=? WHERE order_id=? AND product_id=?",
                             (q, price, order_id, pid))
        if not conn.execute("SELECT 1 FROM sales_order_items WHERE order_id=?", (order_id,)).fetchone():
            raise BusinessError("An order must keep at least one line; reject it instead")
        new = {i["product_id"]: [i["qty_m"], i["price_paise"]] for i in order_items(conn, order_id)}
        log(conn, user, "edit_order", "sales_order", order_id, old=old, new=new, ref=o["order_no"])


def approve_order(conn, user, order_id):
    with tx(conn):
        o = get_order(conn, order_id)
        if o["status"] != "submitted":
            raise BusinessError(f"Order is already {o['status']}")
        chk = stock_check(conn, order_id)
        if not chk["ok"]:
            bad = [r for r in chk["rows"] if r["shortage_m"]]
            from ..money import fmt_qty
            raise BusinessError("Cannot approve - insufficient finished goods: " + "; ".join(
                f"{r['product_name']} ordered {fmt_qty(r['ordered_m'])}, available {fmt_qty(r['available_m'])}, "
                f"shortage {fmt_qty(r['shortage_m'])}" for r in bad) + ". Reduce the quantity or reject the order.")
        conn.execute("UPDATE sales_orders SET status='approved', approved_by=?, approved_at=datetime('now') WHERE id=?",
                     (user["id"], order_id))
        log(conn, user, "approve_order", "sales_order", order_id, old={"status": "submitted"},
            new={"status": "approved"}, ref=o["order_no"])


def reject_order(conn, user, order_id, reason):
    with tx(conn):
        o = get_order(conn, order_id)
        if o["status"] not in ("submitted", "approved"):
            raise BusinessError(f"A {o['status']} order cannot be rejected")
        conn.execute("UPDATE sales_orders SET status='rejected', reject_reason=? WHERE id=?", (reason or "Rejected by admin", order_id))
        log(conn, user, "reject_order", "sales_order", order_id, old={"status": o["status"]},
            new={"status": "rejected", "reason": reason}, ref=o["order_no"])


# ───────────────────────── invoicing ─────────────────────────
def generate_invoice(conn, user, order_id, invoice_date=None):
    with tx(conn):
        o = get_order(conn, order_id)
        if o["status"] != "approved":
            raise BusinessError("Only approved orders can be invoiced")
        if conn.execute("SELECT 1 FROM invoices WHERE order_id=? AND status='posted'", (order_id,)).fetchone():
            raise BusinessError("An invoice already exists for this order")
        cust = conn.execute("SELECT * FROM customers WHERE id=?", (o["customer_id"],)).fetchone()
        on = invoice_date or inv.today()
        fy = financial_year(on)
        conn.execute("INSERT OR IGNORE INTO invoice_sequences(fy,last) VALUES (?,0)", (fy,))
        conn.execute("UPDATE invoice_sequences SET last=last+1 WHERE fy=?", (fy,))
        seq = conn.execute("SELECT last FROM invoice_sequences WHERE fy=?", (fy,)).fetchone()[0]
        ino = f"INV/{fy}/{seq:04d}"
        intra = cust["state_code"] == get_setting(conn, "company_state_code")
        items = order_items(conn, order_id)
        lines = compute_lines([{"qty_m": i["qty_m"], "price_paise": i["price_paise"], "gst_rate_bp": i["gst_rate_bp"]}
                               for i in items], intra)
        tot = {k: sum(l[k] for l in lines) for k in ("taxable_paise", "cgst_paise", "sgst_paise", "igst_paise", "total_paise")}
        cur = conn.execute(
            "INSERT INTO invoices(invoice_no,fy,order_id,customer_id,invoice_date,customer_name,customer_gstin,customer_address,"
            "place_of_supply,place_of_supply_code,tax_type,taxable_paise,cgst_paise,sgst_paise,igst_paise,total_paise,created_by)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ino, fy, order_id, cust["id"], on, cust["name"], cust["gstin"], cust["address"], cust["state"],
             cust["state_code"], "INTRA" if intra else "INTER", tot["taxable_paise"], tot["cgst_paise"],
             tot["sgst_paise"], tot["igst_paise"], tot["total_paise"], user["id"]))
        iid = cur.lastrowid
        for it, ln in zip(items, lines):
            conn.execute(
                "INSERT INTO invoice_items(invoice_id,product_id,description,hsn,qty_m,price_paise,taxable_paise,gst_rate_bp,"
                "cgst_paise,sgst_paise,igst_paise,total_paise) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (iid, it["product_id"], it["product_name"], it["hsn"], it["qty_m"], it["price_paise"], ln["taxable_paise"],
                 it["gst_rate_bp"], ln["cgst_paise"], ln["sgst_paise"], ln["igst_paise"], ln["total_paise"]))
        conn.execute("UPDATE sales_orders SET status='invoiced' WHERE id=?", (order_id,))
        log(conn, user, "generate_invoice", "invoice", iid, new={**tot, "tax_type": "INTRA" if intra else "INTER"}, ref=ino)
        return iid


def get_invoice(conn, invoice_id):
    r = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not r:
        raise BusinessError("Invoice not found")
    return r


def invoice_lines(conn, invoice_id):
    return conn.execute("SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY id", (invoice_id,)).fetchall()


def dispatch_status(conn, invoice_id):
    """Per invoice line: invoiced, dispatched, remaining (thousandths)."""
    out = []
    for l in invoice_lines(conn, invoice_id):
        d = conn.execute("SELECT COALESCE(SUM(di.qty_m),0) FROM dispatch_items di JOIN dispatches d ON d.id=di.dispatch_id "
                         "WHERE d.invoice_id=? AND di.product_id=?", (invoice_id, l["product_id"])).fetchone()[0]
        out.append({"product_id": l["product_id"], "description": l["description"], "invoiced_m": l["qty_m"],
                    "dispatched_m": d, "remaining_m": l["qty_m"] - d})
    return out


# ───────────────────────── dispatch ─────────────────────────
def dispatch_invoice(conn, user, invoice_id, quantities=None, vehicle=None, dispatch_date=None):
    """Record dispatch (full by default, partial if `quantities`={product_id: qty_m}) and reduce FG stock."""
    with tx(conn):
        inv_row = get_invoice(conn, invoice_id)
        if inv_row["status"] != "posted":
            raise BusinessError("Cancelled invoices cannot be dispatched")
        status = dispatch_status(conn, invoice_id)
        todo = {}
        for s in status:
            q = s["remaining_m"] if quantities is None else quantities.get(s["product_id"], 0)
            if q < 0 or q > s["remaining_m"]:
                raise BusinessError(f"Dispatch quantity for {s['description']} exceeds the invoiced balance")
            if q > 0:
                todo[s["product_id"]] = q
        if not todo:
            raise BusinessError("Nothing to dispatch")
        n = next_seq(conn, "DSP")
        dno = f"DSP-{n:04d}"
        on = dispatch_date or inv.today()
        cur = conn.execute("INSERT INTO dispatches(dispatch_no,invoice_id,dispatch_date,vehicle,created_by) VALUES (?,?,?,?,?)",
                           (dno, invoice_id, on, vehicle, user["id"]))
        did = cur.lastrowid
        for pid, q in todo.items():
            item_id = conn.execute("SELECT item_id FROM products WHERE id=?", (pid,)).fetchone()[0]
            conn.execute("INSERT INTO dispatch_items(dispatch_id,product_id,qty_m) VALUES (?,?,?)", (did, pid, q))
            # raises InsufficientStock (and rolls the whole dispatch back) if the factory does not hold the goods
            inv.post_issue(conn, item_id, "DISPATCH", q, "dispatch", did, f"{dno} / {inv_row['invoice_no']}", user, on)
        remaining = sum(s["remaining_m"] - todo.get(s["product_id"], 0) for s in status)
        if remaining == 0 and inv_row["order_id"]:
            conn.execute("UPDATE sales_orders SET status='dispatched' WHERE id=?", (inv_row["order_id"],))
        log(conn, user, "dispatch", "dispatch", did, new={"qty": todo, "invoice": inv_row["invoice_no"]}, ref=dno)
        return did


# ───────────────────────── payments & receivables ─────────────────────────
def invoice_paid(conn, invoice_id) -> int:
    return conn.execute("SELECT COALESCE(SUM(amount_paise),0) FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone()[0]


def invoice_payment_status(conn, inv_row):
    paid = invoice_paid(conn, inv_row["id"])
    if inv_row["status"] == "cancelled":
        return "Cancelled", paid
    if paid >= inv_row["total_paise"]:
        return "Paid", paid
    return ("Partial" if paid else "Unpaid"), paid


def record_payment(conn, user, customer_id, amount_paise, invoice_id=None, mode="cash", reference=None, on=None):
    if amount_paise <= 0:
        raise BusinessError("Payment amount must be greater than zero")
    with tx(conn):
        if invoice_id:
            iv = get_invoice(conn, invoice_id)
            if iv["customer_id"] != customer_id:
                raise BusinessError("Invoice does not belong to this customer")
            if iv["status"] != "posted":
                raise BusinessError("Cannot record a payment against a cancelled invoice")
            bal = iv["total_paise"] - invoice_paid(conn, invoice_id)
            if amount_paise > bal:
                from ..money import fmt_inr
                raise BusinessError(f"Payment exceeds the invoice balance of {fmt_inr(bal)}")
        n = next_seq(conn, "RCT")
        rno = f"RCT-{n:04d}"
        cur = conn.execute("INSERT INTO payments(receipt_no,customer_id,invoice_id,amount_paise,mode,payment_date,reference,created_by)"
                           " VALUES (?,?,?,?,?,?,?,?)",
                           (rno, customer_id, invoice_id, amount_paise, mode, on or inv.today(), reference, user["id"]))
        log(conn, user, "record_payment", "payment", cur.lastrowid, new={"customer_id": customer_id, "invoice_id": invoice_id,
                                                                          "amount_paise": amount_paise}, ref=rno)
        return cur.lastrowid


def customer_outstanding(conn, customer_id) -> int:
    billed = conn.execute("SELECT COALESCE(SUM(total_paise),0) FROM invoices WHERE customer_id=? AND status='posted'",
                          (customer_id,)).fetchone()[0]
    paid = conn.execute("SELECT COALESCE(SUM(amount_paise),0) FROM payments WHERE customer_id=?", (customer_id,)).fetchone()[0]
    return billed - paid


def outstanding_by_customer(conn):
    rows = conn.execute("""
        SELECT c.id, c.name, c.type, c.state,
          COALESCE((SELECT SUM(total_paise) FROM invoices i WHERE i.customer_id=c.id AND i.status='posted'),0) AS billed_paise,
          COALESCE((SELECT SUM(amount_paise) FROM payments p WHERE p.customer_id=c.id),0) AS paid_paise
        FROM customers c WHERE c.active=1 ORDER BY c.name""").fetchall()
    return [dict(r, outstanding_paise=r["billed_paise"] - r["paid_paise"]) for r in rows]


# ───────────────────────── cancellation / credit note ─────────────────────────
def cancel_invoice(conn, user, invoice_id, reason):
    """Corrections are done by cancelling with a credit note - the invoice row is never deleted or edited.
    Goods already dispatched are returned to finished-goods stock at their original issue value."""
    if not reason or not reason.strip():
        raise BusinessError("A reason is required to cancel an invoice")
    with tx(conn):
        iv = get_invoice(conn, invoice_id)
        if iv["status"] != "posted":
            raise BusinessError("Invoice is already cancelled")
        fy = financial_year(inv.today())
        n = next_seq(conn, f"CN-{fy}")
        cn_no = f"CN/{fy}/{n:04d}"
        cur = conn.execute("INSERT INTO credit_notes(cn_no,invoice_id,cn_date,taxable_paise,cgst_paise,sgst_paise,igst_paise,"
                           "total_paise,reason,created_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (cn_no, invoice_id, inv.today(), iv["taxable_paise"], iv["cgst_paise"], iv["sgst_paise"],
                            iv["igst_paise"], iv["total_paise"], reason, user["id"]))
        for m in conn.execute("SELECT m.* FROM inventory_movements m JOIN dispatches d ON d.id=m.src_id "
                              "WHERE m.src_type='dispatch' AND m.txn_type='DISPATCH' AND d.invoice_id=?", (invoice_id,)).fetchall():
            inv.post_receipt(conn, m["item_id"], "RETURN", m["qty_out_m"], -m["value_paise"], "invoice", invoice_id,
                             f"{cn_no} / {iv['invoice_no']}", user, note="Goods returned - invoice cancelled")
        conn.execute("UPDATE invoices SET status='cancelled', cancel_reason=?, cancelled_at=datetime('now') WHERE id=?",
                     (reason, invoice_id))
        if iv["order_id"]:
            conn.execute("UPDATE sales_orders SET status='cancelled' WHERE id=?", (iv["order_id"],))
        log(conn, user, "cancel_invoice", "invoice", invoice_id, old={"status": "posted"},
            new={"status": "cancelled", "credit_note": cn_no, "reason": reason}, ref=iv["invoice_no"])
        return cur.lastrowid


# ───────────────────────── salesman views ─────────────────────────
def salesman_summary(conn, user_id, start=None, end=None):
    start = start or date.today().replace(day=1).isoformat()
    end = end or date.today().isoformat()
    by_status = {r[0]: r[1] for r in conn.execute(
        "SELECT status, COUNT(*) FROM sales_orders WHERE salesman_user_id=? AND order_date BETWEEN ? AND ? GROUP BY status",
        (user_id, start, end))}
    sales = conn.execute(
        "SELECT COALESCE(SUM(i.taxable_paise),0), COUNT(*) FROM invoices i JOIN sales_orders o ON o.id=i.order_id "
        "WHERE o.salesman_user_id=? AND i.status='posted' AND i.invoice_date BETWEEN ? AND ?", (user_id, start, end)).fetchone()
    return {"by_status": by_status, "invoiced_taxable_paise": sales[0], "invoice_count": sales[1],
            "orders_total": sum(by_status.values())}


def customer_sales_for_salesman(conn, user_id):
    return conn.execute("""
        SELECT c.id, c.name, c.type, c.state,
               COUNT(DISTINCT o.id) AS orders,
               COALESCE((SELECT SUM(i.taxable_paise) FROM invoices i JOIN sales_orders so ON so.id=i.order_id
                         WHERE so.customer_id=c.id AND so.salesman_user_id=? AND i.status='posted'),0) AS sales_paise,
               COALESCE((SELECT SUM(oi.qty_m*oi.price_paise/1000) FROM sales_order_items oi JOIN sales_orders so2 ON so2.id=oi.order_id
                         WHERE so2.customer_id=c.id AND so2.salesman_user_id=? AND so2.status NOT IN ('rejected','cancelled')),0) AS ordered_paise
        FROM customers c LEFT JOIN sales_orders o ON o.customer_id=c.id AND o.salesman_user_id=?
        WHERE c.salesman_user_id=? AND c.active=1 GROUP BY c.id ORDER BY c.name""",
        (user_id, user_id, user_id, user_id)).fetchall()
