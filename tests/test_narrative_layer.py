from __future__ import annotations

from api.reports.narrative import (
    build_narrative_signals,
    classify_case_strength,
    classify_mismatch_band,
    classify_structural_band,
    classify_tone_position,
    render_narrative_blocks,
)


def _base_case_result() -> dict:
    return {
        "business_type": "retail",
        "case_strength": "Strong",
        "voa_rv": 100000,
        "modelled_rv": 85000,
        "comp_count": 10,
        "property_address": "1 High Street",
        "postcode": "SW1A 1AA",
        "final_tone_psm": 250.0,
        "valuation_basis": "ITZA",
        "valuation_basis_sqm": 320.0,
        "nia_sqm": 400.0,
        "layout_adjustment_applied": True,
        "floor_config": "ground_lower_ground",
        "comparables": [
            {"address": "3 High Street", "postcode": "SW1A 1AA", "rate": 220},
            {"address": "5 High Street", "postcode": "SW1A 1AA", "rate": 240},
            {"address": "7 High Street", "postcode": "SW1A 1AA", "rate": 260},
            {"address": "2 Market Road", "postcode": "SW1A 1AA", "rate": 280},
            {"address": "4 Market Road", "postcode": "SW1A 1AA", "rate": 300},
            {"address": "6 Market Road", "postcode": "SW1A 1AA", "rate": 255},
            {"address": "8 Market Road", "postcode": "SW1A 1AA", "rate": 245},
            {"address": "10 Market Road", "postcode": "SW1A 1AA", "rate": 235},
            {"address": "12 Market Road", "postcode": "SW1A 1AA", "rate": 265},
            {"address": "14 Market Road", "postcode": "SW1A 1AA", "rate": 275},
        ],
        "voa_reconciliation": {
            "overall_status": "partial",
            "checks": {
                "total_area_alignment": {"percentage_difference": 6.0},
                "layout_categorisation_alignment": {"status": "no"},
            },
        },
    }


def test_tone_position_thresholds():
    assert classify_tone_position(4.99) == "broadly_in_line"
    assert classify_tone_position(5.00) == "above"
    assert classify_tone_position(12.50) == "materially_above"
    assert classify_tone_position(-5.00) == "below"
    assert classify_tone_position(-12.50) == "materially_below"


def test_mismatch_thresholds():
    assert classify_mismatch_band("exact", 0.0, 0.0) == "none"
    assert classify_mismatch_band("partial", 6.0, None) == "minor"
    assert classify_mismatch_band("partial", 10.0, None) == "moderate"
    assert classify_mismatch_band("partial", 20.0, None) == "material"


def test_structural_band_thresholds():
    assert classify_structural_band(False, "ground_only", "retail") == "not_applied"
    assert classify_structural_band(True, "ground_lower_ground", "retail") == "basement_relevant"
    assert classify_structural_band(True, "ground_only", "restaurant_cafe") == "restaurant_layout"
    assert classify_structural_band(True, "ground_only", "retail") == "standard_layout"


def test_case_strength_thresholds():
    assert classify_case_strength(15.0, "high", "moderate") == "strong"
    assert classify_case_strength(8.0, "low", "high") == "moderate"
    assert classify_case_strength(4.0, "high", "low") == "weak"


def test_render_contains_required_phrases():
    strong = build_narrative_signals(_base_case_result())
    strong_rendered = render_narrative_blocks(strong)
    assert "Strong" in strong_rendered["case_assessment"]
    assert "material difference" not in strong_rendered["evidence_interpretation"]
    assert "basement or lower-ground" in strong_rendered["evidence_interpretation"]

    weak_case = _base_case_result()
    weak_case["case_strength"] = "Weak"
    weak_case["voa_reconciliation"]["checks"]["total_area_alignment"]["percentage_difference"] = 20.0
    weak_signals = build_narrative_signals(weak_case)
    weak_rendered = render_narrative_blocks(weak_signals)

    assert "material difference" in weak_rendered["evidence_interpretation"]
    assert "reviewable, but not clear-cut" in weak_rendered["case_assessment"]
    assert "submit a Check" in weak_rendered["recommended_action"]
    assert "Moderate" in weak_rendered["case_position_label"]
