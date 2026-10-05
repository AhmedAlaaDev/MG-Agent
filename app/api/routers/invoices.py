"""Invoice / debit-note extraction and Dynamics cost-posting routes."""

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID

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
from app.api.routers.tariffs import get_vendor_tariffs

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

from app.api.schemas import InvoiceExtractResponse, MultiInvoiceExtractResponse, MultiInvoiceGroupResult

router = APIRouter()
logger = logging.getLogger(__name__)

def _extract_wecan_excel_invoice(file_bytes: bytes, filename: str) -> Dict[str, Any]:
    """Deterministically parse a legacy WE-CAN .xls/.xlsx invoice workbook."""
    from app.infrastructure.spreadsheet.wecan_proxy_bill_extractor import extract_wecan_proxy_bill, to_multi_invoice_payload

    suffix = ".xls" if str(filename or "").lower().endswith(".xls") else ".xlsx"
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="wecan_invoice_", suffix=suffix, delete=False) as handle:
            handle.write(file_bytes)
            temp_path = handle.name
        parsed = extract_wecan_proxy_bill(temp_path)
        payload = to_multi_invoice_payload(parsed, filename)
        sheets = parsed.get("sheets") or []
        if sheets:
            meta = sheets[0].get("meta") or {}
            if meta.get("consol_description"):
                payload["cargo_description"] = meta["consol_description"]
        return payload
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _invoice_container_parts(extracted_data: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Normalize container/seal/type values extracted from an invoice."""
    raw_container = str(extracted_data.get("container_number") or "").strip()
    raw_seal = str(extracted_data.get("seal_number") or "").strip()
    container_type = str(
        extracted_data.get("container_type")
        or extracted_data.get("container_size")
        or ""
    ).strip()

    parts = [p.strip() for p in re.split(r"[/,;|]", raw_container) if p.strip()]
    container_no = ""
    for part in parts or [raw_container]:
        candidate = _normalize_container_number(part)
        if re.fullmatch(r"[A-Z]{4}\d{7}", candidate):
            container_no = candidate
            break

    if not container_no:
        container_no = _normalize_container_number(raw_container)

    if not raw_seal and len(parts) > 1:
        for part in parts[1:]:
            if not re.search(r"\d+\s*x\s*\d+", part, re.I):
                raw_seal = part.strip()
                break

    if not container_type and len(parts) > 1:
        for part in parts[1:]:
            compact = re.sub(r"\s+", "", part.upper())
            match = re.search(r"(?:\d+X)?(20|40|45)(HC|HQ|GP|DV|DC|RF|OT|FR)?", compact)
            if match:
                container_type = "".join(g for g in match.groups() if g)
                break

    return {
        "container_no": container_no or None,
        "seal_no": raw_seal or None,
        "container_type": container_type or None,
    }


def _ensure_invoice_container(
    client: DataverseClientService,
    operation_id: str,
    extracted_data: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Create or update the operation container found on an invoice."""
    parts = _invoice_container_parts(extracted_data)
    container_no = parts["container_no"]
    if not container_no:
        return None

    container_no_id = _resolve_containerno_lookup(
        client,
        container_no,
        parts.get("container_type"),
    )
    if not container_no_id:
        logger.warning("Could not resolve/create ContainerNo lookup for %s", container_no)
        return None

    fields: Dict[str, Any] = {
        "mesco_name": container_no,
        "mesco_containernumber": container_no,
        "mesco_ContainerNo@odata.bind": f"/mesco_containernos({container_no_id})",
        "mesco_MasterOperation@odata.bind": f"/mesco_operations({operation_id})",
    }
    if parts.get("seal_no"):
        fields["mesco_carrierseal"] = parts["seal_no"]
    type_option = container_type_option(parts.get("container_type"))
    if type_option is not None:
        fields["mesco_containertype"] = type_option
    container_numbers = {
        "mesco_noofpackages": extracted_data.get("total_packages"),
        "mesco_grosskg": extracted_data.get("total_gross_weight_kg"),
        "mesco_volcbm": extracted_data.get("total_volume_cbm"),
    }
    for field_name, raw_value in container_numbers.items():
        if raw_value not in (None, ""):
            fields[field_name] = float(raw_value)
    fields["mesco_quantity"] = 1

    existing_id = _find_existing_container(client, operation_id, container_no)
    if existing_id:
        _update_entity(client, _CONTAINER_ENTITY, existing_id, fields)
        return {
            "id": existing_id,
            "container_no": container_no,
            "reused": True,
            "seal_no": parts.get("seal_no"),
        }

    container_id = _create_entity(client, _CONTAINER_ENTITY, fields)
    return {
        "id": container_id,
        "container_no": container_no,
        "reused": False,
        "seal_no": parts.get("seal_no"),
    }


def _clean_invoice_bl(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _invoice_missing_bl_error(bl_number: Any, *, kind: str = "B/L") -> str:
    """Explain that invoice posting never creates a missing operation."""
    label = str(bl_number or "").strip() or "unknown"
    return (
        f"{kind} {label} was not found in Dynamics; "
        "invoice lines were not posted and no operation was created"
    )


def _parse_invoice_charge_rows(raw_text: str) -> List[Dict[str, Any]]:
    """Parse table-style invoice charge rows that include an HBL/CNT column."""
    rows: List[Dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    lines = [line.strip() for line in raw_text.splitlines()]
    in_table = False
    pending_desc: List[str] = []
    index = 0

    def parse_row(line: str) -> Optional[Dict[str, Any]]:
        tokens = line.split()
        if len(tokens) < 9:
            return None
        hbl = tokens[0].upper()
        if not re.fullmatch(r"[A-Z]{3,}\d{5,}[A-Z0-9]*", hbl):
            return None
        try:
            kgs = float(tokens[1])
            cbm = float(tokens[2])
            credit = float(tokens[-1])
            debit = float(tokens[-2])
            currency = tokens[-3].upper()
            unit_price = float(tokens[-4])
            quantity = float(tokens[-5])
            unit = tokens[-6].upper()
        except (IndexError, ValueError):
            return None
        if not re.fullmatch(r"[A-Z]{3}", currency):
            return None
        return {
            "house_bl_number": hbl,
            "kgs": kgs,
            "cbm": cbm,
            "service_description": re.sub(r"\s+", " ", " ".join(tokens[3:-6])).strip().upper(),
            "unit": unit,
            "quantity": quantity,
            "unit_price": unit_price,
            "currency": currency,
            "total_amount": debit,
            "credit_amount": credit,
        }

    def is_table_noise(line: str) -> bool:
        upper = line.upper()
        return (
            not line
            or upper.startswith("[")
            or upper.startswith("HBL/CNT")
            or upper.startswith("SUB TOTAL")
            or upper.startswith("TOTAL:")
            or upper.startswith("WWW.")
            or "COPYRIGHT" in upper
        )

    while index < len(lines):
        line = lines[index]
        if "HBL/CNT" in line.upper() and "DEBIT" in line.upper():
            in_table = True
            pending_desc = []
            index += 1
            continue
        if in_table and line.upper().startswith(("SUB TOTAL", "TOTAL:")):
            in_table = False
            pending_desc = []
            index += 1
            continue
        if not in_table:
            index += 1
            continue

        parsed = parse_row(line)
        if parsed:
            desc_parts = [part for part in pending_desc if part]
            if parsed["service_description"]:
                desc_parts.append(parsed["service_description"])
            pending_desc = []

            lookahead = index + 1
            while lookahead < len(lines):
                next_line = lines[lookahead]
                if parse_row(next_line) or is_table_noise(next_line):
                    break
                desc_parts.append(next_line)
                lookahead += 1

            if desc_parts:
                parsed["service_description"] = re.sub(
                    r"\s+",
                    " ",
                    " ".join(desc_parts),
                ).strip().upper()

            signature = (
                parsed["house_bl_number"],
                parsed["service_description"],
                parsed["quantity"],
                parsed["unit_price"],
                parsed["total_amount"],
            )
            if signature not in seen:
                seen.add(signature)
                rows.append(parsed)
            index = lookahead
            continue

        if not is_table_noise(line):
            pending_desc.append(line)
        index += 1

    return rows


def _apply_operation_invoice_scope(
    extracted_data: Dict[str, Any],
    raw_text: str,
    operation_bl: Optional[str],
) -> Dict[str, Any]:
    """When an invoice contains many HBLs, keep only rows for the selected operation."""
    target_bl = _clean_invoice_bl(operation_bl)
    if not target_bl:
        return extracted_data

    rows = _parse_invoice_charge_rows(raw_text)
    matching_rows = [
        row for row in rows
        if _clean_invoice_bl(row.get("house_bl_number")) == target_bl
    ]
    if not matching_rows:
        return extracted_data

    scoped = dict(extracted_data)
    scoped["house_bl_number"] = operation_bl
    scoped["currency"] = matching_rows[0].get("currency") or scoped.get("currency")
    scoped["line_items"] = [
        {
            "service_description": row["service_description"],
            "quantity": row["quantity"],
            "unit_price": row["unit_price"],
            "total_amount": row["total_amount"],
        }
        for row in matching_rows
    ]
    scoped["invoice_scope"] = {
        "operation_bl": operation_bl,
        "source": "deterministic_hbl_table_filter",
        "original_line_items_count": len(extracted_data.get("line_items") or []),
        "matched_line_items_count": len(scoped["line_items"]),
        "document_hbl_count": len({_clean_invoice_bl(row.get("house_bl_number")) for row in rows}),
    }
    return scoped


def _invoice_groups_from_table(raw_text: str) -> List[Dict[str, Any]]:
    rows = _parse_invoice_charge_rows(raw_text)
    grouped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        hbl = row["house_bl_number"]
        group = grouped.setdefault(
            hbl,
            {
                "house_bl_number": hbl,
                "cbm": row.get("cbm"),
                "kgs": row.get("kgs"),
                "line_items": [],
            },
        )
        group["line_items"].append({
            "service_description": row["service_description"],
            "quantity": row["quantity"],
            "unit_price": row["unit_price"],
            "total_amount": row["total_amount"],
        })
    return list(grouped.values())


def _invoice_source_key(item: Dict[str, Any]) -> Optional[str]:
    parts: List[str] = []
    if item.get("source_row") not in (None, ""):
        parts.append(f"source row {item['source_row']}")
    if item.get("source_term") not in (None, ""):
        parts.append(f"term {item['source_term']}")
    return f"Invoice line {'; '.join(parts)}." if parts else None


def _invoice_cost_values(item: Dict[str, Any]) -> Dict[str, Any]:
    """Return Dataverse-safe values while preserving each invoice line total.

    Quote Cost Line unit amounts cannot be negative in Dataverse, but quantity
    can. Credits therefore use a negative quantity and positive unit amount;
    the source values are retained in Comments for auditability.
    """
    source_quantity = Decimal(str(item.get("quantity") or 1))
    source_unit_price = Decimal(str(item.get("unit_price") or 0))
    source_total = item.get("total_amount")
    if source_total in (None, ""):
        source_total = source_quantity * source_unit_price
    source_total = Decimal(str(source_total))

    is_credit = (
        str(item.get("posting_direction") or "").strip().lower() == "credit"
        or source_total < Decimal("0")
        or source_unit_price < Decimal("0")
        or source_quantity < Decimal("0")
    )
    quantity = -abs(source_quantity) if is_credit else abs(source_quantity)
    unit_price = abs(source_unit_price)
    if unit_price == 0 and source_total:
        unit_price = abs(source_total) / (abs(source_quantity) or Decimal("1"))

    quantity_step = Decimal("0.01")
    unit_step = Decimal("0.001")
    total_step = Decimal("0.001")
    direct_total = (quantity * unit_price).quantize(total_step)
    expected_total = source_total.quantize(total_step)
    precision_adjusted = (
        quantity.quantize(quantity_step) != quantity
        or unit_price.quantize(unit_step) != unit_price
        or direct_total != expected_total
    )
    if precision_adjusted:
        quantity = Decimal("-1") if is_credit else Decimal("1")
        unit_price = abs(source_total).quantize(unit_step)

    comment_parts: List[str] = []
    source_key = _invoice_source_key(item)
    if source_key:
        comment_parts.append(source_key)
    if is_credit or precision_adjusted:
        currency = str(item.get("currency") or "").strip()
        suffix = f" {currency}" if currency else ""
        reason = "Invoice credit" if is_credit else "Dynamics precision adjustment"
        comment_parts.append(
            f"{reason}; original quantity {source_quantity:g}, "
            f"unit price {source_unit_price:g}{suffix}, total {source_total:g}{suffix}."
        )

    return {
        "quantity": float(quantity),
        "unit_price": float(unit_price),
        "comments": " ".join(comment_parts) or None,
    }


def _ensure_invoice_house_cargo(
    client: DataverseClientService,
    *,
    house_operation_id: str,
    master_operation_id: str,
    container_id: Optional[str],
    group: Dict[str, Any],
    extracted_data: Dict[str, Any],
) -> str:
    """Persist house measurements on cargo, the source of Dynamics rollups."""
    hbl = str(group.get("house_bl_number") or "").strip()
    invoice = str(
        group.get("vendor_invoice_number")
        or extracted_data.get("vendor_invoice_number")
        or ""
    ).strip()
    source_id = f"{hbl} / INVOICE {invoice}"[:100]
    query = (
        "mesco_cargos?"
        "$select=mesco_cargoid,mesco_id,mesco_noofpackages,mesco_grosskg,mesco_volcbm"
        f"&$filter=_mesco_houseoperation_value eq {house_operation_id}&$top=100"
    )
    rows = client.get(query).json().get("value", [])
    tagged = [row for row in rows if str(row.get("mesco_id") or "") == source_id]

    expected = {
        "mesco_noofpackages": float(group.get("packages") or 0),
        "mesco_grosskg": float(group.get("kgs") or 0),
        "mesco_volcbm": float(group.get("cbm") or 0),
    }
    if rows and not tagged:
        current = {
            key: sum(float(row.get(key) or 0) for row in rows)
            for key in expected
        }
        if all(abs(current[key] - expected[key]) <= 0.01 for key in expected):
            _sync_operation_totals_from_cargo(
                client, house_operation_id, is_house=True, load_type=LCL
            )
            return str(rows[0]["mesco_cargoid"])
        raise ValueError(
            f"existing cargo measurements {current} conflict with invoice {expected}"
        )

    fields: Dict[str, Any] = {
        "mesco_id": source_id,
        "mesco_descriptionofgoods": (
            extracted_data.get("cargo_description")
            or f"Invoice cargo measurements {invoice}"
        ),
        **expected,
        "mesco_HouseOperation@odata.bind": f"/mesco_operations({house_operation_id})",
        "mesco_MasterOperation@odata.bind": f"/mesco_operations({master_operation_id})",
    }
    if container_id:
        fields["mesco_Conainter@odata.bind"] = f"/mesco_containers({container_id})"

    if tagged:
        cargo_id = str(tagged[0]["mesco_cargoid"])
        if not _update_entity(client, "mesco_cargos", cargo_id, fields):
            raise RuntimeError(f"failed to update invoice cargo {cargo_id}")
    else:
        cargo_id = _create_entity(client, "mesco_cargos", fields)

    _sync_operation_totals_from_cargo(
        client, house_operation_id, is_house=True, load_type=LCL
    )
    return cargo_id


def _invoice_cost_signature(item: Dict[str, Any]) -> Dict[str, Any]:
    values = _invoice_cost_values(item)
    return {
        "name": str(item.get("service_description") or "Invoice Charge").strip(),
        "quantity": values["quantity"],
        "unit_price": values["unit_price"],
    }


def _find_existing_invoice_cost_line(
    client: DataverseClientService,
    operation_id: str,
    is_master: bool,
    vendor_invoice_number: Optional[str],
    item: Dict[str, Any],
) -> Optional[str]:
    if not vendor_invoice_number:
        return None
    sig = _invoice_cost_signature(item)
    op_field = "_mesco_master3_value" if is_master else "_mesco_operation_value"
    name = sig["name"].replace("'", "''")
    invoice = str(vendor_invoice_number).replace("'", "''")
    query = (
        "xollsp_quotecostlines?"
        "$select=xollsp_quotecostlineid,xollsp_quantity,xollsp_unitamount,xollsp_comments,createdon"
        f"&$filter={op_field} eq {operation_id} "
        f"and mesco_vendorinvoicenumber eq '{invoice}' "
        f"and xollsp_name eq '{name}'"
        "&$orderby=createdon asc&$top=100"
    )
    rows = client.get(query).json().get("value", [])
    if not rows:
        return None

    source_key = _invoice_source_key(item)
    if source_key:
        keyed = [row for row in rows if source_key in str(row.get("xollsp_comments") or "")]
        if keyed:
            return keyed[0].get("xollsp_quotecostlineid")

    # Backward compatibility for records created before source keys were added.
    # Dataverse already rounded their decimal values, so use the oldest unkeyed
    # line as the canonical record and update it in place.
    legacy_rows = [
        row for row in rows
        if not str(row.get("xollsp_comments") or "").startswith("Invoice line ")
    ]
    if legacy_rows:
        return legacy_rows[0].get("xollsp_quotecostlineid")

    rounded_matches = [
        row for row in rows
        if round(float(row.get("xollsp_quantity") or 0), 2) == round(sig["quantity"], 2)
        and round(float(row.get("xollsp_unitamount") or 0), 3) == round(sig["unit_price"], 3)
    ]
    return rounded_matches[0].get("xollsp_quotecostlineid") if rounded_matches else None


def _parse_reviewed_payload(reviewed_json: Optional[str]) -> Optional[Dict[str, Any]]:
    """Decode the payload a caller already reviewed during a dry run.

    Returning it verbatim lets the confirm step post the figures the user saw
    instead of re-running the model, which would cost another call and could
    drift from what was approved. Dynamics lookups are still resolved
    server-side from this payload, so they cannot be spoofed by the caller.
    """
    raw = (reviewed_json or "").strip()
    if not raw or raw == "string":
        return None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"reviewed_json is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("reviewed_json must be a JSON object.")

    payload.pop("dynamics_mapping", None)
    payload.pop("mapping_validation", None)
    return payload


def _validate_tariff_selection(vendor_id: Optional[str], scheme_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not vendor_id and not scheme_id:
        return None
    if not vendor_id or not scheme_id:
        raise ValueError("Choose both a tariff vendor and a tariff card.")
    tariffs = get_vendor_tariffs(UUID(vendor_id))
    selected = next((tariff for tariff in tariffs["tariffs"] if tariff["id"] == scheme_id and tariff["current"]), None)
    if not selected:
        raise ValueError("The selected tariff is not current for this vendor.")
    return {"vendor": tariffs["vendor"], "scheme": selected}


def _vendor_matches_invoice(tariff_vendor_name: str, invoice_vendor_name: Optional[str]) -> Optional[bool]:
    if not invoice_vendor_name:
        return None
    tariff_name = re.sub(r"[^a-z0-9]", "", tariff_vendor_name.casefold())
    invoice_name = re.sub(r"[^a-z0-9]", "", invoice_vendor_name.casefold())
    if "saco" in tariff_name or "saco" in invoice_name:
        return "saco" in tariff_name and "saco" in invoice_name
    if "globelink" in tariff_name or "globelink" in invoice_name:
        return "globelink" in tariff_name and "globelink" in invoice_name
    return tariff_name in invoice_name or invoice_name in tariff_name


_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _user_lookup_id(source: Optional[Dict[str, Any]], *keys: str) -> Optional[str]:
    """Return the first explicit Dynamics id a review screen already chose."""
    if not isinstance(source, dict):
        return None
    for key in keys:
        value = str(source.get(key) or "").strip()
        if value and value.lower() not in {"none", "null"} and _GUID_RE.match(value):
            return value
    return None


def _exchange_rate_for(currency_id: Optional[str], currencies: List[Dict[str, Any]], fallback: float = 1.0) -> float:
    if not currency_id:
        return fallback
    for row in currencies:
        if str(row.get("transactioncurrencyid") or "") == str(currency_id):
            return float(row.get("exchangerate") or fallback or 1.0)
    return fallback


@router.post("/extract/invoice", response_model=InvoiceExtractResponse, tags=["Invoice Extraction"])
async def extract_invoice(
    file: Optional[UploadFile] = File(None, description="PDF or Image invoice file"),
    operation_id: Optional[str] = Form(None, description="Dynamics operation ID to post cost lines under"),
    current_bl: Optional[str] = Form(None, description="Current operation BL number to match against"),
    post_to_dataverse: bool = Form(False, description="Whether to post extracted items to Dynamics Dataverse"),
    reviewed_json: Optional[str] = Form(
        None,
        description=(
            "Extraction payload returned by an earlier dry run. When supplied the AI step is "
            "skipped and exactly this payload is posted, so the user posts what they reviewed."
        ),
    ),
    llm_provider: Optional[LlmProviderQuery] = Form(
        LlmProviderQuery.gemini,
        description="AI backend: Gemini API",
    ),
    llm_model: Optional[GeminiModelQuery] = Form(
        None,
        description="Gemini model id when llm_provider=gemini",
    ),
):
    provider_val = llm_provider.value if llm_provider else None
    model_val = llm_model.value if llm_model else None
    try:
        from app.infrastructure.ai.ai_extractor import extract_invoice_with_llm, normalize_invoice_result

        if operation_id and operation_id.strip() == "string":
            operation_id = None
        if current_bl and current_bl.strip() == "string":
            current_bl = None

        file_bytes = None
        filename = "browser_extracted.pdf"
        if file:
            file_bytes = await file.read()
            filename = file.filename

            # Always read the source document. Browser/third-party JSON still
            # needs deterministic label validation (Ref vs Invoice, Client vs
            # Consignee) and operation-scoped table filtering.
            extracted = extract_document_text_professionally(file_bytes, filename)
            raw_text = extracted.get("text", "")
            if not raw_text.strip():
                if str(filename or "").lower().endswith(".pdf"):
                    raw_text = "[PDF text layer unavailable; inspect the attached PDF directly.]"
                else:
                    return InvoiceExtractResponse(success=False, error="No text extracted from file.")
        else:
            return InvoiceExtractResponse(success=False, error="No invoice file provided.")
        
        try:
            reviewed_data = _parse_reviewed_payload(reviewed_json)
        except ValueError as reviewed_exc:
            return InvoiceExtractResponse(success=False, error=str(reviewed_exc))

        if reviewed_data is not None:
            extracted_data = reviewed_data
        else:
            with llm_request_overrides(provider_val, model_val):
                extracted_data = extract_invoice_with_llm(raw_text, file_bytes=file_bytes, filename=filename)

            # Normalize the server-side Gemini response before lookup mapping.
            extracted_data = normalize_invoice_result(
                extracted_data,
                raw_text,
                filename=filename,
                file_bytes=file_bytes,
            )
        
        extracted_mbl = extracted_data.get("master_bl_number") or ""
        extracted_hbl = extracted_data.get("house_bl_number") or ""
        
        # Initialize variables for lookup
        resolved_op_id = operation_id
        resolved_op_code = None
        resolved_op_bl = current_bl
        op_bl_number = current_bl
        is_master = True
        tariff_quote_id = None
        
        from app.infrastructure.dataverse.client_service import DataverseClientService
        client = None
        client_init_error: Optional[str] = None
        try:
            client = DataverseClientService.get_instance()
        except Exception as client_exc:
            client_init_error = f"{type(client_exc).__name__}: {client_exc}"
            logger.warning("Dataverse client is unavailable: %s", client_init_error)

        single_mapping: Optional[Dict[str, Any]] = None
        single_references: Dict[str, List[Dict[str, Any]]] = {}
        single_reference_errors: List[str] = []
        if client:
            single_references, single_reference_errors = fetch_single_invoice_reference_data(client)
            single_mapping = build_single_invoice_lookup_plan(
                extracted_data,
                single_references,
                single_reference_errors,
            )
            extracted_data["dynamics_mapping"] = single_mapping
        elif client_init_error:
            extracted_data["dynamics_mapping"] = {
                "ready_to_post": False,
                "errors": [client_init_error],
                "lookups": {},
            }

        # If no operation_id is provided, resolve by B/L in a deterministic order.
        # Prefer House B/L over Master B/L so multi-HBL debit notes do not post
        # every invoice row to the master operation.
        if not resolved_op_id and client and (current_bl or extracted_hbl or extracted_mbl):
            def find_operation_by_bl(bl_value: str, bl_type: Optional[int] = None) -> Optional[Dict[str, Any]]:
                safe_bl = str(bl_value or "").replace("'", "''").strip()
                if not safe_bl:
                    return None
                conditions = [
                    f"(mesco_masterblno eq '{safe_bl}' or mesco_code eq '{safe_bl}')",
                ]
                if bl_type is not None:
                    conditions.append(f"mesco_bltype eq {bl_type}")
                filter_str = " and ".join(conditions)
                url = (
                    "mesco_operations?"
                    "$select=mesco_operationid,mesco_code,mesco_bltype,mesco_masterblno,mesco_xollsp_TariffQuote"
                    f"&$filter={filter_str}&$top=1"
                )
                resp = client.get(url)
                if resp.status_code != 200:
                    return None
                rows = resp.json().get("value", [])
                return rows[0] if rows else None

            try:
                candidates: List[tuple[str, Optional[int]]] = []
                if current_bl:
                    candidates.append((current_bl, None))
                if extracted_hbl:
                    candidates.append((extracted_hbl, 886150002))
                    candidates.append((extracted_hbl, None))
                if extracted_mbl:
                    candidates.append((extracted_mbl, 886150001))
                    candidates.append((extracted_mbl, None))

                for bl_value, bl_type in candidates:
                    op = find_operation_by_bl(bl_value, bl_type)
                    if not op:
                        continue
                    resolved_op_id = op["mesco_operationid"]
                    resolved_op_code = op.get("mesco_code")
                    resolved_op_bl = op.get("mesco_masterblno")
                    is_master = op.get("mesco_bltype") == 886150001
                    op_bl_number = resolved_op_bl
                    if op.get("_mesco_xollsp_tariffquote_value"):
                        tariff_quote_id = op["_mesco_xollsp_tariffquote_value"]
                    break
            except Exception as search_err:
                logger.warning("Failed to lookup operation in Dataverse: %s", search_err)

        # Invoice posting never creates Master/House operations. If the B/L is
        # not already in Dynamics, skip posting rather than inventing a record.

        # Fetch operation details if not resolved yet (e.g. if operation_id was passed explicitly)
        if resolved_op_id and client and not resolved_op_code:
            try:
                op_url = f"mesco_operations({resolved_op_id})?$select=mesco_code,mesco_bltype,mesco_masterblno,mesco_xollsp_TariffQuote"
                op_resp = client.get(op_url)
                if op_resp.status_code == 200:
                    op_data = op_resp.json()
                    resolved_op_code = op_data.get("mesco_code")
                    resolved_op_bl = op_data.get("mesco_masterblno")
                    op_bl_number = resolved_op_bl
                    is_master = op_data.get("mesco_bltype") == 886150001
                    if op_data.get("_mesco_xollsp_tariffquote_value"):
                        tariff_quote_id = op_data["_mesco_xollsp_tariffquote_value"]
            except Exception as metadata_err:
                logger.warning("Failed to fetch resolved operation details: %s", metadata_err)

        if resolved_op_bl:
            extracted_data = _apply_operation_invoice_scope(
                extracted_data,
                raw_text,
                resolved_op_bl,
            )
            if client:
                single_mapping = build_single_invoice_lookup_plan(
                    extracted_data,
                    single_references,
                    single_reference_errors,
                )
                extracted_data["dynamics_mapping"] = single_mapping

        # Build direct web URL to the Operation record in Dynamics
        dynamics_url = None
        if resolved_op_id:
            dynamics_url = f"{settings.base_url}/main.aspx?pagetype=entityrecord&etn=mesco_operation&id={resolved_op_id}"

        # Compare BL number
        is_matched = None
        if op_bl_number:
            clean_curr = re.sub(r"[^a-z0-9]", "", op_bl_number.lower())
            clean_mbl = re.sub(r"[^a-z0-9]", "", extracted_mbl.lower()) if extracted_mbl else ""
            clean_hbl = re.sub(r"[^a-z0-9]", "", extracted_hbl.lower()) if extracted_hbl else ""
            
            is_matched = bool(
                (clean_mbl and (clean_mbl in clean_curr or clean_curr in clean_mbl)) or
                (clean_hbl and (clean_hbl in clean_curr or clean_curr in clean_hbl))
            )
        
        dataverse_results = []
        dataverse_error = None

        if post_to_dataverse and not resolved_op_id:
            if client_init_error:
                dataverse_error = f"Dynamics client is unavailable: {client_init_error}"
            else:
                dataverse_error = _invoice_missing_bl_error(
                    extracted_hbl or extracted_mbl or current_bl
                )
        
        if post_to_dataverse and resolved_op_id and client:
            try:
                lookup_plan = (single_mapping or {}).get("lookups") or {}
                line_items = extracted_data.get("line_items") or []
                user_vendor_id = _user_lookup_id(extracted_data, "vendor_id")
                user_currency_id = _user_lookup_id(extracted_data, "currency_id")
                user_container_id = _user_lookup_id(extracted_data, "container_id")
                user_ready = bool(
                    user_vendor_id
                    and user_currency_id
                    and line_items
                    and all(_user_lookup_id(item, "service_id") for item in line_items)
                )
                if not user_ready and (not single_mapping or not single_mapping.get("ready_to_post")):
                    mapping_errors = (single_mapping or {}).get("errors") or [
                        "Dynamics lookup mapping is unavailable"
                    ]
                    raise ValueError(
                        "Invoice was not posted because required Dynamics lookups "
                        f"did not resolve uniquely: {'; '.join(mapping_errors)}"
                    )

                currency_resolution = lookup_plan.get("currency") or {}
                vendor_resolution = lookup_plan.get("invoice_vendor") or {}
                currency_id = user_currency_id or currency_resolution.get("id")
                vendor_id = user_vendor_id or vendor_resolution.get("id")
                ex_rate = _exchange_rate_for(
                    currency_id,
                    single_references.get("currencies") or [],
                    float(currency_resolution.get("exchange_rate") or 1.0),
                )

                # Bind a chosen operation container, or create one from the
                # reviewed container number when the operation did not exist
                # at extract time.
                if user_container_id:
                    invoice_container = {"id": user_container_id, "reused": True}
                elif str(extracted_data.get("container_number") or "").strip():
                    invoice_container = _ensure_invoice_container(
                        client,
                        resolved_op_id,
                        extracted_data,
                    )
                else:
                    invoice_container = None
                if invoice_container:
                    dataverse_results.append({
                        "type": "container",
                        "success": True,
                        **invoice_container,
                    })

                for item in extracted_data.get("line_items", []):
                    desc = item.get("service_description") or "Invoice Charge"
                    service_resolution = (lookup_plan.get("services") or {}).get(desc) or {}
                    matched_srv = _user_lookup_id(item, "service_id") or service_resolution.get("id")
                    item_vendor_id = _user_lookup_id(item, "vendor_id") or vendor_id
                    item_currency_id = _user_lookup_id(item, "currency_id") or currency_id
                    item_container_id = _user_lookup_id(item, "container_id") or (
                        invoice_container or {}
                    ).get("id")
                    item_ex_rate = _exchange_rate_for(
                        item_currency_id,
                        single_references.get("currencies") or [],
                        ex_rate,
                    )
                    if not matched_srv or not item_vendor_id or not item_currency_id:
                        raise ValueError(
                            f"Charge '{desc}' is missing a Dynamics service, vendor, or currency."
                        )
                    
                    cost_values = _invoice_cost_values(item)
                    qty = cost_values["quantity"]
                    u_price = cost_values["unit_price"]
                    
                    payload = {
                        "xollsp_name": desc,
                        "xollsp_quantity": qty,
                        "xollsp_unitamount": u_price,
                        "xollsp_unitamountbase": u_price / item_ex_rate if item_ex_rate else u_price,
                        "xollsp_fixedamount": 0,
                        "xollsp_fixedamountbase": 0,
                        "mesco_servicecategory": 886150006, # Others
                        "mesco_vendorinvoicenumber": extracted_data.get("vendor_invoice_number"),
                        "xollsp_exchangerate": item_ex_rate,
                    }
                    if extracted_data.get("shipment_ref"):
                        payload["mesco_bookingnumber"] = extracted_data["shipment_ref"]
                    if extracted_data.get("container_number"):
                        payload["mesco_containernumber"] = extracted_data["container_number"]

                    audit_comments = [cost_values["comments"]] if cost_values["comments"] else []
                    if item.get("comments"):
                        audit_comments.insert(0, str(item["comments"]).strip())
                    if extracted_data.get("payment_request_reference"):
                        audit_comments.append(
                            f"Payment Request Ref: {extracted_data['payment_request_reference']}."
                        )
                    if extracted_data.get("payment_request_date"):
                        audit_comments.append(
                            f"Payment Request Date: {extracted_data['payment_request_date']}."
                        )
                    if extracted_data.get("client_name"):
                        audit_comments.append(f"Client: {extracted_data['client_name']}.")
                    if extracted_data.get("withholding_tax_amount") is not None:
                        withholding_amount = float(extracted_data["withholding_tax_amount"])
                        audit_comments.append(
                            "Withholding tax: "
                            f"{withholding_amount:g} "
                            f"{extracted_data.get('currency') or ''}.".rstrip()
                        )
                    if audit_comments:
                        payload["xollsp_comments"] = " ".join(audit_comments)
                    
                    # Bind lookups
                    payload["xollsp_LogisticService@odata.bind"] = f"/xollsp_servicedefinitions({matched_srv})"
                    payload["transactioncurrencyid@odata.bind"] = f"/transactioncurrencies({item_currency_id})"
                    payload["xollsp_Currency@odata.bind"] = f"/transactioncurrencies({item_currency_id})"
                    payload["mesco_invoicevendor_shippingline@odata.bind"] = f"/mesco_shippinglines({item_vendor_id})"
                    if tariff_quote_id:
                        payload["xollsp_TariffQuote@odata.bind"] = f"/xollsp_tariffquotes({tariff_quote_id})"
                    if item_container_id:
                        payload["mesco_Container@odata.bind"] = f"/mesco_containers({item_container_id})"
                    
                    if is_master:
                        payload["mesco_Master3@odata.bind"] = f"/mesco_operations({resolved_op_id})"
                    else:
                        payload["mesco_Operation@odata.bind"] = f"/mesco_operations({resolved_op_id})"
                    
                    existing_cost_line_id = _find_existing_invoice_cost_line(
                        client,
                        resolved_op_id,
                        is_master,
                        extracted_data.get("vendor_invoice_number"),
                        item,
                    )
                    if existing_cost_line_id:
                        update_payload = dict(payload)
                        patch_resp = client.patch(
                            f"xollsp_quotecostlines({existing_cost_line_id})",
                            json=update_payload,
                        )
                        dataverse_results.append({
                            "success": True,
                            "status_code": patch_resp.status_code,
                            "action": "updated",
                            "id": existing_cost_line_id,
                        })
                    else:
                        post_resp = client.post("xollsp_quotecostlines", json=payload)
                        if post_resp.status_code in (200, 201):
                            created_body = post_resp.json()
                            created_body.setdefault("success", True)
                            created_body.setdefault("action", "created")
                            dataverse_results.append(created_body)
                        elif post_resp.status_code == 204:
                            dataverse_results.append({
                                "success": True,
                                "status_code": post_resp.status_code,
                                "action": "created",
                                "entity_url": post_resp.headers.get("OData-EntityId"),
                            })
                        else:
                            dataverse_results.append({
                                "success": False,
                                "status_code": post_resp.status_code,
                                "error": post_resp.text,
                            })
            except Exception as e:
                response = getattr(e, "response", None)
                response_text = getattr(response, "text", None)
                dataverse_error = f"{e}: {response_text}" if response_text else str(e)
                logger.exception("Failed to post cost lines to Dynamics")
        
        # Include resolved_op_id in response metadata if needed
        if resolved_op_id and not extracted_data.get("operation_id"):
            extracted_data["resolved_operation_id"] = resolved_op_id
        
        return InvoiceExtractResponse(
            success=True,
            data=extracted_data,
            is_bl_matched=is_matched,
            dataverse_results=dataverse_results,
            dataverse_error=dataverse_error,
            resolved_operation_id=resolved_op_id,
            resolved_operation_code=resolved_op_code,
            resolved_operation_bl=resolved_op_bl,
            dynamics_url=dynamics_url,
        )
    except Exception as exc:
        logger.exception("Invoice extraction endpoint failed")
        return InvoiceExtractResponse(success=False, error=str(exc))
@router.post("/extract/invoice/multi", response_model=MultiInvoiceExtractResponse, tags=["Invoice Extraction"])
async def extract_invoice_multi(
    file: Optional[UploadFile] = File(None, description="PDF, image, XLS, or XLSX invoice/debit note file"),
    operation_id: Optional[str] = Form(None, description="Fallback Dynamics operation ID if HBL lookup fails"),
    post_to_dataverse: bool = Form(False, description="Whether to post extracted items to Dynamics Dataverse"),
    reviewed_json: Optional[str] = Form(
        None,
        description=(
            "extracted_payload returned by an earlier dry run. When supplied the AI step is "
            "skipped and exactly this payload is posted, so the user posts what they reviewed."
        ),
    ),
    tariff_vendor_id: Optional[str] = Form(None, description="Selected Dataverse vendor account for tariff selection"),
    tariff_scheme_id: Optional[str] = Form(None, description="Selected current Dataverse purchase tariff scheme"),
    llm_provider: Optional[LlmProviderQuery] = Form(
        LlmProviderQuery.gemini,
        description="AI backend: Gemini API",
    ),
    llm_model: Optional[GeminiModelQuery] = Form(
        None,
        description="Gemini model id when llm_provider=gemini",
    ),
):
    """Extract a multi-HBL invoice/debit note and post cost lines per HBL group."""
    provider_val = llm_provider.value if llm_provider else None
    model_val = llm_model.value if llm_model else None
    try:
        tariff_selection = _validate_tariff_selection(tariff_vendor_id, tariff_scheme_id)
    except (ValueError, HTTPException) as tariff_exc:
        detail = tariff_exc.detail if isinstance(tariff_exc, HTTPException) else str(tariff_exc)
        return MultiInvoiceExtractResponse(success=False, error=str(detail))
    try:
        from app.infrastructure.ai.ai_extractor import extract_multi_invoice_with_llm

        if operation_id and operation_id.strip() == "string":
            operation_id = None

        file_bytes = None
        filename = "uploaded_invoice.pdf"
        extracted_data = None
        is_excel_invoice = False
        if file:
            file_bytes = await file.read()
            filename = file.filename

            is_excel_invoice = str(filename or "").lower().endswith((".xls", ".xlsx"))
            if is_excel_invoice:
                extracted_data = _extract_wecan_excel_invoice(file_bytes, filename)
                raw_text = ""
            else:
                extracted = extract_document_text_professionally(file_bytes, filename)
                raw_text = extracted.get("text", "")
                if not raw_text.strip():
                    if str(filename or "").lower().endswith(".pdf"):
                        raw_text = "[PDF text layer unavailable; inspect the attached PDF directly.]"
                    else:
                        return MultiInvoiceExtractResponse(success=False, error="No text extracted from file.")
        else:
            return MultiInvoiceExtractResponse(success=False, error="No invoice file provided.")

        try:
            reviewed_data = _parse_reviewed_payload(reviewed_json)
        except ValueError as reviewed_exc:
            return MultiInvoiceExtractResponse(success=False, error=str(reviewed_exc))
        if reviewed_data is not None:
            extracted_data = reviewed_data

        if extracted_data is None:
            with llm_request_overrides(provider_val, model_val):
                extracted_data = extract_multi_invoice_with_llm(raw_text, file_bytes=file_bytes, filename=filename)

        vendor_name = extracted_data.get("vendor_name")
        vendor_invoice_number = extracted_data.get("vendor_invoice_number")
        master_bl_number = extracted_data.get("master_bl_number") or ""
        container_number = extracted_data.get("container_number")
        seal_number = extracted_data.get("seal_number")
        currency = extracted_data.get("currency")
        groups_raw = extracted_data.get("groups") or []
        deterministic_groups = _invoice_groups_from_table(raw_text)
        if deterministic_groups and not groups_raw:
            groups_raw = deterministic_groups
            extracted_data["groups"] = deterministic_groups
            extracted_data["invoice_scope"] = {
                "source": "deterministic_hbl_table_groups",
                "groups_count": len(deterministic_groups),
                "line_items_count": sum(len(g.get("line_items") or []) for g in deterministic_groups),
            }

        total_line_items = sum(len(g.get("line_items", [])) for g in groups_raw)

        # Initialize Dataverse client
        from app.infrastructure.dataverse.client_service import DataverseClientService
        client = None
        client_init_error: Optional[str] = None
        try:
            client = DataverseClientService.get_instance()
        except Exception as client_exc:
            client_init_error = f"{type(client_exc).__name__}: {client_exc}"
            logger.warning("Dataverse client is unavailable: %s", client_init_error)

        # Excel invoice posting uses a strict, schema-backed mapping plan.
        # The plan is also returned during dry runs so the caller can verify
        # every scalar field and lookup before any Dynamics record is changed.
        mapping_validation: Optional[Dict[str, Any]] = None
        if is_excel_invoice:
            reference_data: Dict[str, List[Dict[str, Any]]] = {}
            reference_errors: List[str] = []
            if client:
                reference_data, reference_errors = fetch_invoice_reference_data(client)
            else:
                reference_errors.append(
                    "Dynamics client is unavailable; lookups could not be validated"
                    + (f" ({client_init_error})" if client_init_error else "")
                )
            mapping_validation = build_invoice_mapping_plan(
                extracted_data,
                reference_data,
                reference_errors,
            )
            extracted_data["mapping_validation"] = mapping_validation

        strict_posting_ready = bool(
            not is_excel_invoice
            or (mapping_validation and mapping_validation.get("ready_to_post"))
        )

        # Resolve fallback operation from MBL or provided operation_id
        fallback_op_id = operation_id
        fallback_op_code = None
        fallback_is_master = True
        fallback_tariff_quote_id = None
        fallback_origin_id = None
        master_post_error: Optional[str] = None

        if client and (fallback_op_id or master_bl_number):
            if fallback_op_id and not fallback_op_code:
                try:
                    op_url = f"mesco_operations({fallback_op_id})?$select=mesco_code,mesco_bltype,mesco_masterblno,mesco_xollsp_TariffQuote,_mesco_origin_value"
                    op_resp = client.get(op_url)
                    if op_resp.status_code == 200:
                        op_data = op_resp.json()
                        fallback_op_code = op_data.get("mesco_code")
                        fallback_is_master = op_data.get("mesco_bltype") == 886150001
                        if op_data.get("_mesco_xollsp_tariffquote_value"):
                            fallback_tariff_quote_id = op_data["_mesco_xollsp_tariffquote_value"]
                        fallback_origin_id = op_data.get("_mesco_origin_value")
                except Exception:
                    pass

            # An Excel consolidation always needs the actual master as its
            # parent, even if the UI supplied a currently-open House ID.
            if is_excel_invoice and fallback_op_id and not fallback_is_master and master_bl_number:
                fallback_op_id = None
                fallback_op_code = None
                fallback_is_master = True
                fallback_tariff_quote_id = None
                fallback_origin_id = None

            if not fallback_op_id and master_bl_number:
                try:
                    search_url = (
                        f"mesco_operations?$select=mesco_operationid,mesco_code,mesco_bltype,mesco_masterblno,mesco_xollsp_TariffQuote,_mesco_origin_value"
                        f"&$filter=mesco_masterblno eq '{master_bl_number}' "
                        "and mesco_bltype eq 886150001&$top=1"
                    )
                    search_resp = client.get(search_url)
                    if search_resp.status_code == 200:
                        results = search_resp.json().get("value", [])
                        if results:
                            op = results[0]
                            fallback_op_id = op["mesco_operationid"]
                            fallback_op_code = op.get("mesco_code")
                            fallback_is_master = op.get("mesco_bltype") == 886150001
                            if op.get("_mesco_xollsp_tariffquote_value"):
                                fallback_tariff_quote_id = op["_mesco_xollsp_tariffquote_value"]
                            fallback_origin_id = op.get("_mesco_origin_value")
                except Exception as e:
                    logger.warning("Failed to lookup fallback operation by MBL: %s", e)

        # Invoice posting never creates a Master operation. Existing masters
        # are still refreshed below; missing masters are reported and skipped.
        if (
            not fallback_op_id
            and post_to_dataverse
            and strict_posting_ready
            and client
            and master_bl_number
        ):
            master_post_error = _invoice_missing_bl_error(
                master_bl_number, kind="Master B/L"
            )
            logger.info(
                "Skipping Master Operation creation for missing B/L %s",
                master_bl_number,
            )

        # Existing master records retain their operational code (for example
        # O-10212); all invoice-derived fields and lookup binds are refreshed.
        if (
            fallback_op_id
            and fallback_is_master
            and post_to_dataverse
            and strict_posting_ready
            and client
            and is_excel_invoice
            and mapping_validation
        ):
            try:
                master_patch = dict(mapping_validation["master_operation"]["fields"])
                master_patch.pop("mesco_code", None)
                master_patch.pop("mesco_bltype", None)
                _update_entity(client, "mesco_operations", fallback_op_id, master_patch)
            except Exception as patch_err:
                master_post_error = f"Master Operation mapping failed: {patch_err}"
                strict_posting_ready = False
                logger.exception("Failed to update mapped Master Operation fields: %s", patch_err)

        # Pre-fetch reference lists for Dataverse posting
        services_list = []
        currencies_list = []
        vendors_list = []
        currency_id = None
        vendor_id = None
        ex_rate = 1.0

        if post_to_dataverse and client:
            try:
                services_resp = client.get("xollsp_servicedefinitions?$select=xollsp_servicedefinitionid,xollsp_name")
                services_list = services_resp.json().get("value", [])

                currencies_resp = client.get("transactioncurrencies?$select=transactioncurrencyid,currencyname,isocurrencycode,exchangerate")
                currencies_list = currencies_resp.json().get("value", [])

                vendors_resp = client.get("mesco_shippinglines?$select=mesco_shippinglineid,mesco_name")
                vendors_list = vendors_resp.json().get("value", [])
            except Exception as e:
                logger.warning("Failed to pre-fetch reference lists: %s", e)

            def fuzzy_match(q, options, key_id, key_name, second_key=None, fallback_first=False):
                if not q:
                    return None
                q_clean = re.sub(r"[^a-z0-9]", "", q.lower())
                if not q_clean:
                    return None
                for opt in options:
                    lbl = opt.get(key_name) or ""
                    lbl_clean = re.sub(r"[^a-z0-9]", "", lbl.lower())
                    if lbl_clean and lbl_clean == q_clean:
                        return opt[key_id]
                    if second_key and opt.get(second_key):
                        scnd_clean = re.sub(r"[^a-z0-9]", "", opt[second_key].lower())
                        if scnd_clean and scnd_clean == q_clean:
                            return opt[key_id]
                for opt in options:
                    lbl = opt.get(key_name) or ""
                    lbl_clean = re.sub(r"[^a-z0-9]", "", lbl.lower())
                    if lbl_clean and (q_clean in lbl_clean or lbl_clean in q_clean):
                        return opt[key_id]
                return options[0][key_id] if fallback_first and options else None

            currency_id = fuzzy_match(currency, currencies_list, "transactioncurrencyid", "isocurrencycode", "currencyname")
            if currency_id:
                for cur in currencies_list:
                    if cur.get("transactioncurrencyid") == currency_id:
                        ex_rate = float(cur.get("exchangerate") or 1.0)
                        break

            vendor_id = fuzzy_match(vendor_name, vendors_list, "mesco_shippinglineid", "mesco_name")

            if is_excel_invoice and mapping_validation:
                currency_resolution = mapping_validation["lookups"]["currency"]
                vendor_resolution = mapping_validation["lookups"]["invoice_vendor"]
                currency_id = currency_resolution.get("id")
                vendor_id = vendor_resolution.get("id")
                ex_rate = float(currency_resolution.get("exchange_rate") or 1.0)

            currency_id = _user_lookup_id(extracted_data, "currency_id") or currency_id
            vendor_id = _user_lookup_id(extracted_data, "vendor_id") or vendor_id
            if currency_id:
                ex_rate = _exchange_rate_for(currency_id, currencies_list, ex_rate)

        # One physical container belongs to the master operation.  Reusing it
        # for every HBL cost line avoids the previous duplicate-container bug.
        shared_invoice_container: Optional[Dict[str, Any]] = None
        container_post_error: Optional[str] = None
        if (
            is_excel_invoice
            and post_to_dataverse
            and strict_posting_ready
            and client
            and fallback_op_id
            and fallback_is_master
        ):
            try:
                container_source = dict(extracted_data)
                container_source["total_packages"] = sum(
                    float(group.get("packages") or 0) for group in groups_raw
                )
                shared_invoice_container = _ensure_invoice_container(
                    client,
                    fallback_op_id,
                    container_source,
                )
                if not shared_invoice_container:
                    container_post_error = "Container number could not be resolved or created"
            except Exception as container_exc:
                container_post_error = f"Container creation failed: {container_exc}"

        # Process each HBL group
        group_results: List[MultiInvoiceGroupResult] = []
        total_posted = 0
        dataverse_error = master_post_error
        if post_to_dataverse and is_excel_invoice and not strict_posting_ready:
            if not dataverse_error:
                dataverse_error = "Posting blocked because one or more Dynamics mappings are unresolved: " + "; ".join(
                    (mapping_validation or {}).get("errors") or ["unknown mapping error"]
                )

        for group in groups_raw:
            hbl = group.get("house_bl_number")
            group_vendor_inv = group.get("vendor_invoice_number") or vendor_invoice_number
            group_curr = group.get("currency") or currency
            line_items = group.get("line_items") or []
            gr = MultiInvoiceGroupResult(
                house_bl_number=hbl,
                vendor_invoice_number=group_vendor_inv,
                invoice_date=group.get("invoice_date"),
                currency=group_curr,
                subtotal_amount=group.get("subtotal_amount"),
                tax_amount=group.get("tax_amount"),
                total_amount=group.get("total_amount"),
                cbm=group.get("cbm"),
                kgs=group.get("kgs"),
                packages=group.get("packages"),
                charged_wm=group.get("charged_wm"),
                term=group.get("term"),
                destination=group.get("destination"),
                source_row=group.get("source_row"),
                debit_total=group.get("debit_total"),
                credit_total=group.get("credit_total"),
                local_agreement_total=group.get("local_agreement_total"),
                local_agreement_items=group.get("local_agreement_items") or [],
                shipment_ref=group.get("shipment_ref"),
                container_number=group.get("container_number") or container_number,
                container_type=group.get("container_type"),
                seal_number=group.get("seal_number") or seal_number,
                line_items_count=len(line_items),
                line_items=line_items,
            )
            group_mapping = mapping_group(mapping_validation, hbl) if mapping_validation else None
            gr.mapping_validation = group_mapping
            if container_post_error:
                gr.errors.append(container_post_error)
            if post_to_dataverse and is_excel_invoice and not strict_posting_ready:
                gr.errors.extend((group_mapping or {}).get("errors") or [])

            # Resolve operation for this HBL
            group_op_id = None
            group_op_code = None
            group_is_master = True
            group_tariff_quote_id = None
            group_operation_ready = True

            if client and hbl:
                try:
                    safe_hbl = str(hbl).replace("'", "''")
                    hbl_filter = (
                        f"mesco_masterblno eq '{safe_hbl}' "
                        "and mesco_bltype eq 886150002"
                    )
                    hbl_url = (
                        f"mesco_operations?$select=mesco_operationid,mesco_code,mesco_bltype,mesco_masterblno,mesco_xollsp_TariffQuote"
                        f"&$filter={hbl_filter}&$top=1"
                    )
                    hbl_resp = client.get(hbl_url)
                    if hbl_resp.status_code == 200:
                        hbl_results = hbl_resp.json().get("value", [])
                        if hbl_results:
                            hop = hbl_results[0]
                            group_op_id = hop["mesco_operationid"]
                            group_op_code = hop.get("mesco_code")
                            group_is_master = hop.get("mesco_bltype") == 886150001
                            if hop.get("_mesco_xollsp_tariffquote_value"):
                                group_tariff_quote_id = hop["_mesco_xollsp_tariffquote_value"]
                            
                            # Patch every mapped operation field, including
                            # destination/incoterm/carrier/vessel/currency.
                            if post_to_dataverse and strict_posting_ready and not group_is_master:
                                if is_excel_invoice and group_mapping:
                                    patch_fields = dict(group_mapping["fields"])
                                    patch_fields.pop("mesco_code", None)
                                    patch_fields.pop("mesco_bltype", None)
                                else:
                                    patch_fields = {}
                                    if group.get("cbm") is not None:
                                        patch_fields["cr401_totalvolume"] = float(group["cbm"])
                                    if group.get("kgs") is not None:
                                        patch_fields["cr401_totalgrossweight"] = float(group["kgs"])
                                    if group.get("packages") is not None:
                                        patch_fields["cr401_totalpackages"] = float(group["packages"])
                                if patch_fields:
                                    try:
                                        _update_entity(client, "mesco_operations", group_op_id, patch_fields)
                                    except Exception as patch_e:
                                        group_operation_ready = False
                                        gr.errors.append(f"Operation mapping failed: {patch_e}")
                                        logger.warning("Failed to patch mapped fields for HBL %s: %s", hbl, patch_e)
                except Exception as e:
                    gr.errors.append(f"Operation lookup failed: {e}")
                    logger.warning("Failed to resolve operation for HBL %s: %s", hbl, e)

            # Never create a House operation from an invoice. Post only when
            # that HBL already exists; PDF invoices may still land on an
            # already-resolved master as a fallback. The fallback must not
            # depend on post_to_dataverse, or a dry run would report an
            # operation that the real post would then refuse to use.
            if not group_op_id and not is_excel_invoice:
                group_op_id = fallback_op_id
                group_op_code = fallback_op_code
                group_is_master = fallback_is_master
                group_tariff_quote_id = fallback_tariff_quote_id

            gr.resolved_operation_id = group_op_id
            gr.resolved_operation_code = group_op_code
            if group_op_id:
                gr.dynamics_url = f"{settings.base_url}/main.aspx?pagetype=entityrecord&etn=mesco_operation&id={group_op_id}"
            elif post_to_dataverse and strict_posting_ready:
                group_operation_ready = False
                gr.errors.append(_invoice_missing_bl_error(hbl, kind="House B/L"))

            # Post cost lines for this group
            if post_to_dataverse and strict_posting_ready and group_operation_ready and group_op_id and client:
                # Ensure container for this group
                invoice_container = shared_invoice_container
                group_container_id = _user_lookup_id(group, "container_id") or _user_lookup_id(
                    extracted_data, "container_id"
                )
                if group_container_id:
                    invoice_container = {"id": group_container_id, "reused": True}
                elif not is_excel_invoice and reviewed_data is None:
                    container_data = {
                        "container_number": group.get("container_number") or container_number,
                        "container_type": group.get("container_type") or extracted_data.get("container_type"),
                        "seal_number": group.get("seal_number") or seal_number,
                    }
                    try:
                        invoice_container = _ensure_invoice_container(client, group_op_id, container_data)
                    except Exception as e:
                        gr.errors.append(f"Container creation failed: {e}")

                if is_excel_invoice and not group_is_master and fallback_op_id:
                    try:
                        _ensure_invoice_house_cargo(
                            client,
                            house_operation_id=group_op_id,
                            master_operation_id=fallback_op_id,
                            container_id=(invoice_container or {}).get("id"),
                            group=group,
                            extracted_data=extracted_data,
                        )
                    except Exception as cargo_exc:
                        gr.errors.append(f"Cargo measurement mapping failed: {cargo_exc}")
                        continue

                for item in line_items:
                    desc = item.get("service_description") or "Invoice Charge"
                    item_curr_str = item.get("currency") or group_curr or currency
                    item_curr_id = fuzzy_match(item_curr_str, currencies_list, "transactioncurrencyid", "isocurrencycode", "currencyname") if currencies_list else currency_id
                    if is_excel_invoice and mapping_validation:
                        item_curr_id = mapping_validation["lookups"]["currency"].get("id")
                    item_curr_id = _user_lookup_id(item, "currency_id") or item_curr_id or currency_id
                    item_ex_rate = ex_rate
                    if item_curr_id and currencies_list:
                        for cur in currencies_list:
                            if cur.get("transactioncurrencyid") == item_curr_id:
                                item_ex_rate = float(cur.get("exchangerate") or 1.0)
                                break
                    try:
                        if is_excel_invoice and mapping_validation:
                            matched_srv = (
                                mapping_validation["lookups"]["services"]
                                .get(desc, {})
                                .get("id")
                            )
                        else:
                            matched_srv = fuzzy_match(desc, services_list, "xollsp_servicedefinitionid", "xollsp_name") if services_list else None
                        matched_srv = _user_lookup_id(item, "service_id") or matched_srv
                        item_vendor_id = _user_lookup_id(item, "vendor_id") or vendor_id
                        item_container_id = _user_lookup_id(item, "container_id") or (
                            invoice_container or {}
                        ).get("id")

                        cost_values = _invoice_cost_values(item)
                        qty = cost_values["quantity"]
                        u_price = cost_values["unit_price"]

                        payload = {
                            "xollsp_name": desc,
                            "xollsp_quantity": qty,
                            "xollsp_unitamount": u_price,
                            "xollsp_unitamountbase": u_price / item_ex_rate if item_ex_rate else u_price,
                            "xollsp_fixedamount": 0,
                            "xollsp_fixedamountbase": 0,
                            "mesco_servicecategory": 886150006,
                            "mesco_vendorinvoicenumber": group_vendor_inv,
                            "xollsp_transporttype": SEA,
                            "xollsp_loadtype": LCL,
                            "xollsp_importexport": IMPORT,
                        }
                        if cost_values["comments"] or item.get("comments"):
                            payload["xollsp_comments"] = " ".join(
                                part for part in (item.get("comments"), cost_values["comments"]) if part
                            )

                        if matched_srv:
                            payload["xollsp_LogisticService@odata.bind"] = f"/xollsp_servicedefinitions({matched_srv})"
                        if desc == "FOB OCEAN FREIGHT":
                            destination_id = (
                                ((group_mapping or {}).get("lookups") or {})
                                .get("destination", {})
                                .get("id")
                            )
                            if not fallback_origin_id or not destination_id:
                                gr.errors.append(
                                    "POST FOB OCEAN FREIGHT: Sea Freight requires mapped origin and destination"
                                )
                                continue
                            payload["xollsp_From@odata.bind"] = f"/xollsp_addresses({fallback_origin_id})"
                            payload["xollsp_To@odata.bind"] = f"/xollsp_addresses({destination_id})"
                        if item_curr_id:
                            payload["transactioncurrencyid@odata.bind"] = f"/transactioncurrencies({item_curr_id})"
                            payload["xollsp_Currency@odata.bind"] = f"/transactioncurrencies({item_curr_id})"
                        if item_vendor_id:
                            payload["mesco_invoicevendor_shippingline@odata.bind"] = f"/mesco_shippinglines({item_vendor_id})"
                        if group_tariff_quote_id:
                            payload["xollsp_TariffQuote@odata.bind"] = f"/xollsp_tariffquotes({group_tariff_quote_id})"
                        if item_container_id:
                            payload["mesco_Container@odata.bind"] = f"/mesco_containers({item_container_id})"

                        if group_is_master:
                            payload["mesco_Master3@odata.bind"] = f"/mesco_operations({group_op_id})"
                        else:
                            payload["mesco_Operation@odata.bind"] = f"/mesco_operations({group_op_id})"
                            if fallback_op_id:
                                payload["mesco_Master3@odata.bind"] = f"/mesco_operations({fallback_op_id})"

                        existing_cost_line_id = _find_existing_invoice_cost_line(
                            client,
                            group_op_id,
                            group_is_master,
                            group_vendor_inv,
                            item,
                        )
                        if existing_cost_line_id:
                            update_payload = dict(payload)
                            if not matched_srv:
                                update_payload["xollsp_LogisticService@odata.bind"] = None
                            client.patch(
                                f"xollsp_quotecostlines({existing_cost_line_id})",
                                json=update_payload,
                            )
                            gr.posted_count += 1
                            total_posted += 1
                        else:
                            post_resp = client.post("xollsp_quotecostlines", json=payload)
                            if post_resp.status_code not in (200, 201, 204):
                                gr.errors.append(f"POST {desc}: {post_resp.status_code} {post_resp.text[:200]}")
                                continue
                            gr.posted_count += 1
                            total_posted += 1
                    except Exception as e:
                        gr.errors.append(f"POST {desc}: {e}")

            group_results.append(gr)

        skipped_hbls = [
            str(gr.house_bl_number)
            for gr in group_results
            if gr.house_bl_number
            and any("was not found in Dynamics" in err for err in gr.errors)
        ]
        if skipped_hbls:
            skipped_msg = (
                "Skipped HBL(s) with no existing operation: "
                + ", ".join(skipped_hbls)
                + ". No operations were created."
            )
            dataverse_error = (
                f"{dataverse_error}; {skipped_msg}" if dataverse_error else skipped_msg
            )

        master_dyn_url = None
        if fallback_op_id:
            master_dyn_url = f"{settings.base_url}/main.aspx?pagetype=entityrecord&etn=mesco_operation&id={fallback_op_id}"

        return MultiInvoiceExtractResponse(
            success=True,
            vendor_name=vendor_name,
            tariff_vendor_id=tariff_selection["vendor"]["id"] if tariff_selection else None,
            tariff_vendor_name=tariff_selection["vendor"]["name"] if tariff_selection else None,
            tariff_scheme_id=tariff_selection["scheme"]["id"] if tariff_selection else None,
            tariff_scheme_name=tariff_selection["scheme"]["name"] if tariff_selection else None,
            tariff_vendor_matches_invoice=(
                _vendor_matches_invoice(tariff_selection["vendor"]["name"], vendor_name)
                if tariff_selection else None
            ),
            vendor_invoice_number=vendor_invoice_number,
            master_bl_number=master_bl_number,
            container_number=container_number,
            seal_number=seal_number,
            currency=currency,
            groups_count=len(group_results),
            total_line_items=total_line_items,
            total_posted=total_posted,
            master_operation_id=fallback_op_id,
            master_operation_code=fallback_op_code,
            master_dynamics_url=master_dyn_url,
            groups=group_results,
            extracted_payload=extracted_data,
            processing_summary=extracted_data.get("processing_summary"),
            extraction_validation=extracted_data.get("extraction_validation"),
            measurement_validation=extracted_data.get("measurement_validation"),
            mapping_validation=mapping_validation,
            dataverse_error=dataverse_error,
        )
    except Exception as exc:
        logger.exception("Multi-invoice extraction endpoint failed")
        return MultiInvoiceExtractResponse(success=False, error=str(exc))


@router.post(
    "/extract/invoice/excel",
    response_model=MultiInvoiceExtractResponse,
    tags=["Invoice Extraction"],
    summary="Extract and optionally post an Excel invoice",
)
async def extract_invoice_excel(
    file: UploadFile = File(..., description="Legacy XLS or XLSX invoice workbook"),
    operation_id: Optional[str] = Form(None, description="Fallback Dynamics operation ID if HBL lookup fails"),
    post_to_dataverse: bool = Form(False, description="Whether to post reconciled cost lines to Dynamics Dataverse"),
    reviewed_json: Optional[str] = Form(
        None,
        description=(
            "extracted_payload returned by an earlier dry run, posted verbatim so the user "
            "posts what they reviewed."
        ),
    ),
):
    """
    Process a WE-CAN proxy invoice workbook without OCR or an LLM.

    The workbook is parsed deterministically, reconciled to FINAL DN, grouped
    by HBL, and then passed through the same idempotent Dynamics posting path
    used by the multi-invoice endpoint.
    """
    filename = str(file.filename or "")
    if not filename.lower().endswith((".xls", ".xlsx")):
        raise HTTPException(
            status_code=400,
            detail="Excel invoice endpoint accepts only .xls or .xlsx files.",
        )

    return await extract_invoice_multi(
        file=file,
        operation_id=operation_id,
        post_to_dataverse=post_to_dataverse,
        reviewed_json=reviewed_json,
        llm_provider=LlmProviderQuery.gemini,
        llm_model=None,
    )
