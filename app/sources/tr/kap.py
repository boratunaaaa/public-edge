"""KAP (Kamuyu Aydınlatma Platformu) disclosure adapter.

Access (checked 2026-09-26 from the production server): public disclosure pages
https://www.kap.org.tr/tr/Bildirim/<id> are served (server-side rendered, structured XBRL-like fields);
robots.txt, the JSON list endpoints and some paths answer the KAP WAF block page (HTTP 666 "150207-PPE13") or
time out. PUBLIC EDGE does not work around that block. Production use at scale needs the licensed KAP data
service (MKK) - the adapter is built so the fetch layer can be swapped without touching the parser.

Parsed from each page: company name(s) and BIST codes, "Gönderim Tarihi" (Europe/Istanbul), "Bildirim Tipi"
(ÖDA özel durum, DG diğer, FR finansal rapor, DKB borsa duyurusu, ...), form title, summary, related companies,
update/correction flags, the explanation text and the structured share buy-back table.
"""
from __future__ import annotations

import html as htmllib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.markets.schema import CanonicalEvent, content_hash

IST = ZoneInfo("Europe/Istanbul")
PAGE_URL = "https://www.kap.org.tr/tr/Bildirim/{id}"
WAF_MARKERS = ("150207-PPE13", "hata_500_transparent")
TYPE_NAMES = {"ÖDA": "Özel Durum Açıklaması", "DG": "Diğer", "FR": "Finansal Rapor", "DKB": "Düzenleyici Kurum Bildirimi",
              "FON": "Fon Bildirimi", "SYSB": "Sermaye Yapısı", "UNV": "Unvan"}


class KapBlocked(RuntimeError):
    """The KAP WAF answered instead of the page (never retried aggressively, never bypassed)."""


@dataclass
class KapDisclosure:
    disclosure_id: str
    company_names: list[str]
    tickers: list[str]
    published_at: datetime  # timezone-aware (Istanbul -> UTC by the caller)
    type_code: str
    form_title: str
    summary: str
    related: list[str] = field(default_factory=list)
    is_update: bool = False
    is_correction: bool = False
    text: str = ""  # explanation text (Turkish part first when both languages exist)
    buyback_rows: list[dict] = field(default_factory=list)
    field_keys: list[str] = field(default_factory=list)  # oda_* keys (schema fingerprint)


def _text(page: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page, flags=re.S)
    t = re.sub(r"<br\s*/?>", "\n", t)
    t = re.sub(r"<[^>]+>", "\n", t)
    t = htmllib.unescape(t)
    return re.sub(r"[ \t\xa0]+", " ", re.sub(r"\n\s*\n+", "\n", t))


def _num(s: str) -> float | None:
    """Turkish number format: 1.499.998,82 -> 1499998.82"""
    s = (s or "").strip().replace(" ", "")
    if not re.fullmatch(r"-?[\d.]+(,\d+)?", s):
        return None
    return float(s.replace(".", "").replace(",", "."))


