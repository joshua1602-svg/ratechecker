"""Tests for the layout overweighting layer."""
from __future__ import annotations

import pytest

from api.engine.layout_overweight import (
    LAYOUT_WEIGHT_FACTOR,
    LayoutFingerprint,
    LayoutInput,
    apply_layout_overweighting,
    classify_description,
    fingerprint_from_subject,
    fingerprint_from_sv_lines,
    layout_similarity_score,
)


# ---------------------------------------------------------------------------
# classify_description
# ---------------------------------------------------------------------------

class TestClassifyDescription:
    def test_trading_types(self):
        assert classify_description("Retail Zone A") == "TRADING"
        assert classify_description("Zone B") == "TRADING"
        assert classify_description("Shop") == "TRADING"
        assert classify_description("RESTAURANT") == "TRADING"
        assert classify_description("Dining Area") == "TRADING"
        assert classify_description("Salon") == "TRADING"

    def test_storage_types(self):
        assert classify_description("Store") == "STORAGE"
        assert classify_description("Storage Room") == "STORAGE"
        assert classify_description("Cellar") == "STORAGE"
        assert classify_description("Cold Room") == "STORAGE"

    def test_kitchen_types(self):
        assert classify_description("Kitchen") == "KITCHEN"
        assert classify_description("KITCHEN AREA") == "KITCHEN"
        assert classify_description("Prep Area") == "KITCHEN"

    def test_office_types(self):
        assert classify_description("Office") == "OFFICE"

    def test_ancillary_types(self):
        assert classify_description("WC") == "ANCILLARY"
        assert classify_description("Toilet") == "ANCILLARY"
        assert classify_description("Staff Room") == "ANCILLARY"

    def test_unknown_returns_other(self):
        assert classify_description("XYZZY") == "OTHER"
        assert classify_description("") == "OTHER"


# ---------------------------------------------------------------------------
# fingerprint_from_subject
# ---------------------------------------------------------------------------

class TestFingerprintFromSubject:
    def test_ground_only(self):
        layout = LayoutInput(
            floor_config="ground_only",
            ground_floor_trading_sqm=80,
            ground_floor_storage_sqm=20,
            total_nia_sqm=100,
        )
        fp = fingerprint_from_subject(layout)
        assert fp.storage_ratio == pytest.approx(0.2)
        assert fp.trading_ratio == pytest.approx(0.8)
        assert fp.has_lower_ground is False
        assert fp.has_upper_floor is False
        assert fp.kitchen_on_ground is False

    def test_ground_lower_ground_storage(self):
        layout = LayoutInput(
            floor_config="ground_lower_ground",
            ground_floor_trading_sqm=60,
            ground_floor_storage_sqm=10,
            lower_ground_use="storage",
            total_nia_sqm=100,
        )
        fp = fingerprint_from_subject(layout)
        assert fp.has_lower_ground is True
        assert fp.has_upper_floor is False
        # Remaining 30 sqm allocated to storage
        assert fp.storage_ratio == pytest.approx(0.4)
        assert fp.trading_ratio == pytest.approx(0.6)

    def test_restaurant_kitchen_on_ground(self):
        layout = LayoutInput(
            floor_config="ground_only",
            ground_floor_trading_sqm=70,
            ground_floor_storage_sqm=10,
            kitchen_on_ground="yes",
            total_nia_sqm=100,
        )
        fp = fingerprint_from_subject(layout)
        assert fp.kitchen_on_ground is True


# ---------------------------------------------------------------------------
# fingerprint_from_sv_lines
# ---------------------------------------------------------------------------

class TestFingerprintFromSvLines:
    def test_basic_lines(self):
        lines = [
            {"floor": "G", "description": "Retail Zone A", "area": 50},
            {"floor": "G", "description": "Zone B", "area": 30},
            {"floor": "G", "description": "Store", "area": 20},
        ]
        fp = fingerprint_from_sv_lines(lines, total_nia=100)
        assert fp.trading_ratio == pytest.approx(0.8)
        assert fp.storage_ratio == pytest.approx(0.2)
        assert fp.has_lower_ground is False
        assert fp.has_upper_floor is False

    def test_multi_floor(self):
        lines = [
            {"floor": "G", "description": "Shop", "area": 60},
            {"floor": "Basement", "description": "Storage", "area": 25},
            {"floor": "1st", "description": "Office", "area": 15},
        ]
        fp = fingerprint_from_sv_lines(lines, total_nia=100)
        assert fp.has_lower_ground is True
        assert fp.has_upper_floor is True
        assert fp.trading_ratio == pytest.approx(0.6)
        assert fp.storage_ratio == pytest.approx(0.25)

    def test_kitchen_on_ground(self):
        lines = [
            {"floor": "G", "description": "Restaurant", "area": 50},
            {"floor": "G", "description": "Kitchen", "area": 20},
            {"floor": "LG", "description": "Store", "area": 30},
        ]
        fp = fingerprint_from_sv_lines(lines, total_nia=100)
        assert fp.kitchen_on_ground is True
        assert fp.has_lower_ground is True

    def test_empty_lines(self):
        fp = fingerprint_from_sv_lines([], total_nia=100)
        assert fp.storage_ratio == 0.0
        assert fp.trading_ratio == 0.0


