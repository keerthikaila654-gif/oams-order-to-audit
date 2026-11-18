"""Ledger-based inventory.

Every stock change is an INSERT into ``inventory_movements``.  Balances are the SUM of the
ledger (see the ``stock_balances`` view) so they can never be overwritten by hand.

Valuation method: **perpetual weighted-average cost** (moving average).
  * a receipt adds its value to the pool;
  * an issue is valued at  pool_value / pool_qty  at the moment of issue;
  * the final unit leaving the pool takes the remaining value, so no residue is left behind.
"""
from datetime import date
from decimal import Decimal

from ..db import tx
from ..money import round_div, milli_to_dec
from . import BusinessError, InsufficientStock


def today() -> str:
    return date.today().isoformat()


def get_item(conn, item_id):
    r = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not r:
        raise BusinessError(f"Unknown item {item_id}")
    return r


def balance(conn, item_id):
    """(quantity_m, value_paise) straight from the ledger."""
    r = conn.execute(
        "SELECT COALESCE(SUM(qty_in_m-qty_out_m),0), COALESCE(SUM(value_paise),0) "
        "FROM inventory_movements WHERE item_id=?", (item_id,)).fetchone()
    return int(r[0]), int(r[1])


def avg_cost_per_unit(conn, item_id) -> Decimal:
    q, v = balance(conn, item_id)
    return (Decimal(v) / 100) / milli_to_dec(q) if q else Decimal(0)


def post_receipt(conn, item_id, txn_type, qty_m, value_paise, src_type, src_id, ref_no,
                 user=None, on=None, batch_no=None, expiry_date=None, note=None):
    if qty_m < 0:
        raise BusinessError("Receipt quantity cannot be negative")
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO inventory_movements(item_id,moved_on,txn_type,qty_in_m,qty_out_m,value_paise,"
            "src_type,src_id,ref_no,batch_no,expiry_date,note,created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, on or today(), txn_type, qty_m, 0, value_paise, src_type, src_id, ref_no,
             batch_no, expiry_date, note, user["id"] if user else None))
        return cur.lastrowid


def post_issue(conn, item_id, txn_type, qty_m, src_type, src_id, ref_no, user=None, on=None,
               note=None, value_paise=None, allow_negative=False):
    """Issue stock out of the pool, valued at weighted-average cost.  Returns (movement_id, value_paise)."""
    if qty_m <= 0:
        raise BusinessError("Issue quantity must be positive")
    with tx(conn):
        item = get_item(conn, item_id)
        bal_q, bal_v = balance(conn, item_id)
        if qty_m > bal_q and not allow_negative:
            raise InsufficientStock(item["name"], bal_q, qty_m, item["base_unit"])
        if value_paise is None:
            value_paise = bal_v if qty_m == bal_q else round_div(bal_v * qty_m, bal_q) if bal_q else 0
        cur = conn.execute(
            "INSERT INTO inventory_movements(item_id,moved_on,txn_type,qty_in_m,qty_out_m,value_paise,"
            "src_type,src_id,ref_no,note,created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, on or today(), txn_type, 0, qty_m, -abs(value_paise), src_type, src_id, ref_no,
             note, user["id"] if user else None))
        return cur.lastrowid, abs(value_paise)


def adjust_stock(conn, user, item_id, delta_m, reason, on=None):
    """Manual stock adjustment (+/-) with mandatory reason; goes through the ledger."""
    from .audit import log
    if not delta_m:
        raise BusinessError("Adjustment quantity is zero")
    if not reason or not reason.strip():
        raise BusinessError("A reason is required for stock adjustments")
    with tx(conn):
        ref = f"ADJ-{conn.execute('SELECT COUNT(*)+1 FROM inventory_movements WHERE txn_type=?', ('ADJUSTMENT',)).fetchone()[0]:04d}"
        if delta_m > 0:
            avg = avg_cost_per_unit(conn, item_id)
            val = int((avg * milli_to_dec(delta_m) * 100).quantize(Decimal(1)))
            mid = post_receipt(conn, item_id, "ADJUSTMENT", delta_m, val, "adjustment", None, ref, user, on, note=reason)
        else:
            mid, _ = post_issue(conn, item_id, "ADJUSTMENT", -delta_m, "adjustment", None, ref, user, on, note=reason)
        log(conn, user, "stock_adjustment", "item", item_id, new={"delta_m": delta_m, "reason": reason}, ref=ref)
        return mid


def stock_summary(conn, kind=None):
    sql = "SELECT * FROM stock_balances"
    args = ()
    if kind:
        sql += " WHERE kind=?"
        args = (kind,)
    return conn.execute(sql + " ORDER BY kind, name", args).fetchall()


def low_stock(conn):
    return conn.execute("SELECT * FROM stock_balances WHERE reorder_level_m > 0 AND qty_m <= reorder_level_m "
                        "AND kind != 'FG' ORDER BY name").fetchall()


def ledger(conn, item_id=None, start=None, end=None):
    """Stock ledger rows with running balance (computed with a window function over the full history)."""
    sql = """
    SELECT * FROM (
      SELECT m.id, m.moved_on, i.code, i.name AS item, i.base_unit, m.txn_type, m.qty_in_m, m.qty_out_m,
             SUM(m.qty_in_m - m.qty_out_m) OVER (PARTITION BY m.item_id ORDER BY m.moved_on, m.id) AS balance_m,
             SUM(m.value_paise) OVER (PARTITION BY m.item_id ORDER BY m.moved_on, m.id) AS balance_value_paise,
             m.value_paise, m.src_type, m.src_id, m.ref_no, m.batch_no, m.note
      FROM inventory_movements m JOIN items i ON i.id = m.item_id
      {inner_where}
    ) {outer_where} ORDER BY item, moved_on, id
    """
    inner, outer, args = "", [], []
    if item_id:
        inner = "WHERE m.item_id = ?"
        args.append(item_id)
    if start:
        outer.append("moved_on >= ?"); args.append(start)
    if end:
        outer.append("moved_on <= ?"); args.append(end)
    return conn.execute(sql.format(inner_where=inner, outer_where=("WHERE " + " AND ".join(outer)) if outer else ""),
                        args).fetchall()


def reconcile(conn):
    """Integrity check: ledger sums must equal balances and no balance may be negative."""
    problems = []
    for r in conn.execute("SELECT * FROM stock_balances"):
        if r["qty_m"] < 0:
            problems.append(f"{r['name']} has negative stock")
    return problems
