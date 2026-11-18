"""The 17 audit reports (+ GSTR-1 / GSTR-3B mapping) as pure queries over the transaction tables.

Each report returns:  {slug, title, columns: [(header, kind)], rows: [tuple], totals: tuple|None, note}
Cell kinds:  text | date | money (paise int) | qty (thousandths int) | dec (Decimal rupees) | pct (Decimal) | int
"""
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from ..money import pct, round_div
from . import costing, inventory as inv, production as prod, sales


def default_range():
    t = date.today()
    return t.replace(day=1).isoformat(), t.isoformat()


def _R(slug, title, columns, rows, totals=None, note=None):
    return {"slug": slug, "title": title, "columns": columns, "rows": [tuple(r) for r in rows],
            "totals": tuple(totals) if totals else None, "note": note}


def _sum(rows, idx):
    return sum((r[idx] or 0) for r in rows)


# 1 ──────────────────────────────────────────────────────────────
def sales_register(conn, start, end):
    rows = conn.execute(
        "SELECT invoice_date, invoice_no, customer_name, COALESCE(customer_gstin,'Unregistered'), place_of_supply, "
        "taxable_paise, cgst_paise, sgst_paise, igst_paise, total_paise, status FROM invoices "
        "WHERE invoice_date BETWEEN ? AND ? ORDER BY invoice_date, id", (start, end)).fetchall()
    live = [r for r in rows if r[10] == "posted"]
    cols = [("Date", "date"), ("Invoice no.", "text"), ("Customer", "text"), ("GSTIN", "text"), ("Place of supply", "text"),
            ("Taxable", "money"), ("CGST", "money"), ("SGST", "money"), ("IGST", "money"), ("Total", "money"), ("Status", "text")]
    return _R("sales_register", "Sales register", cols, rows,
              ("Total (posted invoices)", "", "", "", "", *(_sum(live, i) for i in range(5, 10)), ""),
              "Cancelled invoices are listed for completeness but excluded from totals.")


# 2 ──────────────────────────────────────────────────────────────
def customer_sales(conn, start, end):
    rows = conn.execute(
        "SELECT c.name, c.type, COUNT(i.id), COALESCE(SUM(i.taxable_paise),0), "
        "COALESCE(SUM(i.cgst_paise+i.sgst_paise+i.igst_paise),0), COALESCE(SUM(i.total_paise),0) "
        "FROM customers c JOIN invoices i ON i.customer_id=c.id AND i.status='posted' AND i.invoice_date BETWEEN ? AND ? "
        "GROUP BY c.id ORDER BY 6 DESC", (start, end)).fetchall()
    cols = [("Customer", "text"), ("Type", "text"), ("Invoices", "int"), ("Taxable", "money"), ("GST", "money"), ("Total", "money")]
    return _R("customer_sales", "Customer-wise sales", cols, rows, ("Total", "", _sum(rows, 2), _sum(rows, 3), _sum(rows, 4), _sum(rows, 5)))


# 3 ──────────────────────────────────────────────────────────────
def product_sales(conn, start, end):
    rows = conn.execute(
        "SELECT ii.description, ii.hsn, SUM(ii.qty_m), SUM(ii.taxable_paise), "
        "SUM(ii.cgst_paise+ii.sgst_paise+ii.igst_paise), SUM(ii.total_paise) "
        "FROM invoice_items ii JOIN invoices i ON i.id=ii.invoice_id WHERE i.status='posted' AND i.invoice_date BETWEEN ? AND ? "
        "GROUP BY ii.product_id ORDER BY 4 DESC", (start, end)).fetchall()
    cols = [("Product", "text"), ("HSN", "text"), ("Qty (packets)", "qty"), ("Taxable", "money"), ("GST", "money"), ("Total", "money")]
    return _R("product_sales", "Product-wise sales", cols, rows, ("Total", "", _sum(rows, 2), _sum(rows, 3), _sum(rows, 4), _sum(rows, 5)))


