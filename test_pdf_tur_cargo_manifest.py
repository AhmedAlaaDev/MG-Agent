"""Tests for Turkey CARGO MANIFEST (ISALY houses, ISTE master)."""

from pathlib import Path

from pdf_extractor import extract_pdf_text_professionally
from pdf_tur_cargo_manifest import (
    is_tur_cargo_manifest,
    parse_manifest_rows,
    parse_tur_cargo_manifest,
)
from validator import validate_and_correct
from crm_output_formatter import records_to_master_json, records_to_house_json

PDF = Path(r"d:\MBL\TUR_CARGO_MANIFEST_ISTE0927_20260428134803.pdf")

EXPECTED_HBLS = [
    "ISALY2604028",
    "ISALY2604050",
    "ISALY2604064",
    "ISALY2604067",
    "ISALY2604071",
    "ISALY2604072",
    "ISALY2604073",
    "ISALY2604074",
    "ISALY2604075",
]


def _ocr() -> str:
    if not PDF.exists():
        return ""
    return extract_pdf_text_professionally(PDF.read_bytes())["text"]


def test_detects_tur_cargo_manifest():
    text = _ocr()
    if not text:
        return
    assert is_tur_cargo_manifest(text)


def test_nine_house_rows():
    text = _ocr()
    if not text:
        return
    rows = parse_manifest_rows(text)
    assert [r["hbl"] for r in rows] == EXPECTED_HBLS
    assert len(rows) == 9


def test_master_totals_and_container():
    text = _ocr()
    if not text:
        return
    data = parse_tur_cargo_manifest(text)
    assert data is not None
    master = data["master_record"]
    assert master["mesco_masterblno"] == "ISTE0927"
    assert master["cr401_totalpackages"] == 395
    assert master["cr401_totalgrossweight"] == 14322.74
    assert master["cr401_totalvolume"] == 48.38
    assert master["mesco_nooforgbls"] == "9"
    containers = master.get("containers") or []
    assert containers[0]["container_number"] == "ADMU5001200"
    assert containers[0]["seal_number"] == "1012597"


def test_first_house_metrics():
    text = _ocr()
    if not text:
        return
    rows = parse_manifest_rows(text)
    first = rows[0]
    assert first["hbl"] == "ISALY2604028"
    assert first["gross_weight_kg"] == 182.27
    assert first["measurement_cbm"] == 1.0
    assert first["packages_line"] == "9 PACKAGES"
    assert "GULDOGAN" in (first.get("shipper") or "").upper()


def test_validated_house_parties_and_packages():
    text = _ocr()
    if not text:
        return
    parsed = parse_tur_cargo_manifest(text)
    by_hbl = {r["mesco_houseblno"]: validate_and_correct(r, text) for r in parsed["house_records"]}

    r4050 = by_hbl["ISALY2604050"]
    assert "SKYTEX" in (r4050.get("mesco_consigneenamecontactno") or "").upper()
    assert "SWISS" not in (r4050.get("mesco_consigneenamecontactno") or "").upper()
    assert r4050.get("cr401_totalpackages") == "101 ROLLS"

    r4067 = by_hbl["ISALY2604067"]
    assert "HOLDER OF TRAMB" in (r4067.get("mesco_consigneenamecontactno") or "").upper()
    assert "FRESH ELECTRIC" in (r4067.get("mesco_notify1") or "").upper()
    assert r4067.get("cr401_totalpackages") == "4 PALLETS"

    r4074 = by_hbl["ISALY2604074"]
    assert "JOHNSON WAX" in (r4074.get("mesco_consigneenamecontactno") or "").upper()
    assert "UNSTACKABLE" in (
        (r4074.get("cr401_totalpackages") or "")
        + (r4074.get("mesco_handlinginformation") or "")
    ).upper()
    assert r4074.get("cr401_totalpackages", "").startswith("5 PALLETS")

    r4028 = by_hbl["ISALY2604028"]
    assert r4028.get("cr401_totalpackages") == "9 PACKAGES"
    r4073 = by_hbl["ISALY2604073"]
    assert r4073.get("cr401_totalpackages") == "243 ROLLS"


