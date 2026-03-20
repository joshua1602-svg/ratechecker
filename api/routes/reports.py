"""POST /report/simplified and /report/evidence — PDF generation endpoints."""
from __future__ import annotations

import re

from fastapi import APIRouter, Request
from fastapi.responses import Response

from api.reports.pdf_generator import generate_evidence_pack, generate_simplified_report

router = APIRouter()


def _sanitise_filename(name: str) -> str:
    """Replace spaces with underscores and strip non-alphanumeric chars."""
    name = name.strip().replace(" ", "_")
    return re.sub(r"[^A-Za-z0-9_\-]", "", name)


@router.post("/report/simplified")
async def simplified_report(request: Request) -> Response:
    report_data: dict = await request.json()
    pdf_bytes = generate_simplified_report(report_data)

    biz = _sanitise_filename(report_data.get("business_name", "Report"))
    filename = f"{biz}_RateChecker_Simplified.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/report/evidence")
async def evidence_report(request: Request) -> Response:
    report_data: dict = await request.json()
    pdf_bytes = generate_evidence_pack(report_data)

    biz = _sanitise_filename(report_data.get("business_name", "Report"))
    filename = f"{biz}_RateChecker_Evidence.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
