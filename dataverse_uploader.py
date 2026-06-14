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

  [FIX-6]  Container No: mesco_containerno is a lookup to mesco_containerno entity
           (primary name = container number).  Resolve to mesco_ContainerNo@odata.bind;
           do not rename to mesco_containernumber (that text field is optional).

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
from crm_output_formatter import (
    infer_package_unit_label,
    is_house_bl_type,
    prepare_standalone_house_upload,
)

logger = logging.getLogger(__name__)

_ENTITY           = "mesco_operations"
_CONTAINER_ENTITY = "mesco_containers"
_CARGO_ENTITY     = "mesco_cargos"

# mesco_bltype option-set values (master vs house operation).
_MASTER_BL_TYPE = 886150001
_HOUSE_BL_TYPE  = 886150002

# Lookup *value* columns used by duplicate-detection $filter queries.
_OP_PARENT_MASTER_VALUE_FIELD = "_mesco_operation_value"
_CONTAINER_MASTER_VALUE_FIELD = "_mesco_masteroperation_value"
_CARGO_MASTER_VALUE_FIELD     = "_mesco_masteroperation_value"
_CARGO_HOUSE_VALUE_FIELD      = "_mesco_houseoperation_value"

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
    "mesco_containerno":         "mesco_containernos",
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
    "mesco_containerno":         "mesco_containernoid",
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
            "mesco_bookingterm": {
                "FREEHAND": 886150000,
                "NOMINATION": 886150001,
                "Freehand": 886150000,
                "Nomination": 886150001,
            },
        },

        "field_map": {},

        # Date / DateTime fields — OCR/LLM emit assorted formats
        # ("17/12/2025", "17-DEC-2025", "2025-12-17").  Normalize to ISO
        # (YYYY-MM-DD) so Dataverse never mis-parses day/month or shifts the
        # day across time zones.
        "dates": {
            "mesco_etdorigin",
            "mesco_etadestination",
            "mesco_etaoriign",
            "mesco_atadestination",
            "mesco_atdorigin",
            "mesco_cutoffdate",
            "mesco_closedate",
            "mesco_releasedate",
            "mesco_pickupdate",
            "mesco_expectedpickupdate",
            "mesco_flightdate",
            "mesco_dodate",
        },

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
            "mesco_deliveryaddress":        100,
            "mesco_handlinginformation":    100,
            "mesco_shipperaddress":         250,
            "mesco_consigneeaddress":       250,
        },
    },

    # -----------------------------------------------------------------------
    # mesco_containers
    # -----------------------------------------------------------------------
    "mesco_containers": {
        "lookups": {
            "mesco_masteroperation": "mesco_operation",
            "mesco_containerno":     "mesco_containerno",
            "mesco_um":              "xollsp_unitsofmeasure",
            "mesco_umpackages":      "xollsp_unitsofmeasure",
            "mesco_warehouse":       "mesco_warehouse",
        },
        "invalid": {
            # Nested expand objects from sample JSON — not valid on POST.
            "mesco_ContainerNo",
            # mesco_containertype on the container entity is an OPTION SET, but
            # the extractor produces an ISO string ("40HC", "20GP").  The ISO
            # value is preserved as text on the operation record's
            # mesco_containertype, so strip it here to avoid a 400 on the
            # container POST.
            "mesco_containertype",
        },
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
            "mesco_imoclass",
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
            "mesco_imoclass",
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

try:
    from dynamics_schema_sync import apply_generated_schema_metadata

    _GENERATED_DYNAMICS_SCHEMAS = apply_generated_schema_metadata(
        _ENTITY_SCHEMAS,
        _NAV_PROPERTY_MAP,
        _ENTITY_SET_MAP,
        _ID_FIELD_MAP,
    )
except Exception as exc:  # pragma: no cover - schema sync is best-effort.
    logger.warning("Generated Dynamics schema sync skipped: %s", exc)
    _GENERATED_DYNAMICS_SCHEMAS = {}


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
    "mesco_containerno":     ["mesco_containerno"],
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
    "mesco_agent": {
        "BYTEPORT LOGISTICS TECHNOLOGIES PRIVATE LIMITED": [
            "BYTEPORT LOGISTICS",
            "BYTEPORT",
            "BYTEPORT LOGISTICS TECHNOLOGIES",
            "BYTEPORT LOGISTICS TECHNOLOGIES PVT LTD",
        ],
        "UTT LOGISTICS AND FOREIGN TRADE LTD.": [
            "UTT LOGISTICS",
            "UTT LOGISTICS AND FOREIGN TRADE",
        ],
    },
    "xollsp_unitsofmeasure": {
        "DRUMS": ["DRUM", "DRUMS", "DRM", "DRMS"],
        "PALLETS": ["PALLET", "PALLETS", "PLT", "PLTS"],
        "CARTONS": ["CARTON", "CARTONS", "CTN", "CTNS"],
        "PACKAGES": ["PACKAGE", "PACKAGES", "PKG", "PKGS"],
        "BOXES": ["BOX", "BOXES"],
        "BAGS": ["BAG", "BAGS"],
        "ROLLS": ["ROLL", "ROLLS"],
        "CASES": ["CASE", "CASES"],
        "CRATES": ["CRATE", "CRATES"],
        "BUNDLES": ["BUNDLE", "BUNDLES"],
        "CANS": ["CAN", "CANS"],
    },
}


_MESCO_ACCOUNT_CANONICAL_LABEL = "MARINE AND ENGINEERING SERVICES COMPANY (MESCO)"
_MESCO_ACCOUNT_SEARCH_LABELS = (
    _MESCO_ACCOUNT_CANONICAL_LABEL,
    "MESCO - MARINE AND ENGINEERING SERVICES COMPANY",
    "MESco - Marine and engineering services company",
    "MARINE AND ENGINEERING SERVICES COMPANY",
    "MARINE AND ENGINEERING SERVICES CO",
    "MESCO",
)


# Dataverse decimal columns allow 0 .. 100_000_000_000.
_MAX_DECIMAL_FIELD = 100_000_000_000.0
_MAX_GROSS_KG = 500_000.0
_MAX_PACKAGES = 1_000_000.0
_MAX_CBM = 100_000.0


def _parse_decimal(value: Any) -> Optional[float]:
    """Coerce numbers or strings like '7 PALLETS' / '51.0 kg' to a float."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = float(value)
        if not (0 <= num <= _MAX_DECIMAL_FIELD):
            return None
        return num
    text = str(value).replace(",", "").strip()
    if not text:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    num = float(match.group(1))
    if not (0 <= num <= _MAX_DECIMAL_FIELD):
        return None
    return num


def _sanitize_cargo_quantity(
    value: Any,
    *,
    field: str,
) -> Optional[float]:
    """Parse and bound a cargo/operation quantity before PATCH."""
    num = _parse_decimal(value)
    if num is None:
        return None
    if field in ("mesco_grosskg", "cr401_totalgrossweight"):
        cap = _MAX_GROSS_KG
    elif field in ("mesco_noofpackages", "cr401_totalpackages"):
        cap = _MAX_PACKAGES
    elif field in ("mesco_volcbm", "cr401_totalvolume"):
        cap = _MAX_CBM
    else:
        cap = _MAX_DECIMAL_FIELD
    if num < 0 or num > cap:
        logger.warning("Dropping out-of-range %s value: %r", field, value)
        return None
    return num


def _normalize_container_number(value: str) -> str:
    return re.sub(r"\s+", "", (value or "").strip()).upper()


_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _normalize_date(value: Any) -> Optional[str]:
    """Normalize assorted date strings to ISO date-only (YYYY-MM-DD).

    Shipping documents use day-first formats (DD/MM/YYYY); ambiguous numeric
    dates are interpreted day-first.  Returns None when no date is found.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None

    # Already ISO (optionally with time) — keep the date portion.
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    # DD MON YYYY  /  MON DD YYYY  (e.g. 17-DEC-2025, DEC 17 2025)
    m = re.search(r"(\d{1,2})[ \-/]([A-Za-z]{3,})[ \-/](\d{2,4})", text)
    if m and m.group(2)[:3].upper() in _MONTHS:
        d, mo, y = int(m.group(1)), _MONTHS[m.group(2)[:3].upper()], int(m.group(3))
        y = y + 2000 if y < 100 else y
        if 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.search(r"([A-Za-z]{3,})[ \-/](\d{1,2})[, \-/]+(\d{2,4})", text)
    if m and m.group(1)[:3].upper() in _MONTHS:
        mo, d, y = _MONTHS[m.group(1)[:3].upper()], int(m.group(2)), int(m.group(3))
        y = y + 2000 if y < 100 else y
        if 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    # Numeric DD/MM/YYYY or DD-MM-YYYY (day-first) — also handle YYYY/MM/DD.
    m = re.search(r"(\d{1,4})[/.\-](\d{1,2})[/.\-](\d{1,4})", text)
    if m:
        a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if a > 31:  # YYYY-MM-DD style
            y, mo, d = a, b, c
        else:       # DD/MM/YYYY (day-first)
            d, mo, y = a, b, c
            y = y + 2000 if y < 100 else y
            if mo > 12 and d <= 12:  # value was actually MM/DD — swap
                d, mo = mo, d
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    return None


