"""
Quick end-to-end test against the live Supabase VOA dataset.

Samples 50 properties per segment, runs the full CSA pipeline, and writes
results to quick_test_results.csv for manual inspection.

Usage (from project root):
    python tests/quick_test.py
"""
from __future__ import annotations

import os

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from api.db import get_comparables
from api.engine.csa import Comparable, run_csa

load_dotenv()
engine = create_engine(os.getenv("DATABASE_URL"))

SAMPLES = [
    {
        "label": "retail",
        "sql": """
            SELECT
                le.uarn,
                le.postcode,
                le.primary_description_text,
                le.rateable_value,
                svh.total_area_or_units AS nia_sqm
            FROM voa_list_entries le
            LEFT JOIN voa_sv_header svh ON le.uarn = svh.uarn
            WHERE le.primary_description_text ILIKE '%%SHOP%%'
            AND le.rateable_value > 0
            AND svh.total_area_or_units IS NOT NULL
            AND svh.unit_of_measurement = 'NIA'
            LIMIT 50
        """,
        "business_type": "retail",
        "scat_codes": [249],
    },
    {
        "label": "restaurant_cafe",
        "sql": """
            SELECT
                le.uarn,
                le.postcode,
                le.primary_description_text,
                le.rateable_value,
                svh.total_area_or_units AS nia_sqm
            FROM voa_list_entries le
            LEFT JOIN voa_sv_header svh ON le.uarn = svh.uarn
            WHERE (
                le.primary_description_text ILIKE '%%CAFE%%'
                OR le.primary_description_text ILIKE '%%RESTAURANT%%'
                OR le.primary_description_text ILIKE '%%TAKEAWAY%%'
            )
            AND le.rateable_value > 0
            AND svh.total_area_or_units IS NOT NULL
            LIMIT 50
        """,
        "business_type": "restaurant_cafe",
        "scat_codes": [409, 234],
    },
    {
        "label": "nursery",
        "sql": """
            SELECT
                le.uarn,
                le.postcode,
                le.primary_description_text,
                le.rateable_value,
                svh.total_area_or_units AS nia_sqm
            FROM voa_list_entries le
            LEFT JOIN voa_sv_header svh ON le.uarn = svh.uarn
            WHERE le.primary_description_text ILIKE '%%NURSERY%%'
            AND le.rateable_value > 0
            AND svh.total_area_or_units IS NOT NULL
            LIMIT 50
        """,
        "business_type": "nursery",
        "scat_codes": [85],
    },
]


def get_coords(postcode: str):
    sql = text("""
        SELECT latitude, longitude
        FROM postcode_coords
        WHERE postcode = :postcode
        LIMIT 1
    """)
    with engine.connect() as conn:
        row = conn.execute(sql, {"postcode": postcode}).fetchone()
    if not row:
        return None, None
    return float(row.latitude), float(row.longitude)


def dicts_to_comparables(rows: list[dict]) -> list[Comparable]:
    """Convert raw get_comparables() dicts to Comparable objects for run_csa()."""
    return [
        Comparable(
            uarn=r["uarn"],
            address=r.get("address") or "",
            scat_code=int(r["scat_code"]),
            rv=float(r["rv"]),
            nia_sqm=float(r["nia_sqm"]),
            unadjusted_price_psm=r.get("unadjusted_price_psm"),
            unit_of_measurement=r.get("unit_of_measurement") or "NIA",
            has_summary=bool(r.get("has_summary")),
            lat=float(r["lat"]),
            lon=float(r["lon"]),
            description=r.get("description") or "",
        )
        for r in rows
    ]


all_rows = []

for sample in SAMPLES:
    print(f"Running {sample['label']}...")
    df = pd.read_sql(sample["sql"], engine)
    print(f"  Found {len(df)} rows")

    for _, row in df.iterrows():
        uarn = row["uarn"]
        postcode = row["postcode"]
        desc = row["primary_description_text"]
        voa_rv = row["rateable_value"]
        nia_sqm = row["nia_sqm"]

        try:
            lat, lon = get_coords(postcode)
            if lat is None or lon is None:
                all_rows.append({
                    "segment": sample["label"],
                    "uarn": uarn,
                    "postcode": postcode,
                    "primary_description_text": desc,
                    "voa_rv": voa_rv,
                    "model_rv": None,
                    "pct_diff": None,
                    "confidence": None,
                    "comparable_count": 0,
                    "signal": None,
                    "error": "No postcode coordinates found",
                })
                continue

            raw_comps = get_comparables(
                lat=lat,
                lon=lon,
                scat_codes=sample["scat_codes"],
                radius_m=2000,
                nia_sqm=nia_sqm,
                size_band_pct=50,
            )

            # get_comparables() returns list[dict]; run_csa() requires list[Comparable]
            comps = dicts_to_comparables(raw_comps)

            result = run_csa(
                comps=comps,
                lat=lat,
                lon=lon,
                business_type=sample["business_type"],
                nia_sqm=nia_sqm,
                voa_rv=voa_rv,
            )

            model_rv = result.get("estimated_rv")
            pct_diff = None
            if model_rv not in (None, 0) and voa_rv not in (None, 0):
                pct_diff = ((model_rv - voa_rv) / voa_rv) * 100

            all_rows.append({
                "segment": sample["label"],
                "uarn": uarn,
                "postcode": postcode,
                "primary_description_text": desc,
                "voa_rv": voa_rv,
                "model_rv": model_rv,
                "pct_diff": pct_diff,
                "confidence": result.get("confidence"),
                "comparable_count": result.get("comparable_count"),
                "signal": result.get("signal"),
                "error": None,
            })

        except Exception as e:
            all_rows.append({
                "segment": sample["label"],
                "uarn": uarn,
                "postcode": postcode,
                "primary_description_text": desc,
                "voa_rv": voa_rv,
                "model_rv": None,
                "pct_diff": None,
                "confidence": None,
                "comparable_count": 0,
                "signal": None,
                "error": str(e),
            })

out = pd.DataFrame(all_rows)
out.to_csv("quick_test_results.csv", index=False)

print()
print("Done. Saved: quick_test_results.csv")
print()
print(out[["segment", "postcode", "voa_rv", "model_rv", "pct_diff", "confidence", "comparable_count", "signal", "error"]].head(20).to_string(index=False))
