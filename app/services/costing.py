"""Product costing per production run.

Material valuation : perpetual weighted-average cost (see inventory.py).  Every material line shows the
                     exact ledger value that left stock for the run, so the figure is traceable.
Packaging          : pouches/cartons consumed (good + damaged) at weighted-average cost.
Overhead rule      : labour, gas/power, electricity, other are recorded *against the run* and are
                     charged 100 % to that run (direct assignment).
Cost per packet    : (materials + packaging + overheads) / GOOD packets.  Losses (yield loss, damaged
                     pouches, over-consumption) therefore raise the cost of the good output - they are
                     not hidden.
Inventory value    : finished goods are carried at material + packaging cost only (overheads are shown
                     in the costing report but not capitalised into FG stock value) - documented assumption.
"""
from decimal import Decimal, ROUND_HALF_UP

from ..money import Q2, round_div
from . import production as prod

VALUATION_METHOD = ("Weighted-average cost (perpetual): each issue is valued at pool value ÷ pool quantity "
                    "at the time of issue.")
OVERHEAD_RULE = ("Overheads (labour, gas & power, other) are entered against a specific production run and "
                 "charged in full to that run; cost per packet = total run cost ÷ good packets produced.")


def run_cost(conn, run_id):
    run = prod.get_run(conn, run_id)
    product = conn.execute("SELECT * FROM products WHERE id=?", (run["product_id"],)).fetchone()
    lines = []
    for r in conn.execute(
            "SELECT i.name, i.kind, i.base_unit, SUM(m.qty_out_m) AS qty_m, SUM(-m.value_paise) AS value_paise "
            "FROM inventory_movements m JOIN items i ON i.id=m.item_id "
            "WHERE m.src_type='production_run' AND m.src_id=? "
            "AND m.txn_type IN ('PRODUCTION_ISSUE','PRODUCTION_CONSUME','WASTAGE') "
            "GROUP BY m.item_id ORDER BY CASE i.kind WHEN 'RM' THEN 0 ELSE 1 END, i.name", (run_id,)):
        rate = (Decimal(r["value_paise"]) / 100) / (Decimal(r["qty_m"]) / 1000) if r["qty_m"] else Decimal(0)
        lines.append({"name": r["name"], "kind": r["kind"], "unit": r["base_unit"], "qty_m": r["qty_m"],
                      "rate": rate.quantize(Decimal("0.0001")), "value_paise": r["value_paise"]})
    rm = sum(l["value_paise"] for l in lines if l["kind"] == "RM")
    pkg = sum(l["value_paise"] for l in lines if l["kind"] == "PKG")
    exp = {c: 0 for c in ("labour", "gas_power", "electricity", "other")}
    for e in conn.execute("SELECT category, SUM(amount_paise) AS a FROM expenses WHERE run_id=? GROUP BY category", (run_id,)):
        exp[e["category"]] = e["a"]
    overhead = sum(exp.values())
    total = rm + pkg + overhead
    good_m = run["actual_output_m"] or 0
    cpp = (Decimal(total) / 100) / (Decimal(good_m) / 1000) if good_m else None
    mat_cpp = (Decimal(rm + pkg) / 100) / (Decimal(good_m) / 1000) if good_m else None
    oh_cpp = (Decimal(overhead) / 100) / (Decimal(good_m) / 1000) if good_m else None
    price = Decimal(product["base_price_paise"]) / 100
    margin = (price - cpp) if cpp is not None else None
    return {
        "run": run, "product": product, "lines": lines, "rm_paise": rm, "pkg_paise": pkg,
        "labour_paise": exp["labour"], "gas_power_paise": exp["gas_power"] + exp["electricity"],
        "other_paise": exp["other"], "overhead_paise": overhead, "total_paise": total,
        "good_m": good_m,
        "cost_per_packet": cpp.quantize(Q2, ROUND_HALF_UP) if cpp is not None else None,
        "cost_per_packet_exact": cpp,
        "material_cpp": mat_cpp.quantize(Q2, ROUND_HALF_UP) if mat_cpp is not None else None,
        "overhead_cpp": oh_cpp.quantize(Q2, ROUND_HALF_UP) if oh_cpp is not None else None,
        "selling_price": price,
        "margin_per_packet": margin.quantize(Q2, ROUND_HALF_UP) if margin is not None else None,
        "margin_pct": (margin * 100 / price).quantize(Q2, ROUND_HALF_UP) if margin is not None and price else None,
        "valuation_method": VALUATION_METHOD, "overhead_rule": OVERHEAD_RULE,
    }


def completed_run_costs(conn, start=None, end=None):
    out = []
    for r in prod.list_runs(conn, start, end):
        if r["status"] == "completed":
            out.append(run_cost(conn, r["id"]))
    return out


def latest_cost_per_packet(conn):
    """Latest completed run per product -> list of run_cost dicts (for the owner dashboard)."""
    rows = conn.execute("SELECT MAX(id) AS id FROM production_runs WHERE status='completed' GROUP BY product_id").fetchall()
    return [run_cost(conn, r["id"]) for r in rows]
