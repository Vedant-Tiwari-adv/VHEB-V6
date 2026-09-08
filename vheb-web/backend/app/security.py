"""
Single shared-secret auth. Deliberately simple: this is a household /
single-admin tool, not a multi-user product, and it's about to sit on
the public internet on a free host -- so every /api/* route (other
than /api/health) requires `Authorization: Bearer <ADMIN_TOKEN>`.

If you ever need more than one admin with their own login, swap this
dependency out for real per-user auth; nothing else in the app assumes
a single shared token.
"""
from __future__ import annotations

import hmac

from fastapi import Header, HTTPException

from .config import get_settings


def require_auth(authorization: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if not settings.admin_token:
        raise HTTPException(500, "ADMIN_TOKEN is not configured on the server.")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token.")
    token = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token, settings.admin_token):
        raise HTTPException(401, "Invalid token.")
