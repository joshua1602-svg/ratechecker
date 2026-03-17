"""
Unit tests for the valuation engine.

Covers:
  - Adjustment arithmetic (multiplicative chaining)
  - Trigger evaluator (all six operators, edge cases)
  - Geometry flag (assumed vs observed)
  - ITZA helpers
"""
from __future__ import annotations

import math
import pytest

from api.engine.csa import itza_from_geometry, itza_from_nia
from api.engine.valuation import (
    _apply_op,
    _eval_nursery_trigger,
    _eval_trigger,
    apply_adjustments,
    calculate_rv,
)
from api.models import AreasInput, BusinessType, FlagsInput, NurseryInput, PropertyInput


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _prop(
    nia_sqm: float = 100.0,
    business_type: str = "retail",
    frontage_m: float | None = None,
    depth_m: float | None = None,
) -> PropertyInput:
    return PropertyInput(
        postcode="SW1A 1AA",
        business_type=BusinessType(business_type),
        nia_sqm=nia_sqm,
        frontage_m=frontage_m,
        depth_m=depth_m,
    )


def _flags(**kwargs) -> FlagsInput:
    return FlagsInput(consent_disclaimer=True, **kwargs)


# ---------------------------------------------------------------------------
# Adjustment arithmetic — multiplicative chaining
# ---------------------------------------------------------------------------

class TestAdjustmentArithmetic:
    """
    Confirm adjustments are chained multiplicatively, not summed additively.

    Example: -5% + -8% applied to £10,000
      Additive (wrong):      10,000 × (1 - 0.05 - 0.08)  = £8,700
      Multiplicative (right): 10,000 × 0.95 × 0.92        = £8,740
    """

    def test_single_adjustment_unchanged(self):
        """A single adjustment should give value × (1 + adj)."""
        prop = _prop(nia_sqm=100.0, business_type="retail")
        # Force poor_frontage trigger: frontage < 3.0
        prop2 = PropertyInput(
            postcode="EC1A 1BB",
            business_type=BusinessType.retail,
            nia_sqm=100.0,
            frontage_m=2.0,  # triggers poor_frontage (-5%)
        )
        result = calculate_rv(prop2, tone_rate=200.0)
        base_rv = result["base_rv"]
        # With one adjustment of -5%, multiplier = 0.95
        assert abs(result["adjustment_multiplier"] - 0.95) < 0.001

    def test_two_adjustments_are_multiplicative(self):
        """Two adjustments must be chained: (1+a1) × (1+a2), not (1+a1+a2)."""
        prop = PropertyInput(
            postcode="EC1A 1BB",
            business_type=BusinessType.retail,
            nia_sqm=100.0,
            frontage_m=2.0,   # poor_frontage: -5%
            depth_m=25.0,     # excessive_depth: -5%  (depth > 20m)
        )
        result = calculate_rv(prop, tone_rate=200.0)
        # Multiplicative: 0.95 × 0.95 = 0.9025
        # Additive (wrong): 1 - 0.05 - 0.05 = 0.90
        expected_multiplicative = 0.95 * 0.95
        assert abs(result["adjustment_multiplier"] - expected_multiplicative) < 0.001

    def test_no_adjustments_gives_multiplier_one(self):
        """Properties with no triggered allowances should have multiplier = 1.0."""
        prop = _prop(nia_sqm=100.0, business_type="retail")
        result = calculate_rv(prop, tone_rate=200.0)
        assert result["adjustment_multiplier"] == 1.0
        assert result["adjustments"] == []

    def test_positive_adjustment(self):
        """outdoor_seating (+5%) on a restaurant should raise the multiplier."""
        prop = PropertyInput(
            postcode="EC1A 1BB",
            business_type=BusinessType.restaurant_cafe,
            nia_sqm=80.0,
        )
        areas = AreasInput(outdoor_seating=True)
        result = calculate_rv(prop, tone_rate=300.0, areas=areas)
        assert abs(result["adjustment_multiplier"] - 1.05) < 0.001

    def test_rv_rounds_to_nearest_100(self):
        """Final RV must be rounded to the nearest £100."""
        prop = _prop(nia_sqm=100.0)
        result = calculate_rv(prop, tone_rate=137.0)
        assert result["rv"] % 100 == 0


# ---------------------------------------------------------------------------
# Trigger evaluator
# ---------------------------------------------------------------------------

