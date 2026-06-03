"""
Smoke tests for the unified intelligent extraction pipeline.

These tests do NOT require Azure OpenAI credentials.  ``parse_document_intelligently``
is monkey-patched to return a deterministic IntelligentParseResult so we can
assert the orchestration glue (reconciliation, CRM projection, Dataverse field
limit enforcement, response shape) behaves correctly.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

import intelligent_extractor as ie
from dataverse_field_limits import OPERATION_LIMITS, cap_field
from document_parser import IntelligentParseResult
from intelligent_reconciler import reconcile_record, reconcile_records


# ---------------------------------------------------------------------------
# Field-limit registry
# ---------------------------------------------------------------------------

def test_cargo_description_capped_to_1500():
    raw = "CARGO DESCRIPTION SECTION. " * 200  # ~ 5400 chars
    out = cap_field("mesco_operations", "mesco_cargodescription", raw)
    assert len(out) <= OPERATION_LIMITS["mesco_cargodescription"] == 1500
    # Should cut at a sentence boundary, not mid-word
    assert not out.endswith(" CARGO DESCRIPTION SECT")


def test_party_name_capped_to_100():
    out = cap_field("mesco_operations", "mesco_shippernamecontactno", "X" * 250)
    assert len(out) == 100


# ---------------------------------------------------------------------------
# Reconciler
# ---------------------------------------------------------------------------

def test_reconciler_prefers_cleaner_consignee():
    det = {
        "mesco_masterblno": "SHZ7922638",
        "mesco_consigneenamecontactno": "MARINE & ENGINEERING SERVICES COMPANY - MESCO",
    }
    az = {
        "mesco_masterblno": "SHZ7922638",
        "mesco_consigneenamecontactno": "EXPORT REFERENCES",
    }
    merged = reconcile_record(deterministic=det, azure=az)
    assert "MESCO" in merged["mesco_consigneenamecontactno"]
    assert merged["_field_sources"]["mesco_consigneenamecontactno"] == "deterministic"


def test_reconciler_unions_hs_codes_across_sources():
    det = {"mesco_hscode": "85334000|8477590000"}
    az = {"mesco_hscode": "3407001000|39269090|84198100|85334000"}
    merged = reconcile_record(deterministic=det, azure=az)
    codes = merged["mesco_hscode"].split("|")
    assert "85334000" in codes
    assert "8477590000" in codes
    assert "3407001000" in codes
    assert "84198100" in codes
    assert len(set(codes)) == len(codes), "HS codes should be deduplicated"


def test_reconciler_takes_clean_deterministic_acid_over_corrupted_azure():
    det = {"mesco_acidnumber": "6408316721009110053"}  # exactly 19
    az = {"mesco_acidnumber": "6408316721009110053182"}  # corrupted with KGS suffix
    merged = reconcile_record(deterministic=det, azure=az)
    assert merged["mesco_acidnumber"] == "6408316721009110053"
    assert merged["_field_sources"]["mesco_acidnumber"] == "deterministic"


def test_reconciler_pairs_records_by_house_bl():
    det_list = [
        {"mesco_houseblno": "ISALY2604028", "mesco_acidnumber": "1234567890123456789"},
        {"mesco_houseblno": "ISALY2604050", "mesco_acidnumber": "5188153581009310016"},
    ]
    az_list = [
        {"mesco_houseblno": "ISALY2604028", "mesco_dateofissue": "27 APR 2026"},
        {"mesco_houseblno": "ISALY2604050", "mesco_dateofissue": "27 APR 2026"},
    ]
    out = reconcile_records(deterministic_records=det_list, azure_records=az_list)
    assert len(out) == 2
    by_bl = {r["mesco_houseblno"]: r for r in out}
    assert by_bl["ISALY2604028"]["mesco_acidnumber"] == "1234567890123456789"
    assert by_bl["ISALY2604028"]["mesco_dateofissue"] == "27 APR 2026"
    assert by_bl["ISALY2604050"]["mesco_acidnumber"] == "5188153581009310016"


# ---------------------------------------------------------------------------
# End-to-end orchestrator (no Azure)
# ---------------------------------------------------------------------------

def _fake_parse_result(records: List[Dict[str, Any]]) -> IntelligentParseResult:
    return IntelligentParseResult(
        records=records,
        document_layout="single_bl",
        quality={"parser": "intelligent", "azure_attempted": False},
        azure_warnings=[],
    )


def test_orchestrator_caps_oversized_cargo_in_nested_payload(monkeypatch):
    huge_cargo = "STEEL COILS DELIVERED TO ALEXANDRIA. " * 100  # ~3700 chars
    fake = _fake_parse_result(
        [
            {
                "mesco_masterblno": "SHZ7922638",
                "mesco_houseblno": None,
                "mesco_consigneenamecontactno": "MARINE & ENGINEERING SERVICES COMPANY - MESCO",
                "mesco_consigneeaddress": "8 PATRICE LUMUMBA ST. BAB SHARQ, ALEXANDRIA - EGYPT",
                "mesco_shippernamecontactno": "TRANS PACIFIC CARGO LIMITED",
                "mesco_cargodescription": huge_cargo,
                "mesco_hscode": "85334000|3407001000",
                "cr401_totalpackages": "179",
                "cr401_totalgrossweight": "14801.730",
                "cr401_totalvolume": "50.657",
                "container_number": "TLLU4178846",
                "containers": [],
            }
        ]
    )
    monkeypatch.setattr(ie, "parse_document_intelligently", lambda *a, **kw: fake)

    result = ie.extract_intelligent("some raw text")

    assert result.records, "orchestrator must return at least one record"
    assert result.crm_master, "orchestrator must produce a master payload"
    master_cargo = result.crm_master.get("mesco_cargodescription") or ""
    assert len(master_cargo) <= 1500, (
        f"Master cargo description {len(master_cargo)} exceeded Dataverse limit of 1500"
    )
    assert not result.field_limit_overflows, (
        f"Unexpected overflows after capping: {result.field_limit_overflows}"
    )


def test_orchestrator_returns_empty_when_no_records(monkeypatch):
    monkeypatch.setattr(
        ie, "parse_document_intelligently", lambda *a, **kw: _fake_parse_result([]),
    )
    result = ie.extract_intelligent("noise")
    assert result.records == []
    assert result.crm_master == {}
    assert result.crm_houses == []


def test_orchestrator_split_master_per_record(monkeypatch):
    fake = _fake_parse_result(
        [
            {"mesco_masterblno": "BL1", "containers": []},
            {"mesco_masterblno": "BL2", "containers": []},
        ]
    )
    monkeypatch.setattr(ie, "parse_document_intelligently", lambda *a, **kw: fake)
    result = ie.extract_intelligent("x", split_master_per_record=True)
    assert len(result.crm_masters_split) == 2


def test_orchestrator_applies_azure_override(monkeypatch):
    det = [{"mesco_masterblno": "BL1", "mesco_consigneenamecontactno": "MESCO", "containers": []}]
    fake = _fake_parse_result(det)
    monkeypatch.setattr(ie, "parse_document_intelligently", lambda *a, **kw: fake)

    az_override = [
        {
            "mesco_masterblno": "BL1",
            "mesco_consigneenamecontactno": "EXPORT REFERENCES",  # should lose
            "mesco_dateofissue": "01 JAN 2026",  # should win
        }
    ]
    result = ie.extract_intelligent("x", azure_records_override=az_override)
    assert result.records
    rec = result.records[0]
    assert rec["mesco_consigneenamecontactno"] == "MESCO"
    assert rec["mesco_dateofissue"] == "01 JAN 2026"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
