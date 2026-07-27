import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

import fitz
import numpy as np
from config import settings


@dataclass
class NativeExtractionResult:
    text: str
    method: str
    page_count: int
    char_count: int
    field_hits: int
    warnings: List[str]


FIELD_KEYWORDS = (
    "bill of lading", "b/l", "booking", "shipper", "consignee", "notify",
    "vessel", "voyage", "port of loading", "port of discharge",
    "place of receipt", "place of delivery", "container", "seal",
    "gross weight", "measurement", "description", "hs code", "acid",
    "freight", "original", "non-negotiable",
)


def normalize_text(text: str) -> str:
    text = text.replace("\x00", " ").replace("\x0c", "\n")
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def count_field_hits(text: str) -> int:
    upper = text.lower()
    return sum(1 for kw in FIELD_KEYWORDS if kw in upper)


def _words_to_visual_lines(words: List[Tuple], y_tolerance: float = 3.0) -> List[str]:
    if not words:
        return []

    words = sorted(words, key=lambda w: (round(float(w[1]) / y_tolerance), float(w[0])))

    lines: List[List[Tuple]] = []
    current: List[Tuple] = []
    current_y: Optional[float] = None

    for w in words:
        y = float(w[1])
        if current_y is None or abs(y - current_y) <= y_tolerance:
            current.append(w)
            current_y = y if current_y is None else (current_y * 0.75 + y * 0.25)
        else:
            lines.append(sorted(current, key=lambda x: float(x[0])))
            current = [w]
            current_y = y

    if current:
        lines.append(sorted(current, key=lambda x: float(x[0])))

    output: List[str] = []
    for line_words in lines:
        parts: List[str] = []
        prev_x1: Optional[float] = None
        median_width = np.median([max(1.0, float(w[2]) - float(w[0])) for w in line_words]) if line_words else 5.0

        for w in line_words:
            x0, _, x1, _, word = float(w[0]), float(w[1]), float(w[2]), float(w[3]), str(w[4])
            if prev_x1 is not None:
                gap = x0 - prev_x1
                if gap > median_width * 4:
                    parts.append("    ")
                elif gap > median_width * 1.2:
                    parts.append(" ")
                else:
                    parts.append(" ")
            parts.append(word)
            prev_x1 = x1

        output.append("".join(parts).strip())

    return [ln for ln in output if ln]


def _blocks_to_text(page: fitz.Page) -> str:
    blocks = page.get_text("blocks") or []
    valid = []
    for block in blocks:
        if len(block) >= 5 and isinstance(block[4], str) and block[4].strip():
            block_type = block[6] if len(block) > 6 else 0
            if block_type == 0:
                valid.append(block)
    valid.sort(key=lambda b: (round(float(b[1]), 1), round(float(b[0]), 1)))
    return "\n".join(normalize_text(b[4]) for b in valid if normalize_text(b[4]))


def extract_native_pdf_text(pdf_bytes: bytes) -> NativeExtractionResult:
    warnings: List[str] = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    pages: List[str] = []
    page_count = len(doc)

    try:
        for i, page in enumerate(doc, start=1):
            words = page.get_text("words") or []
            visual_lines = _words_to_visual_lines(words)
            visual_text = "\n".join(visual_lines)

            block_text = _blocks_to_text(page)

            candidates = []
            if visual_text.strip():
                candidates.append("[VISUAL WORD ORDER]\n" + visual_text)
            if block_text.strip() and block_text.strip() != visual_text.strip():
                candidates.append("[BLOCK ORDER]\n" + block_text)

            if candidates:
                pages.append(f"--- PAGE {i} ---\n" + "\n\n".join(candidates))
            else:
                warnings.append(f"Page {i}: no native text layer found.")
    finally:
        doc.close()

    text = normalize_text("\n\n".join(pages))
    field_hits = count_field_hits(text)
    method = "native" if len(text) >= settings.native_min_chars and field_hits >= settings.native_min_field_hits else "native_sparse"

    if method == "native_sparse":
        warnings.append(
            f"Native text looks sparse: {len(text)} chars and {field_hits} field-keyword hits. OCR/hybrid extraction is recommended."
        )

    return NativeExtractionResult(
        text=text,
        method=method,
        page_count=page_count,
        char_count=len(text),
        field_hits=field_hits,
        warnings=warnings,
    )


def extract_pdf_text_professionally(pdf_bytes: bytes, force_ocr: bool = False) -> dict:
    from ocr_extractor import ocr_pdf

    native = extract_native_pdf_text(pdf_bytes)

    if not force_ocr and native.method == "native":
        return {
            "method": "native",
            "text": native.text,
            "quality": {
                "native_char_count": native.char_count,
                "native_field_hits": native.field_hits,
                "page_count": native.page_count,
                "warnings": native.warnings,
            },
        }

    ocr = ocr_pdf(pdf_bytes)
    ocr_text = ocr["raw_text"]

    if native.text.strip():
        hybrid_text = normalize_text(
            "[NATIVE PDF TEXT]\n"
            + native.text
            + "\n\n[OCR TEXT FALLBACK / VISUAL TEXT]\n"
            + ocr_text
        )
        method = "hybrid_native_ocr"
    else:
        hybrid_text = ocr_text
        method = "ocr"

    return {
        "method": method,
        "text": hybrid_text,
        "quality": {
            "native_char_count": native.char_count,
            "native_field_hits": native.field_hits,
            "page_count": native.page_count,
            "native_warnings": native.warnings,
            "ocr_summary": ocr.get("ocr_summary", {}),
        },
    }