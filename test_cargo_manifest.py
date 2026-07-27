"""Regression tests for the labelled HBL: cargo-manifest parser.

Reproduces the NSA26030217 consolidated LCL manifest layout (5 house bills under
one master B/L) and asserts each house keeps its OWN consignee, goods
description, HS code, weight, volume and package count — the exact failure that
produced "Number of" consignees, copied descriptions and date-shaped volumes.
"""

from crm_output_formatter import records_to_house_json, records_to_master_json
from pdf_cargo_manifest import (
    is_cargo_manifest_hbl_blocks,
    parse_cargo_manifest_hbl_blocks,
)

# Mirrors the BLOCK-ORDER text the PDF extractor produces (pages repeat 1-3 as 4-6).
RAW = """--- PAGE 1 ---
[BLOCK ORDER]
CARGO MANIFEST
MASTER B/L VESSEL NAME DATE PLACE OF DISCHARGE POL POD Cont.Type
NSA26030217 VIVIEN A 13/04/2026 Alexandria, Egypt (old) Nhava Sheva Alexandria, Egypt (old) 1x40HC
HBL: NAV26MU0714
CNEE: Marine And Engineering
Services Company
80 DRUMS
STC
08 PALLETS STC 80 DRUMS
TRIMETHOPRIM EP
HS CODE: 29335995
NET WT: 2000.00 KGS
2299.6
KGM
7.928 CBM
TRKU4457572
POL: Nhava Sheva
Seal No: 18874
Store Location :
WAREHOUSE MERGHEM

HBL: NAV26MU0821
CNEE: EURO Shipping Egypt
320 PALLET
STC
01-18 WOODEN PALLETS
CARVACROL OIL OREGANO OIL CLOVE OIL
HS CODE: 29071190, 33012990, 33012921
9200 KGM
27.085
CBM
TRKU4457572
POL: Nhava Sheva
Seal No: 18874
Store Location :
WAREHOUSE MERGHEM

HBL: NAV26MU0795
CNEE: Marine And Engineering
Services Company
26 DRUMS
STC
7 (SEVEN) PALLETS STC. 26 DRUMS
CELLULOSE ACETATE PHTHALATE EP 11.8
H.S. CODE : 3912.9090
893 KGM
5.284 CBM
TRKU4457572
POL: Nhava Sheva
Seal No: 18874
Store Location :
WAREHOUSE MERGHEM

HBL: NAV26MU0794
CNEE: EURO Shipping Egypt
7 PACKAGE
STC
7 PACKAGES
MOULDED RUBBER PRODUCTS V-BELTS GEAR COUPLINGS
HS CODE: 4016.99.90, 4010.39.99, 8483.60.10
2019.15
KGM
6.85 CBM
TRKU4457572
POL: Nhava Sheva
Seal No: 18874
Store Location :
WAREHOUSE MERGHEM

HBL: NAV26MU0800
CNEE: Marine And Engineering
Services Company
34 BOX
STC
TOTAL 34 BOXES ONLY
COUPLING.
9524 KGM
10.715
CBM
TRKU4457572
POL: Nhava Sheva
Seal No: 18874
Store Location :
WAREHOUSE MERGHEM
Totals : No. Of Bills : 5 No. Of Packages : 467 Weight : 23935.750 KGM Volume : 57.862 CBM
"""


def _houses_by_hbl(result):
    return {h["hbl"]: h for h in result}


def test_detects_hbl_block_layout():
    assert is_cargo_manifest_hbl_blocks(RAW)


def test_parses_all_five_houses():
    result = parse_cargo_manifest_hbl_blocks(RAW)
    assert result is not None
    assert result["house_bl_numbers"] == [
        "NAV26MU0714",
        "NAV26MU0821",
        "NAV26MU0795",
        "NAV26MU0794",
        "NAV26MU0800",
    ]


def test_per_house_weight_volume_packages_are_distinct():
    houses = {h["mesco_houseblno"]: h for h in parse_cargo_manifest_hbl_blocks(RAW)["house_records"]}

    assert houses["NAV26MU0714"]["cr401_totalgrossweight"] == 2299.6
    assert houses["NAV26MU0714"]["cr401_totalvolume"] == 7.928
    assert houses["NAV26MU0714"]["cr401_totalpackages"] == 80

    assert houses["NAV26MU0821"]["cr401_totalgrossweight"] == 9200
    assert houses["NAV26MU0821"]["cr401_totalvolume"] == 27.085
    assert houses["NAV26MU0821"]["cr401_totalpackages"] == 320

    assert houses["NAV26MU0795"]["cr401_totalvolume"] == 5.284
    assert houses["NAV26MU0794"]["cr401_totalvolume"] == 6.85
    assert houses["NAV26MU0800"]["cr401_totalgrossweight"] == 9524


