"""SEC Form 4 ownership XML parser and transaction classification."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from lxml import etree

from app.parsers.sec_common import strip_ns, to_bool, to_date, to_float

# Categories (the only high-quality buy signal is OPEN_MARKET_BUY).
OPEN_MARKET_BUY = "OPEN_MARKET_BUY"
OPEN_MARKET_SALE = "OPEN_MARKET_SALE"
OPTION_EXERCISE = "OPTION_EXERCISE"
GRANT_AWARD = "GRANT_AWARD"
TAX_WITHHOLDING = "TAX_WITHHOLDING"
DISPOSITION = "DISPOSITION"
GIFT = "GIFT"
CONVERSION = "CONVERSION"
OTHER = "OTHER"

_CODE_CATEGORY = {
    "P": OPEN_MARKET_BUY,
    "S": OPEN_MARKET_SALE,
    "M": OPTION_EXERCISE,
    "X": OPTION_EXERCISE,
    "O": OPTION_EXERCISE,
    "A": GRANT_AWARD,
    "F": TAX_WITHHOLDING,
    "D": DISPOSITION,
    "G": GIFT,
    "C": CONVERSION,
}

CEO_RE = re.compile(r"\b(ceo|chief\s+executive|principal\s+executive)\b", re.I)
CFO_RE = re.compile(r"\b(cfo|chief\s+financial|principal\s+financial)\b", re.I)
# "President" alone is not CEO; "President & CEO" matches CEO_RE already.
PLAN_RE = re.compile(r"10b5-?1|10b-5-?1|rule\s+10b5", re.I)


def classify(code: str | None, is_derivative: bool) -> str:
    c = (code or "").strip().upper()
    if is_derivative and c in {"M", "X", "O"}:
        return OPTION_EXERCISE
    if is_derivative:
        return OTHER if c not in _CODE_CATEGORY else (_CODE_CATEGORY[c] if c != "P" else OTHER)
    return _CODE_CATEGORY.get(c, OTHER)


def role_flags(title: str | None) -> tuple[bool, bool]:
    t = title or ""
    return bool(CEO_RE.search(t)), bool(CFO_RE.search(t))


@dataclass
class Owner:
    cik: str
    name: str
    is_director: bool
    is_officer: bool
    is_ten_pct: bool
    officer_title: str | None
    is_ceo: bool
    is_cfo: bool


@dataclass
class Transaction:
    line_key: str
    is_derivative: bool
    security_title: str | None
    trans_date: date | None
    trans_code: str | None
    acq_disp: str | None
    shares: float | None
    price: float | None
    shares_after: float | None
    direct_indirect: str | None
    footnote_ids: list[str]
    category: str
    is_10b5_1: bool = False

    @property
    def value(self) -> float | None:
        if self.shares is None or self.price is None:
            return None
        return round(self.shares * self.price, 2)


@dataclass
class Form4:
    document_type: str
    issuer_cik: str
    issuer_name: str
    ticker: str | None
    owners: list[Owner]
    transactions: list[Transaction] = field(default_factory=list)
    aff10b5one: bool = False
    footnotes: dict[str, str] = field(default_factory=dict)

    @property
    def primary_owner(self) -> Owner:
        return self.owners[0]


def _t(el, path: str) -> str | None:
    found = el.find(path)
    if found is None:
        return None
    return (found.text or "").strip() or None


def _val(el, path: str) -> str | None:
    return _t(el, f"{path}/value") or _t(el, path)


def parse_form4(xml: str) -> Form4:
    root = etree.fromstring(strip_ns(xml).encode("utf-8"), parser=etree.XMLParser(recover=True, huge_tree=True))
    issuer = root.find("issuer")
    owners: list[Owner] = []
    for ro in root.findall("reportingOwner"):
        rel = ro.find("reportingOwnerRelationship")
        title = _t(rel, "officerTitle") if rel is not None else None
        other = _t(rel, "otherText") if rel is not None else None
        is_ceo, is_cfo = role_flags(" ".join(x for x in (title, other) if x))
        owners.append(Owner(
            cik=(_t(ro, "reportingOwnerId/rptOwnerCik") or "").zfill(10),
            name=_t(ro, "reportingOwnerId/rptOwnerName") or "",
            is_director=to_bool(_t(rel, "isDirector")) if rel is not None else False,
            is_officer=to_bool(_t(rel, "isOfficer")) if rel is not None else False,
            is_ten_pct=to_bool(_t(rel, "isTenPercentOwner")) if rel is not None else False,
            officer_title=title,
            is_ceo=is_ceo,
            is_cfo=is_cfo,
        ))
    footnotes = {}
    for fn in root.findall("footnotes/footnote"):
        footnotes[fn.get("id", "")] = " ".join("".join(fn.itertext()).split())

    doc = Form4(
        document_type=_t(root, "documentType") or "",
        issuer_cik=(_t(issuer, "issuerCik") or "").zfill(10),
        issuer_name=_t(issuer, "issuerName") or "",
        ticker=((_t(issuer, "issuerTradingSymbol") or "").upper() or None),
        owners=owners,
        aff10b5one=to_bool(_t(root, "aff10b5One")),
        footnotes=footnotes,
    )
    remarks = _t(root, "remarks") or ""

    for table, is_deriv, tag, prefix in (
        ("nonDerivativeTable", False, "nonDerivativeTransaction", "N"),
        ("derivativeTable", True, "derivativeTransaction", "D"),
    ):
        tbl = root.find(table)
        if tbl is None:
            continue
        for i, tx in enumerate(tbl.findall(tag)):
            code = _t(tx, "transactionCoding/transactionCode")
            fids = [f.get("id") for f in tx.iter("footnoteId") if f.get("id")]
            t = Transaction(
                line_key=f"{prefix}{i}",
                is_derivative=is_deriv,
                security_title=_val(tx, "securityTitle"),
                trans_date=to_date(_val(tx, "transactionDate")),
                trans_code=(code or "").upper() or None,
                acq_disp=_val(tx, "transactionAmounts/transactionAcquiredDisposedCode"),
                shares=to_float(_val(tx, "transactionAmounts/transactionShares")),
                price=to_float(_val(tx, "transactionAmounts/transactionPricePerShare")),
                shares_after=to_float(_val(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction")),
                direct_indirect=_val(tx, "ownershipNature/directOrIndirectOwnership"),
                footnote_ids=fids,
                category=classify(code, is_deriv),
            )
            # 10b5-1: the checkbox (2023+ forms) or an explicit plan reference in the
            # transaction's footnotes / remarks.
            txt = " ".join(footnotes.get(f, "") for f in fids) + " " + remarks
            t.is_10b5_1 = bool(doc.aff10b5one or PLAN_RE.search(txt))
            doc.transactions.append(t)
    return doc
