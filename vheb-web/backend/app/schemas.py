"""Pydantic request/response models for the API."""
from __future__ import annotations

from pydantic import BaseModel, Field


class MeterIn(BaseModel):
    meter_id: str
    label: str
    tenant_name: str = ""
    meter_number: str = ""
    type: str  # main | physical | motor | derived
    active: bool = True
    water_share: float = 1.0
    phone_number: str = ""  # optional: only used to build a one-tap "forward to
                             # tenant" wa.me link once you've reviewed the bill
    photo_url: str = ""
    sort_order: float = 0
    notes: str = ""
    # Only used the first time a physical/motor meter is created, to seed a
    # baseline so next month's delta has something to subtract from.
    initial_reading: float | None = None


class PhotoUpload(BaseModel):
    filename: str
    content_base64: str
    mime_type: str = "image/jpeg"


class ComputeMonthRequest(BaseModel):
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    main_current: float
    main_bill_amount: float
    readings: dict[str, float]  # meter_id -> current reading (physical + motor meters)
    last_readings: dict[str, float] = Field(default_factory=dict)  # meter_id -> last reading,
    # for meters with no history yet or whose stored baseline needs correcting
    unit_price: float | None = None  # None -> use ML/heuristic suggestion
    water_mode: str = "even"  # "even" | "proportional" | "manual"
    margin_pct: float | None = None
    overrides: dict[str, float] = Field(default_factory=dict)  # meter_id -> total_amount
    water_overrides: dict[str, float] = Field(default_factory=dict)  # meter_id -> water amount
    water_excluded: list[str] = Field(default_factory=list)  # flats away this month -> no share
    units_overrides: dict[str, float] = Field(default_factory=dict)  # meter_id -> units this month
    # (stuck / replaced / rolled-over / brand-new meter)
    unit_price_overrides: dict[str, float] = Field(default_factory=dict)  # meter_id -> Rs/unit
    main_last: float | None = None  # only needed for the very first month ever entered


class FinalizeMonthRequest(ComputeMonthRequest):
    pass


class SendBillsRequest(BaseModel):
    month: str = Field(pattern=r"^\d{4}-\d{2}$")


class SettingsUpdate(BaseModel):
    key: str
    value: str
