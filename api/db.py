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
    _scat_in = ", ".join(str(int(s)) for s in scat_codes)
    try:
        with Session(engine) as _s:
            _scat_count = _s.execute(
                text(f"SELECT COUNT(*) FROM voa_list_entries WHERE scat_code IN ({_scat_in}) AND rateable_value > 0"),
            ).scalar()
        log.debug("DB_DEBUG rows_matching_scat_only=%s", _scat_count)
    except Exception as _e:
        log.debug("DB_DEBUG rows_matching_scat_only=ERROR %s", _e)

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
            le.scat_code IN ({_scat_in})
            AND le.composite_indicator IS DISTINCT FROM 'C'
            AND le.rateable_value > 0
            AND pc.latitude  BETWEEN :lat_lo AND :lat_hi
            AND pc.longitude BETWEEN :lon_lo AND :lon_hi
            AND COALESCE(svh.total_area_or_units, :nia_fallback) BETWEEN :lo_nia AND :hi_nia
            AND (svh.unit_of_measurement IS NULL OR svh.unit_of_measurement = 'NIA')
            {postcode_clause}
    """)

    params: dict[str, Any] = {
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

    log.debug("DB_DEBUG sql_query=\n%s", sql.text)
    log.debug("DB_DEBUG sql_params=%s", params)

    try:
        with Session(engine) as session:
            rows = session.execute(sql, params).fetchall()
        results = [dict(r._mapping) for r in rows]
    except Exception as _e:
        # Database not yet populated — caller will return "Insufficient Data"
        log.debug("DB_DEBUG query_exception=%s", _e)
        return []

    log.debug("DB_DEBUG rows_after_bbox_and_size_filter=%s", len(results))

    log.debug("DB_DEBUG rows_after_outlier_filter=%s (final returned)", len(results))
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


def get_sv_lines_batch(uarns: list[str]) -> dict[str, list[dict]]:
    """
    Return full SV line rows (floor, description, area) keyed by UARN.

    Used by the layout overweighting layer to build layout fingerprints
    for each comparable.  Read-only query.

    Returns an empty dict if the table is unavailable or uarns is empty.
    """
    if not uarns:
        return {}
    sql = text("""
        SELECT uarn, floor, description, area
        FROM voa_sv_lines
        WHERE uarn = ANY(:uarns)
          AND description IS NOT NULL
        ORDER BY uarn, line_number
    """)
    try:
        with Session(engine) as session:
            rows = session.execute(sql, {"uarns": list(uarns)}).fetchall()
        result: dict[str, list[dict]] = {}
        for r in rows:
            result.setdefault(str(r.uarn), []).append({
                "floor": r.floor,
                "description": r.description,
                "area": float(r.area) if r.area is not None else 0.0,
            })
        return result
    except Exception:
        log.warning("get_sv_lines_batch: query failed — returning empty dict")
        return {}


# ---------------------------------------------------------------------------
# 03-07 fit-layer batch queries
# ---------------------------------------------------------------------------

def get_sv_car_parking_batch(uarns: list[str]) -> dict[str, dict]:
    """
    Return car-parking summary keyed by UARN from voa_sv_car_parking (record type 05).

    Each value contains ``cp_spaces`` and ``cp_total`` (total parking value £).
    Used by the 03-07 fit layer to detect parking presence and contribution.
    Returns {} on failure or empty input — callers degrade gracefully.
    """
    if not uarns:
        return {}
    sql = text("""
        SELECT uarn, cp_spaces, cp_total
        FROM voa_sv_car_parking
        WHERE uarn = ANY(:uarns)
    """)
    try:
        with Session(engine) as session:
            rows = session.execute(sql, {"uarns": list(uarns)}).fetchall()
        return {
            str(r.uarn): {
                "cp_spaces": float(r.cp_spaces) if r.cp_spaces is not None else None,
                "cp_total":  float(r.cp_total)  if r.cp_total  is not None else None,
            }
            for r in rows
        }
    except Exception:
        return {}


def get_sv_additions_batch(uarns: list[str]) -> dict[str, dict]:
    """
    Return additions summary keyed by UARN from voa_sv_additions (record type 03).

    Aggregates ``SUM(oa_value)`` and row count per UARN.
    Used by the fit layer to detect additions presence and their contribution
    relative to the comp's rateable value.
    Returns {} on failure or empty input.
    """
    if not uarns:
        return {}
    sql = text("""
        SELECT uarn,
               SUM(oa_value) AS total_oa_value,
               COUNT(*)      AS addition_count
        FROM voa_sv_additions
        WHERE uarn = ANY(:uarns)
        GROUP BY uarn
    """)
    try:
        with Session(engine) as session:
            rows = session.execute(sql, {"uarns": list(uarns)}).fetchall()
        return {
            str(r.uarn): {
                "total_oa_value": float(r.total_oa_value) if r.total_oa_value is not None else 0.0,
                "addition_count": int(r.addition_count),
            }
            for r in rows
        }
    except Exception:
        return {}


def get_sv_plant_machinery_batch(uarns: list[str]) -> dict[str, dict]:
    """
    Return plant-and-machinery summary keyed by UARN from voa_sv_plant_machinery
    (record type 04).

    Aggregates ``SUM(pm_value)`` and row count per UARN.
    Treated as a complexity signal by the fit layer, not a direct valuation input.
    Returns {} on failure or empty input.
    """
    if not uarns:
        return {}
    sql = text("""
        SELECT uarn,
               SUM(pm_value) AS pm_value,
               COUNT(*)      AS pm_count
        FROM voa_sv_plant_machinery
        WHERE uarn = ANY(:uarns)
        GROUP BY uarn
    """)
    try:
        with Session(engine) as session:
            rows = session.execute(sql, {"uarns": list(uarns)}).fetchall()
        return {
            str(r.uarn): {
                "pm_value": float(r.pm_value) if r.pm_value is not None else 0.0,
                "pm_count": int(r.pm_count),
            }
            for r in rows
        }
    except Exception:
        return {}


def get_sv_adjustment_totals_batch(uarns: list[str]) -> dict[str, dict]:
    """
    Return adjustment totals keyed by UARN from voa_sv_adjustment_totals (record type 07).

    Preferred over voa_sv_adjustments (record type 06) because it gives a
    ready-made total_before_adj / total_adj pair for intensity calculation.
    Used by the fit layer as the adjustment-intensity signal.
    Returns {} on failure or empty input — fit layer treats missing data as neutral.
    """
    if not uarns:
        return {}
    sql = text("""
        SELECT uarn, total_before_adj, total_adj
        FROM voa_sv_adjustment_totals
        WHERE uarn = ANY(:uarns)
    """)
    try:
        with Session(engine) as session:
            rows = session.execute(sql, {"uarns": list(uarns)}).fetchall()
        return {
            str(r.uarn): {
                "total_before_adj": float(r.total_before_adj) if r.total_before_adj is not None else 0.0,
                "total_adj":        float(r.total_adj)        if r.total_adj        is not None else 0.0,
            }
            for r in rows
        }
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
