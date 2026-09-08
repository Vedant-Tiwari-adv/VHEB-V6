"""
Suggested unit price.

Approach: a small RandomForestRegressor predicts next month's *actual
cost per unit* (main bill amount / main meter delta) from a handful of
lagged/rolling features. We then apply a hard floor -- the prediction
can never suggest a price below last month's known actual cost per unit
-- and add the configured margin on top.

Honest limit of that floor, confirmed by backtesting against this
building's own 6 years of history (see scripts/backtest_report.py):
it guarantees you never price below the last *known* cost, but it
can't see a sudden jump in the utility's rate before the bill for
that month actually arrives -- on this building's real data that
caused a handful of months (out of ~30) to fall a few percent short
of "fully covered", never a large miss, and disappears entirely with
a slightly larger margin. It is not a guarantee against a rate spike
nobody could have known about yet, only against re-using a price you
already know was too low.

With only a few dozen months of history, a big model would just
memorise noise, so this stays deliberately small (shallow trees, few
of them) and always has the plain heuristic formula
(calculations.heuristic_suggested_price) as a fallback when there
isn't enough history to fit a model at all.

Nothing here is persisted to disk: the model is refit from the Sheet's
history on demand, in well under a second even with years of data, so
there's no model file to manage on a host with no persistent disk.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math

from .calculations import heuristic_suggested_price

MIN_MONTHS_FOR_ML = 12


@dataclass
class HistoryMonth:
    month: str  # "YYYY-MM"
    main_delta: float
    main_bill_amount: float
    total_units: float  # sum of all sub-meter units that month (== main_delta by construction)

    @property
    def actual_cost_per_unit(self) -> float:
        return self.main_bill_amount / self.main_delta if self.main_delta else 0.0


@dataclass
class PriceSuggestion:
    suggested_price: float
    method: str  # "random_forest" | "heuristic"
    floor_price: float
    predicted_cost_per_unit: float | None
    margin_pct: float
    months_used: int
    note: str = ""


def _month_num(month: str) -> int:
    return int(month.split("-")[1])


def _build_features(history: list[HistoryMonth]) -> tuple[list[list[float]], list[float]]:
    """Rolling/lag features built only from *prior* months -- no leakage."""
    X, y = [], []
    for i in range(2, len(history)):
        prior = history[:i]
        cur = history[i]
        last1 = prior[-1]
        last2 = prior[-2]
        last3 = prior[-3] if len(prior) >= 3 else last2
        recent = prior[-3:]
        rolling_avg_cost = sum(h.actual_cost_per_unit for h in recent) / len(recent)
        rolling_avg_units = sum(h.total_units for h in recent) / len(recent)
        month_num = _month_num(cur.month)
        features = [
            last1.actual_cost_per_unit,
            last2.actual_cost_per_unit,
            last3.actual_cost_per_unit,
            rolling_avg_cost,
            last1.total_units,
            rolling_avg_units,
            math.sin(2 * math.pi * month_num / 12),
            math.cos(2 * math.pi * month_num / 12),
            float(i),  # months-since-start, captures long-run drift/inflation
        ]
        X.append(features)
        y.append(cur.actual_cost_per_unit)
    return X, y


def _features_for_next(history: list[HistoryMonth]) -> list[float]:
    last1, last2 = history[-1], history[-2]
    last3 = history[-3] if len(history) >= 3 else last2
    recent = history[-3:]
    rolling_avg_cost = sum(h.actual_cost_per_unit for h in recent) / len(recent)
    rolling_avg_units = sum(h.total_units for h in recent) / len(recent)
    next_month_num = (_month_num(last1.month) % 12) + 1
    return [
        last1.actual_cost_per_unit,
        last2.actual_cost_per_unit,
        last3.actual_cost_per_unit,
        rolling_avg_cost,
        last1.total_units,
        rolling_avg_units,
        math.sin(2 * math.pi * next_month_num / 12),
        math.cos(2 * math.pi * next_month_num / 12),
        float(len(history)),
    ]


def suggest_price(history: list[HistoryMonth], margin_pct: float) -> PriceSuggestion:
    history = sorted(history, key=lambda h: h.month)
    if not history:
        raise ValueError("No historical months available to base a suggestion on.")

    floor_price = history[-1].actual_cost_per_unit

    if len(history) < MIN_MONTHS_FOR_ML:
        price = heuristic_suggested_price(
            last_bill_amount=history[-1].main_bill_amount,
            last_total_units=history[-1].total_units,
            margin_pct=margin_pct,
        )
        return PriceSuggestion(
            suggested_price=max(price, round(floor_price * (1 + margin_pct / 100), 2)),
            method="heuristic",
            floor_price=round(floor_price, 2),
            predicted_cost_per_unit=None,
            margin_pct=margin_pct,
            months_used=len(history),
            note=f"Only {len(history)} months of history (need {MIN_MONTHS_FOR_ML}+ for the "
                 "model) -- using the plain last-bill-plus-margin formula instead.",
        )

    try:
        from sklearn.ensemble import RandomForestRegressor
    except ImportError:
        price = heuristic_suggested_price(
            last_bill_amount=history[-1].main_bill_amount,
            last_total_units=history[-1].total_units,
            margin_pct=margin_pct,
        )
        return PriceSuggestion(
            suggested_price=max(price, round(floor_price * (1 + margin_pct / 100), 2)),
            method="heuristic",
            floor_price=round(floor_price, 2),
            predicted_cost_per_unit=None,
            margin_pct=margin_pct,
            months_used=len(history),
            note="scikit-learn isn't installed -- using the plain heuristic formula.",
        )

    X, y = _build_features(history)
    model = RandomForestRegressor(
        n_estimators=150, max_depth=5, min_samples_leaf=2, random_state=42,
    )
    model.fit(X, y)
    predicted_cost = float(model.predict([_features_for_next(history)])[0])

    # Hard floor: never suggest below the last known actual cost per unit,
    # no matter what the model predicts.
    base = max(predicted_cost, floor_price)
    suggested = round(base * (1 + margin_pct / 100), 2)

    return PriceSuggestion(
        suggested_price=suggested,
        method="random_forest",
        floor_price=round(floor_price, 2),
        predicted_cost_per_unit=round(predicted_cost, 4),
        margin_pct=margin_pct,
        months_used=len(history),
    )


@dataclass
class BacktestReport:
    months_tested: int
    never_a_loss: bool
    worst_margin_pct: float
    average_margin_pct: float
    mean_absolute_error: float
    details: list[dict] = field(default_factory=list)


def backtest(history: list[HistoryMonth], margin_pct: float, min_train_months: int = MIN_MONTHS_FOR_ML) -> BacktestReport:
    """Walk forward through history: for each month, train only on the months
    strictly before it, suggest a price, and check that price*actual_units
    would have covered that month's real bill (i.e. never a loss)."""
    history = sorted(history, key=lambda h: h.month)
    details = []
    for i in range(min_train_months, len(history)):
        train = history[:i]
        actual = history[i]
        suggestion = suggest_price(train, margin_pct)
        revenue_if_used = suggestion.suggested_price * actual.total_units
        margin_pct_achieved = (
            (revenue_if_used - actual.main_bill_amount) / actual.main_bill_amount * 100
            if actual.main_bill_amount else 0.0
        )
        details.append({
            "month": actual.month,
            "suggested_price": suggestion.suggested_price,
            "actual_cost_per_unit": round(actual.actual_cost_per_unit, 4),
            "would_have_covered_cost": revenue_if_used >= actual.main_bill_amount,
            "margin_pct_achieved": round(margin_pct_achieved, 2),
        })

    if not details:
        return BacktestReport(0, True, 0.0, 0.0, 0.0, [])

    margins = [d["margin_pct_achieved"] for d in details]
    errors = [abs(d["suggested_price"] - d["actual_cost_per_unit"]) for d in details]
    return BacktestReport(
        months_tested=len(details),
        never_a_loss=all(d["would_have_covered_cost"] for d in details),
        worst_margin_pct=round(min(margins), 2),
        average_margin_pct=round(sum(margins) / len(margins), 2),
        mean_absolute_error=round(sum(errors) / len(errors), 4),
        details=details,
    )
