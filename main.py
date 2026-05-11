import io
import json
import os
import re
from typing import Any, Dict, List, Optional

import fitz
import numpy as np
import pytesseract
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from openai import AzureOpenAI
from PIL import Image, ImageOps
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


# =========================
# Settings
# =========================

class Settings(BaseSettings):
    azure_openai_endpoint: str = Field(default="", alias="AZURE_OPENAI_ENDPOINT")
    azure_openai_api_key: str = Field(default="", alias="AZURE_OPENAI_API_KEY")
    azure_openai_api_version: str = Field(default="2024-08-01-preview", alias="AZURE_OPENAI_API_VERSION")
    azure_openai_deployment: str = Field(default="gpt-4o-mini", alias="AZURE_OPENAI_DEPLOYMENT")

    ocr_dpi: int = Field(default=300, alias="OCR_DPI")
    tesseract_lang: str = Field(default="eng", alias="TESSERACT_LANG")
    return_raw_text: bool = Field(default=False, alias="RETURN_RAW_TEXT")
    max_ocr_chars: int = Field(default=60000, alias="MAX_OCR_CHARS")

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()


# =========================
# FastAPI
# =========================

app = FastAPI(
    title="Intelligent Bill of Lading Extractor",
    version="2.0.0",
    description="PDF OCR + Azure OpenAI structured Bill of Lading extraction"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================
# Models
# =========================

class TextExtractionRequest(BaseModel):
    ocr_text: str
    return_raw_text: bool = False


class ContainerItem(BaseModel):
    container_number: Optional[str] = None
    seal_number: Optional[str] = None
    container_type: Optional[str] = None
    packages: Optional[str] = None
    gross_weight_kg: Optional[str] = None
    measurement_cbm: Optional[str] = None


class BLEntity(BaseModel):
    document_type: Optional[str] = "Bill of Lading"
    mesco_masterblno: Optional[str] = Field(None, alias="bl_number")
    mesco_bookingnumber: Optional[str] = Field(None, alias="booking_number")
    mesco_acidnumber: Optional[str] = Field(None, alias="acid_number")
    
    mesco_shippernamecontactno: Optional[str] = Field(None, alias="shipper_name")
    mesco_shipperaddress: Optional[str] = Field(None, alias="shipper_address")
    mesco_consigneenamecontactno: Optional[str] = Field(None, alias="consignee_name")
    mesco_consigneeaddress: Optional[str] = Field(None, alias="consignee_address")
    
    mesco_vessel: Optional[str] = Field(None, alias="vessel")
    mesco_voytruckno: Optional[str] = Field(None, alias="voyage_number")
    
    mesco_origin: Optional[str] = Field(None, alias="port_of_loading")
    mesco_destination: Optional[str] = Field(None, alias="port_of_discharge")
    
    mesco_cargodescription: Optional[str] = Field(None, alias="description_of_goods")
    cr401_totalgrossweight: Optional[str] = Field(None, alias="gross_weight_kg")
    cr401_totalvolume: Optional[str] = Field(None, alias="measurement_cbm")
    cr401_totalpackages: Optional[str] = Field(None, alias="number_of_packages")
    mesco_nooforgbls: Optional[str] = Field(None, alias="number_of_original_bls")

    container_number: Optional[str] = None
    seal_number: Optional[str] = None
    containers: List[ContainerItem] = Field(default_factory=list)

    confidence: Dict[str, Any] = Field(default_factory=dict)
    warnings: List[str] = Field(default_factory=list)

    class Config:
        populate_by_name = True


# =========================
# OCR functions
# =========================

def render_pdf_pages(pdf_bytes: bytes, dpi: int = 300) -> List[Image.Image]:
    """Convert PDF pages to PIL images."""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid PDF: {exc}")

    pages: List[Image.Image] = []
    zoom = dpi / 72

    try:
        for page in doc:
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            pages.append(img)
    finally:
        doc.close()

    if not pages:
        raise HTTPException(status_code=400, detail="PDF has no readable pages.")

    return pages


def preprocess_for_ocr(image: Image.Image) -> Image.Image:
    """
    Conservative OCR preprocessing:
    - grayscale
    - autocontrast
    - light thresholding
    """
    gray = ImageOps.grayscale(image)
    gray = ImageOps.autocontrast(gray)

    arr = np.array(gray)
    # Adaptive-ish simple threshold. Avoid over-processing scanned documents.
    threshold = max(150, int(arr.mean() * 0.95))
    arr = np.where(arr > threshold, 255, arr).astype(np.uint8)

    return Image.fromarray(arr)


def ocr_image(image: Image.Image, page_no: int) -> Dict[str, Any]:
    processed = preprocess_for_ocr(image)

    # psm 6 often destroys layout on B/L. psm 4 is better for multi-column documents.
    config = "--oem 3 --psm 4"

    text = pytesseract.image_to_string(
        processed,
        lang=settings.tesseract_lang,
        config=config,
    )

    data = pytesseract.image_to_data(
        processed,
        lang=settings.tesseract_lang,
        config=config,
        output_type=pytesseract.Output.DICT,
    )

    words = []
    for i, token in enumerate(data.get("text", [])):
        token = (token or "").strip()
        if not token:
            continue

        try:
            conf = float(data["conf"][i])
        except Exception:
            conf = -1

        if conf >= 25:
            words.append({
                "text": token,
                "confidence": conf,
                "x": data["left"][i],
                "y": data["top"][i],
                "w": data["width"][i],
                "h": data["height"][i],
            })

    avg_conf = round(sum(w["confidence"] for w in words) / len(words), 2) if words else 0

    return {
        "page": page_no,
        "text": normalize_ocr_spacing(text),
        "word_count": len(words),
        "average_confidence": avg_conf,
    }


def ocr_pdf(pdf_bytes: bytes) -> Dict[str, Any]:
    pages = render_pdf_pages(pdf_bytes, dpi=settings.ocr_dpi)

    page_results = []
    for idx, image in enumerate(pages, start=1):
        page_results.append(ocr_image(image, idx))

    merged_text = "\n\n".join(
        f"--- PAGE {p['page']} ---\n{p['text']}" for p in page_results
    )

    avg_conf = 0
    if page_results:
        avg_conf = round(sum(p["average_confidence"] for p in page_results) / len(page_results), 2)

    return {
        "raw_text": merged_text,
        "pages": page_results,
        "ocr_summary": {
            "page_count": len(page_results),
            "average_confidence": avg_conf,
            "total_word_count": sum(p["word_count"] for p in page_results),
        },
    }


def normalize_ocr_spacing(text: str) -> str:
    text = text.replace("\x0c", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# =========================
# Azure OpenAI extraction
# =========================

SYSTEM_PROMPT = """
You are an expert Bill of Lading extraction engine for Mesco CRM (Dynamics 365).

Extract structured data from OCR text of Bills of Lading.

Return ONLY valid JSON with these specific CRM fields:
1. mesco_masterblno: The B/L number (near labels like BILL OF LADING NO, B/L NO).
2. mesco_bookingnumber: The booking number.
3. mesco_acidnumber: The 19-digit Egyptian ACID number.
4. mesco_shippernamecontactno: Shipper name and contact details.
5. mesco_shipperaddress: Full shipper address.
6. mesco_consigneenamecontactno: Consignee name and contact details.
7. mesco_consigneeaddress: Full consignee address.
8. mesco_vessel: The vessel name.
9. mesco_voytruckno: The voyage or truck number.
10. mesco_origin: Port of Loading.
11. mesco_destination: Port of Discharge.
12. mesco_cargodescription: Description of goods.
13. cr401_totalgrossweight: Total gross weight (numeric string).
14. cr401_totalvolume: Total measurement/CBM (numeric string).
15. cr401_totalpackages: Total number of packages.
16. mesco_nooforgbls: Number of original B/Ls (usually 3).

Critical extraction rules:
1. Do not guess. If a value is not visible, return null.
2. Container numbers usually match 4 letters + 7 digits (e.g. OOLU9707244).
3. ACID number is usually 19 digits for Egypt.
4. Description of goods should exclude legal boilerplate.
5. Return ONLY valid JSON.
"""

JSON_SCHEMA = {
    "name": "bill_of_lading_extraction",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "document_type": {"type": ["string", "null"]},
            "mesco_masterblno": {"type": ["string", "null"]},
            "mesco_bookingnumber": {"type": ["string", "null"]},
            "mesco_acidnumber": {"type": ["string", "null"]},
            "mesco_shippernamecontactno": {"type": ["string", "null"]},
            "mesco_shipperaddress": {"type": ["string", "null"]},
            "mesco_consigneenamecontactno": {"type": ["string", "null"]},
            "mesco_consigneeaddress": {"type": ["string", "null"]},
            "mesco_vessel": {"type": ["string", "null"]},
            "mesco_voytruckno": {"type": ["string", "null"]},
            "mesco_origin": {"type": ["string", "null"]},
            "mesco_destination": {"type": ["string", "null"]},
            "mesco_cargodescription": {"type": ["string", "null"]},
            "cr401_totalgrossweight": {"type": ["string", "null"]},
            "cr401_totalvolume": {"type": ["string", "null"]},
            "cr401_totalpackages": {"type": ["string", "null"]},
            "mesco_nooforgbls": {"type": ["string", "null"]},
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
                        "container_number",
                        "seal_number",
                        "container_type",
                        "packages",
                        "gross_weight_kg",
                        "measurement_cbm",
                    ],
                },
            },
            "confidence": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "post_validation": {"type": ["string", "null"]},
                    "bl_number_rule": {"type": ["string", "null"]},
                    "container_number_rule": {"type": ["string", "null"]}
                },
                "required": ["post_validation", "bl_number_rule", "container_number_rule"]
            },
            "warnings": {
                "type": "array",
                "items": {"type": "string"}
            },
        },
        "required": [
            "document_type",
            "mesco_masterblno",
            "mesco_bookingnumber",
            "mesco_acidnumber",
            "mesco_shippernamecontactno",
            "mesco_shipperaddress",
            "mesco_consigneenamecontactno",
            "mesco_consigneeaddress",
            "mesco_vessel",
            "mesco_voytruckno",
            "mesco_origin",
            "mesco_destination",
            "mesco_cargodescription",
            "cr401_totalgrossweight",
            "cr401_totalvolume",
            "cr401_totalpackages",
            "mesco_nooforgbls",
            "container_number",
            "seal_number",
            "containers",
            "confidence",
            "warnings",
        ],
    },
    "strict": True,
}


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


