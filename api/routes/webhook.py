"""POST /stripe/webhook — Stripe event handler.

Handles checkout.session.completed events to mark pending report drafts
as paid.  All other event types are acknowledged but ignored.

Requires STRIPE_WEBHOOK_SECRET to be set for signature verification.
"""
from __future__ import annotations

import logging
import os

import stripe
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from api.pending_reports import mark_paid

log = logging.getLogger(__name__)

router = APIRouter()

_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")


@router.post("/stripe/webhook")
async def stripe_webhook(request: Request) -> JSONResponse:
    # ── 1. Read raw body (required for Stripe signature verification) ──
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    if not _WEBHOOK_SECRET:
        log.error("STRIPE_WEBHOOK_SECRET is not configured — rejecting webhook")
        raise HTTPException(status_code=500, detail="Webhook secret not configured")

    # ── 2. Verify signature ──
    try:
        event = stripe.Webhook.construct_event(payload, sig, _WEBHOOK_SECRET)
    except ValueError:
        log.warning("Webhook: invalid payload")
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.SignatureVerificationError:
        log.warning("Webhook: signature verification failed")
        raise HTTPException(status_code=400, detail="Invalid signature")

    # ── 3. Handle checkout.session.completed ──
    if event["type"] == "checkout.session.completed":
        session_obj = event["data"]["object"]
        metadata = session_obj.get("metadata", {})
        session_id = metadata.get("ratechecker_session_id")

        if not session_id:
            log.warning(
                "Webhook checkout.session.completed with no ratechecker_session_id: stripe_id=%s",
                session_obj.get("id"),
            )
            return JSONResponse({"status": "ignored", "reason": "no ratechecker_session_id"})

        stripe_session_id = session_obj.get("id", "")
        updated = mark_paid(session_id, stripe_session_id)

        if updated:
            log.info(
                "Webhook: draft marked paid. session_id=%s stripe=%s",
                session_id, stripe_session_id,
            )
        else:
            log.warning(
                "Webhook: no matching draft for session_id=%s stripe=%s",
                session_id, stripe_session_id,
            )

        return JSONResponse({"status": "ok", "session_id": session_id, "paid": updated})

    # ── 4. Acknowledge other event types without action ──
    log.debug("Webhook: ignoring event type %s", event["type"])
    return JSONResponse({"status": "ignored", "event_type": event["type"]})
