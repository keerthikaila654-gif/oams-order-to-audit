# Order-to-Audit Management System

One integrated system for a small South-Indian snacks manufacturer: a salesman's order flows through admin verification, GST invoice, dispatch, finished-goods stock, receivables and payment — and a recipe-based production workflow (purchase → BOM issue → production run → yield/variance → cost per packet) feeds the same finished-goods ledger. Monthly sales, GST, stock, production, collection and ageing reports come straight from those records. *(Internship problem: Centre for Digital Infrastructure, NIT Tiruchirappalli. All data is dummy.)*

## Objectives
Enter every transaction once at source; keep ledger-based stock; compare planned vs actual consumption and yield; compute cost per packet; produce auditor-ready GST/stock/receivable reports — without depending on Tally.

## Technology stack & why
**Python 3.13 · Flask · SQLite (WAL) · Jinja2 · vanilla JS PWA · ReportLab (PDF) · openpyxl (Excel) · pytest.**
A single small Flask app with one SQLite file keeps the whole system easy to run (one command), explain and modify in an interview; SQLite gives real ACID transactions, triggers and window functions for the immutable stock ledger. The salesman screen is a mobile-first PWA (service worker + localStorage queue) so orders work offline.

## Run it (single command)
```bash
./run.sh                 # installs requirements, creates + seeds the DB, serves http://localhost:5000
# or:  docker compose up --build
```
Manual: `pip install -r requirements.txt && python manage.py run`.

| Command | What it does |
|---|---|
| `python manage.py reset` | fresh DB with benchmark master data + opening stock (nothing transacted) |
| `python manage.py benchmark [--export]` | reset, run all 9 benchmark steps, verify results (`--export` writes every report as CSV/XLSX/PDF to `./exports`) |
| `python manage.py verify` | re-check expected results against the current DB |
| `python -m pytest` | 31 tests (tax, units, ledger, benchmark, roles, UI-driven benchmark) |

**Environment:** `OAMS_DB` (database path, default `data/oams.db`), `OAMS_SECRET_KEY` (session key; auto-generated for dev), `PORT`. **Database setup/seed:** automatic on first start; `python manage.py reset` rebuilds it (`app/seed.py`, schema in `app/db.py`).

## Test logins (password for all: `Pass@123`)
| Role | Username | Device / home |
|---|---|---|
| Admin (factory desk) | `admin` | desktop – orders, invoices, dispatch, payments, masters, users, settings, audit log |
| Salesman A (Retailer A, C) | `salesman_a` | mobile – new order, my orders, customer-wise sales |
| Salesman B (Distributor B) | `salesman_b` | mobile |
| Production supervisor | `production` | desktop/tablet – purchases, BOM, runs, variance, overheads, adjustments |
| Accountant / auditor | `accountant` | **read-only** – all reports + exports |
| Owner | `owner` | any – live dashboard |

## Benchmark — how to execute (UI)
1. `production` → **Purchases → Record purchase**: Flour 60 kg @ ₹45.
2. **New run**: Murukku 200 g × 1 batch; issue flour 40, oil **10.5**, spice 1.2 → **Create run**.
3. On the run page, **Record actual production**: good 188, pouches used 188 + damaged 6, cartons 9.
4. `salesman_a` (phone view): Retailer A, Murukku 200 g, 150 → Submit.
5. `admin` → Orders → open → Approve → Generate GST invoice → Record dispatch (CGST + SGST).
6. `salesman_b`: Distributor B, 120 packets → admin order page shows **shortage 32**, Approve is disabled/refused, GST preview says **IGST**.
7. Admin → Payments: Retailer A ₹4,000.
8. `production` → the run → Add overheads: labour ₹1,200, gas & power ₹600 → cost per packet shown with the valuation/overhead rules.
9. `accountant` → Reports (all 17 + GSTR-1/3B mapping; CSV/Excel/PDF).

Admin → **Benchmark check** recomputes every expected result live from the database. `python manage.py benchmark` runs the same nine steps automatically.

## Benchmark results (from the database)
| Check | Expected | Result |
|---|---|---|
| Flour closing | 120 kg | **120 kg** ✔ |
| Oil closing | 19.5 L | **19.5 L** ✔ |
| Pouches closing | 306 | **306** ✔ |
| Finished goods after production / after dispatch | 238 / 88 | **238 / 88** ✔ |
| Yield variance | 188 vs 200, −6 %, flagged | **−6.00 %, flagged** ✔ |
| Oil variance | +5 %, flagged | **+5.00 %, flagged** ✔ |
| Retailer A taxable / CGST / SGST / total | ₹7,500 / 187.50 / 187.50 / 7,875 | **identical** ✔ |
| Retailer A outstanding | ₹3,875 | **₹3,875.00** ✔ |
| Distributor B shortage / tax if invoiced | 32 / IGST | **32 / IGST** ✔ (approval blocked) |
| Cost per packet | system-calculated | **₹31.14** |

