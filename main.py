r"""
Professional Native + Scanned PDF Bill of Lading Extractor
=======================================================

Run:
    pip install fastapi uvicorn python-multipart pymupdf pillow pytesseract numpy openai pydantic-settings
    uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""

from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from spreadsheet_extractor import extract_document_text_professionally
from ai_extractor import extract_with_azure_openai
from validator import validate_and_correct


app = FastAPI(
    title="Professional PDF + OCR + Excel Bill of Lading Extractor",
    version="4.0.0",
    description="Extracts native/OCR PDF text or Excel sheet data, then extracts structured B/L JSON.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
      <h1>B/L Extractor v4.0</h1>
      <p>FastAPI service for extracting Bill of Lading data from PDF and Excel files.</p>
      <form action="/docs" method="get">
        <button type="submit">Open API Docs</button>
      </form>
      <h2>Quick Test</h2>
      <form action="/extract/file" method="post" enctype="multipart/form-data">
        <input type="file" name="file" accept=".pdf,.xlsx,.xls,.csv" required>
        <button type="submit">Extract B/L</button>
      </form>
    </body>
    </html>
    """


@app.get("/health")
async def health():
    return {"status": "ok", "version": "4.0.0"}


class ExtractRequest(BaseModel):
    ocr_text: str


class ExtractResponse(BaseModel):
    success: bool
    data: Optional[Dict[str, Any]] = None
    records: Optional[List[Dict[str, Any]]] = None
    error: Optional[str] = None
    raw_text: Optional[str] = None
    extraction_quality: Optional[Dict[str, Any]] = None


def process_single_record(record_text: str, source_info: str) -> Dict[str, Any]:
    """Process a single record through AI extraction and validation."""
    try:
        ai_result = extract_with_azure_openai(record_text)
        validated = validate_and_correct(ai_result, record_text)
        validated["_source_info"] = source_info
        return validated
    except Exception as exc:
        return {"_source_info": source_info, "_error": str(exc)}


def _drop_empty_values(value: Any) -> Any:
    """Remove null/empty fields from direct spreadsheet records."""
    if isinstance(value, dict):
        cleaned = {k: _drop_empty_values(v) for k, v in value.items()}
        return {
            k: v
            for k, v in cleaned.items()
            if v is not None and v != "" and v != [] and v != {}
        }
    if isinstance(value, list):
        return [
            cleaned
            for item in value
            if (cleaned := _drop_empty_values(item)) not in (None, "", [], {})
        ]
    return value


def _record_hbl(rec: Dict[str, Any]) -> Optional[str]:
    values = rec.get("values_by_header", {}) or {}
    for key in (
        "mesco_houseblno",
        "hbl_no",
        "HBL NO.",
        "HBL NO",
        "H/BL No.",
        "H/BL Nos.",
        "HOUSE B/L",
    ):
        value = rec.get(key) or values.get(key)
        if value:
            return str(value).strip()
    return None


def _record_source_info(rec: Dict[str, Any]) -> str:
    hbl = _record_hbl(rec) or "N/A"
    return f"Sheet: {rec.get('sheet_name')}, Row: {rec.get('source_row')}, HBL: {hbl}"


def _direct_spreadsheet_record(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return deterministic output for records parsed directly from spreadsheet layouts."""
    mesco_payload = rec.get("mesco_payload")
    financial_processing = rec.get("financial_processing")
    if not isinstance(mesco_payload, dict):
        return None

    hbl = _record_hbl(rec)
    output: Dict[str, Any] = dict(mesco_payload)
    if hbl:
        output["mesco_houseblno"] = hbl

    output.update({
        "record_index": rec.get("record_index"),
        "sheet_name": rec.get("sheet_name"),
        "source_row": rec.get("source_row"),
        "unique_key": rec.get("unique_key") or hbl,
        "cargo_type": rec.get("cargo_type") or output.get("mesco_incoterm"),
        "extraction_method": "spreadsheet_direct_proxy_bill",
        "_source_info": _record_source_info(rec),
        "confidence": {
            "post_validation": "not_needed",
            "source": "spreadsheet_extractor",
            "house_bl_rule": "accepted" if hbl else "missing",
            "container_number_rule": "accepted" if output.get("container_number") else "missing",
        },
    })

    if isinstance(financial_processing, dict):
        output["spreadsheet_record"] = {
            key: value
            for key, value in financial_processing.items()
            if key not in {"debit", "credit"}
        }
        output["financial_processing"] = {
            "debit": financial_processing.get("debit", {}),
            "credit": financial_processing.get("credit", {}),
        }

    return _drop_empty_values(output)


@app.post("/extract/file", response_model=ExtractResponse)
async def extract_file(file: UploadFile = File(...)):
    try:
        file_bytes = await file.read()
        extracted = extract_document_text_professionally(file_bytes, file.filename)
        
        raw_text = extracted.get("text", "")
        extraction_quality = extracted.get("quality", {})
        records = extracted.get("records", [])

        if not raw_text.strip():
            return ExtractResponse(success=False, error="No text extracted from file.")

        # If spreadsheet has individual records, process each separately
        if records and len(records) > 0:
            extracted_records = []
            for rec in records:
                direct_result = _direct_spreadsheet_record(rec)
                if direct_result:
                    extracted_records.append(direct_result)
                    continue

                record_text = rec.get("text", "")
                source_info = _record_source_info(rec)
                
                if record_text:
                    result = process_single_record(record_text, source_info)
                    extracted_records.append(result)

            return ExtractResponse(
                success=True,
                records=extracted_records,
                raw_text=raw_text[:5000] + "..." if len(raw_text) > 5000 else raw_text,
                extraction_quality=extraction_quality
            )
        
        # No individual records - process as single document (PDF or simple spreadsheet)
        ai_result = extract_with_azure_openai(raw_text)
        validated = validate_and_correct(ai_result, raw_text)

        validated["extraction_method"] = extracted.get("method", "unknown")
        validated["extraction_quality"] = extraction_quality

        return ExtractResponse(success=True, data=validated, raw_text=raw_text, extraction_quality=extraction_quality)
    except Exception as exc:
        return ExtractResponse(success=False, error=str(exc))


@app.post("/extract/text", response_model=ExtractResponse)
async def extract_text(request: ExtractRequest):
    try:
        raw_text = request.ocr_text
        if not raw_text.strip():
            return ExtractResponse(success=False, error="No text provided.")

        ai_result = extract_with_azure_openai(raw_text)
        validated = validate_and_correct(ai_result, raw_text)

        return ExtractResponse(success=True, data=validated, raw_text=raw_text)
    except Exception as exc:
        return ExtractResponse(success=False, error=str(exc))


@app.post("/extract/pdf", response_model=ExtractResponse)
async def extract_pdf(file: UploadFile = File(...)):
    """Legacy endpoint - use /extract/file instead"""
    return await extract_file(file)


@app.post("/extract/excel", response_model=ExtractResponse)
async def extract_excel(file: UploadFile = File(...)):
    """Extract from Excel files (.xlsx, .xls, .csv)"""
    return await extract_file(file)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
