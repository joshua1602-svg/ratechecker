#!/usr/bin/env python3
"""Batch overassessment runner using the live /assess valuation pipeline.

This script calls the reusable live assessment service function that powers
the production assess result path (CSA + layout + fit + adjustments).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import sys

from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.models import (
    AssessRequest,
    BusinessType,
    ContactInput,
    FlagsInput,
    PropertyInput,
)
from api.engine.rules import csa_rules

log = logging.getLogger("batch_overassessment_runner")

DEFAULT_SOURCE_SQL = """
SELECT
    id,
    uarn,
    address,
    postcode,
    business_type,
    scat_code,
    voa_rv,
    nia_sqm
FROM overassessment_universe
ORDER BY id
LIMIT :limit OFFSET :offset
"""

OUTPUT_COLUMNS = [
    "id",
    "uarn",
    "address",
    "postcode",
    "business_type",
    "scat_code",
    "voa_rv",
    "modelled_rv",
    "fair_rv_low",
    "fair_rv_high",
    "abs_gap",
    "pct_gap",
    "overassessment_band",
    "is_overassessed",
    "confidence_score",
    "comparables_count",
    "status",
    "error_message",
]


@dataclass
class RowResult:
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
    raise ValueError(f"Could not resolve business_type from business_type={raw_business_type!r} scat_code={raw_scat_code!r}")


def _confidence_score(signal: str | None, comparable_count: int | None) -> float:
    base = {
        "High": 0.90,
        "Medium": 0.70,
        "Low": 0.45,
        "Insufficient Data": 0.20,
    }.get(signal or "", 0.30)
    if comparable_count is None:
        return round(base, 2)
    if comparable_count >= 8:
        return round(min(1.0, base + 0.05), 2)
    if comparable_count <= 2:
        return round(max(0.0, base - 0.10), 2)
    return round(base, 2)


def _banding(voa_rv: float, fair_high: float | None, pct_gap: float | None) -> tuple[str, bool]:
    if fair_high is None or voa_rv <= 0:
        return "not_flagged", False

    over = voa_rv > fair_high
    if not over:
        return "not_flagged", False

    if pct_gap is None:
        return "borderline", True
    if pct_gap >= 20:
        return "strong", True
    if pct_gap >= 10:
        return "moderate", True
    return "borderline", True


def _build_request(row: dict[str, Any]) -> AssessRequest:
    business_type = _infer_business_type(row.get("business_type"), row.get("scat_code"))
    address = str(row.get("address") or "").strip()
    postcode = str(row.get("postcode") or "").strip()
    nia_sqm = float(row.get("nia_sqm") or 0)
    voa_rv = float(row.get("voa_rv") or 0)

    if not postcode:
        raise ValueError("Missing postcode")
    if nia_sqm <= 0:
        raise ValueError(f"Invalid nia_sqm={nia_sqm}")
    if voa_rv <= 0:
        raise ValueError(f"Invalid voa_rv={voa_rv}")

    return AssessRequest(
        contact=ContactInput(
            email="batch@ratechecker.local",
            business_name=address or f"UARN {row.get('uarn') or row.get('id')}",
        ),
        property=PropertyInput(
            address=address,
            postcode=postcode,
            uprn=str(row.get("uarn")) if row.get("uarn") is not None else None,
            business_type=BusinessType(business_type),
            voa_rv=voa_rv,
            nia_sqm=nia_sqm,
        ),
        flags=FlagsInput(
            consent_disclaimer=True,
            layout_flag=False,
            cramped_flag=False,
        ),
        captcha_token=None,
    )


async def _process_one(
    row: dict[str, Any],
    semaphore: asyncio.Semaphore,
    assess_runner: Any,
) -> RowResult:
    base = {
        "id": row.get("id"),
        "uarn": row.get("uarn"),
        "address": row.get("address"),
        "postcode": row.get("postcode"),
        "business_type": row.get("business_type"),
        "scat_code": row.get("scat_code"),
        "voa_rv": row.get("voa_rv"),
        "modelled_rv": None,
        "fair_rv_low": None,
        "fair_rv_high": None,
        "abs_gap": None,
        "pct_gap": None,
        "overassessment_band": "not_flagged",
        "is_overassessed": False,
        "confidence_score": None,
        "comparables_count": 0,
        "status": "error",
        "error_message": None,
    }

    try:
        req = _build_request(row)
        base["business_type"] = req.property.business_type.value
    except Exception as exc:
        base["error_message"] = f"input_error: {exc}"
        return RowResult(base)

    try:
        async with semaphore:
            resp = await assess_runner(req)

        modelled_rv = (
            resp.adjusted_estimated_rv
            if resp.adjusted_estimated_rv is not None
            else resp.base_estimated_rv
        )
        fair_low = round(modelled_rv * 0.95 / 100) * 100 if modelled_rv is not None else None
        fair_high = round(modelled_rv * 1.05 / 100) * 100 if modelled_rv is not None else None

        voa_rv = float(req.property.voa_rv)
        abs_gap = float(voa_rv - modelled_rv) if modelled_rv is not None else None
        pct_gap = ((voa_rv - modelled_rv) / voa_rv * 100) if modelled_rv is not None and voa_rv > 0 else None

        band, is_over = _banding(voa_rv=voa_rv, fair_high=fair_high, pct_gap=pct_gap)
        comp_count = int(resp.comparable_count or 0)

        base.update({
            "modelled_rv": modelled_rv,
            "fair_rv_low": fair_low,
            "fair_rv_high": fair_high,
            "abs_gap": round(abs_gap, 2) if abs_gap is not None else None,
            "pct_gap": round(pct_gap, 2) if pct_gap is not None else None,
            "overassessment_band": band,
            "is_overassessed": is_over,
            "confidence_score": _confidence_score(resp.signal, comp_count),
            "comparables_count": comp_count,
            "status": "ok",
            "error_message": "",
        })
        return RowResult(base)
    except Exception as exc:
        base["error_message"] = f"pipeline_error: {exc}"
        return RowResult(base)


def _fetch_rows(source_sql: str, limit: int, offset: int) -> list[dict[str, Any]]:
    from data.db import get_engine

    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(text(source_sql), {"limit": limit, "offset": offset})
        return [dict(r) for r in result.mappings().all()]


def _write_csv(rows: list[dict[str, Any]], output_csv: Path) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in OUTPUT_COLUMNS})


def _rank_key(row: dict[str, Any]) -> tuple[int, float, float]:
    band_rank = {"strong": 0, "moderate": 1, "borderline": 2, "not_flagged": 3}
    return (
        band_rank.get(str(row.get("overassessment_band")), 99),
        -float(row.get("abs_gap") or 0.0),
        -float(row.get("pct_gap") or 0.0),
    )


async def _run(args: argparse.Namespace) -> int:
    rows = _fetch_rows(args.source_sql, args.limit, args.offset)
    if not rows:
        log.warning("No rows returned from source query")
        _write_csv([], Path(args.output_csv))
        return 0

    sem = asyncio.Semaphore(args.concurrency)
    from api.services.assessment import run_live_assessment
    assess_runner = run_live_assessment

    tasks = [
        asyncio.create_task(
            _process_one(
                r,
                sem,
                assess_runner=assess_runner,
            )
        )
        for r in rows
    ]
    results = [r.data for r in await asyncio.gather(*tasks)]
    ranked = sorted(results, key=_rank_key)
    _write_csv(ranked, Path(args.output_csv))

    ok = sum(1 for r in ranked if r["status"] == "ok")
    flagged = sum(1 for r in ranked if r["is_overassessed"])
    errors = len(ranked) - ok
    log.info("Completed %s rows: ok=%s flagged=%s errors=%s -> %s", len(ranked), ok, flagged, errors, args.output_csv)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run batch overassessment checks using live assess logic")
    parser.add_argument("--source-sql", default=DEFAULT_SOURCE_SQL, help="SQL query returning required columns")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--output-csv", default="artifacts/overassessment_batch_results.csv")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
