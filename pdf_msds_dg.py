"""Deterministic parser for MSDS / Dangerous Goods PDFs.

These documents are not Bills of Lading, but users upload them to populate the
Dynamics cargo IMO section.  This fallback emits a minimal BL-shaped record so
the existing formatter/uploader can create/update the house operation and cargo
row with the correct IMO data.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from imo_extractor import extract_imo_fields


def _has(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def is_msds_or_dg_document(text: str) -> bool:
    sample = (text or "")[:20000].upper()
    return bool(
        re.search(
            r"MATERIAL\s+SAFETY\s+DATA\s+SHEET|\bMSDS\b|"
            r"SAFETY\s+DATA\s+SHEET|MULTIMODAL\s+DANGEROUS\s+GOODS|"
            r"DANGEROUS\s+GOODS\s+DECLARATION",
            sample,
            re.I,
        )
    )


def _clean(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = re.sub(r"\s+", " ", value).strip(" ,;:-")
    return value or None


def _first(pattern: str, text: str, flags: int = re.I | re.S) -> Optional[str]:
    m = re.search(pattern, text, flags)
    return _clean(m.group(1)) if m else None


def _report_reference(text: str, filename: Optional[str] = None) -> Optional[str]:
    for pattern in (
        r"\b(MSDS[-\s]*[A-Z0-9]+(?:[-\s]*[A-Z])?)\b",
        r"\b(?:REPORT|REPORT[-\s]*NO\.?)\s*[:#\-]?\s*([A-Z0-9][A-Z0-9\-]{5,40})\b",
        r"\bTRANSPORT\s+DOCUMENT\s+NUMBER\s+([A-Z0-9][A-Z0-9\-]{5,40})\b",
    ):
        for match in re.finditer(pattern, text or "", re.I):
            value = _clean(match.group(1))
            if value and re.search(r"\d", value):
                return re.sub(r"\s+", "", value).upper()
    if filename:
        stem = re.sub(r"\.[^.]+$", "", filename)
        return _clean(stem)
    return None


def _bl_from_reference(reference: Optional[str], filename: Optional[str] = None) -> Optional[str]:
    candidates = []
    if reference:
        candidates.append(reference)
    if filename:
        candidates.append(filename)
    for candidate in candidates:
        digits = re.findall(r"\d{6,15}", candidate)
        if digits:
            # Prefer the longest useful identifier, e.g. MSDS-2205220035-A.
            return max(digits, key=len)
    return None


def _product_name(text: str) -> Optional[str]:
    value = (
        _first(r"\bSample\s+Name\s*:?\s*(.*?)(?=\s+Company\s+Name\b|\n|$)", text, re.I | re.S)
        or _first(r"\bGHS\s+Product\s+identifier\s*:?\s*(.*?)(?=\s+(?:Other|Recommended|Supplier|Section)\b|\n|$)", text, re.I | re.S)
        or _first(r"\bProduct\s+(?:identifier|name)\s*:?\s*(.*?)(?=\s+(?:Other|Supplier|Section)\b|\n|$)", text, re.I | re.S)
        or _proper_shipping_name(text)
    )
    return value.rstrip(".") if value else None


def _company_name(text: str) -> Optional[str]:
    return (
        _first(r"\bCompany\s+Name\s*:?\s*(.*?)(?=\s+Company\s+Address\b|\n|$)", text, re.I | re.S)
        or _first(r"\bSupplier(?:['’]s)?\s+details\s*:?\s*(.*?)(?=\s+Address\b|\n|$)", text, re.I | re.S)
        or _first(r"\b(?:Manufacturer|Supplier)\s*:?\s*(.*?)(?=\s+Address\b|\n|$)", text, re.I | re.S)
        or _first(r"\bShipper\s+\d*\.?\s*(.*?)(?=\s+\d+\.\s*Transport|\n|$)", text, re.I | re.S)
    )


def _company_address(text: str) -> Optional[str]:
    return (
        _first(r"\bCompany\s+Address\s*:?\s*(.*?)(?=\s+(?:Shanghai|EBTest|TEL|FAX|Section|$))", text, re.I | re.S)
        or _first(r"\bAddress\s*:?\s*(.*?)(?=\s+(?:Section|TEL|FAX|$))", text, re.I | re.S)
    )


def _proper_shipping_name(text: str) -> Optional[str]:
    return _first(
        r"\bUN\s+proper\s+shipping\s+name\s*:?\s*(.*?)(?=\s+(?:Transport\s+hazard|Packing\s+group|Environmental|Special|Section)\b|\n|$)",
        text,
        re.I | re.S,
    )


def _packing_group(text: str) -> Optional[str]:
    value = _first(
        r"\bPacking\s+group(?:,\s*if\s+applicable)?\s*:?\s*([IVX]{1,4}|[123])\b",
        text,
        re.I,
    )
    if not value:
        return None
    roman = value.upper()
    return {"1": "I", "2": "II", "3": "III"}.get(roman, roman)


def _cas_number(text: str) -> Optional[str]:
    return _first(
        r"\bCAS\s+No\.?\s*(?:\n|\s)+[A-Za-z0-9 ,;()/.-]*?\b(\d{2,7}-\d{2}-\d)\b",
        text,
        re.I | re.S,
    ) or _first(r"\b(\d{2,7}-\d{2}-\d)\b", text, re.I)


def _issue_date(text: str) -> Optional[str]:
    return (
        _first(
            r"\bDate\s*:?\s*([0-9]{1,2}(?:st|nd|rd|th)?[-/ ][A-Za-z]{3,9}[-/ ,]\s*[0-9]{4})",
            text,
            re.I,
        )
        or _first(
            r"\bSection\s+16\b.*?\b([0-9]{1,2}[-/ ][A-Za-z]{3,9}[-/ ][0-9]{4})\b",
            text,
            re.I | re.S,
        )
    )


def _dg_notes(
    proper_shipping_name: Optional[str],
    packing_group: Optional[str],
    cas_no: Optional[str],
) -> Optional[str]:
    parts = []
    if proper_shipping_name:
        parts.append(f"UN proper shipping name: {proper_shipping_name}")
    if packing_group:
        parts.append(f"Packing group: {packing_group}")
    if cas_no:
        parts.append(f"CAS No.: {cas_no}")
    return "; ".join(parts) or None


def parse_msds_dg_record(text: str, filename: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if not is_msds_or_dg_document(text):
        return None

    imo_fields = extract_imo_fields(text)
    product = _product_name(text)
    proper_shipping_name = _proper_shipping_name(text)
    packing_group = _packing_group(text)
    cas_no = _cas_number(text)
    reference = _report_reference(text, filename)
    bl_no = _bl_from_reference(reference, filename)
    if not (imo_fields or product or bl_no):
        return None

    rec: Dict[str, Any] = {
        "document_type": "Bill of Lading",
        "extraction_method": "msds_dg_direct",
        "_msds_dg_document": True,
        "_imo_detected": bool(imo_fields.get("_imo_detected") or imo_fields),
        "mesco_transporttype": 300000000,
        "mesco_loadtype": 300000001,
        "mesco_direction": 300000000,
    }

    if bl_no:
        rec["mesco_masterblno"] = bl_no
        rec["mesco_houseblno"] = bl_no
        rec["mesco_bookingnumber"] = bl_no
    if reference:
        rec["mesco_customerreference"] = reference
    if product:
        rec["mesco_cargodescription"] = product
    if proper_shipping_name:
        rec["dg_proper_shipping_name"] = proper_shipping_name.rstrip(".")
    if packing_group:
        rec["dg_packing_group"] = packing_group
    if cas_no:
        rec["dg_cas_no"] = cas_no
    company = _company_name(text)
    if company:
        rec["mesco_shippernamecontactno"] = company
        rec["mesco_shipper"] = company

    address = _company_address(text)
    if address:
        rec["mesco_shipperaddress"] = address

    upper_blob = f"{company or ''} {address or ''}".upper()
    if "CHINA" in upper_blob:
        rec["mesco_country"] = "China"
        rec["mesco_countryoforigin"] = "China"

    issue_date = _issue_date(text)
    if issue_date:
        rec["mesco_dateofissue"] = issue_date
    notes = _dg_notes(
        rec.get("dg_proper_shipping_name"),
        rec.get("dg_packing_group"),
        rec.get("dg_cas_no"),
    )
    if issue_date:
        notes = f"{notes}; Issue date: {issue_date}" if notes else f"Issue date: {issue_date}"
    if notes:
        rec["mesco_notes"] = notes

    for key, value in imo_fields.items():
        if key.startswith("_"):
            continue
        if _has(value):
            rec[key] = value

    return rec