def parse_page(page: str, disclosure_id: str) -> KapDisclosure:
    if any(m in page for m in WAF_MARKERS):
        raise KapBlocked(f"KAP WAF block page for {disclosure_id}")
    t = _text(page)
    i = t.find("Merkezi Saklama ve Veri Depolama Kuruluşu")
    if i < 0 or "Gönderim Tarihi" not in t:
        raise ValueError(f"unexpected KAP page layout for {disclosure_id}")
    lines = [x.strip() for x in t[i:].split("\n")]
    k = lines.index("Kapat")
    head = [x for x in lines[1:k] if x]
    names = [x for x in head if not re.fullmatch(r"[A-Z0-9]{2,6}(, ?[A-Z0-9]{2,6})*", x)]
    codes = [c.strip() for x in head if re.fullmatch(r"[A-Z0-9]{2,6}(, ?[A-Z0-9]{2,6})*", x) for c in x.split(",")]
    g = lines.index("Gönderim Tarihi", k)
    d, tm = lines[g + 1], lines[g + 2]
    published = datetime.strptime(f"{d} {tm}", "%d.%m.%Y %H:%M:%S").replace(tzinfo=IST)
    type_code = lines[lines.index("Bildirim Tipi", g) + 1]
    a = lines.index("A+", g)
    form_title = lines[a - 1]
    try:  # fund / some DG pages have no "Özet Bilgi" block: the form title is the summary
        summary = lines[lines.index("Özet Bilgi", a) + 1]
    except ValueError:
        summary = form_title
    rel = re.search(r"İlgili Şirketler\nRelated Companies\n\[(.*?)\]", t[i:])
    related = [x.strip() for x in rel.group(1).split(",") if x.strip()] if rel else []
    upd = re.search(r"(?:oda_UpdateAnnouncementFlag\|\n.*?\n.*?\n|Yapılan Açıklama Güncelleme mi ?\?\n)(Evet|Hayır)", t[i:])
    cor = re.search(r"(?:oda_CorrectionAnnouncementFlag\|\n.*?\n.*?\n|Yapılan Açıklama Düzeltme mi ?\?\n)(Evet|Hayır)", t[i:])
    exp = re.search(r"oda_ExplanationTextBlock\|\n(.*?)(?:\noda_|\Z)", t[i:], re.S)
    body = exp.group(1) if exp else t[t.find("Özet Bilgi", i):]
    rows = []
    det = t.find("Geri Alım İşlemlerinin Detayları", i)  # the trade table; the planned-period table above it has dates
    NUM = r"(?!\d\d\.\d\d\.\d{4}\n)[\d.]+(?:,\d+)?"
    for m in re.finditer(r"\n([A-Z] Grubu, ([A-Z0-9]+), (TR[A-Z0-9]{10}))\n(\d\d\.\d\d\.\d{4})\n(" + NUM + r")\n(" + NUM + r")\n(" + NUM + r")\n",
                         t[det:] if det >= 0 else ""):
        rows.append({"ticker": m.group(2), "isin": m.group(3), "date": datetime.strptime(m.group(4), "%d.%m.%Y").date().isoformat(),
                     "nominal": _num(m.group(5)), "pct_capital": _num(m.group(6)), "price": _num(m.group(7))})
    return KapDisclosure(disclosure_id=str(disclosure_id), company_names=names, tickers=codes, published_at=published,
                         type_code=type_code, form_title=form_title, summary=summary, related=related,
                         is_update=bool(upd and upd.group(1).startswith("Evet")), is_correction=bool(cor and cor.group(1).startswith("Evet")),
                         text=body.strip()[:20000], buyback_rows=rows,
                         field_keys=sorted(set(re.findall(r"\n(oda_[A-Za-z]+)\|", t))))


# ------------------------------------------------------------------ classification (Turkish disclosure language)
AMOUNT = re.compile(r"([\d.]+(?:,\d+)?)\s*-?\s*(TL|TRY|USD|ABD Doları|Dolar|EUR|Avro|Euro)\b", re.I)
PCT = re.compile(r"%\s?(\d+(?:,\d+)?)")
SKIP = re.compile(r"finansman bonosu|tahvil|kupon ödemesi|\bitfa|\bMTN\b|piyasa yapıcılığı|likidite sağlayıcılı|varant|"
                  r"sürdürülebilirlik raporu|kurumsal yönetim ilkelerine uyum|esas sözleşme(nin)?\s+(\d+\.\s*maddesinin\s+)?tadil|"
                  r"repo|borsa yatırım fonu|portföy dağılım|fon bülteni|temerrüt|kira sertifikası|sukuk|borçlanma aracı|"
                  r"ihraç tavanı|kupon oran|fon ihraç|izahname|sorumluluk beyanı|fonun kullanımına ilişkin|satış hasılatı alanı|"
                  r"yatırımcı tazmin merkezi|^esas sözleşme$|\|\s*esas sözleşme\s*$", re.I)
GENERAL_MEETING_ADMIN = re.compile(r"genel kurul", re.I)
INSIDER_ROLE = re.compile(r"yönetim kurulu (başkanı|üyesi|başkan vekili)|genel müdür|yönetimde sorumluluğu bulunan|"
                          r"icra kurulu|ceo|cfo|mali işler", re.I)


