"""Bill of lading extraction HTTP routes and pipeline helpers."""

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Body, File, Form, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

from app.infrastructure.dataverse.client_service import DataverseClientService, RetryConfig
from app.infrastructure.dataverse.uploader import (
    _CARGO_ENTITY,
    _CONTAINER_ENTITY,
    _ENTITY,
    _create_entity,
    _find_existing_container,
    _normalize_container_number,
    _resolve_containerno_lookup,
    _sync_operation_totals_from_cargo,
    _update_entity,
)
from app.application.invoice_dataverse_mapper import (
    IMPORT,
    LCL,
    SEA,
    build_invoice_mapping_plan,
    build_single_invoice_lookup_plan,
    container_type_option,
    fetch_invoice_reference_data,
    fetch_single_invoice_reference_data,
    mapping_group,
)
from app.infrastructure.dataverse.field_limits import cap_nested_payload

from app.infrastructure.spreadsheet.spreadsheet_extractor import extract_document_text_professionally
from app.infrastructure.ai.ai_extractor import MULTI_BL_JSON_SCHEMA, SYSTEM_PROMPT, extract_with_azure_openai
from app.application.document_parser import parse_document_intelligently
from app.application.pdf_batch_processor import process_pdf_bytes
from app.application.crm_mapper import map_crm_operation_to_records
from app.core.config import GEMINI_MODELS, settings
from app.core.llm_context import (
    llm_extraction_prefix,
    llm_meta,
    llm_request_overrides,
    uses_gemini,
    validate_llm_request,
)
from app.core.llm_models import GeminiModelQuery, LlmProviderQuery
from app.domain.rules.validator import validate_and_correct
from app.application.crm_output_formatter import (
    apply_bl_type_to_crm_payload,
    normalize_bl_type,
    records_to_house_json,
    records_to_master_json,
)
from app.infrastructure.pdf.parsers.pdf_attached_list import build_house_records_from_attached_list, extract_attached_list_house_refs
from app.infrastructure.pdf.parsers.pdf_lcl_export_manifest import is_export_lcl_manifest, parse_export_lcl_manifest
from app.infrastructure.pdf.parsers.pdf_tur_cargo_manifest import is_tur_cargo_manifest, parse_tur_cargo_manifest
from app.infrastructure.pdf.parsers.pdf_cargo_manifest import (
    is_cargo_manifest_hbl_blocks,
    parse_cargo_manifest_hbl_blocks,
)
from app.infrastructure.pdf.parsers.pdf_consolidated_lcl import (
    is_consolidated_lcl_multi_hbl,
    parse_consolidated_lcl_multi_hbl,
)
from app.infrastructure.pdf.parsers.pdf_debit_note import is_freight_debit_note, parse_freight_debit_note
from app.infrastructure.pdf.parsers.pdf_house_bl import is_standard_house_bl, parse_standard_house_bl
from app.infrastructure.pdf.parsers.pdf_standard_master_bl import is_standard_master_bl, parse_standard_master_bl
from app.infrastructure.pdf.parsers.pdf_sea_waybill import (
    build_house_records_for_consolidation_sea_waybill,
    is_consolidation_sea_waybill,
    master_record_without_house_cargo,
    parse_consolidation_sea_waybill,
)
from app.infrastructure.audit.upload_audit import audit_store

from app.api.schemas import BatchPdfTestResponse, BlTypeQuery, CrmExtractRequest, ExtractRequest, ExtractResponse
from app.infrastructure.dataverse.uploader import upload_crm_json

router = APIRouter()
logger = logging.getLogger(__name__)

@router.post("/extract/crm", response_model=ExtractResponse)
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


def process_single_record(
    record_text: str,
    source_info: str,
    *,
    file_bytes: Optional[bytes] = None,
    filename: Optional[str] = None,
) -> Dict[str, Any]:
    """Process a single record through intelligent AI extraction and validation."""
    try:
        parse_result = parse_document_intelligently(
            record_text,
            file_bytes=file_bytes,
            filename=filename,
        )
        if not parse_result.records:
            raise ValueError("No records extracted from spreadsheet row text.")
        validated = parse_result.records[0]
        validated["_source_info"] = source_info
        validated["extraction_method"] = validated.get("extraction_method") or f"{llm_extraction_prefix()}_intelligent_record"
        return validated
    except Exception as exc:
        return {"_source_info": source_info, "_error": str(exc)}


def process_workbook_with_azure(
    raw_text: str,
    extracted: Dict[str, Any],
    extraction_quality: Dict[str, Any],
    *,
    file_bytes: Optional[bytes] = None,
    filename: Optional[str] = None,
) -> Dict[str, Any]:
    """Intelligent parse for unknown workbook/PDF layouts; returns first validated record."""
    parse_result = parse_document_intelligently(
        raw_text,
        extracted,
        file_bytes=file_bytes,
        filename=filename,
    )
    if not parse_result.records:
        raise ValueError("Intelligent parser returned no B/L records.")
    validated = parse_result.records[0]
    validated["extraction_method"] = validated.get("extraction_method") or f"{llm_extraction_prefix()}_intelligent_workbook"
    validated["source_extraction_method"] = extracted.get("method", "unknown")
    validated["extraction_quality"] = {**extraction_quality, **parse_result.quality}
    validated["_document_layout"] = parse_result.document_layout
    if len(parse_result.records) > 1:
        validated["_additional_records"] = parse_result.records[1:]
    return validated


