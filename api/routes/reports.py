"""POST /report/simplified and /report/evidence — PDF generation endpoints.

Production safety:
  - No placeholder or synthetic data.  All fields must be provided by the caller.
  - Payloads are validated strictly — missing required fields return 422.
  - Basic rate limiting via an in-memory sliding window to prevent abuse.
"""
from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from api.models import EvidenceReportRequest, SimplifiedReportRequest
from api.reports.pdf_generator import generate_evidence_pack, generate_simplified_report

router = APIRouter()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Simple in-memory rate limiter for report generation
# ---------------------------------------------------------------------------
_RATE_LIMIT_WINDOW_S = 60    # 1-minute sliding window
_RATE_LIMIT_MAX_REQUESTS = 5  # max 5 report generations per IP per window

_request_log: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(client_ip: str) -> None:
    """Raise HTTPException(429) if the client has exceeded the rate limit."""
    now = time.monotonic()
    window_start = now - _RATE_LIMIT_WINDOW_S

    # Prune old entries
    timestamps = _request_log[client_ip]
    _request_log[client_ip] = [t for t in timestamps if t > window_start]

    if len(_request_log[client_ip]) >= _RATE_LIMIT_MAX_REQUESTS:
        raise HTTPException(
            status_code=429,
            detail="Too many report requests. Please wait before trying again.",
        )

    _request_log[client_ip].append(now)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

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
    "voa_description",
    "nia_sqm",
    "modelled_rv",
    "final_tone_psm",
    "tone_basis",
    "confidence",
    "recommendation_text",
}

# Known placeholder/synthetic values that must be rejected
_PLACEHOLDER_INDICATORS = {
    "Placeholder Trading Ltd",
    "123 Example High Street, Exampletown",
    "AB1 2CD",
}


def _validate_report_payload(
    data: dict[str, Any],
    required_fields: set[str],
) -> None:
    """Validate that all required fields are present and not placeholder data.

    Raises HTTPException(422) with a specific error message on failure.
    """
    missing = []
    for field in required_fields:
        val = data.get(field)
        if val is None:
            missing.append(field)
        elif isinstance(val, str) and not val.strip():
            missing.append(field)
        elif isinstance(val, list) and field == "comparables" and len(val) == 0:
            missing.append(field)
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Missing required report fields: {', '.join(sorted(missing))}",
        )

    # Reject known placeholder values
    for indicator in _PLACEHOLDER_INDICATORS:
        for field in ("business_name", "property_address", "postcode"):
            if data.get(field) == indicator:
                raise HTTPException(
                    status_code=422,
                    detail=f"Report contains placeholder data in '{field}'. Use real values from /assess.",
                )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitise_filename(name: str) -> str:
    """Replace spaces with underscores and strip non-alphanumeric chars."""
    name = name.strip().replace(" ", "_")
    return re.sub(r"[^A-Za-z0-9_\-]", "", name)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

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
    request: Request,
    payload: SimplifiedReportRequest,
) -> Response:
    _check_rate_limit(request.client.host if request.client else "unknown")

    report_data = payload.model_dump()
    _validate_report_payload(report_data, _SIMPLIFIED_REQUIRED_FIELDS)

    logger.info(
        "Generating simplified report. business_name=%s comp_count=%s",
        report_data.get("business_name"),
        report_data.get("comp_count"),
    )

    try:
        pdf_bytes = generate_simplified_report(report_data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    biz = _sanitise_filename(report_data.get("business_name", "Report")) or "Report"
    filename = f"{biz}_RateChecker_Simplified.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
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
    request: Request,
    payload: EvidenceReportRequest,
) -> Response:
    _check_rate_limit(request.client.host if request.client else "unknown")

    report_data = payload.model_dump()
    _validate_report_payload(report_data, _EVIDENCE_REQUIRED_FIELDS)

    logger.info(
        "Generating evidence report. business_name=%s comp_count=%s",
        report_data.get("business_name"),
        report_data.get("comp_count"),
    )

    try:
        pdf_bytes = generate_evidence_pack(report_data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    biz = _sanitise_filename(report_data.get("business_name", "Report")) or "Report"
    filename = f"{biz}_RateChecker_Evidence.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
