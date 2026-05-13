import json
from typing import Any, Dict, Optional

from config import settings
from pdf_extractor import normalize_text


JSON_SCHEMA = {
    "name": "bill_of_lading_extraction",
    "schema": {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "document_type": {"type": ["string", "null"]},
            "mesco_masterblno": {"type": ["string", "null"]},
            "mesco_houseblno": {"type": ["string", "null"]},
            "mesco_bookingnumber": {"type": ["string", "null"]},
            "mesco_acidnumber": {"type": ["string", "null"]},
            "mesco_shippernamecontactno": {"type": ["string", "null"]},
            "mesco_shipperaddress": {"type": ["string", "null"]},
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
                        "container_number", "seal_number", "container_type",
                        "packages", "gross_weight_kg", "measurement_cbm"
                    ],
                },
            },
            "confidence": {"type": "object"},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "document_type", "mesco_masterblno", "mesco_houseblno", "mesco_bookingnumber", "mesco_acidnumber",
            "mesco_shippernamecontactno", "mesco_shipperaddress",
            "mesco_consigneenamecontactno", "mesco_consigneeaddress",
            "mesco_notify1", "mesco_notifyaddress", "mesco_vessel", "mesco_voytruckno",
            "mesco_origin", "mesco_destination", "mesco_cargodescription",
            "cr401_totalgrossweight", "cr401_totalvolume", "cr401_totalpackages",
            "mesco_nooforgbls", "mesco_containertype", "mesco_containertype2",
            "mesco_containertype3", "mesco_handlinginformation",
            "mesco_freightpayableat", "mesco_ponumber", "mesco_customerreference",
            "mesco_bltype", "mesco_transporttype", "mesco_loadtype", "mesco_direction",
            "cr401_totalteus", "mesco_pcfreightterm", "mesco_etdorigin",
            "mesco_etadestination", "mesco_pickupaddress", "mesco_deliveryaddress",
            "mesco_transhipmentport", "mesco_importerstaxno",
            "mesco_foreignsupplierregistrationnumber", "mesco_incoterm",
            "mesco_telexrelease", "mesco_imoclass", "mesco_unnumber", "mesco_hscode",
            "mesco_dateofissue", "mesco_placeofissue", "mesco_shippedonboarddate",
            "container_number", "seal_number", "containers", "confidence", "warnings",
        ],
    },
}


SYSTEM_PROMPT = """
You are a professional Bill of Lading extraction engine for Mesco CRM / Dynamics 365.

You receive text extracted from a PDF or spreadsheet. It may be:
- native PDF text from coordinate-based extraction,
- OCR text from a scanned/image PDF,
- hybrid text containing both,
- or spreadsheet text from XLSX/XLS/CSV rows, cells, formulas, and detected key/value pairs.

Return ONLY valid JSON that follows the provided schema.

Important source rules:
- Native PDFs often store text out of visual order. Use labels and nearby values, not only line order.
- If there are [NATIVE PDF TEXT] and [OCR TEXT FALLBACK / VISUAL TEXT] sections, prefer the value that is most clearly attached to a field label.
- OCR may reveal visual text that native text misses.
- Spreadsheets are presented as [SPREADSHEET TEXT] with ROW lines, CELLS lines, and DETECTED KEY/VALUE PAIRS. Use sheet names, row numbers, cell addresses, and nearby headers to map values.
- For spreadsheet manifests/invoices, a B/L number may be under HBL, HBL NO, HOUSE B/L, MBL, MASTER B/L, B/L NO, or BL NO. Map MBL/MASTER B/L to mesco_masterblno and HBL/HOUSE B/L to mesco_houseblno. Use booking fields only when explicitly a booking number.

Critical Bill of Lading rules:
- mesco_masterblno is the B/L number. It is ALWAYS found near the label "B/L No.", "B/L NO", "BL NO", or "BILL OF LADING NO".
- CRITICAL DISAMBIGUATION: The shipper company name (e.g., "SWEDEV AB") is NEVER the B/L number. The B/L number appears on the RIGHT side near the carrier logo.
- PURE NUMERIC B/L numbers with spaces (e.g., "85 008") are valid when they appear directly after a B/L label. Preserve the space in the value (e.g., return "85 008").
- Do not confuse B/L number with booking number, ACID number, container number, postal code, company name, or address.
- mesco_bookingnumber is booking/reference number when explicitly labeled booking number.
- mesco_acidnumber is the Egyptian ACID number. Return digits only.
- Extract shipper, consignee, notify party, vessel, voyage, origin/loading port, destination/discharge port.
- Vessel names must not include the port of loading. If text says "CMA CGM TIGA 0NVLOS1MA HAMBURG", vessel is "CMA CGM TIGA", voyage is "0NVLOS1MA", origin is "HAMBURG".
- Extract container numbers as 4 letters + 7 digits, seal numbers, container type, packages, gross weight, and CBM.
- Freight terms: if "FREIGHT COLLECT", set mesco_pcfreightterm="COLLECT"; if "FREIGHT PREPAID", set "PREPAID".
- mesco_transporttype must be 300000000 for sea B/Ls.
- mesco_loadtype: FCL/container present = 300000000; LCL explicitly stated = 300000001.
- mesco_direction: import = 300000000 if destination/discharge is Egypt; export = 300000001 if origin/loading is Egypt.
- mesco_bltype: if ORIGINAL appears and it is not only boilerplate, use 886150001. If NON-NEGOTIABLE is prominent, leave null unless your CRM mapping requires another value.
- Do not guess. Use null when a value is not visible.
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


def extract_with_azure_openai(extracted_text: str) -> Dict[str, Any]:
    text = normalize_text(extracted_text)
    if not text:
        raise ValueError("No text was extracted from the PDF.")

    client = AzureClient.get_client()
    safe_text = text[: settings.max_input_chars]

    response = client.chat.completions.create(
        model=settings.azure_openai_deployment,
        temperature=0,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "Extract this Bill of Lading:\n\n" + safe_text},
        ],
        response_format={"type": "json_schema", "json_schema": JSON_SCHEMA},
    )

    content = response.choices[0].message.content
    if not content:
        raise ValueError("Azure OpenAI returned empty content.")

    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Azure OpenAI returned invalid JSON: {exc}")

    return data
