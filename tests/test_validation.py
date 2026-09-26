"""Unit tests — no network, no API key, no PDF needed."""
from utils import parse_pages_arg
from validator import cross_page_checks, validate_page_payload


def test_parse_single():
    assert parse_pages_arg("1", 33) == [1]


def test_parse_range():
    assert parse_pages_arg("1-3", 33) == [1, 2, 3]


def test_parse_list_clamped():
    assert parse_pages_arg("1,3,99", 5) == [1, 3]


def test_validate_good_page():
    payload = {"page_number": 1, "voters": [
        {"serial_number": 1, "epic_number": "ABC1234567", "name": "RAMESH KUMAR",
         "relation_type": "FATHER", "relation_name": "SURESH KUMAR",
         "house_number": "125", "age": 45, "gender": "Male",
         "confidence": "HIGH", "needs_review": False, "review_reason": None}]}
    parsed, issues, _ = validate_page_payload(payload, 1)
    assert parsed is not None
    assert len(parsed.voters) == 1
    assert not any("out of range" in i for i in issues)


def test_validate_flags_bad_age_and_epic():
    payload = {"page_number": 2, "voters": [
        {"serial_number": 1, "epic_number": "WRONG", "name": "X",
         "relation_type": "FATHER", "relation_name": "Y",
         "house_number": "1", "age": 999, "gender": "Male",
         "confidence": "HIGH", "needs_review": False, "review_reason": None}]}
    parsed, issues, susp = validate_page_payload(payload, 2)
    assert parsed is not None
    assert parsed.voters[0].needs_review is True
    assert any("out of range" in i for i in issues)
    assert any("non-standard" in i for i in issues)
    assert len(susp) >= 1


def test_validate_missing_name():
    payload = {"page_number": 3, "voters": [
        {"serial_number": 5, "epic_number": None, "name": None,
         "relation_type": None, "relation_name": None, "house_number": None,
         "age": None, "gender": None, "confidence": "LOW",
         "needs_review": False, "review_reason": None}]}
    parsed, issues, _ = validate_page_payload(payload, 3)
    assert parsed.voters[0].needs_review is True
    assert any("missing" in i.lower() for i in issues)


def test_cross_page_duplicates():
    voters = [{"epic_number": "ABC1234567", "serial_number": 1},
              {"epic_number": "ABC1234567", "serial_number": 2},
              {"epic_number": "ZZZ9999999", "serial_number": 1}]
    out = cross_page_checks(voters)
    assert out["duplicate_epics"] == {"ABC1234567": 2}
    assert out["duplicate_serials"] == {1: 2}
