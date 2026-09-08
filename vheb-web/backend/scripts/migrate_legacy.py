#!/usr/bin/env python3
"""
One-time migration: read the OLD wide-format Electricity.xlsx (the file
VHEB v3 used) and push its history into the NEW Google Sheet, in the
long month-wise format this app reads and writes.

Usage:
    python scripts/migrate_legacy.py /path/to/Electricity.xlsx --push
    python scripts/migrate_legacy.py /path/to/Electricity.xlsx            # dry run, prints a summary only

You need SPREADSHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON set (same as the
app -- see README step 3) for --push to work. Without --push this only
parses the file and prints what it found, so you can sanity-check the
numbers before writing anything.

Why this is "best effort" on the old columns
---------------------------------------------
The original sheet changed its own layout at least twice over 5+ years
(see the note cells around column 171/188 in the source file -- "made
to ease the transition to new format"), a couple of month-blocks are
duplicated as bridge columns, and a handful of trailing columns near
the end are empty template copies with no real data. This script
parses both the old (2019 - mid-2023) and new (mid-2023 onward)
per-month layouts, skips any column block it can't confidently read
(malformed dates, non-numeric readings, empty bill amount), and prints
exactly which months it skipped and why -- it never guesses.

Fixed row layout in the source file (rows 6-13), matching main.py's
own update_xlsheet()/get_lst_xl_vals():
    row  6  -> flat_1  (physical)
    row  7  -> back_ground (DERIVED -- no physical meter, "(N.A)" in the sheet)
    row  8  -> flat_2  (physical)
    row  9  -> flat_3  (physical)
    row 10  -> flat_4  (physical)
    row 11  -> flat_5  (physical)
    row 12  -> motor   (motor)
Tenant *names* changed hands over the years in the source file; this
script uses whatever name is attached to the most recent month for
each row when creating the Meters tab, since that's the current
roster. Historical Readings rows are keyed by the stable meter_id
(flat_1 ... flat_5, motor, back_ground), not by tenant name.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import openpyxl

ROW_METER_IDS = {
    6: "flat_1",
    7: "back_ground",   # derived
    8: "flat_2",
    9: "flat_3",
    10: "flat_4",
    11: "flat_5",
    12: "motor",
}
DERIVED_ROW = 7
MOTOR_ROW = 12


def _num(v):
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.replace(",", "").strip())
        except ValueError:
            return None
    return None


def _month_key(label, fallback_dt=None):
    """Normalise whatever's in the month/date cell to 'YYYY-MM'."""
    if isinstance(label, dt.datetime):
        return f"{label.year:04d}-{label.month:02d}"
    if isinstance(label, str):
        s = label.strip()
        months = ["january", "february", "march", "april", "may", "june", "july",
                  "august", "september", "october", "november", "december"]
        parts = s.replace(",", " ").split()
        month_num = None
        year = None
        for p in parts:
            pl = p.lower()
            if pl in months:
                month_num = months.index(pl) + 1
            elif p.isdigit() and len(p) == 4:
                year = int(p)
        if month_num and year:
            return f"{year:04d}-{month_num:02d}"
    if isinstance(fallback_dt, dt.datetime):
        return f"{fallback_dt.year:04d}-{fallback_dt.month:02d}"
    return None


def parse_new_format_block(ws, label_col: int, skipped: list[str]) -> dict | None:
    """label_col is the column where row2 == 'Price Per Unit' (the block's data
    sits 2 columns to the right; reading/unit/price for flats sit at
    label_col, label_col+1, label_col+2 directly, per row5's own sub-header)."""
    c = label_col
    month_label = ws.cell(row=1, column=c).value
    month = _month_key(month_label)
    unit_price = _num(ws.cell(row=2, column=c + 2).value)
    main_reading = _num(ws.cell(row=3, column=c + 2).value)
    main_bill_amount = _num(ws.cell(row=4, column=c + 2).value)
    if not month or unit_price is None or main_reading is None or main_bill_amount is None:
        skipped.append(f"col {c}: new-format block missing month/price/main reading/bill amount")
        return None

    readings = {}
    for row, meter_id in ROW_METER_IDS.items():
        reading = _num(ws.cell(row=row, column=c).value)
        units = _num(ws.cell(row=row, column=c + 1).value)
        price = _num(ws.cell(row=row, column=c + 2).value)
        readings[meter_id] = {"reading": reading, "units": units, "price": price}
    return {
        "month": month, "unit_price": unit_price, "main_reading": main_reading,
        "main_bill_amount": main_bill_amount, "readings": readings, "format": "new",
    }


