"""Central configuration. Never hardcode secrets — everything comes from .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

# NOTE: spec .env example uses GEMINI_MODEL=gemini-3.8-flash.
# The model name is fully configurable — any Gemini model supported by
# the `google-genai` SDK works. Default below is a widely available one;
# override via .env.


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    pdf_render_dpi: int = _get_int("PDF_RENDER_DPI", 300)
    max_gemini_retries: int = _get_int("MAX_GEMINI_RETRIES", 3)
    enable_second_verification: bool = _get_bool("ENABLE_SECOND_VERIFICATION", True)
    input_pdf: str = os.getenv("INPUT_PDF", "input/voter_roll.pdf")
    min_age: int = _get_int("MIN_AGE", 1)
    max_age: int = _get_int("MAX_AGE", 120)
    retry_base_delay_sec: float = float(os.getenv("RETRY_BASE_DELAY_SEC", "2"))

    # Directories (relative to project root)
    pages_dir: Path = BASE_DIR / "pages"
    images_dir: Path = BASE_DIR / "images"
    preprocessed_dir: Path = BASE_DIR / "preprocessed"
    ocr_dir: Path = BASE_DIR / "ocr"
    results_dir: Path = BASE_DIR / "results"
    review_dir: Path = BASE_DIR / "review"
    state_dir: Path = BASE_DIR / "state"
    logs_dir: Path = BASE_DIR / "logs"
    output_dir: Path = BASE_DIR / "output"
    input_dir: Path = BASE_DIR / "input"


SETTINGS = Settings()

ALL_DIRS = [
    SETTINGS.pages_dir,
    SETTINGS.images_dir,
    SETTINGS.preprocessed_dir,
    SETTINGS.ocr_dir,
    SETTINGS.results_dir,
    SETTINGS.review_dir,
    SETTINGS.state_dir,
    SETTINGS.logs_dir,
    SETTINGS.output_dir,
    SETTINGS.input_dir,
]


def ensure_dirs() -> None:
    for d in ALL_DIRS:
        d.mkdir(parents=True, exist_ok=True)
    # placeholders so empty folders survive in git
    for d in ALL_DIRS:
        keep = d / ".gitkeep"
        if not keep.exists():
            try:
                keep.touch(exist_ok=True)
            except OSError:
                pass
