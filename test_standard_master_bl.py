"""Tests for no-LLM standard master B/L extraction."""

from crm_output_formatter import (
    MASTER_CARGO_KEY,
    MASTER_CONTAINERS_KEY,
    MASTER_HOUSES_KEY,
    records_to_master_json,
)
from custom_business_rules import use_custom_rules
import pdf_batch_processor as pdf_batch_processor_module
from pdf_deterministic_registry import best_deterministic_parse
from pdf_standard_master_bl import is_standard_master_bl, parse_standard_master_bl
from validator import validate_and_correct


CMA_CGM_MASTER_SAMPLE = """
--- PAGE 1 ---
[VISUAL WORD ORDER]
VOYAGE NUMBER
SHIPPER
TRANS PACIFIC CARGO 0BEN9W1MA
LIMITED(SHENZHEN) COPY NON NEGOTIABLE
2701 SHENNAN 1001,SHENNAN EAST BILL OF LADING NUMBER
ROAD, BILL OF LADING
SHZ7922638
LUOHU DISTRICT,SHENZHEN,CHINA*
CONSIGNEE EXPORT REFERENCES
MARINE&ENGINEERING SERVICES
COMPANY - MESCO
8 PATRICE LUMUMBA ST.BAB SHARQ
ALEXANDRIA - EGYPT
TEL.+2(03)3991000**
NOTIFY PARTY, Carrier not to be responsible for failure to notify
MARINE&ENGINEERING SERVICES
COMPANY - MESCO
CARRIER: CMA CGM Societe Anonyme au Capital de 234 988 330 Euros
8 PATRICE LUMUMBA ST.BAB SHARQ
ALEXANDRIA - EGYPT Tel: (33) 4 88 91 90 00
PRE CARRIAGE BY* PLACE OF RECEIPT* FREIGHT TO BE PAID AT NUMBER OF ORIGINAL BILLS OF LADING
SHENZHEN THREE (3)
VESSEL PORT OF LOADING PORT OF DISCHARGE FINAL PLACE OF DELIVERY*
CMA CGM SAO PAULO SHEKOU ALEXANDRIA ***********************
MARKS AND NOS NO AND KIND DESCRIPTION OF PACKAGES AND GOODS AS STATED BY SHIPPER GROSS WEIGHT TARE MEASUREMENT
CONTAINER AND SEALS OF PACKAGES SHIPPER'S LOAD STOW AND COUNT SAID TO CONTAIN CARGO
KGS KGS CBM
TLLU4178846 1 x 40HC 179 PACKAGE(S) 14801.730 3900 50.657
SEAL M9342681
N/M IC902 CONTROLLER OF PCB CONTROLLER
220V
INDUSTRIAL CASTORS FOR MACHINERY
HS CODE:832200090
Continued on Next Sheet Sheet 1 of 2
PLACE AND DATE OF ISSUE NINGBO 19 MAR 2026
BY CMA CGM Ningbo

[BLOCK ORDER]
VOYAGE NUMBER
0BEN9W1MA
BILL OF LADING NUMBER
BILL OF LADING
SHZ7922638
PRE CARRIAGE BY*
PLACE OF RECEIPT*
FREIGHT TO BE PAID AT
NUMBER OF ORIGINAL BILLS OF LADING
THREE (3)
SHENZHEN
VESSEL
PORT OF LOADING
FINAL PLACE OF DELIVERY*
PORT OF DISCHARGE
CMA CGM SAO PAULO
SHEKOU
ALEXANDRIA
***********************
MARKS AND NOS

--- PAGE 2 ---
[VISUAL WORD ORDER]
VOYAGE NUMBER
0BEN9W1MA
COPY NON NEGOTIABLE
BILL OF LADING NUMBER
BILL OF LADING
SHZ7922638
VESSEL PORT OF LOADING PORT OF DISCHARGE FINAL PLACE OF DELIVERY*
CMA CGM SAO PAULO SHEKOU ALEXANDRIA ***********************
MARKS AND NOS NO AND KIND DESCRIPTION OF PACKAGES AND GOODS AS STATED BY SHIPPER GROSS WEIGHT TARE MEASUREMENT
CONTAINER AND SEALS OF PACKAGES SHIPPER'S LOAD STOW AND COUNT SAID TO CONTAIN CARGO
KGS KGS CBM
DOCTOR BLADE
AUTOMOBILE PARTS
SOLID WAX
HS CODE:3407001000
ARTICLES OF PLASTICS
275CM*122CM/SHEET
H.S CODE : 39269090
INDUCTION WOK RANGE
ELECTRIC S/U STAMER
HS CODE:84198100
CONSOLIDATED CARGO
CARGO IN TRANSIT TO MERGHEM BONDED
WAREHOUSE AT CARGO RECEIVER'S
RISKS, CARES AND EXPENSES, LINE'S
RESPONSIBILITY CEASES AT PORT OF
DISCHARGE.
ACID:2979239001004410035
EGYPTIAN FREIGHT FORWARDER TAX ID:
297923900
FOREIGN FREIGHT FORWARDER
REGISTRATION TYPE: VAT NUMBER
FOREIGN FREIGHT FORWARDER ID:
91440300MA5EJFER3Y
FOREIGN FREIGHT FORWARDER COUNTRY:
CHINA
FOREIGN FREIGHT FORWARDER COUNTRY
CODE: CN
1 X 40HC
179 PACKAGE(S)
SAY ONE HUNDRED SEVENTY-NINE PACKAGE(S)
Shipped on Board CMA CGM SAO PAULO 19-MAR-2026 CMA CGM CHINA
SHIPPING CO. LTD As agents for the Carrier
Weight in Kgs Total: 1 CONTAINER(S) Continued From Previous Sheet Sheet 2 of 2 14801.730 3900 50.657
SIGNED FOR THE CARRIER CMA CGM S.A.
PLACE AND DATE OF ISSUE NINGBO 19 MAR 2026
BY CMA CGM Ningbo
"""


