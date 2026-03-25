from api.services.voa_reconciliation import reconcile_subject_against_voa


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
