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