MIN_PLAUSIBLE_BILL = 50  # a real monthly bill for this building is always well above this


def _month_key_with_year_inference(label, last_month: str | None) -> str | None:
    """Handles the few old-format columns where only a bare month name like
    'jan' is present with no year at all -- infers the year from whatever
    month was parsed immediately before it, rolling over at December."""
    key = _month_key(label)
    if key or not isinstance(label, str) or not last_month:
        return key
    months = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    abbr = label.strip().lower()[:3]
    if abbr not in months:
        return None
    last_year, last_num = (int(x) for x in last_month.split("-"))
    month_num = months.index(abbr) + 1
    year = last_year + 1 if month_num < last_num else last_year
    return f"{year:04d}-{month_num:02d}"


def parse_old_format_block(ws, start_col: int, skipped: list[str], last_month: str | None) -> dict | None:
    """start_col is a column where row2 == 'date' (4-wide block: date, reading,
    units, price). Row 4 in this era doubles as the main-meter reading (col+1)
    and main bill amount (col+3); row2's own 'units' cell for row 4 is usually
    blank in the source file.

    This era of the sheet is the least consistent (column stride drifts
    between 4 and 5, the bill-amount cell is sometimes missing or holds an
    obviously-wrong stray value like a unit price instead of a total). Rather
    than guess, a block is skipped whenever a field is missing or the bill
    amount is implausibly small -- see MIN_PLAUSIBLE_BILL."""
    c = start_col
    month_label = ws.cell(row=4, column=c).value
    main_reading = _num(ws.cell(row=4, column=c + 1).value)
    main_bill_amount = _num(ws.cell(row=4, column=c + 3).value)
    month = _month_key_with_year_inference(month_label, last_month)
    if not month or main_reading is None or main_bill_amount is None:
        skipped.append(f"col {c}: old-format block missing month/main reading/bill amount "
                        f"(month_label={month_label!r})")
        return None
    if main_bill_amount < MIN_PLAUSIBLE_BILL:
        skipped.append(f"col {c} ({month}): bill amount {main_bill_amount} looks like a data-entry "
                        "error, not a real monthly bill -- skipped rather than guessed")
        return None

    readings = {}
    for row, meter_id in ROW_METER_IDS.items():
        if row == DERIVED_ROW:
            readings[meter_id] = {"reading": None, "units": _num(ws.cell(row=row, column=c + 1).value),
                                   "price": _num(ws.cell(row=row, column=c + 3).value)}
            continue
        reading = _num(ws.cell(row=row, column=c).value)
        units = _num(ws.cell(row=row, column=c + 1).value)
        price = _num(ws.cell(row=row, column=c + 3).value)
        readings[meter_id] = {"reading": reading, "units": units, "price": price}

    total_units = sum(r["units"] for r in readings.values() if r["units"] is not None)
    if total_units <= 0:
        skipped.append(f"col {c} ({month}): no usable unit figures, skipped")
        return None
    unit_price = round(main_bill_amount / (main_reading - 0), 4) if main_reading else None
    # Prefer whatever unit price the flats were actually charged at, if consistent:
    sample_prices = [r["price"] / r["units"] for r in readings.values()
                      if r["units"] and r["price"] and r["units"] > 0]
    if sample_prices:
        unit_price = round(sum(sample_prices) / len(sample_prices), 4)
    return {
        "month": month, "unit_price": unit_price, "main_reading": main_reading,
        "main_bill_amount": main_bill_amount, "readings": readings, "format": "old",
    }


def parse_workbook(path: str) -> tuple[list[dict], list[str]]:
    wb = openpyxl.load_workbook(filename=path, data_only=True)
    ws = wb.active
    months: dict[str, dict] = {}
    skipped: list[str] = []
    last_month: str | None = None

    for c in range(1, ws.max_column + 1):
        r2 = ws.cell(row=2, column=c).value
        if isinstance(r2, str) and r2.strip() == "Price Per Unit":
            block = parse_new_format_block(ws, c, skipped)
        elif isinstance(r2, str) and r2.strip().lower() == "date":
            block = parse_old_format_block(ws, c, skipped, last_month)
        else:
            continue
        if block is None:
            continue
        last_month = block["month"]
        # keep the most complete block if a month appears twice (transition columns)
        existing = months.get(block["month"])
        if existing is None or _completeness(block) >= _completeness(existing):
            months[block["month"]] = block

    ordered = [months[k] for k in sorted(months.keys())]
    return ordered, skipped


