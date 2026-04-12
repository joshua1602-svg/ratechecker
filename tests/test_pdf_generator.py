"""Tests for api.reports.pdf_generator."""
from __future__ import annotations

import pytest

from api.models import (
    AssessRequest,
    AssessResponse,
    BusinessType,
    ContactInput,
    FlagsInput,
    PropertyInput,
    build_evidence_payload_from_assess,
)
from api.reports import pdf_generator
from api.reports.pdf_generator import generate_evidence_pack, generate_simplified_report


def _base_report_data() -> dict:
    """Full report_data dict covering all required and optional fields."""
    return {
        # --- simplified required ---
        "business_name": "Bean & Brew Ltd",
        "property_address": "12 High Street",
        "postcode": "SW1A 1AA",
        "business_type": "restaurant_cafe",
        "date_prepared": "2026-03-20",
        "voa_rv": 25000,
        "modelled_rv_low": 18000,
        "modelled_rv_high": 22000,
        "annual_saving_low": 1400,
        "annual_saving_high": 3300,
        "case_strength": "High",
        "comparables": [
            {"address": "10 High Street", "rv": 23000, "nia_sqm": 85},
            {"address": "14 High Street", "rv": 21000, "nia_sqm": 78},
            {"address": "5 Market Square", "rv": 26000, "nia_sqm": 95},
        ],
        "comp_count": 3,
        # --- evidence extra required ---
        "uprn": "100023456789",
        "voa_description": "Restaurant and premises",
        "nia_sqm": 90,
        "modelled_rv": 20000,
        "final_tone_psm": 222.22,
        "tone_basis": "median",
        "confidence": "high",
        "recommendation_text": "Strong case for appeal.",
        "valuation_method": "nia",
        "valuation_basis": "Comparable Tone (£/sqm NIA)",
        "zoning_rows": [
            {"zone": "A", "depth": 6.1, "area": 30.5, "rate": 250, "value": 7625},
            {"zone": "B", "depth": 6.1, "area": 30.5, "rate": 125, "value": 3812},
        ],
        "nursery_adjustments": None,
        "voa_reconciliation": {
            "overall_status": "partially",
            "summary": {"total_checks": 4, "yes": 2, "no": 1, "unknown": 1},
            "checks": {
                "gross_floor_space": {"status": "no", "detail_text": "55.0 sqm entered; VOA record shows 70.0 sqm."},
                "floor_plan_configuration": {"status": "yes", "detail_text": None},
                "floor_split": {"status": "unknown", "detail_text": None},
                "business_type": {"status": "yes", "detail_text": None},
            },
        },
        # --- optional layout fields ---
        "layout_adjustment_applied": True,
        "layout_summary": "Ground floor trading dominant",
        "floor_config": "ground_only",
        "ground_floor_trading_sqm": 60.0,
        "ground_floor_storage_sqm": 20.0,
        "kitchen_on_ground": "yes",
        "kitchen_area_sqm": 15.0,
        "upper_floor_use": "not_applicable",
        "lower_ground_use": "not_applicable",
        "subtotal_pre": 11437,
        "allowances_summary": "5% end-of-terrace allowance",
    }


# ── Simplified report ──────────────────────────────────────────────────


class TestSimplifiedReport:
    def test_returns_pdf_bytes(self):
        data = _base_report_data()
        result = generate_simplified_report(data)
        assert isinstance(result, bytes)
        assert result[:4] == b"%PDF"

    def test_missing_required_field_raises(self):
        data = _base_report_data()
        del data["business_name"]
        with pytest.raises(ValueError, match="business_name"):
            generate_simplified_report(data)

    def test_optional_field_none_no_error(self):
        data = _base_report_data()
        data["layout_adjustment_applied"] = None
        data["layout_summary"] = None
        data["floor_config"] = None
        result = generate_simplified_report(data)
        assert isinstance(result, bytes)
        assert result[:4] == b"%PDF"


# ── Evidence pack ──────────────────────────────────────────────────────