def extract_with_azure_openai(ocr_text: str) -> Dict[str, Any]:
    if not ocr_text or not ocr_text.strip():
        raise HTTPException(status_code=400, detail="OCR text is empty.")

    safe_text = ocr_text[: settings.max_ocr_chars]

    user_prompt = f"""
Extract the Bill of Lading fields from this OCR text.

OCR TEXT:
{safe_text}
"""

    client = get_azure_client()

    try:
        response = client.chat.completions.create(
            model=settings.azure_openai_deployment,
            temperature=0,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": JSON_SCHEMA,
            },
        )
    except Exception as exc:
        error_msg = f"Azure OpenAI extraction failed: {exc}"
        if "DeploymentNotFound" in str(exc):
            error_msg += f"\n\nDEBUG: Attempted to use deployment name '{settings.azure_openai_deployment}'. Please check Azure OpenAI Studio -> Deployments tab for the correct 'Deployment name'."
        raise HTTPException(status_code=502, detail=error_msg)

    content = response.choices[0].message.content
    if not content:
        raise HTTPException(status_code=502, detail="Azure OpenAI returned empty content.")

    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail=f"Azure OpenAI returned invalid JSON: {exc}")

    return validate_and_correct(data, ocr_text)


# =========================
# Validators / correction rules
# =========================

