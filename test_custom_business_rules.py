"""Tests for toggleable CRM business rules."""

from custom_business_rules import (
    BOOKING_FREEHAND_OPT,
    BOOKING_NOMINATION_OPT,
    FREIGHT_AT_DESTINATION,
    FREIGHT_AT_ORIGIN,
    LCL_LOAD_TYPE,
    FCL_LOAD_TYPE,
    PREPAID_OPT,
    apply_crm_payload_rules,
    apply_freight_booking_rules,
    apply_record_business_rules,
    custom_rules_enabled,
    infer_load_type,
    prepare_crm_payload_for_upload,
    reconcile_master_totals_from_houses,
    sum_house_operation_totals,
    use_custom_rules,
)
from crm_output_formatter import records_to_master_json
from test_cargo_manifest import RAW as NSA_MANIFEST_RAW
from pdf_cargo_manifest import parse_cargo_manifest_hbl_blocks


def test_freight_prepaid_maps_to_freehand_and_origin():
    data = {"mesco_pcfreightterm": "PREPAID"}
    apply_freight_booking_rules(data)
    assert data["mesco_bookingterm"] == BOOKING_FREEHAND_OPT
    assert data["mesco_freightpayableat"] == FREIGHT_AT_ORIGIN


def test_freight_collect_maps_to_nomination_and_destination():
    data = {"mesco_pcfreightterm": "Collect"}
    apply_freight_booking_rules(data)
    assert data["mesco_bookingterm"] == BOOKING_NOMINATION_OPT
    assert data["mesco_freightpayableat"] == FREIGHT_AT_DESTINATION


def test_lcl_house_teus_zero():
    data = {"mesco_loadtype": LCL_LOAD_TYPE, "cr401_totalteus": 2}
    apply_record_business_rules(data, is_house=True)
    assert data["cr401_totalteus"] == 0


def test_manifest_infers_lcl():
    parsed = parse_cargo_manifest_hbl_blocks(NSA_MANIFEST_RAW)
    house = parsed["house_records"][0]
    assert infer_load_type(house, NSA_MANIFEST_RAW) == LCL_LOAD_TYPE


def test_fcl_straight_bl():
    data = {
        "containers": [{"container_number": "MSCU1234567"}],
        "mesco_cargodescription": "FULL CONTAINER LOAD STEEL COILS",
    }
    assert infer_load_type(data, "FCL SHIPMENT") == FCL_LOAD_TYPE


def test_house_totals_sum():
    houses = [
        {"cr401_totalgrossweight": 100, "cr401_totalpackages": 10, "cr401_totalvolume": 1.5},
        {"cr401_totalgrossweight": 200, "cr401_totalpackages": 20, "cr401_totalvolume": 2.5},
    ]
    totals = sum_house_operation_totals(houses)
    assert totals["cr401_totalgrossweight"] == 300
    assert totals["cr401_totalpackages"] == 30
    assert totals["cr401_totalvolume"] == 4.0


def test_rules_can_be_disabled_per_request():
    with use_custom_rules(False):
        assert custom_rules_enabled() is False
        data = {"mesco_pcfreightterm": "PREPAID"}
        apply_record_business_rules(data, enabled=None)
        assert "mesco_bookingterm" not in data
    assert custom_rules_enabled() is True


def test_crm_master_manifest_totals_and_house_teus():
    parsed = parse_cargo_manifest_hbl_blocks(NSA_MANIFEST_RAW)
    crm = records_to_master_json(parsed["house_records"], master_record=parsed["master_record"])
    assert crm["mesco_loadtype"] == LCL_LOAD_TYPE
    assert crm["cr401_totalgrossweight"] == 23935.75
    assert crm["cr401_totalpackages"] == 467
    assert crm["cr401_totalteus"] == 1
    houses = crm["mesco_Operation_mesco_Operation_mesco_Operation"]
    assert len(houses) == 5
    for h in houses:
        assert h.get("cr401_totalteus") == 0
        assert h.get("mesco_bookingterm") == BOOKING_FREEHAND_OPT or h.get("mesco_pcfreightterm")


def test_reconcile_master_from_houses():
    master = {}
    houses = [
        {"cr401_totalgrossweight": 2299.6, "cr401_totalpackages": 80},
        {"cr401_totalgrossweight": 9200, "cr401_totalpackages": 320},
    ]
    reconcile_master_totals_from_houses(master, houses)
    assert master["cr401_totalgrossweight"] == 11499.6
    assert master["cr401_totalpackages"] == 400
    assert master["mesco_nooforgbls"] == "2"


def test_upload_prep_propagates_prepaid_and_booking_to_houses():
    payload = {
        "mesco_masterblno": "NSA26030217",
        "mesco_loadtype": LCL_LOAD_TYPE,
        "mesco_pcfreightterm": PREPAID_OPT,
        "mesco_Operation_mesco_Operation_mesco_Operation": [
            {"mesco_masterblno": "NAV26MU0800", "mesco_bltype": 886150002},
            {"mesco_masterblno": "NAV26MU0714", "mesco_bltype": 886150002},
        ],
    }
    prepare_crm_payload_for_upload(payload)
    houses = payload["mesco_Operation_mesco_Operation_mesco_Operation"]
    for house in houses:
        assert house["mesco_pcfreightterm"] == PREPAID_OPT
        assert house["mesco_bookingterm"] == BOOKING_FREEHAND_OPT
        assert house["mesco_freightpayableat"] == FREIGHT_AT_ORIGIN
        assert house["cr401_totalteus"] == 0
    assert payload["mesco_bookingterm"] == BOOKING_FREEHAND_OPT
    assert payload["mesco_freightpayableat"] == FREIGHT_AT_ORIGIN