VISUAL_ONLY_CMA_CGM_ROUTE_SAMPLE = """
--- PAGE 1 ---
[VISUAL WORD ORDER]
VOYAGE NUMBER
0BEN9W1MA
COPY NON NEGOTIABLE
BILL OF LADING NUMBER
BILL OF LADING
SHZ7922638
VESSEL PORT OF LOADING PORT OF DISCHARGE FINAL PLACE OF DELIVERY*
CMA CGM SAO PAULO SHEKOU ALEXANDRIA ***********************
"""


def test_standard_master_detects_and_parses_cma_cgm_mbl():
    assert is_standard_master_bl(CMA_CGM_MASTER_SAMPLE)

    rec = parse_standard_master_bl(CMA_CGM_MASTER_SAMPLE)
    assert rec
    assert rec["mesco_masterblno"] == "SHZ7922638"
    assert rec["mesco_bookingnumber"] == "SHZ7922638"
    assert rec["mesco_shippingline"] == "CMA CGM"
    assert rec["mesco_agent"] == "CMA CGM Ningbo"
    assert rec["mesco_vessel"] == "CMA CGM SAO PAULO"
    assert rec["mesco_voytruckno"] == "0BEN9W1MA"
    assert rec["mesco_origin"] == "SHEKOU"
    assert rec["mesco_destination"] == "ALEXANDRIA"
    assert rec["mesco_pickupaddress"] == "SHENZHEN"
    assert rec["container_number"] == "TLLU4178846"
    assert rec["seal_number"] == "M9342681"
    assert rec["mesco_containertype"] == "40HC"
    assert rec["cr401_totalpackages"] == 179
    assert rec["cr401_totalgrossweight"] == 14801.73
    assert rec["cr401_totalvolume"] == 50.657
    assert rec["mesco_nooforgbls"] == "3"
    assert rec["mesco_dateofissue"] == "2026-03-19"
    assert rec["mesco_shippedonboarddate"] == "2026-03-19"
    assert rec["mesco_pcfreightterm"] == "PREPAID"
    assert rec["mesco_freightpayableat"] == "Origin"
    assert rec["mesco_bookingterm"] == 886150000
    assert rec["mesco_importerstaxno"] == "297923900"
    assert rec["mesco_foreignsupplierregistrationnumber"] == "91440300MA5EJFER3Y"
    assert rec["mesco_typeofregistrationnumber"] == "Tax Number"
    assert rec["mesco_country"] == "China"
    assert rec["mesco_countryoforigin"] == "China"


