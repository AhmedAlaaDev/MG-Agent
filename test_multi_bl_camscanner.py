"""Multi-B/L CamScanner PDF: two masters on two pages (fallback + validation)."""

from pathlib import Path

from crm_output_formatter import records_to_master_json
from pdf_multi_bl import detect_and_extract_multi_bl_records
from record_reconciliation import reconcile_record_lists
from validator import format_container_number, validate_and_correct


FIXTURE = Path(__file__).with_name("test_fixtures").joinpath("camscanner_multi_bl_ocr.txt")


def _load_fixture() -> str:
    if not FIXTURE.exists():
        raise FileNotFoundError(
            f"Run OCR once to create {FIXTURE.name} or place CamScanner text there."
        )
    return FIXTURE.read_text(encoding="utf-8")


def test_detect_two_bl_pages():
    raw = _load_fixture()
    records = detect_and_extract_multi_bl_records(raw)
    assert records is not None
    assert len(records) == 2
    bls = {r["mesco_masterblno"] for r in records}
    assert bls == {"2025121147", "202512139"}


def test_page1_fields():
    raw = _load_fixture()
    records = detect_and_extract_multi_bl_records(raw)
    page1 = next(r for r in records if r["mesco_masterblno"] == "2025121147")
    assert "MIDDLE EAST" in (page1.get("mesco_consigneenamecontactno") or "").upper()
    assert page1.get("cr401_totalpackages") == "2 PALLETS"
    assert page1.get("cr401_totalvolume") == "5"
    assert page1.get("mesco_acidnumber") == "4358113202025120018"
    container = page1["containers"][0]["container_number"]
    assert format_container_number(container) == "CSLU203520/4"


def test_page2_fields():
    raw = _load_fixture()
    records = detect_and_extract_multi_bl_records(raw)
    page2 = next(r for r in records if r["mesco_masterblno"] == "202512139")
    assert "CITI FOR INVEST" in (page2.get("mesco_consigneenamecontactno") or "").upper()
    assert page2.get("cr401_totalpackages") == "7 PALLETS"
    assert page2.get("cr401_totalgrossweight") == "2360"
    assert page2.get("cr401_totalvolume") == "30"
    assert page2.get("mesco_acidnumber") == "4867917772025120023"


def test_two_separate_crm_masters_no_false_hs():
    raw = _load_fixture()
    fallback = detect_and_extract_multi_bl_records(raw)
    records = reconcile_record_lists(
        [{"mesco_masterblno": "2025121147", "mesco_hscode": "2025121147"}],
        fallback,
    )
    masters = []
    for rec in records:
        page_text = rec.pop("_page_text", raw)
        validated = validate_and_correct(rec, page_text)
        assert not validated.get("mesco_hscode")
        masters.append(records_to_master_json([validated]))

    assert len(masters) == 2
    assert masters[0]["mesco_masterblno"] != masters[1]["mesco_masterblno"]
    assert masters[0]["mesco_consigneenamecontactno"] != masters[1]["mesco_consigneenamecontactno"]
    assert masters[0]["cr401_totalvolume"] == 5
    assert masters[1]["cr401_totalvolume"] == 30
    assert "2025121147" not in (masters[0].get("mesco_hscode") or "")
    cargo0 = masters[0]["mesco_Cargo_MasterOperation_mesco_Operation"][0]
    assert "HS:" not in (cargo0.get("mesco_descriptionofgoods") or "")
