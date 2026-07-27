"""
Parse STAR CONCORD / MESCO ISALY draft B/L PDFs (one house B/L form per page).

Layout (per page, [VISUAL WORD ORDER] block):
  Shipper block → Consignee block → SAME AS CONSIGNEE → M/V VESSEL/VOY POL
  → MARKS:ISALYxxxxxxx <packages> ACID: <19 digits>
  → IMPORTER/EXPORTER ID → cargo line → HS CODE:...
  → container row (ADMU5001200 ...)

Distinct ISALY numbers on 2+ pages → multi-B/L scan (not a single manifest).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from pdf_multi_bl import split_pdf_pages

ISALY_RE = re.compile(r"\b(ISALY\d{7,12})\b", re.I)

MARKS_ROW_RE = re.compile(
    r"MARKS:\s*(?P<hbl>ISALY\d+)\s+"
    r"(?P<pkgs>\d+\s+(?:PACKAGES|ROLLS|CARTONS?|PALLETS?))"
    r"(?:\s+\*\*\*[^*]+\*\*\*)?"
    r"[\s\S]*?"
    r"ACID:\s*(?P<acid>\d{19})\b",
    re.I,
)

CONTAINER_ROW_RE = re.compile(
    r"\b([A-Z]{4}\d{7})\s+(\d{4,12})\s+40['\u2019]?\s*HC\b",
    re.I,
)

VESSEL_RE = re.compile(
    r"M/V\s+(.+?)\s+(\d+)\s+(\w+)",
    re.I,
)

HS_LINE_RE = re.compile(
    r"HS\s*CODE\s*:?\s*([0-9./\s,]+)",
    re.I,
)

IMPORTER_RE = re.compile(r"IMPORTER\s*ID:\s*(\d+)", re.I)
EXPORTER_RE = re.compile(r"EXPORTER\s*ID:\s*([\d\s]+)", re.I)

CARGO_WEIGHT_RE = re.compile(
    r"^(.+?)\s+([\d,]+(?:\.\d+)?)\s*\(KGS\)\s*$",
    re.I | re.M,
)

COMPANY_LINE_RE = re.compile(
    r"(?:CO\.|LTD|LLC|INC|GARMENTS|TEKSTIL|MENSUCAT|SAN\s+VE|COMPANY|HOLDING|WAX|"
    r"ELECTRIC|BUTTONS?|FILM|LABEL|AMBALAJ|APPLIANCES|JOHNSON|BASKI)",
    re.I,
)

_ADDRESS_LINE_RE = re.compile(
    r"^\d+\s|KM\.|DESERT\s+ROAD|BELBIES|^\d{3,}|B\.CEKMECE|MAHALLESI|SANAYI\s+SITESI",
    re.I,
)


def _visual_section(page_text: str) -> str:
    m = re.search(
        r"\[VISUAL\s+WORD\s+ORDER\]\s*(.*?)(?=\[BLOCK\s+ORDER\]|---\s*PAGE|\Z)",
        page_text or "",
        re.I | re.S,
    )
    return (m.group(1) if m else page_text or "").strip()


def detect_isaly_draft_multi_bl(raw_text: str) -> bool:
    """True when 2+ pages each carry a distinct ISALY B/L in MARKS: row."""
    pages = split_pdf_pages(raw_text)
    if len(pages) < 2:
        return False
    bl_numbers: List[str] = []
    for _page_no, page_text in pages:
        visual = _visual_section(page_text)
        m = MARKS_ROW_RE.search(visual)
        if m:
            hbl = m.group("hbl").upper()
        else:
            m2 = re.search(r"MARKS:\s*(ISALY\d+)", visual, re.I)
            if not m2:
                continue
            hbl = m2.group(1).upper()
        if hbl not in bl_numbers:
            bl_numbers.append(hbl)
    return len(bl_numbers) >= 2


def _parse_hs_codes(visual: str) -> Optional[str]:
    codes: List[str] = []
    seen: set[str] = set()

    m = HS_LINE_RE.search(visual)
    if m:
        raw = m.group(1)
        dotted = re.findall(r"\d{4}\.\d{2}\.\d{2}\.\d{2}\.\d{2}", raw)
        for d in dotted:
            part = re.sub(r"\D", "", d)
            if 6 <= len(part) <= 12 and part not in seen:
                seen.add(part)
                codes.append(part)
        for part in re.split(r"[/,\s]+", raw):
            part = re.sub(r"\D", "", part)
            if len(part) < 6 or len(part) > 12:
                continue
            if part.startswith("20") and len(part) >= 10:
                continue
            if part in seen:
                continue
            seen.add(part)
            codes.append(part)

    if not codes:
        for line in visual.splitlines():
            upper = line.upper()
            if not re.search(r"BUTTON|COROZO|UREA|HS\s*CODE|TARIFF", upper):
                continue
            if re.search(r"EXPORTER\s+ID|IMPORTER\s+ID|ACID:", upper):
                continue
            for m in re.finditer(r"(?<![\d.])(\d{8,12})(?![\d])", line):
                part = m.group(1)
                if part.startswith("20") and len(part) >= 10:
                    continue
                if part not in seen:
                    seen.add(part)
                    codes.append(part)
    return "|".join(codes) if codes else None


def _parse_parties(visual: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Return shipper name, shipper address, consignee name, consignee address."""
    lines = [ln.strip() for ln in visual.splitlines() if ln.strip()]
    same_idx = next(
        (i for i, ln in enumerate(lines) if re.search(r"SAME\s+AS\s+CONSIGNEE", ln, re.I)),
        None,
    )
    vessel_idx = next(
        (i for i, ln in enumerate(lines) if re.search(r"^M/V\s+", ln, re.I)),
        len(lines),
    )
    header_end = same_idx if same_idx is not None else vessel_idx
    header = lines[:header_end]

    shipper_lines: List[str] = []
    consignee_lines: List[str] = []
    phase = "shipper"
    for ln in header:
        if re.search(r"^HTTP://|^WWW\.", ln, re.I):
            shipper_lines.append(ln)
            phase = "consignee"
            continue
        if (
            phase == "shipper"
            and len(shipper_lines) >= 2
            and COMPANY_LINE_RE.search(ln)
            and not _ADDRESS_LINE_RE.search(ln)
            and not re.search(r"TEKSTIL|MENSUCAT|AMBALAJ\s+A\.?S|BASKI", ln, re.I)
        ):
            phase = "consignee"
        if phase == "shipper":
            if re.search(r"DRAFT|SAME\s+AS", ln, re.I):
                continue
            shipper_lines.append(ln)
        else:
            if re.search(r"DRAFT|SAME\s+AS", ln, re.I):
                continue
            consignee_lines.append(ln)

    if not consignee_lines and len(header) >= 4:
        mid = len(header) // 2
        shipper_lines = header[:mid]
        consignee_lines = header[mid:]

    def _name(lines_in: List[str], *, consignee: bool = False) -> Optional[str]:
        skip_addr = re.compile(
            r"FREE\s+ZONE|PRIVATE\s+ZONE|PUBLIC\s+FREE|VAT\s+NR|^\d{3,}|^HTTP|@"
            r"|^TEL\s|LOGISTICS@|PORT\s+SAID|RAMADAN|BURSA|ISTANBUL|KAHRAMANMARAS",
            re.I,
        )
        for ln in lines_in:
            if skip_addr.search(ln) or _ADDRESS_LINE_RE.search(ln):
                continue
            if COMPANY_LINE_RE.search(ln):
                name = re.sub(r"\s+", " ", ln).strip()
                name = re.sub(r"\s+DRAFT\s*$", "", name, flags=re.I)
                return name[:100]
        if consignee:
            for ln in lines_in:
                if skip_addr.search(ln):
                    continue
                if len(ln) > 4 and re.search(r"[A-Z]{3}", ln):
                    return re.sub(r"\s+", " ", ln).strip()[:100]
        return re.sub(r"\s+", " ", lines_in[0]).strip()[:100] if lines_in else None

    shipper_name = _name(shipper_lines)
    consignee_name = _name(consignee_lines, consignee=True)

    if re.search(r"TO\s+THE\s+HOLDER\s+OF", visual, re.I):
        m_holder = re.search(
            r"TO\s+THE\s+HOLDER\s+OF\s*\n\s*([^\n]+)\s*\n\s*([^\n]+)",
            visual,
            re.I,
        )
        if m_holder:
            ref_line = re.sub(r"\s+DRAFT\s*$", "", m_holder.group(1).strip(), flags=re.I)
            name_line = re.sub(r"\s+DRAFT\s*$", "", m_holder.group(2).strip(), flags=re.I)
            if COMPANY_LINE_RE.search(name_line) or re.search(
                r"ELECTRIC|APPLIANCES|GARMENTS|WAX|BUTTON", name_line, re.I
            ):
                consignee_name = name_line[:100]
            else:
                consignee_name = f"TO THE HOLDER OF {ref_line}"[:100]

    shipper_addr = "\n".join(shipper_lines[1:6]) if len(shipper_lines) > 1 else None
    consignee_addr = "\n".join(consignee_lines[1:8]) if len(consignee_lines) > 1 else None
    return shipper_name, shipper_addr, consignee_name, consignee_addr


