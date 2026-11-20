"""Factory administrator desk: order verification, GST invoices, dispatch, payments, masters, users, settings."""
import re
import urllib.parse

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

from ..benchmark import verify
from ..db import get_setting, set_setting, tx
from ..money import D, fmt_inr, fmt_qty, to_milli, to_paise
from ..services import BusinessError, inventory as inv, pdf, sales
from ..services.audit import log
from . import db, guard, roles_required
from .salesman import STATUS_CLASS

bp = Blueprint("admin", __name__, url_prefix="/admin")
STATES = {"Tamil Nadu": "33", "Karnataka": "29", "Kerala": "32", "Andhra Pradesh": "37", "Telangana": "36", "Maharashtra": "27",
          "Puducherry": "34", "Gujarat": "24", "Delhi": "07"}


def _field(name, label, type="text", value="", options=None, required=False, **kw):
    return {"name": name, "label": label, "type": type, "value": value, "options": options or [], "required": required, **kw}


# ───────────────────────── dashboard ─────────────────────────
@bp.route("/")
@roles_required("admin")
def home():
    conn = db()
    pend = conn.execute("SELECT o.id, o.order_no, o.order_date, c.name, u.full_name, "
                        "(SELECT SUM(qty_m*price_paise/1000) FROM sales_order_items WHERE order_id=o.id) "
                        "FROM sales_orders o JOIN customers c ON c.id=o.customer_id JOIN users u ON u.id=o.salesman_user_id "
                        "WHERE o.status='submitted' ORDER BY o.id").fetchall()
    to_dispatch = conn.execute("SELECT i.id, i.invoice_no, i.customer_name, i.total_paise FROM invoices i WHERE i.status='posted' AND "
                               "(SELECT COALESCE(SUM(qty_m),0) FROM invoice_items WHERE invoice_id=i.id) > "
                               "(SELECT COALESCE(SUM(di.qty_m),0) FROM dispatch_items di JOIN dispatches d ON d.id=di.dispatch_id WHERE d.invoice_id=i.id)").fetchall()
    out = sum(r["outstanding_paise"] for r in sales.outstanding_by_customer(conn))
    fg = []
    for p in conn.execute("SELECT * FROM products WHERE active=1 ORDER BY name"):
        a = sales.availability(conn, p["id"])
        fg.append((p["name"], a["on_hand_m"], a["reserved_m"], a["available_m"]))
    return render_template(
        "dashboard.html", title="Admin dashboard", subtitle="Verify orders, bill, dispatch and collect",
        kpis=[("Orders to verify", len(pend), "submitted by salesmen", "warn" if pend else ""),
              ("Invoices to dispatch", len(to_dispatch), "", "warn" if to_dispatch else ""),
              ("Receivables", fmt_inr(out), "outstanding from customers", ""),
              ("Low-stock items", len(inv.low_stock(conn)), "raw / packaging", "bad" if inv.low_stock(conn) else "")],
        sections=[
            {"title": "Pending orders", "cols": [("Order", "link"), ("Date", "date"), ("Customer", "text"), ("Salesman", "text"), ("Value", "money")],
             "rows": [((r[1], url_for("admin.order", order_id=r[0])), r[2], r[3], r[4], int(r[5] or 0)) for r in pend],
             "empty": "Nothing waiting for approval."},
            {"title": "Invoices awaiting dispatch", "cols": [("Invoice", "link"), ("Customer", "text"), ("Total", "money")],
             "rows": [((r[1], url_for("admin.invoice", invoice_id=r[0])), r[2], r[3]) for r in to_dispatch], "empty": "All invoices dispatched."},
            {"title": "Finished-goods availability", "cols": [("Product", "text"), ("On hand", "qty"), ("Reserved", "qty"), ("Available", "qty")],
             "rows": fg}])


