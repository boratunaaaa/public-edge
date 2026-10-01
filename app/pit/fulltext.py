"""Reader of the EDGAR full-text store written by app.jobs.fulltext_backfill (files, never the database).

scan_status says whether [start, end] is covered for a key by shards that tile it without a gap and are all `done`
(a `truncated` or `failed` shard, or a hole, makes it incomplete). load_hits returns the documents of given issuers."""
from __future__ import annotations

import gzip
import json
from datetime import date, timedelta
from pathlib import Path


def _manifests(store: Path, key: str) -> list[dict]:
    return sorted((json.loads(p.read_text()) for p in (store / "manifest" / key).glob("*.json")), key=lambda m: m["start"])


def scan_status(store: Path, keys: list[str], start: date, end: date) -> dict:
    """{"done": bool, "truncated": bool, "keys": {key: why-not or "ok"}} for [start, end]."""
    out, ok_all, trunc = {}, True, False
    for key in keys:
        ms = [m for m in _manifests(store, key) if date.fromisoformat(m["end"]) >= start and date.fromisoformat(m["start"]) <= end]
        why = "ok"
        cur = start
        for m in ms:
            s, e = date.fromisoformat(m["start"]), date.fromisoformat(m["end"])
            if s > cur:
                why = f"gap {cur}..{s - timedelta(days=1)}"
                break
            if m["status"] != "done":
                why = f"{m['status']} shard {m['start']}..{m['end']}"
                trunc = trunc or m["status"] == "truncated"
                break
            cur = max(cur, e + timedelta(days=1))
        if why == "ok" and cur <= end:
            why = f"not scanned {cur}..{end}"
        out[key] = why
        ok_all = ok_all and why == "ok"
    return {"done": ok_all, "truncated": trunc or not ok_all, "keys": out}


def load_hits(store: Path, keys: list[str], ciks: list[str], start: date | None = None,
              end: date | None = None) -> dict[str, list[dict]]:
    """cik -> [{risk_key, adsh, cik, form, file_date, filename}] (EFTS search-hit shape), oldest first."""
    want = set(ciks)
    out: dict[str, list[dict]] = {c: [] for c in ciks}
    for key in keys:
        for p in sorted((store / "hits" / key).glob("*.jsonl.gz")):
            with gzip.open(p, "rt") as fh:
                for line in fh:
                    d = json.loads(line)
                    fd = d.get("file_date")
                    if fd and ((start and fd < start.isoformat()) or (end and fd > end.isoformat())):
                        continue
                    for c in d["ciks"]:
                        if c in want:
                            out[c].append({"risk_key": key, "adsh": d["adsh"], "cik": c, "form": d["form"],
                                           "file_date": fd, "filename": d["filename"]})
    for c in out:
        out[c].sort(key=lambda h: (h["file_date"] or "", h["adsh"], h["risk_key"], h["filename"]))
    return out
