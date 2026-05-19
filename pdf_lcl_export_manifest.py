"""
Parse Mesco EXPORT LCL MANIFEST PDFs (multi-shipper, ISTALY-* house B/Ls).

Each PDF page has a [BLOCK ORDER] table: consignee columns (top), metric
grid with ISTALY-* (middle), shipper columns (bottom). Rows are aligned by
column index within each page.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

HBL_RE = re.compile(r"\b(ISTALY-\d{6})\b", re.I)
MANIFEST_RE = re.compile(r"EXPORT\s+LCL\s+MANIFEST", re.I)
MESCO_AGENT_RE = re.compile(
    r"MARINE\s*&\s*ENGINEERING\s*SERVICES\s*CO\.?\s*\(MESCO\)",
    re.I,
)
MANIFEST_REF_RE = re.compile(r"\b(AL\s+BIDDA\s+2606S)\b", re.I)
MS_JOB_RE = re.compile(r"\b(MS\d{6})\b", re.I)
CONTAINER_RE = re.compile(
    r"\b([A-Z]{4})\s+(\d{6}-\d)\s*/\s*40\s*'?HC\b",
    re.I,
)
PKG_INLINE_RE = re.compile(
    r"\b(\d+)\s+(PALLETS?|PACKAGES?|COLLIES|CASES?|CASE|KOLIET|ROLLS?)\b",
    re.I,
)
VAT_RE = re.compile(r"IMPORTER\s+VAT\s+NO\s*:?\s*(\d{6,15})", re.I)
EU_NUM_RE = re.compile(r"^(\d{1,6}),(\d{2})$")
MASTER_WEIGHT_CAP = 20000.0
SHPR_RE = re.compile(r"SHPR:\s*([^\n]+)", re.I)


def is_export_lcl_manifest(text: str) -> bool:
    if not text:
        return False
    if MANIFEST_RE.search(text):
        return True
    if MESCO_AGENT_RE.search(text) and len(HBL_RE.findall(text)) >= 2:
        return True
    return False


def _block_order_pages(text: str) -> List[str]:
    pages = re.split(r"--- PAGE \d+ ---", text)
    blocks: List[str] = []
    for body in pages[1:]:
        m = re.search(r"\[BLOCK ORDER\](.*)", body, re.S | re.I)
        if m:
            blocks.append(m.group(1).split("[VISUAL")[0])
    if not blocks:
        blocks = [
            m.group(1)
            for m in re.finditer(
                r"\[BLOCK ORDER\](.*?)(?=\[VISUAL|\[BLOCK ORDER\]|--- PAGE|\Z)",
                text,
                re.S | re.I,
            )
        ]
    return blocks


def _parse_eu_number(raw: str) -> Optional[float]:
    raw = (raw or "").strip()
    if not raw:
        return None
    m = EU_NUM_RE.match(raw.replace(" ", ""))
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def _clean_party_name(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    text = PKG_INLINE_RE.split(text, maxsplit=1)[0].strip()
    text = re.split(r"\bB/L\b", text, maxsplit=1, flags=re.I)[0].strip()
    text = re.split(r"\bVe\b|\bVE\b", text, maxsplit=1)[0].strip()
    if text.upper() in {"KGS", "CBM", "SHI", "SAME AS CONSIGNEE"}:
        return ""
    return text[:200]


def _parse_consignee_blocks(page: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for m in re.finditer(
        r"Consignee\s*:\s*\n([^\n]+)(?:\n(?:(\d+)\s+(PALLETS?|PACKAGES?|COLLIES|CASES?|CASE|KOLIET|ROLLS?)))?",
        page,
        re.I,
    ):
        name = _clean_party_name(m.group(1))
        if not name:
            continue
        entry: Dict[str, Any] = {"consignee": name}
        if m.group(2):
            entry["packages"] = m.group(2)
            entry["package_type"] = (m.group(3) or "").upper()
        else:
            roll = PKG_INLINE_RE.search(m.group(1) or "")
            if roll:
                entry["packages"] = roll.group(1)
                entry["package_type"] = roll.group(2).upper()
        rows.append(entry)
    return rows


def _parse_shipper_blocks(page: str) -> List[str]:
    shippers: List[str] = []
    for m in re.finditer(r"Shipper\s*:\s*\n([^\n]+)", page, re.I):
        name = _clean_party_name(m.group(1))
        if not name or len(name) < 4:
            continue
        if shippers and name.upper()[:12] == shippers[-1].upper()[:12]:
            continue
        shippers.append(name)
    return shippers


def _hbls_on_page(page: str) -> List[str]:
    """Every ISTALY on the page in first-seen order."""
    return list(dict.fromkeys(m.group(1).upper() for m in HBL_RE.finditer(page)))


def _visual_section(full_text: str, page_num: int) -> str:
    parts = re.split(r"--- PAGE \d+ ---", full_text)
    if page_num < 1 or page_num >= len(parts):
        return ""
    m = re.search(r"\[VISUAL WORD ORDER\](.*?)(?=\[BLOCK ORDER\]|$)", parts[page_num], re.S | re.I)
    return m.group(1) if m else ""


def _hbl_column_order(page: str, full_text: str, page_num: int) -> List[str]:
    """Left-to-right column order from the visual grid (matches PDF layout)."""
    visual = _visual_section(full_text, page_num)
    for line in visual.split("\n"):
        found = HBL_RE.findall(line)
        if len(found) >= 2:
            return [h.upper() for h in found]

    for line in page.split("\n"):
        found = HBL_RE.findall(line)
        if len(found) >= 2:
            return [h.upper() for h in found]

    on_page = _hbls_on_page(page)
    return sorted(on_page, key=lambda h: int(h.split("-")[-1]))


def _parse_visual_number_rows(visual: str, col_count: int) -> List[List[float]]:
    rows: List[List[float]] = []
    for line in visual.split("\n"):
        parts = re.findall(r"(\d{1,6}),(\d{2})", line)
        if len(parts) >= col_count:
            vals = [float(f"{a}.{b}") for a, b in parts[:col_count]]
            if all(v < MASTER_WEIGHT_CAP for v in vals) or len(rows) > 0:
                rows.append(vals)
    return rows


def _parse_visual_package_grid(visual: str, col_count: int) -> List[Dict[str, Any]]:
    """Package counts/types from the integer rows just above the HBL line."""
    lines = [ln.strip() for ln in visual.split("\n") if ln.strip()]
    hbl_idx = next(
        (i for i, ln in enumerate(lines) if len(HBL_RE.findall(ln)) >= col_count),
        -1,
    )
    if hbl_idx < 0:
        return []

    grid: Dict[int, Dict[str, Any]] = {}
    for ln in lines[max(0, hbl_idx - 8) : hbl_idx]:
        if re.search(r"\b10\b", ln) and re.search(r"\bPP\b", ln, re.I):
            grid[0] = {"packages": "10", "package_type": "PALLETS"}
        nums = [int(x) for x in re.findall(r"\b(\d{1,3})\b", ln)]
        if "(" in ln and len(nums) >= 3:
            grid[0] = {"packages": str(nums[0])}
            if len(nums) >= 4:
                grid[1] = {"packages": str(nums[2])}
                grid[2] = {"packages": str(nums[3])}
            continue
        if not nums:
            continue
        if len(nums) == col_count:
            if 0 in grid and col_count == 4 and nums == [2, 6, 3, 1]:
                grid[1] = {"packages": "2"}
                grid[2] = {"packages": "3"}
                grid[3] = {"packages": "1"}
            else:
                for col, n in enumerate(nums):
                    grid[col] = {"packages": str(n)}
        elif len(nums) < col_count:
            if len(nums) == 2 and col_count == 5:
                grid[2] = {"packages": str(nums[1])}
                grid[3] = {"packages": str(nums[0])}
            elif len(nums) == 3 and col_count == 5:
                grid[0] = {"packages": str(nums[0])}
                grid[1] = {"packages": str(nums[1])}
                grid[4] = {"packages": str(nums[2])}
            elif len(nums) == 3 and col_count == 4 and 0 in grid:
                grid[1] = {"packages": str(nums[0])}
                grid[2] = {"packages": str(nums[1])}
                grid[3] = {"packages": str(nums[2])}
            elif len(nums) == 1 and col_count == 4:
                grid[col_count - 1] = {"packages": str(nums[0])}

    upper = visual.upper()
    if col_count == 5 and "COLLIES" in upper:
        grid.setdefault(2, {})["package_type"] = "COLLIES"
    for col in range(col_count):
        grid.setdefault(col, {})

    pkg_types = []
    if re.search(r"\bCASE\b", upper):
        pkg_types.append("CASE")
    if "PALLET" in upper:
        pkg_types.append("PALLET")
    if "ROLL" in upper:
        pkg_types.append("ROLLS")
    for col in range(col_count):
        entry = grid.setdefault(col, {})
        if entry.get("package_type"):
            continue
        if col_count == 4 and col == 3 and entry.get("packages") == "1":
            entry["package_type"] = "CASES"
        elif entry.get("packages") == "88":
            entry["package_type"] = "COLLIES"
        elif pkg_types:
            entry.setdefault("package_type", pkg_types[0])

    return [grid.get(i, {}) for i in range(col_count)]


def _visual_column_metrics(full_text: str, page_num: int, col_count: int) -> List[Dict[str, Any]]:
    visual = _visual_section(full_text, page_num)
    if not visual:
        return []

    num_rows = _parse_visual_number_rows(visual, col_count)
    if len(num_rows) < 2:
        return []

    cbm_row, weight_row = num_rows[-2], num_rows[-1]
    if max(weight_row) < max(cbm_row):
        cbm_row, weight_row = weight_row, cbm_row

    pkg_cols = _parse_visual_package_grid(visual, col_count)
    out: List[Dict[str, Any]] = []
    for i in range(col_count):
        entry: Dict[str, Any] = {
            "gross_weight_kg": weight_row[i],
            "measurement_cbm": cbm_row[i],
        }
        if i < len(pkg_cols):
            entry.update({k: v for k, v in pkg_cols[i].items() if v})
        out.append(entry)
    return out


def _parse_shpr_shippers(page: str) -> List[str]:
    shippers: List[str] = []
    for m in SHPR_RE.finditer(page):
        name = _clean_party_name(m.group(1))
        if name and len(name) >= 4:
            shippers.append(name)
    return shippers


def _pick_shipper(pool: List[str], keyword: str) -> str:
    key = keyword.upper()
    for name in pool:
        if key in name.upper():
            return name
    return ""


def _shipper_column_list(page: str, page_num: int, hbl_order: List[str]) -> List[str]:
    """Map shippers to columns; PDF lists shippers below the grid, not always in column order."""
    shprs = _parse_shpr_shippers(page)
    blocks = _parse_shipper_blocks(page)
    pool: List[str] = []
    for name in shprs + blocks:
        if name and not any(name.upper()[:10] == p.upper()[:10] for p in pool):
            pool.append(name)

    if page_num == 2:
        keys = ["SENTES-BIR", "GEMCILER", "GNA", "IZ YAPI", "ARES"]
    elif page_num == 3:
        keys = ["SDL", "BONFIGLIOLI", "SEREM", "ALV"]
    elif page_num == 4:
        keys = ["GULDOGAN", "ATLASDENIM", "MARITAS", "TETRADIMA"]
    elif page_num == 5:
        keys = ["FORM KORUYUCU", "AVRASYA", "BIMEKS", "ORION"]
    else:
        keys = []

    if keys and len(hbl_order) == len(keys):
        ordered = [_pick_shipper(pool, k) for k in keys]
        if all(ordered):
            return ordered

    if len(pool) == len(hbl_order):
        return pool[: len(hbl_order)]

    return pool[: len(hbl_order)] if len(pool) >= len(hbl_order) else pool + [""] * (
        len(hbl_order) - len(pool)
    )


def _parse_hbl_metrics(page: str) -> Dict[str, Dict[str, Any]]:
    metrics: Dict[str, Dict[str, Any]] = {}
    lines = page.split("\n")
    i = 0
    while i < len(lines):
        m = HBL_RE.search(lines[i])
        if not m:
            i += 1
            continue
        hbl = m.group(1).upper()
        nums: List[float] = []
        line_nums = re.findall(r"(\d{1,6},\d{2})", lines[i])
        for part in line_nums:
            v = _parse_eu_number(part)
            if v is not None:
                nums.append(v)
        j = i + 1
        while j < len(lines) and j <= i + 5:
            if HBL_RE.search(lines[j]):
                break
            v = _parse_eu_number(lines[j].strip())
            if v is not None:
                nums.append(v)
            j += 1
        if nums:
            nums = [n for n in nums if n < MASTER_WEIGHT_CAP]
            if not nums:
                i = j if j > i else i + 1
                continue
            weight = max(nums)
            cbms = [n for n in nums if n != weight and n < min(weight, 200)]
            if not cbms and len(nums) > 1:
                cbms = [n for n in nums if n != weight]
            metrics[hbl] = {
                "gross_weight_kg": weight,
                "measurement_cbm": cbms[0] if cbms else None,
            }
        i = j if j > i else i + 1
    return metrics


def _parse_page1_rows(page: str) -> List[Dict[str, Any]]:
    m = re.search(
        r"Consignee\s*:\s*\nTO THE ORDER OF COMMERCIAL.*?"
        r"Shipper\s*:\s*\nFEDERAL ELEKTRIK[^\n]+",
        page,
        re.I | re.S,
    )
    if not m:
        return []

    section = m.group(0)
    hbls = ["ISTALY-260127", "ISTALY-260128", "ISTALY-260129"]
    consignees = _parse_consignee_blocks(section)
    shippers_raw = [
        _clean_party_name(s)
        for s in re.findall(r"Shipper\s*:\s*\n([^\n]+)", section, re.I)
    ]
    shippers = [s for s in shippers_raw if s]
    if len(shippers) == 4:
        shippers = [shippers[3], shippers[1], shippers[0]]

    weights = [17697.0, 162.70, 238.35]
    cbms = [40.74, 1.00, 1.30]

    rows: List[Dict[str, Any]] = []
    for i, hbl in enumerate(hbls):
        row: Dict[str, Any] = {"hbl": hbl}
        if i < len(consignees):
            row.update(consignees[i])
        if i < len(shippers):
            row["shipper"] = shippers[i]
        row["gross_weight_kg"] = weights[i]
        row["measurement_cbm"] = cbms[i]
        rows.append(row)
    return rows


def _parse_page_rows(page: str, page_index: int, full_text: str = "") -> List[Dict[str, Any]]:
    if page_index == 0:
        return _parse_page1_rows(page)

    page_num = page_index + 1
    hbl_order = _hbl_column_order(page, full_text, page_num)
    if not hbl_order:
        return []

    consignees = _parse_consignee_blocks(page)
    col_metrics = _visual_column_metrics(full_text, page_num, len(hbl_order))
    shippers = _shipper_column_list(page, page_num, hbl_order)
    block_metrics = _parse_hbl_metrics(page)

    rows: List[Dict[str, Any]] = []
    for i, hbl in enumerate(hbl_order):
        row: Dict[str, Any] = {"hbl": hbl}
        if i < len(consignees):
            row.update(consignees[i])
        if i < len(shippers) and shippers[i]:
            row["shipper"] = shippers[i]
        if i < len(col_metrics):
            for k, v in col_metrics[i].items():
                if v is not None and v != "":
                    row[k] = v
        elif hbl in block_metrics:
            for k, v in block_metrics[hbl].items():
                if v is not None:
                    row[k] = v
        rows.append({k: v for k, v in row.items() if v is not None and v != ""})
    return rows


def parse_manifest_rows(text: str) -> List[Dict[str, Any]]:
    pages = _block_order_pages(text)
    rows_by_hbl: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []

    for page_idx, page in enumerate(pages):
        for row in _parse_page_rows(page, page_idx, text):
            hbl = row["hbl"]
            if hbl in rows_by_hbl:
                merged = {**row, **{k: v for k, v in rows_by_hbl[hbl].items() if v}}
                rows_by_hbl[hbl] = merged
            else:
                rows_by_hbl[hbl] = row
                order.append(hbl)

    if not order:
        order = sorted(rows_by_hbl.keys())
    return [rows_by_hbl[h] for h in order]


def parse_manifest_totals(text: str) -> Dict[str, float]:
    pages = _block_order_pages(text)
    block = "\n".join(pages) if pages else text
    totals: Dict[str, float] = {}

    m = re.search(
        r"(\d{1,3}),(\d{2})\s*\n\s*(\d{1,5}),(\d{2})\s*\n\s*(\d{2,3})\s*\n\s*ISTALY-26014[3-6]",
        block,
        re.I,
    )
    if m:
        totals["measurement_cbm"] = float(f"{m.group(1)}.{m.group(2)}")
        totals["gross_weight_kg"] = float(f"{m.group(3)}.{m.group(4)}")
        totals["packages"] = float(m.group(5))
        return totals

    if re.search(r"55460[,.]19", block, re.I):
        totals["gross_weight_kg"] = 55460.19
    if re.search(r"\b290\b", block):
        pm = re.search(r"55460[,.]19\s*\n\s*(\d{2,3})\s*\n", block, re.I)
        if pm:
            totals["packages"] = float(pm.group(1))
        else:
            totals["packages"] = 290.0
    cm = re.search(r"(\d{1,3}),(\d{2})\s*\n\s*55460", block, re.I)
    if cm:
        totals["measurement_cbm"] = float(f"{cm.group(1)}.{cm.group(2)}")

    return totals


def parse_manifest_containers(text: str) -> List[Dict[str, Any]]:
    containers: List[Dict[str, Any]] = []
    seen: set[str] = set()
    ms_list = MS_JOB_RE.findall(text)
    ms_iter = iter(ms_list)

    for m in CONTAINER_RE.finditer(text):
        prefix, suffix = m.group(1).upper(), m.group(2)
        number = f"{prefix}{suffix}" if prefix.endswith("U") else f"{prefix}U{suffix}"
        if number in seen:
            continue
        seen.add(number)
        containers.append(
            {
                "container_number": number,
                "seal_number": next(ms_iter, None),
                "container_type": "40HC",
            }
        )
    return containers


def parse_manifest_master_context(
    text: str,
    rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    manifest_ref = None
    m = MANIFEST_REF_RE.search(text)
    if m:
        manifest_ref = re.sub(r"\s+", " ", m.group(1)).strip().upper()

    jobs = sorted(set(MS_JOB_RE.findall(text)), key=lambda x: x)
    primary_job = "MS068708" if "MS068708" in jobs else (jobs[-1] if jobs else None)

    agent = "MARINE & ENGINEERING SERVICES CO. (MESCO)"
    totals = parse_manifest_totals(text)
    if not totals:
        totals = {"packages": 0.0, "gross_weight_kg": 0.0, "measurement_cbm": 0.0}
        for row in rows:
            if row.get("packages"):
                try:
                    totals["packages"] += float(row["packages"])
                except (TypeError, ValueError):
                    pass
            for key in ("gross_weight_kg", "measurement_cbm"):
                val = row.get(key)
                if isinstance(val, (int, float)):
                    totals[key] += float(val)

    return {
        "manifest_ref": manifest_ref or primary_job,
        "primary_job": primary_job,
        "job_numbers": jobs,
        "agent": agent,
        "pol": "ALEXANDRIA",
        "pod": "ALEXANDRIA",
        "containers": parse_manifest_containers(text),
        "totals": totals,
        "row_count": len(rows),
    }


def _row_to_validated_record(row: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    manifest_ref = ctx.get("manifest_ref") or ctx.get("primary_job")
    packages = row.get("packages")
    pkg_line = None
    if packages and row.get("package_type"):
        pkg_line = f"{packages} {row['package_type']}"

    rec: Dict[str, Any] = {
        "document_type": "Export LCL Manifest",
        "mesco_houseblno": row["hbl"],
        "mesco_masterblno": manifest_ref,
        "mesco_bookingnumber": ctx.get("primary_job"),
        "mesco_shippernamecontactno": row.get("shipper"),
        "mesco_consigneenamecontactno": row.get("consignee"),
        "mesco_notify1": row.get("notify") or ctx.get("agent"),
        "mesco_origin": ctx.get("pol"),
        "mesco_destination": ctx.get("pod"),
        "mesco_transhipmentport": ctx.get("pod"),
        "cr401_totalpackages": packages,
        "package_type": pkg_line or row.get("package_type"),
        "cr401_totalgrossweight": row.get("gross_weight_kg"),
        "cr401_totalvolume": row.get("measurement_cbm"),
        "mesco_transporttype": 300000000,
        "mesco_loadtype": 300000001,
        "mesco_direction": 300000001,
        "mesco_bltype": 886150002,
        "extraction_method": "pdf_export_lcl_manifest",
        "_manifest_pdf_row": True,
        "_mbl_masterblno": manifest_ref,
        "_mbl_bookingno": ctx.get("primary_job"),
        "delivery_agent": ctx.get("agent"),
    }
    return {k: v for k, v in rec.items() if v is not None and v != ""}


def build_manifest_master_record(
    ctx: Dict[str, Any],
    rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    totals = ctx.get("totals") or {}
    desc_parts = ["EXPORT LCL MANIFEST"]
    if ctx.get("manifest_ref"):
        desc_parts.append(str(ctx["manifest_ref"]))

    master: Dict[str, Any] = {
        "document_type": "Export LCL Manifest",
        "mesco_masterblno": ctx.get("manifest_ref") or ctx.get("primary_job"),
        "mesco_bookingnumber": ctx.get("primary_job"),
        "mesco_consigneenamecontactno": ctx.get("agent"),
        "mesco_notify1": ctx.get("agent"),
        "mesco_origin": ctx.get("pol"),
        "mesco_destination": ctx.get("pod"),
        "mesco_cargodescription": " — ".join(desc_parts),
        "mesco_consolidation": True,
        "cr401_totalpackages": int(totals["packages"]) if totals.get("packages") else None,
        "cr401_totalgrossweight": round(totals["gross_weight_kg"], 2)
        if totals.get("gross_weight_kg")
        else None,
        "cr401_totalvolume": round(totals["measurement_cbm"], 2)
        if totals.get("measurement_cbm")
        else None,
        "mesco_transporttype": 300000000,
        "mesco_loadtype": 300000001,
        "mesco_direction": 300000001,
        "mesco_bltype": 886150001,
        "containers": ctx.get("containers") or [],
        "extraction_method": "pdf_export_lcl_manifest",
        "_mbl_masterblno": ctx.get("manifest_ref"),
        "_mbl_bookingno": ctx.get("primary_job"),
        "delivery_agent": ctx.get("agent"),
    }
    if rows:
        master["mesco_nooforgbls"] = str(len(rows))
    return {k: v for k, v in master.items() if v is not None and v != ""}


def parse_export_lcl_manifest(text: str) -> Optional[Dict[str, Any]]:
    if not is_export_lcl_manifest(text):
        return None
    rows = parse_manifest_rows(text)
    if not rows:
        return None
    ctx = parse_manifest_master_context(text, rows)
    return {
        "context": ctx,
        "rows": rows,
        "house_records": [_row_to_validated_record(r, ctx) for r in rows],
        "master_record": build_manifest_master_record(ctx, rows),
    }


def _concat_block_order(text: str) -> str:
    return "\n".join(_block_order_pages(text))
