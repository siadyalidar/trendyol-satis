"""
Trendyol Günlük Satış Paneli + Kâr/Zarar Paneli
--------------------------------------------------
Trendyol Partner (Satıcı) API'sinden sipariş, iade ve finans verilerini çekip
satış özeti ve gerçek kâr/zarar hesaplaması gösteren local Flask uygulaması.

Resmi API dokümantasyonu:
https://developers.trendyol.com/docs/sipariş-paketlerini-çekme-getshipmentpackages
https://developers.trendyol.com/docs/2-authorization
https://developers.trendyol.com/docs/cari-hesap-ekstresi-entegrasyonu

Çalıştırmadan önce .env dosyasını doldurmanız gerekir (bkz. .env.example).
"""

import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from werkzeug.utils import secure_filename

from cost_import import import_product_costs
from database import (
    fail_sync_progress,
    finish_sync_progress,
    get_connection,
    get_sync_progress,
    init_db,
    start_sync_progress,
    update_sync_progress,
    upsert_order_lines,
    upsert_orders,
    upsert_product_costs,
)
from profit_engine import best_sellers as compute_best_sellers
from profit_engine import compute_profit_summary
from trendyol_finance import sync_finance_data

load_dotenv()

SUPPLIER_ID = os.getenv("TRENDYOL_SUPPLIER_ID", "").strip()
API_KEY = os.getenv("TRENDYOL_API_KEY", "").strip()
API_SECRET = os.getenv("TRENDYOL_API_SECRET", "").strip()
ENV = os.getenv("TRENDYOL_ENV", "PROD").strip().upper()  # PROD veya STAGE
INTEGRATOR_NAME = os.getenv("TRENDYOL_INTEGRATOR_NAME", "SelfIntegration").strip()

# "Tüm Zamanlar" senkronizasyonunun başlangıç noktası. Trendyol mağazanızın
# gerçek açılış tarihini .env'de TRENDYOL_DATA_START_DATE=YYYY-MM-DD olarak
# belirtmezseniz, güvenli bir varsayım olarak 3 yıl öncesi kullanılır.
_DEFAULT_START = os.getenv("TRENDYOL_DATA_START_DATE", "").strip()
try:
    DATA_START_DATE = datetime.strptime(_DEFAULT_START, "%Y-%m-%d") if _DEFAULT_START else (datetime.now() - timedelta(days=365 * 3))
except ValueError:
    DATA_START_DATE = datetime.now() - timedelta(days=365 * 3)

BASE_URL = (
    "https://apigw.trendyol.com"
    if ENV == "PROD"
    else "https://stageapigw.trendyol.com"
)

# Trendyol dokümantasyonuna göre User-Agent zorunlu:
# "{SatıcıId} - {EntegratörFirmaAdı}" ya da kendi yazılımınızsa "{SatıcıId} - SelfIntegration"
USER_AGENT = f"{SUPPLIER_ID} - {INTEGRATOR_NAME}"

app = Flask(__name__)
init_db()

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

_sync_lock = threading.Lock()  # aynı anda birden fazla senkronizasyon başlamasın


def _check_credentials():
    if not (SUPPLIER_ID and API_KEY and API_SECRET):
        return (
            "API bilgileri eksik. Lütfen proje klasöründeki .env dosyasını "
            "TRENDYOL_SUPPLIER_ID, TRENDYOL_API_KEY ve TRENDYOL_API_SECRET "
            "değerleriyle doldurun."
        )
    return None


def trendyol_get(path, params=None, max_retries=5, throttle_seconds=0.35):
    """Trendyol API'ye GET isteği atar.
    - 429 (rate limit) durumunda artan bekleme süresiyle tekrar dener.
    - Başarılı her istekten sonra küçük bir bekleme uygular, art arda çok
      sayıda istek atıldığında (senkronizasyon) limite takılma ihtimalini azaltır.
    """
    url = f"{BASE_URL}{path}"
    headers = {"User-Agent": USER_AGENT}

    for attempt in range(max_retries):
        resp = requests.get(
            url,
            params=params,
            headers=headers,
            auth=(API_KEY, API_SECRET),  # Basic Authentication
            timeout=30,
        )
        if resp.status_code == 429:
            time.sleep(3 * (2 ** attempt))
            continue
        resp.raise_for_status()
        if throttle_seconds:
            time.sleep(throttle_seconds)
        return resp.json()

    resp.raise_for_status()
    return resp.json()


