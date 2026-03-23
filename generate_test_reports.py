# generate_test_reports.py

import sys
import os
from pathlib import Path
sys.path.insert(0, os.path.dirname(__file__))

os.environ["REPORT_TEMPLATE_DIR"] = str(
    Path(__file__).resolve().parent / "src" / "templates" / "reports"
)

from api.reports.pdf_generator import (
    generate_simplified_report, 
    generate_evidence_pack
)

# ── Mock data ──────────────────────────────────────────
report_data = {
    # Core property
    "business_name": "The Crown Public House",
    "property_address": "12 High Street, Wimbledon, London",
    "postcode": "SW19 5DX",
    "business_type": "restaurant",
    "date_prepared": "20 March 2026",
    "uprn": "100021234567",
    "voa_description": "Restaurant and Premises",

    # Valuation
    "voa_rv": 48000,
    "modelled_rv_low": 36000,
    "modelled_rv_high": 41000,
    "modelled_rv": 38500,
    "annual_saving_low": 3500,
    "annual_saving_high": 5900,
    "rv_delta": 9500,
    "rv_delta_pct": 19.8,
    "case_strength": "High",
    "confidence": "High",
    "nia_sqm": 120,
    "final_tone_psm": 320.83,
    "tone_basis": "Weighted median of NIA comparables",
    "subtotal_pre": 38500,
    "allowances_summary": "None applied",
    "recommendation_text": (
        "The evidence supports a strong case for reduction. "
        "Proceed to Check and submit this pack as supporting "
        "evidence at Challenge stage if not resolved."
    ),

    # Layout
    "layout_adjustment_applied": True,
    "floor_config": "Ground + Lower Ground",
    "ground_floor_trading_sqm": 75,
    "ground_floor_storage_sqm": 15,
    "kitchen_on_ground": "Yes",
    "kitchen_area_sqm": 30,
    "upper_floor_use": None,
    "lower_ground_use": "Storage",
    "layout_summary": {
        "high_similarity_count": 3,
        "medium_similarity_count": 2,
        "low_similarity_count": 0,
        "material_weight_shift": True
    },

    # Comparables
    "comp_count": 5,
    "comparables": [
        {
            "address": "8 High Street, Wimbledon SW19 5DX",
            "uarn": "10012345001",
            "nia_sqm": 115,
            "rv": 44000,
            "rate_psm": 382.61,
            "floor_config": "Ground only",
            "layout_similarity_score": 0.82,
            "adjusted_weight": 0.31
        },
        {
            "address": "24 High Street, Wimbledon SW19 5DX",
            "uarn": "10012345002",
            "nia_sqm": 130,
            "rv": 49500,
            "rate_psm": 380.77,
            "floor_config": "Ground + Lower Ground",
            "layout_similarity_score": 0.76,
            "adjusted_weight": 0.28
        },
        {
            "address": "3 The Broadway, Wimbledon SW19 1NE",
            "uarn": "10012345003",
            "nia_sqm": 108,
            "rv": 39000,
            "rate_psm": 361.11,
            "floor_config": "Ground only",
            "layout_similarity_score": 0.55,
            "adjusted_weight": 0.21
        },
        {
            "address": "17 The Broadway, Wimbledon SW19 1NE",
            "uarn": "10012345004",
            "nia_sqm": 95,
            "rv": 34500,
            "rate_psm": 363.16,
            "floor_config": "Ground only",
            "layout_similarity_score": 0.41,
            "adjusted_weight": 0.12
        },
        {
            "address": "55 Wimbledon Hill Road SW19 7QW",
            "uarn": "10012345005",
            "nia_sqm": 140,
            "rv": 51000,
            "rate_psm": 364.29,
            "floor_config": "Ground + First",
            "layout_similarity_score": 0.38,
            "adjusted_weight": 0.08
        },
    ],

    # Zoning (restaurant uses this)
    "zoning_rows": [
        {
            "zone": "Zone A",
            "area_sqm": 40,
            "relativity": "1.00",
            "weight": "1.00",
            "tone_psm": 320.83,
            "value": 12833
        },
        {
            "zone": "Zone B",
            "area_sqm": 35,
            "relativity": "0.50",
            "weight": "0.50",
            "tone_psm": 160.42,
            "value": 5615
        },
        {
            "zone": "Remainder",
            "area_sqm": 45,
            "relativity": "0.25",
            "weight": "0.25",
            "tone_psm": 80.21,
            "value": 3609
        },
    ],
    "nursery_adjustments": [],
}

# ── Generate ───────────────────────────────────────────
print("Generating simplified report...")
simplified_bytes = generate_simplified_report(report_data)
with open("test_simplified_report.pdf", "wb") as f:
    f.write(simplified_bytes)
print(f"  ✓ Saved: test_simplified_report.pdf "
      f"({len(simplified_bytes):,} bytes)")

print("Generating evidence pack...")
evidence_bytes = generate_evidence_pack(report_data)
with open("test_evidence_pack.pdf", "wb") as f:
    f.write(evidence_bytes)
print(f"  ✓ Saved: test_evidence_pack.pdf "
      f"({len(evidence_bytes):,} bytes)")

print("\nDone. Open both PDFs to review layout.")
