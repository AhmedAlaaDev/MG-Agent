"""
Upload extracted CRM JSON to Microsoft Dynamics 365 Dataverse.

Handles the full hierarchy:
  1. Master operation  (mesco_operations)
  2. House operations  (mesco_operations, linked to master)
  3. Containers        (mesco_containers, linked to master)
  4. Cargo items       (mesco_cargos, linked to house + container)

Schema source: OperationSchema (mesco_operation) — auto-generated TypeScript schema
converted to Python for field validation and type coercion.

Fix log (vs previous version):
  [FIX-1]  _preprocess_payload lookup branch: when resolution fails the bind
           annotation key must NEVER be written — not even without a value.
           The previous code wrote the @odata.bind key into `already_bound` even
           on failure, which caused the annotation to appear without a value and
           triggered Dataverse error 0x80048d19:
             "An undeclared property 'mesco_notify1' which only has property
              annotations in the payload but no property value was found."
           Root cause: the `continue` at the end of the lookup block fired
           AFTER the bind key was conditionally written to `already_bound` but
           NOT to `cleaned`.  Rewritten so the bind key is only ever added to
           `already_bound` when also successfully written to `cleaned`.

  [FIX-2]  Fields that are Lookup in the schema but whose OCR value is a
           plain human-readable string that can NEVER be resolved (e.g.
           "SAME AS CONSIGNEE" for mesco_notify1, "CMA CGM TIGA" for
           mesco_vessel, "HAMBURG" / "ALEXANDRIA OLD PORT" for mesco_origin /
           mesco_destination) must be dropped silently rather than causing a
           400.  The lookup resolution already drops them on miss — the bug
           was in step 1 (pass-1 pre-built bind detection) which must not
           register a bare annotation without a value.

  [FIX-3]  House fields that are in the invalid set for master operations
           (mesco_hscode, mesco_dateofissue, mesco_placeofissue) were leaking
           through into the house payload because the house records are also
           posted to mesco_operations with the SAME schema.  The invalid set
           already contains these correctly — but the pass-1 pre-built bind
           detection was not filtering them out.  Confirmed working.

  [FIX-4]  Decimal coercion: house/cargo numeric fields arrived as strings
           ("5.131", "3001.000", "4") — coercion was correct in principle but
           integers stored as "4" (no ".") were coerced to int, which Dataverse
           accepts for Decimal fields.  No change needed, confirmed correct.

  [FIX-5]  mesco_pcfreightterm: arrives as the string "COLLECT" in both
           master and house payloads — picklist_strings map handles this.
           No change needed, confirmed correct.

  [FIX-6]  Container field_map: mesco_containerno → mesco_containernumber.
           The real payload already uses mesco_containernumber so no rename
           fires, but the map is kept for safety.

  [FIX-7]  _normalize_upload_payload: the real API response wraps everything
           under {"success": true, "data": {...}, "house_data": {...}}.
           The function now correctly unwraps the "data" key as the master
           payload (which already contains the nested house/container/cargo
           arrays).  The "house_data" key is ignored — it is a duplicate view
           structured differently and should NOT be used as the primary input.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

from dataverse.client_service import DataverseClientService, RetryConfig

logger = logging.getLogger(__name__)

_ENTITY           = "mesco_operations"
_CONTAINER_ENTITY = "mesco_containers"
_CARGO_ENTITY     = "mesco_cargos"

# ---------------------------------------------------------------------------
# OData metadata stripping
# ---------------------------------------------------------------------------

_ODATA_META_SUFFIXES = (
    "@odata.etag",
    "@odata.context",
    "@OData.Community.Display.V1.FormattedValue",
    "@Microsoft.Dynamics.CRM.associatednavigationproperty",
    "@Microsoft.Dynamics.CRM.lookuplogicalname",
    # Strip the top-level OData collection annotations that arrive on
    # house_data / any sub-collections pulled from Dataverse
    "@Microsoft.Dynamics.CRM.totalrecordcount",
    "@Microsoft.Dynamics.CRM.totalrecordcountlimitexceeded",
)


def _clean_odata_meta(obj: Any) -> Any:
    """Remove OData metadata annotations (read-only) before POST/PATCH."""
    if isinstance(obj, dict):
        return {
            k: _clean_odata_meta(v)
            for k, v in obj.items()
            if not any(k == s or k.endswith(s) for s in _ODATA_META_SUFFIXES)
            and not k.startswith("@")
        }
    if isinstance(obj, list):
        return [_clean_odata_meta(item) for item in obj]
    return obj


def _strip_null(obj: Any) -> Any:
    """Remove keys with None values (Dataverse rejects explicit null for some fields)."""
    if isinstance(obj, dict):
        cleaned: Dict[str, Any] = {}
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
# Entity-set name mapping  (logical name → OData set name)
# ---------------------------------------------------------------------------

_ENTITY_SET_MAP: Dict[str, str] = {
    "mesco_operations":          "mesco_operations",
    "mesco_containers":          "mesco_containers",
    "mesco_cargos":              "mesco_cargos",
    "account":                   "accounts",
    "systemuser":                "systemusers",
    "transactioncurrency":       "transactioncurrencies",
    "mesco_vesselsmis":          "mesco_vesselsmises",
    "mesco_agent":               "mesco_agents",
    "mesco_airline":             "mesco_airlines",
    "mesco_branch":              "mesco_branchs",
    "mesco_shippingline":        "mesco_shippinglines",
    "mesco_vendor":              "mesco_vendors",
    "xollsp_address":            "xollsp_addresses",
    "xollsp_country":            "xollsp_countries",
    "xollsp_incoterm":           "xollsp_incoterms",
    "xollsp_commoditygroup":     "xollsp_commoditygroups",
    "xollsp_servicedefinition":  "xollsp_servicedefinitions",
    "xollsp_tariffquote":        "xollsp_tariffquotes",
    "mesco_operation":           "mesco_operations",
}

_ID_FIELD_MAP: Dict[str, str] = {
    "mesco_operations":          "mesco_operationid",
    "mesco_containers":          "mesco_containerid",
    "mesco_cargos":              "mesco_cargoid",
    "account":                   "accountid",
    "systemuser":                "systemuserid",
    "transactioncurrency":       "transactioncurrencyid",
    "mesco_vesselsmis":          "mesco_vesselsmisid",
    "mesco_agent":               "mesco_agentid",
    "mesco_airline":             "mesco_airlineid",
    "mesco_branch":              "mesco_branchid",
    "mesco_shippingline":        "mesco_shippinglineid",
    "mesco_vendor":              "mesco_vendorid",
    "xollsp_address":            "xollsp_addressid",
    "xollsp_country":            "xollsp_countryid",
    "xollsp_incoterm":           "xollsp_incotermid",
    "xollsp_commoditygroup":     "xollsp_commoditygroupid",
    "xollsp_servicedefinition":  "xollsp_servicedefinitionid",
    "xollsp_tariffquote":        "xollsp_tariffquoteid",
}

# Navigation property name map: logical_name → PascalCase schema_name  [FIX-9]
# Dataverse @odata.bind keys MUST use the schema (PascalCase) name, NOT the
# logical (lowercase) name.  Confirmed entries come from actual GET response
# associatednavigationproperty annotations; unconfirmed entries follow the
# pattern mesco_ + PascalCase(rest).
_NAV_PROPERTY_MAP: Dict[str, str] = {
    # Confirmed from Dataverse GET responses
    "mesco_origin":             "mesco_Origin",
    "mesco_destination":        "mesco_Destination",
    "mesco_vessel":             "mesco_Vessel",
    "mesco_consignee":          "mesco_Consignee",
    "mesco_consignee2":         "mesco_Consignee2",
    "mesco_agent":              "mesco_Agent",
    "mesco_shippingline":       "mesco_ShippingLine",
    "mesco_country":            "mesco_Country",
    "mesco_bookingpartner":     "mesco_BookingPartner",
    "mesco_incoterm":           "mesco_Incoterm",
    "mesco_servicetype":        "mesco_ServiceType",
    "mesco_xollsp_tariffquote": "mesco_xollsp_TariffQuote",
    "mesco_operation":          "mesco_Operation",
    "mesco_notify1":            "mesco_Notify1",
    "mesco_notify2":            "mesco_Notify2",
    # Container / cargo nav properties (confirmed)
    "mesco_conainter":          "mesco_Conainter",
    "mesco_houseoperation":     "mesco_HouseOperation",
    "mesco_masteroperation":    "mesco_MasterOperation",
    "mesco_umpackages":         "mesco_UMPackages",
    "mesco_umcontainer":        "mesco_UMContainer",
    "mesco_um":                 "mesco_UM",
    "mesco_containerno":        "mesco_ContainerNo",
    "mesco_warehouse":          "mesco_Warehouse",
    # System nav properties (always lowercase)
    "ownerid":                  "ownerid",
    "owningbusinessunit":       "owningbusinessunit",
    "transactioncurrencyid":    "transactioncurrencyid",
    # Confirmed from OperationSchema in index.ts (auto-generated entity metadata)
    "mesco_shipper":            "mesco_Shipper",
    "mesco_airline":            "mesco_Airline",
    "mesco_branch":             "mesco_Branch",
    "mesco_commodity":          "mesco_Commodity",
    "mesco_countryoforigin":    "mesco_Countryoforigin",
    "mesco_currency":           "mesco_Currency",
    "mesco_freightforwarder":   "mesco_FreightForwarder",
    "mesco_operationperson":    "mesco_OperationPerson",
    "mesco_salesman":           "mesco_SalesMan",
    "mesco_supplier":           "mesco_Supplier",
    "mesco_transhipmentport":   "mesco_TranshipmentPort",
}


def _entity_set_name(logical_name: str) -> str:
    return _ENTITY_SET_MAP.get(logical_name, f"{logical_name}s")


def _id_field(entity_set: str) -> str:
    return _ID_FIELD_MAP.get(entity_set, f"{entity_set.rstrip('s')}id")


# ---------------------------------------------------------------------------
# Schema definitions
# ---------------------------------------------------------------------------

_EntitySchema = Dict[str, Any]

_ENTITY_SCHEMAS: Dict[str, _EntitySchema] = {

    # -----------------------------------------------------------------------
    # mesco_operations  (used for BOTH master and house records)
    # -----------------------------------------------------------------------
    "mesco_operations": {

        # Lookup fields: logicalName → target entity logical name.
        # Source: OperationSchema.ManyToOneRelationships (TypeScript).
        # ALL entries here cause the preprocessor to attempt name→GUID
        # resolution.  If resolution fails the field is silently DROPPED —
        # never emitted as a bare string and never emitted as an orphan
        # annotation key.  See [FIX-1] and [FIX-2].
        "lookups": {
            # Routing
            "mesco_origin":           "xollsp_address",
            "mesco_destination":      "xollsp_address",
            "mesco_transhipmentport": "xollsp_address",
            # Countries
            "mesco_country":          "xollsp_country",
            "mesco_countryoforigin":  "xollsp_country",
            # Parties
            "mesco_shipper":          "account",
            "mesco_consignee":        "account",
            "mesco_consignee2":       "account",
            "mesco_notify1":          "account",
            "mesco_notify2":          "account",
            "mesco_bookingpartner":   "account",
            "mesco_freightforwarder": "account",
            # Vessel / carrier
            "mesco_vessel":           "mesco_vesselsmis",
            "mesco_shippingline":     "mesco_shippingline",
            "mesco_airline":          "mesco_airline",
            # People
            "mesco_operationperson":  "systemuser",
            "mesco_salesman":         "systemuser",
            "ownerid":                "systemuser",
            # Org / commercial
            "mesco_agent":            "mesco_agent",
            "mesco_branch":           "mesco_branch",
            "mesco_supplier":         "mesco_vendor",
            "mesco_commodity":        "xollsp_commoditygroup",
            "mesco_incoterm":         "xollsp_incoterm",
            "mesco_servicetype":      "xollsp_servicedefinition",
            # Currency (two separate fields)
            "transactioncurrencyid":  "transactioncurrency",
            "mesco_currency":         "transactioncurrency",
            # Tariff
            "mesco_xollsp_tariffquote": "xollsp_tariffquote",
            # Parent operation (self-referential — injected by caller for houses)
            "mesco_operation":        "mesco_operation",
        },

        # Fields that must be dropped — either not on the entity, read-only,
        # or nested-relation keys that are handled separately.
        "invalid": {
            # System read-only
            "createdby",
            "createdon",
            "exchangerate",
            "modifiedby",
            "modifiedon",
            "owningbusinessunit",
            "owningteam",
            # Rollup / formula — read-only
            "mesco_totalcosts",
            "mesco_totalcostsmaster",
            "mesco_totalmargin",
            "mesco_totalmarginmaster",
            "mesco_totalsales",
            "mesco_totalsalesmaster",
            "mesco_totalsalesmaster_date",
            "mesco_totalsalesmaster_state",
            # House-BL fields — not on the master operation entity.
            # These fields ARE valid on house operation records; they are still
            # in this invalid set because the same schema is used for both
            # master and house POST calls, and the schema has no separate entry
            # for house-specific fields.  They arrive in the house sub-object
            # from OCR but Dataverse rejects them on the house record too
            # (they live on a related entity, not directly on mesco_operation).
            "mesco_hscode",
            "mesco_dateofissue",
            "mesco_placeofissue",
            "mesco_houseblno",
            "mesco_shippedonboarddate",
            # Internal pipeline markers
            "source_extraction_method",
            # Nested relation keys — handled as separate POST calls, not fields
            "mesco_Operation_mesco_Operation_mesco_Operation",
            "mesco_Container_MasterOperation_mesco_Operation",
            "mesco_Cargo_MasterOperation_mesco_Operation",
            "mesco_Operation",
            "mesco_Container_mesco_houses",
            "mesco_Cargo_HouseOperation_mesco_Operation",
        },

        "decimals": {
            "cr401_totalgrossweight",
            "cr401_totalvolume",
            "cr401_totalpackages",
            "cr401_totalteus",
            "mesco_freetimedestination",
            "mesco_freetimeorigin",
            "mesco_invoicevalue",
            "mesco_quantity",
            "mesco_quantity2",
            "mesco_quantity3",
            "mesco_releasevalue",
            "mesco_totalotherchargesduecarrier",
        },

        "picklist_strings": {
            "mesco_pcfreightterm": {
                "PREPAID": 100000000,
                "COLLECT": 100000001,
            },
            "mesco_transporttype": {
                "SEA":  300000000,
                "AIR":  300000001,
                "ROAD": 300000002,
                "RAIL": 300000003,
            },
            "mesco_direction": {
                "EXPORT": 300000000,
                "IMPORT": 300000001,
                "CROSS":  300000002,
            },
            "mesco_loadtype": {
                "FCL":  300000000,
                "LCL":  300000001,
                "BULK": 300000002,
            },
            "mesco_bltype": {
                "MASTER": 886150001,
                "HOUSE":  886150002,
                "DIRECT": 886150003,
            },
        },

        "field_map": {},
    },

    # -----------------------------------------------------------------------
    # mesco_containers
    # -----------------------------------------------------------------------
    "mesco_containers": {
        "lookups": {
            "mesco_masteroperation": "mesco_operation",
            "mesco_um":              "mesco_um",
            "mesco_umpackages":      "mesco_um",
            "mesco_warehouse":       "mesco_warehouse",
        },
        "invalid": {
            "mesco_containerno",
        },
        "field_map": {
            "mesco_containerno": "mesco_containernumber",
        },
        "decimals": {
            "mesco_noofpackages",
            "mesco_grosskg",
            "mesco_volcbm",
            "mesco_netwtkg",
            "mesco_tarewtkg",
            "mesco_flashptc",
            "mesco_maxtempc",
            "mesco_mintempc",
            "mesco_ventilation",
            "mesco_humidity",
            "mesco_unno",
            "mesco_quantity",
        },
        "picklist_strings": {},
    },

    # -----------------------------------------------------------------------
    # mesco_cargos
    # -----------------------------------------------------------------------
    "mesco_cargos": {
        "lookups": {
            "mesco_conainter":       "mesco_container",
            "mesco_houseoperation":  "mesco_operation",
            "mesco_masteroperation": "mesco_operation",
            "mesco_umpackages":      "mesco_um",
        },
        "invalid": set(),
        "field_map": {},
        "decimals": {
            "mesco_noofpackages",
            "mesco_grosskg",
            "mesco_volcbm",
            "mesco_netwtkg",
            "mesco_tarewtkg",
            "mesco_flashptc",
            "mesco_maxtempc",
            "mesco_mintempc",
            "mesco_ventilation",
            "mesco_humidity",
            "mesco_unno",
            "mesco_chargeableweight",
            "mesco_height",
            "mesco_lenght",
            "mesco_ratecharge",
            "mesco_volumetricweight",
            "mesco_width",
        },
        "picklist_strings": {},
    },
}


# ---------------------------------------------------------------------------
# Lookup resolution
# ---------------------------------------------------------------------------

_NAME_FIELDS_BY_ENTITY: Dict[str, List[str]] = {
    "xollsp_address":        ["xollsp_name", "xollsp_addressname", "mesco_name", "name"],
    "mesco_vesselsmis":      ["mesco_name", "name"],
    "mesco_agent":           ["mesco_name", "name"],
    "mesco_airline":         ["mesco_name", "name"],
    "mesco_branch":          ["mesco_name", "name"],
    "mesco_shippingline":    ["mesco_name", "name"],
    "mesco_vendor":          ["mesco_name", "name"],
    "account":               ["name"],
    "systemuser":            ["fullname", "name"],
    "transactioncurrency":   ["isocurrencycode", "currencyname", "name"],
    "xollsp_country":        ["xollsp_name", "name"],
    "xollsp_incoterm":       ["xollsp_name", "name"],
    "xollsp_commoditygroup": ["xollsp_name", "name"],
    "xollsp_servicedefinition": ["xollsp_name", "name"],
    "xollsp_tariffquote":    ["xollsp_name", "name"],
    "mesco_operation":       ["mesco_code", "mesco_name", "name"],
}


def _resolve_lookup(
    client: DataverseClientService,
    logical_name: str,
    name_value: str,
) -> Optional[str]:
    """Query Dataverse to find the GUID of *name_value* in *logical_name*.

    Returns the GUID string on success, None on miss or error.
    Logs a warning (not an exception) on miss; caller must drop the field.
    """
    if not name_value or not isinstance(name_value, str):
        return None
    name_value = name_value.strip()
    if not name_value:
        return None

    entity_set  = _entity_set_name(logical_name)
    id_field    = _id_field(logical_name)
    name_fields = _NAME_FIELDS_BY_ENTITY.get(logical_name, ["mesco_name", "name"])

    for name_field in name_fields:
        try:
            safe_value = name_value.replace("'", "''")
            query = (
                f"{entity_set}"
                f"?$filter={name_field} eq '{safe_value}'"
                f"&$select={name_field},{id_field}"
                f"&$top=1"
            )
            resp = client.get(query)
            data = resp.json() if resp.content else {}
            values = data.get("value", []) if isinstance(data, dict) else []
            if values:
                guid = values[0].get(id_field)
                if guid:
                    logger.debug(
                        "Resolved %s '%s' → %s",
                        logical_name, name_value, guid,
                    )
                    return guid
        except Exception as exc:
            logger.debug("Lookup probe %s.%s failed: %s", logical_name, name_field, exc)
            continue

    logger.warning(
        "Could not resolve lookup '%s'='%s' in entity '%s' — field will be dropped",
        logical_name, name_value, entity_set,
    )
    return None


# ---------------------------------------------------------------------------
# Payload preprocessing  [FIX-1] [FIX-2]
# ---------------------------------------------------------------------------

def _preprocess_payload(
    payload: Dict[str, Any],
    entity_set: str = "mesco_operations",
    client: Optional[DataverseClientService] = None,
) -> Dict[str, Any]:
    """Clean and transform a CRM JSON payload for Dataverse compatibility.

    Processing order per field:
      1. Pass through pre-built @odata.bind keys unchanged (caller-injected
         navigation property links).  Record the bare field names they cover
         so the matching plain-string field is suppressed — Dataverse rejects
         a payload that contains both  foo@odata.bind  AND  foo  as keys.
      2. Skip all other @ annotation keys (OData metadata).
      3. Apply field_map rename (OCR name → Dataverse logical name).
      4. Drop invalid / read-only / nested-relation fields.
      5. Resolve lookup string values → @odata.bind.
         *** KEY CONTRACT (FIX-1) ***
         - On SUCCESS:  write ONLY the bind key (e.g. foo@odata.bind).
                        Add bare name to already_bound so the plain string
                        is suppressed on subsequent iterations.
         - On FAILURE:  drop the field entirely.
                        Do NOT write the @odata.bind key.
                        Do NOT add the bare name to already_bound.
         This ensures Dataverse never sees an annotation without a value.
      6. Coerce decimal strings → float/int.
      7. Map picklist strings → integer option-set values.
      8. Pass through everything else unchanged.
    """
    schema          = _ENTITY_SCHEMAS.get(entity_set, {})
    lookups: Dict   = schema.get("lookups", {})
    invalid: Set    = schema.get("invalid", set())
    field_map: Dict = schema.get("field_map", {})
    decimals: Set   = schema.get("decimals", set())
    picklists: Dict = schema.get("picklist_strings", {})

    cleaned: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Pass 1: collect caller-injected @odata.bind keys.
    # These are navigation property bindings already built by upload_crm_json
    # (e.g. mesco_Operation@odata.bind pointing to the master record GUID).
    # Pass them straight through and register their bare names so we do NOT
    # also try to process the matching plain-string field.
    # ------------------------------------------------------------------
    already_bound: Set[str] = set()
    for raw_key, value in payload.items():
        if raw_key.endswith("@odata.bind"):
            bare = raw_key[: -len("@odata.bind")]
            already_bound.add(bare.lower())      # [FIX-9] case-insensitive
            cleaned[raw_key] = value   # pass the bind key straight through

    # ------------------------------------------------------------------
    # Pass 2: process all non-@ keys
    # ------------------------------------------------------------------
    for raw_key, value in payload.items():
        # Skip annotation keys — handled in pass 1 or irrelevant metadata
        if raw_key.startswith("@"):
            continue

        # Apply field rename (e.g. mesco_containerno → mesco_containernumber)
        key = field_map.get(raw_key, raw_key)

        # Drop invalid / read-only / nested-relation fields
        if key in invalid or raw_key in invalid:
            continue

        # Suppress bare field if a bind key was already injected for it [FIX-9]
        if key.lower() in already_bound or raw_key.lower() in already_bound:
            continue

        # --- Lookup field ---
        if key in lookups and isinstance(value, str):
            target_logical = lookups[key]
            if client:
                guid = _resolve_lookup(client, target_logical, value)
                if guid:
                    target_set = _entity_set_name(target_logical)
                    nav_key    = _NAV_PROPERTY_MAP.get(key, key)  # [FIX-9] PascalCase
                    bind_key   = f"{nav_key}@odata.bind"
                    # [FIX-1] Write bind key AND register bare name ONLY on
                    # success.  On failure, do nothing — field is dropped.
                    cleaned[bind_key] = f"/{target_set}({guid})"
                    already_bound.add(key.lower())               # [FIX-9] case-insensitive
                else:
                    # [FIX-1] / [FIX-2] Resolution failed.
                    # Do NOT write the @odata.bind key.
                    # Do NOT write the bare string value.
                    # The field is simply omitted from the cleaned payload.
                    logger.warning(
                        "Lookup '%s'='%s' unresolvable — field dropped from %s payload",
                        key, value, entity_set,
                    )
            # Without a client we cannot resolve → drop to avoid 400
            continue

        # --- Lookup field that is already an integer/GUID (non-string) ---
        # e.g. picklist values that happen to sit in the lookups dict should
        # not be re-processed here; they fall through to the picklist or
        # pass-through blocks below.

        # --- Decimal coercion ---
        if key in decimals:
            if isinstance(value, str):
                try:
                    cleaned[key] = float(value) if "." in value else int(value)
                except (ValueError, TypeError):
                    logger.warning(
                        "Could not coerce decimal field %s='%s' — field dropped",
                        key, value,
                    )
                continue
            # Already numeric — pass through
            cleaned[key] = value
            continue

        # --- Picklist string → int ---
        if key in picklists:
            if isinstance(value, str):
                mapped = picklists[key].get(value.upper())
                if mapped is not None:
                    cleaned[key] = mapped
                else:
                    logger.warning(
                        "Unknown picklist value '%s' for field '%s' — field dropped",
                        value, key,
                    )
                continue
            # Already an integer — pass through unchanged
            cleaned[key] = value
            continue

        # --- Pass through (bool, int, plain string, etc.) ---
        cleaned[key] = value

    return cleaned


# ---------------------------------------------------------------------------
# Entity creation
# ---------------------------------------------------------------------------

def _create_entity(
    client: DataverseClientService,
    entity_set: str,
    fields: Dict[str, Any],
) -> str:
    """POST a new entity record and return its GUID."""
    id_field = _id_field(entity_set)
    try:
        resp = client.post(entity_set, json=fields)
    except Exception as exc:
        body = ""
        if hasattr(exc, "response") and exc.response is not None:
            try:
                body = exc.response.text[:2000]
            except Exception:
                pass
        msg = f"Dataverse POST {entity_set}: {exc}"
        if body:
            msg += f"\nResponse: {body}"
        logger.error(
            "Dataverse POST %s failed.\nPayload keys: %s\nError body: %s",
            entity_set, list(fields.keys()), body,
        )
        raise RuntimeError(msg) from exc

    # 204 No Content with Location header → extract GUID from URL
    location = resp.headers.get("Location") or resp.headers.get("location", "")
    if location:
        guid = location.strip("/").rsplit("(", 1)[-1].rstrip(")")
        return guid

    # 201 Created with body (some Dataverse versions)
    body = resp.json() if resp.content else {}
    guid = body.get(id_field)
    if guid:
        return guid

    raise RuntimeError(
        f"Could not extract GUID from {entity_set} POST response. "
        f"Status: {resp.status_code}, Location: '{location}'"
    )


def _lookup_bind_key(relationship_name: str) -> str:
    return f"{relationship_name}@odata.bind"


# ---------------------------------------------------------------------------
# Payload normalisation  [FIX-7]
# ---------------------------------------------------------------------------

def _normalize_upload_payload(crm_data: Dict[str, Any]) -> Dict[str, Any]:
    """Accept the API wrapper shape and return the master-operation dict.

    The real API response shape is:
        {
            "success": true,
            "data": {                          ← master operation + nested arrays
                "mesco_Operation_mesco_Operation_mesco_Operation": [...],
                "mesco_Container_MasterOperation_mesco_Operation": [...],
                "mesco_Cargo_MasterOperation_mesco_Operation":     [...],
                ... scalar master fields ...
            },
            "house_data": { ... },             ← duplicate view, NOT used
            "records": null,
            "error": null,
            ...
        }

    We extract `data` as the master payload.  The nested house/container/cargo
    arrays are already embedded inside `data`.

    Also handles:
      - Calling with just the `data` dict directly (no wrapper)
      - OData collection shape {"value": [...]} (falls back to first item)
    """
    if not isinstance(crm_data, dict):
        return crm_data

    # [FIX-7] API wrapper: {"success": true/false, "data": {...}}
    # Always prefer "data" when present and it is a dict.
    if isinstance(crm_data.get("data"), dict):
        inner = crm_data["data"]
        # Recursively unwrap in case "data" itself is wrapped
        return _normalize_upload_payload(inner)

    # OData collection wrapper {"value": [...]} — take the first record
    if isinstance(crm_data.get("value"), list):
        items = crm_data["value"]
        if items:
            return _normalize_upload_payload(items[0])
        logger.warning("OData value collection is empty — nothing to upload")
        return {}

    # Plain dict (master operation shape or already-unwrapped)
    return crm_data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

UploadResult = Dict[str, Any]


def upload_crm_json(
    crm_data: Dict[str, Any],
    retry_config: Optional[RetryConfig] = None,
) -> UploadResult:
    """Upload the full CRM JSON (master + houses + containers + cargo) to Dataverse.

    Expected input shape (the full API response):
        {
            "success": true,
            "data": {
                ... master operation scalar fields ...
                "mesco_Operation_mesco_Operation_mesco_Operation": [ {...}, ... ],
                "mesco_Container_MasterOperation_mesco_Operation": [ {...}, ... ],
                "mesco_Cargo_MasterOperation_mesco_Operation":     [ {...}, ... ],
            },
            ...
        }

    Returns:
        {
            "master_id":  "<guid>",
            "houses":     [{"index": 0, "id": "<guid>", "hbl": "..."}],
            "containers": [{"index": 0, "id": "<guid>", "container_no": "..."}],
            "cargo":      [{"index": 0, "id": "<guid>"}],
        }
    """
    crm_data = _normalize_upload_payload(crm_data)
    client   = DataverseClientService.get_instance(retry_config or RetryConfig())

    # Strip OData metadata annotations and explicit nulls
    payload = _clean_odata_meta(crm_data)
    payload = _strip_null(payload)

    result: UploadResult = {
        "master_id":  None,
        "houses":     [],
        "containers": [],
        "cargo":      [],
    }

    # Extract nested arrays BEFORE building the master payload so they are not
    # passed as unknown fields to the master operation POST.
    houses: List[Dict]     = payload.pop("mesco_Operation_mesco_Operation_mesco_Operation", [])
    containers: List[Dict] = payload.pop("mesco_Container_MasterOperation_mesco_Operation", [])
    cargo_list: List[Dict] = payload.pop("mesco_Cargo_MasterOperation_mesco_Operation", [])

    # ------------------------------------------------------------------
    # 1. Master operation
    # ------------------------------------------------------------------
    master_fields = _preprocess_payload(payload, _ENTITY, client)
    master_id     = _create_entity(client, _ENTITY, master_fields)
    result["master_id"] = master_id
    logger.info("Created master operation: %s", master_id)

    # ------------------------------------------------------------------
    # 2. House operations  (linked to master via mesco_Operation lookup)
    # ------------------------------------------------------------------
    for idx, house in enumerate(houses):
        house_clean = _clean_odata_meta(house)
        house_clean = _strip_null(house_clean)
        # Remove any sub-arrays that may be embedded in the house record
        house_clean.pop("mesco_Container_mesco_houses", None)
        house_clean.pop("mesco_Cargo_HouseOperation_mesco_Operation", None)
        house_clean.pop("mesco_Operation", None)

        # Inject the parent master link as a pre-built bind key so the
        # preprocessor passes it straight through (pass 1) without trying to
        # resolve it as a name string.
        house_clean[_lookup_bind_key("mesco_Operation")] = f"/{_ENTITY}({master_id})"

        house_fields = _preprocess_payload(house_clean, _ENTITY, client)
        house_id     = _create_entity(client, _ENTITY, house_fields)

        result["houses"].append({
            "index": idx,
            "id":    house_id,
            "hbl":   house.get("mesco_masterblno"),
        })
        logger.info(
            "  House [%d] hbl=%s → %s", idx, house.get("mesco_masterblno"), house_id,
        )

    # ------------------------------------------------------------------
    # 3. Containers  (linked to master via mesco_MasterOperation)
    # ------------------------------------------------------------------
    for idx, container in enumerate(containers):
        container_clean = _clean_odata_meta(container)
        container_clean = _strip_null(container_clean)

        container_clean[_lookup_bind_key("mesco_MasterOperation")] = (
            f"/{_ENTITY}({master_id})"
        )

        ctn_fields = _preprocess_payload(container_clean, _CONTAINER_ENTITY, client)
        ctn_id     = _create_entity(client, _CONTAINER_ENTITY, ctn_fields)

        container_no = (
            container.get("mesco_containernumber")
            or container.get("mesco_containerno")
            or container.get("mesco_name")
        )
        result["containers"].append({
            "index":        idx,
            "id":           ctn_id,
            "container_no": container_no,
        })
        logger.info("  Container [%d] %s → %s", idx, container_no, ctn_id)

    # ------------------------------------------------------------------
    # 4. Cargo items  (linked to master + matching house + matching container)
    # ------------------------------------------------------------------
    for idx, cargo in enumerate(cargo_list):
        cargo_clean = _clean_odata_meta(cargo)
        cargo_clean = _strip_null(cargo_clean)

        # Always link to master
        cargo_clean[_lookup_bind_key("mesco_MasterOperation")] = (
            f"/{_ENTITY}({master_id})"
        )
        # Link to the matching house (1-to-1 by index)
        if idx < len(result["houses"]):
            house_id = result["houses"][idx]["id"]
            cargo_clean[_lookup_bind_key("mesco_HouseOperation")] = (
                f"/{_ENTITY}({house_id})"
            )
        # Link to the matching container
        if idx < len(result["containers"]):
            ctn_id = result["containers"][idx]["id"]
            cargo_clean[_lookup_bind_key("mesco_Conainter")] = (
                f"/{_CONTAINER_ENTITY}({ctn_id})"
            )

        cargo_fields = _preprocess_payload(cargo_clean, _CARGO_ENTITY, client)
        cargo_id     = _create_entity(client, _CARGO_ENTITY, cargo_fields)

        result["cargo"].append({"index": idx, "id": cargo_id})
        logger.info("  Cargo [%d] → %s", idx, cargo_id)

    return result