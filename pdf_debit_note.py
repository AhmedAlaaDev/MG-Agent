"""
Parse freight-forwarder DEBIT NOTE PDFs (e.g. Byteport / Nxt_Customer_INV_GST).

These documents use a two-column layout where right-column labels (Shipment Ref.,
Master Number, House Number, Gross Wt, …) are interleaved on the same lines as
shipper/consignee party blocks.  LLM extraction often picks the wrong label as
the consignee name — this parser strips column noise and splits name vs address.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

DEBIT_NOTE_RE = re.compile(r"\bDEBIT\s+NOTE\b", re.I)
HOUSE_NO_RE = re.compile(r"\bHouse\s+Number\s*:\s*([A-Z0-9\-]+)", re.I)
MASTER_NO_RE = re.compile(r"\bMaster\s+Number\s*:\s*([A-Z0-9\-]+)", re.I)
SHIPMENT_REF_RE = re.compile(r"\bShipment\s+Ref\.?\s*:\s*([A-Z0-9\-]+)", re.I)
INVOICE_NO_RE = re.compile(r"\bInvoice\s+Number\s*:\s*([^\n]+?)(?=\s+IRN|\s+Date\s*:|$)", re.I)
REBATE_CHARGE_RE = re.compile(
    r"\b1\s+REBATE\b|\bCharge\s+Name\b.*?REBATE\b",
    re.I | re.S,
)

_RIGHT_COL_INLINE = re.compile(
    r"\s+(?:Shipment\s+Ref\.|Master\s+Number|House\s+Number|Gross\s+Wt|Volume|"
    r"Chargeble\s+Volume|Payment\s+Due\s+Date|Date|IRN|Invoice\s+Number)\s*:.*$",
    re.I,
)

_JUNK_LINE_RE = re.compile(
    r"^(?:IRN|Date|Payment\s+Due\s+Date|Invoice\s+Number|Customer\s+Code|"
    r"Daybook\s+Code|Customer\s+PAN)\s*:",
    re.I,
)
_EMAIL_LINE_RE = re.compile(r"^[\w.\-+]+@[\w.\-]+\.\w+", re.I)
_HEX_IRN_RE = re.compile(r"\b[0-9a-f]{32,}\b", re.I)

_PORT_ORIGIN_RE = re.compile(
    r"Port\s+of\s+Origin\s*:\s*(.+?)(?=\s+Number\s+of\s+Packs|\s+Port\s+of\s+Discharge|$)",
    re.I | re.S,
)
_PORT_DISCHARGE_RE = re.compile(
    r"Port\s+of\s+Discharge\s*:\s*([^\n]+?)(?=\s+Gross\s+Wt|\s+Vessel|\s+Volume|$)",
    re.I,
)
_VESSEL_RE = re.compile(r"Vessel\s*:\s*([^\n]+?)(?=\s+Voyage|\s+Volume|$)", re.I)
_VOYAGE_RE = re.compile(r"Voyage\s+Number\s*:\s*([^\n]+?)(?=\s+Incoterm|\s+Chargeble|$)", re.I)
_INCOTERM_RE = re.compile(
    r"Incoterm\s*:\s*([A-Z]{3})\b[^,\n]*",
    re.I,
)
_PACKS_RE = re.compile(r"Number\s+of\s+Packs\s*:\s*(\d+)", re.I)
_GROSS_RE = re.compile(r"Gross\s+Wt\s*\(Kgs\)\s*:\s*([\d.]+)", re.I)
_VOLUME_RE = re.compile(r"Volume\s+\(CBM\)\s*:\s*([\d.]+)", re.I)
_CONTAINER_RE = re.compile(
    r"Container\s+No\s*&\s*Type\s*\n?\s*([A-Z]{4}\d{7})\s*/\s*([^\n]+)",
    re.I,
)

_SHIPPER_BLOCK_RE = re.compile(
    r"Shipper\s*:\s*(.*?)(?=Consignee\s*:|Port\s+of\s+Origin\s*:|Date\s*:|Payment\s+Due)",
    re.I | re.S,
)
_CONSIGNEE_BLOCK_RE = re.compile(
    r"Consignee\s*:\s*(.*?)(?=Port\s+of\s+Origin\s*:|Port\s+of\s+Discharge\s*:|SNo\.)",
    re.I | re.S,
)

_BOGUS_CONSIGNEE_NAMES = re.compile(
    r"^(Shipment\s+Ref\.?|Master\s+Number|House\s+Number|Gross\s+Wt|Port\s+of|Customer\s+Code)$",
    re.I,
)


def is_freight_debit_note(text: str) -> bool:
    if not text or not DEBIT_NOTE_RE.search(text):
        return False
    return bool(HOUSE_NO_RE.search(text) or MASTER_NO_RE.search(text))


def _clean_inline_noise(line: str) -> str:
    line = _RIGHT_COL_INLINE.sub("", line or "")
    line = re.sub(r"\s+IRN\s*:\s*[0-9a-f]+\s*", " ", line, flags=re.I)
    line = _HEX_IRN_RE.sub("", line)
    line = re.sub(r"\s+Email\s*:\s*[\w.\-+]+@[\w.\-]+\.\w+.*$", "", line, flags=re.I)
    line = re.sub(r"\s+P/M\s*:\s*[\d\-()+\s]+(?:LINES\))?\s*", " ", line, flags=re.I)
    line = re.sub(r"\s+", " ", line).strip()
    line = line.rstrip("*,").strip()
    return line


def _lines_from_block(block: str) -> List[str]:
    lines: List[str] = []
    for raw in (block or "").replace("\r", "\n").split("\n"):
        cleaned = _clean_inline_noise(raw)
        if not cleaned:
            continue
        if _JUNK_LINE_RE.match(cleaned):
            continue
        if _EMAIL_LINE_RE.match(cleaned):
            continue
        if re.match(r"^(House|Master)\s+Number\s*:", cleaned, re.I):
            continue
        if re.fullmatch(r"[0-9a-f]{32,}", cleaned, re.I):
            continue
        lines.append(cleaned)
    return lines


def _split_name_address(lines: List[str]) -> Tuple[Optional[str], Optional[str]]:
    if not lines:
        return None, None

    joined = " ".join(lines)
    joined = re.sub(r"\s+", " ", joined).strip()

    # Company name often ends before ", 176 ..." street number on same line.
    street_split = re.search(
        r",\s*(\d+[\w\-]*\s+(?:EI\s+)?[A-Z].+)$",
        joined,
        re.I,
    )
    if street_split:
        name = joined[: street_split.start()].strip().rstrip(",")
        address = street_split.group(1).strip()
        return name[:100] if name else None, address[:250] if address else None

    if len(lines) == 1:
        return lines[0][:100], None

    name = lines[0]
    if len(lines) >= 2 and not re.search(r"\d", lines[0]):
        # Second line may continue legal name (S.A.E, L.L.C, …) without street number yet.
        second = lines[1]
        if not re.match(r"^\d", second) and len(second) < 80:
            name = f"{name} {second}".strip()
            address_lines = lines[2:]
        else:
            address_lines = lines[1:]
    else:
        address_lines = lines[1:]

    address = ", ".join(address_lines).strip() if address_lines else None
    if name:
        name = re.sub(r"\s+IRN\s*:.*$", "", name, flags=re.I).strip().rstrip(",")
        name = _HEX_IRN_RE.sub("", name).strip().rstrip(",")
    if address:
        address = _HEX_IRN_RE.sub("", address)
        parts = [p.strip() for p in address.split(",") if p.strip()]
        parts = [
            p
            for p in parts
            if not re.fullmatch(r"[0-9a-f]{12,}", p, re.I)
        ]
        address = ", ".join(parts)
        address = re.sub(r",\s*,", ", ", address)
        address = re.sub(r"\s+P/M\s*:.*?(?=,|$)", "", address, flags=re.I)
        address = re.sub(r"Email\s*:.*$", "", address, flags=re.I).strip(" ,")
    return (name[:100] if name else None), (address[:250] if address else None)


def _parse_party_block(block: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (lookup_name, display_name, address)."""
    lines = _lines_from_block(block)
    name, address = _split_name_address(lines)
    lookup = name
    if name and address and address.split(",")[0].strip().isdigit() is False:
        # Keep full first line for CRM account matching.
        lookup = re.sub(r"\s+", " ", name).strip()
    return lookup, name, address


