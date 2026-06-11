"""
Ocean B/L number validation and multi-page record finalization.

Rejects MESCO form/serial numbers (e.g. 0038260, 9933955) and keeps only
page-anchored ocean B/L numbers from the shipper header row.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set

from pdf_multi_bl import detect_and_extract_multi_bl_records, detect_multi_bl_candidate, split_pdf_pages
from record_reconciliation import merge_record_fields


# Known OCR form / reference numbers that are NOT ocean B/L numbers.
_FORM_SERIAL_BLOCKLIST = {
    "9933955",
    "0038260",
    "0038255",
    "3991018",
    "3991001",
}


def _digits(val: Any) -> str:
    return re.sub(r"\D", "", str(val or ""))


def fmc_organization_numbers(text: str) -> Set[str]:
    """FMC Organization numbers are regulatory refs, never ocean B/L numbers."""
    nums: Set[str] = set()
    for m in re.finditer(r"FMCG?\s+ORGANIZATION\s+NO\.?\s*(\d{5,8})\b", text or "", re.I):
        raw = m.group(1)
        nums.add(raw)
        nums.add(raw.lstrip("0") or raw)
        nums.add(_digits(raw))
    return nums


def is_fmc_organization_number(bl: str, page_text: str = "") -> bool:
    compact = _digits(bl)
    if not compact:
        return False
    return compact in {_digits(n) for n in fmc_organization_numbers(page_text)}


def extract_shipper_glued_bl_number(page_text: str) -> Optional[str]:
    """
    SACO / Tellus-style B/L printed beside the shipper block (OCR glues it to the street line).

    Example OCR::
        Shipper B/L No
        SWEDEV AB
        FABRIKSVAGEN 1 85 008
        684 22 MUNKFORS
    → B/L is ``85 008``, not part of the address.
    """
    if not page_text:
        return None
    header = page_text[:3000]
    if not re.search(r"SHIPPER\s+B/?\s*L\s*NO|B/?\s*L\s*NO\.?", header, re.I):
        return None

    m = re.search(
        r"([A-Z][A-Z\s]{4,40}\s+\d{1,5})\s+(\d{2,3})\s+(\d{2,4})\s*\n\s*(\d{2,3}\s+\d{2,4}\s+[A-Z]{3,})",
        header,
        re.I,
    )
    if not m:
        return None

    bl = f"{m.group(2)} {m.group(3)}"
    if is_fmc_organization_number(bl, page_text):
        return None
    return bl


def clean_shipper_address_bl_bleed(
    address: Optional[str],
    bl_no: Optional[str],
    page_text: str = "",
) -> Optional[str]:
    """Remove a shipper-header B/L number accidentally included in the address."""
    if not address:
        return address
    addr = re.sub(r"\s+", " ", str(address)).strip()
    if not bl_no:
        glued = extract_shipper_glued_bl_number(page_text)
        if glued:
            bl_no = glued
    if not bl_no:
        return addr

    bl = str(bl_no).strip()
    parts = bl.split()
    if len(parts) == 2:
        addr = re.sub(
            rf"\s+{re.escape(parts[0])}\s+{re.escape(parts[1])}\b",
            "",
            addr,
            flags=re.I,
        )
    addr = re.sub(rf"\b{re.escape(bl)}\b", "", addr, flags=re.I)
    addr = re.sub(r"\s+", " ", addr).strip(" ,")

    if page_text and not re.search(r"\d{5}\s+\d{2,4}\s+[A-Z]", addr, re.I):
        m = re.search(
            r"([A-Z][A-Z\s]{4,40}\s+\d{1,5})\s+\d{2,3}\s+\d{2,4}\s*\n\s*(\d{2,3}\s+\d{2,4}\s+[A-Z]{3,}.*?)\n\s*([A-Z]{4,})",
            page_text[:3000],
            re.I,
        )
        if m:
            street = re.sub(r"\s+\d{2,3}\s+\d{2,4}\s*$", "", m.group(1).strip(), flags=re.I)
            city = re.sub(r"\s*Carrier:?\s*$", "", m.group(2).strip(), flags=re.I)
            country = re.sub(r"\s*Carrier:?\s*$", "", m.group(3).strip(), flags=re.I)
            addr = f"{street}, {city}, {country}"

    addr = re.sub(r",?\s*Carrier:?\s*,?", ",", addr, flags=re.I)
    addr = re.sub(r"\s+", " ", addr).strip(" ,")
    return addr or None


def is_form_or_serial_bl_candidate(bl: str, page_text: str = "") -> bool:
    """True when the value is a MESCO form no. / header serial, not an ocean B/L."""
    raw = str(bl or "").strip().upper()
    # Carrier-prefixed ocean B/L numbers (e.g. Arkas IST000027163, NSA…).
    if re.fullmatch(r"[A-Z]{3}\d{6,12}", raw):
        return False

    compact = _digits(bl)
    if not compact:
        return True
    if compact in _FORM_SERIAL_BLOCKLIST:
        return True
    if is_fmc_organization_number(bl, page_text):
        return True
    # Leading zeros + short length → form stamp (0038260, 0038255)
    if re.fullmatch(r"0\d{6,8}", compact):
        return True
    # Seven digits not on the shipper/B/L header row
    if len(compact) == 7 and compact not in _FORM_SERIAL_BLOCKLIST:
        upper = (page_text or "").upper()
        if compact in upper and not re.search(
            rf"HEAVY\s+EQUIPMENT\s+SERVICES\s+{re.escape(compact)}\b",
            upper,
        ):
            if re.search(rf"(?:MESCO|BILL\s+OF\s+LADING\s*\|)[^\n]{{0,40}}{re.escape(compact)}", upper):
                return True
    return False


def is_valid_ocean_bl_number(bl: str, page_text: str = "", acid: Optional[str] = None) -> bool:
    from validator import is_likely_bl_number

    if not bl or not is_likely_bl_number(str(bl), acid):
        return False
    if is_form_or_serial_bl_candidate(str(bl), page_text):
        return False
    compact = _digits(bl)
    if is_fmc_organization_number(bl, page_text):
        return False
    # Short numeric B/L beside shipper header (e.g. SACO "85 008")
    if re.match(r"^\d{2,3}\s+\d{2,4}$", str(bl).strip()):
        return True
    # Ocean B/L on these scans: 9–12 digits, typically 202512xxxx
    if compact.isdigit() and len(compact) >= 9:
        return True
    if re.fullmatch(r"[A-Z]{3}\d{6,12}", str(bl).strip().upper()):
        return True
    return len(compact) >= 7 and not compact.startswith("00")


def extract_ocean_bl_from_page(page_text: str) -> Optional[str]:
    """Authoritative ocean B/L from shipper header (Combicon / MESCO / SACO layout)."""
    if not page_text:
        return None
    carrier_m = re.search(
        r"\b(IST\d{9,12}|NSA\d{9,12}|ARK[A-Z]?\d{8,12})\b",
        page_text,
        re.I,
    )
    if carrier_m:
        return carrier_m.group(1).upper()
    glued = extract_shipper_glued_bl_number(page_text)
    if glued:
        return glued
    patterns = [
        r"HEAVY\s+EQUIPMENT\s+SERVICES\s+(\d{9,12})\b",
        r"SHIPPER\s+EXPORTER[^\n]*BILL\s+OF\s+LADING\s+NO\.?[^\n]*\n\s*[^\n]+\s+(\d{9,12})\b",
    ]
    for pat in patterns:
        m = re.search(pat, page_text, re.I | re.S)
        if m:
            bl = m.group(1).strip()
            if is_valid_ocean_bl_number(bl, page_text):
                return bl
    return None


def list_canonical_page_bls(raw_text: str) -> List[Dict[str, Any]]:
    """One entry per page: {page, bl, page_text}."""
    entries: List[Dict[str, Any]] = []
    for page_no, page_text in split_pdf_pages(raw_text):
        if not re.search(r"BILL\s+OF\s+LADING", page_text, re.I):
            continue
        bl = extract_ocean_bl_from_page(page_text)
        if bl:
            entries.append({"page": page_no, "bl": bl, "page_text": page_text})
    return entries


def is_manifest_header_record(rec: Dict[str, Any]) -> bool:
    """Master row from a parsed manifest PDF (header PKGS total, not a house line)."""
    return (
        rec.get("extraction_method") in ("pdf_export_lcl_manifest", "pdf_tur_cargo_manifest")
        and not rec.get("_manifest_pdf_row")
    )


def is_manifest_house_record(rec: Dict[str, Any]) -> bool:
    """Per-house row from a parsed manifest PDF (packages/consignee are row-specific)."""
    return bool(rec.get("_manifest_pdf_row"))


def is_isaly_draft_record(rec: Dict[str, Any]) -> bool:
    """Per-page ISALY draft B/L (STAR CONCORD / MESCO scan, one B/L per page)."""
    return bool(rec.get("_isaly_draft_row")) or rec.get("extraction_method") == "pdf_isaly_draft_direct"


def normalize_packages_field(value: Any, page_text: str = "") -> Optional[str]:
    """Return package count with unit, e.g. '2 PALLETS'."""
    text = str(value or "").strip()
    manifest_pkg = re.search(
        r"^(\d+)\s+(PACKAGES|ROLLS|CARTONS?|PALLETS?)(?:\s+\*\*\*[^*]+\*\*\*)?$",
        text,
        re.I,
    )
    if manifest_pkg:
        unit = manifest_pkg.group(2).upper()
        if unit == "PALLET":
            unit = "PALLETS"
        return f"{manifest_pkg.group(1)} {unit}"
    if text.isdigit() and int(text) >= 20:
        total_m = re.search(
            rf"Total:\s*{re.escape(text)}\s+PKGS\b",
            page_text or "",
            re.I,
        )
        if total_m:
            return text
    upper = text.upper()
    if re.search(r"\d+\s*PALLETS\b", upper, re.I):
        m = re.search(r"(\d+)\s*PALLETS", upper, re.I)
        return f"{m.group(1)} PALLETS" if m else text
    if "ALLETS" in upper and re.search(r"\bALLETS\s*(?:=\s*)?STC", page_text or "", re.I):
        return "2 PALLETS"
    m = re.search(r"\b(\d+)\s*PALLETS\b", page_text or "", re.I)
    if m:
        return f"{m.group(1)} PALLETS"
    if text.isdigit():
        if re.search(r"PALLETS", page_text or "", re.I):
            return f"{text} PALLETS"
        return text
    return text or None


def packages_count_numeric(packages: Any) -> Optional[str]:
    """Leading integer from '7 PALLETS' for numeric totals."""
    if packages is None:
        return None
    m = re.search(r"(\d+)", str(packages))
    return m.group(1) if m else None


def clean_mesco_notes(notes: Optional[str]) -> Optional[str]:
    if not notes:
        return notes
    text = str(notes)
    text = re.sub(r"ALEXANDRIA\s*:\s*,", "ALEXANDRIA,", text, flags=re.I)
    text = re.sub(r"\s*:\s*,", ",", text)
    text = re.sub(r'[\s,]*i"\s*', " ", text)
    text = re.sub(r"\s+", " ", text).strip(" ,")
    if text.upper().startswith("DELIVERY AGENT"):
        text = re.sub(
            r"(TEL:\s*00203\s*3991018)\s*[^\w]*$",
            r"\1",
            text,
            flags=re.I,
        )
    return text or None


def normalize_consignee_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return name
    text = re.sub(r"\s+", " ", str(name)).strip().rstrip(".")
    if re.search(r"CITI\s+FOR\s+INVEST", text, re.I):
        return "CITI FOR INVEST & TRADE L.L.C."
    return text


def correct_record_from_page(
    rec: Dict[str, Any],
    page_text: str,
    *,
    raw_text: str = "",
    pdf_bytes: Optional[bytes] = None,
    page_no: Optional[int] = None,
) -> Dict[str, Any]:
    """Force authoritative page fields before validation."""
    out = dict(rec)
    bl = extract_ocean_bl_from_page(page_text)
    if bl:
        out["mesco_masterblno"] = bl
        out["mesco_bookingnumber"] = bl

    acid = out.get("mesco_acidnumber")
    current_bl = out.get("mesco_masterblno")
    if current_bl and (
        is_form_or_serial_bl_candidate(str(current_bl), page_text)
        or is_fmc_organization_number(str(current_bl), page_text)
    ):
        if bl:
            out["mesco_masterblno"] = bl
            out["mesco_bookingnumber"] = bl
        else:
            out.pop("mesco_masterblno", None)

    out["mesco_consigneenamecontactno"] = normalize_consignee_name(
        out.get("mesco_consigneenamecontactno")
    )
    if not is_manifest_header_record(out) and not is_manifest_house_record(out) and not is_isaly_draft_record(out):
        pkg = normalize_packages_field(out.get("cr401_totalpackages"), page_text)
        if pkg:
            out["cr401_totalpackages"] = pkg
        if out.get("containers"):
            for c in out["containers"]:
                if isinstance(c, dict):
                    cp = normalize_packages_field(c.get("packages"), page_text)
                    if cp:
                        c["packages"] = cp

    if not out.get("cr401_totalgrossweight"):
        from ocr_cargo_fields import (
            extract_gross_weight_from_page_pdf,
            extract_gross_weight_from_page_text,
        )

        weight = extract_gross_weight_from_page_text(page_text)
        if not weight and pdf_bytes and page_no:
            weight = extract_gross_weight_from_page_pdf(pdf_bytes, int(page_no), page_text)
        if weight:
            out["cr401_totalgrossweight"] = weight

    if re.search(r"(?:NEW\s+YORK|V\s*YORE\s+NEW\s+YORK)", page_text, re.I):
        out["mesco_origin"] = "NEW YORK"
    elif not out.get("mesco_origin") and raw_text and re.search(
        r"(?:NEW\s+YORK|V\s*YORE\s+NEW\s+YORK)", raw_text, re.I
    ):
        out["mesco_origin"] = "NEW YORK"
    m_vessel = re.search(r"BERNHARD\s+SCHULTE(?:\s+(\S+))?", page_text, re.I)
    if m_vessel:
        out["mesco_vessel"] = "BERNHARD SCHULTE"
        if m_vessel.group(1) and not re.search(r"CARRIER|ORIGINAL|NUMSER", m_vessel.group(1), re.I):
            out["mesco_voytruckno"] = m_vessel.group(1)

    if out.get("mesco_notes"):
        out["mesco_notes"] = clean_mesco_notes(out.get("mesco_notes"))

    if out.get("cr401_totalgrossweight"):
        for c in out.get("containers") or []:
            if isinstance(c, dict):
                c["gross_weight_kg"] = out["cr401_totalgrossweight"]

    if out.get("mesco_shipperaddress") or out.get("mesco_masterblno"):
        out["mesco_shipperaddress"] = clean_shipper_address_bl_bleed(
            out.get("mesco_shipperaddress"),
            out.get("mesco_masterblno"),
            page_text,
        )

    return out


_CANONICAL_FORCE_KEYS = {
    "mesco_masterblno",
    "mesco_bookingnumber",
    "mesco_consigneenamecontactno",
    "mesco_acidnumber",
    "cr401_totalpackages",
    "cr401_totalgrossweight",
    "cr401_totalvolume",
    "mesco_origin",
    "mesco_destination",
    "mesco_vessel",
    "mesco_voytruckno",
    "container_number",
    "seal_number",
    "containers",
}


def finalize_multi_bl_records(
    records: List[Dict[str, Any]],
    raw_text: str,
    *,
    pdf_bytes: Optional[bytes] = None,
) -> List[Dict[str, Any]]:
    """
    For multi-page PDFs: return exactly one record per canonical page B/L.
    Drops spurious Azure duplicates (wrong form numbers).
    """
    if not detect_multi_bl_candidate(raw_text):
        filtered = []
        for rec in records:
            page_text = raw_text
            if rec.get("extraction_method") == "pdf_sea_waybill_direct":
                filtered.append(rec)
                continue
            bl = rec.get("mesco_masterblno")
            if bl and is_form_or_serial_bl_candidate(str(bl), page_text):
                fixed = extract_ocean_bl_from_page(page_text)
                if fixed:
                    rec = dict(rec)
                    rec["mesco_masterblno"] = fixed
                else:
                    continue
            filtered.append(rec)
        return filtered

    canonical = detect_and_extract_multi_bl_records(raw_text)
    if not canonical or len(canonical) < 2:
        return records

    page_entries = list_canonical_page_bls(raw_text)
    allowed_bls = {_digits(c.get("mesco_masterblno")) for c in canonical if c.get("mesco_masterblno")}

    by_bl: Dict[str, Dict[str, Any]] = {}
    by_page: Dict[int, Dict[str, Any]] = {}
    for rec in records:
        bl_key = _digits(rec.get("mesco_masterblno"))
        page = rec.get("source_page") or rec.get("_page_number")
        if bl_key and bl_key in allowed_bls and bl_key not in by_bl:
            by_bl[bl_key] = rec
        if page is not None:
            by_page[int(page)] = rec

    finalized: List[Dict[str, Any]] = []
    for fb in canonical:
        page_no = int(fb.get("_page_number") or 0)
        page_text = fb.get("_page_text") or ""
        if not page_text:
            for ent in page_entries:
                if ent["page"] == page_no:
                    page_text = ent["page_text"]
                    break

        bl_key = _digits(fb.get("mesco_masterblno"))
        azure_match = by_bl.get(bl_key) or by_page.get(page_no)
        if azure_match:
            merged = merge_record_fields(azure_match, fb, prefer_secondary_keys=_CANONICAL_FORCE_KEYS)
        else:
            merged = dict(fb)

        merged = correct_record_from_page(
            merged,
            page_text,
            raw_text=raw_text,
            pdf_bytes=pdf_bytes,
            page_no=page_no,
        )
        merged["mesco_masterblno"] = fb.get("mesco_masterblno")
        merged["mesco_bookingnumber"] = fb.get("mesco_masterblno")
        merged["source_page"] = page_no
        merged["_page_number"] = page_no
        merged["_page_text"] = page_text
        finalized.append(merged)

    return finalized
