r"""
Professional Native + Scanned PDF Bill of Lading Extractor
=========================================================

Run:
    pip install fastapi uvicorn python-multipart pymupdf pillow pytesseract numpy openai pydantic-settings
    uvicorn bl_extractor_native_pdf_app:app --reload --host 0.0.0.0 --port 8000

Environment (.env):
    AZURE_OPENAI_ENDPOINT=https://YOUR-RESOURCE.openai.azure.com/
    AZURE_OPENAI_API_KEY=YOUR_KEY
    AZURE_OPENAI_API_VERSION=2024-08-01-preview
    AZURE_OPENAI_DEPLOYMENT=gpt-4o-mini
    TESSERACT_LANG=eng
    # Windows only, if needed:
    # TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe
"""

from __future__ import annotations

import io
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import fitz  # PyMuPDF
import numpy as np
import pytesseract
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from openai import AzureOpenAI
from PIL import Image, ImageOps
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


# ============================================================
# Settings
# ============================================================

class Settings(BaseSettings):
    azure_openai_endpoint: str = Field(default="", alias="AZURE_OPENAI_ENDPOINT")
    azure_openai_api_key: str = Field(default="", alias="AZURE_OPENAI_API_KEY")
    azure_openai_api_version: str = Field(default="2024-08-01-preview", alias="AZURE_OPENAI_API_VERSION")
    azure_openai_deployment: str = Field(default="gpt-4o-mini", alias="AZURE_OPENAI_DEPLOYMENT")

    ocr_dpi: int = Field(default=300, alias="OCR_DPI")
    tesseract_lang: str = Field(default="eng", alias="TESSERACT_LANG")
    tesseract_cmd: Optional[str] = Field(default=None, alias="TESSERACT_CMD")

    max_input_chars: int = Field(default=90000, alias="MAX_INPUT_CHARS")
    native_min_chars: int = Field(default=600, alias="NATIVE_MIN_CHARS")
    native_min_field_hits: int = Field(default=5, alias="NATIVE_MIN_FIELD_HITS")

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()

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


# ============================================================
# FastAPI
# ============================================================

app = FastAPI(
    title="Professional Native PDF + OCR Bill of Lading Extractor",
    version="3.0.0",
    description="Extracts native text when available, falls back to OCR for scanned/image-only PDFs, then extracts structured B/L JSON.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# Models
# ============================================================

class ContainerItem(BaseModel):
    container_number: Optional[str] = None
    seal_number: Optional[str] = None
    container_type: Optional[str] = None
    packages: Optional[str] = None
    gross_weight_kg: Optional[str] = None
    measurement_cbm: Optional[str] = None


class BLEntity(BaseModel):
    document_type: Optional[str] = "Bill of Lading"
    mesco_masterblno: Optional[str] = None
    mesco_bookingnumber: Optional[str] = None
    mesco_acidnumber: Optional[str] = None

    mesco_shippernamecontactno: Optional[str] = None
    mesco_shipperaddress: Optional[str] = None
    mesco_consigneenamecontactno: Optional[str] = None
    mesco_consigneeaddress: Optional[str] = None
    mesco_notify1: Optional[str] = None
    mesco_notifyaddress: Optional[str] = None

    mesco_vessel: Optional[str] = None
    mesco_voytruckno: Optional[str] = None
    mesco_origin: Optional[str] = None
    mesco_destination: Optional[str] = None

    mesco_cargodescription: Optional[str] = None
    cr401_totalgrossweight: Optional[str] = None
    cr401_totalvolume: Optional[str] = None
    cr401_totalpackages: Optional[str] = None
    mesco_nooforgbls: Optional[str] = None

    mesco_containertype: Optional[str] = None
    mesco_containertype2: Optional[str] = None
    mesco_containertype3: Optional[str] = None
    mesco_handlinginformation: Optional[str] = None
    mesco_freightpayableat: Optional[str] = None
    mesco_ponumber: Optional[str] = None
    mesco_customerreference: Optional[str] = None
    mesco_bltype: Optional[int] = None
    mesco_transporttype: Optional[int] = None
    mesco_loadtype: Optional[int] = None
    mesco_direction: Optional[int] = None
    cr401_totalteus: Optional[str] = None

    mesco_pcfreightterm: Optional[str] = None
    mesco_etdorigin: Optional[str] = None
    mesco_etadestination: Optional[str] = None
    mesco_pickupaddress: Optional[str] = None
    mesco_deliveryaddress: Optional[str] = None
    mesco_transhipmentport: Optional[str] = None
    mesco_importerstaxno: Optional[str] = None
    mesco_foreignsupplierregistrationnumber: Optional[str] = None
    mesco_incoterm: Optional[str] = None
    mesco_telexrelease: Optional[bool] = False
    mesco_imoclass: Optional[str] = None
    mesco_unnumber: Optional[str] = None

    mesco_hscode: Optional[str] = None
    mesco_dateofissue: Optional[str] = None
    mesco_placeofissue: Optional[str] = None
    mesco_shippedonboarddate: Optional[str] = None

    container_number: Optional[str] = None
    seal_number: Optional[str] = None
    containers: List[ContainerItem] = Field(default_factory=list)

    extraction_method: Optional[str] = None
    extraction_quality: Dict[str, Any] = Field(default_factory=dict)
    confidence: Dict[str, Any] = Field(default_factory=dict)
    warnings: List[str] = Field(default_factory=list)

    class Config:
        extra = "allow"


REQUIRED_FIELDS = list(BLEntity.model_fields.keys())


def empty_bl_entity() -> Dict[str, Any]:
    data = {k: None for k in REQUIRED_FIELDS}
    data["document_type"] = "Bill of Lading"
    data["containers"] = []
    data["mesco_telexrelease"] = False
    data["extraction_quality"] = {}
    data["confidence"] = {}
    data["warnings"] = []
    return data


# ============================================================
# Native PDF text extraction
# ============================================================

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
    """
    Rebuilds lines from page.get_text("words") using coordinates.
    This is stronger than page.get_text("text") for native PDFs because many B/Ls
    contain columns and absolute-positioned text.
    """
    if not words:
        return []

    # PyMuPDF word tuple:
    # x0, y0, x1, y1, word, block_no, line_no, word_no
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
                    parts.append("    ")  # visible column gap
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
    """
    Professional native-PDF extraction.
    It returns visual-line text from word coordinates and block text for redundancy.
    """
    warnings: List[str] = []
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid PDF: {exc}")

    pages: List[str] = []
    try:
        for i, page in enumerate(doc, start=1):
            words = page.get_text("words") or []
            visual_lines = _words_to_visual_lines(words)
            visual_text = "\n".join(visual_lines)

            block_text = _blocks_to_text(page)

            # Use both. The LLM benefits when field values appear in more than one native ordering.
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
        page_count = len(doc)
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


# ============================================================
# OCR extraction
# ============================================================

@dataclass
class OcrPageResult:
    page: int
    text: str
    word_count: int
    average_confidence: float


def render_pdf_pages(pdf_bytes: bytes, dpi: int = 300) -> List[Image.Image]:
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid PDF: {exc}")

    pages: List[Image.Image] = []
    zoom = dpi / 72.0
    try:
        for page in doc:
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            pages.append(Image.frombytes("RGB", [pix.width, pix.height], pix.samples))
    finally:
        doc.close()

    if not pages:
        raise HTTPException(status_code=400, detail="PDF has no readable pages.")
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

    # Header and body are handled differently because B/L numbers often live in small top-right boxes.
    width, height = processed.size
    header = processed.crop((0, 0, width, int(height * 0.28)))
    body = processed.crop((0, int(height * 0.22), width, height))

    try:
        header_text = _ocr_text(header, psm=6)
        body_text = _ocr_text(body, psm=4)
        full_text_4 = _ocr_text(processed, psm=4)
        full_text_6 = _ocr_text(processed, psm=6)
        wc4, conf4 = _ocr_data_confidence(processed, psm=4)
        wc6, conf6 = _ocr_data_confidence(processed, psm=6)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Tesseract OCR failed: {exc}. Current tesseract_cmd={pytesseract.pytesseract.tesseract_cmd!r}",
        )

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


