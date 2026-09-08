import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.calculations import (
    CalculationError, MeterInput, compute_month, heuristic_suggested_price,
    recommended_price_from_this_month,
)


def make_meters(with_water_shares=False):
    return [
        MeterInput("main", "Main", "", "main"),
        MeterInput("flat1", "Front Ground", "Marvari", "physical",
                   water_share=2 if with_water_shares else 1, current_reading=140, last_reading=100),
        MeterInput("flat2", "Balcony Room", "Rita", "physical",
                   current_reading=80, last_reading=50, water_share=1),
        MeterInput("flat3", "Hall Upper", "Anamika", "physical",
                   current_reading=105, last_reading=80, water_share=1),
        MeterInput("flat4", "Middle Upper", "Nimisha", "physical",
                   current_reading=75, last_reading=40, water_share=1),
        MeterInput("flat5", "Back Upper", "3 idiots", "physical",
                   current_reading=80, last_reading=60, water_share=1),
        MeterInput("motor", "Motor", "Motor", "motor", current_reading=30, last_reading=20),
        MeterInput("back_ground", "Back Ground", "Hanu", "derived", water_share=1),
    ]


def test_derived_meter_is_the_leftover():
    meters = make_meters()
    result = compute_month(meters, main_current=400, main_last=200, main_bill_amount=1000,
                            unit_price=7.5, water_mode="even")
    derived = next(m for m in result.meters if m.type == "derived")
    # known units: 40+30+25+35+20 (flats) + 10 (motor) = 160; main_delta=200 -> derived=40
    assert derived.units == 40
    assert result.main_delta == 200


def test_total_collected_equals_main_delta_times_price_even_split():
    meters = make_meters()
    result = compute_month(meters, main_current=400, main_last=200, main_bill_amount=1000,
                            unit_price=7.5, water_mode="even")
    assert math.isclose(result.total_collected, result.main_delta * 7.5, rel_tol=1e-6)
    # every tenant meter's water share should be pool / 6
    tenants = result.tenant_meters()
    assert len(tenants) == 6
    pool = result.water_pool_amount
    for t in tenants:
        assert math.isclose(t.water_share_amount, round(pool / 6, 2), abs_tol=0.02)


def test_proportional_split_is_by_units_consumed_this_month():
    meters = make_meters()
    result = compute_month(meters, main_current=400, main_last=200, main_bill_amount=1000,
                            unit_price=7.5, water_mode="proportional")
    # per-line rounding to the nearest paisa can leak a couple of cents across
    # several tenants; that's expected in a real bill, so allow a small tolerance.
    assert math.isclose(result.total_collected, result.main_delta * 7.5, abs_tol=0.05)
    tenants = {t.meter_id: t for t in result.tenant_meters()}
    pool = result.water_pool_amount
    # tenant units: flat1 40, flat2 30, flat3 25, flat4 35, flat5 20, derived 40 -> total 190
    assert math.isclose(tenants["flat1"].water_share_amount, round(pool * 40 / 190, 2), abs_tol=0.02)
    assert math.isclose(tenants["flat2"].water_share_amount, round(pool * 30 / 190, 2), abs_tol=0.02)
    # the flat that used the most units carries at least as much water as any other
    assert tenants["flat1"].water_share_amount >= tenants["flat5"].water_share_amount


def test_proportional_split_falls_back_to_even_when_no_consumption():
    meters = [
        MeterInput("main", "Main", "", "main"),
        MeterInput("flat1", "A", "", "physical", current_reading=100, last_reading=100),
        MeterInput("flat2", "B", "", "physical", current_reading=50, last_reading=50),
        MeterInput("motor", "Motor", "", "motor", current_reading=30, last_reading=20),
        MeterInput("back_ground", "Derived", "", "derived"),
    ]
    # main delta 10 all goes to the derived meter; flats + derived split the pool
    result = compute_month(meters, main_current=210, main_last=200, main_bill_amount=100,
                            unit_price=7.5, water_mode="proportional")
    tenants = {t.meter_id: t for t in result.tenant_meters()}
    # flat1 and flat2 used 0 units; an even split still gives them a share
    assert tenants["flat1"].water_share_amount == tenants["flat2"].water_share_amount
    assert tenants["flat1"].water_share_amount > 0