class TestEvidencePack:
    def test_returns_pdf_bytes(self):
        data = _base_report_data()
        result = generate_evidence_pack(data)
        assert isinstance(result, bytes)
        assert result[:4] == b"%PDF"

    def test_missing_required_field_raises(self):
        data = _base_report_data()
        del data["voa_description"]
        with pytest.raises(ValueError, match="voa_description"):
            generate_evidence_pack(data)

    def test_optional_field_none_no_error(self):
        data = _base_report_data()
        data["layout_adjustment_applied"] = None
        data["layout_summary"] = None
        data["subtotal_pre"] = None
        data["allowances_summary"] = None
        result = generate_evidence_pack(data)
        assert isinstance(result, bytes)
        assert result[:4] == b"%PDF"


# ── Retail vs nursery branching ────────────────────────────────────────


class TestBusinessTypeBranching:
    def test_retail_with_zoning_rows(self):
        data = _base_report_data()
        data["business_type"] = "retail"
        data["valuation_method"] = "itza"
        data["valuation_basis"] = "ITZA (Zoning)"
        data["zoning_rows"] = [
            {"zone": "A", "depth": 6.1, "area": 40, "rate": 300, "value": 12000},
        ]
        data["nursery_adjustments"] = None
        result = generate_evidence_pack(data)
        assert isinstance(result, bytes)
        assert result[:4] == b"%PDF"

    def test_nursery_with_adjustments(self):
        data = _base_report_data()
        data["business_type"] = "nursery"
        data["valuation_method"] = "nia"
        data["valuation_basis"] = "Comparable Tone (£/sqm NIA)"
        data["zoning_rows"] = []
        data["nursery_adjustments"] = [
            {"name": "Purpose-built", "factor": 1.05, "notes": "Modern facility"},
        ]
        result = generate_evidence_pack(data)
        assert isinstance(result, bytes)
        assert result[:4] == b"%PDF"

    def test_nursery_missing_adjustments_no_longer_required(self):
        data = _base_report_data()
        data["business_type"] = "nursery"
        data["valuation_method"] = "nia"
        data["valuation_basis"] = "Comparable Tone (£/sqm NIA)"
        data["zoning_rows"] = []
        data["nursery_adjustments"] = None
        result = generate_evidence_pack(data)
        assert isinstance(result, bytes)
        assert result[:4] == b"%PDF"

    def test_retail_missing_zoning_rows_no_longer_required(self):
        data = _base_report_data()
        data["business_type"] = "retail"
        data["valuation_method"] = "itza"
        data["valuation_basis"] = "ITZA (Zoning)"
        data["zoning_rows"] = None
        data["nursery_adjustments"] = None
        result = generate_evidence_pack(data)
        assert isinstance(result, bytes)
        assert result[:4] == b"%PDF"


# ── Layout adjustment flag ─────────────────────────────────────────────


class TestLayoutAdjustment:
    def test_layout_applied_false_no_error(self):
        data = _base_report_data()
        data["layout_adjustment_applied"] = False
        result = generate_evidence_pack(data)
        assert isinstance(result, bytes)
        assert result[:4] == b"%PDF"

    def test_layout_applied_true_no_error(self):
        data = _base_report_data()
        data["layout_adjustment_applied"] = True
        result = generate_evidence_pack(data)
        assert isinstance(result, bytes)
        assert result[:4] == b"%PDF"