# ---------------------------------------------------------------------------
# layout_similarity_score
# ---------------------------------------------------------------------------

class TestLayoutSimilarityScore:
    def test_identical_fingerprints(self):
        fp = LayoutFingerprint(
            storage_ratio=0.2, trading_ratio=0.8,
            has_lower_ground=False, has_upper_floor=False,
        )
        score = layout_similarity_score(fp, fp)
        assert score == pytest.approx(1.0)

    def test_completely_different(self):
        subject = LayoutFingerprint(
            storage_ratio=0.0, trading_ratio=1.0,
            has_lower_ground=False, has_upper_floor=False,
        )
        comp = LayoutFingerprint(
            storage_ratio=0.8, trading_ratio=0.2,
            has_lower_ground=True, has_upper_floor=True,
        )
        score = layout_similarity_score(subject, comp)
        assert score == 0.0

    def test_partial_match(self):
        subject = LayoutFingerprint(
            storage_ratio=0.2, trading_ratio=0.8,
            has_lower_ground=True, has_upper_floor=False,
        )
        comp = LayoutFingerprint(
            storage_ratio=0.2, trading_ratio=0.6,  # within 0.30 but not 0.15
            has_lower_ground=True, has_upper_floor=False,
        )
        score = layout_similarity_score(subject, comp)
        # storage: 1.0*0.35, trading: 0.5*0.35, floor: 1.0*0.30
        expected = 1.0 * 0.35 + 0.5 * 0.35 + 1.0 * 0.30
        assert score == pytest.approx(expected)

    def test_restaurant_kitchen_bonus(self):
        subject = LayoutFingerprint(
            storage_ratio=0.2, trading_ratio=0.7,
            has_lower_ground=False, has_upper_floor=False,
            kitchen_on_ground=True,
        )
        comp = LayoutFingerprint(
            storage_ratio=0.2, trading_ratio=0.7,
            has_lower_ground=False, has_upper_floor=False,
            kitchen_on_ground=True,
        )
        score_no_bonus = layout_similarity_score(subject, comp, is_restaurant=False)
        score_with_bonus = layout_similarity_score(subject, comp, is_restaurant=True)
        # Perfect match = 1.0 raw, with 1.15 bonus capped at 1.0
        assert score_no_bonus == pytest.approx(1.0)
        assert score_with_bonus == pytest.approx(1.0)  # capped

    def test_restaurant_kitchen_mismatch_no_bonus(self):
        subject = LayoutFingerprint(
            storage_ratio=0.2, trading_ratio=0.8,
            has_lower_ground=False, has_upper_floor=False,
            kitchen_on_ground=True,
        )
        comp = LayoutFingerprint(
            storage_ratio=0.35, trading_ratio=0.6,  # within 0.15 / within 0.30
            has_lower_ground=False, has_upper_floor=False,
            kitchen_on_ground=False,  # mismatch
        )
        score = layout_similarity_score(subject, comp, is_restaurant=True)
        # No bonus applied because kitchen_on_ground doesn't match
        expected = 1.0 * 0.35 + 0.5 * 0.35 + 1.0 * 0.30
        assert score == pytest.approx(expected)


# ---------------------------------------------------------------------------
# apply_layout_overweighting
# ---------------------------------------------------------------------------

def _make_csa_result(comps: list[dict]) -> dict:
    """Helper to build a minimal CSA result dict."""
    return {
        "signal": "Medium",
        "comparable_count": len(comps),
        "tone_rate": 250.0,
        "estimated_rv": 25000,
        "_rated_comps": comps,
    }