def _should_use_gemini_workbook_llm(records: List[Dict[str, Any]], raw_text: str) -> bool:
    """Prefer one whole-workbook Gemini call for long manifests instead of per-row LLM."""
    if not uses_gemini():
        return False
    if len(records) >= settings.gemini_workbook_llm_min_rows:
        return True
    if len(raw_text or "") >= 25_000:
        return True
    upper = (raw_text or "").upper()
    manifest_markers = ("MANIFEST", "H/BL", "HOUSE B/L", "LOADING SHEET", "PROXY BILL")
    if any(m in upper for m in manifest_markers) and len(records) >= 2:
        return True
    return False


def _response_from_intelligent_parse(
    parse_result,
    raw_text: str,
    extraction_quality: Dict[str, Any],
    post_to_dataverse: bool,
    download: bool,
    bl_type: BlTypeQuery,
    *,
    routing_policy: str = "gemini_intelligent_workbook",
) -> Any:
    """Build ExtractResponse from parse_document_intelligently output."""
    extraction_quality.update(parse_result.quality)
    if parse_result.azure_warnings:
        extraction_quality["azure_warnings"] = parse_result.azure_warnings
    records = parse_result.records
    layout = parse_result.document_layout or "unknown"
    extraction_quality.setdefault("record_routing", {})
    extraction_quality["record_routing"].update({
        "policy": routing_policy,
        "mode": layout,
        "azure_fallback": len(records),
    })

    house_recs = [r for r in records if r.get("mesco_houseblno")]
    master_recs = [
        r for r in records
        if r.get("mesco_masterblno") and not r.get("mesco_houseblno")
    ]

    if layout in ("manifest", "master_with_houses") and len(house_recs) >= 2:
        master_record = master_recs[0] if master_recs else None
        crm_output = records_to_master_json(house_recs, master_record=master_record)
        house_output = records_to_house_json(house_recs, master_record=master_record)
        extraction_quality["consolidated_house_count"] = len(house_recs)
        return _build_response(
            crm_output, raw_text, extraction_quality,
            post_to_dataverse, download, house_output, bl_type=bl_type,
        )

    if len(records) >= 2 and layout == "multi_bl_pages":
        crm_masters = [records_to_master_json([v]) for v in records]
        return _build_response(
            crm_masters[0], raw_text, extraction_quality,
            post_to_dataverse, download, house_output={"value": []},
            crm_records=crm_masters, bl_type=bl_type,
        )

    if len(records) >= 2:
        crm_output = records_to_master_json(records)
        house_output = records_to_house_json(records)
    else:
        crm_output = records_to_master_json(records)
        house_output = records_to_house_json(records)
    return _build_response(
        crm_output, raw_text, extraction_quality,
        post_to_dataverse, download, house_output, bl_type=bl_type,
    )


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

    consol_job_no = cells.get("C3") or (job_match.group(1) if job_match else None)
    job_no = _value_below_label(cells, r"JOB\s*NO") or header_map.get("JOB NO.")
    # An LCL loading sheet may have no ocean M/BL number — only a JOB NO that
    # identifies the consolidation. Use it as the master B/L so the master
    # operation has a B/L and every house links back to it via
    # mesco_masterbllinkno.
    master_bl = mbl_no or consol_job_no or job_no

    return {
        "consol_job_no": consol_job_no,
        "mesco_masterblno": master_bl,
        "mesco_vessel": vessel,
        "mesco_voytruckno": voyage,
        "pod": cells.get("J3") or _value_below_label(cells, r"POD", r"PORT\s+OF\s+DISCHARG") or header_map.get("Port Of Discharging"),
        "origin": _value_below_label(cells, r"PORT\s+OF\s+LOADING", r"PLACE\s+OF\s+RECEIPT", r"ORIGIN") or header_map.get("Port Of Loading"),
        "container_number": container,
        "seal_number": seal,
        "mesco_containertype": container_type,
        "mesco_etdorigin": _excel_serial_date(etd_value),
        "carrier": _value_below_label(cells, r"CARRIER") or header_map.get("Carrier"),
        "job_no": job_no,
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


@router.post(
    "/extract/file",
    response_model=ExtractResponse,
    summary="Extract from any supported file (PDF, XLSX, XLS, CSV)",
    description=(
        "Upload a PDF, Excel, or CSV file to extract B/L data. "
        "Choose **bl_type** (master or house) in the form below."
    ),
    tags=["Extraction"],
)
async def extract_file(
    request: Request,
    file: UploadFile = File(..., description="PDF, XLSX, XLS, or CSV file"),
    bl_type: BlTypeQuery = Form(
        BlTypeQuery.master,
        description="B/L type: master (886150001) or house (886150002)",
    ),
    llm_provider: Optional[LlmProviderQuery] = Form(
        LlmProviderQuery.gemini,
        description="AI backend: Gemini API",
    ),
    llm_model: Optional[GeminiModelQuery] = Form(
        None,
        description="Gemini model id",
    ),
    post_to_dataverse: bool = Form(
        True,
        description="Automatically upload extracted data to Dynamics 365 Dataverse",
    ),
    download: bool = Form(
        False,
        description="Download the CRM JSON as a file instead of returning the normal response",
    ),
    apply_custom_rules: bool = Form(
        True,
        description="Apply CRM business rules (freight→booking, load type, LCL TEUs, house totals)",
    ),
):
    provider_val = llm_provider.value if llm_provider else None
    model_val = llm_model.value if llm_model else None
    try:
        validate_llm_request(provider_val, model_val)
    except ValueError as exc:
        return ExtractResponse(success=False, error=str(exc))

    audit_id: Optional[str] = None
    audit_started_at = datetime.now(timezone.utc)
    try:
        file_bytes = await file.read()
        await file.seek(0)
        audit_id = audit_store.start_upload(
            request=request,
            file=file,
            file_bytes=file_bytes,
            bl_type=bl_type,
            post_to_dataverse=post_to_dataverse,
            llm_provider=llm_provider,
            llm_model=llm_model,
            apply_custom_rules=apply_custom_rules,
        )
    except Exception as audit_exc:
        logger.warning("Upload audit start failed: %s", audit_exc)

    try:
        with llm_request_overrides(provider_val, model_val):
            result = await _extract_file_inner(
                file,
                bl_type=bl_type,
                post_to_dataverse=post_to_dataverse,
                download=download,
                apply_custom_rules=apply_custom_rules,
            )
            audit_store.finish_upload(audit_id, result, started_at=audit_started_at)
            return result
    except Exception as exc:
        audit_store.fail_upload(audit_id, exc, started_at=audit_started_at)
        return ExtractResponse(success=False, error=str(exc))


async def _extract_file_inner(
    file: UploadFile,
    *,
    bl_type: BlTypeQuery,
    post_to_dataverse: bool,
    download: bool,
    apply_custom_rules: bool = True,
):
    from app.domain.rules.custom_business_rules import use_custom_rules

    try:
        with use_custom_rules(apply_custom_rules):
            return await _extract_file_inner_impl(
                file,
                bl_type=bl_type,
                post_to_dataverse=post_to_dataverse,
                download=download,
            )
    except Exception as exc:
        return ExtractResponse(success=False, error=str(exc))


async def _extract_file_inner_impl(
    file: UploadFile,
    *,
    bl_type: BlTypeQuery,
    post_to_dataverse: bool,
    download: bool,
):
    try:
        file_bytes = await file.read()
        extracted = extract_document_text_professionally(file_bytes, file.filename)
        
        raw_text = extracted.get("text", "")
        extraction_quality = extracted.get("quality", {})
        records = extracted.get("records", [])

        if not raw_text.strip():
            return ExtractResponse(success=False, error="No text extracted from file.")

        extracted["filename"] = file.filename

        # If spreadsheet has individual records, process each separately
        if records and len(records) > 0:
            # Gemini: read the whole workbook natively (xlsx layout + long text).
            if _should_use_gemini_workbook_llm(records, raw_text):
                parse_result = parse_document_intelligently(
                    raw_text,
                    extracted,
                    file_bytes=file_bytes,
                    filename=file.filename,
                )
                if parse_result.records:
                    extraction_quality["record_routing"] = {
                        "direct": 0,
                        "azure_fallback": len(parse_result.records),
                        "skipped": 0,
                        "policy": "gemini_whole_workbook",
                        "mode": "native_spreadsheet_or_chunked_text",
                    }
                    return _response_from_intelligent_parse(
                        parse_result,
                        raw_text,
                        extraction_quality,
                        post_to_dataverse,
                        download,
                        bl_type,
                        routing_policy="gemini_whole_workbook",
                    )

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
                    result = process_single_record(
                        record_text,
                        source_info,
                        file_bytes=file_bytes,
                        filename=file.filename,
                    )
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
                parse_result = parse_document_intelligently(
                    raw_text,
                    extracted,
                    file_bytes=file_bytes,
                    filename=file.filename,
                )
                if not parse_result.records:
                    return ExtractResponse(
                        success=False,
                        error="No processable records in spreadsheet/workbook.",
                        extraction_quality=extraction_quality,
                    )
                return _response_from_intelligent_parse(
                    parse_result,
                    raw_text,
                    extraction_quality,
                    post_to_dataverse,
                    download,
                    bl_type,
                    routing_policy="intelligent_workbook_fallback",
                )

            crm_output = records_to_master_json(extracted_records)
            house_output = records_to_house_json(extracted_records)
            return _build_response(crm_output, raw_text, extraction_quality, post_to_dataverse, download, house_output, bl_type=bl_type)
        
        if is_consolidation_sea_waybill(raw_text):
            sea_waybill = parse_consolidation_sea_waybill(raw_text)
            if sea_waybill:
                validated = validate_and_correct(sea_waybill, raw_text)
                extraction_quality["document_type_detected"] = "consolidation_sea_waybill_pdf"
                extraction_quality["record_routing"] = {
                    "direct": 1,
                    "azure_fallback": 0,
                    "skipped": 0,
                    "policy": "pdf_consolidation_sea_waybill",
                    "mode": "master_with_attached_list",
                }
                house_records = build_house_records_for_consolidation_sea_waybill(
                    validated,
                    raw_text,
                )
                if house_records:
                    extraction_quality["attached_list_house_count"] = len(house_records)
                    master_for_crm = (
                        master_record_without_house_cargo(validated)
                        if any(r.get("_per_house_cargo") for r in house_records)
                        else validated
                    )
                    crm_output = records_to_master_json(
                        house_records,
                        master_record=master_for_crm,
                    )
                    house_output = records_to_house_json(
                        house_records,
                        master_record=master_for_crm,
                    )
                else:
                    crm_output = records_to_master_json([validated])
                    house_output = records_to_house_json([validated])
                resolved_bl = normalize_bl_type(getattr(bl_type, "value", bl_type))
                if resolved_bl == "house" and not house_records:
                    bl_type = BlTypeQuery.master
                    extraction_quality["bl_type_corrected"] = "master_consolidation_sea_waybill"
                return _build_response(
                    crm_output,
                    raw_text,
                    extraction_quality,
                    post_to_dataverse,
                    download,
                    house_output,
                    bl_type=bl_type,
                )

        # No individual records: future/unknown Excel layouts go to Azure as one workbook.
        extraction_quality["record_routing"] = {
            "direct": 0,
            "azure_fallback": 1,
            "skipped": 0,
            "policy": "direct_when_confident_else_azure",
            "mode": "whole_document_or_workbook",
        }
        if is_tur_cargo_manifest(raw_text):
            manifest = parse_tur_cargo_manifest(raw_text)
            if manifest:
                house_records = [
                    validate_and_correct(rec, raw_text)
                    for rec in manifest["house_records"]
                ]
                master_record = validate_and_correct(
                    manifest["master_record"],
                    raw_text,
                )
                extraction_quality["document_type_detected"] = "tur_cargo_manifest_pdf"
                extraction_quality["manifest_row_count"] = len(house_records)
                extraction_quality["record_routing"] = {
                    "direct": len(house_records),
                    "azure_fallback": 0,
                    "skipped": 0,
                    "policy": "pdf_tur_cargo_manifest",
                    "mode": "manifest_rows",
                }
                crm_output = records_to_master_json(
                    house_records,
                    master_record=master_record,
                )
                house_output = records_to_house_json(
                    house_records,
                    master_record=master_record,
                )
                return _build_response(
                    crm_output,
                    raw_text,
                    extraction_quality,
                    post_to_dataverse,
                    download,
                    house_output,
                    bl_type=bl_type,
                )

        if is_export_lcl_manifest(raw_text):
            manifest = parse_export_lcl_manifest(raw_text)
            if manifest:
                house_records = [
                    validate_and_correct(rec, raw_text)
                    for rec in manifest["house_records"]
                ]
                master_record = validate_and_correct(
                    manifest["master_record"],
                    raw_text,
                )
                extraction_quality["document_type_detected"] = "export_lcl_manifest_pdf"
                extraction_quality["manifest_row_count"] = len(house_records)
                extraction_quality["record_routing"] = {
                    "direct": len(house_records),
                    "azure_fallback": 0,
                    "skipped": 0,
                    "policy": "pdf_export_lcl_manifest",
                    "mode": "manifest_rows",
                }
                crm_output = records_to_master_json(
                    house_records,
                    master_record=master_record,
                )
                house_output = records_to_house_json(
                    house_records,
                    master_record=master_record,
                )
                return _build_response(
                    crm_output,
                    raw_text,
                    extraction_quality,
                    post_to_dataverse,
                    download,
                    house_output,
                    bl_type=bl_type,
                )

        if is_freight_debit_note(raw_text):
            debit_record = parse_freight_debit_note(raw_text)
            if debit_record:
                validated = validate_and_correct(debit_record, raw_text)
                extraction_quality["document_type_detected"] = "freight_debit_note_pdf"
                extraction_quality["record_routing"] = {
                    "direct": 1,
                    "azure_fallback": 0,
                    "skipped": 0,
                    "policy": "pdf_freight_debit_note",
                    "mode": "single_house_from_debit_note",
                }
                house_output = records_to_house_json([validated])
                resolved_bl = normalize_bl_type(
                    getattr(bl_type, "value", bl_type),
                )
                if resolved_bl == "house" and house_output.get("value"):
                    crm_output = house_output["value"][0]
                else:
                    crm_output = records_to_master_json([validated])
                return _build_response(
                    crm_output,
                    raw_text,
                    extraction_quality,
                    post_to_dataverse,
                    download,
                    house_output,
                    bl_type=bl_type,
                )

        if is_standard_house_bl(raw_text):
            house_record = parse_standard_house_bl(raw_text)
            if house_record:
                # This direct parser reads already-labelled House B/L fields.
                # The broad PDF validator can over-enrich noisy OCR on this
                # layout and invent a master link from phone/reference numbers.
                validated = house_record
                extraction_quality["document_type_detected"] = "standard_house_bl_pdf"
                extraction_quality["record_routing"] = {
                    "direct": 1,
                    "azure_fallback": 0,
                    "skipped": 0,
                    "policy": "pdf_standard_house_bl",
                    "mode": "single_house_with_linking_evidence",
                }
                house_output = records_to_house_json([validated])
                resolved_bl = normalize_bl_type(
                    getattr(bl_type, "value", bl_type),
                )
                if resolved_bl == "house" and house_output.get("value"):
                    crm_output = house_output["value"][0]
                else:
                    crm_output = records_to_master_json([validated])
                return _build_response(
                    crm_output,
                    raw_text,
                    extraction_quality,
                    post_to_dataverse,
                    download,
                    house_output,
                    bl_type=bl_type,
                )

        if is_standard_master_bl(raw_text):
            master_record = parse_standard_master_bl(raw_text)
            if master_record:
                resolved_bl = normalize_bl_type(
                    getattr(bl_type, "value", bl_type),
                )
                # When user explicitly sets bl_type=house, skip the deterministic
                # master parser and let LLM/intelligent parsing extract the house.
                if resolved_bl == "house":
                    extraction_quality["deterministic_master_parser_skipped"] = \
                        "bl_type=house overrides standard master BL detection"
                else:
                    validated = validate_and_correct(
                        master_record,
                        raw_text,
                        enrichment_text=raw_text,
                    )
                    extraction_quality["document_type_detected"] = "standard_master_bl_pdf"
                    extraction_quality["record_routing"] = {
                        "direct": 1,
                        "azure_fallback": 0,
                        "skipped": 0,
                        "policy": "pdf_standard_master_bl",
                        "mode": "single_master_bl",
                    }
                    crm_output = records_to_master_json([validated])
                    house_output = records_to_house_json([validated])
                    bl_type = BlTypeQuery.master
                    extraction_quality["bl_type_corrected"] = "master_standard_bl"
                    return _build_response(
                        crm_output,
                        raw_text,
                        extraction_quality,
                        post_to_dataverse,
                        download,
                        house_output,
                        bl_type=bl_type,
                    )

        if is_cargo_manifest_hbl_blocks(raw_text):
            manifest = parse_cargo_manifest_hbl_blocks(raw_text)
            if manifest and len(manifest["house_records"]) >= 2:
                # House blocks are already self-contained and per-house clean.
                # Deliberately skip validate_and_correct here: its whole-document
                # enrichment (consignee block, merged HS codes, cargo description)
                # would bleed the master header / other houses into every record.
                house_records = manifest["house_records"]
                master_record = manifest["master_record"]
                extraction_quality["document_type_detected"] = "cargo_manifest_hbl_blocks_pdf"
                extraction_quality["consolidated_house_count"] = len(house_records)
                extraction_quality["record_routing"] = {
                    "direct": len(house_records),
                    "azure_fallback": 0,
                    "skipped": 0,
                    "policy": "pdf_cargo_manifest_hbl_blocks",
                    "mode": "one_master_with_house_records",
                    "document_layout": "master_with_labelled_house_blocks",
                }
                crm_output = records_to_master_json(
                    house_records,
                    master_record=master_record,
                )
                house_output = records_to_house_json(
                    house_records,
                    master_record=master_record,
                )
                return _build_response(
                    crm_output,
                    raw_text,
                    extraction_quality,
                    post_to_dataverse,
                    download,
                    house_output,
                    bl_type=bl_type,
                )

        if is_consolidated_lcl_multi_hbl(raw_text):
            consolidated = parse_consolidated_lcl_multi_hbl(raw_text)
            if consolidated and len(consolidated["house_records"]) >= 2:
                house_records = [
                    validate_and_correct(rec, raw_text)
                    for rec in consolidated["house_records"]
                ]
                master_record = validate_and_correct(
                    consolidated["master_record"],
                    raw_text,
                )
                extraction_quality["document_type_detected"] = "consolidated_lcl_multi_hbl_pdf"
                extraction_quality["consolidated_house_count"] = len(house_records)
                extraction_quality["record_routing"] = {
                    "direct": len(house_records),
                    "azure_fallback": 0,
                    "skipped": 0,
                    "policy": "pdf_consolidated_lcl_multi_hbl",
                    "mode": "one_master_with_house_records",
                    "document_layout": "master_with_houses",
                }
                crm_output = records_to_master_json(
                    house_records,
                    master_record=master_record,
                )
                house_output = records_to_house_json(
                    house_records,
                    master_record=master_record,
                )
                return _build_response(
                    crm_output,
                    raw_text,
                    extraction_quality,
                    post_to_dataverse,
                    download,
                    house_output,
                    bl_type=bl_type,
                )

        parse_result = parse_document_intelligently(
            raw_text,
            extracted,
            file_bytes=file_bytes,
            filename=file.filename,
        )
        extraction_quality.update(parse_result.quality)
        if parse_result.azure_warnings:
            extraction_quality["azure_warnings"] = parse_result.azure_warnings

        if not parse_result.records:
            return ExtractResponse(
                success=False,
                error="Could not extract any Bill of Lading records from the document.",
                raw_text=raw_text[:5000] + "..." if len(raw_text) > 5000 else raw_text,
                extraction_quality=extraction_quality,
            )

        extraction_quality["record_routing"] = {
            "direct": 0,
            "azure_fallback": len(parse_result.records),
            "skipped": 0,
            "policy": "azure_intelligent_with_fallback",
            "mode": "one_master_per_bl_record",
            "document_layout": parse_result.document_layout,
        }

        if len(parse_result.records) >= 2:
            extraction_quality["multi_bl_count"] = len(parse_result.records)
            crm_masters = [records_to_master_json([v]) for v in parse_result.records]
            return _build_response(
                crm_masters[0],
                raw_text,
                extraction_quality,
                post_to_dataverse,
                download,
                house_output={"value": []},
                crm_records=crm_masters,
                bl_type=bl_type,
            )

        validated = parse_result.records[0]
        house_records = build_house_records_for_consolidation_sea_waybill(
            validated,
            raw_text,
        )
        if house_records:
            extraction_quality["attached_list_house_count"] = len(house_records)
            master_for_crm = (
                master_record_without_house_cargo(validated)
                if any(r.get("_per_house_cargo") for r in house_records)
                else validated
            )
            crm_output = records_to_master_json(
                house_records,
                master_record=master_for_crm,
            )
            house_output = records_to_house_json(
                house_records,
                master_record=master_for_crm,
            )
        else:
            extracted_records = [validated]
            crm_output = records_to_master_json(extracted_records)
            house_output = records_to_house_json(extracted_records)

        return _build_response(crm_output, raw_text, extraction_quality, post_to_dataverse, download, house_output, bl_type=bl_type)
    except Exception as exc:
        return ExtractResponse(success=False, error=str(exc))


def _build_response(
    crm_output: Dict[str, Any],
    raw_text: str,
    extraction_quality: Dict[str, Any],
    post_to_dataverse: bool,
    download: bool = False,
    house_output: Optional[Dict[str, Any]] = None,
    crm_records: Optional[List[Dict[str, Any]]] = None,
    bl_type: str = "master",
) -> Any:
    dataverse_result = None
    dataverse_error = None
    masters = crm_records if crm_records else None
    resolved_bl = getattr(bl_type, "value", bl_type)

    from app.domain.rules.custom_business_rules import apply_crm_payload_rules, custom_rules_enabled

    # When user explicitly sets bl_type=house for a single record, use
    # standalone house payload instead of master-level payload with nested
    # houses. This prevents cargo/containers from being placed on the wrong
    # operation level and avoids creating an extra empty nested house.
    if resolved_bl == "house" and isinstance(house_output, dict):
        house_values = house_output.get("value") or []
        if len(house_values) == 1 and not masters:
            crm_output = house_values[0]

    if isinstance(crm_output, dict) and crm_output:
        apply_bl_type_to_crm_payload(crm_output, resolved_bl)
    if custom_rules_enabled() and isinstance(crm_output, dict) and crm_output:
        apply_crm_payload_rules(crm_output)
    if masters:
        for m in masters:
            if isinstance(m, dict):
                apply_bl_type_to_crm_payload(m, resolved_bl)

    # Final defensive guard: enforce Dataverse string-column length caps on
    # every master payload before either Dataverse POST or response download.
    # This prevents a single oversized field (e.g. mesco_cargodescription >
    # 1500 chars) from blocking the entire save with a 0x80048d19 / 400 error.
    if isinstance(crm_output, dict) and crm_output:
        cap_nested_payload(crm_output)
    if masters:
        for m in masters:
            if isinstance(m, dict):
                cap_nested_payload(m)

    if post_to_dataverse:
        try:
            if masters and len(masters) > 1:
                uploaded = []
                for idx, crm in enumerate(masters):
                    uploaded.append({"index": idx, **upload_crm_json(crm)})
                dataverse_result = {"masters": uploaded, "count": len(uploaded)}
            elif crm_output:
                dataverse_result = upload_crm_json(crm_output)
        except Exception as exc:
            dataverse_error = str(exc)
            logger.warning("Dataverse upload failed: %s", dataverse_error)

    if download:
        payload: Any = crm_output
        if masters and len(masters) > 1:
            payload = {"multi_bl": True, "masters": masters}
        json_bytes = json.dumps(payload, indent=2, default=str).encode("utf-8")
        return Response(
            content=json_bytes,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="crm_output.json"',
                "Content-Length": str(len(json_bytes)),
            },
        )

    if isinstance(extraction_quality, dict):
        resolved_bl_type = getattr(bl_type, "value", bl_type)
        extraction_quality["bl_type"] = resolved_bl_type
        extraction_quality["mesco_bltype"] = (
            886150002 if resolved_bl_type == "house" else 886150001
        )
        extraction_quality.update(llm_meta())
        if "llm_usage" not in extraction_quality:
            if extraction_quality.get("llm_attempted"):
                extraction_quality["llm_usage_available"] = False
            else:
                extraction_quality["llm_usage"] = {
                    "provider": extraction_quality.get("llm_provider"),
                    "model": extraction_quality.get("llm_model"),
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                }
                extraction_quality["llm_usage_available"] = True

    return ExtractResponse(
        success=True,
        data=crm_output,
        records=masters,
        house_data=house_output,
        raw_text=raw_text[:5000] + "..." if len(raw_text) > 5000 else raw_text,
        extraction_quality=extraction_quality,
        dataverse_result=dataverse_result,
        dataverse_error=dataverse_error,
    )


