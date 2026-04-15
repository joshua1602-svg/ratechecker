import argparse

from scripts.run_shortlist import (
    _extract_result,
    _passes_filters,
    _shortlist_score,
    _sorted_rows,
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


def test_shortlist_score_increases_with_inputs():
    low = _shortlist_score(
        annual_saving_low=1000,
        annual_saving_high=2000,
        rv_delta_pct=8,
        confidence_score=0.6,
        comp_count=3,
        same_street_comp_count=0,
    )
    high = _shortlist_score(
        annual_saving_low=4000,
        annual_saving_high=6000,
        rv_delta_pct=20,
        confidence_score=0.9,
        comp_count=8,
        same_street_comp_count=4,
    )
    assert high > low


def test_extract_result_shape_and_values():
    row = {"uarn": "123", "address": "1 High St", "postcode": "SW19 1AA", "scat_code": 249, "voa_rv": 50000}
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

    assert result["fair_rv_low"] is not None
    assert result["fair_rv_high"] is not None
    assert result["rv_delta"] == 20000.0
    assert result["comp_count"] == 6
    assert result["same_street_comp_count"] == 2
    assert result["shortlist_score"] > 0


def test_passes_filters_and_failure_reasons():
    candidate = {
        "estimated_annual_saving_high": 1000,
        "rv_delta_pct": 5,
        "confidence_score": 0.4,
        "comp_count": 2,
        "same_street_comp_count": 0,
    }
    passed, pass_flags, fail_flags = _passes_filters(candidate, _args())
    assert passed is False
    assert pass_flags == ['min_same_street_comp_count']
    assert set(fail_flags) >= {"min_saving", "min_delta_pct", "min_confidence", "min_comp_count"}


def test_parse_args_cli_flags():
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


def test_export_sorting_prefers_pass_and_high_score():
    rows = [
        {"status": "filtered_out", "shortlist_score": 9999, "estimated_annual_saving_high": 9999, "rv_delta_pct": 30},
        {"status": "pass", "shortlist_score": 100, "estimated_annual_saving_high": 100, "rv_delta_pct": 10},
        {"status": "pass", "shortlist_score": 300, "estimated_annual_saving_high": 200, "rv_delta_pct": 15},
    ]
    sorted_rows = _sorted_rows(rows)
    assert sorted_rows[0]["status"] == "pass"
    assert sorted_rows[0]["shortlist_score"] == 300


def test_failure_handling_result_envelope():
    candidate = {
        "estimated_annual_saving_high": 0,
        "rv_delta_pct": 0,
        "confidence_score": 0,
        "comp_count": 0,
        "same_street_comp_count": 0,
    }
    passed, _, fail_flags = _passes_filters(candidate, _args())
    assert passed is False
    assert "subject_match_ambiguity" in fail_flags
