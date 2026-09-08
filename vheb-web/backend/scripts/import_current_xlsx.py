#!/usr/bin/env python3
"""
Import the client's *current* Electricity.xlsx (the trimmed one that
starts at January 2026) into the Google Sheet in this app's long,
month-wise format.

    python scripts/import_current_xlsx.py /path/to/Electricity.xlsx            # dry run: prints every row it would write
    python scripts/import_current_xlsx.py /path/to/Electricity.xlsx --json-out preview.json
    python scripts/import_current_xlsx.py /path/to/Electricity.xlsx --push     # CLEARS the 4 tabs and rewrites them

Unlike migrate_legacy.py this is not "best effort" over a decade of
drifting layouts -- the current file has one clean layout (4-column
month blocks, row 2 == "Price Per Unit") and only 8 months. Every
figure is taken verbatim from the sheet; the only values overridden are
the ones the owner confirmed by hand:

    March 2026  NagarNigam Bill Amount  ->  2700   (sheet had 45749, a paste of the reading)
    August 2026 Price Per Unit          ->  8      (sheet cell was blank)

Where a row's recorded rupee amount doesn't divide evenly by its units
(July's per-tenant amounts, Hall Upper's fixed charge while its meter is
stuck at 100) the amount is kept exactly as recorded and the row is
flagged manual_override=TRUE with a note, rather than "corrected".
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import openpyxl

# xlsx row -> (meter_id, label, type, meter_number, tenant_name)
ROWS = [
    (6,  "front_ground", "Front Ground", "physical", "5", "Marvari"),
    (7,  "back_ground",  "Back Ground",  "derived",  "",  "Hanu"),
    (8,  "balcony_room", "Balcony Room", "physical", "4", "Rita"),
    (9,  "hall_upper",   "Hall Upper",   "physical", "1", "Anamika"),
    (10, "middle_upper", "Middle Upper", "physical", "2", "Nimisha"),
    (11, "back_upper",   "Back Upper",   "physical", "3", "3 idiots"),
    (12, "motor",        "Motor",        "motor",    "0", "Motor"),
]
FLAT_COUNT = 6  # Front/Back Ground + Balcony + Hall + Middle + Back Upper -- motor is not a flat

# Owner-confirmed corrections, keyed by "YYYY-MM".
BILL_AMOUNT_FIX = {"2026-03": 2700}
UNIT_PRICE_FIX = {"2026-08": 8.0}


def _num(v):
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.replace(",", "").strip())
        except ValueError:
            return None
    return None


def _month_key(v) -> str | None:
    if isinstance(v, dt.datetime):
        return f"{v.year:04d}-{v.month:02d}"
    if isinstance(v, str):
        months = ["january", "february", "march", "april", "may", "june", "july",
                  "august", "september", "october", "november", "december"]
        parts = v.replace(",", " ").split()
        mn = yr = None
        for p in parts:
            if p.lower() in months:
                mn = months.index(p.lower()) + 1
            elif p.isdigit() and len(p) == 4:
                yr = int(p)
        if mn and yr:
            return f"{yr:04d}-{mn:02d}"
    return None


def parse_workbook(path: str) -> list[dict]:
    ws = openpyxl.load_workbook(filename=path, data_only=True).active
    blocks = []
    for c in range(1, ws.max_column + 1):
        if ws.cell(row=2, column=c).value != "Price Per Unit":
            continue
        month = _month_key(ws.cell(row=1, column=c).value)
        if not month:
            continue
        unit_price = UNIT_PRICE_FIX.get(month) or _num(ws.cell(row=2, column=c + 2).value)
        main_reading = _num(ws.cell(row=3, column=c + 2).value)
        bill_amount = BILL_AMOUNT_FIX.get(month) or _num(ws.cell(row=4, column=c + 2).value)
        if unit_price is None or main_reading is None or bill_amount is None:
            print(f"  ! skipped {month}: missing price / main reading / bill amount")
            continue
        rows = {}
        for xl_row, meter_id, *_ in ROWS:
            reading = _num(ws.cell(row=xl_row, column=c).value)      # "N.A." -> None
            units = _num(ws.cell(row=xl_row, column=c + 1).value)
            amount = _num(ws.cell(row=xl_row, column=c + 2).value)
            rows[meter_id] = {"reading": reading, "units": units or 0.0, "amount": amount or 0.0}
        blocks.append({
            "month": month, "unit_price": unit_price, "main_reading": main_reading,
            "bill_amount": bill_amount, "rows": rows,
        })
    blocks.sort(key=lambda b: b["month"])
    return blocks


def build_rows(blocks: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (readings_rows, summary_rows), month-ordered, with last_reading
    chained from the previous month and a reconstructed baseline for the first."""
    reading_rows, summary_rows = [], []
    prev_reading: dict[str, float] = {}
    prev_main: float | None = None

    for b in blocks:
        month = b["month"]
        motor_amount = b["rows"]["motor"]["amount"]
        water_each = round(motor_amount / FLAT_COUNT, 2)
        total_units = sum(r["units"] for r in b["rows"].values())
        main_last = prev_main if prev_main is not None else round(b["main_reading"] - total_units, 2)

        collected = 0.0
        for xl_row, meter_id, label, mtype, *_ in ROWS:
            r = b["rows"][meter_id]
            units, amount, reading = r["units"], r["amount"], r["reading"]
            last_reading = prev_reading.get(meter_id)
            if last_reading is None and reading is not None:
                last_reading = round(reading - units, 2)  # first month: reconstruct baseline

            row_price = round(amount / units, 4) if units > 0 else b["unit_price"]
            clean = units > 0 and abs(amount - units * b["unit_price"]) < 0.5
            note = ""
            if mtype != "motor" and not clean:
                note = ("fixed charge -- meter reading not advancing" if units == 0
                        else "per-tenant amount as recorded in the source sheet")

            if mtype == "motor":
                water_amt, total = 0.0, 0.0
            else:
                water_amt = water_each
                total = round(amount + water_amt, 2)
                collected += total

            reading_rows.append({
                "month": month, "meter_id": meter_id,
                "reading": "" if reading is None else reading,
                "last_reading": "" if last_reading is None else last_reading,
                "units": units, "unit_price": row_price,
                "electricity_amount": amount,
                "water_share_amount": "" if mtype == "motor" else water_amt,
                "total_amount": total,
                "manual_override": "TRUE" if note else "FALSE",
                "notes": note, "entered_at": "",
            })
            if reading is not None:
                prev_reading[meter_id] = reading

        summary_rows.append({
            "month": month, "main_meter_reading": b["main_reading"],
            "main_last_reading": main_last, "main_bill_amount": b["bill_amount"],
            "unit_price": b["unit_price"], "margin_pct": "",
            "water_pool_amount": motor_amount, "water_mode": "even",
            "total_collected": round(collected, 2), "status": "imported",
            "suggested_price": "", "created_at": "", "finalized_at": "",
        })
        prev_main = b["main_reading"]
    return reading_rows, summary_rows