# ───────────────────────── orders ─────────────────────────
@bp.route("/orders")
@roles_required("admin")
def orders():
    status = request.args.get("status", "")
    sql = ("SELECT o.id, o.order_no, o.order_date, c.name, c.type, u.full_name, o.status, "
           "(SELECT SUM(qty_m*price_paise/1000) FROM sales_order_items WHERE order_id=o.id) "
           "FROM sales_orders o JOIN customers c ON c.id=o.customer_id JOIN users u ON u.id=o.salesman_user_id")
    args = []
    if status:
        sql += " WHERE o.status=?"
        args.append(status)
    rows = [((r[1], url_for("admin.order", order_id=r[0])), r[2], r[3], r[4], r[5], (r[6].title(), STATUS_CLASS[r[6]]), int(r[7] or 0))
            for r in db().execute(sql + " ORDER BY o.id DESC", args)]
    return render_template("page.html", title="Salesman orders", subtitle="Incoming orders - open one to verify, edit, approve or reject",
                           filters=[("", "All")] + [(s, s.title()) for s in ("submitted", "approved", "invoiced", "dispatched", "rejected", "cancelled")],
                           filter_name="status", filter_value=status,
                           cols=[("Order", "link"), ("Date", "date"), ("Customer", "text"), ("Type", "text"), ("Salesman", "text"),
                                 ("Status", "badge"), ("Value", "money")], rows=rows, empty="No orders.")


@bp.route("/orders/<int:order_id>")
@roles_required("admin")
def order(order_id):
    conn = db()
    o = guard(sales.get_order, conn, order_id)[0] or abort(404)
    items = sales.order_items(conn, order_id)
    inv_row = conn.execute("SELECT * FROM invoices WHERE order_id=? ORDER BY id DESC LIMIT 1", (order_id,)).fetchone()
    chk = sales.stock_check(conn, order_id) if o["status"] in ("submitted", "approved") else None
    return render_template("order.html", o=o, items=items, inv=inv_row, chk=chk, preview=sales.tax_preview(conn, order_id), admin=True,
                           status_class=STATUS_CLASS, disp=sales.dispatch_status(conn, inv_row["id"]) if inv_row else None)


@bp.route("/orders/<int:order_id>/edit", methods=["POST"])
@roles_required("admin")
def order_edit(order_id):
    changes = {}
    try:
        for it in sales.order_items(db(), order_id):
            pid = it["product_id"]
            changes[pid] = (to_milli(request.form.get(f"qty_{pid}", 0)), to_paise(request.form.get(f"price_{pid}", 0)))
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("admin.order", order_id=order_id))
    guard(sales.update_order_items, db(), g.user, order_id, changes, ok="Order updated - stock re-checked below.")
    return redirect(url_for("admin.order", order_id=order_id))


@bp.route("/orders/<int:order_id>/approve", methods=["POST"])
@roles_required("admin")
def order_approve(order_id):
    guard(sales.approve_order, db(), g.user, order_id, ok="Order approved. You can now generate the GST invoice.")
    return redirect(url_for("admin.order", order_id=order_id))


@bp.route("/orders/<int:order_id>/reject", methods=["POST"])
@roles_required("admin")
def order_reject(order_id):
    guard(sales.reject_order, db(), g.user, order_id, request.form.get("reason", ""), ok="Order rejected.")
    return redirect(url_for("admin.order", order_id=order_id))


@bp.route("/orders/<int:order_id>/invoice", methods=["POST"])
@roles_required("admin")
def order_invoice(order_id):
    iid, ok = guard(sales.generate_invoice, db(), g.user, order_id, ok="GST invoice generated.")
    return redirect(url_for("admin.invoice", invoice_id=iid) if ok else url_for("admin.order", order_id=order_id))


# ───────────────────────── invoices & dispatch ─────────────────────────
@bp.route("/invoices")
@roles_required("admin")
def invoices():
    rows = []
    conn = db()
    for r in conn.execute("SELECT * FROM invoices ORDER BY id DESC"):
        st, paid = sales.invoice_payment_status(conn, r)
        rows.append(((r["invoice_no"], url_for("admin.invoice", invoice_id=r["id"])), r["invoice_date"], r["customer_name"],
                     "IGST" if r["tax_type"] == "INTER" else "CGST+SGST", r["taxable_paise"], r["total_paise"], paid,
                     (st, {"Paid": "badge-green", "Partial": "badge-amber", "Unpaid": "badge-red", "Cancelled": "badge-grey"}[st])))
    return render_template("page.html", title="GST invoices", subtitle="Posted invoices are never deleted - corrections use cancellation + credit note",
                           cols=[("Invoice", "link"), ("Date", "date"), ("Customer", "text"), ("Tax", "text"), ("Taxable", "money"),
                                 ("Total", "money"), ("Received", "money"), ("Payment", "badge")], rows=rows, empty="No invoices yet.")


