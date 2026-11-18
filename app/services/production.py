"""BOM-driven production: requirements, issue, completion, variance.

Lifecycle of a run
  1. create_run()    -> status 'in_progress'; raw materials issued (stock ledger PRODUCTION_ISSUE)
  2. complete_run()  -> packaging consumed (PRODUCTION_CONSUME) + damaged packaging (WASTAGE),
                        finished goods received (PRODUCTION_RECEIPT) at material cost, status 'completed'
  3. add_expense()   -> labour / gas / power costs assigned directly to the run (see costing.py)
"""
from decimal import Decimal

from ..db import tx, next_seq, get_setting
from ..money import D, round_div, pct, Q2
from ..units import convert_m
from . import BusinessError
from . import inventory as inv
from .audit import log


# ───────────────────────── planning ─────────────────────────
def get_product_bom(conn, product_id):
    p = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not p:
        raise BusinessError("Unknown product")
    if not p["bom_id"]:
        raise BusinessError(f"{p['name']} has no BOM defined")
    bom = conn.execute("SELECT * FROM boms WHERE id=?", (p["bom_id"],)).fetchone()
    return p, bom


def requirements(conn, product_id, batches_m):
    """Expected material requirement for `batches_m` batches (thousandths).

    Each BOM line is converted from the BOM unit to the item's base unit, then scaled by batch count.
    """
    p, bom = get_product_bom(conn, product_id)
    lines = conn.execute(
        "SELECT bi.*, i.name, i.kind, i.base_unit FROM bom_items bi JOIN items i ON i.id=bi.item_id "
        "WHERE bi.bom_id=? ORDER BY CASE i.kind WHEN 'RM' THEN 0 ELSE 1 END, i.name", (bom["id"],)).fetchall()
    out = []
    for l in lines:
        per_batch_base = convert_m(l["qty_m"], l["unit"], l["base_unit"])
        planned = round_div(per_batch_base * batches_m, 1000)
        out.append({"item_id": l["item_id"], "name": l["name"], "kind": l["kind"], "unit": l["base_unit"],
                    "per_batch_m": per_batch_base, "planned_m": planned})
    expected_output_m = round_div(bom["expected_output_m"] * batches_m, 1000)
    return {"product": p, "bom": bom, "lines": out, "expected_output_m": expected_output_m}


def tolerance(conn) -> Decimal:
    return D(get_setting(conn, "variance_tolerance_pct", "3"))


# ───────────────────────── step 1: issue ─────────────────────────
def create_run(conn, user, product_id, batches_m, issues, run_date=None, notes=None):
    """Create a run and issue raw materials.  `issues` = {item_id: actual_issued_m}."""
    if batches_m <= 0:
        raise BusinessError("Batch size must be greater than zero")
    with tx(conn):
        req = requirements(conn, product_id, batches_m)
        n = next_seq(conn, "RUN")
        run_no = f"PR-{n:04d}"
        cur = conn.execute(
            "INSERT INTO production_runs(run_no,product_id,bom_id,batches_m,expected_output_m,run_date,"
            "supervisor_id,notes,tolerance_pct) VALUES (?,?,?,?,?,?,?,?,?)",
            (run_no, product_id, req["bom"]["id"], batches_m, req["expected_output_m"],
             run_date or inv.today(), user["id"], notes, str(tolerance(conn))))
        run_id = cur.lastrowid
        for ln in req["lines"]:
            conn.execute("INSERT INTO production_consumption(run_id,item_id,planned_m,actual_m) VALUES (?,?,?,0)",
                         (run_id, ln["item_id"], ln["planned_m"]))
        for ln in req["lines"]:
            if ln["kind"] != "RM":
                continue
            actual = issues.get(ln["item_id"], ln["planned_m"])
            if actual < 0:
                raise BusinessError("Issued quantity cannot be negative")
            if actual > 0:
                _issue(conn, user, run_id, run_no, ln["item_id"], actual, run_date)
        log(conn, user, "create_run", "production_run", run_id, new={"product_id": product_id, "batches_m": batches_m,
                                                                      "issues": issues}, ref=run_no)
        return run_id


def _issue(conn, user, run_id, run_no, item_id, qty_m, on=None):
    inv.post_issue(conn, item_id, "PRODUCTION_ISSUE", qty_m, "production_run", run_id, run_no, user, on)
    conn.execute("INSERT INTO production_consumption(run_id,item_id,planned_m,actual_m) VALUES (?,?,0,?) "
                 "ON CONFLICT(run_id,item_id) DO UPDATE SET actual_m = actual_m + excluded.actual_m",
                 (run_id, item_id, qty_m))


