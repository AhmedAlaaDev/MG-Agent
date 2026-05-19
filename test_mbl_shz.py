"""Tests for CMA CGM B/L SHZ7922638 (MBL.pdf) — page-2 cargo and MESCO consignee."""

from pathlib import Path

from pdf_extractor import extract_pdf_text_professionally
from pdf_bl_enrichment import (
    enrich_bl_from_pdf_text,
    extract_consignee_block,
    extract_cargo_description_from_pdf,
    extract_hs_codes_from_goods,
)
from validator import validate_and_correct

PDF = Path(r"d:\MBL\MBL.pdf")


def _ocr() -> str:
    if not PDF.exists():
        return ""
    return extract_pdf_text_professionally(PDF.read_bytes())["text"]


def test_intelligent_parser_includes_page2():
    text = _ocr()
    if not text:
        return
    from document_parser import (
        _enrichment_text_for_record,
        _page_text_for_record,
        parse_document_intelligently,
    )

    rec = {"mesco_masterblno": "SHZ7922638"}
    page_ctx = _page_text_for_record(text, rec)
    enrich_ctx = _enrichment_text_for_record(text, rec)
    assert enrich_ctx == text
    assert "DOCTOR BLADE" not in page_ctx.upper()
    assert "DOCTOR BLADE" in enrich_ctx.upper()

    result = parse_document_intelligently(text)
    if not result.records:
        return
    master = result.records[0]
    assert "DOCTOR BLADE" in (master.get("mesco_cargodescription") or "").upper()
    assert "3407001000" in (master.get("mesco_hscode") or "").replace(" ", "")


def test_mesco_consignee_and_page2_cargo():
    text = _ocr()
    if not text:
        return

    cnee = extract_consignee_block(text)
    assert "EXPORT REFERENCES" not in (cnee.get("name") or "").upper()
    assert "MESCO" in (cnee.get("name") or "").upper()
    assert "MARINE" in (cnee.get("name") or "").upper()
    addr = (cnee.get("address") or "").upper()
    assert "PATRICE LUMUMBA" in addr or "BAB SHARQ" in addr
    assert "ALEXANDRIA" in addr

    hs = extract_hs_codes_from_goods(text) or ""
    for code in ("3407001000", "39269090", "84198100", "832200090", "3809910000"):
        assert code in hs.replace(" ", ""), f"missing HS {code}"

    cargo = (extract_cargo_description_from_pdf(text) or "").upper()
    for item in (
        "DOCTOR BLADE",
        "AUTOMOBILE PARTS",
        "SOLID WAX",
        "INDUCTION WOK",
        "ELECTRIC S/U STAMER",
    ):
        assert item in cargo, f"missing cargo {item}"

    rec = enrich_bl_from_pdf_text({"mesco_masterblno": "SHZ7922638"}, text)
    v = validate_and_correct(rec, text)
    assert "MESCO" in (v.get("mesco_consigneenamecontactno") or "").upper()
    assert "DOCTOR BLADE" in (v.get("mesco_cargodescription") or "").upper()
    assert "3407001000" in (v.get("mesco_hscode") or "").replace(" ", "")
