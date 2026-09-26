"""Voter-roll extractor — CLI entry point.

Usage (PowerShell):
    python main.py --dry-run
    python main.py --pages 1
    python main.py --pages 1-3
    python main.py
    python main.py --retry-failed
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import checkpoint
import excel_writer
import gemini_client
import image_preprocess
import ocr_engine
import pdf_processor
import review_manager
import utils
import validator
from config import SETTINGS, ensure_dirs

log = logging.getLogger("voter_extractor")


# ---------------------------------------------------------------- logging
def setup_logging() -> Path:
    ensure_dirs()
    log_path = SETTINGS.logs_dir / "processing.log"
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    fh = logging.FileHandler(str(log_path), encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(ch)
    return log_path


def _redact(msg: str) -> str:
    # Never log API key
    key = SETTINGS.gemini_api_key
    if key and len(key) > 4 and key in msg:
        return msg.replace(key, "***REDACTED***")
    return msg


# ---------------------------------------------------------------- salvage helper
def _salvage_raw_voters(gemini_data: dict | None, err: str) -> list[dict]:
    """Best-effort rescue of raw AI rows when Pydantic validation rejects a page.

    Returns a list of voter-like dicts (flagged needs_review) — never raises.
    This guarantees AI data that cost time/money to obtain is never dropped
    and always reaches voters.xlsx for manual review.
    """
    try:
        if not isinstance(gemini_data, dict):
            return []
        raw = gemini_data.get("voters")
        if not isinstance(raw, list):
            return []
        out: list[dict] = []
        for rv in raw:
            if not isinstance(rv, dict):
                continue
            row = {
                "serial_number": rv.get("serial_number"),
                "epic_number": (str(rv.get("epic_number")).strip().upper()
                                if rv.get("epic_number") not in (None, "") else None),
                "name": rv.get("name"),
                "relation_type": rv.get("relation_type"),
                "relation_name": rv.get("relation_name"),
                "house_number": (str(rv.get("house_number")).strip()
                                 if rv.get("house_number") not in (None, "") else None),
                "age": rv.get("age") if isinstance(rv.get("age"), int) else None,
                "gender": rv.get("gender"),
                "confidence": rv.get("confidence") if rv.get("confidence") in (
                    "HIGH", "MEDIUM", "LOW") else "LOW",
                "needs_review": True,
                "review_reason": (f"raw AI row kept after schema error: {err}"[:500]),
            }
            # coerce numeric strings: age "45" -> 45
            if row["age"] is None and isinstance(rv.get("age"), str):
                try:
                    row["age"] = int(str(rv.get("age")).strip())
                except (ValueError, TypeError):
                    pass
            try:
                if row["serial_number"] is not None:
                    row["serial_number"] = int(row["serial_number"])
            except (ValueError, TypeError):
                pass
            out.append(row)
        return out
    except Exception:
        return []


# ---------------------------------------------------------------- page job
def process_page(page_number: int, input_pdf: Path, force: bool = False) -> dict:
    """Full pipeline for one page. Always saves result immediately. Never raises."""
    tag = f"{page_number:03d}"
    if not force:
        cached = checkpoint.load_page_result(SETTINGS.results_dir, page_number)
        if cached and cached.get("status") in ("COMPLETED", "REVIEW_REQUIRED"):
            log.info("Page %s: skipping (cached %s)", tag, cached.get("status"))
            return cached

    log.info("Page %s: start", tag)
    issues: list[str] = []
    try:
        # 1. PDF -> page PDF + PNG
        try:
            _, image_path = pdf_processor.split_and_render_page(
                input_pdf, page_number, SETTINGS.pages_dir,
                SETTINGS.images_dir, dpi=SETTINGS.pdf_render_dpi)
        except Exception as exc:
            err = f"PDF split/render failed: {exc}"
            log.error(_redact(f"Page {tag}: {err}"))
            payload = {"page_number": page_number, "status": "FAILED",
                       "voters": [], "issues": [err], "error": str(exc)[:500]}
            checkpoint.save_page_result(SETTINGS.results_dir, page_number, payload)
            checkpoint.mark(SETTINGS.state_dir, page_number, "failed")
            save_interim_excel()
            return payload

        # 2. Preprocess
        pre_path = SETTINGS.preprocessed_dir / f"page_{tag}.png"
        try:
            image_preprocess.preprocess_image(image_path, pre_path)
        except Exception as exc:
            log.warning("Page %s: preprocess failed (%s), using raw image", tag, exc)
            issues.append(f"preprocess fallback: {exc}")
            pre_path = image_path

        # 3. OCR (supporting info only)
        txt_path = SETTINGS.ocr_dir / f"page_{tag}.txt"
        ocr_json_path = SETTINGS.ocr_dir / f"page_{tag}.json"
        ocr_out = ocr_engine.run_ocr(pre_path, txt_path, ocr_json_path)
        ocr_text = ocr_out.get("text", "")
        if not ocr_out.get("ok"):
            issues.append(f"OCR degraded: {ocr_out.get('error', 'unknown')}")
        log.info("Page %s: OCR done (ok=%s, chars=%d)",
                 tag, ocr_out.get("ok"), len(ocr_text))

        # 4. Gemini extraction with retries
        try:
            gemini_data = gemini_client.call_gemini_page(
                image_path, ocr_text, page_number,
                model=SETTINGS.gemini_model, api_key=SETTINGS.gemini_api_key,
                max_retries=SETTINGS.max_gemini_retries,
                base_delay=SETTINGS.retry_base_delay_sec,
                validator=validator.light_schema_check)
        except Exception as exc:
            err = f"Gemini failed: {exc}"
            log.error(_redact(f"Page {tag}: {err}"))
            payload = {"page_number": page_number, "status": "FAILED",
                       "voters": [], "issues": issues + [err],
                       "error": str(exc)[:800]}
            checkpoint.save_page_result(SETTINGS.results_dir, page_number, payload)
            checkpoint.mark(SETTINGS.state_dir, page_number, "failed")
            save_interim_excel()
            return payload

        # 5. Validation
        parsed, v_issues, suspicious = validator.validate_page_payload(
            gemini_data, expected_page=page_number,
            min_age=SETTINGS.min_age, max_age=SETTINGS.max_age)
        issues.extend(v_issues)
        if parsed is None:
            err = f"validation failed: {'; '.join(v_issues) or 'schema error'}"
            log.error("Page %s: %s", tag, err)
            # DATA-SAFE: never drop AI output. Keep raw Gemini rows (flagged
            # for review) so they still land in voters.xlsx via the
            # raw_gemini fallback in excel_writer.build_dataframes, plus keep
            # a best-effort salvaged copy in `voters` itself.
            salvaged = _salvage_raw_voters(gemini_data, err)
            status = "REVIEW_REQUIRED" if salvaged else "FAILED"
            payload = {"page_number": page_number, "status": status,
                       "voters": salvaged, "issues": issues + (
                           [f"kept {len(salvaged)} raw AI row(s) for review"]
                           if salvaged else []),
                       "error": err[:800],
                       "raw_gemini": gemini_data}
            checkpoint.save_page_result(SETTINGS.results_dir, page_number, payload)
            checkpoint.mark(SETTINGS.state_dir, page_number,
                            "review" if salvaged else "failed")
            try:
                review_manager.save_review_artifacts(
                    SETTINGS.review_dir, page_number, image_path, ocr_text,
                    salvaged, issues)
            except Exception as exc:
                log.warning("Page %s: review bundle failed: %s", tag, exc)
            # Excel must reflect this page immediately, even if we stop now.
            save_interim_excel()
            return payload

        voters = [v.model_dump() for v in parsed.voters]

        # 6. Optional second verification for suspicious records only
        if SETTINGS.enable_second_verification and suspicious:
            log.info("Page %s: second verification for %d suspicious record(s)",
                     tag, len(suspicious))
            try:
                boxes = ocr_out.get("boxes", [])
            except Exception:
                boxes = []
            for v in voters:
                if not v.get("needs_review"):
                    continue
                # Best-effort crop: whole page if no box mapping available
                crop_path = SETTINGS.review_dir / f"page_{tag}" / f"serial_{v.get('serial_number')}.png"
                cropped = None
                if boxes:
                    # naive: first box whose text overlaps serial/name — else skip crop
                    cropped = review_manager.try_crop_record(image_path, boxes[0], crop_path) \
                        if boxes else None
                target = cropped or image_path
                try:
                    corrected = gemini_client.verify_record_crop(
                        target, v, model=SETTINGS.gemini_model,
                        api_key=SETTINGS.gemini_api_key)
                    if isinstance(corrected, dict):
                        for k in ("serial_number", "epic_number", "name", "relation_type",
                                  "relation_name", "house_number", "age", "gender",
                                  "confidence", "needs_review", "review_reason"):
                            if k in corrected:
                                v[k] = corrected[k]
                except Exception as exc:
                    log.warning("Page %s serial=%s: 2nd verification failed: %s",
                                tag, v.get("serial_number"), str(exc)[:200])
            # re-run light flagging after correction (no silent drops)
            issues.append(f"second verification applied to {len([x for x in voters if x.get('needs_review')])} record(s)")

        needs_review_any = any(v.get("needs_review") for v in voters)
        status = "REVIEW_REQUIRED" if (needs_review_any or any(
            "duplicate" in i.lower() or "missing" in i.lower() for i in issues)) and voters else "COMPLETED"
        if not voters:
            # Empty pages are legitimate in this roll (cover p.1, maps/photos p.2,
            # summary p.27). Flag for a glance, don't call it a failure.
            status = "REVIEW_REQUIRED"
            issues.append("no voter records on page (cover/map/summary?) — verify against image")

        # 7. Review artifacts for uncertain pages
        if status in ("REVIEW_REQUIRED", "FAILED"):
            try:
                review_manager.save_review_artifacts(
                    SETTINGS.review_dir, page_number, image_path, ocr_text, voters, issues)
            except Exception as exc:
                log.warning("Page %s: review bundle failed: %s", tag, exc)

        payload = {"page_number": page_number, "status": status,
                   "voters": voters, "issues": issues}
        checkpoint.save_page_result(SETTINGS.results_dir, page_number, payload)
        checkpoint.mark(SETTINGS.state_dir, page_number,
                        "completed" if status == "COMPLETED" else
                        ("review" if status == "REVIEW_REQUIRED" else "failed"))
        # Avoid PII dump: log counts only
        log.info("Page %s: %s voters=%d review=%d issues=%d",
                 tag, status, len(voters),
                 sum(1 for v in voters if v.get("needs_review")), len(issues))
        # DATA-SAFE: Excel always mirrors results/*.json after every page.
        save_interim_excel()
        return payload

    except Exception as exc:  # last-resort guard: one page never kills the job
        log.exception("Page %s: unexpected failure: %s", tag, str(exc)[:500])
        payload = {"page_number": page_number, "status": "FAILED",
                   "voters": [], "issues": issues + [f"unexpected: {exc}"],
                   "error": str(exc)[:800]}
        try:
            checkpoint.save_page_result(SETTINGS.results_dir, page_number, payload)
            checkpoint.mark(SETTINGS.state_dir, page_number, "failed")
        except Exception:
            pass
        # Even failed pages must be visible in Excel immediately.
        try:
            save_interim_excel()
        except Exception:
            pass
        return payload


# ---------------------------------------------------------------- dry run
def dry_run(input_pdf: Path) -> int:
    print("=== DRY RUN ===")
    ok = True

    # config
    if not SETTINGS.gemini_api_key or SETTINGS.gemini_api_key == "YOUR_KEY_HERE":
        print("[FAIL] GEMINI_API_KEY missing — copy .env.example to .env and set it")
        ok = False
    else:
        print(f"[OK] GEMINI_API_KEY present (len={len(SETTINGS.gemini_api_key)}, redacted)")
    print(f"[INFO] GEMINI_MODEL={SETTINGS.gemini_model}")
    print(f"[INFO] PDF_RENDER_DPI={SETTINGS.pdf_render_dpi} "
          f"MAX_GEMINI_RETRIES={SETTINGS.max_gemini_retries} "
          f"ENABLE_SECOND_VERIFICATION={SETTINGS.enable_second_verification}")

    # pdf
    if not input_pdf.exists():
        print(f"[FAIL] Input PDF not found: {input_pdf}")
        print(f"       Place your roll at {input_pdf} (see README)")
        ok = False
    else:
        try:
            n = pdf_processor.get_total_pages(input_pdf)
            print(f"[OK] PDF readable: {input_pdf} ({n} pages)")
        except Exception as exc:
            print(f"[FAIL] Cannot read PDF: {exc}")
            ok = False

    # OCR import
    try:
        import paddleocr  # noqa: F401
        print("[OK] PaddleOCR importable")
    except Exception as exc:
        print(f"[WARN] PaddleOCR import failed (OCR will degrade, Gemini still works): {exc}")

    # cv2 / fitz / genai
    for mod, label in (("fitz", "PyMuPDF"), ("cv2", "OpenCV"),
                       ("google.genai", "google-genai SDK"),
                       ("pydantic", "Pydantic"), ("pandas", "pandas")):
        try:
            __import__(mod)
            print(f"[OK] {label}")
        except Exception as exc:
            print(f"[FAIL] {label} missing: {exc}")
            ok = False

    # Gemini client init (no charge — no API call)
    try:
        from google import genai
        if SETTINGS.gemini_api_key and SETTINGS.gemini_api_key != "YOUR_KEY_HERE":
            genai.Client(api_key=SETTINGS.gemini_api_key)
            print("[OK] Gemini client initialises (no API call made)")
        else:
            print("[SKIP] Gemini client init (no key)")
    except Exception as exc:
        print(f"[FAIL] Gemini client init: {exc}")
        ok = False

    ensure_dirs()
    print(f"[OK] Directories ensured: {[p.name for p in ensure_dirs.__self__] if False else 'pages images preprocessed ocr results review state logs output input'}")
    print("DRY RUN " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


# ---------------------------------------------------------------- finalize
def _done_pages_from_results() -> list[int]:
    """Page numbers that have a result file (i.e. AI data safely on disk)."""
    done = []
    for f in SETTINGS.results_dir.glob("page_*.json"):
        try:
            done.append(int(f.stem.split("_")[1]))
        except (IndexError, ValueError):
            continue
    return sorted(done)


def _effective_voters(payload: dict) -> list[dict]:
    """Voters as they will appear in Excel (validated + salvaged raw AI rows)."""
    voters = payload.get("voters") or []
    if voters:
        return voters
    try:
        return excel_writer._salvaged_rows(payload)
    except Exception:
        return []


def finalize(pages: list[int], partial: bool = False, quiet: bool = False) -> dict:
    payloads = utils.collect_results_for_report(SETTINGS.results_dir, pages)
    all_voters: list[dict] = []
    for p in payloads:
        for v in _effective_voters(p):
            row = dict(v)
            row["_page"] = p.get("page_number")
            all_voters.append(row)

    x = validator.cross_page_checks(all_voters)

    # per-page stats (use effective voters so salvaged AI rows count too)
    per_page = {p.get("page_number"): len(_effective_voters(p)) for p in payloads}
    avg = (sum(per_page.values()) / len(per_page)) if per_page else 0
    odd_pages = [pg for pg, c in per_page.items() if avg and (c > avg * 2 or c < avg * 0.3)]

    completed = sum(1 for p in payloads if p.get("status") == "COMPLETED")
    review_p = sum(1 for p in payloads if p.get("status") == "REVIEW_REQUIRED")
    failed = sum(1 for p in payloads if p.get("status") == "FAILED")
    review_recs = sum(1 for v in all_voters if v.get("needs_review"))
    validated = sum(1 for v in all_voters if not v.get("needs_review"))

    stats = {
        "total_pages": len(pages),
        "completed_pages": completed,
        "review_pages": review_p,
        "failed_pages": failed,
        "failed_page_numbers": sorted(p.get("page_number") for p in payloads if p.get("status") == "FAILED"),
        "review_page_numbers": sorted(p.get("page_number") for p in payloads if p.get("status") == "REVIEW_REQUIRED"),
        "total_records": len(all_voters),
        "validated_records": validated,
        "review_records": review_recs,
        "duplicate_epics": len(x["duplicate_epics"]),
        "duplicate_epic_values": dict(list(x["duplicate_epics"].items())[:50]),
        "duplicate_serials": len(x["duplicate_serials"]),
        "duplicate_serial_values": dict(list(x["duplicate_serials"].items())[:50]),
        "records_per_page": per_page,
        "average_records_per_page": round(avg, 2),
        "odd_pages": odd_pages,
        "model": SETTINGS.gemini_model,
        "partial": partial,
        "note": ("PARTIAL save — job did not finish; re-run to complete. "
                 if partial else "") + ("Accuracy is best-effort, never 100%. "
                 "Uncertain records are flagged for review, never guessed."),
    }

    voters_xlsx, review_xlsx = excel_writer.write_excels(SETTINGS.output_dir, payloads)
    report_path = excel_writer.write_report(SETTINGS.output_dir, stats)
    log.info("Excel: %s | Review: %s | Report: %s", voters_xlsx, review_xlsx, report_path)
    if not quiet:
        print(f"\nWrote {voters_xlsx}\nWrote {review_xlsx}\nWrote {report_path}")
        print(json.dumps({k: stats[k] for k in (
            "total_pages", "completed_pages", "review_pages", "failed_pages",
            "total_records", "validated_records", "review_records",
            "duplicate_epics", "duplicate_serials")}, indent=2))
    return stats


def save_interim_excel() -> None:
    """Rebuild Excel + report from whatever page results exist RIGHT NOW.

    Called after every page, so a crash/stop never loses AI data already
    extracted — output/voters.xlsx always holds everything done so far.
    Never raises (result JSONs are the real store; Excel is a view).
    """
    try:
        done = _done_pages_from_results()
        if not done:
            return
        finalize(done, partial=True, quiet=True)
        log.info("Interim Excel saved (%d page(s) so far) -> output/voters.xlsx", len(done))
    except Exception as exc:
        log.warning("Interim Excel save failed (results/*.json are still safe): %s",
                    str(exc)[:200])


# ---------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Electoral-roll PDF -> Excel (image-first, review-safe)")
    ap.add_argument("--dry-run", action="store_true", help="Check config/PDF/OCR/Gemini without processing")
    ap.add_argument("--pages", default=None, help="e.g. '1' or '1-3' or '1,3,5' (1-based)")
    ap.add_argument("--retry-failed", action="store_true", help="Retry pages marked FAILED/review with no cache")
    ap.add_argument("--input", default=None, help="Override input PDF path")
    ap.add_argument("--force", action="store_true", help="Reprocess even cached pages")
    return ap


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    log.info("Startup: model=%s dpi=%s retries=%s 2nd_verify=%s",
             SETTINGS.gemini_model, SETTINGS.pdf_render_dpi,
             SETTINGS.max_gemini_retries, SETTINGS.enable_second_verification)
    args = build_parser().parse_args(argv)
    input_pdf = Path(args.input) if args.input else Path(SETTINGS.input_pdf)

    if args.dry_run:
        return dry_run(input_pdf)

    if not input_pdf.exists():
        log.error("Input PDF not found: %s", input_pdf)
        print(f"ERROR: input PDF not found: {input_pdf}\nPlace it there or pass --input <path>. See README.")
        return 2

    try:
        total = pdf_processor.get_total_pages(input_pdf)
    except Exception as exc:
        log.error("Cannot read PDF %s: %s", input_pdf, exc)
        return 2
    log.info("PDF %s has %d pages", input_pdf, total)

    if args.pages:
        try:
            pages = utils.parse_pages_arg(args.pages, total)
        except Exception as exc:
            print(f"ERROR: --pages: {exc}")
            return 2
    else:
        pages = list(range(1, total + 1))

    force = bool(args.force or args.retry_failed)
    if args.retry_failed:
        # only pages that are failed/review/missing
        todo = []
        for p in pages:
            cached = checkpoint.load_page_result(SETTINGS.results_dir, p)
            if not cached or cached.get("status") in ("FAILED", "REVIEW_REQUIRED"):
                todo.append(p)
        if not todo:
            print("Nothing to retry — no failed/review pages in scope.")
            return 0
        pages = todo
        log.info("Retrying %d failed/review pages: %s", len(pages), pages[:10])

    # Seed progress total
    prog = checkpoint.load_progress(SETTINGS.state_dir)
    prog["total_pages"] = total
    checkpoint.save_progress(SETTINGS.state_dir, prog)

    # --- Crash-safe loop: every page immediately lands in results/*.json
    # AND in output/voters.xlsx (via save_interim_excel inside process_page).
    # Ctrl+C / unexpected errors below still flush a PARTIAL Excel first,
    # so AI data already collected is never lost.
    done_pages: list[int] = []
    try:
        for i, p in enumerate(pages, 1):
            log.info("Progress %d/%d — page %d", i, len(pages), p)
            process_page(p, input_pdf, force=force)  # never raises; failures continue
            done_pages.append(p)
            # Belt-and-suspenders: process_page already saves interim Excel,
            # but re-save here in case it was skipped (e.g. cached page).
            try:
                save_interim_excel()
            except Exception:
                pass
    except KeyboardInterrupt:
        log.warning("Interrupted by user (Ctrl+C) after %d page(s) — saving partial Excel…",
                    len(done_pages))
        try:
            save_interim_excel()
        except Exception:
            pass
        print(f"\nSTOPPED. Partial data is SAFE in output/voters.xlsx "
              f"({len(_done_pages_from_results())} page(s) saved). "
              f"Re-run to resume (cached pages are skipped).")
        return 130
    except Exception as exc:  # noqa: BLE001 — must save partial Excel no matter what
        log.exception("Fatal error in main loop: %s", str(exc)[:500])
        try:
            save_interim_excel()
        except Exception:
            pass
        print(f"\nERROR STOPPED: {exc}\nPartial data is SAFE in output/voters.xlsx "
              f"({len(_done_pages_from_results())} page(s) saved). Fix the issue and re-run to resume.")
        return 1

    try:
        stats = finalize(pages)
    except Exception as exc:  # Excel write itself failed — results/*.json still safe
        log.exception("Final Excel write failed: %s", str(exc)[:500])
        try:
            save_interim_excel()
        except Exception:
            pass
        print(f"\nERROR writing final Excel: {exc}\n"
              f"Per-page AI data is SAFE in results/*.json. Re-run to rebuild Excel.")
        return 1
    if stats["failed_pages"]:
        log.warning("Failed pages: %s", stats["failed_page_numbers"])
    if stats["review_page_numbers"]:
        log.warning("Review pages: %s", stats["review_page_numbers"])
    log.info("Done: %d records (%d review)", stats["total_records"], stats["review_records"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
