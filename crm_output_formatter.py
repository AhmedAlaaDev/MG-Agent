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
from typing import Any, Dict, Iterable, List, Optional

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
}

_MASTER_SKIP = {"mesco_houseblno", "mesco_incoterm"}
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


def _project_to_template(template: Dict[str, Any], src: Dict[str, Any], skip: Optional[set[str]] = None) -> Dict[str, Any]:
    target = _blank_template(template) if template else {}
    return _apply_fields(target, src, skip=skip)


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
    if _has(line.get("mesco_noofpackages")):
        cargo["mesco_noofpackages"] = line["mesco_noofpackages"]
    if _has(line.get("mesco_grosskg")):
        cargo["mesco_grosskg"] = line["mesco_grosskg"]
    return {k: v for k, v in cargo.items() if _has(v)}


def _cargo_rows_from_record(rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    lines = rec.get("cargo_lines")
    if isinstance(lines, list) and lines:
        rows = [_build_cargo_from_line(ln) for ln in lines if isinstance(ln, dict)]
        return [row for row in rows if row]
    cargo = _build_cargo_from_record(rec)
    return [cargo] if cargo else []


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


def _build_cargo_from_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    cargo = _project_to_template(_first_cargo_template(master_level=True), {})
    desc_parts = []
    hs_value = rec.get("mesco_hscode")
    cargo_desc = rec.get("mesco_cargodescription")

    # Avoid duplicating HS codes that already appear inline in the cargo text.
    if _has(hs_value):
        if cargo_desc and re.search(r"\bHS\s*CODE\b", str(cargo_desc), re.I):
            pass
        else:
            desc_parts.append(f"HS: {hs_value}")
    if _has(cargo_desc):
        desc_parts.append(str(cargo_desc))
    if _has(rec.get("cargo_type")):
        ct = str(rec["cargo_type"]).strip()
        if ct not in " ".join(desc_parts):
            desc_parts.append(ct)
    if desc_parts:
        cargo["mesco_descriptionofgoods"] = _truncate_to_limit(
            "\n".join(desc_parts),
            _DATAVERSE_DESC_OF_GOODS_MAX,
        )
    if _has(rec.get("cr401_totalpackages")):
        cargo["mesco_noofpackages"] = rec["cr401_totalpackages"]
    if _has(rec.get("cr401_totalgrossweight")):
        cargo["mesco_grosskg"] = rec["cr401_totalgrossweight"]
    if _has(rec.get("cr401_totalvolume")):
        cargo["mesco_volcbm"] = rec["cr401_totalvolume"]
    return {k: v for k, v in cargo.items() if _has(v)}


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
    house["mesco_bltype"] = HOUSE_BL_TYPE
    house["mesco_masterblno"] = hbl
    if master_mbl:
        house["mesco_masterbllinkno"] = master_mbl
    _fill_operation_defaults(house, is_master=False)
    return house


def _build_container_from_item(c: Dict[str, Any], master_level: bool = True) -> Dict[str, Any]:
    entry = _project_to_template(_first_container_template(master_level=master_level), {})
    if _has(c.get("container_number")):
        entry["mesco_containernumber"] = c["container_number"]
        if not _has(entry.get("mesco_name")):
            entry["mesco_name"] = c["container_number"]
    if _has(c.get("seal_number")):
        entry["mesco_carrierseal"] = c["seal_number"]
    if _has(c.get("container_type")):
        entry["mesco_containertype"] = c["container_type"]
    if _has(c.get("packages")):
        entry["mesco_noofpackages"] = c["packages"]
    if _has(c.get("gross_weight_kg")):
        entry["mesco_grosskg"] = c["gross_weight_kg"]
    if _has(c.get("measurement_cbm")):
        entry["mesco_volcbm"] = c["measurement_cbm"]
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


def _aggregate_totals(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    fields = {
        "cr401_totalpackages": "packages",
        "cr401_totalgrossweight": "gross_weight_kg",
        "cr401_totalvolume": "measurement_cbm",
    }
    totals: Dict[str, float] = {}
    for rec in records:
        for out_key, rec_key in fields.items():
            value = rec.get(out_key)
            if not _has(value):
                for c in _record_container_items(rec):
                    value = c.get(rec_key)
                    if _has(value):
                        break
            try:
                if _has(value):
                    num_text = str(value).replace(",", "")
                    m = re.search(r"(\d+(?:\.\d+)?)", num_text)
                    if m:
                        totals[out_key] = totals.get(out_key, 0.0) + float(m.group(1))
            except (TypeError, ValueError):
                pass
    result: Dict[str, Any] = {}
    for key, value in totals.items():
        result[key] = int(value) if value.is_integer() else round(value, 3)
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

    output = master or {}
    output["@odata.context"] = output.get("@odata.context") or _DEFAULT_MASTER_CONTEXT
    totals_source = [master_record] if master_record else records
    output.update(_aggregate_totals(totals_source))
    master_mbl = output.get("mesco_masterblno")
    physical_records = [master_record] if master_record else list(records)
    if len(physical_records) == 1:
        pkg = physical_records[0].get("cr401_totalpackages")
        if pkg and re.search(r"PALLETS", str(pkg), re.I):
            output["cr401_totalpackages"] = pkg
    if not output.get("mesco_nooforgbls") and master_record:
        output["mesco_nooforgbls"] = master_record.get("mesco_nooforgbls")
    _fill_operation_defaults(output, is_master=True)

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

    # Cargo items (one row per groupage shipper line when parsed from PDF)
    cargo = []
    for rec in physical_records:
        cargo.extend(_cargo_rows_from_record(rec))
    if not cargo and crm_containers:
        for c in crm_containers:
            cargo.append(_pick(c, "mesco_noofpackages", "mesco_grosskg", "mesco_volcbm"))
    output[MASTER_CARGO_KEY] = cargo

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

        house = _build_house(rec, master_mbl=master_fields.get("mesco_masterblno"))
        if not house:
            continue
        house[HOUSE_MASTER_KEY] = master_fields

        # Containers per house — shared master equipment for attached-list houses
        container_source = master_src if master_record else rec
        crm_containers = []
        for c in _record_container_items(container_source):
            entry = _build_container_from_item(c, master_level=False)
            if entry:
                crm_containers.append(entry)
        house[HOUSE_CONTAINERS_KEY] = crm_containers

        # Cargo on master only for consolidated multi-house B/Ls (except manifest PDF rows)
        manifest_row = rec.get("_manifest_pdf_row") or rec.get("extraction_method") == "pdf_export_lcl_manifest"
        if master_record and len(records) > 1 and not manifest_row:
            house[HOUSE_CARGO_KEY] = []
        else:
            house[HOUSE_CARGO_KEY] = _cargo_rows_from_record(
                rec if manifest_row else container_source
            )

        value_list.append(house)

    output["value"] = value_list
    return _prune_output(output)
