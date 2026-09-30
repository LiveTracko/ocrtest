"""Web server — browser UI for the voter-roll extractor.

Run locally:
    pip install -r requirements.txt
    python -m uvicorn server:app --host 127.0.0.1 --port 8000

On Render: start command is `uvicorn server:app --host 0.0.0.0 --port $PORT`.

What it does:
  - Upload PDF in browser -> jobs/<id>/input.pdf (per-user-job isolation,
    so 5+ concurrent users never overwrite each other)
  - Background thread pool (max 2 workers — protects Gemini quota + free-tier RAM)
  - Poll /api/jobs/{id} for progress, /api/jobs/{id}/logs for live log
  - Download voters.xlsx / review.xlsx / processing_report.json in browser
  - Merge 2+ finished jobs into one Excel (same stack-no-dedup rule as desktop app)

Pipeline code is reused unchanged via job_runner.py.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

import job_runner
import security as sec
from config import SETTINGS

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Voter Roll Extractor — Web")

# Background pool: how many PDFs process at once. Rest stay "queued".
# Default 1 — Render's free tier has ~512MB RAM and PaddleOCR + OpenCV +
# Gemini on a 300-DPI page can spike past that with 2 workers (OOM restart
# wipes the temp jobs disk). Set MAX_WORKERS=2 on Starter plan or bigger.
# (Also protects the Gemini per-minute quota with 5+ users.)
_executor = ThreadPoolExecutor(max_workers=max(1, int(os.getenv("MAX_WORKERS", "1"))))

# Serve frontend assets
_static = BASE_DIR / "static"
_static.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static)), name="static")


# ---------------------------------------------------------------- helpers
def _parse_page_bound(raw: str | None) -> int | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        v = int(s)
    except ValueError:
        raise HTTPException(status_code=400, detail="From/To pages must be whole numbers.")
    if v < 1:
        raise HTTPException(status_code=400, detail="Page numbers must be >= 1.")
    return v


def _job_files(job_id: str) -> dict[str, Path]:
    d = job_runner.job_dirs(job_id)
    return {
        "voters": d["output"] / "voters.xlsx",
        "review": d["output"] / "review.xlsx",
        "report": d["output"] / "processing_report.json",
        "merged_voters": d["output"] / "merged_voters.xlsx",
        "merged_review": d["output"] / "merged_review.xlsx",
    }


# ---------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = BASE_DIR / "templates" / "index.html"
    if html.exists():
        return HTMLResponse(html.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Voter Roll Extractor</h1><p>templates/index.html missing.</p>", status_code=500)


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "model": SETTINGS.gemini_model,
        "dpi": SETTINGS.pdf_render_dpi,
        "key_set": bool(SETTINGS.gemini_api_key and SETTINGS.gemini_api_key != "YOUR_KEY_HERE"),
        "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


# ---------------------------------------------------------------- jobs
@app.post("/api/jobs")
async def create_job(
    file: UploadFile = File(...),
    from_page: str | None = Form(None),
    to_page: str | None = Form(None),
    force: bool = Form(False),
    mode: str = Form("normal"),
) -> dict:
    if mode not in ("normal", "dry-run", "retry-failed"):
        raise HTTPException(status_code=400, detail="Invalid mode.")
    lo = _parse_page_bound(from_page)
    hi = _parse_page_bound(to_page)
    if lo is not None and hi is not None and lo > hi:
        raise HTTPException(status_code=400, detail="From page cannot be greater than To page.")

    orig = (file.filename or "upload.pdf").strip()
    # Some multipart parsers retain the surrounding quotes from
    # Content-Disposition (filename="x.pdf" -> '"x.pdf"'). Strip them so
    # real browser uploads are never rejected for a bogus extension.
    if len(orig) >= 2 and orig[0] == orig[-1] and orig[0] in ("'", '"'):
        orig = orig[1:-1].strip()
    orig = orig or "upload.pdf"
    if not orig.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only .pdf files are allowed.")

    meta = job_runner.create_job(orig, lo, hi, force, mode)
    job_id = meta["job_id"]
    dirs = job_runner.job_dirs(job_id)

    # Save upload to per-job input.pdf (streamed, size-guarded).
    # NOTE: temp name must keep the .pdf suffix — sec.validate_pdf requires it.
    safe_name = sec.sanitize_filename(orig, ".pdf")
    tmp_path = dirs["root"] / f"upload_{safe_name}"
    size = 0
    max_bytes = sec.MAX_PDF_MB * 1024 * 1024
    try:
        with open(tmp_path, "wb") as fh:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        status_code=400,
                        detail=f"PDF too large (> {sec.MAX_PDF_MB} MB).")
                fh.write(chunk)
    except HTTPException:
        shutil.rmtree(dirs["root"], ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(dirs["root"], ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Upload failed: {exc}")

    ok, reason = sec.validate_pdf(tmp_path)
    if not ok:
        shutil.rmtree(dirs["root"], ignore_errors=True)
        raise HTTPException(status_code=400, detail=reason)

    dest = dirs["root"] / "input.pdf"
    tmp_path.replace(dest)

    # Validate page range against real PDF now (fast, no AI cost)
    try:
        import pdf_processor
        total = pdf_processor.get_total_pages(dest)
    except Exception as exc:
        shutil.rmtree(dirs["root"], ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"Cannot read PDF: {exc}")
    if lo is not None and lo > total:
        shutil.rmtree(dirs["root"], ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"From page {lo} exceeds PDF pages ({total}).")
    if hi is not None and hi > total:
        shutil.rmtree(dirs["root"], ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"To page {hi} exceeds PDF pages ({total}).")

    meta["total_pages"] = total
    job_runner.save_meta(job_id, meta)
    job_runner.append_log(job_id, f"Uploaded {orig} ({size / 1048576:.1f} MB, {total} pages)")

    # Queue background processing (never blocks the HTTP request)
    _executor.submit(job_runner.run_job, job_id)
    meta = job_runner.load_meta(job_id) or meta
    return {"job_id": job_id, "status": meta.get("status"), "total_pages": total}


@app.get("/api/jobs")
def get_jobs() -> dict:
    return {"jobs": job_runner.list_jobs()}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    meta = job_runner.load_meta(job_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Job not found.")
    files = _job_files(job_id)
    meta = dict(meta)
    meta["has_voters"] = files["voters"].exists()
    meta["has_review"] = files["review"].exists()
    meta["has_report"] = files["report"].exists()
    return meta


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    """Delete one job and ALL its files (PDF, pages, images, Excels).

    Running jobs are refused — wait for them to finish first, otherwise
    the background worker would keep writing into a deleted folder.
    """
    meta = job_runner.load_meta(job_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Job not found.")
    if meta.get("status") == "running":
        raise HTTPException(status_code=400, detail="Job is still running — wait for it to finish first.")
    shutil.rmtree(job_runner.job_root(job_id), ignore_errors=True)
    return {"deleted": job_id}


@app.delete("/api/jobs")
def delete_all_jobs() -> dict:
    """Clear all old jobs/Excels from the server. Running jobs are skipped."""
    deleted: list[str] = []
    skipped: list[str] = []
    for meta in job_runner.list_jobs():
        jid = meta.get("job_id", "")
        if meta.get("status") == "running":
            skipped.append(jid)
            continue
        shutil.rmtree(job_runner.job_root(jid), ignore_errors=True)
        deleted.append(jid)
    return {"deleted": deleted, "deleted_count": len(deleted),
            "skipped_running": skipped}


@app.get("/api/jobs/{job_id}/logs")
def get_logs(job_id: str, tail: int = 200) -> dict:
    if not job_runner.load_meta(job_id):
        raise HTTPException(status_code=404, detail="Job not found.")
    lp = job_runner.job_root(job_id) / "job.log"
    lines: list[str] = []
    if lp.exists():
        try:
            all_lines = lp.read_text(encoding="utf-8").splitlines()
            lines = all_lines[-max(1, min(tail, 1000)):]
        except OSError:
            pass
    return {"job_id": job_id, "logs": lines}


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str, kind: str = "voters"):
    if not job_runner.load_meta(job_id):
        raise HTTPException(status_code=404, detail="Job not found.")
    files = _job_files(job_id)
    mapping = {
        "voters": (files["merged_voters"] if files["merged_voters"].exists() else files["voters"]),
        "review": (files["merged_review"] if files["merged_review"].exists() else files["review"]),
        "report": files["report"],
    }
    if kind not in mapping:
        raise HTTPException(status_code=400, detail="kind must be voters|review|report.")
    path = mapping[kind]
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not ready yet — job still running?")
    media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" \
        if path.suffix == ".xlsx" else "application/json"
    return FileResponse(str(path), media_type=media, filename=f"{job_id}_{path.name}")


@app.get("/api/jobs/{job_id}/preview")
def preview(job_id: str, limit: int = 50) -> dict:
    """Return first N rows of the job's Excel as JSON for in-browser preview.

    Prefers merged_voters.xlsx when a merge was downloaded, else voters.xlsx.
    Works mid-run too (shows the interim Excel built after every page).
    """
    if not job_runner.load_meta(job_id):
        raise HTTPException(status_code=404, detail="Job not found.")
    files = _job_files(job_id)
    path = files["merged_voters"] if files["merged_voters"].exists() else files["voters"]
    if not path.exists():
        raise HTTPException(status_code=404, detail="Excel not ready yet — pages still processing.")
    limit = max(1, min(limit, 200))
    try:
        df = pd.read_excel(path, sheet_name="VOTERS")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Cannot read Excel: {exc}")
    total = len(df)
    head = df.head(limit)
    # to_json maps NaN/NaT -> null so the browser gets clean JSON
    rows = json.loads(head.to_json(orient="records"))
    return {"job_id": job_id, "file": path.name, "total_rows": total,
            "shown_rows": len(rows), "columns": [str(c) for c in df.columns],
            "rows": rows}


# ---------------------------------------------------------------- merge
@app.post("/api/merge")
def merge(job_ids: list[str]) -> dict:
    """Stack voters.xlsx from 2+ finished jobs (pure append, no dedup).

    Body: JSON array of job IDs, e.g. ["a1b2c3", "d4e5f6"].
    Writes merged_voters.xlsx + merged_review.xlsx into a NEW merge job
    and returns its ID for download.
    """
    ids = [str(j).strip() for j in (job_ids or []) if str(j).strip()]
    if len(ids) < 2:
        raise HTTPException(status_code=400, detail="Select at least 2 finished jobs to merge.")

    frames: list = []
    review_frames: list = []
    per: list[str] = []
    for jid in ids:
        meta = job_runner.load_meta(jid)
        if not meta:
            raise HTTPException(status_code=404, detail=f"Job not found: {jid}")
        voters = job_runner.job_dirs(jid)["output"] / "voters.xlsx"
        if not voters.exists():
            raise HTTPException(status_code=400, detail=f"Job {jid} has no voters.xlsx yet.")
        try:
            df = pd.read_excel(voters, sheet_name="VOTERS")
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Cannot read {jid}/voters.xlsx: {exc}")
        label = meta.get("filename", jid)[:40]
        if len(df):
            df = df.copy()
            df["Source Job"] = label
        frames.append(df)
        per.append(f"{label}={len(df)}")
        review_path = job_runner.job_dirs(jid)["output"] / "review.xlsx"
        if review_path.exists():
            try:
                rdf = pd.read_excel(review_path, sheet_name="REVIEW")
                if len(rdf):
                    rdf = rdf.copy()
                    rdf["Source Job"] = label
                review_frames.append(rdf)
            except Exception:
                pass

    if not frames:
        raise HTTPException(status_code=400, detail="No voter data found to merge.")

    merged = pd.concat(frames, ignore_index=True)
    total = len(merged)

    meta = job_runner.create_job(f"merge_{len(ids)}_jobs", None, None, False, "normal")
    mid = meta["job_id"]
    out = job_runner.job_dirs(mid)["output"]
    try:
        from excel_writer import VOTER_COLUMNS, REVIEW_COLUMNS, _write_df_atomic
        v_cols = [c for c in VOTER_COLUMNS if c in merged.columns] + \
                 [c for c in merged.columns if c not in VOTER_COLUMNS]
        merged = merged[v_cols]
        _write_df_atomic(merged, out / "merged_voters.xlsx", "VOTERS", text_cols={3, 7})
        _write_df_atomic(merged, out / "voters.xlsx", "VOTERS", text_cols={3, 7})
        if review_frames:
            r_merged = pd.concat(review_frames, ignore_index=True)
            r_cols = [c for c in REVIEW_COLUMNS if c in r_merged.columns] + \
                     [c for c in r_merged.columns if c not in REVIEW_COLUMNS]
            r_merged = r_merged[r_cols]
        else:
            r_merged = pd.DataFrame([{"Issue": "No records need review", "Status": "OK"}])
        _write_df_atomic(r_merged, out / "merged_review.xlsx", "REVIEW", text_cols={3})
        _write_df_atomic(r_merged, out / "review.xlsx", "REVIEW", text_cols={3})
    except ImportError:
        merged.to_excel(out / "merged_voters.xlsx", sheet_name="VOTERS", index=False)

    meta = job_runner.load_meta(mid) or meta
    meta.update({"status": "done", "total_records": total,
                 "filename": f"merge({'+'.join(per)})"})
    job_runner.save_meta(mid, meta)
    job_runner.append_log(mid, f"Merged {len(ids)} jobs = {total} rows ({' + '.join(per)})")
    return {"merge_job_id": mid, "total_rows": total, "per_job": per}
