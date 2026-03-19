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

import logging
import math
import os
from typing import Any

log = logging.getLogger(__name__)

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
    postcode_prefix: str | None = None,
) -> list[dict]:
    """
    Return comparable properties within radius_m of (lat, lon) matching scat_codes.

    When postcode_prefix is supplied (e.g. "SW20 8"), an additional filter
    restricts comparables to postcodes starting with that string, tightening
    the pool to the same local market area.

    Uses a bounding-box pre-filter for performance, then returns all columns
    needed by the CSA algorithm. Returns [] if the database is unavailable.
    """
    if not scat_codes:
        return []

    log.warning(
        "DB_DEBUG get_comparables lat=%s lon=%s scat_codes=%s radius_m=%s nia_sqm=%s size_band_pct=%s",
        round(lat, 4), round(lon, 4), scat_codes, radius_m, nia_sqm, size_band_pct,
    )

    # DEBUG: count rows matching SCAT only, before any distance or size filter
    try:
        with Session(engine) as _s:
            _scat_count = _s.execute(
                text("SELECT COUNT(*) FROM voa_list_entries WHERE scat_code IN :sc AND rateable_value > 0"),
                {"sc": tuple(scat_codes)},
            ).scalar()
        log.warning("DB_DEBUG rows_matching_scat_only=%s", _scat_count)
    except Exception as _e:
        log.warning("DB_DEBUG rows_matching_scat_only=ERROR %s", _e)

    # Bounding-box deltas (1° lat ≈ 111 km; 1° lon ≈ 111 km × cos(lat))
    lat_delta = radius_m / 111_000
    lon_delta = radius_m / (111_000 * max(math.cos(math.radians(lat)), 0.001))

    lo_nia = nia_sqm * (1 - size_band_pct / 100)
    hi_nia = nia_sqm * (1 + size_band_pct / 100)

    postcode_clause = (
        "AND le.postcode LIKE :postcode_prefix"
        if postcode_prefix is not None
        else ""
    )

    sql = text(f"""
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
            {postcode_clause}
    """)

    params: dict[str, Any] = {
        "scat_codes": tuple(scat_codes),
        "lat_lo": lat - lat_delta,
        "lat_hi": lat + lat_delta,
        "lon_lo": lon - lon_delta,
        "lon_hi": lon + lon_delta,
        "lo_nia": lo_nia,
        "hi_nia": hi_nia,
        "nia_fallback": nia_sqm,
    }
    if postcode_prefix is not None:
        # Append % here so the SQL literal never contains %, avoiding psycopg2
        # treating it as a parameter placeholder escape character.
        params["postcode_prefix"] = postcode_prefix + "%"

    try:
        with Session(engine) as session:
            rows = session.execute(sql, params).fetchall()
        results = [dict(r._mapping) for r in rows]
    except Exception as _e:
        # Database not yet populated — caller will return "Insufficient Data"
        log.warning("DB_DEBUG query_exception=%s", _e)
        return []

    log.warning("DB_DEBUG rows_after_bbox_and_size_filter=%s", len(results))

    # Filter to comparables within ±30% of the median RV/sqm
    rv_psm = [r["rv"] / r["nia_sqm"] for r in results if r["nia_sqm"] and r["nia_sqm"] > 0]
    if rv_psm:
        sorted_psm = sorted(rv_psm)
        mid = len(sorted_psm) // 2
        median = (sorted_psm[mid] + sorted_psm[~mid]) / 2
        lo, hi = median * 0.70, median * 1.30
        results = [r for r in results if r["nia_sqm"] and lo <= r["rv"] / r["nia_sqm"] <= hi]

    log.warning("DB_DEBUG rows_after_outlier_filter=%s (final returned)", len(results))
    return results


def get_sv_line_descs_batch(uarns: list[str]) -> dict[str, tuple[str, ...]]:
    """
    Return SV line descriptions keyed by UARN for a batch of subject properties.

    The descriptions (e.g. 'Retail Zone A', 'Zone B', 'Remainder') come from
    the voa_sv_lines table and are used by _classify_retail_method() to
    distinguish ITZA-zoned high-street retail from area-based methods.

    Returns an empty dict if the table is unavailable or uarns is empty.
    """
    if not uarns:
        return {}
    sql = text("""
        SELECT uarn, description
        FROM voa_sv_lines
        WHERE uarn = ANY(:uarns)
          AND description IS NOT NULL
        ORDER BY uarn, line_number
    """)
    try:
        with Session(engine) as session:
            rows = session.execute(sql, {"uarns": list(uarns)}).fetchall()
        result: dict[str, list[str]] = {}
        for r in rows:
            result.setdefault(r.uarn, []).append(r.description)
        return {k: tuple(v) for k, v in result.items()}
    except Exception:
        return {}


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
