"""Tests for the general deterministic PDF parser registry."""

from pdf_deterministic_registry import best_deterministic_parse
from test_cargo_manifest import RAW as CARGO_MANIFEST_SAMPLE
from test_sea_waybill import COSCO_SAMPLE
from test_standard_house_bl import TPALX_SAMPLE


def test_registry_detects_cosco_sea_waybill():
    parsed = best_deterministic_parse(COSCO_SAMPLE)

    assert parsed
    assert parsed.parser == "pdf_sea_waybill"
    assert parsed.layout == "single_bl"
    assert parsed.records[0]["mesco_voytruckno"] == "0BEN9W1MA"


def test_registry_detects_manifest_houses_without_collapsing():
    parsed = best_deterministic_parse(CARGO_MANIFEST_SAMPLE)

    assert parsed
    assert parsed.layout == "master_with_houses"
    assert parsed.master_record["mesco_masterblno"] == "NSA26030217"
    assert [r["mesco_houseblno"] for r in parsed.records] == [
        "NAV26MU0714",
        "NAV26MU0821",
        "NAV26MU0795",
        "NAV26MU0794",
        "NAV26MU0800",
    ]


def test_registry_detects_standard_house_bl():
    parsed = best_deterministic_parse(TPALX_SAMPLE)

    assert parsed
    assert parsed.parser == "pdf_house_bl"
    assert parsed.layout == "single_house"
    assert parsed.records[0]["mesco_houseblno"] == "TPALX2602005"
    assert parsed.records[0]["mesco_agent"] == "TRANS PACIFIC CARGO LIMITED (SHENZHEN)"
