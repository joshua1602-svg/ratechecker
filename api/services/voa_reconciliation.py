"""VOA subject-record reconciliation diagnostics.

This module provides a deterministic, rule-based comparison between
user-entered subject facts and the mapped VOA subject record.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

GROSS_AREA_MATCH_THRESHOLD_PCT = 10.0
FLOOR_SPLIT_MATCH_THRESHOLD_PCT = 10.0

STATUS_YES = "yes"
STATUS_NO = "no"
STATUS_UNKNOWN = "unknown"

# Release-1 explicit business-type mapping (retail only by requirement)
ACCEPTED_SCAT_BY_BUSINESS_TYPE: dict[str, set[int]] = {
    "retail": {249, 251},
}


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


def _pct_difference(user: float, voa: float) -> float | None:
    if voa <= 0:
        return None
    return abs((user - voa) / voa) * 100


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


def _check_gross_floor_space(user_total_sqm: Any, voa_total_sqm: Any) -> dict[str, Any]:
    user_val = _to_float(user_total_sqm)
    voa_val = _to_float(voa_total_sqm)
    if user_val is None or voa_val is None or voa_val <= 0:
        return {
            "status": STATUS_UNKNOWN,
            "user_value_sqm": user_val,
            "voa_value_sqm": voa_val,
            "absolute_difference_sqm": None,
            "percentage_difference": None,
            "reason_code": "MISSING_AREA_DATA",
            "summary_text": "Gross floor space could not be reconciled.",
            "detail_text": "Insufficient structured area data on user input or VOA record.",
        }

    abs_diff = abs(user_val - voa_val)
    pct_diff = _pct_difference(user_val, voa_val)
    status = STATUS_YES if pct_diff is not None and pct_diff <= GROSS_AREA_MATCH_THRESHOLD_PCT else STATUS_NO
    if status == STATUS_YES:
        reason = "AREA_DIFFERENCE_WITHIN_THRESHOLD"
        summary = "Gross floor space matches VOA records."
        detail = None
    else:
        reason = "AREA_DIFFERENCE_OVER_THRESHOLD"
        summary = "Gross floor space does not match VOA records."
        detail = f"{user_val:.1f} sqm entered; VOA record shows {voa_val:.1f} sqm."

    return {
        "status": status,
        "user_value_sqm": user_val,
        "voa_value_sqm": voa_val,
        "absolute_difference_sqm": round(abs_diff, 1),
        "percentage_difference": round(pct_diff, 1) if pct_diff is not None else None,
        "reason_code": reason,
        "summary_text": summary,
        "detail_text": detail,
    }


def _check_floor_plan_configuration(normalized_facts: dict[str, Any], voa_record: dict[str, Any]) -> dict[str, Any]:
    user_presence = _floor_presence_from_user(normalized_facts)
    voa_presence = _floor_presence_from_voa(voa_record)

    if None in (user_presence.ground_present, user_presence.basement_present, voa_presence.ground_present, voa_presence.basement_present):
        return {
            "status": STATUS_UNKNOWN,
            "user_ground_present": user_presence.ground_present,
            "user_basement_present": user_presence.basement_present,
            "voa_ground_present": voa_presence.ground_present,
            "voa_basement_present": voa_presence.basement_present,
            "reason_code": "MISSING_FLOOR_CONFIGURATION_DATA",
            "summary_text": "Floor plan configuration could not be reconciled.",
            "detail_text": "Insufficient floor-presence data on user input or VOA record.",
        }

    same = (
        user_presence.ground_present == voa_presence.ground_present
        and user_presence.basement_present == voa_presence.basement_present
    )
    if same:
        return {
            "status": STATUS_YES,
            "user_ground_present": user_presence.ground_present,
            "user_basement_present": user_presence.basement_present,
            "voa_ground_present": voa_presence.ground_present,
            "voa_basement_present": voa_presence.basement_present,
            "reason_code": "CONFIGURATION_ALIGNS",
            "summary_text": "Floor plan configuration matches VOA records.",
            "detail_text": None,
        }

    return {
        "status": STATUS_NO,
        "user_ground_present": user_presence.ground_present,
        "user_basement_present": user_presence.basement_present,
        "voa_ground_present": voa_presence.ground_present,
        "voa_basement_present": voa_presence.basement_present,
        "reason_code": "CONFIGURATION_MISMATCH",
        "summary_text": "Floor plan configuration does not match VOA records.",
        "detail_text": (
            f"Entered as {'Ground' if user_presence.ground_present else 'No Ground'}"
            f"{' + Lower Ground' if user_presence.basement_present else ''}; "
            f"VOA shows {'Ground' if voa_presence.ground_present else 'No Ground'}"
            f"{' + Lower Ground' if voa_presence.basement_present else ''}."
        ),
    }


def _check_floor_split(user_floor_areas: dict[str, Any], voa_floor_areas: dict[str, Any]) -> dict[str, Any]:
    floors = ("ground", "basement")
    per_floor: dict[str, dict[str, float | None]] = {}
    compared = 0.0
    material_mismatch = False
    comparable_floor_count = 0

    for floor in floors:
        user_val = _to_float(user_floor_areas.get(floor))
        voa_val = _to_float(voa_floor_areas.get(floor))
        if user_val is not None and voa_val is not None and voa_val > 0:
            comparable_floor_count += 1
            diff = user_val - voa_val
            pct = (diff / voa_val) * 100
            compared += voa_val
            if abs(pct) > FLOOR_SPLIT_MATCH_THRESHOLD_PCT:
                material_mismatch = True
            per_floor[floor] = {
                "user_sqm": round(user_val, 1),
                "voa_sqm": round(voa_val, 1),
                "difference_sqm": round(diff, 1),
                "percentage_difference": round(pct, 1),
            }
        else:
            per_floor[floor] = {
                "user_sqm": user_val,
                "voa_sqm": voa_val,
                "difference_sqm": None,
                "percentage_difference": None,
            }

    if comparable_floor_count == 0:
        return {
            "status": STATUS_UNKNOWN,
            "per_floor_comparison": per_floor,
            "total_compared_sqm": 0.0,
            "reason_code": "MISSING_FLOOR_SPLIT_DATA",
            "summary_text": "Floor split could not be reconciled.",
            "detail_text": "Insufficient per-floor area data for comparison.",
        }

    if material_mismatch:
        ground_row = per_floor.get("ground", {})
        return {
            "status": STATUS_NO,
            "per_floor_comparison": per_floor,
            "total_compared_sqm": round(compared, 1),
            "reason_code": "FLOOR_SPLIT_DIFFERENCE_OVER_THRESHOLD",
            "summary_text": "Floor split does not match VOA records.",
            "detail_text": (
                f"Ground floor entered as {ground_row.get('user_sqm')} sqm; "
                f"VOA shows {ground_row.get('voa_sqm')} sqm."
                if ground_row.get("percentage_difference") is not None
                else "Per-floor distribution differs materially from VOA records."
            ),
        }

    return {
        "status": STATUS_YES,
        "per_floor_comparison": per_floor,
        "total_compared_sqm": round(compared, 1),
        "reason_code": "FLOOR_SPLIT_WITHIN_THRESHOLD",
        "summary_text": "Floor split matches VOA records.",
        "detail_text": None,
    }


def _check_business_type(user_business_type: Any, voa_scat_code: Any, voa_description: Any) -> dict[str, Any]:
    user_type = str(user_business_type or "").strip().lower()
    scat = None
    try:
        scat = int(voa_scat_code) if voa_scat_code is not None else None
    except (TypeError, ValueError):
        scat = None

    if not user_type or scat is None:
        return {
            "status": STATUS_UNKNOWN,
            "user_business_type": user_type or None,
            "voa_scat_code": scat,
            "voa_description": voa_description,
            "reason_code": "MISSING_BUSINESS_TYPE" if not user_type else "MISSING_SCAT_CODE",
            "summary_text": "Business type could not be reconciled with VOA classification.",
            "detail_text": "Insufficient business type or SCAT data.",
        }

    accepted = ACCEPTED_SCAT_BY_BUSINESS_TYPE.get(user_type)
    if accepted is None:
        return {
            "status": STATUS_UNKNOWN,
            "user_business_type": user_type,
            "voa_scat_code": scat,
            "voa_description": voa_description,
            "reason_code": "BUSINESS_TYPE_NOT_CONFIGURED",
            "summary_text": "Business type reconciliation is not configured for this sector.",
            "detail_text": "This release supports explicit SCAT reconciliation for retail only.",
        }

    if scat in accepted:
        return {
            "status": STATUS_YES,
            "user_business_type": user_type,
            "voa_scat_code": scat,
            "voa_description": voa_description,
            "reason_code": "SCAT_CODE_ACCEPTED_FOR_RETAIL",
            "summary_text": "Business type matches VOA classification.",
            "detail_text": None,
        }

    return {
        "status": STATUS_NO,
        "user_business_type": user_type,
        "voa_scat_code": scat,
        "voa_description": voa_description,
        "reason_code": "SCAT_CODE_NOT_ACCEPTED_FOR_RETAIL",
        "summary_text": "Business type does not match VOA classification.",
        "detail_text": f"Entered as {user_type.title()}; VOA classification does not align.",
    }


def _overall_status(statuses: list[str]) -> str:
    """Conservative roll-up: only all-yes or all-unknown are definitive.

    Any mixed outcome (including any mismatch and uncertain combinations)
    resolves to "partially" to avoid overstating certainty.
    """
    if statuses and all(status == STATUS_YES for status in statuses):
        return STATUS_YES
    if statuses and all(status == STATUS_UNKNOWN for status in statuses):
        return STATUS_UNKNOWN
    return "partially"


def reconcile_subject_against_voa(
    *,
    user_payload: dict[str, Any],
    voa_subject_record: dict[str, Any],
    normalized_facts: dict[str, Any],
) -> dict[str, Any]:
    """Build the VOA reconciliation diagnostic object.

    Inputs are expected to be pre-mapped and pre-normalised by existing pipeline
    components. This function is read-only and never mutates valuation outputs.
    """
    gross = _check_gross_floor_space(
        user_payload.get("total_area_sqm"),
        voa_subject_record.get("total_area_sqm"),
    )
    config = _check_floor_plan_configuration(normalized_facts, voa_subject_record)
    split = _check_floor_split(
        normalized_facts.get("floor_areas") or {},
        voa_subject_record.get("floor_areas") or {},
    )
    business = _check_business_type(
        user_payload.get("business_type"),
        voa_subject_record.get("scat_code"),
        voa_subject_record.get("description"),
    )

    statuses = [gross["status"], config["status"], split["status"], business["status"]]

    return {
        "voa_reconciliation": {
            "overall_status": _overall_status(statuses),
            "summary": {
                "total_checks": 4,
                "yes": statuses.count(STATUS_YES),
                "no": statuses.count(STATUS_NO),
                "unknown": statuses.count(STATUS_UNKNOWN),
            },
            "checks": {
                "gross_floor_space": gross,
                "floor_plan_configuration": config,
                "floor_split": split,
                "business_type": business,
            },
        }
    }