def test_shipper_names_and_hs_codes():
    text = _ocr()
    if not text:
        return
    parsed = parse_tur_cargo_manifest(text)
    by_hbl = {r["mesco_houseblno"]: validate_and_correct(r, text) for r in parsed["house_records"]}

    s4028 = by_hbl["ISALY2604028"]["mesco_shippernamecontactno"] or ""
    assert "GULDOGAN" in s4028.upper()
    assert "POCKETING" not in s4028.upper()
    assert "HS CODE" not in s4028.upper()
    assert by_hbl["ISALY2604028"].get("mesco_hscode") == "55132100/62179000"

    s4064 = by_hbl["ISALY2604064"]["mesco_shippernamecontactno"] or ""
    assert "INCI VATKA" in s4064.upper()
    assert "REINFORCEMENT" not in s4064.upper()

    s4072 = by_hbl["ISALY2604072"]["mesco_shippernamecontactno"] or ""
    assert "IDT ETIKET" in s4072.upper()
    assert "CARTON ENVELOPE" not in s4072.upper()

    s4074 = by_hbl["ISALY2604074"]
    assert "OMUR BASKI" in (s4074.get("mesco_shippernamecontactno") or "").upper()
    assert "EXPORTER ID" not in (s4074.get("mesco_shippernamecontactno") or "").upper()
    assert s4074.get("mesco_hscode") in (None, "")
    assert "SHRINKABLE" in (s4074.get("mesco_cargodescription") or "").upper()

    s4075 = by_hbl["ISALY2604075"]
    hs5 = (s4075.get("mesco_hscode") or "").replace(" ", "")
    assert "9606290000" in hs5 and "960621000" in hs5
    assert "551321" not in hs5


EXPECTED_ACIDS = {
    "ISALY2604028": "6408316721009110053",
    "ISALY2604050": "5188153581009310016",
    "ISALY2604064": "6408316721010310023",
    "ISALY2604067": "2003019501010310076",
    "ISALY2604071": "6408316721010310056",
    "ISALY2604072": "6408316721010510036",
    "ISALY2604073": "6408316721010410039",
    "ISALY2604074": "1001365671010710013",
    "ISALY2604075": "6408316721010410088",
}


def test_all_house_acid_numbers():
    text = _ocr()
    if not text:
        return
    parsed = parse_tur_cargo_manifest(text)
    by_hbl = {r["mesco_houseblno"]: validate_and_correct(r, text) for r in parsed["house_records"]}
    for hbl, expected in EXPECTED_ACIDS.items():
        assert by_hbl[hbl].get("mesco_acidnumber") == expected, hbl


def test_acid_and_carton_label():
    text = _ocr()
    if not text:
        return
    parsed = parse_tur_cargo_manifest(text)
    by_hbl = {r["mesco_houseblno"]: validate_and_correct(r, text) for r in parsed["house_records"]}

    assert by_hbl["ISALY2604074"].get("mesco_acidnumber") == "1001365671010710013"
    cargo72 = (by_hbl["ISALY2604072"].get("mesco_cargodescription") or "").upper()
    assert "CARTON LABEL" in cargo72
    assert "CARTON ENVELOPE" in cargo72


def test_crm_nine_houses():
    text = _ocr()
    if not text:
        return
    parsed = parse_tur_cargo_manifest(text)
    houses = [
        validate_and_correct(rec, text) for rec in parsed["house_records"]
    ]
    master = validate_and_correct(parsed["master_record"], text)
    crm_master = records_to_master_json(houses, master_record=master)
    crm_houses = records_to_house_json(houses, master_record=master)
    assert len(crm_houses.get("value") or []) == 9
    assert crm_master.get("cr401_totalpackages") == 395
    assert crm_master.get("cr401_totalgrossweight") == 14322.74
    assert crm_master.get("cr401_totalvolume") == 48.38
