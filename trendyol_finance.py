"""
trendyol_finance.py
---------------------
Trendyol Finans API'sinden (Cari Hesap Ekstresi + Kargo Faturası Detayları)
gerçek hakediş, kesinti ve kargo maliyeti verilerini çeker.

Kaynaklar:
  https://developers.trendyol.com/docs/cari-hesap-ekstresi-entegrasyonu
  https://developers.trendyol.com/docs/kargo-faturası-detayları
  https://developers.trendyol.com/reference/getcargoinvoiceitems

ÜÇ SERVİS:
  - settlements      -> satış / iade hareketleri (satır bazlı gerçek komisyon ve hakediş)
  - otherfinancials   -> stopaj, kesinti faturaları, erken ödeme, hakediş ödemesi (dönem bazlı)
  - cargo-invoice/{invoiceSerialNumber}/items -> otherfinancials'taki "Kargo Faturası" adlı
    DeductionInvoices kayıtlarının satır detayı (hangi sipariş ne kadar kargo ücreti almış)

ÖNEMLİ KISIT: settlements/otherfinancials tek istekte en fazla 15 günlük aralığa izin
veriyor. Bu yüzden trendyol_client.date_chunks(..., max_days=15) kullanılır.

ÖNEMLİ NOT (Türkçeleştirme sorunu): Trendyol API'si yanıttaki "transactionType"
alanını Türkçeleştirilmiş döndürüyor (isteğe "Sale" gönderseniz bile içerikte
"transactionType": "Satış" dönüyor). Bu yüzden DB'ye BİZİM istediğimiz kanonik
tipi ("_queried_type") yazıyoruz; API'nin orijinal metni "raw_transaction_type"
kolonunda ayrıca saklanıyor (görüntüleme / teşhis / kargo faturası ayıklama için).

ÖNEMLİ NOT (kargo faturası şeması belirsizliği): cargo-invoice/items endpoint'inin
tam yanıt şeması Trendyol dokümantasyonunda örnek JSON ile gösterilmiyor. Bu yüzden
_cargo_item_to_row() olası alan adlarını (shipmentPackageId/orderNumber/barcode/amount
vb.) savunmacı şekilde dener ve HER ZAMAN ham JSON'u da saklar (raw_json kolonu) —
gerçek veride alan adları farklı çıkarsa kolayca düzeltilebilir.

ÖNEMLİ NOT (finansal kayıtların oluşma zamanı): Finansal kayıtlar sipariş TESLİM
EDİLDİKTEN sonra oluşur. Yeni/kargodaki bir sipariş için henüz settlement kaydı
olmayabilir — profit_engine.py bu durumda tahmini komisyona düşer.
"""

import json
from datetime import datetime, timedelta

from database import (
    init_db,
    upsert_cargo_costs,
    upsert_other_financials,
    upsert_settlements,
)
from trendyol_client import SUPPLIER_ID, date_chunks, trendyol_get

# Şu an profit_engine.py'nin gerçekten kullandığı tipler. Diğer tipler
# (Discount, Coupon, Provizyon, WireTransfer, vb.) referans olarak dosya
# sonunda listeleniyor — ileride ayrıntı eklemek isterseniz buraya taşıyın.
SETTLEMENT_TRANSACTION_TYPES = ["Sale", "Return"]
OTHER_FINANCIAL_TRANSACTION_TYPES = ["Stoppage", "CashAdvance", "DeductionInvoices", "PaymentOrder"]

# Referans (kullanılmayan diğer tipler):
#   settlements: Discount, DiscountCancel, Coupon, CouponCancel, ProvisionPositive,
#     ProvisionNegative, ManualRefund, ManualRefundCancel, SellerRevenuePositive,
#     SellerRevenueNegative, CommissionPositive, CommissionNegative,
#     SellerRevenuePositiveCancel, SellerRevenueNegativeCancel,
#     CommissionPositiveCancel, CommissionNegativeCancel
#   otherfinancials: WireTransfer, IncomingTransfer, ReturnInvoice,
#     CommissionAgreementInvoice, FinancialItem


def _fetch_paginated(path, base_params, size=500):
    """Tek bir transactionType/tarih parçası için tüm sayfaları çeker."""
    results = []
    page = 0
    while True:
        params = {**base_params, "page": page, "size": size}
        data = trendyol_get(path, params)
        content = data.get("content") or []
        results.extend(content)
        total_pages = data.get("totalPages") or 1
        page += 1
        if page >= total_pages:
            break
    return results