def extract_pdf_text_professionally(pdf_bytes: bytes, force_ocr: bool = False) -> Dict[str, Any]:
    """
    Best practice:
    1) Try native extraction.
    2) If native is complete, use native.
    3) If native is sparse or force_ocr=True, OCR the PDF.
    4) If both exist, send a HYBRID input to the LLM, with native first and OCR second.
    """
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


# ============================================================
# Azure OpenAI structured extraction
# ============================================================

JSON_SCHEMA = {
    "name": "bill_of_lading_extraction",
    "schema": {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "document_type": {"type": ["string", "null"]},
            "mesco_masterblno": {"type": ["string", "null"]},
            "mesco_bookingnumber": {"type": ["string", "null"]},
            "mesco_acidnumber": {"type": ["string", "null"]},
            "mesco_shippernamecontactno": {"type": ["string", "null"]},
            "mesco_shipperaddress": {"type": ["string", "null"]},
            "mesco_consigneenamecontactno": {"type": ["string", "null"]},
            "mesco_consigneeaddress": {"type": ["string", "null"]},
            "mesco_notify1": {"type": ["string", "null"]},
            "mesco_notifyaddress": {"type": ["string", "null"]},
            "mesco_vessel": {"type": ["string", "null"]},
            "mesco_voytruckno": {"type": ["string", "null"]},
            "mesco_origin": {"type": ["string", "null"]},
            "mesco_destination": {"type": ["string", "null"]},
            "mesco_cargodescription": {"type": ["string", "null"]},
            "cr401_totalgrossweight": {"type": ["string", "null"]},
            "cr401_totalvolume": {"type": ["string", "null"]},
            "cr401_totalpackages": {"type": ["string", "null"]},
            "mesco_nooforgbls": {"type": ["string", "null"]},
            "mesco_containertype": {"type": ["string", "null"]},
            "mesco_containertype2": {"type": ["string", "null"]},
            "mesco_containertype3": {"type": ["string", "null"]},
            "mesco_handlinginformation": {"type": ["string", "null"]},
            "mesco_freightpayableat": {"type": ["string", "null"]},
            "mesco_ponumber": {"type": ["string", "null"]},
            "mesco_customerreference": {"type": ["string", "null"]},
            "mesco_bltype": {"type": ["integer", "null"]},
            "mesco_transporttype": {"type": ["integer", "null"]},
            "mesco_loadtype": {"type": ["integer", "null"]},
            "mesco_direction": {"type": ["integer", "null"]},
            "cr401_totalteus": {"type": ["string", "null"]},
            "mesco_pcfreightterm": {"type": ["string", "null"]},
            "mesco_etdorigin": {"type": ["string", "null"]},
            "mesco_etadestination": {"type": ["string", "null"]},
            "mesco_pickupaddress": {"type": ["string", "null"]},
            "mesco_deliveryaddress": {"type": ["string", "null"]},
            "mesco_transhipmentport": {"type": ["string", "null"]},
            "mesco_importerstaxno": {"type": ["string", "null"]},
            "mesco_foreignsupplierregistrationnumber": {"type": ["string", "null"]},
            "mesco_incoterm": {"type": ["string", "null"]},
            "mesco_telexrelease": {"type": ["boolean", "null"]},
            "mesco_imoclass": {"type": ["string", "null"]},
            "mesco_unnumber": {"type": ["string", "null"]},
            "mesco_hscode": {"type": ["string", "null"]},
            "mesco_dateofissue": {"type": ["string", "null"]},
            "mesco_placeofissue": {"type": ["string", "null"]},
            "mesco_shippedonboarddate": {"type": ["string", "null"]},
            "container_number": {"type": ["string", "null"]},
            "seal_number": {"type": ["string", "null"]},
            "containers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "container_number": {"type": ["string", "null"]},
                        "seal_number": {"type": ["string", "null"]},
                        "container_type": {"type": ["string", "null"]},
                        "packages": {"type": ["string", "null"]},
                        "gross_weight_kg": {"type": ["string", "null"]},
                        "measurement_cbm": {"type": ["string", "null"]},
                    },
                    "required": [
                        "container_number", "seal_number", "container_type",
                        "packages", "gross_weight_kg", "measurement_cbm"
                    ],
                },
            },
            "confidence": {"type": "object"},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "document_type", "mesco_masterblno", "mesco_bookingnumber", "mesco_acidnumber",
            "mesco_shippernamecontactno", "mesco_shipperaddress",
            "mesco_consigneenamecontactno", "mesco_consigneeaddress",
            "mesco_notify1", "mesco_notifyaddress", "mesco_vessel", "mesco_voytruckno",
            "mesco_origin", "mesco_destination", "mesco_cargodescription",
            "cr401_totalgrossweight", "cr401_totalvolume", "cr401_totalpackages",
            "mesco_nooforgbls", "mesco_containertype", "mesco_containertype2",
            "mesco_containertype3", "mesco_handlinginformation",
            "mesco_freightpayableat", "mesco_ponumber", "mesco_customerreference",
            "mesco_bltype", "mesco_transporttype", "mesco_loadtype", "mesco_direction",
            "cr401_totalteus", "mesco_pcfreightterm", "mesco_etdorigin",
            "mesco_etadestination", "mesco_pickupaddress", "mesco_deliveryaddress",
            "mesco_transhipmentport", "mesco_importerstaxno",
            "mesco_foreignsupplierregistrationnumber", "mesco_incoterm",
            "mesco_telexrelease", "mesco_imoclass", "mesco_unnumber", "mesco_hscode",
            "mesco_dateofissue", "mesco_placeofissue", "mesco_shippedonboarddate",
            "container_number", "seal_number", "containers", "confidence", "warnings",
        ],
    },
}