def issue_additional(conn, user, run_id, item_id, qty_m):
    with tx(conn):
        run = get_run(conn, run_id)
        if run["status"] != "in_progress":
            raise BusinessError("Materials can only be issued to a run that is in progress")
        item = inv.get_item(conn, item_id)
        if item["kind"] != "RM":
            raise BusinessError("Only raw materials are issued before completion; packaging is recorded on completion")
        _issue(conn, user, run_id, run["run_no"], item_id, qty_m)
        log(conn, user, "issue_additional", "production_run", run_id, new={"item_id": item_id, "qty_m": qty_m},
            ref=run["run_no"])


# ───────────────────────── step 2: completion ─────────────────────────
def complete_run(conn, user, run_id, good_m, packaging, scrap_m=0, scrap_unit="kg", notes=None):
    """Record actual output.  `packaging` = {item_id: {'used_m': .., 'damaged_m': ..}} (item base units)."""
    if good_m < 0:
        raise BusinessError("Actual output cannot be negative")
    with tx(conn):
        run = get_run(conn, run_id)
        if run["status"] != "in_progress":
            raise BusinessError("This run is already completed")
        rno = run["run_no"]
        pkg_items = {r["item_id"] for r in conn.execute(
            "SELECT pc.item_id FROM production_consumption pc JOIN items i ON i.id=pc.item_id "
            "WHERE pc.run_id=? AND i.kind='PKG'", (run_id,))}
        for item_id, v in packaging.items():
            if item_id not in pkg_items:
                raise BusinessError("Packaging item is not part of this run's BOM")
            used, dmg = v.get("used_m", 0), v.get("damaged_m", 0)
            if used < 0 or dmg < 0:
                raise BusinessError("Packaging quantities cannot be negative")
            if used:
                inv.post_issue(conn, item_id, "PRODUCTION_CONSUME", used, "production_run", run_id, rno, user)
            if dmg:
                _, val = inv.post_issue(conn, item_id, "WASTAGE", dmg, "production_run", run_id, rno, user,
                                        note="Damaged packaging")
                conn.execute("INSERT INTO wastage(run_id,kind,item_id,qty_m,unit,value_paise,reason) "
                             "VALUES (?,?,?,?,?,?,?)",
                             (run_id, "DAMAGED_PACKAGING", item_id, dmg, inv.get_item(conn, item_id)["base_unit"],
                              val, "Damaged during packing"))
            conn.execute("UPDATE production_consumption SET actual_m = actual_m + ? WHERE run_id=? AND item_id=?",
                         (used + dmg, run_id, item_id))
        material_cost = conn.execute(
            "SELECT COALESCE(SUM(-value_paise),0) FROM inventory_movements WHERE src_type='production_run' "
            "AND src_id=? AND txn_type IN ('PRODUCTION_ISSUE','PRODUCTION_CONSUME','WASTAGE')", (run_id,)).fetchone()[0]
        product = conn.execute("SELECT * FROM products WHERE id=?", (run["product_id"],)).fetchone()
        if good_m:
            inv.post_receipt(conn, product["item_id"], "PRODUCTION_RECEIPT", good_m, material_cost,
                             "production_run", run_id, rno, user)
        conn.execute("INSERT INTO production_output(run_id,product_id,good_qty_m,material_cost_paise) VALUES (?,?,?,?)",
                     (run_id, run["product_id"], good_m, material_cost))
        lost = run["expected_output_m"] - good_m
        if lost > 0:
            conn.execute("INSERT INTO wastage(run_id,kind,qty_m,unit,value_paise,reason) VALUES (?,?,?,?,?,?)",
                         (run_id, "YIELD_LOSS", lost, "packet",
                          round_div(material_cost * lost, run["expected_output_m"]),
                          "Expected output minus actual good output"))
        if scrap_m:
            conn.execute("INSERT INTO wastage(run_id,kind,qty_m,unit,value_paise,reason) VALUES (?,?,?,?,?,?)",
                         (run_id, "SCRAP", scrap_m, scrap_unit, 0, "Scrap / rejects recorded by supervisor"))
        conn.execute("UPDATE production_runs SET status='completed', actual_output_m=?, completed_at=datetime('now'), "
                     "notes=COALESCE(?, notes) WHERE id=?", (good_m, notes, run_id))
        log(conn, user, "complete_run", "production_run", run_id,
            old={"status": "in_progress"}, new={"status": "completed", "good_m": good_m, "packaging": packaging},
            ref=rno)