class TestPdfTemplateRendering:
    def test_simplified_template_uses_css_page_counters_and_single_line_rv_range(self):
        data = pdf_generator._derive_fields(_base_report_data())
        html = pdf_generator._load_template(
            pdf_generator._get_env(),
            "simplified_report.html",
        ).render(**data)

        assert 'content: "Page " counter(page) " of " counter(pages);' in html
        assert "Annual Saving" in html
        assert "&pound;18,000&ndash;&pound;22,000" in html
        assert 'class="banner-value"' in html

    def test_evidence_pack_renders_title_case_sector_and_dynamic_comparable_reference(self):
        data = pdf_generator._derive_fields(_base_report_data())
        html = pdf_generator._load_template(
            pdf_generator._get_env(),
            "evidence_pack.html",
        ).render(**data)

        assert "Restaurant_cafe" in html
        assert "Comparable evidence is set out on page" not in html
        assert "222.22" in html
        assert "Submission-Ready Narrative" in html

    def test_evidence_pack_renders_tone_source_label_when_provided(self):
        data = pdf_generator._derive_fields(_base_report_data())
        data["tone_source_label"] = "Primary tone source: Same street evidence"
        html = pdf_generator._load_template(
            pdf_generator._get_env(),
            "evidence_pack.html",
        ).render(**data)
        assert "Tone Source" in html
        assert "Primary tone source: Same street evidence" in html


    def test_evidence_pack_renders_location_signal_indicators(self):
        data = pdf_generator._derive_fields(_base_report_data())
        data["crime_adjustment_indicator"] = "Moderate"
        data["flood_adjustment_indicator"] = "Active"
        html = pdf_generator._load_template(
            pdf_generator._get_env(),
            "evidence_pack.html",
        ).render(**data)
        assert "Directional Public Signals" in html
        assert "Moderate" in html
        assert "Active" in html

    def test_evidence_pack_does_not_render_floor_config_comps_column(self):
        data = pdf_generator._derive_fields(_base_report_data())
        html = pdf_generator._load_template(
            pdf_generator._get_env(),
            "evidence_pack.html",
        ).render(**data)
        assert ">Floor Config<" not in html

    def test_restaurant_nia_block_has_no_itza_language(self):
        data = pdf_generator._derive_fields(_base_report_data())
        html = pdf_generator._load_template(
            pdf_generator._get_env(),
            "evidence_pack.html",
        ).render(**data)
        assert "Comparable Tone (" in html
        assert "NIA)" in html
        assert "Subject Area (NIA)" in html
        assert "Zoning Schedule" not in html
        assert "Assumed 1:3 width-to-depth aspect ratio" not in html
        assert "Zone A" not in html

    def test_retail_itza_block_still_renders(self):
        data = _base_report_data()
        data["business_type"] = "retail"
        data["valuation_method"] = "itza"
        data["valuation_basis"] = "ITZA (Zoning)"
        data["zoning_rows"] = [{"zone": "Zone A", "floor": "Ground", "area_sqm": 40.0, "itza_weight": 1.0, "itza_contribution_sqm": 40.0, "displayed_tone": 300.0, "row_value": 12000.0}]
        data["geometry_assumed"] = True
        html = pdf_generator._load_template(
            pdf_generator._get_env(),
            "evidence_pack.html",
        ).render(**pdf_generator._derive_fields(data))
        assert "Zoning Schedule" in html
        assert "Subject Area (ITZA)" in html
        assert "Assumed 1:3 width-to-depth aspect ratio" in html

    def test_retail_itza_uses_voa_geometry_wording_and_lines(self):
        data = _base_report_data()
        data["business_type"] = "retail"
        data["valuation_method"] = "itza"
        data["valuation_basis"] = "ITZA (Zoning)"
        data["geometry_assumed"] = False
        data["geometry_source_indicator"] = "VOA structured valuation record"
        data["zoning_rows"] = [
            {
                "zone": "Zone A",
                "floor": "Ground",
                "area_sqm": 40.0,
                "itza_weight": 1.0,
                "itza_contribution_sqm": 40.0,
                "displayed_tone": 300.0,
                "row_value": 12000.0,
            }
        ]
        html = pdf_generator._load_template(
            pdf_generator._get_env(),
            "evidence_pack.html",
        ).render(**pdf_generator._derive_fields(data))
        assert "Geometry Source" in html
        assert "VOA structured valuation record" in html
        assert "Assumed 1:3 width-to-depth aspect ratio" not in html
        assert ">Floor<" in html
        assert "ITZA Contribution (sqm)" in html
        assert "Zone A" in html

    @pytest.mark.parametrize("business_type", ["retail", "restaurant_cafe", "nursery"])
    def test_itza_schedule_columns_are_consistent_across_business_types(self, business_type):
        data = _base_report_data()
        data["business_type"] = business_type
        data["valuation_method"] = "itza"
        data["valuation_basis"] = "ITZA (Zoning)"
        data["zoning_rows"] = [
            {
                "zone": "Zone A",
                "floor": "Ground",
                "area_sqm": 24.4,
                "itza_weight": 1.0,
                "itza_contribution_sqm": 24.4,
                "displayed_tone": 300.0,
                "row_value": 7320.0,
            }
        ]
        html = pdf_generator._load_template(
            pdf_generator._get_env(),
            "evidence_pack.html",
        ).render(**pdf_generator._derive_fields(data))
        assert "ITZA Weight" in html
        assert "ITZA Contribution (sqm)" in html
        assert ">Description<" not in html


