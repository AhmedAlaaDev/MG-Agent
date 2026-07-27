"""
Quality validation for extracted B/L records (batch PDF testing).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from bl_number_rules import is_form_or_serial_bl_candidate, list_canonical_page_bls
from pdf_multi_bl import (
    detect_and_extract_multi_bl_records,
    detect_multi_bl_candidate,
)


@dataclass
class ValidationIssue:
    level: str  # critical | warning | info
    code: str
    message: str
    field: Optional[str] = None
    record_index: Optional[int] = None


@dataclass
class RecordValidationSummary:
    index: int
    mesco_masterblno: Optional[str]
    passed: bool
    score: int
    issues: List[ValidationIssue] = field(default_factory=list)
    fields_present: Dict[str, bool] = field(default_factory=dict)


@dataclass
class ExtractionValidationReport:
    passed: bool
    score: int
    record_count: int
    issues: List[ValidationIssue] = field(default_factory=list)
    records: List[RecordValidationSummary] = field(default_factory=list)
    document_checks: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "score": self.score,
            "record_count": self.record_count,
            "issues": [
                {
                    "level": i.level,
                    "code": i.code,
                    "message": i.message,
                    "field": i.field,
                    "record_index": i.record_index,
                }
                for i in self.issues
            ],
            "records": [
                {
                    "index": r.index,
                    "mesco_masterblno": r.mesco_masterblno,
                    "passed": r.passed,
                    "score": r.score,
                    "fields_present": r.fields_present,
                    "issues": [
                        {
                            "level": i.level,
                            "code": i.code,
                            "message": i.message,
                            "field": i.field,
                        }
                        for i in r.issues
                    ],
                }
                for r in self.records
            ],
            "document_checks": self.document_checks,
        }


def _has(val: Any) -> bool:
    return val is not None and val != "" and val != []


def _issue(
    level: str,
    code: str,
    message: str,
    *,
    field: Optional[str] = None,
    record_index: Optional[int] = None,
) -> ValidationIssue:
    return ValidationIssue(
        level=level,
        code=code,
        message=message,
        field=field,
        record_index=record_index,
    )


def _validate_single_record(
    rec: Dict[str, Any],
    index: int,
    page_text: str = "",
) -> RecordValidationSummary:
    issues: List[ValidationIssue] = []
    bl = rec.get("mesco_masterblno")
    bl_str = str(bl).strip() if bl else ""

    fields_present = {
        "mesco_masterblno": _has(bl),
        "mesco_consigneenamecontactno": _has(rec.get("mesco_consigneenamecontactno")),
        "mesco_shippernamecontactno": _has(rec.get("mesco_shippernamecontactno")),
        "mesco_origin": _has(rec.get("mesco_origin")),
        "mesco_destination": _has(rec.get("mesco_destination")),
        "mesco_acidnumber": _has(rec.get("mesco_acidnumber")),
        "cr401_totalpackages": _has(rec.get("cr401_totalpackages")),
        "cr401_totalgrossweight": _has(rec.get("cr401_totalgrossweight")),
        "cr401_totalvolume": _has(rec.get("cr401_totalvolume")),
        "container_number": _has(rec.get("container_number"))
        or bool(rec.get("containers")),
        "mesco_vessel": _has(rec.get("mesco_vessel")),
    }

    if not bl_str:
        issues.append(
            _issue("critical", "missing_master_bl", "Master B/L number is missing.", field="mesco_masterblno", record_index=index)
        )
    elif is_form_or_serial_bl_candidate(bl_str, page_text):
        issues.append(
            _issue(
                "critical",
                "invalid_master_bl",
                f"B/L number '{bl_str}' looks like a form/serial reference, not an ocean B/L.",
                field="mesco_masterblno",
                record_index=index,
            )
        )

    hs = rec.get("mesco_hscode")
    if hs and bl_str and re.sub(r"\D", "", str(hs)) == re.sub(r"\D", "", bl_str):
        issues.append(
            _issue(
                "critical",
                "hscode_equals_bl",
                "HS code matches B/L number (likely mis-extraction).",
                field="mesco_hscode",
                record_index=index,
            )
        )

    if not fields_present["mesco_consigneenamecontactno"]:
        issues.append(
            _issue("critical", "missing_consignee", "Consignee is missing.", field="mesco_consigneenamecontactno", record_index=index)
        )

    consignee = (rec.get("mesco_consigneenamecontactno") or "").upper()
    if consignee and any(
        k in consignee for k in ("MESCO MARINE", "DELIVERY AGENT", "FOR DELIVERY PLEASE APPLY")
    ):
        issues.append(
            _issue(
                "warning",
                "consignee_looks_like_agent",
                "Consignee may be delivery agent instead of importer.",
                field="mesco_consigneenamecontactno",
                record_index=index,
            )
        )

    if not fields_present["mesco_origin"]:
        issues.append(
            _issue("warning", "missing_origin", "Port of loading / origin is missing.", field="mesco_origin", record_index=index)
        )
    if not fields_present["mesco_destination"]:
        issues.append(
            _issue("warning", "missing_destination", "Destination is missing.", field="mesco_destination", record_index=index)
        )

    dest = (rec.get("mesco_destination") or "").upper()
    if dest and any(p in dest for p in ("ALEXANDRIA", "PORT SAID", "SOKHNA", "DAMIETTA")):
        if not fields_present["mesco_acidnumber"]:
            issues.append(
                _issue("warning", "missing_acid", "Egypt import but ACID number is missing.", field="mesco_acidnumber", record_index=index)
            )

    if not fields_present["cr401_totalpackages"]:
        issues.append(
            _issue("warning", "missing_packages", "Package count is missing.", field="cr401_totalpackages", record_index=index)
        )
    elif str(rec.get("cr401_totalpackages", "")).upper() in ("ALLETS", "PALLETS"):
        issues.append(
            _issue(
                "warning",
                "packages_ocr_garbled",
                f"Packages value looks like OCR garbage: {rec.get('cr401_totalpackages')}",
                field="cr401_totalpackages",
                record_index=index,
            )
        )

    if not fields_present["cr401_totalgrossweight"]:
        issues.append(
            _issue("warning", "missing_gross_weight", "Gross weight is missing.", field="cr401_totalgrossweight", record_index=index)
        )

    if rec.get("_error"):
        issues.append(
            _issue("critical", "record_processing_error", str(rec["_error"]), record_index=index)
        )

    for w in rec.get("warnings") or []:
        issues.append(_issue("info", "validator_warning", str(w), record_index=index))

    critical = sum(1 for i in issues if i.level == "critical")
    warning = sum(1 for i in issues if i.level == "warning")
    info = sum(1 for i in issues if i.level == "info")
    score = max(0, min(100, 100 - critical * 30 - warning * 10 - info * 2))
    passed = critical == 0 and score >= 60

    return RecordValidationSummary(
        index=index,
        mesco_masterblno=bl_str or None,
        passed=passed,
        score=score,
        issues=issues,
        fields_present=fields_present,
    )


def validate_pdf_extraction(
    records: List[Dict[str, Any]],
    raw_text: str,
    *,
    crm_masters: Optional[List[Dict[str, Any]]] = None,
    page_texts: Optional[List[str]] = None,
) -> ExtractionValidationReport:
    """Validate extracted records and document-level consistency."""
    doc_issues: List[ValidationIssue] = []
    record_summaries: List[RecordValidationSummary] = []

    if not records:
        doc_issues.append(
            _issue("critical", "no_records", "No B/L records were extracted from the document.")
        )
        return ExtractionValidationReport(
            passed=False,
            score=0,
            record_count=0,
            issues=doc_issues,
            records=[],
            document_checks={"multi_bl_expected": detect_multi_bl_candidate(raw_text)},
        )

    multi_expected = detect_multi_bl_candidate(raw_text)
    canonical_pages = list_canonical_page_bls(raw_text)
    canonical_fallback = detect_and_extract_multi_bl_records(raw_text) or []

    document_checks: Dict[str, Any] = {
        "multi_bl_expected": multi_expected,
        "canonical_page_bl_count": len(canonical_pages),
        "canonical_bl_numbers": [e["bl"] for e in canonical_pages],
        "extracted_record_count": len(records),
    }

    if multi_expected:
        expected_count = max(len(canonical_pages), len(canonical_fallback), 2)
        if len(records) < expected_count:
            doc_issues.append(
                _issue(
                    "critical",
                    "multi_bl_under_extracted",
                    f"Document has {expected_count} page B/L(s) but only {len(records)} record(s) extracted.",
                )
            )
        elif len(records) > expected_count + 1:
            doc_issues.append(
                _issue(
                    "warning",
                    "multi_bl_over_extracted",
                    f"Expected ~{expected_count} B/L(s) but got {len(records)} records (possible duplicates).",
                )
            )

        extracted_bls = {re.sub(r"\D", "", str(r.get("mesco_masterblno") or "")) for r in records}
        for ent in canonical_pages:
            key = re.sub(r"\D", "", ent["bl"])
            if key and key not in extracted_bls:
                doc_issues.append(
                    _issue(
                        "critical",
                        "missing_canonical_bl",
                        f"Page {ent['page']} B/L {ent['bl']} was not found in extraction output.",
                    )
                )

    bl_keys = [re.sub(r"\D", "", str(r.get("mesco_masterblno") or "")) for r in records]
    if len(bl_keys) != len(set(k for k in bl_keys if k)):
        doc_issues.append(
            _issue("warning", "duplicate_bl_numbers", "Duplicate master B/L numbers across records.")
        )

    for idx, rec in enumerate(records):
        page_text = ""
        if page_texts and idx < len(page_texts):
            page_text = page_texts[idx]
        elif rec.get("_page_text"):
            page_text = str(rec["_page_text"])
        record_summaries.append(_validate_single_record(rec, idx, page_text))

    all_issues = list(doc_issues)
    for summary in record_summaries:
        all_issues.extend(summary.issues)

    critical = sum(1 for i in all_issues if i.level == "critical")
    warning = sum(1 for i in all_issues if i.level == "warning")
    info = sum(1 for i in all_issues if i.level == "info")
    overall_score = max(0, min(100, 100 - critical * 25 - warning * 8 - info * 2))
    if record_summaries:
        overall_score = int(
            sum(s.score for s in record_summaries) / len(record_summaries) * 0.7
            + overall_score * 0.3
        )

    passed = critical == 0 and overall_score >= 60
    if record_summaries and not all(s.passed for s in record_summaries):
        passed = False

    if crm_masters and len(crm_masters) != len(records):
        doc_issues.append(
            _issue(
                "warning",
                "crm_record_mismatch",
                f"CRM master count ({len(crm_masters)}) differs from flat record count ({len(records)}).",
            )
        )

    return ExtractionValidationReport(
        passed=passed,
        score=overall_score,
        record_count=len(records),
        issues=all_issues,
        records=record_summaries,
        document_checks=document_checks,
    )