def _settlement_to_row(s):
    return {
        "id": str(s.get("id")),
        "transaction_date": s.get("transactionDate"),
        "barcode": s.get("barcode"),
        "transaction_type": s.get("_queried_type"),
        "raw_transaction_type": s.get("transactionType"),
        "receipt_id": str(s.get("receiptId")) if s.get("receiptId") is not None else None,
        "description": s.get("description"),
        "debt": s.get("debt"),
        "credit": s.get("credit"),
        "payment_period": s.get("paymentPeriod"),
        "commission_rate": s.get("commissionRate"),
        "commission_amount": s.get("commissionAmount"),
        "seller_revenue": s.get("sellerRevenue"),
        "order_number": s.get("orderNumber"),
        "payment_order_id": s.get("paymentOrderId"),
        "payment_date": s.get("paymentDate"),
        "shipment_package_id": s.get("shipmentPackageId"),
    }


def _other_financial_to_row(f):
    return {
        "id": str(f.get("id")),
        "transaction_date": f.get("transactionDate"),
        "barcode": f.get("barcode"),
        "transaction_type": f.get("_queried_type"),
        "raw_transaction_type": f.get("transactionType"),
        "transaction_sub_type": f.get("transactionSubType"),
        "receipt_id": str(f.get("receiptId")) if f.get("receiptId") is not None else None,
        "description": f.get("description"),
        "debt": f.get("debt"),
        "credit": f.get("credit"),
        "order_number": f.get("orderNumber"),
        "payment_order_id": f.get("paymentOrderId"),
        "payment_date": f.get("paymentDate"),
        "shipment_package_id": f.get("shipmentPackageId"),
    }


def fetch_settlements(start_dt, end_dt, transaction_types=None, progress_cb=None):
    """(start_dt, end_dt) aralığındaki settlement kayıtlarını 15 günlük parçalar
    halinde çeker ve her parça geldikçe DB'ye kademeli olarak yazar (bir istek
    ortada hata verirse önceki ilerleme kaybolmasın diye).
    progress_cb(mesaj: str) -> opsiyonel, her adımda çağrılır (dashboard'a ilerleme göstermek için).
    """
    types = transaction_types or SETTLEMENT_TRANSACTION_TYPES
    total = 0
    for chunk_start, chunk_end in date_chunks(start_dt, end_dt, max_days=15):
        start_ms = int(chunk_start.timestamp() * 1000)
        end_ms = int(chunk_end.timestamp() * 1000)
        for t_type in types:
            if progress_cb:
                progress_cb(f"Settlements: {t_type} ({chunk_start:%d.%m.%Y}-{chunk_end:%d.%m.%Y})")
            rows = _fetch_paginated(
                f"/integration/finance/che/sellers/{SUPPLIER_ID}/settlements",
                {"startDate": start_ms, "endDate": end_ms, "transactionType": t_type},
            )
            for r in rows:
                r["_queried_type"] = t_type
            upsert_settlements([_settlement_to_row(r) for r in rows])
            total += len(rows)
    return total


def fetch_other_financials(start_dt, end_dt, transaction_types=None, progress_cb=None):
    """otherfinancials için aynı mantık — kademeli kayıt + ilerleme bildirimi."""
    types = transaction_types or OTHER_FINANCIAL_TRANSACTION_TYPES
    total = 0
    for chunk_start, chunk_end in date_chunks(start_dt, end_dt, max_days=15):
        start_ms = int(chunk_start.timestamp() * 1000)
        end_ms = int(chunk_end.timestamp() * 1000)
        for t_type in types:
            if progress_cb:
                progress_cb(f"Diğer finansal kayıtlar: {t_type} ({chunk_start:%d.%m.%Y}-{chunk_end:%d.%m.%Y})")
            rows = _fetch_paginated(
                f"/integration/finance/che/sellers/{SUPPLIER_ID}/otherfinancials",
                {"startDate": start_ms, "endDate": end_ms, "transactionType": t_type},
            )
            for r in rows:
                r["_queried_type"] = t_type
            upsert_other_financials([_other_financial_to_row(r) for r in rows])
            total += len(rows)
    return total


