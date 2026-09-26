"""PDF handling — PyMuPDF. Never modifies the original PDF."""
from __future__ import annotations

from pathlib import Path

import fitz  # PyMuPDF


def get_total_pages(pdf_path: Path) -> int:
    with fitz.open(str(pdf_path)) as doc:
        return len(doc)


def split_and_render_page(pdf_path: Path, page_number_1based: int, pages_dir: Path,
                          images_dir: Path, dpi: int = 300) -> tuple[Path, Path]:
    """Extract 1-based page -> pages/page_XXX.pdf + images/page_XXX.png.

    Returns (page_pdf_path, image_path). Raises on failure.
    """
    tag = f"{page_number_1based:03d}"
    page_pdf_path = pages_dir / f"page_{tag}.pdf"
    image_path = images_dir / f"page_{tag}.png"

    with fitz.open(str(pdf_path)) as src:
        if page_number_1based < 1 or page_number_1based > len(src):
            raise ValueError(f"Page {page_number_1based} out of range (1..{len(src)})")
        with fitz.open() as single:
            single.insert_pdf(src, from_page=page_number_1based - 1,
                              to_page=page_number_1based - 1)
            single.save(str(page_pdf_path))

    # Render at requested DPI (72 PDF points per inch)
    zoom = dpi / 72.0
    with fitz.open(str(page_pdf_path)) as single:
        page = single[0]
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        pix.save(str(image_path))

    return page_pdf_path, image_path