@router.post(
    "/test/pdf/batch",
    response_model=BatchPdfTestResponse,
    summary="Batch test multiple PDFs (extract + validate, no Dataverse)",
    description=(
        "Upload one or more PDF files. Each file is processed through the same extraction "
        "pipeline as /extract/file (OCR, intelligent parse, validation). Returns per-file "
        "pass/fail, quality score, issues, and optional CRM JSON. Does not upload to Dataverse."
    ),
)
async def test_pdf_batch(
    files: List[UploadFile] = File(..., description="One or more PDF files"),
    include_crm_json: bool = Query(
        True,
        description="Include full CRM master JSON per file in each result",
    ),
    include_raw_text: bool = Query(
        False,
        description="Include OCR text preview (first 3000 chars) per file",
    ),
):
    if not files:
        return BatchPdfTestResponse(
            total=0,
            succeeded=0,
            failed=0,
            passed=0,
            failed_validation=0,
            average_score=0.0,
            total_processing_ms=0,
            results=[],
        )

    results: List[Dict[str, Any]] = []
    succeeded = failed = passed_count = failed_validation = 0
    total_ms = 0
    scores: List[int] = []

    for upload in files:
        file_bytes = await upload.read()
        item = process_pdf_bytes(
            file_bytes,
            upload.filename or "upload.pdf",
            include_raw_text_preview=include_raw_text,
        )
        result_dict = item.to_dict(include_crm=include_crm_json, include_raw=include_raw_text)
        results.append(result_dict)

        total_ms += item.processing_ms
        if item.success:
            succeeded += 1
            scores.append(item.score)
            if item.passed:
                passed_count += 1
            else:
                failed_validation += 1
        else:
            failed += 1
            scores.append(0)

    avg_score = round(sum(scores) / len(scores), 1) if scores else 0.0

    return BatchPdfTestResponse(
        total=len(files),
        succeeded=succeeded,
        failed=failed,
        passed=passed_count,
        failed_validation=failed_validation,
        average_score=avg_score,
        total_processing_ms=total_ms,
        results=results,
    )


