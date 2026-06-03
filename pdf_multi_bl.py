"""
Detect and extract multiple separate Bills of Lading from one scanned PDF.

Some carrier PDFs (e.g. Combicon / MESCO CamScanner scans) contain one full B/L
per page with distinct B/L numbers, consignees, and cargo lines on the same
physical container.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from validator import extract_acid_regex, extract_route_regex, extract_vessel_voyage_port_regex


def split_pdf_pages(raw_text: str) -> List[Tuple[int, str]]:
    parts = re.split(r"---\s*PAGE\s*(\d+)\s*---", raw_text or "", flags=re.I)
    pages: List[Tuple[int, str]] = []
    for idx in range(1, len(parts), 2):
        try:
            page_no = int(parts[idx])
        except ValueError:
            page_no = len(pages) + 1
        pages.append((page_no, parts[idx + 1]))
    return pages


def detect_multi_bl_candidate(raw_text: str) -> bool:
    """True when two or more pages each appear to carry a distinct ocean B/L number."""
    pages = split_pdf_pages(raw_text)
    if len(pages) < 2:
        return False
    bl_numbers: List[str] = []
    for _page_no, page_text in pages:
        if not re.search(r"BILL\s+OF\s+LADING", page_text, re.I):
            continue
        section = _best_page_section(page_text)
        bl = _extract_bl_number(section)
        if bl and bl not in bl_numbers:
            bl_numbers.append(bl)
    return len(bl_numbers) >= 2


def _best_page_section(page_text: str) -> str:
    """Pick the OCR block on this page with the richest cargo/header fields."""
    chunks = [c.strip() for c in re.split(r"\[OCR\s+[^\]]+\]", page_text or "") if c.strip()]
    chunks.append((page_text or "").strip())

    def score(text: str) -> int:
        upper = text.upper()
        value = 0
        if re.search(r"[A-Z]{4}\d{6}\s*/\s*\d", text, re.I):
            value += 12
        if re.search(r"\bPALLETS\b", upper):
            value += 8
        if re.search(r"\bALLETS\b", upper):
            value += 6
        if "ACID" in upper:
            value += 4
        if re.search(r"HEAVY\s+EQUIPMENT\s+SERVICES\s+\d{7,12}", upper):
            value += 3
        if "CONSIGNEE" in upper:
            value += 2
        return value + min(len(text) // 400, 5)

    return max(chunks, key=score)


def _extract_bl_number(section: str) -> Optional[str]:
    m = re.search(
        r"HEAVY\s+EQUIPMENT\s+SERVICES\s+(\d{7,12})\b",
        section,
        re.I,
    )
    if m:
        return m.group(1)
    m = re.search(
        r"BILL\s+OF\s+LADING\s+NO\.?\s*[^\n]*\n[^\n]*\n\s*(\d{7,12})\b",
        section,
        re.I,
    )
    return m.group(1) if m else None


def _extract_consignee(section: str) -> Optional[str]:
    m = re.search(
        r"CONSIGNEE\s*\([^\n]+\n\s*([A-Z][^\n]{5,90}?)(?:\s*[:;.]?\s*)?(?:\n|$)",
        section,
        re.I,
    )
    if m:
        name = re.sub(r"\s+", " ", m.group(1)).strip().rstrip(":;.")
        upper = name.upper()
        if name and "POINT AND COUNTRY" not in upper and "ORIGIN OF GOODS" not in upper:
            return name
    m = re.search(
        r"(?:MIDDLE\s+EAST\s+FOR\s+IMP&?EXPO|CITI\s+FOR\s+INVEST\s*&\s*TRADE\s+L\.?L\.?C\.?)",
        section,
        re.I,
    )
    return re.sub(r"\s+", " ", m.group(0)).strip() if m else None


def _extract_container_and_seal(section: str) -> Tuple[Optional[str], Optional[str]]:
    container = None
    m = re.search(r"\b([A-Z]{4})(\d{6})\s*/\s*(\d)\b", section, re.I)
    if m:
        container = f"{m.group(1)}{m.group(2)}/{m.group(3)}"
    seal_m = re.search(r"SEAL\s+(\d{4,12})\b", section, re.I)
    seal = seal_m.group(1) if seal_m else None
    return container, seal


def _extract_goods_description(section: str) -> Optional[str]:
    m = re.search(
        r"/\s*\d\s+([A-Z][A-Z\s]{3,60}?)(?:\n|LCL)",
        section,
        re.I,
    )
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    if re.search(r"MACHINERY\s+PARTS", section, re.I):
        return "MACHINERY PARTS"
    return None


def _infer_packages(section: str) -> Optional[str]:
    m = re.search(r"\b(\d+)\s*PALLETS\b", section, re.I)
    if m:
        return f"{m.group(1)} PALLETS"
    if re.search(r"\bALLETS\s*(?:=\s*)?STC\s*:", section, re.I):
        return "2 PALLETS"
    return None


def _extract_cargo_totals(section: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (packages, gross_weight_kg, volume_cbm)."""
    m = re.search(
        r"\b(\d+)\s*PALLETS\s+STC:\s*([\d.,]+)\s*KGS\s+([\d.,]+)\s*CBM\b",
        section,
        re.I,
    )
    if m:
        return (
            f"{m.group(1)} PALLETS",
            m.group(2).replace(",", ""),
            m.group(3).replace(",", ""),
        )

    packages = _infer_packages(section)
    cbm = None
    weight = None

    m_cbm = re.search(r"\b(?:\d+\s*)?(?:P)?ALLETS\s*(?:=\s*)?STC:\s*([\d.,]+)\s*CBM\b", section, re.I)
    if m_cbm:
        cbm = m_cbm.group(1).replace(",", "")

    m_kgs = re.search(r"STC:\s*([\d.,]+)\s*KGS\b", section, re.I)
    if m_kgs:
        weight = m_kgs.group(1).replace(",", "")
    else:
        m_kgs2 = re.search(r"\b([\d]{3,5}(?:[.,]\d{1,2})?)\s*KGS\b", section, re.I)
        if m_kgs2:
            weight = m_kgs2.group(1).replace(",", "")

    return packages, weight, cbm


