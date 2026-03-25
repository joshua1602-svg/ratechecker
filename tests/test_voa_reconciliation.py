import pytest
from api.services.voa_reconciliation import reconcile_subject_against_voa
from api.models import (
    AssessRequest,
    ContactInput,
    PropertyInput,
    FlagsInput,
    BusinessType,
    AreasInput,
    LayoutInputModel,
    _build_voa_reconciliation,
)


def _run(user=None, voa=None, facts=None):
    return reconcile_subject_against_voa(
        user_payload=user or {},
        voa_subject_record=voa or {},
        normalized_facts=facts or {},
    )["voa_reconciliation"]


def test_exact_match_all_yes():
    result = _run(
        user={"business_type": "retail", "total_area_sqm": 70.0},
        voa={"total_area_sqm": 70.0, "scat_code": 249, "description": "Shop and Premises", "floor_areas": {"ground": 45.0, "basement": 25.0}},
        facts={"ground_present": True, "basement_present": True, "floor_areas": {"ground": 45.0, "basement": 25.0}},
    )
    assert result["overall_status"] == "yes"
    assert result["summary"] == {"total_checks": 4, "yes": 4, "no": 0, "unknown": 0}


def test_gross_floor_area_mismatch_over_threshold():
    result = _run(
        user={"business_type": "retail", "total_area_sqm": 55.0},
        voa={"total_area_sqm": 70.0, "scat_code": 249, "floor_areas": {"ground": 30.0, "basement": 25.0}},
        facts={"ground_present": True, "basement_present": True, "floor_areas": {"ground": 30.0, "basement": 25.0}},
    )
    assert result["checks"]["gross_floor_space"]["status"] == "no"


def test_missing_voa_area_unknown():
    result = _run(
        user={"business_type": "retail", "total_area_sqm": 55.0},
        voa={"total_area_sqm": None, "scat_code": 249, "floor_areas": {"ground": 30.0, "basement": 25.0}},
        facts={"ground_present": True, "basement_present": True, "floor_areas": {"ground": 30.0, "basement": 25.0}},
    )
    assert result["checks"]["gross_floor_space"]["status"] == "unknown"


def test_floor_plan_mismatch():
    result = _run(
        user={"business_type": "retail", "total_area_sqm": 70.0},
        voa={"total_area_sqm": 70.0, "scat_code": 249, "floor_areas": {"ground": 70.0, "basement": 0.0}},
        facts={"ground_present": True, "basement_present": True, "floor_areas": {"ground": 70.0, "basement": 0.0}},
    )
    assert result["checks"]["floor_plan_configuration"]["status"] == "no"


def test_total_match_but_floor_split_mismatch():
    result = _run(
        user={"business_type": "retail", "total_area_sqm": 70.0},
        voa={"total_area_sqm": 70.0, "scat_code": 249, "floor_areas": {"ground": 45.0, "basement": 25.0}},
        facts={"ground_present": True, "basement_present": True, "floor_areas": {"ground": 30.0, "basement": 40.0}},
    )
    assert result["checks"]["gross_floor_space"]["status"] == "yes"
    assert result["checks"]["floor_split"]["status"] == "no"


def test_retail_business_type_match():
    result = _run(
        user={"business_type": "retail", "total_area_sqm": 70.0},
        voa={"total_area_sqm": 70.0, "scat_code": 251, "description": "Showroom", "floor_areas": {"ground": 70.0, "basement": 0.0}},
        facts={"ground_present": True, "basement_present": False, "floor_areas": {"ground": 70.0, "basement": 0.0}},
    )
    assert result["checks"]["business_type"]["status"] == "yes"


def test_retail_business_type_mismatch():
    result = _run(
        user={"business_type": "retail", "total_area_sqm": 70.0},
        voa={"total_area_sqm": 70.0, "scat_code": 409, "description": "Cafe", "floor_areas": {"ground": 70.0, "basement": 0.0}},
        facts={"ground_present": True, "basement_present": False, "floor_areas": {"ground": 70.0, "basement": 0.0}},
    )
    assert result["checks"]["business_type"]["status"] == "no"


def test_all_unknown_inputs_overall_unknown():
    result = _run(user={}, voa={}, facts={})
    assert result["overall_status"] == "unknown"




