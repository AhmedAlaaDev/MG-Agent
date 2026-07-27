"""Deterministic IMO / dangerous-goods extraction from MSDS and DG text."""

from __future__ import annotations

import re
from typing import Any, Dict, Optional


_TRANSPORT_MARKER_RE = re.compile(
    r"\b(?:SECTION\s*14|TRANSPORT\s+INFORMATION|DANGEROUS\s+GOODS|"
    r"MULTIMODAL\s+DANGEROUS\s+GOODS|IMDG|IMO\s+CLASS|"
    r"TRANSPORT\s+HAZARD\s+CLASS|UN\s+(?:NO|NUMBER))\b",
    re.I,
)

_CLASS_VALUE_RE = r"([1-9](?:\.\d)?)"


def _has(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _clean_un_digits(value: Any) -> Optional[str]:
    if not _has(value):
        return None
    m = re.search(r"\b(?:UN\s*)?(\d{4})\b", str(value), re.I)
    return m.group(1) if m else None


def _normalize_un_number(value: Any) -> Optional[str]:
    digits = _clean_un_digits(value)
    return f"UN{digits}" if digits else None


def _normalize_class(value: Any) -> Optional[str]:
    if not _has(value):
        return None
    m = re.search(_CLASS_VALUE_RE, str(value))
    return m.group(1) if m else None


def _normalize_flash_point(value: Any) -> Optional[str]:
    if not _has(value):
        return None
    text = str(value).strip()
    if re.search(r"\b(?:N/?A|NOT\s+AVAILABLE|NONE|/)\b", text, re.I):
        return None
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:deg\.?\s*)?(?:[CF]|°\s*[CF])?", text, re.I)
    return m.group(1) if m else None


def _extract_flash_point(text: str) -> Optional[str]:
    """Extract a flash point without drifting into the next form field."""
    for match in re.finditer(
        r"\bFLASH\s*POINT(?:\s*\([^)]*\))?(?:\s*[:#\-]|\s+(?=-?\d|POINT\b))",
        text,
        re.I,
    ):
        tail = text[match.end() : match.end() + 120]
        tail = re.sub(r"^(?:\s*POINT\s*[:#\-]?)+", "", tail, flags=re.I).lstrip()
        tail = re.split(
            r"\b(?:BOILING\s+POINT|QUANTITY|SEGREGATION|LIMITED\s+QUANTITY|"
            r"CONTAINER\s+NUMBER|CONTAINER\s+SIZE|SECTION\s+\d+)\b",
            tail,
            maxsplit=1,
            flags=re.I,
        )[0]
        value = _normalize_flash_point(tail[:40])
        if value is not None:
            return value
    return None


def extract_imo_fields(text: str) -> Dict[str, Any]:
    """Return normalized IMO fields found in MSDS/DGD transport text.

    Operation payloads use ``mesco_unnumber`` (for example ``UN3077``), while
    cargo payloads use numeric ``mesco_unno``.  The returned dict includes both
    so the formatter can map each entity correctly.
    """
    if not text:
        return {}

    normalized = re.sub(r"\s+", " ", text)
    out: Dict[str, Any] = {}

    # UN numbers are the strongest signal that the cargo is IMO/DG.
    for pat in (
        r"\bUN\s*(?:NO\.?|NUMBER)?\s*[:#\-]?\s*(?:NUMBER\s*[:#\-]?\s*)?(?:UN\s*)?(\d{4})\b",
        r"\bUN(\d{4})\b",
    ):
        m = re.search(pat, normalized, re.I)
        if m:
            out["mesco_unnumber"] = f"UN{m.group(1)}"
            out["mesco_unno"] = m.group(1)
            break

    # Prefer explicit IMO / transport labels over general GHS category text.
    for pat in (
        rf"\bIMO\s+CLASS\s*[:#\-]?\s*{_CLASS_VALUE_RE}\b",
        rf"\bTRANSPORT\s+HAZARD\s+CLASS(?:\(ES\))?\s*[:#\-]?\s*{_CLASS_VALUE_RE}\b",
        rf"\bHAZARD\s+CLASS(?:\(ES\))?\s*[:#\-]?\s*{_CLASS_VALUE_RE}\b",
        rf"\bDANGEROUS\s+GOODS\s*[:#\-]?\s*CLASS\s*{_CLASS_VALUE_RE}\b",
        rf"\bCLASS\s*{_CLASS_VALUE_RE}\s+DANGEROUS\s+(?:ARTICLE|GOODS)\b",
        rf"\bBELONGS\s+TO\s+CLASS\s*{_CLASS_VALUE_RE}\s+DANGEROUS\b",
    ):
        m = re.search(pat, normalized, re.I)
        if m:
            out["mesco_imoclass"] = m.group(1)
            break

    flash = _extract_flash_point(normalized)
    if flash is not None:
        out["mesco_flashptc"] = flash

    if re.search(r"\bMARINE\s+POLLUTANT\b.*?\b(?:Y|YES)\b", normalized, re.I):
        out["mesco_marinepollutant"] = True

    has_transport_context = bool(_TRANSPORT_MARKER_RE.search(normalized))
    if out.get("mesco_unnumber") or (out.get("mesco_imoclass") and has_transport_context):
        out["_imo_detected"] = True

    return out


def merge_imo_fields(target: Dict[str, Any], source: Dict[str, Any], text: str = "") -> Dict[str, Any]:
    """Merge explicit source values and text-derived IMO facts into ``target``."""
    facts = extract_imo_fields(text)

    explicit_class = _normalize_class(source.get("mesco_imoclass"))
    if explicit_class:
        facts["mesco_imoclass"] = explicit_class

    explicit_un = _normalize_un_number(source.get("mesco_unnumber") or source.get("mesco_unno"))
    if explicit_un:
        facts["mesco_unnumber"] = explicit_un
        facts["mesco_unno"] = _clean_un_digits(explicit_un)

    explicit_flash = _normalize_flash_point(source.get("mesco_flashptc"))
    if explicit_flash:
        facts["mesco_flashptc"] = explicit_flash

    if source.get("mesco_imo") is True:
        facts["_imo_detected"] = True

    if facts.get("mesco_unnumber") or facts.get("mesco_imoclass"):
        facts["_imo_detected"] = True

    for key, value in facts.items():
        if _has(value) and not _has(target.get(key)):
            target[key] = value
    return target
