"""B/L number rules and multi-record finalization."""

from pathlib import Path

from bl_number_rules import (
    extract_ocean_bl_from_page,
    finalize_multi_bl_records,
    is_form_or_serial_bl_candidate,
    list_canonical_page_bls,
    normalize_packages_field,
)
from crm_output_formatter import records_to_master_json
from document_parser import parse_document_intelligently
from pdf_multi_bl import detect_and_extract_multi_bl_records, split_pdf_pages
from validator import validate_and_correct

FIXTURE = Path(__file__).with_name("test_fixtures").joinpath("camscanner_multi_bl_ocr.txt")


def _raw() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_form_serial_rejected():
    assert is_form_or_serial_bl_candidate("0038260", "EEE ON 0038260 BILL OF LADING")
    assert is_form_or_serial_bl_candidate("9933955", "MESCO 9933955")
    assert not is_form_or_serial_bl_candidate("2025121147", _raw())


def test_page_bl_extraction():
    pages = split_pdf_pages(_raw())
    p1 = next(t for n, t in pages if n == 1)
    p2 = next(t for n, t in pages if n == 2)
    assert extract_ocean_bl_from_page(p1) == "2025121147"
    assert extract_ocean_bl_from_page(p2) == "202512139"


def test_canonical_two_pages():
    entries = list_canonical_page_bls(_raw())
    assert len(entries) == 2
    assert {e["bl"] for e in entries} == {"2025121147", "202512139"}


def test_finalize_drops_spurious_azure_records():
    raw = _raw()
    canonical = detect_and_extract_multi_bl_records(raw)
    spurious = [
        {"mesco_masterblno": "9933955", "source_page": 1},
        {"mesco_masterblno": "0038260", "source_page": 2},
        {"mesco_masterblno": "2025121147", "source_page": 1, "mesco_vessel": "BERNHARD SCHULTE"},
        {"mesco_masterblno": "202512139", "source_page": 2, "cr401_totalpackages": "7"},
    ]
    out = finalize_multi_bl_records(spurious + canonical, raw)
    assert len(out) == 2
    bls = {r["mesco_masterblno"] for r in out}
    assert bls == {"2025121147", "202512139"}


def test_finalize_page1_packages_and_consignees():
    raw = _raw()
    canonical = detect_and_extract_multi_bl_records(raw)
    out = finalize_multi_bl_records(canonical, raw)
    p1 = next(r for r in out if r["mesco_masterblno"] == "2025121147")
    p2 = next(r for r in out if r["mesco_masterblno"] == "202512139")
    assert p1.get("cr401_totalpackages") == "2 PALLETS"
    assert p2.get("cr401_totalpackages") == "7 PALLETS"
    assert p2.get("mesco_origin") == "NEW YORK"
    assert "MIDDLE EAST" in (p1.get("mesco_consigneenamecontactno") or "").upper()
    assert p1.get("mesco_origin") == "NEW YORK"
    assert p1.get("mesco_vessel") == "BERNHARD SCHULTE"
    assert p2.get("cr401_totalgrossweight") == "2360"
    assert p2.get("cr401_totalvolume") == "30"
    assert "L.L.C" in (p2.get("mesco_consigneenamecontactno") or "").upper()


def test_validate_camscanner_two_masters():
    raw = _raw()
    canonical = detect_and_extract_multi_bl_records(raw)
    finalized = finalize_multi_bl_records(canonical, raw)
    masters = []
    for rec in finalized:
        page_text = rec.get("_page_text", raw)
        v = validate_and_correct(rec, page_text)
        masters.append(records_to_master_json([v]))
    assert len(masters) == 2
    assert normalize_packages_field("ALLETS", raw.split("PAGE 2")[0]) == "2 PALLETS"


def test_parser_offline_multi_bl(monkeypatch):
    """Without live Azure: canonical path still yields exactly 2 records."""
    raw = _raw()

    def fake_azure(_text, page_scope=False):
        return {"document_layout": "multi_bl_pages", "records": [], "warnings": [], "confidence": {}}

    monkeypatch.setattr(
        "document_parser.extract_records_with_azure_openai",
        fake_azure,
    )
    result = parse_document_intelligently(raw)
    assert len(result.records) == 2
    assert {r["mesco_masterblno"] for r in result.records} == {"2025121147", "202512139"}