def _parse_cargo_description(visual: str) -> Optional[str]:
    m = CARGO_WEIGHT_RE.search(visual)
    if m:
        desc = re.sub(r"\s+", " ", m.group(1)).strip()
        if desc and not re.search(r"^EXPORTER\s+ID|^IMPORTER\s+ID", desc, re.I):
            return desc
    return None


def _parse_page_record(page_no: int, page_text: str) -> Optional[Dict[str, Any]]:
    visual = _visual_section(page_text)
    m_marks = MARKS_ROW_RE.search(visual)
    if not m_marks:
        m_hbl = re.search(r"MARKS:\s*(ISALY\d+)", visual, re.I)
        if not m_hbl:
            return None
        hbl = m_hbl.group(1).upper()
        pkgs = None
        acid = None
    else:
        hbl = m_marks.group("hbl").upper()
        pkgs = m_marks.group("pkgs").upper()
        acid = m_marks.group("acid")

    shipper, shipper_addr, consignee, consignee_addr = _parse_parties(visual)
    cargo = _parse_cargo_description(visual)
    hs = _parse_hs_codes(visual)

    gross = None
    cbm = None
    m_kgs = re.search(r"([\d,]+(?:\.\d+)?)\s*\(KGS\)", visual, re.I)
    if m_kgs:
        gross = m_kgs.group(1).replace(",", "")
    m_cbm = re.search(r"([\d,]+(?:\.\d+)?)\s*\(CBM\)", visual, re.I)
    if m_cbm:
        cbm = m_cbm.group(1).replace(",", "")

    if not pkgs:
        m_pkg = re.search(
            r"\b(\d+\s+(?:PACKAGES|ROLLS|CARTONS?|PALLETS?))\b",
            visual,
            re.I,
        )
        pkgs = m_pkg.group(1).upper() if m_pkg else None
    if pkgs and re.search(r"\bPALLET\b", pkgs, re.I) and not pkgs.endswith("S"):
        pkgs = pkgs + "S"

    unstackable = bool(re.search(r"\*\*\*UNSTACKABLE\*\*\*", visual, re.I))

    vessel, voyage, pol = None, None, None
    m_v = VESSEL_RE.search(visual)
    if m_v:
        vessel = re.sub(r"\s+", " ", m_v.group(1)).strip()
        voyage = m_v.group(2)
        pol = m_v.group(3).upper()

    dest = None
    if re.search(r"ALEXANDRIA", visual, re.I):
        dest = "ALEXANDRIA"

    container_no, seal = None, None
    m_ctr = CONTAINER_ROW_RE.search(visual)
    if m_ctr:
        container_no = m_ctr.group(1).upper()
        seal = m_ctr.group(2)

    importer = IMPORTER_RE.search(visual)
    exporter = EXPORTER_RE.search(visual)
    exporter_id = re.sub(r"\s+", "", exporter.group(1)) if exporter else None

    notes = None
    m_notes = re.search(
        r"(DON'T RELEASE THIS SHIPMENT[\s\S]{0,400}?SHIPPER'S ACCOUNT\.)",
        visual,
        re.I,
    )
    if m_notes:
        notes = re.sub(r"\s+", " ", m_notes.group(1)).strip()

    m_org = re.search(r"^(?:ZERO|ONE|TWO|THREE|FOUR|FIVE|SIX|SEVEN|EIGHT|NINE)\((\d+)\)", visual, re.I | re.M)
    org_bls = m_org.group(1) if m_org else "0"

    containers: List[Dict[str, Any]] = []
    if container_no:
        containers.append(
            {
                "container_number": container_no,
                "seal_number": seal,
                "container_type": "40HC",
                "packages": pkgs,
                "gross_weight_kg": gross,
                "measurement_cbm": cbm,
            }
        )

    record: Dict[str, Any] = {
        "document_type": "Bill of Lading",
        "mesco_masterblno": hbl,
        "mesco_houseblno": hbl,
        "mesco_bookingnumber": hbl,
        "mesco_shippernamecontactno": shipper,
        "mesco_shipperaddress": shipper_addr,
        "mesco_consigneenamecontactno": consignee,
        "mesco_consigneeaddress": consignee_addr,
        "mesco_notify1": "same as cnee"
        if re.search(r"SAME\s+AS\s+CONSIGNEE", visual, re.I)
        else None,
        "mesco_cargodescription": cargo,
        "mesco_hscode": hs,
        "cr401_totalpackages": pkgs,
        "cr401_totalgrossweight": gross,
        "cr401_totalvolume": cbm,
        "mesco_acidnumber": acid,
        "mesco_importerstaxno": importer.group(1) if importer else None,
        "mesco_foreignsupplierregistrationnumber": exporter_id,
        "mesco_vessel": vessel,
        "mesco_voytruckno": voyage,
        "mesco_origin": pol,
        "mesco_destination": dest,
        "mesco_deliveryaddress": dest,
        "mesco_containertype": "40HC",
        "mesco_pcfreightterm": "COLLECT"
        if re.search(r"FREIGHT\s+COLLECT", visual, re.I)
        else None,
        "mesco_nooforgbls": org_bls,
        "mesco_handlinginformation": "***UNSTACKABLE***" if unstackable else None,
        "unstackable": unstackable,
        "mesco_notes": notes,
        "mesco_routenotes": "CFS-CFS DESTINATION / OCEAN FREIGHT"
        if re.search(r"CFS-CFS", visual, re.I)
        else None,
        "mesco_shippingline": "MARINE & ENGINEERING SERVICES COMPANY (MESCO)"
        if re.search(r"MARINE\s*&\s*ENGINEERING", visual, re.I)
        else None,
        "mesco_notify2": "MARINE & ENGINEERING SERVICES COMPANY (MESCO)"
        if re.search(r"MARINE\s*&\s*ENGINEERING", visual, re.I)
        else None,
        "container_number": container_no,
        "seal_number": seal,
        "containers": containers,
        "extraction_method": "pdf_isaly_draft_direct",
        "_isaly_draft_row": True,
        "source_page": page_no,
        "_page_number": page_no,
        "_page_text": page_text,
    }
    return record


def extract_isaly_draft_records(raw_text: str) -> Optional[List[Dict[str, Any]]]:
    """One record per page with a distinct ISALY MARKS: row. None if < 2 pages."""
    pages = split_pdf_pages(raw_text)
    if len(pages) < 2:
        return None

    records: List[Dict[str, Any]] = []
    bl_numbers: List[str] = []
    for page_no, page_text in pages:
        rec = _parse_page_record(page_no, page_text)
        if not rec:
            continue
        hbl = str(rec.get("mesco_masterblno") or "").strip().upper()
        if not hbl or hbl in bl_numbers:
            continue
        bl_numbers.append(hbl)
        records.append(rec)

    if len(records) >= 2:
        return records
    return None
