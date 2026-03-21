"""POST /report/simplified and /report/evidence — PDF generation endpoints."""
from __future__ import annotations

import logging
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import Response
from api.models import EvidenceReportRequest, SimplifiedReportRequest
from api.reports.pdf_generator import generate_evidence_pack, generate_simplified_report

router = APIRouter()
logger = logging.getLogger(__name__)
_REPORT_OUTPUT_DIR = Path(gettempdir()) / "ratechecker_reports"
_SIMPLIFIED_REQUIRED_FIELDS = {
    "business_name",
    "property_address",
    "postcode",
    "business_type",
    "date_prepared",
    "voa_rv",
    "modelled_rv_low",
    "modelled_rv_high",
    "annual_saving_low",
    "annual_saving_high",
    "case_strength",
    "comparables",
    "comp_count",
}
_EVIDENCE_REQUIRED_FIELDS = _SIMPLIFIED_REQUIRED_FIELDS | {
    "uprn",
    "voa_description",
    "nia_sqm",
    "modelled_rv",
    "final_tone_psm",
    "tone_basis",
    "confidence",
    "recommendation_text",
}


def _sanitise_filename(name: str) -> str:
    """Replace spaces with underscores and strip non-alphanumeric chars."""
    name = name.strip().replace(" ", "_")
    return re.sub(r"[^A-Za-z0-9_\-]", "", name)


def build_default_report_data() -> dict[str, Any]:
    """Return a complete placeholder payload for simplified report generation."""
    today = datetime.now(timezone.utc).strftime("%d %B %Y")
    return {
        "business_name": "Placeholder Trading Ltd",
        "property_address": "123 Example High Street, Exampletown",
        "postcode": "AB1 2CD",
        "business_type": "retail",
        "date_prepared": today,
        "voa_rv": 24500,
        "modelled_rv_low": 18250,
        "modelled_rv_high": 19800,
        "annual_saving_low": 3100,
        "annual_saving_high": 3850,
        "case_strength": "Medium",
        "comp_count": 3,
        "layout_adjustment_applied": False,
        "comparables": [
            {
                "address": "101 Market Street, Exampletown",
                "nia_sqm": 118,
                "rv": 17600,
                "rate_psm": 149.15,
                "layout_similarity_score": 0.62,
                "floor_config": "ground_only",
                "uarn": "00010001",
            },
            {
                "address": "7 Station Parade, Exampletown",
                "nia_sqm": 121,
                "rv": 18950,
                "rate_psm": 156.61,
                "layout_similarity_score": 0.54,
                "floor_config": "ground_only",
                "uarn": "00010002",
            },
            {
                "address": "14 Riverside Walk, Exampletown",
                "nia_sqm": 126,
                "rv": 19400,
                "rate_psm": 153.97,
                "layout_similarity_score": 0.48,
                "floor_config": "ground_only",
                "uarn": "00010003",
            },
        ],
        "summary_text": (
            "Placeholder assessment generated because no complete report payload "
            "was supplied. Replace these values with live valuation data when "
            "frontend integration is ready."
        ),
        "estimated_rv": 19025,
        "current_rv": 24500,
        "property_type": "Retail unit",
        "comparable_count": 3,
        "estimated_saving": "£3,100–£3,850 per year",
        "overassessment_likelihood": "Possible overassessment",
        "address": "123 Example High Street, Exampletown, AB1 2CD",
    }


def build_default_evidence_report_data() -> dict[str, Any]:
    """Return a complete placeholder payload for evidence report generation."""
    data = build_default_report_data()
    modelled_rv = 19025
    nia_sqm = 122
    final_tone_psm = round(modelled_rv / nia_sqm, 2)
    data.update({
        "uprn": "100012345678",
        "voa_description": "Shop and premises",
        "nia_sqm": nia_sqm,
        "modelled_rv": modelled_rv,
        "final_tone_psm": final_tone_psm,
        "tone_basis": "Placeholder comparable tone derived from nearby retail properties.",
        "confidence": "Medium",
        "recommendation_text": "Proceed to Check and Challenge using the enclosed comparable evidence.",
        "zoning_rows": [
            {
                "zone": "A",
                "area_sqm": 61.0,
                "tone": 156.0,
                "relativity": 1.0,
                "area_type_weight": 1.0,
                "value": 9516.0,
            },
            {
                "zone": "B",
                "area_sqm": 61.0,
                "tone": 156.0,
                "relativity": 0.5,
                "area_type_weight": 0.5,
                "value": 4758.0,
            },
        ],
        "nursery_adjustments": [],
        "allowances_summary": "No additional allowances applied in this placeholder evidence pack.",
        "subtotal_pre": 14274.0,
        "floor_config": "ground_only",
        "ground_floor_trading_sqm": 90.0,
        "ground_floor_storage_sqm": 32.0,
        "kitchen_area_sqm": 0.0,
        "kitchen_on_ground": "no_kitchen",
    })
    return data


