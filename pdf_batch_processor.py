"""
Batch PDF extraction for testing (no Dataverse by default).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from crm_output_formatter import records_to_house_json, records_to_master_json
from document_parser import parse_document_intelligently
from extraction_report import ExtractionValidationReport, validate_pdf_extraction
from pdf_attached_list import build_house_records_from_attached_list, extract_attached_list_house_refs
from pdf_lcl_export_manifest import is_export_lcl_manifest, parse_export_lcl_manifest
from pdf_tur_cargo_manifest import is_tur_cargo_manifest, parse_tur_cargo_manifest
from pdf_consolidated_lcl import is_consolidated_lcl_multi_hbl, parse_consolidated_lcl_multi_hbl
from pdf_standard_master_bl import is_standard_master_bl, parse_standard_master_bl
from spreadsheet_extractor import extract_document_text_professionally
from validator import validate_and_correct


@dataclass
class PdfBatchItemResult:
    filename: str
    success: bool
    passed: bool
    score: int
    record_count: int
    processing_ms: int
    error: Optional[str] = None
    extraction_quality: Dict[str, Any] = field(default_factory=dict)
    validation: Optional[Dict[str, Any]] = None
    records_summary: List[Dict[str, Any]] = field(default_factory=list)
    crm_masters: Optional[List[Dict[str, Any]]] = None
    raw_text_preview: Optional[str] = None

    def to_dict(self, *, include_crm: bool = True, include_raw: bool = False) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "filename": self.filename,
            "success": self.success,
            "passed": self.passed,
            "score": self.score,
            "record_count": self.record_count,
            "processing_ms": self.processing_ms,
            "error": self.error,
            "extraction_quality": self.extraction_quality,
            "validation": self.validation,
            "records_summary": self.records_summary,
        }
        if include_crm and self.crm_masters is not None:
            out["crm_masters"] = self.crm_masters
        if include_raw and self.raw_text_preview:
            out["raw_text_preview"] = self.raw_text_preview
        return out


def _record_summary(rec: Dict[str, Any], crm: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    summary = {
        "mesco_masterblno": rec.get("mesco_masterblno"),
        "mesco_consigneenamecontactno": rec.get("mesco_consigneenamecontactno"),
        "mesco_origin": rec.get("mesco_origin"),
        "mesco_destination": rec.get("mesco_destination"),
        "cr401_totalpackages": rec.get("cr401_totalpackages"),
        "cr401_totalgrossweight": rec.get("cr401_totalgrossweight"),
        "cr401_totalvolume": rec.get("cr401_totalvolume"),
        "mesco_acidnumber": rec.get("mesco_acidnumber"),
        "container_number": rec.get("container_number"),
        "mesco_vessel": rec.get("mesco_vessel"),
        "mesco_voytruckno": rec.get("mesco_voytruckno"),
        "source_page": rec.get("source_page") or rec.get("_page_number"),
    }
    if crm:
        summary["crm_masterblno"] = crm.get("mesco_masterblno")
    return summary


def process_pdf_bytes(
    file_bytes: bytes,
    filename: str,
    *,
    include_raw_text_preview: bool = False,
) -> PdfBatchItemResult:
    """
    Run full PDF extraction pipeline (same as /extract/file for PDFs) and validate.
    Does not upload to Dataverse.
    """
    started = time.perf_counter()
    name = filename or "upload.pdf"
    if not name.lower().endswith(".pdf"):
        return PdfBatchItemResult(
            filename=name,
            success=False,
            passed=False,
            score=0,
            record_count=0,
            processing_ms=0,
            error="Only PDF files are supported on this endpoint.",
        )

    try:
        extracted = extract_document_text_professionally(file_bytes, name)
        raw_text = extracted.get("text", "")
        extraction_quality: Dict[str, Any] = dict(extracted.get("quality") or {})

        if not raw_text.strip():
            elapsed = int((time.perf_counter() - started) * 1000)
            report = validate_pdf_extraction([], raw_text)
            return PdfBatchItemResult(
                filename=name,
                success=False,
                passed=False,
                score=0,
                record_count=0,
                processing_ms=elapsed,
                error="No text extracted from PDF.",
                extraction_quality=extraction_quality,
                validation=report.to_dict(),
            )

        validated_records: List[Dict[str, Any]] = []
        crm_masters: List[Dict[str, Any]] = []
        document_layout = "unknown"

        if is_tur_cargo_manifest(raw_text):
            manifest = parse_tur_cargo_manifest(raw_text)
            if manifest:
                extraction_quality["document_type_detected"] = "tur_cargo_manifest_pdf"
                house_records = [
                    validate_and_correct(rec, raw_text) for rec in manifest["house_records"]
                ]
                master_record = validate_and_correct(manifest["master_record"], raw_text)
                validated_records = house_records
                crm_masters = [records_to_master_json(house_records, master_record=master_record)]
                document_layout = "manifest"
            else:
                extraction_quality["document_type_detected"] = "tur_cargo_manifest_failed"

        if not validated_records and is_export_lcl_manifest(raw_text):
            manifest = parse_export_lcl_manifest(raw_text)
            if manifest:
                extraction_quality["document_type_detected"] = "export_lcl_manifest_pdf"
                house_records = [
                    validate_and_correct(rec, raw_text) for rec in manifest["house_records"]
                ]
                master_record = validate_and_correct(manifest["master_record"], raw_text)
                validated_records = house_records
                crm_masters = [records_to_master_json(house_records, master_record=master_record)]
                document_layout = "manifest"
            else:
                extraction_quality["document_type_detected"] = "export_lcl_manifest_failed"

        if not validated_records and is_consolidated_lcl_multi_hbl(raw_text):
            consolidated = parse_consolidated_lcl_multi_hbl(raw_text)
            if consolidated and len(consolidated["house_records"]) >= 2:
                extraction_quality["document_type_detected"] = "consolidated_lcl_multi_hbl_pdf"
                house_records = [
                    validate_and_correct(rec, raw_text)
                    for rec in consolidated["house_records"]
                ]
                master_record = validate_and_correct(
                    consolidated["master_record"],
                    raw_text,
                )
                validated_records = house_records
                crm_masters = [
                    records_to_master_json(house_records, master_record=master_record)
                ]
                document_layout = "master_with_houses"
                extraction_quality["record_routing"] = {
                    "policy": "pdf_consolidated_lcl_multi_hbl",
                    "mode": "one_master_with_house_records",
                    "document_layout": document_layout,
                }

        if not validated_records and is_standard_master_bl(raw_text):
            master_record = parse_standard_master_bl(raw_text)
            if master_record:
                validated = validate_and_correct(
                    master_record,
                    raw_text,
                    enrichment_text=raw_text,
                )
                validated_records = [validated]
                crm_masters = [records_to_master_json([validated])]
                document_layout = "single_bl"
                extraction_quality["document_type_detected"] = "standard_master_bl_pdf"
                extraction_quality["record_routing"] = {
                    "policy": "pdf_standard_master_bl",
                    "mode": "single_master_bl",
                    "document_layout": document_layout,
                }

        if not validated_records:
            parse_result = parse_document_intelligently(
                raw_text, extracted, file_bytes=file_bytes, filename=name
            )
            extraction_quality.update(parse_result.quality)
            document_layout = parse_result.document_layout
            if parse_result.azure_warnings:
                extraction_quality["azure_warnings"] = parse_result.azure_warnings

            if not parse_result.records:
                elapsed = int((time.perf_counter() - started) * 1000)
                report = validate_pdf_extraction([], raw_text)
                return PdfBatchItemResult(
                    filename=name,
                    success=False,
                    passed=False,
                    score=0,
                    record_count=0,
                    processing_ms=elapsed,
                    error="No B/L records extracted.",
                    extraction_quality=extraction_quality,
                    validation=report.to_dict(),
                    raw_text_preview=raw_text[:3000] if include_raw_text_preview else None,
                )

            extraction_quality["record_routing"] = {
                "policy": "azure_intelligent_with_fallback",
                "mode": "one_master_per_bl_record",
                "document_layout": document_layout,
            }

            if len(parse_result.records) >= 2:
                extraction_quality["multi_bl_count"] = len(parse_result.records)
                validated_records = parse_result.records
                crm_masters = [records_to_master_json([v]) for v in validated_records]
            else:
                validated = parse_result.records[0]
                attached_refs = extract_attached_list_house_refs(raw_text)
                if attached_refs:
                    extraction_quality["attached_list_house_count"] = len(attached_refs)
                    house_records = build_house_records_from_attached_list(validated, attached_refs)
                    validated_records = house_records
                    crm_masters = [
                        records_to_master_json(house_records, master_record=validated)
                    ]
                else:
                    validated_records = [validated]
                    crm_masters = [records_to_master_json([validated])]

        report: ExtractionValidationReport = validate_pdf_extraction(
            validated_records,
            raw_text,
            crm_masters=crm_masters,
        )
        summaries = [
            _record_summary(rec, crm_masters[i] if i < len(crm_masters) else None)
            for i, rec in enumerate(validated_records)
        ]

        elapsed = int((time.perf_counter() - started) * 1000)
        return PdfBatchItemResult(
            filename=name,
            success=True,
            passed=report.passed,
            score=report.score,
            record_count=len(validated_records),
            processing_ms=elapsed,
            extraction_quality=extraction_quality,
            validation=report.to_dict(),
            records_summary=summaries,
            crm_masters=crm_masters,
            raw_text_preview=raw_text[:3000] if include_raw_text_preview else None,
        )

    except Exception as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        return PdfBatchItemResult(
            filename=filename or "upload.pdf",
            success=False,
            passed=False,
            score=0,
            record_count=0,
            processing_ms=elapsed,
            error=str(exc),
        )
