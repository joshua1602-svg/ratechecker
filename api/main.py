"""RateChecker API — FastAPI application entry point."""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.db import count_voa_rows, ensure_runtime_indexes

log = logging.getLogger(__name__)

app = FastAPI(
    title="RateChecker API",
    version="1.0.0",
    description="Business rates overassessment checking engine.",
)

# CORS — CORS_ORIGINS must be set in production.  Wildcard is allowed only
# when RATECHECKER_ENV is explicitly "development".
_origins_env = os.environ.get("CORS_ORIGINS", "")
_env_mode = os.environ.get("RATECHECKER_ENV", "production")

if _origins_env:
    _origins: list[str] = [o.strip() for o in _origins_env.split(",") if o.strip()]
elif _env_mode == "development":
    _origins = ["*"]
    log.warning("CORS_ORIGINS not set — using wildcard (*) because RATECHECKER_ENV=development")
else:
    # Production default: no origins allowed.  Requests from browsers will be
    # blocked by CORS until CORS_ORIGINS is configured.
    _origins = []
    log.warning(
        "CORS_ORIGINS not set and RATECHECKER_ENV=%s — no origins allowed. "
        "Set CORS_ORIGINS to a comma-separated list of allowed origins.",
        _env_mode,
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)

# Lazy imports so CORS middleware is registered before routes
from api.routes.assess import router as assess_router  # noqa: E402
from api.routes.purchase import router as purchase_router  # noqa: E402
from api.routes.reports import router as reports_router  # noqa: E402

app.include_router(assess_router)
app.include_router(purchase_router)
app.include_router(reports_router)


@app.get("/", tags=["ops"])
def root() -> dict:
    """Simple root endpoint for platform health checks and operator sanity checks."""
    return {
        "service": "RateChecker API",
        "status": "ok",
        "docs_url": "/docs",
        "health_url": "/health",
    }


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
    Returns status "degraded" with voa_row_count null if the database is unreachable.
    """
    try:
        n = count_voa_rows()
    except Exception as exc:
        log.error("Health check: database unreachable: %s", exc)
        return {"status": "degraded", "voa_row_count": None, "error": "database_unreachable"}
    return {"status": "ok", "voa_row_count": n}
