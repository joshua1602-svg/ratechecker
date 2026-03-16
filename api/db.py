"""
Database connection and comparable-property query.

Schema (created by the VOA ingest pipeline — see data/INGEST_SPEC.md):

  voa_list_entries   — one row per rated hereditament
  voa_sv_header      — summary valuation header (record type 01)
  postcode_coords    — latitude/longitude per postcode (geocoded reference table)

The query returns an empty list when the database does not exist or has no data,
allowing the API to return "Insufficient Data" cleanly.
"""
from __future__ import annotations

import math
import os
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

load_dotenv()  # must come before os.environ.get so .env values are present

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./ratechecker.db")

_engine_kwargs: dict[str, Any] = {}
if "sqlite" in DATABASE_URL:
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    # PostgreSQL / Supabase — SSL required, bounded pool for the API server
    _engine_kwargs["connect_args"] = {"sslmode": "require"}
    _engine_kwargs["pool_size"] = 5
    _engine_kwargs["max_overflow"] = 10

engine = create_engine(DATABASE_URL, **_engine_kwargs)


def get_comparables(
    lat: float,
    lon: float,
    scat_codes: list[int],
    radius_m: float = 2000,
    nia_sqm: float = 0,
    size_band_pct: float = 50,
) -> list[dict]:
    """
    Return comparable properties within radius_m of (lat, lon) matching scat_codes.

    Uses a bounding-box pre-filter for performance, then returns all columns
    needed by the CSA algorithm. Returns [] if the database is unavailable.
    """
    if not scat_codes:
        return []

    # Bounding-box deltas (1° lat ≈ 111 km; 1° lon ≈ 111 km × cos(lat))
    lat_delta = radius_m / 111_000
    lon_delta = radius_m / (111_000 * max(math.cos(math.radians(lat)), 0.001))

    lo_nia = nia_sqm * (1 - size_band_pct / 100)
    hi_nia = nia_sqm * (1 + size_band_pct / 100)

    sql = text("""
        SELECT
            le.uarn,
            le.full_property_identifier           AS address,
            le.scat_code,
            le.rateable_value                     AS rv,
            le.primary_description_text           AS description,
            COALESCE(svh.total_area_or_units, :nia_fallback) AS nia_sqm,
            svh.unadjusted_price_psm,
            svh.unit_of_measurement,
            pc.latitude                           AS lat,
            pc.longitude                          AS lon,
            CASE WHEN svh.uarn IS NOT NULL THEN 1 ELSE 0 END AS has_summary
        FROM voa_list_entries le
        LEFT JOIN voa_sv_header svh ON le.uarn = svh.uarn
        JOIN postcode_coords pc ON le.postcode = pc.postcode
        WHERE
            le.scat_code IN :scat_codes
            AND le.composite_indicator IS DISTINCT FROM 'C'
            AND le.rateable_value > 0
            AND pc.latitude  BETWEEN :lat_lo AND :lat_hi
            AND pc.longitude BETWEEN :lon_lo AND :lon_hi
            AND COALESCE(svh.total_area_or_units, :nia_fallback) BETWEEN :lo_nia AND :hi_nia
            AND (svh.unit_of_measurement IS NULL OR svh.unit_of_measurement = 'NIA')
    """)

    try:
        with Session(engine) as session:
            rows = session.execute(
                sql,
                {
                    "scat_codes": tuple(scat_codes),
                    "lat_lo": lat - lat_delta,
                    "lat_hi": lat + lat_delta,
                    "lon_lo": lon - lon_delta,
                    "lon_hi": lon + lon_delta,
                    "lo_nia": lo_nia,
                    "hi_nia": hi_nia,
                    "nia_fallback": nia_sqm,
                },
            ).fetchall()
        return [dict(r._mapping) for r in rows]
    except Exception:
        # Database not yet populated — caller will return "Insufficient Data"
        return []


def count_voa_rows() -> int:
    """
    Return the number of rows in voa_list_entries.
    Used by the /health endpoint to confirm the VOA dataset is loaded.
    Raises if the table does not exist or the connection fails.
    """
    with Session(engine) as session:
        return session.execute(text("SELECT COUNT(*) FROM voa_list_entries")).scalar()


def ensure_runtime_indexes() -> None:
    """
    Create indexes needed for the API query path that are not created by the
    ingest pipeline.  All statements use IF NOT EXISTS — safe to call on every
    startup against an already-indexed database.

    Indexes added here (beyond what data/ingest.py creates):
      - voa_list_entries(postcode)  — used in the JOIN to postcode_coords
      - postcode_coords(latitude, longitude) — used in the bounding-box WHERE clause
    """
    stmts = [
        # The ingest pipeline indexes postcode_sector; the API query joins on postcode
        "CREATE INDEX IF NOT EXISTS idx_le_postcode ON voa_list_entries(postcode)",
        # Bounding-box pre-filter scans postcode_coords on lat/lon
        "CREATE INDEX IF NOT EXISTS idx_pc_latlon ON postcode_coords(latitude, longitude)",
    ]
    with Session(engine) as session:
        for stmt in stmts:
            session.execute(text(stmt))
        session.commit()
