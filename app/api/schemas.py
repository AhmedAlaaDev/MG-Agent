"""HTTP request and response models shared by the API routers."""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from app.core.llm_models import LlmProviderQuery


class BlTypeQuery(str, Enum):
    """Dynamics mesco_bltype: master (886150001) or house (886150002)."""

    master = "master"
    house = "house"


class CrmExtractRequest(BaseModel):
    crm_json: Dict[str, Any]


class ExtractRequest(BaseModel):
    ocr_text: str
    bl_type: BlTypeQuery = BlTypeQuery.master
    llm_provider: Optional[LlmProviderQuery] = LlmProviderQuery.gemini
    llm_model: Optional[str] = None


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


class BatchPdfTestResponse(BaseModel):
    total: int
    succeeded: int
    failed: int
    passed: int
    failed_validation: int
    average_score: float
    total_processing_ms: int
    results: List[Dict[str, Any]]


class DataverseUploadResponse(BaseModel):
    success: bool
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class CompareRequest(BaseModel):
    master_id: str
    sent_payload: Dict[str, Any]


class CompareResponse(BaseModel):
    success: bool
    master_id: str
    saved: Optional[Dict[str, Any]] = None
    comparison: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class InvoiceExtractResponse(BaseModel):
    success: bool
    data: Optional[Dict[str, Any]] = None
    is_bl_matched: Optional[bool] = None
    error: Optional[str] = None
    dataverse_results: Optional[List[Dict[str, Any]]] = None
    dataverse_error: Optional[str] = None
    resolved_operation_id: Optional[str] = None
    resolved_operation_code: Optional[str] = None
    resolved_operation_bl: Optional[str] = None
    dynamics_url: Optional[str] = None


class MultiInvoiceGroupResult(BaseModel):
    house_bl_number: Optional[str] = None
    vendor_invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None
    currency: Optional[str] = None
    subtotal_amount: Optional[float] = None
    tax_amount: Optional[float] = None
    total_amount: Optional[float] = None
    cbm: Optional[float] = None
    kgs: Optional[float] = None
    packages: Optional[float] = None
    charged_wm: Optional[float] = None
    term: Optional[str] = None
    destination: Optional[str] = None
    source_row: Optional[int] = None
    debit_total: Optional[float] = None
    credit_total: Optional[float] = None
    local_agreement_total: Optional[float] = None
    local_agreement_items: List[Dict[str, Any]] = []
    shipment_ref: Optional[str] = None
    container_number: Optional[str] = None
    container_type: Optional[str] = None
    seal_number: Optional[str] = None
    line_items_count: int = 0
    line_items: List[Dict[str, Any]] = []
    resolved_operation_id: Optional[str] = None
    resolved_operation_code: Optional[str] = None
    dynamics_url: Optional[str] = None
    posted_count: int = 0
    errors: List[str] = []
    mapping_validation: Optional[Dict[str, Any]] = None


class MultiInvoiceExtractResponse(BaseModel):
    success: bool
    vendor_name: Optional[str] = None
    tariff_vendor_id: Optional[str] = None
    tariff_vendor_name: Optional[str] = None
    tariff_scheme_id: Optional[str] = None
    tariff_scheme_name: Optional[str] = None
    tariff_vendor_matches_invoice: Optional[bool] = None
    vendor_invoice_number: Optional[str] = None
    master_bl_number: Optional[str] = None
    container_number: Optional[str] = None
    seal_number: Optional[str] = None
    currency: Optional[str] = None
    groups_count: int = 0
    total_line_items: int = 0
    total_posted: int = 0
    master_operation_id: Optional[str] = None
    master_operation_code: Optional[str] = None
    master_dynamics_url: Optional[str] = None
    groups: List[MultiInvoiceGroupResult] = []
    extracted_payload: Optional[Dict[str, Any]] = None
    processing_summary: Optional[Dict[str, Any]] = None
    extraction_validation: Optional[Dict[str, Any]] = None
    measurement_validation: Optional[Dict[str, Any]] = None
    mapping_validation: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    dataverse_error: Optional[str] = None


class StorageInvoiceVendorOption(BaseModel):
    code: str
    label: str
    endpoint: str


class StorageInvoiceVendorListResponse(BaseModel):
    vendors: List[StorageInvoiceVendorOption]
