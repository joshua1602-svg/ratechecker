from __future__ import annotations

from api.engine.subject_exclusion import exclude_subject_rows


def test_exact_identifier_exclusion_wins():
    rows = [
        {"uarn": "111", "address": "1 High Street", "postcode": "SW1A 1AA"},
        {"uarn": "222", "address": "2 High Street", "postcode": "SW1A 1AA"},
    ]
    filtered, stats = exclude_subject_rows(
        rows,
        subject_id="111",
        subject_address="1 High Street",
        subject_postcode="SW1A 1AA",
    )

    assert [r["uarn"] for r in filtered] == ["222"]
    assert stats["removed_by_id"] == 1
    assert stats["removed_by_address"] == 0


def test_fallback_normalised_address_and_postcode_exclusion():
    rows = [
        {"uarn": "333", "address": "12, High St.", "postcode": "sw1a 1aa"},
        {"uarn": "444", "address": "14 High Street", "postcode": "SW1A 1AA"},
    ]
    filtered, stats = exclude_subject_rows(
        rows,
        subject_id=None,
        subject_address="12 HIGH STREET",
        subject_postcode="SW1A1AA",
    )

    assert [r["uarn"] for r in filtered] == ["444"]
    assert stats["removed_by_id"] == 0
    assert stats["removed_by_address"] == 1


def test_shared_postcode_non_subject_preserved():
    rows = [
        {"uarn": "555", "address": "3 Market Road", "postcode": "SW1A 1AA"},
        {"uarn": "666", "address": "5 Market Road", "postcode": "SW1A 1AA"},
    ]
    filtered, stats = exclude_subject_rows(
        rows,
        subject_id=None,
        subject_address="7 Market Road",
        subject_postcode="SW1A 1AA",
    )

    assert [r["uarn"] for r in filtered] == ["555", "666"]
    assert stats["removed_by_address"] == 0


def test_thin_pool_subject_removed_without_dropping_others():
    rows = [
        {"uarn": "777", "address": "Unit 1, King's Parade", "postcode": "BS1 4QA"},
        {"uarn": "888", "address": "Unit 3 Kings Parade", "postcode": "BS1 4QA"},
    ]
    filtered, _ = exclude_subject_rows(
        rows,
        subject_id=None,
        subject_address="Unit 1 Kings Parade",
        subject_postcode="BS14QA",
    )

    assert [r["uarn"] for r in filtered] == ["888"]


def test_address_postcode_fallback_still_applies_when_subject_id_is_wrong():
    rows = [
        {"uarn": "777", "address": "Unit 1, King's Parade", "postcode": "BS1 4QA"},
        {"uarn": "888", "address": "Unit 3 Kings Parade", "postcode": "BS1 4QA"},
    ]
    filtered, stats = exclude_subject_rows(
        rows,
        subject_id="999",  # unresolved/incorrect id should not block fallback
        subject_address="Unit 1 Kings Parade",
        subject_postcode="BS1 4QA",
    )
    assert [r["uarn"] for r in filtered] == ["888"]
    assert stats["removed_by_address"] == 1