class TestApplyLayoutOverweighting:
    def test_no_layout_input_passthrough(self):
        comps = [
            {"uarn": "1", "address": "A", "rv": 10000, "nia_sqm": 50, "rate": 200, "weight": 1.0, "distance_m": 100},
            {"uarn": "2", "address": "B", "rv": 12000, "nia_sqm": 60, "rate": 200, "weight": 0.8, "distance_m": 200},
        ]
        result = apply_layout_overweighting(
            _make_csa_result(comps), layout_input=None, sv_lines_by_uarn={},
        )
        assert result["layout_adjustment_applied"] is False
        assert len(result["comps"]) == 2
        assert result["comps"][0]["adjusted_weight"] == 1.0
        assert result["comps"][1]["adjusted_weight"] == 0.8

    def test_insufficient_sv_coverage_passthrough(self):
        comps = [
            {"uarn": "1", "address": "A", "rv": 10000, "nia_sqm": 50, "rate": 200, "weight": 1.0, "distance_m": 100},
            {"uarn": "2", "address": "B", "rv": 12000, "nia_sqm": 60, "rate": 200, "weight": 0.8, "distance_m": 200},
            {"uarn": "3", "address": "C", "rv": 11000, "nia_sqm": 55, "rate": 200, "weight": 0.9, "distance_m": 150},
        ]
        layout = LayoutInput(
            floor_config="ground_only",
            ground_floor_trading_sqm=40,
            ground_floor_storage_sqm=10,
            total_nia_sqm=50,
        )
        # Only 1 out of 3 UARNs has SV lines — 33% < 50% threshold
        sv_lines = {"1": [{"floor": "G", "description": "Shop", "area": 50}]}
        result = apply_layout_overweighting(
            _make_csa_result(comps), layout_input=layout, sv_lines_by_uarn=sv_lines,
        )
        assert result["layout_adjustment_applied"] is False

    def test_full_adjustment_applied(self):
        comps = [
            {"uarn": "1", "address": "A", "rv": 10000, "nia_sqm": 100, "rate": 200, "weight": 1.0, "distance_m": 100},
            {"uarn": "2", "address": "B", "rv": 12000, "nia_sqm": 100, "rate": 200, "weight": 1.0, "distance_m": 200},
        ]
        layout = LayoutInput(
            floor_config="ground_only",
            ground_floor_trading_sqm=80,
            ground_floor_storage_sqm=20,
            total_nia_sqm=100,
        )
        # Comp 1: similar layout; Comp 2: different layout
        sv_lines = {
            "1": [
                {"floor": "G", "description": "Shop", "area": 80},
                {"floor": "G", "description": "Store", "area": 20},
            ],
            "2": [
                {"floor": "G", "description": "Shop", "area": 30},
                {"floor": "G", "description": "Store", "area": 50},
                {"floor": "1st", "description": "Office", "area": 20},
            ],
        }
        result = apply_layout_overweighting(
            _make_csa_result(comps), layout_input=layout, sv_lines_by_uarn=sv_lines,
        )
        assert result["layout_adjustment_applied"] is True
        assert len(result["comps"]) == 2

        c1 = result["comps"][0]
        c2 = result["comps"][1]
        assert c1["original_weight"] == 1.0
        assert c2["original_weight"] == 1.0

        # Comp 1 should have higher layout similarity → higher adjusted weight
        assert c1["layout_similarity_score"] > c2["layout_similarity_score"]
        assert c1["adjusted_weight"] > c2["adjusted_weight"]

        # Weights should sum to original total (2.0)
        total = c1["adjusted_weight"] + c2["adjusted_weight"]
        assert total == pytest.approx(2.0, abs=0.001)

    def test_weight_normalisation_preserves_sum(self):
        comps = [
            {"uarn": str(i), "address": f"Addr {i}", "rv": 10000, "nia_sqm": 80,
             "rate": 200, "weight": 0.5 + i * 0.1, "distance_m": 100 + i * 50}
            for i in range(5)
        ]
        layout = LayoutInput(
            floor_config="ground_only",
            ground_floor_trading_sqm=60,
            ground_floor_storage_sqm=20,
            total_nia_sqm=80,
        )
        sv_lines = {
            str(i): [{"floor": "G", "description": "Shop", "area": 60 + i * 5}]
            for i in range(5)
        }
        result = apply_layout_overweighting(
            _make_csa_result(comps), layout_input=layout, sv_lines_by_uarn=sv_lines,
        )
        assert result["layout_adjustment_applied"] is True
        original_sum = sum(c["weight"] for c in comps)
        adjusted_sum = sum(c["adjusted_weight"] for c in result["comps"])
        assert adjusted_sum == pytest.approx(original_sum, abs=0.01)

    def test_layout_summary_counts(self):
        comps = [
            {"uarn": "1", "address": "A", "rv": 10000, "nia_sqm": 100, "rate": 200, "weight": 1.0, "distance_m": 100},
            {"uarn": "2", "address": "B", "rv": 12000, "nia_sqm": 100, "rate": 200, "weight": 1.0, "distance_m": 200},
        ]
        layout = LayoutInput(
            floor_config="ground_only",
            ground_floor_trading_sqm=80,
            ground_floor_storage_sqm=20,
            total_nia_sqm=100,
        )
        sv_lines = {
            "1": [
                {"floor": "G", "description": "Shop", "area": 80},
                {"floor": "G", "description": "Store", "area": 20},
            ],
            "2": [
                {"floor": "G", "description": "Shop", "area": 80},
                {"floor": "G", "description": "Store", "area": 20},
            ],
        }
        result = apply_layout_overweighting(
            _make_csa_result(comps), layout_input=layout, sv_lines_by_uarn=sv_lines,
        )
        summary = result["layout_summary"]
        assert summary is not None
        assert "subject_fingerprint" in summary
        total = (summary["high_similarity_count"]
                 + summary["moderate_similarity_count"]
                 + summary["low_similarity_count"])
        assert total == 2

    def test_layout_weight_factor_is_named_constant(self):
        assert LAYOUT_WEIGHT_FACTOR == 0.5