def fetch_all_orders(start_ts_ms, end_ts_ms, status=None):
    """Belirtilen tarih aralığındaki tüm sipariş paketlerini sayfalayarak çeker.
    Not: Trendyol bu endpoint için maksimum 2 haftalık aralığa izin verir,
    bu yüzden çağıran taraf aralığı 2 haftalık parçalara böler.
    """
    all_orders = []
    page = 0
    size = 200  # Trendyol'un izin verdiği maksimum sayfa boyutu

    while True:
        params = {
            "startDate": start_ts_ms,
            "endDate": end_ts_ms,
            "page": page,
            "size": size,
            "orderByField": "PackageLastModifiedDate",
            "orderByDirection": "DESC",
        }
        if status:
            params["status"] = status

        data = trendyol_get(f"/integration/order/sellers/{SUPPLIER_ID}/orders", params)
        content = data.get("content") or []
        all_orders.extend(content)

        total_pages = data.get("totalPages") or 1
        page += 1
        if page >= total_pages:
            break

    return all_orders


def _date_chunks(start_dt, end_dt, max_days=14):
    """Trendyol'un 2 haftalık aralık kısıtına uymak için tarih aralığını parçalara böler."""
    chunks = []
    cur = start_dt
    while cur < end_dt:
        chunk_end = min(cur + timedelta(days=max_days), end_dt)
        chunks.append((cur, chunk_end))
        cur = chunk_end
    return chunks


def get_daily_sales(days=30, statuses=None, start_dt=None, end_dt=None):
    """Belirtilen aralıktaki siparişleri çekip günlük bazda satış özetine dönüştürür.
    days VEYA açık start_dt/end_dt kabul eder (start_dt/end_dt verilirse days yok sayılır).
    """
    if start_dt is None or end_dt is None:
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=days)

    all_orders = []
    for chunk_start, chunk_end in _date_chunks(start_dt, end_dt):
        start_ms = int(chunk_start.timestamp() * 1000)
        end_ms = int(chunk_end.timestamp() * 1000)
        orders = fetch_all_orders(start_ms, end_ms, status=statuses)
        all_orders.extend(orders)

    unique = {o["shipmentPackageId"]: o for o in all_orders if "shipmentPackageId" in o}
    orders = list(unique.values())

    daily = defaultdict(lambda: {
        "order_count": 0,
        "package_count": 0,
        "gross_amount": 0.0,
        "discount_amount": 0.0,
        "net_amount": 0.0,
        "item_count": 0,
        "commission_amount": 0.0,
        "cancelled_count": 0,
    })

    order_numbers_seen = defaultdict(set)

    for o in orders:
        order_date_ms = o.get("orderDate")
        if not order_date_ms:
            continue
        day_key = datetime.fromtimestamp(order_date_ms / 1000).strftime("%Y-%m-%d")

        bucket = daily[day_key]
        bucket["package_count"] += 1
        bucket["gross_amount"] += o.get("packageGrossAmount", 0) or 0
        bucket["discount_amount"] += o.get("packageTotalDiscount", 0) or 0
        bucket["net_amount"] += o.get("packageTotalPrice", 0) or 0

        order_numbers_seen[day_key].add(o.get("orderNumber"))

        status = o.get("status")
        if status in ("Cancelled", "UnSupplied"):
            bucket["cancelled_count"] += 1

        for line in o.get("lines", []):
            bucket["item_count"] += line.get("quantity", 0) or 0
            line_price = line.get("lineUnitPrice", 0) or 0
            qty = line.get("quantity", 0) or 0
            commission_rate = line.get("commission", 0) or 0
            bucket["commission_amount"] += line_price * qty * (commission_rate / 100)

    for day_key, bucket in daily.items():
        bucket["order_count"] = len(order_numbers_seen[day_key])

    result = [
        {"date": day, **stats}
        for day, stats in sorted(daily.items())
    ]
    return result, orders


def get_daily_returns(days=30, start_dt=None, end_dt=None):
    """getClaims servisinden iade verilerini çekip günlük bazda özetler."""
    if start_dt is None or end_dt is None:
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=days)

    all_claims = []
    for chunk_start, chunk_end in _date_chunks(start_dt, end_dt):
        start_ms = int(chunk_start.timestamp() * 1000)
        end_ms = int(chunk_end.timestamp() * 1000)
        page = 0
        size = 200
        while True:
            params = {
                "startDate": start_ms,
                "endDate": end_ms,
                "page": page,
                "size": size,
            }
            data = trendyol_get(f"/integration/order/sellers/{SUPPLIER_ID}/claims", params)
            content = data.get("content") or []
            all_claims.extend(content)
            total_pages = data.get("totalPages") or 1
            page += 1
            if page >= total_pages:
                break

    daily = defaultdict(lambda: {"claim_count": 0, "item_count": 0})
    for c in all_claims:
        claim_date_ms = c.get("claimDate") or c.get("orderDate")
        if not claim_date_ms:
            continue
        day_key = datetime.fromtimestamp(claim_date_ms / 1000).strftime("%Y-%m-%d")
        bucket = daily[day_key]
        bucket["claim_count"] += 1
        items = c.get("claimItems") or c.get("items") or []
        bucket["item_count"] += len(items)

    return [{"date": d, **s} for d, s in sorted(daily.items())]


