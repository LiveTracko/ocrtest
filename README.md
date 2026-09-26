# Voter Roll Extractor — PDF → Excel (image-first, review-safe)

Extracts voter records from large electoral-roll PDFs (30+ pages, 2000+ records)
with **maximum practical accuracy — never 100%, never guessed**.

Pipeline per page:

```text
PDF → page PDF → PNG (300 DPI) → preprocess → PaddleOCR (supporting)
→ Gemini Vision (image = truth) → Pydantic validation → retry?
→ results/page_XXX.json → Excel + review + report
```

Uncertain fields → `needs_review=true`, saved under `review/`, never fabricated.

---

## 1. Requirements

- Windows 10/11, Python **3.11+** (tested 3.11 / 3.12)
- A Google Gemini API key ([aistudio.google.com](https://aistudio.google.com))
- ~2 GB free disk (page images + OCR), internet for Gemini calls

## 2. Setup (PowerShell)

```powershell
cd B:\piyush\ocrtest

py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install --upgrade pip
pip install -r requirements.txt
```

> **PaddleOCR / PaddlePaddle on Windows:** the CPU wheel is large.
> If `paddlepaddle` fails, install it first per
> <https://www.paddlepaddle.org.cn/en/install/quick> (Windows / CPU / pip),
> then re-run `pip install -r requirements.txt`.
> OCR is *supporting only* — the app still runs (degraded) if OCR import fails.

## 3. Configuration

```powershell
Copy-Item .env.example .env
notepad .env
```

`.env`:

```env
GEMINI_API_KEY=YOUR_KEY_HERE
GEMINI_MODEL=gemini-2.0-flash
PDF_RENDER_DPI=300
MAX_GEMINI_RETRIES=3
ENABLE_SECOND_VERIFICATION=true
```

- Never hardcode the key; never commit `.env` (see `.gitignore`).
- `GEMINI_MODEL` is fully configurable — any model your key supports via the
  official `google-genai` SDK (spec example `gemini-3.8-flash` also works if
  your account has it; `gemini-2.0-flash` is the tested default).
- `PDF_RENDER_DPI` ≈ 300 recommended. Higher = better small-print accuracy, slower.
- Optional: `INPUT_PDF`, `MIN_AGE`/`MAX_AGE` (default 1–120), `RETRY_BASE_DELAY_SEC`.

Place the roll:

```powershell
Copy-Item C:\path\to\roll.pdf input\voter_roll.pdf
```

The original PDF is **never modified** — per-page copies go to `pages/`.

## 4. Folder structure

```text
main.py  config.py  models.py  pdf_processor.py  image_preprocess.py
ocr_engine.py  gemini_client.py  validator.py  checkpoint.py
review_manager.py  excel_writer.py  utils.py
input/ pages/ images/ preprocessed/ ocr/ results/ review/
state/ logs/ output/ tests/
```

Missing folders are created automatically on every run (`ensure_dirs()`).

## 5. Testing workflow (mandatory order)

```powershell
# 1. config/PDF/OCR/Gemini checks only — no processing, no charges
python main.py --dry-run

# 2. one-page test — verify these 5 files exist and look right
python main.py --pages 1
#   images/page_001.png
#   preprocessed/page_001.png
#   ocr/page_001.txt (+ ocr/page_001.json boxes/confidence)
#   results/page_001.json
#   output/voters.xlsx

# 3. small batch
python main.py --pages 1-3

# 4. full run (sequential, resumable)
python main.py

# 5. retry only failed/review pages
python main.py --retry-failed
```

## 6. Resume behaviour

- Every page result is written **immediately** to `results/page_XXX.json`.
- Progress in `state/progress.json` (`completed / failed / review`).
- Crash after page 15 → restart `python main.py` skips 1–15, continues at 16.
- `--force` reprocesses even cached pages; default uses the cache.

## 7. Retry behaviour

Retries on: timeout, 429/5xx, invalid JSON, schema error, empty response,
validation failure. Exponential backoff: base × 2^(attempt-1),
default 2 s → 4 s → 8 s, `MAX_GEMINI_RETRIES=3` (configurable).
Exhausted pages are marked `FAILED`/`REVIEW_REQUIRED` — the job **continues**.

## 8. Excel output

- `output/voters.xlsx` — sheet `VOTERS`: Page/Serial/EPIC/Name/Relation Type/
  Relation Name/House/Age/Gender/Confidence/Validation Status/Review Reason.
  Headers styled, autofilter, frozen row 1, sensible widths,
  EPIC + House forced to **text** (`@`) so leading zeros survive.
- `output/review.xlsx` — sheet `REVIEW`: Page/Serial/EPIC/Name/Issue/Status.
- `output/processing_report.json` — real counts, e.g.:

```json
{
  "total_pages": 33, "completed_pages": 30, "review_pages": 3,
  "failed_pages": 0, "total_records": 2000, "validated_records": 1980,
  "review_records": 20, "duplicate_epics": 0, "duplicate_serials": 0
}
```

## 9. Accuracy rules (enforced in prompt + code)

```text
IMAGE > OCR. OCR is supporting only. Never guess. Never fabricate.
Never silently discard or accept invalid data. Never overwrite source PDF.
Never lose completed pages. Uncertain → needs_review + review/ bundle.
```

Layers: OCR + image verification + structured JSON + Pydantic +
business rules + cross-page checks + retry + 2nd verification (suspicious only) + manual review.

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| `GEMINI_API_KEY missing` | copy `.env.example` → `.env`, paste key |
| `429 / quota` | wait, lower pace (sequential is default), retry `--retry-failed` |
| `PaddleOCR import failed` | install CPU paddlepaddle manually; app continues with image-only Gemini |
| `Input PDF not found` | put file at `input/voter_roll.pdf` or pass `--input <path>` |
| Garbled small print | raise `PDF_RENDER_DPI=400` in `.env`, `--force --pages N` |
| `invalid JSON` retries | usually transient; check `logs/processing.log`, retry failed |

Logs: `logs/processing.log` (startup, per-page, OCR/Gemini status, retries,
validation errors, final stats). API key is redacted; voter PII is
count-summarised, never dumped wholesale.

## 11. Privacy / security

- Electoral data is personal data: keep `input/`, `output/`, `review/` local,
  do not commit them (gitignored), delete/redact when done.
- Gemini API calls transmit page images + OCR text to Google — process only
  data you are authorised to upload; review your organisation's policy.
- Rotate a leaked key immediately in AI Studio.

## 12. Tests

```powershell
pytest -q
```

Covers Pydantic normalisation, age/gender/EPIC flagging, page-args parsing,
report framing — no network, no API key needed.