def classify(dsc: KapDisclosure) -> list[tuple[str, dict]]:
    """Canonical (family, fields) list for one KAP disclosure; [] when it is administrative."""
    s = f"{dsc.form_title} | {dsc.summary}"
    txt = dsc.text
    low = f"{s} {txt[:3000]}".lower()
    out: list[tuple[str, dict]] = []
    if dsc.type_code == "DKB" or "BORSA İSTANBUL" in " ".join(dsc.company_names).upper():
        if re.search(r"volatilite bazlı tedbir|VBTS", s + txt, re.I):
            f = {"measure": "VBTS"}
            if re.search(r"açığa satış", txt, re.I):
                f["short_sale_ban"] = True
            if re.search(r"kredili işlem", txt, re.I):
                f["margin_ban"] = True
            if re.search(r"brüt takas", txt, re.I):
                f["gross_settlement"] = True
            if re.search(r"tek fiyat", txt, re.I):
                f["single_price"] = True
            if re.search(r"emir paketi|piyasa emri", txt, re.I):
                f["order_restriction"] = True
            dts = re.findall(r"(\d\d/\d\d/\d{4})", txt)
            if len(dts) >= 2:
                f["from"], f["to"] = [datetime.strptime(x, "%d/%m/%Y").date().isoformat() for x in dts[:2]]
            out.append(("TRADING_RESTRICTION", f))
        elif re.search(r"devre kesici", s, re.I):
            out.append(("TRADING_RESTRICTION", {"measure": "CIRCUIT_BREAKER"}))
        elif re.search(r"işlem sırası kapatıl|işleme kapatıl|brüt takas|tedbir", s + txt[:800], re.I):
            out.append(("TRADING_RESTRICTION", {"measure": "EXCHANGE_MEASURE"}))
        return out
    if SKIP.search(s) or re.search(r"\bFON\b|FONU\b", " ".join(dsc.company_names).upper()):
        return []
    if GENERAL_MEETING_ADMIN.search(s) and not re.search(r"kar|kâr|temettü|sermaye|birleşme|bölünme", s, re.I):
        return []  # registration / invitation of a general meeting: administrative
    if dsc.type_code == "FR" and not re.search(r"sürdürülebilir", s, re.I):
        if re.search(r"faaliyet raporu", s, re.I):
            return [("PERIODIC_REPORT", {"form": dsc.form_title})]
        return [("EARNINGS", {"form": dsc.form_title})]
    negated = re.search(r"sonlan|sona er|vazgeç|iptal|feshedil", s, re.I)
    if re.search(r"ilişkili taraf", s, re.I):
        return [("OTHER_MATERIAL_DISCLOSURE", {"subtype": "RELATED_PARTY_TRANSACTION"})]
    if re.search(r"geri al[ıi]m|geri alın", s, re.I):
        if negated:
            return [("OTHER_MATERIAL_DISCLOSURE", {"subtype": "BUYBACK_PROGRAM_ENDED"})]
        f = {"trades": dsc.buyback_rows}
        if dsc.buyback_rows:
            f["total_nominal"] = sum(r["nominal"] or 0 for r in dsc.buyback_rows)
            last = dsc.buyback_rows[-1]
            f["last_trade_date"], f["last_price_try"] = last["date"], last["price"]
        return [("SHARE_BUYBACK", f)]
    if re.search(r"zorunlu pay alım teklifi|pay alım teklifi", s, re.I):
        return [("ACQUISITION", {"subtype": "TENDER_OFFER"})]
    if re.search(r"pay (alım|alış|satım|satış)|hisse (alım|alış|satış)|sermaye piyasası aracı alım satım", s, re.I) or \
            re.search(r"(hisse|pay) (alış|alım|satış) işlemi", txt, re.I):
        sell = bool(re.search(r"satış|satım", s + " " + txt[:1500], re.I)) and not re.search(r"alış|alım", s, re.I)
        insider = bool(INSIDER_ROLE.search(txt[:3000]))
        fam = ("INSIDER_SELL" if insider else "MAJOR_HOLDER_SELL") if sell else ("INSIDER_BUY" if insider else "MAJOR_HOLDER_BUY")
        f = {}
        p = re.search(r"([\d.]+,\d+)\s*-?\s*TL ortalama fiyat", txt)
        if p:
            f["avg_price_try"] = _num(p.group(1))
        n = re.search(r"([\d.]+)\s*-?\s*TL nominal", txt)
        if n:
            f["nominal_try"] = _num(n.group(1))
        amt = re.search(r"toplam tutar[ıi]?\s*([\d.]+(?:,\d+)?)\s*TL", txt, re.I)
        if amt:
            f["amount_try"] = _num(amt.group(1))
        pc = re.search(r"(?:payımız|pay oranı|sermayedeki pay\w*).{0,80}?%\s?(\d+(?:,\d+)?)", txt, re.I)
        if pc:
            f["ownership_pct_after"] = _num(pc.group(1))
        by = re.search(r"([A-ZÇĞİÖŞÜ][\wÇĞİÖŞÜçğıöşü.&\- ]{2,80}?(?:A\.Ş\.|Holding|Ltd))", s)
        if by:
            f["buyer_or_seller"] = by.group(1).strip()
        return [(fam, f)]
    if re.search(r"bedelli sermaye art", s, re.I):
        f = {}
        r = PCT.search(s)
        if r:
            f["rate_pct"] = _num(r.group(1))
        return [("RIGHTS_ISSUE", f), ("DILUTION", {"source": "bedelli"})]
    if re.search(r"tahsisli sermaye art", s, re.I):
        return [("PRIVATE_PLACEMENT", {}), ("DILUTION", {"source": "tahsisli"})]
    if re.search(r"bedelsiz sermaye art|içsel kaynaklardan", s, re.I):
        return [("OTHER_MATERIAL_DISCLOSURE", {"subtype": "BONUS_ISSUE"})]
    if re.search(r"sermaye art", s, re.I):
        return [("CAPITAL_RAISE", {})]
    if re.search(r"birleşme|devralma", s, re.I):
        if negated:
            return [("OTHER_MATERIAL_DISCLOSURE", {"subtype": "MERGER_TALKS_ENDED"})]
        if re.search(r"%\s?100|tamamına sahip|bağlı ortaklığı ile|kolaylaştırılmış usul", s + " " + txt[:1500], re.I):
            return [("OTHER_MATERIAL_DISCLOSURE", {"subtype": "INTRAGROUP_MERGER"})]  # absorbing a wholly owned subsidiary
        return [("MERGER", {})]
    if re.search(r"(pay|hisse)\w* (tamamının |)satış|iştirak.{0,20}satış|bağlı ortaklı.{0,40}satış", s, re.I):
        return [("OTHER_MATERIAL_DISCLOSURE", {"subtype": "DIVESTMENT"})]
    if re.search(r"pay devri|satın alın|satın alma|iktisap|edinim", s, re.I):
        return [("ACQUISITION", {})]
    if re.search(r"yönetim kurulu üye|genel müdür|yönetim değişikliği|üst yönetim|atama|atan|istifa|ayrılma|görevden", s, re.I):
        role = "IR" if re.search(r"yatırımcı ilişkileri", s, re.I) else "BOARD_OR_EXECUTIVE"
        return [("MANAGEMENT_CHANGE", {"role": role})]
    if re.search(r"devir ve temlik", s, re.I):
        return [("OTHER_MATERIAL_DISCLOSURE", {"subtype": "RECEIVABLE_TRANSFER"})]
    if re.search(r"kar payı|kâr payı|temettü|kar dağıt|karın dağıtım|kârın dağıtım", s, re.I):
        if re.search(r"dağıtılmama|dağıtmama|yapılmaması", s, re.I):
            return [("OTHER_MATERIAL_DISCLOSURE", {"subtype": "NO_DIVIDEND"})]
        return [("DIVIDEND", {})]
    if re.search(r"kredi derecelendirme|derecelendirme notu", s, re.I):
        return [("OTHER_MATERIAL_DISCLOSURE", {"subtype": "CREDIT_RATING"})]
    contract = re.search(r"yeni iş ilişkisi|(?<!esas )sözleşme|ihale|sipariş|anlaşma|gelir paylaşımı", s, re.I) or \
        (re.search(r"yeni iş ilişkisi|(?<!esas )sözleşme imzala|ihale(yi)? kazan|sipariş (alın|aldı)", txt[:1200], re.I))
    if contract and not negated:
        fam = "NEW_CONTRACT"
        if re.search(r"savunma sanayii|SSB\b|bakanlığı|kamu kurum|belediye|TCDD|MSB|jandarma|emniyet", txt[:3000], re.I):
            fam = "GOVERNMENT_CONTRACT"
        elif re.search(r"ihracat|yurt ?dışı|export", txt[:3000], re.I):
            fam = "EXPORT_CONTRACT"
        f = {}
        m = AMOUNT.search(txt[:3000])
        if m:
            f["amount"], f["currency"] = _num(m.group(1)), m.group(2).upper().replace("ABD DOLARI", "USD").replace("DOLAR", "USD")
        return [(fam, f)]
    if re.search(r"kapasite art", low):
        return [("CAPACITY_EXPANSION", {})]
    if re.search(r"fabrika|üretim tesisi", low) and re.search(r"yatırım|kurulum|açılış|inşa", low):
        return [("NEW_FACTORY", {})]
    if re.search(r"üretim hattı", low):
        return [("NEW_PRODUCTION_LINE", {})]
    if re.search(r"teşvik belgesi|yatırım teşvik", low):
        return [("GOVERNMENT_SUPPORT", {"subtype": "INVESTMENT_INCENTIVE"})]
    if re.search(r"ruhsat|onay|izin", s, re.I) and re.search(r"spk|epdk|bddk|titck|bakanlık", s + txt[:600], re.I):
        return [("REGULATORY_APPROVAL", {})]
    if dsc.type_code == "ÖDA":
        return [("OTHER_MATERIAL_DISCLOSURE", {})]
    return []


