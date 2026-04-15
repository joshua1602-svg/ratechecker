#!/usr/bin/env python3
"""Autonomous overassessment shortlist runner.

This runner reuses ``api.routes.assess.run_assessment_pipeline`` as the single
valuation source of truth. It batch-selects eligible VOA subjects, runs the
existing assessment logic, applies configurable shortlist filters, ranks
candidates, optionally writes run/candidate rows, and exports CSV/JSON.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.engine.rules import csa_rules
from api.models import AssessRequest, BusinessType, ContactInput, FlagsInput, PropertyInput
from api.routes.assess import run_assessment_pipeline
from api.services.savings import build_downside_rv_range

log = logging.getLogger("shortlist_runner")

DEFAULT_LIMIT = 500
DEFAULT_BATCH_SIZE = 100
DEFAULT_ALGO_VERSION = "run_assessment_pipeline"

EXPORT_COLUMNS = [
    "uarn",
    "property_name",
    "address",
    "postcode",
    "sector",
    "scat_code",
    "current_rv",
    "fair_rv_low",
    "fair_rv_high",
    "fair_rv_mid",
    "rv_delta",
    "rv_delta_pct",
    "estimated_annual_saving_low",
    "estimated_annual_saving_high",
    "confidence_score",
    "comp_count",
    "same_street_comp_count",
    "shortlist_score",
    "primary_reason",
    "reason_summary",
    "status",
    "error_message",
]

CREATE_SHORTLIST_RUNS = """
CREATE TABLE IF NOT EXISTS shortlist_runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    run_scope_json TEXT NOT NULL,
    total_subjects_considered INTEGER NOT NULL DEFAULT 0,
    total_subjects_assessed INTEGER NOT NULL DEFAULT 0,
    total_candidates_passing INTEGER NOT NULL DEFAULT 0,
    algorithm_version TEXT NOT NULL,
    notes TEXT
)
"""

CREATE_SHORTLIST_CANDIDATES = """
CREATE TABLE IF NOT EXISTS shortlist_candidates (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    uarn TEXT,
    property_name TEXT,
    address TEXT,
    postcode TEXT,
    sector TEXT,
    scat_code INTEGER,
    current_rv REAL,
    estimated_fair_rv_low REAL,
    estimated_fair_rv_high REAL,
    estimated_fair_rv_mid REAL,
    rv_delta REAL,
    rv_delta_pct REAL,
    estimated_annual_saving_low REAL,
    estimated_annual_saving_high REAL,
    confidence_score REAL,
    comp_count INTEGER,
    same_street_comp_count INTEGER,
    shortlist_score REAL,
    primary_reason TEXT,
    reason_codes_json TEXT,
    evidence_summary_json TEXT,
    pass_flags_json TEXT,
    fail_flags_json TEXT,
    created_at TEXT NOT NULL
)
"""


@dataclass
class CandidateResult:
    data: dict[str, Any]


def _scat_to_business_type() -> dict[int, str]:
    sc = csa_rules()["scat_codes"]
    return {
        int(sc["cafe"]): "restaurant_cafe",
        int(sc["retail_shop"]): "retail",
        int(sc["showroom"]): "retail",
        int(sc["hair_beauty"]): "hair_beauty",
        int(sc["nursery"]): "nursery",
        int(sc["pub"]): "pub",
        int(sc["pub_with_lodge"]): "pub",
    }


def _infer_business_type(raw_business_type: Any, raw_scat_code: Any) -> str:
    if raw_business_type:
        v = str(raw_business_type).strip().lower()
        if v in {t.value for t in BusinessType}:
            return v
    try:
        scat = int(raw_scat_code)
    except (TypeError, ValueError):
        scat = None
    if scat is not None:
        mapped = _scat_to_business_type().get(scat)
        if mapped:
            return mapped
    raise ValueError(
        f"Could not resolve business_type from business_type={raw_business_type!r} scat_code={raw_scat_code!r}",
    )


def _confidence_score(signal: str | None, comp_count: int, same_street_comp_count: int) -> float:
    base = {
        "High": 0.90,
        "Medium": 0.70,
        "Low": 0.45,
        "Insufficient Data": 0.20,
    }.get(signal or "", 0.30)
    if comp_count >= 8:
        base += 0.05
    elif comp_count <= 2:
        base -= 0.10
    if same_street_comp_count >= 4:
        base += 0.03
    return round(max(0.0, min(1.0, base)), 3)


def _shortlist_score(
    *,
    annual_saving_low: float,
    annual_saving_high: float,
    rv_delta_pct: float,
    confidence_score: float,
    comp_count: int,
    same_street_comp_count: int,
) -> float:
    """Transparent ranking score.

    Formula (higher is better):
    - Saving component: midpoint annual saving (primary commercial weight)
    - Overassessment component: RV delta percent * 100
    - Evidence component: confidence score * 1000
    - Depth component: comparable count (capped) * 120
    - Same-street component: same-street count (capped) * 100
    """
    saving_mid = (max(0.0, annual_saving_low) + max(0.0, annual_saving_high)) / 2.0
    score = saving_mid
    score += max(0.0, rv_delta_pct) * 100.0
    score += max(0.0, confidence_score) * 1000.0
    score += min(max(comp_count, 0), 15) * 120.0
    score += min(max(same_street_comp_count, 0), 10) * 100.0
    return round(score, 2)


def _parse_list_arg(value: str | None) -> list[str]:
    if not value:
        return []
    return [x.strip() for x in value.split(",") if x.strip()]


def _build_where_clauses(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    clauses = [
        "le.rateable_value > 0",
        "svh.unit_of_measurement = 'NIA'",
        "svh.total_area_or_units > 0",
        "(le.composite_indicator IS NULL OR le.composite_indicator != 'C')",
    ]
    params: dict[str, Any] = {}

    postcodes = _parse_list_arg(args.postcode)
    if postcodes:
        placeholders = []
        for i, val in enumerate(postcodes):
            key = f"postcode_{i}"
            placeholders.append(f":{key}")
            params[key] = val.upper()
        clauses.append(f"UPPER(le.postcode) IN ({', '.join(placeholders)})")

    if args.postcode_prefix:
        params["postcode_prefix"] = args.postcode_prefix.upper() + "%"
        clauses.append("UPPER(le.postcode) LIKE :postcode_prefix")

    if args.sector:
        sectors = {x.lower() for x in _parse_list_arg(args.sector)}
        codes_by_type = {}
        for code, btype in _scat_to_business_type().items():
            codes_by_type.setdefault(btype, []).append(code)
        allow_codes: list[int] = []
        for sector in sectors:
            allow_codes.extend(codes_by_type.get(sector, []))
        if not allow_codes:
            clauses.append("1=0")
        else:
            placeholders = []
            for i, code in enumerate(sorted(set(allow_codes))):
                key = f"scat_{i}"
                placeholders.append(f":{key}")
                params[key] = int(code)
            clauses.append(f"le.scat_code IN ({', '.join(placeholders)})")

    if args.min_rv is not None:
        clauses.append("le.rateable_value >= :min_rv")
        params["min_rv"] = args.min_rv
    if args.max_rv is not None:
        clauses.append("le.rateable_value <= :max_rv")
        params["max_rv"] = args.max_rv

    if args.description_type:
        params["description_type"] = f"%{args.description_type.lower()}%"
        clauses.append("LOWER(COALESCE(le.primary_description_text, '')) LIKE :description_type")

    include_uarns = _parse_list_arg(args.include_uarns)
    if include_uarns:
        placeholders = []
        for i, uarn in enumerate(include_uarns):
            key = f"include_uarn_{i}"
            placeholders.append(f":{key}")
            params[key] = uarn
        clauses.append(f"CAST(le.uarn AS TEXT) IN ({', '.join(placeholders)})")

    exclude_uarns = _parse_list_arg(args.exclude_uarns)
    if exclude_uarns:
        placeholders = []
        for i, uarn in enumerate(exclude_uarns):
            key = f"exclude_uarn_{i}"
            placeholders.append(f":{key}")
            params[key] = uarn
        clauses.append(f"CAST(le.uarn AS TEXT) NOT IN ({', '.join(placeholders)})")

    if args.supported_sectors_only:
        supported_codes = sorted(_scat_to_business_type().keys())
        placeholders = []
        for i, code in enumerate(supported_codes):
            key = f"supported_scat_{i}"
            placeholders.append(f":{key}")
            params[key] = int(code)
        clauses.append(f"le.scat_code IN ({', '.join(placeholders)})")

    where_sql = "\n      AND ".join(clauses)
    return where_sql, params


def _subject_query_sql(where_sql: str) -> str:
    return f"""
