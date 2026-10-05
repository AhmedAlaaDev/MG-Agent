"""SACO and Globelink storage-invoice parsing."""

from pathlib import Path

import fitz
import pytest
from fastapi.testclient import TestClient

from app.main import app
from paths import PROJECT_ROOT

client = TestClient(app)
SAMPLE_DIR = PROJECT_ROOT / "Invoices" / "Saco_Invoices"
GLOBELINK_DIR = PROJECT_ROOT / "Invoices" / "globlink"

SACO_133349 = {
    "vendor_profile": "saco",
    "document_language": "ar",
    "vendor_invoice_number": "INV-2026-26275",
    "house_bl_number": "01/26/133349",
    "container_number": "MCLU5093081",
    "vessel_name": "JOANNA BORCHARD",
    "port_of_loading": "Milan",
    "storage_days": 36,
    "subtotal_amount": 1596.0,
    "tax_amount": 223.44,
    "total_amount": 1819.44,
}

SACO_134632 = {
    "vendor_invoice_number": "INV-2026-26299",
    "house_bl_number": "01/26/134632",
    "container_number": "MCLU5110506",
    "vessel_name": "RACHEL BORCHARD",
    "storage_days": 27,
    "total_amount": 1573.2,
}

GLOBELINK_BL_ON_SACO = {
    "vendor_profile": "saco",
    "vendor_invoice_number": "INV-2026-26273",
    "house_bl_number": "GLSEALX2606092",
    "shipment_ref": "EALX2606092",
    "container_number": "CSGU7053430",
    "vessel_name": "CMA CGM ANTIGONE",
    "port_of_loading": "Busan",
    "storage_days": 43,
    "total_amount": 6354.36,
}


def _post_pdf(path: Path, url: str, **form):
    return client.post(
        url,
        files={"file": (path.name, path.read_bytes(), "application/pdf")},
        data={"post_to_dataverse": "false", **form},
    )


def test_vendor_catalog_lists_saco_and_globelink():
    response = client.get("/extract/invoice/vendors")
    assert response.status_code == 200
    codes = [row["code"] for row in response.json()["vendors"]]
    assert codes == ["saco", "globelink"]


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("01 26 133349.pdf", SACO_133349),
        ("01 26 134632.pdf", SACO_134632),
        ("GLSEALX2606092.pdf", GLOBELINK_BL_ON_SACO),
    ],
)
def test_saco_arabic_storage_invoices(filename: str, expected: dict):
    sample = SAMPLE_DIR / filename
    if not sample.exists():
        pytest.skip("SACO sample invoice is not in the workspace")
    response = _post_pdf(sample, "/extract/invoice/saco")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    data = body["data"]
    for key, value in expected.items():
        assert data[key] == value
    assert data["client_name"] == "Mesco - Marine and engineering services co"
    assert data["line_items"][0]["service_description"] == "Storage"
    assert data["tax_rate"] == "14%"
    assert "Lumumba" in data["vendor_address"]


def test_globelink_choice_rejects_saco_invoice():
    sample = SAMPLE_DIR / "01 26 133349.pdf"
    if not sample.exists():
        pytest.skip("SACO sample invoice is not in the workspace")
    response = _post_pdf(sample, "/extract/invoice/storage", vendor="globlink")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert "SACO" in body["error"]
    assert "/extract/invoice/saco" in body["error"]


def test_english_globelink_storage_invoice(tmp_path: Path):
    pdf_path = tmp_path / "globelink-storage.pdf"
    _write_english_storage_pdf(pdf_path, "GLOBELINK EGYPT")
    response = _post_pdf(pdf_path, "/extract/invoice/globelink")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["vendor_profile"] == "globelink"
    assert data["document_language"] == "en"
    assert data["vendor_invoice_number"] == "INV-2026-30001"
    assert data["house_bl_number"] == "GLSEALX2607001"
    assert data["shipment_ref"] == "EALX2607001"
    assert data["container_number"] == "MSCU1234567"
    assert data["vessel_name"] == "CMA CGM TEST"
    assert data["port_of_loading"] == "Jeddah"
    assert data["storage_days"] == 15
    assert data["total_amount"] == 1140.0
    assert data["tax_rate"] == "14%"