# 4 ──────────────────────────────────────────────────────────────
def gst_summary(conn, start, end):
    def agg(where, args=()):
        r = conn.execute(f"SELECT COUNT(*), COALESCE(SUM(taxable_paise),0), COALESCE(SUM(igst_paise),0), "
                         f"COALESCE(SUM(cgst_paise),0), COALESCE(SUM(sgst_paise),0) FROM invoices "
                         f"WHERE status='posted' AND invoice_date BETWEEN ? AND ? {where}", (start, end, *args)).fetchone()
        return tuple(r)

    def row(table, desc, a):
        return (table, desc, a[0], a[1], a[2], a[3], a[4], a[2] + a[3] + a[4])

    rows = [
        row("GSTR-1 · 4A/4B  B2B", "Supplies to registered persons (GSTIN)", agg("AND customer_gstin IS NOT NULL")),
        row("GSTR-1 · 7  B2C (Small)", "Supplies to unregistered persons", agg("AND customer_gstin IS NULL")),
        row("GSTR-1 · Intra-state", "Place of supply = Tamil Nadu (CGST + SGST)", agg("AND tax_type='INTRA'")),
        row("GSTR-1 · Inter-state", "Place of supply outside Tamil Nadu (IGST)", agg("AND tax_type='INTER'")),
    ]
    cn = conn.execute("SELECT COUNT(*), COALESCE(SUM(c.taxable_paise),0), COALESCE(SUM(c.igst_paise),0), COALESCE(SUM(c.cgst_paise),0), "
                      "COALESCE(SUM(c.sgst_paise),0) FROM credit_notes c WHERE c.cn_date BETWEEN ? AND ?", (start, end)).fetchone()
    rows.append(row("GSTR-1 · 9B  Credit notes", "Credit notes issued (cancelled invoices)", tuple(cn)))
    for h in conn.execute(
            "SELECT ii.hsn, ii.gst_rate_bp, SUM(ii.qty_m), SUM(ii.taxable_paise), SUM(ii.igst_paise), SUM(ii.cgst_paise), SUM(ii.sgst_paise) "
            "FROM invoice_items ii JOIN invoices i ON i.id=ii.invoice_id WHERE i.status='posted' AND i.invoice_date BETWEEN ? AND ? "
            "GROUP BY ii.hsn, ii.gst_rate_bp ORDER BY ii.hsn", (start, end)):
        rows.append(("GSTR-1 · 12  HSN summary", f"HSN {h[0]} @ {Decimal(h[1]) / 100:.1f}%  (qty {Decimal(h[2]) / 1000:.0f})",
                     "", h[3], h[4], h[5], h[6], h[4] + h[5] + h[6]))
    t = agg("")
    rows.append(row("GSTR-3B · 3.1(a)", "Outward taxable supplies (other than zero-rated, nil-rated, exempt)", t))
    rows.append(row("GSTR-3B · 3.2", "Inter-state supplies to unregistered persons", agg("AND customer_gstin IS NULL AND tax_type='INTER'")))
    cols = [("Return · table", "text"), ("Description", "text"), ("Docs", "int"), ("Taxable value", "money"), ("IGST", "money"),
            ("CGST", "money"), ("SGST", "money"), ("Total tax", "money")]
    return _R("gst_summary", "GST outward supplies summary (GSTR-1 / GSTR-3B mapping)", cols, rows,
              note="Posted (non-cancelled) invoices only. Input tax credit (GSTR-3B 4) is not tracked because purchase invoices "
                   "are recorded without tax breakup.")


# 5 ──────────────────────────────────────────────────────────────
def purchase_register(conn, start, end):
    rows = conn.execute(
        "SELECT p.purchase_date, p.purchase_no, s.name, COALESCE(p.supplier_invoice_no,''), i.name, pi.entered_qty_m, pi.entered_unit, "
        "pi.rate_per_entered_unit, pi.amount_paise FROM purchase_items pi JOIN purchases p ON p.id=pi.purchase_id "
        "JOIN suppliers s ON s.id=p.supplier_id JOIN items i ON i.id=pi.item_id "
        "WHERE p.purchase_date BETWEEN ? AND ? ORDER BY p.purchase_date, p.id, pi.id", (start, end)).fetchall()
    rows = [(*r[:7], Decimal(r[7]), r[8]) for r in rows]
    cols = [("Date", "date"), ("Purchase no.", "text"), ("Supplier", "text"), ("Supplier inv.", "text"), ("Item", "text"),
            ("Qty", "qty"), ("Unit", "text"), ("Rate ₹/unit", "dec"), ("Amount", "money")]
    return _R("purchase_register", "Purchase register", cols, rows, ("Total", "", "", "", "", "", "", "", _sum(rows, 8)))


