"""
Deterministic enrichment of B/L records from PDF OCR/native text.

Fills CRM fields that Azure often omits: HS codes, marks, contacts, routing,
service type, freight clauses, carrier, delivery place, and documents the
difference between page-1 cargo references vs attachment-list house B/Ls.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from pdf_attached_list import extract_attached_list_house_refs


def _page1_text(raw_text: str) -> str:
    pages = re.split(r"---\s*PAGE\s*\d+\s*---", raw_text or "", flags=re.I)
    return pages[1] if len(pages) > 1 else (raw_text or "")


def _tagged_section(text: str, tag: str) -> str:
    pattern = rf"\[{re.escape(tag)}\]\s*(.*?)(?=\n\[[^\]\n]{{3,80}}\]|\Z)"
    m = re.search(pattern, text or "", re.I | re.S)
    return m.group(1) if m else ""


def _section_looks_readable(section: str) -> bool:
    if not section or len(section.strip()) < 80:
        return False
    upper = section.upper()
    hits = sum(
        1
        for marker in (
            "BILL OF LADING",
            "SHIPPER",
            "CONSIGNEE",
            "NOTIFY",
            "PORT OF LOADING",
            "DESCRIPTION OF GOODS",
            "SAID TO CONTAIN",
            "HS CODE",
        )
        if marker in upper
    )
    return hits >= 2


def _visual_page_text(raw_text: str, page_index: int = 1) -> str:
    """Prefer [VISUAL WORD ORDER] section (reading order) over block-order OCR."""
    pages = re.split(r"---\s*PAGE\s*\d+\s*---", raw_text or "", flags=re.I)
    if page_index < 1 or page_index >= len(pages):
        return pages[1] if len(pages) > 1 else (raw_text or "")
    body = pages[page_index]
    visual = _tagged_section(body, "VISUAL WORD ORDER")
    if _section_looks_readable(visual):
        return visual
    for tag in ("OCR FULL PAGE BEST", "OCR BODY PSM4", "OCR HEADER PSM6", "BLOCK ORDER"):
        candidate = _tagged_section(body, tag)
        if _section_looks_readable(candidate):
            return candidate
    return visual or body


def _is_mtd_document(text: str) -> bool:
    """Multi-Modal Transport Document (MTD) layout — not a standard ocean B/L."""
    sample = (text or "")[:12000]
    return bool(
        re.search(
            r"MULTI-?MODAL\s+TRANSPORT\s+DOCUMENT|\bMTD\s+NO\b",
            sample,
            re.I,
        )
    )


def _cargo_pages_text(raw_text: str) -> str:
    """All pages that contain the goods / HS description table."""
    pages = re.split(r"---\s*PAGE\s*\d+\s*---", raw_text or "", flags=re.I)
    parts: List[str] = []
    for body in pages[1:]:
        if re.search(
            r"DESCRIPTION OF PACKAGES|SAID TO CONTAIN|HS\s*CODE|"
            r"Continued on Next Sheet|Continued From Previous|"
            r"MULTI-?MODAL\s+TRANSPORT|MTD\s+NO|Particulars above furnished|"
            r"Kind of Packages and Goods",
            body,
            re.I,
        ):
            parts.append(body)
    return "\n".join(parts) if parts else _page1_text(raw_text)


_CARGO_LINE_STOP_RE = re.compile(
    r"^(CONSOLIDATED CARGO|CARGO IN TRANSIT|FOREIGN EXPORTER|EGYPTIAN FREIGHT|FOREIGN FREIGHT|"
    r"REGISTRATION TYPE|PARTICULARS OF GOODS|Continued on Next Sheet|Continued From Previous|"
    r"ABOVE PARTICULARS|SAY ONE HUNDRED|SAY \w+ HUNDRED|Shipped on Board|Weight in Kgs Total|"
    r"SIGNED FOR THE CARRIER|FREIGHT AND CHARGES|SHIPPED ON BOARD|ACID\s*:|TELEX\s+RELEASE|"
    r"REVENUE TONS|SERVICE MODE|RECEIVED BY THE CARRIER|CHARGE RATE|TOTAL PREPAID|"
    r"PLACE AND DATE OF ISSUE|LADEN ON BOARD DATE)",
    re.I,
)
_CARGO_LINE_SKIP_RE = re.compile(
    r"^(\[[^\]]+\]|MARKS AND|CONTAINER AND|NO AND KIND|DESCRIPTION OF PACKAGES|DESCRIPTIONS OF GOODS|"
    r"NUMBER OF PACKAGES|GROSS WEIGHT|TARE|MEASUREMENT|SHIPPER'?S LOAD|SAID TO CONTAIN|"
    r"VOYAGE|BILL OF LADING|PRE CARRIAGE|VESSEL|PORT OF LOADING|"
    r"PORT OF DISCHARGE|FINAL PLACE|COPY NON|BILL OF LADING NUMBER|"
    r"PLACE OF RECEIPT|FREIGHT TO BE|NUMBER OF ORIGINAL|"
    r"FREIGHT\s+PREPAID|FREIGHT\s+COLLECT|TELEX\s+RELEASE|SEAL\s+NO|"
    r"KGS\s*$|CBM\s*$|KGS\s+KGS\s+CBM|"
    r"\*{3,}|SHENZHEN THREE|"
    r"MARKS\s+\d+\s*:|^\d{2}\s+HC-|"
    r"(?:TLLU|MSCU|CMAU|SEGU|OOLU|TCNU|TGHU|MEDU|GESU|CSLU|TGBU|HLBU|MSKU|MAEU|GAOU)[A-Z0-9]{4,}|"
    r"^[A-Z]{4}\d{6,7}[-]?\s*$)",
    re.I,
)
_GOODS_LINE_START_RE = re.compile(
    r"^\d+\s+PALLETS?\b|"
    r"^\d+\s+(?:CARTONS?|PACKAGES?|DRUMS?|BAGS?|ROLLS?|CASES?|BALES?)\b|"
    r"^(?:ROASTED|SALTED|DRIED|FROZEN|FRESH|BLANCHED|RAW|WHOLE|GROUND)\b",
    re.I,
)
_GOODS_NARRATIVE_STOP_RE = re.compile(
    r"^(FOREIGN EXPORTER|REGISTRATION TYPE|PARTICULARS OF GOODS|"
    r"Shipped on Board|Weight in Kgs Total|SIGNED FOR THE CARRIER|"
    r"FREIGHT AND CHARGES|Continued on Next Sheet|EGYPTIAN FREIGHT|"
    r"All above particulars)",
    re.I,
)
# Container header row e.g. "TLLU4178846 1 x 40HC 179 PACKAGE(S) 14801.730 3900 50.657"
_CARGO_CONTAINER_ROW_RE = re.compile(
    r"^[A-Z]{4}\d{6,7}\s+\d+\s*x\s*\d{2}[A-Z]{0,3}\s+\d+\s+PACKAGE",
    re.I,
)
_CARGO_NUMERIC_NOISE_RE = re.compile(
    r"^[\d.,\s]+(?:KGS?|CBM|PACKAGE\(?S?\)?)?[\d.,\s]*$",
    re.I,
)
# Maximum length we emit so the master cargo row stays under the
# Dataverse 1500-char limit (formatter prepends "HS: ...\n" too).
_CARGO_DESCRIPTION_MAX = 1200


def _first_group(pattern: str, text: str, flags: int = re.I | re.S) -> Optional[str]:
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else None


_TAX_ID_CONTEXT_RE = re.compile(
    r"(?:ACID|TAX\s*ID|TAX\s*NO|VAT\s*NO|FORWARDER\s*ID|FORWARDER\s*REGISTRATION|"
    r"IMPORTER\s*ID|EXPORTER\s*ID|REGISTRATION\s*ID|SHIPPER\s*REGISTRATION|"
    r"FOREIGN\s*FREIGHT|EGYPTIAN\s*FREIGHT)",
    re.I,
)


def _looks_like_phone_or_contact(code: str) -> bool:
    """Egyptian mobiles / contact numbers must never be treated as HS codes."""
    digits = re.sub(r"\D", "", code or "")
    if not digits:
        return True
    if len(digits) >= 19:
        return True
    if re.match(r"^20\d{8,10}$", digits):
        return True
    if re.match(r"^01\d{8,9}$", digits):
        return True
    if re.match(r"^201\d{7,9}$", digits):
        return True
    return False


def _collect_tax_ids(cargo_text: str) -> set:
    """Numeric identifiers that look like Tax/ACID/Registration/contact IDs, not HS codes."""
    ids: set = set()
    lines = cargo_text.splitlines()
    for idx, line in enumerate(lines):
        upper = line.upper()
        context_lines = [upper]
        if idx > 0:
            context_lines.append(lines[idx - 1].upper())
        if idx > 1:
            context_lines.append(lines[idx - 2].upper())
        ctx_blob = " | ".join(context_lines)
        contact_ctx = bool(
            re.search(
                r"TEL\b|MOB\b|FAX\b|VAT\s*NR|@\w|\.COM|PHONE|CONTACT\s*NO",
                ctx_blob,
                re.I,
            )
        )
        if _TAX_ID_CONTEXT_RE.search(ctx_blob) or contact_ctx:
            for m in re.finditer(r"\b(\d{6,19})\b", line):
                ids.add(m.group(1))
        if re.match(r"^\d{10,11}(?:-\d+)?\s*$", line.strip()):
            for m in re.finditer(r"\d{10,11}", line):
                ids.add(m.group(0))
    return ids


def extract_hs_codes_from_goods(text: str) -> Optional[str]:
    """HS codes from all cargo pages (page 1 + continuation sheets)."""
    codes: List[str] = []
    cargo_text = _cargo_pages_text(text)
    tax_ids = _collect_tax_ids(cargo_text)

    def add_code(code: str, *, explicit_hs: bool) -> None:
        code = re.sub(r"\D", "", (code or "").strip())
        if not code or code in codes:
            return
        if len(code) >= 19 or _looks_like_phone_or_contact(code):
            return
        if not explicit_hs and code in tax_ids:
            return
        if not explicit_hs and len(code) >= 8:
            return
        codes.append(code)

    for m in re.finditer(
        r"H\.?S\.?\s*CODE\s*:?\s*([0-9]{6,12})",
        cargo_text,
        re.I,
    ):
        add_code(m.group(1), explicit_hs=True)

    for line in cargo_text.splitlines():
        line = line.strip()
        if not line:
            continue
        upper = line.upper()
        if any(
            k in upper
            for k in (
                "ACID",
                "TAX ID",
                "TAX NO",
                "VAT:",
                "TEL:",
                "FAX:",
                "BILL OF LADING",
                "BOOKING NO",
                "EQUIPMENT SERVICES",
                "EXPORT REFERENCES",
                "FORWARDER",
                "IMPORTER ID",
                "EXPORTER ID",
                "REGISTRATION",
            )
        ):
            continue
        explicit_hs = bool(re.search(r"HS\s*CODE|H\.?S\.?\s*CODE", line, re.I))
        if explicit_hs:
            for part in re.split(r"[/,\s]+", line):
                part_digits = re.sub(r"\D", "", part)
                if 6 <= len(part_digits) <= 12:
                    add_code(part_digits, explicit_hs=True)
    return ", ".join(codes) if codes else None


def _is_goods_noise(line: str) -> bool:
    """Container header rows, package totals, or numeric-only lines — not goods."""
    if _CARGO_CONTAINER_ROW_RE.match(line):
        return True
    if re.match(r"^SEAL\s+", line, re.I):
        return True
    if re.match(r"^\d{2}\s+HC-", line, re.I):
        return True
    if re.match(r"^[A-Z]{4}\d{6,7}[-]?\s*$", line, re.I):
        return True
    if re.match(r"^\d+\s*x\s*\d{2}[A-Z]{0,3}\b", line, re.I):
        return True
    if re.match(r"^\d+\s+PACKAGE", line, re.I):
        return True
    if _CARGO_NUMERIC_NOISE_RE.match(line):
        return True
    return False


_MTD_CARGO_STOP_RE = re.compile(
    r"^(Particulars above furnished|Shipped on Board|Weight in Kgs Total|"
    r"SIGNED FOR THE CARRIER|FREIGHT\s+AND\s+CHARGES|Place and Date of Issue|"
    r"All above particulars|LADEN ON BOARD|CARRIAGE FORWARD)",
    re.I,
)
_MTD_CARGO_SKIP_RE = re.compile(
    r"^(Container No:?\s*$|A/Seal|C/Seal|M/Seal|Marks and Numbers|"
    r"Packages and Packaging|Gross Weight|Measurement|Type of Movement|"
    r"Kind of Packages|QUANTITY\s+GROSS|MODE OF TRANSPORT|"
    r"MULTI-?MODAL\s+TRANSPORT|MTD\s+No|B/L\s+No|Shipper|Consignee|Notify|"
    r"Port of Loading|Port of Discharge|Place of Delivery|"
    r"Pre-carriage|On-carriage|Vessel|Voyage|"
    r"(?:TLLU|TRKU|MSCU|CSLU|CMAU|SEGU|OOLU|TCNU|HLBU|MSKU|MAEU)[A-Z0-9]{4,7}\s*$|"
    r"^\d+\s+PALLETS?\s*$|^\d+[,.]\d+\s*KGS|^\d+[,.]?\d*\s*CBM)",
    re.I,
)
_MTD_INLINE_GOODS_RE = re.compile(
    r"(\d+\s+PALLETS?\s+STC.+?)(?=\s*\d+[,.]\d+\s*KGS|\s*\d+[,.]?\d*\s*CBM|\s*Particulars above)",
    re.I | re.S,
)


def _cap_cargo_description(description: str) -> str:
    if len(description) > _CARGO_DESCRIPTION_MAX:
        return description[: _CARGO_DESCRIPTION_MAX].rsplit("\n", 1)[0]
    return description


def extract_mtd_cargo_description_from_pdf(text: str) -> Optional[str]:
    """
    Goods narrative from Multi-Modal Transport Documents (MTD).

    MTD layouts place the full cargo text between the container/seal block and
    the "Particulars above furnished" / "Shipped on Board" footer — not after
    the standard "SAID TO CONTAIN CARGO" / N/M markers used on ocean B/Ls.
    """
    lines_out: List[str] = []
    seen: set[str] = set()

    def add_line(raw: str) -> None:
        s = re.sub(r"\s+", " ", (raw or "")).strip()
        if not s or len(s) < 4:
            return
        if _MTD_CARGO_SKIP_RE.match(s):
            return
        if _CARGO_NUMERIC_NOISE_RE.match(s):
            return
        if re.search(r"\d+[,.]\d+\s*KGS|\d+[,.]?\d*\s*CBM", s, re.I):
            s = re.sub(r"\s+\d+[,.]\d+\s*KGS.*$", "", s, flags=re.I).strip()
            if not s or len(s) < 4:
                return
        key = re.sub(r"[^A-Z0-9]+", "", s.upper())[:80]
        if not key or key in seen:
            return
        seen.add(key)
        lines_out.append(s)

    pages = re.split(r"---\s*PAGE\s*\d+\s*---", text or "", flags=re.I)
    for idx in range(1, len(pages)):
        bodies = (_visual_page_text(text, idx), pages[idx])
        for body in bodies:
            inline = _MTD_INLINE_GOODS_RE.search(body)
            if inline:
                chunk = re.sub(r"\s+", " ", inline.group(1)).strip()
                for part in re.split(
                    r"(?<=[.)])\s+(?=(?:\(\s*(?:ONE|TWO|THREE|FOUR|FIVE|SIX|SEVEN|EIGHT|NINE|TEN|\d+)\s+PALLETS?\s+SAID|[A-Z0-9]))",
                    chunk,
                ):
                    add_line(part)

            start = 0
            m_stc = re.search(r"\d+\s+PALLETS?\s+STC", body, re.I)
            m_paren = re.search(
                r"\(\s*(?:ONE|TWO|THREE|FOUR|FIVE|SIX|SEVEN|EIGHT|NINE|TEN|\d+)\s+PALLETS?\s+SAID TO CONTAIN",
                body,
                re.I,
            )
            if m_stc:
                start = m_stc.start()
            elif m_paren:
                start = m_paren.start()
            elif inline:
                continue
            else:
                continue

            for ln in body[start:].splitlines():
                s = ln.strip()
                if not s:
                    continue
                if _MTD_CARGO_STOP_RE.match(s):
                    break
                if _MTD_CARGO_SKIP_RE.match(s):
                    continue
                if _is_goods_noise(s):
                    continue
                add_line(s)

            if lines_out:
                break
        if lines_out:
            break

    if not lines_out:
        return None
    return _cap_cargo_description("\n".join(lines_out))


def _collect_goods_lines(
    body: str,
    start: int,
    *,
    stop_re: re.Pattern[str],
) -> List[str]:
    """Walk cargo page lines from *start*, dedupe and return goods narrative lines."""
    lines_out: List[str] = []
    seen: set[str] = set()

    def add_line(raw: str) -> None:
        s = re.sub(r"\s+", " ", (raw or "")).strip()
        if not s or len(s) < 3:
            return
        key = re.sub(r"[^A-Z0-9]+", "", s.upper())[:80]
        if not key or key in seen:
            return
        seen.add(key)
        lines_out.append(s)

    for ln in body[start:].splitlines():
        s = ln.strip()
        if not s:
            continue
        if stop_re.match(s):
            break
        if _CARGO_LINE_SKIP_RE.match(s):
            continue
        if _is_goods_noise(s):
            continue
        add_line(s)
    return lines_out


def _extract_goods_narrative_block(text: str) -> Optional[str]:
    """
    Extract goods narrative anchored at the first package/product line.

    Handles standard B/L layouts where FREIGHT PREPAID / container marks appear
    before the actual DESCRIPTIONS OF GOODS column text.
    """
    pages = re.split(r"---\s*PAGE\s*\d+\s*---", text or "", flags=re.I)
    for idx in range(1, len(pages)):
        body = _visual_page_text(text, idx) if idx < len(pages) else pages[idx]
        split_lines = body.splitlines()
        start_idx: Optional[int] = None
        for i, ln in enumerate(split_lines):
            s = ln.strip()
            if s and _GOODS_LINE_START_RE.match(s):
                start_idx = i
                break
        if start_idx is None:
            continue
        start = sum(len(split_lines[j]) + 1 for j in range(start_idx))
        lines_out = _collect_goods_lines(
            body,
            start,
            stop_re=_GOODS_NARRATIVE_STOP_RE,
        )
        if lines_out:
            return _cap_cargo_description("\n".join(lines_out))
    return None


def _extract_standard_cargo_description(text: str) -> Optional[str]:
    """Goods lines from standard ocean B/L cargo pages."""
    lines_out: List[str] = []
    has_continuation = bool(
        re.search(
            r"Continued\s+(?:on\s+Next|From\s+Previous)\s+Sheet",
            text or "",
            re.I,
        )
    )
    pages = re.split(r"---\s*PAGE\s*\d+\s*---", text or "", flags=re.I)
    for idx in range(1, len(pages)):
        body = _visual_page_text(text, idx) if idx < len(pages) else pages[idx]
        start = 0
        m = re.search(
            r"(?:SEAL\s+NO\s*:?\s*\d+\s*\n|SEAL\s+[A-Z0-9]+\s*\n|"
            r"SAID TO CONTAIN(?:E)?(?:\s+CARGO)?\s*:?[^\n]*\n|"
            r"\d+\s*(?:CARTONS?|PACKAGES?|PALLETS?)\s+IN\s+TOTAL[^\n]*\n|"
            r"N/M\s+|\d+\s+PALLETS?\s*\n)",
            body,
            re.I,
        )
        if m:
            start = m.end()
        chunk = _collect_goods_lines(body, start, stop_re=_CARGO_LINE_STOP_RE)
        lines_out.extend(chunk)
        if lines_out and not has_continuation:
            break

    if not lines_out:
        return None
    return _cap_cargo_description("\n".join(lines_out))


def extract_cargo_description_from_pdf(text: str) -> Optional[str]:
    """Goods lines from PDF text — standard B/L and MTD layouts."""
    mtd_desc = extract_mtd_cargo_description_from_pdf(text) if _is_mtd_document(text) else None
    std_desc = _extract_standard_cargo_description(text)
    narrative_desc = _extract_goods_narrative_block(text)

    candidates = [d for d in (narrative_desc, std_desc, mtd_desc) if d]
    if not candidates:
        return None
    best = max(candidates, key=len)
    merged = best
    for candidate in candidates:
        if candidate != best:
            merged = _merge_cargo_description(merged, candidate) or merged
    return merged


def _merge_hs_codes(existing: Optional[str], pdf_hs: Optional[str]) -> Optional[str]:
    """Union of HS codes (Azure page-1 only + PDF continuation sheets)."""
    seen: set[str] = set()
    merged: List[str] = []
    for chunk in (existing, pdf_hs):
        if not chunk:
            continue
        for part in re.split(r"[,|;]+", str(chunk)):
            code = re.sub(r"\s+", "", part.strip())
            if not code or len(code) < 6 or len(code) > 12:
                continue
            if code in seen:
                continue
            seen.add(code)
            merged.append(code)
    return ", ".join(merged) if merged else None


def _merge_cargo_description(
    existing: Optional[str],
    pdf_desc: Optional[str],
) -> Optional[str]:
    """
    Combine Azure summary + PDF goods lines without duplicating content.

    When the PDF already covers every Azure token, keep the PDF version only.
    Otherwise union them, dedupe semantically, and cap to fit Dataverse 1500-char limit.
    """
    if not pdf_desc and not existing:
        return None
    if not pdf_desc:
        return existing
    if not existing:
        return pdf_desc

    def _split(value: str) -> List[str]:
        return [p.strip() for p in re.split(r"[\n;,]+", value) if p.strip()]

    def _key(value: str) -> str:
        return re.sub(r"[^A-Z0-9]+", "", value.upper())

    existing_tokens = {_key(t) for t in _split(existing) if _key(t)}
    pdf_tokens = {_key(t) for t in _split(pdf_desc) if _key(t)}

    if pdf_desc and len(pdf_desc.strip()) >= max(len((existing or "").strip()) * 2, 100):
        if not existing_tokens or existing_tokens.issubset(pdf_tokens):
            return _cap_cargo_description(pdf_desc)

    if existing_tokens and existing_tokens.issubset(pdf_tokens):
        merged_lines = _split(pdf_desc)
    else:
        merged_lines = []
        seen: set[str] = set()
        for chunk in (existing, pdf_desc):
            for s in _split(chunk):
                k = _key(s)
                if not k or k in seen:
                    continue
                seen.add(k)
                merged_lines.append(s)

    description = "\n".join(merged_lines)
    if len(description) > _CARGO_DESCRIPTION_MAX:
        description = description[: _CARGO_DESCRIPTION_MAX].rsplit("\n", 1)[0]
    return description or existing


def extract_marks_and_numbers(text: str) -> Optional[str]:
    page1 = _page1_text(text)
    m = re.search(r"\b(N/?M)\b\s+CONSOLIDATION", page1, re.I)
    if m:
        return m.group(1).upper().replace("//", "/")
    m = re.search(
        r"MARKS?\s*(?:&|AND)\s*NOS?\.?\s+Kind of Packages",
        page1,
        re.I,
    )
    if m:
        return "N/M"
    return None


def extract_shipper_load_count(text: str) -> Optional[str]:
    page1 = _page1_text(text)
    if re.search(r"SHIPPER'?S?\s+LOAD\s*&\s*COUNT", page1, re.I):
        return "SHIPPER'S LOAD & COUNT"
    return None


def _merge_handling_information(
    existing: Optional[str],
    raw_text: str,
) -> Optional[str]:
    """Combine marks (N/M) and shipper load clause without duplicating AI text."""
    parts: List[str] = []
    seen: set[str] = set()

    def add(fragment: str) -> None:
        text = re.sub(r"\s+", " ", (fragment or "")).strip()
        if not text:
            return
        key = re.sub(r"[^A-Z0-9]+", "", text.upper())
        if key in seen:
            return
        seen.add(key)
        if text.upper() in ("N/M", "NM"):
            parts.append("Marks & Nos: N/M")
        else:
            parts.append(text)

    if existing:
        for segment in re.split(r"[;\n|]+", existing):
            segment = re.sub(r"^\s*Marks?\s*&\s*Nos?\s*:\s*", "", segment, flags=re.I).strip()
            if segment:
                add(segment)

    marks = extract_marks_and_numbers(raw_text)
    if marks:
        add(marks)

    load_count = extract_shipper_load_count(raw_text)
    if load_count:
        add(load_count)

    return "; ".join(parts) if parts else existing


def _normalize_note_fragment(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().rstrip(".")


def _note_signature(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9]+", " ", text.upper())).strip()


def _is_note_subsumed(needle: str, haystack: str) -> bool:
    n_sig = _note_signature(needle)
    h_sig = _note_signature(haystack)
    if not n_sig or not h_sig or len(n_sig) < 4:
        return False
    return n_sig in h_sig


def _split_existing_notes(existing: str) -> List[str]:
    """Split AI/PDF notes into comparable fragments."""
    fragments: List[str] = []
    for block in re.split(r"\n+", existing):
        block = block.strip()
        if not block:
            continue
        if block.upper().startswith("PDF:"):
            block = block[4:].strip()
        if re.search(r"\bAMIGL\d|\bWCSE\d", block, re.I):
            fragments.append(block)
            continue
        for segment in re.split(r"\s*\|\s*", block):
            segment = segment.strip()
            if not segment:
                continue
            if re.search(r"^\d+\.", segment):
                fragments.append(segment)
                continue
            for piece in re.split(r",\s+", segment):
                piece = piece.strip()
                if piece:
                    fragments.append(piece)
    return fragments


def _merge_notes(existing: Optional[str], additions: List[str]) -> Optional[str]:
    """Deduplicate Azure notes vs PDF enrichment (keep the fuller unique lines)."""
    candidates: List[str] = []
    if existing:
        candidates.extend(_split_existing_notes(existing))
    candidates.extend(additions)
    candidates.sort(key=lambda x: len(_note_signature(x)), reverse=True)

    merged: List[str] = []
    for cand in candidates:
        cand = _normalize_note_fragment(cand)
        if not cand:
            continue
        sig = _note_signature(cand)
        merged = [kept for kept in merged if not _is_note_subsumed(kept, cand)]
        if any(sig == _note_signature(kept) for kept in merged):
            continue
        if any(_is_note_subsumed(cand, kept) for kept in merged):
            continue
        merged.append(cand)

    return "\n".join(merged) if merged else existing


def extract_shipper_contact(text: str) -> Optional[str]:
    page1 = _page1_text(text)
    tel = _first_group(r"TEL\s*:?\s*([0-9]{6,15})", page1)
    fax = _first_group(r"FAX\s*:?\s*([0-9]{6,15})", page1)
    parts = []
    if tel:
        parts.append(f"TEL {tel}")
    if fax:
        parts.append(f"FAX {fax}")
    return " / ".join(parts) if parts else None


_WAREHOUSE_ARRANGEMENT = (
    r"WAREHOUSE\s+AT\s+CNEE'?S?\s+OWN\s+COST\s+AND\s+ARRANGEMENT"
)


def _is_contact_line(line: str) -> bool:
    upper = line.upper()
    return bool(
        re.search(r"\bMOB\s*:", upper)
        or re.search(r"\bTEL\s*:", upper)
        or "@" in line
        or upper.startswith(("E:", "T:", "EMAIL:"))
    )


def extract_routing_notes(text: str) -> Optional[str]:
    page1 = _page1_text(text)
    parts: List[str] = []
    # OCR splits the in-transit clause across the vessel row and place-of-delivery block.
    head = re.search(
        r"CONSOLIDATED\s+CARGO\s+IN\s+TRANSIT\s+TO\s+MERGHEM\s+BONDED",
        page1,
        re.I,
    )
    tail = re.search(_WAREHOUSE_ARRANGEMENT, page1, re.I)
    if head and tail:
        parts.append(
            re.sub(r"\s+", " ", f"{head.group(0)} {tail.group(0)}").strip()[:500]
        )
    elif head:
        parts.append(re.sub(r"\s+", " ", head.group(0)).strip()[:500])

    service = extract_express_service_mode(page1)
    if service:
        parts.append(service)

    return "\n".join(parts) if parts else None


def extract_express_service_mode(page1: str) -> Optional[str]:
    """Freight/service mode lines (CFS-CFS, OCEAN FREIGHT) — routenotes, not legal notes."""
    upper = page1.upper()
    chunks: List[str] = []
    if re.search(r"CFS-CFS\s+DESTINATION", upper):
        chunks.append("CFS-CFS DESTINATION")
    elif re.search(r"CFS-CFS", upper):
        chunks.append("CFS-CFS")
    if re.search(r"\bOCEAN\s+FREIGHT\b", upper):
        chunks.append("OCEAN FREIGHT")
    return " / ".join(chunks) if chunks else None


def _is_service_mode_notes_only(text: Optional[str]) -> bool:
    if not text:
        return False
    remainder = re.sub(
        r"CFS-CFS|OCEAN\s+FREIGHT|DESTINATION|SERVICE\s+TYPE|MODE:?",
        "",
        text.upper(),
    )
    remainder = re.sub(r"[^A-Z0-9]+", "", remainder)
    return len(remainder) < 8


def extract_bl_legal_notes(text: str) -> Optional[str]:
    """Clause text at foot of B/L (T/T release, SOLAS/VGM), not freight mode labels."""
    page1 = _page1_text(text)
    parts: List[str] = []
    for pat in (
        r"DON'?T\s+RELEASE\s+THIS\s+SHIPMENT[^;]+;[^;\n]*",
        r"SOLAS/VGM\s+COMPLIANCE[^.\n]+(?:\.|ACCOUNT\.?)",
        r"CARRIER\s+IS\s+NOT\s+LIABLE[^.\n]+\.",
    ):
        m = re.search(pat, page1, re.I)
        if m:
            frag = re.sub(r"\s+", " ", m.group(0)).strip()
            frag = frag.replace("&amp;", "&")
            if frag and frag not in parts:
                parts.append(frag)
    return " ".join(parts)[:500] if parts else None


def _extract_consignee_before_same_as(page1: str) -> Dict[str, Optional[str]]:
    """Consignee block ending at standalone SAME AS CONSIGNEE (original/express B/L)."""
    m = re.search(
        r"\n([A-Z][^\n]{4,100}?(?:COMPANY|CO\.|LTD|INDUSTRIES|S\.A\.))\s*\n"
        r"(.*?)(?=\nSAME\s+AS\s+CONSIGNEE\b)",
        page1,
        re.I | re.S,
    )
    if not m:
        return {}
    name = re.sub(r"\s+", " ", m.group(1)).strip()
    if not _is_plausible_consignee_name(name):
        return {}
    address_parts: List[str] = []
    contact_parts: List[str] = []
    for line in (ln.strip() for ln in m.group(2).splitlines() if ln.strip()):
        if _is_contact_line(line):
            contact_parts.append(re.sub(r"\s+", " ", line).strip())
        elif not line.upper().startswith(("MARKS:", "ACID:", "IMPORTER", "EXPORTER")):
            address_parts.append(re.sub(r"\s+", " ", line).strip())
    out: Dict[str, Optional[str]] = {"name": name[:200]}
    if address_parts:
        out["address"] = ", ".join(address_parts)[:250]
    if contact_parts:
        out["contact"] = ", ".join(contact_parts)[:250]
    return out


def _uses_literal_same_as_consignee_notify(text: str) -> bool:
    """Literal notify phrase when SAME AS CONSIGNEE is a standalone line, not a dual-column notify header."""
    if not re.search(r"SAME\s+AS\s+CONSIGNEE", text, re.I):
        return False
    return not re.search(
        r"Delivery\s+Agent\s+at\s+Destination|Notify\s+Party",
        text,
        re.I,
    )


def _is_plausible_consignee_name(name: str) -> bool:
    upper = name.upper()
    if not name or len(name) < 4:
        return False
    if re.fullmatch(r"[\d\s\-+().]+", name):
        return False
    if re.search(r"^\d{7,}$", re.sub(r"\D", "", name)):
        return False
    if "EXPRESS BILL" in upper or "BILL OF LADING" in upper:
        return False
    if "HTTP://" in upper or "WWW." in upper:
        return False
    if "MESCO" in upper and "MARINE" in upper:
        return False
    if re.search(r"\bM/?V\b", upper) or upper.startswith("VESSEL"):
        return False
    if re.fullmatch(r"[A-Z ]{3,30}", upper) and upper.strip() in {
        "AMBARLI",
        "ALEXANDRIA",
        "ISTANBUL",
        "HAMBURG",
    }:
        return False
    if "ALEXANDRIA ALEXANDRIA" in upper:
        return False
    if re.search(r"\bISTANBUL\b", upper) and re.search(r"\bTURK", upper):
        return False
    if "NAKLIYAT" in upper or "TICARET A.S" in upper:
        return False
    if upper.startswith("TEL ") or upper.startswith("TEL:"):
        return False
    return True


_BOGUS_PARTY_ADDRESS_RE = re.compile(
    r"^(?:packages?|package\(s\)|number\s+of(?:\s+packages)?|of\s+packages|"
    r"no\.?\s+and\s+kind(?:\s+of\s+packages)?|description\s+of\s+packages|"
    r"marks\s+and(?:\s+nos)?|container\s+and(?:\s+seals)?|gross\s+weight|"
    r"measurements?|shipper'?s\s+load|said\s+to\s+contain|coupling\.?|"
    r"shpr:|notify|mto\s+registration|kind\s+of\s+packages)$",
    re.I,
)

_ADDRESS_SIGNAL_RE = re.compile(
    r"\d|(?:^|[\s,])(?:ST\.?|STREET|ROAD|AVE|BLVD|PORT|CITY|ZONE|"
    r"EGYPT|INDIA|CAIRO|ALEXANDRIA|PUNE|MUMBAI|GIZA|RAMADAN)(?:[\s,]|$)",
    re.I,
)


def _is_plausible_party_address(addr: Optional[str]) -> bool:
    """Reject cargo-table column headers and other non-address OCR bleed."""
    if not addr or not str(addr).strip():
        return False
    text = re.sub(r"\s+", " ", str(addr)).strip()
    if len(text) < 6:
        return False
    if _BOGUS_PARTY_ADDRESS_RE.match(text):
        return False
    upper = text.upper()
    if upper in {"PACKAGES", "PACKAGE(S)", "OF PACKAGES", "PACKAGE", "COUPLING"}:
        return False
    if re.fullmatch(r"[A-Z\s()./\-]{3,40}", upper) and not _ADDRESS_SIGNAL_RE.search(text):
        return False
    return bool(_ADDRESS_SIGNAL_RE.search(text) or len(text) > 24)


def sanitize_party_address(addr: Optional[str]) -> Optional[str]:
    if not _is_plausible_party_address(addr):
        return None
    return re.sub(r"\s+", " ", str(addr)).strip()[:250]


def _is_to_order_consignee_name(name: Optional[str]) -> bool:
    return bool(re.search(r"\bTO\s+THE\s+ORDER\s+OF\b", str(name or ""), re.I))


_TO_ORDER_BANK_STOP_RE = re.compile(
    r"^(?:NOTIFY|MTO\s+REGISTRATION|MARKS\s+AND|CONTAINER|VESSEL|PLACE\s+OF|"
    r"BYTEPORT|WORKAFELLA)",
    re.I,
)


def _extract_mtd_consignee_block(text: str) -> Dict[str, Optional[str]]:
    """MTD: company + street lines after Consignee header (skip MTO registration line)."""
    page1 = _page1_text(text)
    if not (
        _is_mtd_document(text)
        or re.search(r"Consignee[^\n]*\n\s*MTO\s+REGISTRATION", page1, re.I)
    ):
        return {}
    if re.search(r"TO\s+THE\s+ORDER\s+OF", page1, re.I):
        return {}
    m = re.search(
        r"Consignee[^\n]*\n"
        r"(?:MTO\s+REGISTRATION[^\n]*\n)?"
        r"([^\n]+)\n"
        r"(.*?)(?=\n(?:TEL:|FAX:|Notify|NOTIFY|SAME\s+AS|Vessel|Place\s+Of|Port\s+of|BYTEPORT|ICD/|MARKS\s*&))",
        page1,
        re.I | re.S,
    )
    if not m:
        return {}
    name = re.sub(r"\s+", " ", m.group(1)).strip()
    if not _is_plausible_consignee_name(name) or name.upper().startswith("MTO "):
        return {}
    addr_parts: List[str] = []
    contact: Optional[str] = None
    for ln in m.group(2).splitlines():
        s = ln.strip()
        if not s:
            continue
        if re.match(r"^(TEL|FAX|EMAIL)", s, re.I):
            if re.match(r"^TEL", s, re.I):
                contact = re.sub(r"\s+", " ", s)
            continue
        cleaned = re.sub(r"\s+", " ", s)
        if _is_plausible_party_address(cleaned) or re.search(
            r"\d|ST\.?|STREET|ROAD|EGYPT|INDIA|CAIRO|ALEXANDRIA|MANSOURA",
            cleaned,
            re.I,
        ):
            addr_parts.append(cleaned)
    address = sanitize_party_address(", ".join(addr_parts)) if addr_parts else None
    out: Dict[str, Optional[str]] = {"name": name[:200], "address": address}
    if contact:
        out["contact"] = contact[:250]
    return out


def normalize_delivery_address_for_crm(
    value: Optional[str],
    *,
    destination: Optional[str] = None,
) -> Optional[str]:
    """Keep destination-agent blocks out of the short delivery-address column."""
    from dataverse_field_limits import cap_field

    if not value or not str(value).strip():
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    upper = text.upper()
    if "MARINE" in upper and "ENGINEERING" in upper:
        if destination and str(destination).strip():
            return cap_field("mesco_operations", "mesco_deliveryaddress", str(destination).strip())
        return None
    return cap_field("mesco_operations", "mesco_deliveryaddress", text)


def _extract_to_order_consignee(page1: str) -> Dict[str, Optional[str]]:
    """Consignee when goods are to order of a bank — name only, no street address."""
    bank: Optional[str] = None
    m = re.search(r"TO\s+THE\s+ORDER\s+OF\s*\n\s*([^\n]+)", page1, re.I)
    if m:
        bank = re.sub(r"\s+", " ", m.group(1)).strip()
    else:
        m = re.search(
            r"TO\s+THE\s+ORDER\s+OF\s+([A-Z0-9][A-Z0-9 &./\-'()]+)",
            page1,
            re.I,
        )
        if m:
            bank = re.sub(r"\s+", " ", m.group(1)).strip()
    if not bank or _TO_ORDER_BANK_STOP_RE.match(bank):
        return {}
    if "MESCO" in bank.upper() and "MARINE" in bank.upper():
        return {}
    if not _is_plausible_consignee_name(bank) and "BANK" not in bank.upper():
        return {}
    name = bank if _is_to_order_consignee_name(bank) else f"TO THE ORDER OF {bank}"
    return {"name": name[:200], "address": None}


def _extract_holder_consignee(page1: str) -> Dict[str, Optional[str]]:
    m = re.search(r"TO THE HOLDER OF\s*\n([A-Z0-9\-]+)", page1, re.I)
    if not m:
        return {}
    ref = re.sub(r"\s+", " ", m.group(1)).strip()
    return {"name": f"TO THE HOLDER OF {ref}"[:200], "address": None}


def _extract_express_bl_notify_party(page1: str) -> Dict[str, Optional[str]]:
    """Notify block after TO THE HOLDER OF / master B/L ref, before vessel routing."""
    m = re.search(
        r"TO THE HOLDER OF\s*\n[A-Z0-9\-]+\s*\n(.*?)(?=\nM/?V\s|\nMARKS:)",
        page1,
        re.I | re.S,
    )
    if not m:
        return {}
    lines = [ln.strip() for ln in m.group(1).splitlines() if ln.strip()]
    if not lines:
        return {}
    name = re.sub(r"\s+", " ", lines[0]).strip()
    if not _is_plausible_consignee_name(name) or "MESCO" in name.upper():
        return {}
    addr_parts: List[str] = []
    for line in lines[1:]:
        upper = line.upper()
        if upper.startswith(("E:", "T:", "EMAIL:", "TEL:")):
            addr_parts.append(line)
        elif any(k in upper for k in ("EGYPT", "RAMADAN", "OCTOBER", "CITY", "ZONE")):
            addr_parts.append(line)
        elif re.search(r"@[A-Z0-9.\-]+\.[A-Z]{2,}", line, re.I):
            addr_parts.append(line)
        elif re.search(r"^\+?\d", line):
            addr_parts.append(line)
    result: Dict[str, Optional[str]] = {"mesco_notify1": name[:200]}
    if addr_parts:
        result["mesco_notifyaddress"] = re.sub(r"\s+", " ", ", ".join(addr_parts))[:250]
    return result


_DESTINATION_AGENT_RE = re.compile(
    r"MESCO|MARINE\s*&?\s*ENGINEERING\s+SERVICES?\s+COMPANY",
    re.I,
)

_ISSUING_AGENT_HINT_RE = re.compile(
    r"\b(?:LOGISTICS|FREIGHT|FORWARDING|NAVEXEL|BYTEPORT|NXT|UTT|SHIPPING\s+AGENCY)\b",
    re.I,
)

_COMPANY_SUFFIX_RE = re.compile(
    r"(?:PRIVATE\s+)?(?:LIMITED|LTD\.?|PTE\.?\s*LTD\.?|LLC|INC\.?|GMBH|S\.A\.?|CO\.?)$",
    re.I,
)


def _clean_issuing_agent_name(name: str) -> Optional[str]:
    cleaned = re.sub(r"\s+", " ", (name or "")).strip(" .,-|")
    cleaned = re.sub(r"^(?:FOR|THE)\s+", "", cleaned, flags=re.I).strip()
    if not cleaned or len(cleaned) < 6:
        return None
    if re.search(
        r"\b(?:LOSS\s+OF|DAMAGE\s+TO\s+THE\s+GOODS|GOODS\s+SHALL\s+BE|"
        r"LIABILITY|CARRIAGE|MERCHANT|CLAUSE|CONTRACT|COURTS?)\b",
        cleaned,
        re.I,
    ):
        return None
    if _DESTINATION_AGENT_RE.search(cleaned):
        return None
    if re.search(r"^(CONSIGNEE|SHIPPER|NOTIFY|VESSEL|PORT\s+OF|MTO\s+REGISTRATION)\b", cleaned, re.I):
        return None
    if not _ISSUING_AGENT_HINT_RE.search(cleaned) and not _COMPANY_SUFFIX_RE.search(cleaned):
        return None
    return cleaned[:200]


def _is_destination_agent_name(name: Optional[str]) -> bool:
    return bool(name and _DESTINATION_AGENT_RE.search(str(name)))


def extract_issuing_agent(text: str) -> Optional[str]:
    """
    Issuing forwarder / MTO on the B/L (maps to Dynamics ``mesco_agent``).

  Not the ocean carrier (``mesco_shippingline``) and not the destination
    delivery agent at the bottom of the page (MESCO / notify2).
    """
    page1 = _page1_text(text)
    sample = (text or "")[:20000]

    patterns = (
        r"(?:Freight\s+Amount\s+)?For\s+([A-Z0-9][A-Z0-9 &./\-'()]{4,100}?(?:PRIVATE\s+)?(?:LIMITED|LTD\.?))",
        r"SIGNED\s+FOR\s+(?:THE\s+)?([A-Z0-9][A-Z0-9 &./\-'()]{4,100}?(?:LIMITED|LTD\.?))",
        r"(?:Forwarding\s+Agent|Issued\s+by|As\s+agents?\s+for)\s*[:\-]?\s*"
        r"([A-Z0-9][A-Z0-9 &./\-'()]{4,100}?(?:LIMITED|LTD\.?))",
    )
    for pat in patterns:
        m = re.search(pat, sample, re.I)
        if m:
            name = _clean_issuing_agent_name(m.group(1))
            if name:
                return name

    if _is_mtd_document(text) or re.search(r"MTO\s+REGISTRATION", page1, re.I):
        for pat in (
            r"\n([A-Z][A-Z0-9 &./\-'()]*LOGISTICS[^\n]{0,80}?(?:PRIVATE\s+)?(?:LIMITED|LTD\.?))",
            r"\n(BYTEPORT\s+LOGISTICS[^\n]{0,80}?(?:PRIVATE\s+)?(?:LIMITED|LTD\.?))",
            r"(NAVEXEL[^\n]{0,40})",
        ):
            m = re.search(pat, page1, re.I)
            if m:
                name = _clean_issuing_agent_name(m.group(1))
                if name:
                    return name

    m = re.search(
        r"Carrier['\u2019]?s?\s+Agent[^\n]*\n\s*([A-Z][^\n]{5,100}?(?:LIMITED|LTD\.?))",
        page1,
        re.I,
    )
    if m:
        name = _clean_issuing_agent_name(m.group(1))
        if name:
            return name

    return None


def _extract_mesco_delivery_agent(page1: str) -> Dict[str, Optional[str]]:
    m = re.search(
        r"(MARINE\s*&\s*ENGINEERING\s+SERVICES\s+COMPANY\s*\(MESCO\))\s*\n([^\n]+)",
        page1,
        re.I,
    )
    if not m:
        return {}
    name = re.sub(r"\s+", " ", m.group(1)).strip()
    addr = re.sub(r"\s+", " ", m.group(2)).strip()
    out: Dict[str, Optional[str]] = {
        "delivery_agent": name,
        "mesco_shippingline": name,
    }
    tel_m = re.search(
        r"TEL\s*:\s*([^\n]+)",
        page1[m.end() : m.end() + 120],
        re.I,
    )
    if tel_m:
        addr = f"{addr}, TEL: {tel_m.group(1).strip()}"
    out["delivery_agent_address"] = addr[:250]
    return out


def extract_freight_payable_at(text: str) -> Optional[str]:
    page1 = _page1_text(text)
    known_pol = ("AMBARLI", "ISTANBUL", "MERSIN", "ALIAGA", "GEMLIK", "HAYDARPASA")
    for port in known_pol:
        m = re.search(rf"CFS-CFS\s*\n\s*{port}\b", page1, re.I)
        if m:
            return port
        m = re.search(rf"CFS-CFS\s+{port}\b", page1, re.I)
        if m:
            return port
    m = re.search(r"FREIGHT\s+PREPAID\s*/\s*(AMBARLI)\b", page1, re.I)
    if m:
        return m.group(1).strip()
    return None


def _extract_express_bl_consignee(page1: str) -> Dict[str, Optional[str]]:
    """Express B/L layout: consignee name/address after shipper, before SAME AS CONSIGNEE."""
    patterns = (
        r"EXPRESS\s+BILL\s+OF\s+LADING\s*\n([^\n]+)\n([^\n]+)",
        r"HTTP://[^\n]+\nEXPRESS\s+BILL\s+OF\s+LADING\s*\n([^\n]+)\n([^\n]+)",
        r"\n([A-Z][^\n]{5,80}?(?:COMPANY|CO\.|LTD|INDUSTRIES))\s*\n"
        r"([^\n]+(?:EGYPT|RAMADAN|FREE\s+ZONE|OCTOBER\s+CITY)[^\n]*)",
    )
    for pat in patterns:
        m = re.search(pat, page1, re.I)
        if not m:
            continue
        name = re.sub(r"\s+", " ", m.group(1)).strip()
        addr = re.sub(r"\s+", " ", m.group(2)).strip()
        if _is_plausible_consignee_name(name):
            return {"name": name[:200], "address": addr[:250]}
    return {}


def _extract_mesco_consignee_block(text: str) -> Dict[str, Optional[str]]:
    """
    CMA CGM import B/L: consignee column is MESCO (EXPORT REFERENCES is a form header).
    """
    page1 = _visual_page_text(text, 1)
    m = re.search(
        r"Consignee\s*(?:EXPORT\s+REFERENCES\s*)?\n"
        r"(MARINE\s*&?\s*ENGINEERING\s+SERVICES[^\n]*)\n"
        r"(COMPANY\s*-\s*MESCO)\n"
        r"(.*?)(?=\n(?:NOTIFY PARTY|CARRIER\s*:|TEL\.\+?\d|TEL\.\*))",
        page1,
        re.I | re.S,
    )
    if not m:
        return {}
    part1 = re.sub(r"\s+", " ", m.group(1)).strip()
    part2 = re.sub(r"\s+", " ", m.group(2)).strip()
    name = f"{part1} {part2}"
    name = name.replace("MARINE&ENGINEERING", "MARINE & ENGINEERING")
    if "COMPANY" not in part1.upper():
        name = f"{part1} {part2}"
    addr_parts: List[str] = []
    for ln in m.group(3).splitlines():
        s = ln.strip()
        if not s or re.match(r"^TEL", s, re.I):
            continue
        if "NOTIFY" in s.upper():
            break
        addr_parts.append(s)
    address = re.sub(r"\s+", " ", ", ".join(addr_parts))[:250]
    return {"name": name[:200], "address": address or None}


def extract_consignee_block(text: str) -> Dict[str, Optional[str]]:
    """Consignee name and street address (never the delivery agent / MESCO block)."""
    page1 = _page1_text(text)

    mesco_cnee = _extract_mesco_consignee_block(text)
    if mesco_cnee:
        return mesco_cnee

    holder = _extract_holder_consignee(page1)
    if holder:
        return holder

    order = _extract_to_order_consignee(page1)
    if order:
        return order

    mtd_cnee = _extract_mtd_consignee_block(text)
    if mtd_cnee:
        return mtd_cnee

    m = re.search(
        r"(?<!SAME AS )Consignee(?:\s+or\s+Order)?\s*\n([^\n]+)\n([^\n]+)",
        page1,
        re.I,
    )
    if m:
        name = re.sub(r"\s+", " ", m.group(1)).strip()
        addr = re.sub(r"\s+", " ", m.group(2)).strip()
        if name.upper() in ("EXPORT REFERENCES", "EXPORT REFERENCE"):
            pass
        elif _is_plausible_consignee_name(name):
            if addr.upper().startswith(("SHPR:", "IMPORTER VAT", "DELIVERY AGENT", "NOTIFY")):
                return {"name": name[:200], "address": None}
            clean_addr = sanitize_party_address(addr)
            return {"name": name[:200], "address": clean_addr}

    same_as_block = _extract_consignee_before_same_as(page1)
    if same_as_block:
        return same_as_block

    return _extract_express_bl_consignee(page1)


def extract_consignee_from_pdf(text: str) -> Optional[str]:
    """Consignee block is separate from Delivery Agent / Notify (do not use MESCO here)."""
    return extract_consignee_block(text).get("name")


def _delivery_agent_notify_block(page1: str) -> Optional[str]:
    block_m = re.search(
        r"Delivery Agent at Destination\s*(.*?)(?:Vessel and Voy|Container Nos\b)",
        page1,
        re.I | re.S,
    )
    if block_m:
        return block_m.group(1)
    block_m = re.search(
        r"Notify\s+Party[^\n]*\n(.*?)(?:Vessel and Voy|Container Nos\b)",
        page1,
        re.I | re.S,
    )
    return block_m.group(1) if block_m else None


def _extract_stacked_same_as_consignee(
    block: str,
    consignee: Dict[str, Optional[str]],
) -> Dict[str, Optional[str]]:
    """
    Vertical layout: delivery agent (MESCO) lines, then SAME AS CONSIGNEE for notify.
    OCR often puts MESCO on the Notify Party header line — that is the delivery agent.
    """
    result: Dict[str, Optional[str]] = {}
    delivery_parts: List[str] = []

    mesco_m = re.search(
        r"Notify\s+Party[^\n]*\s+(MESCO\s+MARINE\s*&?\s*ENGINEERING\s+SERVICE\s+CO\.?)",
        block,
        re.I,
    )
    if mesco_m:
        result["delivery_agent"] = re.sub(r"\s+", " ", mesco_m.group(1)).strip()

    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    for line in lines:
        if re.search(r"Notify\s+Party", line, re.I):
            continue
        if re.search(r"SAME\s+AS\s+CONSIGNEE", line, re.I):
            break
        upper = line.upper()
        if any(k in upper for k in ("PATRICE", "LUMUMBA", "MESCO")):
            delivery_parts.append(line)

    sac_at = None
    for i, line in enumerate(lines):
        if re.search(r"SAME\s+AS\s+CONSIGNEE", line, re.I):
            sac_at = i
            break
    if sac_at is not None:
        for line in lines[sac_at + 1 :]:
            upper = line.upper()
            if any(k in upper for k in ("VESSEL", "CONTAINER NOS", "AL BIDDA", "PORT OF")):
                break
            if any(
                k in upper
                for k in ("BAB SHARQ", "ALEXANDRIA", "TEL:", "FAX:", "TAX ID", "3991018", "297")
            ):
                delivery_parts.append(line)

    if delivery_parts:
        result["delivery_agent_address"] = re.sub(
            r"\s+", " ", ", ".join(delivery_parts)
        )[:250]

    if consignee.get("name"):
        result["mesco_notify1"] = consignee["name"]
    if consignee.get("address"):
        result["mesco_notifyaddress"] = consignee["address"]

    return result


def extract_notify_and_delivery_agent(
    text: str,
    consignee: Optional[Dict[str, Optional[str]]] = None,
) -> Dict[str, Optional[str]]:
    """
    Split Notify Party from Delivery Agent at Destination.

    Supports:
    - Stacked layout with SAME AS CONSIGNEE (notify = consignee, agent = MESCO).
    - Two-column layout with EL SEWEDY and MESCO on one header line (FEDERAL-style).
    """
    page1 = _page1_text(text)
    block = _delivery_agent_notify_block(page1)
    if not block:
        return {}

    if re.search(r"SAME\s+AS\s+CONSIGNEE", block, re.I):
        if consignee is None:
            consignee = extract_consignee_block(text)
        return _extract_stacked_same_as_consignee(block, consignee)

    result: Dict[str, Optional[str]] = {}

    header = re.search(
        r"(EL\s+SEWEDY[^.\n]{0,80}?)\.?\s*(MESCO\s+MARINE\s*&\s*ENGINEERING\s+SERVICE\s+CO\.?)",
        block,
        re.I,
    )
    if header:
        result["mesco_notify1"] = re.sub(r"\s+", " ", header.group(1)).strip()
        result["delivery_agent"] = re.sub(r"\s+", " ", header.group(2)).strip()

    notify_addr_parts: List[str] = []
    delivery_addr_parts: List[str] = []

    def _split_dual_column_line(line: str) -> Optional[tuple[str, str]]:
        upper = line.upper()
        if "INDURTRIAL" in upper and "LUMUMBA" in upper:
            left, _, right = line.partition("8 PATRICE")
            return left.strip().rstrip(","), ("8 PATRICE" + right).strip()
        if "GIZA" in upper and "BAB SHARQ" in upper:
            left, _, right = line.partition("BAB SHARQ")
            return left.strip().rstrip(","), ("BAB SHARQ" + right).strip()
        if "204987636" in upper and "3991018" in upper:
            left, _, right = line.partition("TEL:")
            return left.strip().rstrip(","), ("TEL:" + right).strip()
        return None

    for line in block.splitlines():
        line = line.strip()
        if not line:
            continue
        if header and line.upper().startswith("EL SEWEDY") and "MESCO" in line.upper():
            continue
        dual = _split_dual_column_line(line)
        if dual:
            if dual[0]:
                notify_addr_parts.append(dual[0])
            if dual[1]:
                delivery_addr_parts.append(dual[1])
            continue
        upper = line.upper()
        if any(k in upper for k in ("MESCO", "LUMUMBA", "BAB SHARQ", "3991018", "3991001")):
            delivery_addr_parts.append(line)
        elif any(
            k in upper
            for k in ("SEWEDY", "INDURTRIAL", "OCTOBER", "GIZA", "204987636", "IMPORTER VAT")
        ):
            notify_addr_parts.append(line)
        elif "TAX ID" in upper and "297" in upper:
            delivery_addr_parts.append(line)

    if notify_addr_parts:
        result["mesco_notifyaddress"] = re.sub(r"\s+", " ", ", ".join(notify_addr_parts))[:250]
    elif result.get("mesco_notify1"):
        result["mesco_notifyaddress"] = result["mesco_notify1"]

    if delivery_addr_parts:
        result["delivery_agent_address"] = re.sub(r"\s+", " ", ", ".join(delivery_addr_parts))[:250]

    return result


def _normalize_place_name(place: str) -> str:
    place = re.sub(r"\s+", " ", place).strip()
    parts = place.split()
    if len(parts) >= 2 and parts[0].upper() == parts[1].upper():
        return parts[0]
    return place


def extract_place_of_delivery(text: str) -> Optional[str]:
    """Place of delivery port (not place/date of B/L issue)."""
    page1 = _page1_text(text)
    m = re.search(
        r"SAME AS CONSIGNEE\s*\n(?:M/?V[^\n]+\n)?([A-Z][A-Z0-9 /\-]{2,30})\s*\n([A-Z][A-Z ]{3,40})\s*\n",
        page1,
        re.I,
    )
    if m:
        return _normalize_place_name(m.group(2))[:120]
    m = re.search(r"\n(ALEXANDRIA)\s+ALEXANDRIA\s*\n", page1, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r"\n([A-Z][A-Z ]{3,40})\s+\1\s*\n", page1, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(
        r"SAME AS CONSIGNEE\s*\n(?:M/?V[^\n]+\n)?(?:[A-Z][A-Z0-9 /\-]{2,30}\s*\n)?(ALEXANDRIA)\s*\n",
        page1,
        re.I,
    )
    if m:
        return m.group(1).strip()
    m = re.search(
        r"M/?V\s+[^\n]+\s+(AMBARLI)\s*\n(ALEXANDRIA)\s*\n",
        page1,
        re.I,
    )
    if m:
        return m.group(2).strip()
    return None


def extract_express_vessel_voyage(text: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """M/V NAME / voyage, optional port of loading on same or next line."""
    page1 = _page1_text(text)
    m = re.search(
        r"M/?V\s+([A-Z][A-Z0-9 ]+?)\s*/\s*([0-9/]+)(?:\s+([A-Z][A-Z ]{2,30}))?",
        page1,
        re.I,
    )
    if not m:
        return None, None, None
    vessel = re.sub(r"\s+", " ", m.group(1)).strip()
    voyage = re.sub(r"\s+", " ", m.group(2)).strip()
    pol = re.sub(r"\s+", " ", m.group(3)).strip() if m.group(3) else None
    return vessel, voyage, pol


def extract_no_of_org_bls(text: str) -> Optional[str]:
    upper = (text or "").upper()
    if re.search(r"0\s*/\s*ZERO", upper):
        return "0/ZERO"
    if re.search(r"ZERO\s*\(\s*0\s*\)", upper):
        return "0"
    m = re.search(
        r"(?:NUMBER\s+OF\s+ORIGINAL|NO\.?\s*OF\s+ORIG\.?)\s+BILLS?\s+OF\s+LADING\s*[:\-]?\s*([^\n]{1,40})",
        upper,
        re.I,
    )
    if m:
        token = m.group(1).strip()
        if re.search(r"ZERO|\b0\b|\(0\)", token):
            return "0"
        digit = re.search(r"\b(\d+)\b", token)
        if digit:
            return digit.group(1)
    return None


def extract_delivery_address(text: str) -> Optional[str]:
    page1 = _page1_text(text)
    m = re.search(_WAREHOUSE_ARRANGEMENT, page1, re.I)
    if m:
        return re.sub(
            r"\s+",
            " ",
            f"ALEXANDRIA {m.group(0)}",
        ).strip()[:250]
    m = re.search(
        rf"\(17\)\s*Place\s+of\s+Delivery\s*[\s\S]{{0,80}}?{_WAREHOUSE_ARRANGEMENT}",
        page1,
        re.I,
    )
    if m:
        wh = re.search(_WAREHOUSE_ARRANGEMENT, m.group(0), re.I)
        if wh:
            return re.sub(r"\s+", " ", f"ALEXANDRIA {wh.group(0)}").strip()[:250]
    return extract_place_of_delivery(text)


def extract_service_type(text: str) -> Optional[str]:
    return _first_group(
        r"\(26\)\s*Service\s+Type/Mode\s*\n?\s*([A-Z0-9/ ]{3,30})",
        _page1_text(text),
    ) or _first_group(r"\b(FCL/FCL\s+O/O|FCL/FCL|LCL/LCL)\b", _page1_text(text))


def extract_freight_clauses(text: str) -> Optional[str]:
    page1 = _page1_text(text)
    clauses: List[str] = []
    m = re.search(
        r"\(24\)\s*FREIGHT\s*&\s*CHARGES.*?(?=\(25\)|\(26\)|\(27\)|\(28\))",
        page1,
        re.I | re.S,
    )
    block = m.group(0) if m else None
    if block:
        for line in re.split(r"[\n;]+", block):
            line = re.sub(r"\s+", " ", line).strip()
            if re.match(r"^\d+\.", line) or "FREE OUT" in line.upper():
                clauses.append(line)
    return " | ".join(clauses) if clauses else None


def extract_form_number(text: str) -> Optional[str]:
    return _first_group(r"FORM\s+NO\.?\s*([A-Z0-9\-]+)", _page1_text(text))


def extract_carrier_name(text: str) -> Optional[str]:
    page1 = _page1_text(text)
    m = re.search(
        r"Vessel\s+Provider\s+([A-Za-z0-9 ().,&\-]+?Pte\.?\s*Ltd\.?)",
        page1,
        re.I,
    )
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()[:120]
    m = re.search(r'doing business as\s*"([^"]+)"', page1, re.I)
    if m:
        return m.group(1).strip()
    return None


def extract_page1_cargo_references(text: str, attached_refs: List[str]) -> List[str]:
    """AMIGL/WCSE lines in the goods block on page 1 (not attachment-list houses)."""
    page1 = _page1_text(text)
    attached = {r.upper() for r in attached_refs}
    refs: List[str] = []
    for m in re.finditer(r"\b(AMIGL\d{8,12}[A-Z]?|WCSE\d{6,14}[A-Z]?)\b", page1, re.I):
        ref = m.group(1).upper()
        if ref in attached or ref in refs:
            continue
        refs.append(ref)
    return refs


def extract_teus(text: str) -> Optional[str]:
    m = re.search(r"\b(\d+)\s*X\s*(\d{2}[A-Z]{0,3})\b", _page1_text(text), re.I)
    return "1" if m else None


def enrich_bl_from_pdf_text(data: Dict[str, Any], raw_text: str) -> Dict[str, Any]:
    """Merge deterministic PDF fields into a validated B/L record."""
    if not raw_text:
        return data

    # Manifest / ISALY draft / debit-note / sea-waybill rows already have per-B/L fields.
    if (
        data.get("_manifest_pdf_row")
        or data.get("_isaly_draft_row")
        or data.get("document_type") == "debit_note"
        or data.get("extraction_method") == "debit_note_direct"
        or data.get("document_type") == "consolidation_sea_waybill"
        or data.get("extraction_method") == "pdf_sea_waybill_direct"
    ):
        locked = {
            k: data[k]
            for k in (
                "mesco_vessel",
                "mesco_voytruckno",
                "cr401_totalpackages",
                "cr401_totalgrossweight",
                "mesco_cargodescription",
                "cargo_lines",
                "mesco_bookingnumber",
            )
            if data.get(k) not in (None, "", [], {})
        }
        if not locked:
            return data
        enriched = dict(data)
        hs = extract_hs_codes_from_goods(raw_text)
        if hs:
            enriched["mesco_hscode"] = _merge_hs_codes(enriched.get("mesco_hscode"), hs)
        if not enriched.get("mesco_shippingline"):
            carrier = extract_carrier_name(raw_text)
            if carrier:
                enriched["mesco_shippingline"] = carrier
        for key, val in locked.items():
            enriched[key] = val
        return enriched

    attached = extract_attached_list_house_refs(raw_text)
    page1_refs = extract_page1_cargo_references(raw_text, attached)

    hs = extract_hs_codes_from_goods(raw_text)
    if hs:
        data["mesco_hscode"] = _merge_hs_codes(data.get("mesco_hscode"), hs)

    cargo_desc = extract_cargo_description_from_pdf(raw_text)
    if cargo_desc:
        data["mesco_cargodescription"] = _merge_cargo_description(
            data.get("mesco_cargodescription"),
            cargo_desc,
        )

    merged_handling = _merge_handling_information(
        data.get("mesco_handlinginformation"),
        raw_text,
    )
    if merged_handling:
        data["mesco_handlinginformation"] = merged_handling

    contact = extract_shipper_contact(raw_text)
    if contact:
        data["mesco_shippercontactnumber"] = contact

    page1 = _page1_text(raw_text)
    consignee_block = extract_consignee_block(raw_text)
    if consignee_block.get("name"):
        data["mesco_consigneenamecontactno"] = consignee_block["name"]
    if consignee_block.get("address"):
        data["mesco_consigneeaddress"] = sanitize_party_address(consignee_block["address"])
    elif _is_to_order_consignee_name(consignee_block.get("name")) or (
        (consignee_block.get("name") or "").upper().startswith("TO THE HOLDER")
    ):
        data["mesco_consigneeaddress"] = None
    else:
        data["mesco_consigneeaddress"] = sanitize_party_address(
            data.get("mesco_consigneeaddress"),
        )
    if _is_to_order_consignee_name(data.get("mesco_consigneenamecontactno")):
        data["mesco_consigneeaddress"] = None
    if consignee_block.get("contact"):
        data["mesco_consigneecontactnumber"] = consignee_block["contact"]

    literal_same_as = _uses_literal_same_as_consignee_notify(raw_text)
    same_as_notify_addr = sanitize_party_address(
        consignee_block.get("address") or data.get("mesco_consigneeaddress"),
    )
    if literal_same_as:
        data["mesco_notify1"] = "SAME AS CONSIGNEE"
        if same_as_notify_addr:
            data["mesco_notifyaddress"] = same_as_notify_addr
    elif re.search(r"SAME\s+AS\s+CONSIGNEE", raw_text, re.I) and consignee_block.get("name"):
        data["mesco_notify1"] = consignee_block["name"]
        if same_as_notify_addr:
            data["mesco_notifyaddress"] = same_as_notify_addr
    else:
        express_notify = _extract_express_bl_notify_party(page1)
        if express_notify.get("mesco_notify1"):
            data["mesco_notify1"] = express_notify["mesco_notify1"]
        if express_notify.get("mesco_notifyaddress"):
            data["mesco_notifyaddress"] = express_notify["mesco_notifyaddress"]

    mesco_delivery = _extract_mesco_delivery_agent(page1)
    if mesco_delivery.get("delivery_agent"):
        data["delivery_agent"] = mesco_delivery["delivery_agent"]
        data["mesco_notify2"] = mesco_delivery["delivery_agent"]
        if mesco_delivery.get("delivery_agent_address"):
            data["delivery_agent_address"] = mesco_delivery["delivery_agent_address"]

    issuing_agent = extract_issuing_agent(raw_text)
    if issuing_agent:
        data["mesco_agent"] = issuing_agent
    elif _is_destination_agent_name(data.get("mesco_agent")):
        data["mesco_agent"] = None

    freight_at = extract_freight_payable_at(raw_text)
    if freight_at:
        data["mesco_freightpayableat"] = freight_at

    parties = extract_notify_and_delivery_agent(raw_text, consignee_block)
    if parties.get("mesco_notify1") and not literal_same_as:
        data["mesco_notify1"] = parties["mesco_notify1"]
    if parties.get("mesco_notifyaddress"):
        data["mesco_notifyaddress"] = parties["mesco_notifyaddress"]
    if parties.get("delivery_agent") and not data.get("delivery_agent"):
        data["delivery_agent"] = parties["delivery_agent"]
        data["mesco_notify2"] = parties["delivery_agent"]
    if parties.get("delivery_agent_address"):
        data["delivery_agent_address"] = parties["delivery_agent_address"]
        agent_note = (
            f"Delivery Agent at Destination: {parties['delivery_agent_address']}"
        )
        data["mesco_notes"] = _merge_notes(data.get("mesco_notes"), [agent_note])

    routing = extract_routing_notes(raw_text)
    if routing:
        data["mesco_routenotes"] = routing

    pod = extract_place_of_delivery(raw_text)
    delivery = extract_delivery_address(raw_text)
    dest_hint = data.get("mesco_destination") or pod
    if pod:
        data["mesco_deliveryaddress"] = normalize_delivery_address_for_crm(
            pod, destination=dest_hint,
        )
    elif delivery:
        data["mesco_deliveryaddress"] = normalize_delivery_address_for_crm(
            delivery, destination=dest_hint,
        )
    elif data.get("mesco_deliveryaddress"):
        data["mesco_deliveryaddress"] = normalize_delivery_address_for_crm(
            data.get("mesco_deliveryaddress"),
            destination=dest_hint,
        )
    elif re.search(r"ISTANBUL\s*,\s*TURKEY", str(data.get("mesco_deliveryaddress") or ""), re.I):
        if pod:
            data["mesco_deliveryaddress"] = normalize_delivery_address_for_crm(
                pod, destination=dest_hint,
            )

    from pdf_groupage_cargo import extract_groupage_cargo_lines

    cargo_lines = extract_groupage_cargo_lines(raw_text)
    if cargo_lines:
        data["cargo_lines"] = cargo_lines
        data["mesco_consolidation"] = True

    org_bls = extract_no_of_org_bls(raw_text)
    if org_bls is not None:
        data["mesco_nooforgbls"] = org_bls

    vessel, voyage, pol = extract_express_vessel_voyage(raw_text)
    if vessel:
        data["mesco_vessel"] = vessel
    if voyage:
        data["mesco_voytruckno"] = voyage
    if pol and not data.get("mesco_origin"):
        data["mesco_origin"] = pol

    if re.search(r"EXPRESS\s+BILL\s+OF\s+LADING", raw_text, re.I):
        data["mesco_telexrelease"] = True

    service = extract_service_type(raw_text) or extract_express_service_mode(page1)
    if service:
        data["mesco_servicetype_text"] = service

    legal_notes = extract_bl_legal_notes(raw_text)
    if legal_notes:
        existing_notes = data.get("mesco_notes")
        if _is_service_mode_notes_only(existing_notes):
            data["mesco_notes"] = legal_notes
        elif existing_notes and legal_notes.upper() not in existing_notes.upper():
            data["mesco_notes"] = f"{existing_notes}\n{legal_notes}"[:500]
        else:
            data["mesco_notes"] = legal_notes

    notes_parts: List[str] = []

    form_no = extract_form_number(raw_text)
    if form_no:
        data["mesco_certificatenumber"] = form_no

    if not data.get("mesco_shippingline"):
        carrier = extract_carrier_name(raw_text)
        if carrier:
            data["mesco_shippingline"] = carrier

    teus = extract_teus(raw_text)
    if teus and not data.get("cr401_totalteus"):
        data["cr401_totalteus"] = teus

    if page1_refs:
        data["_cargo_page1_references"] = page1_refs
        notes_parts.append(
            "Cargo references on B/L face (not attachment-list houses): "
            + ", ".join(page1_refs)
        )

    if attached:
        data["_attached_list_house_refs"] = attached
        notes_parts.append(
            f"Attachment-list house B/L count: {len(attached)}"
        )

    if notes_parts:
        merged_notes = _merge_notes(data.get("mesco_notes"), notes_parts)
        if merged_notes:
            data["mesco_notes"] = merged_notes

    eq = data.setdefault("extraction_quality", {})
    if isinstance(eq, dict):
        eq["page1_cargo_reference_count"] = len(page1_refs)
        eq["attached_list_house_count"] = len(attached)

    return data
