"""Parse house B/L references from PDF attached-list pages."""

from __future__ import annotations

import re
from typing import List

_ATTACHED_LIST_REF_RE = re.compile(
    r"\b(AMIGL\d{8,12}[A-Z]?|WCSE\d{6,14}[A-Z]?)\b",
    re.I,
)


def extract_attached_list_house_refs(text: str) -> List[str]:
    """
    Return house/reference numbers from attachment pages (not the main B/L body).

    Evergreen-style B/Ls put sub-B/L refs on page 2+ under ATTACHMENT / ATTACHED LIST.
    """
    if not text or not text.strip():
        return []

    pages = re.split(r"---\s*PAGE\s*\d+\s*---", text, flags=re.I)
    sections: List[str] = []
    for page in pages[1:] if len(pages) > 1 else pages:
        # Require the dedicated attachment page header (not "see attached list" on page 1).
        if re.search(r"\bATTACHMENT\b|\bATTACHED\s+LIST\s+PAGE\b", page, re.I):
            sections.append(page)

    if not sections:
        return []

    refs: List[str] = []
    seen: set[str] = set()
    for section in sections:
        for match in _ATTACHED_LIST_REF_RE.finditer(section):
            ref = match.group(1).upper()
            if ref not in seen:
                seen.add(ref)
                refs.append(ref)
    return refs


def build_house_records_from_attached_list(
    master_rec: dict,
    refs: List[str],
) -> List[dict]:
    """Build minimal house-shaped records linked to a master extraction."""
    inherit_keys = (
        "mesco_shippernamecontactno",
        "mesco_shipperaddress",
        "mesco_consigneenamecontactno",
        "mesco_consigneeaddress",
        "mesco_notify1",
        "mesco_notifyaddress",
        "mesco_vessel",
        "mesco_voytruckno",
        "mesco_origin",
        "mesco_destination",
        "mesco_transhipmentport",
        "mesco_transporttype",
        "mesco_loadtype",
        "mesco_direction",
        "mesco_pcfreightterm",
        "mesco_acidnumber",
        "mesco_importerstaxno",
        "mesco_foreignsupplierregistrationnumber",
        "mesco_handlinginformation",
        "mesco_freightpayableat",
        "mesco_telexrelease",
    )
    master_mbl = master_rec.get("mesco_masterblno")
    records: List[dict] = []
    for ref in refs:
        rec = {
            k: master_rec[k]
            for k in inherit_keys
            if master_rec.get(k) not in (None, "", [], {})
        }
        rec["mesco_houseblno"] = ref
        rec["mesco_masterblno"] = master_mbl
        rec["_attached_list_house"] = True
        records.append(rec)
    return records
