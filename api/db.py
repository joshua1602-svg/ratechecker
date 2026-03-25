"""
Database connection and comparable-property query.

Schema (created by the VOA ingest pipeline — see data/INGEST_SPEC.md):

  voa_list_entries   — one row per rated hereditament
  voa_sv_header      — summary valuation header (record type 01)
  postcode_coords    — latitude/longitude per postcode (geocoded reference table)

Query failures raise DatabaseError so the caller can return an explicit 503,
not a silent "Insufficient Data".
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


class DatabaseError(Exception):
    """Raised when a database query fails unexpectedly.

    Callers should translate this into an HTTP 503 — never silently degrade
    to "Insufficient Data".
    """


def _coerce_uarns_to_int(uarns: list[str]) -> list[int]:
    """Convert string UARNs to integers for PostgreSQL bigint column compatibility.

    Silently drops any non-numeric values (should not occur with real VOA data,
    but prevents a full batch failure from one bad value).
    """
    result: list[int] = []
    for u in uarns:
        try:
            result.append(int(u))
        except (ValueError, TypeError):
            log.warning("Skipping non-numeric UARN value: %r", u)
    return result


def get_comparables(
    lat: float,
    lon: float,
    scat_codes: list[int],
    radius_m: float = 2000,
    nia_sqm: float = 0,
    size_band_pct: float = 50,
    postcode_prefix: str | None = None,
    exclude_uarn: str | None = None,
) -> list[dict]:
    """
    Return comparable properties within radius_m of (lat, lon) matching scat_codes.

    When postcode_prefix is supplied (e.g. "SW20 8"), an additional filter
    restricts comparables to postcodes starting with that string, tightening
    the pool to the same local market area.

    When exclude_uarn is supplied, that UARN is excluded from results to
    prevent the subject property from acting as its own comparable.

    Uses a bounding-box pre-filter for performance, then returns all columns
    needed by the CSA algorithm.

    Raises DatabaseError on any query failure so the caller can return an
    explicit server error rather than silently degrading.
    """
    if not scat_codes:
        return []

    log.info(
        "get_comparables lat=%.4f lon=%.4f scat_codes=%s radius_m=%s nia_sqm=%s size_band_pct=%s exclude_uarn=%s",
        lat, lon, scat_codes, radius_m, nia_sqm, size_band_pct, exclude_uarn,
    )

    # Bounding-box deltas (1° lat ≈ 111 km; 1° lon ≈ 111 km × cos(lat))
    lat_delta = radius_m / 111_000
    lon_delta = radius_m / (111_000 * max(math.cos(math.radians(lat)), 0.001))

    lo_nia = nia_sqm * (1 - size_band_pct / 100)
    hi_nia = nia_sqm * (1 + size_band_pct / 100)

    # Build parameterized scat_code filter using numbered bind params
    scat_binds = {f"scat_{i}": int(s) for i, s in enumerate(scat_codes)}
    scat_placeholders = ", ".join(f":scat_{i}" for i in range(len(scat_codes)))

    # Optional clauses
    postcode_clause = "AND le.postcode LIKE :postcode_prefix" if postcode_prefix is not None else ""
    exclude_clause = "AND le.uarn != :exclude_uarn" if exclude_uarn is not None else ""

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
            le.scat_code IN ({scat_placeholders})
            AND (le.composite_indicator IS NULL OR le.composite_indicator != 'C')
            AND le.rateable_value > 0
            AND pc.latitude  BETWEEN :lat_lo AND :lat_hi
            AND pc.longitude BETWEEN :lon_lo AND :lon_hi
            AND COALESCE(svh.total_area_or_units, :nia_fallback) BETWEEN :lo_nia AND :hi_nia
            AND (svh.unit_of_measurement IS NULL OR svh.unit_of_measurement = 'NIA')
            {postcode_clause}
            {exclude_clause}
    """)

    params: dict[str, Any] = {
        **scat_binds,
        "lat_lo": lat - lat_delta,
        "lat_hi": lat + lat_delta,
        "lon_lo": lon - lon_delta,
        "lon_hi": lon + lon_delta,
        "lo_nia": lo_nia,
        "hi_nia": hi_nia,
        "nia_fallback": nia_sqm,
    }
    if postcode_prefix is not None:
        params["postcode_prefix"] = postcode_prefix + "%"
    if exclude_uarn is not None:
        try:
            params["exclude_uarn"] = int(exclude_uarn)
        except (ValueError, TypeError):
            log.warning("Non-numeric exclude_uarn %r — skipping exclusion", exclude_uarn)
            # Remove the clause from the SQL by not setting the param;
            # but since the SQL was already built with the clause, we need to
            # set it to a value that won't match anything (0 is not a valid UARN).
            params["exclude_uarn"] = 0

    try:
        with Session(engine) as session:
            rows = session.execute(sql, params).fetchall()
        results = [dict(r._mapping) for r in rows]
    except Exception as exc:
        log.error("get_comparables query failed: %s", exc, exc_info=True)
        raise DatabaseError(f"Comparable query failed: {exc}") from exc

    log.info("get_comparables returned %d rows", len(results))
    return results


