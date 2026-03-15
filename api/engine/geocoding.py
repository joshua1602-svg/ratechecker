"""Postcode → (latitude, longitude) using the free postcodes.io API."""
from __future__ import annotations

from typing import Optional, Tuple

import httpx


async def postcode_to_coords(postcode: str) -> Optional[Tuple[float, float]]:
    """Return (lat, lon) for a UK postcode, or None if not found."""
    clean = postcode.replace(" ", "").upper()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"https://api.postcodes.io/postcodes/{clean}")
        if resp.status_code != 200:
            return None
        result = resp.json().get("result") or {}
        lat = result.get("latitude")
        lon = result.get("longitude")
        if lat is None or lon is None:
            return None
        return float(lat), float(lon)
    except Exception:
        return None