def _cargo_item_to_row(item, invoice_serial_number):
    """Kargo faturası kalemini DB satırına çevirir.
    Alan adları doğrulanmadığı için (bkz. dosya başındaki not) olası isimleri
    sırayla dener; hiçbiri tutmazsa None bırakır ama ham JSON'u her zaman saklar.
    """
    def first(*keys):
        for k in keys:
            if k in item and item[k] is not None:
                return item[k]
        return None

    item_id = first("id", "invoiceItemId", "itemId")
    if item_id is None:
        # Kararlı bir PK üretmek için invoice no + barkod/sipariş no birleşimi kullan
        item_id = f"{invoice_serial_number}-{first('barcode', 'orderNumber', 'shipmentPackageId') or len(json.dumps(item))}"

    return {
        "id": str(item_id),
        "invoice_serial_number": str(invoice_serial_number),
        "shipment_package_id": first("shipmentPackageId", "packageId"),
        "order_number": first("orderNumber", "orderNo"),
        "barcode": first("barcode"),
        "amount": first("amount", "price", "cargoPrice", "invoiceAmount", "total"),
        "raw_json": json.dumps(item, ensure_ascii=False),
    }


def fetch_cargo_invoice_items(invoice_serial_number):
    """Tek bir kargo faturasının satır kalemlerini çeker (tüm sayfalar)."""
    return _fetch_paginated(
        f"/integration/finance/che/sellers/{SUPPLIER_ID}/cargo-invoice/{invoice_serial_number}/items",
        {},
    )


def sync_cargo_costs(progress_cb=None):
    """DB'deki otherfinancials tablosunda description/raw_transaction_type içinde
    "kargo" geçen DeductionInvoices kayıtlarını bulur, her birinin invoiceSerialNumber'ı
    (= kaydın "id"'si) ile kargo faturası kalemlerini çeker ve cargo_costs tablosuna yazar.
    NOT: Bu fonksiyon settlements/otherfinancials'ın DB'de zaten senkronize edilmiş
    olmasını varsayar (önce fetch_other_financials çağrılmalı).
    """
    from database import get_connection

    with get_connection() as conn:
        rows = conn.execute("""
            SELECT id FROM other_financials
            WHERE transaction_type = 'DeductionInvoices'
              AND (
                    lower(COALESCE(description, '')) LIKE '%kargo%'
                    OR lower(COALESCE(raw_transaction_type, '')) LIKE '%kargo%'
                  )
        """).fetchall()

    invoice_ids = [r["id"] for r in rows]
    total_items = 0
    for i, invoice_id in enumerate(invoice_ids):
        if progress_cb:
            progress_cb(f"Kargo faturası detayı: {i + 1}/{len(invoice_ids)} ({invoice_id})")
        try:
            items = fetch_cargo_invoice_items(invoice_id)
        except Exception:
            # Bir fatura no ile ilgili sorun (örn. servis geçmişe dönük çalışmıyor)
            # tüm senkronizasyonu durdurmasın; diğer faturalarla devam et.
            continue
        cargo_rows = [_cargo_item_to_row(it, invoice_id) for it in items]
        upsert_cargo_costs(cargo_rows)
        total_items += len(cargo_rows)

    return len(invoice_ids), total_items


def sync_finance_data(start_dt, end_dt, progress_cb=None):
    """(start_dt, end_dt) aralığı için settlements + otherfinancials + kargo faturası
    detaylarını çekip DB'ye yazar (kademeli). 'days' yerine artık doğrudan tarih
    aralığı alıyor ki hem "son N gün" hem "tüm zamanlar" aynı fonksiyonla çalışsın.
    Returns: dict — settlement_count, other_financial_count, cargo_invoice_count, cargo_item_count
    """
    init_db()

    n_settlements = fetch_settlements(start_dt, end_dt, progress_cb=progress_cb)
    n_other = fetch_other_financials(start_dt, end_dt, progress_cb=progress_cb)
    n_invoices, n_cargo_items = sync_cargo_costs(progress_cb=progress_cb)

    return {
        "settlement_count": n_settlements,
        "other_financial_count": n_other,
        "cargo_invoice_count": n_invoices,
        "cargo_item_count": n_cargo_items,
    }


if __name__ == "__main__":
    import sys
    n_days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=n_days)
    result = sync_finance_data(start_dt, end_dt, progress_cb=print)
    print(result)
