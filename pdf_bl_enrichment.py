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


def _visual_page_text(raw_text: str, page_index: int = 1) -> str:
    """Prefer [VISUAL WORD ORDER] section (reading order) over block-order OCR."""
    pages = re.split(r"---\s*PAGE\s*\d+\s*---", raw_text or "", flags=re.I)
    if page_index < 1 or page_index >= len(pages):
        return pages[1] if len(pages) > 1 else (raw_text or "")
    body = pages[page_index]
    m = re.search(r"\[VISUAL WORD ORDER\](.*?)(?=\[BLOCK ORDER\]|$)", body, re.S | re.I)
    return m.group(1) if m else body


def _cargo_pages_text(raw_text: str) -> str:
    """All pages that contain the goods / HS description table."""
    pages = re.split(r"---\s*PAGE\s*\d+\s*---", raw_text or "", flags=re.I)
    parts: List[str] = []
    for body in pages[1:]:
        if re.search(
            r"DESCRIPTION OF PACKAGES|SAID TO CONTAIN|HS\s*CODE|Continued on Next Sheet|Continued From Previous",
            body,
            re.I,
        ):
            parts.append(body)
    return "\n".join(parts) if parts else _page1_text(raw_text)


_CARGO_LINE_STOP_RE = re.compile(
    r"^(CONSOLIDATED CARGO|ACID\s*:|EGYPTIAN FREIGHT|FOREIGN FREIGHT|"
    r"Continued on Next Sheet|Continued From Previous|ABOVE PARTICULARS|"
    r"SAY ONE HUNDRED|Shipped on Board|Weight in Kgs Total|SIGNED FOR THE CARRIER)",
    re.I,
)
_CARGO_LINE_SKIP_RE = re.compile(
    r"^(MARKS AND|CONTAINER AND|NO AND KIND|DESCRIPTION OF PACKAGES|"
    r"GROSS WEIGHT|TARE|MEASUREMENT|SHIPPER'?S LOAD|SAID TO CONTAIN|"
    r"VOYAGE|BILL OF LADING|PRE CARRIAGE|VESSEL|PORT OF LOADING|"
    r"PORT OF DISCHARGE|FINAL PLACE|COPY NON|BILL OF LADING NUMBER|"
    r"PLACE OF RECEIPT|FREIGHT TO BE|NUMBER OF ORIGINAL|KGS\s*$|CBM\s*$|"
    r"\*{3,}|SHENZHEN THREE)",
    re.I,
)


def _first_group(pattern: str, text: str, flags: int = re.I | re.S) -> Optional[str]:
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else None


def extract_hs_codes_from_goods(text: str) -> Optional[str]:
    """HS codes from all cargo pages (page 1 + continuation sheets)."""
    codes: List[str] = []
    cargo_text = _cargo_pages_text(text)

    def add_code(code: str) -> None:
        code = code.strip()
        if not code or code in codes:
            return
        if len(code) >= 19:
            return
        if len(code) == 9 and re.search(
            rf"(?:TAX\s*ID|FORWARDER\s*TAX)[^\n]*\b{re.escape(code)}\b",
            cargo_text,
            re.I,
        ):
            return
        codes.append(code)

    for m in re.finditer(
        r"H\.?S\.?\s*CODE\s*:?\s*([0-9]{6,12})",
        cargo_text,
        re.I,
    ):
        add_code(m.group(1))

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
                "VAT:",
                "TEL:",
                "FAX:",
                "BILL OF LADING",
                "BOOKING NO",
                "EQUIPMENT SERVICES",
                "EXPORT REFERENCES",
            )
        ):
            continue
        for m in re.finditer(r"\b(\d{6,12})\b", line):
            if re.search(r"HS\s*CODE|H\.?S\.?\s*CODE", line, re.I):
                add_code(m.group(1))
                continue
            if len(m.group(1)) >= 8:
                add_code(m.group(1))
    return ", ".join(codes) if codes else None


