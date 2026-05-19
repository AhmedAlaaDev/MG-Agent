"""Tests for groupage master B/L per-shipper cargo lines."""

from pathlib import Path

from pdf_extractor import extract_pdf_text_professionally
from pdf_groupage_cargo import extract_groupage_cargo_lines, clean_ocr_value
from validator import validate_and_correct
from crm_output_formatter import records_to_master_json


def test_groupage_nine_cargo_lines():
    pdf = Path(r"d:\MBL\AD2601101165.pdf")
    if not pdf.exists():
        return
    raw = extract_pdf_text_professionally(pdf.read_bytes())["text"]
    lines = extract_groupage_cargo_lines(raw)
    shippers = [ln["shipper"] for ln in lines]
    assert len(lines) == 9
    assert "GULDOGAN" in shippers[0].upper()
    assert "FORM" in shippers[-1].upper()
    assert sum(int(float(ln["packages"])) for ln in lines) == 395
    assert clean_ocr_value("6408316721009110053\\'") == "6408316721009110053"


def test_crm_master_has_multiple_cargo_rows():
    pdf = Path(r"d:\MBL\AD2601101165.pdf")
    if not pdf.exists():
        return
    raw = extract_pdf_text_professionally(pdf.read_bytes())["text"]
    v = validate_and_correct({"mesco_masterblno": "AD2601101165"}, raw_text=raw)
    crm = records_to_master_json([v])
    cargo = crm.get("mesco_Cargo_MasterOperation_mesco_Operation") or []
    assert len(cargo) == 9
    assert "GULDOGAN" in (cargo[0].get("mesco_descriptionofgoods") or "").upper()
    assert "INCI VATKA" in " ".join(
        c.get("mesco_descriptionofgoods") or "" for c in cargo
    ).upper()
