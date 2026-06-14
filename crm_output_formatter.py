"""Convert flat BLEntity records into Dynamics CRM JSON format.

The project uses two Dataverse response examples as target shapes:
``master.json`` is a single master operation with nested houses, containers,
and cargo. ``house.json`` is a collection of house operations with a nested
master operation per row.

This module intentionally keeps extracted values in those relationship shapes
without copying sample GUIDs, formatted lookup annotations, or extraction-only
debug fields into the upload payload.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

MASTER_BL_TYPE = 886150001
HOUSE_BL_TYPE = 886150002
TRANSPORT_SEA = 300000000
LOAD_LCL = 300000001
LOAD_FCL = 300000000
DIRECTION_IMPORT = 300000000
DIRECTION_EXPORT = 300000001

MASTER_HOUSES_KEY = "mesco_Operation_mesco_Operation_mesco_Operation"
MASTER_CONTAINERS_KEY = "mesco_Container_MasterOperation_mesco_Operation"
MASTER_CARGO_KEY = "mesco_Cargo_MasterOperation_mesco_Operation"
HOUSE_MASTER_KEY = "mesco_Operation"
HOUSE_CONTAINERS_KEY = "mesco_Container_mesco_houses"
HOUSE_CARGO_KEY = "mesco_Cargo_HouseOperation_mesco_Operation"


def _has(val: Any) -> bool:
    return val is not None and val != "" and val != [] and val != {}


def _parse_numeric(value: Any) -> Optional[float]:
    """Extract a numeric value from plain numbers or strings like '7 PALLETS' / '51.0 kg'."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").strip()
    if not text:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    return float(match.group(1))


def _set_numeric_field(target: Dict[str, Any], key: str, value: Any) -> None:
    parsed = _parse_numeric(value)
    if parsed is not None:
        target[key] = int(parsed) if parsed.is_integer() else round(parsed, 3)


def _normalize_container_number(value: Any) -> Optional[str]:
    if not _has(value):
        return None
    return re.sub(r"\s+", "", str(value).strip()).upper()


def _um_hint_from_container_type(container_type: Any) -> Optional[str]:
    if not _has(container_type):
        return None
    text = str(container_type).upper()
    if "40" in text:
        return "40 FT"
    if "20" in text:
        return "20 FT"
    return None


_PACKAGE_UNIT_ALIASES: Dict[str, Tuple[str, ...]] = {
    "DRUMS": ("DRUM", "DRUMS", "DRM", "DRMS"),
    "PALLETS": ("PALLET", "PALLETS", "PLT", "PLTS"),
    "CARTONS": ("CARTON", "CARTONS", "CTN", "CTNS"),
    "PACKAGES": ("PACKAGE", "PACKAGES", "PKG", "PKGS"),
    "BOXES": ("BOX", "BOXES"),
    "BAGS": ("BAG", "BAGS"),
    "ROLLS": ("ROLL", "ROLLS"),
    "CASES": ("CASE", "CASES"),
    "CRATES": ("CRATE", "CRATES"),
    "BUNDLES": ("BUNDLE", "BUNDLES"),
    "CANS": ("CAN", "CANS"),
}

_PACKAGE_UNIT_BY_TOKEN = {
    alias: label
    for label, aliases in _PACKAGE_UNIT_ALIASES.items()
    for alias in aliases
}
_PACKAGE_UNIT_PATTERN = re.compile(
    r"\b(?:(\d{1,7}(?:\.\d+)?)\s*)?(?:\([^)]{1,40}\)\s*)?"
    r"(DRUMS?|DRMS?|DRM|PALLETS?|PLTS?|PLT|CARTONS?|CTNS?|CTN|"
    r"PACKAGES?|PKGS?|PKG|BOXES?|BOX|BAGS?|BAG|ROLLS?|ROLL|"
    r"CASES?|CASE|CRATES?|CRATE|BUNDLES?|BUNDLE|CANS?|CAN)\b",
    re.I,
)
_OUTER_PACKAGE_UNITS = {"PALLETS", "PACKAGES"}


def _canonical_package_unit(value: Any) -> Optional[str]:
    if not _has(value):
        return None
    token = re.sub(r"[^A-Z0-9]+", "", str(value).upper())
    if not token:
        return None
    return _PACKAGE_UNIT_BY_TOKEN.get(token)


def infer_package_unit_label(*values: Any) -> Optional[str]:
    """Infer the Dataverse U/M Packages lookup label from cargo text.

    In phrases like ``08 PALLETS STC 80 DRUMS`` the cargo package unit is the
    contained unit (DRUMS), not the outer pallet count.  The scoring below
    favours units near STC/contain wording, then larger package counts, while
    still falling back to direct cargo_type values such as "drums".
    """
    best: Optional[Tuple[float, int, str]] = None
    order = 0

    for value in values:
        if not _has(value):
            continue
        text = re.sub(r"\s+", " ", str(value)).strip()
        if not text:
            continue
        upper = text.upper()

        direct = _canonical_package_unit(upper)
        if direct:
            candidate = (75.0, order, direct)
            if best is None or candidate[:2] > best[:2]:
                best = candidate
            order += 1

        for match in _PACKAGE_UNIT_PATTERN.finditer(upper):
            unit = _canonical_package_unit(match.group(2))
            if not unit:
                continue
            qty = _parse_numeric(match.group(1))
            before = upper[max(0, match.start() - 90):match.start()]
            after = upper[match.end():match.end() + 40]

            score = 10.0
            if qty is not None:
                score += min(qty, 10000.0)
            if re.search(r"\b(STC|SAID TO CONTAIN|CONTAIN|CONTAINS|CONTAINING)\b", before):
                score += 1000.0
            if unit not in _OUTER_PACKAGE_UNITS:
                score += 50.0
            if unit in _OUTER_PACKAGE_UNITS and re.search(r"\bSTC\b", after):
                score -= 100.0

            candidate = (score, order, unit)
            if best is None or candidate[:2] > best[:2]:
                best = candidate
            order += 1

    return best[2] if best else None


