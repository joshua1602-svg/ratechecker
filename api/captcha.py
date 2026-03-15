"""Cloudflare Turnstile captcha verification."""
from __future__ import annotations

import os
from typing import Optional

import httpx

TURNSTILE_SECRET = os.environ.get("TURNSTILE_SECRET_KEY", "")
SKIP_CAPTCHA = os.environ.get("SKIP_CAPTCHA", "false").lower() == "true"

_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


async def verify_turnstile(token: Optional[str]) -> bool:
    """Return True if the token passes Cloudflare Turnstile verification."""
    if SKIP_CAPTCHA:
        return True
    if not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                _VERIFY_URL,
                data={"secret": TURNSTILE_SECRET, "response": token},
            )
        return bool(resp.json().get("success"))
    except Exception:
        return False