# 6-8 ────────────────────────────────────────────────────────────
def _stock(conn, slug, title, kind, start, end):
    rows = []
    for it in conn.execute("SELECT * FROM items WHERE kind=? ORDER BY name", (kind,)):
        q = lambda w, *a: conn.execute(f"SELECT COALESCE(SUM(qty_in_m),0), COALESCE(SUM(qty_out_m),0), COALESCE(SUM(value_paise),0) "
                                       f"FROM inventory_movements WHERE item_id=? AND {w}", (it["id"], *a)).fetchone()
        # OPENING-balance entries always count as opening, even when dated inside the period
        o = q("(moved_on < ? OR (txn_type = 'OPENING' AND moved_on <= ?))", start, end)
        p = q("moved_on BETWEEN ? AND ? AND txn_type != 'OPENING'", start, end)
        opening, receipts, issues = o[0] - o[1], p[0], p[1]
        closing = opening + receipts - issues
        value = o[2] + p[2]
        avg = (Decimal(value) / 100) / (Decimal(closing) / 1000) if closing else Decimal(0)
        status = "LOW" if kind != "FG" and it["reorder_level_m"] and closing <= it["reorder_level_m"] else "OK"
        rows.append((it["name"], it["base_unit"], opening, receipts, issues, closing, avg.quantize(Decimal("0.01"), ROUND_HALF_UP), value,
                     it["reorder_level_m"] if kind != "FG" else None, status if kind != "FG" else ""))
    cols = [("Item", "text"), ("Unit", "text"), ("Opening", "qty"), ("Receipts", "qty"), ("Issues / sales", "qty"),
            ("Closing", "qty"), ("Avg cost ₹/unit", "dec"), ("Value", "money"), ("Reorder level", "qty"), ("Status", "text")]
    return _R(slug, title, cols, rows, ("Total value", "", "", "", "", "", "", _sum(rows, 7), "", ""),
              "Opening + Receipts − Issues = Closing. Value at weighted-average cost.")


def rm_stock(conn, s, e): return _stock(conn, "rm_stock", "Raw-material stock summary", "RM", s, e)
def pkg_stock(conn, s, e): return _stock(conn, "pkg_stock", "Packaging stock summary", "PKG", s, e)
def fg_stock(conn, s, e): return _stock(conn, "fg_stock", "Finished-goods stock summary", "FG", s, e)


# 9 ──────────────────────────────────────────────────────────────
def stock_ledger(conn, start, end, item_id=None):
    rows = [(r["moved_on"], r["item"], r["base_unit"], r["txn_type"], r["qty_in_m"], r["qty_out_m"], r["balance_m"],
             abs(r["value_paise"]), r["src_type"], r["ref_no"], r["batch_no"] or "", r["note"] or "")
            for r in inv.ledger(conn, item_id, start, end)]
    cols = [("Date", "date"), ("Item", "text"), ("Unit", "text"), ("Transaction", "text"), ("Qty received", "qty"),
            ("Qty issued", "qty"), ("Balance", "qty"), ("Value", "money"), ("Source doc", "text"), ("Reference", "text"),
            ("Batch", "text"), ("Note", "text")]
    return _R("stock_ledger", "Stock ledger", cols, rows, note="Running balance is computed over the full movement history; "
                                                              "every row links to its source document.")


# 10-13 ──────────────────────────────────────────────────────────
def production_report(conn, start, end):
    rows = []
    for r in prod.list_runs(conn, start, end)[::-1]:
        c = costing.run_cost(conn, r["id"])
        rows.append((r["run_date"], r["run_no"], r["product_name"], r["batches_m"], r["expected_output_m"], r["actual_output_m"],
                     r["status"], c["rm_paise"] + c["pkg_paise"], c["overhead_paise"], c["total_paise"]))
    cols = [("Date", "date"), ("Run", "text"), ("Product", "text"), ("Batches", "qty"), ("Expected packets", "qty"),
            ("Actual packets", "qty"), ("Status", "text"), ("Material + packaging", "money"), ("Overheads", "money"), ("Total cost", "money")]
    return _R("production_report", "Production report", cols, rows, ("Total", "", "", "", _sum(rows, 4), _sum(rows, 5), "",
                                                                       _sum(rows, 7), _sum(rows, 8), _sum(rows, 9)))


