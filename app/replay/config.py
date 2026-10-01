"""Replay run parameters (reproducible: the config hash + code commit + input snapshot id identify a result)."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PERIOD_ROLES = ("development", "validation", "holdout")
STEP_MODELS = ("SESSION_3STEP",)  # PRE_OPEN 09:00 ET signal | OPEN 09:30 execution | CLOSE 16:00 ET + EOD lag
EXECUTION_MODEL_VERSION = "paper.fills.v3-prior-session-adv+ledger.retry-within-validity"
SIGNAL_VERSION = "paper.proposals.gate.v2.1-adv-any-cap"
_ID = re.compile(r"^[a-z0-9_]{1,40}$")


@dataclass(frozen=True)
class ReplayConfig:
    replay_id: str
    start: datetime
    end: datetime
    market: str = "US"
    event_types: tuple[str, ...] = ("INSIDER_OPEN_MARKET_BUY",)
    initial_cash: float = 10_000.0
    step: str = "SESSION_3STEP"
    period_role: str = "development"
    purge_gap_days: int = 0
    seed: int = 0
    signal_version: str = SIGNAL_VERSION
    execution_model_version: str = EXECUTION_MODEL_VERSION
    # stage the SEC-only THESIS_INVALIDATION proxy (risk_history.THESIS_PROXY) as thesis_changes: the run's best
    # comparability is then FULL_WITH_PROXY, never FULL. Off by default; the same snapshot replays both ways
    thesis_proxy: bool = False
    # stage the full-text going-concern proxy (risk_history.GC_PROXY) for annual reports no current-classifier profile
    # decides - every 20-F / 40-F and unprofiled 10-Ks: GOING_CONCERN is then reported under proxy (FULL_WITH_PROXY)
    gc_proxy: bool = False
    notes: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _ID.match(self.replay_id):
            raise ValueError("replay_id: lower-case letters, digits and _ only (it names the run's schema)")
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("start / end must be timezone-aware")
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if self.market != "US":
            raise NotImplementedError("V1 replays the US market only (TR / HK / IN are not implemented)")
        if self.period_role not in PERIOD_ROLES:
            raise ValueError(f"period_role must be one of {PERIOD_ROLES}")
        if self.step not in STEP_MODELS:
            raise ValueError(f"step must be one of {STEP_MODELS}")

    @property
    def schema(self) -> str:
        return f"replay_{self.replay_id}"

    def as_dict(self) -> dict:
        d = asdict(self)
        d["start"], d["end"] = self.start.astimezone(timezone.utc).isoformat(), self.end.astimezone(timezone.utc).isoformat()
        d["event_types"] = list(self.event_types)
        return d

    def config_hash(self) -> str:
        """Hash of everything that can change a result (the replay_id / notes only name the run)."""
        d = {k: v for k, v in self.as_dict().items() if k not in ("replay_id", "notes")}
        for k in ("thesis_proxy", "gc_proxy"):
            if not d.get(k):
                d.pop(k, None)  # runs configured before the option keep their hash
        return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()[:16]

    @property
    def proxies(self) -> frozenset:
        """The risk families whose SEC-only proxy this run stages."""
        return frozenset(f for f, on in (("THESIS_INVALIDATION", self.thesis_proxy), ("GOING_CONCERN", self.gc_proxy)) if on)

    def comparison_key(self) -> str:
        """Equal for runs that differ ONLY in their proxy options: the with / without-proxy pairs (campaign.proxy_pairs)."""
        d = {k: v for k, v in self.as_dict().items() if k not in ("replay_id", "notes", "thesis_proxy", "gc_proxy")}
        return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()[:16]


def code_commit() -> str:
    """git HEAD (+dirty) of the code base; PE_CODE_COMMIT when running from an image without git metadata."""
    import os
    if os.environ.get("PE_CODE_COMMIT"):
        return os.environ["PE_CODE_COMMIT"]
    root = Path(__file__).resolve().parents[2]
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root, capture_output=True,
                               text=True, timeout=10).stdout.strip()
        return (head or "unknown") + ("+dirty" if dirty else "")
    except Exception:  # noqa: BLE001 - a container without git
        return "unknown"
