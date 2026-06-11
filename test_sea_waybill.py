"""Tests for Arkas / ONE consolidation sea waybill direct extraction."""

from ai_extractor import _parse_json_response
from document_parser import parse_document_intelligently
from crm_output_formatter import records_to_house_json, records_to_master_json
from pdf_sea_waybill import (
    build_house_records_for_consolidation_sea_waybill,
    extract_arkas_attached_list_cargo,
    is_consolidation_sea_waybill,
    master_record_without_house_cargo,
    parse_consolidation_sea_waybill,
)
from validator import validate_and_correct

ARKAS_SAMPLE = """
--- PAGE 1 ---
[VISUAL WORD ORDER]
ONE ULUSLARARASI TAS VE DIS TIC A S IST000027163
KOSUYOLU CAD NO 23
POST CODE 34718 KADIKOY ISTANBUL
TURKIYE TAX ID 4690517154
TEL: 0216 422 11 01 FAX: 0216 422 11 50
MARINE & ENGINEERING SERVICES COMPANY
MESCO 8 PATRIC LUMUMBA ST.BAB SHARQ
ALEXANDRIA - EGYPT TEL. +2(03)3991000 FAX.
+2(03)3991001
MARINE & ENGINEERING SERVICES COMPANY
MESCO 8 PATRIC LUMUMBA ST.BAB SHARQ
ALEXANDRIA - EGYPT TEL. +2(03)3991000 FAX.
+2(03)3991001
ISTANBUL
GULBENIZ A TLS06S25
ALEXANDRIA
1 /40' HC Containers Said to Contain
***** AS PER ATTACHED LIST *****
GAOU 6839174 CONSOLIDATION CARGO ON CARRIAGE FROM ALEXANDRIA PORT TO MERGHEM
BONDED WAREHOUSE ON
RECEIVER'S RISK, RESPONSIBILITY AND ACCOUNT
ACID: 2979239002025020654
IMPORTER TAXATION NUMBER: 297923900
EXPORTER NUMBER: 4690517154
21 DAYS FREETIME OF DEMURRAGE
1 ============================ =============
TOTAL : 144 PALLET GW: 19,204.000 -KGS
FREIGHT PREPAID
NON-NEGOTIABLE SEA WAY BILL OF LADING
ISTANBUL/TURKEY 09/03/2025 GULBENIZ A TLS06S25
FCL/FCL FREE IN / FREE OUT ALEXANDRIA
ARKAS DENIZCILIK VE NAKLIYAT A.S.

[BLOCK ORDER]
IST000027163
ONE ULUSLARARASI TAS VE DIS TIC A S
KOSUYOLU CAD NO 23
POST CODE 34718 KADIKOY ISTANBUL
TURKIYE TAX ID 4690517154
TEL: 0216 422 11 01 FAX: 0216 422 11 50
MARINE & ENGINEERING SERVICES COMPANY
MESCO 8 PATRIC LUMUMBA ST.BAB SHARQ
ALEXANDRIA - EGYPT TEL. +2(03)3991000 FAX.
+2(03)3991001
ISTANBUL
GULBENIZ A
TLS06S25
ALEXANDRIA
1 /40' HC Containers Said to Contain
***** AS PER ATTACHED LIST *****
GAOU 6839174
CONSOLIDATION CARGO ON CARRIAGE FROM ALEXANDRIA PORT TO MERGHEM
TOTAL : 144 PALLET
GW: 19,204.000 -KGS
FREIGHT PREPAID
NON-NEGOTIABLE SEA WAY BILL OF LADING
ARKAS DENIZCILIK VE NAKLIYAT A.S.
"""


def test_detects_consolidation_sea_waybill():
    assert is_consolidation_sea_waybill(ARKAS_SAMPLE)


def test_parses_arkas_master_fields():
    from pathlib import Path
    from spreadsheet_extractor import extract_document_text_professionally

    pdf = Path(__file__).with_name("IST_ARKASLINE_ORIJINAL__IST000027163.pdf")
    if pdf.exists():
        raw = extract_document_text_professionally(
            pdf.read_bytes(),
            pdf.name,
        ).get("text", "")
        rec = parse_consolidation_sea_waybill(raw)
    else:
        raw = ARKAS_SAMPLE
        rec = parse_consolidation_sea_waybill(raw)

    assert rec
    assert rec["mesco_masterblno"] == "IST000027163"
    assert "ONE ULUSLARARASI" in (rec.get("mesco_shippernamecontactno") or "").upper()
    assert rec.get("mesco_origin") == "ISTANBUL"
    assert rec.get("mesco_destination") == "ALEXANDRIA"
    assert rec.get("mesco_vessel") == "GULBENIZ A"
    assert rec.get("mesco_voytruckno") == "TLS06S25"
    assert rec.get("container_number") == "GAOU6839174"
    assert rec.get("cr401_totalpackages") == 144
    assert rec.get("cr401_totalgrossweight") == 19204.0
    assert rec.get("mesco_acidnumber") == "2979239002025020654"
    assert rec.get("mesco_consolidation") is True
    assert "ATTACHED LIST" in (rec.get("mesco_cargodescription") or "").upper()

    v = validate_and_correct(rec, raw)
    assert v.get("mesco_vessel") == "GULBENIZ A"
    assert v.get("cr401_totalpackages") == 144
    lines = v.get("cargo_lines") or []
    if pdf.exists():
        assert len(lines) >= 8
        assert not any("TOTAL : 144" in (ln.get("mesco_descriptionofgoods") or "") for ln in lines)


