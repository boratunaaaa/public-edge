"""EDGAR full-text (EFTS) history of the production risk phrases, for the replay (FINANCING_TEXT / GOING_CONCERN).

    python -m app.jobs.fulltext_backfill --out DIR [--from 2001-01-01] [--to 2026-09-30] [--keys K1,K2] [--apply]
                                         [--max-minutes M]

One query per (phrase, period) covers EVERY company (the replay filters by issuer later), like production's
scan_text_risks. Queries: the production TEXT_RISKS phrases on their production forms, plus FOREIGN_GC - the same
going-concern wording on 20-F / 40-F annual reports (production never scans those forms: the replay's equivalent
going-concern path for foreign issuers, staged only as a proxy).

Storage is FILES, not the production database: DIR/hits/<key>/<start>_<end>.jsonl.gz (one line per matching document:
adsh, ciks, form, file_date, filename) and DIR/manifest/<key>/<start>_<end>.json (query, EFTS total, documents kept,
run id, attempts, status). A shard is written atomically (temp file + rename) and only after EVERY page arrived and the
documents match EFTS's total; otherwise it is `failed` (retried by the next run, given up after MAX_ATTEMPTS) - never a
silent partial. A period whose total reaches EFTS's result window is split in halves until it fits; a single day that
still does not fit is stored `truncated` (coverage then PARTIAL). Periods are split until ONE page holds them: EFTS page
order is not stable (fetch_period). Re-runs skip `done` shards (idempotent, resumable).

Default is a DRY RUN: one request per (key, year) for the totals, nothing written. --apply needs PE_SHARED_RATELIMIT=1
(one SEC budget with the live workers); the job stops cleanly under MIN_FREE_GB or at --max-minutes."""
from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app import http

EFTS_WINDOW = 10_000  # EFTS (Elasticsearch) result window: `from` cannot reach past it
PAGE = 100  # EFTS page size: a period is split until its total fits one page
DAY_PASSES = 20  # a single day beyond one page: page it again until the union reaches the total (stops when it does)
PAGE_RETRIES = 3  # on top of the HTTP client's own retries: EFTS answers bursts of 500s
MIN_FREE_GB = 8.0
MAX_ATTEMPTS = 3
GC_WORDING = ("substantial doubt about our ability to continue as a going concern",
              "substantial doubt about the Company's ability to continue as a going concern",
              "substantial doubt about the Group's ability to continue as a going concern")
FOREIGN_ANNUAL = ["20-F", "40-F"]


def queries() -> dict[str, tuple[str, list[str]]]:
    """key -> (exact phrase, forms): production TEXT_RISKS unchanged + the foreign annual-report going-concern lane."""
    from app.discovery.risks import TEXT_RISKS
    q = {k: (phrase, list(forms)) for k, (phrase, forms, _sev) in TEXT_RISKS.items()}
    for i, phrase in enumerate(GC_WORDING):
        q[f"FOREIGN_GC_{i}"] = (phrase, list(FOREIGN_ANNUAL))
    return q


def _page(phrase: str, forms: list[str], start: date, end: date, frm: int) -> dict:
    from app.discovery.sec_data import EFTS_URL
    return http.get(EFTS_URL, params={"q": f'"{phrase}"', "forms": ",".join(forms), "dateRange": "custom",
                                      "startdt": start.isoformat(), "enddt": end.isoformat(), "from": frm},
                    retries=3).json()


def _get(phrase: str, forms: list[str], start: date, end: date, frm: int) -> dict:
    """_page with a few extra, slower attempts on a server error: one stubborn leaf must not fail a whole year."""
    for i in range(PAGE_RETRIES + 1):
        try:
            return _page(phrase, forms, start, end, frm)
        except http.HttpError as e:
            if i == PAGE_RETRIES or not (e.status is None or e.status >= 500):
                raise
            time.sleep(5 * (i + 1))
    raise AssertionError("unreachable")


def _total(r: dict) -> tuple[int, str]:
    t = (r.get("hits") or {}).get("total") or {}
    return int(t.get("value") or 0), t.get("relation") or "eq"


def _docs(r: dict) -> list[dict]:
    out = []
    for h in (r.get("hits") or {}).get("hits") or []:
        s = h.get("_source") or {}
        adsh, _, fn = (h.get("_id") or "").partition(":")
        out.append({"adsh": adsh or s.get("adsh"), "filename": fn, "form": s.get("form") or s.get("file_type"),
                    "file_date": s.get("file_date"), "ciks": sorted({c.zfill(10) for c in s.get("ciks") or []})})
    return out