@bp.route("/invoices/<int:invoice_id>")
@roles_required("admin")
def invoice(invoice_id):
    conn = db()
    iv = guard(sales.get_invoice, conn, invoice_id)[0] or abort(404)
    cust = conn.execute("SELECT * FROM customers WHERE id=?", (iv["customer_id"],)).fetchone()
    status, paid = sales.invoice_payment_status(conn, iv)
    pays = conn.execute("SELECT * FROM payments WHERE invoice_id=? ORDER BY id", (invoice_id,)).fetchall()
    disp = sales.dispatch_status(conn, invoice_id)
    dispatches = conn.execute("SELECT * FROM dispatches WHERE invoice_id=? ORDER BY id", (invoice_id,)).fetchall()
    pdf_url = url_for("admin.invoice_pdf", invoice_id=invoice_id, _external=True)
    msg = f"Invoice {iv['invoice_no']} for {fmt_inr(iv['total_paise'])} from {sales.seller(conn)['company_name']}. Download: {pdf_url}"
    phone = re.sub(r"\D", "", cust["phone"] or "")
    wa = "https://wa.me/" + (("91" + phone[-10:]) if phone else "") + "?text=" + urllib.parse.quote(msg)
    mail = "mailto:?subject=" + urllib.parse.quote(f"Tax invoice {iv['invoice_no']}") + "&body=" + urllib.parse.quote(msg)
    cn = conn.execute("SELECT * FROM credit_notes WHERE invoice_id=?", (invoice_id,)).fetchone()
    return render_template("invoice.html", iv=iv, lines=sales.invoice_lines(conn, invoice_id), cust=cust, pay_status=status, paid=paid,
                           balance=iv["total_paise"] - paid, pays=pays, disp=disp, dispatches=dispatches, wa=wa, mail=mail, cn=cn)


@bp.route("/invoices/<int:invoice_id>/pdf")
@roles_required("admin", "accountant", "owner")
def invoice_pdf(invoice_id):
    conn = db()
    try:
        iv = sales.get_invoice(conn, invoice_id)
    except BusinessError:
        abort(404)
    fname = iv["invoice_no"].replace("/", "-") + ".pdf"
    return Response(pdf.invoice_pdf(conn, invoice_id), mimetype="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{fname}"'})


@bp.route("/invoices/<int:invoice_id>/dispatch", methods=["POST"])
@roles_required("admin")
def dispatch(invoice_id):
    conn = db()
    try:
        q = {d["product_id"]: to_milli(request.form.get(f"dq_{d['product_id']}", 0)) for d in sales.dispatch_status(conn, invoice_id)}
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("admin.invoice", invoice_id=invoice_id))
    guard(sales.dispatch_invoice, conn, g.user, invoice_id, q, request.form.get("vehicle") or None,
          ok="Dispatch recorded - finished-goods stock reduced.")
    return redirect(url_for("admin.invoice", invoice_id=invoice_id))


@bp.route("/invoices/<int:invoice_id>/cancel", methods=["POST"])
@roles_required("admin")
def invoice_cancel(invoice_id):
    guard(sales.cancel_invoice, db(), g.user, invoice_id, request.form.get("reason", ""),
          ok="Invoice cancelled and credit note issued. Dispatched goods were returned to stock.")
    return redirect(url_for("admin.invoice", invoice_id=invoice_id))


