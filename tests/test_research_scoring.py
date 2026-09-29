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
