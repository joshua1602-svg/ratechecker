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
import time
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
UNIVERSE_SOURCE_VIEW = "overassessment_universe"

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
    "derived_confidence_score",
    "derived_confidence_reason",
    "saving_score",
    "delta_score",
    "confidence_score_component",
    "comp_score",
    "same_street_score",
    "comp_count",
    "same_street_comp_count",
    "shortlist_score",
    "score_breakdown_summary",
    "score_breakdown_json",
    "early_filter_reason",
    "pass_fail_reason",
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
    universe_source TEXT NOT NULL,
    total_subjects_considered INTEGER NOT NULL DEFAULT 0,
    total_subjects_filtered_pre_assessment INTEGER NOT NULL DEFAULT 0,
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
    derived_confidence_score REAL,
    saving_score REAL,
    delta_score REAL,
    confidence_score_component REAL,
    comp_score REAL,
    same_street_score REAL,
    comp_count INTEGER,
    same_street_comp_count INTEGER,
    shortlist_score REAL,
    score_breakdown_summary TEXT,
    score_breakdown_json TEXT,
    primary_reason TEXT,
    pass_fail_reason TEXT,
    early_filter_reason TEXT,
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


def _cap_to_percent(value: float, cap: float) -> float:
    if cap <= 0:
        return 0.0
    return round(max(0.0, min(100.0, (max(0.0, value) / cap) * 100.0)), 2)


def _derive_confidence_proxy(*, signal: str | None, comp_count: int, same_street_comp_count: int) -> tuple[float, str, dict[str, Any]]:
    """Derive a transparent confidence proxy from existing assess outputs.

    Raw inputs are intentionally limited to fields confirmed in AssessResponse:
    - signal
    - comparable_count
    - rated_comps[].is_same_street

    Score starts from signal mapping then adjusts for evidence depth and quality
    penalties. Final score is a bounded 0.0-1.0 value.
    """
    signal_map = {
        "High": 0.90,
        "Medium": 0.70,
        "Low": 0.45,
        "Insufficient Data": 0.20,
    }
    base = signal_map.get(signal or "", 0.30)
    adjustments: list[str] = [f"signal_base={base:.2f}"]

    if comp_count >= 8:
        base += 0.05
        adjustments.append("comp_count>=8:+0.05")
    elif comp_count <= 2:
        base -= 0.10
        adjustments.append("comp_count<=2:-0.10")

    if same_street_comp_count >= 4:
        base += 0.03
        adjustments.append("same_street>=4:+0.03")

    # Ambiguity / quality penalties (from supported payload signals).
    if comp_count == 0:
        base -= 0.15
        adjustments.append("ambiguity_no_comps:-0.15")
    if signal == "Insufficient Data":
        base -= 0.10
        adjustments.append("insufficient_data:-0.10")
    if signal == "Low" and comp_count < 3:
        base -= 0.05
        adjustments.append("low_signal_shallow_pool:-0.05")

    score = round(max(0.0, min(1.0, base)), 3)
    reason = " | ".join(adjustments)
    raw = {
        "signal": signal,
        "comp_count": comp_count,
        "same_street_comp_count": same_street_comp_count,
        "signal_map": signal_map,
        "adjustments_applied": adjustments,
    }
    return score, reason, raw


def _score_components(*, annual_saving_high: float, rv_delta_pct: float, confidence_score: float, comp_count: int, same_street_comp_count: int) -> dict[str, float]:
    """Normalized shortlist sub-scores (all 0-100, capped).

    Caps are fixed so extreme outliers do not dominate rank:
    - saving_score:      annual_saving_high capped at £10,000
    - delta_score:       rv_delta_pct capped at 30%
    - confidence_score:  derived confidence 0-1 mapped to 0-100
    - comp_score:        comparable count capped at 10
    - same_street_score: same-street comparable count capped at 6
    """
    saving_score = _cap_to_percent(annual_saving_high, 10_000.0)
    delta_score = _cap_to_percent(rv_delta_pct, 30.0)
    confidence_component = round(max(0.0, min(1.0, confidence_score)) * 100.0, 2)
    comp_score = _cap_to_percent(float(comp_count), 10.0)
    same_street_score = _cap_to_percent(float(same_street_comp_count), 6.0)

    shortlist_score = round(
        0.35 * saving_score
        + 0.20 * delta_score
        + 0.20 * confidence_component
        + 0.15 * comp_score
        + 0.10 * same_street_score,
        2,
    )

    return {
        "saving_score": saving_score,
        "delta_score": delta_score,
        "confidence_score_component": confidence_component,
        "comp_score": comp_score,
        "same_street_score": same_street_score,
        "shortlist_score": shortlist_score,
    }