def planned_vs_actual(conn, start, end):
    rows = []
    for r in prod.list_runs(conn, start, end)[::-1]:
        v = prod.run_variance(conn, r["id"])
        for m in v["rows"]:
            rows.append((r["run_no"], m["name"], m["unit"], m["planned_m"], m["actual_m"] if m["actual_m"] is not None else 0,
                         m["var_m"] if m["var_m"] is not None else 0, m["var_pct"], "FLAG" if m["flagged"] else ("pending" if m["pending"] else "ok")))
    cols = [("Run", "text"), ("Material", "text"), ("Unit", "text"), ("Planned", "qty"), ("Actual", "qty"), ("Variance", "qty"),
            ("Variance %", "pct"), ("Flag", "text")]
    return _R("planned_vs_actual", "Planned-versus-actual consumption", cols, rows,
              note=f"Variance % = (actual − planned) ÷ planned × 100. Flag when |variance| exceeds the tolerance "
                   f"(currently {prod.tolerance(conn)} %).")


def yield_report(conn, start, end):
    rows = []
    for r in prod.list_runs(conn, start, end)[::-1]:
        if r["status"] != "completed":
            continue
        y = prod.run_variance(conn, r["id"])["yield"]
        rows.append((r["run_no"], r["product_name"], y["expected_m"], y["actual_m"], y["var_m"], y["var_pct"], y["wastage_pct"],
                     "FLAG" if y["flagged"] else "ok"))
    cols = [("Run", "text"), ("Product", "text"), ("Expected packets", "qty"), ("Actual packets", "qty"), ("Variance", "qty"),
            ("Yield variance %", "pct"), ("Wastage %", "pct"), ("Flag", "text")]
    return _R("yield_report", "Production yield report", cols, rows)


def wastage_report(conn, start, end):
    rows = conn.execute(
        "SELECT r.run_date, r.run_no, w.kind, COALESCE(i.name,'Finished product'), w.qty_m, w.unit, w.value_paise, COALESCE(w.reason,'') "
        "FROM wastage w JOIN production_runs r ON r.id=w.run_id LEFT JOIN items i ON i.id=w.item_id "
        "WHERE r.run_date BETWEEN ? AND ? ORDER BY w.id", (start, end)).fetchall()
    cols = [("Date", "date"), ("Run", "text"), ("Type", "text"), ("Item", "text"), ("Qty", "qty"), ("Unit", "text"),
            ("Value at cost", "money"), ("Reason", "text")]
    return _R("wastage_report", "Wastage report", cols, rows, ("Total", "", "", "", "", "", _sum(rows, 6), ""))


# 14 ─────────────────────────────────────────────────────────────
def costing_report(conn, start, end):
    rows = []
    for c in costing.completed_run_costs(conn, start, end)[::-1]:
        rows.append((c["run"]["run_no"], c["product"]["name"], c["rm_paise"], c["pkg_paise"], c["labour_paise"], c["gas_power_paise"],
                     c["other_paise"], c["total_paise"], c["good_m"], c["cost_per_packet"], c["selling_price"], c["margin_pct"]))
    cols = [("Run", "text"), ("Product", "text"), ("Raw material", "money"), ("Packaging", "money"), ("Labour", "money"),
            ("Gas & power", "money"), ("Other", "money"), ("Total cost", "money"), ("Good packets", "qty"),
            ("Cost / packet ₹", "dec"), ("Selling price ₹", "dec"), ("Margin %", "pct")]
    return _R("costing_report", "Product costing report", cols, rows,
              note=f"{costing.VALUATION_METHOD} {costing.OVERHEAD_RULE}")


# 15-17 ──────────────────────────────────────────────────────────
def collections(conn, start, end):
    rows = conn.execute(
        "SELECT p.payment_date, p.receipt_no, c.name, COALESCE(i.invoice_no,'On account'), p.mode, COALESCE(p.reference,''), p.amount_paise "
        "FROM payments p JOIN customers c ON c.id=p.customer_id LEFT JOIN invoices i ON i.id=p.invoice_id "
        "WHERE p.payment_date BETWEEN ? AND ? ORDER BY p.payment_date, p.id", (start, end)).fetchall()
    cols = [("Date", "date"), ("Receipt", "text"), ("Customer", "text"), ("Invoice", "text"), ("Mode", "text"),
            ("Reference", "text"), ("Amount", "money")]
    return _R("collections", "Payment and collection report", cols, rows, ("Total collected", "", "", "", "", "", _sum(rows, 6)))