def _containertype_option_from_iso(iso: Optional[str]) -> int:
    """Map ISO container codes (40HC, 20GP, …) to mesco_containerno picklist values."""
    text = (iso or "").upper()
    if "RF" in text or "REEFER" in text:
        return 100000002
    if "HC" in text or "HQ" in text:
        return 100000001
    return 100000000


def _um_hint_from_container_type(container_type: Optional[str]) -> Optional[str]:
    if not container_type:
        return None
    text = str(container_type).upper()
    if "40" in text:
        return "40 FT"
    if "20" in text:
        return "20 FT"
    return None


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

    upper = base.upper()

    if logical_name == "account" and "MESCO" in upper:
        for label in _MESCO_ACCOUNT_SEARCH_LABELS:
            add(label)

    add(base)

    paren = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", base)
    if paren:
        add(paren.group(1))
        add(paren.group(2))

    hints = _LOOKUP_LABEL_HINTS.get(logical_name, {})
    for hint_key, alternates in hints.items():
        if _normalize_lookup_label(hint_key) == _normalize_lookup_label(base):
            for alt in alternates:
                add(alt)
        elif _normalize_lookup_label(hint_key) in _normalize_lookup_label(base):
            for alt in alternates:
                add(alt)

    if logical_name == "account" and "MESCO" in upper:
        for label in _MESCO_ACCOUNT_SEARCH_LABELS:
            add(label)

    if logical_name == "mesco_agent":
        if "BYTEPORT" in upper:
            add("BYTEPORT LOGISTICS")
            add("BYTEPORT")
        if "UTT" in upper and "LOGISTICS" in upper:
            add("UTT LOGISTICS")

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

    if logical_name == "account" and "MESCO" in name_value.upper():
        canonical_key = (logical_name, _normalize_lookup_label(_MESCO_ACCOUNT_CANONICAL_LABEL))
        if canonical_key in _LOOKUP_CACHE:
            guid = _LOOKUP_CACHE[canonical_key]
            _LOOKUP_CACHE[(logical_name, _normalize_lookup_label(name_value))] = guid
            return guid

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
                        if logical_name == "account" and "MESCO" in name_value.upper():
                            _LOOKUP_CACHE[
                                (logical_name, _normalize_lookup_label(_MESCO_ACCOUNT_CANONICAL_LABEL))
                            ] = guid
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
                        if logical_name == "account" and "MESCO" in name_value.upper():
                            _LOOKUP_CACHE[
                                (logical_name, _normalize_lookup_label(_MESCO_ACCOUNT_CANONICAL_LABEL))
                            ] = guid
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


def _um_label_variants(container_type_hint: Optional[str]) -> List[str]:
    """Ordered U/M name candidates for a container type (xollsp_unitsofmeasure)."""
    text = (container_type_hint or "").upper()
    is_40 = "40" in text
    is_hc = "HC" in text or "HQ" in text or "HIGH CUBE" in text
    if is_40:
        return [
            "40 HC" if is_hc else "40 FT",
            "40 FT", "40HC", "40 HC", "40HQ", "40 HQ",
            "40'", "40FT", "40", "40 DV", "40DC",
        ]
    return ["20 FT", "20FT", "20'", "20", "20 DV", "20DC", "20 GP"]


def _resolve_container_um(
    client: DataverseClientService,
    container_type_hint: Optional[str],
) -> Optional[str]:
    for label in _um_label_variants(container_type_hint):
        guid = _resolve_lookup(client, "xollsp_unitsofmeasure", label)
        if guid:
            return guid
    return None


def _resolve_containerno_lookup(
    client: DataverseClientService,
    name_value: str,
    container_type_hint: Optional[str] = None,
) -> Optional[str]:
    """Resolve or create a mesco_containerno record for a container number."""
    normalized = _normalize_container_number(name_value)
    if not normalized:
        return None

    for candidate in (normalized, name_value.strip()):
        guid = _resolve_lookup(client, "mesco_containerno", candidate)
        if guid:
            return guid

    payload: Dict[str, Any] = {
        "mesco_containerno": normalized,
        "mesco_containertype": _containertype_option_from_iso(container_type_hint),
    }
    # U/M is application-required (form level) but optional at the Web API level.
    # Bind it when we can resolve it; otherwise create the record without it so
    # the container number lookup is never dropped just because the U/M is unknown.
    um_guid = _resolve_container_um(client, container_type_hint)
    if um_guid:
        payload[f"{_NAV_PROPERTY_MAP['mesco_umcontainer']}@odata.bind"] = (
            f"/{_entity_set_name('xollsp_unitsofmeasure')}({um_guid})"
        )
    else:
        logger.info(
            "No U/M resolved for new container no '%s' (type=%s) — creating without U/M",
            normalized,
            container_type_hint,
        )
    try:
        guid = _create_entity(client, "mesco_containernos", payload)
        cache_key = ("mesco_containerno", _normalize_lookup_label(normalized))
        _LOOKUP_CACHE[cache_key] = guid
        logger.info("Created container no '%s' → %s", normalized, guid)
        return guid
    except Exception as exc:
        logger.warning(
            "Failed to create container no '%s': %s",
            normalized,
            exc,
        )
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
    dates: Set      = schema.get("dates", set())
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
        if raw_key.startswith("@") or raw_key.startswith("_"):
            continue
        if raw_key.startswith("dg_"):
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
                if target_logical == "mesco_containerno":
                    guid = _resolve_containerno_lookup(
                        client,
                        value,
                        payload.get("mesco_containertype"),
                    )
                else:
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

        # --- Date / DateTime normalization → ISO date-only ---
        if key in dates:
            iso = _normalize_date(value)
            if iso:
                cleaned[key] = iso
            elif isinstance(value, str):
                logger.warning(
                    "Could not parse date field %s='%s' — field dropped",
                    key, value,
                )
            else:
                cleaned[key] = value
            continue

        # --- Decimal coercion ---
        if key in decimals:
            parsed = _parse_decimal(value)
            if parsed is not None:
                cleaned[key] = int(parsed) if parsed.is_integer() else parsed
            elif isinstance(value, str):
                logger.warning(
                    "Could not coerce decimal field %s='%s' — field dropped",
                    key, value,
                )
            else:
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
                from dataverse_field_limits import cap_field

                logger.warning(
                    "Truncating '%s' from %d to %d chars (Dataverse max length)",
                    key, len(value), max_len,
                )
                value = cap_field(entity_set, key, value) or value[:max_len]

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
    from dataverse_field_limits import cap_record

    id_field = _id_field(entity_set)
    entity_key = (
        entity_set
        if entity_set in {"mesco_operations", "mesco_cargos", "mesco_containers"}
        else "mesco_operations"
    )
    payload = cap_record(entity_key, dict(fields))

    def _post(body: Dict[str, Any]):
        return client.post(entity_set, json=body)

    try:
        resp = _post(payload)
    except Exception as exc:
        body = ""
        if hasattr(exc, "response") and exc.response is not None:
            try:
                body = exc.response.text[:2000]
            except Exception:
                pass
        repaired = _repair_oversized_patch_fields(entity_set, payload, body) if body else None
        if repaired and repaired != payload:
            try:
                resp = _post(repaired)
            except Exception as retry_exc:
                exc = retry_exc
                body = ""
                if hasattr(retry_exc, "response") and retry_exc.response is not None:
                    try:
                        body = retry_exc.response.text[:2000]
                    except Exception:
                        pass
            else:
                location = resp.headers.get("Location") or resp.headers.get("location", "")
                if location:
                    return location.strip("/").rsplit("(", 1)[-1].rstrip(")")
                body_json = resp.json() if resp.content else {}
                guid = body_json.get(id_field)
                if guid:
                    return guid
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
# Duplicate detection / upsert helpers
#
# Operations created from prior uploads (or manual data entry in Dataverse)
# must NOT be duplicated.  We match on the B/L number + bltype and, for houses,
# the parent-master link.  When a match exists we PATCH the existing record and
# reuse its GUID so houses link to the correct master and cargo/containers are
# not re-created.
# ---------------------------------------------------------------------------

