"""ReplayRun: the production paper pipeline walked forward over NYSE sessions at simulated time.

Per session D (step model SESSION_3STEP):
  PRE_OPEN  09:00 ET  signal phase, bars visible up to the previous session. Events with available_at <= now are staged,
                      the point-in-time reference data / universe is refreshed, each new event of the run's families passes
                      the point-in-time identity check and then the UNCHANGED production gate (create_proposal); a passing
                      proposal is approved by the research auto-approval policy (production decide -> queue_buy), so an
                      event public before the open trades at D's open (first_tradable_session).
  OPEN      09:30 ET  execution phase, D's bar visible to the fill engine only: production fill_orders(D).
  CLOSE     16:00 ET + EOD lag  signal phase, bars up to D: events published during / after the session (they enter at
                      D+1's open), production expire, check_exits(D), fill_orders(D) for time exits, snapshot(D).

Production code is reused as is; the replay swaps only the clock (app.core.clock), the price provider
(ReplayPriceProvider), the database (isolated schema) and, for determinism, the proposal code generator."""
from __future__ import annotations

import contextlib
import copy
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select

from app.core.clock import ReplayClock, use_clock
from app.db.models import events
from app.marketdata.calendar import MARKET_CLOSE, NY, is_trading_day
from app.replay.config import ReplayConfig, code_commit
from app.replay.errors import ReplayLeakage
from app.replay.source import SourceData
from app.replay.store import ReplayStore
from app.replay.visibility import (Guards, PriceIndex, ReplayPriceProvider, Stager, Visibility, check_state,
                                   identity_check)

PRE_OPEN, OPEN_T = time(9, 0), time(9, 30)
EOD_LAG = timedelta(hours=2)
RESEARCH_APPROVER = "replay:research_auto"
RISK_COMPLETE, RISK_PARTIAL, RISK_UNAVAILABLE = "RISK_HISTORY_COMPLETE", "RISK_HISTORY_PARTIAL", "RISK_HISTORY_UNAVAILABLE"
# production decisions that read risk-label history (discovery_risks / thesis_changes); without that history they
# silently see "no risk" - the run is then not comparable with a live verdict
RISK_DEPENDENT_GATES = ("gate:SEVERE_FINANCING_RISK", "gate:LOW_LIQUIDITY(risk label LOW_LIQUIDITY=HIGH)",
                        "sizing:HIGH_FINANCING_RISK cap", "exit:D_THESIS thesis invalidation")


def risk_history(cfg: ReplayConfig, src: SourceData) -> dict:
    """Explicit coverage of the risk-label history over the run's window (the replay never invents past labels)."""
    s, e = cfg.start.astimezone(NY).date(), cfg.end.astimezone(NY).date()
    starts = {k: src.risk_coverage.get(k) for k in ("discovery_risks", "thesis_changes")}
    if all(v is not None and v <= s for v in starts.values()):
        flag = RISK_COMPLETE
    elif any(v is not None and v <= e for v in starts.values()):
        flag = RISK_PARTIAL
    else:
        flag = RISK_UNAVAILABLE
    return {"flag": flag, "history_starts": starts,
            "gates_not_applied": [] if flag == RISK_COMPLETE else list(RISK_DEPENDENT_GATES),
            "comparability": "FULL" if flag == RISK_COMPLETE else "LIMITED"}


def sessions(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def _at(d: date, t: time) -> datetime:
    return datetime.combine(d, t, NY).astimezone(timezone.utc)


@dataclass
class RunResult:
    replay_id: str
    status: str
    steps: int = 0
    events_seen: int = 0
    proposals: int = 0
    rejected: dict = field(default_factory=dict)
    reviews: list = field(default_factory=list)
    leakage: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)


@contextlib.contextmanager
def _patched(provider, seed: int):
    """Swap the process-global hooks the reused production code reads, and restore them afterwards: the market data
    provider, the proposal-code generator (deterministic per seed) and the ticker resolver's cache."""
    from app.entities.registry import resolver
    from app.marketdata import provider as mp
    from app.paper import proposals
    saved = (mp.get_provider, proposals._code, copy.copy(resolver.__dict__))
    counter = {"n": 0}

    def code() -> str:
        counter["n"] += 1
        return f"RP{seed % 1000:03d}{counter['n']:05d}"
    mp.get_provider = lambda: provider
    proposals._code = code
    try:
        yield
    finally:
        mp.get_provider, proposals._code = saved[0], saved[1]
        resolver.__dict__.clear()
        resolver.__dict__.update(saved[2])


