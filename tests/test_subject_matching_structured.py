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