# ───────────────────────── payments ─────────────────────────
@bp.route("/payments", methods=["GET", "POST"])
@roles_required("admin")
def payments():
    conn = db()
    if request.method == "POST":
        try:
            _, ok = guard(sales.record_payment, conn, g.user, int(request.form["customer_id"]), to_paise(request.form.get("amount", 0)),
                          int(request.form["invoice_id"]) if request.form.get("invoice_id") else None, request.form.get("mode", "cash"),
                          request.form.get("reference") or None, request.form.get("date") or None, ok="Payment recorded.")
        except (ValueError, KeyError) as e:
            flash(str(e), "error")
        return redirect(url_for("admin.payments"))
    custs = [(str(c["id"]), c["name"]) for c in conn.execute("SELECT id, name FROM customers WHERE active=1 ORDER BY name")]
    invs = [("", "- on account / not linked -")] + [
        (str(i["id"]), f"{i['invoice_no']} - {i['customer_name']} - balance {fmt_inr(i['total_paise'] - sales.invoice_paid(conn, i['id']))}")
        for i in conn.execute("SELECT * FROM invoices WHERE status='posted' ORDER BY id DESC")
        if i["total_paise"] - sales.invoice_paid(conn, i["id"]) > 0]
    form = {"title": "Record payment", "action": url_for("admin.payments"), "submit": "Record payment", "fields": [
        _field("customer_id", "Customer", "select", options=custs, required=True),
        _field("invoice_id", "Against invoice", "select", options=invs),
        _field("amount", "Amount (₹)", "number", required=True, step="0.01", min="0.01"),
        _field("mode", "Mode", "select", options=[("cash", "Cash"), ("upi", "UPI"), ("bank", "Bank transfer"), ("cheque", "Cheque")]),
        _field("reference", "Reference / UTR"), _field("date", "Date", "date", value=inv.today())]}
    rows = [(r["payment_date"], r["receipt_no"], r["name"], r["invoice_no"] or "On account", r["mode"], r["reference"] or "", r["amount_paise"])
            for r in conn.execute("SELECT p.*, c.name, i.invoice_no FROM payments p JOIN customers c ON c.id=p.customer_id "
                                  "LEFT JOIN invoices i ON i.id=p.invoice_id ORDER BY p.id DESC")]
    out = [(r["name"], r["type"], r["billed_paise"], r["paid_paise"], r["outstanding_paise"]) for r in sales.outstanding_by_customer(conn)]
    return render_template("page.html", title="Payments & receivables", subtitle="Payments update invoice status and customer balances instantly",
                           forms=[form], cols=[("Date", "date"), ("Receipt", "text"), ("Customer", "text"), ("Invoice", "text"), ("Mode", "text"),
                                               ("Reference", "text"), ("Amount", "money")], rows=rows, table_title="Payment history",
                           cols2=[("Customer", "text"), ("Type", "text"), ("Invoiced", "money"), ("Received", "money"), ("Outstanding", "money")],
                           rows2=out, table2_title="Customer outstanding balances", empty="No payments recorded yet.")


