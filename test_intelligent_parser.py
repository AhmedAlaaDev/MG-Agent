"""Tests for intelligent parsing (reconciliation + fallback without live Azure)."""

from pathlib import Path

from crm_output_formatter import records_to_master_json
from pdf_multi_bl import detect_and_extract_multi_bl_records, detect_multi_bl_candidate
from record_reconciliation import merge_record_fields, reconcile_record_lists
from validator import validate_and_correct
from test_sea_waybill import COSCO_SAMPLE
from test_cargo_manifest import RAW as CARGO_MANIFEST_SAMPLE

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


def test_reconcile_can_force_trusted_fallback_fields():
    azure = [{
        "mesco_masterblno": "COSU6446151350",
        "mesco_voytruckno": "WRONGVOY",
        "mesco_shippingline": "CMA CGM",
    }]
    fallback = [{
        "mesco_masterblno": "COSU6446151350",
        "mesco_voytruckno": "0BEN9W1MA",
        "mesco_shippingline": "COSCO SHIPPING LINES CO.,LTD.",
    }]

    merged = reconcile_record_lists(
        azure,
        fallback,
        prefer_fallback_keys={"mesco_voytruckno", "mesco_shippingline"},
    )

    assert merged[0]["mesco_voytruckno"] == "0BEN9W1MA"
    assert merged[0]["mesco_shippingline"] == "COSCO SHIPPING LINES CO.,LTD."


def test_reconcile_keeps_alphanumeric_bl_prefixes_distinct():
    records = reconcile_record_lists(
        [{"mesco_masterblno": "ABC123456"}],
        [{"mesco_masterblno": "XYZ123456"}],
    )

    assert [r["mesco_masterblno"] for r in records] == ["ABC123456", "XYZ123456"]


def test_reconcile_dedupes_house_records_by_house_bl_not_master_bl():
    records = reconcile_record_lists(
        [],
        [
            {"mesco_masterblno": "NSA26030217", "mesco_houseblno": "NAV26MU0714"},
            {"mesco_masterblno": "NSA26030217", "mesco_houseblno": "NAV26MU0821"},
        ],
    )

    assert [r["mesco_houseblno"] for r in records] == ["NAV26MU0714", "NAV26MU0821"]


def test_document_parser_forces_trusted_pdf_fallback_over_wrong_llm():
    import document_parser as dp

    original = dp.extract_records_with_azure_openai

    def fake_llm(*_args, **_kwargs):
        return {
            "document_layout": "single_bl",
            "records": [
                {
                    "mesco_masterblno": "COSU6446151350",
                    "mesco_voytruckno": "WRONGVOY",
                    "mesco_shippingline": "CMA CGM",
                    "container_number": "WRONG1234567",
                }
            ],
            "warnings": [],
        }

    try:
        dp.extract_records_with_azure_openai = fake_llm
        result = dp.parse_document_intelligently(COSCO_SAMPLE)
    finally:
        dp.extract_records_with_azure_openai = original

    assert result.records
    rec = result.records[0]
    assert rec["mesco_voytruckno"] == "0BEN9W1MA"
    assert rec["mesco_shippingline"] == "COSCO SHIPPING LINES CO.,LTD."
    assert rec["mesco_masterblno"] == "COSU6446151350"


def test_document_parser_manifest_fallback_keeps_master_and_all_houses():
    import document_parser as dp

    original = dp.extract_records_with_azure_openai

    try:
        dp.extract_records_with_azure_openai = lambda *_args, **_kwargs: {
            "document_layout": "unknown",
            "records": [],
            "warnings": [],
        }
        result = dp.parse_document_intelligently(CARGO_MANIFEST_SAMPLE)
    finally:
        dp.extract_records_with_azure_openai = original

    assert result.document_layout == "master_with_houses"
    assert result.quality["deterministic_parser"] == "pdf_cargo_manifest"
    assert len(result.records) == 6
    masters = [r for r in result.records if r.get("mesco_masterblno") == "NSA26030217" and not r.get("mesco_houseblno")]
    houses = [r for r in result.records if r.get("mesco_houseblno")]
    assert len(masters) == 1
    assert [h["mesco_houseblno"] for h in houses] == [
        "NAV26MU0714",
        "NAV26MU0821",
        "NAV26MU0795",
        "NAV26MU0794",
        "NAV26MU0800",
    ]
