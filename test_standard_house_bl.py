"""Tests for no-LLM standard House B/L extraction."""

from crm_output_formatter import records_to_house_json
from pdf_house_bl import is_standard_house_bl, parse_standard_house_bl


TPALX_SAMPLE = """
--- PAGE 1 ---
[VISUAL WORD ORDER]
ORIGINAL
XUCHANG HUARUI MACHINERY CO.,LTD
ADD:NO. 7-S302, XINYUE ROAD, WEIDU DISTRICT,
XUCHANG CITY, HENAN PROVINCE, CHINA
TEL: 0086 374 2339028
FAX: 0086 374 2339029
VAT NUMBER: 914110025651212054 TPALX2602005 1/1
AL SAAD FOR ORGANIC FERTILIZERS MANUFACTURING
ADD:BELBEIS CENTER - GALFINA - TAKSIM AL-SAAD -
AL-SHARQIYYAH
EMAIL:KIZARO.SAN.DA.ANA@GMAIL.COM
TEL:+20101 492 823
SAME AS CONSIGNEE MARINE &ENGINEERING SERVICESCOMPANY -MESCO
8 PATRIC LUMUMBA ST.BAB SHARQ
ALEXANDRIA -EGYPT
TEL.+2(03)3991000 FAX.+2(03)3991001
TAXNO.297923900
SHANGHAI,CHINA
CMA CGM SAO PAULO
0BEN9W1MA SHANGHAI,CHINA ALEXANDRIA,EGYPT ALEXANDRIA,EGYPT
HRLL20251206 SAID TO CONTAINE:5 PACKAGES IN TOTAL 5229 3.2
0.7MM WELDING WIRE
HS CODE: 72173010
ACID:4730547441002310029
EGYPTIAN IMPORTER TAX ID: 473054744
FOREIGN EXPORTER REGISTRATION TYPE: VAT NUMBER
FOREIGN EXPORTER ID: 914110025651212054
FOREIGN EXPORTER COUNTRY: CHINA
FOREIGN EXPORTER COUNTRY CODE: CN
CSNU6873347 /CW794147/40HQ
CARGO IN TRANSIT TO MERGHEM BONDED WAREHOUSE
3.2CBM ONLY CFS/CFS
THREE(3) COLLECT
ATA POD: 17 MAR 2026
SHENZHEN,CHINA 16 MAR 2026
16 MAR 2026
AS AGENT TO CARRIER

[BLOCK ORDER]
TPALX2602005
XUCHANG HUARUI MACHINERY CO.,LTD
AL SAAD FOR ORGANIC FERTILIZERS MANUFACTURING
SHANGHAI,CHINA
CMA CGM SAO PAULO
0BEN9W1MA
SHANGHAI,CHINA
ALEXANDRIA,EGYPT
ALEXANDRIA,EGYPT
HRLL20251206
SAID TO CONTAINE:5 PACKAGES IN TOTAL
5229
3.2
0.7MM WELDING WIRE
CSNU6873347 /CW794147/40HQ
CARGO IN TRANSIT TO MERGHEM BONDED WAREHOUSE
ATA POD: 17 MAR 2026
"""


def test_detects_standard_tpalx_house_bl():
    assert is_standard_house_bl(TPALX_SAMPLE)


def test_parses_standard_tpalx_house_bl_evidence_without_llm():
    rec = parse_standard_house_bl(TPALX_SAMPLE)
    assert rec
    assert rec["mesco_houseblno"] == "TPALX2602005"
    assert "mesco_masterblno" not in rec
    assert rec["mesco_vessel"] == "CMA CGM SAO PAULO"
    assert rec["mesco_voytruckno"] == "0BEN9W1MA"
    assert rec["mesco_origin"] == "SHANGHAI, CHINA"
    assert rec["mesco_destination"] == "ALEXANDRIA, EGYPT"
    assert rec["container_number"] == "CSNU6873347"
    assert rec["seal_number"] == "CW794147"
    assert rec["cr401_totalpackages"] == 5
    assert rec["cr401_totalgrossweight"] == 5229.0
    assert rec["cr401_totalvolume"] == 3.2
    assert rec["mesco_umpackages"] == "PACKAGES"
    assert rec["containers"][0]["mesco_warehouse"] == "MERGHEM"
    assert rec["mesco_notify1"] == "SAME AS CONSIGNEE"
    assert rec["mesco_notifyaddress"] == "ADD:BELBEIS CENTER - GALFINA - TAKSIM AL-SAAD -, AL-SHARQIYYAH"
    assert rec["mesco_atadestination"] == "2026-03-17"
    assert rec["mesco_shippedonboarddate"] == "2026-03-16"
    assert rec["mesco_dateofissue"] == "2026-03-16"
    assert rec["mesco_hscode"] == "72173010"
    assert rec["mesco_acidnumber"] == "4730547441002310029"


def test_house_json_does_not_invent_master_link_for_standard_house():
    rec = parse_standard_house_bl(TPALX_SAMPLE)
    house_json = records_to_house_json([rec])
    house = house_json["value"][0]
    assert house["mesco_masterblno"] == "TPALX2602005"
    assert "mesco_masterbllinkno" not in house
    containers = house["mesco_Container_mesco_houses"]
    assert containers[0]["mesco_containernumber"] == "CSNU6873347"
    assert containers[0]["mesco_carrierseal"] == "CW794147"
    assert containers[0]["mesco_warehouse"] == "MERGHEM"
    assert containers[0]["mesco_sendtowarehouse"] is True
    assert house["mesco_notify1"] == "AL SAAD FOR ORGANIC FERTILIZERS MANUFACTURING"
    assert house["mesco_atadestination"] == "2026-03-17"
    cargo = house["mesco_Cargo_HouseOperation_mesco_Operation"][0]
    assert cargo["mesco_umpackages"] == "PACKAGES"
