"""Tests for freight debit note party/address extraction."""

from pathlib import Path

import pytest

from pdf_debit_note import (
    is_freight_debit_note,
    parse_freight_debit_note,
    repair_debit_note_parties,
)
from spreadsheet_extractor import extract_document_text_professionally
from crm_output_formatter import apply_bl_type_to_crm_payload, records_to_house_json
from validator import validate_and_correct


PDF_PATH = Path(__file__).resolve().parent / "NAV26MU0528 - REBATE DN.pdf"


@pytest.mark.skipif(not PDF_PATH.exists(), reason="Sample debit note PDF not present")
def test_nav26mu0528_consignee_address_from_pdf():
    raw = PDF_PATH.read_bytes()
    text = extract_document_text_professionally(raw, PDF_PATH.name)["text"]
    assert is_freight_debit_note(text)

    record = parse_freight_debit_note(text)
    assert record is not None
    assert record["mesco_houseblno"] == "NAV26MU0528"
    assert record["mesco_masterblno"] == "3100127374"

    assert "EVA PHARMA" in (record.get("mesco_consignee") or "").upper()
    assert "Shipment Ref" not in (record.get("mesco_consigneenamecontactno") or "")
    assert "SADAT" in (record.get("mesco_consigneeaddress") or "").upper()
    assert "GIZA" in (record.get("mesco_consigneeaddress") or "").upper()
    assert "EGYPT" in (record.get("mesco_consigneeaddress") or "").upper()

    assert record.get("mesco_shippernamecontactno") == "MIKHAIL CORPORATION"
    assert "MITTAL COURT" in (record.get("mesco_shipperaddress") or "").upper()
    assert "MAHARASHTRA" in (record.get("mesco_shipperaddress") or "").upper()
    assert record.get("mesco_bookingnumber") == "SHP0000647"
    assert "REBATE" in (record.get("mesco_cargodescription") or "").upper()
    assert "BYTEPORT" not in (record.get("mesco_cargodescription") or "").upper()


def test_debit_note_house_payload_has_clean_cargo():
    sample_text = """
    DEBIT NOTE
    Consignee : EVA PHARMA - SUPPLY CHAIN DEPT Shipment Ref. : SHP0000647
    FOR PHARMACEUTICALS AND MEDICAL APPLIANCES S.A.E, 176
    EI SADAT STREET, KAFER EI GABAL PYRAMIDS, GIZA, EGYPT*, Master Number : 3100127374
    House Number : NAV26MU0528
    Port of Origin : NHAVA SHEVA, INDIA
    1 REBATE 996719 USD 65.00
    """
    record = parse_freight_debit_note(sample_text)
    validated = validate_and_correct(record, sample_text)
    house = records_to_house_json([validated])["value"][0]
    apply_bl_type_to_crm_payload(house, "house")
    assert house.get("mesco_houseblno") == "NAV26MU0528"
    cargo_rows = house.get("mesco_Cargo_HouseOperation_mesco_Operation") or []
    assert len(cargo_rows) == 1
    desc = cargo_rows[0].get("mesco_descriptionofgoods", "")
    assert "REBATE" in desc.upper()
    assert "BYTEPORT" not in desc.upper()


def test_repair_bogus_llm_consignee():
    sample_text = """
    DEBIT NOTE
    Consignee : EVA PHARMA - SUPPLY CHAIN DEPT Shipment Ref. : SHP0000647
    FOR PHARMACEUTICALS AND MEDICAL APPLIANCES S.A.E, 176
    EI SADAT STREET, KAFER EI GABAL PYRAMIDS, GIZA, EGYPT*, Master Number : 3100127374
    House Number : NAV26MU0528
    Port of Origin : NHAVA SHEVA, INDIA
    """
    broken = {
        "mesco_consigneenamecontactno": "Shipment Ref.",
        "mesco_consigneeaddress": ": SHP0000647",
        "mesco_consignee": "EVA PHARMA - SUPPLY CHAIN DEPT FOR PHARMACEUTICALS AND MEDICAL APPLIANCES S.A.E",
    }
    fixed = repair_debit_note_parties(broken, sample_text)
    assert "EVA PHARMA" in (fixed.get("mesco_consigneenamecontactno") or "").upper()
    assert "SADAT" in (fixed.get("mesco_consigneeaddress") or "").upper()