@router.post("/extract/text", response_model=ExtractResponse, tags=["Extraction"])
async def extract_text(request: ExtractRequest):
    provider_val = request.llm_provider.value if request.llm_provider else None
    model_val = (request.llm_model or "").strip() or None
    try:
        validate_llm_request(provider_val, model_val)
    except ValueError as exc:
        return ExtractResponse(success=False, error=str(exc))

    try:
        with llm_request_overrides(provider_val, model_val):
            raw_text = request.ocr_text
            if not raw_text.strip():
                return ExtractResponse(success=False, error="No text provided.")

            parse_result = parse_document_intelligently(raw_text)
            if not parse_result.records:
                return ExtractResponse(success=False, error="No B/L records extracted.", raw_text=raw_text)

            if len(parse_result.records) >= 2:
                crm_masters = [records_to_master_json([v]) for v in parse_result.records]
                for m in crm_masters:
                    apply_bl_type_to_crm_payload(m, request.bl_type.value)
                return ExtractResponse(
                    success=True,
                    data=crm_masters[0],
                    records=crm_masters,
                    raw_text=raw_text,
                    extraction_quality=parse_result.quality,
                )

            validated = parse_result.records[0]
            crm_output = records_to_master_json([validated])
            apply_bl_type_to_crm_payload(crm_output, request.bl_type.value)
            return ExtractResponse(
                success=True,
                data=crm_output,
                raw_text=raw_text,
                extraction_quality=parse_result.quality,
            )
    except Exception as exc:
        return ExtractResponse(success=False, error=str(exc))


