"""Per-job pipeline runner for the web server.

Why this file exists:
  main.py uses GLOBAL folders (results/, output/, state/) + a single
  input PDF. That is fine for one desktop user but breaks with 5+
  browser users (they would overwrite each other's Excel + page cache).

  This module reuses the SAME low-level steps (pdf_processor,
  image_preprocess, ocr_engine, gemini_client, validator,
  review_manager, excel_writer, checkpoint) but with PER-JOB folders:

    jobs/<job_id>/input.pdf
    jobs/<job_id>/{pages,images,preprocessed,ocr,results,review,state,output}/
    jobs/<job_id>/meta.json   (status/progress for polling)
    jobs/<job_id>/job.log     (tail shown in browser)

Pipeline logic itself is unchanged — only the directories differ.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
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
from config import SETTINGS

log = logging.getLogger("web_jobs")

BASE_DIR = Path(__file__).resolve().parent
JOBS_ROOT = BASE_DIR / "jobs"

_lock = threading.Lock()


# ---------------------------------------------------------------- dirs/meta
def job_root(job_id: str) -> Path:
    return JOBS_ROOT / job_id


def job_dirs(job_id: str) -> dict[str, Path]:
    r = job_root(job_id)
    return {
        "root": r,
        "pages": r / "pages",
        "images": r / "images",
        "preprocessed": r / "preprocessed",
        "ocr": r / "ocr",
        "results": r / "results",
        "review": r / "review",
        "state": r / "state",
        "output": r / "output",
        "logs": r / "logs",
    }


def _meta_path(job_id: str) -> Path:
    return job_root(job_id) / "meta.json"


def _log_path(job_id: str) -> Path:
    return job_root(job_id) / "job.log"


def load_meta(job_id: str) -> dict | None:
    p = _meta_path(job_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_meta(job_id: str, meta: dict) -> None:
    meta["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    checkpoint._atomic_write_text(_meta_path(job_id), json.dumps(meta, indent=2))


def append_log(job_id: str, msg: str) -> None:
    try:
        lp = _log_path(job_id)
        lp.parent.mkdir(parents=True, exist_ok=True)
        with open(lp, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} | {msg}\n")
    except OSError:
        pass
    # also to server log (Render dashboard)
    log.info("job %s: %s", job_id[:8], msg)


def list_jobs() -> list[dict]:
    out: list[dict] = []
    if not JOBS_ROOT.exists():
        return out
    for child in sorted(JOBS_ROOT.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
        if not child.is_dir():
            continue
        meta = load_meta(child.name)
        if meta:
            out.append(meta)
    return out


# ---------------------------------------------------------------- create
def create_job(original_name: str, from_page: int | None, to_page: int | None,
               force: bool, mode: str) -> dict:
    """Create job folder + meta. Caller must already save input.pdf into it."""
    job_id = uuid.uuid4().hex[:12]
    dirs = job_dirs(job_id)
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    meta = {
        "job_id": job_id,
        "filename": original_name,
        "from_page": from_page,
        "to_page": to_page,
        "force": bool(force),
        "mode": mode,  # normal | dry-run | retry-failed
        "status": "queued",  # queued | running | done | failed
        "total_pages": 0,
        "pages": [],
        "done_pages": [],
        "failed_pages": [],
        "review_pages": [],
        "total_records": 0,
        "review_records": 0,
        "error": None,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    save_meta(job_id, meta)
    append_log(job_id, f"Job created for {original_name} (mode={mode})")
    return meta


# ---------------------------------------------------------------- salvage (same as main.py)
def _salvage_raw_voters(gemini_data: dict | None, err: str) -> list[dict]:
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


def _effective_voters(payload: dict) -> list[dict]:
    voters = payload.get("voters") or []
    if voters:
        return voters
    try:
        return excel_writer._salvaged_rows(payload)
    except Exception:
        return []


# ---------------------------------------------------------------- one page (mirrors main.process_page)
def process_job_page(job_id: str, page_number: int, input_pdf: Path,
                     dirs: dict[str, Path], force: bool = False) -> dict:
    tag = f"{page_number:03d}"
    if not force:
        cached = checkpoint.load_page_result(dirs["results"], page_number)
        if cached and cached.get("status") in ("COMPLETED", "REVIEW_REQUIRED"):
            append_log(job_id, f"Page {tag}: skipping (cached {cached.get('status')})")
            return cached

    append_log(job_id, f"Page {tag}: start")
    issues: list[str] = []
    try:
        try:
            _, image_path = pdf_processor.split_and_render_page(
                input_pdf, page_number, dirs["pages"],
                dirs["images"], dpi=SETTINGS.pdf_render_dpi)
        except Exception as exc:
            err = f"PDF split/render failed: {exc}"
            payload = {"page_number": page_number, "status": "FAILED",
                       "voters": [], "issues": [err], "error": str(exc)[:500]}
            checkpoint.save_page_result(dirs["results"], page_number, payload)
            checkpoint.mark(dirs["state"], page_number, "failed")
            save_interim_excel(job_id, dirs)
            return payload

        pre_path = dirs["preprocessed"] / f"page_{tag}.png"
        try:
            image_preprocess.preprocess_image(image_path, pre_path)
        except Exception as exc:
            issues.append(f"preprocess fallback: {exc}")
            pre_path = image_path

        txt_path = dirs["ocr"] / f"page_{tag}.txt"
        ocr_json_path = dirs["ocr"] / f"page_{tag}.json"
        ocr_out = ocr_engine.run_ocr(pre_path, txt_path, ocr_json_path)
        ocr_text = ocr_out.get("text", "")
        if not ocr_out.get("ok"):
            issues.append(f"OCR degraded: {ocr_out.get('error', 'unknown')}")

        try:
            gemini_data = gemini_client.call_gemini_page(
                image_path, ocr_text, page_number,
                model=SETTINGS.gemini_model, api_key=SETTINGS.gemini_api_key,
                max_retries=SETTINGS.max_gemini_retries,
                base_delay=SETTINGS.retry_base_delay_sec,
                validator=validator.light_schema_check)
        except Exception as exc:
            err = f"Gemini failed: {exc}"
            payload = {"page_number": page_number, "status": "FAILED",
                       "voters": [], "issues": issues + [err],
                       "error": str(exc)[:800]}
            checkpoint.save_page_result(dirs["results"], page_number, payload)
            checkpoint.mark(dirs["state"], page_number, "failed")
            save_interim_excel(job_id, dirs)
            return payload

        parsed, v_issues, suspicious = validator.validate_page_payload(
            gemini_data, expected_page=page_number,
            min_age=SETTINGS.min_age, max_age=SETTINGS.max_age)
        issues.extend(v_issues)
        if parsed is None:
            err = f"validation failed: {'; '.join(v_issues) or 'schema error'}"
            salvaged = _salvage_raw_voters(gemini_data, err)
            status = "REVIEW_REQUIRED" if salvaged else "FAILED"
            payload = {"page_number": page_number, "status": status,
                       "voters": salvaged, "issues": issues + (
                           [f"kept {len(salvaged)} raw AI row(s) for review"]
                           if salvaged else []),
                       "error": err[:800],
                       "raw_gemini": gemini_data}
            checkpoint.save_page_result(dirs["results"], page_number, payload)
            checkpoint.mark(dirs["state"], page_number,
                            "review" if salvaged else "failed")
            try:
                review_manager.save_review_artifacts(
                    dirs["review"], page_number, image_path, ocr_text,
                    salvaged, issues)
            except Exception:
                pass
            save_interim_excel(job_id, dirs)
            return payload

        voters = [v.model_dump() for v in parsed.voters]

        if SETTINGS.enable_second_verification and suspicious:
            try:
                boxes = ocr_out.get("boxes", [])
            except Exception:
                boxes = []
            for v in voters:
                if not v.get("needs_review"):
                    continue
                crop_path = dirs["review"] / f"page_{tag}" / f"serial_{v.get('serial_number')}.png"
                cropped = None
                if boxes:
                    cropped = review_manager.try_crop_record(image_path, boxes[0], crop_path)
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
                except Exception:
                    pass
            issues.append(f"second verification applied to {len([x for x in voters if x.get('needs_review')])} record(s)")

        needs_review_any = any(v.get("needs_review") for v in voters)
        status = "REVIEW_REQUIRED" if (needs_review_any or any(
            "duplicate" in i.lower() or "missing" in i.lower() for i in issues)) and voters else "COMPLETED"
        if not voters:
            status = "REVIEW_REQUIRED"
            issues.append("no voter records on page (cover/map/summary?) — verify against image")

        if status in ("REVIEW_REQUIRED", "FAILED"):
            try:
                review_manager.save_review_artifacts(
                    dirs["review"], page_number, image_path, ocr_text, voters, issues)
            except Exception:
                pass

        payload = {"page_number": page_number, "status": status,
                   "voters": voters, "issues": issues}
        checkpoint.save_page_result(dirs["results"], page_number, payload)
        checkpoint.mark(dirs["state"], page_number,
                        "completed" if status == "COMPLETED" else
                        ("review" if status == "REVIEW_REQUIRED" else "failed"))
        append_log(job_id, f"Page {tag}: {status} voters={len(voters)} issues={len(issues)}")
        save_interim_excel(job_id, dirs)
        return payload

    except Exception as exc:
        payload = {"page_number": page_number, "status": "FAILED",
                   "voters": [], "issues": issues + [f"unexpected: {exc}"],
                   "error": str(exc)[:800]}
        try:
            checkpoint.save_page_result(dirs["results"], page_number, payload)
            checkpoint.mark(dirs["state"], page_number, "failed")
        except Exception:
            pass
        try:
            save_interim_excel(job_id, dirs)
        except Exception:
            pass
        return payload


# ---------------------------------------------------------------- finalize
def _done_pages(dirs: dict[str, Path]) -> list[int]:
    done = []
    for f in dirs["results"].glob("page_*.json"):
        try:
            done.append(int(f.stem.split("_")[1]))
        except (IndexError, ValueError):
            continue
    return sorted(done)


def save_interim_excel(job_id: str, dirs: dict[str, Path]) -> None:
    try:
        done = _done_pages(dirs)
        if not done:
            return
        payloads = utils.collect_results_for_report(dirs["results"], done)
        excel_writer.write_excels(dirs["output"], payloads)
    except Exception as exc:
        append_log(job_id, f"Interim Excel save failed (results/*.json still safe): {exc}")


def finalize_job(job_id: str, dirs: dict[str, Path], pages: list[int]) -> dict:
    payloads = utils.collect_results_for_report(dirs["results"], pages)
    all_voters: list[dict] = []
    for p in payloads:
        for v in _effective_voters(p):
            row = dict(v)
            row["_page"] = p.get("page_number")
            all_voters.append(row)

    x = validator.cross_page_checks(all_voters)
    per_page = {p.get("page_number"): len(_effective_voters(p)) for p in payloads}
    avg = (sum(per_page.values()) / len(per_page)) if per_page else 0

    completed = sum(1 for p in payloads if p.get("status") == "COMPLETED")
    review_p = sum(1 for p in payloads if p.get("status") == "REVIEW_REQUIRED")
    failed = sum(1 for p in payloads if p.get("status") == "FAILED")

    stats = {
        "total_pages": len(pages),
        "completed_pages": completed,
        "review_pages": review_p,
        "failed_pages": failed,
        "failed_page_numbers": sorted(p.get("page_number") for p in payloads if p.get("status") == "FAILED"),
        "review_page_numbers": sorted(p.get("page_number") for p in payloads if p.get("status") == "REVIEW_REQUIRED"),
        "total_records": len(all_voters),
        "validated_records": sum(1 for v in all_voters if not v.get("needs_review")),
        "review_records": sum(1 for v in all_voters if v.get("needs_review")),
        "duplicate_epics": len(x["duplicate_epics"]),
        "duplicate_serials": len(x["duplicate_serials"]),
        "model": SETTINGS.gemini_model,
    }
    voters_xlsx, review_xlsx = excel_writer.write_excels(dirs["output"], payloads)
    report_path = excel_writer.write_report(dirs["output"], stats)
    append_log(job_id, f"Excel: {voters_xlsx.name} | Review: {review_xlsx.name} | Report: {report_path.name}")
    return stats


# ---------------------------------------------------------------- background entry
def run_job(job_id: str) -> None:
    """Background worker entry. Never raises — always updates meta.json."""
    meta = load_meta(job_id)
    if not meta:
        return
    dirs = job_dirs(job_id)
    input_pdf = dirs["root"] / "input.pdf"

    with _lock:
        meta = load_meta(job_id) or meta
        if meta.get("status") == "running":
            return  # already running (e.g. after restart double-submit)
        meta["status"] = "running"
        save_meta(job_id, meta)

    append_log(job_id, "Processing started")
    try:
        if not input_pdf.exists():
            raise FileNotFoundError("input.pdf missing — re-upload the PDF")

        try:
            total = pdf_processor.get_total_pages(input_pdf)
        except Exception as exc:
            raise RuntimeError(f"Cannot read PDF: {exc}")

        f = meta.get("from_page")
        t = meta.get("to_page")
        if f or t:
            lo = int(f or 1)
            hi = int(t or total)
            if lo < 1 or hi > total or lo > hi:
                raise ValueError(f"Page range {lo}-{hi} invalid for {total}-page PDF")
            pages = list(range(lo, hi + 1))
        else:
            pages = list(range(1, total + 1))

        mode = meta.get("mode", "normal")
        force = bool(meta.get("force", False))

        if mode == "retry-failed":
            todo = []
            for p in pages:
                cached = checkpoint.load_page_result(dirs["results"], p)
                if not cached or cached.get("status") in ("FAILED", "REVIEW_REQUIRED"):
                    todo.append(p)
            if not todo:
                append_log(job_id, "Nothing to retry — no failed/review pages")
                meta = load_meta(job_id) or meta
                meta["status"] = "done"
                save_meta(job_id, meta)
                return
            pages = todo
            force = True
            append_log(job_id, f"Retrying {len(pages)} failed/review pages")

        if mode == "dry-run":
            meta = load_meta(job_id) or meta
            meta.update({"status": "done", "total_pages": total, "pages": pages,
                         "done_pages": [], "error": None})
            save_meta(job_id, meta)
            append_log(job_id, f"Dry run OK: {total} pages, key={'set' if SETTINGS.gemini_api_key else 'MISSING'}")
            return

        meta = load_meta(job_id) or meta
        meta.update({"total_pages": total, "pages": pages})
        save_meta(job_id, meta)

        prog = checkpoint.load_progress(dirs["state"])
        prog["total_pages"] = total
        checkpoint.save_progress(dirs["state"], prog)

        done_pages: list[int] = []
        for i, p in enumerate(pages, 1):
            # re-load meta each page so a delete/stop is respected
            cur = load_meta(job_id)
            if not cur or cur.get("status") not in ("running",):
                append_log(job_id, "Job stopped externally")
                return
            append_log(job_id, f"Progress {i}/{len(pages)} — page {p}")
            process_job_page(job_id, p, input_pdf, dirs, force=force)
            done_pages.append(p)
            try:
                save_interim_excel(job_id, dirs)
            except Exception:
                pass
            # live progress for polling
            cur = load_meta(job_id) or {}
            cur["done_pages"] = done_pages
            save_meta(job_id, cur)

        stats = finalize_job(job_id, dirs, pages)
        meta = load_meta(job_id) or meta
        meta.update({
            "status": "done",
            "done_pages": pages,
            "failed_pages": stats.get("failed_page_numbers", []),
            "review_pages": stats.get("review_page_numbers", []),
            "total_records": stats.get("total_records", 0),
            "review_records": stats.get("review_records", 0),
            "error": None,
        })
        save_meta(job_id, meta)
        append_log(job_id, f"Done: {stats.get('total_records', 0)} records ({stats.get('review_records', 0)} review)")
    except Exception as exc:
        append_log(job_id, f"Job failed: {exc}")
        try:
            save_interim_excel(job_id, dirs)
        except Exception:
            pass
        meta = load_meta(job_id) or meta
        meta.update({"status": "failed", "error": str(exc)[:500]})
        save_meta(job_id, meta)
