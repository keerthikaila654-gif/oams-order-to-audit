from decimal import Decimal, ROUND_HALF_UP

from ..db import tx, next_seq
from ..money import D, to_milli
from ..units import convert_m, compatible
from . import BusinessError
from . import inventory as inv
from .audit import log


def record_purchase(conn, user, supplier_id, lines, supplier_invoice_no=None, purchase_date=None):
    """lines: [{item_id, qty (str/Decimal), unit, rate (₹ per entered unit), batch_no?, expiry_date?}]"""
    if not lines:
        raise BusinessError("Add at least one item to the purchase")
    with tx(conn):
        if not conn.execute("SELECT 1 FROM suppliers WHERE id=?", (supplier_id,)).fetchone():
            raise BusinessError("Select a supplier")
        n = next_seq(conn, "PUR")
        pno = f"PUR-{n:04d}"
        on = purchase_date or inv.today()
        cur = conn.execute("INSERT INTO purchases(purchase_no,supplier_id,supplier_invoice_no,purchase_date,total_paise,created_by)"
                           " VALUES (?,?,?,?,0,?)", (pno, supplier_id, supplier_invoice_no, on, user["id"]))
        pid, total = cur.lastrowid, 0
        for ln in lines:
            item = inv.get_item(conn, ln["item_id"])
            if item["kind"] == "FG":
                raise BusinessError("Finished goods cannot be purchased; they are produced")
            qty_m, rate = to_milli(ln["qty"]), D(ln["rate"])
            unit = ln.get("unit") or item["base_unit"]
            if qty_m <= 0 or rate < 0:
                raise BusinessError(f"Quantity must be positive and rate non-negative for {item['name']}")
            if not compatible(unit, item["base_unit"]):
                raise BusinessError(f"{item['name']} is stocked in {item['base_unit']}; cannot enter in {unit}")
            base_m = convert_m(qty_m, unit, item["base_unit"])
            amount = int((Decimal(qty_m) / 1000 * rate * 100).quantize(Decimal(1), ROUND_HALF_UP))
            conn.execute("INSERT INTO purchase_items(purchase_id,item_id,entered_qty_m,entered_unit,rate_per_entered_unit,"
                         "qty_m,amount_paise,batch_no,expiry_date) VALUES (?,?,?,?,?,?,?,?,?)",
                         (pid, item["id"], qty_m, unit, str(rate), base_m, amount, ln.get("batch_no"), ln.get("expiry_date")))
            inv.post_receipt(conn, item["id"], "PURCHASE", base_m, amount, "purchase", pid, pno, user, on,
                             ln.get("batch_no"), ln.get("expiry_date"))
            total += amount
        conn.execute("UPDATE purchases SET total_paise=? WHERE id=?", (total, pid))
        log(conn, user, "record_purchase", "purchase", pid, new={"lines": lines, "total_paise": total}, ref=pno)
        return pid
