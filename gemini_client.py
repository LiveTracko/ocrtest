"""Gemini integration via official `google-genai` SDK with retries."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

SYSTEM_RULES = """The image is the primary source of truth.
OCR is supporting information only.
Compare OCR with the image.
If OCR is wrong, correct it using the image.
Never guess.
If a value is unreadable, return null and mark it for review.
Never invent a voter.
Never merge two voters.
Never split one voter incorrectly."""

PAGE_PROMPT = """You extract voter records from an Indian electoral-roll page scan.

""" + SYSTEM_RULES + """

Page number: {page_number}

Supporting OCR text (may contain errors — verify every field against the image):
---
{ocr_text}
---

Return STRUCTURED JSON ONLY, exactly this shape, no markdown fences, no commentary:
{{"page_number": {page_number}, "voters": [{{"serial_number": 1, "epic_number": "ABC1234567", "name": "RAMESH KUMAR", "relation_type": "FATHER", "relation_name": "SURESH KUMAR", "house_number": "125", "age": 45, "gender": "Male", "confidence": "HIGH", "needs_review": false, "review_reason": null}}]}}

Rules:
- relation_type must be one of FATHER, MOTHER, HUSBAND, WIFE, OTHER (or null if unreadable).
- gender must be Male, Female or Other (or null if unreadable).
- confidence is HIGH only if every field of that voter is clearly readable; MEDIUM if minor doubt; LOW if any field is doubtful (then set needs_review=true and explain in review_reason).
- house_number and epic_number: copy EXACTLY as printed, do not "correct" spelling. Unreadable -> null + needs_review.
- age must be an integer or null.
- Do not drop records. Do not fabricate. Empty page -> {{"page_number": N, "voters": []}}.
"""


def _is_retryable_message(msg: str) -> bool:
    m = msg.lower()
    return any(k in m for k in ("429", "rate limit", "quota", "503", "500", "502",
                                "504", "timeout", "deadline", "temporarily", "overloaded",
                                "resource exhausted", "internal error"))


def _load_image_bytes(image_path: Path) -> tuple[bytes, str]:
    data = image_path.read_bytes()
    mime = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
    return data, mime


def call_gemini_page(image_path: Path, ocr_text: str, page_number: int,
                     *, model: str, api_key: str, max_retries: int = 3,
                     base_delay: float = 2.0,
                     validator: Optional[Callable[[dict], tuple[bool, str]]] = None) -> dict:
    """Call Gemini for one page with exponential backoff. Returns parsed dict.

    Raises RuntimeError after exhausting retries.
    """
    from google import genai
    from google.genai import types

    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is missing. Set it in .env.")

    client = genai.Client(api_key=api_key)
    prompt = PAGE_PROMPT.format(page_number=page_number,
                                ocr_text=(ocr_text or "")[:12000])
    img_bytes, mime = _load_image_bytes(image_path)

    last_err = "unknown"
    for attempt in range(1, max_retries + 1):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=[
                    types.Content(role="user", parts=[
                        types.Part.from_text(text=prompt),
                        types.Part.from_bytes(data=img_bytes, mime_type=mime),
                    ])
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.0,
                ),
            )
            raw = (resp.text or "").strip()
            if not raw:
                raise ValueError("empty response from Gemini")
            # Strip accidental fences
            if raw.startswith("```"):
                raw = raw.strip("`")
                if raw.lower().startswith("json"):
                    raw = raw[4:].strip()
            data = json.loads(raw)
            if not isinstance(data, dict) or "voters" not in data:
                raise ValueError("Gemini JSON missing 'voters' key")
            if validator is not None:
                ok, reason = validator(data)
                if not ok:
                    raise ValueError(f"schema/validation check failed: {reason}")
            log.info("Gemini ok page=%s attempt=%d voters=%d",
                     page_number, attempt, len(data.get("voters", [])))
            return data
        except Exception as exc:  # noqa: BLE001 — retry policy needs broad catch
            last_err = str(exc)
            retryable = _is_retryable_message(last_err) or isinstance(
                exc, (ValueError, json.JSONDecodeError, TimeoutError))
            log.warning("Gemini page=%s attempt=%d/%d failed: %s",
                        page_number, attempt, max_retries, last_err[:300])
            if attempt >= max_retries:
                break
            if not retryable and "validation" not in last_err.lower() \
                    and "schema" not in last_err.lower():
                # Non-retryable programming error — still honour max_retries budget
                pass
            delay = base_delay * (2 ** (attempt - 1))
            time.sleep(delay)
    raise RuntimeError(f"Gemini failed for page {page_number} after "
                       f"{max_retries} attempts: {last_err[:500]}")


VERIFY_PROMPT = """You verify ONE voter record against its cropped scan image.

""" + SYSTEM_RULES + """

Current extracted record (may contain errors):
{record_json}

Correct ONLY fields that clearly contradict the crop image. Unreadable -> null + needs_review=true + review_reason.
Return JSON ONLY for the single voter object, same fields, no commentary."""


def verify_record_crop(crop_image_path: Path, record: dict, *,
                       model: str, api_key: str, max_retries: int = 2,
                       base_delay: float = 2.0) -> Optional[dict]:
    """Second verification for suspicious records. Returns corrected dict or None."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    img_bytes, mime = _load_image_bytes(crop_image_path)
    prompt = VERIFY_PROMPT.format(record_json=json.dumps(record, ensure_ascii=False))
    for attempt in range(1, max_retries + 1):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=[types.Content(role="user", parts=[
                    types.Part.from_text(text=prompt),
                    types.Part.from_bytes(data=img_bytes, mime_type=mime),
                ])],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", temperature=0.0),
            )
            raw = (resp.text or "").strip()
            if raw.startswith("```"):
                raw = raw.strip("`")
                if raw.lower().startswith("json"):
                    raw = raw[4:].strip()
            data = json.loads(raw)
            if isinstance(data, dict) and "voters" in data and isinstance(data["voters"], list) and data["voters"]:
                return data["voters"][0]
            return data
        except Exception as exc:
            log.warning("Second verification attempt %d failed: %s", attempt, str(exc)[:200])
            time.sleep(base_delay * attempt)
    return None
