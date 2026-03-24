"""POST /purchase — persist report draft, then create a Stripe Checkout session.

The full assess request, assess response, and second-screen paid intake data
are durably stored in pending_reports *before* the Stripe redirect.  Only the
draft session_id is passed in Stripe metadata, keeping the metadata payload
small and making the final report recoverable from backend truth alone.
"""
from __future__ import annotations

import logging
import os

import stripe
from fastapi import APIRouter, HTTPException

from api.models import PurchaseRequest, PurchaseResponse
from api.pending_reports import create_draft

log = logging.getLogger(__name__)

router = APIRouter()

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

_PRICES: dict[str, str] = {
    "report": os.environ.get("STRIPE_PRICE_REPORT", ""),
    "evidence": os.environ.get("STRIPE_PRICE_EVIDENCE", ""),
}

BASE_URL = os.environ.get("BASE_URL", "http://localhost:5173")


@router.post("/purchase", response_model=PurchaseResponse)
async def purchase(req: PurchaseRequest) -> PurchaseResponse:
    product = req.product
    if product not in _PRICES:
        raise HTTPException(status_code=400, detail=f"Unknown product: {product!r}")

    price_id = _PRICES[product]
    if not price_id:
        raise HTTPException(status_code=500, detail=f"Stripe price ID for '{product}' is not configured")

    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe is not configured")

    # ── 1. Persist the full report draft before redirecting to Stripe ──
    session_id = create_draft(
        product=product,
        assess_request=req.assess_request,
        assess_response=req.assess_response,
        paid_intake=req.paid_intake.model_dump(exclude_none=True),
    )

    # ── 2. Extract customer email for Stripe (best-effort) ──
    customer_email = (
        req.assess_request.get("contact", {}).get("email")
        or None
    )

    # ── 3. Create Stripe checkout session with only session_id in metadata ──
    try:
        checkout = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="payment",
            customer_email=customer_email,
            success_url=f"{BASE_URL}/success?session_id={session_id}",
            cancel_url=f"{BASE_URL}/cancel?session_id={session_id}",
            metadata={
                "ratechecker_session_id": session_id,
            },
        )
    except stripe.StripeError as exc:
        log.error("Stripe checkout creation failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Payment service is temporarily unavailable. Please try again.",
        ) from exc

    return PurchaseResponse(checkout_url=checkout.url, session_id=session_id)
