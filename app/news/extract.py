"""Entity / ticker / money extraction from press releases and headlines (deterministic)."""
from __future__ import annotations

import html
import re

EXCHANGES = r"(?:NASDAQ|Nasdaq|NASDAQ\s*GS|NASDAQ\s*GM|NASDAQ\s*CM|NYSE|NYSE\s*American|NYSE\s*Arca|NYSE\s*MKT|Cboe|CBOE|OTCQX|OTCQB|TSX|TSXV)"
_TICKER_RE = re.compile(rf"\(\s*{EXCHANGES}\s*:\s*([A-Z][A-Z0-9.\-]{{0,6}})\s*(?:[;,)][^)]*)?\)")
_TICKER_RE2 = re.compile(rf"\b{EXCHANGES}\s*:\s*([A-Z][A-Z0-9.\-]{{0,6}})\b")

_SCALE = {"thousand": 1e3, "k": 1e3, "million": 1e6, "mm": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9, "b": 1e9}
_MONEY_RE = re.compile(r"(?:US)?\$\s?([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+(?:\.[0-9]+)?)\s*(thousand|million|billion|mm|bn|[mMbBkK])?\b")


def clean_text(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(html.unescape(s))
    return " ".join(s.split())


def tickers_in(text: str) -> list[str]:
    out: list[str] = []
    for rx in (_TICKER_RE, _TICKER_RE2):
        for m in rx.finditer(text or ""):
            t = m.group(1).upper().rstrip(".")
            if t not in out:
                out.append(t)
    return out


def money_values(text: str) -> list[float]:
    vals = []
    for m in _MONEY_RE.finditer(text or ""):
        num = float(m.group(1).replace(",", ""))
        scale = _SCALE.get((m.group(2) or "").lower(), 1.0)
        vals.append(num * scale)
    return vals


_CONTRACT_CTX = re.compile(
    r"(contract|agreement|order|award|purchase|supply|deal|backlog|task order|grant|funding|IDIQ|offtake)", re.I)
_FINANCING = re.compile(r"\b(loan|notes?|offering|financing|credit facility|debt|debenture|private placement|"
                        r"repurchase|buyback|dividend|warrants?|shares|equity line|revolv\w+)\b", re.I)


def contract_value(text: str) -> float | None:
    """Largest dollar amount in a sentence with commercial-contract language (financing excluded)."""
    best = None
    for sent in re.split(r"(?<=[.!?])\s+", text or ""):
        if not _CONTRACT_CTX.search(sent) or _FINANCING.search(sent):
            continue
        # revenue/earnings figures are not contract values
        if re.search(r"\b(revenue|net income|net loss|EPS|earnings per share|quarter|fiscal)\b", sent, re.I) and \
                not re.search(r"\b(contract|award|order)\b", sent, re.I):
            continue
        for v in money_values(sent):
            if v >= 1e5 and (best is None or v > best):
                best = v
    return best
