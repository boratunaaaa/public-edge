"""Press-release headline + lead extraction.

Classification must only see what the release actually announces: the headline and the first
paragraphs. Boilerplate (forward-looking statements, "About X", contacts, inline-XBRL headers)
mentions acquisitions, litigation, going concern etc. for every company and would mislabel news.
"""
from __future__ import annotations

import html
import re

LABEL_RE = re.compile(r"^(ex-?\s?99(\.\d+)?|exhibit\s*99(\.\d+)?|document|for immediate release|press release|news release|"
                      r"media (contact|relations)|investor (contact|relations)|contacts?:|source:|nasdaq:|nyse:|"
                      r"[\w\-]+\.htm|page \d+|\d+)\b", re.I)
CUT_RE = re.compile(r"\b(forward[- ]looking statements|safe harbor|cautionary (note|statement)|about [A-Z][\w&.\- ]{1,60}\b(inc|corp|corporation|ltd|plc|holdings|company|technologies|therapeutics)?|"
                    r"non-gaap financial measures|conference call (details|information)|investor contact|media contact)", re.I)
NOISE_RE = re.compile(r"(iso4217:|xbrli:|us-gaap:|dei:|\b\d{10}\b|\bfalse\b|\btrue\b)", re.I)
# "HOUSTON, September 23, 2026 —" / "(BUSINESS WIRE)--" / "/PRNewswire/ --": start of the body
DATELINE_RE = re.compile(r"(\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.? \d{1,2}, \d{4}|business wire\)|prnewswire|globe newswire|accesswire)", re.I)


def html_lines(raw: str) -> list[str]:
    s = re.sub(r"(?is)<ix:header>.*?</ix:header>", " ", raw or "")
    s = re.sub(r"(?is)<(div|span|p)[^>]*display:\s*none[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|h[1-6]|li|td|b|strong)>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s).replace("\xa0", " ")
    return [" ".join(ln.split()) for ln in s.splitlines() if ln.strip()]


def _is_title(line: str) -> bool:
    if len(line) < 25 or len(line) > 300 or LABEL_RE.match(line):
        return False
    noise = len(NOISE_RE.findall(line))
    return noise <= 1 and sum(c.isalpha() for c in line) > 0.6 * len(line)


LEGAL_DOC_RE = re.compile(r"\b(whereas\b|dated as of|this (\w+ ){0,4}agreement|in witness whereof|now, therefore)", re.I)


def is_legal_document(raw_or_text: str) -> bool:
    """An exhibit that is a contract/indenture, not a press release - never classify its wording as news."""
    return len(LEGAL_DOC_RE.findall(raw_or_text[:20000])) >= 2


def headline_and_lead(raw: str, max_lead: int = 900) -> tuple[str | None, str]:
    lines = html_lines(raw)
    if is_legal_document(" ".join(lines[:200])):
        return None, ""
    head = None
    start = 0
    for i, ln in enumerate(lines[:40]):
        if _is_title(ln):
            head, start = ln, i + 1
            # titles are often broken over several lines/<b> runs: join continuations until the dateline
            while start < len(lines) and len(head) < 260:
                nxt = lines[start]
                if DATELINE_RE.search(nxt[:120]) or len(nxt) > 220 or LABEL_RE.match(nxt) or head.endswith((".", "!", "?")):
                    break
                head = f"{head} {nxt}"
                start += 1
            break
    body = " ".join(ln for ln in lines[start:start + 60] if not NOISE_RE.search(ln) or len(ln) > 120)
    m = CUT_RE.search(body)
    if m and m.start() > 80:
        body = body[:m.start()]
    return head, body[:max_lead]
