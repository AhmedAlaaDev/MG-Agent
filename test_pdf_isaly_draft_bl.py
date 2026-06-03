"""Tests for ISALY draft B/L PDFs (one B/L per page, STAR CONCORD layout)."""

from pathlib import Path

from pdf_isaly_draft_bl import (
    detect_isaly_draft_multi_bl,
    extract_isaly_draft_records,
)
from validator import validate_and_correct

# Snippet from TUR_STAR CONORD BL (DRAFT) — pages 1–2
SAMPLE = """--- PAGE 1 ---
[VISUAL WORD ORDER]
GULDOGAN MENSUCAT SAN VE TIC A.S.
ISIKTEPE OSB MAH. EFLATUN CADDE NO:28 16140 NILUFER - BURSA
0224 2419950
HTTP://WWW.GULDOGAN.COM/
SWISS GARMENTS COMPANY
PRIVATE FREE ZONE AREA , 10TH OF RAMADAN CITY-EGYPT
2015410662-7
2015410661
SAME AS CONSIGNEE DRAFT
M/V ADMIRAL MARS 26/468 AMBARLI
ALEXANDRIA ALEXANDRIA
MARKS:ISALY2604028 9 PACKAGES ACID: 6408316721009110053
IMPORTER ID:640831672 1.000(CBM)
EXPORTER ID:4160139781
POCKETING & WAISTBAND 182.270 (KGS)
HS CODE:55132100/62179000
ADMU5001200 1012597 40'HC 9 PACKAGES 182.27 1.000 CFS-CFS
FREIGHT COLLECT

--- PAGE 2 ---
[VISUAL WORD ORDER]
ISKUR TEKSTIL ENERJI TIC. VE SAN. A.S
GENC OSMAN MAHALLESI RECEP TAYYIP ERDOGAN BULVARI
SKYTEX GARMENTS CO.
PUBLIC FREE ZONE,
42511 PORT SAID, EGYPT
LOGISTICS@SKYTEX-EG.COM
SAME AS CONSIGNEE DRAFT
M/V ADMIRAL MARS 26/468 AMBARLI
ALEXANDRIA ALEXANDRIA
MARKS:ISALY2604050 101 ROLLS ACID:5188153581009310016
IMPORTER ID:518815358 13.630(CBM)
EXPORTER ID:4820026001
DENIM FABRIC 5,426.800 (KGS)
HS CODE:520942
ADMU5001200 1012597 40'HC 101 ROLLS 5,426.80 13.630 CFS-CFS
FREIGHT COLLECT
"""


def test_detect_isaly_draft_multi_bl():
    assert detect_isaly_draft_multi_bl(SAMPLE) is True


def test_extract_two_pages():
    records = extract_isaly_draft_records(SAMPLE)
    assert records and len(records) == 2
    by_bl = {r["mesco_masterblno"]: r for r in records}

    r1 = by_bl["ISALY2604028"]
    assert r1["mesco_consigneenamecontactno"] == "SWISS GARMENTS COMPANY"
    assert r1["cr401_totalpackages"] == "9 PACKAGES"
    assert r1["mesco_hscode"] == "55132100|62179000"
    assert "2015410662" not in (r1.get("mesco_hscode") or "")
    assert r1["mesco_acidnumber"] == "6408316721009110053"
    assert "POCKETING" in (r1.get("mesco_cargodescription") or "")

    r2 = by_bl["ISALY2604050"]
    assert "SKYTEX" in (r2.get("mesco_consigneenamecontactno") or "").upper()
    assert r2["cr401_totalpackages"] == "101 ROLLS"
    assert r2["mesco_hscode"] == "520942"


def test_validated_hs_no_phone_numbers():
    records = extract_isaly_draft_records(SAMPLE)
    for rec in records:
        v = validate_and_correct(rec, rec.get("_page_text") or SAMPLE)
        hs = v.get("mesco_hscode") or ""
        assert "2015410662" not in hs
        assert "2015410661" not in hs


def test_full_pdf_if_present():
    p = Path(r"d:\MBL\TUR_STAR CONORD BL (DRAFT) _20260428134833.pdf")
    if not p.exists():
        return
    from pdf_extractor import extract_pdf_text_professionally

    text = extract_pdf_text_professionally(p.read_bytes())["text"]
    assert detect_isaly_draft_multi_bl(text)
    records = extract_isaly_draft_records(text)
    assert records and len(records) >= 2