# ───────────────────────── customers ─────────────────────────
@bp.route("/customers", methods=["GET", "POST"])
@bp.route("/customers/<int:cid>", methods=["GET", "POST"])
@roles_required("admin")
def customers(cid=None):
    conn = db()
    if request.method == "POST":
        f = request.form
        try:
            state = f.get("state", "Tamil Nadu")
            vals = (f["name"].strip(), f["type"], state, STATES.get(state, "00"), (f.get("gstin") or "").strip().upper() or None,
                    f.get("address"), f.get("phone"), int(f["salesman"]) if f.get("salesman") else None,
                    to_paise(f.get("credit_limit", 0)))
            if not vals[0]:
                raise BusinessError("Customer name is required")
            if vals[4] and not re.fullmatch(r"\d{2}[A-Z0-9]{13}", vals[4]):
                raise BusinessError("GSTIN must be 15 characters (2-digit state code + PAN + entity + Z + checksum)")
            if vals[4] and vals[4][:2] != vals[3]:
                raise BusinessError("GSTIN state code does not match the selected state")
            with tx(conn):
                if cid:
                    conn.execute("UPDATE customers SET name=?,type=?,state=?,state_code=?,gstin=?,address=?,phone=?,salesman_user_id=?,"
                                 "credit_limit_paise=? WHERE id=?", (*vals, cid))
                    log(conn, g.user, "edit_customer", "customer", cid, new=dict(zip(("name", "type", "state"), vals[:3])))
                else:
                    code = f"CUST-{conn.execute('SELECT COUNT(*)+1 FROM customers').fetchone()[0]:03d}"
                    pl = conn.execute("SELECT id FROM price_lists LIMIT 1").fetchone()
                    cid = conn.execute("INSERT INTO customers(code,name,type,state,state_code,gstin,address,phone,salesman_user_id,"
                                       "credit_limit_paise,price_list_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                       (code, *vals, pl[0] if pl else None)).lastrowid
                    log(conn, g.user, "create_customer", "customer", cid, new={"name": vals[0]})
            flash("Customer saved.", "success")
            return redirect(url_for("admin.customers"))
        except (BusinessError, ValueError, KeyError) as e:
            flash(str(e), "error")
    cur = conn.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone() if cid else None
    sms = [("", "- none -")] + [(str(u["id"]), u["full_name"]) for u in conn.execute("SELECT id, full_name FROM users WHERE role='salesman' AND active=1")]
    g_ = lambda k, d="": (cur[k] if cur and cur[k] is not None else d)
    form = {"title": "Edit customer" if cur else "Add customer", "action": request.path, "submit": "Save customer", "fields": [
        _field("name", "Name", required=True, value=g_("name")),
        _field("type", "Type", "select", options=[("Retailer", "Retailer"), ("Distributor", "Distributor")], value=g_("type", "Retailer")),
        _field("state", "State (place of supply)", "select", options=[(s, s) for s in STATES], value=g_("state", "Tamil Nadu")),
        _field("gstin", "GSTIN (blank = unregistered)", value=g_("gstin")), _field("address", "Address", value=g_("address")),
        _field("phone", "Phone", value=g_("phone")), _field("salesman", "Salesman", "select", options=sms, value=str(g_("salesman_user_id", ""))),
        _field("credit_limit", "Credit limit (₹)", "number", step="0.01", value=f"{g_('credit_limit_paise', 0) / 100:.2f}")]}
    out = {r["id"]: r["outstanding_paise"] for r in sales.outstanding_by_customer(conn)}
    rows = [((r["name"], url_for("admin.customers", cid=r["id"])), r["type"], r["state"], r["gstin"] or "Unregistered", r["sm"] or "", out.get(r["id"], 0))
            for r in conn.execute("SELECT c.*, u.full_name AS sm FROM customers c LEFT JOIN users u ON u.id=c.salesman_user_id WHERE c.active=1 ORDER BY c.name")]
    return render_template("page.html", title="Customers", subtitle="Retailers and distributors", forms=[form],
                           cols=[("Customer", "link"), ("Type", "text"), ("State", "text"), ("GSTIN", "text"), ("Salesman", "text"),
                                 ("Outstanding", "money")], rows=rows, empty="No customers.")


# ───────────────────────── products ─────────────────────────
@bp.route("/products", methods=["GET", "POST"])
@bp.route("/products/<int:pid>", methods=["GET", "POST"])
@roles_required("admin")
def products(pid=None):
    conn = db()
    if request.method == "POST":
        f = request.form
        try:
            name, price = f["name"].strip(), to_paise(f.get("price", 0))
            bp_ = int((D(f.get("gst", 5)) * 100).to_integral_value())
            if not name or price < 0:
                raise BusinessError("Name and a non-negative price are required")
            bom = int(f["bom_id"]) if f.get("bom_id") else None
            with tx(conn):
                if pid:
                    conn.execute("UPDATE products SET name=?, category_id=?, pack_size=?, hsn=?, gst_rate_bp=?, bom_id=?, base_price_paise=? WHERE id=?",
                                 (name, int(f["category_id"]), f.get("pack"), f.get("hsn"), bp_, bom, price, pid))
                    conn.execute("UPDATE price_list_items SET price_paise=? WHERE product_id=?", (price, pid))
                    log(conn, g.user, "edit_product", "product", pid, new={"name": name, "price_paise": price})
                else:
                    code = (f.get("code") or "").strip().upper()
                    if not code:
                        raise BusinessError("Product code is required")
                    item = conn.execute("INSERT INTO items(code,name,kind,base_unit) VALUES (?,?,?,?)",
                                        (f"FG-{code}", name, "FG", "packet")).lastrowid
                    pid = conn.execute("INSERT INTO products(code,name,category_id,pack_size,hsn,gst_rate_bp,item_id,bom_id,base_price_paise) "
                                       "VALUES (?,?,?,?,?,?,?,?,?)", (code, name, int(f["category_id"]), f.get("pack"), f.get("hsn") or "2106",
                                                                       bp_, item, bom, price)).lastrowid
                    for pl in conn.execute("SELECT id FROM price_lists"):
                        conn.execute("INSERT INTO price_list_items VALUES (?,?,?)", (pl[0], pid, price))
                    log(conn, g.user, "create_product", "product", pid, new={"name": name, "bom_id": bom})
            flash("Product saved.", "success")
            return redirect(url_for("admin.products"))
        except Exception as e:      # sqlite IntegrityError (duplicate code) etc.
            flash(str(e) if isinstance(e, (BusinessError, ValueError, KeyError)) else f"Could not save: {e}", "error")
    cur = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone() if pid else None
    cats = [(str(c["id"]), c["name"]) for c in conn.execute("SELECT * FROM product_categories")]
    boms = [("", "- no BOM (not produced) -")] + [(str(b["id"]), b["name"]) for b in conn.execute("SELECT * FROM boms WHERE active=1")]
    g_ = lambda k, d="": (cur[k] if cur and cur[k] is not None else d)
    fields = [] if cur else [_field("code", "Code (e.g. MUR500)", required=True)]
    fields += [_field("name", "Product name", required=True, value=g_("name")),
               _field("category_id", "Category", "select", options=cats, value=str(g_("category_id", cats[0][0]))),
               _field("pack", "Pack size", value=g_("pack_size")), _field("hsn", "HSN code", value=g_("hsn", "2106")),
               _field("gst", "GST %", "number", step="0.01", value=f"{g_('gst_rate_bp', 500) / 100:.2f}"),
               _field("bom_id", "BOM (recipe) - several pack sizes can share one", "select", options=boms, value=str(g_("bom_id", ""))),
               _field("price", "Selling price ₹ (before tax)", "number", step="0.01", value=f"{g_('base_price_paise', 0) / 100:.2f}")]
    form = {"title": "Edit product" if cur else "Add product / pack size", "action": request.path, "submit": "Save product", "fields": fields}
    rows = [((r["name"], url_for("admin.products", pid=r["id"])), r["pack_size"], r["hsn"], r["gst_rate_bp"] / 100, r["bom"] or "-", r["base_price_paise"])
            for r in conn.execute("SELECT p.*, b.name AS bom FROM products p LEFT JOIN boms b ON b.id=p.bom_id ORDER BY p.name")]
    return render_template("page.html", title="Products & price list", subtitle="A new pack size can reuse an existing BOM", forms=[form],
                           cols=[("Product", "link"), ("Pack", "text"), ("HSN", "text"), ("GST %", "text"), ("BOM", "text"), ("Price", "money")], rows=rows)


# ───────────────────────── users ─────────────────────────
@bp.route("/users", methods=["GET", "POST"])
@roles_required("admin")
def users():
    conn = db()
    if request.method == "POST":
        f = request.form
        try:
            act = f.get("action")
            with tx(conn):
                if act == "create":
                    pw = f.get("password", "")
                    if len(pw) < 8:
                        raise BusinessError("Password must be at least 8 characters")
                    if not re.fullmatch(r"[a-z0-9_.-]{3,30}", f["username"].strip().lower()):
                        raise BusinessError("Username: 3-30 chars, lowercase letters, digits, . _ -")
                    uid = conn.execute("INSERT INTO users(username,password_hash,full_name,role) VALUES (?,?,?,?)",
                                       (f["username"].strip().lower(), generate_password_hash(pw), f["full_name"].strip(), f["role"])).lastrowid
                    if f["role"] == "salesman":
                        conn.execute("INSERT INTO salesmen(user_id,code) VALUES (?,?)", (uid, f"SM-{uid}"))
                    log(conn, g.user, "create_user", "user", uid, new={"username": f["username"], "role": f["role"]})
                    flash("User created.", "success")
                elif act in ("toggle", "reset"):
                    uid = int(f["uid"])
                    if act == "toggle":
                        if uid == g.user["id"]:
                            raise BusinessError("You cannot deactivate your own account")
                        conn.execute("UPDATE users SET active = 1-active WHERE id=?", (uid,))
                        log(conn, g.user, "toggle_user", "user", uid)
                    else:
                        pw = f.get("password", "")
                        if len(pw) < 8:
                            raise BusinessError("Password must be at least 8 characters")
                        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), uid))
                        log(conn, g.user, "reset_password", "user", uid)
                    flash("User updated.", "success")
        except Exception as e:
            flash(str(e) if isinstance(e, (BusinessError, KeyError)) else "Could not save (username already exists?)", "error")
        return redirect(url_for("admin.users"))
    form = {"title": "Add user", "action": url_for("admin.users"), "submit": "Create user", "fields": [
        _field("action", "", "hidden", value="create"), _field("username", "Username", required=True), _field("full_name", "Full name", required=True),
        _field("role", "Role", "select", options=[(r, r.title()) for r in ("salesman", "admin", "production", "accountant", "owner")]),
        _field("password", "Initial password (min 8)", "password", required=True)]}
    rows = [(r["username"], r["full_name"], r["role"], ("Active", "badge-green") if r["active"] else ("Disabled", "badge-grey"),
             ("form", r["id"], r["active"])) for r in conn.execute("SELECT * FROM users ORDER BY id")]
    return render_template("page.html", title="User management", subtitle="Role-based access. Passwords are stored as salted hashes.", forms=[form],
                           cols=[("Username", "text"), ("Name", "text"), ("Role", "text"), ("Status", "badge"), ("Actions", "userform")], rows=rows)