def test_one_no_one_yes_two_unknown_is_partially():
    result = _run(
        user={"business_type": "retail", "total_area_sqm": 70.0},
        voa={"total_area_sqm": None, "scat_code": 409, "description": "Cafe", "floor_areas": {"ground": 45.0, "basement": None}},
        facts={"ground_present": True, "basement_present": None, "floor_areas": {"ground": 45.0, "basement": None}},
    )
    statuses = [
        result["checks"]["gross_floor_space"]["status"],
        result["checks"]["floor_plan_configuration"]["status"],
        result["checks"]["floor_split"]["status"],
        result["checks"]["business_type"]["status"],
    ]
    assert statuses.count("yes") == 1
    assert statuses.count("no") == 1
    assert statuses.count("unknown") == 2
    assert result["overall_status"] == "partially"


def test_mixed_yes_no_outcome_overall_partially():
    result = _run(
        user={"business_type": "retail", "total_area_sqm": 70.0},
        voa={"total_area_sqm": 70.0, "scat_code": 409, "description": "Cafe", "floor_areas": {"ground": 45.0, "basement": 25.0}},
        facts={"ground_present": True, "basement_present": True, "floor_areas": {"ground": 45.0, "basement": 25.0}},
    )
    assert result["overall_status"] == "partially"


def test_one_no_three_unknown_is_partially():
    result = _run(
        user={"business_type": "retail", "total_area_sqm": 70.0},
        voa={"total_area_sqm": None, "scat_code": 409, "description": "Cafe", "floor_areas": {}},
        facts={"ground_present": None, "basement_present": None, "floor_areas": {}},
    )
    assert result["checks"]["business_type"]["status"] == "no"
    assert result["overall_status"] == "partially"


def test_unsupported_business_type_returns_unknown_reason_code():
    result = _run(
        user={"business_type": "nursery", "total_area_sqm": 70.0},
        voa={"total_area_sqm": 70.0, "scat_code": 85, "description": "Nursery", "floor_areas": {"ground": 70.0, "basement": 0.0}},
        facts={"ground_present": True, "basement_present": False, "floor_areas": {"ground": 70.0, "basement": 0.0}},
    )
    check = result["checks"]["business_type"]
    assert check["status"] == "unknown"
    assert check["reason_code"] == "BUSINESS_TYPE_NOT_CONFIGURED"




def _request_without_uprn() -> AssessRequest:
    return AssessRequest(
        contact=ContactInput(email="a@b.com", business_name="Test Biz"),
        property=PropertyInput(
            address="12 High Street",
            postcode="SW1A 1AA",
            uprn=None,
            property_reference=None,
            business_type=BusinessType.retail,
            voa_rv=20000,
            nia_sqm=70.0,
        ),
        flags=FlagsInput(consent_disclaimer=True),
    )


def test_subject_resolution_single_candidate_no_reference(monkeypatch):
    req = _request_without_uprn()
    monkeypatch.setattr("api.db.get_subject_voa_record_by_reference", lambda *_: None)
    monkeypatch.setattr("api.db.get_subject_voa_candidates_by_address_postcode", lambda **_: [{
        "uarn": "100",
        "total_area_sqm": 70.0,
        "floor_areas": {"ground": 70.0, "basement": 0.0},
        "scat_code": 249,
        "description": "Shop and Premises",
    }])
    monkeypatch.setattr("api.db.get_subject_voa_record", lambda *_: None)

    result = _build_voa_reconciliation(req)
    assert result["checks"]["business_type"]["status"] == "yes"


def test_ambiguous_candidates_retail_scat_filter(monkeypatch):
    req = _request_without_uprn()
    monkeypatch.setattr("api.db.get_subject_voa_record_by_reference", lambda *_: None)
    monkeypatch.setattr("api.db.get_subject_voa_candidates_by_address_postcode", lambda **_: [
        {"uarn": "100", "total_area_sqm": 70.0, "floor_areas": {"ground": 70.0, "basement": 0.0}, "scat_code": 409, "description": "Cafe"},
        {"uarn": "101", "total_area_sqm": 70.0, "floor_areas": {"ground": 70.0, "basement": 0.0}, "scat_code": 249, "description": "Shop"},
    ])
    monkeypatch.setattr("api.db.get_subject_voa_record", lambda *_: None)

    result = _build_voa_reconciliation(req)
    assert result["checks"]["business_type"]["status"] == "yes"
    assert result["checks"]["business_type"]["voa_scat_code"] == 249