def sync_orders_to_db(start_dt, end_dt, progress_cb=None):
    """Siparişleri ve satırlarını (barkod, merchantSku dahil) yerel DB'ye yazar.
    profit_engine.py bu tabloyu settlements, cargo_costs ve product_costs ile eşleştirir.
    """
    all_orders = []
    chunks = _date_chunks(start_dt, end_dt)
    for i, (chunk_start, chunk_end) in enumerate(chunks):
        if progress_cb:
            progress_cb(f"Siparişler: {chunk_start:%d.%m.%Y}-{chunk_end:%d.%m.%Y} ({i + 1}/{len(chunks)})")
        start_ms = int(chunk_start.timestamp() * 1000)
        end_ms = int(chunk_end.timestamp() * 1000)
        all_orders.extend(fetch_all_orders(start_ms, end_ms))

    unique = {o["shipmentPackageId"]: o for o in all_orders if "shipmentPackageId" in o}
    orders = list(unique.values())

    order_rows = []
    line_rows = []
    for o in orders:
        spid = o.get("shipmentPackageId")
        order_rows.append({
            "shipment_package_id": spid,
            "order_number": o.get("orderNumber"),
            "order_date": o.get("orderDate"),
            "status": o.get("status"),
            "customer": f"{o.get('customerFirstName', '')} {o.get('customerLastName', '')}".strip(),
            "cargo_provider": o.get("cargoProviderName"),
            "gross_amount": o.get("packageGrossAmount"),
            "discount_amount": o.get("packageTotalDiscount"),
            "net_amount": o.get("packageTotalPrice"),
        })
        for line in o.get("lines", []):
            line_rows.append({
                "shipment_package_id": spid,
                "barcode": line.get("barcode"),
                "merchant_sku": line.get("merchantSku") or line.get("sku"),
                "product_name": line.get("productName"),
                "quantity": line.get("quantity"),
                "line_unit_price": line.get("lineUnitPrice"),
                "commission_rate": line.get("commission"),
            })

    upsert_orders(order_rows)
    upsert_order_lines(line_rows)
    return len(order_rows), len(line_rows)


def _run_full_sync(start_dt, end_dt):
    """Arka planda çalışır: siparişler + finans verisi + kargo faturaları.
    İlerlemeyi database.sync_progress tablosuna yazar; /api/sync-status bunu okur.
    """
    try:
        start_sync_progress(total_steps=1, message="Siparişler çekiliyor…")

        def report(msg):
            update_sync_progress(message=msg)

        order_count, line_count = sync_orders_to_db(start_dt, end_dt, progress_cb=report)
        result = sync_finance_data(start_dt, end_dt, progress_cb=report)

        finish_sync_progress(
            message=(
                f"Tamamlandı: {order_count} sipariş, {line_count} satır, "
                f"{result['settlement_count']} settlement, {result['other_financial_count']} diğer finansal kayıt, "
                f"{result['cargo_invoice_count']} kargo faturası ({result['cargo_item_count']} kalem) senkronize edildi."
            )
        )
    except requests.HTTPError as e:
        fail_sync_progress(f"Trendyol API hatası: {e}")
    except requests.RequestException as e:
        fail_sync_progress(f"Bağlantı hatası: {e}")
    except Exception as e:
        fail_sync_progress(f"Beklenmeyen hata: {e}")


def _resolve_sync_range(args):
    """/api/sync-finance ve /api/dashboard-summary ortak tarih aralığı çözümlemesi.
    Öncelik: full_history=true > start_date=YYYY-MM-DD > days=N (varsayılan 30).
    """
    end_dt = datetime.now()
    if args.get("full_history", "").lower() == "true":
        return DATA_START_DATE, end_dt
    start_date_str = args.get("start_date")
    if start_date_str:
        try:
            start_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
            return start_dt, end_dt
        except ValueError:
            pass
    days = args.get("days", default=30, type=int) if hasattr(args, "get") else 30
    days = max(1, min(days or 30, 3650))
    return end_dt - timedelta(days=days), end_dt


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/config-status")
def config_status():
    error = _check_credentials()
    return jsonify({
        "configured": error is None,
        "message": error,
        "env": ENV,
        "supplier_id": SUPPLIER_ID if SUPPLIER_ID else None,
        "data_start_date": DATA_START_DATE.strftime("%Y-%m-%d"),
    })