def extract_cargo_description_from_pdf(text: str) -> Optional[str]:
    """Goods lines from all cargo pages (including 'Continued on Next Sheet')."""
    lines_out: List[str] = []
    seen: set[str] = set()

    def add_line(raw: str) -> None:
        s = re.sub(r"\s+", " ", (raw or "")).strip()
        if not s or len(s) < 3:
            return
        key = re.sub(r"[^A-Z0-9]+", "", s.upper())[:80]
        if key in seen:
            return
        seen.add(key)
        lines_out.append(s)

    pages = re.split(r"---\s*PAGE\s*\d+\s*---", text or "", flags=re.I)
    for idx in range(1, len(pages)):
        body = _visual_page_text(text, idx) if idx < len(pages) else pages[idx]
        start = 0
        m = re.search(
            r"(?:SEAL\s+[A-Z0-9]+\s*\n|SAID TO CONTAIN CARGO\s*\n|N/M\s+)",
            body,
            re.I,
        )
        if m:
            start = m.end()
        for ln in body[start:].splitlines():
            s = ln.strip()
            if not s:
                continue
            if _CARGO_LINE_STOP_RE.match(s):
                break
            if _CARGO_LINE_SKIP_RE.match(s):
                continue
            if re.match(r"^(TLLU|MSCU|CMAU|SEGU|OOLU)[A-Z0-9]+$", s, re.I):
                continue
            if re.match(r"^SEAL\s+", s, re.I):
                continue
            if re.match(r"^\d+\s*x\s*\d", s, re.I):
                continue
            if re.match(r"^\d+\s+PACKAGE", s, re.I):
                continue
            if re.fullmatch(r"[\d.,]+", s):
                continue
            add_line(s)

    return "\n".join(lines_out) if lines_out else None


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
    if not pdf_desc:
        return existing
    if not existing:
        return pdf_desc
    merged: List[str] = []
    seen: set[str] = set()
    for chunk in (existing, pdf_desc):
        for ln in re.split(r"[\n;]+", chunk):
            s = ln.strip()
            if not s:
                continue
            key = re.sub(r"[^A-Z0-9]+", "", s.upper())[:80]
            if key in seen:
                continue
            seen.add(key)
            merged.append(s)
    return "\n".join(merged) if merged else existing


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

    for pat in (
        r"Consignee(?:\s+or\s+Order)?\s*\n(TO THE ORDER OF[^\n]+)",
        r"\n(TO THE ORDER OF [A-Z0-9 &./\-']+)\n",
    ):
        m = re.search(pat, page1, re.I)
        if m:
            name = re.sub(r"\s+", " ", m.group(1)).strip()
            if name and "MESCO" not in name.upper():
                return {"name": name[:200], "address": None}

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
            return {"name": name[:200], "address": addr[:250]}

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

    # Manifest house rows already have per-B/L consignee, notify, and packages.
    if data.get("_manifest_pdf_row"):
        return data

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
        data["mesco_consigneeaddress"] = consignee_block["address"]
    elif (consignee_block.get("name") or "").upper().startswith("TO THE HOLDER"):
        data["mesco_consigneeaddress"] = None
    if consignee_block.get("contact"):
        data["mesco_consigneecontactnumber"] = consignee_block["contact"]

    literal_same_as = _uses_literal_same_as_consignee_notify(raw_text)
    if literal_same_as:
        data["mesco_notify1"] = "SAME AS CONSIGNEE"
        if consignee_block.get("address"):
            data["mesco_notifyaddress"] = consignee_block["address"]
    elif re.search(r"SAME\s+AS\s+CONSIGNEE", raw_text, re.I) and consignee_block.get("name"):
        data["mesco_notify1"] = consignee_block["name"]
        if consignee_block.get("address"):
            data["mesco_notifyaddress"] = consignee_block["address"]
    else:
        express_notify = _extract_express_bl_notify_party(page1)
        if express_notify.get("mesco_notify1"):
            data["mesco_notify1"] = express_notify["mesco_notify1"]
        if express_notify.get("mesco_notifyaddress"):
            data["mesco_notifyaddress"] = express_notify["mesco_notifyaddress"]

    mesco_agent = _extract_mesco_delivery_agent(page1)
    if mesco_agent.get("delivery_agent"):
        data["delivery_agent"] = mesco_agent["delivery_agent"]
        data["mesco_notify2"] = mesco_agent["delivery_agent"]
        data["mesco_shippingline"] = mesco_agent["mesco_shippingline"]
        if mesco_agent.get("delivery_agent_address"):
            data["delivery_agent_address"] = mesco_agent["delivery_agent_address"]

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
    if pod:
        data["mesco_deliveryaddress"] = pod
    elif delivery:
        data["mesco_deliveryaddress"] = delivery
    elif re.search(r"ISTANBUL\s*,\s*TURKEY", str(data.get("mesco_deliveryaddress") or ""), re.I):
        if pod:
            data["mesco_deliveryaddress"] = pod

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
