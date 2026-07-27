"""Tests for batch extraction validation."""

from extraction_report import validate_pdf_extraction


def test_validate_good_single_record():
    records = [
        {
            "mesco_masterblno": "2025121147",
            "mesco_consigneenamecontactno": "MIDDLE EAST FOR IMP&EXPO",
            "mesco_origin": "NEW YORK",
            "mesco_destination": "ALEXANDRIA",
            "cr401_totalpackages": "2 PALLETS",
            "cr401_totalgrossweight": "1172.58",
            "cr401_totalvolume": "5",
            "mesco_acidnumber": "4358113202025120018",
            "container_number": "CSLU203520/4",
            "mesco_vessel": "BERNHARD SCHULTE",
        }
    ]
    report = validate_pdf_extraction(records, "BILL OF LADING\nHEAVY EQUIPMENT SERVICES 2025121147")
    assert report.passed
    assert report.score >= 60
    assert report.record_count == 1


def test_validate_rejects_form_serial_bl():
    records = [{"mesco_masterblno": "0038260", "mesco_consigneenamecontactno": "TEST CO"}]
    report = validate_pdf_extraction(records, "0038260 BILL OF LADING")
    assert not report.passed
    assert any(i.code == "invalid_master_bl" for i in report.issues)


def test_validate_multi_bl_missing_second():
    raw = (
        "--- PAGE 1 ---\nBILL OF LADING\nHEAVY EQUIPMENT SERVICES 2025121147\n"
        "--- PAGE 2 ---\nBILL OF LADING\nHEAVY EQUIPMENT SERVICES 202512139\n"
    )
    records = [
        {
            "mesco_masterblno": "2025121147",
            "mesco_consigneenamecontactno": "A",
            "mesco_origin": "NEW YORK",
            "mesco_destination": "ALEXANDRIA",
        }
    ]
    report = validate_pdf_extraction(records, raw)
    assert not report.passed
    assert report.document_checks.get("multi_bl_expected") is True
