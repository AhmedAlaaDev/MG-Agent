"""Merge and reconcile Azure extractions with deterministic fallbacks."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


def _has(val: Any) -> bool:
    return val is not None and val != "" and val != []


def _bl_key(rec: Dict[str, Any]) -> str:
    bl = rec.get("mesco_masterblno") or rec.get("mesco_houseblno") or ""
    return re.sub(r"\D", "", str(bl).upper())


def dedupe_records_by_bl(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    for rec in records:
        key = _bl_key(rec)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(rec)
    return out


def merge_record_fields(
    primary: Dict[str, Any],
    secondary: Dict[str, Any],
    *,
    prefer_secondary_keys: Optional[set[str]] = None,
) -> Dict[str, Any]:
    """Fill null/empty primary fields from secondary. Primary wins when both set."""
    prefer_secondary_keys = prefer_secondary_keys or set()
    merged = dict(primary)
    skip = {
        "confidence",
        "warnings",
        "extraction_method",
        "extraction_quality",
        "_page_text",
        "_page_number",
        "_routing",
        "_source_info",
        "_error",
    }
    for key, sec_val in secondary.items():
        if key in skip or not _has(sec_val):
            continue
        pri_val = merged.get(key)
        if key in prefer_secondary_keys or not _has(pri_val):
            merged[key] = sec_val
    if not merged.get("containers") and secondary.get("containers"):
        merged["containers"] = secondary["containers"]
    elif merged.get("containers") and secondary.get("containers"):
        pri_c = merged["containers"][0] if merged["containers"] else {}
        sec_c = secondary["containers"][0] if secondary["containers"] else {}
        if isinstance(pri_c, dict) and isinstance(sec_c, dict):
            combined = dict(sec_c)
            combined.update({k: v for k, v in pri_c.items() if _has(v)})
            merged["containers"] = [combined]
    return merged


def reconcile_record_lists(
    azure_records: List[Dict[str, Any]],
    fallback_records: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Combine Azure interpretation with deterministic fallbacks.
    - If Azure missed a B/L that fallback found, add it (merged with any partial Azure match).
    - If Azure has a record, enrich with matching fallback by B/L number.
    """
    if not fallback_records:
        return dedupe_records_by_bl(azure_records)

    if not azure_records:
        return list(fallback_records)

    fallback_by_bl = {_bl_key(r): r for r in fallback_records if _bl_key(r)}
    used_fallback: set[str] = set()
    merged: List[Dict[str, Any]] = []

    for az in azure_records:
        key = _bl_key(az)
        fb = fallback_by_bl.get(key) if key else None
        if fb:
            used_fallback.add(key)
            merged.append(merge_record_fields(az, fb))
        else:
            merged.append(az)

    for fb in fallback_records:
        key = _bl_key(fb)
        if key and key not in used_fallback:
            merged.append(fb)
        elif not key:
            merged.append(fb)

    return dedupe_records_by_bl(merged)
