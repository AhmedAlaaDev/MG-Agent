"""Tests for EXPORT LCL MANIFEST PDF parsing."""

from pdf_lcl_export_manifest import (
    is_export_lcl_manifest,
    parse_export_lcl_manifest,
    parse_manifest_rows,
    parse_manifest_totals,
    _hbl_column_order,
    _parse_page1_rows,
    _parse_page_rows,
    _visual_section,
)


def test_detect_manifest():
    text = "EXPORT LCL MANIFEST\nISTALY-260127\nMARINE & ENGINEERING SERVICES"
    assert is_export_lcl_manifest(text)


def test_page1_weights():
    block = """
Consignee :
TO THE ORDER OF COMMERCIAL INTERNATIONAL
44 PALLETS
Consignee :
EGYPTIAN MANUFACTURING AND ADVANCED
12 PACKAGES
Consignee :
EL SEWEDY FOR ENGINEERING INDUSTRIES
18 COLLIES
Measurment
40,74
1,00
162,70
Weight
17697,00
238,35
EXPORT LCL MANIFEST
Marks & Number
ISTALY-260127
Shipper :
PALS AYDINLATMA
Shipper :
MATEC ELEKTRONIK
Shipper :
SENTES-BIR METALURJI
Shipper :
FEDERAL ELEKTRIK YATIRIM VE TICARET A.S.
"""
    rows = {r["hbl"]: r for r in _parse_page1_rows(block)}
    assert rows["ISTALY-260127"]["gross_weight_kg"] == 17697.0
    assert rows["ISTALY-260128"]["gross_weight_kg"] == 162.70
    assert rows["ISTALY-260129"]["gross_weight_kg"] == 238.35
    assert rows["ISTALY-260127"]["packages"] == "44"
    assert "FEDERAL" in rows["ISTALY-260127"]["shipper"].upper()


def test_manifest_totals_pattern():
    block = "177,96\n55460,19\n290\nISTALY-260146"
    t = parse_manifest_totals(block)
    assert t.get("packages") == 290
    assert t.get("gross_weight_kg") == 55460.19
    assert abs(t.get("measurement_cbm", 0) - 177.96) < 0.01


def test_eglv_not_manifest():
    assert not is_export_lcl_manifest("B/L NO. EGLV142600454782\nATTACHED LIST PAGE")


def test_hbl_column_order_from_visual():
    text = (
        "--- PAGE 1 ---\n[BLOCK ORDER]\n"
        "--- PAGE 2 ---\n[VISUAL WORD ORDER]\n"
        "1 2 1\nISTALY-260130 ISTALY-260131 ISTALY-260132\n"
        "[BLOCK ORDER]\nISTALY-260133\n"
    )
    order = _hbl_column_order("ISTALY-260133\n", text, 2)
    assert order == ["ISTALY-260130", "ISTALY-260131", "ISTALY-260132"]


def test_page5_orion_shipper():
    block = """
SHPR: FORM KORUYUCU
SHPR: AVRASYA KABLAJ
SHPR: BIMEKS TEL
SHPR: ORION ELEKTRIK
Consignee :
LECICO EGYPT S.A.E.
10 PALLETS
Consignee :
INTERNATIONAL ENGINEERING UNION FOR
2 PACKAGES
Consignee :
AL SAAD FOR ORGANIC FERTILIZERS
3 CASES
Consignee :
EGYPTIAN MANUFACTURING & ADVANCED
1 PALLET
ISTALY-260143
ISTALY-260144
ISTALY-260145
ISTALY-260146
200,20
"""
    full = (
        "--- PAGE 1 ---\n[BLOCK ORDER]\n"
        "--- PAGE 5 ---\n[VISUAL WORD ORDER]\n"
        "2469,00 2785,00 3357,00 200,20\n"
        "15,90 24,17 3,36 2,54\n"
        "10 PP\n2 3 1\n"
        "ISTALY-260143 ISTALY-260144 ISTALY-260145 ISTALY-260146\n"
        "[BLOCK ORDER]\n" + block
    )
    rows = {r["hbl"]: r for r in _parse_page_rows(block, 4, full)}
    assert "ORION" in rows["ISTALY-260146"].get("shipper", "").upper()
