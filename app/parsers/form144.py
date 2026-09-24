"""SEC Form 144 (notice of proposed sale) XML parser."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from lxml import etree

from app.parsers.sec_common import strip_ns, to_date, to_float


@dataclass
class Form144:
    issuer_cik: str | None
    issuer_name: str | None
    insider_name: str | None
    relationship: str | None
    shares: float | None
    est_value: float | None
    shares_outstanding: float | None
    broker: str | None
    approx_sale_date: date | None
    prior_sales: list[dict] = field(default_factory=list)
    plan_adoption_dates: str | None = None  # non-empty => sale under a Rule 10b5-1 plan


def _t(el, path):
    x = el.find(path)
    return None if x is None else ((x.text or "").strip() or None)


def parse_form144(xml: str) -> Form144:
    root = etree.fromstring(strip_ns(xml).encode("utf-8"), parser=etree.XMLParser(recover=True, huge_tree=True))
    info = root.find(".//issuerInfo")
    sec = root.find(".//securitiesInformation")
    rel = None
    if info is not None:
        rels = [(x.text or "").strip() for x in info.iter("relationshipToIssuer") if (x.text or "").strip()]
        rel = ", ".join(rels) or None
    prior = []
    for s in root.iter("securitiesSoldInPast3Months"):
        prior.append({
            "seller": _t(s, ".//name"),
            "sale_date": str(to_date(_t(s, "saleDate")) or ""),
            "shares": to_float(_t(s, "amountOfSecuritiesSold")),
            "gross_proceeds": to_float(_t(s, "grossProceeds")),
        })
    shares = est = outstanding = None
    broker = None
    sale_date = None
    if sec is not None:
        shares = to_float(_t(sec, "noOfUnitsSold"))
        est = to_float(_t(sec, "aggregateMarketValue"))
        outstanding = to_float(_t(sec, "noOfUnitsOutstanding"))
        broker = _t(sec, ".//brokerOrMarketmakerDetails/name") or _t(sec, ".//name")
        sale_date = to_date(_t(sec, "approxSaleDate"))
    return Form144(
        issuer_cik=(_t(info, "issuerCik") or "").zfill(10) if info is not None and _t(info, "issuerCik") else None,
        issuer_name=_t(info, "issuerName") if info is not None else None,
        insider_name=_t(info, "nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold") if info is not None else None,
        relationship=rel,
        shares=shares, est_value=est, shares_outstanding=outstanding, broker=broker,
        approx_sale_date=sale_date, prior_sales=prior,
        plan_adoption_dates=_t(root, ".//noticeSignature/planAdoptionDates"),
    )
