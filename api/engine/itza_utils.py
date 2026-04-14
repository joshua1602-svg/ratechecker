"""Shared ITZA helpers used by CSA and reporting."""
from __future__ import annotations

from typing import Any


def retail_itza_from_sv_lines_effective(sv_lines: list[dict[str, Any]]) -> float:
    """Compute effective retail ITZA from VOA SV lines using standard relativities."""
    if not sv_lines:
        return 0.0

    prices = [
        float(r["price"])
        for r in sv_lines
        if r.get("price") is not None and float(r["price"]) > 0
    ]
    zone_a_price = max(prices) if prices else None

    total = 0.0
    for line in sv_lines:
        area = float(line.get("area") or 0.0)
        if area <= 0:
            continue

        desc = str(line.get("description") or "").lower()
        floor = str(line.get("floor") or "").lower()
        rel: float | None = None

        if "zone a" in desc:
            rel = 1.0
        elif "zone b" in desc:
            rel = 0.5
        elif "zone c" in desc:
            rel = 0.25
        elif "remainder" in desc:
            rel = 0.125

        is_basement = (
            "basement" in desc
            or "lower ground" in desc
            or "basement" in floor
            or "lower ground" in floor
        )

        if rel is None and ("storage" in desc or "internal store" in desc):
            rel = 0.10
        elif rel is None and "kitchen" in desc:
            rel = 0.05 if is_basement else 0.10
        elif rel is None and ("basement" in desc or "lower ground" in desc):
            rel = 0.20

        if rel is None and zone_a_price and line.get("price") is not None and float(line["price"]) > 0:
            rel = float(line["price"]) / zone_a_price
        if rel is None:
            rel = 1.0

        total += area * rel
    return total
