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
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from dataverse.client_service import DataverseClientService, RetryConfig
from dataverse_field_limits import limit_for as _registry_limit_for
from dataverse_metadata import is_option_set_field, resolve_option_value

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
    "xollsp_unitsofmeasure":     "xollsp_unitsofmeasures",
    "mesco_operation":           "mesco_operations",
    "mesco_container":           "mesco_containers",
    "mesco_warehouse":           "mesco_warehouses",
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
    "xollsp_unitsofmeasure":     "xollsp_unitsofmeasureid",
    "mesco_operation":           "mesco_operationid",
    "mesco_container":           "mesco_containerid",
    "mesco_warehouse":           "mesco_warehouseid",
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

        # Offline fallback only — the live option-set metadata
        # (dataverse_metadata.py) is the primary source.  Values corrected to
        # match the real Dataverse option sets.
        "picklist_strings": {
            "mesco_pcfreightterm": {
                "PREPAID": 100000000,
                "COLLECT": 100000001,
            },
            "mesco_transporttype": {
                "SEA":   300000000,
                "TRUCK": 300000001,
                "ROAD":  300000001,
                "AIR":   300000002,
                "MULTIMODAL": 300000003,
            },
            "mesco_direction": {
                "IMPORT":   300000000,
                "EXPORT":   300000001,
                "DOMESTIC": 300000002,
                "CROSS":    300000002,
            },
            "mesco_loadtype": {
                "FCL":  300000000,
                "LCL":  300000001,
                "BULK": 886150003,
                "RORO": 886150001,
            },
            "mesco_bltype": {
                "DIRECT": 886150000,
                "MASTER": 886150001,
                "HOUSE":  886150002,
            },
        },

        "field_map": {},

        # Decimal fields — the Excel/OCR extract often returns these as
        # strings (e.g. "67.228", "3.0"), which must be coerced to numbers
        # before sending to Dataverse (Edm.Decimal).
        "decimals": {
            "cr401_totalgrossweight",
            "cr401_totalpackages",
            "cr401_totalteus",
            "cr401_totalvolume",
            "mesco_freetimedestination",
            "mesco_freetimeorigin",
            "mesco_invoicevalue",
            "mesco_quantity",
            "mesco_quantity2",
            "mesco_quantity3",
            "mesco_releasevalue",
            "mesco_totalotherchargesduecarrier",
        },

        # String max-length limit (from Dataverse metadata / errors).
        # Values exceeding this limit are truncated to avoid 400 errors.
        "max_length": {
            "mesco_shippernamecontactno":   100,
            "mesco_consigneenamecontactno": 100,
            "mesco_notifyaddress":          100,
        },
    },

    # -----------------------------------------------------------------------
    # mesco_containers
    # -----------------------------------------------------------------------
    "mesco_containers": {
        "lookups": {
            "mesco_masteroperation": "mesco_operation",
            "mesco_um":              "xollsp_unitsofmeasure",
            "mesco_umpackages":      "xollsp_unitsofmeasure",
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
            "mesco_umpackages":      "xollsp_unitsofmeasure",
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
    "account":               ["name", "accountnumber"],
    "systemuser":            ["fullname", "name"],
    "transactioncurrency":   ["isocurrencycode", "currencyname", "name"],
    # xollsp_country primary name is xollsp_code (full English name, e.g.
    # "Turkey", "United Arab Emirates"); xollsp_name holds the 2-letter ISO
    # code (e.g. "TR", "AE").  Try the full name first, then the ISO code.
    "xollsp_country":        ["xollsp_code", "xollsp_name"],
    "xollsp_incoterm":       ["xollsp_name", "name"],
    "xollsp_commoditygroup": ["xollsp_name", "name"],
    "xollsp_servicedefinition": ["xollsp_name", "name"],
    "xollsp_tariffquote":    ["xollsp_name", "name"],
    "xollsp_unitsofmeasure": ["xollsp_name", "name"],
    "mesco_operation":       ["mesco_code", "mesco_name", "name"],
}

# Per-upload cache: (entity logical name, normalized label) → GUID or None
_LOOKUP_CACHE: Dict[Tuple[str, str], Optional[str]] = {}
_LOOKUP_WARNED: Set[Tuple[str, str]] = set()

# Common B/L text → labels used in Mesco Dataverse (see sample_output / CRM usage)
_LOOKUP_LABEL_HINTS: Dict[str, Dict[str, List[str]]] = {
    "xollsp_address": {
        "ALEXANDRIA": [
            "ALEXANDRIA OLD PORT",
            "ALEXANDRIA, EG",
            "ALEXANDRIA EG",
        ],
        "SHANGHAI": ["SHANGHAI, CN", "SHANGHAI CN", "CNSHA"],
    },
    "account": {
        "MARINE AND ENGINEERING SERVICES COMPANY (MESCO)": [
            "MESCO",
            "MARINE AND ENGINEERING SERVICES COMPANY",
            "MARINE AND ENGINEERING SERVICES CO",
        ],
        "AL KAYAN FOR IMPORT AND EXPORT": [
            "AL KAYAN",
            "AL KAYAN FOR IMPORT",
        ],
    },
    "mesco_shippingline": {
        "EVERGREEN MARINE (ASIA) PTE. LTD.": [
            "EVERGREEN LINE",
            "EVERGREEN",
            "EMC",
            "EVERGREEN MARINE",
        ],
        "EVERGREEN MARINE (ASIA) PTE LTD": [
            "EVERGREEN LINE",
            "EVERGREEN",
            "EMC",
        ],
    },
}


def _odata_escape(value: str) -> str:
    return value.replace("'", "''")


def _normalize_lookup_label(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", (value or "").upper())


def _lookup_search_variants(logical_name: str, name_value: str) -> List[str]:
    """Build ordered search strings (exact first, then CRM-friendly alternates)."""
    base = re.sub(r"\s+", " ", (name_value or "").strip())
    if not base:
        return []

    seen: set[str] = set()
    variants: List[str] = []

    def add(label: str) -> None:
        text = re.sub(r"\s+", " ", (label or "").strip())
        if not text:
            return
        key = text.upper()
        if key in seen:
            return
        seen.add(key)
        variants.append(text)

    add(base)

    paren = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", base)
    if paren:
        add(paren.group(1))
        add(paren.group(2))

    upper = base.upper()
    hints = _LOOKUP_LABEL_HINTS.get(logical_name, {})
    for hint_key, alternates in hints.items():
        if _normalize_lookup_label(hint_key) == _normalize_lookup_label(base):
            for alt in alternates:
                add(alt)
        elif _normalize_lookup_label(hint_key) in _normalize_lookup_label(base):
            for alt in alternates:
                add(alt)

    if logical_name == "account" and "MESCO" in upper:
        add("MESCO")
        add("MARINE AND ENGINEERING SERVICES COMPANY")

    if logical_name == "account":
        tokens = [t for t in re.split(r"\W+", base) if len(t) >= 3]
        if len(tokens) >= 2:
            add(" ".join(tokens[:3]))
            add(" ".join(tokens[:2]))

    if logical_name == "xollsp_address":
        port = upper.split(",")[0].strip()
        if port and port != upper:
            add(port)
        if "ALEXANDRIA" in upper and "ALEXANDRIA" not in seen:
            add("ALEXANDRIA")

    if logical_name == "mesco_shippingline" and "EVERGREEN" in upper:
        add("EVERGREEN LINE")
        add("EVERGREEN")

    return variants


def _match_score(search: str, candidate: str) -> int:
    s = _normalize_lookup_label(search)
    c = _normalize_lookup_label(candidate)
    if not s or not c:
        return 0
    if s == c:
        return 100
    if c.startswith(s) or s.startswith(c):
        return 90
    if s in c or c in s:
        return 75
    search_tokens = set(re.findall(r"[A-Z0-9]{3,}", search.upper()))
    cand_tokens = set(re.findall(r"[A-Z0-9]{3,}", candidate.upper()))
    if search_tokens and cand_tokens:
        overlap = len(search_tokens & cand_tokens)
        if overlap:
            return int(60 * overlap / max(len(search_tokens), len(cand_tokens)))
    return 0


def _pick_best_row(
    rows: List[Dict[str, Any]],
    id_field: str,
    name_fields: List[str],
    search: str,
) -> Optional[str]:
    best_guid: Optional[str] = None
    best_score = 0
    for row in rows:
        for name_field in name_fields:
            label = row.get(name_field)
            if not isinstance(label, str):
                continue
            score = _match_score(search, label)
            if score > best_score:
                best_score = score
                best_guid = row.get(id_field)
    return best_guid if best_score >= 60 else None


def _query_lookup_rows(
    client: DataverseClientService,
    entity_set: str,
    id_field: str,
    name_field: str,
    filter_expr: str,
    select_fields: List[str],
    top: int = 5,
) -> List[Dict[str, Any]]:
    # Only select the id + the single field being filtered.  Selecting every
    # candidate name field (e.g. a generic "name") breaks the request with a
    # 400 when that column does not exist on the target entity (xollsp_*
    # entities have no "name" attribute), which previously caused valid
    # lookups like incoterm "CIF" to silently fail and be dropped.
    select = ",".join(dict.fromkeys([id_field, name_field]))
    query = (
        f"{entity_set}?$filter={filter_expr}"
        f"&$select={select}&$top={top}"
    )
    resp = client.get(query)
    data = resp.json() if resp.content else {}
    values = data.get("value", []) if isinstance(data, dict) else []
    return values if isinstance(values, list) else []


def _resolve_lookup(
    client: DataverseClientService,
    logical_name: str,
    name_value: str,
) -> Optional[str]:
    """Query Dataverse to find the GUID of *name_value* in *logical_name*.

    Tries exact match, then startswith/contains with scoring (CRM names often
    differ from B/L text, e.g. ALEXANDRIA → ALEXANDRIA OLD PORT).
    """
    if not name_value or not isinstance(name_value, str):
        return None
    name_value = name_value.strip()
    if not name_value:
        return None

    cache_key = (logical_name, _normalize_lookup_label(name_value))
    if cache_key in _LOOKUP_CACHE:
        return _LOOKUP_CACHE[cache_key]

    entity_set = _entity_set_name(logical_name)
    id_field = _id_field(logical_name)
    name_fields = _NAME_FIELDS_BY_ENTITY.get(logical_name, ["mesco_name", "name"])
    select_fields = list(name_fields)

    for variant in _lookup_search_variants(logical_name, name_value):
        safe = _odata_escape(variant)
        for name_field in name_fields:
            try:
                rows = _query_lookup_rows(
                    client,
                    entity_set,
                    id_field,
                    name_field,
                    f"{name_field} eq '{safe}'",
                    select_fields,
                    top=1,
                )
                if rows:
                    guid = rows[0].get(id_field)
                    if guid:
                        logger.info(
                            "Resolved %s '%s' (as '%s') → %s",
                            logical_name,
                            name_value,
                            variant,
                            guid,
                        )
                        _LOOKUP_CACHE[cache_key] = guid
                        return guid
            except Exception as exc:
                logger.debug(
                    "Lookup eq %s.%s='%s' failed: %s",
                    logical_name,
                    name_field,
                    variant,
                    exc,
                )

        if len(_normalize_lookup_label(variant)) < 3:
            continue

        for name_field in name_fields:
            for op in ("startswith", "contains"):
                try:
                    rows = _query_lookup_rows(
                        client,
                        entity_set,
                        id_field,
                        name_field,
                        f"{op}({name_field},'{safe}')",
                        select_fields,
                        top=8,
                    )
                    guid = _pick_best_row(rows, id_field, name_fields, variant)
                    if guid:
                        logger.info(
                            "Resolved %s '%s' via %s on '%s' → %s",
                            logical_name,
                            name_value,
                            op,
                            variant,
                            guid,
                        )
                        _LOOKUP_CACHE[cache_key] = guid
                        return guid
                except Exception as exc:
                    logger.debug(
                        "Lookup %s %s.%s='%s' failed: %s",
                        op,
                        logical_name,
                        name_field,
                        variant,
                        exc,
                    )

    if cache_key not in _LOOKUP_WARNED:
        _LOOKUP_WARNED.add(cache_key)
        logger.warning(
            "Could not resolve lookup '%s'='%s' in entity '%s' — field will be dropped",
            logical_name,
            name_value,
            entity_set,
        )
    _LOOKUP_CACHE[cache_key] = None
    return None


def clear_lookup_cache() -> None:
    """Reset lookup cache (useful between tests or batch uploads)."""
    _LOOKUP_CACHE.clear()
    _LOOKUP_WARNED.clear()


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
    max_length: Dict = schema.get("max_length", {})

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
                    # [FIX-1] / [FIX-2] Resolution failed — field omitted (warn once in resolver).
                    pass
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

        # --- Picklist / choice (option set) → int ---
        # Prefer the live option-set metadata fetched from Dataverse so the
        # value maps to the integer that actually exists in the database
        # (fixes direction/loadtype/transporttype/bltype, and enables
        # container-type mapping that was previously dropped).  Fall back to
        # the local hardcoded map only when metadata yields nothing.
        if key in picklists or is_option_set_field(entity_set, key, client):
            mapped = resolve_option_value(entity_set, key, value, client)
            if mapped is None and key in picklists and isinstance(value, str):
                mapped = picklists[key].get(value.upper())
            if mapped is not None:
                cleaned[key] = mapped
            else:
                logger.warning(
                    "Unknown picklist value '%s' for field '%s' — field dropped",
                    value, key,
                )
            continue

        # --- Truncate string values that exceed Dataverse max length ---
        # Two layers: entity-local override map (legacy) first, then the
        # central dataverse_field_limits registry as a safety net so a single
        # oversized field (e.g. mesco_cargodescription > 1500) can never
        # block the entire save with a 0x80048d19 / 400 error again.
        if isinstance(value, str):
            max_len = max_length.get(key)
            if max_len is None:
                max_len = _registry_limit_for(entity_set, key)
            if max_len and len(value) > max_len:
                logger.warning(
                    "Truncating '%s' from %d to %d chars (Dataverse max length)",
                    key, len(value), max_len,
                )
                value = value[:max_len]

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


def _link_container_to_house(
    client: DataverseClientService,
    house_id: str,
    container_id: str,
) -> None:
    """Associate a container with a house operation (many-to-many)."""
    rel = "mesco_Container_mesco_houses"
    url = f"{_ENTITY}({house_id})/{rel}/$ref"
    container_url = f"{client.base_url.rstrip('/')}/{_CONTAINER_ENTITY}({container_id})"
    client.post(url, json={"@odata.id": container_url})


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
    clear_lookup_cache()
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
            "mbl":   house.get("mesco_masterbllinkno"),
        })
        logger.info(
            "  House [%d] hbl=%s mbl=%s → %s",
            idx,
            house.get("mesco_masterblno"),
            house.get("mesco_masterbllinkno"),
            house_id,
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

    # Link each container to every house on the operation (shared equipment).
    if result["houses"] and result["containers"]:
        for house_info in result["houses"]:
            for ctn_info in result["containers"]:
                try:
                    _link_container_to_house(client, house_info["id"], ctn_info["id"])
                except Exception as exc:
                    logger.warning(
                        "Container %s ↔ house %s association failed: %s",
                        ctn_info.get("container_no"),
                        house_info.get("hbl"),
                        exc,
                    )

    # ------------------------------------------------------------------
    # 4. Cargo items  (linked to master + matching house + matching container)
    # ------------------------------------------------------------------
    multi_house = len(result["houses"]) > 1
    for idx, cargo in enumerate(cargo_list):
        cargo_clean = _clean_odata_meta(cargo)
        cargo_clean = _strip_null(cargo_clean)

        # Always link to master
        cargo_clean[_lookup_bind_key("mesco_MasterOperation")] = (
            f"/{_ENTITY}({master_id})"
        )
        # Single-house shipments link cargo to the house; consolidated masters keep cargo on master only.
        if not multi_house and idx < len(result["houses"]):
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