def _query_first(
    client: DataverseClientService,
    entity_set: str,
    filter_expr: str,
    select: str,
) -> Optional[Dict[str, Any]]:
    query = f"{entity_set}?$filter={filter_expr}&$select={select}&$top=1"
    try:
        resp = client.get(query)
        data = resp.json() if resp.content else {}
        rows = data.get("value") if isinstance(data, dict) else None
        if isinstance(rows, list) and rows:
            return rows[0]
    except Exception as exc:
        logger.warning("Dataverse dedup query failed (%s): %s", filter_expr, exc)
    return None


def _query_many(
    client: DataverseClientService,
    entity_set: str,
    filter_expr: str,
    select: str,
    *,
    top: int = 25,
) -> List[Dict[str, Any]]:
    query = f"{entity_set}?$filter={filter_expr}&$select={select}&$top={top}"
    try:
        resp = client.get(query)
        data = resp.json() if resp.content else {}
        rows = data.get("value") if isinstance(data, dict) else None
        return list(rows or []) if isinstance(rows, list) else []
    except Exception as exc:
        logger.warning("Dataverse candidate query failed (%s): %s", filter_expr, exc)
    return []


def _find_existing_operation(
    client: DataverseClientService,
    bl_no: Optional[str],
    *,
    is_house: bool,
    master_id: Optional[str] = None,
) -> Optional[str]:
    """Return the GUID of an existing operation matching *bl_no* (or None)."""
    if not bl_no:
        return None
    safe = _odata_escape(str(bl_no).strip())
    if not safe:
        return None
    id_field = _id_field(_ENTITY)
    # On mesco_operations the B/L number lives in mesco_masterblno for BOTH
    # master and house rows (a house stores its HBL there; mesco_houseblno is
    # NOT a column on this entity). Master vs house is distinguished by
    # mesco_bltype, and a house additionally links to its parent via the
    # mesco_operation lookup (_mesco_operation_value).
    bltype = _HOUSE_BL_TYPE if is_house else _MASTER_BL_TYPE
    clauses = (
        f"mesco_masterblno eq '{safe}'",
        f"mesco_bltype eq {bltype}",
    )
    # First try scoped to the parent master (most precise for houses).
    if is_house and master_id:
        scoped = " and ".join(
            clauses + (f"{_OP_PARENT_MASTER_VALUE_FIELD} eq {master_id}",)
        )
        row = _query_first(client, _ENTITY, scoped, id_field)
        if row and row.get(id_field):
            return row[id_field]
        # Reuse only truly orphaned houses for this master.  Do not grab a
        # same-number house that is already linked to another master.
        orphan = " and ".join(
            clauses + (f"{_OP_PARENT_MASTER_VALUE_FIELD} eq null",)
        )
        row = _query_first(client, _ENTITY, orphan, id_field)
        if row and row.get(id_field):
            return row[id_field]
        return None
    row = _query_first(client, _ENTITY, " and ".join(clauses), id_field)
    return row.get(id_field) if row else None


_MASTER_MATCH_SELECT = ",".join(
    (
        "mesco_operationid",
        "mesco_code",
        "mesco_masterblno",
        "mesco_voytruckno",
        "mesco_etdorigin",
        "mesco_atdorigin",
        "mesco_shippedonboarddate",
        "_mesco_vessel_value",
        "_mesco_origin_value",
        "_mesco_destination_value",
    )
)


def _norm_match_text(value: Any) -> str:
    text = re.sub(r"[^A-Z0-9]+", " ", str(value or "").upper())
    return re.sub(r"\s+", " ", text).strip()


def _row_lookup_label(row: Dict[str, Any], logical_key: str) -> str:
    value_key = f"_{logical_key}_value"
    for key in (
        f"{value_key}@OData.Community.Display.V1.FormattedValue",
        f"{value_key}@Microsoft.Dynamics.CRM.lookuplogicalname",
        logical_key,
        _NAV_PROPERTY_MAP.get(logical_key, logical_key),
    ):
        if row.get(key):
            return str(row[key])
    return ""


def _payload_container_numbers(
    payload: Dict[str, Any],
    containers: Optional[List[Dict[str, Any]]] = None,
) -> List[str]:
    numbers: List[str] = []

    def add(value: Any) -> None:
        normalized = _normalize_container_number(str(value or ""))
        if normalized and normalized not in numbers:
            numbers.append(normalized)

    add(payload.get("container_number"))
    for key in ("mesco_containernumber", "mesco_containerno", "mesco_name"):
        add(payload.get(key))
    for item in containers or []:
        if not isinstance(item, dict):
            continue
        for key in ("container_number", "mesco_containernumber", "mesco_containerno", "mesco_name"):
            add(item.get(key))
    return numbers


def _payload_match_dates(payload: Dict[str, Any]) -> Set[str]:
    dates: Set[str] = set()
    for key in (
        "mesco_etdorigin",
        "mesco_atdorigin",
        "mesco_shippedonboarddate",
        "mesco_dateofissue",
        "laden_date",
    ):
        value = _normalize_date(payload.get(key))
        if value:
            dates.add(value)
    return dates


def _row_match_dates(row: Dict[str, Any]) -> Set[str]:
    dates: Set[str] = set()
    for key in ("mesco_etdorigin", "mesco_atdorigin", "mesco_shippedonboarddate"):
        value = _normalize_date(row.get(key))
        if value:
            dates.add(value)
    return dates


def _candidate_master_ids_by_container(
    client: DataverseClientService,
    container_numbers: List[str],
) -> Set[str]:
    ids: Set[str] = set()
    select = f"{_id_field(_CONTAINER_ENTITY)},{_CONTAINER_MASTER_VALUE_FIELD},mesco_containernumber"
    for number in container_numbers:
        safe = _odata_escape(number)
        if not safe:
            continue
        rows = _query_many(
            client,
            _CONTAINER_ENTITY,
            f"mesco_containernumber eq '{safe}' and {_CONTAINER_MASTER_VALUE_FIELD} ne null",
            select,
            top=10,
        )
        for row in rows:
            master_id = row.get(_CONTAINER_MASTER_VALUE_FIELD)
            if master_id:
                ids.add(str(master_id))
    return ids


def _get_master_candidate_row(
    client: DataverseClientService,
    master_id: str,
) -> Optional[Dict[str, Any]]:
    return _query_first(
        client,
        _ENTITY,
        f"{_id_field(_ENTITY)} eq {master_id} and mesco_bltype eq {_MASTER_BL_TYPE}",
        _MASTER_MATCH_SELECT,
    )


