"""Master-data seed: all Section-5 benchmark master data plus a few invented extras.

All names, GSTINs and phone numbers are dummy values.
"""
from datetime import date

from werkzeug.security import generate_password_hash

from .db import set_setting
from .money import D, to_milli, to_paise
from .services import inventory as inv

DEFAULT_PASSWORD = "Pass@123"

USERS = [
    # username, full name, role
    ("admin", "Factory Admin", "admin"),
    ("salesman_a", "Salesman A", "salesman"),
    ("salesman_b", "Salesman B", "salesman"),
    ("production", "Production Supervisor", "production"),
    ("accountant", "Accountant / Auditor", "accountant"),
    ("owner", "Owner", "owner"),
]

SETTINGS = {
    "company_name": "Kaveri Snacks & Savouries",
    "company_address": "12, Industrial Estate, Thuvakudi, Tiruchirappalli - 620 015, Tamil Nadu",
    "company_gstin": "33ABCDE1234F1Z5",
    "company_state": "Tamil Nadu",
    "company_state_code": "33",
    "company_phone": "0431-0000000",
    "company_email": "billing@kaverisnacks.example",
    "company_bank": "Demo Bank, A/c 000000000000, IFSC DEMO0000001",
    "variance_tolerance_pct": "3",
}


def _add_item(conn, code, name, kind, unit, reorder=0):
    return conn.execute("INSERT INTO items(code,name,kind,base_unit,reorder_level_m) VALUES (?,?,?,?,?)",
                        (code, name, kind, unit, to_milli(reorder))).lastrowid


