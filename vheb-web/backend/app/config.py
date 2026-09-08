"""
Central configuration, loaded from environment variables.

Locally, values come from a `.env` file (see .env.example) loaded via
python-dotenv. On Render (or any host), set these as real environment
variables in the dashboard -- never commit a filled-in .env.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


class Settings:
    """Read fresh from the environment on every instantiation (not at class
    -definition/import time) so `get_settings.cache_clear()` + re-call always
    reflects whatever's currently in os.environ -- this is what makes the
    settings testable with monkeypatched env vars, and safe if a host ever
    reloads env vars without a full process restart."""

    def __init__(self) -> None:
        # --- Auth ---------------------------------------------------
        # A single shared secret the frontend sends as `Authorization: Bearer <token>`.
        # Good enough for a single-admin household tool; see README "Security notes"
        # if you ever want per-user logins instead.
        self.admin_token: str = os.environ.get("ADMIN_TOKEN", "")

        # --- Google Sheets / Drive -------------------------------------
        # Paste the *entire* service-account JSON key as one env var value.
        self.google_service_account_json: str = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        self.spreadsheet_id: str = os.environ.get("SPREADSHEET_ID", "")
        self.drive_photos_folder_id: str = os.environ.get("DRIVE_PHOTOS_FOLDER_ID", "")

        # --- WhatsApp Cloud API -----------------------------------------
        # Every bill message goes to YOUR OWN number -- you review, then
        # forward each one to the tenant yourself. No per-tenant setup on
        # Meta's side, no template approval required for the common case
        # (see app/whatsapp.py docstring for why).
        self.whatsapp_token: str = os.environ.get("WHATSAPP_TOKEN", "")
        self.whatsapp_phone_number_id: str = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
        self.admin_whatsapp_number: str = os.environ.get("ADMIN_WHATSAPP_NUMBER", "")
        self.whatsapp_template_name: str = os.environ.get("WHATSAPP_TEMPLATE_NAME", "monthly_bill")
        self.whatsapp_template_lang: str = os.environ.get("WHATSAPP_TEMPLATE_LANG", "en")
        self.whatsapp_api_version: str = os.environ.get("WHATSAPP_API_VERSION", "v21.0")

        # --- App-level defaults (overridable per-request / in Settings tab) --
        self.default_margin_pct: float = float(os.environ.get("DEFAULT_MARGIN_PCT", "7"))
        self.building_name: str = os.environ.get("BUILDING_NAME", "The Building")
        self.currency_symbol: str = os.environ.get("CURRENCY_SYMBOL", "Rs")
        self.timezone: str = os.environ.get("TIMEZONE", "Asia/Kolkata")

        # --- CORS (only needed if you serve the frontend from a different
        # origin than the API; not needed when FastAPI serves the static
        # frontend itself, which is the default setup in this project) ---
        self.extra_cors_origins: list[str] = [
            o.strip() for o in os.environ.get("EXTRA_CORS_ORIGINS", "").split(",") if o.strip()
        ]

    @property
    def google_service_account_info(self) -> dict:
        if not self.google_service_account_json:
            raise RuntimeError(
                "GOOGLE_SERVICE_ACCOUNT_JSON is not set. Paste the full service-account "
                "JSON key (as one line) into that environment variable. See README step 2."
            )
        return json.loads(self.google_service_account_json)


@lru_cache
def get_settings() -> Settings:
    return Settings()