@router.post(
    "/extract/pdf",
    response_model=ExtractResponse,
    summary="Extract from PDF (Master or House B/L)",
    description=(
        "Upload a PDF and extract B/L data. Use **bl_type** to post as "
        "**master** (886150001) or **house** (886150002) in Dynamics.\n\n"
        "**AI provider:** Gemini API (server-side)."
    ),
    tags=["Extraction"],
)
async def extract_pdf(
    request: Request,
    file: UploadFile = File(..., description="PDF file to extract"),
    bl_type: BlTypeQuery = Query(
        BlTypeQuery.master,
        title="B/L Type",
        description="Post as Master B/L (886150001) or House B/L (886150002)",
    ),
    llm_provider: Optional[LlmProviderQuery] = Query(
        LlmProviderQuery.gemini,
        description="AI backend: Gemini API",
    ),
    llm_model: Optional[GeminiModelQuery] = Query(
        GeminiModelQuery.gemini_3_pro_preview,
        description="Gemini model id",
    ),
    post_to_dataverse: bool = Query(
        True,
        description="Automatically upload extracted data to Dynamics 365 Dataverse",
    ),
    download: bool = Query(
        False,
        description="Download the CRM JSON as a file instead of returning the normal response",
    ),
    apply_custom_rules: bool = Query(
        True,
        description="Apply CRM business rules (freight→booking, load type, LCL TEUs, house totals)",
    ),
):
    return await extract_file(
        request,
        file,
        bl_type=bl_type,
        llm_provider=llm_provider,
        llm_model=llm_model,
        post_to_dataverse=post_to_dataverse,
        download=download,
        apply_custom_rules=apply_custom_rules,
    )


