"""
Quick end-to-end test against the live Supabase VOA dataset.

Samples 25 properties per segment, runs the full CSA pipeline, and writes
results to quick_test_results.csv for manual inspection.

Retail comparables are restricted to the same postcode sector as the
subject property (e.g. "SW20 8" for "SW20 8AA"). Restaurant/cafe and
nursery comparables deliberately use wider catchments without postcode-sector
anchoring to reflect production behaviour for those segments.

Usage (from project root):
    python tests/quick_test.py
"""
from __future__ import annotations

import os

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from api.db import get_comparables, get_sv_line_descs_batch
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

    # Batch-fetch VOA SV line descriptions for all subject UARNs.
    # These identify whether the subject was valued on a Zone A / ITZA basis
    # (e.g. "Retail Zone A" lines) or an area basis, enabling the correct
    # rate-basis classification in run_csa().
    subject_uarns = df["uarn"].astype(str).tolist()
    sv_lines_map = get_sv_line_descs_batch(subject_uarns)

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
            if sample["business_type"] == "nursery":
                _radius = 10_000
                _postcode_prefix = None
            elif sample["business_type"] == "restaurant_cafe":
                _radius = 1_500
                _postcode_prefix = None
            else:
                _radius = 1_000
                _postcode_prefix = sector
            raw_comps = get_comparables(
                lat=lat,
                lon=lon,
                scat_codes=sample["scat_codes"],
                radius_m=_radius,
                nia_sqm=nia_sqm,
                size_band_pct=50,
                postcode_prefix=_postcode_prefix,
            )
            if _postcode_prefix:
                comp_source = f"sector:{_postcode_prefix}"
            else:
                comp_source = f"radius:{_radius}m"

            comps = dicts_to_comparables(raw_comps)

            result = run_csa(
                comps=comps,
                lat=lat,
                lon=lon,
                business_type=sample["business_type"],
                nia_sqm=nia_sqm,
                voa_rv=voa_rv,
                subject_description=str(desc) if desc else "",
                subject_sv_line_descs=sv_lines_map.get(str(uarn), ()),
                subject_postcode_sector="" if _postcode_prefix is None else sector,
            )

            model_rv = result.get("estimated_rv")
            tone_rate = result.get("tone_rate")
            pct_diff = None
            if model_rv not in (None, 0) and voa_rv not in (None, 0):
                pct_diff = ((model_rv - voa_rv) / voa_rv) * 100

            # Diagnostic: subject implied VOA rate on the same basis the model used
            rn = result.get("rate_normalisation") or {}
            basis_label = rn.get("subject_basis_label")
            subject_basis = rn.get("subject_basis_sqm")
            voa_implied_rate = (
                voa_rv / subject_basis
                if subject_basis and subject_basis > 0
                else None
            )

            dbg = result.get("_debug") or {}
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
                "tone_rate": round(tone_rate, 2) if tone_rate else None,
                "voa_implied_rate": round(voa_implied_rate, 2) if voa_implied_rate else None,
                "subject_implied_zone_a": dbg.get("subject_implied_zone_a_rate"),
                "basis": basis_label,
                "subject_basis_sqm": subject_basis,
                "tier_psm": rn.get("tier_unadjusted_psm"),
                "tier_rv_nia": rn.get("tier_rv_over_nia"),
                "excluded": rn.get("excluded_no_rate"),
                "confidence": result.get("confidence"),
                "comparable_count": result.get("comparable_count"),
                "signal": result.get("signal"),
                # Pipeline stage counts
                "n_initial": dbg.get("n_initial_comps"),
                "n_after_size": dbg.get("n_after_size_and_launderette"),
                "n_after_dist": dbg.get("n_after_distance"),
                "n_after_outlier": dbg.get("n_after_outlier_removal"),
                # Nursery diagnostics
                "pre_cap_comparable_count": dbg.get("pre_cap_comparable_count"),
                "post_cap_comparable_count": dbg.get("post_cap_comparable_count"),
                "min_distance_used": dbg.get("min_distance_used"),
                "max_distance_used": dbg.get("max_distance_used"),
                # Location tier
                "location_tier": dbg.get("location_tier_used"),
                "same_street_count": dbg.get("same_street_count"),
                "same_postcode_sector_count": dbg.get("same_postcode_sector_count"),
                "same_street_key": dbg.get("same_street_key"),
                # Clustering
                "cluster_count": dbg.get("cluster_count"),
                "selected_cluster_id": dbg.get("selected_cluster_id"),
                "n_in_cluster": dbg.get("n_in_selected_cluster"),
                # Rate distribution of selected cluster
                "cluster_rate_min": dbg.get("cluster_rate_min"),
                "cluster_rate_median": dbg.get("cluster_rate_median"),
                "cluster_rate_max": dbg.get("cluster_rate_max"),
                # Rate-distance diagnostics
                "retail_method": dbg.get("retail_method"),
                "subject_implied_rate": dbg.get("subject_implied_zone_a_rate"),
                "rate_distance_to_subject": dbg.get("rate_distance_to_subject"),
                # Full-pool percentiles (pre-clustering, post-outlier)
                "pool_rate_p25": dbg.get("cluster_rate_p25"),
                "pool_rate_p75": dbg.get("cluster_rate_p75"),
                # Tier split medians
                "tier1_count": dbg.get("tier1_count"),
                "tier1_rate_median": dbg.get("tier1_rate_median"),
                "tier2_count": dbg.get("tier2_count"),
                "tier2_rate_median": dbg.get("tier2_rate_median"),
                # Comp-set diagnostics
                "num_comps_used": dbg.get("num_comps_used"),
                "median_distance": dbg.get("median_distance"),
                "size_ratio_median": dbg.get("size_ratio_median"),
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
                "tone_rate": None,
                "voa_implied_rate": None,
                "basis": None,
                "subject_basis_sqm": None,
                "tier_psm": None,
                "tier_rv_nia": None,
                "excluded": None,
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
print(out[["segment", "postcode", "voa_rv", "model_rv", "pct_diff",
           "tone_rate", "subject_implied_rate", "rate_distance_to_subject",
           "location_tier", "same_street_count", "same_postcode_sector_count",
           "cluster_count", "selected_cluster_id",
           "cluster_rate_min", "cluster_rate_median", "cluster_rate_max",
           "comparable_count", "confidence", "signal", "error"]
          ].head(30).to_string(index=False))

