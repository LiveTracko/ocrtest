"""Conservative OpenCV preprocessing. Must not destroy characters."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PIL import Image


def _deskew_angle(gray: np.ndarray) -> float:
    # Threshold + find coords of dark pixels, fit min-area rect.
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    coords = np.column_stack(np.where(bw > 0))
    if coords.shape[0] < 100:
        return 0.0
    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle
    # Clamp: only correct small skew, never rotate real content 90 deg.
    if abs(angle) > 10:
        return 0.0
    return angle


def preprocess_image(input_path: Path, output_path: Path) -> Path:
    """Grayscale -> CLAHE -> denoise -> deskew -> sharpen. Saves output, returns path."""
    img = cv2.imread(str(input_path), cv2.IMREAD_COLOR)
    if img is None:
        # Fallback via Pillow (e.g. unusual PNG)
        pil = Image.open(str(input_path)).convert("RGB")
        img = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # Contrast: CLAHE is gentler than global equalizeHist
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)

    # Denoise (light — preserves strokes)
    denoised = cv2.fastNlMeansDenoising(enhanced, h=10,
                                        templateWindowSize=7, searchWindowSize=21)

    # Deskew small angles only
    try:
        angle = _deskew_angle(denoised)
        if abs(angle) > 0.3:
            (h, w) = denoised.shape[:2]
            m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
            denoised = cv2.warpAffine(denoised, m, (w, h),
                                      flags=cv2.INTER_CUBIC,
                                      borderMode=cv2.BORDER_REPLICATE)
    except Exception:
        pass  # deskew is best-effort

    # Gentle sharpen
    kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]], dtype=np.float32)
    sharpened = cv2.filter2D(denoised, -1, kernel)

    # NOTE: no hard binary threshold by default — it destroys faint print.
    # Thresholded variants are only produced on demand (see threshold_variant).
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(output_path), sharpened)
    return output_path


def threshold_variant(input_path: Path, output_path: Path) -> Path:
    """Optional adaptive-threshold fallback if plain OCR confidence is poor."""
    gray = cv2.imread(str(input_path), cv2.IMREAD_GRAYSCALE)
    if gray is None:
        raise FileNotFoundError(f"Cannot read {input_path}")
    th = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                               cv2.THRESH_BINARY, 31, 10)
    cv2.imwrite(str(output_path), th)
    return output_path
