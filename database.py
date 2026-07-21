"""
database.py
-----------
SQLite katmanı: siparişler, sipariş satırları, Trendyol Finans API'sinden
gelen settlements / otherfinancials kayıtları, kargo faturası kalemleri,
ürün maliyetleri ve senkronizasyon ilerleme durumu.

Tüm upsert fonksiyonları INSERT OR REPLACE mantığıyla çalışır — aynı ID
tekrar geldiğinde (örn. senkronizasyon tekrar çalıştırıldığında) veriyi
günceller, çift satır oluşturmaz.
"""

import sqlite3
from contextlib import contextmanager

DB_PATH = "trendyol_data.db"


@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_connection() as conn:
        c = conn.cursor()

        # --- Siparişler (getShipmentPackages'tan) ---
        c.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            shipment_package_id INTEGER PRIMARY KEY,
            order_number TEXT,
            order_date INTEGER,
            status TEXT,
            customer TEXT,
            cargo_provider TEXT,
            gross_amount REAL,
            discount_amount REAL,
            net_amount REAL,
            updated_at TEXT DEFAULT (datetime('now'))
        )
        """)

        # --- Sipariş satırları ---
        # barcode: Trendyol'un ürün barkodu (EAN) -> settlements'taki "barcode" ile eşleşir
        # merchant_sku: satıcının kendi SKU'su -> Ürün Maliyet Excel'indeki "SKU" ile eşleşir
        c.execute("""
        CREATE TABLE IF NOT EXISTS order_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shipment_package_id INTEGER,
            barcode TEXT,
            merchant_sku TEXT,
            product_name TEXT,
            quantity INTEGER,
            line_unit_price REAL,
            commission_rate REAL,
            FOREIGN KEY (shipment_package_id) REFERENCES orders(shipment_package_id),
            UNIQUE (shipment_package_id, barcode)
        )
        """)

        # --- Finans API: settlements (Sale, Return, vb.) ---
        # transaction_type: BİZİM istediğimiz kanonik tip (örn. "Sale") -- filtrelemede kullanılır
        # raw_transaction_type: API'nin döndürdüğü Türkçeleştirilmiş orijinal metin (örn. "Satış") -- sadece görüntüleme/teşhis amaçlı
        c.execute("""
        CREATE TABLE IF NOT EXISTS settlements (
            id TEXT PRIMARY KEY,
            transaction_date INTEGER,
            barcode TEXT,
            transaction_type TEXT,
            raw_transaction_type TEXT,
            receipt_id TEXT,
            description TEXT,
            debt REAL,
            credit REAL,
            payment_period INTEGER,
            commission_rate REAL,
            commission_amount REAL,
            seller_revenue REAL,
            order_number TEXT,
            payment_order_id INTEGER,
            payment_date INTEGER,
            shipment_package_id INTEGER
        )
        """)

        # --- Finans API: otherfinancials (Stoppage, DeductionInvoices, PaymentOrder, CashAdvance, vb.) ---
        c.execute("""
        CREATE TABLE IF NOT EXISTS other_financials (
            id TEXT PRIMARY KEY,
            transaction_date INTEGER,
            barcode TEXT,
            transaction_type TEXT,
            raw_transaction_type TEXT,
            transaction_sub_type TEXT,
            receipt_id TEXT,
            description TEXT,
            debt REAL,
            credit REAL,
            order_number TEXT,
            payment_order_id INTEGER,
            payment_date INTEGER,
            shipment_package_id INTEGER
        )
        """)

        # --- Kargo faturası kalemleri (cargo-invoice/{invoiceSerialNumber}/items) ---
        # NOT: Bu servisin tam alan şeması Trendyol dokümantasyonunda örnek JSON ile
        # gösterilmiyor. raw_json ham yanıtı saklar; amount/shipment_package_id/order_number
        # alanları olası isimlerden savunmacı şekilde çıkarılır (bkz. trendyol_finance.py).
        c.execute("""
        CREATE TABLE IF NOT EXISTS cargo_costs (
            id TEXT PRIMARY KEY,
            invoice_serial_number TEXT,
            shipment_package_id INTEGER,
            order_number TEXT,
            barcode TEXT,
            amount REAL,
            raw_json TEXT
        )
        """)

        # --- Ürün maliyetleri (Excel'den) ---
        c.execute("""
        CREATE TABLE IF NOT EXISTS product_costs (
            sku TEXT PRIMARY KEY,
            product_name TEXT,
            sale_price_incl_vat REAL,
            cost_incl_vat REAL,
            sale_price_excl_vat REAL,
            cost_excl_vat REAL,
            updated_at TEXT DEFAULT (datetime('now'))
        )
        """)

        # --- Senkronizasyon durumu (basit key-value) ---
        c.execute("""
        CREATE TABLE IF NOT EXISTS sync_state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """)

        # --- Senkronizasyon ilerlemesi (arka plan iş takibi, dashboard bunu polling ile okur) ---
        c.execute("""
        CREATE TABLE IF NOT EXISTS sync_progress (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            status TEXT,
            current_step INTEGER,
            total_steps INTEGER,
            message TEXT,
            error TEXT,
            started_at TEXT,
            updated_at TEXT
        )
        """)

        conn.commit()


def upsert_orders(rows):
    """rows: dict listesi, her biri orders şemasındaki alan adlarıyla eşleşmeli."""
    if not rows:
        return
    with get_connection() as conn:
        conn.executemany("""
            INSERT INTO orders (shipment_package_id, order_number, order_date, status,
                                 customer, cargo_provider, gross_amount, discount_amount, net_amount)
            VALUES (:shipment_package_id, :order_number, :order_date, :status,
                    :customer, :cargo_provider, :gross_amount, :discount_amount, :net_amount)
            ON CONFLICT(shipment_package_id) DO UPDATE SET
                order_number=excluded.order_number,
                order_date=excluded.order_date,
                status=excluded.status,
                customer=excluded.customer,
                cargo_provider=excluded.cargo_provider,
                gross_amount=excluded.gross_amount,
                discount_amount=excluded.discount_amount,
                net_amount=excluded.net_amount,
                updated_at=datetime('now')
        """, rows)


def upsert_order_lines(rows):
    if not rows:
        return
    with get_connection() as conn:
        conn.executemany("""
            INSERT INTO order_lines (shipment_package_id, barcode, merchant_sku, product_name,
                                      quantity, line_unit_price, commission_rate)
            VALUES (:shipment_package_id, :barcode, :merchant_sku, :product_name,
                    :quantity, :line_unit_price, :commission_rate)
            ON CONFLICT(shipment_package_id, barcode) DO UPDATE SET
                merchant_sku=excluded.merchant_sku,
                product_name=excluded.product_name,
                quantity=excluded.quantity,
                line_unit_price=excluded.line_unit_price,
                commission_rate=excluded.commission_rate
        """, rows)


def upsert_settlements(rows):
    if not rows:
        return
    with get_connection() as conn:
        conn.executemany("""
            INSERT INTO settlements (id, transaction_date, barcode, transaction_type, raw_transaction_type,
                                      receipt_id, description, debt, credit, payment_period, commission_rate,
                                      commission_amount, seller_revenue, order_number,
                                      payment_order_id, payment_date, shipment_package_id)
            VALUES (:id, :transaction_date, :barcode, :transaction_type, :raw_transaction_type,
                    :receipt_id, :description, :debt, :credit, :payment_period, :commission_rate,
                    :commission_amount, :seller_revenue, :order_number,
                    :payment_order_id, :payment_date, :shipment_package_id)
            ON CONFLICT(id) DO UPDATE SET
                transaction_type=excluded.transaction_type,
                raw_transaction_type=excluded.raw_transaction_type,
                debt=excluded.debt, credit=excluded.credit,
                seller_revenue=excluded.seller_revenue, commission_amount=excluded.commission_amount,
                payment_order_id=excluded.payment_order_id, payment_date=excluded.payment_date
        """, rows)


def upsert_other_financials(rows):
    if not rows:
        return
    with get_connection() as conn:
        conn.executemany("""
            INSERT INTO other_financials (id, transaction_date, barcode, transaction_type, raw_transaction_type,
                                           transaction_sub_type, receipt_id, description, debt, credit,
                                           order_number, payment_order_id, payment_date, shipment_package_id)
            VALUES (:id, :transaction_date, :barcode, :transaction_type, :raw_transaction_type,
                    :transaction_sub_type, :receipt_id, :description, :debt, :credit,
                    :order_number, :payment_order_id, :payment_date, :shipment_package_id)
            ON CONFLICT(id) DO UPDATE SET
                transaction_type=excluded.transaction_type,
                raw_transaction_type=excluded.raw_transaction_type,
                debt=excluded.debt, credit=excluded.credit,
                payment_order_id=excluded.payment_order_id, payment_date=excluded.payment_date
        """, rows)


def upsert_cargo_costs(rows):
    if not rows:
        return
    with get_connection() as conn:
        conn.executemany("""
            INSERT INTO cargo_costs (id, invoice_serial_number, shipment_package_id,
                                      order_number, barcode, amount, raw_json)
            VALUES (:id, :invoice_serial_number, :shipment_package_id,
                    :order_number, :barcode, :amount, :raw_json)
            ON CONFLICT(id) DO UPDATE SET
                amount=excluded.amount,
                shipment_package_id=excluded.shipment_package_id,
                order_number=excluded.order_number,
                barcode=excluded.barcode,
                raw_json=excluded.raw_json
        """, rows)


def upsert_product_costs(rows):
    if not rows:
        return
    with get_connection() as conn:
        conn.executemany("""
            INSERT INTO product_costs (sku, product_name, sale_price_incl_vat, cost_incl_vat,
                                        sale_price_excl_vat, cost_excl_vat)
            VALUES (:sku, :product_name, :sale_price_incl_vat, :cost_incl_vat,
                    :sale_price_excl_vat, :cost_excl_vat)
            ON CONFLICT(sku) DO UPDATE SET
                product_name=excluded.product_name,
                sale_price_incl_vat=excluded.sale_price_incl_vat,
                cost_incl_vat=excluded.cost_incl_vat,
                sale_price_excl_vat=excluded.sale_price_excl_vat,
                cost_excl_vat=excluded.cost_excl_vat,
                updated_at=datetime('now')
        """, rows)


def get_sync_state(key):
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_sync_state(key, value):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO sync_state (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, value))


# --- Senkronizasyon ilerlemesi (arka plan thread'i yazar, /api/sync-status okur) ---

def start_sync_progress(total_steps, message="Başlatılıyor…"):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO sync_progress (id, status, current_step, total_steps, message, error, started_at, updated_at)
            VALUES (1, 'running', 0, ?, ?, NULL, datetime('now'), datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                status='running', current_step=0, total_steps=excluded.total_steps,
                message=excluded.message, error=NULL,
                started_at=datetime('now'), updated_at=datetime('now')
        """, (total_steps, message))


