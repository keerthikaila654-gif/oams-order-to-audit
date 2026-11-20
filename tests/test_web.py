"""HTTP-level tests: authentication, role isolation, read-only auditor, and the benchmark driven through the UI."""
import re
import pytest


def test_login_required_and_bad_password(app):
    c = app.test_client()
    assert c.get("/admin/").status_code == 302
    tok = re.search(r'name="_csrf" value="(\w+)"', c.get("/login").get_data(True)).group(1)
    r = c.post("/login", data={"username": "admin", "password": "wrong", "_csrf": tok})
    assert r.status_code == 200 and "Invalid username or password" in r.get_data(True)


def test_csrf_enforced(app, login):
    c = login("admin")
    assert c.post("/admin/settings", data={"variance_tolerance_pct": "5"}).status_code == 400


@pytest.mark.parametrize("user,path", [("salesman_a", "/admin/"), ("salesman_a", "/p/"), ("production", "/admin/orders"),
                                       ("accountant", "/admin/"), ("owner", "/p/runs/new"), ("admin", "/s/new"), ("production", "/owner/")])
def test_role_isolation(login, user, path):
    assert login(user).get(path).status_code == 403


def test_accountant_is_read_only(login):
    c = login("accountant")
    assert c.get("/reports/sales_register").status_code == 200
    tok = c.csrf()
    assert c.post("/admin/settings", data={"_csrf": tok, "variance_tolerance_pct": "9"}).status_code == 403
    assert c.post("/s/api/orders", json={}, headers={"X-CSRF-Token": tok}).status_code == 403


def test_salesman_sees_only_own_orders(login, conn):
    a, b = login("salesman_a"), login("salesman_b")
    p = conn.execute("SELECT id FROM products WHERE code='MUR200'").fetchone()[0]
    cb = conn.execute("SELECT id FROM customers WHERE name='Distributor B'").fetchone()[0]
    r = b.post("/s/api/orders", json={"client_uuid": "u1", "customer_id": cb, "items": [{"product_id": p, "qty": "10"}]}, headers={"X-CSRF-Token": b.csrf()})
    assert r.status_code == 200 and r.get_json()["ok"]
    oid = r.get_json()["id"]
    assert a.get(f"/s/orders/{oid}").status_code == 403
    # salesman A cannot order for B's customer
    r = a.post("/s/api/orders", json={"customer_id": cb, "items": [{"product_id": p, "qty": "1"}]}, headers={"X-CSRF-Token": a.csrf()})
    assert r.status_code == 400


def test_offline_sync_is_idempotent(login, conn):
    a = login("salesman_a")
    p = conn.execute("SELECT id FROM products WHERE code='MUR200'").fetchone()[0]
    ca = conn.execute("SELECT id FROM customers WHERE name='Retailer A'").fetchone()[0]
    body = {"client_uuid": "offline-1", "customer_id": ca, "items": [{"product_id": p, "qty": "5"}]}
    r1 = a.post("/s/api/orders", json=body, headers={"X-CSRF-Token": a.csrf()}).get_json()
    r2 = a.post("/s/api/orders", json=body, headers={"X-CSRF-Token": a.csrf()}).get_json()
    assert r1["id"] == r2["id"] and r2["duplicate"]
    assert conn.execute("SELECT COUNT(*) FROM sales_orders").fetchone()[0] == 1


