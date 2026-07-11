"""Toggleable post-extraction business rules for Dynamics CRM operations.

Rules (when enabled):
  * P/C Freight Prepaid  → Booking Term Freehand, Freight Payable At Origin
  * P/C Freight Collect  → Booking Term Nomination, Freight Payable At Destination
  * Load type FCL vs LCL from document meaning (consolidation, CFS, manifest, containers)
  * LCL house operations: cr401_totalteus = 0
  * Multi-house master: totals = sum of house rows (not double-counted container fallbacks)
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from typing import Any, Dict, List, Optional

from config import settings

LCL_LOAD_TYPE = 300000001
FCL_LOAD_TYPE = 300000000
HOUSE_BL_TYPE = 886150002
BOOKING_FREEHAND = "Freehand"
BOOKING_NOMINATION = "Nomination"
FREIGHT_AT_ORIGIN = "Origin"
FREIGHT_AT_DESTINATION = "Destination"
BOOKING_FREEHAND_OPT = 886150000
BOOKING_NOMINATION_OPT = 886150001
PREPAID_OPT = 100000000
COLLECT_OPT = 100000001

# Nested arrays on master CRM payloads (same keys as crm_output_formatter).
NESTED_HOUSES_KEY = "mesco_Operation_mesco_Operation_mesco_Operation"
NESTED_CONTAINERS_KEY = "mesco_Container_MasterOperation_mesco_Operation"
NESTED_CARGO_KEY = "mesco_Cargo_MasterOperation_mesco_Operation"
NESTED_HOUSE_CARGO_KEY = "mesco_Cargo_HouseOperation_mesco_Operation"

_COMMERCIAL_INHERIT_KEYS = (
    "mesco_pcfreightterm",
    "mesco_direction",
    "mesco_transporttype",
    "mesco_incoterm",
    "mesco_loadtype",
)

_RULES_OVERRIDE: ContextVar[Optional[bool]] = ContextVar(
    "custom_business_rules_enabled", default=None
)


def custom_rules_enabled(override: Optional[bool] = None) -> bool:
    if override is not None:
        return bool(override)
    ctx = _RULES_OVERRIDE.get()
    if ctx is not None:
        return ctx
    return bool(settings.custom_business_rules_enabled)


class _CustomRulesContext:
    def __init__(self, enabled: bool):
        self._enabled = enabled
        self._token = None

    def __enter__(self):
        self._token = _RULES_OVERRIDE.set(self._enabled)
        return self

    def __exit__(self, *args):
        if self._token is not None:
            _RULES_OVERRIDE.reset(self._token)


def use_custom_rules(enabled: bool) -> _CustomRulesContext:
    return _CustomRulesContext(enabled)


def _has(val: Any) -> bool:
    return val is not None and val != "" and val != [] and val != {}


def _normalize_freight_term(value: Any) -> Optional[str]:
    """Return canonical 'Prepaid' or 'Collect'."""
    if value is None or value == "":
        return None
    if isinstance(value, int):
        if value == 100000000:
            return "Prepaid"
        if value == 100000001:
            return "Collect"
    text = re.sub(r"[^A-Z]", "", str(value).upper())
    if text in {"PP", "PREPAID", "FREIGHTPREPAID"}:
        return "Prepaid"
    if text in {"CC", "COLLECT", "FREIGHTCOLLECT"}:
        return "Collect"
    if "PREPAID" in text:
        return "Prepaid"
    if "COLLECT" in text:
        return "Collect"
    return None


def _load_type_is_lcl(value: Any) -> bool:
    if value == LCL_LOAD_TYPE:
        return True
    if isinstance(value, str) and "LCL" in value.upper():
        return True
    return False


def apply_freight_booking_rules(data: Dict[str, Any]) -> Dict[str, Any]:
    """Derive booking term and freight payable at from P/C freight term."""
    term = _normalize_freight_term(data.get("mesco_pcfreightterm"))
    if term == "Prepaid":
        data["mesco_bookingterm"] = BOOKING_FREEHAND_OPT
        data["mesco_freightpayableat"] = FREIGHT_AT_ORIGIN
        if not _has(data.get("mesco_pcfreightterm")):
            data["mesco_pcfreightterm"] = PREPAID_OPT
    elif term == "Collect":
        data["mesco_bookingterm"] = BOOKING_NOMINATION_OPT
        data["mesco_freightpayableat"] = FREIGHT_AT_DESTINATION
        if not _has(data.get("mesco_pcfreightterm")):
            data["mesco_pcfreightterm"] = COLLECT_OPT
    return data


def _inherit_master_commercial_fields(
    master: Dict[str, Any],
    house: Dict[str, Any],
) -> None:
    """Copy master freight / direction / load type onto house rows when missing."""
    if not isinstance(master, dict) or not isinstance(house, dict):
        return
    for key in _COMMERCIAL_INHERIT_KEYS:
        if not _has(house.get(key)) and _has(master.get(key)):
            house[key] = master[key]


def infer_load_type(data: Dict[str, Any], raw_text: str = "") -> Optional[int]:
    """Infer FCL vs LCL from extraction fields and document text."""
    existing = data.get("mesco_loadtype")
    if existing == LCL_LOAD_TYPE or existing == FCL_LOAD_TYPE:
        return int(existing)
    if isinstance(existing, str):
        u = existing.upper()
        if "LCL" in u:
            return LCL_LOAD_TYPE
        if "FCL" in u:
            return FCL_LOAD_TYPE

    upper = (raw_text or "").upper()
    desc = str(data.get("mesco_cargodescription") or "").upper()
    blob = f"{upper}\n{desc}"
    method = str(data.get("extraction_method") or "").lower()

    lcl_markers = (
        "LCL",
        "CFS-CFS",
        " CFS ",
        "CFS TERMINAL",
        "CONSOLIDATED LCL",
        "CONSOLIDATION",
        "LESS THAN CONTAINER",
        "GROUPAGE",
        "CARGO MANIFEST",
        "CONSOLIDATED CARGO",
    )
    if data.get("mesco_consolidation") is True:
        return LCL_LOAD_TYPE
    if data.get("_per_house_cargo") or data.get("_manifest_pdf_row"):
        return LCL_LOAD_TYPE
    if "cargo_manifest" in method or "consolidated_lcl" in method:
        return LCL_LOAD_TYPE
    if _has(data.get("mesco_houseblno")) and _has(data.get("mesco_masterblno")):
        mbl = str(data["mesco_masterblno"]).strip().upper()
        hbl = str(data["mesco_houseblno"]).strip().upper()
        if mbl and hbl and mbl != hbl:
            return LCL_LOAD_TYPE
    if any(m in blob for m in lcl_markers):
        return LCL_LOAD_TYPE

    containers = [
        c for c in (data.get("containers") or []) if isinstance(c, dict)
    ]
    if "FCL" in blob or "FULL CONTAINER LOAD" in blob:
        return FCL_LOAD_TYPE
    if containers and not _has(data.get("mesco_houseblno")):
        if "LCL" not in blob and "CFS" not in blob:
            return FCL_LOAD_TYPE

    return None


def apply_load_type_rules(data: Dict[str, Any], raw_text: str = "") -> Dict[str, Any]:
    inferred = infer_load_type(data, raw_text)
    if inferred is not None:
        data["mesco_loadtype"] = inferred
    return data


def apply_teus_rules(data: Dict[str, Any], *, is_house: bool = False) -> Dict[str, Any]:
    """LCL house operations carry zero TEUs; master keeps equipment count."""
    if is_house and _load_type_is_lcl(data.get("mesco_loadtype")):
        data["cr401_totalteus"] = 0
    return data


def apply_record_business_rules(
    data: Dict[str, Any],
    raw_text: str = "",
    *,
    is_house: bool = False,
    enabled: Optional[bool] = None,
) -> Dict[str, Any]:
    if not custom_rules_enabled(enabled):
        return data
    apply_load_type_rules(data, raw_text)
    apply_freight_booking_rules(data)
    apply_teus_rules(data, is_house=is_house)
    return data


_MAX_GROSS_KG = 500_000.0
_MAX_PACKAGES = 1_000_000.0
_MAX_CBM = 100_000.0


def _parse_numeric(value: Any, *, field: str = "") -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = float(value)
    else:
        text = str(value).replace(",", "").strip()
        if not text:
            return None
        m = re.search(r"-?\d+(?:\.\d+)?", text)
        if not m:
            return None
        num = float(m.group(0))
    if field in ("cr401_totalgrossweight",):
        cap = _MAX_GROSS_KG
    elif field in ("cr401_totalpackages",):
        cap = _MAX_PACKAGES
    elif field in ("cr401_totalvolume",):
        cap = _MAX_CBM
    else:
        cap = 100_000_000_000.0
    if num < 0 or num > cap:
        return None
    return num


def sum_house_operation_totals(house_records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Sum packages / gross / volume across distinct house rows."""
    totals: Dict[str, float] = {}
    field_map = {
        "cr401_totalpackages": "cr401_totalpackages",
        "cr401_totalgrossweight": "cr401_totalgrossweight",
        "cr401_totalvolume": "cr401_totalvolume",
    }
    for rec in house_records:
        if not isinstance(rec, dict):
            continue
        for out_key, rec_key in field_map.items():
            parsed = _parse_numeric(rec.get(rec_key), field=out_key)
            if parsed is not None:
                totals[out_key] = float(totals.get(out_key, 0.0)) + parsed
    result: Dict[str, Any] = {}
    for key, value in totals.items():
        result[key] = int(value) if value.is_integer() else round(value, 3)
    return result


