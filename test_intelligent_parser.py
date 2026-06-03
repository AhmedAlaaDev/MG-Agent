"""Tests for intelligent parsing (reconciliation + fallback without live Azure)."""

from pathlib import Path

from crm_output_formatter import records_to_master_json
from pdf_multi_bl import detect_and_extract_multi_bl_records, detect_multi_bl_candidate
from record_reconciliation import merge_record_fields, reconcile_record_lists
from validator import validate_and_correct

FIXTURE = Path(__file__).with_name("test_fixtures").joinpath("camscanner_multi_bl_ocr.txt")


def _raw() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_multi_bl_candidate_detected():
    assert detect_multi_bl_candidate(_raw()) is True


def test_fallback_two_records():
    records = detect_and_extract_multi_bl_records(_raw())
    assert records and len(records) == 2


def test_reconcile_azure_partial_with_fallback():
    raw = _raw()
    fallback = detect_and_extract_multi_bl_records(raw)
    assert fallback
    # Simulate Azure returning only page 1
    azure_partial = [
        {
            "mesco_masterblno": "2025121147",
            "mesco_consigneenamecontactno": "MIDDLE EAST FOR IMP&EXPO",
            "mesco_hscode": "2025121147",
        }
    ]
    merged = reconcile_record_lists(azure_partial, fallback)
    assert len(merged) == 2
    page2 = next(r for r in merged if r.get("mesco_masterblno") == "202512139")
    assert "CITI" in (page2.get("mesco_consigneenamecontactno") or "").upper()
    assert page2.get("cr401_totalpackages") == "7"


def test_validate_reconciled_no_false_hs():
    raw = _raw()
    fallback = detect_and_extract_multi_bl_records(raw)
    azure_partial = [{"mesco_masterblno": "2025121147", "mesco_hscode": "2025121147"}]
    merged = reconcile_record_lists(azure_partial, fallback)
    masters = []
    for rec in merged:
        page_text = rec.pop("_page_text", raw)
        validated = validate_and_correct(rec, page_text)
        assert not validated.get("mesco_hscode")
        masters.append(records_to_master_json([validated]))
    assert len(masters) == 2
    assert masters[0]["mesco_masterblno"] != masters[1]["mesco_masterblno"]


def test_merge_prefers_azure_when_set():
    azure = {"mesco_masterblno": "2025121147", "mesco_consigneenamecontactno": "FROM AZURE"}
    fb = {"mesco_masterblno": "2025121147", "mesco_consigneenamecontactno": "FROM FALLBACK", "cr401_totalpackages": "2"}
    merged = merge_record_fields(azure, fb)
    assert merged["mesco_consigneenamecontactno"] == "FROM AZURE"
    assert merged["cr401_totalpackages"] == "2"