@router.post(
    "/extract/excel",
    response_model=ExtractResponse,
    summary="Extract from Excel file",
    description=(
        "Upload an Excel file (.xlsx, .xls, .csv). Choose **bl_type** (master or house) in the form.\n\n"
        "**AI provider:** Gemini API (server-side)."
    ),
    tags=["Extraction"],
)
async def extract_excel(
    request: Request,
    file: UploadFile = File(..., description="Excel or CSV file (.xlsx, .xls, .csv)"),
    bl_type: BlTypeQuery = Form(
        BlTypeQuery.master,
        description="B/L type: master (886150001) or house (886150002)",
    ),
    llm_provider: Optional[LlmProviderQuery] = Form(
        LlmProviderQuery.gemini,
        description="AI backend: Gemini API",
    ),
    llm_model: Optional[GeminiModelQuery] = Form(
        GeminiModelQuery.gemini_3_pro_preview,
        description="Gemini model id when llm_provider=gemini",
    ),
    post_to_dataverse: bool = Form(
        True,
        description="Automatically upload extracted data to Dynamics 365 Dataverse",
    ),
    download: bool = Form(
        False,
        description="Download the CRM JSON as a file instead of returning the normal response",
    ),
    apply_custom_rules: bool = Form(
        True,
        description="Apply CRM business rules (freight→booking, load type, LCL TEUs, house totals)",
    ),
):
    return await extract_file(
        request,
        file,
        bl_type=bl_type,
        llm_provider=llm_provider,
        llm_model=llm_model,
        post_to_dataverse=post_to_dataverse,
        download=download,
        apply_custom_rules=apply_custom_rules,
    )


