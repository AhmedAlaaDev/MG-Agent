r"""
Professional Native + Scanned PDF Bill of Lading Extractor
=======================================================

Run:
    pip install fastapi uvicorn python-multipart pymupdf pillow pytesseract numpy openai pydantic-settings
    uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""

import re
from datetime import datetime, timedelta
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
        "HB/L NO.",
        "HB/L NO",
        "HB/L",
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


def _normalize_manifest_key(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def _manifest_value(values: Dict[str, Any], *keys: str) -> Optional[str]:
    normalized = {_normalize_manifest_key(k): v for k, v in values.items()}
    for key in keys:
        value = normalized.get(_normalize_manifest_key(key))
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _parse_record_cell_map(record_text: str) -> Dict[str, str]:
    cells: Dict[str, str] = {}
    for cell, value in re.findall(r"\b([A-Z]{1,3}\d+)=([^|\n]+)", record_text or ""):
        value = value.strip()
        value = re.split(
            r"\s+(?:\[[A-Z ]+\]|[A-Za-z0-9()_\- /]+(?:ROW|CELLS)\s+\d+:)",
            value,
            maxsplit=1,
        )[0].strip()
        if value:
            cells[cell.upper()] = value
    return cells


def _cell_ref_parts(ref: str) -> Optional[tuple[str, int]]:
    match = re.fullmatch(r"([A-Z]+)(\d+)", ref.upper())
    if not match:
        return None
    return match.group(1), int(match.group(2))


def _value_below_label(cells: Dict[str, str], *label_patterns: str) -> Optional[str]:
    compiled = [re.compile(pattern, re.I) for pattern in label_patterns]
    for ref, value in cells.items():
        if not any(pattern.search(value or "") for pattern in compiled):
            continue
        parts = _cell_ref_parts(ref)
        if not parts:
            continue
        col, row = parts
        below = cells.get(f"{col}{row + 1}")
        if below and below.strip():
            return below.strip()
    return None


def _excel_serial_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        serial = float(text)
        if 30000 <= serial <= 60000:
            return (datetime(1899, 12, 30) + timedelta(days=serial)).strftime("%Y-%m-%d")
    return text


def _parse_manifest_context(record_text: str) -> Dict[str, Optional[str]]:
    cells = _parse_record_cell_map(record_text)
    vessel = voyage = None
    vsl_match = re.search(r"\b([A-Z][A-Z ]{2,50})/([A-Z0-9]{4,12})\b", record_text.upper())
    vsl_voy = cells.get("F3")
    if vsl_match:
        vessel = vsl_match.group(1).strip()
        voyage = vsl_match.group(2).strip()
    elif vsl_voy and "/" in vsl_voy:
        vessel, voyage = (part.strip() for part in vsl_voy.split("/", 1))

    container = seal = None
    container_match = re.search(r"\b([A-Z]{4}\d{7})/([A-Z0-9]{4,20})\b", record_text.upper())
    if container_match:
        container = container_match.group(1)
        seal = container_match.group(2)
    container_seal = cells.get("M3")
    if not container and container_seal:
        parts = [part.strip() for part in container_seal.split("/", 1)]
        container = parts[0] if parts else None
        seal = parts[1] if len(parts) > 1 else None

    job_match = re.search(r"\b(ALY[A-Z0-9]{6,})\b", record_text.upper())
    mbl_match = re.search(r"\b([A-Z]{4}\d{9,12})\b", record_text.upper())
    etd_match = re.search(r"\bJ4=(\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}:\d{2})?|[0-9]+(?:\.[0-9]+)?)\b", record_text)

    container_type = cells.get("F4")
    if not container_type:
        type_match = re.search(r"\b\d+X\d{2}[A-Z]{0,3}\b", record_text.upper())
        container_type = type_match.group(0) if type_match else None

    agent = cells.get("M4")
    if agent:
        agent = re.split(r"\s+(?:\[|[A-Z][A-Z0-9 ()/\-]*\s+ROW\s+\d+:)", agent, 1)[0].strip()

    return {
        "consol_job_no": cells.get("C3") or (job_match.group(1) if job_match else None),
        "mesco_masterblno": cells.get("C4") or (mbl_match.group(1) if mbl_match else None),
        "mesco_vessel": vessel,
        "mesco_voytruckno": voyage,
        "pod": cells.get("J3") or _value_below_label(cells, r"POD", r"PORT\s+OF\s+DISCHARG"),
        "origin": _value_below_label(cells, r"PORT\s+OF\s+LOADING", r"PLACE\s+OF\s+RECEIPT", r"ORIGIN"),
        "container_number": container,
        "seal_number": seal,
        "mesco_containertype": container_type,
        "mesco_etdorigin": _excel_serial_date((etd_match.group(1) if etd_match else None) or cells.get("J4")),
        "carrier": _value_below_label(cells, r"CARRIER"),
        "job_no": _value_below_label(cells, r"JOB\s*NO"),
        "mbl_shipper": _value_below_label(cells, r"M/?BL\s+SHIPPER"),
        "delivery_agent": _value_below_label(cells, r"DELIVERY\s+AGENT"),
        "mbl_acid": _value_below_label(cells, r"M/?BL\s+ACID"),
        "agent": agent or _value_below_label(cells, r"AGENT"),
        "schedule": cells.get("A4"),
    }


def _is_manifest_record(rec: Dict[str, Any]) -> bool:
    values = rec.get("values_by_header", {}) or {}
    keys = {_normalize_manifest_key(k) for k in values}
    return (
        bool({"HBLNO", "HBLNOS", "HB/LNO"} & keys)
        or "HBLNO" in keys
        or "HBLTYPE" in keys
    ) and bool({
        "SHIPPER", "CONSIGNEE", "CNEE", "HSCODE", "CARGOVALUE", "PKGS",
        "NOSOFPACKAGES", "GROSSWEIGHTKG", "MEASURMENTSCBM", "MEASUREMENTSCBM",
        "PLACEOFDELIVERY", "PLACEOFRECEIPT", "FREIGHT",
    } & keys)


def _direct_manifest_record(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    values = rec.get("values_by_header", {}) or {}
    if not _is_manifest_record(rec):
        return None

    context = _parse_manifest_context(rec.get("text", ""))
    hbl = _record_hbl(rec) or _manifest_value(values, "HB/L NO.", "HBL NO.", "H/BL No.")
    origin = _manifest_value(values, "PLACE OF RECEIPT", "ORIGIN", "POL") or context.get("origin")
    destination = (
        _manifest_value(values, "DESTINATION", "PLACE OF DELIVERY")
        or _manifest_value(values, "POD")
        or context.get("pod")
    )
    pod = _manifest_value(values, "POD") or context.get("pod")
    freight_text = (_manifest_value(values, "FREIGHT") or "").upper()
    freight_term = None
    if "COLLECT" in freight_text:
        freight_term = "COLLECT"
    elif "PREPAID" in freight_text:
        freight_term = "PREPAID"

    hbl_type = _manifest_value(values, "HBL'S TYPE", "HBL TYPE")
    package_count = _manifest_value(values, "PKGS", "NOS. OF PACKAGES", "NOS OF PACKAGES")
    gross_weight = _manifest_value(values, "GW", "GROSS WEIGHT (KG)", "GROSS WEIGHT", "WEIGHT")
    measurement = _manifest_value(values, "CBM", "MEASURMENTS (CBM)", "MEASUREMENTS (CBM)", "MEASUREMENT (CBM)")
    delivery_term = _manifest_value(values, "DELIVERY TERM", "TERM")
    container = {
        "container_number": context.get("container_number"),
        "seal_number": context.get("seal_number"),
        "container_type": context.get("mesco_containertype"),
        "packages": package_count,
        "gross_weight_kg": gross_weight,
        "measurement_cbm": measurement,
    }

    output: Dict[str, Any] = {
        "document_type": "Bill of Lading",
        "record_index": rec.get("record_index"),
        "sheet_name": rec.get("sheet_name"),
        "source_row": rec.get("source_row"),
        "mesco_masterblno": context.get("mesco_masterblno"),
        "mesco_houseblno": hbl,
        "mesco_bookingnumber": context.get("consol_job_no") or context.get("job_no"),
        "mesco_acidnumber": _manifest_value(values, "H/BL ACID", "HBL ACID"),
        "mesco_customerreference": _manifest_value(values, "REF NO"),
        "mesco_shippernamecontactno": _manifest_value(values, "SHIPPER"),
        "mesco_consigneenamecontactno": _manifest_value(values, "CNEE", "CONSIGNEE"),
        "mesco_vessel": context.get("mesco_vessel"),
        "mesco_voytruckno": context.get("mesco_voytruckno"),
        "mesco_origin": origin,
        "mesco_destination": destination,
        "mesco_transhipmentport": pod,
        "cr401_totalpackages": package_count,
        "package_type": _manifest_value(values, "PACKAGES", "PACKING"),
        "cr401_totalgrossweight": gross_weight,
        "cr401_totalvolume": measurement,
        "mesco_containertype": context.get("mesco_containertype"),
        "mesco_pcfreightterm": freight_term or _manifest_value(values, "FREIGHT"),
        "mesco_incoterm": delivery_term,
        "mesco_hscode": _manifest_value(values, "HS CODE"),
        "cargo_value": _manifest_value(values, "CARGO VALUE"),
        "consignee_contact_details": _manifest_value(values, "CNEE'S CONTACT DETAILS"),
        "hbl_type": hbl_type,
        "nomination_term": _manifest_value(values, "TERM (NOMINATED / FREE HAND)", "TERM NOMINATED FREE HAND"),
        "delivery_term": delivery_term,
        "shipment_status": _manifest_value(values, "STATUS"),
        "cargo_type": _manifest_value(values, "CARGO TYPE"),
        "rate": _manifest_value(values, "RATE"),
        "carrier": context.get("carrier"),
        "mbl_shipper": context.get("mbl_shipper"),
        "delivery_agent": context.get("delivery_agent"),
        "mbl_acid": context.get("mbl_acid"),
        "schedule": context.get("schedule"),
        "mesco_bltype": 886150001 if hbl_type and "ORIGINAL" in hbl_type.upper() else None,
        "mesco_telexrelease": True if hbl_type and "TELEX" in hbl_type.upper() else None,
        "mesco_transporttype": 300000000,
        "mesco_loadtype": 300000001,
        "mesco_direction": 300000000,
        "mesco_etdorigin": context.get("mesco_etdorigin"),
        "agent": context.get("agent"),
        "container_number": context.get("container_number"),
        "seal_number": context.get("seal_number"),
        "containers": [container],
        "manifest_values": values,
        "extraction_method": "spreadsheet_direct_manifest",
        "unique_key": hbl,
        "_source_info": _record_source_info(rec),
        "confidence": {
            "post_validation": "not_needed",
            "source": "spreadsheet_extractor",
            "house_bl_rule": "accepted" if hbl else "missing",
            "container_number_rule": "accepted" if context.get("container_number") else "missing",
        },
    }
    return _drop_empty_values(output)


def _direct_spreadsheet_record(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return deterministic output for records parsed directly from spreadsheet layouts."""
    mesco_payload = rec.get("mesco_payload")
    financial_processing = rec.get("financial_processing")
    if not isinstance(mesco_payload, dict):
        return _direct_manifest_record(rec)

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
