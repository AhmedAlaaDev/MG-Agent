"""Tests for MTD (Multi-Modal Transport Document) cargo description extraction."""

from crm_output_formatter import records_to_master_json
from pdf_bl_enrichment import (
    _is_mtd_document,
    enrich_bl_from_pdf_text,
    extract_cargo_description_from_pdf,
    extract_consignee_block,
    extract_issuing_agent,
    extract_mtd_cargo_description_from_pdf,
    sanitize_party_address,
)

SAMPLE_MTD = """
--- PAGE 1 ---
MULTI-MODAL TRANSPORT DOCUMENT
MTD No: NAV26MU0714
Shipper
ASPEN GLOBAL INC
Consignee
VLD HEALTHCARE CAIRO
Container No: 8 PALLET(S)
TRKU4457572
A/Seal No: ABC123
C/Seal No: XYZ789
08 PALLETS STC 80 DRUMS
(EIGHT PALLETS SAID TO CONTAIN EIGHTY DRUMS)
80 NOS / 25 KGS (NET) EXPORT STANDARD NEW FIBRE DRUMS DIMENSIONS 14" (D) X 21" (H) ON 08 WOODEN PALLETS
TRIMETHOPRIM EP
ASPEN/VLD HEALTHCARE/ CAIRO
ASPEN GLOBAL INCORPORATED
PURCHASE ORDER NUMBER: 4500103915
ASPEN CUSTOMER INVOICE NUMBER: 9500026475 DATED:30.01.2026
MATERIAL DESCRIPTION: TRIMETHOPRIM EP
MATERIAL NUMBER: 3000156
SCI CODE: 29339990
BATCH NUMBER: BT202601
COUNTRY OF ORIGIN: INDIA
2,299.600 KGS 7.928CBM
Particulars above furnished by Shipper
Shipped on Board: 29-March-2026
"""


def test_is_mtd_document():
    assert _is_mtd_document(SAMPLE_MTD)
    assert not _is_mtd_document("BILL OF LADING\nSAID TO CONTAIN CARGO\n7 PALLETS")


def test_mtd_cargo_description():
    desc = extract_mtd_cargo_description_from_pdf(SAMPLE_MTD)
    assert desc
    upper = desc.upper()
    assert "TRIMETHOPRIM" in upper
    assert "4500103915" in desc
    assert "9500026475" in desc
    assert "3000156" in desc
    assert "80 DRUMS" in upper or "80 DRUMS" in desc
    assert "Particulars above" not in desc
    assert "TRKU4457572" not in desc


def test_extract_cargo_prefers_mtd():
    desc = extract_cargo_description_from_pdf(SAMPLE_MTD)
    assert desc
    assert "TRIMETHOPRIM" in desc.upper()
    assert len(desc) > 80


NAV26MU0800_OCR = """
--- PAGE 1 ---
MULTI-MODAL TRANSPORT DOCUMENT
MTD NO. NAV26MU0800
CONSIGNEE (OR TO ORDER) MTO REGISTRATION NUMBER : MTO/DGS/120251121000012/DEC/2028
TO THE ORDER OF
ATTUJARIWAFA BANK EGYPT S.A.E
NOTIFY PARTY 2 | 37 WORKAFELLA BUSINESS CENTRE
EGYPTIAN ARAB PUMPS CO TAMIL NADU, 600018 INDIA
1349 CORNISH ELNILE ELSAHEL TELEPHONE: +914446945516
CAIRO-EGYPT.
MARKS AND NOS NO AND KIND DESCRIPTION OF PACKAGES AND GOODS
CONTAINER AND SEALS | OF PACKAGES SHIPPER'S LOAD STOW AND COUNT
PACKAGE(S) COUPLING.
"""


NAV26MU0821_OCR = """
--- PAGE 1 ---
Consignee ( If 'To Order')
MTO REGISTRATION NUMBER :MTO/DGS/120251121000012/DEC/2028
AL KAYAN FOR IMPORT AND EXPORT
11 SAAD ZAGHLOL ST, TOREIL AL-GEDIDA
AL-MANSOURA EGYPT
TEL: +20-1028828574
FAX: +20-0502100233
Notify party 1 Notify party 2
SAME AS CONSIGNEE
"""


