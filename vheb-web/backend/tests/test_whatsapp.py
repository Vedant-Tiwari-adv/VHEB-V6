import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import whatsapp
from app.config import get_settings


def test_build_bill_text_matches_legacy_wording():
    text = whatsapp.build_bill_text(
        building_name="Sunrise Apts", month_label="September 2026", tenant_name="Balcony Room",
        units=42, unit_price=8.0, electricity_amount=336.0, water_amount=15.5,
        total_amount=351.5, currency="Rs",
    )
    assert text.startswith("*Electricity Bill for Balcony Room*")
    assert "September 2026  |  _Rs8.0/Unit_" in text
    assert "Amount  :  Rs336.00" in text
    assert "Water   :  Rs15.50" in text
    assert "*Total   :  Rs351.50*" in text
    assert "\n" in text  # plain multi-line text, not a file


def test_build_bill_text_omits_water_line_when_not_charging():
    text = whatsapp.build_bill_text(
        building_name="X", month_label="Sep 2026", tenant_name="Hall Upper",
        units=10, unit_price=8.0, electricity_amount=80.0, water_amount=0.0,
        total_amount=80.0, currency="Rs", charge_water=False,
    )
    assert "Water" not in text


def test_wa_me_link_strips_non_digits_and_encodes_text():
    link = whatsapp.wa_me_link("+91 98765-43210", "Hi there!")
    assert link.startswith("https://wa.me/919876543210?text=")
    assert "Hi" in link


def test_send_bill_without_admin_number_configured():
    get_settings.cache_clear()
    with patch.dict("os.environ", {"ADMIN_WHATSAPP_NUMBER": ""}, clear=False):
        get_settings.cache_clear()
        result = whatsapp.send_bill(
            tenant_name="Rita", tenant_phone=None, month_label="Sep 2026", units=10,
            unit_price=8, electricity_amount=80, water_amount=5, total_amount=85, meter_id="flat2",
        )
    assert result.ok is False
    assert result.method == "not_configured"
    get_settings.cache_clear()


def test_send_bill_without_api_config_falls_back_to_self_link():
    get_settings.cache_clear()
    env = {"ADMIN_WHATSAPP_NUMBER": "+919373834997", "WHATSAPP_TOKEN": "", "WHATSAPP_PHONE_NUMBER_ID": ""}
    with patch.dict("os.environ", env, clear=False):
        get_settings.cache_clear()
        result = whatsapp.send_bill(
            tenant_name="Rita", tenant_phone=None, month_label="Sep 2026", units=10,
            unit_price=8, electricity_amount=80, water_amount=5, total_amount=85, meter_id="flat2",
        )
    assert result.ok is False
    assert result.self_wa_me_link.startswith("https://wa.me/919373834997")
    assert result.forward_wa_me_link is None
    get_settings.cache_clear()


def test_send_bill_success_sends_free_form_text_to_admin_number():
    get_settings.cache_clear()
    env = {
        "ADMIN_WHATSAPP_NUMBER": "+919373834997",
        "WHATSAPP_TOKEN": "fake-token",
        "WHATSAPP_PHONE_NUMBER_ID": "123456",
    }
    with patch.dict("os.environ", env, clear=False):
        get_settings.cache_clear()
        with patch("app.whatsapp.requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.text = "{}"
            result = whatsapp.send_bill(
                tenant_name="Rita", tenant_phone="+919800000002", month_label="Sep 2026",
                units=10, unit_price=8, electricity_amount=80, water_amount=5,
                total_amount=85, meter_id="flat2",
            )
    assert result.ok is True
    assert result.method == "text"
    payload = mock_post.call_args.kwargs["json"]
    assert payload["type"] == "text"
    assert payload["to"] == "919373834997"  # always the admin's own number
    assert result.forward_wa_me_link.startswith("https://wa.me/919800000002")
    get_settings.cache_clear()


def test_prefer_template_skips_the_free_path():
    get_settings.cache_clear()
    env = {
        "ADMIN_WHATSAPP_NUMBER": "+919373834997",
        "WHATSAPP_TOKEN": "fake-token", "WHATSAPP_PHONE_NUMBER_ID": "123456",
        "WHATSAPP_TEMPLATE_NAME": "monthly_bill",
    }
    with patch.dict("os.environ", env, clear=False):
        get_settings.cache_clear()
        with patch("app.whatsapp.requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.text = "{}"
            result = whatsapp.send_bill(
                tenant_name="Balcony Room", tenant_phone=None, month_label="Sep 2026",
                units=10, unit_price=8, electricity_amount=80, water_amount=5,
                total_amount=85, meter_id="flat2", prefer_template=True,
            )
    assert result.ok is True and result.method == "template"
    assert mock_post.call_count == 1  # went straight to template, no free-text attempt
    payload = mock_post.call_args.kwargs["json"]
    assert payload["type"] == "template"
    params = payload["template"]["components"][0]["parameters"]
    assert len(params) == 6
    assert params[0]["text"] == "Balcony Room"  # {{1}} is the flat label
    assert params[5]["text"] == "Rs85.00"       # {{6}} is the total
    get_settings.cache_clear()


def test_send_bill_falls_back_to_template_when_free_text_fails():
    get_settings.cache_clear()
    env = {
        "ADMIN_WHATSAPP_NUMBER": "+919373834997",
        "WHATSAPP_TOKEN": "fake-token",
        "WHATSAPP_PHONE_NUMBER_ID": "123456",
        "WHATSAPP_TEMPLATE_NAME": "monthly_bill",
    }
    with patch.dict("os.environ", env, clear=False):
        get_settings.cache_clear()
        with patch("app.whatsapp.requests.post") as mock_post:
            fail_resp = type("R", (), {"status_code": 400, "text": "window closed"})()
            ok_resp = type("R", (), {"status_code": 200, "text": "{}"})()
            mock_post.side_effect = [fail_resp, ok_resp]
            result = whatsapp.send_bill(
                tenant_name="Rita", tenant_phone=None, month_label="Sep 2026",
                units=10, unit_price=8, electricity_amount=80, water_amount=5,
                total_amount=85, meter_id="flat2",
            )
    assert result.ok is True
    assert result.method == "template"
    assert mock_post.call_count == 2
    get_settings.cache_clear()
