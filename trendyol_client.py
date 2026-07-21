"""
trendyol_client.py
--------------------
Trendyol API kimlik bilgileri ve ortak GET isteği fonksiyonu.
app.py ve trendyol_finance.py bu modülü paylaşır — API_KEY/SECRET/BASE_URL
tek yerden yönetilir.
"""

import os
import time
from datetime import timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

SUPPLIER_ID = os.getenv("TRENDYOL_SUPPLIER_ID", "").strip()
API_KEY = os.getenv("TRENDYOL_API_KEY", "").strip()
API_SECRET = os.getenv("TRENDYOL_API_SECRET", "").strip()
ENV = os.getenv("TRENDYOL_ENV", "PROD").strip().upper()
INTEGRATOR_NAME = os.getenv("TRENDYOL_INTEGRATOR_NAME", "SelfIntegration").strip()

BASE_URL = (
    "https://apigw.trendyol.com"
    if ENV == "PROD"
    else "https://stageapigw.trendyol.com"
)

USER_AGENT = f"{SUPPLIER_ID} - {INTEGRATOR_NAME}"


def check_credentials():
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
    - Başarılı her istekten sonra da küçük bir bekleme uygular (throttle_seconds),
      böylece art arda çok sayıda istek atıldığında (örn. finans senkronizasyonu)
      limite hiç takılmadan ilerleme şansı artar.
    """
    url = f"{BASE_URL}{path}"
    headers = {"User-Agent": USER_AGENT}

    for attempt in range(max_retries):
        resp = requests.get(
            url,
            params=params,
            headers=headers,
            auth=(API_KEY, API_SECRET),
            timeout=30,
        )
        if resp.status_code == 429:
            wait = 3 * (2 ** attempt)  # 3, 6, 12, 24, 48 sn
            time.sleep(wait)
            continue
        resp.raise_for_status()
        if throttle_seconds:
            time.sleep(throttle_seconds)
        return resp.json()

    resp.raise_for_status()
    return resp.json()


def date_chunks(start_dt, end_dt, max_days=14):
    """Tarih aralığını Trendyol'un servis kısıtlarına uyacak parçalara böler.
    Sipariş servisi (getShipmentPackages): max 14 gün.
    Finans servisi (settlements/otherfinancials): max 15 gün.
    """
    chunks = []
    cur = start_dt
    while cur < end_dt:
        chunk_end = min(cur + timedelta(days=max_days), end_dt)
        chunks.append((cur, chunk_end))
        cur = chunk_end
    return chunks
