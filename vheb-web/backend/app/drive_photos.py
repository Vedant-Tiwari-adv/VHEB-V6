"""
Optional: upload a meter photo (from the browser's file picker, sent to
us as base64) into a Google Drive folder, using the same service
account as the Sheets client. Returns a shareable view link that gets
stored in the Meters tab's photo_url column -- no other file storage
is used anywhere in this app.
"""
from __future__ import annotations

import base64
import io

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from .config import get_settings

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def _drive_service():
    settings = get_settings()
    creds = Credentials.from_service_account_info(settings.google_service_account_info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def upload_photo(meter_id: str, filename: str, content_base64: str, mime_type: str) -> str:
    settings = get_settings()
    if not settings.drive_photos_folder_id:
        raise RuntimeError(
            "DRIVE_PHOTOS_FOLDER_ID isn't set. Create a Drive folder, share it with the "
            "service account's email (Editor access), and put its ID in that env var."
        )
    raw = base64.b64decode(content_base64.split(",")[-1])  # tolerate a data: URL prefix
    service = _drive_service()
    media = MediaIoBaseUpload(io.BytesIO(raw), mimetype=mime_type, resumable=False)
    file = service.files().create(
        body={
            "name": f"{meter_id}-{filename}",
            "parents": [settings.drive_photos_folder_id],
        },
        media_body=media,
        fields="id",
    ).execute()
    file_id = file["id"]
    service.permissions().create(
        fileId=file_id, body={"role": "reader", "type": "anyone"},
    ).execute()
    return f"https://drive.google.com/uc?id={file_id}"