def test_parse_json_response_tolerates_trailing_text():
    payload = _parse_json_response(
        '{"document_layout":"single","records":[{"mesco_masterblno":"IST000027163"}]}\n'
        '{"extra":"ignored"}',
        "Gemini",
    )
    assert payload["records"][0]["mesco_masterblno"] == "IST000027163"


def test_finalize_does_not_drop_wrong_numeric_bl(monkeypatch):
    from bl_number_rules import finalize_multi_bl_records

    bad_llm = [{"mesco_masterblno": "000027163"}]
    out = finalize_multi_bl_records(bad_llm, ARKAS_SAMPLE)
    assert out
    assert out[0]["mesco_masterblno"] == "IST000027163"


def test_document_parser_merges_sea_waybill_when_llm_returns_wrong_bl(monkeypatch):
    fake_llm = {
        "document_layout": "single_bl",
        "records": [{"mesco_masterblno": "000027163", "mesco_origin": "ISTANBUL"}],
        "warnings": [],
    }

    import ai_extractor as ae

    monkeypatch.setattr(ae, "extract_records_with_azure_openai", lambda *a, **k: fake_llm)
    result = parse_document_intelligently(ARKAS_SAMPLE)
    assert result.records
    assert result.records[0]["mesco_masterblno"] == "IST000027163"
    assert result.quality.get("fallback_used") is True


def test_document_parser_falls_back_when_llm_fails(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise ValueError("Gemini returned invalid JSON: Extra data: line 88 column 1")

    import ai_extractor as ae

    monkeypatch.setattr(ae, "extract_records_with_azure_openai", _boom)
    result = parse_document_intelligently(ARKAS_SAMPLE)
    assert result.records
    assert result.records[0]["mesco_masterblno"] == "IST000027163"


def test_crm_output_links_houses_to_master_with_per_house_cargo():
    from pathlib import Path
    from spreadsheet_extractor import extract_document_text_professionally

    pdf = Path(__file__).with_name("IST_ARKASLINE_ORIJINAL__IST000027163.pdf")
    if not pdf.exists():
        return
    raw = extract_document_text_professionally(pdf.read_bytes(), pdf.name).get("text", "")
    rec = validate_and_correct(parse_consolidation_sea_waybill(raw), raw)
    house_records = build_house_records_for_consolidation_sea_waybill(rec, raw)
    assert len(house_records) >= 8
    master = master_record_without_house_cargo(rec)
    crm = records_to_master_json(house_records, master_record=master)
    houses = crm.get("mesco_Operation_mesco_Operation_mesco_Operation") or []
    assert len(houses) == len(house_records)
    assert crm.get("mesco_masterblno") == "IST000027163"
    assert crm.get("mesco_vessel") == "GULBENIZ A"
    assert crm.get("cr401_totalpackages") == 144
    assert crm.get("mesco_houseblno") in (None, "")
    for house in houses:
        assert house.get("mesco_masterbllinkno") == "IST000027163"
        assert house.get("mesco_masterblno", "").startswith("IST000027163-")
        assert house.get("mesco_masterblno") != "IST000027163"
    cargo = crm.get("mesco_Cargo_MasterOperation_mesco_Operation") or []
    assert len(cargo) == len(house_records)
    hbls = {h["mesco_masterblno"] for h in houses}
    assert len(hbls) == len(houses)
    hj = records_to_house_json(house_records, master_record=master)
    for entry in hj.get("value") or []:
        house_cargo = entry.get("mesco_Cargo_HouseOperation_mesco_Operation") or []
        assert len(house_cargo) == 1


def test_validated_sea_waybill_record():
    rec = validate_and_correct(parse_consolidation_sea_waybill(ARKAS_SAMPLE), ARKAS_SAMPLE)
    assert rec.get("mesco_masterblno") == "IST000027163"
    assert rec.get("mesco_blstatus") == 886150001


if __name__ == "__main__":
    test_detects_consolidation_sea_waybill()
    test_parses_arkas_master_fields()
    test_parse_json_response_tolerates_trailing_text()
    test_validated_sea_waybill_record()
    print("All sea waybill tests passed.")
