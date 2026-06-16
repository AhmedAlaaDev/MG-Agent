"""Regression tests for Dataverse payload field filtering."""

import dataverse_uploader as du


def test_operation_payload_drops_cargo_package_unit_field():
    payload = {
        "mesco_masterblno": "TPALX2602005",
        "cr401_totalpackages": "5",
        "mesco_umpackages": "PACKAGES",
    }

    cleaned = du._preprocess_payload(payload, "mesco_operations")

    assert cleaned["cr401_totalpackages"] == 5
    assert "mesco_umpackages" not in cleaned


def test_cargo_schema_keeps_package_unit_lookup():
    cargo_schema = du._ENTITY_SCHEMAS["mesco_cargos"]

    assert cargo_schema["lookups"]["mesco_umpackages"] == "xollsp_unitsofmeasure"
    assert "mesco_umpackages" not in cargo_schema["invalid"]
