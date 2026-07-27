"""
Supplementary OCR for cargo-table fields missing from main page OCR (e.g. gross weight).
"""

from __future__ import annotations

import re
from typing import Optional

from ocr_extractor import preprocess_for_ocr, render_pdf_pages


def _parse_weight_kg(text: str) -> Optional[str]:
    if not text:
        return None
    for pat in (
        r"\b(\d{3,4}[.,]\d{2})\s*KGS?\b",
        r"\b(\d{3,4})\s*KGS?\b",
        r"KGS?\s*[:\-]?\s*(\d{3,4}[.,]\d{2})\b",
    ):
        m = re.search(pat, text, re.I)
        if m:
            return m.group(1).replace(",", ".")
    return None


def extract_gross_weight_from_page_text(page_text: str) -> Optional[str]:
    return _parse_weight_kg(page_text)


def extract_gross_weight_from_page_pdf(
    pdf_bytes: bytes,
    page_no: int,
    page_text: str = "",
) -> Optional[str]:
    """Re-OCR the cargo table band when gross weight is absent from merged page text."""
    from ocr_extractor import _ocr_text

    found = extract_gross_weight_from_page_text(page_text)
    if found:
        return found

    if not pdf_bytes or page_no < 1:
        return None

    try:
        images = render_pdf_pages(pdf_bytes, dpi=360)
    except Exception:
        return None

    if page_no > len(images):
        return None

    image = images[page_no - 1]
    width, height = image.size
    bands = [
        (0.04, 0.36, 0.96, 0.50),
        (0.04, 0.40, 0.96, 0.55),
        (0.50, 0.38, 0.96, 0.52),
    ]
    for x0, y0, x1, y1 in bands:
        crop = image.crop((int(width * x0), int(height * y0), int(width * x1), int(height * y1)))
        crop = preprocess_for_ocr(crop)
        for psm in (6, 4, 11):
            text = _ocr_text(crop, psm=psm)
            weight = _parse_weight_kg(text)
            if weight:
                return weight
    return None
