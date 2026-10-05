"""
Parse STAR CONCORD / Turkey CARGO MANIFEST PDFs (ISALY house B/Ls under one ISTE master).

Each house appears on a summary line in [VISUAL WORD ORDER]:
  ISALY... Destination: PORT CFS-CFS <pkgs> <type> [ACID: ...] <kgs> <cbm> Freight Collect|Prepaid
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

HBL_RE = re.compile(r"\b(ISALY\d{7,12})\b", re.I)
CARGO_MANIFEST_RE = re.compile(r"CARGO\s+MANIFEST", re.I)

# Summary row in visual layout (excludes bare ISALY lines in block-order sections).
HOUSE_ROW_RE = re.compile(
    r"^ISALY\d+.*?"
    r"CFS-CFS\s+"
    r"\d+\s+"
    r"(?:PACKAGES|ROLLS|PALLETS?|CARTONS?|PALLET)\b",
    re.I | re.M,
)

HEADER_RE = re.compile(
    r"^(?P<hbl>ISALY\d+)\b.*?"
    r"(?:Des\S*n\S*:\s*)?(?P<dest>[A-Z][A-Z\s]+?)\s+"
    r"(?P<service>CFS-CFS)\s+"
    r"(?P<pkg_count>\d+)\s+"
    r"(?P<pkg_type>PACKAGES|ROLLS|PALLETS?|CARTONS?|PALLET)\b"
    r"(?:\s+\*\*\*[^*]+\*\*\*)?"
    r".*?"
    r"(?:ACID:?\s*(?P<acid>\d{19}))?"
    r"\s+(?P<kgs>[\d,]+\.?\d*)\s+(?P<cbm>[\d,]+\.?\d*)\s+"
    r"(?P<freight>Freight\s+(?:Collect|Prepaid))",
    re.I | re.S,
)

TOTAL_RE = re.compile(
    r"Total:\s*(?P<pkgs>\d+)\s+PKGS\s*/\s*(?P<kgs>[\d,]+\.?\d*)\s+KGS\s*/\s*(?P<cbm>[\d,]+\.?\d*)\s+CBM",
    re.I,
)
CONTAINER_RE = re.compile(
    r"Container\s+No\.:\s*(?P<container>[A-Z0-9]+)\s+"
    r"Seal\s+No\.:\s*(?P<seal>\S+)\s+"
    r"Master\s+No\.:\s*(?P<master>\S+)\s+"
    r"Size:\s*(?P<size>[^\n]+)",
    re.I,
)
VESSEL_RE = re.compile(
    r"Vessel:\s*(?P<vessel>.+?)\s+Voyage:\s*(?P<voyage>[^\n]+)",
    re.I,
)
DEST_RE = re.compile(r"Destination:\s*([A-Z][A-Z\s]+)", re.I)
ETD_RE = re.compile(r"E\.T\.D\.\s*([A-Z]+):", re.I)
HS_RE = re.compile(r"HS\s*CODE:\s*([^\n]+)", re.I)
ACID_RE = re.compile(r"ACID\s*:?\s*(\d{19})", re.I)
EGYPT_ACID_LEN = 19
HS_INLINE_RE = re.compile(
    r"(?:^|[^\d])(\d{8,12}(?:\s*[/|,\-]\s*\d{8,12})+)(?:[^\d]|$)",
)
SHIPPER_CUT_RE = re.compile(
    r"\s+(?:MARKS\s*:|IMPORTER\s+ID|EXPORTER\s+ID|HS\s+CODE\s*:|NO\s*:\s*\d).*$",
    re.I,
)
SHIPPER_CARGO_CUT_RE = re.compile(
    r"\s+(?:POCKETING|WAISTBAND|REINFORCEMENT|DENIM|FABRIC|SHRINKABLE|"
    r"CARTON\s+ENVELOPE|COROZO|BUTTON|NONWOVEN|CHEST\s+FILLER|SILICONE|GASKET)\b.*$",
    re.I,
)
GOODS_LINE_RE = re.compile(
    r"(POCKETING|WAISTBAND|REINFORCEMENT|DENIM|FABRIC|SHRINKABLE|POLYPROPYLENE|"
    r"COROZO|BUTTON|CARTON\s+LABEL|CARTON\s+ENVELOPE|ENVELOPE|GASKET|NONWOVEN|"
    r"CHEST\s+FILLER|COTTON|FILM)",
    re.I,
)
PARTY_BLOCK_RE = re.compile(
    r"^(?P<tag>[SCN])/\s*(?P<body>.*?)(?=^(?:[SCN]/|ISALY\d|Date:|\Z))",
    re.I | re.M | re.S,
)
CONTACT_LINE_RE = re.compile(r"^(TEL:|FAX:|VAT\s|LOGISTICS@|E:|MOB:)", re.I)
PARTY_STOP_RE = re.compile(
    r"^(HS\s*CODE|IMPORTER|EXPORTER|ACID|MARKS:|POCKETING|DENIM|SHRINKABLE|POLYPROPYLENE|"
    r"COTTON|SILICONE|WAISTBAND|FABRIC|GASKET|CARTON|ROLLS?\b|HTTP)",
    re.I,
)


def _parse_num(raw: str) -> Optional[float]:
    if not raw:
        return None
    try:
        return float(str(raw).replace(",", "").strip())
    except ValueError:
        return None


def is_tur_cargo_manifest(text: str) -> bool:
    if not text or not CARGO_MANIFEST_RE.search(text):
        return False
    return len({m.group(1).upper() for m in HBL_RE.finditer(text)}) >= 2


def _visual_sections(text: str) -> str:
    pages = re.split(r"---\s*PAGE\s*\d+\s*---", text, flags=re.I)
    parts: List[str] = []
    for body in pages[1:] if len(pages) > 1 else pages:
        m = re.search(r"\[VISUAL WORD ORDER\](.*?)(?=\[BLOCK ORDER\]|$)", body, re.S | re.I)
        if m:
            parts.append(m.group(1))
    return "\n".join(parts) if parts else text


def _party_lines(body: str, max_lines: int = 4) -> List[str]:
    raw: List[str] = []
    for ln in (body or "").split("\n"):
        s = ln.strip()
        if not s:
            if raw:
                break
            continue
        if CONTACT_LINE_RE.match(s) or PARTY_STOP_RE.match(s):
            break
        raw.append(s)
    if not raw:
        return []
    lines = [raw[0]]
    address_hint = re.compile(
        r"\b(ROAD|STREET|ZONE|EGYPT|CAIRO|PORT SAID|RAMADAN|HOLDER|TRAMB|BURSA|ISTANBUL)\b",
        re.I,
    )
    for s in raw[1:]:
        if address_hint.search(s) or re.match(r"^\d", s):
            lines.append(s)
        if len(lines) >= max_lines:
            break
    return lines


def _clean_shipper_name(body: str) -> str:
    """Company name only — OCR often merges MARKS/address/cargo onto the S/ line."""
    raw = (body or "").strip()
    if not raw:
        return ""
    first = raw.split("\n", 1)[0].strip()
    first = SHIPPER_CUT_RE.sub("", first)
    first = SHIPPER_CARGO_CUT_RE.sub("", first)
    first = re.sub(r"\s+\d{3,4}\s+\d{5,8}\s*$", "", first).strip()
    first = re.sub(r"\s+\d{4,6}\s+[\w\s/.-]{5,40}$", "", first).strip()
    return re.sub(r"\s+", " ", first).strip()[:200]


def _format_party(body: str, *, shipper: bool = False) -> str:
    if shipper:
        return _clean_shipper_name(body)
    lines = _party_lines(body)
    if not lines:
        return ""
    name = re.sub(r"\s+", " ", " ".join(lines)).strip()
    if name.upper() == "SAME AS CONSIGNEE":
        return "SAME AS CONSIGNEE"
    return name[:400]


def _block_parties(block: str) -> Tuple[str, str, str]:
    shipper = consignee = notify = ""
    for m in PARTY_BLOCK_RE.finditer(block):
        tag = m.group("tag").upper()
        text = _format_party(m.group("body"), shipper=(tag == "S"))
        if not text:
            continue
        if tag == "S" and not shipper:
            shipper = text
        elif tag == "C" and not consignee:
            consignee = text
        elif tag == "N" and not notify:
            notify = text
    return shipper, consignee, notify


def _normalize_acid(raw: Optional[str]) -> Optional[str]:
    """Egyptian manifest ACIDs are 19 digits; OCR may glue KGS/CBM onto the end."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) < EGYPT_ACID_LEN:
        return None
    if len(digits) > EGYPT_ACID_LEN:
        digits = digits[:EGYPT_ACID_LEN]
    return digits


