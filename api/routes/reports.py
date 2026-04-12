"""POST /report/simplified and /report/evidence — PDF generation endpoints.

Also provides GET /report/download/{session_id} for the paid flow:
  load a persisted draft, verify payment, build the report from backend truth.

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
from api.layout_compat import normalize_paid_intake_layout
from api.models import (
    AssessRequest,
    AssessResponse,
    EvidenceReportRequest,
    SimplifiedReportRequest,
    build_evidence_payload_from_assess,
    build_report_payload_from_assess,
)
from api.pending_reports import get_draft, get_draft_payment_status
from api.reports.pdf_generator import generate_evidence_pack, generate_simplified_report
from api.routes.assess import run_assessment_pipeline

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


# ---------------------------------------------------------------------------
# Paid-flow report download
# ---------------------------------------------------------------------------

def _merge_paid_intake(assess_request_dict: dict, paid_intake: dict) -> dict:
    """Merge second-screen paid intake data into the assess request.

    paid_intake keys mirror the AssessRequest structure:
      - "contact": dict of ContactInput overrides (business_name, email)
      - "property": dict of PropertyInput overrides (address, uprn, frontage_m, ...)
      - "layout": dict of LayoutInputModel fields
      - "areas": dict of AreasInput fields
      - "nursery": dict of NurseryInput fields
      - "flags": dict of FlagsInput updates

    The frontend may also send flat convenience keys at the top level
    (business_name, address, postcode, nia_sqm, voa_rv).  These are
    re-routed into the correct nested position before the main merge.

    For dict-valued keys, the merge is shallow (paid_intake values override
    assess_request values at the sub-key level).  For non-dict keys, the
    paid_intake value replaces the assess_request value outright.
    """
    # Re-route flat convenience keys into the correct nested positions.
    intake = normalize_paid_intake_layout(dict(paid_intake))

    # business_name → contact.business_name
    flat_biz = intake.pop("business_name", None)
    if flat_biz:
        contact_overrides = intake.get("contact") or {}
        contact_overrides.setdefault("business_name", flat_biz)
        intake["contact"] = contact_overrides

    # address, postcode, nia_sqm, voa_rv → property.*
    _FLAT_TO_PROPERTY = ("address", "postcode", "nia_sqm", "voa_rv")
    property_overrides = dict(intake.get("property") or {})
    any_property_flat = False
    for flat_key in _FLAT_TO_PROPERTY:
        flat_val = intake.pop(flat_key, None)
        if flat_val is not None:
            property_overrides.setdefault(flat_key, flat_val)
            any_property_flat = True
    if any_property_flat:
        intake["property"] = property_overrides

    # Main merge: nested dicts are shallow-merged; scalars replaced outright.
    merged = dict(assess_request_dict)
    for key, value in intake.items():
        if value is None:
            continue
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = {**existing, **value}
        else:
            merged[key] = value
    return merged


@router.get(
    "/report/download/{session_id}",
    response_class=Response,
    response_model=None,
    response_description="Generated paid report PDF.",
    responses={
        200: {
            "description": "Generated paid report PDF.",
            "content": {
                "application/pdf": {
                    "schema": {"type": "string", "format": "binary"},
                }
            },
        }
    },
)
async def download_report(session_id: str, request: Request) -> Response:
    """Generate a paid report from a persisted draft.

    Flow: frontend redirects here after Stripe success.  The backend loads the
    draft, verifies payment, merges paid_intake into the assess request, builds
    the report payload from backend truth, and returns the PDF.
    """
    # ── 1. Load the persisted draft ──
    draft = get_draft(session_id)
    if draft is None:
        logger.warning("Paid download requested for missing session_id=%s", session_id)
        raise HTTPException(status_code=404, detail="Report session not found")

    if not draft["paid"]:
        logger.info(
            "Paid download requested before payment confirmation. session_id=%s stripe_session_id=%s",
            session_id,
            draft.get("stripe_session_id"),
        )
        raise HTTPException(
            status_code=402,
            detail="Payment not yet confirmed",
            headers={"Retry-After": "3"},
        )

    # Rate limit only expensive PDF-generation requests after payment is confirmed.
    _check_rate_limit(request.client.host if request.client else "unknown")

    # ── 2. Merge paid_intake into the assess request ──
    merged_request_dict = _merge_paid_intake(
        draft["assess_request"],
        draft["paid_intake"],
    )

    # ── 3. Parse back into typed models ──
    try:
        assess_req = AssessRequest.model_validate(merged_request_dict)
    except Exception as exc:
        logger.error("Failed to parse stored assess_request: %s", exc)
        raise HTTPException(
            status_code=500,
            detail="Stored report data is invalid — cannot generate report.",
        ) from exc

    try:
        assess_resp = AssessResponse.model_validate(draft["assess_response"])
    except Exception as exc:
        logger.error("Failed to parse stored assess_response: %s", exc)
        raise HTTPException(
            status_code=500,
            detail="Stored engine data is invalid — cannot generate report.",
        ) from exc

    # Defensive paid-flow rebuild: if stored rated_comps are empty/missing
    # (e.g. frontend persisted an empty list), recompute from backend inputs so
    # evidence tables remain populated from backend truth.
    if not (assess_resp.rated_comps or []):
        logger.info(
            "Stored paid assess_response has no rated_comps; recomputing assessment for session_id=%s",
            session_id,
        )
        assess_resp = await run_assessment_pipeline(assess_req)

    # ── 4. Build the report payload from backend truth ──
    product = draft["product"]
    if product == "evidence":
        report_data = build_evidence_payload_from_assess(assess_resp, assess_req)
    else:
        report_data = build_report_payload_from_assess(assess_resp, assess_req)

    # Inject paid_intake fields that don't map to AssessRequest sub-models.
    paid_intake = draft.get("paid_intake") or {}
    if paid_intake.get("has_parking") is not None:
        report_data["has_parking"] = paid_intake["has_parking"]

    # ── 5. Generate the PDF ──
    _comps = report_data.get("comparables") or []
    logger.info(
        "Generating paid %s report. session_id=%s business_name=%r "
        "property_address=%r postcode=%r comp_count=%s len(comparables)=%s "
        "first_comp=%r",
        product,
        session_id,
        report_data.get("business_name"),
        report_data.get("property_address"),
        report_data.get("postcode"),
        report_data.get("comp_count"),
        len(_comps),
        _comps[0] if _comps else None,
    )

    try:
        if product == "evidence":
            pdf_bytes = generate_evidence_pack(report_data)
        else:
            pdf_bytes = generate_simplified_report(report_data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    biz = _sanitise_filename(report_data.get("business_name", "Report")) or "Report"
    suffix = "Evidence" if product == "evidence" else "Simplified"
    filename = f"{biz}_RateChecker_{suffix}.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/report/status/{session_id}")
async def report_status(session_id: str) -> dict[str, Any]:
    """Return paid status for a pending report draft for debugging/polling."""
    draft = get_draft_payment_status(session_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Report session not found")

    return {
        "session_id": draft["session_id"],
        "product": draft["product"],
        "paid": draft["paid"],
        "stripe_session_id": draft["stripe_session_id"],
        "created_at": draft["created_at"],
    }