def fetch_period(phrase: str, forms: list[str], start: date, end: date) -> dict:
    """Every matching document of [start, end]: {"status": done | truncated, "total": n, "docs": [...], "leaves": k}.

    EFTS pages are ordered by relevance with ties, and the order is NOT stable between requests (measured: 21 of 5,886
    documents shifted across pages of one query). So a period is split in halves until its total fits ONE page - no
    paging, no shifting; only a single day with more than a page is paged, up to DAY_PASSES times, until the union equals the total.
    Raises when it does not (the period is then `failed`, never a silent partial)."""
    first = _get(phrase, forms, start, end, 0)
    total, rel = _total(first)
    if (total > PAGE or rel != "eq") and start == end and len(forms) > 1:
        # a busy single day (e.g. the NT 10-K deadline): one form at a time first - smaller results, fewer ties
        parts = [fetch_period(phrase, [f], start, end) for f in forms]
        return {"status": "truncated" if any(p["status"] == "truncated" for p in parts) else "done",
                "total": sum(p["total"] for p in parts), "relation": "eq",
                "docs": [d for p in parts for d in p["docs"]], "leaves": sum(p["leaves"] for p in parts)}
    if (total > PAGE or rel != "eq") and start < end:
        mid = start + (end - start) // 2
        a, b = fetch_period(phrase, forms, start, mid), fetch_period(phrase, forms, mid + timedelta(days=1), end)
        return {"status": "truncated" if "truncated" in (a["status"], b["status"]) else "done",
                "total": a["total"] + b["total"], "relation": "eq" if rel == "eq" else rel, "docs": a["docs"] + b["docs"],
                "leaves": a["leaves"] + b["leaves"]}
    uniq = {(d["adsh"], d["filename"]): d for d in _docs(first)}
    passes = 0
    while total > PAGE and len(uniq) < min(total, EFTS_WINDOW) and passes < DAY_PASSES:  # one day beyond a page
        passes += 1
        frm = 0  # every pass pages the whole day again: each page request may come in another order
        while frm < min(total, EFTS_WINDOW):
            got = _docs(_get(phrase, forms, start, end, frm))
            if not got:
                break
            uniq.update({(d["adsh"], d["filename"]): d for d in got})
            frm += PAGE
    if rel != "eq" or total >= EFTS_WINDOW:
        return {"status": "truncated", "total": total, "relation": rel, "docs": list(uniq.values()), "leaves": 1}
    if len(uniq) != total:  # a page went missing or EFTS shifted under us: never a silent partial
        raise RuntimeError(f"{start}..{end}: {len(uniq)} documents for a total of {total}")
    return {"status": "done", "total": total, "relation": rel, "docs": list(uniq.values()), "leaves": 1}


# ------------------------------------------------------------------ store

def _name(start: date, end: date) -> str:
    return f"{start.isoformat()}_{end.isoformat()}"


def _atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{uuid.uuid4().hex[:6]}")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def write_shard(out: Path, key: str, phrase: str, forms: list[str], start: date, end: date, res: dict, run_id: str,
                attempts: int) -> None:
    lines = "".join(json.dumps(d, sort_keys=True) + "\n" for d in sorted(res["docs"], key=lambda d: (d["adsh"], d["filename"])))
    _atomic(out / "hits" / key / f"{_name(start, end)}.jsonl.gz", gzip.compress(lines.encode(), mtime=0))
    man = {"key": key, "phrase": phrase, "forms": forms, "start": start.isoformat(), "end": end.isoformat(),
           "status": res["status"], "total": res["total"], "relation": res["relation"], "docs": len(res["docs"]),
           "leaves": res.get("leaves", 1),
           "run_id": run_id, "attempts": attempts, "fetched_at": datetime.now(timezone.utc).isoformat()}
    _atomic(out / "manifest" / key / f"{_name(start, end)}.json", json.dumps(man, sort_keys=True).encode())


def read_manifests(out: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((out / "manifest").glob("*/*.json"))]


def _period_state(out: Path, key: str, start: date, end: date) -> dict | None:
    p = out / "manifest" / key / f"{_name(start, end)}.json"
    return json.loads(p.read_text()) if p.exists() else None


