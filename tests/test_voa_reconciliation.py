from api.models import (
    AssessRequest,
    AreasInput,
    BusinessType,
    ContactInput,
    FlagsInput,
    LayoutInputModel,
    PropertyInput,
    _build_voa_reconciliation,
)
from api.services.voa_reconciliation import reconcile_subject_against_voa


def _run(user=None, voa=None, facts=None):
    return reconcile_subject_against_voa(
        user_payload=user or {},
        voa_subject_record=voa or {},
        normalized_facts=facts or {},
    )["voa_reconciliation"]


def test_exact_total_match_but_categorisation_difference_is_strong():
    result = _run(
        user={"business_type": "retail", "total_area_sqm": 72.5},
        voa={
            "total_area_sqm": 72.55,
            "sv_lines": [
                {"description": "Zone A", "area": 30.0},
                {"description": "Zone B", "area": 25.0},
                {"description": "Zone C", "area": 17.55},
            ],
            "floor_areas": {"ground": 72.55, "basement": 0.0},
        },
        facts={
            "ground_present": True,
            "basement_present": False,
            "floor_areas": {"ground": 45.0, "basement": 0.0},
            "entered_area_components": {"kitchen_sqm": 10.0, "kitchen_present": True, "storage_sqm": 15.0},
        },
    )
    assert result["overall_status"] == "strong"
    assert result["voa_area_match_status"] == "strong"
    assert result["voa_layout_match_status"] == "different"
    assert result["checks"]["total_area_alignment"]["detail_text"] == "Entered total area 72.5 sqm; VOA structured area 72.5 sqm."


def test_worple_style_total_area_compares_full_entered_total_not_trading_only():
    result = _run(
        user={"business_type": "retail", "total_area_sqm": 128.0},
        voa={
            "total_area_sqm": 127.8,
            "sv_lines": [
                {"description": "Zone A", "area": 50.0},
                {"description": "Zone B", "area": 45.0},
                {"description": "Remainder", "area": 32.8},
            ],
            "floor_areas": {"ground": 127.8, "basement": 0.0},
        },
        facts={
            "ground_present": True,
            "basement_present": False,
            "floor_areas": {"ground": 80.0, "basement": 0.0},
            "entered_area_components": {"storage_sqm": 30.0, "kitchen_sqm": 18.0, "kitchen_present": True},
        },
    )
    assert result["overall_status"] == "strong"
    assert "128.0 sqm" in result["checks"]["total_area_alignment"]["detail_text"]
    assert "127.8 sqm" in result["checks"]["total_area_alignment"]["detail_text"]


def test_genuine_total_area_mismatch_is_partial():
    result = _run(
        user={"business_type": "retail", "total_area_sqm": 90.0},
        voa={"total_area_sqm": 120.0, "sv_lines": [{"description": "Zone A", "area": 120.0}]},
        facts={"entered_area_components": {}},
    )
    assert result["overall_status"] == "partial"
    assert result["voa_area_match_status"] == "partial"


def test_missing_kitchen_line_flags_structured_inconsistency_with_area_alignment():
    result = _run(
        user={"business_type": "restaurant_cafe", "total_area_sqm": 72.5},
        voa={
            "total_area_sqm": 72.55,
            "sv_lines": [
                {"description": "Zone A", "area": 40.0},
                {"description": "Zone B", "area": 32.55},
            ],
        },
        facts={"entered_area_components": {"kitchen_sqm": 12.0, "kitchen_present": True, "storage_sqm": 8.0}},
    )
    assert result["overall_status"] == "strong"
    assert result["voa_structured_inconsistency_flag"] is True
    assert any("kitchen" in note.lower() for note in result["voa_structured_inconsistency_notes"])


def test_no_structured_lines_graceful_fallback_unresolved():
    result = _run(
        user={"business_type": "retail", "total_area_sqm": 72.5},
        voa={"total_area_sqm": None, "sv_lines": []},
        facts={"entered_area_components": {}},
    )
    assert result["overall_status"] == "unresolved"
    assert result["voa_area_match_status"] == "unresolved"


def test_build_voa_reconciliation_inputs_use_total_area_for_matching(monkeypatch):
    req = AssessRequest(
        contact=ContactInput(email="a@b.com", business_name="Test"),
        property=PropertyInput(
            address="191 Worple Road",
            postcode="SW20 8AA",
            business_type=BusinessType.retail,
            voa_rv=40000,
            nia_sqm=128.0,
        ),
        areas=AreasInput(sales_area_sqm=80.0, storage_sqm=30.0, visible_kitchen_sqm=18.0),
        layout=LayoutInputModel(floor_config="ground_only", kitchen_on_ground="yes"),
        flags=FlagsInput(consent_disclaimer=True),
    )

    monkeypatch.setattr(
        "api.models.resolve_subject_voa_record",
        lambda *_a, **_k: (
            {
                "uarn": "x",
                "total_area_sqm": 127.8,
                "sv_lines": [{"description": "Zone A", "area": 127.8}],
                "floor_areas": {"ground": 127.8, "basement": 0.0},
            },
            "reference_override",
        ),
    )
    result = _build_voa_reconciliation(req)
    area_detail = result["checks"]["total_area_alignment"]["detail_text"]
    assert "128.0 sqm" in area_detail
    assert "127.8 sqm" in area_detail