class TestVoaReconciliationRendering:
    def test_simplified_report_has_summary_row_only(self):
        data = pdf_generator._derive_fields(_base_report_data())
        html = pdf_generator._load_template(pdf_generator._get_env(), "simplified_report.html").render(**data)
        assert "VOA Record Match:" in html
        assert "Partial" in html
        assert "Floor Area Difference" not in html

    def test_evidence_pack_has_summary_plus_no_rows_only(self):
        data = pdf_generator._derive_fields(_base_report_data())
        html = pdf_generator._load_template(pdf_generator._get_env(), "evidence_pack.html").render(**data)
        assert "VOA Record Match" in html
        assert "Floor Area Difference" in html
        assert "55.0 sqm entered; VOA record shows 70.0 sqm." in html
        assert "Floor Plan Difference" not in html
        assert "Floor Split Difference" not in html


class TestLiveEvidencePathReconciliation:
    def test_retail_live_payload_reconciles_itza_rows_and_subtotal(self, monkeypatch):
        request = AssessRequest(
            contact=ContactInput(email="x@test.com", business_name="Phoenix Style"),
            property=PropertyInput(
                address="1 Test Parade",
                postcode="SW1A 1AA",
                business_type=BusinessType.retail,
                voa_rv=30000,
                nia_sqm=64.1,
            ),
            flags=FlagsInput(consent_disclaimer=True),
        )
        response = AssessResponse(
            signal="High",
            explanation="x",
            comparable_count=8,
            tone_rate=846.4755,
            base_estimated_rv=27700,
            adjusted_estimated_rv=27700,
            rated_comps=[],
        )

        def _fake_subject_record(*_args, **_kwargs):
            return {
                "uarn": "123",
                "sv_lines": [
                    {"floor": "Ground", "description": "Zone A", "area": 24.4, "price": 1300.0},
                    {"floor": "Ground", "description": "Zone B", "area": 11.0, "price": 650.0},
                    {"floor": "Basement", "description": "Internal storage", "area": 28.7, "price": None},
                ],
            }, "reference_override"

        monkeypatch.setattr("api.models.resolve_subject_voa_record", _fake_subject_record)

        payload = build_evidence_payload_from_assess(response, request)
        rows = payload["zoning_rows"]
        itza_sum = round(sum(float(r["itza_contribution_sqm"]) for r in rows), 2)
        subtotal_sum = round(sum(float(r["row_value"]) for r in rows), 2)
        assert payload["valuation_method"] == "itza"
        assert payload["valuation_basis_sqm"] == itza_sum
        assert payload["subtotal_pre"] == subtotal_sum
        assert payload["subtotal_pre"] == 27739.0

        html = pdf_generator._load_template(
            pdf_generator._get_env(),
            "evidence_pack.html",
        ).render(**pdf_generator._derive_fields(payload))
        assert "ITZA Weight" in html
        assert "ITZA Contribution (sqm)" in html
        assert ">Description<" not in html

    @pytest.mark.parametrize("business_type", [BusinessType.restaurant_cafe, BusinessType.nursery])
    def test_non_retail_live_payload_paths_remain_nia(self, business_type):
        request = AssessRequest(
            contact=ContactInput(email="x@test.com", business_name="Live Path Check"),
            property=PropertyInput(
                address="2 Test Parade",
                postcode="SW1A 1AA",
                business_type=business_type,
                voa_rv=25000,
                nia_sqm=100.0,
            ),
            flags=FlagsInput(consent_disclaimer=True),
        )
        response = AssessResponse(
            signal="Medium",
            explanation="x",
            comparable_count=6,
            tone_rate=250.0,
            base_estimated_rv=25000,
            adjusted_estimated_rv=25000,
            rated_comps=[],
        )
        payload = build_evidence_payload_from_assess(response, request)
        assert payload["valuation_method"] == "nia"
        assert payload["zoning_rows"] == []
