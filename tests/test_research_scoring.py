"""Statistics of the read-only scoring-evidence research (app.backtest.research_scoring), checked against independent
hand calculations."""
import math
import random
import statistics

import pytest


def test_summary_mean_se_and_intervals():
    from app.backtest.research_scoring import summarize
    d = {f"c{i}": v for i, v in enumerate([0.1, -0.1, 0.2, 0.0, 0.3] * 40)}  # 200 companies
    s = summarize(d, random.Random(1))
    x = list(d.values())
    se = statistics.stdev(x) / math.sqrt(len(x))
    assert s["effect"] == pytest.approx(statistics.fmean(x)) and s["se"] == pytest.approx(se)
    assert s["ci95"] == pytest.approx([s["effect"] - 1.959964 * se, s["effect"] + 1.959964 * se])
    lo, hi = s["boot_ci95"]
    assert lo < s["effect"] < hi and hi - lo == pytest.approx(2 * 1.96 * se, rel=0.25)  # bootstrap ~ normal here
    assert s["t_p"] == pytest.approx(math.erfc(abs(s["effect"] / se) / math.sqrt(2)))


def test_sign_test_and_mean_uncertainty_differ():
    """80 x +0.01 and 20 x -0.03: the sign test is very significant, the mean is exactly zero."""
    from app.backtest.research_scoring import summarize
    d = {f"c{i}": 0.01 for i in range(80)} | {f"n{i}": -0.04 for i in range(20)}
    s = summarize(d, random.Random(2))
    assert s["sign_p"] < 1e-8 and s["effect"] == pytest.approx(0.0, abs=1e-12) and s["t_p"] > 0.9


def test_benjamini_hochberg_adjusted_p_values():
    from app.backtest.research_scoring import bh
    adj = bh({"a": 0.01, "b": 0.02, "c": 0.03, "d": 0.5})
    assert adj == pytest.approx({"a": 0.04, "b": 0.04, "c": 0.04, "d": 0.5})


def test_incremental_effect_and_cluster_bootstrap():
    from app.backtest.research_scoring import incremental
    with_d = {f"c{i}": -0.05 for i in range(150)}
    without_d = {f"c{i}": 0.02 for i in range(100, 400)}  # companies 100-149 are in both groups
    r = incremental(with_d, without_d, random.Random(3))
    assert r["incremental"] == pytest.approx(-0.07) and r["with_companies"] == 150 and r["without_companies"] == 300
    assert r["boot_ci95"][0] <= -0.07 <= r["boot_ci95"][1]


def test_derived_flags_use_only_earlier_events():
    from datetime import date
    from app.backtest.research_scoring import derive_flags
    rows = [
        {"id": 1, "issuer_cik": "A", "public_date": date(2020, 1, 1), "payload": {"owner_cik": "o1", "value": 1e5}, "excess_ret": 0.2},
        {"id": 2, "issuer_cik": "B", "public_date": date(2020, 3, 1), "payload": {"owner_cik": "o1"}, "excess_ret": 0.1},
        {"id": 3, "issuer_cik": "A", "public_date": date(2020, 9, 1), "payload": {"owner_cik": "o1", "value": 3e5,
                                                                                   "direct_indirect": ["D"]}, "excess_ret": -0.5},
        {"id": 4, "issuer_cik": "A", "public_date": date(2020, 9, 15), "payload": {"owner_cik": "o2"}, "excess_ret": None},
    ]
    f = derive_flags(rows)
    assert "first_buy_same_issuer_5y" in f[1] and "repeat_same_issuer_365d" not in f[1]
    assert {"repeat_same_issuer_365d", "owner_history_positive", "direct_only", "value_ge_250k"} <= f[3]
    assert "owner_history_positive" not in f[2]  # only one prior buy, and it matured too late
    assert "cluster_context_30d" in f[4] and "cluster_context_30d" not in f[3]  # o2 bought AFTER o1's 2020-09-01 buy


def test_per_quarter_thinning_keeps_the_first_event():
    from datetime import date
    from app.backtest.research_scoring import per_quarter
    rows = [{"id": i, "issuer_cik": "A", "public_date": d} for i, d in
            ((1, date(2020, 2, 1)), (2, date(2020, 1, 5)), (3, date(2020, 4, 1)))]
    assert [r["id"] for r in per_quarter(rows)] == [2, 3]


@pytest.mark.parametrize("cell, bh_p, verdict", [
    ({"companies": 99, "effect": 0.2, "boot_ci95": [0.1, 0.3]}, 0.001, "INSUFFICIENT_SAMPLE"),
    ({"companies": 300, "effect": 0.05, "boot_ci95": [0.01, 0.09]}, 0.02, "REPLICATED"),
    ({"companies": 300, "effect": 0.05, "boot_ci95": [0.01, 0.09]}, 0.06, "NOT_REPLICATED"),
    ({"companies": 300, "effect": 0.05, "boot_ci95": [-0.01, 0.09]}, 0.02, "NOT_REPLICATED"),
    ({"companies": 300, "effect": -0.05, "boot_ci95": [-0.09, -0.01]}, 0.02, "NOT_REPLICATED"),  # wrong sign
    ({"companies": 300}, None, "NOT_REPLICATED"),
])
def test_holdout_verdict_follows_the_preregistered_rule(cell, bh_p, verdict):
    from app.backtest.research_scoring import holdout_verdict
    assert holdout_verdict(cell, bh_p) == verdict


def test_holdout_window_and_family_are_the_preregistered_ones():
    from datetime import date

    from app.backtest.research_scoring import HOLDOUT_END, HOLDOUT_FAMILY, HOLDOUT_START
    assert (HOLDOUT_START, HOLDOUT_END) == (date(2024, 7, 1), date(2026, 6, 30))
    assert set(HOLDOUT_FAMILY) == {"serial_3plus_issuers_2y", "owner_history_negative", "indirect_only",
                                   "first_buy_same_issuer_5y", "value_100k_1m", "incr_10_50pct"}