def test_benchmark_through_the_ui(login, conn):
    prodc, adm, sa, sb = login("production"), login("admin"), login("salesman_a"), login("salesman_b")
    ids = lambda q: conn.execute(q).fetchone()[0]
    flour, oil, spice = (ids(f"SELECT id FROM items WHERE code='{c}'") for c in ("RM-FLOUR", "RM-OIL", "RM-SPICE"))
    pouch, carton = ids("SELECT id FROM items WHERE code='PK-POUCH200'"), ids("SELECT id FROM items WHERE code='PK-CARTON20'")
    # 1 purchase
    r = prodc.post("/p/purchases/new", data={"_csrf": prodc.csrf(), "supplier_id": 1, "item_id": [flour], "qty": ["60"], "unit": ["kg"], "rate": ["45"], "batch": [""], "expiry": [""]})
    assert r.status_code == 302
    # 2 run
    r = prodc.post("/p/runs/new", data={"_csrf": prodc.csrf(), "product_id": 1, "batches": "1", f"issue_{flour}": "40", f"issue_{oil}": "10.5", f"issue_{spice}": "1.2"})
    assert r.status_code == 302; run_url = r.headers["Location"]
    # 3 actual production
    r = prodc.post(run_url + "/complete", data={"_csrf": prodc.csrf(), "good": "188", f"used_{pouch}": "188", f"dmg_{pouch}": "6", f"used_{carton}": "9", f"dmg_{carton}": "0"})
    page = prodc.get(run_url).get_data(True)
    assert "FLAGGED" in page and "-6.00%" in page and "+5.00%" in page
    # 4 salesman A order
    ca = ids("SELECT id FROM customers WHERE name='Retailer A'")
    r = sa.post("/s/api/orders", json={"client_uuid": "x", "customer_id": ca, "items": [{"product_id": 1, "qty": "150"}]}, headers={"X-CSRF-Token": sa.csrf()})
    oid = r.get_json()["id"]
    # 5 admin approve + invoice + dispatch
    assert adm.post(f"/admin/orders/{oid}/approve", data={"_csrf": adm.csrf()}).status_code == 302
    r = adm.post(f"/admin/orders/{oid}/invoice", data={"_csrf": adm.csrf()}); inv_url = r.headers["Location"]
    adm.post(inv_url + "/dispatch", data={"_csrf": adm.csrf(), "dq_1": "150"})
    page = adm.get(inv_url).get_data(True)
    assert "INV/" in page and "₹7,875.00" in page and "₹187.50" in page
    pdf = adm.get(inv_url + "/pdf"); assert pdf.status_code == 200 and pdf.data[:4] == b"%PDF"
    # 6 salesman B order -> shortage 32, approval blocked
    cb = ids("SELECT id FROM customers WHERE name='Distributor B'")
    oid_b = sb.post("/s/api/orders", json={"customer_id": cb, "items": [{"product_id": 1, "qty": "120"}]}, headers={"X-CSRF-Token": sb.csrf()}).get_json()["id"]
    page = adm.get(f"/admin/orders/{oid_b}").get_data(True)
    assert "shortage 32" in page and "IGST" in page
    adm.post(f"/admin/orders/{oid_b}/approve", data={"_csrf": adm.csrf()}, follow_redirects=True)
    assert conn.execute("SELECT status FROM sales_orders WHERE id=?", (oid_b,)).fetchone()[0] == "submitted"
    # 7 payment
    iid = int(inv_url.rsplit("/", 1)[1])
    adm.post("/admin/payments", data={"_csrf": adm.csrf(), "customer_id": ca, "invoice_id": iid, "amount": "4000", "mode": "upi"})
    assert "₹3,875.00" in adm.get("/admin/payments").get_data(True)
    # 8 overheads
    rid = run_url.rsplit("/", 1)[1]
    prodc.post(f"/p/runs/{rid}/expense", data={"_csrf": prodc.csrf(), "category": "labour", "amount": "1200"})
    prodc.post(f"/p/runs/{rid}/expense", data={"_csrf": prodc.csrf(), "category": "gas_power", "amount": "600"})
    assert "₹31.14" in prodc.get(run_url).get_data(True)
    # 9 reports + exports, and the live verification page
    for slug in ("sales_register", "gst_summary", "stock_ledger", "costing_report", "ageing"):
        for fmt, sig in (("csv", b"\xef\xbb\xbf"), ("xlsx", b"PK"), ("pdf", b"%PDF")):
            r = adm.get(f"/reports/{slug}?fmt={fmt}")
            assert r.status_code == 200 and r.data.startswith(sig), (slug, fmt)
    assert "FAIL" not in adm.get("/admin/benchmark").get_data(True)
    assert "₹31.14" in login("owner").get("/owner/").get_data(True)
