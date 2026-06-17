"""Registry for deterministic PDF parsers used to stabilize LLM extraction.

The LLM is good at flexible layouts, but fixed parser evidence is more stable
for identifiers, route fields, containers, totals, and house/master structure.
This module normalizes the project-specific PDF parsers into one shape so the
intelligent pipeline can reconcile Gemini/Azure output against trusted evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional


@dataclass
class DeterministicParse:
    parser: str
    layout: str
    records: List[Dict[str, Any]]
    confidence: int
    master_record: Optional[Dict[str, Any]] = None
    document_type: Optional[str] = None

    def reconciliation_records(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if self.master_record:
            master = dict(self.master_record)
            master["_deterministic_role"] = "master"
            out.append(master)
        for record in self.records:
            item = dict(record)
            item["_deterministic_role"] = "house" if self.master_record else "record"
            out.append(item)
        return out


def _nonempty_records(records: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    return [r for r in (records or []) if isinstance(r, dict) and r]


def _single(
    parser: str,
    layout: str,
    record: Optional[Dict[str, Any]],
    confidence: int,
    document_type: str,
) -> Optional[DeterministicParse]:
    if not isinstance(record, dict) or not record:
        return None
    return DeterministicParse(
        parser=parser,
        layout=layout,
        records=[record],
        confidence=confidence,
        document_type=document_type,
    )


def _master_houses(
    parser: str,
    parsed: Optional[Dict[str, Any]],
    confidence: int,
    document_type: str,
) -> Optional[DeterministicParse]:
    if not isinstance(parsed, dict):
        return None
    houses = _nonempty_records(parsed.get("house_records"))
    master = parsed.get("master_record")
    if not houses and not isinstance(master, dict):
        return None
    return DeterministicParse(
        parser=parser,
        layout="master_with_houses",
        records=houses,
        master_record=master if isinstance(master, dict) else None,
        confidence=confidence,
        document_type=document_type,
    )


def _safe_call(fn: Callable[[], Optional[DeterministicParse]]) -> Optional[DeterministicParse]:
    try:
        return fn()
    except Exception:
        return None


def extract_deterministic_parses(
    raw_text: str,
    *,
    filename: Optional[str] = None,
) -> List[DeterministicParse]:
    """Return deterministic parser candidates sorted by confidence."""
    text = raw_text or ""
    candidates: List[DeterministicParse] = []

    def add(candidate: Optional[DeterministicParse]) -> None:
        if candidate and candidate.reconciliation_records():
            candidates.append(candidate)

    from pdf_msds_dg import parse_msds_dg_record
    from pdf_consolidated_lcl import parse_consolidated_lcl_multi_hbl
    from pdf_cargo_manifest import parse_cargo_manifest_hbl_blocks
    from pdf_lcl_export_manifest import parse_export_lcl_manifest
    from pdf_tur_cargo_manifest import parse_tur_cargo_manifest
    from pdf_sea_waybill import is_consolidation_sea_waybill, parse_consolidation_sea_waybill
    from pdf_debit_note import is_freight_debit_note, parse_freight_debit_note
    from pdf_house_bl import is_standard_house_bl, parse_standard_house_bl
    from pdf_standard_master_bl import is_standard_master_bl, parse_standard_master_bl
    from pdf_isaly_draft_bl import detect_isaly_draft_multi_bl, extract_isaly_draft_records
    from pdf_multi_bl import detect_and_extract_multi_bl_records, detect_multi_bl_candidate

    add(
        _safe_call(
            lambda: _single(
                "pdf_msds_dg",
                "single_bl",
                parse_msds_dg_record(text, filename=filename),
                100,
                "msds_dg_pdf",
            )
        )
    )
    add(
        _safe_call(
            lambda: _master_houses(
                "pdf_consolidated_lcl",
                parse_consolidated_lcl_multi_hbl(text),
                98,
                "consolidated_lcl_master_with_houses",
            )
        )
    )
    add(
        _safe_call(
            lambda: _master_houses(
                "pdf_cargo_manifest",
                parse_cargo_manifest_hbl_blocks(text),
                97,
                "cargo_manifest_hbl_blocks_pdf",
            )
        )
    )
    add(
        _safe_call(
            lambda: _master_houses(
                "pdf_export_lcl_manifest",
                parse_export_lcl_manifest(text),
                96,
                "export_lcl_manifest_pdf",
            )
        )
    )
    add(
        _safe_call(
            lambda: _master_houses(
                "pdf_tur_cargo_manifest",
                parse_tur_cargo_manifest(text),
                96,
                "tur_cargo_manifest_pdf",
            )
        )
    )
    if is_consolidation_sea_waybill(text):
        add(
            _safe_call(
                lambda: _single(
                    "pdf_sea_waybill",
                    "single_bl",
                    parse_consolidation_sea_waybill(text),
                    95,
                    "consolidation_sea_waybill_pdf",
                )
            )
        )
    if is_freight_debit_note(text):
        add(
            _safe_call(
                lambda: _single(
                    "pdf_debit_note",
                    "single_bl",
                    parse_freight_debit_note(text),
                    94,
                    "freight_debit_note_pdf",
                )
            )
        )
    if is_standard_house_bl(text):
        add(
            _safe_call(
                lambda: _single(
                    "pdf_house_bl",
                    "single_house",
                    parse_standard_house_bl(text),
                    93,
                    "standard_house_bl_pdf",
                )
            )
        )
    if is_standard_master_bl(text):
        add(
            _safe_call(
                lambda: _single(
                    "pdf_standard_master_bl",
                    "single_bl",
                    parse_standard_master_bl(text),
                    93,
                    "standard_master_bl_pdf",
                )
            )
        )
    if detect_isaly_draft_multi_bl(text):
        records = _nonempty_records(extract_isaly_draft_records(text))
        if records:
            add(
                DeterministicParse(
                    parser="pdf_isaly_draft_bl",
                    layout="multi_bl_pages" if len(records) >= 2 else "single_bl",
                    records=records,
                    confidence=92,
                    document_type="isaly_draft_multi_bl_pdf",
                )
            )
    if detect_multi_bl_candidate(text):
        records = _nonempty_records(detect_and_extract_multi_bl_records(text))
        if records:
            add(
                DeterministicParse(
                    parser="pdf_multi_bl",
                    layout="multi_bl_pages" if len(records) >= 2 else "single_bl",
                    records=records,
                    confidence=90,
                    document_type="multi_bl_pdf",
                )
            )

    candidates.sort(key=lambda c: c.confidence, reverse=True)
    return candidates


def best_deterministic_parse(
    raw_text: str,
    *,
    filename: Optional[str] = None,
) -> Optional[DeterministicParse]:
    candidates = extract_deterministic_parses(raw_text, filename=filename)
    return candidates[0] if candidates else None
