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


def is_container_number(value: Optional[str]) -> bool:
    return bool(value and re.fullmatch(r"[A-Z]{4}\d{7}", value.strip().upper().replace(" ", "")))


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

    if c_compact.isalpha():
        if not re.match(r'^[A-Z]{3,4}[A-Z0-9]{3,}$', c_compact):
            return False

    if c_compact.isdigit():
        return 5 <= len(c_compact) <= 20

    return any(ch.isalpha() for ch in c_compact) and any(ch.isdigit() for ch in c_compact)


def extract_bl_number_regex(text: str, current_acid: Optional[str] = None) -> Optional[str]:
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
        if is_likely_bl_number(val_compact, current_acid):
            if val_compact.isdigit() and " " in val:
                return val
            return val_compact

    header = upper[:2000]
    for val in re.findall(r"\b[A-Z]{2,5}\d[A-Z0-9\-]{4,20}\b|\b\d{5,20}\b", header):
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
    for m in re.finditer(r"\b([A-Z]{4}\d{7})\b(?:[/\s\-]+([A-Z0-9]{4,20}))?", compact):
        container = m.group(1)
        seal = m.group(2)
        if container not in [c["container_number"] for c in containers]:
            containers.append({
                "container_number": container,
                "seal_number": seal,
                "container_type": None,
                "packages": None,
                "gross_weight_kg": None,
                "measurement_cbm": None,
            })
    return containers


def extract_vessel_voyage_port_regex(text: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    upper = text.upper()

    for port in sorted(KNOWN_PORTS, key=len, reverse=True):
        pat = rf"\b([A-Z][A-Z ]{{3,40}}?)\s+([A-Z0-9]{{5,12}})\s+{re.escape(port)}\b"
        m = re.search(pat, upper)
        if m:
            vessel = clean_value(m.group(1))
            voyage = clean_value(m.group(2))
            return vessel, voyage, port

    m = re.search(r"(?:OCEAN\s+)?VESSEL\s*[:\-]?\s*([A-Z][A-Z0-9 ]{3,50})(?:\s*/\s*|\s+)([A-Z0-9]{4,12})", upper)
    if m:
        return clean_value(m.group(1)), clean_value(m.group(2)), None

    return None, None, None


def infer_direction(data: Dict[str, Any]) -> None:
    if data.get("mesco_direction") is not None:
        return
    dest = (data.get("mesco_destination") or "").upper()
    origin = (data.get("mesco_origin") or "").upper()
    if any(p in dest for p in EGYPT_PORTS):
        data["mesco_direction"] = 300000000
    elif any(p in origin for p in EGYPT_PORTS):
        data["mesco_direction"] = 300000001


def validate_and_correct(data: Dict[str, Any], raw_text: str) -> Dict[str, Any]:
    from pydantic import BaseModel
    from models import BLEntity

    base = empty_bl_entity()
    base.update(data or {})
    data = base

    for k, v in list(data.items()):
        if isinstance(v, str):
            data[k] = clean_value(v)

    if not data.get("mesco_acidnumber"):
        acid = extract_acid_regex(raw_text)
        if acid:
            data["mesco_acidnumber"] = acid
            add_warning(data, "mesco_acidnumber filled by regex fallback.")

    if not data.get("mesco_masterblno"):
        bl = extract_bl_number_regex(raw_text, data.get("mesco_acidnumber"))
        if bl:
            data["mesco_masterblno"] = bl
            add_warning(data, "mesco_masterblno filled by regex fallback.")

    if not data.get("mesco_houseblno"):
        hbl = extract_house_bl_number_regex(raw_text, data.get("mesco_acidnumber"))
        if hbl:
            data["mesco_houseblno"] = hbl
            add_warning(data, "mesco_houseblno filled by regex fallback.")

    if not data.get("mesco_hscode"):
        hs = extract_hs_code_regex(raw_text)
        if hs:
            data["mesco_hscode"] = hs
            add_warning(data, "mesco_hscode filled by regex fallback.")

    vessel, voyage, port = extract_vessel_voyage_port_regex(raw_text)
    if vessel and not data.get("mesco_vessel"):
        data["mesco_vessel"] = vessel
        add_warning(data, "mesco_vessel filled by regex fallback.")
    if voyage and not data.get("mesco_voytruckno"):
        data["mesco_voytruckno"] = voyage
        add_warning(data, "mesco_voytruckno filled by regex fallback.")
    if port and not data.get("mesco_origin"):
        data["mesco_origin"] = port
        add_warning(data, "mesco_origin inferred from vessel/voyage/port line.")

    found_containers = extract_containers_regex(raw_text)
    if not data.get("containers") and found_containers:
        data["containers"] = found_containers
        add_warning(data, "containers filled by regex fallback.")

    if data.get("mesco_acidnumber"):
        acid_digits = normalize_digits(data["mesco_acidnumber"])
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

    cleaned_containers: List[Dict[str, Optional[str]]] = []
    for item in data.get("containers") or []:
        if isinstance(item, BaseModel):
            item = item.model_dump()
        if not isinstance(item, dict):
            continue
        c = {
            "container_number": clean_value(item.get("container_number")),
            "seal_number": clean_value(item.get("seal_number")),
            "container_type": clean_value(item.get("container_type")),
            "packages": clean_value(item.get("packages")),
            "gross_weight_kg": normalize_numeric(item.get("gross_weight_kg")),
            "measurement_cbm": normalize_numeric(item.get("measurement_cbm")),
        }
        if c["container_number"]:
            c["container_number"] = c["container_number"].upper().replace(" ", "")
        if any(c.values()):
            cleaned_containers.append(c)

    data["containers"] = cleaned_containers
    if cleaned_containers:
        first = cleaned_containers[0]
        data["container_number"] = data.get("container_number") or first.get("container_number")
        data["seal_number"] = data.get("seal_number") or first.get("seal_number")
        data["mesco_containertype"] = data.get("mesco_containertype") or first.get("container_type")

    upper = raw_text.upper()
    data["mesco_transporttype"] = data.get("mesco_transporttype") or 300000000

    if data.get("mesco_loadtype") is None:
        if "LCL" in upper:
            data["mesco_loadtype"] = 300000001
        elif cleaned_containers or re.search(r"\bFCL\b", upper):
            data["mesco_loadtype"] = 300000000

    if not data.get("mesco_pcfreightterm"):
        if "FREIGHT COLLECT" in upper:
            data["mesco_pcfreightterm"] = "COLLECT"
        elif "FREIGHT PREPAID" in upper:
            data["mesco_pcfreightterm"] = "PREPAID"

    if "TELEX RELEASE" in upper or "EXPRESS RELEASE" in upper:
        data["mesco_telexrelease"] = True

    infer_direction(data)

    data.setdefault("confidence", {})
    data["confidence"]["post_validation"] = "completed"
    data["confidence"]["bl_number_rule"] = "accepted" if data.get("mesco_masterblno") or data.get("mesco_houseblno") else "missing"
    data["confidence"]["container_number_rule"] = "accepted" if data.get("container_number") else "missing"

    return BLEntity(**data).model_dump()
