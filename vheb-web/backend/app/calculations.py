"""
Core billing math. Pure functions, no I/O -- this is the one file where
correctness matters most, so it has no side effects and is fully covered
by tests/test_calculations.py.

Meter types
-----------
main      the utility company's own meter for the whole building. Its
          reading and its actual bill amount are entered by hand every
          month; everything else is derived from it.
physical  a real sub-meter with its own dial (a flat, a shop, etc).
motor     a real sub-meter for a shared pump/motor. Its own rupee cost
          is *not* billed to anyone directly -- it becomes the shared
          "water" pool that gets redistributed across the tenant-type
          meters (see below). You can have more than one motor meter;
          their costs all add into the same pool.

Water pool split
----------------
The motor pool is spread across the tenant meters one of two ways, chosen
per month via ``water_mode``:

  "even"          an equal share each (the old desktop app's only
                  behaviour -- pool / number of flats).
  "proportional"  in proportion to how many electricity units each flat
                  actually consumed that month, so a flat that ran the
                  AC all month carries more of the pump cost. Falls back
                  to an even split if nobody consumed anything.

Whichever mode is used, the caller can still hand in ``water_overrides``
(meter_id -> rupee amount) to pin any individual flat's water figure by
hand before the bills go out; the others keep their computed share.
derived   at most ONE of these. It has no physical dial. Its monthly
          units are whatever is left over once every physical + motor
          meter's units are subtracted from the main meter's delta:

              derived_units = main_delta - sum(units of physical + motor)

          This reproduces the original spreadsheet's "Back Ground /
          (N.A)" row exactly (see legacy/ notes in the README).

Tenant-type meters = physical + derived. These are the ones that get
billed and the ones a WhatsApp message is sent to. Motor meters are
never billed directly; main is never billed at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class CalculationError(ValueError):
    """Raised for input that would produce a nonsensical bill."""


@dataclass
class MeterInput:
    meter_id: str
    label: str
    tenant_name: str
    type: str  # "main" | "physical" | "motor" | "derived"
    # Kept for backwards compatibility with older callers/rows; the water
    # split no longer uses a static weight (see module docstring). Unused.
    water_share: float = 1.0
    # Not required for the main meter or the derived meter (it has no dial).
    current_reading: float | None = None
    last_reading: float | None = None


@dataclass
class MeterResult:
    meter_id: str
    label: str
    tenant_name: str
    type: str
    reading: float | None
    last_reading: float | None
    units: float
    unit_price: float
    electricity_amount: float
    water_share_amount: float
    total_amount: float
    manual_override: bool = False       # the final total was pinned by hand
    water_override: bool = False        # the water share was pinned by hand
    warning: str | None = None


@dataclass
class MonthResult:
    unit_price: float
    main_delta: float
    water_pool_amount: float
    total_collected: float
    main_bill_amount: float
    achieved_margin_pct: float
    # This month's own cost-per-unit plus the configured margin -- what the
    # unit-price field should pre-fill with the moment the bill amount and
    # meter readings are in. 0.0 until there's enough to compute it.
    recommended_unit_price: float = 0.0
    meters: list[MeterResult] = field(default_factory=list)

    def tenant_meters(self) -> list[MeterResult]:
        return [m for m in self.meters if m.type in ("physical", "derived")]


def compute_month(
    meters: list[MeterInput],
    main_current: float,
    main_last: float,
    main_bill_amount: float,
    unit_price: float,
    water_mode: str = "even",  # "even" | "proportional" | "manual"
    overrides: dict[str, float] | None = None,
    water_overrides: dict[str, float] | None = None,
    water_excluded: list[str] | None = None,
    units_overrides: dict[str, float] | None = None,
    unit_price_overrides: dict[str, float] | None = None,
) -> MonthResult:
    if unit_price <= 0:
        raise CalculationError("Unit price must be greater than zero.")
    if water_mode not in ("even", "proportional", "manual"):
        raise CalculationError('water_mode must be "even", "proportional" or "manual"')

    units_ov = units_overrides or {}
    price_ov = unit_price_overrides or {}

    def price_for(meter_id: str) -> float:
        p = price_ov.get(meter_id, unit_price)
        if p <= 0:
            raise CalculationError(f"Unit price for {meter_id!r} must be greater than zero.")
        return p

    derived = [m for m in meters if m.type == "derived"]
    if len(derived) > 1:
        raise CalculationError(
            "Only one derived (no-physical-meter) tenant is supported at a time, "
            f"got {len(derived)}. Give every other flat its own meter reading."
        )

    main_delta = main_current - main_last
    if main_delta < 0:
        raise CalculationError(
            "Main meter reading went down -- check for a typo or a meter rollover."
        )

    results: list[MeterResult] = []
    known_units_total = 0.0
    water_pool = 0.0

    for m in meters:
        if m.type == "main":
            continue
        if m.type == "derived":
            continue  # computed after the loop, once known_units_total is final

        eff_price = price_for(m.meter_id)
        warning = None
        if m.meter_id in units_ov:
            # Units set by hand: covers a stuck meter, a replaced/rolled-over
            # meter, or a brand-new meter with no baseline yet.
            units = float(units_ov[m.meter_id])
            if units < 0:
                raise CalculationError(f"{m.label}: units can't be negative.")
        else:
            if m.current_reading is None or m.last_reading is None:
                raise CalculationError(
                    f"{m.label}: enter this month's and last month's readings, or set the "
                    "units directly if the meter was replaced, reset, or is stuck."
                )
            units = m.current_reading - m.last_reading
            if units < 0:
                raise CalculationError(
                    f"{m.label}: reading went down ({m.last_reading} -> {m.current_reading}). "
                    "If the meter was replaced or rolled over, set this meter's units directly."
                )
        known_units_total += units
        amount = round(units * eff_price, 2)
        if m.type == "motor":
            water_pool += amount
            results.append(MeterResult(
                meter_id=m.meter_id, label=m.label, tenant_name=m.tenant_name, type=m.type,
                reading=m.current_reading, last_reading=m.last_reading, units=units,
                unit_price=eff_price, electricity_amount=amount, water_share_amount=0.0,
                total_amount=0.0,  # motor is never billed to anyone directly
            ))
        else:  # physical, tenant-billed
            results.append(MeterResult(
                meter_id=m.meter_id, label=m.label, tenant_name=m.tenant_name, type=m.type,
                reading=m.current_reading, last_reading=m.last_reading, units=units,
                unit_price=eff_price, electricity_amount=amount, water_share_amount=0.0,
                total_amount=amount, warning=warning,
            ))

    if derived:
        d = derived[0]
        eff_price = price_for(d.meter_id)
        if d.meter_id in units_ov:
            d_units = float(units_ov[d.meter_id])
        else:
            d_units = main_delta - known_units_total
        warning = None
        if d_units < 0:
            warning = (
                "Metered flats + motor add up to more than the main meter's delta. "
                "One of the readings is probably off -- fix it or set this flat's units/total below."
            )
        amount = round(d_units * eff_price, 2)
        results.append(MeterResult(
            meter_id=d.meter_id, label=d.label, tenant_name=d.tenant_name, type="derived",
            reading=None, last_reading=None, units=d_units, unit_price=eff_price,
            electricity_amount=amount, water_share_amount=0.0, total_amount=amount,
            warning=warning,
        ))

    # ---- water pool redistribution -------------------------------------
    #
    # The motor pool is ALWAYS fully spread across the flats -- total water
    # collected == water_pool, in every mode. The only knobs are:
    #   excluded flats  get 0 and are dropped from the split entirely
    #   pinned flats    (water_overrides) are held at exactly that amount
    #   everyone else   shares whatever is left of the pool, weighted by mode:
    #                     even / manual  -> equal
    #                     proportional   -> by electricity units used this month
    # So pinning one flat always re-flows the rest to keep the total exact.
    tenant_results = [r for r in results if r.type in ("physical", "derived")]
    water_by_id = {r.meter_id: r for r in tenant_results}
    excluded = set(water_excluded or [])
    for mid in (water_overrides or {}):
        if mid not in water_by_id:
            raise CalculationError(f"Unknown meter_id in water_overrides: {mid!r}")
    for mid in excluded:
        if mid not in water_by_id:
            raise CalculationError(f"Unknown meter_id in water_excluded: {mid!r}")

    water_warning = None
    if tenant_results:
        pins = {mid: round(float(a), 2) for mid, a in (water_overrides or {}).items()
                if mid not in excluded}
        for r in tenant_results:
            if r.meter_id in excluded:
                r.water_share_amount = 0.0
            elif r.meter_id in pins:
                r.water_share_amount = pins[r.meter_id]
                r.water_override = True

        auto = [r for r in tenant_results if r.meter_id not in excluded and r.meter_id not in pins]
        remaining = round(water_pool - sum(pins.values()), 2)
        if auto:
            if water_mode == "proportional":
                weights = {r.meter_id: max(r.units, 0.0) for r in auto}
                if sum(weights.values()) <= 0:
                    weights = {r.meter_id: 1.0 for r in auto}
            else:  # "even" | "manual"
                weights = {r.meter_id: 1.0 for r in auto}
            weight_sum = sum(weights.values()) or 1.0
            acc = 0.0
            for i, r in enumerate(auto):
                if i == len(auto) - 1:  # last flat absorbs the rounding so the total is exact
                    r.water_share_amount = round(remaining - acc, 2)
                else:
                    r.water_share_amount = round(remaining * weights[r.meter_id] / weight_sum, 2)
                    acc = round(acc + r.water_share_amount, 2)
            if remaining < 0:
                water_warning = ("The amounts you pinned add up to more than the water pool -- "
                                 "the other flats are showing a negative share.")
        elif abs(remaining) > 0.01:
            water_warning = (f"Pinned/excluded flats leave {remaining:g} of the water pool "
                             "unallocated (no un-pinned flat to absorb it).")

        for r in tenant_results:
            r.total_amount = round(r.electricity_amount + r.water_share_amount, 2)

    if water_warning:
        first = tenant_results[0] if tenant_results else None
        if first and not first.warning:
            first.warning = water_warning

    # ---- manual overrides ------------------------------------------------
    if overrides:
        by_id = {r.meter_id: r for r in results}
        for meter_id, new_total in overrides.items():
            if meter_id not in by_id:
                raise CalculationError(f"Unknown meter_id in overrides: {meter_id!r}")
            r = by_id[meter_id]
            r.total_amount = round(new_total, 2)
            r.manual_override = True

    total_collected = round(sum(r.total_amount for r in results if r.type in ("physical", "derived")), 2)
    achieved_margin_pct = (
        round((total_collected - main_bill_amount) / main_bill_amount * 100, 2)
        if main_bill_amount else 0.0
    )

    return MonthResult(
        unit_price=unit_price,
        main_delta=main_delta,
        water_pool_amount=round(water_pool, 2),
        total_collected=total_collected,
        main_bill_amount=main_bill_amount,
        achieved_margin_pct=achieved_margin_pct,
        meters=results,
    )


def recommended_price_from_this_month(
    main_bill_amount: float, main_delta: float, margin_pct: float
) -> float:
    """This month's actual cost per unit (bill / units the whole building
    used) plus the configured margin. Unlike heuristic_suggested_price this
    uses the bill *just entered*, so the unit-price field can pre-fill the
    instant the main reading and bill amount are in. Returns 0.0 if there
    isn't enough to compute it yet."""
    if main_delta <= 0 or main_bill_amount <= 0:
        return 0.0
    return round(main_bill_amount * (1 + margin_pct / 100) / main_delta, 2)


def heuristic_suggested_price(last_bill_amount: float, last_total_units: float, margin_pct: float) -> float:
    """The original app's formula, kept as the always-available fallback:
    last month's bill plus a margin, spread over last month's total units."""
    if last_total_units <= 0:
        raise CalculationError("Need a positive units figure from last month to suggest a price.")
    return round(last_bill_amount * (1 + margin_pct / 100) / last_total_units, 2)
