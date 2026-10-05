"""Read vendor tariff schemes from the existing Dataverse tariff tables."""

from __future__ import annotations

from datetime import date
import re
from typing import Any, Dict, List
from uuid import UUID

from fastapi import APIRouter, HTTPException
import requests

router = APIRouter(prefix="/tariffs", tags=["Tariffs"])

LINE_CODES = {
    "thc": {
        "مصاريف تفريغ": "thc.discharge.cbm",
        "م. اداريه للبوليصه": "thc.bl_admin.fixed",
        "م. رفع اذن": "thc.delivery_order.fixed",
        "مصاريف بضائع IMO": "thc.imo.fixed",
        "ضريبة القيمة المضافة 14%": "thc.vat.percent",
    },
    "storage": {
        "مصاريف التخزين": "storage.storage.cbm_day",
        "م. تفريغ المشمول داخل المخزن": "storage.warehouse_discharge.cbm",
        "م. تستيف داخل المخزن": "storage.warehouse_stowage.cbm",
        "م. تحميل الي خارج المخزن": "storage.warehouse_loading.cbm",
        "م. اداريه للبوليصه": "storage.bl_admin.fixed",
        "عروض و اجراءات تخزينيه 20%": "storage.services.percent",
        "ضريبة القيمة المضافة 14%": "storage.vat.percent",
    },
    "consol": {
        "Storage Fee W/M day": "consol.storage.wm_day",
        "Discharging Fee": "consol.discharge.wm",
        "Loading Fee": "consol.loading.wm",
        "Inspection Fee": "consol.inspection.wm",
        "Cleaning": "consol.cleaning.fixed",
        "Samples": "consol.samples.fixed",
        "Services 9% total": "consol.services.percent",
        "FRIDAY": "consol.friday.fixed",
    },
    "coload": {
        "Storage Fee W/M day": "coload.storage.wm_day",
        "Discharging Fee": "coload.discharge.wm",
        "Loading Fee": "coload.loading.wm",
        "Inspection Fee": "coload.inspection.wm",
        "Cleaning": "coload.cleaning.fixed",
        "Samples": "coload.samples.fixed",
        "Services 6% total": "coload.services.percent",
        "FRIDAY": "coload.friday.fixed",
    },
}


def _client():
    from app.infrastructure.dataverse.client_service import DataverseClientService

    try:
        return DataverseClientService.get_instance()
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Dataverse is unavailable.") from exc


def _get_rows(client: Any, query: str) -> List[Dict[str, Any]]:
    try:
        response = client.get(query, timeout=45)
    except requests.exceptions.RequestException as exc:
        raise HTTPException(status_code=502, detail="Dataverse could not load tariff data.") from exc
    if not response.ok:
        raise HTTPException(status_code=502, detail="Dataverse could not load tariff data.")
    return response.json().get("value", [])


def _brand_match(name: str) -> bool:
    normalized = " ".join(name.casefold().split())
    return normalized in {"saco shipping egypt", "globelink egypt"}


@router.get("/vendors")
def list_tariff_vendors() -> Dict[str, Any]:
    """Return active SACO and GLOBELINK vendor accounts for tariff selection."""
    client = _client()
    accounts = _get_rows(
        client,
        "accounts?$select=accountid,name,statecode&$top=5000",
    )
    vendors = [
        {"id": row["accountid"], "name": row.get("name", ""), "statecode": row.get("statecode")}
        for row in accounts
        if row.get("accountid") and row.get("statecode") == 0 and _brand_match(row.get("name", ""))
    ]
    vendors.sort(key=lambda vendor: (vendor["name"].casefold(), vendor["id"]))
    return {"vendors": vendors}


def _active_line(line: Dict[str, Any], today: date) -> bool:
    valid_from = line.get("xollsp_validfrom")
    valid_to = line.get("xollsp_validto")
    if not valid_from or not valid_to:
        return False
    return date.fromisoformat(valid_from[:10]) <= today <= date.fromisoformat(valid_to[:10])


def _scheme_variant(name: str) -> str:
    if "THC" in name.upper():
        return "thc"
    if "التخزين" in name:
        return "storage"
    if "CO-LOAD" in name.upper():
        return "coload"
    return "consol"


def _default_enabled(code: str) -> bool:
    optional = {
        "thc.imo.fixed", "consol.friday.fixed", "coload.cleaning.fixed",
        "coload.samples.fixed", "coload.services.percent", "coload.friday.fixed",
    }
    return code not in optional


def _basis_for(code: str) -> str:
    if code in {"thc.imo.fixed", "consol.friday.fixed", "coload.friday.fixed"}:
        return "conditional_fixed"
    return code.rsplit(".", 1)[-1]


def _comment_metadata(comment: str) -> Dict[str, str]:
    return dict(re.findall(r"\b([a-z_]+)=([^;()]+)", comment))


