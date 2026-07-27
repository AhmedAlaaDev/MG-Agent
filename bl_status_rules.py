"""Infer Dynamics mesco_blstatus (Original vs Telex) from B/L text."""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

BL_STATUS_ORIGINAL = 886150000
BL_STATUS_TELEX = 886150001

_TELEX_PATTERNS = (
    r"TELEX\s+RELEASE",
    r"EXPRESS\s+RELEASE",
    r"EXPRESS\s+BILL\s+OF\s+LADING",
    r"EXPRESS\s+B/?L\b",
    r"SEAWAY\s+BILL",
    r"SEA\s+WAY\s+BILL",
    r"NON[- ]NEGOTIABLE\s+SEA\s+WAY\s+BILL",
    r"\bSWB\b",
    r"O/EXPRESS",
    r"DESTINATION\s+O/EXPRESS",
)

_ORIGINAL_PATTERNS = (
    r"PRESENTATION\s+OF\s+ORIGINAL\s+B/?L",
    r"WITHOUT\s+PRESENTATION\s+OF\s+ORIGINAL",
    r"WITHOUT\s+ORIGINAL\s+B/?L",
    r"ORIGINAL\s+B/?L\s+SURRENDER",
    r"\d+\s*\(\s*\d+\s*\)\s*ORIGINAL",
    r"\bTHREE\s*\(\s*3\s*\)\s*ORIGINAL",
)


def _parse_org_bl_count(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    text = str(value).strip().upper()
    if text in ("0", "0/ZERO", "ZERO", "ZERO (0)"):
        return 0
    match = re.search(r"\b(\d+)\b", text)
    if match:
        return int(match.group(1))
    return None


def infer_bl_status(data: Dict[str, Any], raw_text: str) -> Dict[str, Any]:
    """
    Set mesco_blstatus (886150000=Original, 886150001=Telex) and keep
    mesco_telexrelease in sync for Dynamics BL Status toggle.
    """
    if data.get("document_type") == "debit_note":
        return data

    upper = (raw_text or "").upper()
    status = data.get("mesco_blstatus")

    if status in (BL_STATUS_ORIGINAL, BL_STATUS_TELEX):
        data["mesco_telexrelease"] = status == BL_STATUS_TELEX
        return data

    if isinstance(status, str):
        normalized = status.strip().lower()
        if normalized in ("telex", "express", "seaway"):
            data["mesco_blstatus"] = BL_STATUS_TELEX
            data["mesco_telexrelease"] = True
            return data
        if normalized in ("original", "original bl", "original b/l"):
            data["mesco_blstatus"] = BL_STATUS_ORIGINAL
            data["mesco_telexrelease"] = False
            return data

    telex_score = 0
    original_score = 0

    for pattern in _TELEX_PATTERNS:
        if re.search(pattern, upper):
            telex_score += 2

    for pattern in _ORIGINAL_PATTERNS:
        if re.search(pattern, upper):
            original_score += 2

    org_count = _parse_org_bl_count(data.get("mesco_nooforgbls"))
    if org_count == 0:
        telex_score += 3
    elif org_count is not None and org_count > 0:
        original_score += 3

    if data.get("mesco_telexrelease") is True:
        telex_score += 2

    if telex_score > original_score and telex_score >= 2:
        data["mesco_blstatus"] = BL_STATUS_TELEX
        data["mesco_telexrelease"] = True
    elif original_score >= 2:
        data["mesco_blstatus"] = BL_STATUS_ORIGINAL
        data["mesco_telexrelease"] = False

    return data
