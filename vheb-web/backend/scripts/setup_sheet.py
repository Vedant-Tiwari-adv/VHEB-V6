#!/usr/bin/env python3
"""
One-time setup: creates the four worksheets (Meters, Readings,
MonthlySummary, Settings) with the right headers in a Google Sheet you
already created and shared with the service account, and seeds a few
sensible default Settings values.

Usage:
    python scripts/setup_sheet.py

Needs SPREADSHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON set (README step 3).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import sheets_client as sc
from app.config import get_settings


def main():
    settings = get_settings()
    for name in sc.WORKSHEETS:
        sc.get_ws(name)
        print(f"  Worksheet ready: {name}")

    defaults = {
        "default_margin_pct": str(settings.default_margin_pct),
        "default_water_mode": "even",
        "building_name": settings.building_name,
        "whatsapp_template_name": settings.whatsapp_template_name,
    }
    existing = sc.get_settings_dict()
    for key, value in defaults.items():
        if key not in existing:
            sc.set_setting(key, value)
            print(f"  Setting seeded: {key} = {value}")

    print("\nDone. You can now:")
    print("  - run scripts/migrate_legacy.py --push to import your old Electricity.xlsx, and/or")
    print("  - add meters by hand from the app's Meters page, and/or")
    print("  - delete the sheet's default 'Sheet1' tab if it's still sitting there empty.")


if __name__ == "__main__":
    main()