GLOBELINK_ENTRY_FEES = {
    "invoice_layout": "globelink_tax_invoice",
    "document_type": "TAX INVOICE",
    "document_status": "DRAFT",
    "vendor_profile": "globelink",
    "vendor_name": "GLOBELINK EGYPT",
    "vendor_invoice_number": "0751260901515",
    "vendor_vat_number": "218171544",
    "client_name": "MARINE AND ENGINEERING COMPANY (MESCO)",
    "client_vat_number": "297923900",
    "job_ref": "ICS075260900001",
    "imp_number": "IBK075260900056",
    "sn_dn_number": "0750002112",
    "payment_term": "60 days",
    "eta_date": "30/09/2026",
    "port_of_discharge": "ALEXANDRIA",
    "house_bl_number": None,
    "container_number": None,
    "prepared_by": "hgaber",
    "remarks": "MESCO LCL STORAGE SERVICE MERGHEM SEP. 2026",
    "subtotal_amount": 87294.0,
    "tax_amount": 12221.16,
    "total_amount": 99515.16,
    "tax_rate": "14%",
}

GLOBELINK_STORAGE = {
    "vendor_invoice_number": "0751260901514",
    "remarks": "ST MESCO MERGHEM LCL SEP. 2026",
    "subtotal_amount": 376154.0,
    "tax_amount": 52661.56,
    "total_amount": 428815.56,
}


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("MESCO LCL STORAGE SERVICE MERGHEM SEP. 2026.pdf", GLOBELINK_ENTRY_FEES),
        ("ST MESCO MERGHEM LCL SEP. 2026.pdf", GLOBELINK_STORAGE),
    ],
)
def test_globelink_tax_invoices(filename: str, expected: dict):
    sample = GLOBELINK_DIR / filename
    if not sample.exists():
        pytest.skip("Globelink sample invoice is not in the workspace")
    response = _post_pdf(sample, "/extract/invoice/globelink")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    data = body["data"]
    for key, field_value in expected.items():
        assert data[key] == field_value
    assert data["currency"] == "EGP"
    assert data["invoice_date"] is None
    assert data["line_items"][0]["currency"] == "EGP"
    assert data["amount_in_words"]


def test_globlink_alias_reads_tax_invoice():
    sample = GLOBELINK_DIR / "MESCO LCL STORAGE SERVICE MERGHEM SEP. 2026.pdf"
    if not sample.exists():
        pytest.skip("Globelink sample invoice is not in the workspace")
    response = _post_pdf(sample, "/extract/invoice/storage", vendor="globlink")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["vendor_profile"] == "globelink"
    assert data["vendor_invoice_number"] == "0751260901515"
    assert data["line_items"][0]["service_description"] == "CFS- Truck Entry Fees"


def test_saco_choice_rejects_globelink_tax_invoice():
    sample = GLOBELINK_DIR / "ST MESCO MERGHEM LCL SEP. 2026.pdf"
    if not sample.exists():
        pytest.skip("Globelink sample invoice is not in the workspace")
    response = _post_pdf(sample, "/extract/invoice/saco")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert "Globelink" in body["error"]
    assert "/extract/invoice/globelink" in body["error"]


def test_storage_choice_rejects_an_unknown_vendor():
    sample = SAMPLE_DIR / "01 26 133349.pdf"
    if not sample.exists():
        pytest.skip("SACO sample invoice is not in the workspace")
    response = _post_pdf(sample, "/extract/invoice/storage", vendor="we-can")
    assert response.status_code == 200
    assert response.json()["success"] is False


def _write_english_storage_pdf(path: Path, issuer: str) -> None:
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    rows = [
        (48, issuer),
        (66, "Merghem Warehouse Alexandria"),
        (87, "Alexandria Egypt INV-2026-30001"),
        (102, "TEL: (03) 1234567"),
        (108, "INV-0000000000099999"),
        (150, "Mesco - Marine and"),
        (156, "297923900 29-09-2026"),
        (165, "engineering services co"),
        (186, "IM26090001 Jeddah CMA CGM TEST"),
        (204, "MSCU1234567 01-09-2026 GLSEALX2607001"),
        (225, "C 20-09-2026 05-09-2026"),
        (246, "EALX2607001 15"),
        (309, "1000.00 EGP Storage 1"),
        (336, "1000.00"),
        (354, "140.00"),
        (384, "1140.00"),
        (417, 'Only " One Thousand One Hundred Forty Egyptian Pounds "'),
        (507, "N/A Sara Ali"),
    ]
    for top, text in rows:
        page.insert_text((72, top + 8), text, fontsize=9)
    document.save(path)
    document.close()
