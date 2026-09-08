"""
End-to-end test of the wired-together API: fake Google Sheets backing
store (in-memory dicts/lists) swapped in for app.sheets_client, real
FastAPI TestClient, real calculation + pricing code. WhatsApp sending
is mocked so no network call happens.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("ADMIN_TOKEN", "test-token")

from fastapi.testclient import TestClient

from app import sheets_client as sc
from app.config import get_settings
from app.main import app

AUTH = {"Authorization": "Bearer test-token"}


class FakeStore:
    def __init__(self):
        self.meters: dict[str, dict] = {}
        self.readings: dict[tuple[str, str], dict] = {}
        self.summaries: dict[str, dict] = {}
        self.settings: dict[str, str] = {}


@pytest.fixture
def store(monkeypatch):
    fs = FakeStore()

    def list_meters(active_only=False):
        rows = list(fs.meters.values())
        if active_only:
            rows = [r for r in rows if str(r.get("active")).upper() in ("TRUE", "1", "YES")]
        return sorted(rows, key=lambda r: float(r.get("sort_order") or 999))

    def upsert_meter(meter):
        fs.meters[meter["meter_id"]] = dict(meter)

    def delete_meter(meter_id, hard):
        if hard:
            fs.meters.pop(meter_id, None)
        else:
            fs.meters[meter_id]["active"] = "FALSE"

    def meter_has_history(meter_id):
        return any(k[1] == meter_id for k in fs.readings)

    def list_readings(month=None):
        rows = list(fs.readings.values())
        if month:
            rows = [r for r in rows if r["month"] == month]
        return rows

    def last_reading_for(meter_id, before_month=None):
        rows = [r for r in fs.readings.values() if r["meter_id"] == meter_id]
        if before_month:
            rows = [r for r in rows if r["month"] < before_month]
        if not rows:
            return None
        return sorted(rows, key=lambda r: r["month"])[-1]

    def upsert_readings(month, rows_by_meter):
        for meter_id, data in rows_by_meter.items():
            fs.readings[(month, meter_id)] = {**data, "month": month, "meter_id": meter_id}

    def list_summaries():
        return sorted(fs.summaries.values(), key=lambda r: r["month"])

    def get_summary(month):
        return fs.summaries.get(month)

    def upsert_summary(month, data):
        fs.summaries[month] = dict(data)

    def get_settings_dict():
        return dict(fs.settings)

    def set_setting(key, value):
        fs.settings[key] = value

    for name, fn in [
        ("list_meters", list_meters), ("upsert_meter", upsert_meter),
        ("delete_meter", delete_meter), ("meter_has_history", meter_has_history),
        ("list_readings", list_readings), ("last_reading_for", last_reading_for),
        ("upsert_readings", upsert_readings), ("list_summaries", list_summaries),
        ("get_summary", get_summary), ("upsert_summary", upsert_summary),
        ("get_settings_dict", get_settings_dict), ("set_setting", set_setting),
    ]:
        monkeypatch.setattr(sc, name, fn)

    get_settings.cache_clear()
    yield fs
    get_settings.cache_clear()


@pytest.fixture
def client():
    return TestClient(app)


def _seed_meters(client):
    meters = [
        {"meter_id": "main", "label": "Main", "type": "main", "sort_order": -1},
        {"meter_id": "flat1", "label": "Front Ground", "tenant_name": "Marvari", "type": "physical",
         "phone_number": "+919800000001", "initial_reading": 100, "sort_order": 1},
        {"meter_id": "flat2", "label": "Balcony Room", "tenant_name": "Rita", "type": "physical",
         "phone_number": "+919800000002", "initial_reading": 50, "sort_order": 2},
        {"meter_id": "flat3", "label": "Hall Upper", "tenant_name": "Anamika", "type": "physical",
         "phone_number": "+919800000003", "initial_reading": 80, "sort_order": 3},
        {"meter_id": "flat4", "label": "Middle Upper", "tenant_name": "Nimisha", "type": "physical",
         "phone_number": "+919800000004", "initial_reading": 40, "sort_order": 4},
        {"meter_id": "flat5", "label": "Back Upper", "tenant_name": "3 idiots", "type": "physical",
         "phone_number": "+919800000005", "initial_reading": 60, "sort_order": 5},
        {"meter_id": "motor", "label": "Motor", "tenant_name": "Motor", "type": "motor",
         "initial_reading": 20, "sort_order": 6},
        {"meter_id": "back_ground", "label": "Back Ground", "tenant_name": "Hanu", "type": "derived",
         "phone_number": "+919800000007", "sort_order": 7},
    ]
    for m in meters:
        resp = client.post("/api/meters", json=m, headers=AUTH)
        assert resp.status_code == 200, resp.text
    # give each physical/motor meter a "last month" reading row so this month has a delta
    from app import sheets_client as scmod
    for meter_id, reading in [("flat1", 100), ("flat2", 50), ("flat3", 80), ("flat4", 40),
                               ("flat5", 60), ("motor", 20)]:
        scmod.upsert_readings("2026-08", {meter_id: {
            "reading": reading, "last_reading": "", "units": 0, "unit_price": "",
            "electricity_amount": 0, "water_share_amount": 0, "total_amount": 0,
            "manual_override": "FALSE", "notes": "", "entered_at": "",
        }})
    scmod.upsert_summary("2026-08", {
        "month": "2026-08", "main_meter_reading": 200, "main_last_reading": "",
        "main_bill_amount": 1000, "unit_price": 7.5, "margin_pct": 7,
        "water_pool_amount": 75, "water_mode": "even", "total_collected": 1500,
        "status": "finalized", "suggested_price": "", "created_at": "", "finalized_at": "",
    })


def test_auth_required(store, client):
    assert client.get("/api/meters").status_code == 401
    assert client.get("/api/meters", headers=AUTH).status_code == 200


def test_full_month_lifecycle(store, client):
    _seed_meters(client)

    compute_payload = {
        "month": "2026-09", "main_current": 400, "main_bill_amount": 1000,
        "readings": {"flat1": 140, "flat2": 80, "flat3": 105, "flat4": 75, "flat5": 80, "motor": 30},
        "unit_price": 7.5, "water_mode": "even",
    }
    resp = client.post("/api/months/compute", json=compute_payload, headers=AUTH)
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["main_delta"] == 200
    derived = next(m for m in result["meters"] if m["type"] == "derived")
    assert derived["units"] == 40  # matches the hand-derived example in test_calculations.py

    resp = client.post("/api/months/finalize", json=compute_payload, headers=AUTH)
    assert resp.status_code == 200, resp.text

    resp = client.get("/api/months/2026-09", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["status"] == "finalized"
    assert len(body["readings"]) == 7  # 5 flats + motor + derived (main isn't a Reading row)


def test_compute_accepts_unit_and_price_overrides(store, client):
    _seed_meters(client)
    payload = {
        "month": "2026-09", "main_current": 400, "main_bill_amount": 1000,
        "readings": {"flat1": 140, "flat2": 80, "flat3": 105, "flat4": 75, "flat5": 80, "motor": 30},
        "unit_price": 7.5, "water_mode": "even",
        "units_overrides": {"flat3": 3},                 # meter stuck -- bill 3 units flat
        "unit_price_overrides": {"flat1": 9.0},          # this flat on its own rate
        "water_overrides": {"flat2": 40},               # pin one flat's water by hand
    }
    resp = client.post("/api/months/compute", json=payload, headers=AUTH)
    assert resp.status_code == 200, resp.text
    meters = {m["meter_id"]: m for m in resp.json()["meters"]}
    assert meters["flat3"]["units"] == 3
    assert meters["flat1"]["unit_price"] == 9.0
    assert meters["flat1"]["electricity_amount"] == round(meters["flat1"]["units"] * 9.0, 2)
    assert meters["flat2"]["water_share_amount"] == 40
    assert resp.json()["recommended_unit_price"] > 0


def test_compute_manual_water_and_exclusion_over_http(store, client):
    _seed_meters(client)
    base = {
        "month": "2026-09", "main_current": 400, "main_bill_amount": 1000,
        "readings": {"flat1": 140, "flat2": 80, "flat3": 105, "flat4": 75, "flat5": 80, "motor": 30},
        "unit_price": 7.5,
    }
    # manual: pin flat1 at 30, exclude flat2 -> pool spreads over flat1(pinned) + the rest
    resp = client.post("/api/months/compute", json={
        **base, "water_mode": "manual",
        "water_overrides": {"flat1": 30}, "water_excluded": ["flat2"],
    }, headers=AUTH)
    assert resp.status_code == 200, resp.text
    meters = {m["meter_id"]: m for m in resp.json()["meters"]}
    assert meters["flat1"]["water_share_amount"] == 30
    assert meters["flat2"]["water_share_amount"] == 0
    pool = resp.json()["water_pool_amount"]
    total_water = sum(m["water_share_amount"] for m in resp.json()["meters"]
                      if m["type"] in ("physical", "derived"))
    assert abs(total_water - pool) < 0.05


def test_compute_with_manual_last_reading_for_meter_without_history(store, client):
    _seed_meters(client)
    # flat3 has a 2026-08 baseline from _seed_meters; simulate correcting it
    payload = {
        "month": "2026-09", "main_current": 400, "main_bill_amount": 1000,
        "readings": {"flat1": 140, "flat2": 80, "flat3": 105, "flat4": 75, "flat5": 80, "motor": 30},
        "last_readings": {"flat3": 90},  # override the stored baseline of 80
        "unit_price": 7.5, "water_mode": "proportional",
    }
    resp = client.post("/api/months/compute", json=payload, headers=AUTH)
    assert resp.status_code == 200, resp.text
    flat3 = next(m for m in resp.json()["meters"] if m["meter_id"] == "flat3")
    assert flat3["units"] == 15  # 105 - 90, not 105 - 80


def test_send_bills_needs_a_saved_month(store, client):
    _seed_meters(client)
    resp = client.post("/api/months/send", json={"month": "2026-09"}, headers=AUTH)
    assert resp.status_code == 400  # nothing saved for 2026-09 yet


def test_can_resend_an_older_saved_month(store, client, monkeypatch):
    _seed_meters(client)
    from app import whatsapp as wa
    monkeypatch.setattr(wa, "send_bill", lambda **k: wa.SendResult(
        k["meter_id"], k["tenant_name"], "text", True, "ok", "https://wa.me/1", None))
    # 2026-08 was seeded with reading rows but no "finalized" summary status
    resp = client.post("/api/months/send", json={"month": "2026-08"}, headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["results"]) >= 1


def test_whatsapp_status_and_test_endpoints(store, client, monkeypatch):
    from app import whatsapp as wa
    monkeypatch.setattr(wa, "config_status", lambda: {"token_ok": True, "detail": "Connected."})
    monkeypatch.setattr(wa, "send_test", lambda: {"ok": True, "detail": "sent", "self_wa_me_link": "https://wa.me/1"})
    assert client.get("/api/whatsapp/status", headers=AUTH).json()["token_ok"] is True
    assert client.post("/api/whatsapp/test", headers=AUTH).json()["ok"] is True
    assert client.get("/api/whatsapp/status").status_code == 401


def test_send_bills_after_finalize(store, client, monkeypatch):
    _seed_meters(client)
    compute_payload = {
        "month": "2026-09", "main_current": 400, "main_bill_amount": 1000,
        "readings": {"flat1": 140, "flat2": 80, "flat3": 105, "flat4": 75, "flat5": 80, "motor": 30},
        "unit_price": 7.5, "water_mode": "even",
    }
    client.post("/api/months/finalize", json=compute_payload, headers=AUTH)

    from app import whatsapp as wa

    def fake_send_bill(**kwargs):
        return wa.SendResult(kwargs["meter_id"], kwargs["tenant_name"], "text", True,
                              "Sent (fake).", "https://wa.me/919373834997", None)

    monkeypatch.setattr(wa, "send_bill", fake_send_bill)
    resp = client.post("/api/months/send", json={"month": "2026-09"}, headers=AUTH)
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) == 6  # 5 flats + derived; motor is never billed
    assert all(r["ok"] for r in results)


def test_meter_hard_delete_blocked_once_it_has_history(store, client):
    _seed_meters(client)
    compute_payload = {
        "month": "2026-09", "main_current": 400, "main_bill_amount": 1000,
        "readings": {"flat1": 140, "flat2": 80, "flat3": 105, "flat4": 75, "flat5": 80, "motor": 30},
        "unit_price": 7.5, "water_mode": "even",
    }
    client.post("/api/months/finalize", json=compute_payload, headers=AUTH)
    resp = client.delete("/api/meters/flat1?hard=true", headers=AUTH)
    assert resp.status_code == 400
    resp = client.delete("/api/meters/flat1?hard=false", headers=AUTH)
    assert resp.status_code == 200