class TestTriggerEvaluator:

    def test_less_than_fires_when_true(self):
        prop = PropertyInput(
            postcode="X", business_type=BusinessType.retail, nia_sqm=50, frontage_m=2.5
        )
        assert _eval_trigger("frontage_m < 3.0", prop, None, _flags()) is True

    def test_less_than_does_not_fire_when_false(self):
        prop = PropertyInput(
            postcode="X", business_type=BusinessType.retail, nia_sqm=50, frontage_m=4.0
        )
        assert _eval_trigger("frontage_m < 3.0", prop, None, _flags()) is False

    def test_greater_than_fires(self):
        prop = PropertyInput(
            postcode="X", business_type=BusinessType.retail, nia_sqm=50, depth_m=22.0
        )
        assert _eval_trigger("depth_m > 20", prop, None, _flags()) is True

    def test_greater_than_does_not_fire_at_boundary(self):
        prop = PropertyInput(
            postcode="X", business_type=BusinessType.retail, nia_sqm=50, depth_m=20.0
        )
        assert _eval_trigger("depth_m > 20", prop, None, _flags()) is False

    def test_bool_flag_true(self):
        assert _eval_trigger("layout_flag == true", _prop(), None, _flags(layout_flag=True)) is True

    def test_bool_flag_false(self):
        assert _eval_trigger("layout_flag == true", _prop(), None, _flags(layout_flag=False)) is False

    def test_gte_operator(self):
        assert _eval_trigger("fitout_year >= 2020", _prop(), None, _flags(fitout_year=2020)) is True
        assert _eval_trigger("fitout_year >= 2020", _prop(), None, _flags(fitout_year=2019)) is False

    def test_none_field_never_fires(self):
        """A property with no frontage supplied must not fire the frontage trigger."""
        prop = _prop(frontage_m=None)
        assert _eval_trigger("frontage_m < 3.0", prop, None, _flags()) is False

    def test_unrecognised_trigger_returns_false(self):
        assert _eval_trigger("unknown_field == true", _prop(), None, _flags()) is False

    def test_outdoor_seating_trigger(self):
        areas_yes = AreasInput(outdoor_seating=True)
        areas_no = AreasInput(outdoor_seating=False)
        prop = PropertyInput(
            postcode="X", business_type=BusinessType.restaurant_cafe, nia_sqm=60
        )
        assert _eval_trigger("outdoor_seating == true", prop, areas_yes, _flags()) is True
        assert _eval_trigger("outdoor_seating == true", prop, areas_no, _flags()) is False

    def test_nursery_purpose_built_true(self):
        nursery = NurseryInput(purpose_built=True)
        assert _eval_nursery_trigger("purpose_built == true", nursery, _flags()) is True
        assert _eval_nursery_trigger("purpose_built == false", nursery, _flags()) is False

    def test_nursery_purpose_built_false(self):
        nursery = NurseryInput(purpose_built=False)
        assert _eval_nursery_trigger("purpose_built == false", nursery, _flags()) is True
        assert _eval_nursery_trigger("purpose_built == true", nursery, _flags()) is False


# ---------------------------------------------------------------------------
# Geometry flag
# ---------------------------------------------------------------------------

class TestGeometryFlag:

    def test_geometry_assumed_when_no_dimensions(self):
        result = calculate_rv(_prop(nia_sqm=100.0), tone_rate=200.0)
        assert result["geometry_assumed"] is True

    def test_geometry_not_assumed_when_dimensions_provided(self):
        prop = PropertyInput(
            postcode="X",
            business_type=BusinessType.retail,
            nia_sqm=100.0,
            frontage_m=5.0,
            depth_m=20.0,
        )
        result = calculate_rv(prop, tone_rate=200.0)
        assert result["geometry_assumed"] is False

    def test_itza_differs_with_actual_vs_assumed_geometry(self):
        """Narrow-deep shop and wide-shallow shop with same NIA have different ITZA."""
        nia = 100.0
        zone = 6.1
        # Very narrow, deep shop: ITZA heavily penalised by halving
        itza_narrow = itza_from_geometry(2.0, 50.0, zone)
        # Standard 1:3 assumption
        itza_assumed = itza_from_nia(nia, zone)
        # A 2×50 shop has a very different ITZA than the assumed geometry
        assert abs(itza_narrow - itza_assumed) > 1.0

    def test_nursery_geometry_assumed_always_false(self):
        prop = PropertyInput(
            postcode="X", business_type=BusinessType.nursery, nia_sqm=200.0
        )
        result = calculate_rv(prop, tone_rate=120.0, nursery=NurseryInput())
        assert result["geometry_assumed"] is False


# ---------------------------------------------------------------------------
# ITZA helpers
# ---------------------------------------------------------------------------