def receivables(conn, start, end):
    rows = []
    for c in conn.execute("SELECT * FROM customers WHERE active=1 ORDER BY name"):
        billed = conn.execute("SELECT COALESCE(SUM(total_paise),0) FROM invoices WHERE customer_id=? AND status='posted' AND invoice_date<=?",
                              (c["id"], end)).fetchone()[0]
        paid = conn.execute("SELECT COALESCE(SUM(amount_paise),0) FROM payments WHERE customer_id=? AND payment_date<=?",
                            (c["id"], end)).fetchone()[0]
        if billed or paid:
            rows.append((c["name"], c["type"], billed, paid, billed - paid))
    cols = [("Customer", "text"), ("Type", "text"), ("Invoiced", "money"), ("Received", "money"), ("Outstanding", "money")]
    return _R("receivables", f"Outstanding receivables by customer (as of {end})", cols, rows,
              ("Total", "", _sum(rows, 2), _sum(rows, 3), _sum(rows, 4)))


def ageing(conn, start, end):
    as_of = date.fromisoformat(end)
    rows = []
    for c in conn.execute("SELECT * FROM customers WHERE active=1 ORDER BY name"):
        invs = conn.execute("SELECT * FROM invoices WHERE customer_id=? AND status='posted' AND invoice_date<=? ORDER BY invoice_date, id",
                            (c["id"], end)).fetchall()
        if not invs:
            continue
        unalloc = conn.execute("SELECT COALESCE(SUM(amount_paise),0) FROM payments WHERE customer_id=? AND invoice_id IS NULL AND payment_date<=?",
                               (c["id"], end)).fetchone()[0]
        buckets = [0, 0, 0, 0]
        for i in invs:
            paid = conn.execute("SELECT COALESCE(SUM(amount_paise),0) FROM payments WHERE invoice_id=? AND payment_date<=?",
                                (i["id"], end)).fetchone()[0]
            bal = i["total_paise"] - paid
            use = min(unalloc, max(bal, 0))
            bal, unalloc = bal - use, unalloc - use          # on-account receipts clear the oldest invoices first
            if bal <= 0:
                continue
            days = (as_of - date.fromisoformat(i["invoice_date"])).days
            buckets[0 if days <= 30 else 1 if days <= 60 else 2 if days <= 90 else 3] += bal
        if sum(buckets):
            rows.append((c["name"], *buckets, sum(buckets)))
    cols = [("Customer", "text"), ("0-30 days", "money"), ("31-60 days", "money"), ("61-90 days", "money"),
            ("Over 90 days", "money"), ("Total due", "money")]
    return _R("ageing", f"Customer ageing report (as of {end})", cols, rows,
              ("Total", *(_sum(rows, i) for i in range(1, 6))), "Age counted from invoice date. On-account receipts clear the oldest invoices first.")


REPORTS = [
    ("sales_register", "Sales register", sales_register, "Sales"),
    ("customer_sales", "Customer-wise sales", customer_sales, "Sales"),
    ("product_sales", "Product-wise sales", product_sales, "Sales"),
    ("gst_summary", "GST outward supplies (GSTR-1 / 3B)", gst_summary, "Tax"),
    ("purchase_register", "Purchase register", purchase_register, "Purchases"),
    ("rm_stock", "Raw-material stock summary", rm_stock, "Stock"),
    ("pkg_stock", "Packaging stock summary", pkg_stock, "Stock"),
    ("fg_stock", "Finished-goods stock summary", fg_stock, "Stock"),
    ("stock_ledger", "Stock ledger", stock_ledger, "Stock"),
    ("production_report", "Production report", production_report, "Production"),
    ("planned_vs_actual", "Planned vs actual consumption", planned_vs_actual, "Production"),
    ("yield_report", "Production yield report", yield_report, "Production"),
    ("wastage_report", "Wastage report", wastage_report, "Production"),
    ("costing_report", "Product costing report", costing_report, "Production"),
    ("collections", "Payment & collection report", collections, "Receivables"),
    ("receivables", "Outstanding receivables", receivables, "Receivables"),
    ("ageing", "Customer ageing", ageing, "Receivables"),
]
REPORT_MAP = {slug: fn for slug, _, fn, _ in REPORTS}


def run_report(conn, slug, start=None, end=None, **kw):
    if slug not in REPORT_MAP:
        raise KeyError(slug)
    d0, d1 = default_range()
    return REPORT_MAP[slug](conn, start or d0, end or d1, **kw)