def get_sv_line_descs_batch(uarns: list[str]) -> dict[str, tuple[str, ...]]:
    """
    Return SV line descriptions keyed by UARN for a batch of subject properties.

    Returns an empty dict if uarns is empty.
    Raises DatabaseError on query failure.
    """
    if not uarns:
        return {}
    int_uarns = _coerce_uarns_to_int(uarns)
    if not int_uarns:
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
            rows = session.execute(sql, {"uarns": int_uarns}).fetchall()
        result: dict[str, list[str]] = {}
        for r in rows:
            result.setdefault(str(r.uarn), []).append(r.description)
        return {k: tuple(v) for k, v in result.items()}
    except Exception as exc:
        log.error("get_sv_line_descs_batch failed: %s", exc, exc_info=True)
        raise DatabaseError(f"SV line descs query failed: {exc}") from exc


def get_sv_lines_batch(uarns: list[str]) -> dict[str, list[dict]]:
    """
    Return full SV line rows (floor, description, area) keyed by UARN.

    Used by the layout overweighting layer to build layout fingerprints
    for each comparable.  Read-only query.

    Returns an empty dict if uarns is empty.
    Raises DatabaseError on query failure.
    """
    if not uarns:
        return {}
    int_uarns = _coerce_uarns_to_int(uarns)
    if not int_uarns:
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
            rows = session.execute(sql, {"uarns": int_uarns}).fetchall()
        result: dict[str, list[dict]] = {}
        for r in rows:
            result.setdefault(str(r.uarn), []).append({
                "floor": r.floor,
                "description": r.description,
                "area": float(r.area) if r.area is not None else 0.0,
            })
        return result
    except Exception as exc:
        log.error("get_sv_lines_batch failed: %s", exc, exc_info=True)
        raise DatabaseError(f"SV lines query failed: {exc}") from exc


# ---------------------------------------------------------------------------
# 03-07 fit-layer batch queries
# ---------------------------------------------------------------------------

def get_sv_car_parking_batch(uarns: list[str]) -> dict[str, dict]:
    """
    Return car-parking summary keyed by UARN from voa_sv_car_parking (record type 05).

    Each value contains ``cp_spaces`` and ``cp_total`` (total parking value £).
    Used by the 03-07 fit layer to detect parking presence and contribution.
    Returns {} on empty input.  Raises DatabaseError on query failure.
    """
    if not uarns:
        return {}
    int_uarns = _coerce_uarns_to_int(uarns)
    if not int_uarns:
        return {}
    sql = text("""
        SELECT uarn, cp_spaces, cp_total
        FROM voa_sv_car_parking
        WHERE uarn = ANY(:uarns)
    """)
    try:
        with Session(engine) as session:
            rows = session.execute(sql, {"uarns": int_uarns}).fetchall()
        return {
            str(r.uarn): {
                "cp_spaces": float(r.cp_spaces) if r.cp_spaces is not None else None,
                "cp_total":  float(r.cp_total)  if r.cp_total  is not None else None,
            }
            for r in rows
        }
    except Exception as exc:
        log.error("get_sv_car_parking_batch failed: %s", exc, exc_info=True)
        raise DatabaseError(f"Car parking query failed: {exc}") from exc


