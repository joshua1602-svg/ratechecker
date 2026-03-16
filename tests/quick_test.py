"""
Quick end-to-end test against the live Supabase VOA dataset.

Samples 50 properties per segment, runs the full CSA pipeline, and writes
results to quick_test_results.csv for manual inspection.

Comparables are restricted to the same postcode sector as the subject
property (e.g. "SW20 8" for "SW20 8AA").  This keeps the comparison pool
tight to the same local market so tone-engine accuracy is meaningful.
A fallback to full postcode-outward (e.g. "SW20") is used when fewer
than MIN_COMPS are found in the sector alone.

Usage (from project root):
    python tests/quick_test.py
"""
from __future__ import annotations

import os

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine

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
                svh.total_area_or_units AS nia_sqm,
                pc.latitude,
                pc.longitude
            FROM voa_list_entries le
            JOIN voa_sv_header svh ON le.uarn = svh.uarn
            JOIN postcode_coords pc ON le.postcode = pc.postcode
            WHERE le.scat_code IN (249, 251)
            AND le.rateable_value > 0
            AND svh.total_area_or_units > 0
            AND svh.unit_of_measurement = 'NIA'
            LIMIT 50
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
                svh.total_area_or_units AS nia_sqm,
                pc.latitude,
                pc.longitude
            FROM voa_list_entries le
            JOIN voa_sv_header svh ON le.uarn = svh.uarn
            JOIN postcode_coords pc ON le.postcode = pc.postcode
            WHERE le.scat_code IN (409, 234)
            AND le.rateable_value > 0
            AND svh.total_area_or_units > 0
            AND svh.unit_of_measurement = 'NIA'
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
                svh.total_area_or_units AS nia_sqm,
                pc.latitude,
                pc.longitude
            FROM voa_list_entries le
            JOIN voa_sv_header svh ON le.uarn = svh.uarn
            JOIN postcode_coords pc ON le.postcode = pc.postcode
            WHERE le.scat_code = 85
            AND le.rateable_value > 0
            AND svh.total_area_or_units > 0
            AND svh.unit_of_measurement = 'NIA'
            LIMIT 50
        """,
        "business_type": "nursery",
        "scat_codes": [85],
    },
]



MIN_COMPS = 3  # fall back to outward code if sector yields fewer than this


def postcode_sector(postcode: str) -> str:
    """Return the postcode sector, e.g. 'SW20 8' from 'SW20 8AA'.

    UK inward codes are always 3 characters (digit + 2 letters), so stripping
    the last 2 characters gives <outward> + space + <sector digit>.
    """
    return postcode.strip()[:-2]


def postcode_outward(postcode: str) -> str:
    """Return the outward code, e.g. 'SW20' from 'SW20 8AA'."""
    return postcode.strip().split()[0]


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

    for i, (_, row) in enumerate(df.iterrows(), 1):
        uarn = row["uarn"]
        postcode = row["postcode"]
        desc = row["primary_description_text"]
        voa_rv = row["rateable_value"]
        nia_sqm = row["nia_sqm"]
        lat = float(row["latitude"])
        lon = float(row["longitude"])
        print(f"  [{i}/{len(df)}] {postcode} ...", end="\r", flush=True)

        try:
            sector = postcode_sector(postcode)
            raw_comps = get_comparables(
                lat=lat,
                lon=lon,
                scat_codes=sample["scat_codes"],
                radius_m=2000,
                nia_sqm=nia_sqm,
                size_band_pct=50,
                postcode_prefix=sector,
            )
            comp_source = f"sector:{sector}"

            # Fall back to outward code if the sector is too sparse
            if len(raw_comps) < MIN_COMPS:
                outward = postcode_outward(postcode)
                raw_comps = get_comparables(
                    lat=lat,
                    lon=lon,
                    scat_codes=sample["scat_codes"],
                    radius_m=2000,
                    nia_sqm=nia_sqm,
                    size_band_pct=50,
                    postcode_prefix=outward,
                )
                comp_source = f"outward:{outward}"

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

    print()  # newline after the \r progress indicator

out = pd.DataFrame(all_rows)
out.to_csv("quick_test_results.csv", index=False)

print()
print("Done. Saved: quick_test_results.csv")
print()
print(out[["segment", "postcode", "postcode_sector", "comp_source", "voa_rv", "model_rv", "pct_diff", "confidence", "comparable_count", "signal", "error"]].head(20).to_string(index=False))