def parse_freight_debit_note(text: str) -> Optional[Dict[str, Any]]:
    if not is_freight_debit_note(text):
        return None

    record: Dict[str, Any] = {
        "document_type": "debit_note",
        "extraction_method": "debit_note_direct",
        "mesco_transporttype": 300000000,
        "mesco_loadtype": 300000000,
        "mesco_direction": 300000000,
    }

    house_m = HOUSE_NO_RE.search(text)
    master_m = MASTER_NO_RE.search(text)
    if house_m:
        record["mesco_houseblno"] = house_m.group(1).strip()
    if master_m:
        record["mesco_masterblno"] = master_m.group(1).strip()

    shipper_m = _SHIPPER_BLOCK_RE.search(text)
    if shipper_m:
        shipper_lookup, shipper_name, shipper_addr = _parse_party_block(shipper_m.group(1))
        if shipper_name:
            record["mesco_shippernamecontactno"] = shipper_name
            record["mesco_shipper"] = shipper_lookup
        if shipper_addr:
            record["mesco_shipperaddress"] = shipper_addr

    consignee_m = _CONSIGNEE_BLOCK_RE.search(text)
    if consignee_m:
        consignee_lookup, consignee_name, consignee_addr = _parse_party_block(
            consignee_m.group(1),
        )
        if consignee_lookup:
            record["mesco_consignee"] = consignee_lookup
        if consignee_name and not _BOGUS_CONSIGNEE_NAMES.match(consignee_name):
            record["mesco_consigneenamecontactno"] = consignee_name
        if consignee_addr:
            record["mesco_consigneeaddress"] = consignee_addr

    origin_m = _PORT_ORIGIN_RE.search(text)
    if origin_m:
        record["mesco_origin"] = origin_m.group(1).strip()

    discharge_m = _PORT_DISCHARGE_RE.search(text)
    if discharge_m:
        record["mesco_destination"] = discharge_m.group(1).strip()

    vessel_m = _VESSEL_RE.search(text)
    if vessel_m:
        record["mesco_vessel"] = vessel_m.group(1).strip()

    voyage_m = _VOYAGE_RE.search(text)
    if voyage_m:
        record["mesco_voytruckno"] = voyage_m.group(1).strip()

    incoterm_m = _INCOTERM_RE.search(text)
    if incoterm_m:
        record["mesco_incoterm"] = incoterm_m.group(1).upper()

    packs_m = _PACKS_RE.search(text)
    if packs_m:
        record["cr401_totalpackages"] = int(packs_m.group(1))

    gross_m = _GROSS_RE.search(text)
    if gross_m:
        record["cr401_totalgrossweight"] = float(gross_m.group(1))

    volume_m = _VOLUME_RE.search(text)
    if volume_m:
        record["cr401_totalvolume"] = float(volume_m.group(1))

    container_m = _CONTAINER_RE.search(text)
    if container_m:
        record["container_number"] = container_m.group(1).strip()
        record["containers"] = [
            {
                "container_number": container_m.group(1).strip(),
                "container_type": container_m.group(2).strip(),
            }
        ]

    shipment_ref_m = SHIPMENT_REF_RE.search(text)
    if shipment_ref_m:
        record["mesco_bookingnumber"] = shipment_ref_m.group(1).strip()

    invoice_m = INVOICE_NO_RE.search(text)
    invoice_no = invoice_m.group(1).strip() if invoice_m else None

    cargo_parts: List[str] = []
    if REBATE_CHARGE_RE.search(text):
        cargo_parts.append("REBATE (debit note charge)")
    if shipment_ref_m:
        cargo_parts.append(f"Shipment Ref: {shipment_ref_m.group(1).strip()}")
    if invoice_no:
        cargo_parts.append(f"Invoice: {invoice_no}")
    house_no = record.get("mesco_houseblno")
    master_no = record.get("mesco_masterblno")
    if house_no and master_no:
        cargo_parts.append(f"House {house_no} / Master {master_no}")
    if cargo_parts:
        record["mesco_cargodescription"] = " | ".join(cargo_parts)

    if not record.get("mesco_houseblno") and not record.get("mesco_consigneenamecontactno"):
        return None

    return record


def repair_debit_note_parties(record: Dict[str, Any], raw_text: str) -> Dict[str, Any]:
    """Fix LLM misreads (e.g. consignee = 'Shipment Ref.') using debit-note regex."""
    if not is_freight_debit_note(raw_text):
        return record

    direct = parse_freight_debit_note(raw_text)
    if not direct:
        return record

    out = dict(record)
    bogus = _BOGUS_CONSIGNEE_NAMES.match(
        str(out.get("mesco_consigneenamecontactno") or "").strip(),
    )
    if bogus or not out.get("mesco_consigneeaddress"):
        for key in (
            "mesco_consignee",
            "mesco_consigneenamecontactno",
            "mesco_consigneeaddress",
            "mesco_shipper",
            "mesco_shippernamecontactno",
            "mesco_shipperaddress",
        ):
            if direct.get(key):
                out[key] = direct[key]

    if not out.get("mesco_houseblno") and direct.get("mesco_houseblno"):
        out["mesco_houseblno"] = direct["mesco_houseblno"]
    if not out.get("mesco_masterblno") and direct.get("mesco_masterblno"):
        out["mesco_masterblno"] = direct["mesco_masterblno"]

    return out