SYSTEM_PROMPT = """
You are a professional Bill of Lading extraction engine for Mesco CRM / Dynamics 365.

You receive text extracted from a PDF. It may be:
- native PDF text from coordinate-based extraction,
- OCR text from a scanned/image PDF,
- or hybrid text containing both.

Return ONLY valid JSON that follows the provided schema.

Important native-PDF rules:
- Native PDFs often store text out of visual order. Use labels and nearby values, not only line order.
- If there are [NATIVE PDF TEXT] and [OCR TEXT FALLBACK / VISUAL TEXT] sections, prefer the value that is most clearly attached to a field label.
- OCR may reveal visual text that native text misses.

Critical Bill of Lading rules:
- mesco_masterblno is the B/L number. It is usually near labels like "BILL OF LADING NO", "B/L No.", "B/L NO", or "BL NO".
- Do not confuse B/L number with booking number, ACID number, container number, postal code, company name, or address.
- mesco_bookingnumber is booking/reference number when explicitly labeled booking number.
- mesco_acidnumber is the Egyptian ACID number. Return digits only.
- Extract shipper, consignee, notify party, vessel, voyage, origin/loading port, destination/discharge port.
- Vessel names must not include the port of loading. If text says "CMA CGM TIGA 0NVLOS1MA HAMBURG", vessel is "CMA CGM TIGA", voyage is "0NVLOS1MA", origin is "HAMBURG".
- Extract container numbers as 4 letters + 7 digits, seal numbers, container type, packages, gross weight, and CBM.
- Freight terms: if "FREIGHT COLLECT", set mesco_pcfreightterm="COLLECT"; if "FREIGHT PREPAID", set "PREPAID".
- mesco_transporttype must be 300000000 for sea B/Ls.
- mesco_loadtype: FCL/container present = 300000000; LCL explicitly stated = 300000001.
- mesco_direction: import = 300000000 if destination/discharge is Egypt; export = 300000001 if origin/loading is Egypt.
- mesco_bltype: if ORIGINAL appears and it is not only boilerplate, use 886150001. If NON-NEGOTIABLE is prominent, leave null unless your CRM mapping requires another value.
- Do not guess. Use null when a value is not visible.
"""