def test_water_override_pins_one_flat_and_reflows_total():
    meters = make_meters()
    result = compute_month(meters, main_current=400, main_last=200, main_bill_amount=1000,
                            unit_price=7.5, water_mode="even", water_overrides={"flat1": 500})
    flat1 = next(m for m in result.meters if m.meter_id == "flat1")
    assert flat1.water_share_amount == 500
    assert flat1.water_override is True
    assert flat1.total_amount == round(flat1.electricity_amount + 500, 2)
    # other flats keep their even share
    flat2 = next(m for m in result.meters if m.meter_id == "flat2")
    assert flat2.water_override is False


def test_units_override_handles_a_stuck_or_replaced_meter():
    meters = make_meters()
    # flat3's meter is stuck at the same reading; bill it for an estimated 12 units
    meters[3].current_reading = meters[3].last_reading  # 0 by delta
    result = compute_month(meters, main_current=400, main_last=200, main_bill_amount=1000,
                            unit_price=7.5, water_mode="even", units_overrides={"flat3": 12})
    flat3 = next(m for m in result.meters if m.meter_id == "flat3")
    assert flat3.units == 12
    assert flat3.electricity_amount == round(12 * 7.5, 2)


def test_units_override_lets_a_reading_go_backwards_without_error():
    meters = make_meters()
    meters[1].current_reading = 5  # far below its last_reading of 100 (meter reset)
    # without an override this raises; with one it's fine
    with pytest.raises(CalculationError):
        compute_month(meters, main_current=400, main_last=200, main_bill_amount=1000,
                       unit_price=7.5, water_mode="even")
    result = compute_month(meters, main_current=400, main_last=200, main_bill_amount=1000,
                            unit_price=7.5, water_mode="even", units_overrides={"flat1": 5})
    assert next(m for m in result.meters if m.meter_id == "flat1").units == 5


def test_per_tenant_unit_price_override():
    meters = make_meters()
    result = compute_month(meters, main_current=400, main_last=200, main_bill_amount=1000,
                            unit_price=7.5, water_mode="even",
                            unit_price_overrides={"flat1": 9.0})
    flat1 = next(m for m in result.meters if m.meter_id == "flat1")
    flat2 = next(m for m in result.meters if m.meter_id == "flat2")
    assert flat1.unit_price == 9.0
    assert flat1.electricity_amount == round(flat1.units * 9.0, 2)
    assert flat2.unit_price == 7.5  # others unchanged


def test_manual_water_pins_some_and_splits_the_rest():
    meters = make_meters()
    result = compute_month(meters, main_current=400, main_last=200, main_bill_amount=1000,
                            unit_price=7.5, water_mode="manual",
                            water_overrides={"flat1": 50, "flat2": 50})
    tenants = {t.meter_id: t for t in result.tenant_meters()}
    pool = result.water_pool_amount
    assert tenants["flat1"].water_share_amount == 50
    assert tenants["flat2"].water_share_amount == 50
    # the remaining 4 flats split whatever is left of the pool, equally
    rest = [tenants[m] for m in ("flat3", "flat4", "flat5", "back_ground")]
    assert all(abs(r.water_share_amount - round((pool - 100) / 4, 2)) < 0.01 for r in rest)
    # every rupee of the pool is still collected
    assert abs(sum(t.water_share_amount for t in tenants.values()) - pool) < 0.05


def test_manual_water_zero_pin_keeps_flat_out_of_the_split():
    meters = make_meters()
    result = compute_month(meters, main_current=400, main_last=200, main_bill_amount=1000,
                            unit_price=7.5, water_mode="manual", water_overrides={"flat3": 0})
    tenants = {t.meter_id: t for t in result.tenant_meters()}
    assert tenants["flat3"].water_share_amount == 0
    others = [tenants[m] for m in ("flat1", "flat2", "flat4", "flat5", "back_ground")]
    assert all(o.water_share_amount > 0 for o in others)
    assert abs(sum(t.water_share_amount for t in tenants.values()) - result.water_pool_amount) < 0.05


