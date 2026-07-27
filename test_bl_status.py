"""Tests for BL Status (Original vs Telex) inference."""

from bl_status_rules import BL_STATUS_ORIGINAL, BL_STATUS_TELEX, infer_bl_status


def test_zero_originals_implies_telex():
    data = {"mesco_nooforgbls": "0"}
    text = "NUMBER OF ORIGINAL BILLS OF LADING ZERO (0)"
    out = infer_bl_status(data, text)
    assert out["mesco_blstatus"] == BL_STATUS_TELEX
    assert out["mesco_telexrelease"] is True


def test_express_bill_implies_telex():
    data = {}
    text = "DESTINATION O/EXPRESS\nEXPRESS BILL OF LADING"
    out = infer_bl_status(data, text)
    assert out["mesco_blstatus"] == BL_STATUS_TELEX


def test_original_presentation_required():
    data = {"mesco_nooforgbls": "3"}
    text = (
        "DON'T RELEASE THIS SHIPMENT WITHOUT PRESENTATION OF ORIGINAL B/L, "
        "AS THE TRADE COULD BE ON T/T TERM"
    )
    out = infer_bl_status(data, text)
    assert out["mesco_blstatus"] == BL_STATUS_ORIGINAL
    assert out["mesco_telexrelease"] is False


def test_three_original_bl_count():
    data = {"mesco_nooforgbls": "3"}
    text = "THREE (3) ORIGINAL BILLS OF LADING"
    out = infer_bl_status(data, text)
    assert out["mesco_blstatus"] == BL_STATUS_ORIGINAL


def test_debit_note_skipped():
    data = {"document_type": "debit_note", "mesco_telexrelease": False}
    out = infer_bl_status(data, "TELEX RELEASE")
    assert "mesco_blstatus" not in out or out.get("mesco_blstatus") is None
