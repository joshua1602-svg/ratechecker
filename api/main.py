"""RateChecker API — FastAPI application entry point."""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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


@app.get("/health", tags=["ops"])
def health() -> dict:
    return {"status": "ok"}