SELECT
    CAST(le.uarn AS TEXT) AS id,
    CAST(le.uarn AS TEXT) AS uarn,
    le.full_property_identifier AS address,
    le.full_property_identifier AS property_name,
    le.postcode AS postcode,
    le.scat_code AS scat_code,
    le.primary_description_text AS primary_description_text,
    le.rateable_value::numeric AS voa_rv,
    svh.total_area_or_units::numeric AS nia_sqm
FROM voa_list_entries le
JOIN voa_sv_header svh ON svh.uarn = le.uarn
WHERE {where_sql}
ORDER BY le.uarn
LIMIT :limit OFFSET :offset
"""


def _fetch_subject_rows(args: argparse.Namespace, limit: int, offset: int) -> list[dict[str, Any]]:
    from data.db import get_engine

    where_sql, params = _build_where_clauses(args)
    sql = _subject_query_sql(where_sql)

    bound = dict(params)
    bound.update({"limit": limit, "offset": offset})

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(text(sql), bound).mappings().all()
        return [dict(r) for r in rows]


def _build_request(row: dict[str, Any]) -> AssessRequest:
    business_type = _infer_business_type(None, row.get("scat_code"))
    return AssessRequest(
        contact=ContactInput(
            email="shortlist@ratechecker.local",
            business_name=str(row.get("property_name") or row.get("address") or "").strip() or f"UARN {row.get('uarn')}",
        ),
        property=PropertyInput(
            address=str(row.get("address") or "").strip(),
            postcode=str(row.get("postcode") or "").strip(),
            uprn=str(row.get("uarn")) if row.get("uarn") is not None else None,
            business_type=BusinessType(business_type),
            voa_rv=float(row.get("voa_rv") or 0),
            nia_sqm=float(row.get("nia_sqm") or 0),
        ),
        flags=FlagsInput(consent_disclaimer=True, layout_flag=False, cramped_flag=False),
        captcha_token=None,
    )


def _extract_result(row: dict[str, Any], response: Any) -> dict[str, Any]:
    point_rv = response.adjusted_estimated_rv if response.adjusted_estimated_rv is not None else response.base_estimated_rv
    fair_low, fair_high = build_downside_rv_range(point_rv)
    fair_mid = round(((fair_low or 0) + (fair_high or 0)) / 2 / 100) * 100 if fair_low is not None and fair_high is not None else None

    current_rv = float(row.get("voa_rv") or 0)
    rv_delta = (current_rv - float(point_rv)) if point_rv is not None else None
    rv_delta_pct = ((rv_delta / current_rv) * 100.0) if rv_delta is not None and current_rv > 0 else None

    rated_comps = response.rated_comps or []
    comp_count = int(response.comparable_count or len(rated_comps) or 0)
    same_street_comp_count = sum(1 for c in rated_comps if bool(c.get("is_same_street")))
    confidence = _confidence_score(response.signal, comp_count, same_street_comp_count)

    annual_low = float(response.implied_annual_saving_low or 0)
    annual_high = float(response.implied_annual_saving_high or 0)
    saving_point = float(response.implied_annual_saving_point or 0)

    reason_codes = [
        f"signal:{response.signal}",
        f"comps:{comp_count}",
        f"same_street:{same_street_comp_count}",
    ]
    primary_reason = f"{response.signal} signal with £{saving_point:,.0f} implied annual saving"
    reason_summary = f"{comp_count} comps; {same_street_comp_count} same-street; tone={response.tone_source_label or response.tone_source or 'unknown'}"

    shortlist_score = _shortlist_score(
        annual_saving_low=annual_low,
        annual_saving_high=annual_high,
        rv_delta_pct=float(rv_delta_pct or 0),
        confidence_score=confidence,
        comp_count=comp_count,
        same_street_comp_count=same_street_comp_count,
    )

    return {
        "uarn": row.get("uarn"),
        "property_name": row.get("property_name") or row.get("address"),
        "address": row.get("address"),
        "postcode": row.get("postcode"),
        "sector": _infer_business_type(None, row.get("scat_code")),
        "scat_code": row.get("scat_code"),
        "current_rv": current_rv,
        "fair_rv_low": fair_low,
        "fair_rv_high": fair_high,
        "fair_rv_mid": fair_mid,
        "rv_delta": round(rv_delta, 2) if rv_delta is not None else None,
        "rv_delta_pct": round(rv_delta_pct, 2) if rv_delta_pct is not None else None,
        "estimated_annual_saving_low": round(annual_low, 2),
        "estimated_annual_saving_high": round(annual_high, 2),
        "confidence_score": confidence,
        "comp_count": comp_count,
        "same_street_comp_count": same_street_comp_count,
        "shortlist_score": shortlist_score,
        "primary_reason": primary_reason,
        "reason_summary": reason_summary,
        "reason_codes": reason_codes,
        "evidence_summary": {
            "tone_source": response.tone_source,
            "tone_source_label": response.tone_source_label,
            "signal": response.signal,
            "comparable_count": comp_count,
        },
    }


def _passes_filters(result: dict[str, Any], args: argparse.Namespace) -> tuple[bool, list[str], list[str]]:
    pass_flags: list[str] = []
    fail_flags: list[str] = []

    if result["estimated_annual_saving_high"] >= args.min_saving:
        pass_flags.append("min_saving")
    else:
        fail_flags.append("min_saving")

    if (result["rv_delta_pct"] or 0) >= args.min_delta_pct:
        pass_flags.append("min_delta_pct")
    else:
        fail_flags.append("min_delta_pct")

    if result["confidence_score"] >= args.min_confidence:
        pass_flags.append("min_confidence")
    else:
        fail_flags.append("min_confidence")

    if result["comp_count"] >= args.min_comp_count:
        pass_flags.append("min_comp_count")
    else:
        fail_flags.append("min_comp_count")

    if result["same_street_comp_count"] >= args.min_same_street_comp_count:
        pass_flags.append("min_same_street_comp_count")
    else:
        fail_flags.append("min_same_street_comp_count")

    ambiguous = result["comp_count"] <= 0
    if args.exclude_ambiguous and ambiguous:
        fail_flags.append("subject_match_ambiguity")

    severe_quality = result["confidence_score"] < 0.30
    if args.exclude_severe_quality and severe_quality:
        fail_flags.append("severe_data_quality")

    return len(fail_flags) == 0, pass_flags, fail_flags


async def _process_subject(row: dict[str, Any], semaphore: asyncio.Semaphore, args: argparse.Namespace) -> CandidateResult:
    base = {
        "uarn": row.get("uarn"),
        "property_name": row.get("property_name") or row.get("address"),
        "address": row.get("address"),
        "postcode": row.get("postcode"),
        "sector": None,
        "scat_code": row.get("scat_code"),
        "current_rv": row.get("voa_rv"),
        "fair_rv_low": None,
        "fair_rv_high": None,
        "fair_rv_mid": None,
        "rv_delta": None,
        "rv_delta_pct": None,
        "estimated_annual_saving_low": 0.0,
        "estimated_annual_saving_high": 0.0,
        "confidence_score": 0.0,
        "comp_count": 0,
        "same_street_comp_count": 0,
        "shortlist_score": 0.0,
        "primary_reason": "",
        "reason_summary": "",
        "reason_codes": [],
        "evidence_summary": {},
        "pass_flags": [],
        "fail_flags": [],
        "status": "error",
        "error_message": "",
    }

    try:
        req = _build_request(row)
        base["sector"] = req.property.business_type.value
    except Exception as exc:
        base["error_message"] = f"input_error: {exc}"
        return CandidateResult(base)

    try:
        async with semaphore:
            resp = await run_assessment_pipeline(req)
        extracted = _extract_result(row, resp)
        passed, pass_flags, fail_flags = _passes_filters(extracted, args)
        base.update(extracted)
        base["pass_flags"] = pass_flags
        base["fail_flags"] = fail_flags
        base["status"] = "pass" if passed else "filtered_out"
        return CandidateResult(base)
    except Exception as exc:
        base["error_message"] = f"pipeline_error: {exc}"
        return CandidateResult(base)


def _write_csv(rows: list[dict[str, Any]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=EXPORT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in EXPORT_COLUMNS})


def _write_json(rows: list[dict[str, Any]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)


def _sorted_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda r: (
            0 if r.get("status") == "pass" else 1,
            -(float(r.get("shortlist_score") or 0.0)),
            -(float(r.get("estimated_annual_saving_high") or 0.0)),
            -(float(r.get("rv_delta_pct") or 0.0)),
        ),
    )


def _ensure_shortlist_tables(conn: Any) -> None:
    conn.execute(text(CREATE_SHORTLIST_RUNS))
    conn.execute(text(CREATE_SHORTLIST_CANDIDATES))


def _insert_run(conn: Any, run_id: str, args: argparse.Namespace, started_at: str) -> None:
    run_scope = {
        "postcode": args.postcode,
        "postcode_prefix": args.postcode_prefix,
        "sector": args.sector,
        "limit": args.limit,
        "offset": args.offset,
        "batch_size": args.batch_size,
        "supported_sectors_only": args.supported_sectors_only,
    }
    conn.execute(
        text(
            """
            INSERT INTO shortlist_runs
            (id, started_at, status, run_scope_json, algorithm_version, notes)
            VALUES (:id, :started_at, :status, :run_scope_json, :algorithm_version, :notes)
            """,
        ),
        {
            "id": run_id,
            "started_at": started_at,
            "status": "running",
            "run_scope_json": json.dumps(run_scope),
            "algorithm_version": DEFAULT_ALGO_VERSION,
            "notes": "created by scripts/run_shortlist.py",
        },
    )


def _insert_candidates(conn: Any, run_id: str, rows: list[dict[str, Any]], created_at: str) -> None:
    for idx, row in enumerate(rows):
        candidate_id = f"{run_id}:{idx + 1}"
        conn.execute(
            text(
                """
                INSERT INTO shortlist_candidates (
                    id, run_id, uarn, property_name, address, postcode, sector, scat_code,
                    current_rv, estimated_fair_rv_low, estimated_fair_rv_high, estimated_fair_rv_mid,
                    rv_delta, rv_delta_pct, estimated_annual_saving_low, estimated_annual_saving_high,
                    confidence_score, comp_count, same_street_comp_count, shortlist_score,
                    primary_reason, reason_codes_json, evidence_summary_json,
                    pass_flags_json, fail_flags_json, created_at
                ) VALUES (
                    :id, :run_id, :uarn, :property_name, :address, :postcode, :sector, :scat_code,
                    :current_rv, :estimated_fair_rv_low, :estimated_fair_rv_high, :estimated_fair_rv_mid,
                    :rv_delta, :rv_delta_pct, :estimated_annual_saving_low, :estimated_annual_saving_high,
                    :confidence_score, :comp_count, :same_street_comp_count, :shortlist_score,
                    :primary_reason, :reason_codes_json, :evidence_summary_json,
                    :pass_flags_json, :fail_flags_json, :created_at
                )
                """,
            ),
            {
                "id": candidate_id,
                "run_id": run_id,
                "uarn": row.get("uarn"),
                "property_name": row.get("property_name"),
                "address": row.get("address"),
                "postcode": row.get("postcode"),
                "sector": row.get("sector"),
                "scat_code": row.get("scat_code"),
                "current_rv": row.get("current_rv"),
                "estimated_fair_rv_low": row.get("fair_rv_low"),
                "estimated_fair_rv_high": row.get("fair_rv_high"),
                "estimated_fair_rv_mid": row.get("fair_rv_mid"),
                "rv_delta": row.get("rv_delta"),
                "rv_delta_pct": row.get("rv_delta_pct"),
                "estimated_annual_saving_low": row.get("estimated_annual_saving_low"),
                "estimated_annual_saving_high": row.get("estimated_annual_saving_high"),
                "confidence_score": row.get("confidence_score"),
                "comp_count": row.get("comp_count"),
                "same_street_comp_count": row.get("same_street_comp_count"),
                "shortlist_score": row.get("shortlist_score"),
                "primary_reason": row.get("primary_reason"),
                "reason_codes_json": json.dumps(row.get("reason_codes") or []),
                "evidence_summary_json": json.dumps(row.get("evidence_summary") or {}),
                "pass_flags_json": json.dumps(row.get("pass_flags") or []),
                "fail_flags_json": json.dumps(row.get("fail_flags") or []),
                "created_at": created_at,
            },
        )


def _finalise_run(conn: Any, run_id: str, *, completed_at: str, status: str, considered: int, assessed: int, passing: int) -> None:
    conn.execute(
        text(
            """
            UPDATE shortlist_runs
            SET completed_at = :completed_at,
                status = :status,
                total_subjects_considered = :considered,
                total_subjects_assessed = :assessed,
                total_candidates_passing = :passing
            WHERE id = :id
            """,
        ),
        {
            "id": run_id,
            "completed_at": completed_at,
            "status": status,
            "considered": considered,
            "assessed": assessed,
            "passing": passing,
        },
    )


async def _run(args: argparse.Namespace) -> int:
    started_at = datetime.now(timezone.utc).isoformat()
    run_id = args.resume_run_id or f"shortlist-{started_at}"

    all_rows: list[dict[str, Any]] = []
    considered = 0
    assessed = 0

    sem = asyncio.Semaphore(args.concurrency)

    remaining = args.limit
    batch_offset = args.offset

    while remaining > 0:
        fetch_limit = min(args.batch_size, remaining)
        batch = _fetch_subject_rows(args, limit=fetch_limit, offset=batch_offset)
        if not batch:
            break

        considered += len(batch)
        log.info("Processing batch offset=%s size=%s", batch_offset, len(batch))

        tasks = [asyncio.create_task(_process_subject(row, sem, args)) for row in batch]
        batch_results = [t.data for t in await asyncio.gather(*tasks)]
        all_rows.extend(batch_results)

        assessed += sum(1 for r in batch_results if r.get("status") in {"pass", "filtered_out"})

        batch_offset += len(batch)
        remaining -= len(batch)

    sorted_rows = _sorted_rows(all_rows)

    if args.export_csv:
        _write_csv(sorted_rows, Path(args.export_csv))
    if args.export_json:
        _write_json(sorted_rows, Path(args.export_json))

    passing = [r for r in sorted_rows if r.get("status") == "pass"]
    filtered = [r for r in sorted_rows if r.get("status") == "filtered_out"]
    failed = [r for r in sorted_rows if r.get("status") == "error"]

    fail_reason_counts: dict[str, int] = {}
    for row in filtered:
        for reason in row.get("fail_flags") or []:
            fail_reason_counts[reason] = fail_reason_counts.get(reason, 0) + 1

    pass_reason_counts: dict[str, int] = {}
    for row in passing:
        for reason in row.get("pass_flags") or []:
            pass_reason_counts[reason] = pass_reason_counts.get(reason, 0) + 1

    log.info(
        "Run complete run_id=%s considered=%s assessed=%s passing=%s filtered=%s failed=%s",
        run_id,
        considered,
        assessed,
        len(passing),
        len(filtered),
        len(failed),
    )
    log.info("Top fail reasons: %s", sorted(fail_reason_counts.items(), key=lambda kv: kv[1], reverse=True)[:5])
    log.info("Top shortlist reasons: %s", sorted(pass_reason_counts.items(), key=lambda kv: kv[1], reverse=True)[:5])

    if args.write_db and not args.dry_run:
        from data.db import get_engine

        engine = get_engine()
        completed_at = datetime.now(timezone.utc).isoformat()
        with engine.begin() as conn:
            _ensure_shortlist_tables(conn)
            if not args.resume_run_id:
                _insert_run(conn, run_id, args, started_at)
            _insert_candidates(conn, run_id, sorted_rows, completed_at)
            _finalise_run(
                conn,
                run_id,
                completed_at=completed_at,
                status="completed",
                considered=considered,
                assessed=assessed,
                passing=len(passing),
            )

    return 0 if not failed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Batch shortlist runner using existing /assess valuation pipeline")
    parser.add_argument("--dry-run", action="store_true", help="Do not write shortlist rows to DB")
    parser.add_argument("--write-db", action="store_true", help="Persist run/candidate rows into shortlist tables")
    parser.add_argument("--export-csv", default="artifacts/shortlist.csv")
    parser.add_argument("--export-json", default="artifacts/shortlist.json")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--postcode", default="")
    parser.add_argument("--postcode-prefix", default="")
    parser.add_argument("--sector", default="")
    parser.add_argument("--description-type", default="")
    parser.add_argument("--min-rv", type=float, default=None)
    parser.add_argument("--max-rv", type=float, default=None)
    parser.add_argument("--min-saving", type=float, default=2500.0)
    parser.add_argument("--min-delta-pct", type=float, default=10.0)
    parser.add_argument("--min-confidence", type=float, default=0.60)
    parser.add_argument("--min-comp-count", type=int, default=3)
    parser.add_argument("--min-same-street-comp-count", type=int, default=0)
    parser.add_argument("--min-comp-density", type=int, default=0, help="Reserved for future DB-side density prefilter")
    parser.add_argument("--supported-sectors-only", action="store_true")
    parser.add_argument("--include-uarns", default="")
    parser.add_argument("--exclude-uarns", default="")
    parser.add_argument("--exclude-ambiguous", action="store_true", default=True)
    parser.add_argument("--exclude-severe-quality", action="store_true", default=True)
    parser.add_argument("--resume-run-id", default="")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    log_level = "DEBUG" if args.verbose else args.log_level.upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.write_db and args.dry_run:
        log.warning("--write-db and --dry-run both set; dry-run takes precedence and DB writes are skipped")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