# ---------------------------------------------------------------------------
# Performance summary
# ---------------------------------------------------------------------------
# pct_diff = ((model_rv - voa_rv) / voa_rv) * 100
# Metrics are computed over rows where pct_diff is not null (successful valuations).
# ---------------------------------------------------------------------------

_SEG_LABELS = [
    ("retail",          "Retail"),
    ("restaurant_cafe", "Restaurant/Café"),
    ("nursery",         "Nursery"),
]

print()
print("=" * 66)
print("  PERFORMANCE SUMMARY")
print("=" * 66)

_all_completed = []

for seg_key, seg_label in _SEG_LABELS:
    seg_df = out[out["segment"] == seg_key]
    completed = seg_df[seg_df["pct_diff"].notna()]["pct_diff"]
    n_total = len(seg_df)
    n_completed = len(completed)
    n_missing = n_total - n_completed

    print(f"\n  {seg_label:<20}  n={n_total}  completed={n_completed}  "
          f"insufficient/error={n_missing}")
    if n_completed > 0:
        abs_pct = completed.abs()
        print(f"    median pct_diff      : {completed.median():+.1f}%   (+ = over-assessed)")
        print(f"    median |pct_diff|    : {abs_pct.median():.1f}%")
        print(f"    p75   |pct_diff|    : {abs_pct.quantile(0.75):.1f}%")
        print(f"    p90   |pct_diff|    : {abs_pct.quantile(0.90):.1f}%")
        _all_completed.append(completed)
    else:
        print("    (no completed valuations)")

print()
print(f"  {'ALL SEGMENTS':<20}  n={len(out)}  "
      f"completed={sum(len(c) for c in _all_completed)}  "
      f"insufficient/error={len(out) - sum(len(c) for c in _all_completed)}")
if _all_completed:
    _all = pd.concat(_all_completed)
    _all_abs = _all.abs()
    print(f"    median pct_diff      : {_all.median():+.1f}%")
    print(f"    median |pct_diff|    : {_all_abs.median():.1f}%")
    print(f"    p75   |pct_diff|    : {_all_abs.quantile(0.75):.1f}%")
    print(f"    p90   |pct_diff|    : {_all_abs.quantile(0.90):.1f}%")
else:
    print("    (no completed valuations)")

print()
print("=" * 66)
