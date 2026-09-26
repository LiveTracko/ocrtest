"""Review system — uncertain records are saved, never guessed."""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)


def _tag(page: int) -> str:
    return f"{page:03d}"


def save_review_artifacts(review_dir: Path, page_number: int, page_image: Path,
                          ocr_text: str, voters: list[dict], issues: list[str]) -> Path:
    """Copy page image + write a per-page review bundle. Returns bundle dir."""
    bundle = review_dir / f"page_{_tag(page_number)}"
    bundle.mkdir(parents=True, exist_ok=True)
    try:
        if page_image.exists():
            shutil.copy2(str(page_image), str(bundle / f"page_{_tag(page_number)}.png"))
    except OSError as exc:
        log.warning("Could not copy review image: %s", exc)
    (bundle / "ocr.txt").write_text(ocr_text or "", encoding="utf-8")
    (bundle / "issues.txt").write_text("\n".join(issues) or "no issues", encoding="utf-8")

    # Per-record crops: best-effort — try OCR boxes if available, else skip crops.
    # Full-page image in bundle is always available for manual review.
    import json as _json
    (bundle / "voters.json").write_text(
        _json.dumps(voters, indent=2, ensure_ascii=False), encoding="utf-8")
    return bundle


def try_crop_record(page_image_path: Path, box: dict, out_path: Path,
                    pad: int = 12) -> Path | None:
    """Crop a record region given an OCR box {'box': [[x,y]x4]}. Returns path or None."""
    try:
        import cv2
        import numpy as np
        img = cv2.imread(str(page_image_path))
        if img is None:
            return None
        pts = np.array(box["box"], dtype=float).reshape(-1, 2)
        x0, y0 = int(pts[:, 0].min()) - pad, int(pts[:, 1].min()) - pad
        x1, y1 = int(pts[:, 0].max()) + pad, int(pts[:, 1].max()) + pad
        h, w = img.shape[:2]
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(w, x1), min(h, y1)
        if x1 <= x0 or y1 <= y0:
            return None
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out_path), img[y0:y1, x0:x1])
        return out_path
    except Exception as exc:
        log.debug("crop failed: %s", exc)
        return None
