"""Direct parser for standard single House B/L PDFs.

This is a no-LLM fallback for readable House B/L forms that contain enough
shipping evidence to create a House operation and link it to a Master by
container/seal + vessel/voyage + route/date.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from validator import extract_acid_regex, extract_house_bl_number_regex


_HOUSE_HINT_RE = re.compile(
    r"\b(?:HOUSE\s+B/?L|H\s*/?\s*BL|CFS\s*/\s*CFS|AS\s+AGENT\s+TO\s+CARRIER)\b",
    re.I,
)
_TPALX_RE = re.compile(r"\b(TPALX\d{6,10})\b", re.I)
_CONTAINER_RE = re.compile(
    r"\b([A-Z]{4}\d{7})\s*/\s*([A-Z0-9]{4,15})\s*/\s*(20|40)\s*(?:HQ|HC|GP|DV|FT)?\b",
    re.I,
)
_VESSEL_VOYAGE_RE = re.compile(
    r"\b([A-Z][A-Z0-9 ]{3,40}?)\s*\n\s*([A-Z0-9]{6,12})\s*\n\s*"
    r"([A-Z][A-Z ,/]+?)\s*\n\s*([A-Z][A-Z ,/]+?)\s*\n\s*([A-Z][A-Z ,/]+?)\b",
    re.I,
)
_INLINE_VESSEL_RE = re.compile(
    r"\b([A-Z][A-Z0-9 ]{3,40}?)\s+([A-Z0-9]{6,12})\s+"
    r"([A-Z][A-Z ,/]+?)\s+([A-Z][A-Z ,/]+?)\s+([A-Z][A-Z ,/]+?)\b",
    re.I,
)
_TOTALS_RE = re.compile(
    r"\b([A-Z]{2,}\d{4,})\s+SAID\s+TO\s+CONTAIN\w*:\s*"
    r"(\d+)\s+([A-Z]+)\s+IN\s+TOTAL\s+([\d,]+(?:\.\d+)?)\s+([\d,]+(?:\.\d+)?)",
    re.I,
)
_HS_RE = re.compile(r"\bH\.?S\.?\s*CODE\s*:?\s*([0-9]{6,12})", re.I)
_EXPORTER_ID_RE = re.compile(r"\bFOREIGN\s+EXPORTER\s+ID\s*:?\s*([A-Z0-9]{6,25})", re.I)
_EXPORTER_COUNTRY_RE = re.compile(r"\bFOREIGN\s+EXPORTER\s+COUNTRY\s*:?\s*([A-Z][A-Z ]+)", re.I)
_IMPORTER_TAX_RE = re.compile(r"\bEGYPTIAN\s+IMPORTER\s+TAX\s+ID\s*:?\s*(\d{6,15})", re.I)


def _block_order_section(text: str) -> str:
    m = re.search(r"\[BLOCK ORDER\]\s*(.*)", text or "", re.I | re.S)
    return m.group(1) if m else (text or "")


def _visual_section(text: str) -> str:
    m = re.search(r"\[VISUAL WORD ORDER\]\s*(.*?)(?:\[BLOCK ORDER\]|\Z)", text or "", re.I | re.S)
    return m.group(1) if m else (text or "")


def _clean(value: Any, max_len: Optional[int] = None) -> Optional[str]:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip(" ,;:-")
    if not text:
        return None
    return text[:max_len] if max_len else text


def _normalize_port(value: Optional[str]) -> Optional[str]:
    text = _clean(value)
    if not text:
        return None
    text = text.replace("/", ",")
    text = re.sub(r"\s*,\s*", ", ", text)
    return text


def _parse_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = re.sub(r"[^\d.,]", "", str(value))
    if not text:
        return None
    if "," in text and "." in text:
        text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _parse_bl_date(value: str) -> Optional[str]:
    text = _clean(value)
    if not text:
        return None
    for fmt in ("%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text.upper(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _extract_hbl(text: str) -> Optional[str]:
    hbl = extract_house_bl_number_regex(text)
    if hbl:
        return hbl.upper()
    m = _TPALX_RE.search(text or "")
    return m.group(1).upper() if m else None


def _extract_party_blocks(text: str, hbl: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    visual = _visual_section(text)
    lines = [ln.strip() for ln in visual.splitlines() if ln.strip()]
    shipper_name = shipper_address = consignee_name = consignee_address = None

    ship_idx = next(
        (i for i, ln in enumerate(lines) if re.search(r"\b[A-Z0-9 .,&()'-]{4,}\b(?:CO\.?,?\s*LTD|LIMITED|LTD)\b", ln, re.I)),
        None,
    )
    if ship_idx is not None:
        shipper_name = _clean(lines[ship_idx], 100)
        addr: List[str] = []
        for ln in lines[ship_idx + 1:ship_idx + 7]:
            if hbl and hbl in ln.upper():
                break
            if re.search(r"\b(TEL|FAX|VAT|EMAIL)\b", ln, re.I):
                continue
            if re.search(r"\b(AL\s+SAAD|SAME\s+AS\s+CONSIGNEE|SHANGHAI|CMA\s+CGM)\b", ln, re.I):
                break
            addr.append(ln)
        shipper_address = _clean(", ".join(addr), 250)

    consignee_idx = next(
        (i for i, ln in enumerate(lines) if re.search(r"\bAL\s+SAAD\b", ln, re.I)),
        None,
    )
    if consignee_idx is not None:
        consignee_name = _clean(lines[consignee_idx], 100)
        addr = []
        for ln in lines[consignee_idx + 1:consignee_idx + 7]:
            if re.search(r"\b(SAME\s+AS\s+CONSIGNEE|MARINE\s*&|MESCO|SHANGHAI|CMA\s+CGM)\b", ln, re.I):
                break
            if re.search(r"\b(TEL|FAX|EMAIL)\b", ln, re.I):
                continue
            addr.append(ln)
        consignee_address = _clean(", ".join(addr), 250)

    return shipper_name, shipper_address, consignee_name, consignee_address


def _extract_mesco_notify(text: str) -> Tuple[Optional[str], Optional[str]]:
    visual = _visual_section(text)
    m = re.search(
        r"(MARINE\s*&\s*ENGINEERING\s+SERVICES\s*COMPANY\s*-\s*MESCO)\s*\n"
        r"(?P<addr>.*?)(?:\nTAXNO|\nSHANGHAI|\nCMA\s+CGM)",
        visual,
        re.I | re.S,
    )
    if not m:
        return None, None
    name = _clean(m.group(1), 100)
    addr_lines = [
        ln.strip()
        for ln in m.group("addr").splitlines()
        if ln.strip() and not re.search(r"\b(TEL|FAX)\b", ln, re.I)
    ]
    return name, _clean(", ".join(addr_lines), 250)


def _extract_route_vessel(text: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    block = _block_order_section(text)
    m = _VESSEL_VOYAGE_RE.search(block)
    if not m:
        visual = _visual_section(text)
        m = _INLINE_VESSEL_RE.search(visual)
    if not m:
        return None, None, None, None
    vessel = _clean(m.group(1), 50)
    voyage = _clean(m.group(2), 30)
    pol = _normalize_port(m.group(3))
    pod = _normalize_port(m.group(4))
    return pol, pod, vessel, voyage


def _extract_container(text: str) -> Optional[Dict[str, Any]]:
    m = _CONTAINER_RE.search(text or "")
    if not m:
        return None
    container_type = f"{m.group(3)}HQ" if m.group(3) == "40" else f"{m.group(3)}FT"
    return {
        "container_number": m.group(1).upper(),
        "seal_number": m.group(2).upper(),
        "container_type": container_type,
    }


def _extract_totals_and_goods(text: str) -> Tuple[Optional[int], Optional[str], Optional[float], Optional[float], Optional[str]]:
    visual = _visual_section(text)
    m = _TOTALS_RE.search(visual)
    if not m:
        return None, None, None, None, None

    packages = int(m.group(2))
    package_unit = m.group(3).upper()
    gross = _parse_float(m.group(4))
    volume = _parse_float(m.group(5))

    after = visual[m.end():]
    goods_lines: List[str] = []
    for ln in after.splitlines():
        s = ln.strip()
        if not s:
            continue
        if re.search(r"\b(HS\s*CODE|ACID|EGYPTIAN\s+IMPORTER|FOREIGN\s+EXPORTER|CSNU|CFS/CFS|ONLY)\b", s, re.I):
            break
        if len(s) >= 4:
            goods_lines.append(s)
        if len(goods_lines) >= 3:
            break
    goods = _clean(" ".join(goods_lines), 500)
    return packages, package_unit, gross, volume, goods


def _extract_dates(text: str) -> Tuple[Optional[str], Optional[str]]:
    visual = _visual_section(text)
    dates = re.findall(r"\b(\d{1,2}\s+[A-Z]{3,9}\s+\d{4})\b", visual, re.I)
    parsed = [_parse_bl_date(d) for d in dates]
    parsed = [d for d in parsed if d]
    if not parsed:
        return None, None
    return parsed[-2] if len(parsed) >= 2 else parsed[0], parsed[-1]


def is_standard_house_bl(text: str) -> bool:
    if not text or not text.strip():
        return False
    if not _extract_hbl(text):
        return False
    upper = text.upper()
    return bool(_HOUSE_HINT_RE.search(upper) or "SAID TO CONTAIN" in upper)


def parse_standard_house_bl(text: str) -> Optional[Dict[str, Any]]:
    if not is_standard_house_bl(text):
        return None

    hbl = _extract_hbl(text)
    if not hbl:
        return None

    shipper, shipper_addr, consignee, consignee_addr = _extract_party_blocks(text, hbl)
    notify, notify_addr = _extract_mesco_notify(text)
    pol, pod, vessel, voyage = _extract_route_vessel(text)
    container = _extract_container(text)
    packages, package_unit, gross, volume, goods = _extract_totals_and_goods(text)
    shipped_on_board, date_of_issue = _extract_dates(text)

    record: Dict[str, Any] = {
        "document_type": "House Bill of Lading",
        "extraction_method": "pdf_standard_house_direct",
        "mesco_houseblno": hbl,
        "mesco_transporttype": 300000000,
        "mesco_loadtype": 300000001,
        "mesco_direction": 300000000,
        "mesco_consolidation": True,
        "mesco_pcfreightterm": "COLLECT" if re.search(r"\bCOLLECT\b", text, re.I) else None,
    }

    if shipper:
        record["mesco_shippernamecontactno"] = shipper
        record["mesco_shipper"] = shipper
    if shipper_addr:
        record["mesco_shipperaddress"] = shipper_addr
    if consignee:
        record["mesco_consigneenamecontactno"] = consignee
        record["mesco_consignee"] = consignee
    if consignee_addr:
        record["mesco_consigneeaddress"] = consignee_addr
    if notify:
        record["mesco_notify1"] = notify
    if notify_addr:
        record["mesco_notifyaddress"] = notify_addr
    if pol:
        record["mesco_origin"] = pol
    if pod:
        record["mesco_destination"] = pod
        record["mesco_deliveryaddress"] = pod
    if vessel:
        record["mesco_vessel"] = vessel
    if voyage:
        record["mesco_voytruckno"] = voyage
    if packages is not None:
        record["cr401_totalpackages"] = packages
    if gross is not None:
        record["cr401_totalgrossweight"] = gross
    if volume is not None:
        record["cr401_totalvolume"] = volume
    if shipped_on_board:
        record["mesco_shippedonboarddate"] = shipped_on_board
    if date_of_issue:
        record["mesco_dateofissue"] = date_of_issue

    if container:
        if packages is not None:
            container["packages"] = packages
        if gross is not None:
            container["gross_weight_kg"] = gross
        if volume is not None:
            container["measurement_cbm"] = volume
        record["container_number"] = container["container_number"]
        record["seal_number"] = container["seal_number"]
        record["mesco_containertype"] = container["container_type"]
        record["containers"] = [container]

    hscode = _HS_RE.search(text or "")
    if hscode:
        record["mesco_hscode"] = hscode.group(1)
    acid = extract_acid_regex(text)
    if acid:
        record["mesco_acidnumber"] = acid
    imp = _IMPORTER_TAX_RE.search(text or "")
    if imp:
        record["mesco_importerstaxno"] = imp.group(1)
    exp = _EXPORTER_ID_RE.search(text or "")
    if exp:
        record["mesco_foreignsupplierregistrationnumber"] = exp.group(1)
    country = _EXPORTER_COUNTRY_RE.search(text or "")
    if country:
        record["mesco_countryoforigin"] = _clean(country.group(1), 80)

    desc_parts = []
    ref = re.search(r"\b([A-Z]{2,}\d{4,})\b\s+SAID\s+TO\s+CONTAIN", text or "", re.I)
    if ref:
        desc_parts.append(ref.group(1).upper())
    if goods:
        desc_parts.append(goods)
    if package_unit:
        record["cargo_type"] = package_unit
    if hscode:
        desc_parts.append(f"HS CODE: {hscode.group(1)}")
    if container:
        desc_parts.append(
            f"Container {container['container_number']} / Seal {container['seal_number']} / {container['container_type']}"
        )
    record["mesco_cargodescription"] = _clean(" ".join(desc_parts), 1200)

    cargo: Dict[str, Any] = {}
    if record.get("mesco_cargodescription"):
        cargo["mesco_descriptionofgoods"] = record["mesco_cargodescription"]
    if packages is not None:
        cargo["mesco_noofpackages"] = packages
    if gross is not None:
        cargo["mesco_grosskg"] = gross
    if volume is not None:
        cargo["mesco_volcbm"] = volume
    if cargo:
        record["cargo_lines"] = [cargo]

    return {k: v for k, v in record.items() if v is not None and v != ""}
