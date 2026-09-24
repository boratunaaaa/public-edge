"""House Periodic Transaction Report (PTR) PDF text parser (official Clerk e-filed PDFs)."""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date

from app.parsers.sec_common import to_date

TX_TYPES = {"P": "PURCHASE", "S": "SALE", "S (PARTIAL)": "SALE_PARTIAL", "E": "EXCHANGE"}
OWNER_CODES = {"SP": "spouse", "JT": "joint", "DC": "dependent"}

_REC_SPLIT = re.compile(r"^\s*F\s+S\s+:.*$", re.M)  # "Filing Status: New" rendered with dropped glyphs
_TX_RE = re.compile(r"\s(P|S \(partial\)|S|E)\s+(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s*(.*)$", re.S | re.I)
_AMT_RE = re.compile(r"\$([\d,]+)")
_TICKER_RE = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,7})\)")
_TYPE_RE = re.compile(r"\[([A-Z]{2})\]")


@dataclass
class PtrLine:
    line_no: int
    owner: str
    asset_name: str
    asset_type: str | None
    ticker: str | None
    transaction_type: str
    transaction_date: date | None
    notification_date: date | None
    amount_min: float | None
    amount_max: float | None
    amount_range: str


@dataclass
class Ptr:
    name: str | None
    state_district: str | None
    lines: list[PtrLine]
    text_ok: bool


def pdf_text(pdf_bytes: bytes) -> str:
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages)


def parse_ptr_text(text: str) -> Ptr:
    # The Clerk's PDFs render some label glyphs as NUL characters.
    text = text.replace("\x00", " ")
    name = re.search(r"Name:\s*(.+)", text)
    sd = re.search(r"State/District:\s*([A-Z]{2}\d{0,2})", text)
    # Body = after the column header, before the asset-type footnote.
    start = re.search(r"\$200\?", text)
    end = re.search(r"\*\s*For the complete list of asset type", text)
    body = text[start.end() if start else 0: end.start() if end else len(text)]
    chunks = _REC_SPLIT.split(body)
    lines: list[PtrLine] = []
    for chunk in chunks:
        # drop sub-lines: "Subholding Of:", "Description:", "Comments:" (glyph-dropped labels)
        keep = [ln for ln in chunk.splitlines() if ln.strip() and not re.match(r"^\s*(S\s+O\s*:|D\s+:|C\s+:|L\s+:)", ln)]
        flat = " ".join(" ".join(keep).split())
        if not flat:
            continue
        m = _TX_RE.search(" " + flat)
        if not m:
            continue
        before = (" " + flat)[: m.start()].strip()
        after = m.group(4)
        amounts = [float(a.replace(",", "")) for a in _AMT_RE.findall(after)]
        asset_rest = _AMT_RE.sub(" ", after).replace(" - ", " ").replace("Over", " ")
        owner = "self"
        tok = before.split(" ", 1)
        if tok and tok[0] in OWNER_CODES:
            owner = OWNER_CODES[tok[0]]
            before = tok[1] if len(tok) > 1 else ""
        asset = " ".join((before + " " + asset_rest).split()).strip(" -")
        asset = re.sub(r"\s+-\s*$", "", asset)
        tk = _TICKER_RE.search(asset)
        ty = _TYPE_RE.search(asset)
        ttype = TX_TYPES.get(m.group(1).upper(), m.group(1).upper())
        amin = amounts[0] if amounts else None
        amax = amounts[1] if len(amounts) > 1 else (None if "Over" not in after else None)
        rng = "Over $" + f"{amin:,.0f}" if ("Over" in after and len(amounts) == 1) else (
            f"${amin:,.0f} - ${amax:,.0f}" if amin is not None and amax is not None else "")
        lines.append(PtrLine(
            line_no=len(lines), owner=owner, asset_name=asset,
            asset_type=ty.group(1) if ty else None, ticker=tk.group(1) if tk else None,
            transaction_type=ttype, transaction_date=to_date(m.group(2)), notification_date=to_date(m.group(3)),
            amount_min=amin, amount_max=amax, amount_range=rng,
        ))
    return Ptr(name=name.group(1).strip() if name else None, state_district=sd.group(1) if sd else None,
               lines=lines, text_ok=bool(start))
