"""Pydantic schemas — the contract for Gemini structured output + validation."""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class VoterRecord(BaseModel):
    serial_number: Optional[int] = Field(default=None, description="Serial number as printed")
    epic_number: Optional[str] = Field(default=None, description="EPIC ID, e.g. ABC1234567")
    name: Optional[str] = Field(default=None, description="Elector name in caps as printed")
    relation_type: Optional[str] = Field(default=None, description="FATHER/MOTHER/HUSBAND/WIFE/OTHER")
    relation_name: Optional[str] = Field(default=None, description="Name of father/mother/husband/etc.")
    house_number: Optional[str] = Field(default=None, description="House number, kept as string to preserve 0s/suffixes")
    age: Optional[int] = Field(default=None)
    gender: Optional[str] = Field(default=None, description="Male/Female/Other")
    confidence: Literal["HIGH", "MEDIUM", "LOW"] = "MEDIUM"
    needs_review: bool = False
    review_reason: Optional[str] = None

    @field_validator("epic_number", mode="before")
    @classmethod
    def _norm_epic(cls, v):
        if v is None:
            return None
        s = str(v).strip().upper().replace(" ", "")
        return s or None

    @field_validator("name", "relation_name", mode="before")
    @classmethod
    def _norm_name(cls, v):
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator("house_number", mode="before")
    @classmethod
    def _norm_house(cls, v):
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator("gender", mode="before")
    @classmethod
    def _norm_gender(cls, v):
        if v is None:
            return None
        s = str(v).strip()
        low = s.lower()
        # This roll uses Male / Female / Third Gender (see summary page).
        # Normalise Third Gender -> Other to match the VOTERS sheet vocabulary.
        if "third" in low:
            return "Other"
        s = s.capitalize()
        mapping = {
            "M": "Male", "Male": "Male",
            "F": "Female", "Female": "Female",
            "O": "Other", "Other": "Other",
            "T": "Other", "Transgender": "Other",
        }
        return mapping.get(s, s or None)

    @field_validator("relation_type", mode="before")
    @classmethod
    def _norm_relation(cls, v):
        if v is None:
            return None
        s = str(v).strip().upper()
        mapping = {
            "F": "FATHER", "FATHER": "FATHER", "F/O": "FATHER",
            "FATHER'S NAME": "FATHER",
            "M": "MOTHER", "MOTHER": "MOTHER", "M/O": "MOTHER",
            "MOTHER'S NAME": "MOTHER",
            "H": "HUSBAND", "HUSBAND": "HUSBAND", "H/O": "HUSBAND",
            "HUSBAND'S NAME": "HUSBAND",
            "W": "WIFE", "WIFE": "WIFE", "W/O": "WIFE",
            "OTHERS": "OTHER", "OTHER": "OTHER", "OTHERS:": "OTHER",
        }
        return mapping.get(s, s or None)


class PageVoters(BaseModel):
    """Expected Gemini payload for one page."""

    page_number: int
    voters: List[VoterRecord]


class PageResult(BaseModel):
    page_number: int
    status: Literal["COMPLETED", "REVIEW_REQUIRED", "FAILED"] = "COMPLETED"
    voters: List[VoterRecord] = Field(default_factory=list)
    issues: List[str] = Field(default_factory=list)
    error: Optional[str] = None
