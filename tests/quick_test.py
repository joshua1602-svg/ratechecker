"""
Quick end-to-end test against the live Supabase VOA dataset.

Samples 25 properties per segment, runs the full CSA pipeline, and writes
results to quick_test_results.csv for manual inspection.

Comparables are restricted to the same postcode sector as the subject
property (e.g. "SW20 8" for "SW20 8AA"). This keeps the comparison pool
tight to the same local market so tone-engine accuracy is meaningful.

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
                svh.nia_sqm
            FROM voa_list_entries le
            JOIN (
                SELECT DISTINCT ON (uarn)
                    uarn,
                    total_area_or_units AS nia_sqm
                FROM voa_sv_header
                WHERE unit_of_measurement = 'NIA'
                  AND total_area_or_units > 0
                ORDER BY uarn
            ) svh ON le.uarn = svh.uarn
            WHERE le.scat_code IN (249, 251)
              AND le.rateable_value > 0
            LIMIT 25
        """,
        "business_type": "retail",
        "scat_codes": [249, 251],
    },
    {
        "label": "restaurant_cafe",
        "sql": """
            SELECT
                le.uarn,
                le.postcode,
                le.primary_description_text,
                le.rateable_value,
                svh.nia_sqm
            FROM voa_list_entries le
            JOIN (
                SELECT DISTINCT ON (uarn)
                    uarn,
                    total_area_or_units AS nia_sqm
                FROM voa_sv_header
                WHERE unit_of_measurement = 'NIA'
                  AND total_area_or_units > 0
                ORDER BY uarn
            ) svh ON le.uarn = svh.uarn
            WHERE le.scat_code IN (409, 234)
              AND le.rateable_value > 0
            LIMIT 25
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
                svh.nia_sqm
            FROM voa_list_entries le
            JOIN (
                SELECT DISTINCT ON (uarn)
                    uarn,
                    total_area_or_units AS nia_sqm
                FROM voa_sv_header
                WHERE unit_of_measurement = 'NIA'
                  AND total_area_or_units > 0
                ORDER BY uarn
            ) svh ON le.uarn = svh.uarn
            WHERE le.scat_code = 85
              AND le.rateable_value > 0
            LIMIT 25
        """,
        "business_type": "nursery",
        "scat_codes": [85],
    },
]


def postcode_sector(postcode: str) -> str:
    """Return the postcode sector, e.g. 'SW20 8' from 'SW20 8AA'."""
    return postcode.strip()[:-2]


def get_coords_batch(postcodes: list[str]) -> dict[str, tuple[float, float]]:
    """
    Batch fetch coordinates for a set of postcodes.

    Returns a dict keyed by normalised postcode (spaces removed, uppercased):
        {"SW208AA": (lat, lon), ...}
    """
    if not postcodes:
        return {}

    normalised = sorted({p.replace(" ", "").upper() for p in postcodes if p})
    sql = text("""
        SELECT postcode, latitude, longitude
        FROM postcode_coords
        WHERE REPLACE(UPPER(postcode), ' ', '') = ANY(:postcodes)
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {"postcodes": normalised}).fetchall()

    return {
        row.postcode.replace(" ", "").upper(): (float(row.latitude), float(row.longitude))
        for row in rows
    }


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

    coords_map = get_coords_batch(df["postcode"].dropna().astype(str).tolist())

    for i, (_, row) in enumerate(df.iterrows(), 1):
        uarn = row["uarn"]
        postcode = row["postcode"]
        desc = row["primary_description_text"]
        voa_rv = row["rateable_value"]
        nia_sqm = row["nia_sqm"]

        postcode_key = str(postcode).replace(" ", "").upper()
        coords = coords_map.get(postcode_key)
        if coords is None:
            all_rows.append({
                "segment": sample["label"],
                "uarn": uarn,
                "postcode": postcode,
                "postcode_sector": postcode_sector(postcode),
                "comp_source": None,
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

        lat, lon = coords
        print(f"  [{i}/{len(df)}] {postcode} ...", end="\r", flush=True)

        try:
            sector = postcode_sector(postcode)
            raw_comps = get_comparables(
                lat=lat,
                lon=lon,
                scat_codes=sample["scat_codes"],
                radius_m=1000,
                nia_sqm=nia_sqm,
                size_band_pct=50,
                postcode_prefix=sector,
            )
            comp_source = f"sector:{sector}"

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
                "postcode_sector": sector,
                "comp_source": comp_source,
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
                "postcode_sector": postcode_sector(postcode),
                "comp_source": None,
                "primary_description_text": desc,
                "voa_rv": voa_rv,
                "model_rv": None,
                "pct_diff": None,
                "confidence": None,
                "comparable_count": 0,
                "signal": None,
                "error": str(e),
            })

    print()

out = pd.DataFrame(all_rows)
out.to_csv("quick_test_results.csv", index=False)

print()
print("Done. Saved: quick_test_results.csv")
print()
print(out[["segment", "postcode", "postcode_sector", "comp_source", "voa_rv", "model_rv", "pct_diff", "confidence", "comparable_count", "signal", "error"]].head(20).to_string(index=False))
