# Trendyol Günlük Satış Paneli

Kendi bilgisayarınızda çalışan, Trendyol Satıcı (Partner) API'sinden sipariş ve
iade verilerini çekip günlük satış özetini gösteren basit bir web paneli.

Gösterdiği veriler:
- Günlük net satış tutarı, brüt tutar, indirim ve tahmini komisyon
- Günlük sipariş adedi grafiği
- Günlük iade adedi grafiği
- Son siparişlerin listesi (tarih, müşteri, durum, kargo, tutar)

## 1. API Bilgilerinizi Alın

1. https://partner.trendyol.com adresinden Satıcı Panelinize giriş yapın (master/admin kullanıcı ile).
2. Hesap menüsünden **Hesap Bilgilerim → Entegrasyon Bilgileri** sekmesine gidin.
3. **Satıcı ID (Supplier ID)**, **API Key** ve **API Secret** bilgilerinizi not edin.

> ⚠️ Bu bilgileri kimseyle paylaşmayın, GitHub gibi açık platformlara yüklemeyin.

## 2. Kurulum

Python 3.9+ gereklidir.

```bash
cd trendyol-satis-paneli
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 3. Yapılandırma

`.env.example` dosyasını `.env` olarak kopyalayın ve kendi bilgilerinizi girin:

```bash
cp .env.example .env
```

`.env` içeriği:

```
TRENDYOL_SUPPLIER_ID=123456
TRENDYOL_API_KEY=xxxxxxxx
TRENDYOL_API_SECRET=xxxxxxxx
TRENDYOL_ENV=PROD
TRENDYOL_INTEGRATOR_NAME=SelfIntegration
```

- `TRENDYOL_ENV`: Gerçek mağaza verileriniz için `PROD`, test ortamı için `STAGE`.
- `TRENDYOL_INTEGRATOR_NAME`: Kendi yazılımınız olduğu için `SelfIntegration` bırakabilirsiniz.

## 4. Çalıştırma

```bash
python app.py
```

Ardından tarayıcıda **http://localhost:5050** adresini açın.

## Notlar ve Sınırlamalar

- Trendyol'un `getShipmentPackages` servisi tek istekte **maksimum 2 haftalık** aralığa izin verir;
  uygulama bunu otomatik olarak parçalara bölüp birleştiriyor. "Son 90 gün" gibi geniş aralıklar
  daha fazla istek atacağından yüklenmesi biraz sürebilir.
- Trendyol API'sinde aynı endpoint'e 10 saniyede en fazla 50 istek atılabiliyor; panel bu limite
  takılırsa (429 hatası) otomatik olarak birkaç saniye bekleyip tekrar dener.
- Komisyon tutarı, sipariş satırlarındaki `commission` (komisyon oranı) alanına göre **tahmini**
  olarak hesaplanır; kesin muhasebe rakamları için Trendyol'un finans/mutabakat raporlarını
  esas almanız önerilir.
- İadeler `getClaims` servisinden çekilir; Trendyol hesabınızda iade işlemi geçmişi kısıtlıysa
  bu servis boş dönebilir.
- API anahtarlarınız yalnızca kendi bilgisayarınızdaki `.env` dosyasında tutulur; hiçbir veri
  Trendyol dışında bir yere gönderilmez.

## Sorun Giderme

- **401 hatası / "ClientApiAuthenticationException"**: API Key/Secret veya Satıcı ID hatalı.
  Bilgileri panelden tekrar kontrol edin.
- **403 hatası**: User-Agent header eksik/hatalı olabilir; `.env` dosyasındaki
  `TRENDYOL_INTEGRATOR_NAME` alanının dolu olduğundan emin olun.
- **429 hatası (çok sık tekrar ederse)**: Kısa süre bekleyip "Yenile" butonuna tekrar basın.