def get_azure_client() -> AzureOpenAI:
    if not settings.azure_openai_endpoint or not settings.azure_openai_api_key:
        raise HTTPException(
            status_code=500,
            detail="Azure OpenAI is not configured. Set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY.",
        )
    return AzureOpenAI(
        azure_endpoint=settings.azure_openai_endpoint,
        api_key=settings.azure_openai_api_key,
        api_version=settings.azure_openai_api_version,
    )


def extract_with_azure_openai(extracted_text: str) -> Dict[str, Any]:
    text = normalize_text(extracted_text)
    if not text:
        raise HTTPException(status_code=400, detail="No text was extracted from the PDF.")

    client = get_azure_client()
    safe_text = text[: settings.max_input_chars]

    try:
        response = client.chat.completions.create(
            model=settings.azure_openai_deployment,
            temperature=0,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "Extract this Bill of Lading:\n\n" + safe_text},
            ],
            response_format={"type": "json_schema", "json_schema": JSON_SCHEMA},
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Azure OpenAI extraction failed: {exc}")

    content = response.choices[0].message.content
    if not content:
        raise HTTPException(status_code=502, detail="Azure OpenAI returned empty content.")

    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail=f"Azure OpenAI returned invalid JSON: {exc}")

    return validate_and_correct(data, extracted_text)


# ============================================================
# Deterministic post-processing and regex fallback
# ============================================================

EGYPT_PORTS = {
    "ALEXANDRIA", "PORT SAID", "DAMIETTA", "SOKHNA", "EL SOKHNA",
    "DEKHEILA", "ADABIYA", "PORT SAID WEST", "AIN SOKHNA",
}

KNOWN_PORTS = {
    *EGYPT_PORTS,
    "HAMBURG", "ROTTERDAM", "GOTHENBURG", "ANTWERP", "SHANGHAI",
    "SINGAPORE", "JEBEL ALI", "PIRAEUS", "FELIXSTOWE", "LE HAVRE",
    "VALENCIA", "BARCELONA", "GENOA", "ISTANBUL", "MERSIN", "AMBARLI",
    "COLOMBO", "PORT KLANG", "TANJUNG PELEPAS", "BUSAN", "NINGBO",
    "QINGDAO", "TIANJIN", "HONG KONG", "YOKOHAMA", "NHAVA SHEVA",
    "DURBAN", "MOMBASA", "LAGOS", "AQABA", "BEIRUT", "HAIFA",
    "LIMASSOL", "MALTA", "GIOIA TAURO",
}

BL_BLACKLIST = {
    "ORIGINAL", "TELEX", "RELEASE", "EXPRESS", "FREIGHT", "COLLECT", "PREPAID",
    "SHIPPER", "CONSIGNEE", "NOTIFY", "VESSEL", "VOYAGE", "CONTAINER", "SEAL",
    "WEIGHT", "MEASUREMENT", "DESCRIPTION", "GOODS", "PORT", "LOADING",
    "DISCHARGE", "DELIVERY", "RECEIPT", "PLACE", "DATE", "NUMBER", "BILL",
    "LADING", "BOOKING", "REFERENCE", "ACID", "ORDER", "INVOICE", "PACKING",
    "GROSS", "NET", "TARE", "TOTAL", "PAGE", "COPY", "DRAFT", "SIGNED",
}


def add_warning(data: Dict[str, Any], message: str) -> None:
    data.setdefault("warnings", [])
    if message not in data["warnings"]:
        data["warnings"].append(message)


def clean_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    value = re.sub(r"\s+", " ", str(value)).strip(" ,;:-")
    return value or None


