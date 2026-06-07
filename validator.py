import re
from typing import Any, Dict, List, Optional, Tuple

from models import empty_bl_entity


EGYPT_PORTS = {
    "ALEXANDRIA", "PORT SAID", "DAMIETTA", "SOKHNA", "EL SOKHNA",
    "DEKHEILA", "ADABIYA", "PORT SAID WEST", "AIN SOKHNA",
}

KNOWN_PORTS = {
    *EGYPT_PORTS,
    "HAMBURG", "ROTTERDAM", "GOTHENBURG", "ANTWERP", "SHANGHAI",
    "SINGAPORE", "JEBEL ALI", "PIRAEUS", "FELIXSTOWE", "LE HAVRE",
    "VALENCIA", "BARCELONA", "GENOA", "ISTANBUL", "MERSIN", "AMBARLI",
    "COLOMBO", "PORT KLANG", "TANJUNG PELEPAS", "BUSAN", "NINGBO",
    "QINGDAO", "TIANJIN", "HONG KONG", "YOKOHAMA", "NHAVA SHEVA",
    "DURBAN", "MOMBASA", "LAGOS", "AQABA", "BEIRUT", "HAIFA",
    "LIMASSOL", "MALTA", "GIOIA TAURO",
}

BL_BLACKLIST = {
    "ORIGINAL", "TELEX", "RELEASE", "EXPRESS", "FREIGHT", "COLLECT", "PREPAID",
    "SHIPPER", "CONSIGNEE", "NOTIFY", "VESSEL", "VOYAGE", "CONTAINER", "SEAL",
    "WEIGHT", "MEASUREMENT", "DESCRIPTION", "GOODS", "PORT", "LOADING",
    "DISCHARGE", "DELIVERY", "RECEIPT", "PLACE", "DATE", "NUMBER", "BILL",
    "LADING", "BOOKING", "REFERENCE", "ACID", "ORDER", "INVOICE", "PACKING",
    "GROSS", "NET", "TARE", "TOTAL", "PAGE", "COPY", "DRAFT", "SIGNED",
}


def add_warning(data: Dict[str, Any], message: str) -> None:
    data.setdefault("warnings", [])
    if message not in data["warnings"]:
        data["warnings"].append(message)


def clean_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    value = re.sub(r"\s+", " ", str(value)).strip(" ,;:-")
    return value or None


