"""
profit_engine.py
------------------
Sipariş satırlarını gerçek Finans API verisiyle (settlements), kargo faturası
kalemleriyle (cargo_costs) ve ürün maliyetleriyle (product_costs, KDV dahil/hariç)
birleştirip gerçek kâr/zarar hesaplar.

EŞLEŞTİRME MANTIĞI:
  order_lines.merchant_sku  <-> product_costs.sku        (maliyet + KDV oranı için)
  order_lines.barcode       <-> settlements.barcode      (gerçek hakediş için,
                                aynı shipment_package_id içinde)
  order_lines.shipment_package_id <-> cargo_costs.shipment_package_id (kargo maliyeti için)

GELİR ÖNCELİK SIRASI (her sipariş satırı için):
  1. Gerçek veri: settlements'ta transaction_type='Sale', aynı
     shipment_package_id + barcode eşleşmesi varsa -> sellerRevenue kullanılır.
  2. Tahmini veri: henüz settlement oluşmamışsa order_lines.commission_rate ile
     tahmini hesaplanır ve "estimated": true olarak işaretlenir.

MALİYET: product_costs.cost_incl_vat * quantity. SKU eşleşmesi yoksa
"missingCost": true olarak işaretlenir ve kâr hesabına DAHİL EDİLMEZ.

KARGO MALİYETİ: cargo_costs tablosundaki tutarlar, ait oldukları siparişin (shipment_package_id)
satırlarına EŞİT olarak bölüştürülür (basit varsayım — Trendyol kargo faturası satır
bazında ürün ayrımı vermiyor, sadece sipariş/paket bazında). Bir siparişin kargo
faturası kalemi henüz senkronize edilmemişse (ya da fatura DB'de yoksa) o siparişin
kargo maliyeti 0 kabul edilir ve "cargoMissing": true olarak işaretlenir.

KDV (VAT) MANTIĞI:
  - Her SKU için product_costs'taki (KDV dahil, KDV hariç) çift fiyatlardan oran
    çıkarılır: kdv_orani = (dahil - hariç) / hariç. Bu, o SKU'nun gerçek KDV oranıdır
    (Türkiye'de ürüne göre %1/%10/%20 değişebildiği için sabit oran varsayılmaz).
  - Gerçek gelir (settlement sellerRevenue, nakit/KDV dahil kabul edilir) bu oranla
    KDV hariç tutara çevrilir: gelir_haric = gelir / (1 + kdv_orani)
  - Maliyet için de aynı şekilde KDV hariç tutar çıkarılır.
  - KDV Yükümlülüğü (basitleştirilmiş) = satış KDV'si - alış KDV'si (mahsuplaşma).
    NOT: Bu, aylık KDV beyannamesi hesaplamasının YERİNE GEÇMEZ — sadece dönem
    içindeki satış/alış hareketlerinin kaba bir netleşmesidir; stopaj, kargo
    faturasının kendi KDV'si, diğer gider faturaları gibi kalemler resmi
    beyannameye ayrıca girer ve burada dahil edilmemiştir.
  - SKU'nun KDV dahil/hariç fiyatları Excel'de yoksa "vatMissing": true işaretlenir
    ve o satır için KDV ayrıştırması yapılmaz (gelir/maliyet KDV dahil kabul edilir).

DÖNEM GİDERLERİ (sipariş satırına değil, döneme ait, olduğu gibi düşülür):
  - Stoppage (stopaj)
  - DeductionInvoices (platform hizmet bedeli / ceza faturaları — kargo faturası da
    bu tipin içinde ama kargo tutarı artık cargo_costs üzerinden satır bazında
    düşüldüğü için burada TEKRAR düşülmüyor, bkz. _load_other_financial_totals)
  - CashAdvance (erken ödeme maliyeti)
"""

from collections import defaultdict
from datetime import datetime, timedelta

from database import get_connection


def _resolve_range(days=None, start_dt=None, end_dt=None):
    """days VEYA açık start_dt/end_dt kabul eder (ikisi birden de tutarlı olmalı).
    'Tüm zamanlar' gibi geniş aralıklar için start_dt/end_dt kullanılır.
    """
    if start_dt is not None and end_dt is not None:
        pass
    else:
        days = days or 30
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=days)
    return int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000)


def _load_lines(conn, start_ms, end_ms):
    return conn.execute("""
        SELECT ol.shipment_package_id, ol.barcode, ol.merchant_sku, ol.product_name,
               ol.quantity, ol.line_unit_price, ol.commission_rate,
               o.order_date, o.order_number, o.status
        FROM order_lines ol
        JOIN orders o ON o.shipment_package_id = ol.shipment_package_id
        WHERE o.order_date BETWEEN ? AND ?
    """, (start_ms, end_ms)).fetchall()


