"""Mobile salesman app: order entry (offline capable), order tracking, customer-wise sales."""
from flask import Blueprint, abort, g, jsonify, render_template, request, url_for

from ..money import fmt_inr, to_milli
from ..services import BusinessError, inventory as inv, sales
from . import db, roles_required

bp = Blueprint("salesman", __name__, url_prefix="/s")
STATUS_CLASS = {"submitted": "badge-blue", "approved": "badge-amber", "invoiced": "badge-purple", "dispatched": "badge-green",
                "rejected": "badge-red", "cancelled": "badge-grey"}


def _own_order(order_id):
    o = sales.get_order(db(), order_id)
    if o["salesman_user_id"] != g.user["id"]:
        abort(403)
    return o


@bp.route("/")
@roles_required("salesman")
def home():
    conn, uid = db(), g.user["id"]
    today = inv.today()
    summ = sales.salesman_summary(conn, uid)
    todays = conn.execute("SELECT COUNT(*) FROM sales_orders WHERE salesman_user_id=? AND order_date=?", (uid, today)).fetchone()[0]
    recent = conn.execute(
        "SELECT o.id, o.order_no, o.order_date, c.name, o.status, (SELECT SUM(qty_m*price_paise/1000) FROM sales_order_items WHERE order_id=o.id) "
        "FROM sales_orders o JOIN customers c ON c.id=o.customer_id WHERE o.salesman_user_id=? ORDER BY o.id DESC LIMIT 8", (uid,)).fetchall()
    rows = [((r[1], url_for("salesman.order", order_id=r[0])), r[2], r[3], (r[4].title(), STATUS_CLASS[r[4]]), int(r[5] or 0)) for r in recent]
    st = summ["by_status"]
    return render_template("dashboard.html", title=f"Hello, {g.user['full_name']}", subtitle="Your orders and performance this month",
                           actions=[("＋ New order", url_for("salesman.new_order"), True)],
                           kpis=[("Orders today", todays, "", ""), ("Orders this month", summ["orders_total"], "", ""),
                                 ("Awaiting approval", st.get("submitted", 0), "submitted", "warn" if st.get("submitted") else ""),
                                 ("Invoiced sales", fmt_inr(summ["invoiced_taxable_paise"]), f"{summ['invoice_count']} invoices (excl. GST)", "")],
                           sections=[{"title": "Recent orders", "cols": [("Order", "link"), ("Date", "date"), ("Customer", "text"),
                                                                       ("Status", "badge"), ("Value", "money")], "rows": rows,
                                      "empty": "No orders yet - tap New order to place one."}])


@bp.route("/new")
@roles_required("salesman")
def new_order():
    conn, uid = db(), g.user["id"]
    custs = conn.execute("SELECT id, name, type, state, address, phone FROM customers WHERE salesman_user_id=? AND active=1 ORDER BY name",
                         (uid,)).fetchall()
    prods = conn.execute("SELECT p.id, p.name, p.pack_size, p.item_id FROM products p WHERE p.active=1 ORDER BY p.name").fetchall()
    data = {"customers": [dict(c, outstanding=fmt_inr(sales.customer_outstanding(conn, c["id"]))) for c in custs],
            "products": [], "prices": {}}
    for p in prods:
        av = sales.availability(conn, p["id"])
        data["products"].append({"id": p["id"], "name": p["name"], "pack": p["pack_size"], "available": av["available_m"] / 1000})
    for c in custs:
        data["prices"][c["id"]] = {p["id"]: sales.price_for(conn, c["id"], p["id"]) for p in prods}
    return render_template("s_new.html", data=data)


@bp.route("/api/orders", methods=["POST"], endpoint="api_orders")
@roles_required("salesman")
def api_orders():
    j = request.get_json(silent=True) or {}
    try:
        items = [(int(i["product_id"]), to_milli(i["qty"])) for i in j.get("items", []) if str(i.get("qty", "")).strip() not in ("", "0")]
        oid, created = sales.create_order(db(), g.user, int(j.get("customer_id", 0)), items,
                                          client_uuid=(j.get("client_uuid") or None), notes=(j.get("notes") or None))
    except (BusinessError, ValueError, KeyError, TypeError) as e:
        return jsonify(ok=False, error=str(e)), 400
    o = sales.get_order(db(), oid)
    return jsonify(ok=True, id=oid, order_no=o["order_no"], duplicate=not created)


