import json
from typing import Any, Dict, List, Optional

from config import normalize_azure_openai_endpoint, settings
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
        "mesco_shipper": {"type": ["string", "null"]},
        "mesco_shipperaddress": {"type": ["string", "null"]},
        "mesco_shippercontactnumber": {"type": ["string", "null"]},
        "mesco_consigneenamecontactno": {"type": ["string", "null"]},
        "mesco_consignee": {"type": ["string", "null"]},
        "mesco_consigneeaddress": {"type": ["string", "null"]},
        "mesco_notify1": {"type": ["string", "null"]},
        "mesco_notifyaddress": {"type": ["string", "null"]},
        "mesco_country": {"type": ["string", "null"]},
        "mesco_countryoforigin": {"type": ["string", "null"]},
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

Return ONLY valid JSON matching the schema. Use null (not empty string) for any
field you cannot find with high confidence on the relevant page.

## Document layout (document_layout field)
- single_bl: one ocean/master B/L in the document (possibly spanning continuation pages).
- multi_bl_pages: two or more SEPARATE ocean B/L numbers, typically one full B/L form per page.
- master_with_houses: one master B/L plus distinct house B/L numbers (attachment list / manifest).
- manifest: spreadsheet-style manifest with many house rows under one master.
- unknown: cannot determine.

## Multiple B/L pages (CRITICAL)
- When PAGE 1 has B/L number A and PAGE 2 has B/L number B, return TWO records in `records`.
- NEVER merge data from different pages into one record.
- Each record uses consignee, packages, gross weight, CBM, and ACID visible on THAT page only.
- Set source_page to the page number (1, 2, ...) for each record.
- Shared fields (same shipper, vessel, container on every page) may repeat on each record.

## Continuation pages (CRITICAL — "Continued on Next Sheet")
- When the SAME B/L number spans multiple sheets (page 1 shows "Continued on Next Sheet"
  and the next page shows "Continued From Previous Sheet" with the same B/L number),
  return ONE record that aggregates content from ALL continuation pages:
    * mesco_cargodescription must include EVERY cargo line from every continuation page
      (deduplicate exact repeats; preserve order).
    * mesco_hscode must include EVERY HS code listed across continuation pages,
      pipe-separated (e.g. "85334000|3809910000|39269090"), in document order, no duplicates.
- This is the difference between page-anchored multi_bl_pages and continuation
  single_bl. If the B/L number on page 2+ equals page 1, it is continuation, not multi.

## Manifest / master-with-houses
- For manifest layouts, emit ONE record per house row plus, if visible, a record for the
  master B/L. Master totals (cr401_totalpackages / totalgrossweight / totalvolume) belong
  on the master record only — do not copy them onto each house.
- Per-house ACID, HS code, packages, weight, CBM must come from THAT house's row, never
  from the master totals or another house.

## Field rules
- mesco_masterblno: ocean B/L number near "BILL OF LADING NO" / "B/L NO" / "MASTER B/L".
  NOT the shipper name, NOT the ACID, NOT the booking number unless explicitly labeled.
- mesco_houseblno: only when a distinct HOUSE B/L exists (manifest row, attached list).
  Null on a straight master B/L.
- mesco_bookingnumber: only when explicitly labeled "BOOKING NO" / "BOOKING REF".
- mesco_acidnumber: Egyptian ACID is EXACTLY 19 digits. Strip any letters/spaces.
  Do NOT concatenate adjacent numeric fields (KGS, CBM, weight) into the ACID — those
  belong in their own fields. If OCR glues digits, return the first 19 contiguous digits.
- mesco_importerstaxno / mesco_foreignsupplierregistrationnumber: Egyptian tax IDs.
  Common Egyptian Freight Forwarder Tax ID lengths are 9 digits (e.g. "297923900").
  These are NEVER HS codes — never copy them into mesco_hscode.
- mesco_hscode: 6–10 digit numeric tariff codes appearing under a goods description and
  explicitly labeled as HS CODE / TARIFF / COMMODITY CODE, or sitting on a goods line.
  Pipe-separate multiple codes ("85334000|3809910000"). NEVER use:
    * the B/L number, booking number, container number, ACID, vessel IMO,
    * a Tax ID (any number adjacent to "TAX ID", "EXPORTER ID", "FORWARDER ID",
      "REGISTRATION NO"),
    * a weight, volume, or package count.