def test_ambiguous_retail_candidates_floor_presence_selects(monkeypatch):
    req = _request_without_uprn()
    req.areas = AreasInput(sales_area_sqm=45.0, basement_sqm=25.0)
    req.layout = LayoutInputModel(floor_config="ground_lower_ground")

    monkeypatch.setattr("api.db.get_subject_voa_record_by_reference", lambda *_: None)
    monkeypatch.setattr("api.db.get_subject_voa_candidates_by_address_postcode", lambda **_: [
        {"uarn": "200", "total_area_sqm": 70.0, "floor_areas": {"ground": 70.0, "basement": 0.0}, "scat_code": 249, "description": "Shop"},
        {"uarn": "201", "total_area_sqm": 70.0, "floor_areas": {"ground": 45.0, "basement": 25.0}, "scat_code": 249, "description": "Shop"},
    ])
    monkeypatch.setattr("api.db.get_subject_voa_record", lambda *_: None)

    result = _build_voa_reconciliation(req)
    assert result["checks"]["floor_plan_configuration"]["status"] == "yes"


def test_ambiguous_candidates_area_proximity_selects_closest(monkeypatch):
    req = _request_without_uprn()
    monkeypatch.setattr("api.db.get_subject_voa_record_by_reference", lambda *_: None)
    monkeypatch.setattr("api.db.get_subject_voa_candidates_by_address_postcode", lambda **_: [
        {"uarn": "300", "total_area_sqm": 50.0, "floor_areas": {"ground": 50.0, "basement": 0.0}, "scat_code": 249, "description": "Shop"},
        {"uarn": "301", "total_area_sqm": 69.0, "floor_areas": {"ground": 69.0, "basement": 0.0}, "scat_code": 249, "description": "Shop"},
    ])
    monkeypatch.setattr("api.db.get_subject_voa_record", lambda *_: None)

    result = _build_voa_reconciliation(req)
    assert result["checks"]["gross_floor_space"]["voa_value_sqm"] == 69.0


def test_optional_reference_override_wins_when_valid(monkeypatch):
    req = _request_without_uprn()
    req.property.property_reference = "ref-999"

    monkeypatch.setattr("api.db.get_subject_voa_record_by_reference", lambda *_: {
        "uarn": "999",
        "total_area_sqm": 70.0,
        "floor_areas": {"ground": 70.0, "basement": 0.0},
        "scat_code": 249,
        "description": "Shop",
    })
    monkeypatch.setattr("api.db.get_subject_voa_candidates_by_address_postcode", lambda **_: pytest.fail("address lookup should not run when override resolves"))
    monkeypatch.setattr("api.db.get_subject_voa_record", lambda *_: None)

    result = _build_voa_reconciliation(req)
    assert result["checks"]["business_type"]["status"] == "yes"


def test_invalid_optional_reference_falls_back_to_address_lookup(monkeypatch):
    req = _request_without_uprn()
    req.property.property_reference = "bad-reference"

    monkeypatch.setattr("api.db.get_subject_voa_record_by_reference", lambda *_: None)
    monkeypatch.setattr("api.db.get_subject_voa_candidates_by_address_postcode", lambda **_: [{
        "uarn": "777",
        "total_area_sqm": 70.0,
        "floor_areas": {"ground": 70.0, "basement": 0.0},
        "scat_code": 249,
        "description": "Shop",
    }])
    monkeypatch.setattr("api.db.get_subject_voa_record", lambda *_: None)

    result = _build_voa_reconciliation(req)
    assert result["checks"]["business_type"]["status"] == "yes"


def test_ambiguous_unresolvable_candidates_return_unknown(monkeypatch):
    req = _request_without_uprn()
    req.property.business_type = BusinessType.hair_beauty
    req.layout = None
    req.areas = None
    monkeypatch.setattr("api.db.get_subject_voa_record_by_reference", lambda *_: None)
    monkeypatch.setattr("api.db.get_subject_voa_candidates_by_address_postcode", lambda **_: [
        {"uarn": "401", "total_area_sqm": None, "floor_areas": {"ground": 0.0, "basement": 0.0}, "scat_code": 417, "description": "HB"},
        {"uarn": "402", "total_area_sqm": None, "floor_areas": {"ground": 0.0, "basement": 0.0}, "scat_code": 417, "description": "HB"},
    ])
    monkeypatch.setattr("api.db.get_subject_voa_record", lambda *_: None)

    result = _build_voa_reconciliation(req)
    assert result["checks"]["gross_floor_space"]["status"] == "unknown"
    assert result["checks"]["business_type"]["status"] == "unknown"