class TestITZA:

    def test_itza_from_nia_zero(self):
        assert itza_from_nia(0) == 0.0

    def test_itza_from_nia_small_shop_less_than_one_zone(self):
        """A shop with depth < 6.1m falls entirely in Zone A (relativity 1.0)."""
        # 5m wide × 4m deep = 20m², depth < zone_depth → ITZA ≈ NIA
        result = itza_from_geometry(5.0, 4.0, zone_depth_m=6.1)
        assert abs(result - 20.0) < 0.01

    def test_itza_from_geometry_two_zones(self):
        """A 5m × 15m shop: Zone A = 5×6.1 = 30.5, Zone B = 5×6.1×0.5 = 15.25, remainder tiny."""
        result = itza_from_geometry(5.0, 15.0, zone_depth_m=6.1)
        expected = 5.0 * 6.1 * 1.0 + 5.0 * 6.1 * 0.5 + 5.0 * (15.0 - 12.2) * 0.25
        assert abs(result - expected) < 0.01

    def test_itza_from_nia_consistent_with_geometry(self):
        """itza_from_nia delegates to itza_from_geometry internally."""
        nia = 100.0
        zone = 6.1
        aspect = 3.0
        width = math.sqrt(nia / aspect)
        depth = nia / width
        assert abs(itza_from_nia(nia, zone) - itza_from_geometry(width, depth, zone)) < 0.001


# ---------------------------------------------------------------------------
# apply_adjustments() — post-CSA adjustment layer
# ---------------------------------------------------------------------------