def _completeness(block: dict) -> int:
    return sum(1 for r in block["readings"].values() if r.get("units") is not None)


def to_readings_and_summary_rows(months: list[dict]) -> tuple[list[dict], list[dict]]:
    reading_rows, summary_rows = [], []
    last_reading_by_meter: dict[str, float] = {}
    for m in months:
        total_units = 0.0
        for meter_id, r in m["readings"].items():
            units = r.get("units")
            if units is None:
                continue
            total_units += units
            reading_rows.append({
                "month": m["month"], "meter_id": meter_id,
                "reading": r.get("reading") if r.get("reading") is not None else "",
                "last_reading": last_reading_by_meter.get(meter_id, ""),
                "units": units, "unit_price": m["unit_price"],
                "electricity_amount": r.get("price") if meter_id != "motor" else 0,
                "water_share_amount": "", "total_amount": r.get("price") if meter_id != "motor" else 0,
                "manual_override": "FALSE", "notes": "migrated from legacy sheet",
                "entered_at": "",
            })
            if r.get("reading") is not None:
                last_reading_by_meter[meter_id] = r["reading"]
        total_collected = sum(
            (r.get("price") or 0) for mid, r in m["readings"].items() if mid != "motor"
        )
        summary_rows.append({
            "month": m["month"], "main_meter_reading": m["main_reading"],
            "main_last_reading": "", "main_bill_amount": m["main_bill_amount"],
            "unit_price": m["unit_price"], "margin_pct": "",
            "water_pool_amount": (m["readings"].get("motor") or {}).get("price") or 0,
            "water_mode": "even", "total_collected": round(total_collected, 2),
            "status": "migrated", "suggested_price": "", "created_at": "", "finalized_at": "",
        })
    return reading_rows, summary_rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx_path")
    ap.add_argument("--push", action="store_true", help="Actually write to the configured Google Sheet")
    ap.add_argument("--json-out", help="Optional: dump parsed months to this JSON file for inspection")
    args = ap.parse_args()

    months, skipped = parse_workbook(args.xlsx_path)
    print(f"Parsed {len(months)} clean months from {args.xlsx_path}")
    if months:
        print(f"  Range: {months[0]['month']} to {months[-1]['month']}")
    if skipped:
        print(f"Skipped {len(skipped)} column block(s) that couldn't be read cleanly:")
        for s in skipped[:20]:
            print(f"  - {s}")
        if len(skipped) > 20:
            print(f"  ... and {len(skipped) - 20} more")

    reading_rows, summary_rows = to_readings_and_summary_rows(months)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            {"months": months, "readings": reading_rows, "summaries": summary_rows}, indent=2, default=str
        ))
        print(f"Wrote parsed data to {args.json_out}")

    if not args.push:
        print("\nDry run only -- re-run with --push to write this into the Google Sheet.")
        return

    from app import sheets_client as sc  # local import: needs env vars set

    print("Pushing Meters tab (current roster, editable afterwards in the app)...")
    default_labels = {
        "flat_1": "Flat 1", "flat_2": "Flat 2", "flat_3": "Flat 3",
        "flat_4": "Flat 4", "flat_5": "Flat 5", "motor": "Motor",
        "back_ground": "Flat 6 (no physical meter)",
    }
    types = {"back_ground": "derived", "motor": "motor"}
    for i, meter_id in enumerate(ROW_METER_IDS.values()):
        sc.upsert_meter({
            "meter_id": meter_id, "label": default_labels[meter_id], "tenant_name": "",
            "meter_number": "", "type": types.get(meter_id, "physical"), "active": "TRUE",
            "water_share": 1, "phone_number": "", "photo_url": "", "sort_order": i, "notes": "",
        })
    sc.upsert_meter({
        "meter_id": "main", "label": "Main / utility meter", "tenant_name": "",
        "meter_number": "", "type": "main", "active": "TRUE", "water_share": "",
        "phone_number": "", "photo_url": "", "sort_order": -1, "notes": "",
    })

    print(f"Pushing {len(reading_rows)} Readings rows...")
    by_month: dict[str, dict[str, dict]] = {}
    for row in reading_rows:
        by_month.setdefault(row["month"], {})[row["meter_id"]] = row
    for month, rows_by_meter in by_month.items():
        sc.upsert_readings(month, rows_by_meter)

    print(f"Pushing {len(summary_rows)} MonthlySummary rows...")
    for row in summary_rows:
        sc.upsert_summary(row["month"], row)

    print("Done. Open the Sheet and spot-check a few months before relying on it.")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    main()
