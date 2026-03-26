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

from api.pending_reports import RATECHECKER_SESSION_METADATA_KEY, mark_paid

log = logging.getLogger(__name__)

router = APIRouter()

_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")


@router.post("/stripe/webhook")
async def stripe_webhook(request: Request) -> JSONResponse:
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    log.info(
        "Webhook received: path=%s payload_bytes=%s signature_present=%s",
        request.url.path,
        len(payload),
        bool(sig),
    )

    if not _WEBHOOK_SECRET:
        log.error("STRIPE_WEBHOOK_SECRET is not configured — rejecting webhook")
        raise HTTPException(status_code=500, detail="Webhook secret not configured")

    try:
        event = stripe.Webhook.construct_event(payload, sig, _WEBHOOK_SECRET)
    except ValueError:
        log.warning("Webhook: invalid payload. payload_bytes=%s", len(payload))
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.SignatureVerificationError:
        log.warning(
            "Webhook: signature verification failed. signature_present=%s payload_bytes=%s",
            bool(sig),
            len(payload),
        )
        raise HTTPException(status_code=400, detail="Invalid signature")

    event_type = event["type"]
    event_id = event["id"]
    log.info("Webhook verified: event_id=%s event_type=%s", event_id, event_type)

    if event_type == "checkout.session.completed":
        session_obj = event["data"]["object"]
        raw_metadata = session_obj.get("metadata") or {}
        metadata_dict = raw_metadata if isinstance(raw_metadata, dict) else dict(raw_metadata.items())
        session_id = metadata_dict.get(RATECHECKER_SESSION_METADATA_KEY)
        stripe_session_id = session_obj["id"]

        log.info(
            "Webhook checkout.session.completed: stripe_session_id=%s metadata_keys=%s extracted_session_id=%s",
            stripe_session_id,
            sorted(metadata.keys()) if isinstance(metadata, dict) else [],
            session_id,
        )

        if not session_id:
            log.warning(
                "Webhook checkout.session.completed with no %s: stripe_id=%s",
                RATECHECKER_SESSION_METADATA_KEY,
                stripe_session_id,
            )
            return JSONResponse(
                {
                    "status": "ignored",
                    "reason": f"no {RATECHECKER_SESSION_METADATA_KEY}",
                }
            )

        updated = mark_paid(session_id, stripe_session_id)

        if updated:
            log.info(
                "Webhook: draft marked paid. session_id=%s stripe=%s",
                session_id,
                stripe_session_id,
            )
        else:
            log.warning(
                "Webhook: no matching draft for session_id=%s stripe=%s",
                session_id,
                stripe_session_id,
            )

        return JSONResponse({"status": "ok", "session_id": session_id, "paid": updated})

    log.debug("Webhook: ignoring event type %s", event_type)
    return JSONResponse({"status": "ignored", "event_type": event_type})
