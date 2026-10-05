"""Create the workbook-backed SACO and GLOBELINK buy tariff schemes in Dataverse."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List

import requests

from app.infrastructure.dataverse.client_service import DataverseClientService

VALID_FROM = "2026-10-01"
VALID_TO = "2027-09-30"
BUY = 300000000
SEA = 300000000
IMPORT = 300000000
LCL = 300000001
EGP_ID = "2976d792-d81c-ed11-b83b-0022487f2774"
CBM_ID = "d61487d6-888a-ed11-81ad-6045bd8f9839"


@dataclass(frozen=True)
class Charge:
    code: str
    service: str
    basis: str
    rate: float
    active_by_default: bool = True


@dataclass(frozen=True)
class Tariff:
    name: str
    description: str
    vendor_name: str
    vendor_id: str
    provider: str
    variant: str
    source: str
    charges: List[Charge]


TARIFFS = [
    Tariff(
        name="فاتورة THC (MESCO) - ساكو",
        description="فاتورة THC الخاصة بساكو (MESCO). مصاريف التفريغ 480 لكل CBM، إدارة البوليصة 300، رفع الإذن 600، وضريبة قيمة مضافة 14%. تضاف 2500 جنيه للشحنات IMO؛ وأساس الضريبة لا يشمل IMO حسب صيغة الملف. المصدر: Tariff.xlsx، ورقة saco، B5:D12.",
        vendor_name="SACO SHIPPING EGYPT",
        vendor_id="3a60e9c4-44c0-ee11-9079-6045bd8c5402",
        provider="SACO",
        variant="thc",
        source="Tariff.xlsx · saco · B5:D12",
        charges=[
            Charge("thc.discharge.cbm", "Terminal Discharging", "per_cbm", 480),
            Charge("thc.bl_admin.fixed", "Admin Fees", "fixed", 300),
            Charge("thc.delivery_order.fixed", "Delivery Order", "fixed", 600),
            Charge("thc.imo.fixed", "IMO Charges", "conditional_fixed", 2500, False),
            Charge("thc.vat.percent", "Taxes", "percent", 14),
        ],
    ),
    Tariff(
        name="فاتورة التخزين غير IMO - ساكو (MESCO)",
        description="تعريفة تخزين ساكو لمخازن مرغم وTMT والدخيلة. التخزين 10 لكل CBM يومياً، تفريغ وتستيف وتحميل 65 لكل CBM لكل بند، إدارة البوليصة 200، إجراءات تخزينية 20%، ثم ضريبة 14% على مجموع البنود والإجراءات. المصدر: Tariff.xlsx، ورقة saco، B22:E31.",
        vendor_name="SACO SHIPPING EGYPT",
        vendor_id="3a60e9c4-44c0-ee11-9079-6045bd8c5402",
        provider="SACO",
        variant="storage",
        source="Tariff.xlsx · saco · B22:E31",
        charges=[
            Charge("storage.storage.cbm_day", "Storage Service", "per_cbm_day", 10),
            Charge("storage.warehouse_discharge.cbm", "Warehouse Discharging", "per_cbm", 65),
            Charge("storage.warehouse_stowage.cbm", "Warehouse Stowage", "per_cbm", 65),
            Charge("storage.warehouse_loading.cbm", "Warehouse Loading", "per_cbm", 65),
            Charge("storage.bl_admin.fixed", "Admin Fees", "fixed", 200),
            Charge("storage.services.percent", "Percentage Services", "percent", 20),
            Charge("storage.vat.percent", "Taxes", "percent", 14),
        ],
    ),
    Tariff(
        name="MESCO Storage / MERGHEM - Consol",
        description="GLOBELINK MESCO Consol tariff for Merghem. Chargeable W/M is the greater of gross weight in metric tonnes or CBM. Storage 10.40/W/M/day; discharging and loading 49/W/M each; inspection 30/W/M; cleaning 91 and samples 91 fixed; services 9%. Friday surcharge 805 is fixed and selected manually. Source: GLOBELINK ST merghem.xlsx, MESCO Consol, G7:N8.",
        vendor_name="Globelink Egypt",
        vendor_id="82526d7e-3e4e-ef11-bfe2-00224888a24f",
        provider="GLOBELINK",
        variant="consol",
        source="GLOBELINK ST merghem.xlsx · MESCO Consol · G7:N8",
        charges=[
            Charge("consol.storage.wm_day", "Storage Service", "per_wm_day", 10.4),
            Charge("consol.discharge.wm", "Terminal Discharging", "per_wm", 49),
            Charge("consol.loading.wm", "Loading Charges", "per_wm", 49),
            Charge("consol.inspection.wm", "Inspection Fees", "per_wm", 30),
            Charge("consol.cleaning.fixed", "Cleaning", "fixed", 91),
            Charge("consol.samples.fixed", "Samples", "fixed", 91),
            Charge("consol.services.percent", "Percentage Services", "percent", 9),
            Charge("consol.friday.fixed", "Friday Surcharge", "conditional_fixed", 805, False),
        ],
    ),
    Tariff(
        name="MESCO Storage / MERGHEM - Co-Load",
        description="GLOBELINK MESCO Co-Load, Merghem. Chargeable W/M=max(gross weight in MT, CBM). Inspection 43.75/W/M per workbook formula. Cleaning and samples are configured but off by default because their formulas are zero. Services: heading 6%, rate 9%, formula 0%; off by default pending confirmation. Friday surcharge 805 is fixed and selected manually. Source: GLOBELINK ST merghem.xlsx, MESCO Co-Load, H7:O8.",
        vendor_name="Globelink Egypt",
        vendor_id="82526d7e-3e4e-ef11-bfe2-00224888a24f",
        provider="GLOBELINK",
        variant="coload",
        source="GLOBELINK ST merghem.xlsx · MESCO Co-Load · H7:O8",
        charges=[
            Charge("coload.storage.wm_day", "Storage Service", "per_wm_day", 21.25),
            Charge("coload.discharge.wm", "Terminal Discharging", "per_wm", 68.75),
            Charge("coload.loading.wm", "Loading Charges", "per_wm", 68.75),
            Charge("coload.inspection.wm", "Inspection Fees", "per_wm", 43.75),
            Charge("coload.cleaning.fixed", "Cleaning", "fixed", 100, False),
            Charge("coload.samples.fixed", "Samples", "fixed", 100, False),
            Charge("coload.services.percent", "Percentage Services", "percent", 9, False),
            Charge("coload.friday.fixed", "Friday Surcharge", "conditional_fixed", 805, False),
        ],
    ),
]

SERVICE_TYPES = {
    "Terminal Discharging": 300000003,
    "Warehouse Discharging": 300000003,
    "Warehouse Stowage": 300000003,
    "Warehouse Loading": 300000003,
    "Cleaning": 300000004,
    "Samples": 300000004,
    "Percentage Services": 300000004,
    "Friday Surcharge": 300000004,
}

LINE_LABELS = {
    "thc.discharge.cbm": "مصاريف تفريغ",
    "thc.bl_admin.fixed": "م. اداريه للبوليصه",
    "thc.delivery_order.fixed": "م. رفع اذن",
    "thc.imo.fixed": "مصاريف بضائع IMO",
    "thc.vat.percent": "ضريبة القيمة المضافة 14%",
    "storage.storage.cbm_day": "مصاريف التخزين",
    "storage.warehouse_discharge.cbm": "م. تفريغ المشمول داخل المخزن",
    "storage.warehouse_stowage.cbm": "م. تستيف داخل المخزن",
    "storage.warehouse_loading.cbm": "م. تحميل الي خارج المخزن",
    "storage.bl_admin.fixed": "م. اداريه للبوليصه",
    "storage.services.percent": "عروض و اجراءات تخزينيه 20%",
    "storage.vat.percent": "ضريبة القيمة المضافة 14%",
    "consol.storage.wm_day": "Storage Fee W/M day",
    "consol.discharge.wm": "Discharging Fee",
    "consol.loading.wm": "Loading Fee",
    "consol.inspection.wm": "Inspection Fee",
    "consol.cleaning.fixed": "Cleaning",
    "consol.samples.fixed": "Samples",
    "consol.services.percent": "Services 9% total",
    "consol.friday.fixed": "FRIDAY",
    "coload.storage.wm_day": "Storage Fee W/M day",
    "coload.discharge.wm": "Discharging Fee",
    "coload.loading.wm": "Loading Fee",
    "coload.inspection.wm": "Inspection Fee",
    "coload.cleaning.fixed": "Cleaning",
    "coload.samples.fixed": "Samples",
    "coload.services.percent": "Services 6% total",
    "coload.friday.fixed": "FRIDAY",
}

LEGACY_SCHEME_NAMES = {
    "فاتورة THC (MESCO) - ساكو": "SACO - THC - 2026",
    "فاتورة التخزين غير IMO - ساكو (MESCO)": "SACO - Storage - 2026",
    "MESCO Storage / MERGHEM - Consol": "GLOBELINK - MESCO Consol - 2026",
    "MESCO Storage / MERGHEM - Co-Load": "GLOBELINK - MESCO Co-Load - 2026",
}

LEGACY_DESCRIPTIONS = {
    "MESCO Storage / MERGHEM - Consol": "GLOBELINK MESCO Consol tariff for Merghem. Chargeable W/M is the greater of gross weight in metric tonnes or CBM. Storage 10.40/W/M/day; discharging and loading 49/W/M each; inspection 30, cleaning 91, samples 91 fixed; services 9%. Friday surcharge 805 is fixed and selected manually. Source: GLOBELINK ST merghem.xlsx, MESCO Consol, G7:N8.",
    "MESCO Storage / MERGHEM - Co-Load": "GLOBELINK MESCO Co-Load, Merghem. Chargeable W/M=max(gross weight in MT, CBM). Inspection is stored fixed per the tariff rule; workbook formula is per W/M. Cleaning and samples are configured but off by default because their formulas are zero. Services: heading 6%, rate 9%, formula 0%; off by default pending confirmation. Friday surcharge 805 is fixed and selected manually. Source: GLOBELINK ST merghem.xlsx, MESCO Co-Load, H7:O8.",
}

LEGACY_LINE_CODES = {
    "consol.inspection.fixed": "consol.inspection.wm",
    "coload.inspection.fixed": "coload.inspection.wm",
}

CHARGE_COMMENTS = {
    "thc.discharge.cbm": "مصاريف تفريغ: 480 جنيه لكل CBM. المصدر: Tariff.xlsx، SACO، B5.",
    "thc.bl_admin.fixed": "مصاريف إدارية للبوليصة: مبلغ ثابت 300 جنيه. المصدر: Tariff.xlsx، SACO، B6.",
    "thc.delivery_order.fixed": "رفع إذن: مبلغ ثابت 600 جنيه. المصدر: Tariff.xlsx، SACO، B7.",
    "thc.imo.fixed": "إضافة 2500 جنيه لشحنة IMO؛ غير مفعلة افتراضياً، ولا تدخل في أساس VAT.",
    "thc.vat.percent": "ضريبة قيمة مضافة بنسبة 14% وفق إجمالي بنود THC في Tariff.xlsx، SACO، D9.",
    "storage.storage.cbm_day": "مصاريف تخزين: 10 جنيه لكل CBM عن كل يوم. المصدر: Tariff.xlsx، SACO، B22:E22.",
    "storage.warehouse_discharge.cbm": "تفريغ داخل المخزن: 65 جنيهاً لكل CBM. المصدر: Tariff.xlsx، SACO، B23:E23.",
    "storage.warehouse_stowage.cbm": "تستيف داخل المخزن: 65 جنيهاً لكل CBM. المصدر: Tariff.xlsx، SACO، B24:E24.",
    "storage.warehouse_loading.cbm": "تحميل إلى خارج المخزن: 65 جنيهاً لكل CBM. المصدر: Tariff.xlsx، SACO، B25:E25.",
    "storage.bl_admin.fixed": "مصاريف إدارية للبوليصة: مبلغ ثابت 200 جنيه. المصدر: Tariff.xlsx، SACO، B26:E26.",
    "storage.services.percent": "عروض وإجراءات تخزينية بنسبة 20% من البنود قبل الخدمات. المصدر: Tariff.xlsx، SACO، B28:E28.",
    "storage.vat.percent": "ضريبة قيمة مضافة بنسبة 14% على البنود والخدمات. المصدر: Tariff.xlsx، SACO، B30:E31.",
    "consol.storage.wm_day": "Storage fee: EGP 10.40 per chargeable W/M per day. Source: GLOBELINK ST merghem.xlsx, MESCO Consol G7.",
    "consol.discharge.wm": "Discharging fee: EGP 49 per chargeable W/M. Source: GLOBELINK ST merghem.xlsx, MESCO Consol H7.",
    "consol.loading.wm": "Loading fee: EGP 49 per chargeable W/M. Source: GLOBELINK ST merghem.xlsx, MESCO Consol I7.",
    "consol.inspection.wm": "Inspection fee: EGP 30 per chargeable W/M (J8 = J7 × W/M).",
    "consol.cleaning.fixed": "Cleaning fee: fixed EGP 91 per tariff row. Source: GLOBELINK ST merghem.xlsx, MESCO Consol K7:K8.",
    "consol.samples.fixed": "Sampling fee: fixed EGP 91 per tariff row. Source: GLOBELINK ST merghem.xlsx, MESCO Consol L7:L8.",
    "consol.services.percent": "Services surcharge: 9% of the listed charges, excluding Friday.",
    "consol.friday.fixed": "Friday surcharge: fixed EGP 805; included only when selected.",
    "coload.storage.wm_day": "Storage fee: EGP 21.25 per chargeable W/M per day. Source: GLOBELINK ST merghem.xlsx, MESCO Co-Load H7:H8.",
    "coload.discharge.wm": "Discharging fee: EGP 68.75 per chargeable W/M. Source: GLOBELINK ST merghem.xlsx, MESCO Co-Load I7:I8.",
    "coload.loading.wm": "Loading fee: EGP 68.75 per chargeable W/M. Source: GLOBELINK ST merghem.xlsx, MESCO Co-Load J7:J8.",
    "coload.inspection.wm": "Inspection fee: EGP 43.75 per chargeable W/M (K8 = K7 × W/M).",
    "coload.cleaning.fixed": "Cleaning: configured EGP 100 fixed; workbook formula is zero, so off by default.",
    "coload.samples.fixed": "Sampling: configured EGP 100 fixed; workbook formula is zero, so off by default.",
    "coload.services.percent": "Services: heading 6%, rate 9%, formula 0%; off by default.",
    "coload.friday.fixed": "Friday surcharge: fixed EGP 805; included only when selected.",
}


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def rows(client: Any, query: str) -> List[Dict[str, Any]]:
    response = client.get(query, timeout=45)
    response.raise_for_status()
    return response.json().get("value", [])


def load_services(client: Any) -> Dict[str, Dict[str, Any]]:
    definitions = rows(
        client,
        "xollsp_servicedefinitions?$select=xollsp_servicedefinitionid,xollsp_name,xollsp_servicetype&$top=5000",
    )
    return {normalize(row.get("xollsp_name", "")): row for row in definitions if row.get("xollsp_name")}


def ensure_service(client: Any, services: Dict[str, Dict[str, Any]], name: str) -> str:
    key = normalize(name)
    if key not in services:
        client.post(
            "xollsp_servicedefinitions",
            json={"xollsp_name": name, "xollsp_servicetype": SERVICE_TYPES[name]},
        ).raise_for_status()
        services.update(load_services(client))
    service_id = services.get(key, {}).get("xollsp_servicedefinitionid")
    if not service_id:
        raise RuntimeError(f"Dataverse service could not be resolved after creation: {name}")
    return service_id


def tariff_description(tariff: Tariff) -> str:
    return tariff.description


def find_scheme_record(client: Any, tariff: Tariff) -> Dict[str, Any] | None:
    matches = rows(
        client,
        "xollsp_tariffschemes?$select=xollsp_tariffschemeid,xollsp_name,xollsp_description,_xollsp_account_value"
        f"&$filter=_xollsp_account_value eq {tariff.vendor_id}&$top=100",
    )
    names={tariff.name,LEGACY_SCHEME_NAMES[tariff.name]}
    return next((row for row in matches if row.get("xollsp_name") in names),None)


def scheme_values(tariff: Tariff) -> Dict[str, Any]:
    return {
        "xollsp_name": tariff.name,
        "xollsp_type": BUY,
        "mesco_transporttype": SEA,
        "mesco_loadtype": LCL,
        "mesco_importexport": IMPORT,
        "xollsp_description": tariff_description(tariff),
        "xollsp_Account@odata.bind": f"/accounts({tariff.vendor_id})",
    }


def create_scheme(client: Any, tariff: Tariff) -> str:
    response = client.post("xollsp_tariffschemes", json=scheme_values(tariff))
    response.raise_for_status()
    location = response.headers.get("OData-EntityId") or response.headers.get("Location", "")
    found = re.search(r"xollsp_tariffschemeid=([0-9a-f-]{36})", location, re.I)
    if found:
        return found.group(1)
    return rows(
        client,
        "xollsp_tariffschemes?$select=xollsp_tariffschemeid&"
        f"$filter=_xollsp_account_value eq {tariff.vendor_id} and xollsp_name eq '{tariff.name}'&$top=1",
    )[0]["xollsp_tariffschemeid"]


def ensure_scheme(client: Any, tariff: Tariff) -> str:
    existing=find_scheme_record(client,tariff)
    if not existing:
        return create_scheme(client,tariff)
    scheme_id=existing["xollsp_tariffschemeid"]
    old_description=LEGACY_DESCRIPTIONS.get(tariff.name)
    description=existing.get("xollsp_description") or ""
    needs_description_update=description.lstrip().startswith("{") or description==old_description
    if existing.get("xollsp_name")!=tariff.name or needs_description_update:
        client.patch(f"xollsp_tariffschemes({scheme_id})",json=scheme_values(tariff)).raise_for_status()
    return scheme_id


def rate_field(charge: Charge) -> Dict[str, Any]:
    fixed = charge.basis in {"fixed", "conditional_fixed"}
    return {
        "xollsp_fixedprice": charge.rate if fixed else None,
        "xollsp_priceperunit1": None if fixed else charge.rate,
    }


def charge_comment(charge: Charge) -> str:
    return CHARGE_COMMENTS[charge.code]


def line_links(service: Dict[str, Any], scheme_id: str) -> Dict[str, Any]:
    service_id=service["xollsp_servicedefinitionid"]
    return {
        "xollsp_ServiceDefinition@odata.bind": f"/xollsp_servicedefinitions({service_id})",
        "xollsp_TariffScheme@odata.bind": f"/xollsp_tariffschemes({scheme_id})",
        "xollsp_validfrom": VALID_FROM,
        "xollsp_validto": VALID_TO,
        "xollsp_Currency@odata.bind": f"/transactioncurrencies({EGP_ID})",
        "xollsp_servicetype": service.get("xollsp_servicetype", 300000004),
    }


def line_values(
    charge: Charge,
    service: Dict[str, Any],
    scheme_id: str,
) -> Dict[str, Any]:
    values: Dict[str, Any] = {
        "xollsp_name": LINE_LABELS[charge.code],
        "xollsp_comments": charge_comment(charge),
    }
    values.update(line_links(service,scheme_id))
    values.update(rate_field(charge))
    if ".cbm" in charge.code:
        values["xollsp_UnitofMeasure@odata.bind"] = f"/xollsp_unitsofmeasures({CBM_ID})"
    return values


def existing_line_codes(client: Any, tariff: Tariff, scheme_id: str) -> Dict[str, Dict[str, Any]]:
    existing = rows(
        client,
        "xollsp_tariffschemelines?$select=xollsp_tariffschemelineid,xollsp_name,xollsp_comments"
        f"&$filter=_xollsp_tariffscheme_value eq {scheme_id}&$top=5000",
    )
    codes={charge.code for tariff in TARIFFS for charge in tariff.charges}
    labels={LINE_LABELS[charge.code]:charge.code for charge in tariff.charges}
    result={}
    for row in existing:
        comment=row.get("xollsp_comments") or ""
        match=re.search(r"(?:^|;)code=([^;]+)",comment)
        code=match.group(1) if match else labels.get(row.get("xollsp_name"),row.get("xollsp_name"))
        code=LEGACY_LINE_CODES.get(code,code)
        if code in codes: result[code]=row
    return result


def save_missing_line(client: Any, services: Dict[str, Dict[str, Any]], charge: Charge, scheme_id: str) -> None:
    ensure_service(client, services, charge.service)
    payload = line_values(charge, services[normalize(charge.service)], scheme_id)
    try:
        client.post("xollsp_tariffschemelines", json=payload).raise_for_status()
    except requests.exceptions.HTTPError as exc:
        detail = exc.response.text[:1200] if exc.response is not None else str(exc)
        raise RuntimeError(f"Could not save tariff line {charge.code}: {detail}") from exc


def save_lines(client: Any, services: Dict[str, Dict[str, Any]], tariff: Tariff, scheme_id: str) -> int:
    existing_lines = existing_line_codes(client, tariff, scheme_id)
    for charge in tariff.charges:
        line=existing_lines.get(charge.code)
        if not line:
            save_missing_line(client,services,charge,scheme_id)
            continue
        comment=line.get("xollsp_comments") or ""
        needs_label_migration=(
            line.get("xollsp_name")!=LINE_LABELS[charge.code]
            or "code=" in comment
            or not comment.strip()
        )
        if needs_label_migration:
            ensure_service(client,services,charge.service)
            payload=line_values(charge,services[normalize(charge.service)],scheme_id)
            line_id=line["xollsp_tariffschemelineid"]
            client.patch(f"xollsp_tariffschemelines({line_id})",json=payload).raise_for_status()
    return len(tariff.charges)


def seed() -> None:
    client = DataverseClientService.get_instance()
    services = load_services(client)
    for tariff in TARIFFS:
        scheme_id = ensure_scheme(client, tariff)
        count = save_lines(client, services, tariff, scheme_id)
        print(f"{tariff.name.encode('ascii','backslashreplace').decode()}: {count} charge lines; vendor={tariff.vendor_name}; scheme={scheme_id}")


if __name__ == "__main__":
    seed()
