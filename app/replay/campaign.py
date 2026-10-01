"""ReplayCampaign: many replay windows (start dates x lengths x regimes), low concurrency, resource-guarded, with
development / validation / holdout roles and a purge gap. V1 runs sequentially by default; nothing in production is
touched (each run has its own schema in the replay database).

Holdout discipline: holdout runs are SEALED (replay_meta.replay_runs.sealed) and tuning_results() never returns them,
so a holdout result cannot become tuning input."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import text

from app.replay.config import ReplayConfig
from app.replay.store import META_SCHEMA

DEFAULT_CONCURRENCY = 1
MAX_CONCURRENCY = 2  # CX33 (4 vCPU / 8 GB) also runs the live workload


@dataclass(frozen=True)
class Split:
    """Chronological roles with a purge gap between them (no window may straddle a boundary)."""
    development_end: datetime
    validation_end: datetime
    purge_gap_days: int = 30

    def role(self, start: datetime, end: datetime) -> str | None:
        gap = timedelta(days=self.purge_gap_days)
        if end <= self.development_end:
            return "development"
        if start >= self.development_end + gap and end <= self.validation_end:
            return "validation"
        if start >= self.validation_end + gap:
            return "holdout"
        return None  # inside a purge gap or straddling a boundary: not scheduled


def windows(starts: list[datetime], months: tuple[int, ...] = (3, 6), *, split: Split, prefix: str = "c",
            **kw) -> list[ReplayConfig]:
    out = []
    for s in sorted(starts):
        for m in months:
            e = s + timedelta(days=round(30.44 * m))
            role = split.role(s, e)
            if role is None:
                continue
            out.append(ReplayConfig(replay_id=f"{prefix}_{s:%Y%m%d}_{m}m", start=s, end=e, period_role=role,
                                    purge_gap_days=split.purge_gap_days, **kw))
    return out


def resources_ok(max_load_per_cpu: float = 0.6, min_mem_mb: int = 1500) -> tuple[bool, str]:
    """Only start a run when the host is quiet (the live pipeline has priority)."""
    try:
        load = os.getloadavg()[0] / (os.cpu_count() or 1)
    except OSError:
        load = 0.0
    mem = None
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    mem = int(line.split()[1]) // 1024
    except OSError:
        pass
    if load > max_load_per_cpu:
        return False, f"load {load:.2f}/cpu"
    if mem is not None and mem < min_mem_mb:
        return False, f"{mem} MB available"
    return True, "ok"


@dataclass
class ReplayCampaign:
    name: str
    configs: list[ReplayConfig]
    database_url: str
    concurrency: int = DEFAULT_CONCURRENCY
    results: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not 1 <= self.concurrency <= MAX_CONCURRENCY:
            raise ValueError(f"concurrency must be 1..{MAX_CONCURRENCY} (V1 default {DEFAULT_CONCURRENCY})")
        if self.concurrency > 1:
            raise NotImplementedError("V1 runs campaigns sequentially; parallel runs are a later step")

    def run(self, source_for, *, wait_s: float = 60.0, max_wait_s: float = 3600.0) -> list[dict]:
        from app.replay.engine import ReplayRun
        for cfg in self.configs:
            waited = 0.0
            while True:
                ok, why = resources_ok()
                if ok:
                    break
                if waited >= max_wait_s:
                    raise RuntimeError(f"host busy for {max_wait_s:.0f}s ({why}); campaign paused")
                time.sleep(wait_s)
                waited += wait_s
            res = ReplayRun(cfg, source_for(cfg), self.database_url).run()
            self.results.append({"replay_id": cfg.replay_id, "role": cfg.period_role, "status": res.status})
        return self.results


def tuning_results(conn) -> list[dict]:
    """Development + validation results only; sealed holdout runs are never tuning input."""
    rows = conn.execute(text(f"""SELECT replay_id, period_role, status, result FROM {META_SCHEMA}.replay_runs
                                 WHERE NOT sealed AND period_role <> 'holdout' AND status = 'COMPLETED'
                                 ORDER BY replay_id""")).mappings().all()
    return [dict(r) for r in rows]


def holdout_verdict_inputs(conn) -> dict:
    """Sealed holdout runs for a FINAL verdict: only FULL comparability (risk-label history covers the window).
    LIMITED runs are returned separately and must never be pooled with them as "fully comparable"."""
    rows = conn.execute(text(f"""SELECT replay_id, comparability, status, result FROM {META_SCHEMA}.replay_runs
                                 WHERE sealed AND period_role = 'holdout' AND status = 'COMPLETED'
                                 ORDER BY replay_id""")).mappings().all()
    return {"full": [dict(r) for r in rows if r["comparability"] == "FULL"],
            # a run on the SEC-only THESIS proxy is reported on its own - never pooled with FULL
            "full_with_proxy_separate": [dict(r) for r in rows if r["comparability"] == "FULL_WITH_PROXY"],
            "limited_excluded": [dict(r) for r in rows if r["comparability"] not in ("FULL", "FULL_WITH_PROXY")]}


def proxy_pairs(conn) -> list[dict]:
    """Completed runs that differ ONLY in thesis_proxy (same comparison key and input snapshot), side by side: what the
    SEC-only THESIS proxy changes. A run without its counterpart is not listed."""
    rows = conn.execute(text(f"""SELECT replay_id, comparability, data_snapshot, result FROM {META_SCHEMA}.replay_runs
                                 WHERE status = 'COMPLETED' ORDER BY replay_id""")).mappings().all()
    groups: dict[tuple, dict] = {}
    for r in rows:
        prov = (r["result"] or {}).get("provenance") or {}
        key = (prov.get("comparison_key"), r["data_snapshot"])
        if key[0] is None:
            continue
        conf = prov.get("config") or {}
        on = [f for f, k in (("THESIS_INVALIDATION", "thesis_proxy"), ("GOING_CONCERN", "gc_proxy")) if conf.get(k)]
        side = "with_proxy:" + "+".join(on) if on else "without_proxy"
        groups.setdefault(key, {})[side] = {"replay_id": r["replay_id"], "comparability": r["comparability"],
                                            "metrics": (r["result"] or {}).get("metrics")}
    return [{"comparison_key": k[0], "data_snapshot": k[1], **v} for k, v in sorted(groups.items())
            if "without_proxy" in v and any(s.startswith("with_proxy") for s in v)]