def test_excluded_flat_gets_no_water_in_any_mode():
    for mode in ("even", "proportional", "manual"):
        meters = make_meters()
        result = compute_month(meters, main_current=400, main_last=200, main_bill_amount=1000,
                                unit_price=7.5, water_mode=mode, water_excluded=["flat4"])
        tenants = {t.meter_id: t for t in result.tenant_meters()}
        assert tenants["flat4"].water_share_amount == 0, mode
        assert tenants["flat4"].total_amount == tenants["flat4"].electricity_amount, mode
        # the pool is fully spread over the other 5 flats
        assert abs(sum(t.water_share_amount for t in tenants.values()) - result.water_pool_amount) < 0.05, mode


def test_manual_water_overpinned_warns():
    meters = make_meters()
    result = compute_month(meters, main_current=400, main_last=200, main_bill_amount=1000,
                            unit_price=7.5, water_mode="manual",
                            water_overrides={"flat1": 99999})
    assert any(m.warning for m in result.meters)


def test_recommended_price_from_this_month():
    # bill 1000 over a 200-unit delta, 7% margin -> 1000*1.07/200
    assert math.isclose(recommended_price_from_this_month(1000, 200, 7), (1000 * 1.07) / 200, abs_tol=0.01)
    assert recommended_price_from_this_month(0, 200, 7) == 0.0
    assert recommended_price_from_this_month(1000, 0, 7) == 0.0


def test_motor_is_never_billed_directly():
    meters = make_meters()
    result = compute_month(meters, main_current=400, main_last=200, main_bill_amount=1000,
                            unit_price=7.5, water_mode="even")
    motor = next(m for m in result.meters if m.type == "motor")
    assert motor.total_amount == 0.0


def test_negative_delta_raises():
    meters = make_meters()
    with pytest.raises(CalculationError):
        compute_month(meters, main_current=100, main_last=200, main_bill_amount=1000,
                       unit_price=7.5, water_mode="even")


def test_meter_reading_going_backwards_raises():
    meters = make_meters()
    meters[1].current_reading = 50  # below its own last_reading of 100
    with pytest.raises(CalculationError):
        compute_month(meters, main_current=400, main_last=200, main_bill_amount=1000,
                       unit_price=7.5, water_mode="even")


def test_more_than_one_derived_meter_rejected():
    meters = make_meters()
    meters.append(MeterInput("extra_derived", "Extra", "X", "derived"))
    with pytest.raises(CalculationError):
        compute_month(meters, main_current=400, main_last=200, main_bill_amount=1000,
                       unit_price=7.5, water_mode="even")


def test_manual_override_replaces_total_and_flags_it():
    meters = make_meters()
    result = compute_month(meters, main_current=400, main_last=200, main_bill_amount=1000,
                            unit_price=7.5, water_mode="even", overrides={"flat1": 999})
    flat1 = next(m for m in result.meters if m.meter_id == "flat1")
    assert flat1.total_amount == 999
    assert flat1.manual_override is True
    # total_collected must reflect the override too
    others_sum = sum(m.total_amount for m in result.tenant_meters() if m.meter_id != "flat1")
    assert math.isclose(result.total_collected, others_sum + 999, rel_tol=1e-6)


def test_derived_going_negative_warns_but_does_not_raise():
    meters = make_meters()
    # blow up one flat's consumption so metered total exceeds the main delta
    meters[1].current_reading = 100000
    result = compute_month(meters, main_current=400, main_last=200, main_bill_amount=1000,
                            unit_price=7.5, water_mode="even")
    derived = next(m for m in result.meters if m.type == "derived")
    assert derived.units < 0
    assert derived.warning is not None


def test_heuristic_suggested_price_matches_legacy_formula():
    # legacy: price = (bill_amt + bill_amt*margin/100) / total_units
    price = heuristic_suggested_price(last_bill_amount=9285, last_total_units=1248, margin_pct=6)
    assert math.isclose(price, (9285 + 9285 * 0.06) / 1248, abs_tol=0.01)


def test_zero_or_negative_unit_price_rejected():
    meters = make_meters()
    with pytest.raises(CalculationError):
        compute_month(meters, main_current=400, main_last=200, main_bill_amount=1000,
                       unit_price=0, water_mode="even")
