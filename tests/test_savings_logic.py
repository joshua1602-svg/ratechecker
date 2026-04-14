from datetime import date

from api.models import (
    AssessRequest,
    AssessResponse,
    BusinessType,
    ContactInput,
    FlagsInput,
    PropertyInput,
    build_evidence_payload_from_assess,
    build_report_payload_from_assess,
)
from api.reports.pdf_generator import _derive_fields
from api.services.savings import (
    calculate_implied_savings,
    estimate_annual_rates_payable,
    get_years_remaining_in_cycle,
)


def _request(voa_rv: float = 20_000) -> AssessRequest:
    return AssessRequest(
        contact=ContactInput(email="test@example.com", business_name="Demo"),
        property=PropertyInput(
            postcode="SW1A 1AA",
            address="1 High Street",
            business_type=BusinessType.retail,
            voa_rv=voa_rv,
            nia_sqm=100.0,
        ),
        flags=FlagsInput(consent_disclaimer=True),
    )


def _response(adj_rv: int = 19_800) -> AssessResponse:
    return AssessResponse(
        signal="Medium",
        explanation="x",
        comparable_count=3,
        tone_rate=200.0,
        base_estimated_rv=adj_rv,
        adjusted_estimated_rv=adj_rv,
        rated_comps=[{"uarn": "1", "address": "A", "rv": 10_000, "nia_sqm": 50, "rate": 200}],
    )


def test_rv_difference_not_emitted_as_annual_saving():
    payload = build_report_payload_from_assess(_response(adj_rv=19_800), _request(voa_rv=20_000))
    assert (20_000 - 19_800) == 200
    assert payload["implied_annual_saving_point"] != 200


def test_annual_saving_uses_rates_payable_conversion_logic():
    payload = build_report_payload_from_assess(_response(adj_rv=19_000), _request(voa_rv=20_000))
    expected = round(estimate_annual_rates_payable(20_000) - estimate_annual_rates_payable(19_000))
    assert payload["implied_annual_saving_point"] == expected


def test_total_saving_equals_annual_times_years_remaining():
    calc = calculate_implied_savings(
        current_rv=20_000,
        modelled_rv_point=19_000,
        modelled_rv_low=18_000,
        modelled_rv_high=19_000,
        as_of=date(2026, 1, 1),
    )
    assert calc["implied_total_saving_point"] == calc["implied_annual_saving_point"] * calc["years_remaining_in_cycle"]


def test_years_remaining_before_cutoff_is_three():
    assert get_years_remaining_in_cycle(as_of=date(2026, 7, 30)) == 3


def test_years_remaining_on_or_after_cutoff_is_two():
    assert get_years_remaining_in_cycle(as_of=date(2026, 7, 31)) == 2
    assert get_years_remaining_in_cycle(as_of=date(2026, 8, 1)) == 2


def test_pdf_payload_uses_implied_total_savings_not_rv_delta_as_savings():
    payload = build_evidence_payload_from_assess(_response(adj_rv=19_800), _request(voa_rv=20_000))
    derived = _derive_fields(payload)
    assert derived["implied_total_saving_point"] != (derived["voa_rv"] - derived["modelled_rv"])
    assert "implied total savings" in derived["submission_narrative"].lower()


def test_free_result_payload_includes_indicative_total_saving_range():
    payload = build_report_payload_from_assess(_response(adj_rv=19_000), _request(voa_rv=20_000))
    assert payload["indicative_total_saving_low"] == payload["implied_total_saving_low"]
    assert payload["indicative_total_saving_high"] == payload["implied_total_saving_high"]
    assert payload["years_remaining_in_cycle"] in {2, 3}
