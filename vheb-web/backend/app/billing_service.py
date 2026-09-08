"""
Glue between the Sheet (sheets_client), the pure calculation engine
(calculations), and the pricing model (ml_pricing). Nothing here talks
HTTP -- main.py's routes call these functions and translate to/from
JSON.
"""
from __future__ import annotations

import datetime as dt

from . import ml_pricing, sheets_client
from .calculations import (
    CalculationError, MeterInput, MeterResult, MonthResult, compute_month,
    recommended_price_from_this_month,
)


def _to_meter_inputs(
    meters: list[dict],
    readings_by_id: dict[str, float],
    last_readings_override: dict[str, float] | None = None,
    before_month: str | None = None,
) -> list[MeterInput]:
    last_readings_override = last_readings_override or {}
    out = []
    for m in meters:
        meter_id = m["meter_id"]
        mtype = m["type"]
        if meter_id in last_readings_override:
            last_reading = float(last_readings_override[meter_id])
        else:
            last = sheets_client.last_reading_for(meter_id, before_month=before_month)
            last_reading = float(last["reading"]) if last and last.get("reading") not in ("", None) else None
        out.append(MeterInput(
            meter_id=meter_id, label=m["label"], tenant_name=m.get("tenant_name", ""),
            type=mtype, water_share=float(m.get("water_share") or 1.0),
            current_reading=readings_by_id.get(meter_id) if mtype in ("physical", "motor") else None,
            last_reading=last_reading if mtype in ("physical", "motor") else None,
        ))
    return out


def get_main_last(month: str) -> float | None:
    summaries = [s for s in sheets_client.list_summaries() if str(s.get("month")) < month]
    if not summaries:
        return None
    summaries.sort(key=lambda s: str(s["month"]))
    val = summaries[-1].get("main_meter_reading")
    return float(val) if val not in ("", None) else None


def build_history(exclude_month: str | None = None) -> list[ml_pricing.HistoryMonth]:
    summaries = sheets_client.list_summaries()
    readings_by_month: dict[str, list[dict]] = {}
    for r in sheets_client.list_readings():
        readings_by_month.setdefault(r["month"], []).append(r)

    history = []
    prev_main = None
    for s in sorted(summaries, key=lambda s: str(s["month"])):
        month = s["month"]
        if exclude_month and month == exclude_month:
            continue
        main_reading = s.get("main_meter_reading")
        bill_amount = s.get("main_bill_amount")
        if main_reading in ("", None) or bill_amount in ("", None):
            continue
        main_reading = float(main_reading)
        bill_amount = float(bill_amount)
        if prev_main is not None:
            delta = main_reading - prev_main
            total_units = sum(float(r.get("units") or 0) for r in readings_by_month.get(month, []))
            if delta > 0 and total_units > 0:
                history.append(ml_pricing.HistoryMonth(
                    month=month, main_delta=delta, main_bill_amount=bill_amount, total_units=total_units,
                ))
        prev_main = main_reading
    return history


def suggest_price(margin_pct: float | None = None) -> ml_pricing.PriceSuggestion:
    settings_dict = sheets_client.get_settings_dict()
    margin = margin_pct if margin_pct is not None else float(settings_dict.get("default_margin_pct", 7))
    history = build_history()
    return ml_pricing.suggest_price(history, margin)


def get_backtest(margin_pct: float | None = None) -> ml_pricing.BacktestReport:
    settings_dict = sheets_client.get_settings_dict()
    margin = margin_pct if margin_pct is not None else float(settings_dict.get("default_margin_pct", 7))
    history = build_history()
    return ml_pricing.backtest(history, margin)


def _resolve_margin(margin_pct: float | None) -> float:
    if margin_pct is not None:
        return margin_pct
    return float(sheets_client.get_settings_dict().get("default_margin_pct", 7))


def compute(*, month: str, main_current: float, main_bill_amount: float,
            readings: dict[str, float], unit_price: float | None, water_mode: str,
            margin_pct: float | None, overrides: dict[str, float],
            water_overrides: dict[str, float] | None = None,
            water_excluded: list[str] | None = None,
            units_overrides: dict[str, float] | None = None,
            unit_price_overrides: dict[str, float] | None = None,
            last_readings: dict[str, float] | None = None,
            main_last_input: float | None) -> MonthResult:
    meters = sheets_client.list_meters(active_only=True)
    if not any(m["type"] == "main" for m in meters):
        raise CalculationError("No meter of type 'main' is configured -- add one on the Meters page.")

    main_last = main_last_input if main_last_input is not None else get_main_last(month)
    if main_last is None:
        raise CalculationError(
            "No previous main-meter reading found. This looks like the first month ever "
            "entered -- enter the main meter's reading at the end of last month to start it off."
        )

    margin = _resolve_margin(margin_pct)
    if unit_price is None:
        suggestion = suggest_price(margin_pct)
        unit_price = suggestion.suggested_price

    meter_inputs = _to_meter_inputs(meters, readings, last_readings, before_month=month)
    result = compute_month(
        meter_inputs, main_current=main_current, main_last=main_last,
        main_bill_amount=main_bill_amount, unit_price=unit_price, water_mode=water_mode,
        overrides=overrides, water_overrides=water_overrides, water_excluded=water_excluded,
        units_overrides=units_overrides, unit_price_overrides=unit_price_overrides,
    )
    result.recommended_unit_price = recommended_price_from_this_month(
        main_bill_amount, result.main_delta, margin
    )
    return result


def finalize(*, month: str, result: MonthResult, water_mode: str, margin_pct: float | None,
             main_last_input: float | None) -> None:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    main_last = main_last_input if main_last_input is not None else get_main_last(month)

    rows_by_meter = {}
    for r in result.meters:
        rows_by_meter[r.meter_id] = {
            "reading": r.reading if r.reading is not None else "",
            "last_reading": r.last_reading if r.last_reading is not None else "",
            "units": r.units, "unit_price": r.unit_price,
            "electricity_amount": r.electricity_amount,
            "water_share_amount": r.water_share_amount, "total_amount": r.total_amount,
            "manual_override": "TRUE" if (r.manual_override or r.water_override) else "FALSE",
            "notes": r.warning or "", "entered_at": now,
        }
    sheets_client.upsert_readings(month, rows_by_meter)

    margin = _resolve_margin(margin_pct)
    sheets_client.upsert_summary(month, {
        "month": month,
        "main_meter_reading": main_last + result.main_delta,
        "main_last_reading": main_last if main_last is not None else "",
        "main_bill_amount": result.main_bill_amount,
        "unit_price": result.unit_price,
        "margin_pct": margin,
        "water_pool_amount": result.water_pool_amount,
        "water_mode": water_mode,
        "total_collected": result.total_collected,
        "status": "finalized",
        "suggested_price": result.unit_price,
        "created_at": now,
        "finalized_at": now,
    })
