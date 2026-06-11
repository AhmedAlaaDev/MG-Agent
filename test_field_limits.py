"""Dataverse field length caps — prevent 400 upload failures."""

from dataverse_field_limits import cap_nested_payload, cap_record
from dataverse_uploader import _preprocess_payload, _ENTITY

LONG_HANDLING = (
    "CONSOLIDATED CARGO IN TRANSIT TO MERGHEM BONDED WAREHOUSE VIA ALEXANDRIA PORT, "
    "EGYPT ON CONSIGNEE'S OWN COST, RISK AND ARRANGEMENT. CARRIER LIABILITY CEASES "
    "AT ALEXANDRIA PORT, EGYPT."
)


def test_handling_information_capped_and_spilled_to_notes():
    record = {"mesco_handlinginformation": LONG_HANDLING}
    cap_record("mesco_operations", record)
    assert len(record["mesco_handlinginformation"]) <= 100
    assert LONG_HANDLING in (record.get("mesco_notes") or "")


def test_preprocess_caps_handling_for_upload():
    payload = {
        "mesco_masterblno": "NSA26030217",
        "mesco_handlinginformation": LONG_HANDLING,
    }
    cap_nested_payload(payload)
    fields = _preprocess_payload(payload, _ENTITY, client=None)
    assert len(fields["mesco_handlinginformation"]) <= 100
