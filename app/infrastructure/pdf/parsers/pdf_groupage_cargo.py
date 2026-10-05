"""
Parse per-shipper cargo lines from groupage / consolidation master B/L PDFs.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


def clean_ocr_value(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    text = re.sub(r"\\+'?$", "", text)
    text = re.sub(r"^['\"]+|['\"]+$", "", text)
    return text or None


def normalize_european_number(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = re.sub(r"[^0-9.,]", "", str(value).upper().replace("COPY", ""))
    if not text:
        return None
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        num = float(text)
        if num == int(num):
            return str(int(num))
        return f"{num:.2f}".rstrip("0").rstrip(".")
    except ValueError:
        return None


def _is_groupage_bl(text: str) -> bool:
    upper = (text or "").upper()
    return "GROUPAGE MASTER" in upper or (
        "SHIPPER:" in upper and "DESC.OF GOODS" in upper
    )


def _parse_body_fields(body: str) -> Dict[str, Any]:
    raw: Dict[str, Any] = {}
    desc_m = re.search(
        r"DESC\.?\s*OF\s*GOODS:\s*(.*?)(?=\nHS\s*CODE:|\nParticulars|\nFreight and|\Z)",
        body,
        re.I | re.S,
    )
    if desc_m:
        raw["description"] = re.sub(r"\s+", " ", desc_m.group(1)).strip().strip('"')[:500]

    field_patterns = (
        (r"hs_code", r"HS\s*CODE:\s*([^\n]+)"),
        (r"packages", r"PACKAGES:\s*([^\n]+)"),
        (r"gross_weight", r"GROSS\s*WEIGHT:\s*([^\n]+)"),
        (r"acid_no", r"ACID\s*NO:?\s*([^\n]+)"),
        (r"shipper_registration_id", r"SHIPPER\s*REGISTRATION\s*ID:\s*([^\n]+)"),
        (r"importer_tax_id", r"EGYPTIAN\s*IMPORTER\s*TAX\s*ID\s*:?\s*([^\n]+)"),
    )
    for key, pat in field_patterns:
        m = re.search(pat, body, re.I)
        if m:
            raw[key] = m.group(1).strip()
    return raw


def _merge_raw(into: Dict[str, Any], from_: Dict[str, Any]) -> None:
    for key, val in from_.items():
        if val and not into.get(key):
            into[key] = val


def _finalize_entry(raw: Dict[str, Any]) -> Dict[str, Any]:
    shipper = clean_ocr_value(raw.get("shipper")) or ""
    desc = clean_ocr_value(raw.get("description")) or ""
    hs = clean_ocr_value(raw.get("hs_code")) or ""
    acid = clean_ocr_value(raw.get("acid_no")) or ""
    reg = clean_ocr_value(raw.get("shipper_registration_id")) or ""
    importer = clean_ocr_value(raw.get("importer_tax_id")) or ""
    packages = normalize_european_number(raw.get("packages"))
    gross = normalize_european_number(raw.get("gross_weight"))

    desc_lines = [f"SHIPPER: {shipper}"]
    if desc:
        desc_lines.append(desc)
    if hs:
        desc_lines.append(f"HS CODE: {hs}")
    if acid:
        desc_lines.append(f"ACID NO: {acid}")
    if reg:
        desc_lines.append(f"SHIPPER REGISTRATION ID: {reg}")
    if importer:
        desc_lines.append(f"EGYPTIAN IMPORTER TAX ID: {importer}")

    return {
        "shipper": shipper,
        "description": desc,
        "hs_code": hs,
        "packages": packages,
        "gross_weight_kg": gross,
        "acid_no": acid,
        "shipper_registration_id": reg,
        "importer_tax_id": importer,
        "mesco_descriptionofgoods": "\n".join(desc_lines),
        "mesco_noofpackages": packages,
        "mesco_grosskg": gross,
    }


def _last_entry_needing(
    entries: List[Dict[str, Any]], *field_keys: str
) -> Optional[Dict[str, Any]]:
    for key in field_keys:
        for entry in reversed(entries):
            if not entry.get(key):
                return entry
    return None


def _merge_by_shipper(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    order: List[str] = []
    by_name: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        name = (entry.get("shipper") or "").upper()
        if not name:
            continue
        if name not in by_name:
            by_name[name] = dict(entry)
            order.append(name)
        else:
            _merge_raw(by_name[name], entry)
    return [by_name[n] for n in order]


def extract_groupage_cargo_lines(text: str) -> List[Dict[str, Any]]:
    if not _is_groupage_bl(text):
        return []

    start_m = re.search(r"GROUPAGE MASTER|SAID TO CONTAIN\s*:", text, re.I)
    end_m = re.search(r"\nTOTAL TARE WEIGHT|\nDISCHARGE TERM CLAUSE", text, re.I)
    if not start_m:
        return []
    cargo_text = text[start_m.start() : end_m.start() if end_m else len(text)]

    entries: List[Dict[str, Any]] = []
    seen_shippers: set[str] = set()
    parts = re.split(r"(?=^SHIPPER:\s*)", cargo_text, flags=re.I | re.M)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        m = re.match(r"SHIPPER:\s*([^\n]+)\s*\n(.*)", part, re.I | re.S)
        if not m:
            continue
        shipper = m.group(1).strip()
        shipper_key = shipper.upper()
        if shipper_key in seen_shippers:
            continue
        seen_shippers.add(shipper_key)
        body = m.group(2)
        body = re.split(
            r"\n(?:Particulars above|Freight and charges|FREIGHT PREPAID)\b",
            body,
            maxsplit=1,
            flags=re.I,
        )[0]
        raw = _parse_body_fields(body)
        raw["shipper"] = shipper
        if raw.get("shipper"):
            entries.append(raw)

    for m in re.finditer(
        r"(?:Marks and Nos|Container / Seal).*?"
        r"((?:HS\s*CODE:[^\n]+\n)"
        r"(?:(?:PACKAGES|GROSS\s*WEIGHT|ACID\s*NO|SHIPPER\s*REGISTRATION|EGYPTIAN\s*IMPORTER)[^\n]+\n)+)"
        r"(?=SHIPPER:)",
        cargo_text,
        re.I | re.S,
    ):
        prefix = cargo_text[max(0, m.start() - 500) : m.start()].upper()
        if "PAGE" not in prefix and "ATTACHMENT" not in prefix:
            continue
        cont = _parse_body_fields(m.group(1))
        target = _last_entry_needing(entries, "hs_code")
        if cont and target:
            _merge_raw(target, cont)

    for m in re.finditer(
        r"((?:GROSS\s*WEIGHT:[^\n]+\n)"
        r"(?:ACID\s*NO:[^\n]+\n)?"
        r"(?:(?:SHIPPER\s*REGISTRATION|EGYPTIAN\s*IMPORTER)[^\n]+\n)*)"
        r"(?=SHIPPER:)",
        cargo_text,
        re.I,
    ):
        block_upper = m.group(1).upper()
        if any(k in block_upper for k in ("PACKAGES:", "HS CODE:", "DESC.OF")):
            continue
        prefix = cargo_text[max(0, m.start() - 400) : m.start()].upper()
        if "PAGE 3" not in prefix and "PAGE 3 OF 3" not in prefix:
            continue
        cont = _parse_body_fields(m.group(1))
        target = _last_entry_needing(entries, "gross_weight")
        if cont and target:
            _merge_raw(target, cont)

    merged = _merge_by_shipper(entries)
    out = [_finalize_entry(e) for e in merged if e.get("shipper")]
    return [e for e in out if e.get("hs_code") or e.get("packages")]