def _load_sale_settlements(conn, start_ms, end_ms):
    """(shipment_package_id, barcode) -> {seller_revenue, commission_amount}"""
    rows = conn.execute("""
        SELECT shipment_package_id, barcode, SUM(seller_revenue) AS seller_revenue,
               SUM(commission_amount) AS commission_amount
        FROM settlements
        WHERE transaction_type = 'Sale' AND transaction_date BETWEEN ? AND ?
        GROUP BY shipment_package_id, barcode
    """, (start_ms, end_ms)).fetchall()
    return {(r["shipment_package_id"], r["barcode"]): r for r in rows}


def _load_return_total(conn, start_ms, end_ms):
    row = conn.execute("""
        SELECT COALESCE(SUM(debt), 0) AS total, COUNT(*) AS cnt
        FROM settlements
        WHERE transaction_type = 'Return' AND transaction_date BETWEEN ? AND ?
    """, (start_ms, end_ms)).fetchone()
    return row["total"], row["cnt"]


def _load_other_financial_totals(conn, start_ms, end_ms):
    rows = conn.execute("""
        SELECT transaction_type,
               COALESCE(SUM(debt), 0) AS debt_total,
               COALESCE(SUM(credit), 0) AS credit_total
        FROM other_financials
        WHERE transaction_date BETWEEN ? AND ?
        GROUP BY transaction_type
    """, (start_ms, end_ms)).fetchall()
    totals = {r["transaction_type"]: {"debt": r["debt_total"], "credit": r["credit_total"]} for r in rows}

    # DeductionInvoices içindeki "Kargo Faturası" kalemlerini AYRI hesaplıyoruz
    # (aşağıda platform_fee_total'dan çıkarılacak) çünkü kargo tutarı zaten
    # cargo_costs üzerinden satır bazında düşülüyor -- aksi halde çift sayım olur.
    kargo_row = conn.execute("""
        SELECT COALESCE(SUM(debt), 0) AS total
        FROM other_financials
        WHERE transaction_type = 'DeductionInvoices' AND transaction_date BETWEEN ? AND ?
          AND (lower(COALESCE(description, '')) LIKE '%kargo%'
               OR lower(COALESCE(raw_transaction_type, '')) LIKE '%kargo%')
    """, (start_ms, end_ms)).fetchone()
    totals["_kargo_within_deduction_invoices"] = {"debt": kargo_row["total"], "credit": 0}
    return totals


def _load_costs(conn):
    rows = conn.execute("""
        SELECT sku, cost_incl_vat, cost_excl_vat, sale_price_incl_vat, sale_price_excl_vat, product_name
        FROM product_costs
    """).fetchall()
    return {r["sku"]: r for r in rows}


def _load_cargo_by_order(conn):
    """shipment_package_id -> toplam kargo tutarı. shipment_package_id boşsa
    order_number üzerinden ikinci bir sözlük döner (fallback eşleştirme için)."""
    rows = conn.execute("""
        SELECT shipment_package_id, order_number, SUM(amount) AS total
        FROM cargo_costs
        WHERE amount IS NOT NULL
        GROUP BY shipment_package_id, order_number
    """).fetchall()
    by_spid, by_order_number = {}, {}
    for r in rows:
        if r["shipment_package_id"] is not None:
            by_spid[r["shipment_package_id"]] = by_spid.get(r["shipment_package_id"], 0) + (r["total"] or 0)
        elif r["order_number"]:
            by_order_number[r["order_number"]] = by_order_number.get(r["order_number"], 0) + (r["total"] or 0)
    return by_spid, by_order_number


def _sku_vat_rate(cost_row, side="cost"):
    """side='cost' -> alış KDV oranı, side='sale' -> satış KDV oranı. Yoksa None."""
    if side == "cost":
        incl, excl = cost_row["cost_incl_vat"], cost_row["cost_excl_vat"]
    else:
        incl, excl = cost_row["sale_price_incl_vat"], cost_row["sale_price_excl_vat"]
    if incl is None or excl is None or excl == 0:
        return None
    return (incl - excl) / excl


