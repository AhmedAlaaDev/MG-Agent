"""Dynamics 365 / Dataverse upload and read-back routes."""

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

from app.api.schemas import BlTypeQuery, CompareRequest, CompareResponse, DataverseUploadResponse
from app.infrastructure.dataverse.uploader import upload_crm_json

router = APIRouter()
logger = logging.getLogger(__name__)

@router.post(
    "/upload/dataverse",
    response_model=DataverseUploadResponse,
    summary="Upload CRM JSON file to Dynamics 365 Dataverse",
    description="Upload a CRM JSON file (previously downloaded from /extract/pdf?download=true or /extract/excel?download=true) to Dynamics 365 Dataverse. Creates the full hierarchy: master operation, houses, containers, and cargo.",
)
async def upload_to_dataverse(
    file: UploadFile = File(..., description="CRM JSON file (the file downloaded with ?download=true)"),
    bl_type: BlTypeQuery = Query(
        BlTypeQuery.master,
        description="B/L type for the created operation: master or house (sets mesco_bltype on upload)",
    ),
    apply_custom_rules: bool = Query(
        True,
        description="Apply CRM business rules (freight→booking, load type, LCL TEUs, house totals)",
    ),
):
    try:
        content = await file.read()
        crm_data = json.loads(content.decode("utf-8"))
        from app.domain.rules.custom_business_rules import prepare_crm_payload_for_upload, use_custom_rules

        with use_custom_rules(apply_custom_rules):
            if isinstance(crm_data, dict):
                masters = crm_data.get("masters")
                if isinstance(masters, list):
                    for m in masters:
                        if isinstance(m, dict):
                            apply_bl_type_to_crm_payload(m, bl_type.value)
                            prepare_crm_payload_for_upload(m)
                else:
                    apply_bl_type_to_crm_payload(crm_data, bl_type.value)
                    prepare_crm_payload_for_upload(crm_data)
            result = upload_crm_json(crm_data)
        return DataverseUploadResponse(success=True, result=result)
    except json.JSONDecodeError:
        return DataverseUploadResponse(success=False, error="Invalid JSON file.")
    except Exception as exc:
        return DataverseUploadResponse(success=False, error=str(exc))


@router.post(
    "/upload/dataverse/json",
    response_model=DataverseUploadResponse,
    summary="Upload CRM JSON payload to Dynamics 365 Dataverse (JSON body)",
    description="Send the CRM JSON structure directly as the request body to upload to Dynamics 365 Dataverse. The body should be the same structure as the 'data' field returned by /extract/pdf or /extract/excel.",
)
async def upload_to_dataverse_json(
    payload: Dict[str, Any] = Body(..., description="CRM JSON structure (the 'data' field from extract endpoints)"),
    bl_type: BlTypeQuery = Query(
        BlTypeQuery.master,
        description="B/L type for the created operation: master or house (sets mesco_bltype on upload)",
    ),
    apply_custom_rules: bool = Query(
        True,
        description="Apply CRM business rules (freight→booking, load type, LCL TEUs, house totals)",
    ),
):
    try:
        from app.domain.rules.custom_business_rules import prepare_crm_payload_for_upload, use_custom_rules

        with use_custom_rules(apply_custom_rules):
            if isinstance(payload, dict):
                apply_bl_type_to_crm_payload(payload, bl_type.value)
                prepare_crm_payload_for_upload(payload)
            result = upload_crm_json(payload)
        return DataverseUploadResponse(success=True, result=result)
    except Exception as exc:
        return DataverseUploadResponse(success=False, error=str(exc))
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


@router.get("/dynamics/operation/{master_id}")
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

@router.get("/dynamics/operation/{master_id}/full")
async def get_dynamics_operation_full(master_id: str):
    """Fetch a master operation with ALL fields, lookups (GUID + display name),
    option-set labels, and nested houses/containers/cargo.

    Unlike /dynamics/operation/{id}, this keeps the OData annotations
    (`@OData.Community.Display.V1.FormattedValue`, `_<field>_value`, lookup
    logical names) so the operation-review React page can resolve lookups and
    option sets exactly as Dynamics does.
    """
    try:
        client = DataverseClientService.get_instance()
        expand = (
            "mesco_Operation_mesco_Operation_mesco_Operation("
            "$expand=mesco_Container_mesco_houses,"
            "mesco_Cargo_HouseOperation_mesco_Operation),"
            "mesco_Container_MasterOperation_mesco_Operation($expand=mesco_ContainerNo),"
            "mesco_Cargo_MasterOperation_mesco_Operation"
        )
        resp = client.get(f"{_ENTITY}({master_id})?$expand={expand}")
        return {"success": True, "data": resp.json()}
    except Exception as exc:
        logger.exception("Failed to fetch full operation %s", master_id)
        return {"success": False, "error": str(exc)}


@router.get("/dynamics/operation/{master_id}/houses")
async def get_dynamics_houses(master_id: str):
    """Fetch house bills under a master operation from Dataverse.

    Queries mesco_operations where _mesco_operation_value = master_id
    (the lookup that links a house to its master). 
    Returns each house with its containers and cargo.
    """
    try:
        client = DataverseClientService.get_instance()
        expand = (
            "mesco_Container_mesco_houses,"
            "mesco_Cargo_HouseOperation_mesco_Operation"
        )
        filter_query = f"_mesco_operation_value eq {master_id}"
        resp = client.get(
            f"{_ENTITY}?$filter={filter_query}&$expand={expand}"
        )
        data = resp.json()
        houses = data.get("value", []) if isinstance(data, dict) else []
        return {
            "success": True,
            "master_id": master_id,
            "count": len(houses),
            "houses": [_clean_dataverse_response(h) for h in houses],
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


@router.get("/dynamics/house/{house_id}")
async def get_dynamics_house(house_id: str):
    """Fetch a single house bill by its ID with containers and cargo."""
    try:
        client = DataverseClientService.get_instance()
        expand = (
            "mesco_Container_mesco_houses,"
            "mesco_Cargo_HouseOperation_mesco_Operation"
        )
        resp = client.get(f"{_ENTITY}({house_id})?$expand={expand}")
        data = resp.json()
        return {"success": True, "house": _clean_dataverse_response(data)}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


@router.post("/dynamics/compare", response_model=CompareResponse)
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
