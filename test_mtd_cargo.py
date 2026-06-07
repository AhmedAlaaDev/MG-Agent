"""Tests for MTD (Multi-Modal Transport Document) cargo description extraction."""

from pdf_bl_enrichment import (
    _is_mtd_document,
    extract_cargo_description_from_pdf,
    extract_mtd_cargo_description_from_pdf,
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


if __name__ == "__main__":
    test_is_mtd_document()
    test_mtd_cargo_description()
    test_extract_cargo_prefers_mtd()
    print("All MTD cargo tests passed.")
