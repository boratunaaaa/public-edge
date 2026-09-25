"""Telegram messages - Turkish, plain language, written for someone who does not follow markets.

Every alert answers four questions in the same order:
  📌 Ne oldu?   💡 Neden önemli?   📊 Geçmişte benzer durumlarda ne oldu?   ⚠️ Nelere dikkat?
Numbers shown as "Alım puanı" come from this system's own no-look-ahead backtest
(app.backtest.probability) - never invented.
"""
from __future__ import annotations

import html
from datetime import date

MONTHS = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]


def e(s) -> str:
    # apostrophes stay readable ("1 $'ın"); only <, >, & must be escaped for Telegram HTML
    return html.escape(str(s), quote=False) if s is not None else ""


def _num(v: float, digits: int) -> str:
    return f"{v:,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def money(v) -> str:
    """Turkish-style dollar amounts: 740 bin $, 1,24 milyon $, 4,61 milyar $."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "bilinmiyor"
    a = abs(v)
    if a >= 999.5e6:
        return f"{_num(v / 1e9, 2)} milyar $"
    if a >= 999.5e3:
        return f"{_num(v / 1e6, 2)} milyon $"
    if a >= 1e3:
        return f"{_num(v / 1e3, 0)} bin $"
    return f"{_num(v, 0)} $"


def pct(v, digits: int = 1, sign: bool = True) -> str:
    if v is None:
        return "bilinmiyor"
    s = f"{v * 100:+.{digits}f}" if sign else f"{v * 100:.{digits}f}"
    return "%" + s.replace(".", ",")


def tarih(d) -> str:
    if d is None:
        return "bilinmiyor"
    if isinstance(d, str):
        d = date.fromisoformat(d[:10])
    delta = (date.today() - d).days
    if delta == 0:
        return "bugün"
    if delta == 1:
        return "dün"
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


BUCKET_TR = {"MICRO": "çok küçük şirket", "EARLY_SMALL": "küçük şirket", "SMALL": "küçük-orta şirket",
             "GROWTH": "orta büyüklükte şirket", "ALREADY_SCALED": "büyük şirket", "BELOW_UNIVERSE": "çok çok küçük şirket"}


def size_line(mcap, bucket=None) -> str:
    if not mcap:
        return "Şirket değeri: bilinmiyor"
    from app.news.classify import mcap_bucket
    b = bucket or mcap_bucket(mcap)
    return f"Şirket değeri: {money(mcap)} ({BUCKET_TR.get(b, 'büyüklük bilinmiyor')})"


ROLE_TR = {"CEO": "CEO (şirketin en üst yöneticisi)", "CFO": "CFO (finans direktörü)",
           "CEO/CFO": "CEO ve finans direktörü", "Director": "yönetim kurulu üyesi", "10% Owner": "%10'dan fazla hisse sahibi büyük ortak"}


def role_tr(role: str | None, p: dict | None = None) -> str:
    p = p or {}
    if p.get("is_ceo") and p.get("is_cfo"):
        return ROLE_TR["CEO/CFO"]
    if p.get("is_ceo"):
        return ROLE_TR["CEO"]
    if p.get("is_cfo"):
        return ROLE_TR["CFO"]
    if role in ROLE_TR:
        return ROLE_TR[role]
    if p.get("is_director"):
        return ROLE_TR["Director"]
    return f"üst düzey yönetici ({e(role)})" if role else "şirket içinden bir yönetici"


CLASS_TR = {
    "MAJOR_CUSTOMER": "büyük müşteri", "NEW_CONTRACT": "yeni sözleşme/sipariş", "GOVERNMENT_CONTRACT": "devlet sözleşmesi",
    "PARTNERSHIP": "ortaklık", "COMMERCIALIZATION": "ürün satışa başladı", "PRODUCTION_START": "üretime başladı",
    "CAPACITY_EXPANSION": "üretim kapasitesini büyütüyor", "DESIGN_WIN": "müşteri ürünü seçti (design win)",
    "TECH_BREAKTHROUGH": "teknolojik ilerleme", "CLINICAL_RESULT": "ilaç deneyi sonucu", "FDA_APPROVAL": "FDA onayı",
    "FDA_REJECTION": "FDA reddi", "REGULATORY": "resmi düzenleme", "M_AND_A": "satın alma/birleşme", "ACTIVIST": "aktivist yatırımcı",
    "CAPITAL_RAISE": "para topluyor (sermaye/borç)", "DILUTION": "yeni hisse basıyor (hisse değeri sulanabilir)",
    "BANKRUPTCY_RISK": "iflas / borsadan çıkarılma riski", "LAWSUIT": "dava", "GOVERNMENT_SUPPORT": "devlet desteği",
    "GOVERNMENT_EQUITY": "devlet şirkete ortak oluyor", "MANAGEMENT_CHANGE": "yönetim değişikliği", "INSIDER_EVENT": "yönetici işlemi",
    "EARNINGS": "bilanço açıklaması", "GUIDANCE_RAISE": "şirket beklentisini yükseltti", "GUIDANCE_CUT": "şirket beklentisini düşürdü",
    "SUPPLY_SHORTAGE": "tedarik sıkıntısı", "CUSTOMER_LOSS": "müşteri kaybı", "PRODUCT_LAUNCH": "yeni ürün",
    "RECALL": "ürün geri çağırma", "CYBERSECURITY": "siber saldırı", "OTHER": "diğer",
}
AGREEMENT_TR = {
    "binding_contract": ("bağlayıcı sözleşme", True), "purchase_order": ("kesin sipariş", True),
    "commercial_supply_agreement": ("bağlayıcı tedarik anlaşması", True), "paid_development": ("ücretli geliştirme anlaşması", True),
    "framework_agreement": ("çerçeve anlaşma (tutar garanti değil)", False), "pilot": ("deneme projesi", False),
    "evaluation": ("değerlendirme/test aşaması", False), "collaboration": ("iş birliği duyurusu (gelir garantisi yok)", False),
    "loi": ("niyet mektubu (bağlayıcı değil)", False), "mou": ("niyet anlaşması - MOU (bağlayıcı değil)", False),
}
RISK_TR = {
    "GOING_CONCERN": "Şirket kendi raporunda 'faaliyetini sürdürememe' riskinden bahsediyor (ciddi uyarı).",
    "ATM_OFFERING": "Şirket piyasaya sürekli yeni hisse satabiliyor; hisse değeri sulanabilir.",
    "REGISTERED_DIRECT": "Son dönemde yatırımcılara doğrudan yeni hisse sattı.",
    "FREQUENT_CAPITAL_RAISES": "Sık sık para topluyor; bu genelde hisse fiyatına baskı yapar.",
    "REVERSE_SPLIT": "Yakın zamanda hisse birleştirme (reverse split) yaptı; zayıflık işareti olabilir.",
    "MIN_BID_DEFICIENCY": "Borsa, hisse fiyatı 1 $'ın altında kaldığı için uyarı vermiş.",
    "NASDAQ_MIN_BID_RISK": "Hisse fiyatı 1 $'ın altında; borsadan çıkarılma riski var.",
    "CONVERTIBLE_DEBT": "Hisseye dönüşebilen borcu var; ileride hisse sayısı artabilir.",
    "MATERIAL_WEAKNESS": "Şirket muhasebe kontrollerinde zayıflık olduğunu bildirmiş.",
    "LATE_FILING": "Resmi raporunu zamanında verememiş.",
    "DILUTION": "Son bir yılda hisse sayısı belirgin arttı (mevcut hisselerin payı küçüldü).",
    "CASH_RUNWAY": "Kasadaki para mevcut harcama hızıyla fazla uzun yetmeyebilir.",
    "NEGATIVE_WORKING_CAPITAL": "Kısa vadeli borçları kısa vadeli varlıklarından fazla.",
    "HIGH_SBC": "Çalışanlara çok fazla hisse ile ödeme yapıyor.",
    "LOW_LIQUIDITY": "Hisse az işlem görüyor; alıp satmak zor olabilir, fiyat sert oynayabilir.",
    "INSIDER_SELLING": "Yöneticiler son 3 ayda hisse satmış.",
    "PROMOTIONAL_PR": "Çok sık basın bülteni yayınlıyor (tanıtım ağırlıklı olabilir).",
    "EXTREME_VALUATION": "Şirket değeri, satışlarına göre çok yüksek fiyatlanmış.",
}
STAGE_TR = {"RESEARCH": "araştırma aşamasında", "PRE-COMMERCIAL": "henüz satış yapmıyor", "PILOT": "deneme üretiminde",
            "CUSTOMER_QUALIFICATION": "müşteri testlerinden geçiyor", "EARLY_COMMERCIAL": "satışa yeni başladı",
            "PRODUCTION_RAMP": "üretimini hızla artırıyor", "MASS_PRODUCTION": "seri üretimde", "SCALED": "büyük ölçekli"}
THEME_TR = {"AI_INFRASTRUCTURE": "yapay zekâ altyapısı", "SEMICONDUCTOR_MATERIALS": "çip malzemeleri", "ADVANCED_PACKAGING": "gelişmiş çip paketleme",
            "PHOTONICS_OPTICS": "fotonik/optik", "NETWORKING": "ağ teknolojisi", "DATA_CENTER_COOLING": "veri merkezi soğutma",
            "POWER_SYSTEMS": "enerji/güç sistemleri", "NUCLEAR": "nükleer enerji", "ROBOTICS": "robotik", "SENSING_VISION": "sensör/görüntüleme",
            "PRECISION_COMPONENTS": "hassas parçalar/mıknatıslar", "ADVANCED_MATERIALS": "ileri malzemeler", "BATTERIES": "batarya",
            "HYDROGEN": "hidrojen", "AEROSPACE_DEFENSE": "havacılık/savunma", "DRONES_EVTOL": "drone/uçan taksi", "AUTONOMY": "otonom araçlar",
            "SPACE": "uzay", "BIOTECH": "biyoteknoloji/ilaç", "MEDTECH": "tıbbi cihaz", "INDUSTRIAL_AUTOMATION": "endüstriyel otomasyon",
            "ADVANCED_MANUFACTURING": "ileri üretim", "QUANTUM": "kuantum"}


# ------------------------------------------------------------------ historical block ("Alım puanı")

def verdict(rate, risks: list[dict]) -> tuple[str, str]:
    """Plain-language overall read based ONLY on the measured historical rate and hard risks."""
    high = [r for r in risks if r.get("severity") == "HIGH"]
    if len(high) >= 2:
        return "🔴", "RİSKLİ — şirkette birden fazla ciddi uyarı var, çok dikkatli olunmalı"
    if rate is None or not rate.available:
        return "⚪", "BELİRSİZ — geçmiş veri yetersiz, sadece takip listesine al"
    p = rate.probability
    if high and p < 0.65:
        return "🟠", "ZAYIF — ciddi risk uyarısı var"
    if p >= 0.60:
        return "🟢", "GÜÇLÜ"
    if p >= 0.53:
        return "🟡", "ORTA"
    return "🔴", "ZAYIF — geçmişte benzer durumlar çoğunlukla piyasanın gerisinde kaldı"


def history_block(rate) -> list[str]:
    if rate is None:
        return ["📊 <b>Geçmişte ne oldu?</b>", "Henüz hesaplanmadı."]
    if not rate.available:
        return ["📊 <b>Geçmişte ne oldu?</b>",
                f"Bu tür olay için sistemde henüz yeterli geçmiş örnek yok ({rate.n} adet). Güvenilir bir yüzde veremiyoruz."]
    months = {30: "1 ay", 90: "3 ay", 180: "6 ay"}.get(rate.horizon_days, f"{rate.horizon_days} gün")
    out = ["📊 <b>Geçmişte ne oldu?</b>",
           f"Geçmişteki benzer olay sayısı: {rate.n}",
           f"Bunlarda hissenin {months} içinde borsa ortalamasını (S&amp;P 500) geçme oranı: <b>%{rate.probability * 100:.0f}</b>"
           f"  <i>(karşılaştırma grubu: {e(rate.group)})</i>"]
    if rate.median_excess is not None:
        out.append(f"Tipik fark: borsa ortalamasına göre {pct(rate.median_excess)}")
    if rate.typical_drawdown is not None and rate.typical_drawdown < 0:
        out.append(f"Bu süreçte arada tipik düşüş: {pct(rate.typical_drawdown, sign=False)} — bu kadar düşüş bu tür "
                   "hisselerde normal görülmüş, tek başına panik sebebi değil.")
    if rate.best_horizon:
        span = {30: "1 ay", 90: "3 ay", 180: "6 ay"}.get(rate.best_horizon, f"{rate.best_horizon} gün")
        out.append(f"Geçmişte en iyi sonuç genelde ~{span} tutunca alınmış.")
    return out


def score_line(rate) -> str:
    if rate is None or not rate.available:
        return "🎯 <b>Alım puanı:</b> hesaplanamıyor (yeterli geçmiş veri yok)"
    return (f"🎯 <b>Alım puanı: %{rate.probability * 100:.0f}</b>  (100 benzer durumdan yaklaşık "
            f"{rate.probability * 100:.0f} tanesi borsa ortalamasından iyi gitti)")


def risk_block(risks: list[dict], extra: list[str] | None = None) -> list[str]:
    lines = [f"• {e(RISK_TR.get(r['risk'], r['risk']))}" for r in sorted(risks, key=lambda r: r.get("severity") != "HIGH")[:5]]
    lines += [f"• {e(x)}" for x in (extra or [])]
    return ["⚠️ <b>Dikkat</b>"] + (lines or ["• Kayıtlı ciddi risk yok."])


def _company(ev: dict) -> str:
    tk = ev.get("ticker")
    name = ev.get("company_name") or ""
    return f"🏢 <b>{e(name.title() if name.isupper() else name)}</b>" + (f"  (hisse kodu: <b>{e(tk)}</b>)" if tk else "")


def _source(ev: dict) -> list[str]:
    return ["🔗 Kaynak belge:", e(ev.get("source_url"))]


# ------------------------------------------------------------------ per event type

def what_happened(ev: dict) -> tuple[str, list[str], list[str]]:
    """(header, 'Ne oldu?' lines, 'Neden önemli?' lines)"""
    t = ev["event_type"]
    p = ev.get("payload") or {}
    if t == "INSIDER_OPEN_MARKET_BUY":
        what = [f"{e(p.get('owner'))} — {role_tr(p.get('role'), p)} — kendi parasıyla <b>{money(p.get('value'))}</b> "
                f"değerinde şirket hissesi aldı ({tarih((p.get('transaction_dates') or [None])[0])})."]
        if p.get("holdings_increase"):
            what.append(f"Elindeki hisse miktarındaki artış: <b>{pct(p['holdings_increase'], 0, sign=False)}</b>")
        why = ["Şirketi en iyi tanıyan kişinin kendi cebinden hisse alması, şirketin geleceğine güvendiğinin işareti sayılır."]
        if p.get("insider_type") == "opportunistic":
            why.append("Bu kişi her yıl aynı zamanda işlem yapan biri değil; bu alım <b>alışılmışın dışında</b>. "
                       "Akademik araştırmalar en güçlü sinyalin bu tür alımlar olduğunu gösteriyor.")
        elif p.get("insider_type") == "routine":
            why.append("Ancak bu kişi her yıl aynı ayda işlem yapıyor (rutin). Rutin alımların bilgi değeri düşük sayılır.")
        if p.get("is_10b5_1"):
            why.append("Alım önceden planlanmış bir programa bağlı; sinyal değeri daha düşük.")
        return "🟢 YÖNETİCİ KENDİ ŞİRKETİNDEN HİSSE ALDI", what, why
    if t == "CLUSTER_INSIDER_BUY":
        what = [f"{p.get('number_of_buyers')} farklı yönetici {p.get('span_days')} gün içinde toplam "
                f"<b>{money(p.get('aggregate_value'))}</b> hisse aldı:"]
        what += [f"• {e(b.get('name'))} ({role_tr(b.get('role'), b)}): {money(b.get('value'))}" for b in (p.get("buyers") or [])[:5]]
        why = ["Birden fazla yöneticinin aynı dönemde alım yapması, tek kişinin alımından daha güçlü bir güven işareti sayılır."]
        return "🟢🟢 BİRDEN FAZLA YÖNETİCİ AYNI ANDA HİSSE ALDI", what, why
    if t == "ACTIVIST_13D":
        what = [f"{e(p.get('investor'))} adlı yatırımcı şirkette <b>{pct((p.get('percent') or 0) / 100, 1, sign=False)}</b> pay sahibi olduğunu bildirdi."]
        if p.get("change") is not None:
            what.append(f"Önceki bildirime göre payı {pct(p['change'] / 100, 2)} değişti.")
        why = ["13D bildirimi: %5'ten fazla hisse toplayan ve şirkette değişiklik isteyebilecek bir yatırımcı demek."]
        cls = p.get("item4_class")
        if cls not in (None, "PASSIVE_OR_UNCLEAR", "MANAGEMENT_DISCUSSION"):
            why.append(f"Yatırımcının açıkladığı amaç: <b>{e(ITEM4_TR.get(cls, cls))}</b>. Bu tür talepler hisseyi hareketlendirebilir.")
        return "🟠 BÜYÜK BİR YATIRIMCI ŞİRKETTE PAY TOPLADI", what, why
    if t == "GOV_CONTRACT":
        what = [f"{e(p.get('recipient'))} şirketi {e(p.get('agency'))} kurumundan <b>{money(p.get('amount'))}</b> değerinde sözleşme aldı."]
        why = [f"Sözleşmenin şirketin toplam değerine oranı: <b>{pct(p.get('value_to_mcap'), 1, sign=False)}</b>. "
               "Küçük bir şirket için bu, büyük bir gelir anlamına gelebilir."]
        return "🏛 ŞİRKET BÜYÜK BİR DEVLET SÖZLEŞMESİ ALDI", what, why
    if t == "CONGRESS_BUY":
        what = [f"ABD Temsilciler Meclisi üyesi {e(p.get('politician'))} bu hisseden {e(p.get('amount_range'))} aralığında aldı "
                f"(işlem: {tarih(p.get('transaction_date'))}, açıklama: {p.get('disclosure_delay_days')} gün sonra)."]
        why = ["Kongre üyelerinin işlemleri takip edilir; ancak açıklamalar gecikmeli geldiği için etkisi sınırlı olabilir."]
        return "🏛 KONGRE ÜYESİ HİSSE ALDI", what, why
    if t == "CONGRESS_CLUSTER_BUY":
        what = [f"{p.get('n_members')} farklı Kongre üyesi {p.get('window_days')} gün içinde bu hisseyi aldı."]
        why = ["Birden fazla üyenin aynı hisseyi alması tek bir alımdan daha dikkat çekicidir."]
        return "🏛🏛 BİRDEN FAZLA KONGRE ÜYESİ AYNI HİSSEYİ ALDI", what, why
    if t == "TRUMP_FAMILY_SEC_EVENT":
        what = [f"{e(p.get('person'))} için SEC'e resmi bir bildirim yapıldı ({e(p.get('filing_type'))})."]
        if p.get("ownership"):
            what.append(f"Sahiplik: {e(p['ownership'])}")
        why = ["Sadece resmi kayıt bilgisi olarak iletiliyor."]
        return "🇺🇸 TRUMP AİLESİ İLE İLGİLİ RESMİ BİLDİRİM", what, why
    if t == "FORM144_MAJOR_SALE":
        what = [f"{e(p.get('insider'))} yaklaşık <b>{money(p.get('est_value'))}</b> değerinde hisse satmayı planladığını bildirdi."]
        why = ["Satış niyeti tek başına olumsuz değildir (vergi, ev alımı vb. olabilir)."
               + (" Önceden planlanmış bir program kapsamında." if p.get("planned_10b5_1") else "")]
        return "🔸 YÖNETİCİ HİSSE SATMAYA HAZIRLANIYOR (bilgi)", what, why
    if t == "MULTI_SIGNAL_EVENT":
        fams = {"CEO_BUY": "CEO alımı", "CFO_BUY": "finans direktörü alımı", "INSIDER_CLUSTER": "çoklu yönetici alımı",
                "ACTIVIST_13D": "aktivist yatırımcı", "GOV_CONTRACT": "devlet sözleşmesi", "CONGRESS_BUY": "Kongre alımı",
                "MATERIAL_NEWS": "önemli şirket haberi"}
        what = ["Aynı şirkette kısa süre içinde birbirinden bağımsız birden fazla olumlu sinyal görüldü: "
                + ", ".join(fams.get(f, f) for f in (p.get("families") or [])) + "."]
        why = ["Farklı kaynaklardan gelen sinyallerin üst üste binmesi, tek bir sinyalden daha dikkat çekicidir."]
        return "⭐ AYNI ŞİRKETTE BİRDEN FAZLA OLUMLU SİNYAL", what, why
    if t == "MATERIAL_NEWS":
        classes = [CLASS_TR.get(c, c) for c in (p.get("classes") or []) if c != "OTHER"]
        what = [f"Konu: <b>{e(', '.join(classes) or 'genel')}</b>",
                f"Şirketin açıklaması (orijinal İngilizce başlık): <i>{e(p.get('headline'))}</i>"]
        agr = AGREEMENT_TR.get(p.get("agreement") or "")
        if agr:
            what.append(f"Anlaşma türü: {agr[0]}")
        if p.get("contract_value"):
            what.append(f"Tahmini tutar: <b>{money(p['contract_value'])}</b>")
        why = []
        if p.get("value_to_revenue") is not None:
            why.append(f"Tutarın şirketin yıllık satışlarına oranı: <b>{pct(p['value_to_revenue'], 0, sign=False)}</b>")
        if p.get("value_to_mcap") is not None:
            why.append(f"Tutarın şirketin toplam değerine oranı: <b>{pct(p['value_to_mcap'], 1, sign=False)}</b> "
                       "(oran büyüdükçe şirket için önemi artar)")
        if agr and not agr[1]:
            why.append("Dikkat: anlaşma bağlayıcı değil, tek başına gelir garantisi vermez.")
        why = why or ["Şirket hakkında fiyatı etkileyebilecek yeni bilgi."]
        return "📰 ÖNEMLİ ŞİRKET HABERİ", what, why
    if t == "STAGE_TRANSITION":
        what = [f"Şirket '{STAGE_TR.get(p.get('from'), p.get('from'))}' aşamasından '<b>{STAGE_TR.get(p.get('to'), p.get('to'))}</b>' aşamasına geçti."]
        why = ["Küçük teknoloji şirketlerinde en değerli anlardan biri, denemeden gerçek satışa geçiştir."]
        return "🚀 ŞİRKET YENİ BİR AŞAMAYA GEÇTİ", what, why
    if t.startswith("FIRST_"):
        what = [f"Resmi belgelere göre şirketin ilk büyük müşterisi/ortağı: <b>{e(p.get('counterparty'))}</b>.",
                f"<i>{e((p.get('sentence') or '')[:300])}</i>"]
        why = ["Küçük bir şirketin büyük ve tanınmış bir müşteri kazanması, ürününün gerçekten işe yaradığının kanıtı sayılır."]
        return "🤝 İLK BÜYÜK MÜŞTERİ", what, why
    if t in ("REVENUE_ACCELERATION", "BACKLOG_BREAKOUT", "MARGIN_INFLECTION", "CAPACITY_EXPANSION"):
        m = p.get("metrics") or {}
        txt = {"REVENUE_ACCELERATION": f"Satışları art arda iki çeyrektir hızlanıyor (son yıllık büyüme {pct(m.get('yoy_latest'), 0)}).",
               "BACKLOG_BREAKOUT": "Bekleyen sipariş miktarı (backlog) bir yılda belirgin büyüdü.",
               "MARGIN_INFLECTION": "Satış başına kârlılığı art arda iki çeyrektir iyileşiyor.",
               "CAPACITY_EXPANSION": "Üretim yatırımlarını ciddi şekilde artırıyor."}[t]
        return "📈 ŞİRKETİN İŞLERİNDE İVMELENME", [txt], ["Tek çeyrek değil, trend olarak doğrulandı."]
    if t in ("THESIS_STRENGTHENING", "THESIS_WEAKENING", "THESIS_INVALIDATION"):
        head = {"THESIS_STRENGTHENING": "🟢 TAKİP ETTİĞİMİZ ŞİRKETTE OLUMLU GELİŞME",
                "THESIS_WEAKENING": "🟠 TAKİP ETTİĞİMİZ ŞİRKETTE OLUMSUZ GELİŞME",
                "THESIS_INVALIDATION": "🔴 TAKİP ETTİĞİMİZ ŞİRKETTE CİDDİ OLUMSUZ GELİŞME"}[t]
        return head, [f"<i>{e(p.get('headline'))}</i>"], [e(p.get("explanation"))]
    return f"ℹ️ {e(t)}", [e(ev.get("title"))], []


ITEM4_TR = {"SALE_OF_COMPANY": "şirketin satılmasını istiyor", "MERGER": "birleşme", "STRATEGIC_ALTERNATIVES": "stratejik seçenekleri değerlendirme",
            "BOARD_SEAT": "yönetim kurulunda koltuk istiyor", "BOARD_CHANGE": "yönetim değişikliği istiyor",
            "BUYBACK_REQUEST": "hisse geri alımı istiyor", "ASSET_SALE": "bazı işlerin satılmasını istiyor",
            "CAPITAL_ALLOCATION": "paranın farklı kullanılmasını istiyor", "OTHER_ACTIVISM": "şirkette değişiklik istiyor"}


def price_context_line(ctx: dict | None) -> str | None:
    """Plain context only (not a signal): recent move and where the price sits in its 1-year range."""
    if not ctx or ctx.get("change_3m") is None:
        return None
    ch = ctx["change_3m"]
    word = "yükseldi" if ch > 0 else "düştü"
    line = f"Hisse son 3 ayda {pct(abs(ch), 0, sign=False)} {word}."
    pos = ctx.get("range_pos")
    if pos is not None:
        if pos <= 0.15:
            line += " Fiyat son bir yılın en düşük seviyelerine yakın."
        elif pos >= 0.85:
            line += " Fiyat son bir yılın en yüksek seviyelerine yakın."
    return line


def format_alert(ev: dict, rate=None, risks: list[dict] | None = None, related: list[str] | None = None,
                 thesis: str | None = None, market_cap=None, bucket=None, price_ctx: dict | None = None) -> str:
    risks = risks or []
    p = ev.get("payload") or {}
    head, what, why = what_happened(ev)
    icon, verdict_txt = verdict(rate, risks)
    extra_risk = []
    if (bucket or p.get("bucket") or p.get("mcap_bucket")) in ("MICRO", "BELOW_UNIVERSE"):
        extra_risk.append("Çok küçük şirket: fiyatı çok sert inip çıkabilir.")
    out = [f"<b>{head}</b>", "", _company(ev), size_line(market_cap or p.get("market_cap"), bucket or p.get("bucket") or p.get("mcap_bucket")),
           f"Bilgi açıklandı: {tarih(ev.get('public_date'))}"]
    pc = price_context_line(price_ctx)
    if pc:
        out.append(f"📈 {pc}")
    out += ["",
           "📌 <b>Ne oldu?</b>"] + what + ["", "💡 <b>Neden önemli?</b>"] + why
    if related:
        out += ["", "🔁 <b>Aynı şirkette son zamanlarda:</b>"] + [f"• {e(r)}" for r in related[:4]]
    if thesis:
        out += ["", f"🧭 Takip notu: {e(thesis)}"]
    out += [""] + history_block(rate) + ["", score_line(rate), f"{icon} <b>Sistem değerlendirmesi: {verdict_txt}</b>", ""]
    out += risk_block(risks, extra_risk) + [""] + _source(ev)
    return "\n".join(out)


def format_discovery(ev: dict, risks: list[dict], sources: list[str], rate=None) -> str:
    p = ev.get("payload") or {}
    m = p.get("metrics") or {}
    themes = [THEME_TR.get(t, t) for t in (p.get("themes") or [])]
    icon, verdict_txt = verdict(rate, risks)
    out = ["<b>🔬 YENİ KEŞİF: HENÜZ ÇOK BİLİNMEYEN BİR ŞİRKET</b>", "", _company(ev),
           size_line(p.get("market_cap"), p.get("bucket")), f"Alan: {e(', '.join(themes) or 'belirlenemedi')}",
           f"Durum: şirket {STAGE_TR.get(p.get('stage'), 'aşaması belirsiz')}", "",
           "📌 <b>Neden radarımıza girdi?</b>"] + [f"• {e(w)}" for w in (p.get("why") or [])[:6]]
    if m.get("ttm_revenue") is not None:
        out.append(f"• Son 12 ay satışı {money(m['ttm_revenue'])}" + (f", bir yılda {pct(m['yoy_latest'], 0)} büyüme" if m.get("yoy_latest") is not None else ""))
    if m.get("runway_months") is not None:
        out.append(f"• Kasadaki para bugünkü harcamayla ~{m['runway_months']:.0f} ay yeter")
    out += ["", "💡 <b>Neden önemli?</b>",
            "Büyük sektörlerde kritik bir ihtiyacı çözen, henüz küçük ama gerçek satış/müşteri ilerlemesi gösteren şirketler "
            "erken fark edilirse büyüme potansiyeli taşıyabilir. Fiyat hareketi değil, resmi belgelerdeki iş ilerlemesi dikkate alındı."]
    out += [""] + history_block(rate) + ["", score_line(rate), f"{icon} <b>Sistem değerlendirmesi: {verdict_txt}</b>", ""]
    out += risk_block(risks) + ["", "🔗 Kaynaklar: " + e(", ".join(s for s in sources if s))]
    return "\n".join(out)


def one_line(ev: dict) -> str:
    """Short Turkish summary used in daily/weekly reports."""
    head, what, _ = what_happened(ev)
    tk = ev.get("ticker")
    first = html.unescape(re_strip_tags(what[0])) if what else ""
    return f"{head.split(' ', 1)[-1].capitalize()} — {tk + ': ' if tk else ''}{first[:140]}"


def re_strip_tags(s: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", s)


# backwards-compatible names
def format_event(ev: dict, stats=None, stats_label: str = "", extra=None, market_cap=None, rate=None, risks=None) -> str:
    return format_alert(ev, rate=rate, risks=risks, related=extra, market_cap=market_cap)


def format_material_news(ev: dict, related: list[str], thesis: str | None, risks: list, rate=None) -> str:
    return format_alert(ev, rate=rate, risks=risks, related=related, thesis=thesis)