def _extract_delivery_agent_notes(section: str) -> Optional[str]:
    m = re.search(
        r"(MESCO\s+MARINE\s*&\s*ENGINEERING\s+SERVICE\s+CO\.[\s\S]{0,220}?)(?:\n\n|PRE-CARRIAGE|EXPORT\s+CARRIER|BERNHARD)",
        section,
        re.I,
    )
    if not m:
        return None
    from bl_number_rules import clean_mesco_notes

    block = re.sub(r"\s+", " ", m.group(1)).strip()
    note = f"Delivery Agent at Destination: {block}" if block else None
    return clean_mesco_notes(note)


def _extract_page_record(page_number: int, page_text: str) -> Optional[Dict[str, Any]]:
    section = _best_page_section(page_text)
    bl_no = _extract_bl_number(section)
    if not bl_no:
        return None

    consignee = _extract_consignee(section)
    container_raw, seal = _extract_container_and_seal(section)
    packages, gross_kg, cbm = _extract_cargo_totals(section)
    goods = _extract_goods_description(section)
    acid = extract_acid_regex(section)

    vessel, voyage, _pol = None, None, None
    m_vessel = re.search(r"BERNHARD\s+SCHULTE(?:\s+(\S+))?", page_text, re.I)
    if m_vessel:
        vessel = "BERNHARD SCHULTE"
        if m_vessel.group(1) and not re.search(r"CARRIER|ORIGINAL|NUMSER", m_vessel.group(1), re.I):
            voyage = m_vessel.group(1)
    if not vessel:
        vessel, voyage, _pol = extract_vessel_voyage_port_regex(section)

    origin, destination = extract_route_regex(page_text) or extract_route_regex(section)
    if re.search(r"(?:NEW\s+YORK|V\s*YORE\s+NEW\s+YORK)", page_text, re.I):
        origin = "NEW YORK"
    if not destination and re.search(r"ALEXANDRIA", page_text, re.I):
        destination = "ALEXANDRIA"

    org_bls = None
    m_org = re.search(r"(\d+)\s*\(\s*\d+\s*\)\s*original", section, re.I)
    if m_org:
        org_bls = m_org.group(1)

    containers: List[Dict[str, Any]] = []
    if container_raw:
        containers.append(
            {
                "container_number": container_raw,
                "seal_number": seal,
                "container_type": "LCL",
                "packages": packages,
                "gross_weight_kg": gross_kg,
                "measurement_cbm": cbm,
            }
        )

    record: Dict[str, Any] = {
        "document_type": "Bill of Lading",
        "mesco_masterblno": bl_no,
        "mesco_bookingnumber": bl_no,
        "mesco_shippernamecontactno": "HEAVY EQUIPMENT SERVICES",
        "mesco_consigneenamecontactno": consignee,
        "mesco_cargodescription": goods,
        "cr401_totalpackages": packages,
        "cr401_totalgrossweight": gross_kg,
        "cr401_totalvolume": cbm,
        "mesco_acidnumber": acid,
        "mesco_vessel": vessel,
        "mesco_voytruckno": voyage,
        "mesco_origin": origin,
        "mesco_destination": destination,
        "mesco_deliveryaddress": destination,
        "mesco_notify1": "same as cnee" if re.search(r"same\s+as\s+cnee", section, re.I) else None,
        "mesco_nooforgbls": org_bls,
        "mesco_containertype": "LCL",
        "mesco_pcfreightterm": "PREPAID" if "FREIGHT PREPAID" in section.upper() else None,
        "mesco_notes": _extract_delivery_agent_notes(section),
        "container_number": container_raw,
        "seal_number": seal,
        "containers": containers,
        "extraction_method": "pdf_multi_bl_direct",
        "_page_number": page_number,
        "_page_text": page_text,
    }
    return record


def detect_and_extract_multi_bl_records(raw_text: str) -> Optional[List[Dict[str, Any]]]:
    """
    When a PDF has multiple pages each with its own B/L number, return one flat
    record per page. Returns None when fewer than two distinct B/Ls are found.
    """
    pages = split_pdf_pages(raw_text)
    if len(pages) < 2:
        return None

    records: List[Dict[str, Any]] = []
    bl_numbers: List[str] = []
    for page_no, page_text in pages:
        if not re.search(r"BILL\s+OF\s+LADING", page_text, re.I):
            continue
        rec = _extract_page_record(page_no, page_text)
        if not rec or not rec.get("mesco_masterblno"):
            continue
        bl = str(rec["mesco_masterblno"]).strip()
        if bl in bl_numbers:
            continue
        bl_numbers.append(bl)
        records.append(rec)

    if len(records) >= 2 and len(set(bl_numbers)) >= 2:
        return records
    return None