def get_sv_additions_batch(uarns: list[str]) -> dict[str, dict]:
    """
    Return additions summary keyed by UARN from voa_sv_additions (record type 03).

    Aggregates ``SUM(oa_value)`` and row count per UARN.
    Returns {} on empty input.  Raises DatabaseError on query failure.
    """
    if not uarns:
        return {}
    int_uarns = _coerce_uarns_to_int(uarns)
    if not int_uarns:
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
            rows = session.execute(sql, {"uarns": int_uarns}).fetchall()
        return {
            str(r.uarn): {
                "total_oa_value": float(r.total_oa_value) if r.total_oa_value is not None else 0.0,
                "addition_count": int(r.addition_count),
            }
            for r in rows
        }
    except Exception as exc:
        log.error("get_sv_additions_batch failed: %s", exc, exc_info=True)
        raise DatabaseError(f"Additions query failed: {exc}") from exc


def get_sv_plant_machinery_batch(uarns: list[str]) -> dict[str, dict]:
    """
    Return plant-and-machinery summary keyed by UARN from voa_sv_plant_machinery
    (record type 04).

    Aggregates ``SUM(pm_value)`` and row count per UARN.
    Returns {} on empty input.  Raises DatabaseError on query failure.
    """
    if not uarns:
        return {}
    int_uarns = _coerce_uarns_to_int(uarns)
    if not int_uarns:
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
            rows = session.execute(sql, {"uarns": int_uarns}).fetchall()
        return {
            str(r.uarn): {
                "pm_value": float(r.pm_value) if r.pm_value is not None else 0.0,
                "pm_count": int(r.pm_count),
            }
            for r in rows
        }
    except Exception as exc:
        log.error("get_sv_plant_machinery_batch failed: %s", exc, exc_info=True)
        raise DatabaseError(f"Plant machinery query failed: {exc}") from exc


def get_sv_adjustment_totals_batch(uarns: list[str]) -> dict[str, dict]:
    """
    Return adjustment totals keyed by UARN from voa_sv_adjustment_totals (record type 07).

    Returns {} on empty input.  Raises DatabaseError on query failure.
    """
    if not uarns:
        return {}
    int_uarns = _coerce_uarns_to_int(uarns)
    if not int_uarns:
        return {}
    sql = text("""
        SELECT uarn, total_before_adj, total_adj
        FROM voa_sv_adjustment_totals
        WHERE uarn = ANY(:uarns)
    """)
    try:
        with Session(engine) as session:
            rows = session.execute(sql, {"uarns": int_uarns}).fetchall()
        return {
            str(r.uarn): {
                "total_before_adj": float(r.total_before_adj) if r.total_before_adj is not None else 0.0,
                "total_adj":        float(r.total_adj)        if r.total_adj        is not None else 0.0,
            }
            for r in rows
        }
    except Exception as exc:
        log.error("get_sv_adjustment_totals_batch failed: %s", exc, exc_info=True)
        raise DatabaseError(f"Adjustment totals query failed: {exc}") from exc





def _normalise_postcode(value: str | None) -> str:
    """Normalise postcode for deterministic equality comparison."""
    return "".join(str(value or "").upper().split())


def _normalise_address(value: str | None) -> str:
    """Normalise address string for deterministic matching."""
    text_value = str(value or "").upper()
    cleaned = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in text_value)
    return " ".join(cleaned.split())


def _build_subject_record_from_row(row: Any, floor_rows: list[dict]) -> dict[str, Any]:
    floor_areas = {"ground": 0.0, "basement": 0.0}
    for item in floor_rows:
        floor_label = str(item.get("floor") or "").lower()
        area = float(item.get("area") or 0.0)
        if "ground" in floor_label and "lower" not in floor_label:
            floor_areas["ground"] += area
        elif "lower ground" in floor_label or "basement" in floor_label:
            floor_areas["basement"] += area

    total = float(row.total_area_or_units) if row.total_area_or_units is not None else None
    if row.unit_of_measurement and str(row.unit_of_measurement).upper() != "NIA":
        total = None

    return {
        "uarn": str(row.uarn),
        "scat_code": int(row.scat_code) if row.scat_code is not None else None,
        "description": row.description,
        "total_area_sqm": total,
        "floor_areas": floor_areas,
    }