# ───────────────────────── settings, audit, benchmark ─────────────────────────
@bp.route("/settings", methods=["GET", "POST"])
@roles_required("admin")
def settings():
    conn = db()
    keys = ["variance_tolerance_pct", "company_name", "company_address", "company_gstin", "company_phone", "company_email", "company_bank"]
    if request.method == "POST":
        try:
            tol = D(request.form.get("variance_tolerance_pct", 3))
            if tol < 0 or tol > 100:
                raise BusinessError("Tolerance must be between 0 and 100 %")
            old = {k: get_setting(conn, k) for k in keys}
            with tx(conn):
                for k in keys:
                    if k in request.form:
                        set_setting(conn, k, request.form[k].strip())
                log(conn, g.user, "edit_settings", "settings", None, old=old, new={k: request.form.get(k) for k in keys})
            flash("Settings saved. New production runs use the new tolerance.", "success")
        except (BusinessError, ValueError) as e:
            flash(str(e), "error")
        return redirect(url_for("admin.settings"))
    labels = {"variance_tolerance_pct": "Variance tolerance % (flag when |variance| is larger)", "company_name": "Company name",
              "company_address": "Address", "company_gstin": "Company GSTIN", "company_phone": "Phone", "company_email": "Email",
              "company_bank": "Bank details (printed on invoice)"}
    form = {"title": "System settings", "action": url_for("admin.settings"), "submit": "Save settings",
            "fields": [_field(k, labels[k], "number" if k.startswith("variance") else "text", value=get_setting(conn, k, ""), step="0.1") for k in keys]}
    return render_template("page.html", title="Settings", subtitle="Seller details printed on GST invoices and the production variance tolerance",
                           forms=[form], cols=[], rows=[])


