"""Validation layer: Pydantic + business rules. Flags, never silently drops."""
from __future__ import annotations

import re
from collections import Counter
from typing import List, Tuple

from models import PageVoters, VoterRecord

EPIC_RE = re.compile(r"^[A-Z]{3}[0-9]{7}$")  # standard Indian EPIC; advisory only
VALID_GENDERS = {"Male", "Female", "Other"}
VALID_RELATIONS = {"FATHER", "MOTHER", "HUSBAND", "WIFE", "OTHER"}
VALID_CONF = {"HIGH", "MEDIUM", "LOW"}


def validate_page_payload(payload: dict, expected_page: int,
                          min_age: int = 1, max_age: int = 120) -> Tuple[PageVoters | None, List[str], List[VoterRecord]]:
    """Returns (parsed_or_None, issues, suspicious_records). Never raises on bad rows."""
    issues: List[str] = []
    suspicious: List[VoterRecord] = []

    try:
        parsed = PageVoters.model_validate(payload)
    except Exception as exc:
        return None, [f"schema error: {exc}"], []

    if parsed.page_number != expected_page:
        issues.append(f"page_number mismatch: got {parsed.page_number}, expected {expected_page}")

    if not parsed.voters:
        issues.append("empty page: no voters returned (verify against image)")

    seen_epic, seen_serial = Counter(), Counter()
    for v in parsed.voters:
        # Required-field presence
        if v.serial_number is None:
            issues.append(f"missing serial_number for name={v.name}")
            v.needs_review = True
            v.review_reason = (v.review_reason or "") + "; missing serial"
        if v.name is None:
            issues.append(f"serial={v.serial_number}: missing name")
            v.needs_review = True
            v.review_reason = (v.review_reason or "") + "; missing name"
        # Age range (configurable)
        if v.age is not None and not (min_age <= v.age <= max_age):
            issues.append(f"serial={v.serial_number}: age {v.age} out of range [{min_age},{max_age}]")
            v.needs_review = True
            suspicious.append(v)
        # Gender / relation vocab
        if v.gender is not None and v.gender not in VALID_GENDERS:
            issues.append(f"serial={v.serial_number}: unexpected gender '{v.gender}'")
            v.needs_review = True
        if v.relation_type is not None and v.relation_type not in VALID_RELATIONS:
            issues.append(f"serial={v.serial_number}: unexpected relation '{v.relation_type}'")
            v.needs_review = True
        if v.confidence not in VALID_CONF:
            issues.append(f"serial={v.serial_number}: bad confidence '{v.confidence}'")
            v.confidence = "LOW"
            v.needs_review = True
        # EPIC: advisory format check — flag, don't reject (do not "correct" EPICs)
        if v.epic_number is None:
            issues.append(f"serial={v.serial_number}: missing EPIC -> review")
            v.needs_review = True
            suspicious.append(v)
        elif not EPIC_RE.match(v.epic_number):
            issues.append(f"serial={v.serial_number}: EPIC '{v.epic_number}' non-standard -> review")
            v.needs_review = True
            suspicious.append(v)
        # Low confidence always suspicious
        if v.confidence == "LOW" and v not in suspicious:
            suspicious.append(v)
        if v.epic_number:
            seen_epic[v.epic_number] += 1
        if v.serial_number is not None:
            seen_serial[v.serial_number] += 1

    for epic, n in seen_epic.items():
        if n > 1:
            issues.append(f"duplicate EPIC within page: {epic} x{n}")
    for s, n in seen_serial.items():
        if n > 1:
            issues.append(f"duplicate serial within page: {s} x{n}")

    # Serial gaps: flag only (legitimate gaps exist)
    serials = sorted(s for s in seen_serial if s is not None)
    if len(serials) >= 2:
        gaps = [b for a, b in zip(serials, serials[1:]) if b - a > 1]
        if gaps:
            issues.append(f"serial gaps detected (advisory, not an error): e.g. ...{gaps[:5]}")

    # Records-per-page sanity
    if len(parsed.voters) > 120:
        issues.append(f"unusually high records per page: {len(parsed.voters)}")
    return parsed, issues, suspicious


def light_schema_check(payload: dict) -> tuple[bool, str]:
    """Cheap pre-validation used inside the Gemini retry loop."""
    if not isinstance(payload, dict):
        return False, "payload is not an object"
    if "voters" not in payload or not isinstance(payload["voters"], list):
        return False, "missing 'voters' list"
    return True, "ok"


def cross_page_checks(all_voters: list[dict]) -> dict:
    """After full run: duplicates, gaps, per-page stats. Flags only."""
    epic_counts = Counter(v.get("epic_number") for v in all_voters if v.get("epic_number"))
    serial_counts = Counter(v.get("serial_number") for v in all_voters if v.get("serial_number") is not None)
    return {
        "duplicate_epics": {k: n for k, n in epic_counts.items() if n > 1},
        "duplicate_serials": {k: n for k, n in serial_counts.items() if n > 1},
        "total_records": len(all_voters),
    }
