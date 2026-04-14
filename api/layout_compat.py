from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

CANONICAL_UPPER_LEVELS = {"first", "second", "third", "mezzanine", "upper"}
CANONICAL_LOWER_LEVELS = {"lower_ground", "basement"}
_ALLOWED_LEVELS = {"ground", *CANONICAL_LOWER_LEVELS, *CANONICAL_UPPER_LEVELS}


def _f(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _floor_total(uses: dict[str, Any]) -> float:
    return _f(uses.get("trading_sqm")) + _f(uses.get("storage_sqm")) + _f(uses.get("kitchen_sqm")) + _f(uses.get("other_sqm"))


def _dominant_use(*, trading: float, storage: float, kitchen: float) -> str:
    ranked = sorted(
        (("trading", trading), ("storage", storage), ("kitchen", kitchen)),
        key=lambda item: (-item[1], item[0]),
    )
    best = ranked[0]
    return best[0] if best[1] > 0 else "not_applicable"


def _derive_floor_config(levels: set[str]) -> str:
    has_ground = "ground" in levels
    has_lower = bool(levels & CANONICAL_LOWER_LEVELS)
    has_upper = bool(levels & CANONICAL_UPPER_LEVELS)
    if has_ground and has_lower and has_upper:
        return "ground_lower_ground_first"
    if has_ground and has_lower:
        return "ground_lower_ground"
    if has_ground and has_upper:
        return "ground_first"
    if has_ground:
        return "ground_only"
    if has_lower and has_upper:
        return "other"
    if has_lower:
        return "ground_lower_ground"
    if has_upper:
        return "ground_first"
    return "ground_only"


def normalize_paid_intake_layout(paid_intake: dict[str, Any]) -> dict[str, Any]:
    """Normalize paid_intake so canonical layout.floors remains source truth.

    When layout.floors[] is present we derive legacy layout/areas fields expected
    by existing downstream report and valuation logic.
    """
    normalized = dict(paid_intake or {})
    layout = dict(normalized.get("layout") or {})
    property_data = dict(normalized.get("property") or {})
    business_type = str(property_data.get("business_type") or "").strip().lower()
    is_retail_sector = business_type in {"retail", "hair_beauty"}
    floors = layout.get("floors")
    if not isinstance(floors, list) or len(floors) == 0:
        return normalized

    sales_area = storage_area = visible_kitchen = 0.0
    basement_sqm = upper_sqm = 0.0
    ancillary_area_sqm = non_ground_ancillary_sqm = 0.0
    ground_trading = ground_storage = ground_kitchen = 0.0
    lower_totals = {"trading": 0.0, "storage": 0.0, "kitchen": 0.0}
    upper_totals = {"trading": 0.0, "storage": 0.0, "kitchen": 0.0}
    levels_seen: set[str] = set()

    for idx, floor in enumerate(floors):
        if not isinstance(floor, dict):
            log.warning("layout_compat: skipping non-dict floor at index=%s", idx)
            continue
        level_raw = floor.get("level")
        level = (
            str(getattr(level_raw, "value", level_raw) or "")
            .strip()
            .lower()
        )
        uses = dict(floor.get("uses") or {})
        if level and level not in _ALLOWED_LEVELS:
            log.warning("layout_compat: unexpected level=%r at index=%s", level, idx)
        levels_seen.add(level)

        trading = _f(uses.get("trading_sqm"))
        storage = _f(uses.get("storage_sqm"))
        # Retail flows may omit kitchen field entirely; in that case kitchen
        # remains zero and "other" captures mixed ancillary (incl. kitchen/toilet).
        # We intentionally do not infer/split kitchen from "other".
        kitchen = _f(uses.get("kitchen_sqm"))
        other = _f(uses.get("other_sqm"))

        sales_area += trading
        storage_area += storage
        visible_kitchen += kitchen
        ancillary_area_sqm += other
        if is_retail_sector and kitchen == 0 and other > 0:
            log.info(
                "layout_compat: retail sector floor uses absent kitchen_sqm; preserving other_sqm as ancillary only",
            )

        floor_total = _floor_total(uses)
        if level == "ground":
            ground_trading += trading
            ground_storage += storage
            ground_kitchen += kitchen
        if level in CANONICAL_LOWER_LEVELS:
            basement_sqm += floor_total
            non_ground_ancillary_sqm += other
            lower_totals["trading"] += trading
            lower_totals["storage"] += storage
            lower_totals["kitchen"] += kitchen
        if level in CANONICAL_UPPER_LEVELS:
            upper_sqm += floor_total
            non_ground_ancillary_sqm += other
            upper_totals["trading"] += trading
            upper_totals["storage"] += storage
            upper_totals["kitchen"] += kitchen

    computed_total = sales_area + storage_area + visible_kitchen + sum(
        _f((f.get("uses") or {}).get("other_sqm")) for f in floors if isinstance(f, dict)
    )
    entered_total = _f(layout.get("total_entered_sqm"))
    if entered_total > 0:
        delta_pct = abs(computed_total - entered_total) / entered_total * 100
        if delta_pct > 20:
            log.warning(
                "layout_compat: large entered/computed sqm delta entered=%.2f computed=%.2f delta_pct=%.2f",
                entered_total,
                computed_total,
                delta_pct,
            )

    areas = dict(normalized.get("areas") or {})
    areas.update(
        {
            "sales_area_sqm": round(sales_area, 4),
            "storage_sqm": round(storage_area, 4),
            "visible_kitchen_sqm": round(visible_kitchen, 4),
            "ancillary_area_sqm": round(ancillary_area_sqm, 4),
            "non_ground_ancillary_area_sqm": round(non_ground_ancillary_sqm, 4),
            "non_ground_ancillary_sqm": round(non_ground_ancillary_sqm, 4),
            "basement_sqm": round(basement_sqm, 4),
            "upper_sqm": round(upper_sqm, 4),
        }
    )
    normalized["areas"] = areas

    layout["ground_floor_trading_sqm"] = round(ground_trading, 4)
    layout["ground_floor_storage_sqm"] = round(ground_storage, 4)
    layout["lower_ground_use"] = _dominant_use(**lower_totals)
    layout["upper_floor_use"] = _dominant_use(**upper_totals)
    layout["kitchen_on_ground"] = "yes" if ground_kitchen > 0 else "no_kitchen"
    layout["floor_config"] = layout.get("floor_config") or _derive_floor_config(levels_seen)
    normalized["layout"] = layout

    log.info(
        "layout_compat: canonical floors mapped floors=%d sales=%.2f storage=%.2f kitchen=%.2f basement=%.2f upper=%.2f",
        len(floors),
        sales_area,
        storage_area,
        visible_kitchen,
        basement_sqm,
        upper_sqm,
    )
    return normalized
