"""Web layer: app wiring, authentication, CSRF, role guards and shared views."""
import functools
import hmac
import os
import secrets
import time

from flask import (Blueprint, Response, abort, current_app, flash, g, jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)
from werkzeug.security import check_password_hash

from .. import db as dbm
from ..money import fmt_inr, fmt_pct, fmt_qty
from ..services import BusinessError, exports, inventory as inv, reports, sales

HOME = {"salesman": "salesman.home", "admin": "admin.home", "production": "production.home",
        "accountant": "reports.index", "owner": "owner.home"}

NAV = {
    "salesman": [("Home", "salesman.home"), ("New order", "salesman.new_order"), ("My orders", "salesman.orders"),
                 ("Customers", "salesman.customers")],
    "admin": [("Dashboard", "admin.home"), ("Orders", "admin.orders"), ("Invoices", "admin.invoices"),
              ("Payments", "admin.payments"), ("Customers", "admin.customers"), ("Products", "admin.products"),
              ("Stock", "shared.stock"), ("Stock ledger", "shared.ledger"), ("Reports", "reports.index"),
              ("Users", "admin.users"), ("Settings", "admin.settings"), ("Audit log", "admin.audit"),
              ("Benchmark check", "admin.benchmark")],
    "production": [("Dashboard", "production.home"), ("Production runs", "production.runs"), ("New run", "production.run_new"),
                   ("BOMs", "production.boms"), ("Purchases", "production.purchases"), ("Suppliers", "production.suppliers"),
                   ("Items & adjustments", "production.items"), ("Stock", "shared.stock"), ("Stock ledger", "shared.ledger")],
    "accountant": [("Reports", "reports.index"), ("Stock", "shared.stock"), ("Stock ledger", "shared.ledger")],
    "owner": [("Dashboard", "owner.home"), ("Stock", "shared.stock"), ("Reports", "reports.index")],
}

shared = Blueprint("shared", __name__)
auth = Blueprint("auth", __name__)
reports_bp = Blueprint("reports", __name__, url_prefix="/reports")


# ───────────────────────── helpers ─────────────────────────
def db():
    if "db" not in g:
        g.db = dbm.connect(current_app.config["DATABASE"])
    return g.db


def current_user():
    return g.get("user")


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


def login_required(f):
    @functools.wraps(f)
    def w(*a, **k):
        if not g.user:
            if ("/api/" in request.path):
                return jsonify(error="login required"), 401
            return redirect(url_for("auth.login", next=request.path))
        return f(*a, **k)
    return w


def roles_required(*roles):
    def deco(f):
        @functools.wraps(f)
        def w(*a, **k):
            if not g.user:
                if ("/api/" in request.path):
                    return jsonify(error="login required"), 401
                return redirect(url_for("auth.login", next=request.path))
            if g.user["role"] not in roles:
                abort(403)
            return f(*a, **k)
        return w
    return deco


def guard(fn, *args, ok=None, **kwargs):
    """Run a service call; show BusinessError / ValueError as a flash message instead of a crash."""
    try:
        r = fn(*args, **kwargs)
        if ok:
            flash(ok, "success")
        return r, True
    except (BusinessError, ValueError) as e:
        flash(str(e), "error")
        return None, False


def date_range():
    d0, d1 = reports.default_range()
    return request.args.get("start") or d0, request.args.get("end") or d1


