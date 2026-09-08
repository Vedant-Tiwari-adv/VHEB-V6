"""
FastAPI app: JSON API under /api/*, plus the static frontend served
from the same process (so the whole thing is one deployable web
service -- see README "Deploy").
"""
from __future__ import annotations
from gspread.exceptions import APIError as GSpreadAPIError
import dataclasses
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import billing_service, drive_photos, sheets_client, whatsapp
from .calculations import CalculationError
from .config import get_settings
from .schemas import ComputeMonthRequest, FinalizeMonthRequest, MeterIn, PhotoUpload, SendBillsRequest, SettingsUpdate
from .security import require_auth

app = FastAPI(title="VHEB - Building Utility Biller")


@app.exception_handler(RuntimeError)
async def runtime_error_handler(request: Request, exc: RuntimeError):
    # Config-missing errors (unset SPREADSHEET_ID, service account, etc.) raise
    # RuntimeError with an actionable message pointing at a README step --
    # surface that to the caller instead of a bare 500.
    return JSONResponse(status_code=503, content={"detail": str(exc)})

@app.exception_handler(PermissionError)
async def permission_error_handler(request: Request, exc: PermissionError):
    return JSONResponse(status_code=503, content={
           "detail": "Google rejected the request (403). Most likely cause: the Sheets/Drive "
                      "API isn't enabled yet on your Cloud project, or the Sheet isn't shared "
                      "with your service account's email. See README step 2-3."
    })


@app.exception_handler(GSpreadAPIError)
async def gspread_api_error_handler(request: Request, exc: GSpreadAPIError):
    return JSONResponse(status_code=503, content={"detail": f"Google Sheets API error: {exc}"})

settings = get_settings()
if settings.extra_cors_origins:
    app.add_middleware(
        CORSMiddleware, allow_origins=settings.extra_cors_origins,
        allow_methods=["*"], allow_headers=["*"],
    )


def asdict(obj):
    return dataclasses.asdict(obj) if dataclasses.is_dataclass(obj) else obj


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/auth/verify", dependencies=[Depends(require_auth)])
def verify_token():
    return {"ok": True}


# ------------------------------------------------------------------ Meters --
@app.get("/api/meters", dependencies=[Depends(require_auth)])
def list_meters(active_only: bool = False):
    return sheets_client.list_meters(active_only=active_only)


@app.post("/api/meters", dependencies=[Depends(require_auth)])
def upsert_meter(meter: MeterIn):
    data = meter.model_dump(exclude={"initial_reading"})
    data["active"] = "TRUE" if meter.active else "FALSE"
    sheets_client.upsert_meter(data)
    if meter.initial_reading is not None and meter.type in ("physical", "motor"):
        sheets_client.upsert_readings("0000-00", {
            meter.meter_id: {
                "reading": meter.initial_reading, "last_reading": "", "units": 0,
                "unit_price": "", "electricity_amount": 0, "water_share_amount": 0,
                "total_amount": 0, "manual_override": "FALSE", "notes": "baseline reading",
                "entered_at": "",
            }
        })
    return {"ok": True}


@app.delete("/api/meters/{meter_id}", dependencies=[Depends(require_auth)])
def delete_meter(meter_id: str, hard: bool = False):
    if hard and sheets_client.meter_has_history(meter_id):
        raise HTTPException(
            400,
            "This meter has reading history -- hard-deleting it would corrupt past months' "
            "records. Deactivate it instead (hard=false), or delete it without ever having "
            "entered a reading for it.",
        )
    try:
        sheets_client.delete_meter(meter_id, hard=hard)
    except KeyError:
        raise HTTPException(404, "Meter not found.")
    return {"ok": True}


@app.post("/api/meters/{meter_id}/photo", dependencies=[Depends(require_auth)])
def upload_meter_photo(meter_id: str, photo: PhotoUpload):
    try:
        url = drive_photos.upload_photo(meter_id, photo.filename, photo.content_base64, photo.mime_type)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    meters = {m["meter_id"]: m for m in sheets_client.list_meters()}
    meter = meters.get(meter_id)
    if not meter:
        raise HTTPException(404, "Meter not found.")
    meter["photo_url"] = url
    sheets_client.upsert_meter(meter)
    return {"photo_url": url}


# ------------------------------------------------------------------ Months --
@app.get("/api/months", dependencies=[Depends(require_auth)])
def list_months():
    return sheets_client.list_summaries()


@app.get("/api/months/{month}", dependencies=[Depends(require_auth)])
def get_month(month: str):
    summary = sheets_client.get_summary(month)
    if not summary:
        raise HTTPException(404, "No data for that month.")
    return {"summary": summary, "readings": sheets_client.list_readings(month)}


