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


TPALX_OCR_LABELLED_SAMPLE = """
--- PAGE 1 ---
[OCR HEADER PSM6]
ORIGINAL
CARRIER: TRANS PACIFIC CARGO LIMITED (SHENZHEN BILL OF LADING For Multimodal Transport or Port to Port Shipments
SHIPPER (COMPLETE NAME / STREET ADDRESS)
LANBO TONGCHUANG APPLIANCE CO.,LTD
TD: 913204120710940674 TP CARGO
LUOYANG INDUSTRIAL PARK WUJIN DISTRICT
CHANGZHOU JIANGSU CHINA
CARRIER REFERENCE B/L. NO. PAGE
TPALX2603001 1/1
CONSIGNEE (NOT NEGOTIABLE UNLESS CONSIGNED TO ORDER) EXPORTER REFERENCE
NILE TRADING COMPANY
26 NAGYB EL REHANY ST CAIRO, EGYPT
TEL :+201227548280
TAX ID.200522469 CONSIGNEE REFERENCE

[OCR BODY PSM4]
NOTIFY PARTY (COMPLETE NAME / STREET ADDRESS) FOR DELIVERY OF GOODS PLEASE APPLY TO
SAME AS CONSIGNEE MARINE &ENGINEERING SERVICESCOMPANY -MESCO
8 PATRIC LUMUMBA ST.BAB SHARQ
ALEXANDRIA -EGYPT
TEL.+2(03)3991000 FAX.+2(03)3991001
PRE-CARRIAGE BY (MODE) PLACE OF RECEIPT ROUTING & INSTRUCTIONS
SHANGHAI, CHINA
OCEAN VESSEL / VOYAGE PORT OF LOADING PORT OF DISCHARGING PLACE OF DELIVERY
CMA CGM SAO PAULO
OBENSW1MA SHANGHAI , CHINA ALEXANDRIA, EGYPT ALEXANDRIA, EGYPT
BELOW PARTICULARS OF THE GOODS DECLARED BY THE SHIPPER AND UNKNOWN TO THE CARRIER
Container Nos., Seal Nos., Marks, and Nos. Numbers and Kind of Packages, Description of Goods Gross Weight Measurements
N/M SAID TO CONTAINE: 10 PALLETS IN TOTAL 5793 21
MOTOR
HS CODE: 8501409090
ACID :2005224691002910019
EGYPTIAN IMPORTER TAX ID: 200522469
FOREIGN EXPORTER REGISTRATION TYPE: VAT NUMBER
FOREIGN EXPORTER ID: 913204120710940674
FOREIGN EXPORTER COUNTRY: CHINA
FOREIGN EXPORTER COUNTRY CODE: CN
CSNU6873347/CW794147/40HO
21CBM ONLY CFS/CFS
CHARGE PREPAID | COLLECT
ASIARRANGED
FREIGHT PAYABLE AT
COLLECT
PLACE AND DATE OF ISSUE
SHENZHEN, CHINA
LADEN ON BOARD DATE
16 MAR 2026
16 MAR 2026
AS AGENT TO CARRIER

[OCR FULL PAGE BEST]
ORIGINAL
SHIPPER (COMPLETE NAME / STREET ADDRESS)
LANBO TONGCHUANG APPLIANCE CO.,LTD
ID: 913204120710940674 TP CARGO
LUOYANG INDUSTRIAL PARK WUJIN DISTRICT
CHANGZHOU JIANGSU CHINA
B/L. NO.
TPALX2603001
CONSIGNEE (NOT NEGOTIABLE UNLESS CONSIGNED TO ORDER) REFERENCE
NILE TRADING COMPANY
26 NAGYB EL REHANY ST CAIRO, EGYPT
TEL :+201227548280
NOTIFY PARTY (COMPLETE NAME / STREET ADDRESS) FOR DELIVERY OF GOODS PLEASE APPLY TO
SAME AS CONSIGNEE MARINE &ENGINEERING SERVICESCOMPANY -MESCO
SHANGHAI, CHINA
OCEAN VESSEL / VOYAGE PORT OF LOADING PORT OF DISCHARGING PLACE OF DELIVERY
CMA CGM SAO PAULO
OBENOW1MA SHANGHAT , CHINA ALEXANDRIA, EGYPT
ALEXANDRIA, EGYPT
N/M SAID TO CONTAINE: 10 PALLETS IN TOTAL 5793 21
MOTOR
HS CODE: 8501409090
CSNU6873347/CW794147/40HO
LADEN ON BOARD DATE
16 MAR 2026
16 MAR 2026
"""


