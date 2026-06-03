"""SACO / Tellus B/L: shipper-header B/L vs FMC Organization number (85 008.pdf)."""

from pathlib import Path

from bl_number_rules import (
    clean_shipper_address_bl_bleed,
    extract_shipper_glued_bl_number,
    is_fmc_organization_number,
)
from document_parser import parse_document_intelligently
from validator import extract_bl_number_regex, validate_and_correct

OCR_SNIPPET = """--- PAGE 1 ---
[OCR HEADER PSM6]
Shipper B/L No
SWEDEV AB ;
FABRIKSVAGEN 1 85 008
684 22 MUNKFORS Carrier:

SWEDEN

FMC Organization No. 025875
"""


def test_fmc_is_not_bl():
    assert is_fmc_organization_number("025875", OCR_SNIPPET)
    assert not is_fmc_organization_number("85 008", OCR_SNIPPET)


def test_extract_shipper_glued_bl():
    assert extract_shipper_glued_bl_number(OCR_SNIPPET) == "85 008"


def test_extract_bl_number_regex_prefers_85_008():
    assert extract_bl_number_regex(OCR_SNIPPET) == "85 008"


def test_clean_shipper_address():
    dirty = "FABRIKSVAGEN 1 85 008, 684 22 MUNKFORS, SWEDEN"
    clean = clean_shipper_address_bl_bleed(dirty, "85 008", OCR_SNIPPET)
    assert "85 008" not in (clean or "")
    assert "FABRIKSVAGEN 1" in (clean or "")
    assert "MUNKFORS" in (clean or "")


def test_full_pdf_if_present():
    p = Path(r"c:\Users\AhmedAlaa\Downloads\LCL-HBLs\85 008.pdf")
    if not p.exists():
        return
    from pdf_extractor import extract_pdf_text_professionally

    text = extract_pdf_text_professionally(p.read_bytes())["text"]
    r = parse_document_intelligently(text)
    assert r.records
    rec = validate_and_correct(r.records[0], text)
    assert rec.get("mesco_masterblno") == "85 008"
    addr = rec.get("mesco_shipperaddress") or ""
    assert "85 008" not in addr
    assert "FABRIKSVAGEN" in addr
    assert "Carrier" not in addr
