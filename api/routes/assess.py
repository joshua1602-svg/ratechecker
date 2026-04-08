"""POST /assess — free quick-check endpoint."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from api.captcha import verify_turnstile
from api.models import AssessRequest, AssessResponse
from api.services.assessment import run_live_assessment

router = APIRouter()


@router.post("/assess", response_model=AssessResponse)
async def assess(req: AssessRequest) -> AssessResponse:
    if not await verify_turnstile(req.captcha_token):
        raise HTTPException(status_code=400, detail="Captcha verification failed")
    return await run_live_assessment(req)