@app.route("/api/daily-sales")
def daily_sales():
    error = _check_credentials()
    if error:
        return jsonify({"error": error}), 400

    start_dt, end_dt = _resolve_sync_range(request.args)

    try:
        daily, raw_orders = get_daily_sales(start_dt=start_dt, end_dt=end_dt)
    except requests.HTTPError as e:
        return jsonify({"error": f"Trendyol API hatası: {e}"}), 502
    except requests.RequestException as e:
        return jsonify({"error": f"Bağlantı hatası: {e}"}), 502

    totals = {
        "order_count": sum(d["order_count"] for d in daily),
        "gross_amount": round(sum(d["gross_amount"] for d in daily), 2),
        "discount_amount": round(sum(d["discount_amount"] for d in daily), 2),
        "net_amount": round(sum(d["net_amount"] for d in daily), 2),
        "commission_amount": round(sum(d["commission_amount"] for d in daily), 2),
        "item_count": sum(d["item_count"] for d in daily),
        "cancelled_count": sum(d["cancelled_count"] for d in daily),
    }

    for d in daily:
        d["gross_amount"] = round(d["gross_amount"], 2)
        d["discount_amount"] = round(d["discount_amount"], 2)
        d["net_amount"] = round(d["net_amount"], 2)
        d["commission_amount"] = round(d["commission_amount"], 2)

    orders_summary = [
        {
            "orderNumber": o.get("orderNumber"),
            "shipmentPackageId": o.get("shipmentPackageId"),
            "orderDate": o.get("orderDate"),
            "status": o.get("status"),
            "customer": f"{o.get('customerFirstName', '')} {o.get('customerLastName', '')}".strip(),
            "netAmount": o.get("packageTotalPrice"),
            "cargoProvider": o.get("cargoProviderName"),
        }
        for o in sorted(raw_orders, key=lambda x: x.get("orderDate", 0), reverse=True)
    ]

    return jsonify({
        "daily": daily,
        "totals": totals,
        "orders": orders_summary[:2000],  # tabloyu makul boyutta tut
    })


@app.route("/api/daily-returns")
def daily_returns():
    error = _check_credentials()
    if error:
        return jsonify({"error": error}), 400

    start_dt, end_dt = _resolve_sync_range(request.args)

    try:
        daily = get_daily_returns(start_dt=start_dt, end_dt=end_dt)
    except requests.HTTPError as e:
        return jsonify({"error": f"Trendyol API hatası (iadeler): {e}"}), 502
    except requests.RequestException as e:
        return jsonify({"error": f"Bağlantı hatası: {e}"}), 502

    return jsonify({"daily": daily})


@app.route("/api/sync-finance", methods=["POST"])
def sync_finance():
    """Siparişleri + Finans API (settlements/otherfinancials/kargo faturaları)
    verisini ARKA PLANDA çeker. Hemen döner; ilerleme için /api/sync-status'ü
    yoklayın (polling). Parametreler: days=N | start_date=YYYY-MM-DD | full_history=true
    """
    error = _check_credentials()
    if error:
        return jsonify({"error": error}), 400

    if not _sync_lock.acquire(blocking=False):
        return jsonify({"error": "Zaten devam eden bir senkronizasyon var."}), 409

    start_dt, end_dt = _resolve_sync_range(request.args)

    def _worker():
        try:
            _run_full_sync(start_dt, end_dt)
        finally:
            _sync_lock.release()

    threading.Thread(target=_worker, daemon=True).start()

    return jsonify({
        "started": True,
        "start_date": start_dt.strftime("%Y-%m-%d"),
        "end_date": end_dt.strftime("%Y-%m-%d"),
    })


@app.route("/api/sync-status")
def sync_status():
    return jsonify(get_sync_progress())


