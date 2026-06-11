"""Tests for standard ocean B/L cargo description extraction (goods table layout)."""

from crm_output_formatter import _build_cargo_from_record
from pdf_bl_enrichment import extract_cargo_description_from_pdf, extract_hs_codes_from_goods

ALMOND_BL = """
--- PAGE 1 ---
[VISUAL WORD ORDER]
PORT OF DISCHARGE ALEXANDRIA
MARKS AND NUMBERS
NUMBER OF PACKAGES AND DESCRIPTIONS OF GOODS
GROSS WEIGHT MEASUREMENT
40 HC-GAOU6839174-
SEAL NO: 0741023
FREIGHT PREPAID
MARKS 45212 : AYDIN KURUYEMIS
4 PALLETS
ROASTED BLANCHED DICED ALMOND KERNEL
SALTED ROASTED BLANCHED DICED ALMOND KERNEL HS CODE:200819128
2 PALLETS - 1.280,00 KGS NET - 1.408,00 KGS GROSS
ROASTED BLANCHED DICED ALMOND KERNEL
SALTED ROASTED BLANCHED DICED ALMOND KERNEL HS CODE:200819128
2 PALLETS - 1.280,00 KGS NET - 1.408,00 KGS GROSS
NON STACKABLE
ACID - 1001206952025010025
EGYPTIAN IMPORTER TAX ID:100120695
FOREIGN EXPORTER
REGISTRATION TYPE: VAT NUMBER
FOREIGN EXPORTER ID:1150832417
FOREIGN EXPORTER COUNTRY: TURKEY
FOREIGN EXPORTER COUNTRY CODE: TR
TELEX RELEASE
PARTICULARS OF GOODS ARE THOSE DECLARED BY SHIPPERS
2816.000 KGS
10.18 CBM
"""


def test_almond_bl_full_cargo_description():
    desc = extract_cargo_description_from_pdf(ALMOND_BL)
    assert desc, "expected a cargo description"
    upper = desc.upper()
    assert "4 PALLETS" in upper
    assert "ROASTED BLANCHED DICED ALMOND KERNEL" in upper
    assert "200819128" in desc
    assert "NON STACKABLE" in upper
    assert "ACID" in upper
    assert "100120695" in desc
    assert "FREIGHT PREPAID" not in upper
    assert "TELEX RELEASE" not in upper
    assert "FOREIGN EXPORTER" not in upper
    assert len(desc) > 120


def test_almond_bl_hs_not_duplicated_in_cargo_row():
    hs = extract_hs_codes_from_goods(ALMOND_BL)
    rec = {
        "mesco_hscode": hs,
        "mesco_cargodescription": extract_cargo_description_from_pdf(ALMOND_BL),
        "cr401_totalpackages": 4,
        "cr401_totalgrossweight": 2816,
        "cr401_totalvolume": 10.18,
    }
    cargo = _build_cargo_from_record(rec)
    goods = cargo.get("mesco_descriptionofgoods") or ""
    assert "HS: 200819128" not in goods
    assert "200819128" in goods
    assert "ROASTED BLANCHED" in goods.upper()


if __name__ == "__main__":
    test_almond_bl_full_cargo_description()
    test_almond_bl_hs_not_duplicated_in_cargo_row()
    print("All standard B/L cargo tests passed.")