def test_detects_standard_tpalx_house_bl():
    assert is_standard_house_bl(TPALX_SAMPLE)


def test_parses_standard_tpalx_house_bl_evidence_without_llm():
    rec = parse_standard_house_bl(TPALX_SAMPLE)
    assert rec
    assert rec["mesco_houseblno"] == "TPALX2602005"
    assert "mesco_masterblno" not in rec
    assert rec["mesco_agent"] == "TRANS PACIFIC CARGO LIMITED (SHENZHEN)"
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
    assert house["mesco_agent"] == "TRANS PACIFIC CARGO LIMITED (SHENZHEN)"
    containers = house["mesco_Container_mesco_houses"]
    assert containers[0]["mesco_containernumber"] == "CSNU6873347"
    assert containers[0]["mesco_carrierseal"] == "CW794147"
    assert containers[0]["mesco_warehouse"] == "MERGHEM"
    assert containers[0]["mesco_sendtowarehouse"] is True
    assert house["mesco_notify1"] == "Same As Consignee"
    assert house["mesco_atadestination"] == "2026-03-17"
    cargo = house["mesco_Cargo_HouseOperation_mesco_Operation"][0]
    assert cargo["mesco_umpackages"] == "PACKAGES"
    assert cargo["mesco_descriptionofgoods"].startswith("S.T.C")


def test_parses_ocr_labelled_tpalx_house_bl_for_master_linking():
    rec = parse_standard_house_bl(TPALX_OCR_LABELLED_SAMPLE)
    assert rec
    assert rec["mesco_houseblno"] == "TPALX2603001"
    assert rec["mesco_agent"] == "TRANS PACIFIC CARGO LIMITED (SHENZHEN)"
    assert rec["mesco_shippernamecontactno"] == "LANBO TONGCHUANG APPLIANCE CO.,LTD"
    assert rec["mesco_shipper"] == "LANBO TONGCHUANG APPLIANCE CO.,LTD"
    assert rec["mesco_consigneenamecontactno"] == "NILE TRADING COMPANY"
    assert rec["mesco_consignee"] == "NILE TRADING COMPANY"
    assert rec["mesco_consigneeaddress"] == "26 NAGYB EL REHANY ST CAIRO, EGYPT"
    assert rec["mesco_notify1"] == "SAME AS CONSIGNEE"
    assert rec["mesco_notifyaddress"] == "26 NAGYB EL REHANY ST CAIRO, EGYPT"
    assert rec["mesco_vessel"] == "CMA CGM SAO PAULO"
    assert rec["mesco_voytruckno"] == "0BEN9W1MA"
    assert rec["mesco_origin"] == "SHANGHAI, CHINA"
    assert rec["mesco_destination"] == "ALEXANDRIA, EGYPT"
    assert rec["container_number"] == "CSNU6873347"
    assert rec["seal_number"] == "CW794147"
    assert rec["mesco_containertype"] == "40HQ"
    assert rec["cr401_totalpackages"] == 10
    assert rec["cr401_totalgrossweight"] == 5793.0
    assert rec["cr401_totalvolume"] == 21.0
    assert rec["mesco_umpackages"] == "PALLETS"
    assert "MOTOR" in rec["mesco_cargodescription"]

    house = records_to_house_json([rec])["value"][0]
    assert house["mesco_consignee"] == "NILE TRADING COMPANY"
    assert house["mesco_notify1"] == "Same As Consignee"
    assert house["mesco_Container_mesco_houses"][0]["mesco_containernumber"] == "CSNU6873347"
    assert house["mesco_Container_mesco_houses"][0]["mesco_carrierseal"] == "CW794147"
    assert house["mesco_Cargo_HouseOperation_mesco_Operation"][0]["mesco_umpackages"] == "PALLETS"
    assert house["mesco_Cargo_HouseOperation_mesco_Operation"][0]["mesco_descriptionofgoods"].startswith("S.T.C")