@router.post(
    "/extract/master",
    response_model=ExtractResponse,
    summary="Extract B/L as Master operation",
    description=(
        "Extract from PDF, Excel, or CSV and stamp **mesco_bltype = 886150001 (Master)** "
        "before upload. Same pipeline as POST /extract/file?bl_type=master."
    ),
    tags=["Extraction — B/L type"],
)
async def extract_as_master(
    request: Request,
    file: UploadFile = File(..., description="PDF, XLSX, XLS, or CSV file"),
    post_to_dataverse: bool = Form(True, description="Automatically upload to Dynamics 365 Dataverse"),
    download: bool = Form(False, description="Download CRM JSON instead of JSON response"),
):
    return await extract_file(
        request,
        file,
        bl_type=BlTypeQuery.master,
        post_to_dataverse=post_to_dataverse,
        download=download,
    )


@router.post(
    "/extract/house",
    response_model=ExtractResponse,
    summary="Extract B/L as House operation",
    description=(
        "Extract from PDF, Excel, or CSV and stamp **mesco_bltype = 886150002 (House)** "
        "before upload. Same pipeline as POST /extract/file?bl_type=house."
    ),
    tags=["Extraction — B/L type"],
)
async def extract_as_house(
    request: Request,
    file: UploadFile = File(..., description="PDF, XLSX, XLS, or CSV file"),
    post_to_dataverse: bool = Form(True, description="Automatically upload to Dynamics 365 Dataverse"),
    download: bool = Form(False, description="Download CRM JSON instead of JSON response"),
):
    return await extract_file(
        request,
        file,
        bl_type=BlTypeQuery.house,
        post_to_dataverse=post_to_dataverse,
        download=download,
    )
