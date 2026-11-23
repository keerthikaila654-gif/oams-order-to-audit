"""Extra demo seed data to populate all modules while strictly keeping Section-6 benchmark checks 100% passing."""
from datetime import date
from decimal import Decimal
from app.db import connect
from app import benchmark
from app.services import sales, inventory as inv
from app.money import to_milli, to_paise

def seed_extra(conn):
    sm_a = conn.execute("SELECT * FROM users WHERE username='salesman_a'").fetchone()
    admin = conn.execute("SELECT * FROM users WHERE username='admin'").fetchone()
    cust_c = conn.execute("SELECT id FROM customers WHERE name LIKE '%Retailer C%'").fetchone()[0]
    
    # Products
    mix_prod = conn.execute("SELECT id, item_id FROM products WHERE code='MIX200'").fetchone()
    chips_prod = conn.execute("SELECT id, item_id FROM products WHERE code='CHP150'").fetchone()
    swt_prod = conn.execute("SELECT id, item_id FROM products WHERE code='SWT250'").fetchone()

    # Check if extra demo orders already exist
    existing = conn.execute("SELECT COUNT(*) FROM sales_orders WHERE client_uuid LIKE 'demo-extra-%'").fetchone()[0]
    if existing > 0:
        print("Extra demo data already seeded.")
        return

    on = date.today().replace(day=1).isoformat()
    # Provide stock for Mixture, Chips, Mysore Pak
    # Mixture 200g: 50 packets @ ₹25
    inv.post_receipt(conn, mix_prod["item_id"], "OPENING", to_milli(50), to_paise(25 * 50), "opening", None, "OPENING",
                     on=on, note="Opening balance 50 packets")
    # Chips 150g: 40 packets @ ₹18
    inv.post_receipt(conn, chips_prod["item_id"], "OPENING", to_milli(40), to_paise(18 * 40), "opening", None, "OPENING",
                     on=on, note="Opening balance 40 packets")
    # Mysore Pak 250g: 30 packets @ ₹60
    inv.post_receipt(conn, swt_prod["item_id"], "OPENING", to_milli(30), to_paise(60 * 30), "opening", None, "OPENING",
                     on=on, note="Opening balance 30 packets")

    # 1. Retailer C - Order 1: Mixture 200g (15 packets) -> Invoiced, Dispatched, Partial Cash payment
    oid_1, _ = sales.create_order(conn, sm_a, cust_c, [(mix_prod["id"], to_milli(15))], client_uuid='demo-extra-1')
    sales.approve_order(conn, admin, oid_1)
    inv_1 = sales.generate_invoice(conn, admin, oid_1)
    sales.dispatch_invoice(conn, admin, inv_1, vehicle='TN 45 CD 5678')
    sales.record_payment(conn, admin, cust_c, to_paise(500), invoice_id=inv_1, mode='cash', reference='CASH-RC-01')

    # 2. Retailer C - Order 2: Chips 150g (10 packets) -> Approved & Invoiced (Awaiting dispatch)
    oid_2, _ = sales.create_order(conn, sm_a, cust_c, [(chips_prod["id"], to_milli(10))], client_uuid='demo-extra-2')
    sales.approve_order(conn, admin, oid_2)
    inv_2 = sales.generate_invoice(conn, admin, oid_2)

    # 3. Retailer C - Order 3: Mysore Pak 250g (5 packets) -> Submitted (Pending admin verification)
    oid_3, _ = sales.create_order(conn, sm_a, cust_c, [(swt_prod["id"], to_milli(5))], client_uuid='demo-extra-3')

    print("Extra demo stock, orders, invoice, dispatch, and payment seeded successfully!")

if __name__ == "__main__":
    conn = connect("data/oams.db")
    seed_extra(conn)
    print("\nRunning benchmark verification:")
    bad = 0
    for c in benchmark.verify(conn):
        print(f"  [{c['status']:^7}] {c['check']:<60} expected {c['expected']:<18} actual {c['actual']}")
        bad += (c["status"] != "pass")
    print(f"\nVerification finished with {bad} failures.")
