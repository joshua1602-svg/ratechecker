import argparse
from decimal import Decimal
import json

from scripts.run_shortlist import (
    _derive_confidence_proxy,
    _early_filter_reason,
    _extract_result,
    _json_safe,
    _passes_filters,
    _score_components,
    _sorted_rows,
    _validate_runtime_scope,
    parse_args,
)


class _Resp:
    def __init__(self, **kwargs):
        self.adjusted_estimated_rv = kwargs.get("adjusted_estimated_rv")
        self.base_estimated_rv = kwargs.get("base_estimated_rv")
        self.implied_annual_saving_low = kwargs.get("implied_annual_saving_low")
        self.implied_annual_saving_high = kwargs.get("implied_annual_saving_high")
        self.implied_annual_saving_point = kwargs.get("implied_annual_saving_point")
        self.comparable_count = kwargs.get("comparable_count")
        self.rated_comps = kwargs.get("rated_comps") or []
        self.signal = kwargs.get("signal", "Medium")
        self.tone_source = kwargs.get("tone_source", "primary_cluster")
        self.tone_source_label = kwargs.get("tone_source_label", "Primary cluster")


def _args(**overrides):
    base = {
        "min_saving": 2500.0,
        "min_delta_pct": 10.0,
        "min_confidence": 0.6,
        "min_comp_count": 3,
        "min_same_street_comp_count": 0,
        "exclude_ambiguous": True,
        "exclude_severe_quality": True,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_normalized_scores_bounded_and_weighted():
    scores = _score_components(
        annual_saving_high=50_000,
        rv_delta_pct=99,
        confidence_score=1.5,
        comp_count=100,
        same_street_comp_count=20,
    )
    assert scores["saving_score"] == 100.0
    assert scores["delta_score"] == 100.0
    assert scores["confidence_score_component"] == 100.0
    assert scores["comp_score"] == 100.0
    assert scores["same_street_score"] == 100.0
    assert scores["shortlist_score"] == 100.0


def test_derived_confidence_is_explicit_and_penalized_for_sparse_data():
    score, reason, raw = _derive_confidence_proxy(signal="Insufficient Data", comp_count=0, same_street_comp_count=0)
    assert score < 0.2
    assert "ambiguity_no_comps" in reason
    assert raw["signal"] == "Insufficient Data"
    assert raw["comp_count"] == 0


def test_extract_result_shape_and_component_fields():
    row = {
        "uarn": "123",
        "address": "1 High St",
        "postcode": "SW19 1AA",
        "business_type": "retail",
        "scat_code": 249,
        "voa_rv": 50000,
    }
    resp = _Resp(
        adjusted_estimated_rv=30000,
        implied_annual_saving_low=7000,
        implied_annual_saving_high=9000,
        implied_annual_saving_point=8000,
        comparable_count=6,
        rated_comps=[{"is_same_street": True}, {"is_same_street": False}, {"is_same_street": True}],
        signal="High",
    )

    result = _extract_result(row, resp)

    assert result["rv_delta"] == 20000.0
    assert result["derived_confidence_score"] > 0
    assert result["saving_score"] > 0
    assert result["delta_score"] > 0
    assert result["confidence_score_component"] > 0
    assert result["comp_score"] > 0
    assert result["same_street_score"] > 0
    assert result["score_breakdown_summary"]
    assert "dominant=" in result["score_breakdown_summary"]
    assert result["score_breakdown_json"].startswith("{")


def test_passes_filters_and_failure_reason_string():
    candidate = {
        "estimated_annual_saving_high": 1000,
        "rv_delta_pct": 5,
        "derived_confidence_score": 0.4,
        "comp_count": 2,
        "same_street_comp_count": 0,
    }
    passed, _, fail_flags, reason = _passes_filters(candidate, _args())
    assert passed is False
    assert set(fail_flags) >= {"min_saving", "min_delta_pct", "min_confidence", "min_comp_count"}
    assert reason.startswith("filtered:")


def test_parse_args_cli_flags_and_removed_min_comp_density():
    args = parse_args([
        "--postcode", "SW19",
        "--sector", "retail",
        "--min-saving", "5000",
        "--dry-run",
        "--batch-size", "50",
    ])
    assert args.postcode == "SW19"
    assert args.sector == "retail"
    assert args.min_saving == 5000.0
    assert args.dry_run is True
    assert args.batch_size == 50
    assert not hasattr(args, "min_comp_density")
    assert args._scope_filter_provided is True


def test_runtime_scope_validation_requires_explicit_scope():
    args = parse_args([])
    assert args._scope_filter_provided is False
    try:
        _validate_runtime_scope(args)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "Refusing to run without explicit scope" in str(exc)


def test_export_sorting_prefers_pass_and_high_score():
    rows = [
        {"status": "filtered_out", "shortlist_score": 99, "estimated_annual_saving_high": 9999, "rv_delta_pct": 30},
        {"status": "pass", "shortlist_score": 50, "estimated_annual_saving_high": 100, "rv_delta_pct": 10},
        {"status": "pass", "shortlist_score": 80, "estimated_annual_saving_high": 200, "rv_delta_pct": 15},
    ]
    sorted_rows = _sorted_rows(rows)
    assert sorted_rows[0]["status"] == "pass"
    assert sorted_rows[0]["shortlist_score"] == 80


def test_early_filter_reason_supported():
    assert _early_filter_reason({"postcode": "", "voa_rv": 1, "nia_sqm": 1, "business_type": "retail", "scat_code": 249}) == "missing_postcode"
    assert _early_filter_reason({"postcode": "SW1", "voa_rv": 0, "nia_sqm": 1, "business_type": "retail", "scat_code": 249}) == "non_positive_rv"
    assert _early_filter_reason({"postcode": "SW1", "voa_rv": 1, "nia_sqm": 0, "business_type": "retail", "scat_code": 249}) == "non_positive_nia"


def test_json_safe_serializes_decimal():
    payload = {"a": Decimal("12.34"), "nested": [Decimal("1.5"), {"b": Decimal("2.5")}]}
    safe = _json_safe(payload)
    encoded = json.dumps(safe)
    assert "\"a\": 12.34" in encoded
