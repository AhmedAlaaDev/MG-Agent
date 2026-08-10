"""Deterministic Dynamics mapping for extracted multi-HBL invoices.

This module deliberately avoids fuzzy/"first row" lookup fallbacks.  A value
is posted only when it resolves to one unique Dataverse row, either by an exact
normalized label or by an explicit business alias documented below.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple


MASTER_BL = 886150001
HOUSE_BL = 886150002
IMPORT = 300000000
SEA = 300000000
LCL = 300000001

CONTAINER_STANDARD_DC = 100000000
CONTAINER_HIGH_CUBE = 100000001
CONTAINER_REEFER = 100000002
CONTAINER_FLAT_RACK = 100000003
CONTAINER_PLATFORM = 100000004
CONTAINER_OPEN_TOP = 100000005
CONTAINER_TANK = 886150001
CONTAINER_REEFER_HIGH_CUBE = 886150002


REFERENCE_QUERIES: Dict[str, str] = {
    "currencies": (
        "transactioncurrencies?"
        "$select=transactioncurrencyid,isocurrencycode,currencyname,exchangerate&$top=5000"
    ),
    "shipping_lines": (
        "mesco_shippinglines?"
        "$select=mesco_shippinglineid,mesco_name&$top=5000"
    ),
    "addresses": (
        "xollsp_addresses?"
        "$select=xollsp_addressid,xollsp_name&$top=5000"
    ),
    "vessels": (
        "mesco_vesselsmises?"
        "$select=mesco_vesselsmisid,mesco_name&$top=5000"
    ),
    "incoterms": (
        "xollsp_incoterms?"
        "$select=xollsp_incotermid,xollsp_name&$top=5000"
    ),
    "services": (
        "xollsp_servicedefinitions?"
        "$select=xollsp_servicedefinitionid,xollsp_name&$top=5000"
    ),
}


# The aliases map source-document language to one exact CRM label.  Keeping
# this explicit makes changes reviewable and prevents a partial match from
# silently selecting a different port, vendor, currency, or service.
LOOKUP_ALIASES: Dict[str, Dict[str, str]] = {
    "destination": {
        "ALEXANDRIA": "Alexandria, Egypt (old)",
        "PORTSAID": "Port Said",
    },
    "shipping_line": {
        "COSCO": "COSCO SHIPPING LINE",
        "COSCOSHIPPING": "COSCO SHIPPING LINE",
        "COSCOSHIPPINGLINES": "COSCO SHIPPING LINE",
    },
    "invoice_vendor": {
        "WECANINTERNATIONALLOGISTICS": "We-Can International Logistics",
        "WECANINTERNATIONALLOGISTICSCO": "We-Can International Logistics",
        "WECANINTERNATIONALLOGISTICSCOLTD": "We-Can International Logistics",
        "SHANGHAIWECANINTERNATIONALLOGISTICSCOLTD": "We-Can International Logistics",
    },
    "service": {
        "ADMINFEES": "Admin Fees",
        "AGREEMENTREBATE": "Rebate",
        "CREDITTS": "Transfer Charges",
        "EXWORK": "Sea Freight + Exwork",
        "FOBOCEANFREIGHT": "Sea Freight",
        "LOADINGUNLOADING": "Loading Charges",
        "PCS": "PCS Port Congestion Surcharges",
        "THC": "THC",
        "LSS": "LSS - Low Sulphur Surcharge",
    },
}


def normalize_label(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def container_type_option(value: Any) -> Optional[int]:
    text = normalize_label(value)
    if not text:
        return None
    if "REEFER" in text or "RF" in text:
        if "HC" in text or "HQ" in text or "HIGHCUBE" in text:
            return CONTAINER_REEFER_HIGH_CUBE
        return CONTAINER_REEFER
    if "FLATRACK" in text or "FR" in text:
        return CONTAINER_FLAT_RACK
    if "OPENTOP" in text or "OT" in text:
        return CONTAINER_OPEN_TOP
    if "PLATFORM" in text:
        return CONTAINER_PLATFORM
    if "TANK" in text:
        return CONTAINER_TANK
    if "HC" in text or "HQ" in text or "HIGHCUBE" in text:
        return CONTAINER_HIGH_CUBE
    return CONTAINER_STANDARD_DC


def _safe_rows(response: Any) -> List[Dict[str, Any]]:
    data = response.json()
    rows = data.get("value", []) if isinstance(data, dict) else []
    return rows if isinstance(rows, list) else []


def fetch_invoice_reference_data(client: Any) -> Tuple[Dict[str, List[Dict[str, Any]]], List[str]]:
    references: Dict[str, List[Dict[str, Any]]] = {}
    errors: List[str] = []
    for key, query in REFERENCE_QUERIES.items():
        try:
            references[key] = _safe_rows(client.get(query))
        except Exception as exc:
            references[key] = []
            errors.append(f"Could not read Dynamics {key}: {exc}")
    return references, errors


def _unique_rows(rows: Iterable[Dict[str, Any]], id_key: str) -> List[Dict[str, Any]]:
    unique: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        row_id = str(row.get(id_key) or "").strip()
        if row_id:
            unique[row_id] = row
    return list(unique.values())


def resolve_unique_lookup(
    source_value: Any,
    rows: List[Dict[str, Any]],
    *,
    kind: str,
    id_key: str,
    label_keys: Iterable[str],
    required: bool = True,
) -> Dict[str, Any]:
    source = str(source_value or "").strip()
    source_key = normalize_label(source)
    target = LOOKUP_ALIASES.get(kind, {}).get(source_key, source)
    target_key = normalize_label(target)

    result: Dict[str, Any] = {
        "source": source or None,
        "target_label": target or None,
        "required": required,
        "status": "missing" if not source_key else "unresolved",
        "id": None,
        "label": None,
    }
    if not source_key:
        return result

    matches: List[Dict[str, Any]] = []
    for row in rows:
        if any(normalize_label(row.get(key)) == target_key for key in label_keys):
            matches.append(row)
    matches = _unique_rows(matches, id_key)

    if len(matches) == 1:
        row = matches[0]
        result["status"] = "resolved"
        result["id"] = row.get(id_key)
        result["label"] = next(
            (row.get(key) for key in label_keys if row.get(key) not in (None, "")),
            target,
        )
        if "exchangerate" in row:
            result["exchange_rate"] = float(row.get("exchangerate") or 1.0)
    elif len(matches) > 1:
        result["status"] = "ambiguous"
        result["matches"] = [
            {
                "id": row.get(id_key),
                "label": next((row.get(k) for k in label_keys if row.get(k)), None),
            }
            for row in matches
        ]
    return result


def _bind(fields: Dict[str, Any], nav: str, entity_set: str, resolution: Dict[str, Any]) -> None:
    if resolution.get("status") == "resolved" and resolution.get("id"):
        fields[f"{nav}@odata.bind"] = f"/{entity_set}({resolution['id']})"


def _number(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _operation_base_fields(extracted: Dict[str, Any]) -> Dict[str, Any]:
    fields: Dict[str, Any] = {
        "mesco_direction": IMPORT,
        "mesco_transporttype": SEA,
        "mesco_loadtype": LCL,
    }
    if extracted.get("etd"):
        fields["mesco_etdorigin"] = extracted["etd"]
    if extracted.get("voyage_number"):
        fields["mesco_voytruckno"] = extracted["voyage_number"]
    type_option = container_type_option(extracted.get("container_type"))
    if type_option is not None:
        fields["mesco_containertype"] = type_option
    return fields


def _required_failure(label: str, resolution: Dict[str, Any]) -> Optional[str]:
    if resolution.get("required") and resolution.get("status") != "resolved":
        return f"{label}: {resolution.get('status')} ({resolution.get('source') or 'empty'})"
    return None


def build_invoice_mapping_plan(
    extracted: Dict[str, Any],
    references: Dict[str, List[Dict[str, Any]]],
    reference_errors: Optional[List[str]] = None,
) -> Dict[str, Any]:
    groups = extracted.get("groups") or []

    currency = resolve_unique_lookup(
        extracted.get("currency"), references.get("currencies", []),
        kind="currency", id_key="transactioncurrencyid",
        label_keys=("isocurrencycode", "currencyname"),
    )
    vendor = resolve_unique_lookup(
        extracted.get("vendor_name"), references.get("shipping_lines", []),
        kind="invoice_vendor", id_key="mesco_shippinglineid", label_keys=("mesco_name",),
    )
    carrier = resolve_unique_lookup(
        extracted.get("carrier"), references.get("shipping_lines", []),
        kind="shipping_line", id_key="mesco_shippinglineid", label_keys=("mesco_name",),
    )
    vessel = resolve_unique_lookup(
        extracted.get("vessel_name"), references.get("vessels", []),
        kind="vessel", id_key="mesco_vesselsmisid", label_keys=("mesco_name",),
    )

    service_sources = sorted({
        str(line.get("service_description") or "Invoice Charge").strip()
        for group in groups for line in (group.get("line_items") or [])
    })
    services = {
        source: resolve_unique_lookup(
            source, references.get("services", []), kind="service",
            id_key="xollsp_servicedefinitionid", label_keys=("xollsp_name",),
        )
        for source in service_sources
    }

    master_fields = _operation_base_fields(extracted)
    master_bl = str(extracted.get("master_bl_number") or "").strip()
    master_fields.update({
        "mesco_code": master_bl,
        "mesco_masterblno": master_bl,
        "mesco_bltype": MASTER_BL,
    })
    master_packages = sum(_number(group.get("packages")) or 0.0 for group in groups)
    totals = {
        "cr401_totalpackages": master_packages,
        "cr401_totalgrossweight": _number(extracted.get("total_gross_weight_kg")),
        "cr401_totalvolume": _number(extracted.get("total_volume_cbm")),
    }
    master_fields.update({key: value for key, value in totals.items() if value is not None})
    _bind(master_fields, "mesco_ShippingLine", "mesco_shippinglines", carrier)
    _bind(master_fields, "mesco_Vessel", "mesco_vesselsmises", vessel)
    _bind(master_fields, "transactioncurrencyid", "transactioncurrencies", currency)

    group_plans: List[Dict[str, Any]] = []
    failures: List[str] = list(reference_errors or [])
    for label, resolution in (
        ("currency", currency), ("invoice vendor", vendor),
        ("shipping line", carrier), ("vessel", vessel),
    ):
        failure = _required_failure(label, resolution)
        if failure:
            failures.append(failure)
    for service_name, resolution in services.items():
        failure = _required_failure(f"service {service_name}", resolution)
        if failure:
            failures.append(failure)

    for group in groups:
        hbl = str(group.get("house_bl_number") or "").strip()
        destination = resolve_unique_lookup(
            group.get("destination"), references.get("addresses", []),
            kind="destination", id_key="xollsp_addressid", label_keys=("xollsp_name",),
        )
        incoterm = resolve_unique_lookup(
            group.get("term"), references.get("incoterms", []),
            kind="incoterm", id_key="xollsp_incotermid", label_keys=("xollsp_name",),
        )
        fields = _operation_base_fields(extracted)
        fields.update({
            "mesco_code": hbl,
            "mesco_masterblno": hbl,
            "mesco_bltype": HOUSE_BL,
        })
        for source, target in (
            (group.get("packages"), "cr401_totalpackages"),
            (group.get("kgs"), "cr401_totalgrossweight"),
            (group.get("cbm"), "cr401_totalvolume"),
        ):
            value = _number(source)
            if value is not None:
                fields[target] = value
        _bind(fields, "mesco_Destination", "xollsp_addresses", destination)
        _bind(fields, "mesco_Incoterm", "xollsp_incoterms", incoterm)
        _bind(fields, "mesco_ShippingLine", "mesco_shippinglines", carrier)
        _bind(fields, "mesco_Vessel", "mesco_vesselsmises", vessel)
        _bind(fields, "transactioncurrencyid", "transactioncurrencies", currency)

        group_failures = []
        for label, resolution in (("destination", destination), ("incoterm", incoterm)):
            failure = _required_failure(f"{hbl} {label}", resolution)
            if failure:
                group_failures.append(failure)
                failures.append(failure)
        group_plans.append({
            "house_bl_number": hbl,
            "fields": fields,
            "lookups": {"destination": destination, "incoterm": incoterm},
            "ready": not group_failures,
            "errors": group_failures,
        })

    reconciled = bool((extracted.get("extraction_validation") or {}).get("is_reconciled"))
    if not reconciled:
        failures.append("invoice totals are not reconciled")

    return {
        "ready_to_post": not failures,
        "errors": failures,
        "operation_field_contract": {
            "incoterm_source": "term -> mesco_Incoterm lookup",
            "freight_term": "not supplied by workbook; mesco_PCFreightTerm intentionally omitted",
            "direction": "Import (300000000)",
            "transport_type": "Sea (300000000)",
            "load_type": "LCL (300000001)",
            "container_type": "1X40HC -> HighCube (100000001)",
        },
        "lookups": {
            "currency": currency,
            "invoice_vendor": vendor,
            "shipping_line": carrier,
            "vessel": vessel,
            "services": services,
        },
        "master_operation": {"fields": master_fields},
        "groups": group_plans,
        "container": {
            "container_number": extracted.get("container_number"),
            "container_type_source": extracted.get("container_type"),
            "container_type_option": container_type_option(extracted.get("container_type")),
            "packages": master_packages,
            "gross_weight_kg": totals.get("cr401_totalgrossweight"),
            "volume_cbm": totals.get("cr401_totalvolume"),
        },
    }


def mapping_group(plan: Dict[str, Any], house_bl_number: Any) -> Optional[Dict[str, Any]]:
    wanted = normalize_label(house_bl_number)
    return next(
        (group for group in plan.get("groups", [])
         if normalize_label(group.get("house_bl_number")) == wanted),
        None,
    )

