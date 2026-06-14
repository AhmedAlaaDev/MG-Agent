import json
import re
from typing import Any, Dict, List, Optional

from config import normalize_azure_openai_endpoint, settings
from llm_context import effective_llm_model, uses_gemini
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
        "mesco_bookingterm": {"type": ["string", "null"]},
        "mesco_etdorigin": {"type": ["string", "null"]},
        "mesco_etadestination": {"type": ["string", "null"]},
        "mesco_pickupaddress": {"type": ["string", "null"]},
        "mesco_deliveryaddress": {"type": ["string", "null"]},
        "mesco_routenotes": {"type": ["string", "null"]},
        "mesco_notes": {"type": ["string", "null"]},
        "mesco_certificatenumber": {"type": ["string", "null"]},
        "mesco_shippingline": {"type": ["string", "null"]},
        "mesco_agent": {"type": ["string", "null"]},
        "mesco_transhipmentport": {"type": ["string", "null"]},
        "mesco_importerstaxno": {"type": ["string", "null"]},
        "mesco_foreignsupplierregistrationnumber": {"type": ["string", "null"]},
        "mesco_incoterm": {"type": ["string", "null"]},
        "mesco_blstatus": {
            "type": ["integer", "null"],
            "description": "886150000=Original B/L, 886150001=Telex/Express release",
        },
        "mesco_telexrelease": {"type": ["boolean", "null"]},
        "mesco_imoclass": {"type": ["string", "null"]},
        "mesco_unnumber": {"type": ["string", "null"]},
        "mesco_unno": {"type": ["string", "null"]},
        "mesco_flashptc": {"type": ["string", "null"]},
        "mesco_imo": {"type": ["boolean", "null"]},
        "mesco_chemical": {"type": ["boolean", "null"]},
        "dg_proper_shipping_name": {"type": ["string", "null"]},
        "dg_packing_group": {"type": ["string", "null"]},
        "dg_cas_no": {"type": ["string", "null"]},
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
- mesco_shippingline: the canonical OCEAN carrier/line name ("EVERGREEN", "MAERSK",
  "CMA CGM", "MSC", "HAPAG-LLOYD", "COSCO", "ONE", "YANG MING") — not the local agent.
