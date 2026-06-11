"""
Direct parser for Arkas / ONE-style consolidation sea waybills.

These B/Ls reference cargo on an attached list (page 2+ may be image-only).
When the LLM returns malformed JSON, regex extraction still yields a master record.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

_MBL_RE = re.compile(r"\b(IST\d{9,12}|NSA\d{9,12}|ARK[A-Z]?\d{8,12})\b", re.I)
_CONTAINER_RE = re.compile(r"\b([A-Z]{4})\s*(\d{6,7})\b")
_TOTAL_RE = re.compile(
    r"TOTAL\s*:\s*(\d+)\s+PALLET.*?GW:\s*([\d,.\s]+)",
    re.I | re.S,
)
_ACID_RE = re.compile(r"ACID\s*:?\s*(\d{10,25})", re.I)
_IMPORTER_TAX_RE = re.compile(
    r"IMPORTER\s+TAX(?:ATION)?\s+NUMBER\s*:?\s*(\d{6,15})",
    re.I,
)
_EXPORTER_RE = re.compile(r"EXPORTER\s+NUMBER\s*:?\s*(\d{6,15})", re.I)
_VESSEL_VOYAGE_INLINE_RE = re.compile(
    r"\b([A-Z][A-Z0-9 ]{2,30}?)\s+([A-Z]{2,3}\d{2}[A-Z0-9]{2,6})\b",
)
_CONSOLIDATION_ROUTE_RE = re.compile(
    r"CONSOLIDATION\s+CARGO\s+ON\s+CARRIAGE\s+FROM\s+([^\n]+)",
    re.I,
)
_VESSEL_LINE_RE = re.compile(
    r"VESSEL\s*:\s*([A-Z][A-Z0-9 ]+?)\s*/\s*([A-Z0-9]{4,12})\b",
    re.I,
)
_CONTAINER_CARGO_RE = re.compile(
    r"[A-Z]{4}\s*\d{6,7}\s+(\d+)\s+(BOXES?|PALLETS?|CARTONS?)\s+(.+?)\s+GW\s*:\s*([\d,.\s]+)",
    re.I,
)
_SHIPPER_BLOCK_START_RE = re.compile(r"^S/\s+([^\n]+)", re.I | re.M)
_BAD_VESSEL_RE = re.compile(
    r"^(?:ZERO|FCL|LCL|FREE|PREPAID|COLLECT|NON[- ]NEG)",
    re.I,
)


def is_consolidation_sea_waybill(text: str) -> bool:
    if not text or not text.strip():
        return False
    upper = text.upper()
    sea_waybill = bool(
        re.search(r"NON[- ]NEGOTIABLE\s+SEA\s+WAY\s+BILL", upper)
        or re.search(r"SEA\s+WAY\s+BILL\s+OF\s+LADING", upper)
    )
    consolidation = "CONSOLIDATION CARGO" in upper or "AS PER ATTACHED LIST" in upper
    arkas = "ARKAS" in upper and _MBL_RE.search(text)
    return (sea_waybill and consolidation) or (arkas and consolidation)


def _block_order_section(text: str) -> str:
    m = re.search(r"\[BLOCK ORDER\]\s*(.*)", text, re.I | re.S)
    return m.group(1) if m else text


def _visual_section(text: str) -> str:
    m = re.search(r"\[VISUAL WORD ORDER\]\s*(.*?)(?:\[BLOCK ORDER\]|\Z)", text, re.I | re.S)
    return m.group(1) if m else text


def _extract_master_bl(text: str) -> Optional[str]:
    block = _block_order_section(text)
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    if lines and _MBL_RE.fullmatch(lines[0]):
        return lines[0].upper()
    m = _MBL_RE.search(text)
    return m.group(1).upper() if m else None


def _extract_shipper(text: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    block = _block_order_section(text)
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    if lines and _MBL_RE.fullmatch(lines[0]):
        lines = lines[1:]
    shipper_lines: List[str] = []
    for ln in lines:
        upper = ln.upper()
        if "MARINE & ENGINEERING" in upper or "MARINE&ENGINEERING" in upper:
            break
        if upper in ("ISTANBUL", "ALEXANDRIA", "MERSIN", "AMBARLI"):
            break
        shipper_lines.append(ln)
    if not shipper_lines:
        visual = _visual_section(text)
        m = re.search(
            r"(ONE\s+ULUSLARARASI[^\n]+|^[A-Z][A-Z\s&./'-]{8,80}$)",
            visual,
            re.I | re.M,
        )
        if m:
            name = re.sub(r"\s+IST\d{9,12}$", "", m.group(1).strip(), flags=re.I)
            return name[:100], None, None
        return None, None, None

    name = re.sub(r"\s+IST\d{9,12}$", "", shipper_lines[0], flags=re.I).strip()
    address = re.sub(r"\s+", " ", ", ".join(shipper_lines[1:6]))[:250] if len(shipper_lines) > 1 else None
    lookup = re.sub(r"\s+", " ", name).strip()
    return name[:100], lookup[:100], address


def _extract_mesco_agent(text: str) -> Dict[str, Optional[str]]:
    m = re.search(
        r"(MARINE\s*&\s*ENGINEERING\s+SERVICES\s+COMPANY)\s*\n"
        r"(MESCO\s+[^\n]+(?:\n[^\n]+){0,4})",
        _block_order_section(text),
        re.I,
    )
    if not m:
        m = re.search(
            r"(MARINE\s*&\s*ENGINEERING\s+SERVICES\s+COMPANY)\s*\n"
            r"(MESCO\s+[^\n]+(?:\n[^\n]+){0,4})",
            text,
            re.I,
        )
    if not m:
        return {}
    name = re.sub(r"\s+", " ", m.group(1)).strip()
    addr_blob = re.sub(r"\s+", " ", " ".join(m.group(2).splitlines())).strip()[:250]
    return {
        "delivery_agent": name,
        "delivery_agent_address": addr_blob,
        "mesco_notify2": name,
    }


def _is_plausible_vessel(name: Optional[str]) -> bool:
    if not name or len(name.strip()) < 3:
        return False
    return not _BAD_VESSEL_RE.match(name.strip())


def _extract_route_vessel(text: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    vessel = voyage = None
    vm = _VESSEL_LINE_RE.search(text)
    if vm:
        vessel = re.sub(r"\s+", " ", vm.group(1)).strip()
        voyage = vm.group(2).strip().upper()

    block = _block_order_section(text)
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    pol = pod = None
    for i, ln in enumerate(lines):
        upper = ln.upper()
        if upper == "ISTANBUL":
            pol = "ISTANBUL"
            if not vessel:
                nxt = lines[i + 1] if i + 1 < len(lines) else ""
                nxt2 = lines[i + 2] if i + 2 < len(lines) else ""
                if nxt and nxt.upper() not in ("ALEXANDRIA", "MERSIN"):
                    inline = _VESSEL_VOYAGE_INLINE_RE.search(nxt)
                    if inline and _is_plausible_vessel(inline.group(1)):
                        vessel = inline.group(1).strip()
                        voyage = inline.group(2).strip()
                    elif _is_plausible_vessel(nxt):
                        vessel = nxt.strip()
                        if nxt2 and re.fullmatch(r"[A-Z0-9]{4,12}", nxt2, re.I):
                            voyage = nxt2.strip()
        if upper == "ALEXANDRIA":
            pod = "ALEXANDRIA"

    if not vessel:
        m = re.search(
            r"(\d{2}/\d{2}/\d{4})\s+([A-Z][A-Z0-9 ]+?)\s+([A-Z]{2,3}\d{2}[A-Z0-9]{2,6})",
            text,
            re.I,
        )
        if m and _is_plausible_vessel(m.group(2)):
            vessel = m.group(2).strip()
            voyage = m.group(3).strip()
    return pol, pod, vessel, voyage


def _attached_list_visual_text(text: str) -> str:
    pages = re.split(r"---\s*PAGE\s*\d+\s*---", text or "", flags=re.I)
    chunks: List[str] = []
    for page in pages[1:]:
        vm = re.search(r"\[VISUAL WORD ORDER\]\s*(.*?)(?:\[BLOCK ORDER\]|\Z)", page, re.I | re.S)
        if vm:
            chunks.append(vm.group(1))
    return "\n".join(chunks)


def _cargo_description_lines(block: str, shipper: Optional[str] = None) -> str:
    lines_out: List[str] = []
    if shipper:
        lines_out.append(f"S/ {shipper}")
    for ln in block.splitlines():
        s = ln.strip()
        if not s or s.upper().startswith("S/"):
            continue
        if re.match(r"^GW\s*:", s, re.I):
            break
        if re.match(r"^TOTAL\s*:", s, re.I):
            break
        if re.match(r"^Signed as Agents", s, re.I):
            break
        lines_out.append(s)
    return "\n".join(lines_out)[:1200]


def extract_arkas_attached_list_cargo(text: str) -> List[Dict[str, Any]]:
    """
    Parse Arkas attached-list cargo rows (S/ shipper blocks + container lines).

    Returns Dynamics-ready cargo line dicts with description, packages, and gross kg.
    """
    visual = _attached_list_visual_text(text)
    if not visual.strip():
        return []

    entries: List[Dict[str, Any]] = []
    seen_shippers: set[str] = set()

    for m in _CONTAINER_CARGO_RE.finditer(visual):
        qty = int(m.group(1))
        unit = m.group(2).upper()
        goods = re.sub(r"\s+", " ", m.group(3)).strip()
        gross = _normalize_weight(m.group(4))
        if gross is None:
            continue
        entries.append(
            {
                "mesco_descriptionofgoods": f"{qty} {unit} {goods}",
                "mesco_noofpackages": qty,
                "mesco_grosskg": gross,
            }
        )

    parts = re.split(r"(?=^S/\s+)", visual, flags=re.M)
    for part in parts:
        part = part.strip()
        if not part.startswith("S/"):
            continue
        sm = re.match(r"S/\s+([^\n]+)\s*\n(.*)", part, re.I | re.S)
        if not sm:
            continue
        shipper = re.sub(r"\s+", " ", sm.group(1)).strip()
        shipper_key = shipper.upper()
        if shipper_key in seen_shippers:
            continue
        body = sm.group(2)
        if re.search(r"^TOTAL\s*:\s*\d+\s+PALLET", body, re.I | re.M):
            continue

        pkg_m = re.search(r"(\d+)\s+(PALLETS?|BOXES?|CARTONS?)", body, re.I)
        packages = int(pkg_m.group(1)) if pkg_m else None
        if packages is not None and packages >= 100:
            continue

        gw_m = re.search(r"GW\s*:\s*([\d,.\s]+)\s*-?\s*KGS?", body, re.I)
        gross = _normalize_weight(gw_m.group(1)) if gw_m else None
        if gross is None:
            gw_m2 = re.search(r"([\d,]+\.[\d]+)\s*-?\s*KGS", body, re.I)
            gross = _normalize_weight(gw_m2.group(1)) if gw_m2 else None

        desc = _cargo_description_lines(body, shipper)
        if not desc and not packages and gross is None:
            continue

        seen_shippers.add(shipper_key)
        row: Dict[str, Any] = {"mesco_descriptionofgoods": desc or f"S/ {shipper}"}
        if packages is not None:
            row["mesco_noofpackages"] = packages
        if gross is not None:
            row["mesco_grosskg"] = gross
        entries.append(row)

    return entries


def _shipper_from_cargo_line(line: Dict[str, Any]) -> Optional[str]:
    desc = (line.get("mesco_descriptionofgoods") or "").strip()
    if not desc:
        return None
    m = re.search(r"^S/\s*([^\n]+)", desc, re.I | re.M)
    if m:
        return m.group(1).strip()[:100]
    first = desc.split("\n", 1)[0].strip()
    return first[:100] if first else None


def _house_slug_from_cargo_line(line: Dict[str, Any], idx: int) -> str:
    shipper = _shipper_from_cargo_line(line)
    if shipper:
        slug = re.sub(r"[^A-Z0-9]+", "", shipper.upper())[:16]
        if slug:
            return slug
    desc = (line.get("mesco_descriptionofgoods") or "").upper()
    if "BOX" in desc:
        return "BOXES"
    return f"CARGO{idx:02d}"


def build_house_records_from_arkas_cargo(
    master_rec: Dict[str, Any],
    cargo_lines: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """One house operation per attached-list shipper line, linked to the master MBL."""
    inherit_keys = (
        "mesco_shipperaddress",
        "mesco_consigneenamecontactno",
        "mesco_consigneeaddress",
        "mesco_notify1",
        "mesco_notifyaddress",
        "mesco_vessel",
        "mesco_voytruckno",
        "mesco_origin",
        "mesco_destination",
        "mesco_transhipmentport",
        "mesco_transporttype",
        "mesco_loadtype",
        "mesco_direction",
        "mesco_pcfreightterm",
        "mesco_acidnumber",
        "mesco_importerstaxno",
        "mesco_foreignsupplierregistrationnumber",
        "mesco_handlinginformation",
        "mesco_freightpayableat",
        "mesco_telexrelease",
        "mesco_shippingline",
        "container_number",
        "seal_number",
        "mesco_containertype",
        "containers",
    )
    master_mbl = (master_rec.get("mesco_masterblno") or "").strip()
    if not master_mbl:
        return []

    records: List[Dict[str, Any]] = []
    seen_hbl: set[str] = set()
    for idx, line in enumerate(cargo_lines, 1):
        if not isinstance(line, dict):
            continue
        slug = _house_slug_from_cargo_line(line, idx)
        hbl = f"{master_mbl}-{slug}"
        suffix = 2
        while hbl.upper() in seen_hbl:
            hbl = f"{master_mbl}-{slug}-{suffix}"
            suffix += 1
        seen_hbl.add(hbl.upper())

        rec = {
            k: master_rec[k]
            for k in inherit_keys
            if master_rec.get(k) not in (None, "", [], {})
        }
        rec["mesco_houseblno"] = hbl
        rec["mesco_masterblno"] = master_mbl
        rec["cargo_lines"] = [line]
        rec["mesco_cargodescription"] = line.get("mesco_descriptionofgoods")
        shipper = _shipper_from_cargo_line(line)
        if shipper:
            rec["mesco_shippernamecontactno"] = shipper
        elif master_rec.get("mesco_shippernamecontactno"):
            rec["mesco_shippernamecontactno"] = master_rec["mesco_shippernamecontactno"]
        if line.get("mesco_noofpackages") is not None:
            rec["cr401_totalpackages"] = line["mesco_noofpackages"]
        if line.get("mesco_grosskg") is not None:
            rec["cr401_totalgrossweight"] = line["mesco_grosskg"]
        rec["_arkas_cargo_house"] = True
        rec["_attached_list_house"] = True
        rec["_per_house_cargo"] = True
        records.append(rec)
    return records


def build_house_records_for_consolidation_sea_waybill(
    master_rec: Dict[str, Any],
    raw_text: str,
) -> List[Dict[str, Any]]:
    """Evergreen attached-list refs, else Arkas per-shipper cargo lines."""
    from pdf_attached_list import (
        build_house_records_from_attached_list,
        extract_attached_list_house_refs,
    )

    refs = extract_attached_list_house_refs(raw_text)
    if refs:
        return build_house_records_from_attached_list(master_rec, refs)
    cargo_lines = master_rec.get("cargo_lines") or []
    if cargo_lines:
        return build_house_records_from_arkas_cargo(master_rec, cargo_lines)
    return []


def master_record_without_house_cargo(master_rec: Dict[str, Any]) -> Dict[str, Any]:
    """Strip per-line cargo from the master so it is not duplicated on upload."""
    master = dict(master_rec)
    master.pop("cargo_lines", None)
    return master


def _extract_container(text: str) -> Optional[Dict[str, str]]:
    for m in _CONTAINER_RE.finditer(text):
        prefix, digits = m.group(1).upper(), m.group(2)
        if prefix in ("PAGE", "TEL", "FAX", "POST", "CODE", "TAX"):
            continue
        if len(digits) >= 6:
            return {
                "container_number": f"{prefix}{digits}",
                "container_type": "40HC",
            }
    return None


def _normalize_weight(value: str) -> Optional[float]:
    cleaned = re.sub(r"[^\d.,]", "", value or "")
    if not cleaned:
        return None
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", cleaned):
        cleaned = cleaned.replace(",", "")
    elif "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _build_cargo_description(text: str, packages: Optional[int], gross: Optional[float]) -> str:
    parts: List[str] = []
    if re.search(r"AS\s+PER\s+ATTACHED\s+LIST", text, re.I):
        parts.append("AS PER ATTACHED LIST")
    route_m = _CONSOLIDATION_ROUTE_RE.search(text)
    if route_m:
        parts.append(re.sub(r"\s+", " ", route_m.group(0)).strip()[:400])
    elif re.search(r"CONSOLIDATION\s+CARGO", text, re.I):
        parts.append("CONSOLIDATION CARGO")
    if packages:
        parts.append(f"{packages} PALLETS")
    if gross:
        parts.append(f"GW: {gross:,.3f} KGS".replace(",", ""))
    return "\n".join(parts) if parts else "CONSOLIDATION CARGO — AS PER ATTACHED LIST"


def parse_consolidation_sea_waybill(text: str) -> Optional[Dict[str, Any]]:
    if not is_consolidation_sea_waybill(text):
        return None

    mbl = _extract_master_bl(text)
    shipper_name, shipper_lookup, shipper_addr = _extract_shipper(text)
    pol, pod, vessel, voyage = _extract_route_vessel(text)
    container = _extract_container(text)
    agent = _extract_mesco_agent(text)

    total_m = _TOTAL_RE.search(text)
    packages = int(total_m.group(1)) if total_m else None
    gross = _normalize_weight(total_m.group(2)) if total_m else None

    record: Dict[str, Any] = {
        "document_type": "consolidation_sea_waybill",
        "extraction_method": "pdf_sea_waybill_direct",
        "mesco_transporttype": 300000000,
        "mesco_loadtype": 300000000,
        "mesco_direction": 300000000,
        "mesco_consolidation": True,
        "mesco_telexrelease": True,
        "mesco_blstatus": 886150001,
        "mesco_pcfreightterm": "PREPAID",
    }

    if mbl:
        record["mesco_masterblno"] = mbl
    if shipper_name:
        record["mesco_shippernamecontactno"] = shipper_name
    if shipper_lookup:
        record["mesco_shipper"] = shipper_lookup
    if shipper_addr:
        record["mesco_shipperaddress"] = shipper_addr
    if pol:
        record["mesco_origin"] = pol
    if pod:
        record["mesco_destination"] = pod
    if vessel:
        record["mesco_vessel"] = vessel[:50]
    if voyage:
        record["mesco_voytruckno"] = voyage[:30]
    if packages is not None:
        record["cr401_totalpackages"] = packages
    if gross is not None:
        record["cr401_totalgrossweight"] = gross
    if container:
        record["container_number"] = container["container_number"]
        record["containers"] = [container]
        record["mesco_containertype"] = container.get("container_type")
        record["cr401_totalteus"] = 1

    acid_m = _ACID_RE.search(text)
    if acid_m:
        record["mesco_acidnumber"] = acid_m.group(1)
    imp_m = _IMPORTER_TAX_RE.search(text)
    if imp_m:
        record["mesco_importerstaxno"] = imp_m.group(1)
    exp_m = _EXPORTER_RE.search(text)
    if exp_m:
        record["mesco_foreignsupplierregistrationnumber"] = exp_m.group(1)

    if re.search(r"ARKAS", text, re.I):
        record["mesco_shippingline"] = "ARKAS DENIZCILIK VE NAKLIYAT A.S."

    record.update({k: v for k, v in agent.items() if v})

    cargo_lines = extract_arkas_attached_list_cargo(text)
    if cargo_lines:
        record["cargo_lines"] = cargo_lines
        record["mesco_cargodescription"] = _build_cargo_description(text, packages, gross)
    else:
        record["mesco_cargodescription"] = _build_cargo_description(text, packages, gross)

    if mbl:
        record["mesco_bookingnumber"] = mbl

    if not record.get("mesco_masterblno"):
        return None
    return record
