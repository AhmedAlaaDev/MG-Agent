"""Live Dataverse metadata service.

Fetches the *choice / option-set (picklist) lists* that are stored in Dataverse
for the operation / container / cargo entities, so the uploader maps extracted
text (e.g. direction "IMPORT", container type "40HC", freight term "COLLECT")
to the correct option integer **using the values that actually exist in the
database** instead of hardcoded guesses.

Resolution order for the option-set definitions:
  1. In-process cache (per Python process).
  2. Live fetch from Dataverse metadata (EntityDefinitions .../Attributes).
  3. On-disk cache file (``dataverse_optionsets_cache.json``), refreshed on every
     successful live fetch.
  4. Bundled defaults (a snapshot of the live option sets) so the extractor still
     works offline / without Dataverse connectivity.

Lookup *reference rows* (accounts, ports, countries, ...) are resolved on demand
by ``dataverse_uploader._resolve_lookup`` — those are unbounded data lists and
are queried live per value rather than pre-loaded here.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# entity_set (OData) → entity logical name used by the metadata API
_ENTITY_SET_TO_LOGICAL: Dict[str, str] = {
    "mesco_operations": "mesco_operation",
    "mesco_containers": "mesco_container",
    "mesco_cargos": "mesco_cargo",
}

_CACHE_FILE = Path(__file__).with_name("dataverse_optionsets_cache.json")

# ---------------------------------------------------------------------------
# Bundled defaults — snapshot of the live option sets (offline fallback).
# Each field maps label → integer value exactly as defined in Dataverse.
# ---------------------------------------------------------------------------
_CONTAINER_TYPE_OPTIONS: Dict[str, int] = {
    "Standard / DC": 100000000, "High Cube": 100000001, "Reefer": 100000002,
    "Reefer High Cube": 886150002, "Flat Rack": 100000003, "Platform": 100000004,
    "Open Top": 100000005, "Tank": 886150001, "Mafi": 886150003, "Pallet Wide": 886150005,
}
_LOAD_TYPE_OPTIONS: Dict[str, int] = {
    "FCL": 300000000, "LCL": 300000001, "RORO": 886150001,
    "General Cargo": 886150002, "Bulk Cargo": 886150003, "Flexi": 886150004,
    "Tank": 886150005, "LCL logistics": 886150006, "FCL logistics": 886150007,
}
_PRODUCT_OPTIONS: Dict[str, int] = {"Flexi": 886150000, "Heater Pad": 886150001, "Iron": 886150002}

_BUNDLED_OPTION_SETS: Dict[str, Dict[str, Dict[str, int]]] = {
    "mesco_operation": {
        "mesco_bltype": {"Direct": 886150000, "House": 886150002, "Master": 886150001},
        "mesco_blstatus": {"Original": 886150000, "Telex": 886150001},
        "mesco_bookingterm": {"Freehand": 886150000, "Nomination": 886150001},
        "mesco_direction": {"Import": 300000000, "Export": 300000001, "Domestic": 300000002},
        "mesco_loadtype": dict(_LOAD_TYPE_OPTIONS),
        "mesco_pcfreightterm": {"Prepaid": 100000000, "Collect": 100000001},
        "mesco_transporttype": {
            "Sea": 300000000, "Truck": 300000001, "Air": 300000002,
            "Multimodal": 300000003, "Container Sale": 886150002, "Trading": 886150004,
            "Warehousing": 886150005, "Gensets": 886150006, "Custom Clearance": 886150007,
            "Projects, Chartering & Agency": 886150008,
        },
        "mesco_containertype": dict(_CONTAINER_TYPE_OPTIONS),
        "mesco_containertype2": dict(_CONTAINER_TYPE_OPTIONS),
        "mesco_containertype3": dict(_CONTAINER_TYPE_OPTIONS),
    },
    "mesco_container": {
        "mesco_containertype": dict(_CONTAINER_TYPE_OPTIONS),
        "mesco_product": dict(_PRODUCT_OPTIONS),
    },
    "mesco_cargo": {
        "mesco_loadtype": dict(_LOAD_TYPE_OPTIONS),
        "mesco_product": dict(_PRODUCT_OPTIONS),
    },
}

# Synonyms: normalized extracted token → canonical option label (per field).
# Only needed where the B/L term differs from the Dataverse label.
_OPTION_SYNONYMS: Dict[str, Dict[str, str]] = {
    "mesco_direction": {
        "IMP": "Import", "IMPORTS": "Import",
        "EXP": "Export", "EXPORTS": "Export",
        "CROSS": "Domestic", "LOCAL": "Domestic", "CROSSTRADE": "Domestic",
    },
    "mesco_loadtype": {
        "BULK": "Bulk Cargo", "BREAKBULK": "General Cargo", "GENERAL": "General Cargo",
        "BB": "General Cargo", "GC": "General Cargo",
    },
    "mesco_transporttype": {
        "OCEAN": "Sea", "VESSEL": "Sea", "ROAD": "Truck", "LAND": "Truck",
        "TRUCKING": "Truck", "AIRFREIGHT": "Air",
    },
    "mesco_bltype": {
        "MBL": "Master", "MASTERBL": "Master", "HBL": "House", "HOUSEBL": "House",
    },
    "mesco_pcfreightterm": {
        "PP": "Prepaid", "CC": "Collect", "FREIGHTPREPAID": "Prepaid",
        "FREIGHTCOLLECT": "Collect",
    },
    "mesco_blstatus": {
        "EXPRESS": "Telex", "SEAWAY": "Telex", "EXPRESSRELEASE": "Telex",
        "TELEXRELEASE": "Telex", "ORIGINALBL": "Original",
    },
}

_CONTAINER_TYPE_FIELDS = {"mesco_containertype", "mesco_containertype2", "mesco_containertype3"}

# Ordered (substring-in-compact-letters → canonical label) rules for ISO/short
# container type codes such as "40HC", "20GP", "40RF", "45RH", "20OT".
_CONTAINER_TYPE_RULES: List[Tuple[str, str]] = [
    ("REEFERHIGHCUBE", "Reefer High Cube"),
    ("RHC", "Reefer High Cube"),
    ("RH", "Reefer High Cube"),
    ("HIGHCUBE", "High Cube"),
    ("HC", "High Cube"),
    ("HQ", "High Cube"),
    ("REEFER", "Reefer"),
    ("RF", "Reefer"),
    ("RE", "Reefer"),
    ("RT", "Reefer"),
    ("FLATRACK", "Flat Rack"),
    ("FLAT", "Flat Rack"),
    ("FR", "Flat Rack"),
    ("OPENTOP", "Open Top"),
    ("OT", "Open Top"),
    ("PLATFORM", "Platform"),
    ("PALLETWIDE", "Pallet Wide"),
    ("PW", "Pallet Wide"),
    ("MAFI", "Mafi"),
    ("ISOTANK", "Tank"),
    ("TANK", "Tank"),
    ("TK", "Tank"),
    ("STANDARD", "Standard / DC"),
    ("DRY", "Standard / DC"),
    ("DC", "Standard / DC"),
    ("DV", "Standard / DC"),
    ("GP", "Standard / DC"),
    ("SD", "Standard / DC"),
]

# In-process cache: entity_logical → {field → {label: value}}
_OPTION_CACHE: Dict[str, Dict[str, Dict[str, int]]] = {}
_DISK_CACHE_LOADED = False


def _norm(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def _load_disk_cache() -> None:
    global _DISK_CACHE_LOADED
    if _DISK_CACHE_LOADED:
        return
    _DISK_CACHE_LOADED = True
    try:
        data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for entity, fields in data.items():
                if isinstance(fields, dict):
                    _OPTION_CACHE.setdefault(entity, {}).update(
                        {f: {str(k): int(v) for k, v in opts.items()}
                         for f, opts in fields.items() if isinstance(opts, dict)}
                    )
            logger.info("Loaded Dataverse option-set cache from %s", _CACHE_FILE.name)
    except (OSError, json.JSONDecodeError, ValueError):
        pass


def _save_disk_cache() -> None:
    try:
        _CACHE_FILE.write_text(
            json.dumps(_OPTION_CACHE, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:
        logger.debug("Could not write option-set cache: %s", exc)


def _fetch_live_option_sets(client, entity_logical: str) -> Dict[str, Dict[str, int]]:
    """Fetch every picklist option set (label → value) for *entity_logical*."""
    url = (
        f"EntityDefinitions(LogicalName='{entity_logical}')/Attributes/"
        "Microsoft.Dynamics.CRM.PicklistAttributeMetadata"
        "?$select=LogicalName"
        "&$expand=OptionSet($select=Options),GlobalOptionSet($select=Options)"
    )
    resp = client.get(url)
    data = resp.json() if resp.content else {}
    result: Dict[str, Dict[str, int]] = {}
    for attr in data.get("value", []) if isinstance(data, dict) else []:
        logical = attr.get("LogicalName")
        container = attr.get("OptionSet") or attr.get("GlobalOptionSet") or {}
        options: Dict[str, int] = {}
        for opt in container.get("Options", []) or []:
            label = ((opt.get("Label") or {}).get("UserLocalizedLabel") or {}).get("Label")
            value = opt.get("Value")
            if label is not None and value is not None:
                options[str(label).strip()] = int(value)
        if options:
            result[logical] = options
    return result


def get_option_sets(
    entity_logical: str,
    client=None,
    refresh: bool = False,
) -> Dict[str, Dict[str, int]]:
    """Return {field: {label: value}} for *entity_logical*.

    Tries cache, then a live fetch (if *client* given), then the disk cache,
    then bundled defaults.  Never raises — falls back gracefully.
    """
    _load_disk_cache()
    if not refresh and entity_logical in _OPTION_CACHE:
        return _OPTION_CACHE[entity_logical]

    if client is not None:
        try:
            live = _fetch_live_option_sets(client, entity_logical)
            if live:
                _OPTION_CACHE[entity_logical] = live
                _save_disk_cache()
                return live
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Live option-set fetch for %s failed (%s) — using cached/bundled values",
                entity_logical, exc,
            )

    if entity_logical in _OPTION_CACHE:
        return _OPTION_CACHE[entity_logical]

    bundled = _BUNDLED_OPTION_SETS.get(entity_logical, {})
    if bundled:
        _OPTION_CACHE[entity_logical] = {f: dict(v) for f, v in bundled.items()}
        return _OPTION_CACHE[entity_logical]
    return {}


def is_option_set_field(entity_set: str, field: str, client=None) -> bool:
    entity_logical = _ENTITY_SET_TO_LOGICAL.get(entity_set, entity_set)
    return field in get_option_sets(entity_logical, client)


def _match_container_type(value: str, label_to_value: Dict[str, int]) -> Optional[int]:
    norm_label_map = {_norm(label): val for label, val in label_to_value.items()}
    compact = re.sub(r"[^A-Z]", "", str(value).upper())
    if not compact:
        # Plain size like "20" / "40" / "20'" → assume Standard dry container.
        if re.search(r"\d", str(value)):
            return norm_label_map.get(_norm("Standard / DC"))
        return None
    for token, label in _CONTAINER_TYPE_RULES:
        if token in compact:
            mapped = norm_label_map.get(_norm(label))
            if mapped is not None:
                return mapped
    # Has a size but unrecognised type letters → default to Standard / DC.
    if re.search(r"\d", str(value)):
        return norm_label_map.get(_norm("Standard / DC"))
    return None


def resolve_option_value(
    entity_set: str,
    field: str,
    value: Any,
    client=None,
) -> Optional[int]:
    """Map *value* to the correct option-set integer for *field*.

    Returns None when *field* is not a picklist or no confident match exists
    (the caller then drops the field rather than sending an invalid value).
    """
    entity_logical = _ENTITY_SET_TO_LOGICAL.get(entity_set, entity_set)
    option_sets = get_option_sets(entity_logical, client)
    label_to_value = option_sets.get(field)
    if not label_to_value:
        return None

    valid_values = set(label_to_value.values())

    # Already an integer (or integer string) that is a valid option → keep it.
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value in valid_values else None
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        as_int = int(value.strip())
        if as_int in valid_values:
            return as_int

    text = str(value).strip()
    if not text:
        return None

    norm_label_map = {_norm(label): val for label, val in label_to_value.items()}

    # Container-type ISO/short-code handling.
    if field in _CONTAINER_TYPE_FIELDS:
        matched = _match_container_type(text, label_to_value)
        if matched is not None:
            return matched

    # Synonym → canonical label.
    syn = _OPTION_SYNONYMS.get(field, {})
    canonical = syn.get(_norm(text))
    if canonical and _norm(canonical) in norm_label_map:
        return norm_label_map[_norm(canonical)]

    # Exact (normalized) label match.
    if _norm(text) in norm_label_map:
        return norm_label_map[_norm(text)]

    # Startswith / contains fallback on labels.
    nt = _norm(text)
    for nlabel, val in norm_label_map.items():
        if nlabel and (nlabel.startswith(nt) or nt.startswith(nlabel)):
            return val
    return None


def clear_metadata_cache() -> None:
    """Reset in-process metadata caches (useful between tests/batches)."""
    global _DISK_CACHE_LOADED
    _OPTION_CACHE.clear()
    _DISK_CACHE_LOADED = False