def _pick(src: Dict[str, Any], *fields: str) -> Dict[str, Any]:
    return {k: src[k] for k in fields if _has(src.get(k))}


_META_KEYS = {
    "extraction_method", "extraction_quality", "confidence", "warnings",
    "_source_info", "_routing", "_master_code", "_house_code",
    "document_type", "record_index", "sheet_name", "source_row",
    "source_extraction_method", "manifest_values", "unique_key",
    "_error", "spreadsheet_record", "financial_processing",
    "values_by_header", "text", "cargo_type", "hbl_no",
    "container_number", "seal_number", "containers",
    "cargo_value", "consignee_contact_details",
    "hbl_type", "nomination_term", "delivery_term", "shipment_status",
    "rate", "carrier", "mbl_shipper", "delivery_agent", "mbl_acid",
    "schedule", "agent",
    "_mbl_shipper", "_mbl_consignee", "_mbl_acid",
    "_mbl_bookingno", "_mbl_masterblno",
    "_cargo_page1_references", "_attached_list_house_refs",
    "mesco_servicetype_text", "cargo_lines",
    "_imo_detected", "mesco_imo", "mesco_chemical",
    "mesco_unno", "mesco_flashptc",
}

_MASTER_SKIP = {"mesco_houseblno"}
_ROOT_METADATA_KEYS = {
    "@odata.context",
    "@Microsoft.Dynamics.CRM.totalrecordcount",
    "@Microsoft.Dynamics.CRM.totalrecordcountlimitexceeded",
    "@Microsoft.Dynamics.CRM.globalmetadataversion",
}

_RELATIONSHIP_KEYS = {
    MASTER_HOUSES_KEY,
    MASTER_CONTAINERS_KEY,
    MASTER_CARGO_KEY,
    HOUSE_MASTER_KEY,
    HOUSE_CONTAINERS_KEY,
    HOUSE_CARGO_KEY,
    f"{HOUSE_CONTAINERS_KEY}@odata.nextLink",
    f"{HOUSE_CARGO_KEY}@odata.nextLink",
}
_PRESERVE_EMPTY_LIST_KEYS = {
    MASTER_HOUSES_KEY,
    MASTER_CONTAINERS_KEY,
    MASTER_CARGO_KEY,
    HOUSE_CONTAINERS_KEY,
    HOUSE_CARGO_KEY,
    "value",
}
_PRESERVE_ANNOTATION_KEYS = {
    "@odata.context",
    "@Microsoft.Dynamics.CRM.totalrecordcount",
    "@Microsoft.Dynamics.CRM.totalrecordcountlimitexceeded",
}

_DEFAULT_MASTER_CONTEXT = (
    "https://mgc.crm4.dynamics.com/api/data/v9.2/"
    "$metadata#mesco_operations("
    "mesco_Operation_mesco_Operation_mesco_Operation(),"
    "mesco_Container_MasterOperation_mesco_Operation(),"
    "mesco_Cargo_MasterOperation_mesco_Operation())/$entity"
)
_DEFAULT_HOUSE_CONTEXT = (
    "https://mgc.crm4.dynamics.com/api/data/v9.2/"
    "$metadata#mesco_operations("
    "mesco_Operation(),mesco_Container_mesco_houses(),"
    "mesco_Cargo_HouseOperation_mesco_Operation())"
)


def _load_json_template(filename: str) -> Dict[str, Any]:
    path = Path(__file__).with_name(filename)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


_MASTER_TEMPLATE = _load_json_template("master.json")
_HOUSE_TEMPLATE = _load_json_template("house.json")


def _blank_value(value: Any) -> Any:
    if isinstance(value, list):
        return []
    if isinstance(value, dict):
        return {k: _blank_value(v) for k, v in value.items()}
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return None
    return None


def _blank_template(template: Dict[str, Any]) -> Dict[str, Any]:
    return {key: _blank_value(value) for key, value in template.items()}


def _prune_output(value: Any, key: Optional[str] = None) -> Any:
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for child_key, child_value in value.items():
            if child_key.startswith("_"):
                continue
            if "@" in child_key and child_key not in _PRESERVE_ANNOTATION_KEYS:
                continue
            if isinstance(child_value, dict) and child_key != HOUSE_MASTER_KEY:
                continue
            pruned = _prune_output(child_value, child_key)
            if pruned is None:
                continue
            if pruned == {}:
                continue
            if pruned == [] and child_key not in _PRESERVE_EMPTY_LIST_KEYS:
                continue
            cleaned[child_key] = pruned
        return cleaned
    if isinstance(value, list):
        return [
            pruned
            for item in value
            if (pruned := _prune_output(item)) not in (None, {}, [])
        ]
    return value if value is not None else None


def _first_house_template() -> Dict[str, Any]:
    houses = _MASTER_TEMPLATE.get(MASTER_HOUSES_KEY) or []
    if houses and isinstance(houses[0], dict):
        return houses[0]
    values = _HOUSE_TEMPLATE.get("value") or []
    if values and isinstance(values[0], dict):
        return {k: v for k, v in values[0].items() if k != HOUSE_MASTER_KEY}
    return {}


def _first_master_in_house_template() -> Dict[str, Any]:
    values = _HOUSE_TEMPLATE.get("value") or []
    if values and isinstance(values[0], dict) and isinstance(values[0].get(HOUSE_MASTER_KEY), dict):
        return values[0][HOUSE_MASTER_KEY]
    return _MASTER_TEMPLATE


def _first_container_template(master_level: bool = True) -> Dict[str, Any]:
    if master_level:
        containers = _MASTER_TEMPLATE.get(MASTER_CONTAINERS_KEY) or []
    else:
        values = _HOUSE_TEMPLATE.get("value") or []
        containers = values[0].get(HOUSE_CONTAINERS_KEY) if values and isinstance(values[0], dict) else []
    return containers[0] if containers and isinstance(containers[0], dict) else {}


