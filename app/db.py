"""SQLite connection handling, transactions and the full schema."""
import contextlib
import sqlite3
import uuid

SCHEMA = """
PRAGMA foreign_keys = ON;

-- ───────────────────────── identity & configuration ─────────────────────────
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    full_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('salesman','admin','production','accountant','owner')),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE salesmen (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
    code TEXT NOT NULL UNIQUE,
    phone TEXT,
    route TEXT
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE doc_sequences (name TEXT PRIMARY KEY, last INTEGER NOT NULL DEFAULT 0);
CREATE TABLE invoice_sequences (fy TEXT PRIMARY KEY, last INTEGER NOT NULL DEFAULT 0);

-- ───────────────────────── master data ─────────────────────────
CREATE TABLE product_categories (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);

-- One row per stockable thing: raw material (RM), packaging (PKG) or finished good (FG).
-- Quantities for an item are held in thousandths of base_unit.
CREATE TABLE items (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('RM','PKG','FG')),
    base_unit TEXT NOT NULL,
    reorder_level_m INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE suppliers (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, gstin TEXT, phone TEXT, state TEXT, address TEXT
);

CREATE TABLE boms (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    expected_output_m INTEGER NOT NULL CHECK (expected_output_m > 0),  -- packets per ONE batch (thousandths)
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE bom_items (
    id INTEGER PRIMARY KEY,
    bom_id INTEGER NOT NULL REFERENCES boms(id),
    item_id INTEGER NOT NULL REFERENCES items(id),
    qty_m INTEGER NOT NULL CHECK (qty_m > 0),   -- per batch, expressed in `unit`
    unit TEXT NOT NULL,                         -- may differ from items.base_unit (converted on use)
    UNIQUE (bom_id, item_id)
);

CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    category_id INTEGER REFERENCES product_categories(id),
    pack_size TEXT,
    hsn TEXT NOT NULL,
    gst_rate_bp INTEGER NOT NULL,                 -- 500 = 5.00 %
    item_id INTEGER NOT NULL UNIQUE REFERENCES items(id),   -- finished-goods stock item
    bom_id INTEGER REFERENCES boms(id),           -- several products (pack sizes) may share a BOM
    base_price_paise INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE price_lists (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE price_list_items (
    price_list_id INTEGER NOT NULL REFERENCES price_lists(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    price_paise INTEGER NOT NULL,
    PRIMARY KEY (price_list_id, product_id)
);
CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('Retailer','Distributor')),
    state TEXT NOT NULL,
    state_code TEXT NOT NULL,
    gstin TEXT,
    address TEXT,
    phone TEXT,
    salesman_user_id INTEGER REFERENCES users(id),
    price_list_id INTEGER REFERENCES price_lists(id),
    credit_limit_paise INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE customer_prices (   -- optional customer-specific price override
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    price_paise INTEGER NOT NULL,
    PRIMARY KEY (customer_id, product_id)
);

-- ───────────────────────── inventory ledger ─────────────────────────
-- Single source of truth for stock.  Append-only (triggers below).
-- qty_in_m / qty_out_m in thousandths of the item base unit; value_paise is signed
-- (+ for receipts, - for issues, valued at weighted-average cost at the time).
CREATE TABLE inventory_movements (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id),
    moved_on TEXT NOT NULL,
    txn_type TEXT NOT NULL CHECK (txn_type IN
        ('OPENING','PURCHASE','PRODUCTION_ISSUE','PRODUCTION_CONSUME','PRODUCTION_RECEIPT',
         'WASTAGE','DISPATCH','RETURN','ADJUSTMENT')),
    qty_in_m INTEGER NOT NULL DEFAULT 0 CHECK (qty_in_m >= 0),
    qty_out_m INTEGER NOT NULL DEFAULT 0 CHECK (qty_out_m >= 0),
    value_paise INTEGER NOT NULL DEFAULT 0,
    src_type TEXT NOT NULL,            -- purchase | production_run | dispatch | invoice | adjustment | opening
    src_id INTEGER,
    ref_no TEXT NOT NULL,
    batch_no TEXT,
    expiry_date TEXT,
    note TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (qty_in_m = 0 OR qty_out_m = 0)
);
CREATE INDEX ix_mov_item ON inventory_movements(item_id, moved_on, id);
CREATE INDEX ix_mov_src ON inventory_movements(src_type, src_id);

-- Stock balances are *derived* from the ledger; they cannot drift or be overwritten.
CREATE VIEW stock_balances AS
SELECT i.id AS item_id, i.code, i.name, i.kind, i.base_unit, i.reorder_level_m,
       COALESCE(SUM(m.qty_in_m - m.qty_out_m), 0) AS qty_m,
       COALESCE(SUM(m.value_paise), 0) AS value_paise
FROM items i LEFT JOIN inventory_movements m ON m.item_id = i.id
GROUP BY i.id;

CREATE TRIGGER mv_no_update BEFORE UPDATE ON inventory_movements
BEGIN SELECT RAISE(ABORT, 'Inventory movements are immutable; post a reversing entry'); END;
CREATE TRIGGER mv_no_delete BEFORE DELETE ON inventory_movements
BEGIN SELECT RAISE(ABORT, 'Inventory movements cannot be deleted'); END;

-- ───────────────────────── purchasing ─────────────────────────
CREATE TABLE purchases (
    id INTEGER PRIMARY KEY,
    purchase_no TEXT NOT NULL UNIQUE,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    supplier_invoice_no TEXT,
    purchase_date TEXT NOT NULL,
    total_paise INTEGER NOT NULL,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE purchase_items (
    id INTEGER PRIMARY KEY,
    purchase_id INTEGER NOT NULL REFERENCES purchases(id),
    item_id INTEGER NOT NULL REFERENCES items(id),
    entered_qty_m INTEGER NOT NULL,
    entered_unit TEXT NOT NULL,
    rate_per_entered_unit TEXT NOT NULL,      -- decimal rupees, kept as text
    qty_m INTEGER NOT NULL,                   -- converted to item base unit
    amount_paise INTEGER NOT NULL,
    batch_no TEXT,
    expiry_date TEXT
);

-- ───────────────────────── production ─────────────────────────
CREATE TABLE production_runs (
    id INTEGER PRIMARY KEY,
    run_no TEXT NOT NULL UNIQUE,
    product_id INTEGER NOT NULL REFERENCES products(id),
    bom_id INTEGER NOT NULL REFERENCES boms(id),
    batches_m INTEGER NOT NULL CHECK (batches_m > 0),     -- number of batches, thousandths
    expected_output_m INTEGER NOT NULL,                   -- packets, snapshot of BOM x batches
    actual_output_m INTEGER,
    status TEXT NOT NULL DEFAULT 'in_progress' CHECK (status IN ('in_progress','completed')),
    run_date TEXT NOT NULL,
    supervisor_id INTEGER REFERENCES users(id),
    completed_at TEXT,
    notes TEXT,
    tolerance_pct TEXT NOT NULL DEFAULT '3',              -- tolerance snapshot used for flags
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE production_consumption (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES production_runs(id),
    item_id INTEGER NOT NULL REFERENCES items(id),
    planned_m INTEGER NOT NULL,       -- BOM x batches, in item base unit
    actual_m INTEGER NOT NULL DEFAULT 0,   -- includes damaged packaging
    UNIQUE (run_id, item_id)
);
CREATE TABLE production_output (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES production_runs(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    good_qty_m INTEGER NOT NULL,
    material_cost_paise INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE wastage (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES production_runs(id),
    kind TEXT NOT NULL CHECK (kind IN ('DAMAGED_PACKAGING','SCRAP','YIELD_LOSS')),
    item_id INTEGER REFERENCES items(id),
    qty_m INTEGER NOT NULL,
    unit TEXT NOT NULL,
    value_paise INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE expenses (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES production_runs(id),
    category TEXT NOT NULL CHECK (category IN ('labour','gas_power','electricity','other')),
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    expense_date TEXT NOT NULL,
    description TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ───────────────────────── sales ─────────────────────────
CREATE TABLE sales_orders (
    id INTEGER PRIMARY KEY,
    order_no TEXT NOT NULL UNIQUE,
    client_uuid TEXT UNIQUE,                 -- idempotency key for offline sync
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    salesman_user_id INTEGER NOT NULL REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'submitted'
        CHECK (status IN ('submitted','approved','invoiced','dispatched','rejected','cancelled')),
    order_date TEXT NOT NULL,
    notes TEXT,
    reject_reason TEXT,
    approved_by INTEGER REFERENCES users(id),
    approved_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE sales_order_items (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES sales_orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    qty_m INTEGER NOT NULL CHECK (qty_m > 0),
    price_paise INTEGER NOT NULL CHECK (price_paise >= 0),
    UNIQUE (order_id, product_id)
);
CREATE TABLE invoices (
    id INTEGER PRIMARY KEY,
    invoice_no TEXT NOT NULL UNIQUE,
    fy TEXT NOT NULL,
    order_id INTEGER REFERENCES sales_orders(id),
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    invoice_date TEXT NOT NULL,
    customer_name TEXT NOT NULL,
    customer_gstin TEXT,
    customer_address TEXT,
    place_of_supply TEXT NOT NULL,
    place_of_supply_code TEXT NOT NULL,
    tax_type TEXT NOT NULL CHECK (tax_type IN ('INTRA','INTER')),
    taxable_paise INTEGER NOT NULL,
    cgst_paise INTEGER NOT NULL,
    sgst_paise INTEGER NOT NULL,
    igst_paise INTEGER NOT NULL,
    total_paise INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'posted' CHECK (status IN ('posted','cancelled')),
    cancel_reason TEXT,
    cancelled_at TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE invoice_items (
    id INTEGER PRIMARY KEY,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    description TEXT NOT NULL,
    hsn TEXT NOT NULL,
    qty_m INTEGER NOT NULL,
    price_paise INTEGER NOT NULL,
    taxable_paise INTEGER NOT NULL,
    gst_rate_bp INTEGER NOT NULL,
    cgst_paise INTEGER NOT NULL,
    sgst_paise INTEGER NOT NULL,
    igst_paise INTEGER NOT NULL,
    total_paise INTEGER NOT NULL
);
CREATE TABLE credit_notes (
    id INTEGER PRIMARY KEY,
    cn_no TEXT NOT NULL UNIQUE,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    cn_date TEXT NOT NULL,
    taxable_paise INTEGER NOT NULL,
    cgst_paise INTEGER NOT NULL,
    sgst_paise INTEGER NOT NULL,
    igst_paise INTEGER NOT NULL,
    total_paise INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE dispatches (
    id INTEGER PRIMARY KEY,
    dispatch_no TEXT NOT NULL UNIQUE,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    dispatch_date TEXT NOT NULL,
    vehicle TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE dispatch_items (
    id INTEGER PRIMARY KEY,
    dispatch_id INTEGER NOT NULL REFERENCES dispatches(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    qty_m INTEGER NOT NULL CHECK (qty_m > 0)
);
CREATE TABLE payments (
    id INTEGER PRIMARY KEY,
    receipt_no TEXT NOT NULL UNIQUE,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    invoice_id INTEGER REFERENCES invoices(id),
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    mode TEXT NOT NULL DEFAULT 'cash',
    payment_date TEXT NOT NULL,
    reference TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Posted invoices are legal documents: never deleted, amounts never edited.
CREATE TRIGGER inv_no_delete BEFORE DELETE ON invoices
BEGIN SELECT RAISE(ABORT, 'Posted invoices cannot be deleted; cancel with a credit note'); END;
CREATE TRIGGER invitem_no_delete BEFORE DELETE ON invoice_items
BEGIN SELECT RAISE(ABORT, 'Invoice lines cannot be deleted'); END;
CREATE TRIGGER inv_no_amount_edit BEFORE UPDATE OF invoice_no, customer_id, invoice_date,
    taxable_paise, cgst_paise, sgst_paise, igst_paise, total_paise, tax_type ON invoices
BEGIN SELECT RAISE(ABORT, 'Invoice amounts are immutable; cancel and re-issue'); END;
CREATE TRIGGER invitem_no_update BEFORE UPDATE ON invoice_items
BEGIN SELECT RAISE(ABORT, 'Invoice lines are immutable'); END;

-- ───────────────────────── audit trail ─────────────────────────
CREATE TABLE audit_logs (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    user_id INTEGER,
    username TEXT,
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_id INTEGER,
    ref TEXT,
    old_json TEXT,
    new_json TEXT
);
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_logs
BEGIN SELECT RAISE(ABORT, 'Audit log is append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_logs
BEGIN SELECT RAISE(ABORT, 'Audit log is append-only'); END;
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextlib.contextmanager
def tx(conn: sqlite3.Connection):
    """Atomic block. Nested use becomes a SAVEPOINT, so services can call each other."""
    if conn.in_transaction:
        name = "sp_" + uuid.uuid4().hex[:8]
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        else:
            conn.execute(f"RELEASE {name}")
    else:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


def next_seq(conn, name: str) -> int:
    conn.execute("INSERT OR IGNORE INTO doc_sequences(name,last) VALUES (?,0)", (name,))
    conn.execute("UPDATE doc_sequences SET last = last + 1 WHERE name = ?", (name,))
    return conn.execute("SELECT last FROM doc_sequences WHERE name = ?", (name,)).fetchone()[0]


def get_setting(conn, key, default=None):
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