# ───────────────────────── app wiring ─────────────────────────
def init_app(app):
    from . import admin, owner, production, salesman

    if not os.path.exists(app.config["DATABASE"]):
        from .. import reset_database
        reset_database(app.config["DATABASE"])

    app.jinja_env.filters["zip"] = lambda first, *others: zip(first, *others)

    @app.before_request
    def load_user():
        g.user = None
        uid = session.get("uid")
        if uid:
            u = db().execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
            if u:
                g.user = u
            else:
                session.clear()
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            tok = request.form.get("_csrf") or request.headers.get("X-CSRF-Token") or ""
            if not hmac.compare_digest(tok, session.get("_csrf", "x")):
                if request.endpoint == "auth.login" and not g.user:
                    session["_csrf"] = secrets.token_hex(16)   # stale login form: ask to retry
                abort(400, "Security token missing or expired - reload the page and try again.")
            # The auditor is strictly read-only: block every write except logging out.
            if g.user and g.user["role"] == "accountant" and request.endpoint != "auth.logout":
                abort(403, "Accountant / auditor access is read-only.")

    @app.teardown_appcontext
    def close_db(_):
        d = g.pop("db", None)
        if d is not None:
            d.close()

    @app.context_processor
    def inject():
        role = g.user["role"] if g.get("user") else None
        company = ""
        if role:
            company = db().execute("SELECT value FROM settings WHERE key='company_name'").fetchone()
            company = company[0] if company else ""
        return {"nav": NAV.get(role, []), "csrf_token": csrf_token, "company_name": company, "role": role,
                "fmt": exports.display, "inr": fmt_inr, "qty": fmt_qty, "pctf": fmt_pct}

    @app.errorhandler(403)
    def e403(e):
        return render_template("error.html", code=403, message=getattr(e, "description", "Access denied")), 403

    @app.errorhandler(404)
    def e404(e):
        return render_template("error.html", code=404, message="Page not found"), 404

    @app.errorhandler(400)
    def e400(e):
        return render_template("error.html", code=400, message=getattr(e, "description", "Bad request")), 400

    @app.after_request
    def headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        return resp

    app.register_blueprint(auth)
    app.register_blueprint(shared)
    app.register_blueprint(reports_bp)
    app.register_blueprint(salesman.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(production.bp)
    app.register_blueprint(owner.bp)

    @app.route("/sw.js")
    def service_worker():
        r = send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")
        r.headers["Service-Worker-Allowed"] = "/"
        r.headers["Cache-Control"] = "no-cache"
        return r


# ───────────────────────── auth ─────────────────────────
_FAILS = {}


@auth.route("/")
def index():
    if g.user:
        return redirect(url_for(HOME[g.user["role"]]))
    return redirect(url_for("auth.login"))


@auth.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        key = (request.remote_addr, username)
        tries, until = _FAILS.get(key, (0, 0))
        if until > time.time():
            flash("Too many failed attempts. Try again in a minute.", "error")
            return render_template("login.html"), 429
        u = db().execute("SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()
        if u and check_password_hash(u["password_hash"], request.form.get("password", "")):
            _FAILS.pop(key, None)
            session.clear()
            session["uid"] = u["id"]
            session.permanent = True
            nxt = request.args.get("next", "")
            if nxt.startswith("/") and not nxt.startswith("//"):
                return redirect(nxt)
            return redirect(url_for(HOME[u["role"]]))
        tries += 1
        _FAILS[key] = (tries, time.time() + 60 if tries >= 5 else 0)
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@auth.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@auth.route("/api/csrf")
@login_required
def api_csrf():
    return jsonify(token=csrf_token())


# ───────────────────────── shared: stock, ledger ─────────────────────────
@shared.route("/stock")
@roles_required("admin", "production", "accountant", "owner")
def stock():
    conn = db()
    groups = []
    for kind, title in (("RM", "Raw materials"), ("PKG", "Packaging materials"), ("FG", "Finished goods")):
        rows = []
        for r in inv.stock_summary(conn, kind):
            low = kind != "FG" and r["reorder_level_m"] and r["qty_m"] <= r["reorder_level_m"]
            avg = (r["value_paise"] / 100) / (r["qty_m"] / 1000) if r["qty_m"] else 0
            rows.append((r["name"], r["base_unit"], r["qty_m"], f"{avg:,.2f}", r["value_paise"],
                         r["reorder_level_m"] if kind != "FG" else None,
                         ("LOW STOCK", "badge-red") if low else ("OK", "badge-green") if kind != "FG" else ""))
        groups.append({"title": title, "cols": [("Item", "text"), ("Unit", "text"), ("On hand", "qty"), ("Avg cost ₹/unit", "text"),
                                                 ("Value", "money"), ("Reorder level", "qty"), ("Status", "badge")], "rows": rows})
    problems = inv.reconcile(conn)
    return render_template("stock.html", groups=groups, problems=problems)


@shared.route("/ledger")
@roles_required("admin", "production", "accountant", "owner")
def ledger():
    conn = db()
    start, end = date_range()
    item_id = request.args.get("item", type=int)
    rep = reports.stock_ledger(conn, start, end, item_id)
    items = conn.execute("SELECT id, name FROM items ORDER BY kind, name").fetchall()
    return render_template("report.html", rep=rep, start=start, end=end, items=items, item_id=item_id, slug="stock_ledger",
                           filters_item=True)


# ───────────────────────── reports ─────────────────────────
@reports_bp.route("/")
@roles_required("accountant", "owner", "admin")
def index():
    groups = {}
    for slug, title, _, grp in reports.REPORTS:
        groups.setdefault(grp, []).append((slug, title))
    start, end = date_range()
    conn = db()
    s = reports.sales_register(conn, start, end)
    kpis = [("Taxable sales", fmt_inr(s["totals"][5]), f"{len(s['rows'])} invoices", ""),
            ("GST collected", fmt_inr(s["totals"][6] + s["totals"][7] + s["totals"][8]), "CGST + SGST + IGST", ""),
            ("Collections", fmt_inr(reports.collections(conn, start, end)["totals"][6] if reports.collections(conn, start, end)["rows"] else 0), "in period", ""),
            ("Receivables", fmt_inr(sum(r["outstanding_paise"] for r in sales.outstanding_by_customer(conn))), "outstanding today", "warn")]
    return render_template("reports_index.html", groups=groups, start=start, end=end, kpis=kpis)


@reports_bp.route("/<slug>")
@roles_required("accountant", "owner", "admin")
def view(slug):
    if slug not in reports.REPORT_MAP:
        abort(404)
    conn = db()
    start, end = date_range()
    kw = {}
    if slug == "stock_ledger" and request.args.get("item", type=int):
        kw["item_id"] = request.args.get("item", type=int)
    rep = reports.run_report(conn, slug, start, end, **kw)
    fmt = request.args.get("fmt")
    if fmt in ("csv", "xlsx", "pdf"):
        company = sales.seller(conn)["company_name"]
        fname = f"{slug}_{start}_to_{end}.{fmt}"
        if fmt == "csv":
            data, mime = exports.to_csv(rep, start, end), "text/csv; charset=utf-8"
        elif fmt == "xlsx":
            data, mime = exports.to_xlsx(rep, start, end), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        else:
            data, mime = exports.to_pdf(rep, start, end, company), "application/pdf"
        return Response(data, mimetype=mime, headers={"Content-Disposition": f'attachment; filename="{fname}"'})
    items = conn.execute("SELECT id, name FROM items ORDER BY kind, name").fetchall() if slug == "stock_ledger" else []
    return render_template("report.html", rep=rep, start=start, end=end, slug=slug, items=items,
                           item_id=kw.get("item_id"), filters_item=slug == "stock_ledger")
