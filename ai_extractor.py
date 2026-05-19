import json
from typing import Any, Dict, List, Optional

from config import settings
from pdf_extractor import normalize_text


def _bl_record_properties() -> Dict[str, Any]:
    return {
        "document_type": {"type": ["string", "null"]},
        "source_page": {"type": ["integer", "null"]},
        "mesco_masterblno": {"type": ["string", "null"]},
        "mesco_houseblno": {"type": ["string", "null"]},
        "mesco_bookingnumber": {"type": ["string", "null"]},
        "mesco_acidnumber": {"type": ["string", "null"]},
        "mesco_shippernamecontactno": {"type": ["string", "null"]},
        "mesco_shipperaddress": {"type": ["string", "null"]},
        "mesco_shippercontactnumber": {"type": ["string", "null"]},
        "mesco_consigneenamecontactno": {"type": ["string", "null"]},
        "mesco_consigneeaddress": {"type": ["string", "null"]},
        "mesco_notify1": {"type": ["string", "null"]},
        "mesco_notifyaddress": {"type": ["string", "null"]},
        "mesco_vessel": {"type": ["string", "null"]},
        "mesco_voytruckno": {"type": ["string", "null"]},
        "mesco_origin": {"type": ["string", "null"]},
        "mesco_destination": {"type": ["string", "null"]},
        "mesco_cargodescription": {"type": ["string", "null"]},
        "cr401_totalgrossweight": {"type": ["string", "null"]},
        "cr401_totalvolume": {"type": ["string", "null"]},
        "cr401_totalpackages": {"type": ["string", "null"]},
        "mesco_nooforgbls": {"type": ["string", "null"]},
        "mesco_containertype": {"type": ["string", "null"]},
        "mesco_containertype2": {"type": ["string", "null"]},
        "mesco_containertype3": {"type": ["string", "null"]},
        "mesco_handlinginformation": {"type": ["string", "null"]},
        "mesco_freightpayableat": {"type": ["string", "null"]},
        "mesco_ponumber": {"type": ["string", "null"]},
        "mesco_customerreference": {"type": ["string", "null"]},
        "mesco_bltype": {"type": ["integer", "null"]},
        "mesco_transporttype": {"type": ["integer", "null"]},
        "mesco_loadtype": {"type": ["integer", "null"]},
        "mesco_direction": {"type": ["integer", "null"]},
        "cr401_totalteus": {"type": ["string", "null"]},
        "mesco_pcfreightterm": {"type": ["string", "null"]},
        "mesco_etdorigin": {"type": ["string", "null"]},
        "mesco_etadestination": {"type": ["string", "null"]},
        "mesco_pickupaddress": {"type": ["string", "null"]},
        "mesco_deliveryaddress": {"type": ["string", "null"]},
        "mesco_routenotes": {"type": ["string", "null"]},
        "mesco_notes": {"type": ["string", "null"]},
        "mesco_certificatenumber": {"type": ["string", "null"]},
        "mesco_shippingline": {"type": ["string", "null"]},
        "mesco_transhipmentport": {"type": ["string", "null"]},
        "mesco_importerstaxno": {"type": ["string", "null"]},
        "mesco_foreignsupplierregistrationnumber": {"type": ["string", "null"]},
        "mesco_incoterm": {"type": ["string", "null"]},
        "mesco_telexrelease": {"type": ["boolean", "null"]},
        "mesco_imoclass": {"type": ["string", "null"]},
        "mesco_unnumber": {"type": ["string", "null"]},
        "mesco_hscode": {"type": ["string", "null"]},
        "mesco_dateofissue": {"type": ["string", "null"]},
        "mesco_placeofissue": {"type": ["string", "null"]},
        "mesco_shippedonboarddate": {"type": ["string", "null"]},
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
    }


def _bl_record_required() -> List[str]:
    props = _bl_record_properties()
    return [k for k in props if k != "containers"] + ["containers"]


BL_RECORD_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "properties": _bl_record_properties(),
    "required": _bl_record_required(),
}


MULTI_BL_JSON_SCHEMA = {
    "name": "bill_of_lading_extractions",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "document_layout": {
                "type": "string",
                "enum": [
                    "single_bl",
                    "multi_bl_pages",
                    "master_with_houses",
                    "manifest",
                    "unknown",
                ],
            },
            "records": {
                "type": "array",
                "items": BL_RECORD_SCHEMA,
                "minItems": 1,
            },
            "confidence": {"type": "object"},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["document_layout", "records", "confidence", "warnings"],
    },
}


# Legacy single-object schema (kept for callers that still expect one B/L object).
JSON_SCHEMA = {
    "name": "bill_of_lading_extraction",
    "schema": {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            **_bl_record_properties(),
            "confidence": {"type": "object"},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": _bl_record_required() + ["confidence", "warnings"],
    },
}


