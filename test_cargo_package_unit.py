from crm_output_formatter import _build_cargo_from_record, infer_package_unit_label
from dataverse_uploader import _apply_cargo_package_unit_hint


def test_infers_contained_drums_over_outer_pallets():
    text = (
        "08 PALLETS STC 80 DRUMS (EIGHT PALLETS SAID TO CONTAIN "
        "EIGHTY DRUMS) NEW FIBRE DRUMS"
    )

    assert infer_package_unit_label(text) == "DRUMS"


def test_cargo_record_sets_um_packages_lookup_hint():
    cargo = _build_cargo_from_record(
        {
            "cr401_totalpackages": 8,
            "cr401_totalgrossweight": 2299.6,
            "mesco_cargodescription": (
                "08 PALLETS STC 80 DRUMS TRIMETHOPRIM EP HS CODE: 29335995"
            ),
        }
    )

    assert cargo["mesco_noofpackages"] == 8
    assert cargo["mesco_umpackages"] == "DRUMS"


def test_upload_fallback_sets_um_packages_when_formatter_did_not():
    cargo = {
        "mesco_noofpackages": 8,
        "mesco_descriptionofgoods": (
            "08 PALLETS STC 80 DRUMS TRIMETHOPRIM EP HS CODE: 29335995"
        ),
    }

    _apply_cargo_package_unit_hint(cargo)

    assert cargo["mesco_umpackages"] == "DRUMS"
