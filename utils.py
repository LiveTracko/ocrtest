"""Small shared helpers."""
from __future__ import annotations

import logging
from pathlib import Path

import checkpoint


def parse_pages_arg(spec: str, total: int) -> list[int]:
    """Parse '1' or '1-3' or '1,3,5' into sorted 1-based page numbers."""
    wanted: set[int] = set()
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            lo, hi = int(a), int(b)
            if lo > hi:
                lo, hi = hi, lo
            wanted.update(range(lo, hi + 1))
        else:
            wanted.add(int(part))
    pages = sorted(p for p in wanted if 1 <= p <= total)
    if not pages:
        raise ValueError(f"No valid pages in '{spec}' (PDF has {total} pages)")
    return pages


def collect_results_for_report(results_dir: Path, pages: list[int]) -> list[dict]:
    out = []
    for p in pages:
        data = checkpoint.load_page_result(results_dir, p)
        if data:
            out.append(data)
        else:
            out.append({"page_number": p, "status": "FAILED",
                        "voters": [], "issues": ["no result file"], "error": "missing"})
    return out
