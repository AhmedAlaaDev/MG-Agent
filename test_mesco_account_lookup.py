from dataverse_uploader import _lookup_search_variants
from crm_output_formatter import _apply_operation_lookups, _derive_operation_lookups


def test_mesco_account_variants_prioritize_canonical_account():
    variants = _lookup_search_variants("account", "MESCO PARCEL-G")

    assert variants[0] == "MARINE AND ENGINEERING SERVICES COMPANY (MESCO)"
    assert "MESCO PARCEL-G" in variants
    assert variants.index("MARINE AND ENGINEERING SERVICES COMPANY") < variants.index(
        "MESCO PARCEL-G"
    )


def test_notify_mesco_uses_same_canonical_account_as_consignee():
    src = {
        "mesco_consignee": "Mesco - Marine and engineering services co.",
        "mesco_notify1": "MESCO PARCEL-G",
        "mesco_notifyaddress": "8 PATRICE LUMUMBA ST. BAB SHARQ ALEXANDRIA",
    }

    lookups = _derive_operation_lookups(src)

    assert lookups["mesco_consignee"] == "MARINE AND ENGINEERING SERVICES COMPANY (MESCO)"
    assert lookups["mesco_notify1"] == "MARINE AND ENGINEERING SERVICES COMPANY (MESCO)"

    op = {"mesco_notify1": "MESCO PARCEL-G"}
    _apply_operation_lookups(op, src)
    assert op["mesco_notify1"] == "MARINE AND ENGINEERING SERVICES COMPANY (MESCO)"
