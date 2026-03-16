"""RateChecker API — FastAPI application entry point."""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.db import count_voa_rows, ensure_runtime_indexes
from api.routes.assess import router as assess_router
from api.routes.purchase import router as purchase_router

app = FastAPI(
    title="RateChecker API",
    version="1.0.0",
    description="Business rates overassessment checking engine.",
)

# CORS — tighten allowed_origins in production via the CORS_ORIGINS env var
_origins_env = os.environ.get("CORS_ORIGINS", "*")
_origins = [o.strip() for o in _origins_env.split(",")] if _origins_env != "*" else ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(assess_router)
app.include_router(purchase_router)


@app.on_event("startup")
def startup() -> None:
    """Create any missing runtime indexes on the VOA tables."""
    try:
        ensure_runtime_indexes()
    except Exception:
        # Tables may not exist yet in a fresh dev environment — not fatal
        pass


@app.get("/health", tags=["ops"])
def health() -> dict:
    """
    Returns database row count so callers can confirm the VOA dataset is loaded.
    Returns voa_row_count: null if the table is not yet populated.
    """
    try:
        n = count_voa_rows()
    except Exception:
        n = None
    return {"status": "ok", "voa_row_count": n}