SYSTEM_PROMPT = """
You are a professional Bill of Lading extraction engine for Mesco CRM / Dynamics 365.

You receive text extracted from a PDF or spreadsheet. It may be:
- native PDF text from coordinate-based extraction,
- OCR text from a scanned/image PDF (often with --- PAGE N --- markers and [OCR ...] blocks),
- hybrid text containing both,
- or spreadsheet text from XLSX/XLS/CSV rows.

Return ONLY valid JSON matching the schema.

## Document layout (document_layout field)
- single_bl: one ocean/master B/L in the document.
- multi_bl_pages: two or more SEPARATE ocean B/L numbers, typically one full B/L form per page.
- master_with_houses: one master B/L plus distinct house B/L numbers (attachment list).
- manifest: spreadsheet-style manifest with many rows.
- unknown: cannot determine.

## Multiple B/L pages (CRITICAL)
- When PAGE 1 has B/L number A and PAGE 2 has B/L number B, return TWO records in the records array.
- NEVER merge data from different pages into one record.
- Each record must use consignee, packages, gross weight, CBM, and ACID visible on THAT page only.
- Set source_page to the page number (1, 2, ...) for each record.
- Shared fields (same shipper, vessel, container on every page) may repeat on each record.

## Field rules
- mesco_masterblno: ocean B/L number near "BILL OF LADING NO" / "B/L NO" — NOT shipper name, NOT ACID.
- mesco_houseblno: only when a distinct house B/L exists; null on straight master B/L.
- mesco_bookingnumber: only when explicitly labeled booking (not the same as B/L unless labeled).
- mesco_acidnumber: Egyptian ACID — digits only.
- mesco_hscode: null unless "HS CODE" / tariff codes appear in goods. NEVER use B/L number, booking, or ACID as HS code.
- Consignee: party under CONSIGNEE header (importer). Delivery agent / "FOR DELIVERY PLEASE APPLY TO" (e.g. MESCO) is NOT consignee — put in mesco_notes.
- mesco_notify1: use literal "same as cnee" when the document says so.
- Container: 4 letters + 6 digits + check digit; OCR may show CSLU203520 / 4 — preserve slash form or full number.
- Packages: extract count and unit (e.g. 7 PALLETS). OCR may show "ALLETS" for "2 PALLETS" — infer when clearly pallets row.
- Weights: gross weight in KGS and volume in CBM from the goods table on that page.
- Vessel/voyage: split correctly; port of loading is mesco_origin, discharge/delivery is mesco_destination.
- Freight: FREIGHT PREPAID -> mesco_pcfreightterm PREPAID; COLLECT -> COLLECT.
- mesco_transporttype: 300000000 for sea.
- mesco_loadtype: LCL = 300000001; FCL = 300000000.
- mesco_direction: import 300000000 if Egypt is destination; export 300000001 if Egypt is origin.
- Do not guess. Use null when not visible on that page/record.

## OCR / PDF quirks
- Prefer values tied to field labels over reading order.
- Duplicate OCR blocks on the same page: use the clearest cargo row (Container:/PALLETS/CSLU/ACID).
- Ignore boilerplate legal text for data fields.
"""


PAGE_SCOPE_PROMPT = """
This text is a SINGLE page from a PDF that may be part of a multi-page scan.
Extract exactly ONE Bill of Lading record for this page only.
If the page is not a B/L, return document_layout unknown and a single record with nulls.
"""


class AzureClient:
    _client = None

    @classmethod
    def get_client(cls):
        if cls._client is not None:
            return cls._client

        try:
            from openai import AzureOpenAI as OpenAIClient
        except Exception:
            raise ImportError("Azure OpenAI SDK is not available. Install: pip install openai")

        if not settings.azure_openai_endpoint or not settings.azure_openai_api_key:
            raise ValueError("Azure OpenAI is not configured. Set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY.")

        cls._client = OpenAIClient(
            azure_endpoint=settings.azure_openai_endpoint,
            api_key=settings.azure_openai_api_key,
            api_version=settings.azure_openai_api_version,
        )
        return cls._client


def _call_azure_json(system: str, user: str, schema: Dict[str, Any]) -> Dict[str, Any]:
    client = AzureClient.get_client()
    response = client.chat.completions.create(
        model=settings.azure_openai_deployment,
        temperature=0,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_schema", "json_schema": schema},
    )
    content = response.choices[0].message.content
    if not content:
        raise ValueError("Azure OpenAI returned empty content.")
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Azure OpenAI returned invalid JSON: {exc}") from exc


def extract_records_with_azure_openai(
    extracted_text: str,
    *,
    page_scope: bool = False,
) -> Dict[str, Any]:
    """
    Intelligent extraction: returns { document_layout, records[], confidence, warnings }.
    """
    text = normalize_text(extracted_text)
    if not text:
        raise ValueError("No text was extracted from the document.")

    safe_text = text[: settings.max_input_chars]
    system = SYSTEM_PROMPT + (PAGE_SCOPE_PROMPT if page_scope else "")
    user_prefix = "Extract all Bill(s) of Lading from this document:\n\n"
    if page_scope:
        user_prefix = "Extract the Bill of Lading on this page only:\n\n"

    return _call_azure_json(system, user_prefix + safe_text, MULTI_BL_JSON_SCHEMA)


def extract_with_azure_openai(extracted_text: str) -> Dict[str, Any]:
    """Backward-compatible: returns a single flat B/L dict (first record)."""
    payload = extract_records_with_azure_openai(extracted_text)
    records = payload.get("records") or []
    if not records:
        raise ValueError("Azure OpenAI returned no B/L records.")
    first = dict(records[0])
    if payload.get("warnings"):
        first.setdefault("warnings", [])
        first["warnings"] = list(first.get("warnings") or []) + list(payload["warnings"])
    if payload.get("confidence"):
        first["confidence"] = {**(first.get("confidence") or {}), **payload["confidence"]}
    first["_document_layout"] = payload.get("document_layout")
    return first