def compute_profit_summary(days=None, start_dt=None, end_dt=None):
    start_ms, end_ms = _resolve_range(days, start_dt, end_dt)

    with get_connection() as conn:
        lines = _load_lines(conn, start_ms, end_ms)
        sale_settlements = _load_sale_settlements(conn, start_ms, end_ms)
        return_total, return_count = _load_return_total(conn, start_ms, end_ms)
        other_totals = _load_other_financial_totals(conn, start_ms, end_ms)
        costs = _load_costs(conn)
        cargo_by_spid, cargo_by_order_number = _load_cargo_by_order(conn)

    # Siparişteki satır sayısı (kargo maliyetini eşit bölüştürmek için)
    lines_per_order = defaultdict(int)
    for ln in lines:
        lines_per_order[ln["shipment_package_id"]] += 1

    line_results = []
    missing_cost_skus = set()
    estimated_count = 0
    real_count = 0
    cargo_missing_orders = set()

    for ln in lines:
        key = (ln["shipment_package_id"], ln["barcode"])
        settlement = sale_settlements.get(key)

        if settlement and settlement["seller_revenue"] is not None:
            revenue = settlement["seller_revenue"]
            commission = settlement["commission_amount"] or 0
            estimated = False
            real_count += 1
        else:
            rate = (ln["commission_rate"] or 0) / 100
            revenue = (ln["line_unit_price"] or 0) * (ln["quantity"] or 0) * (1 - rate)
            commission = (ln["line_unit_price"] or 0) * (ln["quantity"] or 0) * rate
            estimated = True
            estimated_count += 1

        cost_row = costs.get(ln["merchant_sku"])
        if cost_row and cost_row["cost_incl_vat"] is not None:
            cogs = cost_row["cost_incl_vat"] * (ln["quantity"] or 0)
            missing_cost = False
        else:
            cogs = None
            missing_cost = True
            if ln["merchant_sku"]:
                missing_cost_skus.add(ln["merchant_sku"])

        # --- Kargo maliyeti (siparişe eşit bölüştürülmüş) ---
        spid = ln["shipment_package_id"]
        cargo_total_for_order = cargo_by_spid.get(spid)
        if cargo_total_for_order is None:
            cargo_total_for_order = cargo_by_order_number.get(ln["order_number"])
        if cargo_total_for_order is None:
            cargo_line = 0.0
            cargo_missing = True
            cargo_missing_orders.add(ln["order_number"])
        else:
            n = max(lines_per_order.get(spid, 1), 1)
            cargo_line = cargo_total_for_order / n
            cargo_missing = False

        # --- KDV ayrıştırması ---
        vat_missing = cost_row is None
        revenue_excl_vat = None
        cogs_excl_vat = None
        vat_on_sale = None
        vat_on_cost = None
        if cost_row is not None:
            sale_vat_rate = _sku_vat_rate(cost_row, "sale")
            cost_vat_rate = _sku_vat_rate(cost_row, "cost")
            if sale_vat_rate is not None:
                revenue_excl_vat = revenue / (1 + sale_vat_rate)
                vat_on_sale = revenue - revenue_excl_vat
            else:
                vat_missing = True
            if cost_vat_rate is not None and cogs is not None:
                cogs_excl_vat = cogs / (1 + cost_vat_rate)
                vat_on_cost = cogs - cogs_excl_vat
            elif cogs is not None:
                vat_missing = True

        profit = (revenue - cogs - cargo_line) if cogs is not None else None
        profit_excl_vat = None
        if cogs is not None and revenue_excl_vat is not None and cogs_excl_vat is not None:
            profit_excl_vat = revenue_excl_vat - cogs_excl_vat - cargo_line  # kargonun kendi KDV'si ayrıştırılmıyor (bkz. dosya başı notu)

        line_results.append({
            "orderNumber": ln["order_number"],
            "sku": ln["merchant_sku"],
            "productName": ln["product_name"],
            "quantity": ln["quantity"],
            "revenue": round(revenue, 2) if revenue is not None else None,
            "revenueExclVat": round(revenue_excl_vat, 2) if revenue_excl_vat is not None else None,
            "commission": round(commission, 2) if commission is not None else None,
            "cogs": round(cogs, 2) if cogs is not None else None,
            "cogsExclVat": round(cogs_excl_vat, 2) if cogs_excl_vat is not None else None,
            "cargo": round(cargo_line, 2),
            "vatOnSale": round(vat_on_sale, 2) if vat_on_sale is not None else None,
            "vatOnCost": round(vat_on_cost, 2) if vat_on_cost is not None else None,
            "profit": round(profit, 2) if profit is not None else None,
            "profitExclVat": round(profit_excl_vat, 2) if profit_excl_vat is not None else None,
            "estimated": estimated,
            "missingCost": missing_cost,
            "cargoMissing": cargo_missing,
            "vatMissing": vat_missing,
        })

    gross_profit = sum(r["profit"] for r in line_results if r["profit"] is not None)
    gross_profit_excl_vat = sum(r["profitExclVat"] for r in line_results if r["profitExclVat"] is not None)
    total_revenue = sum(r["revenue"] for r in line_results if r["revenue"] is not None)
    total_cargo = sum(r["cargo"] for r in line_results)
    total_vat_on_sale = sum(r["vatOnSale"] for r in line_results if r["vatOnSale"] is not None)
    total_vat_on_cost = sum(r["vatOnCost"] for r in line_results if r["vatOnCost"] is not None)
    vat_payable = total_vat_on_sale - total_vat_on_cost

    stoppage_total = other_totals.get("Stoppage", {}).get("debt", 0)
    platform_fee_total_raw = other_totals.get("DeductionInvoices", {}).get("debt", 0)
    kargo_within_deductions = other_totals.get("_kargo_within_deduction_invoices", {}).get("debt", 0)
    # Kargo tutarı satır bazında (cargo_costs) zaten düşüldüğü için burada çıkarıyoruz:
    platform_fee_total = platform_fee_total_raw - kargo_within_deductions
    cash_advance_total = other_totals.get("CashAdvance", {}).get("debt", 0)
    # NOT (sign düzeltmesi): Trendyol API'sinde "PaymentOrder" (gerçek "Marketplace
    # Ödeme" / hakediş havalesi) kayıtları HER ZAMAN "debt" alanında geliyor, "credit"
    # hep 0 çıkıyor (bkz. trendyol_data.db örnek kayıtlar: 15/15 kayıt debt'te).
    # Eskiden "credit - debt" hesaplandığı için sana gerçekten ödenen tutar negatif
    # görünüyordu. Doğrusu:
    payment_order_net = (
        other_totals.get("PaymentOrder", {}).get("debt", 0)
        - other_totals.get("PaymentOrder", {}).get("credit", 0)
    )

    # platform_fee_total artık kargo faturası tutarı HARİÇ (yukarıda çıkarıldı),
    # bu yüzden çift sayım olmadan cargo_total ile birlikte kullanılabilir.
    overhead_total = stoppage_total + platform_fee_total + cash_advance_total
    net_profit = gross_profit - overhead_total
    net_profit_after_vat = net_profit - vat_payable

    return {
        "totals": {
            "revenue": round(total_revenue, 2),
            "gross_profit": round(gross_profit, 2),
            "gross_profit_excl_vat": round(gross_profit_excl_vat, 2),
            "cargo_total": round(total_cargo, 2),
            "stoppage": round(stoppage_total, 2),
            "platform_service_fee": round(platform_fee_total, 2),
            "cash_advance_cost": round(cash_advance_total, 2),
            "overhead_total": round(overhead_total, 2),
            "net_profit": round(net_profit, 2),
            "vat_on_sales": round(total_vat_on_sale, 2),
            "vat_on_purchases": round(total_vat_on_cost, 2),
            "vat_payable_estimate": round(vat_payable, 2),
            "net_profit_after_vat_estimate": round(net_profit_after_vat, 2),
            "return_amount": round(return_total, 2),
            "return_count": return_count,
            "payment_order_net": round(payment_order_net, 2),
        },
        "data_quality": {
            "lines_with_real_settlement": real_count,
            "lines_estimated_pending_settlement": estimated_count,
            "skus_missing_cost": sorted(missing_cost_skus),
            "orders_missing_cargo_invoice": len([o for o in cargo_missing_orders if o]),
        },
        "lines": line_results,
    }


def best_sellers(days=None, start_dt=None, end_dt=None, limit=10):
    summary = compute_profit_summary(days=days, start_dt=start_dt, end_dt=end_dt)

    agg = defaultdict(lambda: {"sku": None, "productName": None, "quantity": 0,
                                "revenue": 0.0, "profit": 0.0, "profit_lines": 0})
    for ln in summary["lines"]:
        a = agg[ln["sku"]]
        a["sku"] = ln["sku"]
        a["productName"] = ln["productName"]
        a["quantity"] += ln["quantity"] or 0
        a["revenue"] += ln["revenue"] or 0
        if ln["profit"] is not None:
            a["profit"] += ln["profit"]
            a["profit_lines"] += 1

    result = []
    for sku, a in agg.items():
        margin = (a["profit"] / a["revenue"]) if a["revenue"] else None
        result.append({
            "sku": sku,
            "productName": a["productName"],
            "quantity": a["quantity"],
            "revenue": round(a["revenue"], 2),
            "profit": round(a["profit"], 2) if a["profit_lines"] else None,
            "margin": round(margin, 4) if margin is not None else None,
        })

    result.sort(key=lambda r: (r["profit"] if r["profit"] is not None else -1e18), reverse=True)
    return result[:limit]