def _parse_list_arg(value: str | None) -> list[str]:
    if not value:
        return []
    return [x.strip() for x in value.split(",") if x.strip()]


def _validate_runtime_scope(args: argparse.Namespace) -> None:
    """Fail fast when no explicit runtime scope guard is provided.

    Mandatory safety requirement: operators must provide at least one explicit
    scope constraint so the runner cannot accidentally scan the whole universe.
    """
    if getattr(args, "_scope_filter_provided", False):
        return
    raise ValueError(
        "Refusing to run without explicit scope. Provide at least one of: "
        "--postcode, --postcode-prefix, --sector, --supported-sectors-only, or --limit.",
    )


def _universe_source_sql() -> str:
    # Canonical shortlist universe source: SQL helper view from this repo.
    # Join to voa_list_entries for description field filters.
    return """
SELECT
    ou.id,
    ou.uarn,
    ou.address,
    ou.address AS property_name,
    ou.postcode,
    ou.business_type,
    ou.scat_code,
    ou.voa_rv,
    ou.nia_sqm,
    le.primary_description_text
FROM overassessment_universe ou
LEFT JOIN voa_list_entries le ON CAST(le.uarn AS TEXT) = ou.uarn
"""


def _build_where_clauses(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    clauses = [
        "ou.voa_rv > 0",
        "ou.nia_sqm > 0",
    ]
    params: dict[str, Any] = {}

    postcodes = _parse_list_arg(args.postcode)
    if postcodes:
        placeholders = []
        for i, val in enumerate(postcodes):
            key = f"postcode_{i}"
            placeholders.append(f":{key}")
            params[key] = val.upper()
        clauses.append(f"UPPER(ou.postcode) IN ({', '.join(placeholders)})")

    if args.postcode_prefix:
        params["postcode_prefix"] = args.postcode_prefix.upper() + "%"
        clauses.append("UPPER(ou.postcode) LIKE :postcode_prefix")

    if args.sector:
        sectors = {x.lower() for x in _parse_list_arg(args.sector)}
        clauses.append("LOWER(COALESCE(ou.business_type, '')) IN (" + ", ".join(f":sector_{i}" for i, _ in enumerate(sectors)) + ")")
        for i, sector in enumerate(sorted(sectors)):
            params[f"sector_{i}"] = sector

    if args.min_rv is not None:
        clauses.append("ou.voa_rv >= :min_rv")
        params["min_rv"] = args.min_rv
    if args.max_rv is not None:
        clauses.append("ou.voa_rv <= :max_rv")
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
        clauses.append(f"ou.uarn IN ({', '.join(placeholders)})")

    exclude_uarns = _parse_list_arg(args.exclude_uarns)
    if exclude_uarns:
        placeholders = []
        for i, uarn in enumerate(exclude_uarns):
            key = f"exclude_uarn_{i}"
            placeholders.append(f":{key}")
            params[key] = uarn
        clauses.append(f"ou.uarn NOT IN ({', '.join(placeholders)})")

    if args.supported_sectors_only:
        clauses.append("LOWER(COALESCE(ou.business_type, '')) IN ('restaurant_cafe','retail','hair_beauty','nursery','pub')")

    where_sql = "\n  WHERE " + "\n    AND ".join(clauses)
    return where_sql, params


def _subject_query_sql(args: argparse.Namespace) -> tuple[str, dict[str, Any], str]:
    where_sql, params = _build_where_clauses(args)
    sql = _universe_source_sql() + where_sql + "\nORDER BY ou.uarn\nLIMIT :limit OFFSET :offset\n"
    return sql, params, UNIVERSE_SOURCE_VIEW


def _fetch_subject_rows(args: argparse.Namespace, limit: int, offset: int) -> tuple[list[dict[str, Any]], str]:
    from data.db import get_engine

    sql, params, source = _subject_query_sql(args)
    bound = dict(params)
    bound.update({"limit": limit, "offset": offset})

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(text(sql), bound).mappings().all()
        return [dict(r) for r in rows], source


def _build_request(row: dict[str, Any]) -> AssessRequest:
    business_type = _infer_business_type(row.get("business_type"), row.get("scat_code"))
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


def _early_filter_reason(row: dict[str, Any]) -> str | None:
    if not str(row.get("postcode") or "").strip():
        return "missing_postcode"
    if float(row.get("voa_rv") or 0) <= 0:
        return "non_positive_rv"
    if float(row.get("nia_sqm") or 0) <= 0:
        return "non_positive_nia"
    try:
        _infer_business_type(row.get("business_type"), row.get("scat_code"))
    except ValueError:
        return "unsupported_business_type"
    return None


def _extract_result(row: dict[str, Any], response: Any) -> dict[str, Any]:
    point_rv = response.adjusted_estimated_rv if response.adjusted_estimated_rv is not None else response.base_estimated_rv
    fair_low, fair_high = build_downside_rv_range(point_rv)
    fair_mid = round(((fair_low or 0) + (fair_high or 0)) / 2 / 100) * 100 if fair_low is not None and fair_high is not None else None

    current_rv = float(row.get("voa_rv") or 0)
    rv_delta = (current_rv - float(point_rv)) if point_rv is not None else None
    rv_delta_pct = ((rv_delta / current_rv) * 100.0) if rv_delta is not None and current_rv > 0 else 0.0

    rated_comps = response.rated_comps or []
    comp_count = int(response.comparable_count or len(rated_comps) or 0)
    same_street_comp_count = sum(1 for c in rated_comps if bool(c.get("is_same_street")))

    derived_confidence_score, derived_confidence_reason, confidence_raw_inputs = _derive_confidence_proxy(
        signal=response.signal,
        comp_count=comp_count,
        same_street_comp_count=same_street_comp_count,
    )

    annual_low = float(response.implied_annual_saving_low or 0)
    annual_high = float(response.implied_annual_saving_high or 0)
    saving_point = float(response.implied_annual_saving_point or 0)

    score_bits = _score_components(
        annual_saving_high=annual_high,
        rv_delta_pct=float(rv_delta_pct or 0),
        confidence_score=derived_confidence_score,
        comp_count=comp_count,
        same_street_comp_count=same_street_comp_count,
    )

    reason_codes = [
        f"signal:{response.signal}",
        f"comps:{comp_count}",
        f"same_street:{same_street_comp_count}",
    ]
    primary_reason = f"{response.signal} signal with £{saving_point:,.0f} implied annual saving"
    reason_summary = f"{comp_count} comps; {same_street_comp_count} same-street; tone={response.tone_source_label or response.tone_source or 'unknown'}"
    score_breakdown = {
        "saving_score": score_bits["saving_score"],
        "delta_score": score_bits["delta_score"],
        "confidence_score_component": score_bits["confidence_score_component"],
        "comp_score": score_bits["comp_score"],
        "same_street_score": score_bits["same_street_score"],
        "weights": {
            "saving_score": 0.35,
            "delta_score": 0.20,
            "confidence_score_component": 0.20,
            "comp_score": 0.15,
            "same_street_score": 0.10,
        },
    }
    dominant_driver = max(
        (
            ("saving_score", 0.35 * score_bits["saving_score"]),
            ("delta_score", 0.20 * score_bits["delta_score"]),
            ("confidence_score_component", 0.20 * score_bits["confidence_score_component"]),
            ("comp_score", 0.15 * score_bits["comp_score"]),
            ("same_street_score", 0.10 * score_bits["same_street_score"]),
        ),
        key=lambda x: x[1],
    )[0]
    score_breakdown_summary = (
        f"dominant={dominant_driver}; "
        f"saving={score_bits['saving_score']:.1f}, "
        f"delta={score_bits['delta_score']:.1f}, "
        f"conf={score_bits['confidence_score_component']:.1f}, "
        f"comp={score_bits['comp_score']:.1f}, "
        f"same_street={score_bits['same_street_score']:.1f}"
    )

    return {
        "uarn": row.get("uarn"),
        "property_name": row.get("property_name") or row.get("address"),
        "address": row.get("address"),
        "postcode": row.get("postcode"),
        "sector": _infer_business_type(row.get("business_type"), row.get("scat_code")),
        "scat_code": row.get("scat_code"),
        "current_rv": current_rv,
        "fair_rv_low": fair_low,
        "fair_rv_high": fair_high,
        "fair_rv_mid": fair_mid,
        "rv_delta": round(rv_delta, 2) if rv_delta is not None else None,
        "rv_delta_pct": round(rv_delta_pct, 2),
        "estimated_annual_saving_low": round(annual_low, 2),
        "estimated_annual_saving_high": round(annual_high, 2),
        "confidence_score": derived_confidence_score,
        "derived_confidence_score": derived_confidence_score,
        "derived_confidence_reason": derived_confidence_reason,
        **score_bits,
        "comp_count": comp_count,
        "same_street_comp_count": same_street_comp_count,
        "score_breakdown_summary": score_breakdown_summary,
        "score_breakdown_json": json.dumps(score_breakdown, ensure_ascii=False),
        "primary_reason": primary_reason,
        "reason_summary": reason_summary,
        "reason_codes": reason_codes,
        "evidence_summary": {
            "tone_source": response.tone_source,
            "tone_source_label": response.tone_source_label,
            "signal": response.signal,
            "comparable_count": comp_count,
            "confidence_raw_inputs": confidence_raw_inputs,
        },
    }


def _passes_filters(result: dict[str, Any], args: argparse.Namespace) -> tuple[bool, list[str], list[str], str]:
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

    if result["derived_confidence_score"] >= args.min_confidence:
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

    severe_quality = result["derived_confidence_score"] < 0.30
    if args.exclude_severe_quality and severe_quality:
        fail_flags.append("severe_data_quality")

    if fail_flags:
        return False, pass_flags, fail_flags, f"filtered:{','.join(fail_flags)}"
    return True, pass_flags, fail_flags, "pass:all_thresholds_met"


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
        "derived_confidence_score": 0.0,
        "derived_confidence_reason": "",
        "saving_score": 0.0,
        "delta_score": 0.0,
        "confidence_score_component": 0.0,
        "comp_score": 0.0,
        "same_street_score": 0.0,
        "comp_count": 0,
        "same_street_comp_count": 0,
        "shortlist_score": 0.0,
        "score_breakdown_summary": "",
        "score_breakdown_json": "{}",
        "early_filter_reason": None,
        "pass_fail_reason": "",
        "primary_reason": "",
        "reason_summary": "",
        "reason_codes": [],
        "evidence_summary": {},
        "pass_flags": [],
        "fail_flags": [],
        "status": "error",
        "error_message": "",
    }

    early_reason = _early_filter_reason(row)
    if early_reason:
        base["status"] = "skipped_pre_assessment"
        base["early_filter_reason"] = early_reason
        base["pass_fail_reason"] = f"skipped:{early_reason}"
        return CandidateResult(base)

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
        passed, pass_flags, fail_flags, pass_fail_reason = _passes_filters(extracted, args)
        base.update(extracted)
        base["pass_flags"] = pass_flags
        base["fail_flags"] = fail_flags
        base["pass_fail_reason"] = pass_fail_reason
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