def _master_candidate_rows_by_voyage(
    client: DataverseClientService,
    voyage: Any,
) -> List[Dict[str, Any]]:
    safe = _odata_escape(str(voyage or "").strip())
    if not safe:
        return []
    return _query_many(
        client,
        _ENTITY,
        f"mesco_bltype eq {_MASTER_BL_TYPE} and mesco_voytruckno eq '{safe}'",
        _MASTER_MATCH_SELECT,
        top=25,
    )


def _score_master_match(
    row: Dict[str, Any],
    payload: Dict[str, Any],
    container_master_ids: Set[str],
    house_dates: Set[str],
) -> Tuple[int, List[str]]:
    score = 0
    reasons: List[str] = []
    row_id = row.get(_id_field(_ENTITY))

    payload_vessel = _norm_match_text(payload.get("mesco_vessel"))
    row_vessel = _norm_match_text(_row_lookup_label(row, "mesco_vessel"))
    payload_voyage = _norm_match_text(payload.get("mesco_voytruckno"))
    row_voyage = _norm_match_text(row.get("mesco_voytruckno"))
    if payload_vessel and row_vessel and payload_vessel == row_vessel:
        score += 2
        reasons.append("vessel")
    if payload_voyage and row_voyage and payload_voyage == row_voyage:
        score += 3
        reasons.append("voyage")

    if row_id and str(row_id) in container_master_ids:
        score += 4
        reasons.append("container")

    for logical_key, label in (("mesco_origin", "origin"), ("mesco_destination", "destination")):
        payload_port = _norm_match_text(payload.get(logical_key))
        row_port = _norm_match_text(_row_lookup_label(row, logical_key))
        if payload_port and row_port and (payload_port == row_port or payload_port in row_port or row_port in payload_port):
            score += 1
            reasons.append(label)

    if house_dates and (_row_match_dates(row) & house_dates):
        score += 2
        reasons.append("date")

    return score, reasons


def _find_master_by_shipment_evidence(
    client: DataverseClientService,
    payload: Dict[str, Any],
    containers: Optional[List[Dict[str, Any]]] = None,
) -> Optional[str]:
    """Find an existing master from vessel/voyage + container + route + date evidence."""
    id_field = _id_field(_ENTITY)
    container_numbers = _payload_container_numbers(payload, containers)
    container_master_ids = _candidate_master_ids_by_container(client, container_numbers)

    candidate_rows: Dict[str, Dict[str, Any]] = {}
    for row in _master_candidate_rows_by_voyage(client, payload.get("mesco_voytruckno")):
        if row.get(id_field):
            candidate_rows[str(row[id_field])] = row
    for master_id in container_master_ids:
        row = _get_master_candidate_row(client, master_id)
        if row and row.get(id_field):
            candidate_rows[str(row[id_field])] = row

    if not candidate_rows:
        return None

    house_dates = _payload_match_dates(payload)
    scored: List[Tuple[int, str, List[str]]] = []
    for master_id, row in candidate_rows.items():
        score, reasons = _score_master_match(row, payload, container_master_ids, house_dates)
        scored.append((score, master_id, reasons))
    scored.sort(reverse=True, key=lambda item: item[0])
    best_score, best_id, best_reasons = scored[0]
    tied = [item for item in scored if item[0] == best_score]
    if len(tied) > 1:
        logger.warning(
            "Shipment-evidence master match is ambiguous: score=%s ids=%s",
            best_score,
            [item[1] for item in tied],
        )
        return None
    strong_anchor = "container" in best_reasons or (
        "vessel" in best_reasons and "voyage" in best_reasons
    )
    if strong_anchor and best_score >= 6:
        logger.info(
            "Linked standalone house to master %s by shipment evidence (%s, score=%s)",
            best_id,
            ", ".join(best_reasons),
            best_score,
        )
        return best_id
    return None


def _find_orphan_house_operations(
    client: DataverseClientService,
    bl_no: Optional[str],
) -> List[Dict[str, Any]]:
    """Return unlinked house operations with the same B/L number as a master."""
    if not bl_no:
        return []
    safe = _odata_escape(str(bl_no).strip())
    if not safe:
        return []
    id_field = _id_field(_ENTITY)
    select = f"{id_field},mesco_code,mesco_masterblno,mesco_bltype,{_OP_PARENT_MASTER_VALUE_FIELD}"
    query = (
        f"{_ENTITY}?$filter=mesco_masterblno eq '{safe}'"
        f" and mesco_bltype eq {_HOUSE_BL_TYPE}"
        f" and {_OP_PARENT_MASTER_VALUE_FIELD} eq null"
        f"&$select={select}&$top=100"
    )
    try:
        resp = client.get(query)
        data = resp.json() if resp.content else {}
        return list(data.get("value") or [])
    except Exception as exc:
        logger.warning("Orphan-house query failed for %s: %s", bl_no, exc)
        return []