def seed_master(conn):
    """Insert master data and opening stock.  Expects an empty schema."""
    for k, v in SETTINGS.items():
        set_setting(conn, k, v)

    uid = {}
    for username, name, role in USERS:
        uid[username] = conn.execute(
            "INSERT INTO users(username,password_hash,full_name,role) VALUES (?,?,?,?)",
            (username, generate_password_hash(DEFAULT_PASSWORD), name, role)).lastrowid
    conn.execute("INSERT INTO salesmen(user_id,code,phone,route) VALUES (?,?,?,?)",
                 (uid["salesman_a"], "SM-A", "90000 00001", "Tiruchirappalli - Town"))
    conn.execute("INSERT INTO salesmen(user_id,code,phone,route) VALUES (?,?,?,?)",
                 (uid["salesman_b"], "SM-B", "90000 00002", "Bengaluru - South"))

    cats = {n: conn.execute("INSERT INTO product_categories(name) VALUES (?)", (n,)).lastrowid
            for n in ("Murukku", "Mixture", "Chips", "Sweets")}

    # ── stock items ──
    flour = _add_item(conn, "RM-FLOUR", "Flour", "RM", "kg", 50)
    oil = _add_item(conn, "RM-OIL", "Cooking oil", "RM", "L", 15)
    spice = _add_item(conn, "RM-SPICE", "Salt & spice mix", "RM", "kg", 2)
    pouch = _add_item(conn, "PK-POUCH200", "200 g pouch", "PKG", "pc", 200)
    carton = _add_item(conn, "PK-CARTON20", "Carton (20 packets)", "PKG", "pc", 20)
    fg = {
        "MUR200": _add_item(conn, "FG-MUR200", "Murukku 200 g", "FG", "packet"),
        "MIX200": _add_item(conn, "FG-MIX200", "Mixture 200 g", "FG", "packet"),
        "CHP150": _add_item(conn, "FG-CHP150", "Chips 150 g", "FG", "packet"),
        "SWT250": _add_item(conn, "FG-SWT250", "Mysore Pak 250 g", "FG", "packet"),
    }

    # ── BOM: Murukku 200 g (1 batch = 200 packets) ──
    bom = conn.execute("INSERT INTO boms(code,name,expected_output_m) VALUES (?,?,?)",
                       ("BOM-MUR200", "Murukku 200 g", to_milli(200))).lastrowid
    for item, qty, unit in ((flour, 40, "kg"), (oil, 10, "L"), (spice, "1.2", "kg"), (pouch, 200, "pc"), (carton, 10, "pc")):
        conn.execute("INSERT INTO bom_items(bom_id,item_id,qty_m,unit) VALUES (?,?,?,?)", (bom, item, to_milli(qty), unit))

    # ── products & price list ──
    plist = conn.execute("INSERT INTO price_lists(name) VALUES ('Standard')").lastrowid
    products = [
        ("MUR200", "Murukku 200 g", "Murukku", "200 g", "2106", 500, 50, bom),
        ("MIX200", "Mixture 200 g", "Mixture", "200 g", "2106", 500, 55, None),
        ("CHP150", "Chips 150 g", "Chips", "150 g", "2005", 1200, 40, None),
        ("SWT250", "Mysore Pak 250 g", "Sweets", "250 g", "1704", 500, 120, None),
    ]
    for code, name, cat, pack, hsn, bp, price, b in products:
        pid = conn.execute(
            "INSERT INTO products(code,name,category_id,pack_size,hsn,gst_rate_bp,item_id,bom_id,base_price_paise) "
            "VALUES (?,?,?,?,?,?,?,?,?)", (code, name, cats[cat], pack, hsn, bp, fg[code], b, to_paise(price))).lastrowid
        conn.execute("INSERT INTO price_list_items VALUES (?,?,?)", (plist, pid, to_paise(price)))

    # ── customers ──
    custs = [
        ("CUST-A", "Retailer A", "Retailer", "Tamil Nadu", "33", "33AABCR1111A1Z5", "Sri Retail Stores, Srirangam, Tiruchirappalli",
         "98400 11111", uid["salesman_a"], 50000),
        ("CUST-B", "Distributor B", "Distributor", "Karnataka", "29", "29AABCD2222B1Z3", "B Distributors, Jayanagar, Bengaluru",
         "98450 22222", uid["salesman_b"], 200000),
        ("CUST-C", "Retailer C (unregistered)", "Retailer", "Tamil Nadu", "33", None, "C Bakery & Snacks, Woraiyur, Tiruchirappalli",
         "98400 33333", uid["salesman_a"], 20000),
    ]
    for code, name, typ, state, scode, gstin, addr, phone, sm, limit in custs:
        conn.execute("INSERT INTO customers(code,name,type,state,state_code,gstin,address,phone,salesman_user_id,price_list_id,"
                     "credit_limit_paise) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (code, name, typ, state, scode, gstin, addr, phone, sm, plist, to_paise(limit)))

    # ── suppliers ──
    for name, gstin, phone, state in (("Sri Murugan Flour Mills", "33AAAFS1111K1Z1", "98420 44444", "Tamil Nadu"),
                                      ("Annapoorna Packaging", "33AAAFA2222P1Z2", "98420 55555", "Tamil Nadu"),
                                      ("Kaveri Edible Oils", "33AAAFK3333E1Z3", "98420 66666", "Tamil Nadu")):
        conn.execute("INSERT INTO suppliers(name,gstin,phone,state) VALUES (?,?,?,?)", (name, gstin, phone, state))

    # ── opening stock (ledger entries, dated the 1st of the current month) ──
    on = date.today().replace(day=1).isoformat()
    for item, qty, rate in ((flour, 100, 42), (oil, 30, 140), (spice, 5, 200), (pouch, 500, "2.50"), (carton, 30, 15)):
        inv.post_receipt(conn, item, "OPENING", to_milli(qty), int(to_paise(rate) * qty), "opening", None, "OPENING", on=on,
                         note="Opening balance")
    # Finished goods opening: no rate is given in the brief -> valued at standard material cost of one packet
    # from the opening rates: (40x42 + 10x140 + 1.2x200 + 200x2.50 + 10x15) / 200 = ₹19.85
    std = (D(40) * 42 + D(10) * 140 + D("1.2") * 200 + D(200) * D("2.5") + D(10) * 15) / 200   # Decimal: 19.85
    inv.post_receipt(conn, fg["MUR200"], "OPENING", to_milli(50), to_paise(std * 50), "opening", None, "OPENING",
                     on=on, note="Opening balance valued at standard material cost ₹19.85/packet")