def test_standard_master_parses_visual_only_cma_cgm_route():
    rec = parse_standard_master_bl(VISUAL_ONLY_CMA_CGM_ROUTE_SAMPLE)

    assert rec
    assert rec["mesco_masterblno"] == "SHZ7922638"
    assert rec["mesco_vessel"] == "CMA CGM SAO PAULO"
    assert rec["mesco_origin"] == "SHEKOU"
    assert rec["mesco_destination"] == "ALEXANDRIA"
    assert rec["mesco_deliveryaddress"] == "ALEXANDRIA"


def test_standard_master_registry_fallback_has_no_llm_dependency():
    deterministic = best_deterministic_parse(CMA_CGM_MASTER_SAMPLE)
    assert deterministic
    assert deterministic.parser == "pdf_standard_master_bl"
    assert deterministic.layout == "single_bl"
    assert deterministic.records[0]["mesco_masterblno"] == "SHZ7922638"


def test_standard_master_validation_includes_continuation_page_cargo():
    rec = parse_standard_master_bl(CMA_CGM_MASTER_SAMPLE)
    validated = validate_and_correct(
        rec,
        CMA_CGM_MASTER_SAMPLE,
        enrichment_text=CMA_CGM_MASTER_SAMPLE,
    )

    cargo = (validated.get("mesco_cargodescription") or "").upper()
    assert "INDUSTRIAL CASTORS" in cargo
    assert "DOCTOR BLADE" in cargo
    assert "AUTOMOBILE PARTS" in cargo
    assert "INDUCTION WOK RANGE" in cargo
    hs_codes = (validated.get("mesco_hscode") or "").replace(" ", "")
    for code in ("832200090", "3407001000", "39269090", "84198100"):
        assert code in hs_codes


def test_standard_master_crm_payload_keeps_master_teus_and_nested_rows():
    rec = validate_and_correct(
        parse_standard_master_bl(CMA_CGM_MASTER_SAMPLE),
        CMA_CGM_MASTER_SAMPLE,
        enrichment_text=CMA_CGM_MASTER_SAMPLE,
    )

    with use_custom_rules(True):
        master = records_to_master_json([rec])

    assert master["mesco_bltype"] == 886150001
    assert master["cr401_totalteus"] == 1
    assert master[MASTER_HOUSES_KEY] == []
    assert len(master[MASTER_CONTAINERS_KEY]) == 1
    assert len(master[MASTER_CARGO_KEY]) == 1
    assert master[MASTER_CONTAINERS_KEY][0]["mesco_containernumber"] == "TLLU4178846"
    assert master[MASTER_CARGO_KEY][0]["mesco_noofpackages"] == 179


def test_batch_processor_routes_standard_master_directly(monkeypatch):
    def fake_extract_document_text(file_bytes, filename):
        return {
            "method": "native",
            "text": CMA_CGM_MASTER_SAMPLE,
            "quality": {
                "native_char_count": len(CMA_CGM_MASTER_SAMPLE),
                "native_field_hits": 19,
                "page_count": 2,
                "warnings": [],
            },
        }

    def fail_intelligent_parse(*args, **kwargs):
        raise AssertionError("standard master B/L should not require LLM fallback")

    monkeypatch.setattr(
        pdf_batch_processor_module,
        "extract_document_text_professionally",
        fake_extract_document_text,
    )
    monkeypatch.setattr(
        pdf_batch_processor_module,
        "parse_document_intelligently",
        fail_intelligent_parse,
    )

    result = pdf_batch_processor_module.process_pdf_bytes(
        b"%PDF-1.7\n",
        "SHZ7922638.pdf",
    )

    assert result.success
    assert result.passed
    assert result.records_summary[0]["mesco_masterblno"] == "SHZ7922638"
    assert result.records_summary[0]["mesco_vessel"] == "CMA CGM SAO PAULO"
    assert result.extraction_quality["record_routing"]["policy"] == "pdf_standard_master_bl"
    crm_master = result.crm_masters[0]
    assert crm_master["mesco_pcfreightterm"] == "PREPAID"
    assert crm_master["mesco_freightpayableat"] == "Origin"
    assert crm_master["mesco_bookingterm"] == 886150000
    assert crm_master["mesco_importerstaxno"] == "297923900"
    assert crm_master["mesco_foreignsupplierregistrationnumber"] == "91440300MA5EJFER3Y"
    assert crm_master["mesco_typeofregistrationnumber"] == "Tax Number"
    assert crm_master["mesco_country"] == "China"