def _write_review_csv(rows: list[dict[str, Any]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _sorted_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rank = {"pass": 0, "filtered_out": 1, "skipped_pre_assessment": 2, "error": 3}
    return sorted(
        rows,
        key=lambda r: (
            rank.get(str(r.get("status")), 99),
            -(float(r.get("shortlist_score") or 0.0)),
            -(float(r.get("estimated_annual_saving_high") or 0.0)),
            -(float(r.get("rv_delta_pct") or 0.0)),
        ),
    )


def _ensure_shortlist_tables(conn: Any) -> None:
    conn.execute(text(CREATE_SHORTLIST_RUNS))
    conn.execute(text(CREATE_SHORTLIST_CANDIDATES))


def _insert_run(conn: Any, run_id: str, args: argparse.Namespace, started_at: str, universe_source: str) -> None:
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
            (id, started_at, status, run_scope_json, universe_source, algorithm_version, notes)
            VALUES (:id, :started_at, :status, :run_scope_json, :universe_source, :algorithm_version, :notes)
            """,
        ),
        {
            "id": run_id,
            "started_at": started_at,
            "status": "running",
            "run_scope_json": json.dumps(run_scope),
            "universe_source": universe_source,
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
                    derived_confidence_score, saving_score, delta_score, confidence_score_component,
                    comp_score, same_street_score, comp_count, same_street_comp_count,
                    shortlist_score, score_breakdown_summary, score_breakdown_json,
                    primary_reason, pass_fail_reason, early_filter_reason,
                    reason_codes_json, evidence_summary_json, pass_flags_json, fail_flags_json, created_at
                ) VALUES (
                    :id, :run_id, :uarn, :property_name, :address, :postcode, :sector, :scat_code,
                    :current_rv, :estimated_fair_rv_low, :estimated_fair_rv_high, :estimated_fair_rv_mid,
                    :rv_delta, :rv_delta_pct, :estimated_annual_saving_low, :estimated_annual_saving_high,
                    :derived_confidence_score, :saving_score, :delta_score, :confidence_score_component,
                    :comp_score, :same_street_score, :comp_count, :same_street_comp_count,
                    :shortlist_score, :score_breakdown_summary, :score_breakdown_json,
                    :primary_reason, :pass_fail_reason, :early_filter_reason,
                    :reason_codes_json, :evidence_summary_json, :pass_flags_json, :fail_flags_json, :created_at
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
                "derived_confidence_score": row.get("derived_confidence_score"),
                "saving_score": row.get("saving_score"),
                "delta_score": row.get("delta_score"),
                "confidence_score_component": row.get("confidence_score_component"),
                "comp_score": row.get("comp_score"),
                "same_street_score": row.get("same_street_score"),
                "comp_count": row.get("comp_count"),
                "same_street_comp_count": row.get("same_street_comp_count"),
                "shortlist_score": row.get("shortlist_score"),
                "score_breakdown_summary": row.get("score_breakdown_summary"),
                "score_breakdown_json": row.get("score_breakdown_json"),
                "primary_reason": row.get("primary_reason"),
                "pass_fail_reason": row.get("pass_fail_reason"),
                "early_filter_reason": row.get("early_filter_reason"),
                "reason_codes_json": json.dumps(row.get("reason_codes") or []),
                "evidence_summary_json": json.dumps(row.get("evidence_summary") or {}),
                "pass_flags_json": json.dumps(row.get("pass_flags") or []),
                "fail_flags_json": json.dumps(row.get("fail_flags") or []),
                "created_at": created_at,
            },
        )


def _finalise_run(
    conn: Any,
    run_id: str,
    *,
    completed_at: str,
    status: str,
    considered: int,
    filtered_pre: int,
    assessed: int,
    passing: int,
) -> None:
    conn.execute(
        text(
            """
            UPDATE shortlist_runs
            SET completed_at = :completed_at,
                status = :status,
                total_subjects_considered = :considered,
                total_subjects_filtered_pre_assessment = :filtered_pre,
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
            "filtered_pre": filtered_pre,
            "assessed": assessed,
            "passing": passing,
        },
    )


