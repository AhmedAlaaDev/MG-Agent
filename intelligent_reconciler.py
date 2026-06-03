"""
Per-field reconciliation between Azure OpenAI and deterministic parsers.

The unified intelligent pipeline runs **both** Azure and the deterministic
PDF-specific parsers, then merges the two on a field-by-field basis using
explicit precedence rules.  This avoids the failure modes we have hit in
real PDFs:

* Azure mis-identifies the consignee as a form-header (``EXPORT REFERENCES``)
  while the deterministic parser correctly finds MESCO.
* Azure picks the master B/L's ACID for a house row inside a manifest while
  the deterministic per-row parser has the right one.
* Azure emits ``297923900`` (Egyptian Freight Forwarder Tax ID) inside the
  HS-code list while the deterministic parser correctly filters it.
* Conversely, Azure is best for multi-page goods descriptions across
  "Continued on Next Sheet" pages while the deterministic per-page parser
  only sees the first page.

The reconciler tracks the source of every chosen value in ``_field_sources``
on the returned record so downstream logging / UI can show provenance.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Field precedence
# ---------------------------------------------------------------------------

#: Fields where the deterministic per-PDF parser is authoritative when set.
#: Azure only fills the gap if deterministic returned null.
_PREFER_DETERMINISTIC: Set[str] = {
    # Identifiers visible verbatim on the page — Azure sometimes paraphrases.
    "mesco_masterblno",
    "mesco_houseblno",
    "mesco_bookingnumber",
    "mesco_acidnumber",
    "container_number",
    "seal_number",
    # Egypt-specific quirks that need strict parsing rules.
    "mesco_importerstaxno",
    "mesco_foreignsupplierregistrationnumber",
    # Numbers that need to match the page totals exactly.
    "cr401_totalpackages",
    "cr401_totalgrossweight",
    "cr401_totalvolume",
    # Picklist-driven enums (Azure occasionally invents new strings).
    "mesco_pcfreightterm",
    "mesco_transporttype",
    "mesco_loadtype",
    "mesco_direction",
    "mesco_bltype",
}

#: Fields where Azure tends to win — multi-page narrative content the
#: deterministic parser cannot reliably stitch.
_PREFER_AZURE: Set[str] = {
    "mesco_dateofissue",
    "mesco_shippedonboarddate",
    "mesco_placeofissue",
    "mesco_etdorigin",
    "mesco_etadestination",
    "mesco_incoterm",
    "mesco_servicetype",
}

#: Fields that should be UNIONED (token-set merge) instead of picking one.
_UNION_FIELDS: Set[str] = {
    "mesco_hscode",
}

#: Free-text fields where we prefer the **longer** non-empty value
#: (after dedup) — typically the multi-page cargo description.
_PREFER_LONGER: Set[str] = {
    "mesco_cargodescription",
    "mesco_handlinginformation",
    "mesco_notes",
    "mesco_routenotes",
    "mesco_shipperaddress",
    "mesco_consigneeaddress",
}

#: Party-name fields where short, clean strings beat noisy multi-line OCR.
_PREFER_CLEANER_NAME: Set[str] = {
    "mesco_shippernamecontactno",
    "mesco_consigneenamecontactno",
    "mesco_notify1",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_blank(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return not v.strip()
    if isinstance(v, (list, dict)):
        return len(v) == 0
    return False


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).upper()


def _tokenize_codes(value: str) -> List[str]:
    """Split an HS-code string into a deduplicated, ordered list of codes."""
    if not value:
        return []
    raw = re.split(r"[|,\s/;]+", value)
    out: List[str] = []
    seen: Set[str] = set()
    for tok in raw:
        tok = tok.strip()
        if not tok:
            continue
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def _union_codes(*values: Optional[str]) -> Optional[str]:
    combined: List[str] = []
    seen: Set[str] = set()
    for v in values:
        for tok in _tokenize_codes(v or ""):
            if tok in seen:
                continue
            seen.add(tok)
            combined.append(tok)
    return "|".join(combined) if combined else None


_NAME_NOISE_RE = re.compile(
    r"(?:HS\s*CODE|HS:|EXPORTER\s+ID|TAX\s+ID|ACID|CONTAINER|SEAL|PACKAGE|PALLETS|"
    r"CARTON|ROLLS|KGS|CBM|VESSEL|VOYAGE|MARKS\s*&\s*NOS|MARKS\s+AND\s+NOS|"
    r"FREIGHT|PORT\s+OF|PLACE\s+OF|BILL\s+OF\s+LADING|REINFORCEMENT)",
    re.I,
)

# Phrases that are form-printed boilerplate, NEVER an actual party name.
# Hitting any of these disqualifies the candidate outright (huge negative score).
_NAME_FORM_HEADER_RE = re.compile(
    r"^\s*(?:"
    r"EXPORT\s+REFERENCES?"
    r"|FORWARDING\s+AGENT\s+REFERENCES?"
    r"|FOR\s+DELIVERY\s+PLEASE\s+APPLY\s+TO"
    r"|FOR\s+DELIVERY\s+OF\s+GOODS"
    r"|FOR\s+RELEASE\s+OF\s+GOODS"
    r"|ALSO\s+NOTIFY(?:\s+PARTY)?"
    r"|TO\s+ORDER(?:\s+OF)?"
    r"|TO\s+THE\s+ORDER\s+OF"
    r"|NOTIFY\s+PARTY"
    r"|SAME\s+AS\s+CONSIGNEE"
    r"|SAME\s+AS\s+CNEE"
    r"|CONSIGNEE\s*[:\-]?\s*$"
    r"|SHIPPER\s*[:\-]?\s*$"
    r"|N\s*/\s*A"
    r")\s*$",
    re.I,
)


def _name_quality(value: str) -> Tuple[int, int]:
    """
    Score a party name. Higher = better.

    * Form-header boilerplate ("EXPORT REFERENCES", "TO ORDER", etc.) → -inf.
    * Penalise strings containing telltale noise (HS CODE, EXPORTER ID, etc.).
    * Prefer reasonably-sized clean strings — typically a company name.
    * Penalise excessive length (>200 chars usually means cargo bleed-over).
    """
    if not value:
        return (-1_000_000, 0)
    text = value.strip()
    if _NAME_FORM_HEADER_RE.match(text):
        return (-1_000_000, 0)
    noise_hits = len(_NAME_NOISE_RE.findall(text))
    length = len(text)
    over_long_pen = max(0, length - 200)
    score = -noise_hits * 50 - over_long_pen
    if 3 <= length <= 200:
        score += 25
    return (score, length)


def _pick_cleaner_name(*candidates: Optional[str]) -> Optional[str]:
    best: Optional[str] = None
    best_score: Tuple[int, int] = (-(1 << 30), 0)
    for cand in candidates:
        if _is_blank(cand):
            continue
        score = _name_quality(cand or "")
        if score > best_score:
            best_score = score
            best = cand
    return best


def _pick_longer(*candidates: Optional[str]) -> Optional[str]:
    best: Optional[str] = None
    for cand in candidates:
        if _is_blank(cand):
            continue
        if best is None or len(cand or "") > len(best):
            best = cand
    return best


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def reconcile_record(
    *,
    deterministic: Optional[Dict[str, Any]],
    azure: Optional[Dict[str, Any]],
    fallback: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Merge a single B/L record from up to three sources, ordered by trust:
    ``deterministic`` (per-PDF parser) > ``azure`` > ``fallback`` (regex).

    Returns a new dict with ``_field_sources`` describing where each value
    came from. Existing record-internal keys (``_page_text``, etc.) on the
    deterministic input are preserved.
    """
    deterministic = deterministic or {}
    azure = azure or {}
    fallback = fallback or {}

    all_keys: Set[str] = set()
    for src in (deterministic, azure, fallback):
        all_keys.update(src.keys())

    merged: Dict[str, Any] = {}
    sources: Dict[str, str] = {}

    for key in sorted(all_keys):
        det_v = deterministic.get(key)
        az_v = azure.get(key)
        fb_v = fallback.get(key)

        chosen: Any = None
        source = "none"

        if key in _UNION_FIELDS:
            chosen = _union_codes(det_v, az_v, fb_v)
            if chosen:
                non_blank: List[str] = []
                if not _is_blank(det_v):
                    non_blank.append("deterministic")
                if not _is_blank(az_v):
                    non_blank.append("azure")
                if not _is_blank(fb_v):
                    non_blank.append("fallback")
                source = "+".join(non_blank) or "none"

        elif key in _PREFER_LONGER:
            chosen = _pick_longer(az_v, det_v, fb_v)
            if chosen is az_v:
                source = "azure"
            elif chosen is det_v:
                source = "deterministic"
            elif chosen is fb_v:
                source = "fallback"

        elif key in _PREFER_CLEANER_NAME:
            chosen = _pick_cleaner_name(det_v, az_v, fb_v)
            if chosen is det_v:
                source = "deterministic"
            elif chosen is az_v:
                source = "azure"
            elif chosen is fb_v:
                source = "fallback"

        elif key in _PREFER_AZURE:
            if not _is_blank(az_v):
                chosen, source = az_v, "azure"
            elif not _is_blank(det_v):
                chosen, source = det_v, "deterministic"
            elif not _is_blank(fb_v):
                chosen, source = fb_v, "fallback"

        elif key in _PREFER_DETERMINISTIC:
            if not _is_blank(det_v):
                chosen, source = det_v, "deterministic"
            elif not _is_blank(az_v):
                chosen, source = az_v, "azure"
            elif not _is_blank(fb_v):
                chosen, source = fb_v, "fallback"

        else:
            # Default precedence: deterministic > azure > fallback, first non-blank wins.
            if not _is_blank(det_v):
                chosen, source = det_v, "deterministic"
            elif not _is_blank(az_v):
                chosen, source = az_v, "azure"
            elif not _is_blank(fb_v):
                chosen, source = fb_v, "fallback"

        if not _is_blank(chosen):
            merged[key] = chosen
            sources[key] = source

    merged["_field_sources"] = sources
    return merged