def add_expense(conn, user, run_id, category, amount_paise, description=None, on=None):
    if amount_paise <= 0:
        raise BusinessError("Expense amount must be positive")
    with tx(conn):
        run = get_run(conn, run_id)
        cur = conn.execute("INSERT INTO expenses(run_id,category,amount_paise,expense_date,description,created_by) "
                           "VALUES (?,?,?,?,?,?)",
                           (run_id, category, amount_paise, on or inv.today(), description, user["id"]))
        log(conn, user, "add_expense", "expense", cur.lastrowid,
            new={"run": run["run_no"], "category": category, "amount_paise": amount_paise}, ref=run["run_no"])
        return cur.lastrowid


# ───────────────────────── queries ─────────────────────────
def get_run(conn, run_id):
    r = conn.execute("SELECT r.*, p.name AS product_name, p.item_id AS fg_item_id FROM production_runs r "
                     "JOIN products p ON p.id=r.product_id WHERE r.id=?", (run_id,)).fetchone()
    if not r:
        raise BusinessError("Production run not found")
    return r


def run_variance(conn, run_id):
    """Planned-vs-actual consumption and yield for one run.

    variance %  = (actual - planned) / planned x 100        (positive = used more than the recipe)
    yield var % = (actual output - expected output) / expected x 100
    flagged     = |variance %| > tolerance (the tolerance in force when the run was created)
    """
    run = get_run(conn, run_id)
    tol = D(run["tolerance_pct"])
    rows = []
    for r in conn.execute(
            "SELECT pc.*, i.name, i.kind, i.base_unit FROM production_consumption pc JOIN items i ON i.id=pc.item_id "
            "WHERE pc.run_id=? ORDER BY CASE i.kind WHEN 'RM' THEN 0 ELSE 1 END, i.name", (run_id,)):
        pending = r["kind"] == "PKG" and run["status"] != "completed"
        var_m = r["actual_m"] - r["planned_m"]
        var_pct = None if pending else pct(var_m, r["planned_m"])
        rows.append({"item_id": r["item_id"], "name": r["name"], "kind": r["kind"], "unit": r["base_unit"],
                     "planned_m": r["planned_m"], "actual_m": None if pending else r["actual_m"],
                     "var_m": None if pending else var_m, "var_pct": var_pct,
                     "flagged": var_pct is not None and abs(var_pct) > tol, "pending": pending})
    done = run["status"] == "completed"
    y_pct = pct(run["actual_output_m"] - run["expected_output_m"], run["expected_output_m"]) if done else None
    waste_pct = None
    if done:
        lost = max(run["expected_output_m"] - run["actual_output_m"], 0)
        waste_pct = pct(lost, run["expected_output_m"])
    return {"run": run, "tolerance": tol, "rows": rows,
            "yield": {"expected_m": run["expected_output_m"], "actual_m": run["actual_output_m"],
                      "var_m": (run["actual_output_m"] - run["expected_output_m"]) if done else None,
                      "var_pct": y_pct, "flagged": y_pct is not None and abs(y_pct) > tol,
                      "wastage_pct": waste_pct},
            "any_flag": any(r["flagged"] for r in rows) or (y_pct is not None and abs(y_pct) > tol)}


def list_runs(conn, start=None, end=None):
    sql = ("SELECT r.*, p.name AS product_name FROM production_runs r JOIN products p ON p.id=r.product_id")
    args, w = [], []
    if start: w.append("r.run_date >= ?"); args.append(start)
    if end: w.append("r.run_date <= ?"); args.append(end)
    if w: sql += " WHERE " + " AND ".join(w)
    return conn.execute(sql + " ORDER BY r.id DESC", args).fetchall()


def default_packaging(conn, run_id, good_m):
    """Suggested packaging usage for the supervisor: BOM quantity scaled by actual yield, rounded to whole pieces."""
    run = get_run(conn, run_id)
    out = {}
    for r in conn.execute("SELECT pc.item_id, pc.planned_m, i.name, i.base_unit FROM production_consumption pc "
                          "JOIN items i ON i.id=pc.item_id WHERE pc.run_id=? AND i.kind='PKG' ORDER BY i.name", (run_id,)):
        scaled = round_div(r["planned_m"] * good_m, run["expected_output_m"]) if run["expected_output_m"] else 0
        out[r["item_id"]] = {"name": r["name"], "unit": r["base_unit"], "planned_m": r["planned_m"],
                             "suggested_m": round_div(scaled, 1000) * 1000}
    return out