def plan(start: date, end: date) -> list[tuple[date, date]]:
    """Calendar years (clipped to [start, end])."""
    out, y = [], start.year
    while y <= end.year:
        out.append((max(start, date(y, 1, 1)), min(end, date(y, 12, 31))))
        y += 1
    return out


def free_gb(path: str = "/") -> float:
    return shutil.disk_usage(path).free / 1024 ** 3


def record_failure(out: Path, key: str, phrase: str, forms: list[str], start: date, end: date, err: str, run_id: str,
                   attempts: int) -> None:
    man = {"key": key, "phrase": phrase, "forms": forms, "start": start.isoformat(), "end": end.isoformat(),
           "status": "failed", "error": err[:300], "run_id": run_id, "attempts": attempts,
           "fetched_at": datetime.now(timezone.utc).isoformat()}
    _atomic(out / "manifest" / key / f"{_name(start, end)}.json", json.dumps(man, sort_keys=True).encode())


def run(out: Path, start: date, end: date, *, keys: list[str] | None = None, apply: bool = False,
        max_minutes: float | None = None, min_free_gb: float = MIN_FREE_GB) -> dict:
    q = queries()
    keys = keys or sorted(q)
    run_id = f"efts-{datetime.now(timezone.utc):%Y%m%dT%H%M}-{uuid.uuid4().hex[:4]}"
    t0 = time.time()
    res = {"mode": "APPLY" if apply else "DRY_RUN", "run_id": run_id, "periods": 0, "done": 0, "skipped_done": 0,
           "truncated": 0, "failed": 0, "gave_up": 0, "documents": 0, "requests_estimate": 0, "errors": {}}
    for key in keys:
        phrase, forms = q[key]
        for s, e in plan(start, end):
            res["periods"] += 1
            if not apply:
                total, _rel = _total(_page(phrase, forms, s, e, 0))
                res["documents"] += total
                res["requests_estimate"] += max(1, 2 * -(-total // PAGE))  # binary split to one-page leaves
                continue
            st = _period_state(out, key, s, e)
            if st and st["status"] in ("done", "truncated"):
                res["skipped_done"] += 1
                continue
            attempts = (st or {}).get("attempts", 0) + 1 if st else 1
            if st and st["status"] == "failed" and st.get("attempts", 0) >= MAX_ATTEMPTS:
                res["gave_up"] += 1
                continue
            if free_gb() < min_free_gb:
                res["stopped"] = f"free disk {free_gb():.1f} GB < {min_free_gb} GB"
                return _finish(res, t0)
            if max_minutes and (time.time() - t0) / 60 >= max_minutes:
                res["stopped"] = f"time budget {max_minutes} min reached"
                return _finish(res, t0)
            try:
                r = fetch_period(phrase, forms, s, e)
            except Exception as ex:  # noqa: BLE001 - one period never stops the job
                res["failed"] += 1
                if len(res["errors"]) < 20:
                    res["errors"][f"{key}:{s}"] = f"{type(ex).__name__}: {ex}"[:200]
                record_failure(out, key, phrase, forms, s, e, f"{type(ex).__name__}: {ex}", run_id, attempts)
                continue
            write_shard(out, key, phrase, forms, s, e, r, run_id, attempts)
            res["documents"] += len(r["docs"])
            res["truncated" if r["status"] == "truncated" else "done"] += 1
    return _finish(res, t0)


def _finish(res: dict, t0: float) -> dict:
    res["seconds"] = round(time.time() - t0, 1)
    res["free_gb"] = round(free_gb(), 1)
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--from", dest="start", default="2001-01-01")
    ap.add_argument("--to", dest="end", default=(date.today() - timedelta(days=1)).isoformat())
    ap.add_argument("--keys")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--max-minutes", type=float)
    a = ap.parse_args()
    if a.apply and os.environ.get("PE_SHARED_RATELIMIT") != "1":
        ap.error("--apply needs PE_SHARED_RATELIMIT=1 (one SEC budget shared with the live workers)")
    keys = [k.strip() for k in a.keys.split(",")] if a.keys else None
    print(json.dumps(run(Path(a.out), date.fromisoformat(a.start), date.fromisoformat(a.end), keys=keys, apply=a.apply,
                         max_minutes=a.max_minutes), indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