@bp.route("/audit")
@roles_required("admin")
def audit():
    rows = [(r["ts"], r["username"], r["action"], r["entity"], r["ref"] or "", r["old_json"] or "", r["new_json"] or "")
            for r in db().execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 300")]
    return render_template("page.html", title="Audit trail", subtitle="Append-only: who did what, when, with previous and new values (latest 300)",
                           cols=[("When (UTC)", "text"), ("User", "text"), ("Action", "text"), ("Entity", "text"), ("Document", "text"),
                                 ("Previous", "mono"), ("New", "mono")], rows=rows, empty="No audit entries.")


@bp.route("/benchmark")
@roles_required("admin")
def benchmark():
    checks = verify(db())
    rows = [(c["check"], c["expected"], c["actual"], ({"pass": ("PASS", "badge-green"), "pending": ("PENDING", "badge-grey")}).get(c["status"], ("FAIL", "badge-red")))
            for c in checks]
    from ..services import costing
    return render_template("page.html", title="Benchmark verification",
                           subtitle="Every expected result from Section 6, recomputed live from the database. "
                                    "Run the nine steps (or `python manage.py benchmark`) and refresh.",
                           cols=[("Check", "text"), ("Expected", "text"), ("Actual (from DB)", "text"), ("Result", "badge")], rows=rows,
                           note=f"Valuation method: {costing.VALUATION_METHOD}  Overhead rule: {costing.OVERHEAD_RULE}")
