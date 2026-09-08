"""
Thin wrapper around the live Google Sheets API (via gspread) for the
four worksheets this app uses. Every read/write happens directly on the
user's Google Sheet -- there is no local database and nothing is cached
to disk, so the Sheet is always the single source of truth.

Worksheet layout (created automatically by scripts/setup_sheet.py):

Meters
  meter_id | label | tenant_name | meter_number | type | active |
  water_share | phone_number | photo_url | sort_order | notes

Readings   (one row per month per meter, upserted)
  month | meter_id | reading | last_reading | units | unit_price |
  electricity_amount | water_share_amount | total_amount |
  manual_override | notes | entered_at

MonthlySummary  (one row per month)
  month | main_meter_reading | main_last_reading | main_bill_amount |
  unit_price | margin_pct | water_pool_amount | water_mode |
  total_collected | status | suggested_price | created_at | finalized_at

Settings  (key/value)
  key | value
"""
from __future__ import annotations

import threading
import time
from functools import lru_cache
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

from .config import get_settings

# ---------------------------------------------------------------- cache --
# Google's Sheets API allows ~60 reads/min/user. A single page in the UI
# used to fan out to ~15 reads (one per _records() call, plus a per-meter
# loop), so a couple of clicks tripped the quota. This app is used briefly,
# by one person, once a month -- so a short-TTL in-memory cache of each
# worksheet's rows, wiped on any write, is safe and collapses a page load
# to at most one read per tab.
_CACHE_TTL = 25.0
_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_ws_handles: dict[str, Any] = {}
_lock = threading.Lock()


def _invalidate_cache() -> None:
    with _lock:
        _cache.clear()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

METERS_HEADERS = [
    "meter_id", "label", "tenant_name", "meter_number", "type", "active",
    "water_share", "phone_number", "photo_url", "sort_order", "notes",
]
READINGS_HEADERS = [
    "month", "meter_id", "reading", "last_reading", "units", "unit_price",
    "electricity_amount", "water_share_amount", "total_amount",
    "manual_override", "notes", "entered_at",
]
SUMMARY_HEADERS = [
    "month", "main_meter_reading", "main_last_reading", "main_bill_amount",
    "unit_price", "margin_pct", "water_pool_amount", "water_mode",
    "total_collected", "status", "suggested_price", "created_at", "finalized_at",
]
SETTINGS_HEADERS = ["key", "value"]

WORKSHEETS = {
    "Meters": METERS_HEADERS,
    "Readings": READINGS_HEADERS,
    "MonthlySummary": SUMMARY_HEADERS,
    "Settings": SETTINGS_HEADERS,
}


@lru_cache
def _client() -> gspread.Client:
    settings = get_settings()
    creds = Credentials.from_service_account_info(
        settings.google_service_account_info, scopes=SCOPES
    )
    return gspread.authorize(creds)


@lru_cache
def _spreadsheet():
    settings = get_settings()
    if not settings.spreadsheet_id:
        raise RuntimeError("SPREADSHEET_ID is not set. See README step 3.")
    return _client().open_by_key(settings.spreadsheet_id)


def get_ws(name: str):
    """Get a worksheet by name, creating it with the right headers if missing.
    Worksheet handles are cached -- the tab set never changes at runtime."""
    if name in _ws_handles:
        return _ws_handles[name]
    sh = _spreadsheet()
    try:
        ws = sh.worksheet(name)
    except gspread.WorksheetNotFound:
        headers = WORKSHEETS[name]
        ws = sh.add_worksheet(title=name, rows=1000, cols=len(headers) + 2)
        ws.append_row(headers)
    _ws_handles[name] = ws
    return ws


def _records(ws) -> list[dict[str, Any]]:
    """gspread's get_all_records but tolerant of blank trailing rows."""
    return [r for r in ws.get_all_records() if any(str(v).strip() for v in r.values())]


def _rows(name: str) -> list[dict[str, Any]]:
    """Cached rows for a worksheet. One live read per tab per _CACHE_TTL
    window; wiped immediately by any write via _invalidate_cache()."""
    now = time.monotonic()
    hit = _cache.get(name)
    if hit and now - hit[0] < _CACHE_TTL:
        return list(hit[1])  # copy: callers sort/mutate the list in place
    rows = _records(get_ws(name))
    with _lock:
        _cache[name] = (now, rows)
    return list(rows)


# ---------------------------------------------------------------- Meters --
def list_meters(active_only: bool = False) -> list[dict[str, Any]]:
    rows = _rows("Meters")
    if active_only:
        rows = [r for r in rows if str(r.get("active", "")).strip().upper() in ("TRUE", "1", "YES")]
    rows.sort(key=lambda r: (_safe_float(r.get("sort_order"), 999), r.get("label", "")))
    return rows