def add_warning(data: Dict[str, Any], message: str) -> None:
    data.setdefault("warnings", [])
    if message not in data["warnings"]:
        data["warnings"].append(message)


def clean_value(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip()
    value = re.sub(r"\s+", " ", value)
    value = value.strip(" ,;:-")
    return value or None


def is_container_number(value: Optional[str]) -> bool:
    return bool(value and re.fullmatch(r"[A-Z]{4}\d{7}", value.strip().upper()))


def normalize_numeric(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = str(value)
    # Convert OCR comma decimal from examples like 5,131 to 5.131 only when no dot exists.
    value = value.replace(" ", "")
    if "," in value and "." not in value:
        # If pattern is 1,234. keep? For B/L weights 3.001,000 may mean 3001.000.
        parts = value.split(",")
        if len(parts[-1]) == 3:
            value = "".join(parts[:-1]) + "." + parts[-1]
        else:
            value = value.replace(",", ".")
    else:
        value = value.replace(",", "")
    match = re.search(r"\d+(?:\.\d+)?", value)
    return match.group(0) if match else None


def normalize_freight_terms(value: Optional[str], ocr_text: str) -> Optional[str]:
    combined = ((value or "") + " " + ocr_text[:1000]).upper()
    has_prepaid = "PREPAID" in combined
    has_collect = "COLLECT" in combined

    if has_prepaid and has_collect:
        # Only use mixed if both are true freight terms, not destination charges.
        value_upper = (value or "").upper()
        if "PREPAID" in value_upper and "COLLECT" in value_upper:
            return "PREPAID/COLLECT"
        if "FREIGHT COLLECT" in combined and "FREIGHT PREPAID" in combined:
            return "PREPAID/COLLECT"
        if "OCEAN FREIGHT PREPAID" in combined:
            return "PREPAID"
    if has_prepaid:
        return "PREPAID"
    if has_collect:
        return "COLLECT"
    return clean_value(value)


def fallback_regexes(data: Dict[str, Any], ocr_text: str) -> Dict[str, Any]:
    text = ocr_text.upper()

    if not data.get("mesco_masterblno"):
        m = re.search(r"\b[A-Z]{4}\d{7}\b", text)
        if m:
            data["mesco_masterblno"] = m.group(0)
            add_warning(data, "mesco_masterblno filled by regex fallback.")

    if not data.get("mesco_acidnumber"):
        m = re.search(r"\bACID\s*[:\-]?\s*(\d{10,30})\b", text)
        if m:
            data["mesco_acidnumber"] = m.group(1)
            add_warning(data, "mesco_acidnumber filled by regex fallback.")

    return data


def validate_and_correct(data: Dict[str, Any], ocr_text: str) -> Dict[str, Any]:
    # Clean all string fields recursively.
    for key, value in list(data.items()):
        if isinstance(value, str):
            data[key] = clean_value(value)

    data = fallback_regexes(data, ocr_text)

    # Normalize primary fields.
    if data.get("container_number"):
        data["container_number"] = data["container_number"].upper().replace(" ", "")

    # B/L number cannot accidentally equal first container number unless explicitly labeled.
    bl = data.get("mesco_masterblno")
    cn = data.get("container_number")
    if bl and cn and bl == cn:
        # Look for B/L labels near the text to confirm if it's actually a B/L number
        labeled_bl = re.search(
            rf"(B/L\s*NO\.?|BILL\s*OF\s*LADING\s*NO\.?|B/LN|BL\s*NO\.?|BIL\s*NO\.?).{{0,50}}{re.escape(bl)}",
            ocr_text,
            re.IGNORECASE | re.DOTALL,
        )
        if not labeled_bl:
            data["mesco_masterblno"] = None
            add_warning(data, "Removed mesco_masterblno because it matched the container number without a clear B/L label.")

    # Fix common numeric fields.
    for field in ["cr401_totalgrossweight", "cr401_totalvolume"]:
        data[field] = normalize_numeric(data.get(field))

    # Normalize containers.
    containers = data.get("containers") or []
    corrected_containers = []
    for c in containers:
        if not isinstance(c, dict):
            continue
        c["container_number"] = clean_value(c.get("container_number"))
        if c.get("container_number"):
            c["container_number"] = c["container_number"].upper().replace(" ", "")
        c["seal_number"] = clean_value(c.get("seal_number"))
        c["container_type"] = clean_value(c.get("container_type"))
        c["packages"] = clean_value(c.get("packages"))
        c["gross_weight_kg"] = normalize_numeric(c.get("gross_weight_kg"))
        c["measurement_cbm"] = normalize_numeric(c.get("measurement_cbm"))

        if c.get("container_number") or c.get("seal_number"):
            corrected_containers.append(c)

    # If no containers array but primary container exists, create one.
    if not corrected_containers and data.get("container_number"):
        corrected_containers.append({
            "container_number": data.get("container_number"),
            "seal_number": data.get("seal_number"),
            "container_type": None,
            "packages": data.get("cr401_totalpackages"),
            "gross_weight_kg": data.get("cr401_totalgrossweight"),
            "measurement_cbm": data.get("cr401_totalvolume"),
        })

    data["containers"] = corrected_containers

    # Fill primary from first container.
    if corrected_containers:
        first = corrected_containers[0]
        data["container_number"] = data.get("container_number") or first.get("container_number")
        data["seal_number"] = data.get("seal_number") or first.get("seal_number")

    # Validate ACID.
    acid = data.get("mesco_acidnumber")
    if acid:
        acid_digits = re.sub(r"\D", "", acid)
        if len(acid_digits) < 10:
            data["mesco_acidnumber"] = None
            add_warning(data, "Removed mesco_acidnumber because it was too short.")
        else:
            data["mesco_acidnumber"] = acid_digits

    # Confidence notes.
    data.setdefault("confidence", {})
    data["confidence"]["post_validation"] = "completed"
    if data.get("mesco_masterblno"):
        data["confidence"]["bl_number_rule"] = "accepted"

    # Ensure schema shape.
    return BLEntity(**data).model_dump()


# =========================
# Routes
# =========================

@app.get("/")
async def root() -> Dict[str, str]:
    return {
        "service": "Intelligent Bill of Lading Extractor",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "azure_openai_configured": bool(settings.azure_openai_endpoint and settings.azure_openai_api_key),
        "deployment": settings.azure_openai_deployment,
        "ocr_dpi": settings.ocr_dpi,
    }


@app.post("/extract/text")
async def extract_text(payload: TextExtractionRequest) -> JSONResponse:
    data = extract_with_azure_openai(payload.ocr_text)

    response = {
        "extracted_fields": data,
    }

    if payload.return_raw_text:
        response["raw_text"] = payload.ocr_text

    return JSONResponse(response)


@app.post("/extract/pdf")
async def extract_pdf(
    file: UploadFile = File(...),
    return_raw_text: bool = Form(False),
) -> JSONResponse:
    filename = file.filename or ""
    if filename and not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Please upload a PDF file.")

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Uploaded PDF is empty.")

    ocr_result = ocr_pdf(pdf_bytes)
    data = extract_with_azure_openai(ocr_result["raw_text"])

    response = {
        "filename": filename,
        "ocr_summary": ocr_result["ocr_summary"],
        "extracted_fields": data,
    }

    if return_raw_text or settings.return_raw_text:
        response["raw_text"] = ocr_result["raw_text"]
        response["pages"] = ocr_result["pages"]

    return JSONResponse(response)


@app.post("/ocr/pdf")
async def ocr_only_pdf(file: UploadFile = File(...)) -> JSONResponse:
    pdf_bytes = await file.read()
    ocr_result = ocr_pdf(pdf_bytes)
    return JSONResponse(ocr_result)