- mesco_agent: the ISSUING forwarder / NVOCC / MTO who signs or issues the document
  (e.g. "BYTEPORT LOGISTICS TECHNOLOGIES PRIVATE LIMITED", "UTT LOGISTICS AND FOREIGN
  TRADE LTD."). On MTD layouts this is the large company block beside the MTO
  registration / notify-2 column, or the name after "For ..." / "Signed for ..." at the
  bottom. NEVER the destination delivery agent (MESCO / "MARINE & ENGINEERING SERVICES
  COMPANY" at "Destination Agent Address"), NEVER the shipper, consignee, or notify party,
  and NEVER the ocean vessel operator unless they are clearly the document issuer.
- mesco_incoterm: the 3-letter Incoterm code only (CIF, CFR, FOB, EXW, FCA, DAP, DDP,
  CPT, CIP). Strip any trailing place ("CIF ALEXANDRIA" -> "CIF").
- Container number: 4 letters + 6 digits + check digit. Preserve full form; OCR slash
  forms (CSLU203520 / 4) are acceptable as-is.
- mesco_containertype: ISO type ONLY ("40HC", "20GP", "40RF") with NO count prefix.
  Strip leading "1 x ", "2x", "N X " etc. — that count belongs in cr401_totalteus.
- Packages: count + unit verbatim from the goods row ("7 PALLETS", "101 ROLLS",
  "9 PACKAGES", "243 CARTONS"). OCR may show "ALLETS" for "PALLETS" — infer.
- Standard ocean B/L (MARKS AND NUMBERS | DESCRIPTIONS OF GOODS table): copy the
  COMPLETE goods narrative from the goods column — every pallet/product line,
  per-line net/gross weights, NON STACKABLE, inline HS CODE, ACID, and Egyptian
  importer tax ID when printed there. Never summarize to a single short product name.
- MULTI-MODAL TRANSPORT DOCUMENT (MTD): capture the FULL goods narrative between
  the container/seal block and "Particulars above furnished" / "Shipped on Board".
  Include pallet/drum counts, product name, PO/invoice refs, material number, batch,
  origin — as one coherent mesco_cargodescription (dedupe repeated lines).
- Weights/CBM: from the goods table on the same page (or continuation pages for one B/L).
- Vessel/voyage: split correctly. Port of loading -> mesco_origin; discharge -> mesco_destination.
- Freight: FREIGHT PREPAID -> mesco_pcfreightterm "PREPAID"; FREIGHT COLLECT -> "COLLECT".
  Downstream rules map Prepaid→Freehand booking + Origin payable; Collect→Nomination + Destination.
- mesco_loadtype: choose FCL (300000000) vs LCL (300000001) by shipment meaning — LCL for
  consolidation/CFS/groupage/cargo manifest/multiple houses under one container; FCL for a full
  container load dedicated to one shipper/consignee. Do not guess FCL when the document is an
  LCL manifest or mentions CFS/CFS or consolidated cargo.
- mesco_blstatus (Dynamics BL Status toggle): 886150001 (Telex) when the document is
  telex/express/seaway release or number of original B/Ls is ZERO; 886150000 (Original)
  when original B/Ls are required (e.g. "THREE (3) ORIGINAL", "presentation of original B/L").
  Set mesco_telexrelease true when Telex, false when Original.
- mesco_transporttype: 300000000 for sea.
- mesco_loadtype: LCL = 300000001; FCL = 300000000.
  CRITICAL: one or more container numbers does NOT automatically mean FCL.
  Use LCL when the document says LCL, CFS, groupage, consolidation,
  consolidated cargo, N/M consolidation, attached house list, master with
  houses, or multiple house B/Ls sharing one master/container.
- mesco_direction: 300000000 (Import) when Egypt is destination/consignee; 300000001 (Export) when Egypt is origin/shipper.
- MSDS / Safety Data Sheet / Dangerous Goods documents:
  * Treat "Supplier's details", "Supplier details", "Manufacturer", or "Company Name"
    as the shipper/supplier. Put the clean company name in mesco_shippernamecontactno
    and mesco_shipper; put the following Address field in mesco_shipperaddress.
  * GHS Product identifier / Product name / Sample Name -> mesco_cargodescription.
  * Section 14 Transport Information:
    - UN number -> mesco_unnumber as "UN####" and mesco_unno as the four digits.
    - Transport hazard class(es) -> mesco_imoclass.
    - UN proper shipping name -> dg_proper_shipping_name exactly, including
      qualifiers such as "STABILIZED".
    - Packing group -> dg_packing_group (I, II, or III).
  * CAS No. from Section 3 composition -> dg_cas_no.
  * Flash point -> mesco_flashptc numeric Celsius value when visible.
  * Set mesco_imo=true and mesco_chemical=true when the document identifies
    dangerous goods / UN transport information.
  * Section 16 form/revision date -> mesco_dateofissue when no explicit issue
    date exists elsewhere.
- mesco_handlinginformation: short special-handling / clause summary only (MAX 100
  chars in CRM). Examples: "***UNSTACKABLE***", "CONSOLIDATED CARGO IN TRANSIT",
  "SHIPPER'S LOAD & COUNT". Put the full legal clause text in mesco_notes when longer.
- Do not guess. Use null when not visible on that page/record.

## Dataverse length limits (DO NOT EXCEED)
- mesco_cargodescription: max 1500 chars. Deduplicate repeated lines; keep the cleanest
  narrative. Do NOT repeat the container row, package totals, or numeric weight rows.
- mesco_shippernamecontactno / mesco_consigneenamecontactno / mesco_notify1: max 100 chars.
- mesco_shipperaddress / mesco_consigneeaddress: max 250 chars; mesco_deliveryaddress
  and mesco_handlinginformation: max 100 chars.
- mesco_masterblno / mesco_houseblno: max 100 chars.
- If the source text is longer, return the most informative truncated value; the
  downstream pipeline will not retry on overflow.

## Spreadsheet / Excel / CSV (CRITICAL)
- Text may include ``--- SHEET: Name ---`` blocks with ROW N / CELLS lines, or tab-separated
  manifest tables with header rows (H/BL, SHIPPER, CONSIGNEE, POL, POD, GROSS, CBM, etc.).
- document_layout ``manifest``: one master B/L (ocean MBL or job/booking number) plus ONE record
  per house row in the table. Map each row's H/BL to mesco_houseblno; master totals stay on master.
- Column headers map to CRM fields:
    * H/BL, HBL, HOUSE B/L -> mesco_houseblno
    * M/BL, MBL, MASTER B/L, OCEAN B/L -> mesco_masterblno (master row only)
    * SHIPPER -> mesco_shippernamecontactno / mesco_shipper
    * CONSIGNEE, CNEE -> mesco_consigneenamecontactno / mesco_consignee
    * POL, PLACE OF RECEIPT -> mesco_origin
    * POD, PLACE OF DELIVERY, DISCHARGE -> mesco_destination
    * GROSS, WEIGHT, KGS, KGM -> cr401_totalgrossweight (numeric only)
    * CBM, MEAS, VOLUME -> cr401_totalvolume (numeric only)
    * PKGS, PACKAGES, NOS -> cr401_totalpackages (count + unit or integer)
    * CONTAINER, CNTR -> container_number
    * SEAL -> seal_number
    * HS CODE, TARIFF -> mesco_hscode (that row only)
    * CARGO, DESCRIPTION -> mesco_cargodescription (that row only)
- Never copy master totals onto every house row. Never merge one row's cargo into another.
- When the attached spreadsheet file is provided, prefer its column alignment over noisy OCR text.

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


class GeminiClient:
    _client = None

    @classmethod
    def get_client(cls):
        if cls._client is not None:
            return cls._client

        try:
            from google import genai
        except Exception as exc:
            raise ImportError(
                "Google Gen AI SDK is not available. Install: pip install google-genai"
            ) from exc

        api_key = (settings.gemini_api_key or "").strip()
        if not api_key:
            raise ValueError("Gemini is not configured. Set GEMINI_API_KEY in .env.")

        cls._client = genai.Client(api_key=api_key)
        return cls._client


def _strip_json_fences(content: str) -> str:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()


def _parse_json_response(content: str, provider: str) -> Dict[str, Any]:
    if not content:
        raise ValueError(f"{provider} returned empty content.")
    text = _strip_json_fences(content)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for idx, ch in enumerate(text):
        if ch not in "{[":
            continue
        try:
            parsed, _end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    raise ValueError(f"{provider} returned invalid JSON (no parseable object).")


def _call_azure_json(system: str, user: str, schema: Dict[str, Any]) -> Dict[str, Any]:
    client = AzureClient.get_client()
    response = client.chat.completions.create(
        model=effective_llm_model(),
        temperature=0,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_schema", "json_schema": schema},
    )
    content = response.choices[0].message.content or ""
    return _parse_json_response(content, "Azure OpenAI")


def _gemini_file_part(file_bytes: bytes, mime_type: str):
    """Return a Gemini content Part for a file, inline for small files, Files API for large."""
    from google.genai import types

    max_inline = settings.gemini_inline_pdf_max_bytes
    if len(file_bytes) <= max_inline:
        return types.Part.from_bytes(data=file_bytes, mime_type=mime_type)

    import io

    client = GeminiClient.get_client()
    uploaded = client.files.upload(
        file=io.BytesIO(file_bytes),
        config={"mime_type": mime_type},
    )
    return uploaded


def _call_gemini_json(
    system: str,
    user: str,
    schema: Dict[str, Any],
    *,
    file_bytes: Optional[bytes] = None,
    filename: Optional[str] = None,
) -> Dict[str, Any]:
    from google.genai import types

    client = GeminiClient.get_client()
    inner_schema = schema.get("schema") or schema
    # Gemini Developer API cannot use our OpenAI-style schema (nullable unions,
    # additionalProperties). JSON mode + schema in the prompt matches Azure output.
    schema_hint = json.dumps(inner_schema, indent=2)
    combined_system = (
        f"{system}\n\n"
        "Return ONLY one valid JSON object matching this schema (use null for missing fields). "
        "No markdown fences, no commentary, and no second JSON object after the first:\n"
        f"{schema_hint}"
    )

    # Multimodal: when the original file is available, give Gemini the real
    # document (visual layout, tables, multi-column blocks) plus extracted text as
    # a hint. This dramatically improves field mapping over OCR/text alone.
    contents: Any = user
    mime = _native_mime_for_file(file_bytes, filename)
    if file_bytes and mime:
        try:
            file_part = _gemini_file_part(file_bytes, mime)
            if mime == "application/pdf":
                guidance = (
                    "The attached PDF is the authoritative source. Read its tables and "
                    "layout directly to map each field. The text below is OCR/native-text "
                    "extraction of the same document, provided only as a hint:\n\n"
                )
            else:
                guidance = (
                    "The attached spreadsheet/workbook is the authoritative source. Read "
                    "sheet tabs, column headers, and row alignment directly to map each "
                    "house/manifest row. The text below is a flattened export of the same "
                    "workbook, provided only as a hint:\n\n"
                )
            contents = [file_part, guidance + user]
        except Exception:
            contents = user

    response = client.models.generate_content(
        model=effective_llm_model(),
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=combined_system,
            temperature=0,
            response_mime_type="application/json",
        ),
    )
    content = (response.text or "").strip()
    return _parse_json_response(content, "Gemini")


def _call_llm_json(
    system: str,
    user: str,
    schema: Dict[str, Any],
    *,
    file_bytes: Optional[bytes] = None,
    filename: Optional[str] = None,
) -> Dict[str, Any]:
    if uses_gemini():
        return _call_gemini_json(
            system, user, schema, file_bytes=file_bytes, filename=filename
        )
    return _call_azure_json(system, user, schema)


def _llm_label() -> str:
    return "Gemini" if uses_gemini() else "Azure OpenAI"


def _input_char_budget() -> int:
    """Per-call input budget; Gemini's large context allows far more than Azure."""
    if uses_gemini():
        return max(settings.gemini_max_input_chars, settings.max_input_chars)
    return settings.max_input_chars


_PAGE_MARKER_RE = re.compile(r"(?=^---\s*PAGE\s+\d+\s*---)", re.I | re.M)
_SHEET_MARKER_RE = re.compile(r"(?=^---\s*SHEET\s*:)", re.I | re.M)


def _native_mime_for_file(file_bytes: Optional[bytes], filename: Optional[str] = None) -> Optional[str]:
    """Detect MIME type for Gemini native multimodal upload."""
    if not file_bytes:
        return None
    if file_bytes[:5] == b"%PDF-":
        return "application/pdf"
    if file_bytes[:2] == b"PK":
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if file_bytes[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "application/vnd.ms-excel"
    ext = ""
    if filename and "." in filename:
        ext = filename.rsplit(".", 1)[-1].lower()
    if ext == "csv":
        return "text/csv"
    if ext in ("xlsx", "xlsm"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if ext == "xls":
        return "application/vnd.ms-excel"
    return None


def _should_send_native_file(
    mime: Optional[str],
    *,
    page_scope: bool,
) -> bool:
    if not mime or page_scope:
        return False
    if mime == "application/pdf":
        return bool(settings.gemini_native_pdf)
    if mime in (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
        "text/csv",
    ):
        return bool(settings.gemini_native_spreadsheet)
    return False


def _chunk_long_text(text: str, max_chars: int) -> List[str]:
    """
    Split very long text into chunks that each fit in ``max_chars``.

    Prefers ``--- PAGE N ---`` boundaries (multi-page PDFs), then ``--- SHEET:`` blocks
    (Excel workbooks); otherwise falls back to line boundaries (long CSV). A single
    oversized unit is hard sliced so nothing is dropped.
    """
    if len(text) <= max_chars:
        return [text]

    pages = [p for p in _PAGE_MARKER_RE.split(text) if p.strip()]
    if len(pages) <= 1:
        pages = [p for p in _SHEET_MARKER_RE.split(text) if p.strip()]
    units = pages if len(pages) > 1 else text.splitlines(keepends=True)

    chunks: List[str] = []
    buf = ""
    for unit in units:
        if len(unit) > max_chars:
            if buf:
                chunks.append(buf)
                buf = ""
            for i in range(0, len(unit), max_chars):
                chunks.append(unit[i : i + max_chars])
            continue
        if len(buf) + len(unit) > max_chars:
            chunks.append(buf)
            buf = unit
        else:
            buf += unit
    if buf.strip():
        chunks.append(buf)
    return chunks or [text[:max_chars]]


def _merge_chunk_payloads(payloads: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge per-chunk LLM payloads into one, de-duplicating records by B/L number."""
    from record_reconciliation import dedupe_records_by_bl

    all_records: List[Dict[str, Any]] = []
    warnings: List[str] = []
    confidence: Dict[str, Any] = {}
    layout = "unknown"
    for payload in payloads:
        all_records.extend(payload.get("records") or [])
        warnings.extend(payload.get("warnings") or [])
        if isinstance(payload.get("confidence"), dict):
            confidence.update(payload["confidence"])
        cand = payload.get("document_layout")
        if cand and (layout == "unknown" or layout == "single_bl"):
            layout = cand

    merged_records = dedupe_records_by_bl(all_records)
    if len(merged_records) >= 2 and layout in ("unknown", "single_bl"):
        layout = "multi_bl_pages"
    return {
        "document_layout": layout,
        "records": merged_records,
        "confidence": confidence,
        "warnings": warnings,
    }


def extract_records_with_azure_openai(
    extracted_text: str,
    *,
    page_scope: bool = False,
    file_bytes: Optional[bytes] = None,
    filename: Optional[str] = None,
    pdf_bytes: Optional[bytes] = None,
) -> Dict[str, Any]:
    """
    Intelligent extraction: returns { document_layout, records[], confidence, warnings }.

    Long documents that exceed the model input budget are split into page/sheet/line
    chunks and merged, so no content is silently truncated. When ``file_bytes`` is
    supplied and the provider is Gemini, the original PDF or spreadsheet is sent
    natively for true layout/table understanding.
    """
    # Backward-compatible alias used by older callers.
    if file_bytes is None and pdf_bytes is not None:
        file_bytes = pdf_bytes

    text = normalize_text(extracted_text)
    if not text:
        raise ValueError("No text was extracted from the document.")

    system = SYSTEM_PROMPT + (PAGE_SCOPE_PROMPT if page_scope else "")
    user_prefix = "Extract all Bill(s) of Lading from this document:\n\n"
    if page_scope:
        user_prefix = "Extract the Bill of Lading on this page only:\n\n"

    budget = _input_char_budget()
    mime = _native_mime_for_file(file_bytes, filename)
    native_file = _should_send_native_file(mime, page_scope=page_scope)

    if native_file:
        return _call_llm_json(
            system,
            user_prefix + text[:budget],
            MULTI_BL_JSON_SCHEMA,
            file_bytes=file_bytes,
            filename=filename,
        )

    if len(text) <= budget or page_scope:
        return _call_llm_json(
            system,
            user_prefix + text[:budget],
            MULTI_BL_JSON_SCHEMA,
            file_bytes=None,
            filename=filename,
        )

    # Long text (e.g. big spreadsheet or many-page PDF without native upload):
    # chunk, extract per chunk, then merge.
    chunks = _chunk_long_text(text, budget)
    payloads: List[Dict[str, Any]] = []
    for chunk in chunks:
        try:
            payloads.append(
                _call_llm_json(system, user_prefix + chunk, MULTI_BL_JSON_SCHEMA)
            )
        except Exception:
            continue
    if not payloads:
        return _call_llm_json(
            system,
            user_prefix + text[:budget],
            MULTI_BL_JSON_SCHEMA,
            file_bytes=file_bytes if native_file else None,
            filename=filename,
        )
    merged = _merge_chunk_payloads(payloads)
    merged.setdefault("warnings", []).append(
        f"long_document_chunked: {len(chunks)} segments merged"
    )
    return merged


def extract_records_with_llm(
    extracted_text: str,
    *,
    page_scope: bool = False,
    file_bytes: Optional[bytes] = None,
    filename: Optional[str] = None,
    pdf_bytes: Optional[bytes] = None,
) -> Dict[str, Any]:
    """Provider-agnostic alias (routes via LLM_PROVIDER in .env)."""
    return extract_records_with_azure_openai(
        extracted_text,
        page_scope=page_scope,
        file_bytes=file_bytes,
        filename=filename,
        pdf_bytes=pdf_bytes,
    )


def extract_with_azure_openai(extracted_text: str) -> Dict[str, Any]:
    """Backward-compatible: returns a single flat B/L dict (first record)."""
    payload = extract_records_with_azure_openai(extracted_text)
    records = payload.get("records") or []
    if not records:
        raise ValueError(f"{_llm_label()} returned no B/L records.")
    first = dict(records[0])
    if payload.get("warnings"):
        first.setdefault("warnings", [])
        first["warnings"] = list(first.get("warnings") or []) + list(payload["warnings"])
    if payload.get("confidence"):
        first["confidence"] = {**(first.get("confidence") or {}), **payload["confidence"]}
    first["_document_layout"] = payload.get("document_layout")
    return first
