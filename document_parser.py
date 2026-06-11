"""
Intelligent document parsing: Azure OpenAI interpretation + validation + fallbacks.

For multi-page PDFs with distinct ocean B/L numbers, page-anchored canonical
records are authoritative; Azure enriches fields without creating duplicates.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ai_extractor import extract_records_with_azure_openai
from bl_number_rules import finalize_multi_bl_records
from llm_context import llm_extraction_prefix, llm_meta
from pdf_isaly_draft_bl import (
    detect_isaly_draft_multi_bl,
    extract_isaly_draft_records,
)
from pdf_multi_bl import (
    detect_and_extract_multi_bl_records,
    detect_multi_bl_candidate,
    split_pdf_pages,
)
from record_reconciliation import dedupe_records_by_bl, merge_record_fields
from validator import validate_and_correct

logger = logging.getLogger(__name__)

MAX_PER_PAGE_AZURE_CALLS = 8

_CANONICAL_FORCE_KEYS = {
    "mesco_masterblno",
    "mesco_bookingnumber",
    "mesco_consigneenamecontactno",
    "mesco_acidnumber",
    "cr401_totalpackages",
    "cr401_totalgrossweight",
    "cr401_totalvolume",
    "mesco_origin",
    "mesco_destination",
    "mesco_vessel",
    "mesco_voytruckno",
    "container_number",
    "seal_number",
    "containers",
}


@dataclass
class IntelligentParseResult:
    records: List[Dict[str, Any]] = field(default_factory=list)
    document_layout: str = "unknown"
    quality: Dict[str, Any] = field(default_factory=dict)
    azure_warnings: List[str] = field(default_factory=list)


def _page_text_for_record(raw_text: str, record: Dict[str, Any]) -> str:
    if record.get("_page_text"):
        return str(record["_page_text"])
    bl = record.get("mesco_masterblno") or record.get("mesco_houseblno")
    source_page = record.get("source_page") or record.get("_page_number")
    pages = split_pdf_pages(raw_text)

    if source_page is not None:
        for page_no, text in pages:
            if page_no == int(source_page):
                return text

    if bl:
        bl_s = str(bl).strip()
        for _page_no, text in pages:
            if bl_s in text:
                return text

    return raw_text


def _enrichment_text_for_record(raw_text: str, record: Dict[str, Any]) -> str:
    """
    Text used for cargo / HS enrichment (all continuation sheets).

    Page-scoped validation still uses _page_text_for_record; when the same B/L
    continues on sheet 2+ we must pass the full OCR text into enrichment.
    """
    if not re.search(
        r"Continued\s+(?:on\s+Next|From\s+Previous)\s+Sheet",
        raw_text or "",
        re.I,
    ):
        return _page_text_for_record(raw_text, record)

    bl = record.get("mesco_masterblno") or record.get("mesco_houseblno")
    if not bl:
        return raw_text

    bl_s = str(bl).strip()
    pages = split_pdf_pages(raw_text)
    if sum(1 for _page_no, text in pages if bl_s in text) >= 2:
        return raw_text
    return _page_text_for_record(raw_text, record)


def _enrich_canonical_with_per_page_azure(
    canonical: List[Dict[str, Any]],
    raw_text: str,
) -> List[Dict[str, Any]]:
    """One Azure call per canonical page; merge into deterministic base (no extra records)."""
    enriched: List[Dict[str, Any]] = []
    calls = 0
    for fb in canonical:
        page_no = fb.get("_page_number")
        page_text = fb.get("_page_text") or _page_text_for_record(raw_text, fb)
        az_rec: Dict[str, Any] = {}
        if calls < MAX_PER_PAGE_AZURE_CALLS:
            calls += 1
            scoped = f"--- PAGE {page_no} ---\n{page_text}"
            try:
                payload = extract_records_with_azure_openai(scoped, page_scope=True)
                recs = payload.get("records") or []
                if recs:
                    az_rec = dict(recs[0])
            except Exception as exc:
                logger.warning("Per-page Azure enrichment failed page %s: %s", page_no, exc)

        merged = merge_record_fields(az_rec, dict(fb), prefer_secondary_keys=_CANONICAL_FORCE_KEYS)
        merged["mesco_masterblno"] = fb.get("mesco_masterblno")
        merged["mesco_bookingnumber"] = fb.get("mesco_masterblno")
        merged.setdefault("source_page", page_no)
        merged["_page_number"] = page_no
        merged["_page_text"] = page_text
        enriched.append(merged)
    return enriched


def _validate_records(
    records: List[Dict[str, Any]],
    raw_text: str,
    extraction_method: str,
    pdf_bytes: Optional[bytes] = None,
) -> List[Dict[str, Any]]:
    validated: List[Dict[str, Any]] = []
    for rec in records:
        ctx = _page_text_for_record(raw_text, rec)
        enrich_ctx = _enrichment_text_for_record(raw_text, rec)
        item = validate_and_correct(rec, ctx, enrichment_text=enrich_ctx)
        from bl_number_rules import correct_record_from_page

        item = correct_record_from_page(
            item,
            ctx,
            raw_text=raw_text,
            pdf_bytes=pdf_bytes,
            page_no=item.get("source_page") or item.get("_page_number"),
        )
        item = validate_and_correct(item, ctx, enrichment_text=enrich_ctx)
        item["extraction_method"] = item.get("extraction_method") or extraction_method
        item["_routing"] = {
            "route": "azure_intelligent",
            "reason": extraction_method,
            "page": rec.get("source_page") or rec.get("_page_number"),
        }
        validated.append(item)
    return validated


def parse_document_intelligently(
    raw_text: str,
    extracted_meta: Optional[Dict[str, Any]] = None,
    pdf_bytes: Optional[bytes] = None,
    file_bytes: Optional[bytes] = None,
    filename: Optional[str] = None,
) -> IntelligentParseResult:
    """
    Parse a document into validated B/L record(s).
    Multi-page scans: exactly one record per page-anchored ocean B/L (no duplicates).
    """
    if file_bytes is None and pdf_bytes is not None:
        file_bytes = pdf_bytes
    if not filename and isinstance(extracted_meta, dict):
        filename = extracted_meta.get("filename") or extracted_meta.get("source_filename")

    quality: Dict[str, Any] = {
        "parser": "intelligent",
        **llm_meta(),
        "llm_attempted": False,
        "per_page_llm_calls": 0,
        "fallback_used": False,
        # Legacy keys kept for downstream consumers
        "azure_attempted": False,
        "per_page_azure_calls": 0,
    }
    azure_warnings: List[str] = []
    document_layout = "unknown"
    isaly_expected = detect_isaly_draft_multi_bl(raw_text)
    multi_expected = detect_multi_bl_candidate(raw_text)
    quality["isaly_draft_expected"] = isaly_expected
    quality["multi_bl_expected"] = multi_expected

    from pdf_msds_dg import parse_msds_dg_record

    msds_record = parse_msds_dg_record(raw_text, filename=filename)
    if msds_record:
        quality["parser_mode"] = "msds_dg_direct"
        quality["msds_dg_fallback"] = True
        quality["fallback_used"] = True
        document_layout = "msds_dg"
        validated = _validate_records(
            [msds_record],
            raw_text,
            "msds_dg_direct",
            pdf_bytes=file_bytes,
        )
        quality["validated_record_count"] = len(validated)
        quality["document_type_detected"] = "msds_dg_pdf"
        if extracted_meta:
            quality["source_extraction_method"] = extracted_meta.get("method")
        return IntelligentParseResult(
            records=validated,
            document_layout=document_layout,
            quality=quality,
            azure_warnings=azure_warnings,
        )

    canonical = None
    if isaly_expected:
        canonical = extract_isaly_draft_records(raw_text)
    if not canonical and multi_expected:
        canonical = detect_and_extract_multi_bl_records(raw_text)

    if canonical and len(canonical) >= 2:
        quality["parser_mode"] = (
            "isaly_draft_page_anchored" if isaly_expected else "multi_bl_page_anchored"
        )
        quality["canonical_bl_count"] = len(canonical)
        quality["fallback_used"] = True
        document_layout = "multi_bl_pages"

        try:
            azure_records = _enrich_canonical_with_per_page_azure(canonical, raw_text)
            quality["llm_attempted"] = True
            quality["azure_attempted"] = True
            quality["per_page_llm_calls"] = len(canonical)
            quality["per_page_azure_calls"] = len(canonical)
        except Exception as exc:
            logger.warning("Per-page Azure enrichment skipped: %s", exc)
            azure_warnings.append(str(exc))
            azure_records = [dict(c) for c in canonical]

        method = "isaly_draft_page_anchored" if isaly_expected else "multi_bl_page_anchored"
    else:
        azure_records: List[Dict[str, Any]] = []
        try:
            payload = extract_records_with_azure_openai(
                raw_text,
                file_bytes=file_bytes,
                filename=filename,
            )
            quality["llm_attempted"] = True
            quality["azure_attempted"] = True
            document_layout = payload.get("document_layout") or "unknown"
            azure_records = [dict(r) for r in (payload.get("records") or [])]
            azure_warnings = list(payload.get("warnings") or [])
            quality["azure_document_layout"] = document_layout
            quality["azure_record_count"] = len(azure_records)
        except Exception as exc:
            logger.warning("Whole-document Azure extraction failed: %s", exc)
            azure_warnings.append(f"azure_whole_document_error: {exc}")
            quality["azure_error"] = str(exc)

        from pdf_debit_note import is_freight_debit_note, parse_freight_debit_note
        from pdf_sea_waybill import is_consolidation_sea_waybill, parse_consolidation_sea_waybill

        debit_fallback: List[Dict[str, Any]] = []
        if is_freight_debit_note(raw_text):
            dn = parse_freight_debit_note(raw_text)
            if dn:
                debit_fallback = [dn]
                quality["debit_note_fallback"] = True

        sea_waybill_fallback: List[Dict[str, Any]] = []
        if is_consolidation_sea_waybill(raw_text):
            sw = parse_consolidation_sea_waybill(raw_text)
            if sw:
                sea_waybill_fallback = [sw]
                quality["sea_waybill_fallback"] = True

        fallback_records = (
            debit_fallback
            or sea_waybill_fallback
            or extract_isaly_draft_records(raw_text)
            or detect_and_extract_multi_bl_records(raw_text)
            or []
        )
        force_special_fallback = bool(sea_waybill_fallback or debit_fallback)
        if fallback_records and (
            force_special_fallback
            or not azure_records
            or len(fallback_records) > len(azure_records)
        ):
            from record_reconciliation import reconcile_record_lists

            azure_records = reconcile_record_lists(azure_records, fallback_records)
            quality["fallback_used"] = True

        azure_records = dedupe_records_by_bl(azure_records)
        azure_records = finalize_multi_bl_records(azure_records, raw_text)
        prefix = llm_extraction_prefix()
        method = f"{prefix}_intelligent"
        if quality.get("fallback_used"):
            method = f"{prefix}_intelligent_with_fallback"

    if not azure_records:
        quality["parser"] = "failed"
        return IntelligentParseResult(
            records=[],
            document_layout=document_layout,
            quality=quality,
            azure_warnings=azure_warnings,
        )

    azure_records = finalize_multi_bl_records(azure_records, raw_text, pdf_bytes=file_bytes)
    validated = _validate_records(azure_records, raw_text, method, pdf_bytes=file_bytes)

    quality["validated_record_count"] = len(validated)
    quality["document_type_detected"] = (
        "multi_bl_pdf" if len(validated) >= 2 else "single_bl_pdf"
    )
    if extracted_meta:
        quality["source_extraction_method"] = extracted_meta.get("method")

    return IntelligentParseResult(
        records=validated,
        document_layout=document_layout,
        quality=quality,
        azure_warnings=azure_warnings,
    )
