"""VOA subject-record reconciliation diagnostics.

This module keeps valuation logic untouched and focuses only on:
  - total-area matching
  - layout/categorisation diagnostics
  - structured-record inconsistency flags for reporting
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

STATUS_YES = "yes"
STATUS_NO = "no"
STATUS_UNKNOWN = "unknown"

AREA_MATCH_STRONG = "strong"
AREA_MATCH_BROAD = "broad"
AREA_MATCH_PARTIAL = "partial"
AREA_MATCH_UNRESOLVED = "unresolved"

LAYOUT_MATCH_ALIGNED = "aligned"
LAYOUT_MATCH_DIFFERENT = "different"
LAYOUT_MATCH_UNKNOWN = "unknown"


@dataclass(frozen=True)
class FloorPresence:
    ground_present: bool | None
    basement_present: bool | None


def _to_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        if out < 0:
            return None
        return out
    except (TypeError, ValueError):
        return None


def _floor_presence_from_user(normalized_facts: dict[str, Any]) -> FloorPresence:
    ground = normalized_facts.get("ground_present")
    basement = normalized_facts.get("basement_present")
    return FloorPresence(
        ground_present=bool(ground) if ground is not None else None,
        basement_present=bool(basement) if basement is not None else None,
    )


def _floor_presence_from_voa(voa_record: dict[str, Any]) -> FloorPresence:
    floor_areas = voa_record.get("floor_areas") or {}
    ground = _to_float(floor_areas.get("ground"))
    basement = _to_float(floor_areas.get("basement"))
    return FloorPresence(
        ground_present=(ground > 0) if ground is not None else None,
        basement_present=(basement > 0) if basement is not None else None,
    )


def _voa_structured_total_area(voa_record: dict[str, Any]) -> tuple[float | None, str]:
    """Prefer summed positive-area SV lines when available, else total_area_sqm."""
    sv_lines = voa_record.get("sv_lines") or []
    areas: list[float] = []
    for line in sv_lines:
        area = _to_float((line or {}).get("area"))
        if area is not None and area > 0:
            areas.append(area)
    if areas:
        return round(sum(areas), 2), "sv_lines_sum"

    voa_total = _to_float(voa_record.get("total_area_sqm"))
    return (round(voa_total, 2), "record_total") if voa_total is not None else (None, "missing")


def _classify_area_match(user_total_sqm: Any, voa_total_sqm: Any) -> dict[str, Any]:
    user_val = _to_float(user_total_sqm)
    voa_val = _to_float(voa_total_sqm)
    if user_val is None or voa_val is None or voa_val <= 0:
        return {
            "status": STATUS_UNKNOWN,
            "match_status": AREA_MATCH_UNRESOLVED,
            "user_value_sqm": user_val,
            "voa_value_sqm": voa_val,
            "absolute_difference_sqm": None,
            "percentage_difference": None,
            "reason_code": "MISSING_AREA_DATA",
            "summary_text": "Total floor area could not be reconciled.",
            "detail_text": "No reliable VOA structured total area was available for comparison.",
        }

    abs_diff = abs(user_val - voa_val)
    pct_diff = (abs_diff / voa_val) * 100 if voa_val > 0 else None
    if abs_diff <= 2.0 or (pct_diff is not None and pct_diff <= 5.0):
        match_status = AREA_MATCH_STRONG
        status = STATUS_YES
    elif abs_diff <= 5.0 or (pct_diff is not None and pct_diff <= 10.0):
        match_status = AREA_MATCH_BROAD
        status = STATUS_YES
    else:
        match_status = AREA_MATCH_PARTIAL
        status = STATUS_NO

    return {
        "status": status,
        "match_status": match_status,
        "user_value_sqm": round(user_val, 2),
        "voa_value_sqm": round(voa_val, 2),
        "absolute_difference_sqm": round(abs_diff, 2),
        "percentage_difference": round(pct_diff, 2) if pct_diff is not None else None,
        "reason_code": "TOTAL_AREA_COMPARISON",
        "summary_text": "Entered total area compared with VOA structured total area.",
        "detail_text": f"Entered total area {user_val:.1f} sqm; VOA structured area {voa_val:.1f} sqm.",
    }


def _check_layout_categorisation(
    normalized_facts: dict[str, Any],
    voa_record: dict[str, Any],
) -> dict[str, Any]:
    user_presence = _floor_presence_from_user(normalized_facts)
    voa_presence = _floor_presence_from_voa(voa_record)
    detail_notes: list[str] = []

    if None not in (user_presence.ground_present, user_presence.basement_present, voa_presence.ground_present, voa_presence.basement_present):
        if user_presence.basement_present != voa_presence.basement_present:
            detail_notes.append(
                "Basement presence differs between entered layout and VOA structured record."
            )

    user_components = normalized_facts.get("entered_area_components") or {}
    user_kitchen = _to_float(user_components.get("kitchen_sqm")) or 0.0
    user_storage = _to_float(user_components.get("storage_sqm")) or 0.0
    has_user_kitchen = bool(user_components.get("kitchen_present")) or user_kitchen > 0

    sv_lines = voa_record.get("sv_lines") or []
    descs = [str((line or {}).get("description") or "").lower() for line in sv_lines]
    has_voa_kitchen = any("kitchen" in d for d in descs)
    has_voa_storage = any("storage" in d or "store" in d for d in descs)

    if has_user_kitchen and not has_voa_kitchen:
        detail_notes.append(
            "Entered layout includes kitchen space, but VOA structured lines do not separately identify kitchen."
        )
    if user_storage > 0 and sv_lines and not has_voa_storage:
        detail_notes.append(
            "Entered layout includes storage space, but VOA structured lines do not separately identify storage."
        )

    if not detail_notes and sv_lines:
        return {
            "status": STATUS_YES,
            "match_status": LAYOUT_MATCH_ALIGNED,
            "reason_code": "LAYOUT_CATEGORISATION_ALIGNS",
            "summary_text": "Internal layout/categorisation appears broadly aligned.",
            "detail_text": None,
        }
    if detail_notes:
        return {
            "status": STATUS_NO,
            "match_status": LAYOUT_MATCH_DIFFERENT,
            "reason_code": "LAYOUT_CATEGORISATION_DIFFERENCE",
            "summary_text": "Internal layout/categorisation differs from VOA structured treatment.",
            "detail_text": " ".join(detail_notes),
        }
    return {
        "status": STATUS_UNKNOWN,
        "match_status": LAYOUT_MATCH_UNKNOWN,
        "reason_code": "INSUFFICIENT_LAYOUT_CATEGORISATION_DATA",
        "summary_text": "Layout/categorisation could not be reconciled.",
        "detail_text": "Insufficient structured line detail to compare internal categorisation.",
    }


def _structured_inconsistency(
    user_payload: dict[str, Any],
    normalized_facts: dict[str, Any],
    voa_record: dict[str, Any],
    area_match: dict[str, Any],
    layout_match: dict[str, Any],
) -> tuple[bool, list[str]]:
    notes: list[str] = []
    bt = str(user_payload.get("business_type") or "").strip().lower()
    if bt not in {"retail", "restaurant_cafe", "hair_beauty"}:
        return False, notes

    if area_match.get("match_status") in {AREA_MATCH_STRONG, AREA_MATCH_BROAD} and layout_match.get("match_status") == LAYOUT_MATCH_DIFFERENT:
        notes.append(
            "Total area aligns, but component categorisation differs between entered layout and VOA structured lines."
        )

    user_components = normalized_facts.get("entered_area_components") or {}
    has_user_kitchen = bool(user_components.get("kitchen_present")) or (_to_float(user_components.get("kitchen_sqm")) or 0.0) > 0
    sv_lines = voa_record.get("sv_lines") or []
    has_voa_kitchen = any("kitchen" in str((line or {}).get("description") or "").lower() for line in sv_lines)
    if has_user_kitchen and sv_lines and not has_voa_kitchen:
        notes.append(
            "VOA structured record does not separately identify kitchen despite entered kitchen detail."
        )
    return bool(notes), notes


def _overall_status(area_match_status: str) -> str:
    return {
        AREA_MATCH_STRONG: "strong",
        AREA_MATCH_BROAD: "broad",
        AREA_MATCH_PARTIAL: "partial",
    }.get(area_match_status, "unresolved")


def reconcile_subject_against_voa(
    *,
    user_payload: dict[str, Any],
    voa_subject_record: dict[str, Any],
    normalized_facts: dict[str, Any],
) -> dict[str, Any]:
    """Build VOA reconciliation diagnostics.

    Primary driver is total area alignment (entered total vs VOA structured total).
    Layout/categorisation is secondary and never downgrades strong/broad area match
    into a false "partial" record match.
    """
    voa_total, total_source = _voa_structured_total_area(voa_subject_record)
    area_match = _classify_area_match(user_payload.get("total_area_sqm"), voa_total)
    layout_match = _check_layout_categorisation(normalized_facts, voa_subject_record)
    inconsistency_flag, inconsistency_notes = _structured_inconsistency(
        user_payload=user_payload,
        normalized_facts=normalized_facts,
        voa_record=voa_subject_record,
        area_match=area_match,
        layout_match=layout_match,
    )

    match_summary = {
        "strong": "Total area broadly aligns with the VOA structured record.",
        "broad": "Total area is broadly aligned with minor variance against VOA structured record.",
        "partial": "There is a material difference between entered total area and VOA structured area.",
        "unresolved": "No reliable VOA structured area was available for total-area comparison.",
    }[_overall_status(area_match.get("match_status"))]

    statuses = [area_match["status"], layout_match["status"]]
    return {
        "voa_reconciliation": {
            "overall_status": _overall_status(area_match.get("match_status")),
            "voa_record_match_status": _overall_status(area_match.get("match_status")),
            "voa_area_match_status": area_match.get("match_status"),
            "voa_layout_match_status": layout_match.get("match_status"),
            "voa_structured_total_area_source": total_source,
            "voa_structured_inconsistency_flag": inconsistency_flag,
            "voa_structured_inconsistency_notes": inconsistency_notes,
            "summary_text": match_summary,
            "summary": {
                "total_checks": 2,
                "yes": statuses.count(STATUS_YES),
                "no": statuses.count(STATUS_NO),
                "unknown": statuses.count(STATUS_UNKNOWN),
            },
            "checks": {
                "total_area_alignment": area_match,
                "layout_categorisation_alignment": layout_match,
            },
        }
    }