def test_per_house_consignee_and_hscode_not_contaminated():
    houses = {h["mesco_houseblno"]: h for h in parse_cargo_manifest_hbl_blocks(RAW)["house_records"]}

    # Never the garbage "Number of" from the column header.
    for rec in houses.values():
        assert rec["mesco_consigneenamecontactno"] != "Number of"

    assert houses["NAV26MU0714"]["mesco_consigneenamecontactno"] == "Marine And Engineering Services Company"
    assert houses["NAV26MU0821"]["mesco_consigneenamecontactno"] == "EURO Shipping Egypt"

    # HS codes stay per-house (not merged across all five).
    assert houses["NAV26MU0714"]["mesco_hscode"] == "29335995"
    assert "3912.9090" in houses["NAV26MU0795"]["mesco_hscode"]
    assert "29335995" not in houses["NAV26MU0795"]["mesco_hscode"]


def test_per_house_descriptions_are_distinct():
    houses = {h["mesco_houseblno"]: h for h in parse_cargo_manifest_hbl_blocks(RAW)["house_records"]}
    assert "TRIMETHOPRIM" in houses["NAV26MU0714"]["mesco_cargodescription"].upper()
    assert "CELLULOSE" in houses["NAV26MU0795"]["mesco_cargodescription"].upper()
    assert "RUBBER" in houses["NAV26MU0794"]["mesco_cargodescription"].upper()
    # House 0795 must NOT carry house 0714's goods.
    assert "TRIMETHOPRIM" not in houses["NAV26MU0795"]["mesco_cargodescription"].upper()


def test_master_header_fields():
    master = parse_cargo_manifest_hbl_blocks(RAW)["master_record"]
    assert master["mesco_masterblno"] == "NSA26030217"
    assert master["mesco_vessel"] == "VIVIEN A"
    assert master["mesco_origin"] == "Nhava Sheva"
    assert "Alexandria" in master["mesco_destination"]
    assert master["mesco_consolidation"] is True
    assert master["mesco_nooforgbls"] == "5"
    assert master["cr401_totalgrossweight"] == 23935.75
    assert master["cr401_totalvolume"] == 57.862
    assert master["cr401_totalpackages"] == 467


def test_real_nsa26030217_pdf_per_house_totals():
    """Regression: column-interleaved OCR on the physical manifest PDF."""
    from pathlib import Path

    pdf_path = Path(__file__).resolve().parent / "ManifestNSA26030217.pdf"
    if not pdf_path.is_file():
        return
    from pdf_extractor import extract_pdf_text_professionally

    raw = extract_pdf_text_professionally(pdf_path.read_bytes())["text"]
    houses = {
        h["mesco_houseblno"]: h
        for h in parse_cargo_manifest_hbl_blocks(raw)["house_records"]
    }
    assert houses["NAV26MU0714"]["cr401_totalgrossweight"] == 2299.6
    assert houses["NAV26MU0821"]["cr401_totalgrossweight"] == 9200
    assert houses["NAV26MU0795"]["cr401_totalgrossweight"] == 893
    assert houses["NAV26MU0794"]["cr401_totalgrossweight"] == 2019.15
    assert houses["NAV26MU0800"]["cr401_totalvolume"] == 10.715


def test_crm_master_aggregates_and_houses_have_cargo():
    result = parse_cargo_manifest_hbl_blocks(RAW)
    houses = result["house_records"]
    master = result["master_record"]

    crm_master = records_to_master_json(houses, master_record=master)
    assert crm_master["mesco_masterblno"] == "NSA26030217"
    # Aggregated totals reconstruct the manifest "Totals" line.
    assert crm_master["cr401_totalgrossweight"] == 23935.75
    assert round(crm_master["cr401_totalvolume"], 3) == 57.862
    assert crm_master["cr401_totalpackages"] == 467
    # One shared container, so a single TEU.
    assert crm_master["cr401_totalteus"] == 1
    # Master carries one cargo row per house (not one giant blob).
    cargo_key = next(k for k in crm_master if k.startswith("mesco_Cargo_MasterOperation"))
    assert len(crm_master[cargo_key]) == 5

    house_json = records_to_house_json(houses, master_record=master)
    assert len(house_json["value"]) == 5
    for entry in house_json["value"]:
        cargo_key = next(k for k in entry if k.startswith("mesco_Cargo_HouseOperation"))
        # Each house now has its OWN cargo line (the original bug: empty []).
        assert len(entry[cargo_key]) == 1