def reconcile_records(
    *,
    deterministic_records: Optional[List[Dict[str, Any]]],
    azure_records: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """
    Pair records across sources by ``mesco_masterblno`` / ``mesco_houseblno``
    and reconcile each pair. Records present only in one source pass through
    unchanged (with ``_field_sources`` set accordingly).
    """
    det_list = list(deterministic_records or [])
    az_list = list(azure_records or [])

    def _bl_key(rec: Dict[str, Any]) -> str:
        return _norm(
            str(rec.get("mesco_houseblno") or rec.get("mesco_masterblno") or "")
        )

    az_by_key: Dict[str, Dict[str, Any]] = {}
    for rec in az_list:
        k = _bl_key(rec)
        if k and k not in az_by_key:
            az_by_key[k] = rec

    used_az: Set[str] = set()
    out: List[Dict[str, Any]] = []

    for det in det_list:
        k = _bl_key(det)
        az = az_by_key.get(k) if k else None
        if az is not None:
            used_az.add(k)
        merged = reconcile_record(deterministic=det, azure=az)
        out.append(merged)

    # Anything Azure found that deterministic missed is appended as-is.
    for k, az in az_by_key.items():
        if k in used_az:
            continue
        merged = reconcile_record(deterministic=None, azure=az)
        out.append(merged)

    return out
