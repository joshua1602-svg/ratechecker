"""POST /purchase — create a Stripe Checkout session."""
from __future__ import annotations

import os

import stripe
from fastapi import APIRouter, HTTPException

from api.models import PurchaseRequest, PurchaseResponse

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

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="payment",
            customer_email=req.form_data.contact.email or None,
            success_url=f"{BASE_URL}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{BASE_URL}/cancel",
            metadata={
                "product": product,
                "business_name": req.form_data.contact.business_name,
                "postcode": req.form_data.property.postcode,
                "business_type": req.form_data.property.business_type.value,
                "nia_sqm": str(req.form_data.property.nia_sqm),
                "voa_rv": str(req.form_data.property.voa_rv),
            },
        )
    except stripe.StripeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return PurchaseResponse(checkout_url=session.url)
