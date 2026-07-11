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


def test_operation_payload_drops_unsupported_notify_contact_field():
    payload = {
        "mesco_masterblno": "TPSALX20260112",
        "mesco_notifycontactnumber": "TEL.+2 (03) 3991000",
        "mesco_notifyaddress": "8 PATRIC LUMUMBA ST.BAB SHARQ",
    }

    cleaned = du._preprocess_payload(payload, "mesco_operations")

    assert "mesco_notifycontactnumber" not in cleaned
    assert cleaned["mesco_notifyaddress"] == "8 PATRIC LUMUMBA ST.BAB SHARQ"


def test_cargo_schema_keeps_package_unit_lookup():
    cargo_schema = du._ENTITY_SCHEMAS["mesco_cargos"]

    assert cargo_schema["lookups"]["mesco_umpackages"] == "xollsp_unitsofmeasure"
    assert "mesco_umpackages" not in cargo_schema["invalid"]
