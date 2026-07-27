"""Unit tests for Dataverse lookup label variants."""

from dataverse_uploader import (
    _lookup_search_variants,
    _match_score,
    _normalize_lookup_label,
    _preprocess_payload,
)


class _FakeResponse:
    content = b"x"

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class _FakeLookupClient:
    base_url = "https://fake.crm.dynamics.com/api/data/v9.2"

    def get(self, url, **kwargs):
        if url.startswith("mesco_shippinglines?") and "COSCO" in url.upper():
            return _FakeResponse(
                {
                    "value": [
                        {
                            "mesco_shippinglineid": "11111111-1111-1111-1111-111111111111",
                            "mesco_name": "COSCO",
                        }
                    ]
                }
            )
        if url.startswith("mesco_agents?") and (
            "TP%20CARGO" in url.upper()
            or "TP CARGO" in url.upper()
            or "TRANS%20PACIFIC" in url.upper()
            or "TRANS PACIFIC" in url.upper()
        ):
            return _FakeResponse(
                {
                    "value": [
                        {
                            "mesco_agentid": "22222222-2222-2222-2222-222222222222",
                            "mesco_name": "TP CARGO",
                        }
                    ]
                }
            )
        return _FakeResponse({"value": []})


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


def test_cosco_carrier_variants():
    variants = _lookup_search_variants(
        "mesco_shippingline",
        "COSCO SHIPPING LINES CO.,LTD.",
    )
    assert "COSCO" in variants
    assert "COSCO SHIPPING LINES" in variants


def test_tp_cargo_agent_variants():
    variants = _lookup_search_variants(
        "mesco_agent",
        "TRANS PACIFIC CARGO LIMITED (SHENZHEN)",
    )
    assert "TP CARGO" in variants
    assert "TRANS PACIFIC CARGO" in variants
    assert "TRANS PACIFIC CARGO LIMITED" in variants


def test_master_shippingline_lookup_preserves_voyage_scalar():
    cleaned = _preprocess_payload(
        {
            "mesco_shippingline": "COSCO SHIPPING LINES CO.,LTD.",
            "mesco_voytruckno": "0BEN9W1MA",
        },
        "mesco_operations",
        _FakeLookupClient(),
    )

    assert cleaned["mesco_voytruckno"] == "0BEN9W1MA"
    assert cleaned["mesco_ShippingLine@odata.bind"] == (
        "/mesco_shippinglines(11111111-1111-1111-1111-111111111111)"
    )


def test_tp_cargo_agent_lookup_preprocesses_to_bind():
    cleaned = _preprocess_payload(
        {"mesco_agent": "TRANS PACIFIC CARGO LIMITED (SHENZHEN)"},
        "mesco_operations",
        _FakeLookupClient(),
    )

    assert cleaned["mesco_Agent@odata.bind"] == (
        "/mesco_agents(22222222-2222-2222-2222-222222222222)"
    )
    assert "mesco_agent" not in cleaned


def test_match_score_port_prefix():
    assert _match_score("ALEXANDRIA", "ALEXANDRIA OLD PORT") >= 75
    assert _match_score(
        "MARINE AND ENGINEERING SERVICES COMPANY (MESCO)",
        "MESCO",
    ) >= 60


def test_normalize_lookup_label():
    assert _normalize_lookup_label("Foo (Bar)") == "FOOBAR"