async def _run(args: argparse.Namespace) -> int:
    _validate_runtime_scope(args)
    started_at = datetime.now(timezone.utc).isoformat()
    started_monotonic = time.monotonic()
    run_id = args.resume_run_id or f"shortlist-{started_at}"

    all_rows: list[dict[str, Any]] = []
    considered = 0
    assessed = 0
    filtered_pre = 0
    universe_source = UNIVERSE_SOURCE_VIEW

    sem = asyncio.Semaphore(args.concurrency)

    effective_limit = args.limit
    if args.max_subjects is not None:
        effective_limit = min(effective_limit, args.max_subjects)
    remaining = effective_limit
    batch_offset = args.offset

    log.info("Universe source=%s", universe_source)

    while remaining > 0:
        if args.max_runtime_minutes is not None:
            elapsed_minutes = (time.monotonic() - started_monotonic) / 60.0
            if elapsed_minutes >= args.max_runtime_minutes:
                log.warning("Stopping early due to --max-runtime-minutes=%s", args.max_runtime_minutes)
                break
        fetch_limit = min(args.batch_size, remaining)
        batch, source = _fetch_subject_rows(args, limit=fetch_limit, offset=batch_offset)
        universe_source = source
        if not batch:
            break

        considered += len(batch)
        log.info("Processing batch offset=%s size=%s", batch_offset, len(batch))

        tasks = [asyncio.create_task(_process_subject(row, sem, args)) for row in batch]
        batch_results = [t.data for t in await asyncio.gather(*tasks)]
        all_rows.extend(batch_results)

        assessed += sum(1 for r in batch_results if r.get("status") in {"pass", "filtered_out"})
        filtered_pre += sum(1 for r in batch_results if r.get("status") == "skipped_pre_assessment")

        batch_offset += len(batch)
        remaining -= len(batch)

    sorted_rows = _sorted_rows(all_rows)

    if args.export_csv:
        _write_csv(sorted_rows, Path(args.export_csv))
    if args.export_json:
        _write_json(sorted_rows, Path(args.export_json))
    if args.export_review_csv:
        _write_review_csv(sorted_rows, Path(args.export_review_csv))

    passing = [r for r in sorted_rows if r.get("status") == "pass"]
    filtered = [r for r in sorted_rows if r.get("status") == "filtered_out"]
    skipped = [r for r in sorted_rows if r.get("status") == "skipped_pre_assessment"]
    failed = [r for r in sorted_rows if r.get("status") == "error"]

    fail_reason_counts: dict[str, int] = {}
    for row in filtered:
        for reason in row.get("fail_flags") or []:
            fail_reason_counts[reason] = fail_reason_counts.get(reason, 0) + 1

    pre_reason_counts: dict[str, int] = {}
    for row in skipped:
        reason = row.get("early_filter_reason")
        if reason:
            pre_reason_counts[reason] = pre_reason_counts.get(reason, 0) + 1

    log.info(
        "Run complete run_id=%s considered=%s pre_filtered=%s assessed=%s passing=%s filtered=%s skipped=%s failed=%s",
        run_id,
        considered,
        filtered_pre,
        assessed,
        len(passing),
        len(filtered),
        len(skipped),
        len(failed),
    )
    log.info("Top early-filter reasons: %s", sorted(pre_reason_counts.items(), key=lambda kv: kv[1], reverse=True)[:5])
    log.info("Top fail reasons: %s", sorted(fail_reason_counts.items(), key=lambda kv: kv[1], reverse=True)[:5])
    log.info("Top 10 passed: %s", [
        {"uarn": r.get("uarn"), "shortlist_score": r.get("shortlist_score"), "reason": r.get("pass_fail_reason")}
        for r in passing[:10]
    ])
    borderline = [r for r in filtered if float(r.get("shortlist_score") or 0) >= 50][:10]
    log.info("Top 10 borderline: %s", [
        {"uarn": r.get("uarn"), "shortlist_score": r.get("shortlist_score"), "reason": r.get("pass_fail_reason")}
        for r in borderline
    ])

    if args.write_db and not args.dry_run:
        from data.db import get_engine

        engine = get_engine()
        completed_at = datetime.now(timezone.utc).isoformat()
        with engine.begin() as conn:
            _ensure_shortlist_tables(conn)
            if not args.resume_run_id:
                _insert_run(conn, run_id, args, started_at, universe_source)
            _insert_candidates(conn, run_id, sorted_rows, completed_at)
            _finalise_run(
                conn,
                run_id,
                completed_at=completed_at,
                status="completed",
                considered=considered,
                filtered_pre=filtered_pre,
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
    parser.add_argument("--export-review-csv", default="")
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
    parser.add_argument("--supported-sectors-only", action="store_true")
    parser.add_argument("--include-uarns", default="")
    parser.add_argument("--exclude-uarns", default="")
    parser.add_argument("--exclude-ambiguous", action="store_true", default=True)
    parser.add_argument("--exclude-severe-quality", action="store_true", default=True)
    parser.add_argument("--resume-run-id", default="")
    parser.add_argument("--max-runtime-minutes", type=float, default=None)
    parser.add_argument("--max-subjects", type=int, default=None)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    raw_args = list(argv) if argv is not None else sys.argv[1:]
    parsed = parser.parse_args(raw_args)
    parsed._scope_filter_provided = any(
        flag in raw_args
        for flag in ("--postcode", "--postcode-prefix", "--sector", "--supported-sectors-only", "--limit")
    )
    return parsed


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    log_level = "DEBUG" if args.verbose else args.log_level.upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.write_db and args.dry_run:
        log.warning("--write-db and --dry-run both set; dry-run takes precedence and DB writes are skipped")
    try:
        return asyncio.run(_run(args))
    except ValueError as exc:
        log.error("%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
