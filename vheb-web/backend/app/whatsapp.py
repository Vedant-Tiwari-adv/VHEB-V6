"""
WhatsApp delivery -- everything goes to YOUR OWN number (ADMIN_WHATSAPP_NUMBER).
You review each bill there, then forward it to the tenant yourself. Nothing
in this app messages a tenant's phone directly.

Why this can be genuinely free, not just "official and cheap"
-----------------------------------------------------------------
Meta only requires a pre-approved TEMPLATE (and only then bills a small
per-message "utility" rate) for business-initiated messages sent OUTSIDE
an open 24-hour customer-service window. That window opens for free the
moment a user messages your WhatsApp Business number, and since here the
"user" receiving every message is *you*, opening it is entirely in your
control: before running the monthly bill, send any message (even just
"hi") from your own phone to your business number. For the next 24
hours, free-form text replies are free -- unlimited, no template, no
Meta review, no per-message charge (service conversations have been
free since Nov 2024). This module tries that free-form path first.

If you forget to open the window, the send will fail with a clear
error, and:
  - if WHATSAPP_TEMPLATE_NAME is configured *and approved*, it retries
    with that template (billed at Meta's small utility rate -- see
    README step 4 for the exact template text to submit), otherwise
  - it hands back a wa.me link addressed to your own number with the
    text pre-filled, so you can send it to yourself with one tap and
    lose nothing.

Every result also carries a second wa.me link addressed to the
tenant's own number (if you've saved one on their meter) with the same
text pre-filled -- a one-tap way to actually forward it on once you've
checked it, as an alternative to WhatsApp's native "Forward" button.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from urllib.parse import quote

import requests

from .config import get_settings

GRAPH_BASE = "https://graph.facebook.com"


class WhatsAppError(RuntimeError):
    pass


@dataclass
class SendResult:
    meter_id: str
    tenant_name: str
    method: str  # "text" | "template" | "not_configured"
    ok: bool
    detail: str
    self_wa_me_link: str          # always present: send-to-self fallback
    forward_wa_me_link: str | None  # present only if the tenant has a phone number on file


def build_bill_text(*, building_name: str, month_label: str, tenant_name: str,
                     units: float, unit_price: float, electricity_amount: float,
                     water_amount: float, total_amount: float, currency: str = "Rs",
                     charge_water: bool = True) -> str:
    """Plain text only -- no PDF, this is exactly what gets sent/previewed.

    This mirrors the old desktop app's message wording on purpose (tenants
    have seen it for years), with only the column alignment tidied up.
    `tenant_name` here is the flat's name -- every message lands on the
    same phone (yours), so the flat has to be obvious in the header."""
    lines = [
        f"*Electricity Bill for {tenant_name}*",
        f"{month_label}  |  _{currency}{unit_price}/Unit_",
        "",
        f"Units   :  {units:g}",
        f"Amount  :  {currency}{electricity_amount:.2f}",
    ]
    if charge_water:
        lines.append(f"Water   :  {currency}{water_amount:.2f}")
    lines.append(f"*Total   :  {currency}{total_amount:.2f}*")
    return "\n".join(lines)


def wa_me_link(phone_number: str, text: str) -> str:
    digits = "".join(ch for ch in phone_number if ch.isdigit())
    return f"https://wa.me/{digits}?text={quote(text)}"


def _digits(phone: str) -> str:
    return "".join(ch for ch in phone if ch.isdigit())


def _meta_error(resp) -> str:
    """Turn Meta's JSON error into one short, actionable line."""
    try:
        err = resp.json().get("error", {}) or {}
    except Exception:
        err = {}
    code = err.get("code")
    if code == 190:
        return (
            "WhatsApp access token is expired or invalid. Generate a fresh one on "
            "developers.facebook.com (WhatsApp -> API Setup for a 24h token, or a System "
            "User token that doesn't expire -- README step 4), put it in WHATSAPP_TOKEN, "
            "and restart. Then just press Send again -- the month stays saved."
        )
    if code == 131047 or "re-engagement" in str(err.get("message", "")).lower():
        return ("The free 24-hour window is closed. Send any message from your phone to "
                "your WhatsApp Business number, then press Send again.")
    msg = err.get("message") or resp.text[:200]
    return f"Meta API error {resp.status_code}: {msg}"


def _send_text(to: str, body: str) -> tuple[bool, str]:
    settings = get_settings()
    url = f"{GRAPH_BASE}/{settings.whatsapp_api_version}/{settings.whatsapp_phone_number_id}/messages"
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}
    resp = requests.post(url, json=payload,
                          headers={"Authorization": f"Bearer {settings.whatsapp_token}"}, timeout=15)
    if resp.status_code >= 400:
        return False, _meta_error(resp)
    return True, "Sent as a free-form message (24h window)."


# The 6 body parameters the app fills into the approved template, in order.
# Static structure (headings, newlines, currency, bold) lives in the template
# itself -- WhatsApp forbids newlines inside a parameter value, so each of
# these must be a single short line. The template body to submit once in
# WhatsApp Manager (category: Utility, language: en):
#
#     *Electricity Bill for {{1}}*
#     {{2}}
#
#     Units: {{3}}
#     Amount: {{4}}
#     Water: {{5}}
#     *Total: {{6}}*
#
def _template_params(*, flat_label: str, month_label: str, units: float,
                     electricity_amount: float, water_amount: float,
                     total_amount: float, currency: str) -> list[dict]:
    values = [
        flat_label, month_label, f"{units:g}",
        f"{currency}{electricity_amount:.2f}",
        f"{currency}{water_amount:.2f}",
        f"{currency}{total_amount:.2f}",
    ]
    return [{"type": "text", "text": v} for v in values]


