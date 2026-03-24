"""
Durable persistence for paid report drafts.

Stores the full assess request, assess response, and second-screen paid intake
data before the Stripe checkout redirect.  This ensures the final report can
be rebuilt entirely from backend truth, even if the browser tab is closed or
frontend state is lost.

Schema: pending_reports table (auto-created on startup).
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from api.db import engine

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Table DDL
# ---------------------------------------------------------------------------

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS pending_reports (
    session_id       TEXT PRIMARY KEY,
    product          TEXT NOT NULL,
    assess_request   TEXT NOT NULL,
    assess_response  TEXT NOT NULL,
    paid_intake      TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL,
    stripe_session_id TEXT,
    paid             INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_pr_stripe_sid
    ON pending_reports (stripe_session_id)
"""


def ensure_table() -> None:
    """Create the pending_reports table if it does not exist.

    Safe to call on every startup — uses IF NOT EXISTS.
    """
    with Session(engine) as session:
        session.execute(text(_CREATE_TABLE))
        session.execute(text(_CREATE_INDEX))
        session.commit()
    log.info("pending_reports table ensured")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_draft(
    product: str,
    assess_request: dict,
    assess_response: dict,
    paid_intake: dict,
) -> str:
    """Persist a report draft and return the generated session_id (UUID)."""
    session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with Session(engine) as db:
        db.execute(
            text(
                "INSERT INTO pending_reports "
                "(session_id, product, assess_request, assess_response, paid_intake, created_at) "
                "VALUES (:sid, :product, :req, :resp, :intake, :created)"
            ),
            {
                "sid": session_id,
                "product": product,
                "req": json.dumps(assess_request),
                "resp": json.dumps(assess_response),
                "intake": json.dumps(paid_intake),
                "created": now,
            },
        )
        db.commit()
    log.info("Draft created: session_id=%s product=%s", session_id, product)
    return session_id


def mark_paid(session_id: str, stripe_session_id: str) -> bool:
    """Set paid=true for the given draft.  Returns True if a row was updated."""
    with Session(engine) as db:
        result = db.execute(
            text(
                "UPDATE pending_reports "
                "SET paid = 1, stripe_session_id = :ssid "
                "WHERE session_id = :sid"
            ),
            {"sid": session_id, "ssid": stripe_session_id},
        )
        db.commit()
        updated = result.rowcount > 0
    if updated:
        log.info("Draft marked paid: session_id=%s stripe=%s", session_id, stripe_session_id)
    else:
        log.warning("mark_paid: no row for session_id=%s", session_id)
    return updated


def get_draft(session_id: str) -> dict | None:
    """Load a draft by session_id.  Returns None if not found."""
    with Session(engine) as db:
        row = db.execute(
            text(
                "SELECT session_id, product, assess_request, assess_response, "
                "paid_intake, created_at, stripe_session_id, paid "
                "FROM pending_reports WHERE session_id = :sid"
            ),
            {"sid": session_id},
        ).mappings().first()

    if not row:
        return None
    return {
        "session_id": row["session_id"],
        "product": row["product"],
        "assess_request": json.loads(row["assess_request"]),
        "assess_response": json.loads(row["assess_response"]),
        "paid_intake": json.loads(row["paid_intake"]),
        "created_at": row["created_at"],
        "stripe_session_id": row["stripe_session_id"],
        "paid": bool(row["paid"]),
    }
