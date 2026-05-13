r"""
Professional Native + Scanned PDF Bill of Lading Extractor
========================================================

Run:
    pip install fastapi uvicorn python-multipart pymupdf pillow pytesseract numpy openai pydantic-settings
    uvicorn main:app --reload --host 0.0.0.0 --port 8000

Environment (.env):
    AZURE_OPENAI_ENDPOINT=https://YOUR-RESOURCE.openai.azure.com/
    AZURE_OPENAI_API_KEY=YOUR_KEY
    AZURE_OPENAI_API_VERSION=2024-08-01-preview
    AZURE_OPENAI_DEPLOYMENT=gpt-4o-mini
    TESSERACT_LANG=eng
    # Windows only, if needed:
    # TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe
"""

from typing import Any, Dict, Optional

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
      <form action="/extract/pdf" method="post" enctype="multipart/form-data">
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
    error: Optional[str] = None
    raw_text: Optional[str] = None
    extraction_quality: Optional[Dict[str, Any]] = None


@app.post("/extract/file", response_model=ExtractResponse)
async def extract_file(file: UploadFile = File(...)):
    try:
        file_bytes = await file.read()
        extracted = extract_document_text_professionally(file_bytes, file.filename)
        raw_text = extracted.get("text", "")
        extraction_quality = extracted.get("quality", {})

        if not raw_text.strip():
            return ExtractResponse(success=False, error="No text extracted from file.")

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