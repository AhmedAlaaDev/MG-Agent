"""Consolidated LCL multi-HBL: one master + shared container + N house B/Ls."""

from pathlib import Path

from crm_output_formatter import MASTER_HOUSES_KEY, records_to_master_json
from pdf_consolidated_lcl import (
    is_consolidated_lcl_multi_hbl,
    parse_consolidated_lcl_multi_hbl,
)
from validator import validate_and_correct

FIXTURE = (
    Path(__file__).with_name("test_fixtures").joinpath("nsa26030217_consolidated_ocr.txt")
)


def _load_fixture() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_detects_consolidated_lcl():
    raw = _load_fixture()
    assert is_consolidated_lcl_multi_hbl(raw)


def test_extracts_four_house_bls():
    raw = _load_fixture()
    parsed = parse_consolidated_lcl_multi_hbl(raw)
    assert parsed is not None
    hbls = {r["mesco_houseblno"] for r in parsed["house_records"]}
    assert hbls == {
        "NAV26MU0714",
        "NAV26MU0821",
        "NAV26MU0795",
        "NAV26MU0794",
    }


def test_master_mbl_and_container():
    raw = _load_fixture()
    parsed = parse_consolidated_lcl_multi_hbl(raw)
    assert parsed is not None
    master = parsed["master_record"]
    assert master["mesco_masterblno"] == "NSA26030217"
    assert master["mesco_consolidation"] is True
    assert "TRKU4457572" in str(master.get("container_number") or "")


def test_crm_output_one_master_with_houses():
    raw = _load_fixture()
    parsed = parse_consolidated_lcl_multi_hbl(raw)
    assert parsed is not None

    house_records = [
        validate_and_correct(rec, raw) for rec in parsed["house_records"]
    ]
    master_record = validate_and_correct(parsed["master_record"], raw)
    crm = records_to_master_json(house_records, master_record=master_record)

    assert crm["mesco_masterblno"] == "NSA26030217"
    assert crm["mesco_consolidation"] is True
    houses = crm.get(MASTER_HOUSES_KEY) or []
    assert len(houses) == 4
    house_nos = {h["mesco_masterblno"] for h in houses}
    assert "NAV26MU0821" in house_nos
    assert crm["mesco_masterblno"] not in house_nos

    # Master totals should aggregate all houses (not first house only).
    assert float(crm["cr401_totalgrossweight"]) > 5000
    assert float(crm["cr401_totalvolume"]) > 40
