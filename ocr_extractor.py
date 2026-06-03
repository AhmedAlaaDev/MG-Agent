import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

import fitz
import numpy as np
import pytesseract
from PIL import Image, ImageOps

from config import settings
from pdf_extractor import normalize_text


if settings.tesseract_cmd:
    pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd
elif os.name == "nt":
    for possible in (
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ):
        if os.path.exists(possible):
            pytesseract.pytesseract.tesseract_cmd = possible
            break


@dataclass
class OcrPageResult:
    page: int
    text: str
    word_count: int
    average_confidence: float


def render_pdf_pages(pdf_bytes: bytes, dpi: int = 300) -> List[Image.Image]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages: List[Image.Image] = []
    zoom = dpi / 72.0
    try:
        for page in doc:
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            pages.append(Image.frombytes("RGB", [pix.width, pix.height], pix.samples))
    finally:
        doc.close()
    return pages


def preprocess_for_ocr(image: Image.Image) -> Image.Image:
    gray = ImageOps.grayscale(image)
    gray = ImageOps.autocontrast(gray)
    arr = np.asarray(gray)
    threshold = max(150, int(arr.mean() * 0.92))
    arr = np.where(arr > threshold, 255, arr).astype(np.uint8)
    return Image.fromarray(arr)


def _ocr_text(image: Image.Image, psm: int) -> str:
    return pytesseract.image_to_string(
        image,
        lang=settings.tesseract_lang,
        config=f"--oem 3 --psm {psm}",
    )


def _ocr_data_confidence(image: Image.Image, psm: int) -> Tuple[int, float]:
    data = pytesseract.image_to_data(
        image,
        lang=settings.tesseract_lang,
        config=f"--oem 3 --psm {psm}",
        output_type=pytesseract.Output.DICT,
    )
    confs = []
    word_count = 0
    for txt, conf in zip(data.get("text", []), data.get("conf", [])):
        if not str(txt).strip():
            continue
        try:
            c = float(conf)
        except Exception:
            c = -1
        if c >= 25:
            word_count += 1
        if c >= 0:
            confs.append(c)
    avg = round(sum(confs) / len(confs), 2) if confs else 0.0
    return word_count, avg


def ocr_page(image: Image.Image, page_no: int) -> OcrPageResult:
    processed = preprocess_for_ocr(image)

    width, height = processed.size
    header = processed.crop((0, 0, width, int(height * 0.28)))
    body = processed.crop((0, int(height * 0.22), width, height))

    header_text = _ocr_text(header, psm=6)
    body_text = _ocr_text(body, psm=4)
    full_text_4 = _ocr_text(processed, psm=4)
    full_text_6 = _ocr_text(processed, psm=6)
    wc4, conf4 = _ocr_data_confidence(processed, psm=4)
    wc6, conf6 = _ocr_data_confidence(processed, psm=6)

    best_full_text = full_text_4 if conf4 >= conf6 else full_text_6
    word_count = max(wc4, wc6)
    avg_conf = max(conf4, conf6)

    merged = "\n".join(
        [
            "[OCR HEADER PSM6]",
            header_text,
            "[OCR BODY PSM4]",
            body_text,
            "[OCR FULL PAGE BEST]",
            best_full_text,
        ]
    )

    return OcrPageResult(
        page=page_no,
        text=normalize_text(merged),
        word_count=word_count,
        average_confidence=avg_conf,
    )


def ocr_pdf(pdf_bytes: bytes) -> Dict[str, Any]:
    images = render_pdf_pages(pdf_bytes, dpi=settings.ocr_dpi)
    results = [ocr_page(img, i) for i, img in enumerate(images, start=1)]
    merged = "\n\n".join(f"--- PAGE {r.page} ---\n{r.text}" for r in results)
    avg = round(sum(r.average_confidence for r in results) / len(results), 2) if results else 0.0
    return {
        "raw_text": normalize_text(merged),
        "pages": [r.__dict__ for r in results],
        "ocr_summary": {
            "page_count": len(results),
            "average_confidence": avg,
            "total_word_count": sum(r.word_count for r in results),
        },
    }