def _extract_acid_from_block(block: str, summary_acid: Optional[str]) -> Optional[str]:
    """ACID for this house only — summary row or explicit ACID: line in the block."""
    summary_digits = _normalize_acid(summary_acid)
    candidates: List[Tuple[int, str]] = []
    for i, ln in enumerate(block.split("\n")):
        m = ACID_RE.search(ln)
        if not m:
            continue
        digits = _normalize_acid(m.group(1))
        if digits:
            candidates.append((i, digits))

    if not candidates:
        return summary_digits

    if summary_digits and any(d == summary_digits for i, d in candidates if i == 0):
        return summary_digits

    body = [d for i, d in candidates if i > 0]
    if body:
        return body[0]
    return candidates[0][1]


def _extract_hs_from_block(block: str) -> Optional[str]:
    """HS codes present on this house block only (never document-wide)."""
    m = HS_RE.search(block)
    if m:
        return re.sub(r"\s+", " ", m.group(1).strip())

    for ln in block.split("\n"):
        if not GOODS_LINE_RE.search(ln):
            continue
        codes = re.findall(r"\b(\d{8,12})\b", ln)
        if len(codes) >= 2:
            return " / ".join(codes)
        if len(codes) == 1 and re.search(r"BUTTON|COROZO|HS", ln, re.I):
            return codes[0]

    m = HS_INLINE_RE.search(block)
    if m:
        return re.sub(r"\s*[/|,\-]\s*", " / ", m.group(1).strip())
    return None


