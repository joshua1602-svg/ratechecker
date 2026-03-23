"""Tests for api.reports.pdf_generator."""
from __future__ import annotations

import pytest

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
        "zoning_rows": [
            {"zone": "A", "depth": 6.1, "area": 30.5, "rate": 250, "value": 7625},
            {"zone": "B", "depth": 6.1, "area": 30.5, "rate": 125, "value": 3812},
        ],
        "nursery_adjustments": None,
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
        del data["uprn"]
        with pytest.raises(ValueError, match="uprn"):
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
        data["zoning_rows"] = []
        data["nursery_adjustments"] = None
        result = generate_evidence_pack(data)
        assert isinstance(result, bytes)
        assert result[:4] == b"%PDF"

    def test_retail_missing_zoning_rows_no_longer_required(self):
        data = _base_report_data()
        data["business_type"] = "retail"
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

        assert 'Page <span class="page-number"></span> of <span class="total-pages"></span>' in html
        assert ".page-number::before { content: counter(page); }" in html
        assert ".total-pages::before { content: counter(pages); }" in html
        assert "Annual Saving" in html
        assert "&pound;18000&ndash;&pound;22000" in html
        assert 'class="banner-value nowrap-range"' in html

    def test_evidence_pack_renders_title_case_sector_and_dynamic_comparable_reference(self):
        data = pdf_generator._derive_fields(_base_report_data())
        html = pdf_generator._load_template(
            pdf_generator._get_env(),
            "evidence_pack.html",
        ).render(**data)

        assert ">Restaurant Cafe<" in html
        assert 'id="comparable-evidence"' in html
        assert 'class="page-ref"' in html
        assert "Comparable evidence is set out on page" in html
        assert "Challenge deadline:" in html
