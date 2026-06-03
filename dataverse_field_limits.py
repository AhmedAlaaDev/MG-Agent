"""
Single source of truth for Dataverse string-column length caps.

Centralises every text-field maximum used across the project so they are
enforced **once**, just before the payload is sent to Dataverse, instead of
being scattered across multiple modules. Every text field that can blow the
Dataverse 0x80048d19 / 400 errors lives here.

Limits are conservative — set to the Dataverse column max-length minus a small
safety margin for OData annotations.  Sources:
  * Direct Dataverse error messages observed during uploads (cargo description
    1500, party names 100, etc.).
  * ``index.ts`` schema (length constraints per attribute).
  * ``dataverse_uploader._SCHEMA["mesco_operations"]["max_length"]`` legacy
    table.

Adding a new field here automatically clamps it everywhere — in the CRM JSON
formatter, in the validator, and in the upload pre-processor.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Per-entity field caps
# ---------------------------------------------------------------------------

#: Master / house operations (entity: ``mesco_operations``).
OPERATION_LIMITS: Dict[str, int] = {
    # Free-text / descriptions
    "mesco_cargodescription":         1500,
    "mesco_notes":                    2000,
    "mesco_routenotes":               2000,
    "mesco_handlinginformation":      2000,
    "mesco_specialinstructions":      2000,
    "mesco_remarks":                  2000,

    # Party names + addresses (Dataverse: 100)
    "mesco_shippernamecontactno":     100,
    "mesco_shipperaddress":           250,
    "mesco_consigneenamecontactno":   100,
    "mesco_consigneeaddress":         250,
    "mesco_notify1":                  100,
    "mesco_notify2":                  100,
    "mesco_notifyaddress":            100,

    # B/L identifiers
    "mesco_masterblno":               100,
    "mesco_houseblno":                100,
    "mesco_bookingnumber":            100,
    "mesco_certificatenumber":        100,
    "mesco_acidnumber":                30,
    "mesco_customerreference":        100,
    "mesco_ponumber":                 100,

    # Routing
    "mesco_freightpayableat":         100,
    "mesco_placeofissue":             100,
    "mesco_deliveryaddress":          250,
    "mesco_pickupaddress":            250,

    # Transport
    "mesco_vessel":                   100,
    "mesco_voytruckno":                50,
    "mesco_shippingline":             200,

    # Cargo classifiers
    "mesco_hscode":                   250,
    "mesco_imoclass":                  50,
    "mesco_unnumber":                  20,

    # Other
    "mesco_pcfreightterm":             50,
    "mesco_servicetype_text":         100,
    "mesco_nooforgbls":                10,
    "cr401_totalpackages":             50,
}

#: Cargo line entity (``mesco_cargos``).
CARGO_LIMITS: Dict[str, int] = {
    "mesco_descriptionofgoods": 1500,
    "mesco_remarks":            2000,
    "mesco_markings":           2000,
    "mesco_hscode":              250,
}

#: Container entity (``mesco_containers``).
CONTAINER_LIMITS: Dict[str, int] = {
    "mesco_containernumber": 50,
    "mesco_carrierseal":     50,
    "mesco_shipperseal":     50,
    "mesco_marksandnumbers": 2000,
}


_ENTITY_LIMITS: Dict[str, Dict[str, int]] = {
    "mesco_operations": OPERATION_LIMITS,
    "mesco_cargos":     CARGO_LIMITS,
    "mesco_containers": CONTAINER_LIMITS,
}


# ---------------------------------------------------------------------------
# Truncation helpers
# ---------------------------------------------------------------------------

def _smart_truncate(value: str, limit: int) -> str:
    """Cut at a line / sentence boundary inside ``limit`` to keep meaning."""
    if len(value) <= limit:
        return value
    snippet = value[:limit]
    for sep in ("\n", ". ", "; ", ", "):
        cut = snippet.rfind(sep)
        if cut >= int(limit * 0.6):
            return snippet[:cut].rstrip(" .,;\n")
    return snippet.rstrip()


def cap_field(entity: str, field: str, value: Any) -> Any:
    """Return ``value`` truncated to its registered Dataverse limit (if any)."""
    if not isinstance(value, str):
        return value
    limit = _ENTITY_LIMITS.get(entity, {}).get(field)
    if not limit or len(value) <= limit:
        return value
    return _smart_truncate(value, limit)


def cap_record(entity: str, record: Dict[str, Any]) -> Dict[str, Any]:
    """Clamp every known text field on a record in place; returns the record."""
    limits = _ENTITY_LIMITS.get(entity)
    if not limits:
        return record
    for field, limit in limits.items():
        val = record.get(field)
        if isinstance(val, str) and len(val) > limit:
            record[field] = _smart_truncate(val, limit)
    return record


def cap_nested_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Walk a CRM master payload (``records_to_master_json`` shape) and clamp
    every text field on the master, nested houses, containers, and cargos.

    Returns the same dict (mutated).
    """
    cap_record("mesco_operations", payload)

    for nested_key, entity in (
        ("mesco_Operation_mesco_Operation_mesco_Operation", "mesco_operations"),
        ("mesco_Container_MasterOperation_mesco_Operation", "mesco_containers"),
        ("mesco_Cargo_MasterOperation_mesco_Operation",     "mesco_cargos"),
    ):
        nested = payload.get(nested_key)
        if isinstance(nested, list):
            for child in nested:
                if isinstance(child, dict):
                    cap_record(entity, child)

    # House operations carry their own nested cargo collections.
    houses = payload.get("mesco_Operation_mesco_Operation_mesco_Operation") or []
    for house in houses:
        if not isinstance(house, dict):
            continue
        for nested_key, entity in (
            ("mesco_Container_mesco_houses",            "mesco_containers"),
            ("mesco_Cargo_HouseOperation_mesco_Operation", "mesco_cargos"),
        ):
            nested = house.get(nested_key)
            if isinstance(nested, list):
                for child in nested:
                    if isinstance(child, dict):
                        cap_record(entity, child)

    return payload


def fields_exceeding_limits(entity: str, record: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Diagnostic — return a list of fields that would be truncated."""
    out: List[Dict[str, Any]] = []
    for field, limit in _ENTITY_LIMITS.get(entity, {}).items():
        val = record.get(field)
        if isinstance(val, str) and len(val) > limit:
            out.append(
                {
                    "field": field,
                    "limit": limit,
                    "actual": len(val),
                    "overflow": len(val) - limit,
                }
            )
    return out


def registered_entities() -> List[str]:
    return list(_ENTITY_LIMITS.keys())


def limit_for(entity: str, field: str) -> Optional[int]:
    return _ENTITY_LIMITS.get(entity, {}).get(field)