def _cargo_description(block: str, hs_code: Optional[str] = None) -> str:
    lines: List[str] = []
    seen: set[str] = set()
    for ln in block.split("\n"):
        s = ln.strip()
        if not s:
            continue
        if re.match(r"^(S/|C/|N/|MARKS:|ACID|IMPORTER|EXPORTER|TEL:|FAX:|HTTP)", s, re.I):
            continue
        if re.match(r"^ISALY", s, re.I):
            continue
        if re.search(r"Des\S*n\S*:|CFS-CFS|Freight\s+(Collect|Prepaid)", s, re.I):
            continue
        if re.match(r"^(STI\.|SAN\.|LTD\.?)$", s, re.I):
            continue
        hm = HS_RE.search(s)
        if hm:
            line = f"HS CODE: {hm.group(1).strip()}"
        elif GOODS_LINE_RE.search(s):
            line = re.sub(r"\s+", " ", s)[:200]
        elif len(s) > 12 and not re.match(r"^\d{5,}$", s):
            if re.search(r"\b(ID|OSB|MAH|CAD|BULVAR|SK\.|SITESI)\b", s, re.I):
                line = re.sub(r"\s+", " ", s)[:200]
            else:
                continue
        else:
            continue
        key = line.upper()[:60]
        if key in seen:
            continue
        seen.add(key)
        lines.append(line)
    if hs_code and not any("HS CODE" in x.upper() for x in lines):
        lines.append(f"HS CODE: {hs_code}")
    return "\n".join(lines[:8]).strip()


