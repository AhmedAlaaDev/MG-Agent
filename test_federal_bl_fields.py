"""Tests for ISTALY house B/L field fixes (container dash, notify split)."""

from validator import extract_containers_regex, format_container_number, validate_and_correct
from crm_output_formatter import records_to_master_json
from pdf_bl_enrichment import (
    extract_consignee_from_pdf,
    extract_notify_and_delivery_agent,
    enrich_bl_from_pdf_text,
)


def test_container_dash_format():
    assert format_container_number("TCNU5600976") == "TCNU560097-6"
    assert format_container_number("TCNU 560097-6") == "TCNU560097-6"
    assert format_container_number("TCNU560097/6") == "TCNU560097/6"
    text = "SAID TO CONTAIN:\nTCNU 560097-6 / 40'HC SN: MS068705 44 PALLETS"
    rows = extract_containers_regex(text)
    assert rows[0]["container_number"] == "TCNU560097-6"
    assert rows[0]["seal_number"] == "MS068705"


def test_notify_delivery_split():
    text = """
--- PAGE 1 ---
[BLOCK ORDER]
Delivery Agent at Destination
Notify Party/Addresses
EL SEWEDY FOR ENGINEERING INDUSTRIES. MESCO MARINE&ENGINEERING SERVICE CO.
PLOT NO.53, 5TH INDURTRIAL ZONE, 8 PATRICE LUMUMBA ST.
6TH OF OCTOBER CITY GIZA, EGYPT BAB SHARQ - ALEXANDRIA
IMPORTER VAT NO : 204987636 TEL: 00203 3991018 FAX: 00203 3991001
TAX ID: 297 923 900
Vessel and Voy. No.
"""
    parties = extract_notify_and_delivery_agent(text)
    assert "SEWEDY" in (parties.get("mesco_notify1") or "").upper()
    assert "MESCO" not in (parties.get("mesco_notifyaddress") or "").upper()
    assert "INDURTRIAL" in (parties.get("mesco_notifyaddress") or "").upper()
    assert "MESCO" in (parties.get("delivery_agent") or "").upper()
    assert "LUMUMBA" in (parties.get("delivery_agent_address") or "").upper()


def test_consignee_not_delivery_agent():
    text = """
--- PAGE 1 ---
Consignee or Order
TO THE ORDER OF COMMERCIAL INTERNATIONAL BANK-CAIRO
Delivery Agent at Destination
Notify Party/Addresses
EL SEWEDY FOR ENGINEERING INDUSTRIES. MESCO MARINE&ENGINEERING SERVICE CO.
"""
    assert "COMMERCIAL INTERNATIONAL" in extract_consignee_from_pdf(text).upper()
    data = enrich_bl_from_pdf_text(
        {"mesco_consigneenamecontactno": "MESCO MARINE&ENGINEERING SERVICE CO."},
        text,
    )
    assert "COMMERCIAL INTERNATIONAL" in data["mesco_consigneenamecontactno"].upper()
    assert "MESCO" not in data["mesco_consigneenamecontactno"].upper()


def test_crm_master_consignee_not_delivery_agent():
    text = """
--- PAGE 1 ---
Consignee or Order
TO THE ORDER OF COMMERCIAL INTERNATIONAL BANK-CAIRO
Delivery Agent at Destination
"""
    v = enrich_bl_from_pdf_text(
        {
            "mesco_consigneenamecontactno": "MESCO MARINE&ENGINEERING SERVICE CO.",
            "delivery_agent": "MESCO MARINE&ENGINEERING SERVICE CO.",
        },
        text,
    )
    crm = records_to_master_json([v])
    assert "COMMERCIAL INTERNATIONAL" in (crm.get("mesco_consigneenamecontactno") or "").upper()
    assert "MESCO" not in (crm.get("mesco_consigneenamecontactno") or "").upper()