- mesco_consigneenamecontactno: the party UNDER the CONSIGNEE header. CRITICAL rules:
    * Form-printed boilerplate ("EXPORT REFERENCES", "FORWARDING AGENT REFERENCES",
      "FOR DELIVERY PLEASE APPLY TO", "ALSO NOTIFY", "TO ORDER") is NEVER the consignee
      — those are column titles, not party names.
    * The delivery agent at destination (e.g. MESCO / "MARINE & ENGINEERING SERVICES
      COMPANY") IS the consignee when the form lists them under CONSIGNEE; do not
      confuse them with the notify party.
    * Include the full multi-line address in mesco_consigneeaddress (street, city, country).
- mesco_shippernamecontactno: only the SHIPPER company name (first 1–2 lines under SHIPPER).
  Do NOT include the address (that goes in mesco_shipperaddress), HS codes, cargo
  descriptions, exporter IDs, or marks-and-numbers.
- mesco_notify1: literal "same as cnee" when document says "SAME AS CONSIGNEE".

## Canonical lookup names (CRITICAL for CRM object mapping)
These fields are resolved against existing Dataverse records (account / country /
incoterm / shipping line) and bound as lookups. Output the CLEANEST canonical form
so they can be matched — strip addresses, contact numbers, attn lines, and legal
suffix noise. Use null when not confidently present.
- mesco_shipper: the shipper's CLEAN legal COMPANY NAME only (e.g. "ELARABY GROUP",
  "ARCELORMITTAL"). Same company as mesco_shippernamecontactno but name-only — no
  address, no "ATTN/TEL/FAX/EMAIL", no marks. This is used to match a CRM account.
- mesco_consignee: the consignee's CLEAN legal COMPANY NAME only. Same company as
  mesco_consigneenamecontactno but name-only. Used to match a CRM account. Never a
  column title ("TO ORDER", "EXPORT REFERENCES") and never a "SAME AS ..." reference.
- mesco_country: full ENGLISH country NAME of the port of loading / shipper side
  (e.g. "Turkey", "China", "Germany", "United Arab Emirates"). A COUNTRY, never a
  city or port. Prefer the full name; a 2-letter ISO code (TR, CN, DE) is acceptable
  if that is all the document shows.
- mesco_countryoforigin: full ENGLISH country name stated as the goods' COUNTRY OF
  ORIGIN / made-in country (e.g. "COUNTRY OF ORIGIN: CHINA" -> "China"). Null if not
  explicitly stated.
- mesco_shippingline: the canonical carrier/line name ("EVERGREEN", "MAERSK",
  "CMA CGM", "MSC", "HAPAG-LLOYD", "COSCO", "ONE", "YANG MING") — not the local agent.
- mesco_incoterm: the 3-letter Incoterm code only (CIF, CFR, FOB, EXW, FCA, DAP, DDP,
  CPT, CIP). Strip any trailing place ("CIF ALEXANDRIA" -> "CIF").
- Container number: 4 letters + 6 digits + check digit. Preserve full form; OCR slash
  forms (CSLU203520 / 4) are acceptable as-is.
- mesco_containertype: ISO type ONLY ("40HC", "20GP", "40RF") with NO count prefix.
  Strip leading "1 x ", "2x", "N X " etc. — that count belongs in cr401_totalteus.
- Packages: count + unit verbatim from the goods row ("7 PALLETS", "101 ROLLS",
  "9 PACKAGES", "243 CARTONS"). OCR may show "ALLETS" for "PALLETS" — infer.
- Weights/CBM: from the goods table on the same page (or continuation pages for one B/L).
- Vessel/voyage: split correctly. Port of loading -> mesco_origin; discharge -> mesco_destination.
- Freight: FREIGHT PREPAID -> mesco_pcfreightterm "PREPAID"; FREIGHT COLLECT -> "COLLECT".
- mesco_transporttype: 300000000 for sea.
- mesco_loadtype: LCL = 300000001; FCL = 300000000.
- mesco_direction: 300000000 (Import) when Egypt is destination/consignee; 300000001 (Export) when Egypt is origin/shipper.
- mesco_handlinginformation: collect special-handling notes — "***UNSTACKABLE***",
  "DO NOT STACK", "FRAGILE", "KEEP DRY", "HAZARDOUS" — when visible.
- Do not guess. Use null when not visible on that page/record.

## Dataverse length limits (DO NOT EXCEED)
- mesco_cargodescription: max 1500 chars. Deduplicate repeated lines; keep the cleanest
  narrative. Do NOT repeat the container row, package totals, or numeric weight rows.
- mesco_shippernamecontactno / mesco_consigneenamecontactno / mesco_notify1: max 100 chars.
- mesco_shipperaddress / mesco_consigneeaddress / mesco_deliveryaddress: max 250 chars.
- mesco_masterblno / mesco_houseblno: max 100 chars.
- If the source text is longer, return the most informative truncated value; the
  downstream pipeline will not retry on overflow.

## OCR / PDF quirks
- Prefer values tied to field labels over reading order.
- Duplicate OCR blocks on the same page: use the clearest cargo row (Container:/PALLETS/CSLU/ACID).
- Ignore boilerplate legal text and disclaimers ("RECEIVED IN APPARENT GOOD ORDER", "ON
  BOARD CONDITION OF CARRIAGE", "SHIPPER'S LOAD STOW AND COUNT") for data fields.
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

        endpoint = normalize_azure_openai_endpoint(settings.azure_openai_endpoint)
        cls._client = OpenAIClient(
            azure_endpoint=endpoint,
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
