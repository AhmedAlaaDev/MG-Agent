"""Unit tests for Dataverse lookup label variants (no API calls)."""

from dataverse_uploader import _lookup_search_variants, _match_score, _normalize_lookup_label


def test_mesco_account_variants():
    variants = _lookup_search_variants(
        "account",
        "MARINE AND ENGINEERING SERVICES COMPANY (MESCO)",
    )
    assert "MESCO" in variants
    assert any("MARINE" in v.upper() for v in variants)


def test_alexandria_port_variants():
    variants = _lookup_search_variants("xollsp_address", "ALEXANDRIA")
    assert "ALEXANDRIA OLD PORT" in variants


def test_evergreen_carrier_variants():
    variants = _lookup_search_variants(
        "mesco_shippingline",
        "Evergreen Marine (Asia) Pte. Ltd.",
    )
    assert "EVERGREEN LINE" in variants


def test_match_score_port_prefix():
    assert _match_score("ALEXANDRIA", "ALEXANDRIA OLD PORT") >= 75
    assert _match_score(
        "MARINE AND ENGINEERING SERVICES COMPANY (MESCO)",
        "MESCO",
    ) >= 60


def test_normalize_lookup_label():
    assert _normalize_lookup_label("Foo (Bar)") == "FOOBAR"