def meter_rows() -> list[dict]:
    out = []
    for i, (_, meter_id, label, mtype, number, tenant) in enumerate(ROWS):
        out.append({
            "meter_id": meter_id, "label": label, "tenant_name": tenant,
            "meter_number": number, "type": mtype, "active": "TRUE", "water_share": 1,
            "phone_number": "", "photo_url": "", "sort_order": i, "notes": "",
        })
    out.append({
        "meter_id": "main", "label": "Main / NagarNigam", "tenant_name": "", "meter_number": "",
        "type": "main", "active": "TRUE", "water_share": "", "phone_number": "",
        "photo_url": "", "sort_order": -1, "notes": "",
    })
    return out


def _print_table(readings, summaries):
    print(f"\n{'month':8} {'meter':13} {'read':>8} {'last':>8} {'units':>6} "
          f"{'price':>7} {'elec':>9} {'water':>7} {'total':>9}  note")
    for r in readings:
        print(f"{r['month']:8} {r['meter_id']:13} {str(r['reading']):>8} {str(r['last_reading']):>8} "
              f"{r['units']:>6} {str(r['unit_price']):>7} {r['electricity_amount']:>9} "
              f"{str(r['water_share_amount']):>7} {str(r['total_amount']):>9}  {r['notes']}")
    print(f"\n{'month':8} {'mainRead':>9} {'mainLast':>9} {'bill':>8} {'price':>6} "
          f"{'pool':>8} {'collected':>10} status")
    for s in summaries:
        print(f"{s['month']:8} {s['main_meter_reading']:>9} {s['main_last_reading']:>9} "
              f"{s['main_bill_amount']:>8} {s['unit_price']:>6} {s['water_pool_amount']:>8} "
              f"{s['total_collected']:>10} {s['status']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx_path")
    ap.add_argument("--push", action="store_true",
                    help="CLEAR the Meters/Readings/MonthlySummary tabs and rewrite them from this file")
    ap.add_argument("--json-out")
    args = ap.parse_args()

    blocks = parse_workbook(args.xlsx_path)
    print(f"Parsed {len(blocks)} months: {blocks[0]['month']} .. {blocks[-1]['month']}")
    meters = meter_rows()
    readings, summaries = build_rows(blocks)
    _print_table(readings, summaries)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            {"meters": meters, "readings": readings, "summaries": summaries}, indent=2, default=str))
        print(f"\nWrote {args.json_out}")

    if not args.push:
        print("\nDry run only. Re-run with --push to CLEAR and rewrite the Google Sheet tabs.")
        return

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app import sheets_client as sc

    print("\nClearing and rewriting tabs...")
    for name, headers, rows in [
        ("Meters", sc.METERS_HEADERS, meters),
        ("Readings", sc.READINGS_HEADERS, readings),
        ("MonthlySummary", sc.SUMMARY_HEADERS, summaries),
    ]:
        ws = sc.get_ws(name)
        ws.clear()
        ws.append_row(headers)
        ws.append_rows([[str(r.get(h, "")) for h in headers] for r in rows])
        print(f"  {name}: {len(rows)} rows")

    for k, v in {
        "building_name": "VHEB", "currency_symbol": "Rs",
        "default_margin_pct": "7", "default_water_mode": "even",
    }.items():
        sc.set_setting(k, v)
    print("  Settings: seeded")
    print("\nDone. Open the app and spot-check History.")


if __name__ == "__main__":
    main()
