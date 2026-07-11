"""Direct parser for readable single master ocean B/L PDFs.

This covers carrier forms such as CMA CGM where the PDF text is readable, but
the generic whole-document route would otherwise depend on an LLM just to seed
one master record.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from validator import extract_acid_regex, extract_bl_number_regex, is_likely_bl_number


_MASTER_BL_TOKEN_RE = re.compile(r"\b(?=[A-Z0-9-]{5,25}\b)(?=[A-Z0-9-]*\d)[A-Z]{2,8}[A-Z0-9-]*\b", re.I)
_HOUSE_HINT_RE = re.compile(r"\b(?:HOUSE\s+B/?L|H\s*/?\s*BL|HBL)\b", re.I)
_CONTAINER_ROW_RE = re.compile(
    r"\b(?P<container>[A-Z]{4}\d{7})\s+"
    r"(?P<count>\d+)\s*x\s*(?P<ctype>\d{2}[A-Z0-9]{0,4})\s+"
    r"(?P<packages>\d+)\s+"
    r"(?P<unit>PACKAGES?|PACKAGE\(S\)|PALLETS?|CARTONS?|BOXES?)\s+"
    r"(?P<gross>[\d,.]+)\s+(?P<tare>[\d,.]+)\s+(?P<volume>[\d,.]+)",
    re.I,
)
_DATE_FORMATS = ("%d %b %Y", "%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d")
_COUNTRY_BY_CODE = {
    "CN": "China",
    "EG": "Egypt",
    "IN": "India",
    "TR": "Turkey",
    "AE": "United Arab Emirates",
    "DE": "Germany",
    "IT": "Italy",
    "ES": "Spain",
    "FR": "France",
    "US": "United States",
    "GB": "United Kingdom",
}
_ROUTE_PORTS = (
    "ALEXANDRIA OLD PORT",
    "PORT SAID WEST",
    "PORT SAID",
    "AIN SOKHNA",
    "EL SOKHNA",
    "ALEXANDRIA",
    "ANTWERP",
    "AMBARLI",
    "BEIRUT",
    "BUSAN",
    "COLOMBO",
    "DAMIETTA",
    "DEKHEILA",
    "FELIXSTOWE",
    "GENOA",
    "GIOIA TAURO",
    "HAMBURG",
    "HONG KONG",
    "ISTANBUL",
    "JEBEL ALI",
    "LE HAVRE",
    "LIMASSOL",
    "MERSIN",
    "NINGBO",
    "PIRAEUS",
    "QINGDAO",
    "ROTTERDAM",
    "SHANGHAI",
    "SHEKOU",
    "SHENZHEN",
    "SINGAPORE",
    "TIANJIN",
    "VALENCIA",
    "XIAMEN",
    "YANTIAN",
)


def _tagged_section(text: str, tag: str) -> str:
    pattern = rf"\[{re.escape(tag)}\]\s*(.*?)(?=\n\[[^\]\n]{{3,80}}\]|\Z)"
    m = re.search(pattern, text or "", re.I | re.S)
    return m.group(1) if m else ""


def _visual_section(text: str) -> str:
    visual = _tagged_section(text, "VISUAL WORD ORDER")
    if _section_looks_readable(visual):
        return visual
    for tag in ("BLOCK ORDER", "OCR FULL PAGE BEST", "OCR BODY PSM4", "OCR HEADER PSM6"):
        candidate = _tagged_section(text, tag)
        if _section_looks_readable(candidate):
            return candidate
    return visual or (text or "")


def _block_order_section(text: str) -> str:
    return _tagged_section(text, "BLOCK ORDER") or (text or "")


def _section_looks_readable(section: str) -> bool:
    if not section or len(section.strip()) < 80:
        return False
    upper = section.upper()
    hits = sum(
        1
        for marker in (
            "BILL OF LADING",
            "SHIPPER",
            "CONSIGNEE",
            "NOTIFY",
            "PORT OF LOADING",
            "PORT OF DISCHARG",
            "ACID",
        )
        if marker in upper
    )
    return hits >= 2


def _clean(value: Any, max_len: Optional[int] = None) -> Optional[str]:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip(" ,;:-")
    if not text:
        return None
    return text[:max_len] if max_len else text


def _clean_lines(section: str) -> List[str]:
    lines: List[str] = []
    for raw in (section or "").splitlines():
        line = re.sub(r"\s+", " ", raw).strip(" \t|")
        if line:
            lines.append(line)
    return lines


def _parse_float(value: Any) -> Optional[float]:
    text = re.sub(r"[^\d.,]", "", str(value or ""))
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


def _parse_date(value: Any) -> Optional[str]:
    text = _clean(value)
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text.upper(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _extract_master_bl(text: str) -> Optional[str]:
    sample = "\n".join(
        part
        for part in (
            _visual_section(text),
            _block_order_section(text),
            _tagged_section(text, "OCR FULL PAGE BEST"),
            text or "",
        )
        if part
    )

    carrier_ref = re.search(
        r"CARRIER\s+REFERENCE\s+B\s*/?\s*L\.?\s*NO\.?(?:\s+PAGE)?(?P<body>.{0,220})",
        sample,
        re.I | re.S,
    )
    if carrier_ref:
        for token in re.findall(r"\b[A-Z0-9][A-Z0-9-]{4,25}\b", carrier_ref.group("body").upper()):
            if any(ch.isdigit() for ch in token) and is_likely_bl_number(token):
                return token

    patterns = (
        r"CARRIER\s+REFERENCE\s+B\s*/?\s*L\.?\s*NO\.?\s*(?:PAGE)?\s*(?P<bl>[A-Z0-9-]{5,25})",
        r"BILL\s+OF\s+LADING\s+NUMBER\s*(?:\n|\s)+(?:BILL\s+OF\s+LADING\s*)?(?P<bl>[A-Z0-9-]{5,25})",
        r"BILL\s+OF\s+LADING\s*(?:NO|NUMBER|#)\.?\s*[:\-]?\s*(?P<bl>[A-Z0-9-]{5,25})",
    )
    for pattern in patterns:
        m = re.search(pattern, sample, re.I)
        if m:
            candidate = m.group("bl").strip().upper()
            if _MASTER_BL_TOKEN_RE.fullmatch(candidate) and is_likely_bl_number(candidate):
                return candidate

    bl = extract_bl_number_regex(sample)
    if bl and _MASTER_BL_TOKEN_RE.fullmatch(str(bl).strip().upper()):
        return str(bl).strip().upper()
    return None


def _extract_voyage(text: str, master_bl: Optional[str]) -> Optional[str]:
    for section in (_visual_section(text), _block_order_section(text), text or ""):
        m = re.search(r"VOYAGE\s+NUMBER\s*(?P<body>.{0,180})", section, re.I | re.S)
        if not m:
            m = re.search(r"OCEAN\s+VESSEL\s*/\s*VOYAGE(?P<body>.{0,260})", section, re.I | re.S)
        if not m:
            slash = re.search(r"/\s*(?P<voyage>[A-Z0-9]{5,15})\b", section, re.I)
            if slash:
                return slash.group("voyage").upper()
            continue
        for token in re.findall(r"\b(?=[A-Z0-9]*\d)[A-Z0-9]{5,15}\b", m.group("body"), re.I):
            token = token.upper()
            if master_bl and token == master_bl.upper():
                continue
            if not re.fullmatch(r"\d+", token):
                return token
    return None


def _clean_shipper_line(line: str, *, master_bl: Optional[str], voyage: Optional[str]) -> Optional[str]:
    text = line
    for token in (master_bl, voyage):
        if token:
            text = re.sub(rf"\b{re.escape(str(token))}\b", " ", text, flags=re.I)
    text = re.sub(r"\([^)]*(?:COMPLETE\s+NAME|STREET\s+ADDRESS)[^)]*\)", " ", text, flags=re.I)
    text = re.sub(r"\bCARRIER\s+REFERENCE\s+B\s*/?\s*L\.?\s*NO\.?\s*PAGE\b.*$", " ", text, flags=re.I)
    text = re.sub(r"\bCOPY\s+NON\s+NEGOTIABLE\b", " ", text, flags=re.I)
    text = re.sub(r"\bBILL\s+OF\s+LADING\s+NUMBER\b", " ", text, flags=re.I)
    text = re.sub(r"\bBILL\s+OF\s+LADING\b", " ", text, flags=re.I)
    return _clean(text)


def _extract_shipper(text: str, master_bl: Optional[str], voyage: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    visual = _visual_section(text)
    m = re.search(r"\bSHIPPER\b\s*(?P<body>.*?)(?=\bCONSIGNEE\b)", visual, re.I | re.S)
    if not m:
        return None, None

    name_parts: List[str] = []
    address_parts: List[str] = []
    for raw in _clean_lines(m.group("body")):
        line = _clean_shipper_line(raw, master_bl=master_bl, voyage=voyage)
        if not line:
            continue
        upper = line.upper()
        is_address = upper.startswith("ADD:") or re.match(r"^\d", upper)
        if not address_parts and not is_address:
            name_parts.append(line)
            continue
        address_parts.append(line)

    name = _clean(" ".join(name_parts), 120)
    address = _clean(", ".join(address_parts), 250)
    return name, address


def _party_from_lines(lines: List[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    cleaned: List[str] = []
    contact_parts: List[str] = []
    for line in lines:
        line = re.sub(r"\b(?:EXPORTER?\s+REFERENCES?|Carrier not to be responsible for failure to notify)\b", " ", line, flags=re.I)
        line = re.sub(r"\([^)]*(?:NOT\s+NEGOTIABLE|COMPLETE\s+NAME|STREET\s+ADDRESS)[^)]*\)", " ", line, flags=re.I)
        line = re.sub(r"\b(?:CONSIGNEE|NOTIFY\s+PARTY)\s+REFERENCE\b", " ", line, flags=re.I)
        line = _clean(line)
        if not line:
            continue
        if re.search(r"\bTEL\.?|PHONE|FAX|MOB|MOBILE|EMAIL|VAT\s+NO\b", line, re.I):
            contact_parts.append(line)
            continue
        cleaned.append(line)

    if not cleaned:
        return None, None, _clean(" | ".join(contact_parts), 160)

    name_parts: List[str] = []
    address_parts: List[str] = []
    for line in cleaned:
        upper = line.upper()
        is_address = (
            upper.startswith("ADD:")
            or re.match(r"^[\d-]", upper)
            or any(token in upper for token in (" GIZA", " EGYPT", " ALEXANDRIA", "ST."))
        )
        if not address_parts and not is_address:
            name_parts.append(line)
            continue
        address_parts.append(line)

    if not name_parts:
        name_parts = cleaned[:1]
        address_parts = cleaned[1:]
    return _clean(" ".join(name_parts), 120), _clean(", ".join(address_parts), 250), _clean(" | ".join(contact_parts), 160)


def _extract_consignee(text: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    visual = _visual_section(text)
    m = re.search(
        r"\bCONSIGNEE\b(?:\s+EXPORT\s+REFERENCES)?\s*(?P<body>.*?)(?=\bNOTIFY\s+PARTY\b|\bCARRIER\s*:)",
        visual,
        re.I | re.S,
    )
    return _party_from_lines(_clean_lines(m.group("body"))) if m else (None, None, None)


def _extract_notify(text: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    visual = _visual_section(text)
    m = re.search(
        r"\bNOTIFY\s+PARTY[^\n]*\n(?P<body>.*?)(?=\bCARRIER\s*:|\bPRE[-\s]*CARRIAGE\b)",
        visual,
        re.I | re.S,
    )
    return _party_from_lines(_clean_lines(m.group("body"))) if m else (None, None, None)


def _is_route_label(line: str) -> bool:
    return bool(
        re.search(
            r"^(?:VESSEL|PORT\s+OF\s+LOADING|PORT\s+OF\s+DISCHARGE|FINAL\s+PLACE|"
            r"PLACE\s+OF\s+RECEIPT|PRE\s+CARRIAGE|FREIGHT\s+TO\s+BE|"
            r"NUMBER\s+OF\s+ORIGINAL)",
            line,
            re.I,
        )
    )


def _parse_compact_route_line(line: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    value = _clean(re.sub(r"\*+", " ", line or ""))
    if not value:
        return None, None, None, None

    ports = sorted(_ROUTE_PORTS, key=len, reverse=True)
    for destination in ports:
        dest_re = re.escape(destination)
        m_dest = re.match(
            rf"^(?P<before>.+?)\s+{dest_re}(?:\s+(?P<delivery>.+))?$",
            value,
            re.I,
        )
        if not m_dest:
            continue

        before = _clean(m_dest.group("before"))
        delivery = _clean(m_dest.group("delivery"))
        if delivery and re.fullmatch(r"[\s*Xx-]+", delivery):
            delivery = None

        for origin in ports:
            origin_re = re.escape(origin)
            m_origin = re.match(rf"^(?P<vessel>.+?)\s+{origin_re}$", before or "", re.I)
            if not m_origin:
                continue
            vessel = _clean(m_origin.group("vessel"), 50)
            if vessel:
                return vessel, origin, destination, delivery or destination

    return None, None, None, None


def _extract_route_vessel(text: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    block = _block_order_section(text)
    lines = _clean_lines(block)
    for idx, line in enumerate(lines):
        if not re.fullmatch(r"VESSEL", line, re.I):
            continue
        values: List[str] = []
        for candidate in lines[idx + 1 : idx + 14]:
            if re.search(r"^MARKS\s+AND\s+NOS\b", candidate, re.I):
                break
            if _is_route_label(candidate):
                continue
            values.append(candidate)
            if len(values) >= 4:
                break
        if len(values) >= 3:
            vessel = _clean(values[0], 50)
            origin = _clean(values[1], 80)
            destination = _clean(values[2], 80)
            delivery = _clean(values[3], 120) if len(values) >= 4 else destination
            if delivery and re.fullmatch(r"\*+", delivery):
                delivery = destination
            return vessel, origin, destination, delivery

    visual = _visual_section(text)
    visual_lines = _clean_lines(visual)
    for idx, line in enumerate(visual_lines):
        if not re.search(r"VESSEL\s+PORT\s+OF\s+LOADING\s+PORT\s+OF\s+DISCHARGE", line, re.I):
            continue
        for candidate in visual_lines[idx + 1 : idx + 4]:
            if re.search(r"^MARKS\s+AND\s+NOS\b", candidate, re.I):
                break
            parsed = _parse_compact_route_line(candidate)
            if parsed[0]:
                return parsed

    compact = re.sub(r"\s+", " ", visual).strip()
    m_route = re.search(
        r"OCEAN\s+VESSEL\s*/\s*VOYAGE\s+PORT\s+OF\s+LOADING\s+PORT\s+OF\s+DISCHARG\w*"
        r"\s+PLACE\s+OF\s+DELIVERY\s+(?P<body>.+?)(?:BELOW\s+PARTICULARS|Container\s+Nos\.|$)",
        compact,
        re.I,
    )
    if m_route:
        body = m_route.group("body")
        m = re.search(
            r"(?P<vessel>.+?)\s+"
            r"(?P<origin>SHENZHEN\s*,?\s*CHINA|HONG\s+KONG|SHANGHAI\s*,?\s*CHINA|NINGBO\s*,?\s*CHINA|YANTIAN\s*,?\s*CHINA)"
            r"\s+"
            r"(?P<dest>ALEXANDRIA\s*,?\s*EGYPT|PORT\s+SAID\s*,?\s*EGYPT|DAMIETTA\s*,?\s*EGYPT)"
            r"(?:\s+(?P<delivery>ALEXANDRIA\s*,?\s*EGYPT|PORT\s+SAID\s*,?\s*EGYPT|DAMIETTA\s*,?\s*EGYPT))?",
            body,
            re.I,
        )
        if m:
            vessel = _clean(re.sub(r"/\s*[A-Z0-9]{5,15}\b", "", m.group("vessel")), 50)
            return (
                vessel,
                _clean(m.group("origin"), 80),
                _clean(m.group("dest"), 80),
                _clean(m.group("delivery"), 120) or _clean(m.group("dest"), 120),
            )

    m = re.search(
        r"VESSEL\s+PORT\s+OF\s+LOADING\s+PORT\s+OF\s+DISCHARGE[^\n]*\n"
        r"(?P<vessel>[A-Z][A-Z0-9 ]+?)\s+(?P<origin>[A-Z][A-Z ]{2,30})\s+"
        r"(?P<dest>ALEXANDRIA|[A-Z][A-Z ]{2,30})(?:\s+(?P<delivery>[A-Z*][A-Z* ]{2,40}))?",
        visual,
        re.I,
    )
    if not m:
        return None, None, None, None
    delivery = _clean(m.group("delivery"), 120)
    if delivery and re.fullmatch(r"\*+", delivery):
        delivery = _clean(m.group("dest"), 120)
    return (
        _clean(m.group("vessel"), 50),
        _clean(m.group("origin"), 80),
        _clean(m.group("dest"), 80),
        delivery or _clean(m.group("dest"), 120),
    )


def _extract_place_of_receipt(text: str) -> Optional[str]:
    visual = _visual_section(text)
    m = re.search(
        r"PRE\s+CARRIAGE\s+BY\*?\s+PLACE\s+OF\s+RECEIPT\*?[^\n]*\n\s*([A-Z][A-Z ]{2,40})\b",
        visual,
        re.I,
    )
    if not m:
        m = re.search(
            r"PRE[-\s]*CARRIAGE\s+BY[^\n]*PLACE\s+OF\s+RECEIPT[^\n]*\n+\s*([A-Z][A-Z ,]{2,50})\b",
            visual,
            re.I,
        )
    if not m:
        return None
    candidate = re.sub(r"\bTHREE\b.*$", "", m.group(1), flags=re.I)
    return _clean(candidate, 80)


def _extract_container(text: str) -> Tuple[Optional[Dict[str, Any]], Optional[int], Optional[str], Optional[float], Optional[float]]:
    m = _CONTAINER_ROW_RE.search(text or "")
    if not m:
        return None, None, None, None, None

    packages = int(m.group("packages"))
    unit = m.group("unit").upper().replace("PACKAGE(S)", "PACKAGES")
    gross = _parse_float(m.group("gross"))
    volume = _parse_float(m.group("volume"))
    seal = None
    tail = (text or "")[m.end() : m.end() + 120]
    sm = re.search(r"\bSEAL\s+([A-Z0-9]{4,20})\b", tail, re.I)
    if sm:
        seal = sm.group(1).upper()

    container = {
        "container_number": m.group("container").upper(),
        "seal_number": seal,
        "container_type": m.group("ctype").upper(),
        "packages": packages,
        "gross_weight_kg": gross,
        "measurement_cbm": volume,
    }
    return container, packages, unit, gross, volume


def _extract_loose_cargo_totals(text: str) -> Tuple[Optional[int], Optional[str], Optional[float], Optional[float]]:
    patterns = (
        r"SAID\s+TO\s+CONTAIN(?:E)?\s*:?\s*(?P<packages>\d+)\s*(?P<unit>[A-Z]{3,20})"
        r"(?:\s+IN\s+TOTAL)?\s+(?P<gross>[\d,.]+)\s+(?P<volume>[\d,.]+)",
        r"(?P<packages>\d+)\s*(?P<unit>CARTONS?|PACKAGES?|PALLETS?|BOXES?)\s+IN\s+TOTAL"
        r"\s+(?P<gross>[\d,.]+)\s+(?P<volume>[\d,.]+)",
    )
    for pattern in patterns:
        m = re.search(pattern, text or "", re.I)
        if not m:
            continue
        return (
            int(m.group("packages")),
            m.group("unit").upper(),
            _parse_float(m.group("gross")),
            _parse_float(m.group("volume")),
        )
    return None, None, None, None


def _extract_no_of_originals(text: str) -> Optional[str]:
    m = re.search(r"NUMBER\s+OF\s+ORIGINAL\s+BILLS\s+OF\s+LADING[^\n]*\n[^\n]*\b(\d+)\b", text, re.I)
    if m:
        return m.group(1)
    m = re.search(r"\bZERO\s*\(\s*0\s*\)", text, re.I)
    if m:
        return "0"
    m = re.search(r"\b(ONE|TWO|THREE|FOUR|FIVE|SIX|SEVEN|EIGHT|NINE)\s*\((\d+)\)", text, re.I)
    return m.group(2) if m else None


def _extract_issue(text: str) -> Tuple[Optional[str], Optional[str]]:
    m = re.search(
        r"PLACE\s+AND\s+DATE\s+OF\s+ISSUE\s+(?P<place>[A-Z][A-Z ]{2,40})\s+"
        r"(?P<date>\d{1,2}\s+[A-Z]{3,9}\s+\d{4})",
        text,
        re.I,
    )
    if m:
        return _clean(m.group("place"), 80), _parse_date(m.group("date"))
    m = re.search(
        r"PLACE\s+AND\s+DATE\s+OF\s+ISSUE\s+(?P<place>[A-Z][A-Z ,]{2,60})\s+"
        r"(?P<date>\d{4}-\d{2}-\d{2})",
        text,
        re.I,
    )
    if m:
        return _clean(m.group("place"), 80), _parse_date(m.group("date"))
    return None, None


def _extract_shipped_on_board(text: str) -> Optional[str]:
    m = re.search(r"Shipped\s+on\s+Board[^\n]*?\b(\d{1,2}[-\s][A-Z]{3,9}[-\s]\d{4})", text, re.I)
    if m:
        return _parse_date(m.group(1))
    m = re.search(r"LADEN\s+ON\s+BOARD\s+DATE[^\n]*?\b(\d{4}-\d{2}-\d{2})", text, re.I | re.S)
    return _parse_date(m.group(1)) if m else None


def _extract_issuing_agent(text: str) -> Optional[str]:
    m = re.search(r"\bBY\s+(CMA\s+CGM\s+[A-Z][A-Z ]{2,40})\b", text, re.I)
    return _clean(m.group(1), 120) if m else None


def _extract_shipping_line(text: str) -> Optional[str]:
    m = re.search(r"\bCARRIER\s*:\s*([A-Z][A-Z0-9 .,&'-]{3,80})", text or "", re.I)
    if m:
        carrier = _clean(m.group(1), 120)
        if carrier and re.search(r"\bCMA\s+CGM\b", carrier, re.I):
            return "CMA CGM"
        return carrier
    if re.search(r"\bCMA\s+CGM\b", text or "", re.I):
        return "CMA CGM"
    return None


def _extract_hs_codes(text: str) -> Optional[str]:
    codes: List[str] = []
    for m in re.finditer(r"H\.?\s*S\.?\s*CODE\s*:?\s*([0-9]{6,12})", text or "", re.I):
        code = m.group(1)
        if code not in codes:
            codes.append(code)
    return ", ".join(codes) if codes else None


def _country_from_text(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    upper = re.sub(r"\s+", " ", value).strip().upper()
    if upper in _COUNTRY_BY_CODE:
        return _COUNTRY_BY_CODE[upper]
    if "CHINA" in upper:
        return "China"
    if "EGYPT" in upper:
        return "Egypt"
    if "INDIA" in upper:
        return "India"
    if "TURKEY" in upper:
        return "Turkey"
    return _clean(value, 80)


def _extract_nafeza_fields(text: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    source = text or ""

    importer = re.search(
        r"\b(?:EGYPTIAN\s+(?:FREIGHT\s+FORWARDER|IMPORTER)|IMPORTER)\s+TAX\s+ID\s*:?\s*(\d{6,15})",
        source,
        re.I,
    )
    if importer:
        fields["mesco_importerstaxno"] = importer.group(1)
    else:
        vat = re.search(r"\bVAT\s+NO\.?\s*:?\s*(\d{6,15})\b", source, re.I)
        if vat:
            fields["mesco_importerstaxno"] = vat.group(1)

    reg_type = re.search(
        r"\bFOREIGN\s+(?:FREIGHT\s+FORWARDER|EXPORTER|SUPPLIER)\s+REGISTRATION\s+TYPE\s*:?\s*([A-Z][A-Z ]{2,40})",
        source,
        re.I,
    )
    if reg_type:
        reg_value = _clean(reg_type.group(1))
        if reg_value and re.search(r"\b(?:VAT|TAX)\b", reg_value, re.I):
            fields["mesco_typeofregistrationnumber"] = "Tax Number"
        elif reg_value and re.search(r"\bREGISTRATION\b", reg_value, re.I):
            fields["mesco_typeofregistrationnumber"] = "Registration number"

    foreign_id = re.search(
        r"\bFOREIGN\s+(?:FREIGHT\s+FORWARDER|EXPORTER|SUPPLIER)\s+ID\s*:?\s*([A-Z0-9-]{5,30})",
        source,
        re.I,
    )
    if foreign_id:
        fields["mesco_foreignsupplierregistrationnumber"] = foreign_id.group(1).upper()
    else:
        vat_foreign = re.search(r"\bVAT\s+NO\.?\s*:?\s*([A-Z]{2}-[A-Z0-9-]{5,30})", source, re.I)
        if vat_foreign:
            fields["mesco_foreignsupplierregistrationnumber"] = vat_foreign.group(1).upper()
            fields.setdefault("mesco_typeofregistrationnumber", "Tax Number")

    foreign_country = re.search(
        r"\bFOREIGN\s+(?:FREIGHT\s+FORWARDER|EXPORTER|SUPPLIER)\s+COUNTRY\s*:?\s*([A-Z][A-Z ]{1,40})",
        source,
        re.I,
    )
    country_code = re.search(
        r"\bFOREIGN\s+(?:FREIGHT\s+FORWARDER|EXPORTER|SUPPLIER)\s+COUNTRY\s+CODE\s*:?\s*([A-Z]{2})\b",
        source,
        re.I,
    )
    country = _country_from_text(foreign_country.group(1)) if foreign_country else None
    if not country and country_code:
        country = _country_from_text(country_code.group(1))
    if country:
        fields["mesco_country"] = country
        fields.setdefault("mesco_countryoforigin", country)

    return fields


def _infer_shipper_country(shipper_address: Optional[str], origin: Optional[str]) -> Optional[str]:
    return _country_from_text(shipper_address) or _country_from_text(origin)


def _extract_freight_terms(text: str) -> Dict[str, Any]:
    upper = (text or "").upper()
    if re.search(r"FREIGHT\s+PAYABLE\s+AT.{0,100}\bCOLLECT\b", upper, re.S):
        return {
            "mesco_pcfreightterm": "COLLECT",
            "mesco_freightpayableat": "Destination",
            "mesco_bookingterm": 886150001,
        }
    if re.search(r"\bFREIGHT\s+COLLECT\b", upper):
        return {
            "mesco_pcfreightterm": "COLLECT",
            "mesco_freightpayableat": "Destination",
            "mesco_bookingterm": 886150001,
        }
    if re.search(r"\bFREIGHT\s+PREPAID\b", upper):
        return {
            "mesco_pcfreightterm": "PREPAID",
            "mesco_freightpayableat": "Origin",
            "mesco_bookingterm": 886150000,
        }
    if re.search(r"\bFREIGHT\s+TO\s+BE\s+PAID\s+AT\b", upper) and not re.search(r"\bCOLLECT\b", upper):
        return {
            "mesco_pcfreightterm": "PREPAID",
            "mesco_freightpayableat": "Origin",
            "mesco_bookingterm": 886150000,
        }
    return {}


def is_standard_master_bl(text: str) -> bool:
    if not text or not text.strip():
        return False
    upper = text.upper()
    if "BILL OF LADING" not in upper:
        return False
    if _HOUSE_HINT_RE.search(upper):
        return False
    if re.search(r"\b(?:MANIFEST|DEBIT\s+NOTE|SEA\s+WAY\s*BILL|SEA\s+WAYBILL)\b", upper):
        return False
    return bool(_extract_master_bl(text))


def parse_standard_master_bl(text: str) -> Optional[Dict[str, Any]]:
    if not is_standard_master_bl(text):
        return None

    master_bl = _extract_master_bl(text)
    if not master_bl:
        return None

    voyage = _extract_voyage(text, master_bl)
    shipper, shipper_address = _extract_shipper(text, master_bl, voyage)
    consignee, consignee_address, consignee_contact = _extract_consignee(text)
    notify, notify_address, notify_contact = _extract_notify(text)
    vessel, origin, destination, delivery = _extract_route_vessel(text)
    place_receipt = _extract_place_of_receipt(text)
    container, packages, package_unit, gross, volume = _extract_container(text)
    if packages is None:
        packages, package_unit, gross, volume = _extract_loose_cargo_totals(text)
    place_of_issue, date_of_issue = _extract_issue(text)
    shipped_on_board = _extract_shipped_on_board(text)
    acid = extract_acid_regex(text)
    hs_codes = _extract_hs_codes(text)
    nafeza_fields = _extract_nafeza_fields(text)
    freight_fields = _extract_freight_terms(text)
    is_lcl = bool(re.search(r"\b(?:CFS\s*/\s*CFS|LCL|CONSOLIDATED\s+CARGO)\b", text, re.I))

    record: Dict[str, Any] = {
        "document_type": "Bill of Lading",
        "extraction_method": "pdf_standard_master_bl_direct",
        "mesco_masterblno": master_bl,
        "mesco_bookingnumber": master_bl,
        "mesco_transporttype": 300000000,
        "mesco_loadtype": 300000001 if is_lcl else 300000000,
        "mesco_consolidation": bool(re.search(r"\bCONSOLIDATED\s+CARGO\b", text, re.I)),
    }
    record.update(freight_fields)
    if re.search(r"\bTELEX\s+RELEASE\b", text, re.I):
        record["mesco_telexrelease"] = True

    if shipper:
        record["mesco_shippernamecontactno"] = shipper
        record["mesco_shipper"] = shipper
    if shipper_address:
        record["mesco_shipperaddress"] = shipper_address
    if consignee:
        record["mesco_consigneenamecontactno"] = consignee
        record["mesco_consignee"] = consignee
    if consignee_address:
        record["mesco_consigneeaddress"] = consignee_address
    if consignee_contact:
        record["mesco_consigneecontactnumber"] = consignee_contact
    if notify:
        record["mesco_notify1"] = notify
    if notify_address:
        record["mesco_notifyaddress"] = notify_address
    if vessel:
        record["mesco_vessel"] = vessel
    if voyage:
        record["mesco_voytruckno"] = voyage
    if origin:
        record["mesco_origin"] = origin
    if destination:
        record["mesco_destination"] = destination
    if delivery:
        record["mesco_deliveryaddress"] = delivery
    if place_receipt:
        record["mesco_pickupaddress"] = place_receipt
    shipper_country = _infer_shipper_country(shipper_address, origin)
    if shipper_country:
        record["mesco_country"] = shipper_country
        record.setdefault("mesco_countryoforigin", shipper_country)
    if packages is not None:
        record["cr401_totalpackages"] = packages
    if package_unit:
        record["mesco_umpackages"] = package_unit
        record["cargo_type"] = package_unit
    if gross is not None:
        record["cr401_totalgrossweight"] = gross
    if volume is not None:
        record["cr401_totalvolume"] = volume
    if container:
        record["container_number"] = container["container_number"]
        if container.get("seal_number"):
            record["seal_number"] = container["seal_number"]
        record["mesco_containertype"] = container.get("container_type")
        record["containers"] = [container]
        record["cr401_totalteus"] = 1
    if place_of_issue:
        record["mesco_placeofissue"] = place_of_issue
    if date_of_issue:
        record["mesco_dateofissue"] = date_of_issue
    if shipped_on_board:
        record["mesco_shippedonboarddate"] = shipped_on_board
    if acid:
        record["mesco_acidnumber"] = acid
    if hs_codes:
        record["mesco_hscode"] = hs_codes
    record.update(nafeza_fields)

    originals = _extract_no_of_originals(text)
    if originals:
        record["mesco_nooforgbls"] = originals

    shipping_line = _extract_shipping_line(text)
    if shipping_line:
        record["mesco_shippingline"] = shipping_line
    issuing_agent = _extract_issuing_agent(text)
    if issuing_agent:
        record["mesco_agent"] = issuing_agent

    return {k: v for k, v in record.items() if v is not None and v != ""}