def _adopt_orphan_houses_for_master(
    client: DataverseClientService,
    master_id: str,
    master_bl: Optional[str],
    master_shippingline_bind: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Link same-B/L orphan house operations to a master operation."""
    adopted: List[Dict[str, Any]] = []
    if not master_id:
        return adopted
    bind = {_lookup_bind_key("mesco_Operation"): f"/{_ENTITY}({master_id})"}
    if master_shippingline_bind:
        bind[_lookup_bind_key("mesco_ShippingLine")] = master_shippingline_bind
    id_field = _id_field(_ENTITY)
    for row in _find_orphan_house_operations(client, master_bl):
        house_id = row.get(id_field)
        if not house_id or house_id == master_id:
            continue
        _update_entity(client, _ENTITY, house_id, bind)
        adopted.append(
            {
                "index": len(adopted),
                "id": house_id,
                "hbl": row.get("mesco_masterblno") or master_bl,
                "mbl": master_bl,
                "reused": True,
                "adopted": True,
                "code": row.get("mesco_code"),
            }
        )
    return adopted


def _get_operation_parent(
    client: DataverseClientService,
    op_id: Optional[str],
) -> Optional[str]:
    """Return the parent-master GUID currently stored on an operation (or None)."""
    if not op_id:
        return None
    row = _query_first(
        client,
        _ENTITY,
        f"{_id_field(_ENTITY)} eq {op_id}",
        _OP_PARENT_MASTER_VALUE_FIELD,
    )
    return row.get(_OP_PARENT_MASTER_VALUE_FIELD) if row else None


def _present_lookup_value(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _lookup_bind_from_payload(payload: Dict[str, Any], logical_key: str) -> Optional[str]:
    nav_key = _NAV_PROPERTY_MAP.get(logical_key, logical_key)
    for key in (
        _lookup_bind_key(nav_key),
        _lookup_bind_key(logical_key),
        f"{nav_key}@odata.bind",
        f"{logical_key}@odata.bind",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _payload_has_lookup_value(payload: Dict[str, Any], logical_key: str) -> bool:
    nav_key = _NAV_PROPERTY_MAP.get(logical_key, logical_key)
    return (
        _present_lookup_value(payload.get(logical_key))
        or _lookup_bind_from_payload(payload, logical_key) is not None
        or _present_lookup_value(payload.get(nav_key))
    )


def _get_operation_lookup_bind(
    client: DataverseClientService,
    op_id: Optional[str],
    logical_key: str,
) -> Optional[str]:
    """Read an operation lookup value and return a Dataverse bind string."""
    if not op_id:
        return None
    target_logical = _ENTITY_SCHEMAS[_ENTITY]["lookups"].get(logical_key)
    if not target_logical:
        return None
    value_field = f"_{logical_key}_value"
    row = _query_first(
        client,
        _ENTITY,
        f"{_id_field(_ENTITY)} eq {op_id}",
        value_field,
    )
    guid = row.get(value_field) if row else None
    if not guid:
        return None
    return f"/{_entity_set_name(target_logical)}({guid})"


def _inherit_lookup_bind(
    target: Dict[str, Any],
    logical_key: str,
    inherited_bind: Optional[str],
) -> bool:
    """Copy a parent lookup bind to target only when target has no value."""
    if not inherited_bind or _payload_has_lookup_value(target, logical_key):
        return False
    nav_key = _NAV_PROPERTY_MAP.get(logical_key, logical_key)
    target[_lookup_bind_key(nav_key)] = inherited_bind
    return True


def _find_existing_container(
    client: DataverseClientService,
    master_id: Optional[str],
    number: Optional[str],
) -> Optional[str]:
    if not number:
        return None
    safe = _odata_escape(str(number).strip())
    if not safe:
        return None
    id_field = _id_field(_CONTAINER_ENTITY)
    clauses = [f"mesco_containernumber eq '{safe}'"]
    if master_id:
        clauses.append(f"{_CONTAINER_MASTER_VALUE_FIELD} eq {master_id}")
    row = _query_first(client, _CONTAINER_ENTITY, " and ".join(clauses), id_field)
    return row.get(id_field) if row else None


def _norm_qty(value: Any, *, intish: bool = False) -> str:
    """Normalise a quantity (packages / weight / volume) to a stable string."""
    if value in (None, ""):
        return ""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return re.sub(r"\s+", "", str(value)).upper()
    return str(int(round(num))) if intish else f"{num:.3f}"


def _cargo_signature(
    desc: Any,
    packages: Any,
    gross: Any,
    volume: Any = None,
) -> str:
    """Identity of a cargo row for deduplication.

    Keyed on the QUANTITIES (packages + gross weight + volume) rather than the
    description text, because the same physical cargo is described differently
    across sources (Excel manifest text vs. B/L OCR text vs. an empty cell), and
    keying on description produced 2-3 duplicate rows for one cargo. Only when a
    row has no usable quantities at all do we fall back to the description.
    """
    pkg = _norm_qty(packages, intish=True)
    gw = _norm_qty(gross)
    vol = _norm_qty(volume)
    if pkg or gw or vol:
        return f"Q|{pkg}|{gw}|{vol}"
    norm_desc = re.sub(r"\s+", " ", str(desc or "").strip().upper())[:120]
    return f"D|{norm_desc}"


def _description_score(desc: Any) -> int:
    """Higher = more informative. Used to keep the best description on dedup."""
    return len(re.sub(r"\s+", " ", str(desc or "").strip()))


def _looks_like_msds_page_dump(desc: Any) -> bool:
    text = re.sub(r"\s+", " ", str(desc or "")).upper()
    if not text:
        return False
    markers = sum(
        1
        for marker in (
            "MATERIAL SAFETY DATA SHEET",
            "SAMPLE NAME",
            "COMPANY NAME",
            "COMPANY ADDRESS",
            "EBTEST REPORT",
            " SECTION ",
        )
        if marker in f" {text} "
    )
    return markers >= 2


def _should_patch_cargo_description(new_desc: Any, existing_desc: Any) -> bool:
    if not new_desc:
        return False
    if _looks_like_msds_page_dump(existing_desc) and not _looks_like_msds_page_dump(new_desc):
        return True
    return _description_score(new_desc) > _description_score(existing_desc)


def _existing_cargo_map(
    client: DataverseClientService,
    value_field: str,
    op_id: str,
) -> Dict[str, Dict[str, Any]]:
    """Map signature → {id, desc} for cargo already linked to *op_id*."""
    out: Dict[str, Dict[str, Any]] = {}
    for row in _list_existing_cargo_rows(client, value_field, op_id):
        sig = _cargo_signature(
            row.get("mesco_descriptionofgoods"),
            row.get("mesco_noofpackages"),
            row.get("mesco_grosskg"),
            row.get("mesco_volcbm"),
        )
        prev = out.get(sig)
        if prev is None or _description_score(
            row.get("mesco_descriptionofgoods")
        ) > _description_score(prev.get("desc")):
            id_field = _id_field(_CARGO_ENTITY)
            out[sig] = {
                "id": row.get(id_field),
                "desc": row.get("mesco_descriptionofgoods"),
            }
    return out


def _list_existing_cargo_rows(
    client: DataverseClientService,
    value_field: str,
    op_id: str,
) -> List[Dict[str, Any]]:
    """All cargo rows linked to an operation via *value_field*."""
    if not op_id:
        return []
    select = (
        f"{_id_field(_CARGO_ENTITY)},mesco_descriptionofgoods,"
        "mesco_noofpackages,mesco_grosskg,mesco_volcbm"
    )
    query = (
        f"{_CARGO_ENTITY}?$filter={value_field} eq {op_id}"
        f"&$select={select}&$top=500"
    )
    try:
        resp = client.get(query)
        data = resp.json() if resp.content else {}
        return list(data.get("value") or [])
    except Exception as exc:
        logger.warning("Existing-cargo query failed for %s: %s", op_id, exc)
        return []


def _cargo_qty_float(value: Any, *, field: str = "mesco_grosskg") -> Optional[float]:
    return _sanitize_cargo_quantity(value, field=field)


def _quantity_match_score(
    existing: Dict[str, Any],
    incoming: Dict[str, Any],
) -> int:
    """Higher = incoming quantities align with this cargo row."""
    score = 0
    for key in ("mesco_grosskg", "mesco_noofpackages", "mesco_volcbm"):
        a = _cargo_qty_float(existing.get(key))
        b = _cargo_qty_float(incoming.get(key))
        if a is None or b is None:
            continue
        if abs(a - b) <= 0.05:
            score += 4
        elif abs(a - b) <= max(abs(b) * 0.02, 1.0):
            score += 2
    return score


def _cargo_row_rank(row: Dict[str, Any]) -> Tuple[int, float]:
    """Sort key for picking the best keeper row (description only — not gross)."""
    return (_description_score(row.get("mesco_descriptionofgoods")),)


def _pick_cargo_keeper(
    rows: List[Dict[str, Any]],
    incoming: Dict[str, Any],
) -> Dict[str, Any]:
    """Choose the cargo row to keep; prefer quantities matching the upload."""
    return max(
        rows,
        key=lambda row: (
            _quantity_match_score(row, incoming),
            _description_score(row.get("mesco_descriptionofgoods")),
        ),
    )


def _delete_cargo_entities(
    client: DataverseClientService,
    cargo_ids: List[str],
) -> None:
    for cargo_id in cargo_ids:
        if not cargo_id:
            continue
        try:
            client.delete(f"{_CARGO_ENTITY}({cargo_id})")
        except Exception as exc:
            logger.warning("Cargo delete %s failed: %s", cargo_id, exc)


def _apply_cargo_package_unit_hint(cargo_clean: Dict[str, Any]) -> None:
    """Populate mesco_umpackages from cargo text before lookup preprocessing."""
    def _present(value: Any) -> bool:
        return value is not None and value != "" and value != [] and value != {}

    if not isinstance(cargo_clean, dict):
        return
    bind_keys = {
        _lookup_bind_key("mesco_UMPackages"),
        _lookup_bind_key("mesco_umpackages"),
        "mesco_UMPackages@odata.bind",
        "mesco_umpackages@odata.bind",
    }
    if _present(cargo_clean.get("mesco_umpackages")) or any(
        _present(cargo_clean.get(key)) for key in bind_keys
    ):
        return

    package_unit = infer_package_unit_label(
        cargo_clean.get("package_unit"),
        cargo_clean.get("u_m_packages"),
        cargo_clean.get("um_packages"),
        cargo_clean.get("cargo_type"),
        cargo_clean.get("cr401_totalpackages"),
        cargo_clean.get("mesco_noofpackages"),
        cargo_clean.get("mesco_cargodescription"),
        cargo_clean.get("mesco_descriptionofgoods"),
    )
    if package_unit:
        cargo_clean["mesco_umpackages"] = package_unit


def _cargo_fields_for_update(
    cargo_clean: Dict[str, Any],
    client: Optional[DataverseClientService] = None,
) -> Dict[str, Any]:
    """Fields safe to PATCH onto an existing cargo row.

    Existing cargo re-uploads must receive the same schema-aware mapping as new
    cargo creation.  Otherwise valid fields such as IMO/Chemical toggles, UN
    number, IMO class, reefer flags, product lookups, and lookup binds are
    extracted correctly but silently omitted from the Dataverse PATCH.
    """
    prepared = _preprocess_payload(cargo_clean, _CARGO_ENTITY, client)
    out: Dict[str, Any] = {}
    for key, value in prepared.items():
        if value is None:
            continue
        if key in {"mesco_noofpackages", "mesco_grosskg", "mesco_volcbm"}:
            safe = _sanitize_cargo_quantity(value, field=key)
            if safe is None:
                continue
            out[key] = int(safe) if safe.is_integer() else round(safe, 3)
            continue
        out[key] = value
    return out


def _upsert_house_linked_cargo(
    client: DataverseClientService,
    dedup_field: str,
    dedup_op_id: str,
    cargo_clean: Dict[str, Any],
    new_desc: Any,
) -> Optional[str]:
    """When cargo links to a house, update the existing row instead of duplicating.

    Re-uploading a manifest row or a standalone house PDF often changes gross
    weight or description while referring to the same physical cargo. Quantity-
    only signatures miss those updates and created duplicate rows per house.
    """
    if dedup_field != _CARGO_HOUSE_VALUE_FIELD or not dedup_op_id:
        return None
    rows = _list_existing_cargo_rows(client, dedup_field, dedup_op_id)
    if not rows:
        return None
    id_field = _id_field(_CARGO_ENTITY)
    keeper = _pick_cargo_keeper(rows, cargo_clean)
    keeper_id = keeper.get(id_field)
    if not keeper_id:
        return None
    patch = _cargo_fields_for_update(cargo_clean, client)
    if not _should_patch_cargo_description(new_desc, keeper.get("mesco_descriptionofgoods")):
        patch.pop("mesco_descriptionofgoods", None)
    if patch:
        _update_entity(client, _CARGO_ENTITY, keeper_id, patch)
    extras = [
        str(r.get(id_field))
        for r in rows
        if r.get(id_field) and r.get(id_field) != keeper_id
    ]
    if extras:
        logger.info(
            "  Removing %d extra cargo row(s) on house %s",
            len(extras),
            dedup_op_id,
        )
        _delete_cargo_entities(client, extras)
    return keeper_id


def _sum_cargo_quantities(rows: List[Dict[str, Any]]) -> Dict[str, float]:
    totals = {
        "cr401_totalpackages": 0.0,
        "cr401_totalgrossweight": 0.0,
        "cr401_totalvolume": 0.0,
    }
    field_map = {
        "mesco_noofpackages": "cr401_totalpackages",
        "mesco_grosskg": "cr401_totalgrossweight",
        "mesco_volcbm": "cr401_totalvolume",
    }
    for row in rows:
        for src, dst in field_map.items():
            val = _cargo_qty_float(row.get(src), field=src)
            if val is not None:
                totals[dst] = float(totals[dst]) + float(val)
    return totals


_OPERATION_TOTAL_FIELDS = (
    "cr401_totalpackages",
    "cr401_totalgrossweight",
    "cr401_totalvolume",
    "cr401_totalteus",
)


def _positive_total(value: Any, *, field: str) -> bool:
    parsed = _sanitize_cargo_quantity(value, field=field)
    return parsed is not None and parsed > 0


def _get_operation_totals(
    client: DataverseClientService,
    op_id: str,
) -> Dict[str, Any]:
    if not op_id:
        return {}
    row = _query_first(
        client,
        _ENTITY,
        f"{_id_field(_ENTITY)} eq {op_id}",
        ",".join(_OPERATION_TOTAL_FIELDS),
    )
    return row or {}


def _sync_operation_totals_from_cargo(
    client: DataverseClientService,
    op_id: str,
    *,
    is_house: bool,
    load_type: Optional[Any] = None,
    container_count: int = 0,
) -> None:
    """PATCH operation totals from linked cargo rows (matches Dynamics recalc)."""
    if not op_id:
        return
    cargo_field = _CARGO_HOUSE_VALUE_FIELD if is_house else _CARGO_MASTER_VALUE_FIELD
    rows = _list_existing_cargo_rows(client, cargo_field, op_id)
    if not rows:
        return
    totals = _sum_cargo_quantities(rows)
    existing_totals = {} if is_house else _get_operation_totals(client, op_id)
    patch: Dict[str, Any] = {}
    for key, value in totals.items():
        if (
            not is_house
            and key in {"cr401_totalpackages", "cr401_totalgrossweight", "cr401_totalvolume"}
            and _positive_total(existing_totals.get(key), field=key)
        ):
            continue
        safe = _sanitize_cargo_quantity(value, field=key)
        if safe is not None and safe > 0:
            patch[key] = int(safe) if safe.is_integer() else round(safe, 3)
    lcl_values = {300000001, "LCL", "lcl"}
    is_lcl = load_type in lcl_values or str(load_type or "").upper() == "LCL"
    if is_house and is_lcl:
        patch["cr401_totalteus"] = 0
    elif not is_house and container_count > 0 and is_lcl:
        patch["cr401_totalteus"] = container_count
    if patch:
        _update_entity(client, _ENTITY, op_id, patch)
        logger.info("  Synced %s totals on %s: %s", "house" if is_house else "master", op_id, patch)


def _repair_oversized_patch_fields(
    entity_set: str,
    fields: Dict[str, Any],
    error_body: str,
) -> Optional[Dict[str, Any]]:
    """Drop or truncate fields named in a Dataverse max-length validation error."""
    m = re.search(
        r"length of the '(\w+)' attribute",
        error_body,
        re.I,
    )
    if not m:
        return None
    bad_key = m.group(1)
    if bad_key not in fields:
        return None
    repaired = dict(fields)
    value = repaired[bad_key]
    if not isinstance(value, str):
        repaired.pop(bad_key, None)
        return repaired
    limit = _registry_limit_for(entity_set, bad_key)
    if limit and len(value) > limit:
        from dataverse_field_limits import cap_field

        repaired[bad_key] = cap_field(entity_set, bad_key, value)
    else:
        repaired.pop(bad_key, None)
    return repaired


def _update_entity(
    client: DataverseClientService,
    entity_set: str,
    guid: str,
    fields: Dict[str, Any],
) -> None:
    """PATCH an existing record (best-effort; logs but does not raise)."""
    if not fields:
        return
    from dataverse_field_limits import cap_record

    entity_key = entity_set if entity_set in {"mesco_operations", "mesco_cargos", "mesco_containers"} else "mesco_operations"
    payload = cap_record(entity_key, dict(fields))
    try:
        client.patch(f"{entity_set}({guid})", json=payload)
    except Exception as exc:
        body = ""
        if hasattr(exc, "response") and exc.response is not None:
            try:
                body = exc.response.text[:1000]
            except Exception:
                pass
        repaired = _repair_oversized_patch_fields(entity_set, payload, body) if body else None
        if repaired and repaired != payload:
            try:
                client.patch(f"{entity_set}({guid})", json=repaired)
                logger.info(
                    "Dataverse PATCH %s(%s) succeeded after trimming oversized fields",
                    entity_set,
                    guid,
                )
                return
            except Exception as retry_exc:
                logger.warning(
                    "Dataverse PATCH %s(%s) retry failed: %s",
                    entity_set,
                    guid,
                    retry_exc,
                )
        logger.warning(
            "Dataverse PATCH %s(%s) failed: %s%s",
            entity_set, guid, exc, f"\nResponse: {body}" if body else "",
        )


def _upsert_operation(
    client: DataverseClientService,
    fields: Dict[str, Any],
    *,
    bl_no: Optional[str],
    is_house: bool,
    master_id: Optional[str] = None,
    deduplicate: bool = True,
) -> Tuple[str, bool]:
    """Find-or-create an operation. Returns (guid, reused)."""
    if deduplicate:
        existing = _find_existing_operation(
            client, bl_no, is_house=is_house, master_id=master_id,
        )
        if existing:
            _update_entity(client, _ENTITY, existing, fields)
            return existing, True
    return _create_entity(client, _ENTITY, fields), False


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
    deduplicate: bool = True,
) -> UploadResult:
    """Upload the full CRM JSON (master + houses + containers + cargo) to Dataverse.

    When ``deduplicate`` is True (default) existing operations/containers/cargo
    (from a previous upload or manual Dataverse entry) are reused and updated
    instead of being created again — so houses always link to the correct
    master and no duplicate rows are produced.

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
    from custom_business_rules import prepare_crm_payload_for_upload

    crm_data = prepare_crm_payload_for_upload(crm_data)
    crm_data = _normalize_upload_payload(crm_data)
    client   = DataverseClientService.get_instance(retry_config or RetryConfig())

    # Strip OData metadata annotations and explicit nulls
    payload = _clean_odata_meta(crm_data)
    payload = _strip_null(payload)

    if is_house_bl_type(payload.get("mesco_bltype")):
        prepare_standalone_house_upload(payload)

    result: UploadResult = {
        "master_id":     None,
        "master_reused": False,
        "houses":        [],
        "containers":    [],
        "cargo":         [],
        "deduplicated":  bool(deduplicate),
        "skipped_cargo": 0,
    }

    # Extract nested arrays BEFORE building the master payload so they are not
    # passed as unknown fields to the master operation POST.
    houses: List[Dict]     = payload.pop("mesco_Operation_mesco_Operation_mesco_Operation", []) or []
    containers: List[Dict] = payload.pop("mesco_Container_MasterOperation_mesco_Operation", []) or []
    cargo_list: List[Dict] = payload.pop("mesco_Cargo_MasterOperation_mesco_Operation", []) or []
    house_cargo: List[Dict] = payload.pop("mesco_Cargo_HouseOperation_mesco_Operation", []) or []
    is_standalone_house = is_house_bl_type(payload.get("mesco_bltype")) and not houses
    if house_cargo:
        if is_standalone_house or is_house_bl_type(payload.get("mesco_bltype")):
            cargo_list = house_cargo
        elif not cargo_list:
            cargo_list = house_cargo
        else:
            cargo_list = cargo_list + house_cargo
    parent_master_bind = payload.get(_lookup_bind_key("mesco_Operation"))
    parent_master_id: Optional[str] = None
    if isinstance(parent_master_bind, str):
        parent_match = re.search(r"\(([^)]+)\)", parent_master_bind)
        if parent_match:
            parent_master_id = parent_match.group(1)

    # A standalone house must hang off a master operation. The house PDF rarely
    # carries an explicit parent GUID, so when none was supplied try to find the
    # master by its B/L number (mesco_masterbllinkno → an existing master with
    # mesco_masterblno equal to it). If found, inject the parent bind so the
    # house is linked instead of floating as a top-level (master-looking) row.
    if is_standalone_house and not parent_master_id and deduplicate:
        link_bl = payload.get("mesco_masterbllinkno")
        if link_bl:
            parent_master_id = _find_existing_operation(
                client, link_bl, is_house=False,
            )
            if parent_master_id:
                payload[_lookup_bind_key("mesco_Operation")] = (
                    f"/{_ENTITY}({parent_master_id})"
                )
                logger.info(
                    "Linked standalone house to master %s via M/BL %s",
                    parent_master_id, link_bl,
                )
        if not parent_master_id:
            parent_master_id = _find_master_by_shipment_evidence(
                client,
                payload,
                containers,
            )
            if parent_master_id:
                payload[_lookup_bind_key("mesco_Operation")] = (
                    f"/{_ENTITY}({parent_master_id})"
                )

    standalone_had_shippingline = _payload_has_lookup_value(payload, "mesco_shippingline")
    standalone_parent_shippingline_bind: Optional[str] = None
    if is_standalone_house and parent_master_id:
        standalone_parent_shippingline_bind = _get_operation_lookup_bind(
            client,
            parent_master_id,
            "mesco_shippingline",
        )
        if standalone_parent_shippingline_bind:
            _inherit_lookup_bind(
                payload,
                "mesco_shippingline",
                standalone_parent_shippingline_bind,
            )

    # ------------------------------------------------------------------
    # 1. Root operation (master or standalone house) — find-or-create
    # ------------------------------------------------------------------
    # The B/L number is persisted in mesco_masterblno for both master and
    # house operations (mesco_houseblno is only a transient extractor field).
    root_bl = payload.get("mesco_masterblno") or payload.get("mesco_houseblno")

    master_fields = _preprocess_payload(payload, _ENTITY, client)
    master_id, master_reused = _upsert_operation(
        client,
        master_fields,
        bl_no=root_bl,
        is_house=is_standalone_house,
        master_id=parent_master_id if is_standalone_house else None,
        deduplicate=deduplicate,
    )
    result["master_id"] = master_id
    result["master_reused"] = master_reused
    # For a standalone house that we did not explicitly link, the record may
    # already be linked to a master from a previous (e.g. Excel manifest) upload.
    # Read it back so the response reflects the real parent.
    if is_standalone_house and not parent_master_id:
        parent_master_id = _get_operation_parent(client, master_id)
        if parent_master_id and not standalone_had_shippingline:
            standalone_parent_shippingline_bind = _get_operation_lookup_bind(
                client,
                parent_master_id,
                "mesco_shippingline",
            )
            if standalone_parent_shippingline_bind:
                _update_entity(
                    client,
                    _ENTITY,
                    master_id,
                    {
                        _lookup_bind_key("mesco_ShippingLine"):
                            standalone_parent_shippingline_bind
                    },
                )
    # Clarity for callers: what kind of operation the root record is, and (for a
    # standalone house) which master it is linked to (None ⇒ unlinked).
    result["root_kind"] = "house" if is_standalone_house else "master"
    result["parent_master_id"] = parent_master_id if is_standalone_house else None
    logger.info(
        "%s %s operation (%s): %s%s",
        "Reused" if master_reused else "Created",
        "house" if is_standalone_house else "master",
        root_bl,
        master_id,
        f" → master {parent_master_id}" if is_standalone_house and parent_master_id else "",
    )

    master_shippingline_bind = (
        None
        if is_standalone_house
        else _lookup_bind_from_payload(master_fields, "mesco_shippingline")
    )
    if not is_standalone_house and not master_shippingline_bind:
        master_shippingline_bind = _get_operation_lookup_bind(
            client,
            master_id,
            "mesco_shippingline",
        )

    if not is_standalone_house and not houses:
        adopted_houses = _adopt_orphan_houses_for_master(
            client,
            master_id,
            root_bl,
            master_shippingline_bind,
        )
        if adopted_houses:
            result["houses"].extend(adopted_houses)
            logger.info(
                "Linked %d orphan house operation(s) to master %s",
                len(adopted_houses),
                master_id,
            )

    # Report the standalone house in the houses list too, so callers (and the
    # UI house counter) see it as a house rather than only a "master_id".
    if is_standalone_house:
        result["houses"].append({
            "index":  0,
            "id":     master_id,
            "hbl":    root_bl,
            "mbl":    payload.get("mesco_masterbllinkno"),
            "reused": master_reused,
            "root":   True,
            "linked_to_master": parent_master_id,
        })

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
        _inherit_lookup_bind(
            house_clean,
            "mesco_shippingline",
            master_shippingline_bind,
        )

        house_bl = house.get("mesco_masterblno") or house.get("mesco_houseblno")
        # mesco_houseblno is not a column on mesco_operations — drop it so the
        # POST/PATCH does not 400. The HBL is carried in mesco_masterblno.
        house_clean.pop("mesco_houseblno", None)
        house_fields = _preprocess_payload(house_clean, _ENTITY, client)
        house_id, house_reused = _upsert_operation(
            client,
            house_fields,
            bl_no=house_bl,
            is_house=True,
            master_id=master_id,
            deduplicate=deduplicate,
        )

        result["houses"].append({
            "index":  idx,
            "id":     house_id,
            "hbl":    house_bl,
            "mbl":    house.get("mesco_masterbllinkno"),
            "reused": house_reused,
        })
        logger.info(
            "  %s house [%d] hbl=%s mbl=%s → %s",
            "Reused" if house_reused else "Created",
            idx,
            house_bl,
            house.get("mesco_masterbllinkno"),
            house_id,
        )

    # ------------------------------------------------------------------
    # 3. Containers  (linked to master via mesco_MasterOperation)
    # ------------------------------------------------------------------
    for idx, container in enumerate(containers):
        container_clean = _clean_odata_meta(container)
        container_clean = _strip_null(container_clean)

        # Ensure the Container No lookup (mesco_containerno) is populated from the
        # container number, even when the extractor only produced the text field.
        if not container_clean.get("mesco_containerno"):
            ctn_no = (
                container_clean.get("mesco_containernumber")
                or container_clean.get("mesco_name")
            )
            if ctn_no:
                container_clean["mesco_containerno"] = ctn_no

        container_clean[_lookup_bind_key("mesco_MasterOperation")] = (
            f"/{_ENTITY}({master_id})"
        )

        container_no = (
            container.get("mesco_containernumber")
            or container.get("mesco_containerno")
            or container.get("mesco_name")
        )

        ctn_fields = _preprocess_payload(container_clean, _CONTAINER_ENTITY, client)
        existing_ctn = (
            _find_existing_container(client, master_id, container_no)
            if deduplicate else None
        )
        if existing_ctn:
            _update_entity(client, _CONTAINER_ENTITY, existing_ctn, ctn_fields)
            ctn_id = existing_ctn
            ctn_reused = True
        else:
            ctn_id = _create_entity(client, _CONTAINER_ENTITY, ctn_fields)
            ctn_reused = False

        result["containers"].append({
            "index":        idx,
            "id":           ctn_id,
            "container_no": container_no,
            "reused":       ctn_reused,
        })
        logger.info(
            "  %s container [%d] %s → %s",
            "Reused" if ctn_reused else "Created",
            idx, container_no, ctn_id,
        )

    # Standalone house: associate containers on the house operation (N:N).
    if is_standalone_house and result["containers"]:
        for ctn_info in result["containers"]:
            try:
                _link_container_to_house(client, master_id, ctn_info["id"])
            except Exception as exc:
                logger.warning(
                    "Container %s ↔ standalone house association failed: %s",
                    ctn_info.get("container_no"),
                    exc,
                )

    # Link each container to every house on a master operation (shared equipment).
    elif result["houses"] and result["containers"]:
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
    per_house_cargo_link = (
        multi_house
        and len(cargo_list) == len(result["houses"])
        and len(cargo_list) > 0
    )
    # Cargo dedup is scoped to the operation each row actually links to (the
    # house for per-house cargo, otherwise the master). Maps are loaded lazily
    # and cached so distinct houses with coincidentally equal quantities are not
    # merged together.
    cargo_maps: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = {}

    def _scope_map(value_field: str, op_id: str) -> Dict[str, Dict[str, Any]]:
        key = (value_field, op_id)
        if key not in cargo_maps:
            cargo_maps[key] = (
                _existing_cargo_map(client, value_field, op_id)
                if deduplicate else {}
            )
        return cargo_maps[key]

    houses_by_hbl = {
        str(h.get("hbl", "")).strip().upper(): h
        for h in result["houses"]
        if h.get("hbl")
    }

    for idx, cargo in enumerate(cargo_list):
        cargo_clean = _clean_odata_meta(cargo)
        cargo_clean = _strip_null(cargo_clean)
        _apply_cargo_package_unit_hint(cargo_clean)
        cargo_hbl = (
            cargo_clean.pop("_house_hbl", None)
            or cargo_clean.pop("mesco_houseblno", None)
        )
        if cargo_hbl:
            cargo_hbl = str(cargo_hbl).strip().upper()

        # Determine which operation this cargo is primarily attached to, so we
        # dedup within the right scope.
        dedup_field = _CARGO_MASTER_VALUE_FIELD
        dedup_op_id = master_id
        house_id: Optional[str] = None
        if is_standalone_house:
            cargo_clean[_lookup_bind_key("mesco_HouseOperation")] = (
                f"/{_ENTITY}({master_id})"
            )
            if parent_master_id:
                cargo_clean[_lookup_bind_key("mesco_MasterOperation")] = (
                    f"/{_ENTITY}({parent_master_id})"
                )
            dedup_field, dedup_op_id = _CARGO_HOUSE_VALUE_FIELD, master_id
            house_id = master_id
        else:
            cargo_clean[_lookup_bind_key("mesco_MasterOperation")] = (
                f"/{_ENTITY}({master_id})"
            )
            matched = houses_by_hbl.get(cargo_hbl) if cargo_hbl else None
            if matched:
                house_id = matched["id"]
            elif idx < len(result["houses"]) and (
                not multi_house or per_house_cargo_link
            ):
                house_id = result["houses"][idx]["id"]
            if house_id:
                cargo_clean[_lookup_bind_key("mesco_HouseOperation")] = (
                    f"/{_ENTITY}({house_id})"
                )
                dedup_field, dedup_op_id = _CARGO_HOUSE_VALUE_FIELD, house_id

        # Link to container (same equipment when one container serves many house cargo rows).
        if result["containers"]:
            ctn_idx = idx if idx < len(result["containers"]) else 0
            ctn_id = result["containers"][ctn_idx]["id"]
            cargo_clean[_lookup_bind_key("mesco_Conainter")] = (
                f"/{_CONTAINER_ENTITY}({ctn_id})"
            )

        new_desc = cargo_clean.get("mesco_descriptionofgoods")
        if deduplicate:
            reused_id = _upsert_house_linked_cargo(
                client,
                dedup_field,
                dedup_op_id,
                cargo_clean,
                new_desc,
            )
            if reused_id:
                result["skipped_cargo"] += 1
                result["cargo"].append({"index": idx, "id": reused_id, "reused": True})
                logger.info(
                    "  Updated existing house cargo [%d] → %s", idx, reused_id
                )
                continue

        sig = _cargo_signature(
            new_desc,
            cargo_clean.get("mesco_noofpackages"),
            cargo_clean.get("mesco_grosskg"),
            cargo_clean.get("mesco_volcbm"),
        )
        scope = _scope_map(dedup_field, dedup_op_id) if deduplicate else {}
        if deduplicate and sig in scope:
            existing = scope[sig]
            patch = _cargo_fields_for_update(cargo_clean, client)
            if not _should_patch_cargo_description(new_desc, existing.get("desc")):
                patch.pop("mesco_descriptionofgoods", None)
            if patch:
                _update_entity(client, _CARGO_ENTITY, existing["id"], patch)
                if "mesco_descriptionofgoods" in patch:
                    existing["desc"] = new_desc
                logger.info(
                    "  Updated duplicate cargo [%d] on %s", idx, existing["id"]
                )
            result["skipped_cargo"] += 1
            result["cargo"].append({"index": idx, "id": existing["id"], "reused": True})
            logger.info("  Skipped duplicate cargo [%d] (%s)", idx, sig[:40])
            continue

        cargo_fields = _preprocess_payload(cargo_clean, _CARGO_ENTITY, client)
        cargo_id     = _create_entity(client, _CARGO_ENTITY, cargo_fields)
        if deduplicate:
            scope[sig] = {"id": cargo_id, "desc": new_desc}

        result["cargo"].append({"index": idx, "id": cargo_id})
        logger.info("  Cargo [%d] → %s", idx, cargo_id)

    # ------------------------------------------------------------------
    # 5. Sync operation totals from cargo (fixes stale rollups after re-upload)
    # ------------------------------------------------------------------
    if not is_standalone_house:
        master_load = payload.get("mesco_loadtype")
        container_count = len(result.get("containers") or [])
        _sync_operation_totals_from_cargo(
            client,
            master_id,
            is_house=False,
            load_type=master_load,
            container_count=container_count,
        )
        for house_info in result.get("houses") or []:
            house_load = master_load
            _sync_operation_totals_from_cargo(
                client,
                house_info["id"],
                is_house=True,
                load_type=house_load,
            )
    else:
        _sync_operation_totals_from_cargo(
            client,
            master_id,
            is_house=True,
            load_type=payload.get("mesco_loadtype"),
        )

    return result
