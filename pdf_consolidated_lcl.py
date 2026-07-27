"""
Consolidated LCL multi-HBL PDFs: one master B/L + one shared container + N house B/Ls.

Typical layouts:
  - MESCO NAV26MU* house B/L forms (CamScanner multi-page under one ocean MBL)
  - ISALY / ISTALY-* draft house forms sharing a container
  - Page 2+ manifest tables listing multiple HBL rows under one container

Distinct from Combicon-style multi-page scans where each page is an independent
ocean B/L (numeric booking numbers, same container but separate masters).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from pdf_isaly_draft_bl import _parse_page_record as parse_isaly_page_record
from pdf_isaly_draft_bl import detect_isaly_draft_multi_bl
from pdf_multi_bl import split_pdf_pages
from validator import extract_acid_regex, extract_route_regex, extract_vessel_voyage_port_regex

# House B/L prefixes that indicate consolidation (not standalone ocean B/Ls).
HOUSE_BL_PREFIX_RE = re.compile(
    r"^(?:NAV\d+MU\d{3,6}|ISALY\d{5,12}|ISTALY-\d{4,8})$",
    re.I,
)

NAV26MU_RE = re.compile(r"\b(NAV\d+MU\d{3,6})\b", re.I)
ISALY_RE = re.compile(r"\b(ISALY\d{5,12})\b", re.I)
ISTALY_RE = re.compile(r"\b(ISTALY-\d{4,8})\b", re.I)

MASTER_MBL_RE = re.compile(
    r"\b(?:MASTER\s+B/?L\s*(?:NO\.?)?\s*:?\s*)?(NSA\d{6,12}|ISTE\d{4,12}|MS\d{6,8})\b",
    re.I,
)
CONTAINER_RE = re.compile(r"\b([A-Z]{4})(\d{6})\s*/\s*(\d)\b", re.I)
CONTAINER_HC_RE = re.compile(
    r"\b([A-Z]{4})(\d{7})\b.*?(?:40\s*['\u2019]?\s*HC|40HC)",
    re.I,
)
SEAL_RE = re.compile(r"SEAL\s+(\d{4,12})\b", re.I)
LCL_MARKER_RE = re.compile(r"\bLCL\s*/\s*LCL\b|\bLCLYLCL\b", re.I)
CONSOLIDATION_RE = re.compile(r"\bN/?M\s+CONSOLIDATION\b", re.I)

PKG_WEIGHT_CBM_RE = re.compile(
    r"\b(\d+)\s+(?:DRUMS?|PALLETS?|PACKAGES?|HDPE[^\n]{0,20}?)\b"
    r"[\s\S]{0,120}?"
    r"([\d,]+(?:\.\d+)?)\s*(?:KG|KGS)\b"
    r"[\s\S]{0,40}?"
    r"([\d,]+(?:\.\d+)?)\s*CBM\b",
    re.I,
)

MANIFEST_ROW_RE = re.compile(
    r"\b(NAV\d+MU\d{3,6}|ISALY\d{5,12}|ISTALY-\d{4,8})\b"
    r"[\s\S]{0,200}?"
    r"([\d,]+(?:\.\d+)?)\s*(?:KG|KGS)\b"
    r"[\s\S]{0,40}?"
    r"([\d,]+(?:\.\d+)?)\s*CBM\b",
    re.I,
)


def _normalize_container(prefix: str, digits: str, check: str) -> str:
    return f"{prefix.upper()}{digits}/{check}"


def _extract_containers_from_text(text: str) -> List[str]:
    found: List[str] = []
    for m in CONTAINER_RE.finditer(text or ""):
        c = _normalize_container(m.group(1), m.group(2), m.group(3))
        if c not in found:
            found.append(c)
    for m in CONTAINER_HC_RE.finditer(text or ""):
        c = m.group(1).upper() + m.group(2)
        if c not in found:
            found.append(c)
    return found


def _is_prefixed_house_bl(bl: str) -> bool:
    return bool(HOUSE_BL_PREFIX_RE.match((bl or "").strip()))


def _collect_distinct_house_bls(raw_text: str) -> List[str]:
    """All distinct prefixed house B/L numbers anywhere in the document."""
    seen: List[str] = []
    for pattern in (NAV26MU_RE, ISALY_RE, ISTALY_RE):
        for m in pattern.finditer(raw_text or ""):
            bl = m.group(1).upper()
            if bl not in seen:
                seen.append(bl)
    return seen


def extract_document_master_mbl(raw_text: str) -> Optional[str]:
    """Ocean / master B/L when present and distinct from house B/L prefixes."""
    house_bls = set(_collect_distinct_house_bls(raw_text))
    for m in MASTER_MBL_RE.finditer(raw_text or ""):
        candidate = m.group(1).upper()
        if candidate not in house_bls:
            return candidate
    return None


def is_consolidated_lcl_multi_hbl(raw_text: str) -> bool:
    """
    True when the PDF is a consolidated LCL container with 2+ distinct house B/Ls.
    Excludes Combicon numeric-only multi-page scans (separate ocean B/Ls).
    """
    if not raw_text:
        return False
    if detect_isaly_draft_multi_bl(raw_text):
        return True

    house_bls = _collect_distinct_house_bls(raw_text)
    if len(house_bls) < 2:
        return False

    if any(_is_prefixed_house_bl(h) for h in house_bls):
        return True

    if extract_document_master_mbl(raw_text):
        return True

    if CONSOLIDATION_RE.search(raw_text):
        return True

    containers = _extract_containers_from_text(raw_text)
    if len(containers) == 1 and LCL_MARKER_RE.search(raw_text):
        # Same container + LCL — only consolidate when house B/Ls are prefixed,
        # not pure numeric Combicon booking numbers.
        if not all(re.fullmatch(r"\d{7,12}", h, re.I) for h in house_bls):
            return True

    return False


def _extract_packages_weight_cbm(section: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    m = re.search(
        r"\b(\d+)\s+(DRUMS?|PALLETS?|PACKAGES?|HDPE[^\n]{0,30})\b",
        section,
        re.I,
    )
    packages = None
    if m:
        unit = m.group(2).upper()
        if not unit.endswith("S") and unit.endswith("M"):
            unit += "S"
        packages = f"{m.group(1)} {unit}"

    gross = None
    m_kg = re.search(r"([\d,]+(?:\.\d+)?)\s*(?:KG|KGS)\b", section, re.I)
    if m_kg:
        gross = m_kg.group(1).replace(",", "")

    cbm = None
    m_cbm = re.search(r"([\d,]+(?:\.\d+)?)\s*CBM\b", section, re.I)
    if m_cbm:
        cbm = m_cbm.group(1).replace(",", "")

    return packages, gross, cbm


def _extract_consignee(section: str) -> Optional[str]:
    m = re.search(
        r"CONSIGNEE\s*\([^\n]*\n\s*([A-Z][^\n]{5,100}?)(?:\n|$)",
        section,
        re.I,
    )
    if m:
        name = re.sub(r"\s+", " ", m.group(1)).strip().rstrip(":;.")
        if name and "POINT AND COUNTRY" not in name.upper():
            return name[:120]
    for label in (
        r"MARINE\s+AND\s+ENGINEERING\s+SERVICES\s+COMPANY",
        r"EURO\s+SHIPPING\s+EGYPT",
    ):
        m2 = re.search(label, section, re.I)
        if m2:
            return re.sub(r"\s+", " ", m2.group(0)).strip()[:120]
    return None


def _extract_cargo_description(section: str) -> Optional[str]:
    m = re.search(
        r"(?:DESCRIPTION\s+OF\s+(?:PACKAGES\s+AND\s+)?GOODS|STC:)\s*[:\n]?\s*([^\n]{10,200})",
        section,
        re.I,
    )
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()[:500]
    m2 = re.search(
        r"(Trimethoprim|Cellulose\s+Acetate|Essential\s+Oils|Moulded\s+Rubber|V-Belts|Gear\s+Coupling)[^\n]{0,120}",
        section,
        re.I,
    )
    if m2:
        return re.sub(r"\s+", " ", m2.group(0)).strip()[:500]
    return None


def _parse_nav26mu_page(page_no: int, page_text: str) -> Optional[Dict[str, Any]]:
    section = page_text or ""
    m_hbl = NAV26MU_RE.search(section)
    if not m_hbl:
        return None
    hbl = m_hbl.group(1).upper()

    packages, gross, cbm = _extract_packages_weight_cbm(section)
    consignee = _extract_consignee(section)
    cargo = _extract_cargo_description(section)
    acid = extract_acid_regex(section)

    container_raw, seal = None, None
    m_ctr = CONTAINER_RE.search(section)
    if m_ctr:
        container_raw = _normalize_container(m_ctr.group(1), m_ctr.group(2), m_ctr.group(3))
    else:
        m_hc = CONTAINER_HC_RE.search(section)
        if m_hc:
            container_raw = m_hc.group(1).upper() + m_hc.group(2)
    m_seal = SEAL_RE.search(section)
    if m_seal:
        seal = m_seal.group(1)

    vessel, voyage, pol = extract_vessel_voyage_port_regex(section)
    origin, destination = extract_route_regex(section) or (None, None)

    containers: List[Dict[str, Any]] = []
    if container_raw:
        containers.append(
            {
                "container_number": container_raw,
                "seal_number": seal,
                "container_type": "40HC",
                "packages": packages,
                "gross_weight_kg": gross,
                "measurement_cbm": cbm,
            }
        )

    return {
        "document_type": "Bill of Lading",
        "mesco_houseblno": hbl,
        "mesco_consigneenamecontactno": consignee,
        "mesco_cargodescription": cargo,
        "cr401_totalpackages": packages,
        "cr401_totalgrossweight": gross,
        "cr401_totalvolume": cbm,
        "mesco_acidnumber": acid,
        "mesco_vessel": vessel,
        "mesco_voytruckno": voyage,
        "mesco_origin": origin or pol,
        "mesco_destination": destination,
        "mesco_containertype": "40HC",
        "container_number": container_raw,
        "seal_number": seal,
        "containers": containers,
        "extraction_method": "pdf_consolidated_lcl_house",
        "_consolidated_lcl_row": True,
        "_page_number": page_no,
        "_page_text": page_text,
    }


def _parse_manifest_table_rows(raw_text: str, known: List[str]) -> List[Dict[str, Any]]:
    """Supplement page-anchored records with manifest-style row hits."""
    rows: List[Dict[str, Any]] = []
    seen = set(known)
    for m in MANIFEST_ROW_RE.finditer(raw_text or ""):
        hbl = m.group(1).upper()
        if hbl in seen:
            continue
        gross = m.group(2).replace(",", "")
        cbm = m.group(3).replace(",", "")
        seen.add(hbl)
        rows.append(
            {
                "document_type": "Bill of Lading",
                "mesco_houseblno": hbl,
                "cr401_totalgrossweight": gross,
                "cr401_totalvolume": cbm,
                "extraction_method": "pdf_consolidated_lcl_manifest_row",
                "_consolidated_lcl_row": True,
            }
        )
    return rows


def _normalize_house_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(rec)
    hbl = out.get("mesco_houseblno") or out.get("mesco_masterblno")
    if hbl:
        out["mesco_houseblno"] = str(hbl).strip().upper()
    mbl = out.get("mesco_masterblno")
    if mbl and out.get("mesco_houseblno") and str(mbl).upper() == str(out["mesco_houseblno"]).upper():
        out.pop("mesco_masterblno", None)
    out["_consolidated_lcl_row"] = True
    if not out.get("extraction_method"):
        out["extraction_method"] = "pdf_consolidated_lcl_house"
    return out


def _extract_house_records_from_pages(raw_text: str) -> List[Dict[str, Any]]:
    pages = split_pdf_pages(raw_text)
    if not pages:
        pages = [(1, raw_text)]

    records: List[Dict[str, Any]] = []
    seen_hbl: List[str] = []

    for page_no, page_text in pages:
        rec = parse_isaly_page_record(page_no, page_text)
        if not rec:
            rec = _parse_nav26mu_page(page_no, page_text)
        if not rec:
            continue
        rec = _normalize_house_record(rec)
        hbl = str(rec.get("mesco_houseblno") or "").strip().upper()
        if not hbl or hbl in seen_hbl:
            continue
        seen_hbl.append(hbl)
        records.append(rec)

    for extra in _parse_manifest_table_rows(raw_text, seen_hbl):
        hbl = str(extra.get("mesco_houseblno") or "").upper()
        if hbl and hbl not in seen_hbl:
            seen_hbl.append(hbl)
            records.append(extra)

    return records


def _aggregate_house_totals(houses: List[Dict[str, Any]]) -> Dict[str, Any]:
    gross = 0.0
    cbm = 0.0
    pallet_count = 0
    for rec in houses:
        try:
            if rec.get("cr401_totalgrossweight"):
                gross += float(str(rec["cr401_totalgrossweight"]).replace(",", ""))
        except (TypeError, ValueError):
            pass
        try:
            if rec.get("cr401_totalvolume"):
                cbm += float(str(rec["cr401_totalvolume"]).replace(",", ""))
        except (TypeError, ValueError):
            pass
        pkg = str(rec.get("cr401_totalpackages") or "")
        m = re.search(r"(\d+)\s+PALLETS?", pkg, re.I)
        if m:
            pallet_count += int(m.group(1))
        else:
            m2 = re.search(r"(\d+)\s+DRUMS?", pkg, re.I)
            if m2:
                pallet_count += int(m2.group(1))

    totals: Dict[str, Any] = {}
    if gross:
        totals["cr401_totalgrossweight"] = round(gross, 3) if gross % 1 else int(gross)
    if cbm:
        totals["cr401_totalvolume"] = round(cbm, 3) if cbm % 1 else int(cbm)
    if pallet_count:
        totals["cr401_totalpackages"] = f"{pallet_count} PALLETS"
    return totals


def build_consolidated_master_record(
    house_records: List[Dict[str, Any]],
    raw_text: str,
) -> Dict[str, Any]:
    first = house_records[0]
    master_mbl = extract_document_master_mbl(raw_text)

    containers = _extract_containers_from_text(raw_text)
    container_no = containers[0] if containers else first.get("container_number")
    seal = first.get("seal_number")
    for rec in house_records:
        if not seal and rec.get("seal_number"):
            seal = rec["seal_number"]
        if not container_no and rec.get("container_number"):
            container_no = rec["container_number"]

    totals = _aggregate_house_totals(house_records)
    shared_vessel = next((r.get("mesco_vessel") for r in house_records if r.get("mesco_vessel")), None)
    shared_voyage = next((r.get("mesco_voytruckno") for r in house_records if r.get("mesco_voytruckno")), None)
    shared_origin = next((r.get("mesco_origin") for r in house_records if r.get("mesco_origin")), None)
    shared_dest = next(
        (r.get("mesco_destination") for r in house_records if r.get("mesco_destination")),
        None,
    )

    container_items: List[Dict[str, Any]] = []
    if container_no:
        container_items.append(
            {
                "container_number": container_no,
                "seal_number": seal,
                "container_type": first.get("mesco_containertype") or "40HC",
                "packages": totals.get("cr401_totalpackages"),
                "gross_weight_kg": totals.get("cr401_totalgrossweight"),
                "measurement_cbm": totals.get("cr401_totalvolume"),
            }
        )

    master: Dict[str, Any] = {
        "document_type": "Consolidated LCL Container",
        "mesco_masterblno": master_mbl,
        "mesco_bookingnumber": master_mbl,
        "_mbl_masterblno": master_mbl,
        "mesco_vessel": shared_vessel,
        "mesco_voytruckno": shared_voyage,
        "mesco_origin": shared_origin,
        "mesco_destination": shared_dest,
        "mesco_deliveryaddress": shared_dest,
        "mesco_consolidation": True,
        "mesco_loadtype": 300000001,
        "mesco_containertype": first.get("mesco_containertype") or "40HC",
        "mesco_cargodescription": "Consolidated LCL container manifest",
        "mesco_nooforgbls": str(len(house_records)),
        "container_number": container_no,
        "seal_number": seal,
        "containers": container_items,
        "extraction_method": "pdf_consolidated_lcl_master",
        **totals,
    }
    return {k: v for k, v in master.items() if v is not None and v != ""}


def parse_consolidated_lcl_multi_hbl(raw_text: str) -> Optional[Dict[str, Any]]:
    """
    Parse a consolidated LCL PDF into one master + N house records.
    Returns None when fewer than two house B/Ls are found.
    """
    if not is_consolidated_lcl_multi_hbl(raw_text):
        return None

    house_records = _extract_house_records_from_pages(raw_text)
    if len(house_records) < 2:
        return None

    master_record = build_consolidated_master_record(house_records, raw_text)
    return {
        "master_record": master_record,
        "house_records": house_records,
        "house_bl_numbers": [str(r.get("mesco_houseblno")) for r in house_records],
    }
