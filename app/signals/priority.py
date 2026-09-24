"""Temporary alert-priority heuristic.

This is NOT an investment score. It only decides which events are worth a Telegram
message. Raw features are stored separately (signal_features) so real weights can be
learned from the backtest later. Political party is never an input here.
"""
from __future__ import annotations

INSIDER_ROLE_POINTS = {"ceo": 3, "cfo": 2, "director": 1}


def value_points(v: float | None) -> int:
    v = v or 0
    if v >= 1_000_000:
        return 3
    if v >= 500_000:
        return 2
    if v >= 100_000:
        return 1
    return 0


def insider_buy_priority(*, is_ceo: bool, is_cfo: bool, is_director: bool, value: float, is_10b5_1: bool) -> int:
    p = 1 + value_points(value)
    p += INSIDER_ROLE_POINTS["ceo"] if is_ceo else 0
    p += INSIDER_ROLE_POINTS["cfo"] if is_cfo else 0
    p += INSIDER_ROLE_POINTS["director"] if (is_director and not (is_ceo or is_cfo)) else 0
    if is_10b5_1:
        p -= 2  # pre-planned purchases carry less information
    return max(p, 0)


def cluster_priority(*, n_buyers: int, ceo: bool, cfo: bool, aggregate: float) -> int:
    p = 4 if n_buyers >= 3 else 3
    p += 1 if ceo else 0
    p += 1 if cfo else 0
    p += 1 if aggregate >= 1_000_000 else 0
    return p


ACTIVISM_CLASSES = {"BOARD_CHANGE", "BOARD_SEAT", "STRATEGIC_ALTERNATIVES", "SALE_OF_COMPANY", "CAPITAL_ALLOCATION",
                    "BUYBACK_REQUEST", "ASSET_SALE", "MERGER", "OTHER_ACTIVISM"}


def activist_priority(*, item4_class: str, is_amendment: bool, percent: float | None, percent_change: float | None) -> int:
    if item4_class in ACTIVISM_CLASSES:
        p = 4
    else:
        p = 2
    if not is_amendment:
        p += 1
    if percent is not None and percent >= 10:
        p += 1
    if is_amendment and percent_change is not None and percent_change >= 2:
        p += 1
    return p


def contract_level(ratio: float | None) -> str | None:
    if ratio is None:
        return None
    for lvl, thr in ((">50%", 0.50), (">25%", 0.25), (">10%", 0.10), (">5%", 0.05), (">2%", 0.02)):
        if ratio > thr:
            return lvl
    return None


def contract_priority(ratio: float | None) -> int:
    return {">50%": 6, ">25%": 5, ">10%": 4, ">5%": 3, ">2%": 2}.get(contract_level(ratio) or "", 0)


def congress_buy_priority(amount_min: float | None) -> int:
    return 1 + (1 if (amount_min or 0) >= 250_001 else 0)


def congress_cluster_priority(n_members: int) -> int:
    return 2 + max(0, n_members - 2)


FORM144_MAJOR_VALUE = 5_000_000
FORM144_MAJOR_PCT = 0.01


def form144_is_major(est_value: float | None, shares: float | None, outstanding: float | None) -> bool:
    if est_value and est_value >= FORM144_MAJOR_VALUE:
        return True
    if shares and outstanding and outstanding > 0 and shares / outstanding >= FORM144_MAJOR_PCT:
        return True
    return False


TRUMP_FAMILY_PRIORITY = 5