def reconcile_master_totals_from_houses(
    master: Dict[str, Any],
    house_records: List[Dict[str, Any]],
    *,
    master_record: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Master multi-house totals should equal the sum of house rows."""
    if len(house_records) < 2:
        return master

    house_sum = sum_house_operation_totals(house_records)
    if not house_sum:
        return master

    manifest_totals: Dict[str, Any] = {}
    if master_record:
        for key in house_sum:
            if _has(master_record.get(key)):
                manifest_totals[key] = master_record[key]

    for key, summed in house_sum.items():
        master[key] = summed

    # When manifest footer totals exist and differ materially, prefer house sum
    # (per-HBL rows are authoritative; footer catches OCR drift).
    if not _has(master.get("mesco_nooforgbls")):
        master["mesco_nooforgbls"] = str(len(house_records))

    return master


def apply_crm_operation_rules(
    operation: Dict[str, Any],
    *,
    is_house: bool = False,
    raw_text: str = "",
    enabled: Optional[bool] = None,
) -> Dict[str, Any]:
    if not custom_rules_enabled(enabled):
        return operation
    apply_record_business_rules(operation, raw_text, is_house=is_house, enabled=True)
    return operation


def _nested_houses(crm_output: Dict[str, Any]) -> List[Dict[str, Any]]:
    nested = crm_output.get(NESTED_HOUSES_KEY) or []
    return [h for h in nested if isinstance(h, dict)]


def _is_house_operation(data: Dict[str, Any]) -> bool:
    return str(data.get("mesco_bltype") or "") == str(HOUSE_BL_TYPE)


def apply_crm_payload_rules(
    crm_output: Dict[str, Any],
    *,
    house_records: Optional[List[Dict[str, Any]]] = None,
    raw_text: str = "",
    enabled: Optional[bool] = None,
) -> Dict[str, Any]:
    """Apply business rules to a master CRM payload and nested houses."""
    if not isinstance(crm_output, dict) or not crm_output:
        return crm_output
    if not custom_rules_enabled(enabled):
        return crm_output

    nested = _nested_houses(crm_output)
    is_standalone_house = not nested and _is_house_operation(crm_output)

    if house_records and len(house_records) > 1:
        reconcile_master_totals_from_houses(
            crm_output, house_records, master_record=crm_output
        )
    elif len(nested) > 1:
        reconcile_master_totals_from_houses(crm_output, nested, master_record=crm_output)

    if is_standalone_house:
        apply_crm_operation_rules(
            crm_output, is_house=True, raw_text=raw_text, enabled=True
        )
        return crm_output

    apply_crm_operation_rules(crm_output, is_house=False, raw_text=raw_text, enabled=True)

    for house in nested:
        _inherit_master_commercial_fields(crm_output, house)
        apply_crm_operation_rules(house, is_house=True, raw_text=raw_text, enabled=True)

    return crm_output


def prepare_crm_payload_for_upload(
    crm_data: Dict[str, Any],
    *,
    raw_text: str = "",
    enabled: Optional[bool] = None,
) -> Dict[str, Any]:
    """Apply toggleable business rules immediately before Dataverse POST/PATCH.

    Used by ``upload_crm_json`` and the ``/upload/dataverse*`` endpoints so
    re-uploads refresh Booking Term, Freight Payable At, load type, TEUs, and
    master totals even when the JSON was saved before rules existed.
    """
    if not isinstance(crm_data, dict) or not crm_data:
        return crm_data
    if not custom_rules_enabled(enabled):
        return crm_data

    nested = _nested_houses(crm_data)
    if nested:
        apply_crm_payload_rules(crm_data, raw_text=raw_text, enabled=True)
    else:
        from crm_output_formatter import is_house_bl_type

        apply_crm_operation_rules(
            crm_data,
            is_house=is_house_bl_type(crm_data.get("mesco_bltype")),
            raw_text=raw_text,
            enabled=True,
        )
    return crm_data