def test_same_as_consignee_stacked_notify():
    """ISTALY-260129: MESCO is delivery agent; notify follows consignee."""
    text = """
--- PAGE 1 ---
Consignee Or Order
EL SEWEDY FOR ENGINEERING INDUSTRIES
PLOT 53, 5TH INDUSTRIAL ZONE, 6TH OF OCTOBER CITY - GIZA, EGYPT
IMPORTER VAT NO : 204987636
Delivery Agent At Destination
Notify Party / Addresses MESCO MARINE & ENGINEERING SERVICE CO.
8 PATRICE LUMUMBA ST.
SAME AS CONSIGNEE
BAB SHARQ - ALEXANDRIA
TEL: 00203 3991018
TAX ID: 297 923 900
Vessel And Voy. No.
"""
    parties = extract_notify_and_delivery_agent(text)
    assert "SEWEDY" in (parties.get("mesco_notify1") or "").upper()
    assert "MESCO" not in (parties.get("mesco_notifyaddress") or "").upper()
    assert "PLOT 53" in (parties.get("mesco_notifyaddress") or "").upper()
    assert "MESCO" in (parties.get("delivery_agent") or "").upper()
    assert "LUMUMBA" in (parties.get("delivery_agent_address") or "").upper()
    data = enrich_bl_from_pdf_text({}, text)
    assert "SEWEDY" in data["mesco_consigneenamecontactno"].upper()
    assert data["mesco_notify2"] == data["delivery_agent"]
    assert "SEWEDY" in (data.get("mesco_notify1") or "").upper()


def test_isaly_express_bl_consignee_and_delivery():
    """ISALY2604028: express B/L without Consignee label; vessel line must not become consignee."""
    text = """
--- PAGE 1 ---
[BLOCK ORDER]
GULDOGAN MENSUCAT SAN VE TIC A.S.
HTTP://WWW.GULDOGAN.COM/
EXPRESS BILL OF LADING
SWISS GARMENTS COMPANY
PRIVATE FREE ZONE AREA , 10TH OF RAMADAN CITY-EGYPT
2015410662-7
SAME AS CONSIGNEE
M/V ADMIRAL MARS/26/468
AMBARLI
ALEXANDRIA
ALEXANDRIA
ADMU5001200 1012597 40'HC
ZERO(0) 27/04/2026 ISTANBUL, TURKEY
"""
    from pdf_bl_enrichment import (
        extract_consignee_block,
        extract_place_of_delivery,
        extract_no_of_org_bls,
        enrich_bl_from_pdf_text,
    )

    block = extract_consignee_block(text)
    assert "SWISS GARMENTS" in (block.get("name") or "").upper()
    assert "FREE ZONE" in (block.get("address") or "").upper()
    assert "M/V" not in (block.get("name") or "").upper()
    assert extract_place_of_delivery(text) == "ALEXANDRIA"
    assert extract_no_of_org_bls(text) == "0"
    data = enrich_bl_from_pdf_text(
        {
            "mesco_consigneenamecontactno": "M/V ADMIRAL MARS/26/468 AMBARLI",
            "mesco_deliveryaddress": "ISTANBUL, TURKEY",
            "mesco_nooforgbls": "1",
            "mesco_telexrelease": False,
        },
        text,
    )
    assert "SWISS GARMENTS" in data["mesco_consigneenamecontactno"].upper()
    assert data["mesco_deliveryaddress"] == "ALEXANDRIA"
    assert data["mesco_nooforgbls"] == "0"
    assert data["mesco_telexrelease"] is True
    assert data["mesco_vessel"] == "ADMIRAL MARS"


def test_original_bl_same_as_consignee_contacts_and_notes():
    """ISALY2604064: literal SAME AS CONSIGNEE, consignee contacts, legal notes vs service mode."""
    text = """
--- PAGE 1 ---
[BLOCK ORDER]
INCI VATKA TEKSTIL SANAYI VE DIS TIC.LTD. STI.
BAGCILAR / ISTANBUL
SWISS GARMENTS COMPANY
PRIVATE FREE ZONE/ A1-10TH OF RAMADAN CITY
MOB:+201003817182 EGYPT / TEL : +201070998852
MAMOHAMED@SGC.COM.EG
MELIAS@SGC.COM.EG
SAME AS CONSIGNEE
M/V ADMIRAL MARS/26/468
AMBARLI
OCEAN FREIGHT
CFS-CFS
DESTINATION
MARINE & ENGINEERING SERVICES COMPANY (MESCO)
DON'T RELEASE THIS SHIPMENT WITHOUT PRESENTATION OF ORIGINAL B/L, AS THE TRADE COULD BE ON T/T TERM ;
SOLAS/VGM COMPLIANCE & LIABILITIES ARE ON SHIPPER'S ACCOUNT.
"""
    from pdf_bl_enrichment import enrich_bl_from_pdf_text

    data = enrich_bl_from_pdf_text(
        {
            "mesco_notify1": "SWISS GARMENTS COMPANY",
            "mesco_notes": "CFS-CFS DESTINATION\nOCEAN FREIGHT",
        },
        text,
    )
    assert data["mesco_notify1"] == "SAME AS CONSIGNEE"
    assert "SWISS GARMENTS" in data["mesco_consigneenamecontactno"].upper()
    assert "MOB:" in (data.get("mesco_consigneecontactnumber") or "").upper()
    assert "@" in (data.get("mesco_consigneecontactnumber") or "")
    assert "DON'T RELEASE" in (data.get("mesco_notes") or "").upper()
    assert "SOLAS" in (data.get("mesco_notes") or "").upper()
    assert "CFS-CFS" not in (data.get("mesco_notes") or "").upper()
    assert "OCEAN FREIGHT" in (data.get("mesco_routenotes") or "").upper()


