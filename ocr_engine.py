"""PaddleOCR wrapper. OCR is supporting info only — never final truth."""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_ocr_instance = None


def _get_ocr():
    global _ocr_instance
    if _ocr_instance is not None:
        return _ocr_instance
    from paddleocr import PaddleOCR
    # English elector rolls are typically English caps + digits.
    _ocr_instance = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
    return _ocr_instance


def run_ocr(image_path: Path, txt_path: Path, json_path: Path) -> dict:
    """Run OCR, always write txt + json (even if empty on failure).

    Returns {"text": str, "boxes": [...], "mean_confidence": float, "ok": bool}.
    """
    txt_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        ocr = _get_ocr()
        result = ocr.ocr(str(image_path), cls=True)
        # PaddleOCR returns [ [ [box], (text, conf) ], ... ] wrapped per-page
        lines, boxes = [], []
        confs = []
        pages = result[0] if result and isinstance(result[0], list) else (result or [])
        for item in pages:
            try:
                box, (text, conf) = item[0], item[1]
                lines.append(str(text))
                boxes.append({"box": box, "text": str(text), "confidence": float(conf)})
                confs.append(float(conf))
            except Exception:
                continue
        text = "\n".join(lines)
        mean_conf = sum(confs) / len(confs) if confs else 0.0
        txt_path.write_text(text, encoding="utf-8")
        json_path.write_text(json.dumps(
            {"image": str(image_path), "mean_confidence": mean_conf,
             "lines": boxes}, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("OCR ok for %s (%d lines, mean_conf=%.2f)",
                 image_path.name, len(lines), mean_conf)
        return {"text": text, "boxes": boxes, "mean_confidence": mean_conf, "ok": True}
    except Exception as exc:  # OCR must never crash the pipeline
        log.warning("OCR failed for %s: %s", image_path, exc)
        if not txt_path.exists():
            txt_path.write_text("", encoding="utf-8")
        if not json_path.exists():
            json_path.write_text(json.dumps(
                {"image": str(image_path), "error": str(exc),
                 "mean_confidence": 0.0, "lines": []}, indent=2), encoding="utf-8")
        return {"text": txt_path.read_text(encoding="utf-8") if txt_path.exists() else "",
                "boxes": [], "mean_confidence": 0.0, "ok": False, "error": str(exc)}
