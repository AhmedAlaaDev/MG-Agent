"""
Upload extracted CRM JSON to Microsoft Dynamics 365 Dataverse.

Handles the full hierarchy:
  1. Master operation (mesco_operations)
  2. House operations (mesco_operations, linked to master)
  3. Containers (mesco_containers, linked to master)
   4. Cargo items (mesco_cargos, linked to house + container)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from dataverse.client_service import DataverseClientService, RetryConfig

logger = logging.getLogger(__name__)

_ENTITY = "mesco_operations"
_CONTAINER_ENTITY = "mesco_containers"
_CARGO_ENTITY = "mesco_cargos"

# Fields to strip when posting (metadata-only, not for creating)
_ODATA_META_SUFFIXES = (
    "@odata.etag", "@odata.context",
    "@OData.Community.Display.V1.FormattedValue",
    "@Microsoft.Dynamics.CRM.associatednavigationproperty",
    "@Microsoft.Dynamics.CRM.lookuplogicalname",
)


def _clean_odata_meta(obj: Any) -> Any:
    """Remove OData metadata annotations (read-only) before POST/PATCH."""
    if isinstance(obj, dict):
        return {
            k: _clean_odata_meta(v)
            for k, v in obj.items()
            if not k.startswith("@") and not k.endswith(_ODATA_META_SUFFIXES)
        }
    if isinstance(obj, list):
        return [_clean_odata_meta(item) for item in obj]
    return obj


def _strip_null(obj: Any) -> Any:
    """Remove keys with None values (Dataverse rejects explicit null for some fields)."""
    if isinstance(obj, dict):
        cleaned = {}
        for k, v in obj.items():
            if v is None:
                continue
            child = _strip_null(v)
            if child in ({}, []):
                continue
            cleaned[k] = child
        return cleaned
    if isinstance(obj, list):
        return [_strip_null(item) for item in obj]
    return obj


# ---------------------------------------------------------------------------
# Dataverse schema helpers — field validation and type conversion
# ---------------------------------------------------------------------------

# Per-entity schema definitions for CRM JSON → Dataverse translation.

_EntitySchema = Dict[str, Any]

_ENTITY_SCHEMAS: Dict[str, _EntitySchema] = {
    # --  mesco_operations  --
    "mesco_operations": {
        # Lookup fields — can't be set as plain strings
        "lookups": {
            "mesco_vessel", "mesco_origin", "mesco_destination",
            "mesco_notify1", "mesco_notify2",
            "mesco_shipper", "mesco_consignee", "mesco_consignee2",
            "mesco_agent", "mesco_airline",
            "mesco_bookingpartner", "mesco_branch", "mesco_commodity",
            "mesco_country", "mesco_countryoforigin", "mesco_currency",
            "mesco_incoterm", "mesco_shippingline",
            "mesco_freightforwarder", "mesco_operationperson",
            "mesco_salesman", "mesco_supplier",
            "mesco_transhipmentport", "mesco_operation",
            "mesco_xollsp_tariffquote", "mesco_servicetype",
        },
        # Fields that don't exist in Dataverse (old CRM, internal markers)
        "invalid": {
            "mesco_dateofissue", "mesco_placeofissue",
            "mesco_shippedonboarddate",
            "source_extraction_method", "mesco_houseblno", "mesco_hscode",
        },
        # Decimal fields — Dataverse expects numbers, not strings
        "decimals": {
            "cr401_totalgrossweight", "cr401_totalvolume",
            "cr401_totalpackages", "cr401_totalteus",
            "mesco_freetimedestination", "mesco_freetimeorigin",
            "mesco_invoicevalue", "mesco_quantity", "mesco_quantity2",
            "mesco_quantity3", "mesco_releasevalue",
            "mesco_totalcosts", "mesco_totalcostsmaster",
            "mesco_totalmargin", "mesco_totalmarginmaster",
            "mesco_totalotherchargesduecarrier",
            "mesco_totalsales", "mesco_totalsalesmaster",
        },
        # Picklist string → int mappings
        "picklist_strings": {
            "mesco_pcfreightterm": {
                "COLLECT": 100000001,
                "PREPAID": 100000000,
            },
        },
    },
    # --  mesco_containers  --
    "mesco_containers": {
        "lookups": {
            "mesco_containerno",  # Lookup — use mesco_containernumber instead
            "mesco_masteroperation", "mesco_um", "mesco_umpackages",
            "mesco_warehouse",
        },
        "invalid": {
            "mesco_containerno",  # maps to mesco_containernumber
        },
        "field_map": {
            "mesco_containerno": "mesco_containernumber",
            "mesco_quantity": "mesco_quantity",
            "mesco_noofpackages": "mesco_noofpackages",
            "mesco_grosskg": "mesco_grosskg",
            "mesco_volcbm": "mesco_volcbm",
        },
        "decimals": {
            "mesco_noofpackages", "mesco_grosskg", "mesco_volcbm",
            "mesco_netwtkg", "mesco_tarewtkg", "mesco_flashptc",
            "mesco_maxtempc", "mesco_mintempc", "mesco_ventilation",
            "mesco_humidity", "mesco_unno", "mesco_quantity",
        },
        "picklist_strings": {},
    },
    # --  mesco_cargos  --
    "mesco_cargos": {
        "lookups": {
            "mesco_conainter", "mesco_houseoperation",
            "mesco_masteroperation", "mesco_umpackages",
        },
        "invalid": {
            "mesco_description", "mesco_grossweight", "mesco_volume",
            "mesco_containerno",
        },
        "field_map": {
            "mesco_description": "mesco_descriptionofgoods",
            "mesco_grossweight": "mesco_grosskg",
            "mesco_volume": "mesco_volcbm",
            "mesco_noofpackages": "mesco_noofpackages",
        },
        "decimals": {
            "mesco_noofpackages", "mesco_grosskg", "mesco_volcbm",
            "mesco_netwtkg", "mesco_tarewtkg", "mesco_flashptc",
            "mesco_maxtempc", "mesco_mintempc", "mesco_ventilation",
            "mesco_humidity", "mesco_unno", "mesco_chargeableweight",
            "mesco_height", "mesco_lenght", "mesco_ratecharge",
            "mesco_volumetricweight", "mesco_width",
        },
        "picklist_strings": {},
    },
}


def _preprocess_payload(
    payload: Dict[str, Any],
    entity_set: str = "mesco_operations",
) -> Dict[str, Any]:
    """Clean a CRM JSON payload for Dataverse compatibility.

    * Strips lookups passed as plain values
    * Removes non-existent fields
    * Renames fields via *field_map* (CRM JSON → Dataverse schema)
    * Converts decimal strings to numbers
    * Maps picklist strings to integers
    """
    schema = _ENTITY_SCHEMAS.get(entity_set, {})
    lookups = schema.get("lookups", set())
    invalid = schema.get("invalid", set())
    field_map = schema.get("field_map", {})
    decimals = schema.get("decimals", set())
    picklist_strings = schema.get("picklist_strings", {})

    cleaned: Dict[str, Any] = {}
    for key, value in payload.items():
        if value is None:
            continue
        # Rename field
        target_key = field_map.get(key, key)
        if target_key in lookups:
            continue  # can't set lookups by plain value
        if target_key in invalid:
            continue  # field doesn't exist in Dataverse
        if target_key in decimals and isinstance(value, str):
            try:
                cleaned[target_key] = float(value) if "." in value else int(value)
            except (ValueError, TypeError):
                cleaned[target_key] = value
        elif target_key in picklist_strings and isinstance(value, str):
            mapped = picklist_strings[target_key].get(value.upper())
            if mapped is not None:
                cleaned[target_key] = mapped
            else:
                cleaned[target_key] = value
        else:
            cleaned[target_key] = value
    return cleaned


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

UploadResult = Dict[str, Any]


def upload_crm_json(
    crm_data: Dict[str, Any],
    retry_config: Optional[RetryConfig] = None,
) -> UploadResult:
    """
    Upload the full CRM JSON structure (master + houses + containers + cargo)
    to Dynamics 365 Dataverse.

    Returns a dict with the created entity IDs:
    {
        "master_id": "<guid>",
        "houses": [{"index": 0, "id": "<guid>", "hbl": "..."}, ...],
        "containers": [{"index": 0, "id": "<guid>", "container_no": "..."}, ...],
        "cargo": [{"index": 0, "id": "<guid>"}, ...],
    }
    """
    crm_data = _normalize_upload_payload(crm_data)
    client = DataverseClientService.get_instance(retry_config or RetryConfig())

    payload = _clean_odata_meta(crm_data)
    payload = _strip_null(payload)

    result: UploadResult = {
        "master_id": None,
        "houses": [],
        "containers": [],
        "cargo": [],
    }

    # ------------------------------------------------------------------
    # 1. Create master operation
    # ------------------------------------------------------------------
    master_fields = dict(payload)
    master_fields.pop("mesco_Operation_mesco_Operation_mesco_Operation", None)
    master_fields.pop("mesco_Container_MasterOperation_mesco_Operation", None)
    master_fields.pop("mesco_Cargo_MasterOperation_mesco_Operation", None)

    master_id = _create_entity(client, _ENTITY, _preprocess_payload(master_fields, _ENTITY))
    result["master_id"] = master_id
    logger.info("Created master operation: %s", master_id)

    # ------------------------------------------------------------------
    # 2. Create house operations (linked to master)
    # ------------------------------------------------------------------
    houses = payload.get("mesco_Operation_mesco_Operation_mesco_Operation", [])
    for idx, house in enumerate(houses):
        house_link = {_lookup_key("mesco_Operation"): f"{_ENTITY}({master_id})"}
        house_id = _create_entity(client, _ENTITY, _preprocess_payload({**house, **house_link}, _ENTITY))
        result["houses"].append({
            "index": idx,
            "id": house_id,
            "hbl": house.get("mesco_masterblno"),
        })
        logger.info("  House [%d] %s → %s", idx, house.get("mesco_masterblno"), house_id)

    # ------------------------------------------------------------------
    # 3. Create containers (linked to master)
    # ------------------------------------------------------------------
    containers = payload.get("mesco_Container_MasterOperation_mesco_Operation", [])
    for idx, container in enumerate(containers):
        ctn_link = {_lookup_key("mesco_MasterOperation"): f"{_ENTITY}({master_id})"}
        ctn_id = _create_entity(client, _CONTAINER_ENTITY, _preprocess_payload({**container, **ctn_link}, _CONTAINER_ENTITY))
        result["containers"].append({
            "index": idx,
            "id": ctn_id,
            "container_no": container.get("mesco_containernumber") or container.get("mesco_containerno"),
        })
        logger.info("  Container [%d] %s → %s", idx, container.get("mesco_containerno"), ctn_id)

    # ------------------------------------------------------------------
    # 4. Create cargo items (linked to house + container if applicable)
    # ------------------------------------------------------------------
    cargo_list = payload.get("mesco_Cargo_MasterOperation_mesco_Operation", [])
    for idx, cargo in enumerate(cargo_list):
        cargo_links = {}
        house_info = result["houses"][idx] if idx < len(result["houses"]) else None
        if house_info:
            cargo_links[_lookup_key("mesco_HouseOperation")] = f"{_ENTITY}({house_info['id']})"
            cargo_links[_lookup_key("mesco_MasterOperation")] = f"{_ENTITY}({master_id})"
        ctn_info = result["containers"][idx] if idx < len(result["containers"]) else None
        if ctn_info:
            cargo_links[_lookup_key("mesco_Conainter")] = f"{_CONTAINER_ENTITY}({ctn_info['id']})"

        cargo_id = _create_entity(client, _CARGO_ENTITY, _preprocess_payload({**cargo, **cargo_links}, _CARGO_ENTITY))
        result["cargo"].append({"index": idx, "id": cargo_id})
        logger.info("  Cargo [%d] → %s", idx, cargo_id)

    return result


def _normalize_upload_payload(crm_data: Dict[str, Any]) -> Dict[str, Any]:
    """Accept API responses, master-shaped JSON, or house-shaped JSON for upload."""
    if not isinstance(crm_data, dict):
        return crm_data

    if isinstance(crm_data.get("data"), dict):
        return _normalize_upload_payload(crm_data["data"])

    if (
        isinstance(crm_data.get("house_data"), dict)
        and not isinstance(crm_data.get("mesco_Operation_mesco_Operation_mesco_Operation"), list)
    ):
        return _normalize_upload_payload(crm_data["house_data"])

    if isinstance(crm_data.get("value"), list):
        from crm_mapper import map_crm_operation_to_records
        from crm_output_formatter import records_to_master_json

        records = map_crm_operation_to_records(crm_data)
        return records_to_master_json(records)

    return crm_data


def _lookup_key(relationship_name: str) -> str:
    """Return the odata.bind key for a Dataverse lookup field."""
    return f"{relationship_name}@odata.bind"


def _create_entity(client: DataverseClientService, entity_set: str, fields: Dict[str, Any]) -> str:
    """
    POST a new entity record and return its GUID (mesco_operationid, mesco_containerid, etc.).
    """
    id_field = _id_field(entity_set)
    resp = client.post(entity_set, json=fields)
    # On success, Dataverse returns 204 No Content with Location header.
    location = resp.headers.get("Location") or resp.headers.get("location", "")
    if location:
        guid = location.strip("/").rsplit("(", 1)[-1].rstrip(")")
        return guid
    # Fallback: try to parse from response body
    body = resp.json() if resp.content else {}
    guid = body.get(id_field)
    if guid:
        return guid
    raise RuntimeError(
        f"Could not extract GUID from {entity_set} POST response. "
        f"Status: {resp.status_code}, Location: {location}"
    )


def _id_field(entity_set: str) -> str:
    mapping = {
        "mesco_operations": "mesco_operationid",
        "mesco_containers": "mesco_containerid",
        "mesco_cargos": "mesco_cargoid",
    }
    return mapping.get(entity_set, f"{entity_set.rstrip('s')}id")