def _parse_house_row(line: str, block: str) -> Optional[Dict[str, Any]]:
    m = HEADER_RE.match(line.strip())
    if not m:
        return None
    hbl = m.group("hbl").upper()
    pkg_count = m.group("pkg_count")
    pkg_type = m.group("pkg_type").upper()
    if pkg_type == "PALLET":
        pkg_type = "PALLETS"
    unstackable = bool(re.search(r"\*\*\*UNSTACKABLE\*\*\*", line, re.I))
    packages_line = f"{pkg_count} {pkg_type}"
    if unstackable:
        packages_line = f"{packages_line} ***UNSTACKABLE***"
    shipper, consignee, notify = _block_parties(block)
    summary_acid = _normalize_acid(m.group("acid"))
    acid = _extract_acid_from_block(block, summary_acid)

    hs_code = _extract_hs_from_block(block)
    freight = (m.group("freight") or "").strip()
    freight_term = None
    if re.search(r"Collect", freight, re.I):
        freight_term = "Collect"
    elif re.search(r"Prepaid", freight, re.I):
        freight_term = "Prepaid"

    row: Dict[str, Any] = {
        "hbl": hbl,
        "destination": (m.group("dest") or "").strip(),
        "service": m.group("service"),
        "packages": pkg_count,
        "package_type": pkg_type,
        "packages_line": packages_line,
        "gross_weight_kg": _parse_num(m.group("kgs")),
        "measurement_cbm": _parse_num(m.group("cbm")),
        "acid": acid,
        "freight_term": freight_term,
        "shipper": shipper,
        "consignee": consignee,
        "notify": notify,
        "cargo_description": _cargo_description(block, hs_code),
        "hs_code": hs_code,
        "unstackable": unstackable,
    }
    return {k: v for k, v in row.items() if v is not None and v != ""}


def parse_manifest_rows(text: str) -> List[Dict[str, Any]]:
    visual = _visual_sections(text)
    if not HOUSE_ROW_RE.search(visual):
        return []

    rows_by_hbl: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for m in HOUSE_ROW_RE.finditer(visual):
        start = m.start()
        nxt = HOUSE_ROW_RE.search(visual, m.end())
        end = nxt.start() if nxt else len(visual)
        block = visual[start:end]
        line = block.split("\n", 1)[0]
        row = _parse_house_row(line, block)
        if not row:
            continue
        hbl = row["hbl"]
        if hbl not in rows_by_hbl:
            rows_by_hbl[hbl] = row
            order.append(hbl)

    return [rows_by_hbl[h] for h in order]


def parse_manifest_master_context(text: str) -> Dict[str, Any]:
    header_text = _visual_sections(text) or text
    ctx: Dict[str, Any] = {
        "agent": "MARINE & ENGINEERING SERVICES CO. (MESCO)",
    }

    tm = TOTAL_RE.search(header_text) or TOTAL_RE.search(text)
    if tm:
        ctx["totals"] = {
            "packages": float(tm.group("pkgs")),
            "gross_weight_kg": _parse_num(tm.group("kgs")),
            "measurement_cbm": _parse_num(tm.group("cbm")),
        }

    cm = CONTAINER_RE.search(header_text) or CONTAINER_RE.search(text)
    if cm:
        ctx["master_no"] = cm.group("master").strip()
        ctx["containers"] = [
            {
                "container_number": cm.group("container").strip(),
                "seal_number": cm.group("seal").strip(),
                "container_type": re.sub(r"\s+", "", cm.group("size").strip()),
            }
        ]

    vm = VESSEL_RE.search(header_text) or VESSEL_RE.search(text)
    if vm:
        ctx["vessel"] = vm.group("vessel").strip()
        ctx["voyage"] = vm.group("voyage").strip()

    dm = DEST_RE.search(header_text) or DEST_RE.search(text)
    if dm:
        ctx["pod"] = dm.group(1).strip()

    em = ETD_RE.search(header_text) or ETD_RE.search(text)
    if em:
        ctx["pol"] = em.group(1).strip()

    hbls = [m.group(1).upper() for m in HBL_RE.finditer(header_text)]
    ctx["expected_house_count"] = len(set(hbls)) if hbls else None
    return ctx


