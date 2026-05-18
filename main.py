r"""
Professional Native + Scanned PDF Bill of Lading Extractor
=======================================================

Run:
    pip install fastapi uvicorn python-multipart pymupdf pillow pytesseract numpy openai pydantic-settings
    uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from fastapi import FastAPI, File, Query, UploadFile, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from dataverse.client_service import DataverseClientService, RetryConfig
from dataverse_uploader import _ENTITY, _CONTAINER_ENTITY, _CARGO_ENTITY

from spreadsheet_extractor import extract_document_text_professionally
from ai_extractor import extract_with_azure_openai
from crm_mapper import map_crm_operation_to_records
from validator import validate_and_correct
from crm_output_formatter import records_to_house_json, records_to_master_json


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


class CrmExtractRequest(BaseModel):
    crm_json: Dict[str, Any]


class ExtractRequest(BaseModel):
    ocr_text: str


class ExtractResponse(BaseModel):
    success: bool
    data: Optional[Dict[str, Any]] = None
    house_data: Optional[Dict[str, Any]] = None
    records: Optional[List[Dict[str, Any]]] = None
    error: Optional[str] = None
    raw_text: Optional[str] = None
    extraction_quality: Optional[Dict[str, Any]] = None
    dataverse_result: Optional[Dict[str, Any]] = None
    dataverse_error: Optional[str] = None


def upload_crm_json(crm_data: Dict[str, Any]) -> Dict[str, Any]:
    from dataverse_uploader import upload_crm_json as _upload_crm_json

    return _upload_crm_json(crm_data)


@app.post("/extract/crm", response_model=ExtractResponse)
async def extract_crm(request: CrmExtractRequest):
    """Extract B/L records from a Dynamics CRM mesco_operation JSON payload."""
    try:
        crm_data = request.crm_json
        records = map_crm_operation_to_records(crm_data)
        if not records:
            return ExtractResponse(success=False, error="No records could be generated from the CRM data.")

        validated_records = []
        for rec in records:
            rec_text = f"CRM Operation: {rec.get('_master_code') or 'N/A'} / House: {rec.get('_house_code') or 'N/A'}"
            validated = validate_and_correct(rec, rec_text)
            validated_records.append(validated)

        extraction_quality = {
            "source": "crm_operation_json",
            "record_count": len(validated_records),
            "master_code": crm_data.get("mesco_code"),
            "master_bl": crm_data.get("mesco_masterblno"),
        }

        return ExtractResponse(
            success=True,
            records=validated_records,
            extraction_quality=extraction_quality,
        )
    except Exception as exc:
        return ExtractResponse(success=False, error=str(exc))


class DataverseUploadResponse(BaseModel):
    success: bool
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


@app.post(
    "/upload/dataverse",
    response_model=DataverseUploadResponse,
    summary="Upload CRM JSON file to Dynamics 365 Dataverse",
    description="Upload a CRM JSON file (previously downloaded from /extract/pdf?download=true or /extract/excel?download=true) to Dynamics 365 Dataverse. Creates the full hierarchy: master operation, houses, containers, and cargo.",
)
async def upload_to_dataverse(
    file: UploadFile = File(..., description="CRM JSON file (the file downloaded with ?download=true)"),
):
    try:
        content = await file.read()
        crm_data = json.loads(content.decode("utf-8"))
        result = upload_crm_json(crm_data)
        return DataverseUploadResponse(success=True, result=result)
    except json.JSONDecodeError:
        return DataverseUploadResponse(success=False, error="Invalid JSON file.")
    except Exception as exc:
        return DataverseUploadResponse(success=False, error=str(exc))


@app.post(
    "/upload/dataverse/json",
    response_model=DataverseUploadResponse,
    summary="Upload CRM JSON payload to Dynamics 365 Dataverse (JSON body)",
    description="Send the CRM JSON structure directly as the request body to upload to Dynamics 365 Dataverse. The body should be the same structure as the 'data' field returned by /extract/pdf or /extract/excel.",
)
async def upload_to_dataverse_json(
    payload: Dict[str, Any] = Body(..., description="CRM JSON structure (the 'data' field from extract endpoints)"),
):
    try:
        result = upload_crm_json(payload)
        return DataverseUploadResponse(success=True, result=result)
    except Exception as exc:
        return DataverseUploadResponse(success=False, error=str(exc))


def process_single_record(record_text: str, source_info: str) -> Dict[str, Any]:
    """Process a single record through AI extraction and validation."""
    try:
        ai_result = extract_with_azure_openai(record_text)
        validated = validate_and_correct(ai_result, record_text)
        validated["_source_info"] = source_info
        validated["extraction_method"] = validated.get("extraction_method") or "azure_openai_record"
        return validated
    except Exception as exc:
        return {"_source_info": source_info, "_error": str(exc)}


def process_workbook_with_azure(raw_text: str, extracted: Dict[str, Any], extraction_quality: Dict[str, Any]) -> Dict[str, Any]:
    """Fallback for future/unknown Excel layouts that direct row mapping does not know."""
    ai_result = extract_with_azure_openai(raw_text)
    validated = validate_and_correct(ai_result, raw_text)
    validated["extraction_method"] = "azure_openai_workbook_fallback"
    validated["source_extraction_method"] = extracted.get("method", "unknown")
    validated["extraction_quality"] = extraction_quality
    return validated


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


def _has_value(record: Dict[str, Any], key: str) -> bool:
    value = record.get(key)
    return value is not None and value != "" and value != [] and value != {}


def _direct_record_confidence(record: Dict[str, Any]) -> Dict[str, Any]:
    """Decide whether deterministic spreadsheet extraction is safe enough to use."""
    method = record.get("extraction_method")
    has_hbl = _has_value(record, "mesco_houseblno") or _has_value(record, "unique_key")

    if method == "spreadsheet_direct_proxy_bill":
        required = ["mesco_masterblno", "mesco_houseblno", "container_number", "financial_processing"]
        present = [key for key in required if _has_value(record, key)]
        accepted = has_hbl and len(present) >= 3
        return {
            "accepted": accepted,
            "score": len(present),
            "required_present": present,
            "required_missing": [key for key in required if key not in present],
            "reason": "recognized_proxy_bill" if accepted else "proxy_bill_low_confidence",
        }

    if method == "spreadsheet_direct_manifest":
        useful = [
            "mesco_shippernamecontactno",
            "mesco_consigneenamecontactno",
            "mesco_origin",
            "mesco_destination",
            "cr401_totalpackages",
            "cr401_totalgrossweight",
            "cr401_totalvolume",
            "mesco_acidnumber",
            "mesco_incoterm",
            "mesco_pcfreightterm",
            "hbl_type",
            "cargo_value",
            "mesco_hscode",
        ]
        present = [key for key in useful if _has_value(record, key)]
        accepted = has_hbl and len(present) >= 4
        return {
            "accepted": accepted,
            "score": len(present),
            "required_present": present,
            "required_missing": [key for key in useful if key not in present],
            "reason": "recognized_manifest_or_loading_sheet" if accepted else "manifest_low_confidence",
        }

    return {
        "accepted": False,
        "score": 0,
        "required_present": [],
        "required_missing": [],
        "reason": "unknown_spreadsheet_layout",
    }


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


def _parse_header_value_map(cells: Dict[str, str], header_row: int = 4, data_row: int = 5) -> Dict[str, str]:
    """Read column header labels from header_row and their values from data_row."""
    hdr_to_val: Dict[str, str] = {}
    col_letters: set[str] = set()
    for ref, val in cells.items():
        parts = _cell_ref_parts(ref)
        if parts and parts[1] == header_row:
            col_letters.add(parts[0])
    for col in sorted(col_letters):
        header = cells.get(f"{col}{header_row}")
        if header:
            value = cells.get(f"{col}{data_row}")
            if value:
                hdr_to_val[header.strip()] = value.strip()
    return hdr_to_val


def _is_address_text(text: Optional[str]) -> bool:
    """Check if text looks like an address rather than vessel/voyage."""
    if not text:
        return True
    upper = text.upper()
    address_indicators = [
        "HONG KONG", "ROAD", "STREET", "LANE", "DRIVE", "AVENUE",
        " BUILDING", "FLOOR", "SUITE", "P.O. BOX", "PO BOX",
        "C/O ", "C/O", "ATTN:", "ATTENTION",
    ]
    if any(ind in upper for ind in address_indicators):
        return True
    if len(text) > 40:
        return True
    if re.match(r"^\d{6,}", text):
        return True
    return False


def _parse_manifest_context(record_text: str) -> Dict[str, Optional[str]]:
    cells = _parse_record_cell_map(record_text)
    header_map = _parse_header_value_map(cells, header_row=4, data_row=5)

    vessel = voyage = None
    vsl_match = re.search(r"\b([A-Z][A-Z ]{2,50})/([A-Z0-9]{4,12})\b", record_text.upper())
    vsl_voy = cells.get("F3")
    if vsl_match:
        v = vsl_match.group(1).strip()
        vo = vsl_match.group(2).strip()
        if not _is_address_text(v) and not _is_address_text(vo):
            vessel = v
            voyage = vo
    elif vsl_voy and "/" in vsl_voy:
        v, vo = (part.strip() for part in vsl_voy.split("/", 1))
        if not _is_address_text(v) and not _is_address_text(vo):
            vessel = v
            voyage = vo

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

    # Use header_map to get values by column label instead of hardcoded positions
    etd_value = header_map.get("ETD") or cells.get("E5")
    container_type = None
    if not container_type:
        type_match = re.search(r"\b\d+X\d{2}[A-Z]{0,3}\b", record_text.upper())
        container_type = type_match.group(0) if type_match else None

    mbl_no = (mbl_match.group(1) if mbl_match else None)
    # Try M/BL header label if present in row 3
    mbl_val = cells.get("I3")
    if mbl_val and mbl_val.upper() != "M/BL" and not mbl_no:
        mbl_no = mbl_val

    agent = cells.get("M4")
    if agent:
        agent = re.split(r"\s+(?:\[|[A-Z][A-Z0-9 ()/\-]*\s+ROW\s+\d+:)", agent, 1)[0].strip()

    return {
        "consol_job_no": cells.get("C3") or (job_match.group(1) if job_match else None),
        "mesco_masterblno": mbl_no,
        "mesco_vessel": vessel,
        "mesco_voytruckno": voyage,
        "pod": cells.get("J3") or _value_below_label(cells, r"POD", r"PORT\s+OF\s+DISCHARG") or header_map.get("Port Of Discharging"),
        "origin": _value_below_label(cells, r"PORT\s+OF\s+LOADING", r"PLACE\s+OF\s+RECEIPT", r"ORIGIN") or header_map.get("Port Of Loading"),
        "container_number": container,
        "seal_number": seal,
        "mesco_containertype": container_type,
        "mesco_etdorigin": _excel_serial_date(etd_value),
        "carrier": _value_below_label(cells, r"CARRIER") or header_map.get("Carrier"),
        "job_no": _value_below_label(cells, r"JOB\s*NO") or header_map.get("JOB NO."),
        "mbl_shipper": _value_below_label(cells, r"M/?BL\s+SHIPPER") or header_map.get("M/BL Shipper"),
        "delivery_agent": _value_below_label(cells, r"DELIVERY\s+AGENT") or header_map.get("Delivery Agent"),
        "mbl_acid": _value_below_label(cells, r"M/?BL\s+ACID") or header_map.get("M/BL ACID"),
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
    remarks = _manifest_value(values, "REMARKS", "STATUS")
    package_count = _manifest_value(values, "PKGS", "NOS. OF PACKAGES", "NOS OF PACKAGES")
    gross_weight = _manifest_value(values, "GW", "GROSS WEIGHT (KG)", "GROSS WEIGHT", "WEIGHT")
    measurement = _manifest_value(values, "CBM", "MEASURMENTS (CBM)", "MEASUREMENTS (CBM)", "MEASUREMENT (CBM)")
    delivery_term = _manifest_value(values, "DELIVERY TERM", "TERM")
    cargo_type = _manifest_value(values, "CARGO TYPE")
    hscode = _manifest_value(values, "HS CODE")

    container = {
        "container_number": context.get("container_number"),
        "seal_number": context.get("seal_number"),
        "container_type": context.get("mesco_containertype"),
        "packages": package_count,
        "gross_weight_kg": gross_weight,
        "measurement_cbm": measurement,
    }

    # Clean vessel/voyage — manifest Excel sheets often have no vessel info,
    # and the regex may pick up false matches from address text
    vessel = context.get("mesco_vessel")
    voyage = context.get("mesco_voytruckno")
    if vessel and _is_address_text(vessel):
        vessel = None
    if voyage and _is_address_text(voyage):
        voyage = None

    # Determine TELEX release from HBL TYPE or REMARKS column
    is_telex = (
        (hbl_type and "TELEX" in hbl_type.upper())
        or (remarks and "TELEX" in remarks.upper())
    )
    is_original = hbl_type and "ORIGINAL" in hbl_type.upper()

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
        "mesco_vessel": vessel,
        "mesco_voytruckno": voyage,
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
        "mesco_hscode": hscode,
        "cargo_value": _manifest_value(values, "CARGO VALUE"),
        "consignee_contact_details": _manifest_value(values, "CNEE'S CONTACT DETAILS"),
        "hbl_type": hbl_type,
        "nomination_term": _manifest_value(values, "TERM (NOMINATED / FREE HAND)", "TERM NOMINATED FREE HAND"),
        "delivery_term": delivery_term,
        "shipment_status": _manifest_value(values, "STATUS"),
        "cargo_type": cargo_type,
        "rate": _manifest_value(values, "RATE"),
        "carrier": context.get("carrier"),
        # Master-context fields (stored as meta for records_to_master_json to use)
        "_mbl_shipper": context.get("mbl_shipper"),
        "_mbl_consignee": context.get("delivery_agent"),
        "_mbl_acid": context.get("mbl_acid"),
        "_mbl_bookingno": context.get("job_no"),
        "_mbl_masterblno": context.get("mesco_masterblno"),
        "mbl_shipper": context.get("mbl_shipper"),
        "delivery_agent": context.get("delivery_agent"),
        "mbl_acid": context.get("mbl_acid"),
        "schedule": context.get("schedule"),
        "mesco_bltype": 886150001 if is_original else None,
        "mesco_telexrelease": is_telex,
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


@app.post(
    "/extract/file",
    response_model=ExtractResponse,
    summary="Extract from any supported file (PDF, XLSX, XLS, CSV)",
    description="Upload a PDF, Excel, or CSV file to extract B/L data. Set download=true to get the CRM JSON as a file for later upload to /upload/dataverse.",
)
async def extract_file(
    file: UploadFile = File(..., description="PDF, XLSX, XLS, or CSV file"),
    post_to_dataverse: bool = Query(True, description="Automatically upload extracted data to Dynamics 365 Dataverse"),
    download: bool = Query(False, description="Download the CRM JSON as a file instead of returning the normal response"),
):
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
            route_counts = {
                "direct": 0,
                "azure_fallback": 0,
                "skipped": 0,
                "policy": "direct_when_confident_else_azure",
            }
            for rec in records:
                direct_result = _direct_spreadsheet_record(rec)
                if direct_result:
                    direct_confidence = _direct_record_confidence(direct_result)
                    direct_result.setdefault("confidence", {})
                    direct_result["confidence"]["direct_extraction"] = direct_confidence
                    if direct_confidence["accepted"]:
                        route_counts["direct"] += 1
                        validated = validate_and_correct(direct_result, rec.get("text", ""))
                        # Spreadsheet manifest records don't have vessel/voyage;
                        # the raw_text may contain addresses that regex falsely matches.
                        if validated.get("mesco_vessel") and _is_address_text(validated.get("mesco_vessel")):
                            validated["mesco_vessel"] = None
                        if validated.get("mesco_voytruckno") and _is_address_text(validated.get("mesco_voytruckno")):
                            validated["mesco_voytruckno"] = None
                        extracted_records.append(validated)
                        continue

                record_text = rec.get("text", "")
                source_info = _record_source_info(rec)
                
                if record_text:
                    result = process_single_record(record_text, source_info)
                    result["_routing"] = {
                        "route": "azure_fallback",
                        "reason": (
                            _direct_record_confidence(direct_result)["reason"]
                            if direct_result else "unknown_spreadsheet_layout"
                        ),
                    }
                    route_counts["azure_fallback"] += 1
                    extracted_records.append(result)
                else:
                    route_counts["skipped"] += 1

            extraction_quality["record_routing"] = route_counts
            if not extracted_records:
                validated = process_workbook_with_azure(raw_text, extracted, extraction_quality)
                validated["_routing"] = {
                    "route": "azure_workbook_fallback",
                    "reason": "no_processable_spreadsheet_records",
                }
                extracted_records = [validated]
                crm_output = records_to_master_json(extracted_records)
                house_output = records_to_house_json(extracted_records)
                return _build_response(crm_output, raw_text, extraction_quality, post_to_dataverse, download, house_output)

            crm_output = records_to_master_json(extracted_records)
            house_output = records_to_house_json(extracted_records)
            return _build_response(crm_output, raw_text, extraction_quality, post_to_dataverse, download, house_output)
        
        # No individual records: future/unknown Excel layouts go to Azure as one workbook.
        extraction_quality["record_routing"] = {
            "direct": 0,
            "azure_fallback": 1,
            "skipped": 0,
            "policy": "direct_when_confident_else_azure",
            "mode": "whole_document_or_workbook",
        }
        validated = process_workbook_with_azure(raw_text, extracted, extraction_quality)
        extracted_records = [validated]
        crm_output = records_to_master_json(extracted_records)
        house_output = records_to_house_json(extracted_records)

        return _build_response(crm_output, raw_text, extraction_quality, post_to_dataverse, download, house_output)
    except Exception as exc:
        return ExtractResponse(success=False, error=str(exc))


def _build_response(
    crm_output: Dict[str, Any],
    raw_text: str,
    extraction_quality: Dict[str, Any],
    post_to_dataverse: bool,
    download: bool = False,
    house_output: Optional[Dict[str, Any]] = None,
) -> Any:
    dataverse_result = None
    dataverse_error = None

    if post_to_dataverse and crm_output:
        try:
            dataverse_result = upload_crm_json(crm_output)
        except Exception as exc:
            dataverse_error = str(exc)
            logger.warning("Dataverse upload failed: %s", dataverse_error)

    if download:
        json_bytes = json.dumps(crm_output, indent=2, default=str).encode("utf-8")
        return Response(
            content=json_bytes,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="crm_output.json"',
                "Content-Length": str(len(json_bytes)),
            },
        )

    return ExtractResponse(
        success=True,
        data=crm_output,
        house_data=house_output,
        raw_text=raw_text[:5000] + "..." if len(raw_text) > 5000 else raw_text,
        extraction_quality=extraction_quality,
        dataverse_result=dataverse_result,
        dataverse_error=dataverse_error,
    )


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


@app.post(
    "/extract/pdf",
    response_model=ExtractResponse,
    summary="Extract from PDF file",
    description="Upload a PDF file to extract B/L data. Use download=true to get the CRM JSON as a downloadable file.",
)
async def extract_pdf(
    file: UploadFile = File(..., description="PDF file to extract"),
    post_to_dataverse: bool = Query(True, description="Automatically upload extracted data to Dynamics 365 Dataverse"),
    download: bool = Query(False, description="Download the CRM JSON as a file instead of returning the normal response"),
):
    return await extract_file(file, post_to_dataverse=post_to_dataverse, download=download)


@app.post(
    "/extract/excel",
    response_model=ExtractResponse,
    summary="Extract from Excel file",
    description="Upload an Excel file (.xlsx, .xls, .csv) to extract B/L data. Use download=true to get the CRM JSON as a downloadable file.",
)
async def extract_excel(
    file: UploadFile = File(..., description="Excel or CSV file (.xlsx, .xls, .csv)"),
    post_to_dataverse: bool = Query(True, description="Automatically upload extracted data to Dynamics 365 Dataverse"),
    download: bool = Query(False, description="Download the CRM JSON as a file instead of returning the normal response"),
):
    return await extract_file(file, post_to_dataverse=post_to_dataverse, download=download)


class CompareRequest(BaseModel):
    master_id: str
    sent_payload: Dict[str, Any]


class CompareResponse(BaseModel):
    success: bool
    master_id: str
    saved: Optional[Dict[str, Any]] = None
    comparison: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


def _flatten(obj: Any, parent_key: str = "") -> Dict[str, Any]:
    """Flatten nested dict for field-level comparison."""
    items: Dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            new_key = f"{parent_key}.{k}" if parent_key else k
            if isinstance(v, (dict, list)):
                items.update(_flatten(v, new_key))
            else:
                items[new_key] = v
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            new_key = f"{parent_key}[{i}]"
            if isinstance(v, (dict, list)):
                items.update(_flatten(v, new_key))
            else:
                items[new_key] = v
    return items


def _clean_dataverse_response(obj: Any) -> Any:
    """Remove OData metadata fields from Dataverse response."""
    if isinstance(obj, dict):
        return {
            k: _clean_dataverse_response(v)
            for k, v in obj.items()
            if not k.startswith("@") and not k.startswith("_") and not k.endswith("@OData.Community.Display.V1.FormattedValue")
        }
    if isinstance(obj, list):
        return [_clean_dataverse_response(item) for item in obj]
    return obj


@app.get("/dynamics/operation/{master_id}")
async def get_dynamics_operation(master_id: str):
    """Fetch a master operation with its houses, containers, and cargo from Dataverse."""
    try:
        client = DataverseClientService.get_instance()
        expand = (
            "mesco_Operation_mesco_Operation_mesco_Operation,"
            "mesco_Container_MasterOperation_mesco_Operation,"
            "mesco_Cargo_MasterOperation_mesco_Operation"
        )
        resp = client.get(f"{_ENTITY}({master_id})?$expand={expand}")
        data = resp.json()
        return {"success": True, "data": _clean_dataverse_response(data)}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


@app.post("/dynamics/compare", response_model=CompareResponse)
async def compare_dynamics(request: CompareRequest):
    """Compare the sent payload against what was actually saved in Dataverse."""
    try:
        client = DataverseClientService.get_instance()
        expand = (
            "mesco_Operation_mesco_Operation_mesco_Operation,"
            "mesco_Container_MasterOperation_mesco_Operation,"
            "mesco_Cargo_MasterOperation_mesco_Operation"
        )
        resp = client.get(f"{_ENTITY}({request.master_id})?$expand={expand}")
        saved_raw = resp.json()
        saved = _clean_dataverse_response(saved_raw)

        sent_flat = _flatten(request.sent_payload)
        saved_flat = _flatten(saved)

        saved_keys = set()
        not_saved_keys = set()
        different_keys = {}

        for key in sent_flat:
            if key in saved_flat:
                sent_val = sent_flat[key]
                saved_val = saved_flat[key]
                if str(sent_val) == str(saved_val):
                    saved_keys.add(key)
                else:
                    different_keys[key] = {
                        "sent": sent_val,
                        "saved": saved_val,
                    }
            else:
                not_saved_keys.add(key)

        # Some fields exist in saved but not in sent (Dataverse defaults)
        extra_keys = set(saved_flat.keys()) - set(sent_flat.keys())

        comparison = {
            "fields_saved": sorted(saved_keys) or None,
            "fields_not_saved": sorted(not_saved_keys) or None,
            "fields_different": different_keys or None,
            "fields_saved_count": len(saved_keys),
            "fields_not_saved_count": len(not_saved_keys),
            "fields_different_count": len(different_keys),
            "fields_extra_in_dataverse": sorted(extra_keys)[:50] if extra_keys else None,
        }

        return CompareResponse(
            success=True,
            master_id=request.master_id,
            saved=saved,
            comparison=comparison,
        )
    except Exception as exc:
        return CompareResponse(success=False, master_id=request.master_id, error=str(exc))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
