from types import SimpleNamespace
import inspect

from api import db


def _row(uarn: str, street: str, fpi: str):
    return SimpleNamespace(uarn=uarn, street=street, full_property_identifier=fpi)


def test_simple_numeric_address_match():
    street, num = db._derive_user_street_and_number("22 High Street, London")
    assert street == "HIGH STREET"
    assert num == 22

    rows = [_row("1", "HIGH STREET", "22, HIGH STREET, WIMBLEDON, LONDON")]
    filtered, street_count, number_count = db._filter_structured_subject_candidates(rows, user_street=street, user_number=num)
    assert len(filtered) == 1
    assert street_count == 1
    assert number_count == 1


def test_structured_match_without_exact_full_address_equality():
    street, num = db._derive_user_street_and_number("22 High Street, London")
    rows = [_row("1", "HIGH STREET", "22, HIGH STREET, WIMBLEDON, LONDON")]
    filtered, _, _ = db._filter_structured_subject_candidates(rows, user_street=street, user_number=num)
    assert filtered[0].uarn == "1"


def test_multiple_candidates_building_number_narrows():
    street, num = db._derive_user_street_and_number("22 High Street, London")
    rows = [
        _row("1", "HIGH STREET", "22, HIGH STREET, WIMBLEDON, LONDON"),
        _row("2", "HIGH STREET", "24, HIGH STREET, WIMBLEDON, LONDON"),
    ]
    filtered, street_count, number_count = db._filter_structured_subject_candidates(rows, user_street=street, user_number=num)
    assert street_count == 2
    assert number_count == 1
    assert [r.uarn for r in filtered] == ["1"]


def test_no_clean_user_building_number_falls_back_to_street():
    street, num = db._derive_user_street_and_number("High Street, London")
    assert num is None
    rows = [
        _row("1", "HIGH STREET", "22, HIGH STREET, WIMBLEDON, LONDON"),
        _row("2", "HIGH STREET", "24, HIGH STREET, WIMBLEDON, LONDON"),
    ]
    filtered, street_count, number_count = db._filter_structured_subject_candidates(rows, user_street=street, user_number=num)
    assert street_count == 2
    assert number_count == 2
    assert len(filtered) == 2


def test_complex_voa_identifier_returns_no_clean_number():
    assert db._extract_voa_building_number("GND & 1ST FLR 24, HIGH STREET, WIMBLEDON, LONDON") is None


def test_no_naive_full_address_substring_matching_before_structured_fields():
    src = inspect.getsource(db.get_subject_voa_candidates_by_address_postcode)
    assert "_normalise_address(row.full_property_identifier)" not in src
    assert "user_street" in src


def test_exclude_subject_by_canonical_uarn_match():
    rows = [
        {"uarn": "100", "address": "22, HIGH STREET, LONDON", "street": "HIGH STREET", "postcode": "SW1A 1AA"},
        {"uarn": "101", "address": "24, HIGH STREET, LONDON", "street": "HIGH STREET", "postcode": "SW1A 1AA"},
    ]
    filtered, excluded = db.exclude_subject_from_comparables(
        rows,
        subject_record={"uarn": "100", "street": "HIGH STREET", "postcode": "SW1A1AA", "building_number": 22},
        subject_address="22 High Street, London",
        subject_postcode="SW1A 1AA",
    )
    assert [r["uarn"] for r in filtered] == ["101"]
    assert excluded[0]["basis"] == "canonical_uarn"


def test_exclude_subject_by_structured_fields_when_address_strings_differ():
    rows = [
        {"uarn": "999", "address": "22, HIGH STREET, WIMBLEDON, LONDON", "street": "High Street", "postcode": "SW1A 1AA"},
        {"uarn": "101", "address": "24 HIGH STREET LONDON", "street": "High Street", "postcode": "SW1A 1AA"},
    ]
    filtered, excluded = db.exclude_subject_from_comparables(
        rows,
        subject_record=None,
        subject_address="22 High Street, London",
        subject_postcode="SW1A 1AA",
    )
    assert [r["uarn"] for r in filtered] == ["101"]
    assert excluded[0]["basis"] == "postcode_street_number"


def test_structured_exclusion_retains_nearby_non_subject_properties():
    rows = [
        {"uarn": "200", "address": "22, HIGH STREET, LONDON", "street": "HIGH STREET", "postcode": "SW1A 1AA"},
        {"uarn": "201", "address": "20, HIGH STREET, LONDON", "street": "HIGH STREET", "postcode": "SW1A 1AA"},
        {"uarn": "202", "address": "24, HIGH STREET, LONDON", "street": "HIGH STREET", "postcode": "SW1A 1AA"},
    ]
    filtered, _ = db.exclude_subject_from_comparables(
        rows,
        subject_record=None,
        subject_address="22 High Street, London",
        subject_postcode="SW1A 1AA",
    )
    assert [r["uarn"] for r in filtered] == ["201", "202"]


def test_structured_exclusion_works_without_subject_identifier():
    rows = [{"uarn": "300", "address": "7, MARKET ROAD, LONDON", "street": "MARKET ROAD", "postcode": "E1 2AB"}]
    filtered, excluded = db.exclude_subject_from_comparables(
        rows,
        subject_record={},
        subject_address="7 Market Road, London",
        subject_postcode="E1 2AB",
    )
    assert filtered == []
    assert excluded[0]["basis"] == "postcode_street_number"
