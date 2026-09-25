"""Story clustering: syndicated / re-published copies of the same news become one story."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

STOP = {"the", "a", "an", "of", "to", "and", "in", "for", "on", "with", "its", "inc", "corp", "announces", "announced",
        "reports", "company", "co", "ltd", "plc", "llc", "at", "by", "from", "as", "is", "be", "new", "nasdaq", "nyse"}
CLUSTER_WINDOW = timedelta(hours=72)
SIM_THRESHOLD = 0.5


def tokens(title: str) -> list[str]:
    t = re.sub(r"[^a-z0-9$%. ]", " ", (title or "").lower())
    return sorted({w.strip(".") for w in t.split() if w.strip(".") and w.strip(".") not in STOP})


def similarity(a: list[str], b: list[str]) -> float:
    """Overlap coefficient - robust when one copy adds a prefix like 'PRESS RELEASE:'."""
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


def same_story(a_tokens: list[str], a_time: datetime, b_tokens: list[str], b_time: datetime,
               threshold: float = SIM_THRESHOLD) -> bool:
    if abs(a_time - b_time) > CLUSTER_WINDOW:
        return False
    # numbers must agree when both mention them ($180M vs $18M are different stories)
    na = {t for t in a_tokens if re.match(r"^\$?\d", t)}
    nb = {t for t in b_tokens if re.match(r"^\$?\d", t)}
    if na and nb and not (na & nb):
        return False
    return similarity(a_tokens, b_tokens) >= threshold


def public_time(items: list[dict]) -> datetime:
    """Earliest time the story was public: min over items of the ORIGINAL publication time, but never
    earlier than the item's own first-seen time minus a sanity bound, and never an 'updated' time.
    Items with no original timestamp contribute their first_seen_at."""
    best = None
    for it in items:
        pub = it.get("original_published_at")
        seen = it["first_seen_at"]
        t = pub if (pub is not None and pub <= seen + timedelta(minutes=5)) else seen  # future-dated feeds ignored
        best = t if best is None or t < best else best
    return best


def choose_primary(items: list[dict]) -> dict:
    """Most authoritative (lowest tier), then earliest."""
    return sorted(items, key=lambda it: (it["source_tier"], it.get("original_published_at") or it["first_seen_at"]))[0]


# ------------------------------------------------------------------ semantic copy detection
# Syndicated copies (Reuters/AP/wire re-posts, aggregator re-titles) keep the same lead paragraph
# while the headline and domain change. Pipeline: exact URL -> canonical URL -> headline similarity
# -> lead-text MinHash similarity -> entity (same ticker) + time window -> source lineage.
import hashlib  # noqa: E402
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse  # noqa: E402

TRACKING = re.compile(r"^(utm_|cmp|cid|ref|src|source|tsrc|ncid|guccounter|guce_|mod|feedtype|rss)", re.I)
N_HASHES = 32
SHINGLE = 4
LEAD_SIM_THRESHOLD = 0.55
LINEAGE = [("SEC", re.compile(r"\bsec\.gov\b|\bform 8-k\b", re.I)),
           ("PRNEWSWIRE", re.compile(r"\bpr ?newswire\b|prnewswire\.com", re.I)),
           ("BUSINESSWIRE", re.compile(r"\bbusiness ?wire\b|businesswire\.com", re.I)),
           ("GLOBENEWSWIRE", re.compile(r"\bglobe ?newswire\b|globenewswire\.com", re.I)),
           ("ACCESSWIRE", re.compile(r"\baccess ?wire\b|accesswire\.com", re.I)),
           ("REUTERS", re.compile(r"\breuters\b", re.I)),
           ("AP", re.compile(r"\bassociated press\b|\(ap\)", re.I))]


def canonical_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        u = urlparse(url.strip())
    except ValueError:
        return url
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=False) if not TRACKING.match(k)]
    path = re.sub(r"/(amp|amp\.html)$", "", u.path.rstrip("/")) or "/"
    host = (u.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    return urlunparse(("https", host, path, "", urlencode(sorted(q)), ""))


def _shingles(textv: str) -> set[str]:
    w = [x for x in re.sub(r"[^a-z0-9$% ]", " ", (textv or "").lower()).split() if x not in STOP]
    if len(w) < SHINGLE:
        return {" ".join(w)} if w else set()
    return {" ".join(w[i:i + SHINGLE]) for i in range(len(w) - SHINGLE + 1)}


def lead_signature(headline: str, lead: str | None) -> list[int] | None:
    """Deterministic MinHash (blake2b with per-slot salt) of the lead paragraph (headline as fallback)."""
    sh = _shingles(lead if lead and len(lead) >= 80 else f"{headline} {lead or ''}")
    if not sh:
        return None
    sig = []
    for i in range(N_HASHES):
        salt = i.to_bytes(2, "little")
        sig.append(min(int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8, salt=salt).digest(), "little") for s in sh))
    return sig


def signature_similarity(a: list[int] | None, b: list[int] | None) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(1 for x, y in zip(a, b) if x == y) / len(a)


def source_lineage(source: str, headline: str, lead: str | None, url: str | None) -> str | None:
    if source == "sec_8k":
        return "SEC"
    blob = f"{headline} {(lead or '')[:400]} {url or ''}"
    for name, rx in LINEAGE:
        if rx.search(blob):
            return name
    return {"news_prnewswire": "PRNEWSWIRE", "news_businesswire": "BUSINESSWIRE"}.get(source)


def is_same_story(a: dict, b: dict) -> bool:
    """a, b: {tokens, time, canonical_url, signature, lineage}."""
    if abs(a["time"] - b["time"]) > CLUSTER_WINDOW:
        return False
    if a.get("canonical_url") and a.get("canonical_url") == b.get("canonical_url"):
        return True
    na = {t for t in a["tokens"] if re.match(r"^\$?\d", t)}
    nb = {t for t in b["tokens"] if re.match(r"^\$?\d", t)}
    if na and nb and not (na & nb):
        return False  # different figures ($180M vs $18M) are different stories
    head = similarity(a["tokens"], b["tokens"])
    if head >= SIM_THRESHOLD:
        return True
    lead = signature_similarity(a.get("signature"), b.get("signature"))
    if lead >= LEAD_SIM_THRESHOLD:
        return True  # rewritten headline, same body
    return bool(a.get("lineage") and a.get("lineage") == b.get("lineage") and head >= 0.35 and lead >= 0.35)


def timestamp_confidence(source: str, published_at: datetime | None, first_seen_at: datetime, *, exact: bool = False,
                         backfill: bool = False, tier: int = 3) -> str:
    """HIGH = authoritative (SEC acceptance) or a wire timestamp confirmed by prompt first sight;
    MEDIUM = publisher timestamp without contradiction; LOW = missing, future-dated or unverifiable."""
    if exact or source == "sec_8k":
        return "HIGH" if published_at is not None else "LOW"
    if published_at is None or published_at > first_seen_at + timedelta(minutes=5):
        return "LOW"
    lag = first_seen_at - published_at
    if not backfill and lag <= timedelta(hours=6):
        return "HIGH" if tier <= 1 else "MEDIUM"
    if tier <= 2:
        return "MEDIUM"
    return "LOW" if lag > timedelta(days=3) else "MEDIUM"


CONF_ORDER = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, None: 0}
