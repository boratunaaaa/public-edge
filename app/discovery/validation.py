"""Discovery precision validation dataset.

A reproducible random sample (seeded by sample_id) of 50 HIGH / 100 MEDIUM / 100 LOW companies with
a frozen evidence snapshot. Each row gets a label and reason codes from a reviewer; precision is
estimated only from labelled rows and reported with the labeler, so model-assisted labels are
never mistaken for independent human ground truth.
"""
from __future__ import annotations

import hashlib
import random
from datetime import datetime, timezone

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine

from app.db.models import discovery_companies, discovery_evidence, discovery_risks, discovery_validation

LABELS = ("TRUE_DISCOVERY", "PLAUSIBLE", "FALSE_POSITIVE", "INSUFFICIENT_DATA")
REASONS = ("SCALED_COMPANY", "NO_REAL_COMMERCIAL_EVIDENCE", "PR_HYPE", "WEAK_BOTTLENECK", "FINANCING_RISK",
           "GENUINE_EARLY_COMMERCIAL", "REAL_SUPPLY_CONSTRAINT", "STRONG_CUSTOMER_SIGNAL", "OTHER")
DEFAULT_SIZES = {"HIGH": 50, "MEDIUM": 100, "LOW": 100}


def _seed(sample_id: str) -> int:
    return int(hashlib.sha256(sample_id.encode()).hexdigest()[:12], 16)


def snapshot(conn: Connection, row: dict) -> dict:
    ev = conn.execute(select(discovery_evidence.c.kind, discovery_evidence.c.label, discovery_evidence.c.relation,
                             discovery_evidence.c.agreement, discovery_evidence.c.counterparty, discovery_evidence.c.hedged,
                             discovery_evidence.c.sentence, discovery_evidence.c.form, discovery_evidence.c.filed_date,
                             discovery_evidence.c.source_url)
                      .where(discovery_evidence.c.cik == row["cik"]).order_by(discovery_evidence.c.filed_date.desc()).limit(12)).mappings().all()
    risks = conn.execute(select(discovery_risks.c.risk, discovery_risks.c.severity, discovery_risks.c.value)
                         .where(discovery_risks.c.cik == row["cik"])).mappings().all()
    f = row["features"] or {}
    keep = ("market_cap", "ttm_revenue", "revenue_yoy", "stage", "major_customers", "bottleneck_signals", "themes",
            "strong_evidence", "insider_buyers_90d", "insider_buy_value_90d", "gov_contract_to_mcap", "financing_risk_score",
            "financing_flags", "hype_sentences", "risks", "inflection_flags")
    return {"name": row["name"], "bucket": row["bucket"], "score": row["interest_score"], "level": row["interest_level"],
            "features": {k: f.get(k) for k in keep},
            "evidence": [{k: (str(v) if k == "filed_date" and v else v) for k, v in dict(e).items()} for e in ev],
            "risks": [dict(r) for r in risks]}


def create_sample(engine: Engine, sample_id: str, sizes: dict[str, int] | None = None) -> dict:
    sizes = sizes or DEFAULT_SIZES
    rng = random.Random(_seed(sample_id))
    out = {}
    with engine.begin() as conn:
        for level, n in sizes.items():
            rows = [dict(r) for r in conn.execute(select(discovery_companies).where(
                discovery_companies.c.interest_level == level).order_by(discovery_companies.c.cik)).mappings().all()]
            pick = rows if len(rows) <= n else rng.sample(rows, n)
            for r in pick:
                conn.execute(pg_insert(discovery_validation).values(
                    sample_id=sample_id, cik=r["cik"], ticker=r["ticker"], level_at_sample=level, score_at_sample=r["interest_score"],
                    evidence_snapshot=snapshot(conn, r)).on_conflict_do_nothing(index_elements=["sample_id", "cik"]))
            out[level] = {"population": len(rows), "sampled": len(pick)}
    return out


def label(conn: Connection, sample_id: str, ticker: str, value: str, reasons: list[str], notes: str, labeler: str) -> bool:
    if value not in LABELS or any(r not in REASONS for r in reasons):
        raise ValueError(f"label must be one of {LABELS}; reasons from {REASONS}")
    res = conn.execute(discovery_validation.update().where(and_(
        discovery_validation.c.sample_id == sample_id, discovery_validation.c.ticker == ticker.upper())).values(
        label=value, reason_codes=reasons, notes=notes[:2000], labeler=labeler[:40], labeled_at=datetime.now(timezone.utc))
        .returning(discovery_validation.c.id)).first()
    return res is not None


def precision(conn: Connection, sample_id: str) -> dict:
    rows = conn.execute(select(discovery_validation.c.level_at_sample, discovery_validation.c.label, discovery_validation.c.labeler)
                        .where(discovery_validation.c.sample_id == sample_id)).all()
    out: dict = {}
    for level in ("HIGH", "MEDIUM", "LOW"):
        lv = [r for r in rows if r[0] == level]
        lab = [r for r in lv if r[1]]
        decided = [r for r in lab if r[1] != "INSUFFICIENT_DATA"]
        tp = sum(1 for r in decided if r[1] == "TRUE_DISCOVERY")
        pl = sum(1 for r in decided if r[1] == "PLAUSIBLE")
        out[level] = {"sampled": len(lv), "labelled": len(lab), "insufficient_data": len(lab) - len(decided),
                      "true_discovery": tp, "plausible": pl, "false_positive": len(decided) - tp - pl,
                      "strict_precision": (tp / len(decided)) if decided else None,
                      "lenient_precision": ((tp + pl) / len(decided)) if decided else None,
                      "labelers": sorted({r[2] for r in lab if r[2]})}
    return out