def to_canonical(dsc: KapDisclosure) -> list[CanonicalEvent]:
    out = []
    ticker = (dsc.related[0] if dsc.related else None) if dsc.type_code == "DKB" else (dsc.tickers[0] if dsc.tickers else None)
    for fam, f in classify(dsc):
        f = {**f, "update": dsc.is_update, "correction": dsc.is_correction, "content_hash": content_hash(dsc.summary, dsc.text[:4000])}
        out.append(CanonicalEvent(
            country="TR", exchange="XIST", source="kap", source_id=dsc.disclosure_id, family=fam, ticker=ticker,
            company_name=(dsc.company_names[0] if dsc.company_names else None), published_at=dsc.published_at,
            source_url=PAGE_URL.format(id=dsc.disclosure_id), title=f"{ticker or ''}: {dsc.summary}"[:500], price_sensitive=None,
            fields=f, raw={"type_code": dsc.type_code, "form_title": dsc.form_title, "summary": dsc.summary, "tickers": dsc.tickers,
                           "related": dsc.related, "text": dsc.text[:4000]},
            confidence="HIGH" if (fam == "SHARE_BUYBACK" and dsc.buyback_rows) or fam == "TRADING_RESTRICTION" else "MEDIUM",
            security_id=f"XIST:{ticker}" if ticker else None))
    return out


def dedupe(events: list[CanonicalEvent]) -> list[CanonicalEvent]:
    """Same content re-published (update/correction/second ticker) is one event: keep the first."""
    seen, out = set(), []
    for e in sorted(events, key=lambda e: e.published_at):
        k = (e.ticker, e.family, e.fields.get("content_hash"))
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    return out


def local_date(e: CanonicalEvent) -> date:
    return e.published_at.astimezone(IST).date()


SKELETON = ("Merkezi Saklama ve Veri Depolama Kuruluşu", "Kapat", "Gönderim Tarihi", "Bildirim Tipi", "Bildirim Ekleri", "A+")


def skeleton_keys(page: str) -> list[str]:
    """Stable page anchors the parser relies on (the oda_* keys differ by disclosure type, so they are not used)."""
    t = _text(page)
    return [a for a in SKELETON if a in t]
