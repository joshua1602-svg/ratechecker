"""POST /report/simplified and /report/evidence — PDF generation endpoints."""
from __future__ import annotations

import logging
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import Response
from api.models import SimplifiedReportRequest
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


def _resolve_simplified_report_payload(payload: SimplifiedReportRequest | None) -> tuple[dict[str, Any], str, str]:
    """Return merged report data plus a mode label for logging/response metadata."""
    defaults = build_default_report_data()

    if payload is None:
        return defaults, "full_defaults", "No request body supplied; used placeholder defaults."

    provided = payload.model_dump(exclude_none=True, exclude_unset=True)
    if not provided:
        return defaults, "full_defaults", "Empty JSON payload supplied; used placeholder defaults."

    merged = _merge_defaults(defaults, provided)
    required_present = all(provided.get(field) not in (None, "", []) for field in _SIMPLIFIED_REQUIRED_FIELDS)
    mode_used = "provided" if required_present else "merged_defaults"
    debug_message = (
        "Generated report from provided payload."
        if mode_used == "provided"
        else "Generated report from partial payload merged with defaults."
    )
    return merged, mode_used, debug_message


def _persist_pdf(filename: str, pdf_bytes: bytes) -> Path:
    """Write the generated PDF to a writable runtime directory for debugging."""
    try:
        _REPORT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        file_path = _REPORT_OUTPUT_DIR / filename
        file_path.write_bytes(pdf_bytes)
    except OSError as exc:
        logger.exception("Failed to persist simplified report PDF to %s", _REPORT_OUTPUT_DIR)
        raise HTTPException(
            status_code=500,
            detail="Generated the simplified report PDF but could not persist it to runtime storage.",
        ) from exc
    return file_path


@router.post(
    "/report/simplified",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}, "description": "Generated simplified PDF report."}},
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
    return JSONResponse(content=response_payload.model_dump())


@router.post("/report/evidence")
async def evidence_report(request: Request) -> Response:
    report_data: dict = await request.json()
    try:
        pdf_bytes = generate_evidence_pack(report_data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    biz = _sanitise_filename(report_data.get("business_name", "Report"))
    filename = f"{biz}_RateChecker_Evidence.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
