"""SACO and Globelink invoice endpoints.

Callers choose the vendor. SACO storage claims and Globelink tax invoices
are parsed from the PDF text layer, then optionally posted through the
existing invoice cost-line flow.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.api.routers.invoices import _parse_reviewed_payload, extract_invoice
from app.api.schemas import (
    InvoiceExtractResponse,
    StorageInvoiceVendorListResponse,
    StorageInvoiceVendorOption,
)
from app.core.llm_models import LlmProviderQuery
from app.infrastructure.pdf.parsers.pdf_globelink_tax_invoice import (
    is_globelink_tax_invoice_pdf,
    parse_globelink_tax_invoice_pdf,
)
from app.infrastructure.pdf.parsers.pdf_vendor_storage_invoice import (
    GLOBELINK_VENDOR,
    SACO_VENDOR,
    VENDOR_LABELS,
    StorageInvoiceParseError,
    normalize_vendor_choice,
    parse_storage_invoice_pdf,
    vendor_endpoint,
)

router = APIRouter()


@dataclass(frozen=True)
class _StorageInvoiceRequest:
    operation_id: Optional[str]
    current_bl: Optional[str]
    post_to_dataverse: bool
    reviewed_json: Optional[str]


def _vendor_catalog() -> list[StorageInvoiceVendorOption]:
    return [
        StorageInvoiceVendorOption(
            code=code,
            label=VENDOR_LABELS[code],
            endpoint=vendor_endpoint(code),
        )
        for code in (SACO_VENDOR, GLOBELINK_VENDOR)
    ]


@router.get(
    "/extract/invoice/vendors",
    response_model=StorageInvoiceVendorListResponse,
    tags=["Invoice Extraction"],
    summary="List SACO and Globelink storage-invoice choices",
)
def list_storage_invoice_vendors() -> StorageInvoiceVendorListResponse:
    """Return the vendor pipelines a caller can choose."""
    return StorageInvoiceVendorListResponse(vendors=_vendor_catalog())


@router.post(
    "/extract/invoice/storage",
    response_model=InvoiceExtractResponse,
    tags=["Invoice Extraction"],
    summary="Extract a SACO storage invoice or a Globelink invoice",
)
async def extract_storage_invoice(
    vendor: str = Form(..., description="saco or globelink (globlink is accepted)"),
    file: UploadFile = File(..., description="SACO storage invoice or Globelink invoice PDF"),
    operation_id: Optional[str] = Form(None),
    current_bl: Optional[str] = Form(None),
    post_to_dataverse: bool = Form(False),
    reviewed_json: Optional[str] = Form(None),
) -> InvoiceExtractResponse:
    """Parse the invoice with the vendor pipeline the caller chose."""
    try:
        vendor_code = normalize_vendor_choice(vendor)
    except StorageInvoiceParseError as exc:
        return InvoiceExtractResponse(success=False, error=str(exc))
    return await _extract_chosen_vendor(
        vendor_code,
        file,
        _StorageInvoiceRequest(operation_id, current_bl, post_to_dataverse, reviewed_json),
    )


@router.post(
    "/extract/invoice/saco",
    response_model=InvoiceExtractResponse,
    tags=["Invoice Extraction"],
    summary="Extract a SACO storage invoice",
)
async def extract_saco_invoice(
    file: UploadFile = File(..., description="SACO storage invoice PDF, Arabic or English"),
    operation_id: Optional[str] = Form(None),
    current_bl: Optional[str] = Form(None),
    post_to_dataverse: bool = Form(False),
    reviewed_json: Optional[str] = Form(None),
) -> InvoiceExtractResponse:
    return await _extract_chosen_vendor(
        SACO_VENDOR,
        file,
        _StorageInvoiceRequest(operation_id, current_bl, post_to_dataverse, reviewed_json),
    )


@router.post(
    "/extract/invoice/globelink",
    response_model=InvoiceExtractResponse,
    tags=["Invoice Extraction"],
    summary="Extract a Globelink tax invoice or storage invoice",
)
async def extract_globelink_invoice(
    file: UploadFile = File(..., description="Globelink tax invoice or storage-claim PDF"),
    operation_id: Optional[str] = Form(None),
    current_bl: Optional[str] = Form(None),
    post_to_dataverse: bool = Form(False),
    reviewed_json: Optional[str] = Form(None),
) -> InvoiceExtractResponse:
    return await _extract_chosen_vendor(
        GLOBELINK_VENDOR,
        file,
        _StorageInvoiceRequest(operation_id, current_bl, post_to_dataverse, reviewed_json),
    )


async def _extract_chosen_vendor(
    vendor_code: str,
    upload: UploadFile,
    request: _StorageInvoiceRequest,
) -> InvoiceExtractResponse:
    _require_pdf(upload)
    try:
        payload = _invoice_payload(await upload.read(), vendor_code, request.reviewed_json)
    except (StorageInvoiceParseError, ValueError) as exc:
        return InvoiceExtractResponse(success=False, error=str(exc))
    if request.post_to_dataverse:
        return await _post_storage_invoice(upload, payload, request)
    return InvoiceExtractResponse(success=True, data=payload)


async def _post_storage_invoice(
    upload: UploadFile,
    payload: dict[str, Any],
    request: _StorageInvoiceRequest,
) -> InvoiceExtractResponse:
    await upload.seek(0)
    return await extract_invoice(
        file=upload,
        operation_id=_blank_form_value(request.operation_id),
        current_bl=_blank_form_value(request.current_bl) or payload.get("house_bl_number"),
        post_to_dataverse=True,
        reviewed_json=json.dumps(payload),
        llm_provider=LlmProviderQuery.gemini,
        llm_model=None,
    )


def _require_pdf(upload: UploadFile) -> None:
    filename = str(upload.filename or "")
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail="SACO and Globelink invoices must be PDF files.",
        )


def _invoice_payload(
    file_bytes: bytes,
    vendor_code: str,
    reviewed_json: Optional[str],
) -> dict[str, Any]:
    reviewed = _parse_reviewed_payload(reviewed_json)
    payload = reviewed if reviewed is not None else _parse_vendor_pdf(file_bytes)
    _require_chosen_vendor(payload, vendor_code)
    return payload


def _parse_vendor_pdf(file_bytes: bytes) -> dict[str, Any]:
    if is_globelink_tax_invoice_pdf(file_bytes):
        return parse_globelink_tax_invoice_pdf(file_bytes)
    return parse_storage_invoice_pdf(file_bytes)


def _require_chosen_vendor(payload: dict[str, Any], vendor_code: str) -> None:
    detected = str(payload.get("vendor_profile") or "")
    if detected == vendor_code:
        return
    detected_label = VENDOR_LABELS.get(detected, detected or "an unknown vendor")
    issuer = payload.get("vendor_name") or detected_label
    chosen_label = VENDOR_LABELS[vendor_code]
    hint = vendor_endpoint(detected) if detected in VENDOR_LABELS else "/extract/invoice/vendors"
    raise StorageInvoiceParseError(
        f"This invoice is issued by {issuer} ({detected_label}). "
        f"You chose {chosen_label}. Open {hint} for the matching pipeline."
    )


def _blank_form_value(value: Optional[str]) -> Optional[str]:
    text = str(value or "").strip()
    if text.lower() in {"", "string", "none", "null"}:
        return None
    return text
