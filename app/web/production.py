"""Production supervisor: purchases, BOMs, production runs, variance, costing, stock adjustments."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from ..db import tx
from ..money import D, fmt_inr, fmt_qty, to_milli, to_paise
from ..services import BusinessError, costing, inventory as inv, production as prod, purchasing
from ..services.audit import log
from ..units import UNIT_CHOICES, compatible
from . import db, guard, roles_required
from .admin import _field

bp = Blueprint("production", __name__, url_prefix="/p")
ROLES = ("production", "admin")   # admin may view production screens too (read) - writes limited below


# ───────────────────────── dashboard ─────────────────────────
@bp.route("/")
@roles_required("production")
def home():
    conn = db()
    runs = prod.list_runs(conn)
    flagged, rows = 0, []
    for r in runs[:10]:
        v = prod.run_variance(conn, r["id"])
        flagged += v["any_flag"]
        rows.append(((r["run_no"], url_for("production.run", run_id=r["id"])), r["run_date"], r["product_name"], r["expected_output_m"],
                     r["actual_output_m"], r["status"].replace("_", " ").title(),
                     ("VARIANCE FLAG", "badge-red") if v["any_flag"] else ("Within tolerance", "badge-green") if r["status"] == "completed" else ("In progress", "badge-blue")))
    stock = [(r["name"], r["base_unit"], r["qty_m"], ("LOW", "badge-red") if r["reorder_level_m"] and r["qty_m"] <= r["reorder_level_m"] else ("OK", "badge-green"))
             for r in inv.stock_summary(conn) if r["kind"] != "FG"]
    fg = [(r["name"], r["qty_m"]) for r in inv.stock_summary(conn, "FG") if r["qty_m"] or r["name"].startswith("Murukku")]
    return render_template("dashboard.html", title="Production dashboard", subtitle="Recipe-based runs with planned-versus-actual control",
                           actions=[("＋ New production run", url_for("production.run_new"), True), ("Record purchase", url_for("production.purchase_new"), False)],
                           kpis=[("Runs in progress", sum(1 for r in runs if r["status"] == "in_progress"), "", ""),
                                 ("Runs with variance flags", flagged, f"tolerance {prod.tolerance(conn)} %", "bad" if flagged else ""),
                                 ("Low-stock materials", len(inv.low_stock(conn)), "", "warn" if inv.low_stock(conn) else ""),
                                 ("Runs this month", len(runs), "", "")],
                           sections=[{"title": "Recent production runs", "cols": [("Run", "link"), ("Date", "date"), ("Product", "text"), ("Expected", "qty"),
                                                                                 ("Actual", "qty"), ("Status", "text"), ("Variance", "badge")], "rows": rows,
                                      "empty": "No runs yet."},
                                     {"title": "Raw-material & packaging stock", "cols": [("Item", "text"), ("Unit", "text"), ("On hand", "qty"), ("Level", "badge")], "rows": stock},
                                     {"title": "Finished goods", "cols": [("Product", "text"), ("Packets", "qty")], "rows": fg}])


# ───────────────────────── runs ─────────────────────────
@bp.route("/runs")
@roles_required("production", "admin", "owner", "accountant")
def runs():
    conn = db()
    rows = []
    for r in prod.list_runs(conn):
        v = prod.run_variance(conn, r["id"])
        y = v["yield"]
        rows.append(((r["run_no"], url_for("production.run", run_id=r["id"])), r["run_date"], r["product_name"], r["batches_m"], r["expected_output_m"],
                     r["actual_output_m"], y["var_pct"], ("FLAG", "badge-red") if v["any_flag"] else ("OK", "badge-green") if r["status"] == "completed" else ("In progress", "badge-blue")))
    return render_template("page.html", title="Production runs", subtitle="Yield variance = (actual − expected) ÷ expected", cols=[
        ("Run", "link"), ("Date", "date"), ("Product", "text"), ("Batches", "qty"), ("Expected", "qty"), ("Actual", "qty"), ("Yield var %", "pct"), ("Status", "badge")],
                           rows=rows, empty="No production runs yet.",
                           actions=[("＋ New run", url_for("production.run_new"), True)] if g.user["role"] == "production" else [])


@bp.route("/runs/new", methods=["GET", "POST"])
@roles_required("production")
def run_new():
    conn = db()
    products = conn.execute("SELECT id, name FROM products WHERE bom_id IS NOT NULL AND active=1 ORDER BY name").fetchall()
    if request.method == "POST":
        try:
            pid, batches = int(request.form["product_id"]), to_milli(request.form["batches"])
            req = prod.requirements(conn, pid, batches)
            issues = {l["item_id"]: to_milli(request.form.get(f"issue_{l['item_id']}", 0)) for l in req["lines"] if l["kind"] == "RM"}
            rid, ok = guard(prod.create_run, conn, g.user, pid, batches, issues, None, request.form.get("notes") or None,
                            ok="Production run created and raw materials issued.")
            if ok:
                return redirect(url_for("production.run", run_id=rid))
        except (ValueError, KeyError, BusinessError) as e:
            flash(str(e), "error")
    pid = request.values.get("product_id", type=int) or (products[0]["id"] if products else None)
    batches = request.values.get("batches") or "1"
    req = None
    if pid:
        try:
            req = prod.requirements(conn, pid, to_milli(batches))
            for l in req["lines"]:
                l["stock_m"] = inv.balance(conn, l["item_id"])[0]
        except (BusinessError, ValueError) as e:
            flash(str(e), "error")
    return render_template("run_new.html", products=products, pid=pid, batches=batches, req=req)


@bp.route("/runs/<int:run_id>")
@roles_required("production", "admin", "owner", "accountant")
def run(run_id):
    conn = db()
    try:
        v = prod.run_variance(conn, run_id)
    except BusinessError:
        abort(404)
    r = v["run"]
    pk = prod.default_packaging(conn, run_id, r["expected_output_m"]) if r["status"] == "in_progress" else {}
    rm_items = conn.execute("SELECT id, name, base_unit FROM items WHERE kind='RM' ORDER BY name").fetchall()
    exp = conn.execute("SELECT * FROM expenses WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
    waste = conn.execute("SELECT w.*, i.name AS item FROM wastage w LEFT JOIN items i ON i.id=w.item_id WHERE w.run_id=? ORDER BY w.id", (run_id,)).fetchall()
    moves = conn.execute("SELECT m.*, i.name, i.base_unit FROM inventory_movements m JOIN items i ON i.id=m.item_id WHERE m.src_type='production_run' "
                         "AND m.src_id=? ORDER BY m.id", (run_id,)).fetchall()
    return render_template("run.html", v=v, r=r, pk=pk, rm_items=rm_items, cost=costing.run_cost(conn, run_id), exp=exp, waste=waste, moves=moves,
                           can_edit=g.user["role"] == "production")


@bp.route("/runs/<int:run_id>/issue", methods=["POST"])
@roles_required("production")
def run_issue(run_id):
    try:
        guard(prod.issue_additional, db(), g.user, run_id, int(request.form["item_id"]), to_milli(request.form["qty"]), ok="Additional material issued.")
    except (ValueError, KeyError) as e:
        flash(str(e), "error")
    return redirect(url_for("production.run", run_id=run_id))


@bp.route("/runs/<int:run_id>/complete", methods=["POST"])
@roles_required("production")
def run_complete(run_id):
    conn, f = db(), request.form
    try:
        pkg = {}
        for k in f:
            if k.startswith("used_"):
                iid = int(k[5:])
                pkg[iid] = {"used_m": to_milli(f.get(k, 0)), "damaged_m": to_milli(f.get(f"dmg_{iid}", 0))}
        guard(prod.complete_run, conn, g.user, run_id, to_milli(f["good"]), pkg, to_milli(f.get("scrap", 0)), "kg", f.get("notes") or None,
              ok="Production recorded - raw material and packaging reduced, finished goods increased.")
    except (ValueError, KeyError) as e:
        flash(str(e), "error")
    return redirect(url_for("production.run", run_id=run_id))


@bp.route("/runs/<int:run_id>/expense", methods=["POST"])
@roles_required("production")
def run_expense(run_id):
    try:
        guard(prod.add_expense, db(), g.user, run_id, f_cat(request.form["category"]), to_paise(request.form["amount"]),
              request.form.get("description") or None, ok="Overhead recorded against the run.")
    except (ValueError, KeyError) as e:
        flash(str(e), "error")
    return redirect(url_for("production.run", run_id=run_id))


def f_cat(c):
    if c not in ("labour", "gas_power", "electricity", "other"):
        raise ValueError("Unknown expense category")
    return c


# ───────────────────────── BOM ─────────────────────────
@bp.route("/boms", methods=["GET", "POST"])
@roles_required("production", "admin")
def boms():
    conn = db()
    if request.method == "POST" and g.user["role"] == "production":
        try:
            name, code = request.form["name"].strip(), request.form["code"].strip().upper()
            out = to_milli(request.form["expected"])
            if not name or not code or out <= 0:
                raise BusinessError("Code, name and expected packets per batch are required")
            with tx(conn):
                bid = conn.execute("INSERT INTO boms(code,name,expected_output_m) VALUES (?,?,?)", (code, name, out)).lastrowid
                log(conn, g.user, "create_bom", "bom", bid, new={"code": code, "expected": out})
            return redirect(url_for("production.bom", bom_id=bid))
        except Exception as e:
            flash(str(e) if isinstance(e, (BusinessError, ValueError, KeyError)) else "Could not save (duplicate code?)", "error")
    form = {"title": "New BOM", "action": url_for("production.boms"), "submit": "Create BOM", "fields": [
        _field("code", "Code", required=True), _field("name", "Name", required=True),
        _field("expected", "Expected packets per batch", "number", required=True, step="1", min="1")]}
    rows = [((b["name"], url_for("production.bom", bom_id=b["id"])), b["code"], b["expected_output_m"],
             ", ".join(p["name"] for p in conn.execute("SELECT name FROM products WHERE bom_id=?", (b["id"],)))) for b in conn.execute("SELECT * FROM boms")]
    return render_template("page.html", title="Bills of materials", subtitle="Recipe per batch. Several products (pack sizes) can share one BOM.",
                           forms=[form] if g.user["role"] == "production" else [], cols=[("BOM", "link"), ("Code", "text"), ("Packets / batch", "qty"), ("Used by", "text")], rows=rows)


@bp.route("/boms/<int:bom_id>")
@roles_required("production", "admin")
def bom(bom_id):
    conn = db()
    b = conn.execute("SELECT * FROM boms WHERE id=?", (bom_id,)).fetchone() or abort(404)
    lines = conn.execute("SELECT bi.*, i.name, i.kind, i.base_unit FROM bom_items bi JOIN items i ON i.id=bi.item_id WHERE bi.bom_id=? "
                         "ORDER BY CASE i.kind WHEN 'RM' THEN 0 ELSE 1 END, i.name", (bom_id,)).fetchall()
    items = conn.execute("SELECT id, name, base_unit, kind FROM items WHERE kind != 'FG' ORDER BY kind DESC, name").fetchall()
    return render_template("bom.html", b=b, lines=lines, items=items, units=UNIT_CHOICES, can_edit=g.user["role"] == "production")


@bp.route("/boms/<int:bom_id>/update", methods=["POST"])
@roles_required("production")
def bom_update(bom_id):
    conn = db()
    try:
        old = conn.execute("SELECT * FROM boms WHERE id=?", (bom_id,)).fetchone()
        out = to_milli(request.form["expected"])
        if out <= 0:
            raise BusinessError("Expected output must be positive")
        with tx(conn):
            conn.execute("UPDATE boms SET name=?, expected_output_m=? WHERE id=?", (request.form["name"].strip(), out, bom_id))
            log(conn, g.user, "edit_bom", "bom", bom_id, old={"expected_output_m": old["expected_output_m"], "name": old["name"]},
                new={"expected_output_m": out, "name": request.form["name"]})
        flash("BOM updated. Existing runs keep their original planned quantities.", "success")
    except (ValueError, KeyError, BusinessError) as e:
        flash(str(e), "error")
    return redirect(url_for("production.bom", bom_id=bom_id))


@bp.route("/boms/<int:bom_id>/item", methods=["POST"])
@roles_required("production")
def bom_item(bom_id):
    conn = db()
    try:
        item = inv.get_item(conn, int(request.form["item_id"]))
        unit, qty = request.form["unit"], to_milli(request.form["qty"])
        if qty <= 0:
            raise BusinessError("Quantity per batch must be positive")
        if not compatible(unit, item["base_unit"]):
            raise BusinessError(f"{item['name']} is stocked in {item['base_unit']}; {unit} cannot be converted")
        with tx(conn):
            conn.execute("INSERT INTO bom_items(bom_id,item_id,qty_m,unit) VALUES (?,?,?,?) ON CONFLICT(bom_id,item_id) DO UPDATE SET "
                         "qty_m=excluded.qty_m, unit=excluded.unit", (bom_id, item["id"], qty, unit))
            log(conn, g.user, "edit_bom_item", "bom", bom_id, new={"item": item["name"], "qty_m": qty, "unit": unit})
        flash("BOM line saved.", "success")
    except (ValueError, KeyError, BusinessError) as e:
        flash(str(e), "error")
    return redirect(url_for("production.bom", bom_id=bom_id))


@bp.route("/boms/<int:bom_id>/item/<int:item_id>/delete", methods=["POST"])
@roles_required("production")
def bom_item_delete(bom_id, item_id):
    conn = db()
    with tx(conn):
        conn.execute("DELETE FROM bom_items WHERE bom_id=? AND item_id=?", (bom_id, item_id))
        log(conn, g.user, "delete_bom_item", "bom", bom_id, old={"item_id": item_id})
    flash("BOM line removed.", "success")
    return redirect(url_for("production.bom", bom_id=bom_id))


# ───────────────────────── purchases & suppliers ─────────────────────────
@bp.route("/purchases")
@roles_required("production", "admin")
def purchases():
    rows = [(r["purchase_date"], r["purchase_no"], r["name"], r["supplier_invoice_no"] or "", r["total_paise"])
            for r in db().execute("SELECT p.*, s.name FROM purchases p JOIN suppliers s ON s.id=p.supplier_id ORDER BY p.id DESC")]
    return render_template("page.html", title="Raw-material purchases", subtitle="Every receipt posts to the stock ledger at its purchase rate",
                           actions=[("＋ Record purchase", url_for("production.purchase_new"), True)] if g.user["role"] == "production" else [],
                           cols=[("Date", "date"), ("Purchase", "text"), ("Supplier", "text"), ("Supplier invoice", "text"), ("Amount", "money")], rows=rows, empty="No purchases yet.")


@bp.route("/purchases/new", methods=["GET", "POST"])
@roles_required("production")
def purchase_new():
    conn = db()
    if request.method == "POST":
        f = request.form
        lines = []
        for i in range(len(f.getlist("item_id"))):
            if f.getlist("qty")[i].strip():
                lines.append({"item_id": int(f.getlist("item_id")[i]), "qty": f.getlist("qty")[i], "unit": f.getlist("unit")[i],
                              "rate": f.getlist("rate")[i] or "0", "batch_no": f.getlist("batch")[i] or None, "expiry_date": f.getlist("expiry")[i] or None})
        try:
            pid, ok = guard(purchasing.record_purchase, conn, g.user, int(f.get("supplier_id") or 0), lines, f.get("supplier_invoice_no") or None,
                            f.get("date") or None, ok="Purchase recorded - stock updated.")
            if ok:
                return redirect(url_for("production.purchases"))
        except (ValueError, KeyError) as e:
            flash(str(e), "error")
    return render_template("purchase_new.html", suppliers=conn.execute("SELECT * FROM suppliers ORDER BY name").fetchall(),
                           items=conn.execute("SELECT * FROM items WHERE kind != 'FG' ORDER BY kind DESC, name").fetchall(), units=UNIT_CHOICES, today=inv.today())


@bp.route("/suppliers", methods=["GET", "POST"])
@roles_required("production", "admin")
def suppliers():
    conn = db()
    if request.method == "POST" and g.user["role"] == "production":
        f = request.form
        if f.get("name", "").strip():
            with tx(conn):
                sid = conn.execute("INSERT INTO suppliers(name,gstin,phone,state) VALUES (?,?,?,?)", (f["name"].strip(), f.get("gstin") or None,
                                                                                                      f.get("phone"), f.get("state"))).lastrowid
                log(conn, g.user, "create_supplier", "supplier", sid, new={"name": f["name"]})
            flash("Supplier added.", "success")
        else:
            flash("Supplier name is required", "error")
        return redirect(url_for("production.suppliers"))
    form = {"title": "Add supplier", "action": url_for("production.suppliers"), "submit": "Add supplier",
            "fields": [_field("name", "Name", required=True), _field("gstin", "GSTIN"), _field("phone", "Phone"), _field("state", "State", value="Tamil Nadu")]}
    rows = [(r["name"], r["gstin"] or "", r["phone"] or "", r["state"] or "") for r in conn.execute("SELECT * FROM suppliers ORDER BY name")]
    return render_template("page.html", title="Suppliers", forms=[form] if g.user["role"] == "production" else [],
                           cols=[("Supplier", "text"), ("GSTIN", "text"), ("Phone", "text"), ("State", "text")], rows=rows)


# ───────────────────────── items & adjustments ─────────────────────────
@bp.route("/items", methods=["GET", "POST"])
@roles_required("production", "admin")
def items():
    conn = db()
    if request.method == "POST" and g.user["role"] == "production":
        f = request.form
        try:
            if f.get("action") == "adjust":
                guard(inv.adjust_stock, conn, g.user, int(f["item_id"]), to_milli(f["delta"]), f.get("reason", ""), ok="Stock adjustment posted to the ledger.")
            else:
                if f["kind"] not in ("RM", "PKG") or f["unit"] not in UNIT_CHOICES:
                    raise BusinessError("Invalid kind or unit")
                with tx(conn):
                    iid = conn.execute("INSERT INTO items(code,name,kind,base_unit,reorder_level_m) VALUES (?,?,?,?,?)",
                                       (f["code"].strip().upper(), f["name"].strip(), f["kind"], f["unit"], to_milli(f.get("reorder", 0)))).lastrowid
                    log(conn, g.user, "create_item", "item", iid, new={"name": f["name"]})
                flash("Item created.", "success")
        except Exception as e:
            flash(str(e) if isinstance(e, (BusinessError, ValueError, KeyError)) else "Could not save (duplicate code?)", "error")
        return redirect(url_for("production.items"))
    itm = [(str(i["id"]), f"{i['name']} ({i['base_unit']})") for i in conn.execute("SELECT * FROM items ORDER BY kind, name")]
    forms = []
    if g.user["role"] == "production":
        forms = [{"title": "Add raw material / packaging item", "action": url_for("production.items"), "submit": "Add item", "fields": [
            _field("action", "", "hidden", value="create"), _field("code", "Code", required=True), _field("name", "Name", required=True),
            _field("kind", "Kind", "select", options=[("RM", "Raw material"), ("PKG", "Packaging")]),
            _field("unit", "Base unit", "select", options=[(u, u) for u in UNIT_CHOICES]), _field("reorder", "Reorder level", "number", step="0.001", value="0")]},
                 {"title": "Stock adjustment (damage, count correction)", "action": url_for("production.items"), "submit": "Post adjustment", "fields": [
                     _field("action", "", "hidden", value="adjust"), _field("item_id", "Item", "select", options=itm),
                     _field("delta", "Quantity (+ add / − remove)", "number", step="0.001", required=True), _field("reason", "Reason (mandatory)", required=True)]}]
    rows = [(r["code"], r["name"], r["kind"], r["base_unit"], r["qty_m"], r["reorder_level_m"], r["value_paise"]) for r in inv.stock_summary(conn)]
    return render_template("page.html", title="Items & stock adjustments", forms=forms,
                           cols=[("Code", "text"), ("Item", "text"), ("Kind", "text"), ("Unit", "text"), ("On hand", "qty"), ("Reorder level", "qty"), ("Value", "money")], rows=rows)
