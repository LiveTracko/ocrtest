"""Checkpoint / resume. Every page result is saved immediately.

All writes are atomic (tmp file + os.replace) so killing the process
mid-write never leaves a corrupt JSON behind — the previous good file
survives and AI data already collected is never lost.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text atomically: tmp file in same dir + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            try:
                fh.flush()
                os.fsync(fh.fileno())
            except OSError:
                pass  # fsync not critical on all filesystems
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def progress_path(state_dir: Path) -> Path:
    return state_dir / "progress.json"


def load_progress(state_dir: Path) -> dict:
    p = progress_path(state_dir)
    if not p.exists():
        return {"completed": [], "failed": [], "review": [], "total_pages": 0}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"completed": [], "failed": [], "review": [], "total_pages": 0}


def save_progress(state_dir: Path, data: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    data["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _atomic_write_text(progress_path(state_dir), json.dumps(data, indent=2))


def result_path(results_dir: Path, page_number: int) -> Path:
    return results_dir / f"page_{page_number:03d}.json"


def save_page_result(results_dir: Path, page_number: int, payload: dict) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    p = result_path(results_dir, page_number)
    _atomic_write_text(p, json.dumps(payload, indent=2, ensure_ascii=False))
    return p


def load_page_result(results_dir: Path, page_number: int) -> dict | None:
    p = result_path(results_dir, page_number)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def mark(state_dir: Path, page_number: int, bucket: str, total_pages: int = 0) -> dict:
    data = load_progress(state_dir)
    for k in ("completed", "failed", "review"):
        data.setdefault(k, [])
    # one bucket only
    for k in ("completed", "failed", "review"):
        if page_number in data[k] and k != bucket:
            data[k].remove(page_number)
    if page_number not in data[bucket]:
        data[bucket].append(page_number)
        data[bucket].sort()
    if total_pages:
        data["total_pages"] = total_pages
    save_progress(state_dir, data)
    return data