def _row_to_record(row: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    master_no = ctx.get("master_no")
    rec: Dict[str, Any] = {
        "document_type": "Turkey Cargo Manifest",
        "mesco_houseblno": row["hbl"],
        "mesco_masterblno": master_no,
        "mesco_shippernamecontactno": row.get("shipper"),
        "mesco_consigneenamecontactno": row.get("consignee"),
        "mesco_notify1": row.get("notify"),
        "mesco_origin": ctx.get("pol"),
        "mesco_destination": row.get("destination") or ctx.get("pod"),
        "mesco_transhipmentport": ctx.get("pod"),
        "mesco_vessel": ctx.get("vessel"),
        "mesco_voytruckno": ctx.get("voyage"),
        "mesco_acidnumber": row.get("acid"),
        "mesco_hscode": row.get("hs_code"),
        "mesco_cargodescription": row.get("cargo_description"),
        "cr401_totalpackages": row.get("packages_line") or row.get("packages"),
        "package_type": row.get("packages_line"),
        "mesco_handlinginformation": "***UNSTACKABLE***"
        if row.get("unstackable")
        else None,
        "cr401_totalgrossweight": row.get("gross_weight_kg"),
        "cr401_totalvolume": row.get("measurement_cbm"),
        "mesco_pcfreightterm": row.get("freight_term"),
        "mesco_transporttype": 300000000,
        "mesco_loadtype": 300000001,
        "mesco_direction": 300000001,
        "mesco_bltype": 886150002,
        "extraction_method": "pdf_tur_cargo_manifest",
        "_manifest_pdf_row": True,
        "_mbl_masterblno": master_no,
        "delivery_agent": ctx.get("agent"),
    }
    return {k: v for k, v in rec.items() if v is not None and v != ""}


def build_manifest_master_record(ctx: Dict[str, Any], rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    totals = ctx.get("totals") or {}
    master_no = ctx.get("master_no")
    master: Dict[str, Any] = {
        "document_type": "Turkey Cargo Manifest",
        "mesco_masterblno": master_no,
        "mesco_consigneenamecontactno": ctx.get("agent"),
        "mesco_notify1": ctx.get("agent"),
        "mesco_origin": ctx.get("pol"),
        "mesco_destination": ctx.get("pod"),
        "mesco_vessel": ctx.get("vessel"),
        "mesco_voytruckno": ctx.get("voyage"),
        "mesco_cargodescription": "CARGO MANIFEST",
        "mesco_consolidation": True,
        "cr401_totalpackages": int(totals["packages"]) if totals.get("packages") else None,
        "cr401_totalgrossweight": round(totals["gross_weight_kg"], 2)
        if totals.get("gross_weight_kg") is not None
        else None,
        "cr401_totalvolume": round(totals["measurement_cbm"], 2)
        if totals.get("measurement_cbm") is not None
        else None,
        "mesco_transporttype": 300000000,
        "mesco_loadtype": 300000001,
        "mesco_direction": 300000001,
        "mesco_bltype": 886150001,
        "containers": ctx.get("containers") or [],
        "extraction_method": "pdf_tur_cargo_manifest",
        "_mbl_masterblno": master_no,
        "delivery_agent": ctx.get("agent"),
    }
    if rows:
        master["mesco_nooforgbls"] = str(len(rows))
    return {k: v for k, v in master.items() if v is not None and v != ""}


def parse_tur_cargo_manifest(text: str) -> Optional[Dict[str, Any]]:
    if not is_tur_cargo_manifest(text):
        return None
    rows = parse_manifest_rows(text)
    if not rows:
        return None
    ctx = parse_manifest_master_context(text)
    return {
        "context": ctx,
        "rows": rows,
        "house_records": [_row_to_record(r, ctx) for r in rows],
        "master_record": build_manifest_master_record(ctx, rows),
    }