def get_subject_voa_record_by_reference(reference: str | None) -> dict[str, Any] | None:
    """Resolve a VOA subject record from an optional bill/property reference.

    Current implementation supports numeric references that map to VOA UARN.
    Invalid/unresolvable references return None.
    """
    if not reference:
        return None
    digits = "".join(ch for ch in str(reference) if ch.isdigit())
    if not digits:
        return None
    return get_subject_voa_record(digits)


def get_subject_voa_candidates_by_address_postcode(address: str, postcode: str) -> list[dict[str, Any]]:
    """Return all exact-normalised address+postcode VOA candidates."""
    normalised_postcode = _normalise_postcode(postcode)
    normalised_address = _normalise_address(address)
    if not normalised_postcode or not normalised_address:
        return []

    sql = text("""
        SELECT
            le.uarn,
            le.scat_code,
            le.primary_description_text AS description,
            svh.total_area_or_units AS total_area_or_units,
            svh.unit_of_measurement AS unit_of_measurement,
            le.full_property_identifier AS full_property_identifier
        FROM voa_list_entries le
        LEFT JOIN voa_sv_header svh ON le.uarn = svh.uarn
        WHERE REPLACE(UPPER(le.postcode), ' ', '') = :postcode
        ORDER BY le.uarn
    """)
    try:
        with Session(engine) as session:
            rows = session.execute(sql, {"postcode": normalised_postcode}).fetchall()

        candidates: list[dict[str, Any]] = []
        for row in rows:
            if _normalise_address(row.full_property_identifier) == normalised_address:
                floor_rows = get_sv_lines_batch([str(row.uarn)]).get(str(row.uarn), [])
                candidates.append(_build_subject_record_from_row(row, floor_rows))
        return candidates
    except Exception as exc:
        log.error("get_subject_voa_candidates_by_address_postcode failed: %s", exc, exc_info=True)
        raise DatabaseError(f"Subject candidate query failed: {exc}") from exc

def get_subject_voa_record_by_address_postcode(address: str, postcode: str) -> dict[str, Any] | None:
    """Return a VOA subject record only when address+postcode yields one exact candidate."""
    candidates = get_subject_voa_candidates_by_address_postcode(address, postcode)
    if len(candidates) == 1:
        return candidates[0]
    return None

def get_subject_voa_record(uarn: str | None) -> dict[str, Any] | None:
    """Return mapped VOA subject record for a single UARN."""
    if not uarn:
        return None
    try:
        int_uarn = int(uarn)
    except (TypeError, ValueError):
        return None

    sql = text("""
        SELECT
            le.uarn,
            le.scat_code,
            le.primary_description_text AS description,
            svh.total_area_or_units AS total_area_or_units,
            svh.unit_of_measurement AS unit_of_measurement
        FROM voa_list_entries le
        LEFT JOIN voa_sv_header svh ON le.uarn = svh.uarn
        WHERE le.uarn = :uarn
        LIMIT 1
    """)

    try:
        with Session(engine) as session:
            row = session.execute(sql, {"uarn": int_uarn}).fetchone()
        if row is None:
            return None
        floor_rows = get_sv_lines_batch([str(int_uarn)]).get(str(int_uarn), [])
        return _build_subject_record_from_row(row, floor_rows)
    except Exception as exc:
        log.error("get_subject_voa_record failed: %s", exc, exc_info=True)
        raise DatabaseError(f"Subject VOA record query failed: {exc}") from exc

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
    """
    stmts = [
        "CREATE INDEX IF NOT EXISTS idx_le_postcode ON voa_list_entries(postcode)",
        "CREATE INDEX IF NOT EXISTS idx_pc_latlon ON postcode_coords(latitude, longitude)",
    ]
    with Session(engine) as session:
        for stmt in stmts:
            session.execute(text(stmt))
        session.commit()