def _safe_float(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def upsert_meter(meter: dict[str, Any]) -> None:
    ws = get_ws("Meters")
    rows = ws.get_all_values()
    header = rows[0] if rows else METERS_HEADERS
    idx = {h: i for i, h in enumerate(header)}
    meter_id = str(meter["meter_id"])
    target_row = None
    for i, row in enumerate(rows[1:], start=2):
        if len(row) > idx["meter_id"] and row[idx["meter_id"]] == meter_id:
            target_row = i
            break
    values = [str(meter.get(h, "")) for h in header]
    if target_row:
        ws.update(f"A{target_row}", [values])
    else:
        ws.append_row(values)
    _invalidate_cache()


def delete_meter(meter_id: str, hard: bool) -> None:
    ws = get_ws("Meters")
    rows = ws.get_all_values()
    header = rows[0]
    idx = {h: i for i, h in enumerate(header)}
    for i, row in enumerate(rows[1:], start=2):
        if len(row) > idx["meter_id"] and row[idx["meter_id"]] == meter_id:
            if hard:
                ws.delete_rows(i)
            else:
                ws.update_cell(i, idx["active"] + 1, "FALSE")
            _invalidate_cache()
            return
    raise KeyError(f"meter_id {meter_id!r} not found")


def meter_has_history(meter_id: str) -> bool:
    return any(r.get("meter_id") == meter_id for r in _rows("Readings"))


# -------------------------------------------------------------- Readings --
def list_readings(month: str | None = None) -> list[dict[str, Any]]:
    rows = _rows("Readings")
    if month:
        rows = [r for r in rows if r.get("month") == month]
    return rows


def last_reading_for(meter_id: str, before_month: str | None = None) -> dict[str, Any] | None:
    """Most recent Readings row for a meter, optionally strictly before a given month."""
    rows = [r for r in _rows("Readings") if r.get("meter_id") == meter_id]
    if before_month:
        rows = [r for r in rows if str(r.get("month")) < before_month]
    if not rows:
        return None
    rows.sort(key=lambda r: str(r.get("month")))
    return rows[-1]


def upsert_readings(month: str, rows_by_meter: dict[str, dict[str, Any]]) -> None:
    """Write/overwrite the Readings rows for one month in a single batch."""
    ws = get_ws("Readings")
    existing = ws.get_all_values()
    header = existing[0] if existing else READINGS_HEADERS
    idx = {h: i for i, h in enumerate(header)}
    existing_index = {
        (row[idx["month"]], row[idx["meter_id"]]): i
        for i, row in enumerate(existing[1:], start=2)
        if len(row) > idx["meter_id"]
    }
    updates = []
    appends = []
    for meter_id, data in rows_by_meter.items():
        data = {**data, "month": month, "meter_id": meter_id}
        values = [str(data.get(h, "")) for h in header]
        key = (month, meter_id)
        if key in existing_index:
            updates.append((existing_index[key], values))
        else:
            appends.append(values)
    for row_num, values in updates:
        ws.update(f"A{row_num}", [values])
    if appends:
        ws.append_rows(appends)
    _invalidate_cache()


# --------------------------------------------------------- MonthlySummary --
def list_summaries() -> list[dict[str, Any]]:
    rows = _rows("MonthlySummary")
    rows.sort(key=lambda r: str(r.get("month")))
    return rows


def get_summary(month: str) -> dict[str, Any] | None:
    for r in _rows("MonthlySummary"):
        if r.get("month") == month:
            return r
    return None


def upsert_summary(month: str, data: dict[str, Any]) -> None:
    ws = get_ws("MonthlySummary")
    rows = ws.get_all_values()
    header = rows[0] if rows else SUMMARY_HEADERS
    idx = {h: i for i, h in enumerate(header)}
    values = [str(data.get(h, "")) for h in header]
    for i, row in enumerate(rows[1:], start=2):
        if len(row) > idx["month"] and row[idx["month"]] == month:
            ws.update(f"A{i}", [values])
            _invalidate_cache()
            return
    ws.append_row(values)
    _invalidate_cache()


# --------------------------------------------------------------- Settings --
def get_settings_dict() -> dict[str, str]:
    return {r["key"]: r["value"] for r in _rows("Settings") if r.get("key")}


def set_setting(key: str, value: str) -> None:
    ws = get_ws("Settings")
    rows = ws.get_all_values()
    for i, row in enumerate(rows[1:], start=2):
        if row and row[0] == key:
            ws.update_cell(i, 2, value)
            _invalidate_cache()
            return
    ws.append_row([key, value])
    _invalidate_cache()
