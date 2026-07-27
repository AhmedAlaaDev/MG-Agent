"""Regression tests for PDF attached-list house parsing."""

from crm_output_formatter import records_to_master_json, records_to_house_json
from pdf_attached_list import build_house_records_from_attached_list, extract_attached_list_house_refs
from pdf_bl_enrichment import enrich_bl_from_pdf_text
from pdf_extractor import extract_pdf_text_professionally


def test_attached_list_page_only():
    text = """
--- PAGE 1 ---
B/L NO. EGLV142600454782
* THE BALANCE OF BILL OF LADING SEE ATTACHED LIST *
AMIGL260059392A

--- PAGE 2 ---
ATTACHMENT
ATTACHED LIST PAGE :1/1
AMIGL260069960A
AMIGL260069988A
WCSE26020222
"""
    refs = extract_attached_list_house_refs(text)
    assert refs == ["AMIGL260069960A", "AMIGL260069988A", "WCSE26020222"]
    assert "AMIGL260059392A" not in refs


def test_no_house_when_hbl_equals_mbl():
    validated = {"mesco_masterblno": "EGLV142600454782", "mesco_houseblno": "EGLV142600454782"}
    crm = records_to_master_json([validated])
    assert crm.get("mesco_Operation_mesco_Operation_mesco_Operation", []) == []


def test_evergreen_pdf_enrichment():
    from pathlib import Path

    p = Path(r"d:\new data\1EGLV142600454782.pdf")
    if not p.exists():
        return
    text = extract_pdf_text_professionally(p.read_bytes())["text"]
    data = enrich_bl_from_pdf_text({"mesco_cargodescription": "CONSOLIDATION CARGO"}, text)
    assert "84814000000" in (data.get("mesco_hscode") or "")
    assert "5208490000" in (data.get("mesco_hscode") or "")
    assert data.get("mesco_shippercontactnumber")
    assert "26026000" in data["mesco_shippercontactnumber"]
    assert data.get("mesco_routenotes")
    assert "MERGHEM" in data["mesco_routenotes"].upper()
    assert "AND ARRANGEMENT" in data["mesco_routenotes"].upper()
    assert data.get("mesco_deliveryaddress")
    assert "AND ARRANGEMENT" in data["mesco_deliveryaddress"].upper()
    assert data.get("mesco_shippingline")
    assert "EVERGREEN" in data["mesco_shippingline"].upper()
    assert data.get("mesco_certificatenumber") == "DOC-I-005-02"
    assert len(data.get("_attached_list_house_refs") or []) == 14
    assert "SHIPPER'S LOAD & COUNT" in (data.get("mesco_handlinginformation") or "").upper()
    notes = data.get("mesco_notes") or ""
    assert notes.count("FCL/FCL") <= 1
    assert "PDF:" not in notes
    assert set(data.get("_cargo_page1_references") or []) == {
        "AMIGL260059392A",
        "AMIGL260067029A",
        "AMIGL260068403A",
    }


def test_house_json_includes_master_containers():
    master = {
        "mesco_masterblno": "EGLV142600454782",
        "container_number": "EISU8300180",
        "seal_number": "EMCWLZ4304",
        "mesco_containertype": "40H",
    }
    refs = ["AMIGL260069960A"]
    house_records = build_house_records_from_attached_list(master, refs)
    hj = records_to_house_json(house_records, master_record=master)
    house = hj["value"][0]
    assert house["mesco_Container_mesco_houses"][0]["mesco_containernumber"] == "EISU8300180"


def test_house_uses_hbl_and_linkno():
    master = {"mesco_masterblno": "EGLV142600454782", "container_number": "EISU8300180"}
    refs = ["AMIGL260069960A"]
    crm = records_to_master_json(
        build_house_records_from_attached_list(master, refs),
        master_record=master,
    )
    houses = crm["mesco_Operation_mesco_Operation_mesco_Operation"]
    assert len(houses) == 1
    assert houses[0]["mesco_masterblno"] == "AMIGL260069960A"
    assert houses[0]["mesco_masterbllinkno"] == "EGLV142600454782"
    assert "cr401_totalpackages" not in houses[0]