def _first_cargo_template(master_level: bool = True) -> Dict[str, Any]:
    if master_level:
        cargos = _MASTER_TEMPLATE.get(MASTER_CARGO_KEY) or []
    else:
        values = _HOUSE_TEMPLATE.get("value") or []
        cargos = values[0].get(HOUSE_CARGO_KEY) if values and isinstance(values[0], dict) else []
    return cargos[0] if cargos and isinstance(cargos[0], dict) else {}


def _clean_metadata(rec: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in rec.items() if k not in _META_KEYS}


def _crm_field_items(src: Dict[str, Any]) -> Iterable[tuple[str, Any]]:
    for key, value in _clean_metadata(src).items():
        if key in _RELATIONSHIP_KEYS or key in _ROOT_METADATA_KEYS:
            continue
        if key.startswith("@") or "@" in key:
            continue
        if key.startswith("mesco_") or key.startswith("cr401_") or key in {
            "statecode", "statuscode", "exchangerate", "timezoneruleversionnumber",
        }:
            yield key, value


def _apply_fields(target: Dict[str, Any], src: Dict[str, Any], skip: Optional[set[str]] = None) -> Dict[str, Any]:
    skip = skip or set()
    for key, value in _crm_field_items(src):
        if key in skip or not _has(value):
            continue
        target[key] = value
    return target


_DG_RESPONSE_FIELDS = (
    "dg_proper_shipping_name",
    "dg_packing_group",
    "dg_cas_no",
)


