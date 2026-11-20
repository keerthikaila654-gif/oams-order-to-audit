"""Owner dashboard - every figure is queried live from the transaction tables."""
from datetime import date

from flask import Blueprint, render_template, url_for

from ..money import fmt_inr, fmt_pct
from ..services import costing, inventory as inv, production as prod, reports, sales
from . import db, roles_required

bp = Blueprint("owner", __name__, url_prefix="/owner")


@bp.route("/")
@roles_required("owner")
def home():
    conn = db()
    today = inv.today()
    start = date.today().replace(day=1).isoformat()
    s = reports.sales_register(conn, start, today)
    month_sales = s["totals"][5]
    orders_today = conn.execute("SELECT COUNT(*) FROM sales_orders WHERE order_date=?", (today,)).fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM sales_orders WHERE status='submitted'").fetchone()[0]
    out = sales.outstanding_by_customer(conn)
    total_out = sum(r["outstanding_paise"] for r in out)
    costs = costing.latest_cost_per_packet(conn)
    alerts = []
    for r in prod.list_runs(conn):
        v = prod.run_variance(conn, r["id"])
        if v["any_flag"]:
            if v["yield"]["flagged"]:
                alerts.append((r["run_no"], "Yield", f"{v['yield']['actual_m'] / 1000:g} vs {v['yield']['expected_m'] / 1000:g} packets", v["yield"]["var_pct"]))
            for m in v["rows"]:
                if m["flagged"]:
                    alerts.append((r["run_no"], m["name"], f"{m['actual_m'] / 1000:g} vs {m['planned_m'] / 1000:g} {m['unit']}", m["var_pct"]))
    by_cust = [(r["customer"], r["total"]) for r in [dict(x) for x in conn.execute(
        "SELECT customer_name AS customer, SUM(taxable_paise) AS total FROM invoices WHERE status='posted' AND invoice_date>=? "
        "GROUP BY customer_id ORDER BY 2 DESC LIMIT 6", (start,))]]
    stock = [(r["name"], r["base_unit"], r["qty_m"], r["value_paise"]) for r in inv.stock_summary(conn)]
    low = inv.low_stock(conn)
    secs = [
        {"title": "Production variance alerts", "cols": [("Run", "text"), ("Item", "text"), ("Actual vs planned", "text"), ("Variance", "pct")],
         "rows": alerts, "empty": "No variance outside tolerance."},
        {"title": "Cost per packet (latest run per product)", "cols": [("Product", "text"), ("Run", "text"), ("Cost / packet ₹", "text"), ("Selling ₹", "text"), ("Margin", "pct")],
         "rows": [(c["product"]["name"], c["run"]["run_no"], f"₹{c['cost_per_packet']}", f"₹{c['selling_price']:.2f}", c["margin_pct"]) for c in costs],
         "empty": "No completed production runs."},
        {"title": "Customer outstanding dues", "cols": [("Customer", "text"), ("Invoiced", "money"), ("Received", "money"), ("Outstanding", "money")],
         "rows": [(r["name"], r["billed_paise"], r["paid_paise"], r["outstanding_paise"]) for r in out if r["billed_paise"] or r["paid_paise"]], "empty": "No receivables."},
        {"title": "Current stock levels", "cols": [("Item", "text"), ("Unit", "text"), ("On hand", "qty"), ("Value", "money")], "rows": stock},
    ]
    return render_template("dashboard.html", title="Owner dashboard", subtitle="Live business summary - month to date",
                           kpis=[("Sales this month", fmt_inr(month_sales), "taxable, posted invoices", ""),
                                 ("Today's orders", orders_today, f"{pending} awaiting approval", ""),
                                 ("Outstanding dues", fmt_inr(total_out), "from customers", "warn" if total_out else ""),
                                 ("Variance alerts", len(alerts), "outside tolerance", "bad" if alerts else ""),
                                 ("Low-stock alerts", len(low), ", ".join(r["name"] for r in low) or "all above reorder level", "bad" if low else "")],
                           bars={"title": "Sales by customer (taxable, this month)", "data": [(n, v) for n, v in by_cust]}, sections=secs)
