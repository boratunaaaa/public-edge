"""Optional LLM verification layer with cost tracking.

V1 runs fully deterministic. An LLM is only ever called on candidates that already passed the
deterministic filters, and only when LLM_PROVIDER + LLM_API_KEY are configured by the operator.
Every call is recorded in llm_usage (tokens + estimated USD).
"""
from __future__ import annotations

import os

from sqlalchemy.engine import Connection

from app.db.models import llm_usage

PRICES_PER_MTOK = {"claude-haiku-4-5-20251001": (1.0, 5.0), "claude-sonnet-5": (3.0, 15.0)}  # (input, output) USD


def enabled() -> bool:
    return bool(os.environ.get("LLM_PROVIDER") and os.environ.get("LLM_API_KEY"))


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICES_PER_MTOK.get(model, (3.0, 15.0))
    return (input_tokens * pin + output_tokens * pout) / 1e6


def record_usage(conn: Connection, *, provider: str, model: str, purpose: str, input_tokens: int, output_tokens: int) -> float:
    cost = estimate_cost(model, input_tokens, output_tokens)
    conn.execute(llm_usage.insert().values(provider=provider, model=model, purpose=purpose, input_tokens=input_tokens,
                                           output_tokens=output_tokens, cost_usd=cost))
    return cost
