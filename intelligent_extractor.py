"""
Unified intelligent extraction pipeline.

This is the single entry-point that the FastAPI endpoints should call.
It composes the existing parts into a deterministic, well-defined flow:

    PDF bytes / raw text
        │
        ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │ 1. Text extraction (OCR / native PDF) — owned by main.py            │
  └──────────────────────────────────────────────────────────────────────┘
        │
        ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │ 2. document_parser.parse_document_intelligently                     │
  │    - layout detection                                                │
  │    - Azure call(s) + per-PDF deterministic parsers                   │
  │    - per-page validation, enrichment, regex fallbacks                │
  └──────────────────────────────────────────────────────────────────────┘
        │
        ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │ 3. intelligent_reconciler  (new — explicit precedence)              │
  │    Cross-checks Azure vs deterministic per-field where both ran.    │
  │    Kept idempotent so existing parser output flows through unchanged │
  │    when only one source produced a value.                            │
  └──────────────────────────────────────────────────────────────────────┘
        │
        ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │ 4. crm_output_formatter.records_to_master_json                      │
  │    Projects records into the master.json / house.json schema        │
  │    (with nested containers + cargos).                                │
  └──────────────────────────────────────────────────────────────────────┘
        │
        ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │ 5. dataverse_field_limits.cap_nested_payload  (new)                 │
  │    Final defensive truncation against Dataverse string column caps  │
  │    so a single oversized field never blocks the entire save again.  │
  └──────────────────────────────────────────────────────────────────────┘
        │
        ▼
       result

Every step is optional and can be skipped via flags — the heavy lifting still
lives in the existing modules. This file intentionally contains *no* parsing
logic, only orchestration + reconciliation glue.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from crm_output_formatter import records_to_house_json, records_to_master_json
from dataverse_field_limits import cap_nested_payload, fields_exceeding_limits
from document_parser import IntelligentParseResult, parse_document_intelligently
from intelligent_reconciler import reconcile_records

logger = logging.getLogger(__name__)


@dataclass
class ExtractionResult:
    """Final result returned to FastAPI endpoints."""

    records: List[Dict[str, Any]] = field(default_factory=list)
    crm_master: Dict[str, Any] = field(default_factory=dict)
    crm_houses: List[Dict[str, Any]] = field(default_factory=list)
    crm_masters_split: List[Dict[str, Any]] = field(default_factory=list)
    document_layout: str = "unknown"
    quality: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    field_limit_overflows: List[Dict[str, Any]] = field(default_factory=list)

    def to_response(self) -> Dict[str, Any]:
        return {
            "document_layout": self.document_layout,
            "quality": self.quality,
            "warnings": self.warnings,
            "records": self.records,
            "crm_master": self.crm_master,
            "crm_houses": self.crm_houses,
            "crm_masters_split": self.crm_masters_split,
            "field_limit_overflows": self.field_limit_overflows,
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_intelligent(
    raw_text: str,
    *,
    extracted_meta: Optional[Dict[str, Any]] = None,
    pdf_bytes: Optional[bytes] = None,
    split_master_per_record: bool = False,
    azure_records_override: Optional[List[Dict[str, Any]]] = None,
) -> ExtractionResult:
    """
    Run the full intelligent pipeline against ``raw_text``.

    Parameters
    ----------
    raw_text
        OCR / native-PDF text (with ``--- PAGE N ---`` markers preserved).
    extracted_meta
        Optional context from the OCR / PDF extractor (e.g. method, page count).
    pdf_bytes
        Original PDF bytes — passed through so deterministic per-PDF parsers
        can re-OCR specific pages if needed.
    split_master_per_record
        When True, return one master payload per record under
        ``crm_masters_split`` (used by the batch / multi-B/L test endpoints).
    azure_records_override
        Optional pre-baked Azure record list — used by tests / mock pipelines
        when Azure credentials are unavailable. Reconciled against whatever
        the deterministic parsers produced.
    """
    parse_result = parse_document_intelligently(
        raw_text, extracted_meta=extracted_meta, file_bytes=pdf_bytes, filename=(
            (extracted_meta or {}).get("filename") if isinstance(extracted_meta, dict) else None
        ),
    )

    records = list(parse_result.records or [])

    # Apply cross-source reconciliation when an alternate Azure record list is
    # provided. In production the heavy lifting already happened inside
    # parse_document_intelligently; this hook lets us layer additional Azure
    # output (e.g. a second-pass Azure call with a stricter prompt) on top.
    if azure_records_override:
        records = reconcile_records(
            deterministic_records=records,
            azure_records=azure_records_override,
        )

    result = ExtractionResult(
        document_layout=parse_result.document_layout,
        quality=dict(parse_result.quality or {}),
        warnings=list(parse_result.azure_warnings or []),
        records=records,
    )

    if not records:
        return result

    # ---- Master / house projection ---------------------------------------
    crm_master = records_to_master_json(records)
    crm_houses = records_to_house_json(records)

    if split_master_per_record:
        result.crm_masters_split = [records_to_master_json([r]) for r in records]

    # ---- Dataverse field-limit enforcement --------------------------------
    crm_master = cap_nested_payload(crm_master)
    for split in result.crm_masters_split:
        cap_nested_payload(split)

    # Diagnose any limits that *would* have overflowed if a future change
    # widens the input again. The cap was already applied — this is purely
    # informational for the response payload.
    overflows: List[Dict[str, Any]] = []
    for ent_name, payload in (("mesco_operations", crm_master),):
        for entry in fields_exceeding_limits(ent_name, payload):
            entry["entity"] = ent_name
            entry["scope"] = "master"
            overflows.append(entry)
    result.field_limit_overflows = overflows

    result.crm_master = crm_master
    result.crm_houses = crm_houses

    if not result.quality.get("document_type_detected"):
        result.quality["document_type_detected"] = (
            "multi_bl_pdf" if len(records) >= 2 else "single_bl_pdf"
        )

    return result


def extract_from_parse_result(
    parse_result: IntelligentParseResult,
    *,
    split_master_per_record: bool = False,
) -> ExtractionResult:
    """
    Project an already-computed ``IntelligentParseResult`` through the rest of
    the pipeline (CRM formatter + field-limit caps). Used by code paths in
    ``main.py`` that already called ``parse_document_intelligently`` directly.
    """
    result = ExtractionResult(
        document_layout=parse_result.document_layout,
        quality=dict(parse_result.quality or {}),
        warnings=list(parse_result.azure_warnings or []),
        records=list(parse_result.records or []),
    )

    if not result.records:
        return result

    crm_master = records_to_master_json(result.records)
    crm_houses = records_to_house_json(result.records)

    if split_master_per_record:
        result.crm_masters_split = [records_to_master_json([r]) for r in result.records]

    crm_master = cap_nested_payload(crm_master)
    for split in result.crm_masters_split:
        cap_nested_payload(split)

    result.crm_master = crm_master
    result.crm_houses = crm_houses
    return result
