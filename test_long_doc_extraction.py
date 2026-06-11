"""Tests for long-document chunking + Gemini native-PDF plumbing in ai_extractor."""

from __future__ import annotations

import ai_extractor
from ai_extractor import (
    _chunk_long_text,
    _merge_chunk_payloads,
    extract_records_with_llm,
)


def test_chunk_short_text_is_single_chunk():
    text = "small document text"
    assert _chunk_long_text(text, 1000) == [text]


def test_chunk_splits_on_page_markers():
    pages = "".join(f"--- PAGE {i} ---\n" + ("x" * 80) + "\n" for i in range(1, 7))
    chunks = _chunk_long_text(pages, 200)
    assert len(chunks) >= 2
    # Every page marker is preserved across the chunk set.
    joined = "".join(chunks)
    for i in range(1, 7):
        assert f"--- PAGE {i} ---" in joined
    # No chunk exceeds the budget.
    assert all(len(c) <= 200 for c in chunks)


def test_chunk_falls_back_to_line_split_for_spreadsheets():
    rows = "".join(f"row {i},value{i},{i*10}\n" for i in range(500))
    chunks = _chunk_long_text(rows, 1000)
    assert len(chunks) > 1
    assert all(len(c) <= 1000 for c in chunks)
    # All rows survive chunking.
    assert "".join(chunks) == rows


def test_oversized_single_unit_is_hard_sliced():
    giant = "y" * 5000  # one line, no page markers
    chunks = _chunk_long_text(giant, 1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert "".join(chunks) == giant


def test_merge_dedupes_records_by_bl_and_picks_multi_layout():
    payloads = [
        {
            "document_layout": "single_bl",
            "records": [{"mesco_masterblno": "AAA111"}],
            "confidence": {"a": 1},
            "warnings": ["w1"],
        },
        {
            "document_layout": "single_bl",
            "records": [
                {"mesco_masterblno": "AAA111"},  # duplicate
                {"mesco_masterblno": "BBB222"},
            ],
            "confidence": {"b": 2},
            "warnings": ["w2"],
        },
    ]
    merged = _merge_chunk_payloads(payloads)
    bls = {r["mesco_masterblno"] for r in merged["records"]}
    assert bls == {"AAA111", "BBB222"}
    assert merged["document_layout"] == "multi_bl_pages"
    assert merged["confidence"] == {"a": 1, "b": 2}
    assert set(merged["warnings"]) == {"w1", "w2"}


def test_long_text_is_chunked_and_merged(monkeypatch):
    """A long spreadsheet-style doc should produce one LLM call per chunk, merged."""
    monkeypatch.setattr(ai_extractor.settings, "gemini_max_input_chars", 1000)
    monkeypatch.setattr(ai_extractor.settings, "max_input_chars", 1000)
    monkeypatch.setattr(ai_extractor, "uses_gemini", lambda: False)

    rows = "".join(f"HBL-{i:04d},consignee {i},{i*5} KGS\n" for i in range(400))

    calls = {"n": 0}

    def fake_call(system, user, schema):
        calls["n"] += 1
        # Return a record keyed off the first HBL number found in the chunk.
        import re

        m = re.search(r"HBL-(\d+)", user)
        bl = f"HBL-{m.group(1)}" if m else f"BL{calls['n']}"
        return {
            "document_layout": "single_bl",
            "records": [{"mesco_masterblno": bl}],
            "confidence": {},
            "warnings": [],
        }

    monkeypatch.setattr(ai_extractor, "_call_llm_json", fake_call)

    result = extract_records_with_llm(rows)

    assert calls["n"] > 1  # actually chunked
    assert len(result["records"]) >= 2
    assert any("long_document_chunked" in w for w in result["warnings"])


def test_chunk_splits_on_sheet_markers():
    sheets = "".join(f"--- SHEET: Tab{i} ---\n" + ("y" * 120) + "\n" for i in range(1, 8))
    chunks = _chunk_long_text(sheets, 300)
    assert len(chunks) >= 2
    joined = "".join(chunks)
    for i in range(1, 8):
        assert f"--- SHEET: Tab{i} ---" in joined


def test_native_mime_detects_xlsx():
    from ai_extractor import _native_mime_for_file

    xlsx = b"PK\x03\x04 fake xlsx"
    assert _native_mime_for_file(xlsx, "manifest.xlsx") == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


def test_native_pdf_passes_bytes_to_gemini(monkeypatch):
    """When provider=gemini and a real PDF is supplied, file_bytes reach the LLM call."""
    monkeypatch.setattr(ai_extractor, "uses_gemini", lambda: True)
    monkeypatch.setattr(ai_extractor.settings, "gemini_native_pdf", True)

    captured = {}

    def fake_call(system, user, schema, *, file_bytes=None, filename=None):
        captured["file_bytes"] = file_bytes
        captured["filename"] = filename
        return {"document_layout": "single_bl", "records": [{}], "confidence": {}, "warnings": []}

    monkeypatch.setattr(ai_extractor, "_call_llm_json", fake_call)

    pdf = b"%PDF-1.7 fake pdf body"
    extract_records_with_llm("ocr text hint", file_bytes=pdf, filename="bl.pdf")
    assert captured["file_bytes"] == pdf


def test_native_xlsx_passes_bytes_to_gemini(monkeypatch):
    """When provider=gemini and an xlsx is supplied, file_bytes reach the LLM call."""
    monkeypatch.setattr(ai_extractor, "uses_gemini", lambda: True)
    monkeypatch.setattr(ai_extractor.settings, "gemini_native_spreadsheet", True)

    captured = {}

    def fake_call(system, user, schema, *, file_bytes=None, filename=None):
        captured["file_bytes"] = file_bytes
        return {"document_layout": "manifest", "records": [{}], "confidence": {}, "warnings": []}

    monkeypatch.setattr(ai_extractor, "_call_llm_json", fake_call)

    xlsx = b"PK\x03\x04 zip-based workbook"
    extract_records_with_llm("sheet text", file_bytes=xlsx, filename="manifest.xlsx")
    assert captured["file_bytes"] == xlsx


def test_unknown_bytes_not_sent_natively(monkeypatch):
    """Random bytes without a known MIME type must not be sent as a native file."""
    monkeypatch.setattr(ai_extractor, "uses_gemini", lambda: True)
    monkeypatch.setattr(ai_extractor.settings, "gemini_native_pdf", True)
    monkeypatch.setattr(ai_extractor.settings, "gemini_native_spreadsheet", True)

    captured = {}

    def fake_call(system, user, schema, *, file_bytes=None, filename=None):
        captured["file_bytes"] = file_bytes
        return {"document_layout": "single_bl", "records": [{}], "confidence": {}, "warnings": []}

    monkeypatch.setattr(ai_extractor, "_call_llm_json", fake_call)

    junk = b"not-a-pdf-or-spreadsheet"
    extract_records_with_llm("sheet text", file_bytes=junk)
    assert captured["file_bytes"] is None