def normalize_digits(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    return digits or None


def _iso6346_check_digit_valid(compact: str) -> bool:
    """Return True when the last digit is a valid ISO 6346 check digit."""
    if not re.fullmatch(r"[A-Z]{4}\d{7}", compact):
        return False
    values: List[int] = []
    for ch in compact:
        if ch.isdigit():
            values.append(int(ch))
        else:
            values.append(10 + ord(ch) - ord("A"))
    total = sum(v * (2**i) for i, v in enumerate(values))
    check = total % 11
    if check == 10:
        check = 0
    return check == int(compact[-1])


def format_container_number(value: Optional[str]) -> Optional[str]:
    """Normalize to ISO 6346 display form when check digit validates; else keep compact OCR form."""
    if not value:
        return None
    original = str(value).upper()
    slash = re.search(r"\b([A-Z]{4})(\d{6})\s*/\s*(\d)\b", original)
    if slash:
        return f"{slash.group(1)}{slash.group(2)}/{slash.group(3)}"
    compact = re.sub(r"[\s\-/]", "", original)
    m = re.fullmatch(r"([A-Z]{4})(\d{6})(\d)", compact)
    if m:
        candidate = f"{m.group(1)}{m.group(2)}{m.group(3)}"
        if _iso6346_check_digit_valid(candidate):
            return f"{m.group(1)}{m.group(2)}-{m.group(3)}"
        return candidate
    m = re.fullmatch(r"([A-Z]{4})(\d{7})", compact)
    if m:
        candidate = compact
        if _iso6346_check_digit_valid(candidate):
            return f"{m.group(1)}{m.group(2)[:6]}-{m.group(2)[6]}"
        return candidate
    return clean_value(value)


def is_container_number(value: Optional[str]) -> bool:
    if not value:
        return False
    compact = re.sub(r"[\s\-/]", "", value.strip().upper())
    return bool(re.fullmatch(r"[A-Z]{4}\d{7}", compact))


def normalize_numeric(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(" ", "")
    if "," in text and "." not in text:
        parts = text.split(",")
        text = "".join(parts[:-1]) + "." + parts[-1] if len(parts[-1]) == 3 else text.replace(",", ".")
    else:
        text = text.replace(",", "")
    m = re.search(r"\d+(?:\.\d+)?", text)
    return m.group(0) if m else None


def is_likely_bl_number(candidate: str, current_acid: Optional[str] = None) -> bool:
    c = candidate.upper().strip()
    c_compact = re.sub(r"[\s\-]", "", c)

    if len(c_compact) < 5 or len(c_compact) > 25:
        return False
    if c in BL_BLACKLIST or c_compact in BL_BLACKLIST:
        return False
    if is_container_number(c_compact):
        return False
    if current_acid and c_compact == re.sub(r"\D", "", current_acid):
        return False
    if c_compact.isdigit() and len(c_compact) == 19:
        return False

    if re.fullmatch(r"0\d{6,8}", c_compact):
        return False

    if c_compact.isalpha():
        if not re.match(r'^[A-Z]{3,4}[A-Z0-9]{3,}$', c_compact):
            return False

    if c_compact.isdigit():
        return 5 <= len(c_compact) <= 20

    return any(ch.isalpha() for ch in c_compact) and any(ch.isdigit() for ch in c_compact)


def extract_bl_number_regex(text: str, current_acid: Optional[str] = None) -> Optional[str]:
    from bl_number_rules import (
        _digits,
        extract_shipper_glued_bl_number,
        fmc_organization_numbers,
        is_fmc_organization_number,
    )

    glued = extract_shipper_glued_bl_number(text)
    if glued and not is_fmc_organization_number(glued, text):
        return glued

    fmc_nums = {_digits(n) for n in fmc_organization_numbers(text)}

    def _reject_fmc(val: str) -> bool:
        return _digits(val) in fmc_nums or is_fmc_organization_number(val, text)

    upper = text.upper()
    value_pat = r"([A-Z0-9][A-Z0-9 \-]{3,30}[A-Z0-9])"

    patterns = [
        r"B/L\s*No\.?\s*\n?\s*([0-9]+(?: [0-9]+)*)",
        r"M\s*/?\s*BL\s*[:\-]?\s*([A-Z0-9][A-Z0-9 \-]{2,20})",
        r"(?:ETD|JOB\s*NO)\.?\s*[:\-]?\s*[A-Z0-9]*\s+([0-9]{4,10})\s+[A-Z]",
        rf"(?:BILL\s*OF\s*LADING\s*(?:NO|NUMBER|#)|B/L\s*(?:NO|NUMBER|#)|BL\s*(?:NO|NUMBER|#))\.?\s*[:\-]?\s*\n?\s*{value_pat}",
        rf"\bB/L\s*NO\.?\s*{value_pat}",
        rf"\bBLNO\.?\s*[:\-]?\s*{value_pat}",
        rf"\bREF(?:ERENCE)?\s*(?:NO|NUMBER|#)?\.?\s*[:\-]?\s*{value_pat}",
    ]
    for pat in patterns:
        m = re.search(pat, upper, flags=re.I | re.S)
        if not m:
            continue
        val = m.group(1)
        val = re.split(r"\n| {2,}", val)[0].strip(" ,;:-")
        val_compact = val.replace(" ", "")
        if _reject_fmc(val):
            continue
        if is_likely_bl_number(val_compact, current_acid):
            if val_compact.isdigit() and " " in val:
                return val
            return val_compact

    header = upper[:2000]
    for val in re.findall(r"\b[A-Z]{2,5}\d[A-Z0-9\-]{4,20}\b|\b\d{5,20}\b", header):
        if _reject_fmc(val):
            continue
        if is_likely_bl_number(val, current_acid):
            return val
    return None


def extract_house_bl_number_regex(text: str, current_acid: Optional[str] = None) -> Optional[str]:
    patterns = [
        r"\bmesco_houseblno\s*:\s*([A-Z0-9][A-Z0-9\-]{4,25})",
        r"\bhbl_no\s*:\s*([A-Z0-9][A-Z0-9\-]{4,25})",
        r"\bH\s*/?\s*BL\s*(?:NO\.?|NUMBER)?\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-]{4,25})",
        r"\bHOUSE\s+B\s*/?\s*L\s*(?:NO\.?|NUMBER)?\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-]{4,25})",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.I)
        if not m:
            continue
        value = m.group(1).strip().upper()
        if is_likely_bl_number(value, current_acid):
            return value
    return None


def extract_acid_regex(text: str) -> Optional[str]:
    upper = text.upper()
    m = re.search(r"\bACID\s*(?:NO|NUMBER)?\.?\s*[:\-]?\s*([0-9][0-9\s\-]{12,30})", upper)
    if m:
        digits = normalize_digits(m.group(1))
        if digits and 10 <= len(digits) <= 19:
            return digits
    m = re.search(r"\b(\d{19})\b", upper)
    return m.group(1) if m else None


def extract_hs_code_regex(text: str) -> Optional[str]:
    m = re.search(r"H\.?\s*S\.?\s*[- ]?\s*CODE\s*[:\-]?\s*([0-9\s\-/|]{6,80})", text, flags=re.I)
    if not m:
        return None
    codes = re.findall(r"\b\d{6,10}\b", m.group(1))
    out = []
    for c in codes:
        if c not in out:
            out.append(c)
    return "|".join(out) if out else None


def extract_containers_regex(text: str) -> List[Dict[str, Optional[str]]]:
    containers: List[Dict[str, Optional[str]]] = []
    compact = text.upper()
    seen: set[str] = set()

    patterns = [
        r"\b([A-Z]{4})(\d{6})\s*/\s*(\d)\b",
        r"\b([A-Z]{4})\s+(\d{6})[-/](\d)\b",
        r"\b([A-Z]{4})(\d{6})[-](\d)\b",
        r"\b([A-Z]{4}\d{7})\b",
    ]
    for pat in patterns:
        for m in re.finditer(pat, compact):
            if m.lastindex and m.lastindex >= 3:
                container = format_container_number(f"{m.group(1)}{m.group(2)}{m.group(3)}")
            else:
                container = format_container_number(m.group(1))
            if not container or container in seen:
                continue
            seen.add(container)
            seal = None
            tail = compact[m.end() : m.end() + 80]
            sn = re.search(r"\b(?:SN|SEAL)\s*[:#]?\s*([A-Z0-9]{4,20})\b", tail)
            if sn:
                seal = sn.group(1)
            ctype = None
            if re.search(r"40\s*'?HC|40\s*HC", tail):
                ctype = "40HC"
            containers.append({
                "container_number": container,
                "seal_number": seal,
                "container_type": ctype,
                "packages": None,
                "gross_weight_kg": None,
                "measurement_cbm": None,
            })

    seal_match = re.search(r"\b(?:SN|SEAL)\s*(?:NO|NUMBER)?\.?\s*[:\-#]?\s*([A-Z0-9]{4,20})\b", compact)
    if seal_match and containers and not containers[0].get("seal_number"):
        containers[0]["seal_number"] = seal_match.group(1)
    return containers


VOYAGE_BLACKLIST = {
    "LADING", "NUMBER", "ORIGINAL", "BILL", "BLNO", "PREPAID", "COLLECT",
    "SHIPPER", "CONSIGNEE", "NOTIFY", "CARRIER", "WEIGHT", "MEASUREMENT",
    "CONTAINER", "PACKAGES", "DELIVERY", "RECEIPT", "DESTINATION",
    "DESCRIPTION", "DOCUMENTS", "DOCUMENT", "NEGOTIABLE", "NONNEGOTIABLE",
    "FREIGHT", "GOODS", "PLACE", "PORT", "VESSEL", "VOYAGE",
    "COPY", "ORIGIN", "DRAFT", "SIGNED", "TOTAL", "PAGE", "DATE",
}


def _is_likely_voyage(value: Optional[str]) -> bool:
    if not value:
        return False
    upper = value.upper()
    if upper in VOYAGE_BLACKLIST:
        return False
    if re.fullmatch(r"[A-Z]{4,}", upper):
        return False
    if not re.search(r"\d", upper):
        return False
    return 3 <= len(upper) <= 15


def extract_vessel_voyage_port_regex(text: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    upper = text.upper()

    for port in sorted(KNOWN_PORTS, key=len, reverse=True):
        pat = rf"\b([A-Z][A-Z ]{{3,40}}?)\s+([A-Z0-9]{{5,12}})\s+{re.escape(port)}\b"
        m = re.search(pat, upper)
        if m:
            vessel = clean_value(m.group(1))
            voyage = clean_value(m.group(2))
            if voyage and not _is_likely_voyage(voyage):
                voyage = None
            return vessel, voyage, port

    m = re.search(r"(?:OCEAN\s+)?VESSEL\s*[:\-]?\s*([A-Z][A-Z0-9 ]{3,50})(?:\s*/\s*|\s+)([A-Z0-9]{4,12})", upper)
    if m:
        vessel = clean_value(m.group(1))
        voyage = clean_value(m.group(2))
        if voyage and not _is_likely_voyage(voyage):
            voyage = None
        return vessel, voyage, None

    return None, None, None


def extract_route_regex(text: str) -> Tuple[Optional[str], Optional[str]]:
    upper = text.upper()
    origin = None
    destination = None

    for pattern in (
        r"PLACE\s+OF\s+RECEIPT\s+(?:PRECARRI?A?G?E\s+BY\s+)?([A-Z][A-Z ]{2,40})",
        r"PLACE\s+OF\s+RECEIPT\s*\n(?:PRECARRI?A?G?E\s+BY\s+)?([A-Z][A-Z ]{2,40})",
    ):
        m = re.search(pattern, upper)
        if m:
            candidate = clean_value(m.group(1))
            if candidate:
                for port in sorted(KNOWN_PORTS, key=len, reverse=True):
                    if port in candidate:
                        origin = port
                        break
                origin = origin or candidate
                break

    m = re.search(
        r"PORT\s+OF\s+DISCHARGE\s+PLACE\s+OF\s+DELIVERY\s+FREIGHT\s+PAYABLE.*?\n([A-Z][A-Z ]{3,50})",
        upper,
        flags=re.S,
    )
    if m:
        line = clean_value(m.group(1))
        if line:
            if "ALEXANDRIA OLD PORT" in line:
                destination = "ALEXANDRIA OLD PORT"
            else:
                for port in sorted(EGYPT_PORTS, key=len, reverse=True):
                    if port in line:
                        destination = port
                        break
                destination = destination or line

    if not destination:
        for port in sorted(EGYPT_PORTS, key=len, reverse=True):
            if re.search(rf"\b{re.escape(port)}\b", upper):
                destination = port
                break

    return origin, destination


def extract_issue_regex(text: str) -> Tuple[Optional[str], Optional[str]]:
    m = re.search(
        r"PLACE\s+AND\s+DATE\s+OF\s+ISSUE\s+.*?\b([A-Z][A-Z ]{2,40})\s+(\d{4}-\d{2}-\d{2})",
        text.upper(),
        flags=re.S,
    )
    if not m:
        return None, None
    return clean_value(m.group(1)), m.group(2)


def _egypt_context(*parts: Any) -> str:
    return " ".join(str(p) for p in parts if p).upper()


def infer_direction(data: Dict[str, Any]) -> None:
    """Set or correct Import/Export from routing and party addresses.

    Mesco Dataverse: Import = 300000000, Export = 300000001.  The model
    sometimes swaps these; we always re-derive when Egypt appears in the
    destination/consignee side vs origin/shipper side.
    """
    dest_ctx = _egypt_context(
        data.get("mesco_destination"),
        data.get("mesco_consigneeaddress"),
        data.get("mesco_consigneenamecontactno"),
        data.get("mesco_country"),
    )
    origin_ctx = _egypt_context(
        data.get("mesco_origin"),
        data.get("mesco_shipperaddress"),
        data.get("mesco_shippernamecontactno"),
        data.get("mesco_countryoforigin"),
    )
    dest_egypt = "EGYPT" in dest_ctx or any(p in dest_ctx for p in EGYPT_PORTS)
    origin_egypt = "EGYPT" in origin_ctx or any(p in origin_ctx for p in EGYPT_PORTS)

    if dest_egypt and not origin_egypt:
        data["mesco_direction"] = 300000000  # Import
    elif origin_egypt and not dest_egypt:
        data["mesco_direction"] = 300000001  # Export
    elif dest_egypt and data.get("mesco_direction") is None:
        data["mesco_direction"] = 300000000


def validate_and_correct(
    data: Dict[str, Any],
    raw_text: str,
    enrichment_text: Optional[str] = None,
) -> Dict[str, Any]:
    from pydantic import BaseModel
    from models import BLEntity

    base = empty_bl_entity()
    base.update(data or {})
    data = base

    for k, v in list(data.items()):
        if isinstance(v, str):
            data[k] = clean_value(v)

    from bl_number_rules import (
        clean_mesco_notes,
        correct_record_from_page,
        extract_ocean_bl_from_page,
        is_form_or_serial_bl_candidate,
        is_isaly_draft_record,
        is_manifest_header_record,
        is_manifest_house_record,
        normalize_packages_field,
    )

    if not data.get("mesco_acidnumber") and not is_manifest_house_record(data) and not is_isaly_draft_record(data):
        acid = extract_acid_regex(raw_text)
        if acid:
            data["mesco_acidnumber"] = acid
            add_warning(data, "mesco_acidnumber filled by regex fallback.")

    page_bl = extract_ocean_bl_from_page(raw_text)
    if page_bl and not is_isaly_draft_record(data):
        data["mesco_masterblno"] = page_bl
    elif data.get("mesco_masterblno") and is_form_or_serial_bl_candidate(
        str(data["mesco_masterblno"]), raw_text
    ):
        data.pop("mesco_masterblno", None)
        add_warning(data, "mesco_masterblno removed (form/serial number, not ocean B/L).")

    if not data.get("mesco_masterblno"):
        bl = extract_bl_number_regex(raw_text, data.get("mesco_acidnumber"))
        if bl and not is_form_or_serial_bl_candidate(bl, raw_text):
            data["mesco_masterblno"] = bl
            add_warning(data, "mesco_masterblno filled by regex fallback.")

    if is_manifest_header_record(data):
        pkg_val = data.get("cr401_totalpackages")
        if isinstance(pkg_val, (int, float)):
            data["cr401_totalpackages"] = (
                int(pkg_val) if float(pkg_val) == int(float(pkg_val)) else pkg_val
            )
    elif not is_manifest_house_record(data) and not is_isaly_draft_record(data):
        pkg = normalize_packages_field(data.get("cr401_totalpackages"), raw_text)
        if pkg:
            data["cr401_totalpackages"] = pkg

    if not is_isaly_draft_record(data):
        data = correct_record_from_page(data, raw_text)
    if data.get("mesco_notes"):
        data["mesco_notes"] = clean_mesco_notes(data.get("mesco_notes"))

    if not data.get("mesco_houseblno"):
        hbl = extract_house_bl_number_regex(raw_text, data.get("mesco_acidnumber"))
        if hbl:
            data["mesco_houseblno"] = hbl
            add_warning(data, "mesco_houseblno filled by regex fallback.")

    if data.get("mesco_houseblno") and data.get("mesco_masterblno"):
        hbl_compact = re.sub(r"\W", "", str(data["mesco_houseblno"]).upper())
        mbl_compact = re.sub(r"\W", "", str(data["mesco_masterblno"]).upper())
        if hbl_compact == mbl_compact:
            data.pop("mesco_houseblno", None)
            add_warning(data, "mesco_houseblno dropped because it matched the master B/L.")

    enrich_src = enrichment_text if enrichment_text is not None else raw_text

    if not data.get("mesco_hscode") and not is_manifest_house_record(data) and not is_isaly_draft_record(data):
        from pdf_bl_enrichment import extract_hs_codes_from_goods

        hs = extract_hs_code_regex(enrich_src) or extract_hs_codes_from_goods(enrich_src)
        if hs:
            data["mesco_hscode"] = hs
            add_warning(data, "mesco_hscode filled by regex fallback.")

    from pdf_bl_enrichment import enrich_bl_from_pdf_text

    if not is_isaly_draft_record(data):
        data = enrich_bl_from_pdf_text(data, enrich_src)

    hs_val = data.get("mesco_hscode")
    bl_val = data.get("mesco_masterblno")
    if hs_val and bl_val:
        hs_digits = re.sub(r"\D", "", str(hs_val))
        bl_digits = re.sub(r"\D", "", str(bl_val))
        if hs_digits and bl_digits and hs_digits == bl_digits:
            data.pop("mesco_hscode", None)
            add_warning(data, "mesco_hscode removed (matched B/L number, not HS).")

    vessel, voyage, port = extract_vessel_voyage_port_regex(raw_text)
    if vessel and not data.get("mesco_vessel"):
        data["mesco_vessel"] = vessel
        add_warning(data, "mesco_vessel filled by regex fallback.")
    if data.get("extraction_method") in ("pdf_export_lcl_manifest", "pdf_tur_cargo_manifest"):
        bad_vessel = (data.get("mesco_vessel") or "").upper()
        if bad_vessel.startswith("EXPORT LCL") or "MANIFEST" in bad_vessel:
            data.pop("mesco_vessel", None)
    if voyage and not data.get("mesco_voytruckno"):
        data["mesco_voytruckno"] = voyage
        add_warning(data, "mesco_voytruckno filled by regex fallback.")
    if port and not data.get("mesco_origin"):
        data["mesco_origin"] = port
        add_warning(data, "mesco_origin inferred from vessel/voyage/port line.")

    origin, destination = extract_route_regex(raw_text)
    if origin and (
        not data.get("mesco_origin")
        or ((data.get("mesco_origin") or "").upper() in EGYPT_PORTS and origin.upper() not in EGYPT_PORTS)
    ):
        data["mesco_origin"] = origin
        add_warning(data, "mesco_origin filled by route fallback.")
    if destination and not data.get("mesco_destination"):
        data["mesco_destination"] = destination
        add_warning(data, "mesco_destination filled by route fallback.")

    place_of_issue, date_of_issue = extract_issue_regex(raw_text)
    if place_of_issue and not data.get("mesco_placeofissue"):
        data["mesco_placeofissue"] = place_of_issue
        add_warning(data, "mesco_placeofissue filled by regex fallback.")
    if date_of_issue and not data.get("mesco_dateofissue"):
        data["mesco_dateofissue"] = date_of_issue
        add_warning(data, "mesco_dateofissue filled by regex fallback.")

    found_containers = extract_containers_regex(raw_text)
    if not data.get("containers") and found_containers:
        data["containers"] = found_containers
        add_warning(data, "containers filled by regex fallback.")

    if data.get("mesco_acidnumber"):
        acid_raw = re.sub(r"\\+'?$", "", str(data["mesco_acidnumber"]).strip())
        acid_digits = normalize_digits(acid_raw)
        data["mesco_acidnumber"] = acid_digits if acid_digits and len(acid_digits) >= 10 else None

    if data.get("mesco_masterblno") and data.get("mesco_acidnumber"):
        bl_compact = re.sub(r"\D", "", str(data["mesco_masterblno"]))
        acid = str(data["mesco_acidnumber"])
        if bl_compact and bl_compact == acid:
            alt = extract_bl_number_regex(raw_text, acid)
            if alt and re.sub(r"\D", "", alt) != acid:
                data["mesco_masterblno"] = alt
            else:
                add_warning(data, "B/L number equals ACID number; verify manually.")

    data["cr401_totalgrossweight"] = normalize_numeric(data.get("cr401_totalgrossweight"))
    data["cr401_totalvolume"] = normalize_numeric(data.get("cr401_totalvolume"))
    pkg_val = data.get("cr401_totalpackages")
    if is_manifest_house_record(data) and pkg_val is not None:
        data["cr401_totalpackages"] = re.sub(r"\s+", " ", str(pkg_val)).strip()
    elif pkg_val is not None and re.search(
        r"PALLETS|PACKAGES|ROLLS|CARTONS?|UNSTACKABLE", str(pkg_val), re.I
    ):
        data["cr401_totalpackages"] = re.sub(r"\s+", " ", str(pkg_val)).strip()
    else:
        data["cr401_totalpackages"] = normalize_numeric(pkg_val)

    def _normalize_container_type(value: Optional[str]) -> Optional[str]:
        """Drop leading container count, e.g. '1 x 40HC' -> '40HC'."""
        if value is None:
            return None
        text = re.sub(r"\s+", " ", str(value)).strip().upper()
        if not text:
            return None
        m = re.match(r"^\d+\s*X\s*(\d{2}[A-Z]{0,3})$", text)
        if m:
            return m.group(1).replace(" ", "")
        text = re.sub(r"^\d+\s*X\s*", "", text)
        return text or None

    if data.get("mesco_containertype"):
        data["mesco_containertype"] = _normalize_container_type(data["mesco_containertype"])
    if data.get("mesco_containertype2"):
        data["mesco_containertype2"] = _normalize_container_type(data["mesco_containertype2"])
    if data.get("mesco_containertype3"):
        data["mesco_containertype3"] = _normalize_container_type(data["mesco_containertype3"])

    cleaned_containers: List[Dict[str, Optional[str]]] = []
    for item in data.get("containers") or []:
        if isinstance(item, BaseModel):
            item = item.model_dump()
        if not isinstance(item, dict):
            continue
        c = {
            "container_number": clean_value(item.get("container_number")),
            "seal_number": clean_value(item.get("seal_number")),
            "container_type": _normalize_container_type(item.get("container_type")),
            "packages": clean_value(item.get("packages")),
            "gross_weight_kg": normalize_numeric(item.get("gross_weight_kg")),
            "measurement_cbm": normalize_numeric(item.get("measurement_cbm")),
        }
        if c["container_number"]:
            c["container_number"] = format_container_number(c["container_number"]) or c[
                "container_number"
            ].upper().replace(" ", "")
        if any(c.values()):
            cleaned_containers.append(c)

    data["containers"] = cleaned_containers
    if cleaned_containers:
        first = cleaned_containers[0]
        data["container_number"] = format_container_number(
            first.get("container_number") or data.get("container_number")
        )
        data["seal_number"] = data.get("seal_number") or first.get("seal_number")
        data["mesco_containertype"] = data.get("mesco_containertype") or first.get("container_type")

    upper = raw_text.upper()
    data["mesco_transporttype"] = data.get("mesco_transporttype") or 300000000

    if "LCL" in upper or " CFS " in f" {upper} " or "CFS TERMINAL" in upper:
        data["mesco_loadtype"] = 300000001
    elif data.get("mesco_loadtype") is None and (cleaned_containers or re.search(r"\bFCL\b", upper)):
        data["mesco_loadtype"] = 300000000

    if not data.get("mesco_pcfreightterm"):
        if "FREIGHT COLLECT" in upper:
            data["mesco_pcfreightterm"] = "COLLECT"
        elif "FREIGHT PREPAID" in upper:
            data["mesco_pcfreightterm"] = "PREPAID"

    if (
        "TELEX RELEASE" in upper
        or "EXPRESS RELEASE" in upper
        or "EXPRESS BILL OF LADING" in upper
    ):
        data["mesco_telexrelease"] = True

    infer_direction(data)

    # Cap free-text fields so they fit Dataverse column limits.
    cargo_desc = data.get("mesco_cargodescription")
    if isinstance(cargo_desc, str) and len(cargo_desc) > 1500:
        snippet = cargo_desc[:1500]
        cut = max(snippet.rfind("\n"), snippet.rfind(". "), snippet.rfind("; "))
        if cut > 900:
            snippet = snippet[:cut].rstrip(" .,;\n")
        data["mesco_cargodescription"] = snippet.rstrip()

    from bl_number_rules import (
        clean_shipper_address_bl_bleed,
        extract_shipper_glued_bl_number,
        is_fmc_organization_number,
    )

    if data.get("mesco_masterblno") and is_fmc_organization_number(
        str(data["mesco_masterblno"]), raw_text
    ):
        alt = extract_shipper_glued_bl_number(raw_text)
        if alt:
            data["mesco_masterblno"] = alt
            data["mesco_bookingnumber"] = alt

    data["mesco_shipperaddress"] = clean_shipper_address_bl_bleed(
        data.get("mesco_shipperaddress"),
        data.get("mesco_masterblno"),
        raw_text,
    )

    data.setdefault("confidence", {})
    data["confidence"]["post_validation"] = "completed"
    data["confidence"]["bl_number_rule"] = "accepted" if data.get("mesco_masterblno") or data.get("mesco_houseblno") else "missing"
    data["confidence"]["container_number_rule"] = "accepted" if data.get("container_number") else "missing"

    return BLEntity(**data).model_dump()