def update_sync_progress(current_step=None, total_steps=None, message=None):
    with get_connection() as conn:
        row = conn.execute("SELECT current_step, total_steps, message FROM sync_progress WHERE id = 1").fetchone()
        if row is None:
            return
        cur = current_step if current_step is not None else row["current_step"]
        tot = total_steps if total_steps is not None else row["total_steps"]
        msg = message if message is not None else row["message"]
        conn.execute("""
            UPDATE sync_progress SET current_step = ?, total_steps = ?, message = ?, updated_at = datetime('now')
            WHERE id = 1
        """, (cur, tot, msg))


def finish_sync_progress(message="Tamamlandı"):
    with get_connection() as conn:
        conn.execute("""
            UPDATE sync_progress SET status = 'done', message = ?, updated_at = datetime('now') WHERE id = 1
        """, (message,))


def fail_sync_progress(error_message):
    with get_connection() as conn:
        conn.execute("""
            UPDATE sync_progress SET status = 'error', error = ?, updated_at = datetime('now') WHERE id = 1
        """, (error_message,))


def get_sync_progress():
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM sync_progress WHERE id = 1").fetchone()
        return dict(row) if row else {
            "status": "idle", "current_step": 0, "total_steps": 0,
            "message": None, "error": None, "started_at": None, "updated_at": None,
        }


if __name__ == "__main__":
    init_db()
    print(f"Veritabanı hazır: {DB_PATH}")