@app.get("/api/months/draft/new", dependencies=[Depends(require_auth)])
def draft_new_month():
    meters = sheets_client.list_meters(active_only=True)
    last_readings = {}
    for m in meters:
        if m["type"] not in ("physical", "motor"):
            continue
        last = sheets_client.last_reading_for(m["meter_id"])
        last_readings[m["meter_id"]] = float(last["reading"]) if last and last.get("reading") not in ("", None) else None
    # "9999-99" sorts after every real "YYYY-MM" month, so this asks
    # get_main_last for the most recent summary overall.
    main_last = billing_service.get_main_last(month="9999-99")
    try:
        suggestion = billing_service.suggest_price()
    except ValueError:
        suggestion = None
    return {
        "meters": meters,
        "last_readings": last_readings,
        "main_last_reading": main_last,
        "suggested_price": asdict(suggestion) if suggestion else None,
        "needs_main_last_bootstrap": main_last is None,
    }


@app.post("/api/months/compute", dependencies=[Depends(require_auth)])
def compute_month_route(req: ComputeMonthRequest):
    try:
        result = billing_service.compute(
            month=req.month, main_current=req.main_current, main_bill_amount=req.main_bill_amount,
            readings=req.readings, unit_price=req.unit_price, water_mode=req.water_mode,
            margin_pct=req.margin_pct, overrides=req.overrides,
            water_overrides=req.water_overrides, water_excluded=req.water_excluded,
            units_overrides=req.units_overrides,
            unit_price_overrides=req.unit_price_overrides, last_readings=req.last_readings,
            main_last_input=req.main_last,
        )
    except CalculationError as e:
        raise HTTPException(400, str(e))
    return asdict(result)


@app.post("/api/months/finalize", dependencies=[Depends(require_auth)])
def finalize_month_route(req: FinalizeMonthRequest):
    try:
        result = billing_service.compute(
            month=req.month, main_current=req.main_current, main_bill_amount=req.main_bill_amount,
            readings=req.readings, unit_price=req.unit_price, water_mode=req.water_mode,
            margin_pct=req.margin_pct, overrides=req.overrides,
            water_overrides=req.water_overrides, water_excluded=req.water_excluded,
            units_overrides=req.units_overrides,
            unit_price_overrides=req.unit_price_overrides, last_readings=req.last_readings,
            main_last_input=req.main_last,
        )
        billing_service.finalize(
            month=req.month, result=result, water_mode=req.water_mode,
            margin_pct=req.margin_pct, main_last_input=req.main_last,
        )
    except CalculationError as e:
        raise HTTPException(400, str(e))
    return asdict(result)


@app.post("/api/months/send", dependencies=[Depends(require_auth)])
def send_bills_route(req: SendBillsRequest):
    summary = sheets_client.get_summary(req.month)
    if not summary or summary.get("status") != "finalized":
        raise HTTPException(400, "Finalize this month before sending bills.")
    readings = {r["meter_id"]: r for r in sheets_client.list_readings(req.month)}
    meters = {m["meter_id"]: m for m in sheets_client.list_meters()}

    results = []
    for meter_id, r in readings.items():
        meter = meters.get(meter_id)
        if not meter or meter["type"] not in ("physical", "derived"):
            continue
        outcome = whatsapp.send_bill(
            tenant_name=meter.get("label") or meter.get("tenant_name") or meter_id,
            tenant_phone=meter.get("phone_number") or None,
            month_label=req.month,
            units=float(r["units"]), unit_price=float(r["unit_price"]),
            electricity_amount=float(r["electricity_amount"]),
            water_amount=float(r["water_share_amount"] or 0),
            total_amount=float(r["total_amount"]), meter_id=meter_id,
        )
        results.append(asdict(outcome))
    return {"results": results}


# ---------------------------------------------------------------- Pricing --
@app.get("/api/pricing/suggested", dependencies=[Depends(require_auth)])
def pricing_suggested(margin_pct: float | None = None):
    try:
        return asdict(billing_service.suggest_price(margin_pct))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/pricing/backtest", dependencies=[Depends(require_auth)])
def pricing_backtest(margin_pct: float | None = None):
    return asdict(billing_service.get_backtest(margin_pct))


# ---------------------------------------------------------------- Settings --
@app.get("/api/settings", dependencies=[Depends(require_auth)])
def get_settings_route():
    return sheets_client.get_settings_dict()


@app.post("/api/settings", dependencies=[Depends(require_auth)])
def set_setting_route(update: SettingsUpdate):
    sheets_client.set_setting(update.key, update.value)
    return {"ok": True}


# ------------------------------------------------------------- Static site --
_frontend_dir = Path(__file__).resolve().parents[2] / "frontend"
if _frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(_frontend_dir), html=True), name="frontend")
