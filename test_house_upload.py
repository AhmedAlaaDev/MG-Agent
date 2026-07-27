"""Tests for standalone House B/L upload payload preparation."""

from crm_output_formatter import (
    HOUSE_BL_TYPE,
    MASTER_CARGO_KEY,
    HOUSE_CARGO_KEY,
    apply_bl_type_to_crm_payload,
    prepare_standalone_house_upload,
)
from dataverse_uploader import is_house_bl_type


def test_prepare_standalone_house_creates_cargo_from_operation():
    payload = {
        "mesco_bltype": HOUSE_BL_TYPE,
        "mesco_masterblno": "NAV26MU0714",
        "mesco_cargodescription": "TRIMETHOPRIM EP\n80 DRUMS",
        "cr401_totalpackages": 8,
        "cr401_totalgrossweight": 2299.6,
        "cr401_totalvolume": 7.928,
    }
    prepare_standalone_house_upload(payload)
    assert payload.get("mesco_houseblno") == "NAV26MU0714"
    assert payload.get(MASTER_CARGO_KEY)
    assert payload.get(HOUSE_CARGO_KEY)
    desc = payload[HOUSE_CARGO_KEY][0].get("mesco_descriptionofgoods", "")
    assert "TRIMETHOPRIM" in desc.upper()


def test_apply_bl_type_house_runs_prepare():
    payload = {
        "mesco_masterblno": "HBL123",
        "mesco_cargodescription": "FABRIC ROLLS",
        "cr401_totalpackages": 10,
    }
    apply_bl_type_to_crm_payload(payload, "house")
    assert payload["mesco_bltype"] == HOUSE_BL_TYPE
    assert payload.get(HOUSE_CARGO_KEY)


def test_is_house_bl_type():
    assert is_house_bl_type(HOUSE_BL_TYPE)
    assert is_house_bl_type("house")
    assert not is_house_bl_type(886150001)


if __name__ == "__main__":
    test_prepare_standalone_house_creates_cargo_from_operation()
    test_apply_bl_type_house_runs_prepare()
    test_is_house_bl_type()
    print("All house upload tests passed.")