@app.route("/api/dashboard-summary")
def dashboard_summary():
    start_dt, end_dt = _resolve_sync_range(request.args)
    try:
        summary = compute_profit_summary(start_dt=start_dt, end_dt=end_dt)
    except Exception as e:
        return jsonify({"error": f"Kâr hesaplama hatası: {e}"}), 500
    # 'lines' alanı ayrıntı tablosu için ayrı endpoint yerine burada da
    # dönüyor ama büyük aralıklarda payload büyüyebilir, o yüzden kırpıyoruz.
    summary["lines"] = summary["lines"][:1000]
    return jsonify(summary)


@app.route("/api/best-sellers")
def api_best_sellers():
    start_dt, end_dt = _resolve_sync_range(request.args)
    limit = request.args.get("limit", default=10, type=int)
    limit = max(1, min(limit, 50))
    try:
        result = compute_best_sellers(start_dt=start_dt, end_dt=end_dt, limit=limit)
    except Exception as e:
        return jsonify({"error": f"Hesaplama hatası: {e}"}), 500
    return jsonify({"items": result})


@app.route("/api/cost-settings", methods=["GET", "POST"])
def cost_settings():
    """GET: yüklü ürün maliyeti sayısını döner.
    POST: multipart/form-data ile Excel dosyası yükler ve içe aktarır
    (form alanı adı: 'file'; opsiyonel 'sheet' alanı sekme adı için).
    """
    if request.method == "GET":
        with get_connection() as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM product_costs").fetchone()["c"]
        return jsonify({"product_cost_count": count})

    if "file" not in request.files:
        return jsonify({"error": "Dosya bulunamadı ('file' alanı boş)."}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Dosya seçilmedi."}), 400

    filename = secure_filename(f.filename)
    save_path = os.path.join(UPLOAD_FOLDER, filename)
    f.save(save_path)

    sheet = request.form.get("sheet") or "🧮 Ürün Maliyet"
    try:
        imported, skipped = import_product_costs(save_path, sheet_name=sheet)
    except Exception as e:
        return jsonify({"error": f"İçe aktarma hatası: {e}"}), 400

    return jsonify({"imported": imported, "skipped": skipped})


@app.route("/api/product-cost", methods=["POST"])
def upsert_manual_product_cost():
    """Tek bir SKU'nun maliyetini Excel'e dokunmadan elle girer/günceller.
    Excel'deki ürün maliyeti sekmesindeki SKU, Trendyol'daki gerçek merchantSku
    ile birebir eşleşmiyorsa (örn. Excel'de "-001"/"-002" gibi varyant SKU'ları
    varken Trendyol'da tek bir SKU kullanılıyorsa) bu endpoint'le doğrudan
    Trendyol'daki gerçek SKU için maliyet tanımlayabilirsiniz.
    Beklenen JSON: {sku, product_name?, cost_incl_vat, cost_excl_vat?,
                    sale_price_incl_vat?, sale_price_excl_vat?}
    cost_excl_vat verilmez ama cost_incl_vat verilirse, KDV oranı girilmediği
    sürece ikisi eşit kabul edilir (KDV ayrıştırması o satırda yapılmaz).
    """
    data = request.get_json(silent=True) or {}
    sku = (data.get("sku") or "").strip()
    if not sku:
        return jsonify({"error": "'sku' alanı zorunlu."}), 400

    try:
        cost_incl = float(data["cost_incl_vat"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "'cost_incl_vat' zorunlu ve sayısal olmalı."}), 400

    def _opt_float(key):
        v = data.get(key)
        if v in (None, ""):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    row = {
        "sku": sku,
        "product_name": data.get("product_name") or sku,
        "cost_incl_vat": cost_incl,
        "cost_excl_vat": _opt_float("cost_excl_vat"),
        "sale_price_incl_vat": _opt_float("sale_price_incl_vat"),
        "sale_price_excl_vat": _opt_float("sale_price_excl_vat"),
    }
    upsert_product_costs([row])
    return jsonify({"ok": True, "sku": sku})


if __name__ == "__main__":
    print(f"Trendyol Satış Paneli başlatılıyor... Ortam: {ENV}")
    print(f"'Tüm Zamanlar' senkronizasyonu şu tarihten başlayacak: {DATA_START_DATE:%d.%m.%Y} "
          f"(.env'de TRENDYOL_DATA_START_DATE=YYYY-MM-DD ile değiştirebilirsiniz)")
    if _check_credentials():
        print("UYARI: .env dosyası henüz yapılandırılmadı. Panel açılacak ama veri çekemeyecek.")
    # threaded=True: senkronizasyon arka planda çalışırken /api/sync-status gibi
    # diğer isteklerin de aynı anda cevaplanabilmesi için gerekli.
    app.run(debug=True, port=5050, threaded=True)