def test_holder_consignee_fresh_notify_mesco_shipping():
    """ISALY2604067: holder consignee, FRESH notify, MESCO as agent/shipping line."""
    text = """
--- PAGE 1 ---
[BLOCK ORDER]
BATI NAKLIYAT VE TICARET A.S.
EXPRESS BILL OF LADING
ISTANBUL TURKIYE
TEL : +90 212 2932400 VAT NO : 0420019231
TO THE HOLDER OF
TRAMB-EGALY-0925
FRESH ELECTRIC FOR HOME APPLIANCES
10 TH OF RAMADAN CITY EGYPT
E:SHADY.NABIL@FRESH.COM.EG
T:201274011331
M/V ADMIRAL MARS/26/468
AMBARLI
ALEXANDRIA
MARKS:ISALY2604067
FREIGHT PREPAID
CFS-CFS
AMBARLI
MARINE & ENGINEERING SERVICES COMPANY (MESCO)
503 OF EL MAMSHA ST EL SOLTAB TOWER 5TH FLOOR PORD SAID EGYPT
"""
    from pdf_bl_enrichment import enrich_bl_from_pdf_text

    data = enrich_bl_from_pdf_text(
        {
            "mesco_consigneenamecontactno": "ISTANBUL TURKIYE",
            "mesco_consigneeaddress": "TEL : +90 212 2932400",
            "mesco_notify1": "MARINE & ENGINEERING SERVICES COMPANY (MESCO)",
            "mesco_shippingline": "BATI NAKLIYAT VE TICARET A.S.",
        },
        text,
    )
    assert "HOLDER OF TRAMB" in data["mesco_consigneenamecontactno"].upper()
    assert data.get("mesco_consigneeaddress") is None
    assert "FRESH ELECTRIC" in data["mesco_notify1"].upper()
    assert "MESCO" not in data["mesco_notify1"].upper()
    assert "RAMADAN" in (data.get("mesco_notifyaddress") or "").upper()
    assert "BATI" in (data.get("mesco_shippingline") or "").upper()
    assert "MESCO" in (
        data.get("mesco_notify2") or data.get("delivery_agent") or ""
    ).upper()
    assert data.get("mesco_freightpayableat") == "AMBARLI"


def test_enrich_overrides_merged_notify():
    raw = """
--- PAGE 1 ---
Delivery Agent at Destination
Notify Party/Addresses
EL SEWEDY FOR ENGINEERING INDUSTRIES. MESCO MARINE&ENGINEERING SERVICE CO.
PLOT NO.53, 5TH INDURTRIAL ZONE, 8 PATRICE LUMUMBA ST.
6TH OF OCTOBER CITY GIZA, EGYPT BAB SHARQ - ALEXANDRIA
IMPORTER VAT NO : 204987636
Vessel and Voy. No.
TCNU 560097-6 / 40'HC SN: MS068705
"""
    merged = (
        "EL SEWEDY FOR ENGINEERING INDUSTRIES. MESCO MARINE&ENGINEERING SERVICE CO. "
        "PLOT NO.53, 5TH INDURTRIAL ZONE, 8 PATRICE LUMUMBA ST."
    )
    data = enrich_bl_from_pdf_text({"mesco_notifyaddress": merged}, raw)
    assert "MESCO" not in (data.get("mesco_notifyaddress") or "").upper()
    assert data.get("delivery_agent")