class ReplayRun:
    def __init__(self, config: ReplayConfig, source: SourceData, database_url: str):
        self.cfg, self.src = config, source
        self.store = ReplayStore(database_url, config.schema)
        self.guards = Guards()
        self.vis = Visibility()
        self.index = PriceIndex(source)
        self.stager = Stager(source, self.guards, self.index)
        self.provider = ReplayPriceProvider(source, self.vis, self.guards, self.index)
        self.risk = risk_history(config, source)
        self.clock = ReplayClock(_at(config.start.astimezone(NY).date(), PRE_OPEN) - timedelta(minutes=1))
        self.result = RunResult(config.replay_id, "CREATED")

    # ------------------------------------------------------------------ helpers
    def _step(self, t: datetime, phase: str, signal_cap: date | None, exec_cap: date | None) -> None:
        self.clock.set(t)
        self.vis.phase, self.vis.signal_cap, self.vis.exec_cap = phase, signal_cap, exec_cap
        self.result.steps += 1

    def _check(self) -> None:
        with self.store.engine.connect() as c:
            check_state(c, self.guards, self.clock.now(), self.clock.today(), self.vis.cap)

    def _refresh(self, today: date, cap: date | None, new_ids: list[int]) -> None:
        from app.entities.registry import resolver
        with self.store.engine.begin() as c:
            self.stager.stage_reference(c, today)
            self.stager.stage_risks(c, self.clock.now())
            ciks = set(c.execute(select(events.c.issuer_cik).where(events.c.id.in_(new_ids))).scalars()) if new_ids else set()
            if ciks and cap is not None:
                self.stager.build_universe(c, today, ciks, cap)
            resolver.load(c, force=True)  # the point-in-time registry of this moment, not today's ticker file

    def _signals(self, today: date, cap: date | None) -> None:
        from app.paper.proposals import create_proposal, decide
        with self.store.engine.begin() as c:
            new = self.stager.stage_events(c, self.clock.now(), self.cfg.event_types)
        self._refresh(today, cap, new)
        self.result.events_seen += len(new)
        for eid in new:
            with self.store.engine.connect() as c:
                ev = dict(c.execute(select(events).where(events.c.id == eid)).mappings().one())
                why = identity_check(c, ev, ev["public_date"])
            if why:
                self.guards.review(eid, why)
                self.result.rejected[why] = self.result.rejected.get(why, 0) + 1
                continue
            r = create_proposal(self.store.engine, eid, research_only=True, now=self.clock.now())
            if not r.get("created"):
                for f in r.get("failed", []):
                    self.result.rejected[f] = self.result.rejected.get(f, 0) + 1
                continue
            self.result.proposals += 1
            decide(self.store.engine, r["code"], True, by=RESEARCH_APPROVER, now=self.clock.now())

    # ------------------------------------------------------------------ run
    def run(self) -> RunResult:
        from app.paper.ledger import STRATEGIES, check_exits, ensure_accounts, fill_orders, snapshot
        from app.paper.proposals import expire
        cfg = self.cfg
        prov = {"config": cfg.as_dict(), "config_hash": cfg.config_hash(), "code_commit": code_commit(),
                "data_snapshot": self.src.snapshot_id(), "data_label": self.src.label, "strategies": list(STRATEGIES),
                "risk_history": self.risk}
        self.result.provenance = prov
        self.store.create()
        self.store.record(replay_id=cfg.replay_id, status="RUNNING", period_role=cfg.period_role, config=prov["config"],
                          config_hash=prov["config_hash"], code_commit=prov["code_commit"], data_snapshot=prov["data_snapshot"],
                          comparability=self.risk["comparability"])
        days = sessions(cfg.start.astimezone(NY).date(), cfg.end.astimezone(NY).date())
        try:
            with use_clock(self.clock), _patched(self.provider, cfg.seed):
                with self.store.engine.begin() as c:
                    ensure_accounts(c, capital=cfg.initial_cash, book="RESEARCH")
                prev = None
                for d in days:
                    # PRE_OPEN: decisions on what was public before the open; no bar of D exists yet
                    self._step(_at(d, PRE_OPEN), "SIGNAL", prev, None)
                    self._signals(d, prev)
                    self._check()
                    # OPEN: the fill engine sees D's bar
                    self._step(_at(d, OPEN_T), "EXECUTION", prev, d)
                    with self.store.engine.begin() as c:
                        fill_orders(c, d)
                    self._check()
                    # CLOSE (+ EOD lag): D's bar is public
                    self._step(_at(d, MARKET_CLOSE) + EOD_LAG, "SIGNAL", d, None)
                    self._signals(d, d)
                    expire(self.store.engine, self.clock.now())
                    with self.store.engine.begin() as c:
                        check_exits(c, d)
                    self.vis.phase, self.vis.exec_cap = "EXECUTION", d  # production run(): time exits fill on D
                    with self.store.engine.begin() as c:
                        fill_orders(c, d)
                        snapshot(c, d)
                    self.vis.phase, self.vis.exec_cap = "SIGNAL", None
                    self._check()
                    prev = d
            self.result.status = "COMPLETED"
        except ReplayLeakage:
            self.result.status = "FAILED_LEAKAGE"
        self.result.leakage = list(self.guards.log)
        self.result.reviews = list(self.guards.reviews)
        if self.result.status == "COMPLETED":
            from app.replay.metrics import collect
            self.result.metrics = collect(self.store.engine, self.src, days[-1] if days else None)
        self.store.record(replay_id=cfg.replay_id, status=self.result.status, period_role=cfg.period_role,
                          config=prov["config"], config_hash=prov["config_hash"], code_commit=prov["code_commit"],
                          data_snapshot=prov["data_snapshot"], result=self.summary(), leakage={"log": self.result.leakage},
                          finished=True, comparability=self.risk["comparability"])
        return self.result

    def summary(self) -> dict:
        r = self.result
        return {"status": r.status, "steps": r.steps, "events_seen": r.events_seen, "proposals": r.proposals,
                "rejected": r.rejected, "reviews": len(r.reviews), "risk_history": self.risk,
                "reference_writes": self.stager.writes, "metrics": r.metrics, "provenance": r.provenance}