NAV26MU0714_OCR = """
--- PAGE 1 ---
MULTI-MODAL TRANSPORT DOCUMENT
MTD No: NAV26MU0714
Consignor
INVENTAA INDUSTRIES PRIVATE LIMITED
Consignee (If 'To Order')
VLD HEALTHCARE
C138 TRIVIUM SQUARE 5TH SETTLEMENT 11865 CAIRO, EGYPT
Notify party 1
VLD HEALTHCARE
BYTEPORT LOGISTICS TECHNOLOGIES PRIVATE LIMITED
37 WORKAFELLA BUSINESS CENTRE, TTK ROAD, ALWARPET, CHENNAI
TELEPHONE: +914446945516
www.navexel.com | info@navexel.com
Destination Agent Address
MARINE & ENGINEERING SERIVCE COMPANY
BAB SHARQ - ALEXANDRIA, 8 PATRICE LUMUMBAST
Place and Date of Issue
27/03/2026
Freight Amount For BYTEPORT LOGISTICS TECHNOLOGIES PRIVATE LIMITED
AUTHORISED SIGNATURE
"""


def test_nav26mu0714_byteport_issuing_agent():
    assert "BYTEPORT" in (extract_issuing_agent(NAV26MU0714_OCR) or "").upper()
    data = enrich_bl_from_pdf_text(
        {"mesco_consigneenamecontactno": "VLD HEALTHCARE"},
        NAV26MU0714_OCR,
    )
    assert "BYTEPORT" in (data.get("mesco_agent") or "").upper()
    assert "MESCO" not in (data.get("mesco_agent") or "").upper()

    crm = records_to_master_json([data])
    assert "BYTEPORT" in (crm.get("mesco_agent") or "").upper()


def test_nav26mu0821_mtd_consignee_address():
    block = extract_consignee_block(NAV26MU0821_OCR)
    assert "KAYAN" in (block.get("name") or "").upper()
    assert "SAAD ZAGHLOL" in (block.get("address") or "").upper()
    assert "MANSOURA" in (block.get("address") or "").upper()

    long_agent = (
        "MARINE & ENGINEERING SERIVCE COMPANY, BAB SHARQ - ALEXANDRIA, "
        "8 PATRICE LUMUMBAST, ALEXANDRIA, ALEXANDRIA GOVERNORATE, EGYPT"
    )
    from pdf_bl_enrichment import normalize_delivery_address_for_crm
    from dataverse_uploader import _preprocess_payload, _ENTITY

    assert normalize_delivery_address_for_crm(long_agent, destination="ALEXANDRIA") == "ALEXANDRIA"
    fields = _preprocess_payload(
        {"mesco_deliveryaddress": long_agent, "mesco_masterblno": "NAV26MU0821"},
        _ENTITY,
        client=None,
    )
    assert fields.get("mesco_deliveryaddress") is None or len(fields["mesco_deliveryaddress"]) <= 100


def test_nav26mu0800_to_order_consignee_no_packages_address():
    block = extract_consignee_block(NAV26MU0800_OCR)
    assert "ATTUJARIWAFA" in (block.get("name") or "").upper()
    assert block.get("address") is None
    assert sanitize_party_address("packages") is None

    data = enrich_bl_from_pdf_text(
        {
            "mesco_consigneenamecontactno": "ATTUJARIWAFA BANK EGYPT S.A.E",
            "mesco_consigneeaddress": "packages",
        },
        NAV26MU0800_OCR,
    )
    assert "TO THE ORDER OF" in data["mesco_consigneenamecontactno"].upper()
    assert data.get("mesco_consigneeaddress") is None

    crm = records_to_master_json([data])
    assert crm.get("mesco_consigneeaddress") in (None, "")


if __name__ == "__main__":
    test_is_mtd_document()
    test_mtd_cargo_description()
    test_extract_cargo_prefers_mtd()
    print("All MTD cargo tests passed.")