class TestApplyAdjustments:
    """
    Verify the adjustment layer (apply_adjustments) for each segment.

    Covers:
      - base_rv is passed through unchanged
      - adjusted_rv = round(base_rv * factor / 100) * 100
      - triggered rules reduce/increase the factor
      - missing optional fields skip (not fail) their triggers
      - segment-specific rules fire correctly (retail, restaurant_cafe, nursery)
      - source field names the correct YAML file
    """

    # --- Retail ---------------------------------------------------------------

    def test_retail_no_triggers_returns_base_unchanged(self):
        prop = PropertyInput(
            postcode="X", business_type=BusinessType.retail, nia_sqm=100.0
        )
        result = apply_adjustments("retail", 20_000, prop)
        assert result["base_estimated_rv"] == 20_000
        assert result["adjusted_estimated_rv"] == 20_000
        assert result["adjustments"]["total_adjustment_factor"] == 1.0

    def test_retail_poor_frontage_reduces_rv(self):
        prop = PropertyInput(
            postcode="X", business_type=BusinessType.retail, nia_sqm=100.0,
            frontage_m=2.5,  # triggers poor_frontage (<3.0): -5%
        )
        result = apply_adjustments("retail", 20_000, prop)
        assert abs(result["adjustments"]["total_adjustment_factor"] - 0.95) < 0.001
        assert result["adjusted_estimated_rv"] == round(20_000 * 0.95 / 100) * 100

    def test_retail_awkward_layout_reduces_rv(self):
        prop = PropertyInput(postcode="X", business_type=BusinessType.retail, nia_sqm=100.0)
        flags = FlagsInput(consent_disclaimer=True, layout_flag=True)  # -8%
        result = apply_adjustments("retail", 20_000, prop, flags=flags)
        assert abs(result["adjustments"]["total_adjustment_factor"] - 0.92) < 0.001

    def test_retail_excessive_depth_reduces_rv(self):
        prop = PropertyInput(
            postcode="X", business_type=BusinessType.retail, nia_sqm=100.0,
            depth_m=25.0,  # triggers excessive_depth (>20m): -5%
        )
        result = apply_adjustments("retail", 20_000, prop)
        assert abs(result["adjustments"]["total_adjustment_factor"] - 0.95) < 0.001

    def test_retail_two_triggers_are_multiplicative(self):
        """poor_frontage (-5%) AND awkward_layout (-8%) → 0.95 × 0.92 = 0.874"""
        prop = PropertyInput(
            postcode="X", business_type=BusinessType.retail, nia_sqm=100.0,
            frontage_m=2.0,  # poor_frontage
        )
        flags = FlagsInput(consent_disclaimer=True, layout_flag=True)  # awkward_layout
        result = apply_adjustments("retail", 20_000, prop, flags=flags)
        expected_factor = 0.95 * 0.92
        assert abs(result["adjustments"]["total_adjustment_factor"] - expected_factor) < 0.001
        assert result["adjusted_estimated_rv"] == round(20_000 * expected_factor / 100) * 100

    def test_retail_source_names_retail_yaml(self):
        prop = PropertyInput(postcode="X", business_type=BusinessType.retail, nia_sqm=100.0)
        result = apply_adjustments("retail", 20_000, prop)
        sources = {item["source"] for item in result["adjustments"]["applied"]}
        assert sources == {"retail.yaml"}

    def test_retail_missing_frontage_skips_trigger(self):
        """No frontage_m supplied → poor_frontage must not fire."""
        prop = PropertyInput(postcode="X", business_type=BusinessType.retail, nia_sqm=100.0)
        result = apply_adjustments("retail", 20_000, prop)
        triggered = [i for i in result["adjustments"]["applied"] if i["triggered"]]
        assert not any(i["name"] == "poor_frontage" for i in triggered)

    def test_retail_all_rules_listed_including_not_triggered(self):
        """Every rule in retail.yaml must appear in the applied list."""
        prop = PropertyInput(postcode="X", business_type=BusinessType.retail, nia_sqm=100.0)
        result = apply_adjustments("retail", 20_000, prop)
        names = {i["name"] for i in result["adjustments"]["applied"]}
        assert "poor_frontage" in names
        assert "awkward_layout" in names
        assert "excessive_depth" in names

    # --- hair_beauty (shares retail.yaml) ------------------------------------

    def test_hair_beauty_uses_retail_yaml(self):
        prop = PropertyInput(postcode="X", business_type=BusinessType.hair_beauty, nia_sqm=60.0)
        result = apply_adjustments("hair_beauty", 10_000, prop)
        sources = {item["source"] for item in result["adjustments"]["applied"]}
        assert sources == {"retail.yaml"}

    # --- Restaurant/Café -----------------------------------------------------

    def test_restaurant_outdoor_seating_increases_rv(self):
        prop = PropertyInput(postcode="X", business_type=BusinessType.restaurant_cafe, nia_sqm=80.0)
        areas = AreasInput(outdoor_seating=True)  # +5%
        result = apply_adjustments("restaurant_cafe", 30_000, prop, areas=areas)
        assert abs(result["adjustments"]["total_adjustment_factor"] - 1.05) < 0.001

    def test_restaurant_cramped_layout_reduces_rv(self):
        prop = PropertyInput(postcode="X", business_type=BusinessType.restaurant_cafe, nia_sqm=80.0)
        flags = FlagsInput(consent_disclaimer=True, cramped_flag=True)  # -5%
        result = apply_adjustments("restaurant_cafe", 30_000, prop, flags=flags)
        assert abs(result["adjustments"]["total_adjustment_factor"] - 0.95) < 0.001

    def test_restaurant_premium_fitout_increases_rv(self):
        prop = PropertyInput(postcode="X", business_type=BusinessType.restaurant_cafe, nia_sqm=80.0)
        flags = FlagsInput(consent_disclaimer=True, fitout_year=2022)  # ≥2020: +2%
        result = apply_adjustments("restaurant_cafe", 30_000, prop, flags=flags)
        assert abs(result["adjustments"]["total_adjustment_factor"] - 1.02) < 0.001

    def test_restaurant_old_fitout_does_not_trigger(self):
        prop = PropertyInput(postcode="X", business_type=BusinessType.restaurant_cafe, nia_sqm=80.0)
        flags = FlagsInput(consent_disclaimer=True, fitout_year=2018)
        result = apply_adjustments("restaurant_cafe", 30_000, prop, flags=flags)
        triggered = [i for i in result["adjustments"]["applied"] if i["triggered"]]
        assert not any(i["name"] == "premium_fitout" for i in triggered)

    def test_restaurant_no_areas_skips_outdoor_seating(self):
        """No areas object → outdoor_seating trigger must not fire."""
        prop = PropertyInput(postcode="X", business_type=BusinessType.restaurant_cafe, nia_sqm=80.0)
        result = apply_adjustments("restaurant_cafe", 30_000, prop)
        triggered = [i for i in result["adjustments"]["applied"] if i["triggered"]]
        assert not any(i["name"] == "outdoor_seating" for i in triggered)

    def test_restaurant_source_names_restaurant_yaml(self):
        prop = PropertyInput(postcode="X", business_type=BusinessType.restaurant_cafe, nia_sqm=80.0)
        result = apply_adjustments("restaurant_cafe", 30_000, prop)
        sources = {item["source"] for item in result["adjustments"]["applied"]}
        assert sources == {"restaurant_cafe.yaml"}

    # --- Nursery -------------------------------------------------------------

    def test_nursery_purpose_built_increases_rv(self):
        prop = PropertyInput(postcode="X", business_type=BusinessType.nursery, nia_sqm=200.0)
        nursery = NurseryInput(purpose_built=True)  # +3%
        result = apply_adjustments("nursery", 40_000, prop, nursery=nursery)
        assert abs(result["adjustments"]["total_adjustment_factor"] - 1.03) < 0.001

    def test_nursery_converted_building_reduces_rv(self):
        prop = PropertyInput(postcode="X", business_type=BusinessType.nursery, nia_sqm=200.0)
        nursery = NurseryInput(purpose_built=False)  # converted_building: -5%
        result = apply_adjustments("nursery", 40_000, prop, nursery=nursery)
        triggered = [i for i in result["adjustments"]["applied"] if i["triggered"]]
        assert any(i["name"] == "converted_building" for i in triggered)
        # purpose_built=True triggers +3%, purpose_built=False triggers converted_building -5%
        # Only converted_building should be triggered (purpose_built trigger won't fire)
        assert not any(i["name"] == "purpose_built" for i in triggered)

    def test_nursery_outdoor_play_increases_rv(self):
        prop = PropertyInput(postcode="X", business_type=BusinessType.nursery, nia_sqm=200.0)
        nursery = NurseryInput(outdoor_play=True)  # +3%
        result = apply_adjustments("nursery", 40_000, prop, nursery=nursery)
        triggered = [i for i in result["adjustments"]["applied"] if i["triggered"]]
        assert any(i["name"] == "outdoor_play_area" for i in triggered)

    def test_nursery_awkward_layout_reduces_rv(self):
        prop = PropertyInput(postcode="X", business_type=BusinessType.nursery, nia_sqm=200.0)
        # purpose_built=True avoids the converted_building (-5%) trigger so only
        # awkward_layout (-5%) fires; factor = 1.03 * 0.95 = 0.9785
        nursery = NurseryInput(purpose_built=True)
        flags = FlagsInput(consent_disclaimer=True, layout_flag=True)
        result = apply_adjustments("nursery", 40_000, prop, nursery=nursery, flags=flags)
        triggered = [i for i in result["adjustments"]["applied"] if i["triggered"]]
        assert any(i["name"] == "awkward_layout" for i in triggered)
        # purpose_built (+3%) × awkward_layout (-5%) = 1.03 × 0.95 = 0.9785
        expected = 1.03 * 0.95
        assert abs(result["adjustments"]["total_adjustment_factor"] - expected) < 0.001

    def test_nursery_source_names_nursery_yaml(self):
        prop = PropertyInput(postcode="X", business_type=BusinessType.nursery, nia_sqm=200.0)
        result = apply_adjustments("nursery", 40_000, prop)
        sources = {item["source"] for item in result["adjustments"]["applied"]}
        assert sources == {"nursery.yaml"}

    def test_nursery_purpose_built_and_outdoor_play_compound(self):
        """purpose_built (+3%) × outdoor_play (+3%) = 1.03 × 1.03 = 1.0609"""
        prop = PropertyInput(postcode="X", business_type=BusinessType.nursery, nia_sqm=200.0)
        nursery = NurseryInput(purpose_built=True, outdoor_play=True)
        result = apply_adjustments("nursery", 40_000, prop, nursery=nursery)
        expected_factor = 1.03 * 1.03
        assert abs(result["adjustments"]["total_adjustment_factor"] - expected_factor) < 0.001

    # --- Output contract -----------------------------------------------------

    def test_adjusted_rv_rounds_to_nearest_100(self):
        """Adjusted RV must always be a multiple of £100."""
        prop = PropertyInput(
            postcode="X", business_type=BusinessType.retail, nia_sqm=100.0,
            frontage_m=2.0,
        )
        result = apply_adjustments("retail", 19_750, prop)
        assert result["adjusted_estimated_rv"] % 100 == 0

    def test_adjustment_summary_present(self):
        prop = PropertyInput(postcode="X", business_type=BusinessType.retail, nia_sqm=100.0)
        result = apply_adjustments("retail", 20_000, prop)
        assert "adjustment_summary" in result
        assert isinstance(result["adjustment_summary"], str)
        assert len(result["adjustment_summary"]) > 0

    def test_adjustment_summary_no_triggers(self):
        prop = PropertyInput(postcode="X", business_type=BusinessType.retail, nia_sqm=100.0)
        result = apply_adjustments("retail", 20_000, prop)
        assert "No adjustments applied" in result["adjustment_summary"]

    def test_adjustment_factor_format(self):
        """Factor field on each item is a float (signed decimal)."""
        prop = PropertyInput(postcode="X", business_type=BusinessType.retail, nia_sqm=100.0)
        result = apply_adjustments("retail", 20_000, prop)
        for item in result["adjustments"]["applied"]:
            assert isinstance(item["factor"], float)
            # factors from retail.yaml are all negative or zero in this segment
            assert -1.0 < item["factor"] <= 0.0
