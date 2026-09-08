import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ml_pricing import HistoryMonth, MIN_MONTHS_FOR_ML, backtest, suggest_price


def synthetic_history(n=24, base_cost=7.0, growth=0.02, seed=42):
    import random
    rnd = random.Random(seed)
    months = []
    year, month = 2023, 1
    cost = base_cost
    for i in range(n):
        cost = cost * (1 + growth / 12) + rnd.uniform(-0.15, 0.15)
        units = 1000 + rnd.uniform(-100, 100) + 50 * (month in (5, 6, 7))  # summer bump
        months.append(HistoryMonth(
            month=f"{year:04d}-{month:02d}",
            main_delta=units,
            main_bill_amount=round(cost * units, 2),
            total_units=units,
        ))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return months


def test_falls_back_to_heuristic_with_little_history():
    history = synthetic_history(n=6)
    suggestion = suggest_price(history, margin_pct=7)
    assert suggestion.method == "heuristic"
    assert suggestion.months_used == 6


def test_uses_random_forest_with_enough_history():
    history = synthetic_history(n=MIN_MONTHS_FOR_ML + 5)
    suggestion = suggest_price(history, margin_pct=7)
    assert suggestion.method == "random_forest"
    assert suggestion.predicted_cost_per_unit is not None


def test_suggestion_never_below_floor_times_margin():
    history = synthetic_history(n=30)
    for margin in (0, 5, 10):
        suggestion = suggest_price(history, margin_pct=margin)
        assert suggestion.suggested_price >= round(suggestion.floor_price * (1 + margin / 100), 2) - 0.01


def test_backtest_report_shape():
    history = synthetic_history(n=30)
    report = backtest(history, margin_pct=7)
    assert report.months_tested == 30 - MIN_MONTHS_FOR_ML
    assert 0 <= report.months_tested == len(report.details)
    for d in report.details:
        assert set(d) == {"month", "suggested_price", "actual_cost_per_unit",
                           "would_have_covered_cost", "margin_pct_achieved"}


def test_backtest_mostly_avoids_losses_on_gently_trending_data():
    # Gentle, low-noise growth is the easy case -- the floor should cover
    # the overwhelming majority of months here. (Real, noisier data can and
    # does still miss occasionally -- see the module docstring.)
    history = synthetic_history(n=30, growth=0.03, seed=7)
    report = backtest(history, margin_pct=7)
    loss_rate = 1 - sum(d["would_have_covered_cost"] for d in report.details) / len(report.details)
    assert loss_rate <= 0.2


def test_empty_history_raises():
    with pytest.raises(ValueError):
        suggest_price([], margin_pct=7)