def _merge_defaults(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Recursively overlay non-empty user values onto defaults."""
    merged = deepcopy(defaults)
    for key, value in overrides.items():
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_defaults(merged[key], value)
            continue
        if isinstance(value, list):
            merged[key] = value or merged.get(key, [])
            continue
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                merged[key] = stripped
            continue
        merged[key] = value
    return merged


def _resolve_report_payload(
    payload: SimplifiedReportRequest | EvidenceReportRequest | None,
    defaults: dict[str, Any],
    required_fields: set[str],
) -> tuple[dict[str, Any], str, str]:
    """Return merged report data plus a mode label for logging/response metadata."""
    if payload is None:
        return defaults, "full_defaults", "No request body supplied; used placeholder defaults."

    provided = payload.model_dump(exclude_none=True, exclude_unset=True)
    if not provided:
        return defaults, "full_defaults", "Empty JSON payload supplied; used placeholder defaults."

    merged = _merge_defaults(defaults, provided)
    required_present = all(provided.get(field) not in (None, "", []) for field in required_fields)
    mode_used = "provided" if required_present else "merged_defaults"
    debug_message = (
        "Generated report from provided payload."
        if mode_used == "provided"
        else "Generated report from partial payload merged with defaults."
    )
    return merged, mode_used, debug_message


def _resolve_simplified_report_payload(payload: SimplifiedReportRequest | None) -> tuple[dict[str, Any], str, str]:
    return _resolve_report_payload(payload, build_default_report_data(), _SIMPLIFIED_REQUIRED_FIELDS)


def _resolve_evidence_report_payload(payload: EvidenceReportRequest | None) -> tuple[dict[str, Any], str, str]:
    return _resolve_report_payload(payload, build_default_evidence_report_data(), _EVIDENCE_REQUIRED_FIELDS)


def _persist_pdf(filename: str, pdf_bytes: bytes) -> Path:
    """Write the generated PDF to a writable runtime directory for debugging."""
    try:
        _REPORT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        file_path = _REPORT_OUTPUT_DIR / filename
        file_path.write_bytes(pdf_bytes)
    except OSError as exc:
        logger.exception("Failed to persist report PDF to %s", _REPORT_OUTPUT_DIR)
        raise HTTPException(
            status_code=500,
            detail="Generated the report PDF but could not persist it to runtime storage.",
        ) from exc
    return file_path



def _build_pdf_response(filename: str, pdf_bytes: bytes, disposition: str = "attachment") -> Response:
    """Persist a generated PDF and return it as an HTTP response."""
    _persist_pdf(filename, pdf_bytes)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )


@router.post(
    "/report/simplified",
    response_class=Response,
    response_model=None,
    response_description="Generated simplified PDF report.",
    responses={
        200: {
            "description": "Generated simplified PDF report.",
            "content": {
                "application/pdf": {
                    "schema": {"type": "string", "format": "binary"},
                }
            },
        }
    },
)
async def simplified_report(
    payload: SimplifiedReportRequest | None = Body(default=None),
) -> Response:
    report_data, mode_used, debug_message = _resolve_simplified_report_payload(payload)
    logger.info(
        "Generating simplified report. mode_used=%s business_name=%s comparables=%s",
        mode_used,
        report_data.get("business_name"),
        len(report_data.get("comparables", [])),
    )

    try:
        pdf_bytes = generate_simplified_report(report_data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    biz = _sanitise_filename(report_data.get("business_name", "Report")) or "Report"
    filename = f"{biz}_RateChecker_Simplified.pdf"
    _persist_pdf(filename, pdf_bytes)

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="simplified_report.pdf"'},
    )


@router.post(
    "/report/evidence",
    response_class=Response,
    response_model=None,
    response_description="Generated evidence pack PDF.",
    responses={
        200: {
            "description": "Generated evidence pack PDF.",
            "content": {
                "application/pdf": {
                    "schema": {"type": "string", "format": "binary"},
                }
            },
        }
    },
)
async def evidence_report(
    payload: EvidenceReportRequest | None = Body(default=None),
) -> Response:
    report_data, mode_used, debug_message = _resolve_evidence_report_payload(payload)
    logger.info(
        "Generating evidence report. mode_used=%s business_name=%s comparables=%s note=%s",
        mode_used,
        report_data.get("business_name"),
        len(report_data.get("comparables", [])),
        debug_message,
    )

    try:
        pdf_bytes = generate_evidence_pack(report_data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    biz = _sanitise_filename(report_data.get("business_name", "Report")) or "Report"
    filename = f"{biz}_RateChecker_Evidence.pdf"
    return _build_pdf_response(filename, pdf_bytes)