def _preserve_dg_response_fields(target: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    for key in _DG_RESPONSE_FIELDS:
        if _has(src.get(key)):
            target[key] = src[key]
    return target


def _project_to_template(template: Dict[str, Any], src: Dict[str, Any], skip: Optional[set[str]] = None) -> Dict[str, Any]:
    target = _blank_template(template) if template else {}
    return _apply_fields(target, src, skip=skip)


# ---------------------------------------------------------------------------
# Lookup-name derivation
#
# The AI/regex extractor stores party identities in verbose text fields
# (mesco_shippernamecontactno / mesco_consigneenamecontactno).  Dataverse,
# however, exposes shipper/consignee/notify as *account* lookups and country
# as an xollsp_country lookup.  The uploader can resolve a clean name → GUID
# and emit the @odata.bind link, but only if we hand it a lookup-name field
# (mesco_shipper / mesco_consignee / mesco_country / ...).
#
# This helper derives those canonical lookup-name fields from whatever the
# extractor produced, preferring an explicit clean name when the model
# supplied one.  The verbose text field is left untouched so it still lands
# in the human-readable column as a fallback when the account does not exist.
# ---------------------------------------------------------------------------

# Tokens that mark the start of contact / address noise on a party line.
_PARTY_NOISE_RE = re.compile(
    r"\b(?:ATTN|ATTENTION|TEL|TELE|PHONE|FAX|EMAIL|E-MAIL|MOB|MOBILE|CELL|"
    r"CONTACT|P\.?O\.?\s*BOX|ZIP|POSTAL|VAT|TAX\s*ID)\b\s*[:.\-]?",
    re.I,
)

# Lookup-name fields that already arrive as canonical strings and only need to
# be carried through to the operation payload for the uploader to resolve.
_PASS_THROUGH_LOOKUP_FIELDS = (
    "mesco_country",
    "mesco_countryoforigin",
    "mesco_shippingline",
    "mesco_origin",
    "mesco_destination",
    "mesco_vessel",
    "mesco_agent",
)

# Standard Incoterms (2000/2010/2020).  Used to strip a trailing place name
# ("CIF ALEXANDRIA" -> "CIF") so the value matches an xollsp_incoterm record.
_INCOTERMS = {
    "EXW", "FCA", "FAS", "FOB", "CFR", "CIF", "CPT", "CIP",
    "DAP", "DPU", "DDP", "DAT", "DAF", "DES", "DEQ", "DDU",
}
_INCOTERM_RE = re.compile(r"\b(" + "|".join(sorted(_INCOTERMS)) + r")\b", re.I)
_MESCO_ACCOUNT_LOOKUP_NAME = "MARINE AND ENGINEERING SERVICES COMPANY (MESCO)"


def _is_mesco_party(*parts: Any) -> bool:
    upper = " ".join(str(part) for part in parts if _has(part)).upper()
    return "MESCO" in upper


def _canonical_incoterm(value: Any) -> Optional[str]:
    """Reduce an incoterm value to its 3-letter code (e.g. 'CIF ALEXANDRIA' -> 'CIF')."""
    if not _has(value):
        return None
    match = _INCOTERM_RE.search(str(value).upper())
    if match:
        return match.group(1).upper()
    text = str(value).strip()
    return text or None


def _finalize_consignee_address(operation: Dict[str, Any]) -> Dict[str, Any]:
    """Drop bogus OCR addresses; clear CRM when consignee is to-order (no street)."""
    from pdf_bl_enrichment import (
        _is_to_order_consignee_name,
        sanitize_party_address,
    )

    raw = operation.get("mesco_consigneeaddress")
    cleaned = sanitize_party_address(raw)
    if cleaned:
        operation["mesco_consigneeaddress"] = cleaned
        return operation
    if _is_to_order_consignee_name(operation.get("mesco_consigneenamecontactno")):
        operation["mesco_consigneeaddress"] = ""
    elif raw and str(raw).strip():
        operation["mesco_consigneeaddress"] = ""
    else:
        operation.pop("mesco_consigneeaddress", None)
    return operation


def _split_party_block(value: Any) -> tuple[Optional[str], Optional[str]]:
    """Split a party block into (company name, address lines)."""
    if not _has(value):
        return None, None
    text = str(value).replace("\r", "\n")
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not lines:
        return None, None
    name = _clean_company_name(lines[0]) or lines[0].strip()
    address = "\n".join(lines[1:]).strip() if len(lines) > 1 else None
    if address:
        address = address[:250]
    return name, address or None


def _infer_country_from_text(*parts: Any) -> Optional[str]:
    upper = _egypt_context(*parts)
    if "EGYPT" in upper or "EG " in upper or ", EG" in upper:
        return "Egypt"
    if "INDIA" in upper or ", IN" in upper or " U.P)" in upper:
        return "India"
    if "CHINA" in upper or ", CN" in upper:
        return "China"
    if "TURKEY" in upper or ", TR" in upper:
        return "Turkey"
    return None


def _egypt_context(*parts: Any) -> str:
    return " ".join(str(p) for p in parts if p).upper()


def _infer_totals_from_cargo_description(desc: Any) -> Dict[str, Any]:
    """Best-effort package count from narrative cargo text (e.g. 'TOTAL 320 ... 18 PALLETS')."""
    if not _has(desc):
        return {}
    text = str(desc)
    out: Dict[str, Any] = {}
    total_m = re.search(r"\bTOTAL\s+(\d+)\b", text, re.I)
    if total_m:
        out["cr401_totalpackages"] = int(total_m.group(1))
    pallets_m = re.search(r"(\d+)\s+(?:WOODEN\s+)?PALLETS", text, re.I)
    if pallets_m and "cr401_totalpackages" not in out:
        out["cr401_totalpackages"] = int(pallets_m.group(1))
    return out


def _apply_party_fields(operation: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    """Map verbose party blocks to name + address columns (Dynamics form layout)."""
    if not _has(operation.get("mesco_shippernamecontactno")) or not _has(
        operation.get("mesco_shipperaddress")
    ):
        name, addr = _split_party_block(src.get("mesco_shippernamecontactno"))
        if name and not _has(operation.get("mesco_shipper")):
            operation["mesco_shipper"] = name
        if name and not _has(operation.get("mesco_shippernamecontactno")):
            operation["mesco_shippernamecontactno"] = name[:100]
        if addr and not _has(operation.get("mesco_shipperaddress")):
            operation["mesco_shipperaddress"] = addr

    if not _has(operation.get("mesco_consigneenamecontactno")) or not _has(
        operation.get("mesco_consigneeaddress")
    ):
        name, addr = _split_party_block(src.get("mesco_consigneenamecontactno"))
        if name and not _has(operation.get("mesco_consignee")):
            operation["mesco_consignee"] = name
        if name and not _has(operation.get("mesco_consigneenamecontactno")):
            operation["mesco_consigneenamecontactno"] = name[:100]
        if addr and not _has(operation.get("mesco_consigneeaddress")):
            from pdf_bl_enrichment import sanitize_party_address

            operation["mesco_consigneeaddress"] = sanitize_party_address(addr)

    operation = _finalize_consignee_address(operation)
    if not _has(operation.get("mesco_countryoforigin")):
        country = _infer_country_from_text(
            operation.get("mesco_shipperaddress"),
            src.get("mesco_shipperaddress"),
            src.get("mesco_shippernamecontactno"),
        )
        if country:
            operation["mesco_countryoforigin"] = country
    if not _has(operation.get("mesco_country")):
        country = _infer_country_from_text(
            operation.get("mesco_consigneeaddress"),
            src.get("mesco_consigneeaddress"),
            src.get("mesco_consigneenamecontactno"),
            src.get("mesco_origin"),
        )
        if country:
            operation["mesco_country"] = country
    return operation


def _clean_company_name(value: Any) -> Optional[str]:
    """Extract a resolvable company name from a verbose party block.

    Takes the first non-empty line (the legal/company name on a B/L party
    block) and strips trailing contact noise (ATTN/TEL/FAX/EMAIL/...).
    """
    if not _has(value):
        return None
    text = str(value).replace("\r", "\n")
    first_line = None
    for line in text.split("\n"):
        stripped = line.strip(" \t,-:;|")
        if stripped:
            first_line = stripped
            break
    if not first_line:
        return None
    first_line = _PARTY_NOISE_RE.split(first_line)[0]
    first_line = re.sub(r"\s{2,}", " ", first_line).strip(" \t,-:;|")
    # A bare "SAME AS ..." reference cannot be resolved to an account.
    if re.match(r"^SAME\s+AS\b", first_line, re.I):
        return None
    return first_line or None


def _derive_operation_lookups(src: Dict[str, Any]) -> Dict[str, Any]:
    """Build canonical lookup-name fields the uploader can resolve to GUIDs."""
    out: Dict[str, Any] = {}

    def _account_lookup_name(explicit: Any, block: Any) -> Optional[str]:
        if _is_mesco_party(explicit, block):
            return _MESCO_ACCOUNT_LOOKUP_NAME
        name = _clean_company_name(explicit) if _has(explicit) else None
        if not name and _has(explicit) and "," not in str(explicit) and len(str(explicit)) <= 80:
            name = str(explicit).strip()
        if not name:
            name = _clean_company_name(block)
        return name

    consignee = _account_lookup_name(
        src.get("mesco_consignee"),
        src.get("mesco_consigneenamecontactno"),
    )
    if _has(consignee):
        out["mesco_consignee"] = consignee

    shipper = _account_lookup_name(
        src.get("mesco_shipper"),
        src.get("mesco_shippernamecontactno"),
    )
    if _has(shipper):
        out["mesco_shipper"] = shipper

    notify = _account_lookup_name(
        src.get("mesco_notify1"),
        src.get("mesco_notifyaddress"),
    )
    if _has(notify):
        out["mesco_notify1"] = notify
    elif _has(consignee) and re.match(r"^SAME\s+AS\b", str(src.get("mesco_notify1") or ""), re.I):
        out["mesco_notify1"] = consignee

    for field in _PASS_THROUGH_LOOKUP_FIELDS:
        if _has(src.get(field)):
            out[field] = src[field]

    incoterm = _canonical_incoterm(src.get("mesco_incoterm"))
    if incoterm:
        out["mesco_incoterm"] = incoterm

    return out


# Fields whose derived/canonical form should replace any value already projected
# (the canonical form is strictly more resolvable than the raw extracted text).
_OVERRIDE_LOOKUP_FIELDS = {"mesco_incoterm", "mesco_notify1"}


def _apply_operation_lookups(operation: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    """Merge derived lookup-name fields into *operation*.

    Most fields are only filled when absent; canonicalized fields
    (``_OVERRIDE_LOOKUP_FIELDS``) replace the raw projected value.
    """
    for key, value in _derive_operation_lookups(src).items():
        if key in _OVERRIDE_LOOKUP_FIELDS or not _has(operation.get(key)):
            operation[key] = value
    return operation


def _operation_defaults(is_master: bool) -> Dict[str, Any]:
    return {
        "mesco_bltype": MASTER_BL_TYPE if is_master else HOUSE_BL_TYPE,
        "mesco_transporttype": TRANSPORT_SEA,
        "mesco_loadtype": LOAD_LCL,
        "mesco_direction": DIRECTION_IMPORT,
        "mesco_hideconsignee": False,
        "mesco_crossbooking": False,
        "mesco_form13delivered": False,
        "mesco_consolidation": False,
        "mesco_telexrelease": False,
        "mesco_bldraftconfirmed": False,
        "mesco_shippertoagent": False,
    }


def _fill_operation_defaults(operation: Dict[str, Any], is_master: bool) -> Dict[str, Any]:
    for key, value in _operation_defaults(is_master).items():
        if not _has(operation.get(key)):
            operation[key] = value
    return operation


def _build_cargo_from_line(line: Dict[str, Any]) -> Dict[str, Any]:
    cargo = _project_to_template(_first_cargo_template(master_level=True), {})
    if _has(line.get("mesco_descriptionofgoods")):
        cargo["mesco_descriptionofgoods"] = _truncate_to_limit(
            str(line["mesco_descriptionofgoods"]),
            _DATAVERSE_DESC_OF_GOODS_MAX,
        )
    _set_numeric_field(cargo, "mesco_noofpackages", line.get("mesco_noofpackages"))
    _set_numeric_field(cargo, "mesco_grosskg", line.get("mesco_grosskg"))
    _set_numeric_field(cargo, "mesco_volcbm", line.get("mesco_volcbm"))
    package_unit = infer_package_unit_label(
        line.get("mesco_umpackages"),
        line.get("package_unit"),
        line.get("cargo_type"),
        line.get("mesco_noofpackages"),
        line.get("mesco_descriptionofgoods"),
    )
    if package_unit:
        cargo["mesco_umpackages"] = package_unit
    _apply_imo_fields_to_cargo(cargo, line)
    return {k: v for k, v in cargo.items() if _has(v)}


def _records_have_per_house_cargo(records: List[Dict[str, Any]]) -> bool:
    """True when each house record carries its own cargo row (Arkas attached list)."""
    if not records:
        return False
    return all(isinstance(rec, dict) and rec.get("_per_house_cargo") for rec in records)


def _cargo_rows_from_record(rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    lines = rec.get("cargo_lines")
    if isinstance(lines, list) and lines:
        rows = [_build_cargo_from_line(ln) for ln in lines if isinstance(ln, dict)]
        rows = [row for row in rows if row]
    else:
        cargo = _build_cargo_from_record(rec)
        rows = [cargo] if cargo else []
    hbl = rec.get("mesco_houseblno")
    if hbl:
        for row in rows:
            row["_house_hbl"] = str(hbl).strip()
    return rows


_DATAVERSE_DESC_OF_GOODS_MAX = 1500


def _truncate_to_limit(value: str, limit: int) -> str:
    """Truncate at the last full line/sentence boundary inside the limit."""
    if len(value) <= limit:
        return value
    snippet = value[:limit]
    cut_at = max(snippet.rfind("\n"), snippet.rfind(". "), snippet.rfind("; "))
    if cut_at > limit * 0.6:
        snippet = snippet[:cut_at].rstrip(" .,;\n")
    return snippet.rstrip()


def _dg_description_parts(rec: Dict[str, Any]) -> List[str]:
    if not rec.get("_msds_dg_document"):
        return []
    parts: List[str] = []
    proper = rec.get("dg_proper_shipping_name")
    packing = rec.get("dg_packing_group")
    cas_no = rec.get("dg_cas_no")
    if _has(proper):
        parts.append(f"UN proper shipping name: {proper}")
    if _has(packing):
        parts.append(f"Packing group: {packing}")
    if _has(cas_no):
        parts.append(f"CAS No.: {cas_no}")
    return parts


def _build_cargo_from_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    cargo = _project_to_template(_first_cargo_template(master_level=True), {})
    desc_parts = []
    hs_value = rec.get("mesco_hscode")
    cargo_desc = rec.get("mesco_cargodescription")

    # Avoid duplicating HS codes that already appear inline in the cargo text.
    if _has(hs_value):
        cargo_str = str(cargo_desc or "")
        hs_digits = re.sub(r"\D", "", str(hs_value))
        hs_inline = bool(
            cargo_desc
            and (
                re.search(r"\bHS\s*CODE\b", cargo_str, re.I)
                or (hs_digits and hs_digits in re.sub(r"\D", "", cargo_str))
            )
        )
        if not hs_inline:
            desc_parts.append(f"HS: {hs_value}")
    if _has(cargo_desc):
        desc_parts.append(str(cargo_desc))
    for dg_part in _dg_description_parts(rec):
        if dg_part not in desc_parts:
            desc_parts.append(dg_part)
    if _has(rec.get("cargo_type")):
        ct = str(rec["cargo_type"]).strip()
        if ct not in " ".join(desc_parts):
            desc_parts.append(ct)
    if desc_parts:
        cargo["mesco_descriptionofgoods"] = _truncate_to_limit(
            "\n".join(desc_parts),
            _DATAVERSE_DESC_OF_GOODS_MAX,
        )
    _set_numeric_field(cargo, "mesco_noofpackages", rec.get("cr401_totalpackages"))
    _set_numeric_field(cargo, "mesco_grosskg", rec.get("cr401_totalgrossweight"))
    _set_numeric_field(cargo, "mesco_volcbm", rec.get("cr401_totalvolume"))
    package_unit = infer_package_unit_label(
        rec.get("mesco_umpackages"),
        rec.get("package_unit"),
        rec.get("cargo_type"),
        rec.get("cr401_totalpackages"),
        rec.get("mesco_cargodescription"),
        cargo.get("mesco_descriptionofgoods"),
    )
    if package_unit:
        cargo["mesco_umpackages"] = package_unit
    _apply_imo_fields_to_cargo(cargo, rec)
    return {k: v for k, v in cargo.items() if _has(v)}


def _apply_imo_fields_to_cargo(cargo: Dict[str, Any], src: Dict[str, Any]) -> None:
    """Copy operation/MSDS IMO facts into the nested cargo row shown by CRM."""
    imo_class = src.get("mesco_imoclass")
    un_number = src.get("mesco_unno") or src.get("mesco_unnumber")
    flash = src.get("mesco_flashptc")
    detected = bool(
        src.get("_imo_detected")
        or src.get("mesco_imo") is True
        or _has(imo_class)
        or _has(un_number)
    )
    if not detected:
        return

    cargo["mesco_imo"] = True
    cargo["mesco_chemical"] = True
    if _has(imo_class):
        _set_numeric_field(cargo, "mesco_imoclass", imo_class)
    if _has(un_number):
        m = re.search(r"\b(?:UN\s*)?(\d{4})\b", str(un_number), re.I)
        cargo["mesco_unno"] = m.group(1) if m else str(un_number).strip()
    if _has(flash):
        _set_numeric_field(cargo, "mesco_flashptc", flash)


def _build_house(rec: Dict[str, Any], master_mbl: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Build a house operation. Returns None when no distinct house B/L exists."""
    hbl = rec.get("mesco_houseblno")
    if not _has(hbl):
        return None

    hbl = str(hbl).strip()
    master_mbl = (master_mbl or rec.get("mesco_masterblno") or "").strip() or None
    if master_mbl and hbl.upper() == str(master_mbl).upper():
        return None

    skip = {"mesco_houseblno", "mesco_masterblno"}
    if rec.get("_attached_list_house"):
        skip |= {
            "cr401_totalpackages",
            "cr401_totalgrossweight",
            "cr401_totalvolume",
            "mesco_cargodescription",
            "mesco_nooforgbls",
            "mesco_hscode",
            "mesco_dateofissue",
            "mesco_placeofissue",
            "mesco_shippedonboarddate",
        }

    house = _project_to_template(_first_house_template(), rec, skip=skip)
    _preserve_dg_response_fields(house, rec)
    house["mesco_bltype"] = HOUSE_BL_TYPE
    house["mesco_masterblno"] = hbl
    if master_mbl:
        house["mesco_masterbllinkno"] = master_mbl
    _fill_operation_defaults(house, is_master=False)
    _apply_party_fields(house, rec)
    _apply_operation_lookups(house, rec)
    return house


def _build_container_from_item(c: Dict[str, Any], master_level: bool = True) -> Dict[str, Any]:
    entry = _project_to_template(_first_container_template(master_level=master_level), {})
    container_no = _normalize_container_number(c.get("container_number"))
    if container_no:
        # Dynamics Container No is a lookup to mesco_containerno (primary name = number).
        entry["mesco_containerno"] = container_no
        entry["mesco_containernumber"] = container_no
        if not _has(entry.get("mesco_name")):
            entry["mesco_name"] = container_no
    if _has(c.get("seal_number")):
        entry["mesco_carrierseal"] = c["seal_number"]
    if _has(c.get("container_type")):
        entry["mesco_containertype"] = c["container_type"]
        um_hint = _um_hint_from_container_type(c["container_type"])
        if um_hint:
            entry["mesco_um"] = um_hint
    _set_numeric_field(entry, "mesco_noofpackages", c.get("packages"))
    _set_numeric_field(entry, "mesco_grosskg", c.get("gross_weight_kg"))
    _set_numeric_field(entry, "mesco_volcbm", c.get("measurement_cbm"))
    for key in ("mesco_sendtowarehouse", "mesco_reefer", "mesco_imo", "mesco_nor"):
        if not _has(entry.get(key)):
            entry[key] = False
    return {k: v for k, v in entry.items() if _has(v)}


def _record_container_items(rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    containers = [
        dict(c)
        for c in (rec.get("containers") or [])
        if isinstance(c, dict) and _has(c.get("container_number"))
    ]
    if not containers and _has(rec.get("container_number")):
        containers.append({
            "container_number": rec.get("container_number"),
            "seal_number": rec.get("seal_number"),
            "container_type": rec.get("mesco_containertype"),
            "packages": rec.get("cr401_totalpackages"),
            "gross_weight_kg": rec.get("cr401_totalgrossweight"),
            "measurement_cbm": rec.get("cr401_totalvolume"),
        })
    return containers


def _aggregate_totals(
    records: List[Dict[str, Any]],
    *,
    multi_house: bool = False,
) -> Dict[str, Any]:
    fields = {
        "cr401_totalpackages": "packages",
        "cr401_totalgrossweight": "gross_weight_kg",
        "cr401_totalvolume": "measurement_cbm",
    }
    if not multi_house and len(records) > 1:
        multi_house = True
    totals: Dict[str, float] = {}
    unique_containers: set[str] = set()
    for rec in records:
        for c in _record_container_items(rec):
            no = _normalize_container_number(c.get("container_number"))
            if no:
                unique_containers.add(no)
        for out_key, rec_key in fields.items():
            value = rec.get(out_key)
            # Multi-house manifests repeat the same container on every row — never
            # fall back to container line quantities when summing house totals.
            if not _has(value) and not multi_house:
                for c in _record_container_items(rec):
                    value = c.get(rec_key)
                    if _has(value):
                        break
            parsed = _parse_numeric(value)
            if parsed is not None:
                totals[out_key] = totals.get(out_key, 0.0) + parsed
    result: Dict[str, Any] = {}
    for key, value in totals.items():
        result[key] = int(value) if value.is_integer() else round(value, 3)
    if unique_containers:
        result["cr401_totalteus"] = len(unique_containers)
    return result


def records_to_master_json(
    records: List[Dict[str, Any]],
    extra_context: Optional[Dict[str, Any]] = None,
    master_record: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not records and not master_record:
        return {}

    first = master_record or records[0]
    extra = extra_context or {}

    # Override master-level fields from manifest context meta fields
    # (spreadsheet manifest layout stores these separate from per-row HBL values)
    manifest_overrides = {}
    for meta_keys, crm_key in [
        (("_mbl_shipper", "mbl_shipper"), "mesco_shippernamecontactno"),
        # Do not map delivery_agent → consignee (PDF house B/Ls use delivery_agent for MESCO notify only).
        (("_mbl_consignee",), "mesco_consigneenamecontactno"),
        (("_mbl_acid", "mbl_acid"), "mesco_acidnumber"),
        (("_mbl_bookingno", "job_no"), "mesco_bookingnumber"),
        (("_mbl_masterblno",), "mesco_masterblno"),
    ]:
        val = None
        for mk in meta_keys:
            val = first.get(mk)
            if _has(val):
                break
        if _has(val):
            manifest_overrides[crm_key] = val

    master = _project_to_template(_MASTER_TEMPLATE, first, skip=_MASTER_SKIP)
    if manifest_overrides:
        master.update(manifest_overrides)
    if extra:
        master.update(extra)
    _preserve_dg_response_fields(master, first)

    output = master or {}
    output["@odata.context"] = output.get("@odata.context") or _DEFAULT_MASTER_CONTEXT
    if master_record and len(records) > 1:
        totals_source = records
        aggregated = _aggregate_totals(totals_source, multi_house=True)
        teu_source = [master_record] if master_record else records[:1]
        teu_agg = _aggregate_totals(teu_source, multi_house=False)
        if teu_agg.get("cr401_totalteus") is not None:
            aggregated["cr401_totalteus"] = teu_agg["cr401_totalteus"]
    else:
        totals_source = [master_record] if master_record else records
        aggregated = _aggregate_totals(totals_source)
    if aggregated:
        output.update(aggregated)
    elif master_record:
        for key in ("cr401_totalpackages", "cr401_totalgrossweight", "cr401_totalvolume", "cr401_totalteus"):
            if _has(master_record.get(key)) and not _has(output.get(key)):
                _set_numeric_field(output, key, master_record[key])
    master_mbl = output.get("mesco_masterblno")
    physical_records = [master_record] if master_record else list(records)
    if not output.get("mesco_nooforgbls") and master_record:
        output["mesco_nooforgbls"] = master_record.get("mesco_nooforgbls")
    _fill_operation_defaults(output, is_master=True)
    lookup_src = {**first, **manifest_overrides}
    _apply_party_fields(output, lookup_src)
    if not any(_has(output.get(k)) for k in (
        "cr401_totalpackages", "cr401_totalgrossweight", "cr401_totalvolume"
    )):
        output.update(_infer_totals_from_cargo_description(output.get("mesco_cargodescription")))
    # Emit consignee/shipper/country lookup names so the uploader can resolve
    # them to account / xollsp_country GUIDs and bind the lookup columns.
    _apply_operation_lookups(output, lookup_src)

    # Houses — only when a distinct house B/L is present (never duplicate the MBL).
    houses = []
    for rec in records:
        house = _build_house(rec, master_mbl=master_mbl)
        if house:
            houses.append(house)
    if houses and not output.get("mesco_nooforgbls"):
        output["mesco_nooforgbls"] = str(len(houses))
    output[MASTER_HOUSES_KEY] = houses

    # Containers (deduplicate by number)
    seen_ctnr = set()
    crm_containers = []
    for rec in physical_records:
        for c in _record_container_items(rec):
            no = c.get("container_number") or f"__row_{len(seen_ctnr)}"
            if no not in seen_ctnr:
                seen_ctnr.add(no)
                entry = _build_container_from_item(c, master_level=True)
                if entry:
                    crm_containers.append(entry)

    output[MASTER_CONTAINERS_KEY] = crm_containers

    # Cargo items — per-house rows for Arkas-style attached lists; else master-level cargo.
    cargo = []
    if houses and _records_have_per_house_cargo(records):
        for rec in records:
            cargo.extend(_cargo_rows_from_record(rec))
    else:
        for rec in physical_records:
            cargo.extend(_cargo_rows_from_record(rec))
    if not cargo and crm_containers:
        for c in crm_containers:
            cargo.append(_pick(c, "mesco_noofpackages", "mesco_grosskg", "mesco_volcbm"))
    output[MASTER_CARGO_KEY] = cargo

    from custom_business_rules import apply_crm_payload_rules, custom_rules_enabled

    if custom_rules_enabled():
        apply_crm_payload_rules(
            output,
            house_records=records if len(records) > 1 else None,
        )

    return _prune_output(output)


def records_to_house_json(
    records: List[Dict[str, Any]],
    master_context: Optional[Dict[str, Any]] = None,
    master_record: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    output: Dict[str, Any] = {
        "@odata.context": _DEFAULT_HOUSE_CONTEXT,
        "@Microsoft.Dynamics.CRM.totalrecordcount": -1,
        "@Microsoft.Dynamics.CRM.totalrecordcountlimitexceeded": False,
    }
    ctx = master_context or {}
    master_src = master_record or (records[0] if records else {})

    from custom_business_rules import apply_crm_operation_rules, custom_rules_enabled

    value_list = []
    for rec in records:
        # Separate master-level fields to nest inside mesco_Operation
        master_fields = _project_to_template(
            _first_master_in_house_template(),
            master_src,
            skip=_MASTER_SKIP | {"mesco_houseblno"},
        )
        # Override with manifest-context master values (not house values)
        for meta_key, crm_key in [
            ("_mbl_shipper", "mesco_shippernamecontactno"),
            ("_mbl_consignee", "mesco_consigneenamecontactno"),
            ("_mbl_acid", "mesco_acidnumber"),
            ("_mbl_bookingno", "mesco_bookingnumber"),
            ("_mbl_masterblno", "mesco_masterblno"),
        ]:
            if _has(master_src.get(meta_key)):
                master_fields[crm_key] = master_src[meta_key]
        for k in ctx:
            if k not in master_fields:
                master_fields[k] = ctx[k]
        master_fields["mesco_bltype"] = MASTER_BL_TYPE
        master_fields.update(_aggregate_totals([master_src] if master_record else records))
        if not master_fields.get("mesco_nooforgbls") and master_src.get("mesco_nooforgbls"):
            master_fields["mesco_nooforgbls"] = master_src.get("mesco_nooforgbls")
        _fill_operation_defaults(master_fields, is_master=True)
        _apply_operation_lookups(master_fields, master_src)

        house = _build_house(rec, master_mbl=master_fields.get("mesco_masterblno"))
        if not house:
            continue
        _preserve_dg_response_fields(house, rec)
        house[HOUSE_MASTER_KEY] = master_fields

        # Containers per house — shared master equipment for attached-list houses
        container_source = master_src if master_record else rec
        crm_containers = []
        for c in _record_container_items(container_source):
            entry = _build_container_from_item(c, master_level=False)
            if entry:
                crm_containers.append(entry)
        house[HOUSE_CONTAINERS_KEY] = crm_containers

        # Cargo on master only for consolidated multi-house B/Ls (except manifest / per-house cargo).
        manifest_row = rec.get("_manifest_pdf_row") or rec.get("extraction_method") == "pdf_export_lcl_manifest"
        per_house_cargo = rec.get("_per_house_cargo") or (
            isinstance(rec.get("cargo_lines"), list) and rec["cargo_lines"]
        )
        if (
            master_record
            and len(records) > 1
            and not manifest_row
            and not per_house_cargo
        ):
            house[HOUSE_CARGO_KEY] = []
        else:
            house[HOUSE_CARGO_KEY] = _cargo_rows_from_record(
                rec if (manifest_row or per_house_cargo) else container_source
            )

        if custom_rules_enabled():
            apply_crm_operation_rules(house, is_house=True)
        value_list.append(house)

    output["value"] = value_list
    return _prune_output(output)


def normalize_bl_type(bl_type: Optional[str]) -> str:
    """Return ``master`` or ``house`` for API query parameters."""
    normalized = (bl_type or "master").strip().lower()
    if normalized in ("house", "hbl", "h"):
        return "house"
    return "master"


def apply_bl_type_to_crm_payload(payload: Dict[str, Any], bl_type: Optional[str]) -> None:
    """Stamp Dynamics ``mesco_bltype`` on an operation payload before upload."""
    if not isinstance(payload, dict):
        return
    is_house = normalize_bl_type(bl_type) == "house"
    payload["mesco_bltype"] = HOUSE_BL_TYPE if is_house else MASTER_BL_TYPE
    if is_house:
        prepare_standalone_house_upload(payload)


def is_house_bl_type(bl_type: Any) -> bool:
    """True when the operation should be treated as a House B/L."""
    if bl_type in (HOUSE_BL_TYPE, "886150002"):
        return True
    return normalize_bl_type(str(bl_type) if bl_type is not None else "master") == "house"


def prepare_standalone_house_upload(payload: Dict[str, Any]) -> None:
    """
    House-only uploads (no nested master in the payload) must expose cargo on
    the house cargo collection and carry an HBL number for Dynamics.
    """
    if not isinstance(payload, dict):
        return

    if not _has(payload.get("mesco_houseblno")):
        nested_houses = payload.get(MASTER_HOUSES_KEY) or []
        if nested_houses and isinstance(nested_houses[0], dict):
            nested_hbl = nested_houses[0].get("mesco_masterblno")
            if _has(nested_hbl):
                payload["mesco_houseblno"] = str(nested_hbl).strip()
        if not _has(payload.get("mesco_houseblno")):
            for key in ("mesco_houseblno", "mesco_masterblno"):
                if _has(payload.get(key)):
                    payload["mesco_houseblno"] = str(payload[key]).strip()
                    break

    master_cargo = payload.get(MASTER_CARGO_KEY) or []
    house_cargo = payload.get(HOUSE_CARGO_KEY) or []
    if master_cargo and not house_cargo:
        payload[HOUSE_CARGO_KEY] = list(master_cargo)
    elif house_cargo and not master_cargo:
        payload[MASTER_CARGO_KEY] = list(house_cargo)

    # Standalone house upload: cargo belongs on the house grid only (avoid duplicate rows).
    if payload.get("mesco_bltype") == HOUSE_BL_TYPE and house_cargo:
        payload.pop(MASTER_CARGO_KEY, None)

    if not payload.get(MASTER_CARGO_KEY) and not payload.get(HOUSE_CARGO_KEY):
        row = _build_cargo_from_record(payload)
        if row:
            payload[MASTER_CARGO_KEY] = [row]
            payload[HOUSE_CARGO_KEY] = [row]