**Cost per packet:** flour 40 kg × ₹43.125 = 1,725.00; oil 10.5 L × 140 = 1,470.00; spice 1.2 × 200 = 240.00; pouches 194 × 2.50 = 485.00; cartons 9 × 15 = 135.00 → materials+packaging ₹4,055.00; + labour 1,200 + gas/power 600 = **₹5,855.00 ÷ 188 good packets = ₹31.14** (selling ₹50 → margin 37.71 %).

## Methods
* **Stock valuation:** perpetual **weighted-average cost** (flour pool after purchase = ₹6,900/160 kg = ₹43.125/kg; the brief's "₹42 opening / ₹45 new" rates are honoured as the pool inputs).
* **Costing:** run cost = RM + packaging (good + damaged) + overheads; cost/packet = total ÷ **good** packets, so losses are visible in the cost.
* **Overhead allocation:** direct — overheads are entered against a run and charged 100 % to it.

## Features by tier
* **Must have – done:** 5 roles with RBAC & salted-hash passwords; product/customer/raw-material/BOM masters; mobile order entry; admin approval with stock check and editable qty/price; GST invoice PDF (FY numbering, CGST+SGST / IGST by place of supply); production runs; automatic RM/packaging/FG ledger updates; planned-vs-actual consumption and yield with configurable tolerance; stock summary + ledger; sales register; CSV/Excel/PDF export.
* **Should have – done:** purchases & suppliers; payments, outstanding, customer ageing; cost per packet; overhead allocation; GST summary (GSTR-1/3B mapping); configurable variance tolerance; owner dashboard; WhatsApp/email share links; partial dispatch; cancellation with credit notes + stock return; stock adjustments; unit conversion; append-only audit log; low-stock alerts & reorder levels.
* **Stretch – done:** offline order capture with automatic, idempotent sync (service worker + queue). **Not done:** batch/expiry tracking beyond storing batch/expiry on purchase lines, FEFO, production planning, GPS/visit logs, Tally export, e-invoice/e-way bill, Tamil UI.

## Assumptions
* Opening FG (50 packets) has no rate in the brief → valued at standard material cost ₹19.85/packet (BOM at opening rates). It only affects FG valuation, not any benchmark figure.
* One pouch per packet; cartons are consumed in whole pieces (188 packets ÷ 20 = 9.4 → 9 cartons; 8 loose packets stay in FG stock as packets).
* Salt & spice mix is one item ("spice mix" in opening stock). Stock is reserved for approved/invoiced-but-undispatched orders; stock reduces at **dispatch**, not at invoicing.
* Dates use the server's current date; opening balances are dated the 1st of the current month. Salesmen see only their assigned customers.
* Seller GSTIN, bank and addresses are dummy values editable in Settings. HSN 2106 (namkeen) at 5 %.

## Known limitations
SQLite single-writer (fine for 15 salesmen); login throttle is in-memory (single worker); no input-tax-credit / purchase GST breakup; no e-invoice IRN; credit-note covers full-invoice cancellation only (no partial returns); FG stock value excludes overheads; batch/expiry not enforced at issue; PDF uses "Rs." because the base font lacks ₹.

## Differences from the expected benchmark figures (explained)
All Section-6 figures match exactly. Two *additional* variance lines appear on the run, both derived from the benchmark inputs and shown honestly rather than hidden:
* **Cartons: 9 used vs 10 BOM = −10 %, flagged.** The BOM assumes 200 packets per batch; with 188 packets, 9.4 cartons are needed, so 9 is correct for the yield achieved. (The run page's suggested usage is scaled by yield.)
* **Pouches: 194 consumed (188 + 6 damaged) vs 200 = −3 %,** exactly at tolerance, so **not** flagged (flag requires |variance| > tolerance).
Treatment of the 9 cartons: consumed via `PRODUCTION_CONSUME` (closing 21 of 30); damaged pouches are a separate `WASTAGE` ledger line + wastage record.

## Code map
`app/db.py` schema & triggers · `app/money.py`, `app/units.py` · `app/services/` (inventory ledger, production, costing, sales/GST, purchasing, reports, exports, PDF, audit) · `app/web/` (routes per role) · `app/templates`, `app/static` (UI, PWA) · `app/benchmark.py` · `tests/` · `docs/DESIGN.md` (design note + ER diagram).