def _send_template(to: str, params: list[dict]) -> tuple[bool, str]:
    settings = get_settings()
    url = f"{GRAPH_BASE}/{settings.whatsapp_api_version}/{settings.whatsapp_phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp", "to": to, "type": "template",
        "template": {
            "name": settings.whatsapp_template_name,
            "language": {"code": settings.whatsapp_template_lang},
            "components": [{"type": "body", "parameters": params}],
        },
    }
    resp = requests.post(url, json=payload,
                          headers={"Authorization": f"Bearer {settings.whatsapp_token}"}, timeout=15)
    if resp.status_code >= 400:
        return False, _meta_error(resp)
    return True, "Sent via the approved template (billed at Meta's small utility rate)."


def send_bill(*, tenant_name: str, tenant_phone: str | None, month_label: str,
              units: float, unit_price: float, electricity_amount: float,
              water_amount: float, total_amount: float, meter_id: str,
              prefer_template: bool = False) -> SendResult:
    """prefer_template=True skips the free 24h-window path entirely and sends
    via the approved template every time -- so you never have to message your
    own number first. Billed at Meta's small utility rate (a few paise each)."""
    settings = get_settings()
    text = build_bill_text(
        building_name=settings.building_name, month_label=month_label, tenant_name=tenant_name,
        units=units, unit_price=unit_price, electricity_amount=electricity_amount,
        water_amount=water_amount, total_amount=total_amount, currency=settings.currency_symbol,
    )
    self_link = wa_me_link(settings.admin_whatsapp_number or "", text) if settings.admin_whatsapp_number else ""
    forward_link = wa_me_link(tenant_phone, text) if tenant_phone else None

    if not settings.admin_whatsapp_number:
        return SendResult(meter_id, tenant_name, "not_configured", False,
                           "ADMIN_WHATSAPP_NUMBER isn't set -- see README step 4.",
                           self_link, forward_link)
    if not settings.whatsapp_token or not settings.whatsapp_phone_number_id:
        return SendResult(meter_id, tenant_name, "not_configured", False,
                           "WhatsApp Cloud API isn't configured -- use the link below for now.",
                           self_link, forward_link)

    to = _digits(settings.admin_whatsapp_number)
    tmpl_params = _template_params(
        flat_label=tenant_name, month_label=month_label, units=units,
        electricity_amount=electricity_amount, water_amount=water_amount,
        total_amount=total_amount, currency=settings.currency_symbol,
    )

    if prefer_template and settings.whatsapp_template_name:
        ok, detail = _send_template(to, tmpl_params)
        if ok:
            return SendResult(meter_id, tenant_name, "template", True, detail, self_link, forward_link)
        # fall through and try the free path as a backup
        first_detail = f"Template send failed: {detail}"
    else:
        first_detail = None

    ok, detail = _send_text(to, text)
    if ok:
        return SendResult(meter_id, tenant_name, "text", True, detail, self_link, forward_link)
    if first_detail:
        detail = f"{first_detail}\n{detail}"

    if not prefer_template and settings.whatsapp_template_name:
        ok2, detail2 = _send_template(to, tmpl_params)
        if ok2:
            return SendResult(meter_id, tenant_name, "template", True, detail2, self_link, forward_link)
        detail = f"{detail}\nTemplate attempt also failed: {detail2}"

    return SendResult(
        meter_id, tenant_name, "text", False,
        f"{detail}\nEither send any message from your phone to your WhatsApp Business "
        "number to open the free 24h window, or approve the 'monthly_bill' template once "
        "and turn on 'always use template' in Settings -- then this just works. "
        "For now, tap the link below.",
        self_link, forward_link,
    )


def config_status() -> dict:
    """What's configured, and (if possible) whether the token is still valid --
    a live GET against Meta, no message sent. Used by the Settings page."""
    s = get_settings()
    out = {
        "admin_number": bool(s.admin_whatsapp_number),
        "token": bool(s.whatsapp_token),
        "phone_number_id": bool(s.whatsapp_phone_number_id),
        "template_name": s.whatsapp_template_name or "",
        "token_ok": None,
        "detail": "",
    }
    if not (out["token"] and out["phone_number_id"]):
        out["detail"] = "Token and/or phone number ID not set."
        return out
    try:
        resp = requests.get(
            f"{GRAPH_BASE}/{s.whatsapp_api_version}/{s.whatsapp_phone_number_id}",
            params={"fields": "display_phone_number,verified_name"},
            headers={"Authorization": f"Bearer {s.whatsapp_token}"}, timeout=10,
        )
        if resp.status_code < 400:
            d = resp.json()
            out["token_ok"] = True
            out["detail"] = f"Connected as {d.get('verified_name') or '?'} ({d.get('display_phone_number') or '?'})."
        else:
            out["token_ok"] = False
            out["detail"] = _meta_error(resp)
    except requests.RequestException as e:
        out["detail"] = f"Could not reach Meta: {e}"
    return out


def send_test() -> dict:
    """Send one short message to the admin's own number, right now."""
    s = get_settings()
    text = f"VHEB test — WhatsApp Cloud API is working. {dt.datetime.now():%d %b %Y, %H:%M}"
    self_link = wa_me_link(s.admin_whatsapp_number, text) if s.admin_whatsapp_number else ""
    if not s.admin_whatsapp_number:
        return {"ok": False, "detail": "ADMIN_WHATSAPP_NUMBER isn't set.", "self_wa_me_link": ""}
    if not s.whatsapp_token or not s.whatsapp_phone_number_id:
        return {"ok": False, "detail": "WhatsApp token / phone number ID not set -- tap the link instead.",
                "self_wa_me_link": self_link}
    ok, detail = _send_text(_digits(s.admin_whatsapp_number), text)
    return {"ok": ok, "detail": detail, "self_wa_me_link": self_link}