@bp.route("/orders")
@roles_required("salesman")
def orders():
    conn, uid = db(), g.user["id"]
    status = request.args.get("status")
    sql = ("SELECT o.id, o.order_no, o.order_date, c.name, o.status, (SELECT SUM(qty_m*price_paise/1000) FROM sales_order_items WHERE order_id=o.id) "
           "FROM sales_orders o JOIN customers c ON c.id=o.customer_id WHERE o.salesman_user_id=?")
    args = [uid]
    if status:
        sql += " AND o.status=?"
        args.append(status)
    rows = [((r[1], url_for("salesman.order", order_id=r[0])), r[2], r[3], (r[4].title(), STATUS_CLASS[r[4]]), int(r[5] or 0))
            for r in conn.execute(sql + " ORDER BY o.id DESC", args)]
    return render_template("page.html", title="My orders", subtitle="Track every order you have submitted",
                           filters=[("", "All")] + [(s, s.title()) for s in ("submitted", "approved", "invoiced", "dispatched", "rejected")],
                           filter_name="status", filter_value=status or "",
                           cols=[("Order", "link"), ("Date", "date"), ("Customer", "text"), ("Status", "badge"), ("Value", "money")],
                           rows=rows, empty="No orders found.")


@bp.route("/orders/<int:order_id>")
@roles_required("salesman")
def order(order_id):
    o = _own_order(order_id)
    conn = db()
    items = sales.order_items(conn, order_id)
    inv_row = conn.execute("SELECT * FROM invoices WHERE order_id=? ORDER BY id DESC LIMIT 1", (order_id,)).fetchone()
    return render_template("order.html", o=o, items=items, inv=inv_row, chk=None, preview=None, admin=False,
                           status_class=STATUS_CLASS, disp=sales.dispatch_status(conn, inv_row["id"]) if inv_row else None)


@bp.route("/customers")
@roles_required("salesman")
def customers():
    rows = [((r["name"], url_for("salesman.customer", cid=r["id"])), r["type"], r["state"], r["orders"], r["ordered_paise"], r["sales_paise"])
            for r in sales.customer_sales_for_salesman(db(), g.user["id"])]
    return render_template("page.html", title="Customer-wise sales", subtitle="Customers assigned to you",
                           cols=[("Customer", "link"), ("Type", "text"), ("State", "text"), ("Orders", "int"),
                                 ("Ordered value", "money"), ("Invoiced (excl. GST)", "money")], rows=rows, empty="No customers assigned.")


@bp.route("/customers/<int:cid>")
@roles_required("salesman")
def customer(cid):
    conn = db()
    c = conn.execute("SELECT * FROM customers WHERE id=? AND salesman_user_id=?", (cid, g.user["id"])).fetchone()
    if not c:
        abort(403)
    rows = [((r[1], url_for("salesman.order", order_id=r[0])), r[2], (r[3].title(), STATUS_CLASS[r[3]]), int(r[4] or 0))
            for r in conn.execute("SELECT o.id, o.order_no, o.order_date, o.status, (SELECT SUM(qty_m*price_paise/1000) FROM sales_order_items "
                                  "WHERE order_id=o.id) FROM sales_orders o WHERE o.customer_id=? AND o.salesman_user_id=? ORDER BY o.id DESC",
                                  (cid, g.user["id"]))]
    return render_template("page.html", title=c["name"], subtitle=f"{c['type']} · {c['state']} · {c['phone'] or ''} · "
                                                                  f"Outstanding {fmt_inr(sales.customer_outstanding(conn, cid))}",
                           cols=[("Order", "link"), ("Date", "date"), ("Status", "badge"), ("Value", "money")], rows=rows,
                           empty="No orders for this customer yet.", back=url_for("salesman.customers"))