def normalize_digits(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    return digits or None


def is_container_number(value: Optional[str]) -> bool:
    return bool(value and re.fullmatch(r"[A-Z]{4}\d{7}", value.strip().upper().replace(" ", "")))


def normalize_numeric(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(" ", "")
    # 335,000 in B/L often means 335.000 kg; keep decimal style.
    if "," in text and "." not in text:
        parts = text.split(",")
        text = "".join(parts[:-1]) + "." + parts[-1] if len(parts[-1]) == 3 else text.replace(",", ".")
    else:
        text = text.replace(",", "")
    m = re.search(r"\d+(?:\.\d+)?", text)
    return m.group(0) if m else None


def is_likely_bl_number(candidate: str, current_acid: Optional[str] = None) -> bool:
    c = candidate.upper().strip()
    c_compact = re.sub(r"[\s\-]", "", c)

    if len(c_compact) < 5 or len(c_compact) > 25:
        return False
    if c in BL_BLACKLIST or c_compact in BL_BLACKLIST:
        return False
    if is_container_number(c_compact):
        return False
    if current_acid and c_compact == re.sub(r"\D", "", current_acid):
        return False
    if c_compact.isdigit() and len(c_compact) == 19:
        return False

    if c_compact.isdigit():
        return 5 <= len(c_compact) <= 20

    return any(ch.isalpha() for ch in c_compact) and any(ch.isdigit() for ch in c_compact)


def extract_bl_number_regex(text: str, current_acid: Optional[str] = None) -> Optional[str]:
    upper = text.upper()
    value = r"([A-Z0-9][A-Z0-9 \-]{3,30}[A-Z0-9])"
    patterns = [
        rf"(?:BILL\s*OF\s*LADING\s*(?:NO|NUMBER|#)|B/L\s*(?:NO|NUMBER|#)|BL\s*(?:NO|NUMBER|#))\.?\s*[:\-]?\s*\n?\s*{value}",
        rf"\bB/L\s*NO\.?\s*{value}",
        rf"\bBLNO\.?\s*[:\-]?\s*{value}",
        rf"\bREF(?:ERENCE)?\s*(?:NO|NUMBER|#)?\.?\s*[:\-]?\s*{value}",
    ]
    for pat in patterns:
        m = re.search(pat, upper, flags=re.I | re.S)
        if not m:
            continue
        val = re.split(r"\n| {4,}", m.group(1))[0].strip(" ,;:-")
        val_compact = val.replace(" ", "")
        if is_likely_bl_number(val_compact, current_acid):
            return val_compact

    # Header fallback only, because B/L number is normally in header.
    header = upper[:2000]
    for val in re.findall(r"\b[A-Z]{2,5}\d[A-Z0-9\-]{4,20}\b|\b\d{5,20}\b", header):
        if is_likely_bl_number(val, current_acid):
            return val
    return None


def extract_acid_regex(text: str) -> Optional[str]:
    upper = text.upper()
    m = re.search(r"\bACID\s*(?:NO|NUMBER)?\.?\s*[:\-]?\s*([0-9][0-9\s\-]{12,30})", upper)
    if m:
        digits = normalize_digits(m.group(1))
        if digits and 10 <= len(digits) <= 19:
            return digits
    # Strict 19-digit fallback
    m = re.search(r"\b(\d{19})\b", upper)
    return m.group(1) if m else None


def extract_hs_code_regex(text: str) -> Optional[str]:
    m = re.search(r"HS\s*CODE\s*[:\-]?\s*([0-9\s\-/|]{6,80})", text, flags=re.I)
    if not m:
        return None
    codes = re.findall(r"\b\d{6,10}\b", m.group(1))
    out = []
    for c in codes:
        if c not in out:
            out.append(c)
    return "|".join(out) if out else None


def extract_containers_regex(text: str) -> List[Dict[str, Optional[str]]]:
    containers: List[Dict[str, Optional[str]]] = []
    compact = text.upper()
    for m in re.finditer(r"\b([A-Z]{4}\d{7})\b(?:[/\s\-]+([A-Z0-9]{4,20}))?", compact):
        container = m.group(1)
        seal = m.group(2)
        if container not in [c["container_number"] for c in containers]:
            containers.append({
                "container_number": container,
                "seal_number": seal,
                "container_type": None,
                "packages": None,
                "gross_weight_kg": None,
                "measurement_cbm": None,
            })
    return containers


def extract_vessel_voyage_port_regex(text: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    upper = text.upper()

    # Example: CMA CGM TIGA 0NVLOS1MA HAMBURG
    for port in sorted(KNOWN_PORTS, key=len, reverse=True):
        pat = rf"\b([A-Z][A-Z ]{{3,40}}?)\s+([A-Z0-9]{{5,12}})\s+{re.escape(port)}\b"
        m = re.search(pat, upper)
        if m:
            vessel = clean_value(m.group(1))
            voyage = clean_value(m.group(2))
            return vessel, voyage, port

    # Example: VESSEL: CMA CGM TIGA / 0NVLOS1MA
    m = re.search(r"(?:OCEAN\s+)?VESSEL\s*[:\-]?\s*([A-Z][A-Z0-9 ]{3,50})(?:\s*/\s*|\s+)([A-Z0-9]{4,12})", upper)
    if m:
        return clean_value(m.group(1)), clean_value(m.group(2)), None

    return None, None, None


def infer_direction(data: Dict[str, Any]) -> None:
    if data.get("mesco_direction") is not None:
        return
    dest = (data.get("mesco_destination") or "").upper()
    origin = (data.get("mesco_origin") or "").upper()
    if any(p in dest for p in EGYPT_PORTS):
        data["mesco_direction"] = 300000000
    elif any(p in origin for p in EGYPT_PORTS):
        data["mesco_direction"] = 300000001


def validate_and_correct(data: Dict[str, Any], raw_text: str) -> Dict[str, Any]:
    base = empty_bl_entity()
    base.update(data or {})
    data = base

    for k, v in list(data.items()):
        if isinstance(v, str):
            data[k] = clean_value(v)

    # Regex fallbacks
    if not data.get("mesco_acidnumber"):
        acid = extract_acid_regex(raw_text)
        if acid:
            data["mesco_acidnumber"] = acid
            add_warning(data, "mesco_acidnumber filled by regex fallback.")

    if not data.get("mesco_masterblno"):
        bl = extract_bl_number_regex(raw_text, data.get("mesco_acidnumber"))
        if bl:
            data["mesco_masterblno"] = bl
            add_warning(data, "mesco_masterblno filled by regex fallback.")

    if not data.get("mesco_hscode"):
        hs = extract_hs_code_regex(raw_text)
        if hs:
            data["mesco_hscode"] = hs
            add_warning(data, "mesco_hscode filled by regex fallback.")

    vessel, voyage, port = extract_vessel_voyage_port_regex(raw_text)
    if vessel and not data.get("mesco_vessel"):
        data["mesco_vessel"] = vessel
        add_warning(data, "mesco_vessel filled by regex fallback.")
    if voyage and not data.get("mesco_voytruckno"):
        data["mesco_voytruckno"] = voyage
        add_warning(data, "mesco_voytruckno filled by regex fallback.")
    if port and not data.get("mesco_origin"):
        data["mesco_origin"] = port
        add_warning(data, "mesco_origin inferred from vessel/voyage/port line.")

    found_containers = extract_containers_regex(raw_text)
    if not data.get("containers") and found_containers:
        data["containers"] = found_containers
        add_warning(data, "containers filled by regex fallback.")

    # Normalize ACID and avoid B/L = ACID
    if data.get("mesco_acidnumber"):
        acid_digits = normalize_digits(data["mesco_acidnumber"])
        data["mesco_acidnumber"] = acid_digits if acid_digits and len(acid_digits) >= 10 else None

    if data.get("mesco_masterblno") and data.get("mesco_acidnumber"):
        bl_compact = re.sub(r"\D", "", str(data["mesco_masterblno"]))
        acid = str(data["mesco_acidnumber"])
        if bl_compact and bl_compact == acid:
            alt = extract_bl_number_regex(raw_text, acid)
            if alt and re.sub(r"\D", "", alt) != acid:
                data["mesco_masterblno"] = alt
            else:
                add_warning(data, "B/L number equals ACID number; verify manually.")

    # Normalize numeric totals
    data["cr401_totalgrossweight"] = normalize_numeric(data.get("cr401_totalgrossweight"))
    data["cr401_totalvolume"] = normalize_numeric(data.get("cr401_totalvolume"))

    # Normalize containers
    cleaned_containers: List[Dict[str, Optional[str]]] = []
    for item in data.get("containers") or []:
        if isinstance(item, BaseModel):
            item = item.model_dump()
        if not isinstance(item, dict):
            continue
        c = {
            "container_number": clean_value(item.get("container_number")),
            "seal_number": clean_value(item.get("seal_number")),
            "container_type": clean_value(item.get("container_type")),
            "packages": clean_value(item.get("packages")),
            "gross_weight_kg": normalize_numeric(item.get("gross_weight_kg")),
            "measurement_cbm": normalize_numeric(item.get("measurement_cbm")),
        }
        if c["container_number"]:
            c["container_number"] = c["container_number"].upper().replace(" ", "")
        if any(c.values()):
            cleaned_containers.append(c)

    data["containers"] = cleaned_containers
    if cleaned_containers:
        first = cleaned_containers[0]
        data["container_number"] = data.get("container_number") or first.get("container_number")
        data["seal_number"] = data.get("seal_number") or first.get("seal_number")
        data["mesco_containertype"] = data.get("mesco_containertype") or first.get("container_type")

    # Inferences
    upper = raw_text.upper()
    data["mesco_transporttype"] = data.get("mesco_transporttype") or 300000000

    if data.get("mesco_loadtype") is None:
        if "LCL" in upper:
            data["mesco_loadtype"] = 300000001
        elif cleaned_containers or re.search(r"\bFCL\b", upper):
            data["mesco_loadtype"] = 300000000

    if not data.get("mesco_pcfreightterm"):
        if "FREIGHT COLLECT" in upper:
            data["mesco_pcfreightterm"] = "COLLECT"
        elif "FREIGHT PREPAID" in upper:
            data["mesco_pcfreightterm"] = "PREPAID"

    if "TELEX RELEASE" in upper or "EXPRESS RELEASE" in upper:
        data["mesco_telexrelease"] = True

    infer_direction(data)

    data.setdefault("confidence", {})
    data["confidence"]["post_validation"] = "completed"
    data["confidence"]["bl_number_rule"] = "accepted" if data.get("mesco_masterblno") else "missing"
    data["confidence"]["container_number_rule"] = "accepted" if data.get("container_number") else "missing"

    return BLEntity(**data).model_dump()


# ============================================================
# Routes
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def root() -> str:
    return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>B/L Extractor</title>
  <style>
    body{font-family:Arial,sans-serif;margin:40px;max-width:900px}
    input,button{font-size:16px;padding:8px;margin:6px 0}
    pre{white-space:pre-wrap;background:#f7f7f7;padding:16px;border-radius:8px}
  </style>
</head>
<body>
  <h1>Professional Native PDF + OCR Bill of Lading Extractor</h1>
  <p>Upload a PDF. The API tries native coordinate text first, then OCR/hybrid if needed.</p>
  <form id="f">
    <input type="file" id="file" name="file" accept="application/pdf" required />
    <label><input type="checkbox" id="force_ocr" /> Force OCR/hybrid</label><br/>
    <button type="submit">Extract</button>
  </form>
  <pre id="out"></pre>
<script>
document.getElementById('f').addEventListener('submit', async (e)=>{
  e.preventDefault();
  const fd = new FormData();
  fd.append('file', document.getElementById('file').files[0]);
  const force = document.getElementById('force_ocr').checked ? '?force_ocr=true' : '';
  const res = await fetch('/extract' + force, {method:'POST', body:fd});
  document.getElementById('out').textContent = JSON.stringify(await res.json(), null, 2);
});
</script>
</body>
</html>
"""


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok", "version": app.version}


@app.post("/extract-text")
async def extract_text(file: UploadFile = File(...), force_ocr: bool = False) -> JSONResponse:
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Please upload a PDF file.")
    pdf_bytes = await file.read()
    result = extract_pdf_text_professionally(pdf_bytes, force_ocr=force_ocr)
    return JSONResponse(result)


@app.post("/extract")
async def extract(file: UploadFile = File(...), force_ocr: bool = False) -> JSONResponse:
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Please upload a PDF file.")

    pdf_bytes = await file.read()
    text_result = extract_pdf_text_professionally(pdf_bytes, force_ocr=force_ocr)
    data = extract_with_azure_openai_mapped(text_result["text"])

    data["extraction_method"] = text_result["method"]
    data["extraction_quality"] = text_result["quality"]
    return JSONResponse(data)


@app.post("/extract-without-ai")
async def extract_without_ai(file: UploadFile = File(...), force_ocr: bool = False) -> JSONResponse:
    """
    Useful for testing the native/OCR layer without Azure OpenAI.
    It returns deterministic regex fallbacks only, not full semantic extraction.
    """
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Please upload a PDF file.")

    pdf_bytes = await file.read()
    text_result = extract_pdf_text_professionally(pdf_bytes, force_ocr=force_ocr)
    data = validate_and_correct(empty_bl_entity(), text_result["text"])
    data["extraction_method"] = text_result["method"]
    data["extraction_quality"] = text_result["quality"]
    data["raw_text_preview"] = text_result["text"][:5000]
    return JSONResponse(data)

# ============================================================
# AI-first CRM mapping helpers and endpoint
# ============================================================

CRM_FIELD_ORDER = [
    "document_type",
    "mesco_masterblno",
    "mesco_bookingnumber",
    "mesco_acidnumber",
    "mesco_shippernamecontactno",
    "mesco_shipperaddress",
    "mesco_consigneenamecontactno",
    "mesco_consigneeaddress",
    "mesco_notify1",
    "mesco_notifyaddress",
    "mesco_vessel",
    "mesco_voytruckno",
    "mesco_origin",
    "mesco_destination",
    "mesco_cargodescription",
    "cr401_totalgrossweight",
    "cr401_totalvolume",
    "cr401_totalpackages",
    "mesco_nooforgbls",
    "mesco_containertype",
    "mesco_containertype2",
    "mesco_containertype3",
    "mesco_handlinginformation",
    "mesco_freightpayableat",
    "mesco_ponumber",
    "mesco_customerreference",
    "mesco_bltype",
    "mesco_transporttype",
    "mesco_loadtype",
    "mesco_direction",
    "cr401_totalteus",
    "mesco_pcfreightterm",
    "mesco_etdorigin",
    "mesco_etadestination",
    "mesco_pickupaddress",
    "mesco_deliveryaddress",
    "mesco_transhipmentport",
    "mesco_importerstaxno",
    "mesco_foreignsupplierregistrationnumber",
    "mesco_incoterm",
    "mesco_telexrelease",
    "mesco_imoclass",
    "mesco_unnumber",
    "mesco_hscode",
    "mesco_dateofissue",
    "mesco_placeofissue",
    "mesco_shippedonboarddate",
]

FIELD_DESCRIPTIONS_FOR_AI = {
    "mesco_masterblno": "Bill of Lading number only. Label examples: BILL OF LADING NO, B/L No., BL NO. Never use booking, ACID, container, postal code, company name.",
    "mesco_bookingnumber": "Booking number only. Label examples: BOOKING NO, Booking Ref.",
    "mesco_acidnumber": "Egypt ACID number. Return digits only, normally 19 digits but accept visible shorter/longer only when explicitly labeled ACID.",
    "mesco_shippernamecontactno": "Shipper company/name and contact line only, not full address unless no separation is possible.",
    "mesco_shipperaddress": "Full shipper address.",
    "mesco_consigneenamecontactno": "Consignee company/name and contact line only, not full address unless no separation is possible.",
    "mesco_consigneeaddress": "Full consignee address.",
    "mesco_notify1": "Notify party name/company.",
    "mesco_notifyaddress": "Notify party address.",
    "mesco_vessel": "Vessel name only. Do not include voyage or port.",
    "mesco_voytruckno": "Voyage number/truck number only.",
    "mesco_origin": "Port of loading/origin.",
    "mesco_destination": "Port of discharge/destination.",
    "mesco_cargodescription": "Actual cargo/goods description. Exclude legal boilerplate unless it is part of cargo description.",
    "cr401_totalgrossweight": "Total gross weight in kg as numeric string where possible.",
    "cr401_totalvolume": "Total measurement/volume in CBM as numeric string where possible.",
    "cr401_totalpackages": "Total packages / number of packages.",
    "mesco_nooforgbls": "Number of original B/Ls.",
    "mesco_containertype": "Primary container type, e.g. 40 HC, 20 GP, 40 High Cube.",
    "mesco_pcfreightterm": "PREPAID or COLLECT.",
    "mesco_freightpayableat": "Freight payable at location/city.",
    "mesco_transporttype": "Use 300000000 for sea shipments.",
    "mesco_loadtype": "Use 300000000 for FCL/full container; 300000001 for LCL.",
    "mesco_direction": "Use 300000000 for import to Egypt; 300000001 for export from Egypt.",
    "mesco_bltype": "Use 886150001 only for Original B/L if ORIGINAL is actually stamped/issued. Non-negotiable is not original.",
    "mesco_telexrelease": "True only if TELEX RELEASE or EXPRESS RELEASE is visible.",
    "mesco_hscode": "HS code(s), 6-10 digits, pipe-separated if multiple.",
}


def build_ai_evidence_pack(text: str) -> Dict[str, Any]:
    """Provide the model with deterministic candidates so it maps fields correctly."""
    bl = extract_bl_number_regex(text)
    acid = extract_acid_regex(text)
    vessel, voyage, bled_origin = extract_vessel_voyage_port_regex(text)
    containers = extract_containers_regex(text)
    hs = extract_hs_code_regex(text)
    upper = text.upper()

    ports_seen = [p for p in sorted(KNOWN_PORTS, key=len, reverse=True) if p in upper]

    return {
        "candidate_bl_number": bl,
        "candidate_acid_number": acid,
        "candidate_hs_code": hs,
        "candidate_vessel": vessel,
        "candidate_voyage": voyage,
        "candidate_origin_from_vessel_line": bled_origin,
        "candidate_containers": containers,
        "ports_seen_in_text": ports_seen[:20],
        "freight_collect_seen": "FREIGHT COLLECT" in upper,
        "freight_prepaid_seen": "FREIGHT PREPAID" in upper,
        "telex_release_seen": "TELEX RELEASE" in upper or "EXPRESS RELEASE" in upper,
        "non_negotiable_seen": "NON-NEGOTIABLE" in upper or "NON NEGOTIABLE" in upper,
        "original_seen": "ORIGINAL" in upper,
    }


def to_crm_payload(data: Dict[str, Any], include_nulls: bool = False) -> Dict[str, Any]:
    """
    Final Dynamics/Mesco CRM payload.
    Keeps only CRM fields in the correct names/order and removes internal extraction metadata.
    """
    payload: Dict[str, Any] = {}
    for field in CRM_FIELD_ORDER:
        value = data.get(field)
        if include_nulls or value not in (None, "", [], {}):
            payload[field] = value

    # Keep top-level primary container fields for existing CRM integrations.
    for field in ("container_number", "seal_number", "containers"):
        value = data.get(field)
        if include_nulls or value not in (None, "", [], {}):
            payload[field] = value

    return payload


AI_MAPPING_SYSTEM_PROMPT = SYSTEM_PROMPT + """

CRM MAPPING MODE:
- You are mapping directly to Mesco CRM field names.
- Every output key must be one of the JSON schema keys. Do not invent user-facing labels.
- Prefer explicit field labels over guesses.
- Use the deterministic evidence pack only as hints; the PDF text remains the source of truth.
- If a candidate conflicts with a clearly labeled value in the PDF text, use the clearly labeled PDF value.
- For B/L number, prefer the value after BILL OF LADING NO / B/L NO over every other candidate.
- For the sample SACO style: BOOKING NO is booking, BILL OF LADING NO is mesco_masterblno, EXPORT REFERENCES can be customer reference.
- For rows like "CMA CGM TIGA 0NVLOS1MA HAMBURG": vessel=CMA CGM TIGA, voyage=0NVLOS1MA, origin=HAMBURG.
- For rows like "PORT SAID PORT SAID WEST": destination/port of discharge=PORT SAID and delivery=PORT SAID WEST when labels support that.
- Container syntax like "TCLU5466216/B135553" means container_number=TCLU5466216 and seal_number=B135553.

Field meanings:
""" + json.dumps(FIELD_DESCRIPTIONS_FOR_AI, indent=2)


def extract_with_azure_openai_mapped(extracted_text: str) -> Dict[str, Any]:
    """
    AI-first extraction with deterministic evidence and final CRM mapping validation.
    This is the recommended function used by /extract-ai-full.
    """
    text = normalize_text(extracted_text)
    if not text:
        raise HTTPException(status_code=400, detail="No text was extracted from the PDF.")

    evidence = build_ai_evidence_pack(text)
    client = get_azure_client()
    safe_text = text[: settings.max_input_chars]

    user_prompt = (
        "Extract and map this Bill of Lading into the exact Mesco CRM JSON schema.\n\n"
        "DETERMINISTIC EVIDENCE PACK (hints, not a replacement for the text):\n"
        + json.dumps(evidence, ensure_ascii=False, indent=2)
        + "\n\nPDF EXTRACTED TEXT:\n"
        + safe_text
    )

    try:
        response = client.chat.completions.create(
            model=settings.azure_openai_deployment,
            temperature=0,
            messages=[
                {"role": "system", "content": AI_MAPPING_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_schema", "json_schema": JSON_SCHEMA},
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Azure OpenAI mapped extraction failed: {exc}")

    content = response.choices[0].message.content
    if not content:
        raise HTTPException(status_code=502, detail="Azure OpenAI returned empty content.")

    try:
        raw_ai = json.loads(content)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail=f"Azure OpenAI returned invalid JSON: {exc}")

    validated = validate_and_correct(raw_ai, extracted_text)
    validated["ai_evidence"] = evidence
    validated["crm_payload"] = to_crm_payload(validated, include_nulls=False)
    validated["crm_payload_with_nulls"] = to_crm_payload(validated, include_nulls=True)
    validated.setdefault("confidence", {})["ai_mapping_mode"] = "enabled"
    return validated


@app.post("/extract-ai-full")
async def extract_ai_full(
    file: UploadFile = File(...),
    force_ocr: bool = True,
    include_raw_text: bool = False,
) -> JSONResponse:
    """
    Recommended endpoint.
    - Uses native + OCR hybrid by default for highest accuracy.
    - Sends the complete extracted text to Azure OpenAI.
    - Returns validated Mesco CRM field mapping plus crm_payload.
    """
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Please upload a PDF file.")

    pdf_bytes = await file.read()
    text_result = extract_pdf_text_professionally(pdf_bytes, force_ocr=force_ocr)
    data = extract_with_azure_openai_mapped(text_result["text"])
    data["extraction_method"] = text_result["method"]
    data["extraction_quality"] = text_result["quality"]
    if include_raw_text:
        data["raw_text"] = text_result["text"]
    return JSONResponse(data)