def _annotate_line(line: Dict[str, Any], variant: str) -> Dict[str, Any]:
    metadata = _comment_metadata(line.get("xollsp_comments") or "")
    line["code"] = metadata.get("code") or LINE_CODES.get(variant, {}).get(line.get("xollsp_name", ""), "")
    line["basis"] = metadata.get("basis") or _basis_for(line["code"])
    line["currency"] = metadata.get("currency", "EGP")
    line["default_enabled"] = metadata.get("default_enabled", "1" if _default_enabled(line["code"]) else "0") == "1"
    line["metadata"] = metadata
    return line


def _tariff_config(vendor: Dict[str, Any], variant: str, lines: List[Dict[str, Any]]) -> Dict[str, Any]:
    first_metadata = (lines[0].get("metadata") or {}) if lines else {}
    metadata = [line.get("metadata") or {} for line in lines]
    imo_taxable = next((entry["imo_taxable"] for entry in metadata if "imo_taxable" in entry), "0")
    return {
        "p": "SACO" if "saco" in vendor.get("name", "").casefold() else "GLOBELINK",
        "t": variant,
        "c": first_metadata.get("currency", "EGP"),
        "src": "Tariff.xlsx / saco" if variant in {"thc", "storage"} else "GLOBELINK ST merghem.xlsx",
        "o": {
            "imo_taxable": imo_taxable == "1",
            "inspection_fixed": False,
        },
        "on": {line["code"]: line["default_enabled"] for line in lines if line.get("code")},
    }


def _vendor_record(client: Any, vendor_id: UUID) -> Dict[str, Any]:
    try:
        response = client.get(f"accounts({vendor_id})?$select=accountid,name,statecode", timeout=45)
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Vendor was not found.") from exc
        raise HTTPException(status_code=502, detail="Dataverse could not load the selected vendor.") from exc
    except requests.exceptions.RequestException as exc:
        raise HTTPException(status_code=502, detail="Dataverse could not load the selected vendor.") from exc
    vendor = response.json()
    if vendor.get("statecode") != 0 or not _brand_match(vendor.get("name", "")):
        raise HTTPException(status_code=404, detail="Vendor is not available for these tariff screens.")
    return vendor


def _purchase_schemes(client: Any, vendor_id: UUID) -> List[Dict[str, Any]]:
    query = (
        "xollsp_tariffschemes?$select=xollsp_tariffschemeid,xollsp_name,xollsp_description,xollsp_type,statecode"
        f"&$filter=_xollsp_account_value eq {vendor_id} and xollsp_type eq 300000000 and statecode eq 0"
        "&$top=5000"
    )
    return _get_rows(client, query)


def _current_scheme_lines(client: Any, scheme_id: str, variant: str, today: date) -> List[Dict[str, Any]]:
    query = (
        "xollsp_tariffschemelines?$select=xollsp_tariffschemelineid,xollsp_name,xollsp_comments,xollsp_fixedprice,"
        "xollsp_priceperunit1,xollsp_priceperunit2,xollsp_servicetype,xollsp_validfrom,xollsp_validto,"
        "_xollsp_servicedefinition_value,_xollsp_unitofmeasure_value,statecode"
        f"&$filter=_xollsp_tariffscheme_value eq {scheme_id} and statecode eq 0&$top=5000"
    )
    current = [line for line in _get_rows(client, query) if _active_line(line, today)]
    return [_annotate_line(line, variant) for line in current]


def _scheme_summary(scheme: Dict[str, Any], lines: List[Dict[str, Any]], vendor: Dict[str, Any], variant: str) -> Dict[str, Any]:
    return {
        "id": scheme["xollsp_tariffschemeid"],
        "name": scheme.get("xollsp_name"),
        "description": scheme.get("xollsp_description"),
        "config": _tariff_config(vendor, variant, lines),
        "current": bool(lines),
        "lines": lines,
    }


def _vendor_tariffs(client: Any, vendor: Dict[str, Any], vendor_id: UUID, today: date) -> List[Dict[str, Any]]:
    result=[]
    for scheme in _purchase_schemes(client,vendor_id):
        scheme_id=scheme.get("xollsp_tariffschemeid")
        if not scheme_id: continue
        variant=_scheme_variant(scheme.get("xollsp_name",""))
        lines=_current_scheme_lines(client,scheme_id,variant,today)
        result.append(_scheme_summary(scheme,lines,vendor,variant))
    return result


@router.get("/vendors/{vendor_id}")
def get_vendor_tariffs(vendor_id: UUID) -> Dict[str, Any]:
    """Return current purchase tariff schemes and charge lines for one vendor."""
    client = _client()
    vendor = _vendor_record(client, vendor_id)
    tariffs=_vendor_tariffs(client,vendor,vendor_id,date.today())
    return {"vendor":{"id":str(vendor_id),"name":vendor.get("name")},"tariffs":tariffs}
