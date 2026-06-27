from pathlib import Path

import fitz  # PyMuPDF


def get_page_count(pdf_path: Path | str) -> int:
    with fitz.open(pdf_path) as doc:
        return doc.page_count


def render_page(pdf_path: Path | str, page_idx: int, dpi: int = 200) -> bytes:
    """Render 0-indexed page `page_idx` to PNG bytes at given DPI."""
    with fitz.open(pdf_path) as doc:
        if page_idx < 0 or page_idx >= doc.page_count:
            raise ValueError(f"page_idx {page_idx} out of range [0,{doc.page_count})")
        page = doc[page_idx]
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        return pixmap.tobytes("png")


def render_page_strip(pdf_path: Path | str, page_idx: int,
                      position: str = "bottom", ratio: float = 0.12,
                      dpi: int = 150) -> bytes:
    """Render only the top or bottom strip of a page (for page-number detection).

    position: 'top' or 'bottom'
    ratio: fraction of page height to include (0.0-1.0)
    dpi: render DPI (lower = faster, 150 is enough for 1-3 digit numbers)

    Page numbers in Chinese books are typically in the bottom 10-15% of the page
    (页脚), sometimes top (页眉). Cropping to just that strip makes OCR ~10x
    faster than full-page OCR and avoids confusing the model with body text.
    """
    with fitz.open(pdf_path) as doc:
        if page_idx < 0 or page_idx >= doc.page_count:
            raise ValueError(f"page_idx {page_idx} out of range [0,{doc.page_count})")
        page = doc[page_idx]
        rect = page.rect  # full page rectangle in PDF points (72 dpi)
        w, h = rect.width, rect.height

        if position == "top":
            clip = fitz.Rect(0, 0, w, h * ratio)
        elif position == "bottom":
            clip = fitz.Rect(0, h * (1 - ratio), w, h)
        else:
            raise ValueError(f"position must be 'top' or 'bottom', got {position!r}")

        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        pixmap = page.get_pixmap(matrix=matrix, clip=clip, alpha=False)
        return pixmap.tobytes("png")
