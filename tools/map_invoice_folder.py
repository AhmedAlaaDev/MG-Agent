"""Sequentially inspect invoice PDFs with the configured intelligent agent.

This is a dry-run mapping tool: it reads each PDF, runs the local OCR pipeline,
lets Gemini inspect the original PDF layout, and writes a reviewable mapping
report. It never posts anything to Dataverse.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SERVICE_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = SERVICE_ROOT.parent
INVOICES_ROOT = WORKSPACE_ROOT / "Invoices"
JSON_REPORT = INVOICES_ROOT / "invoice_mapping_report.json"
MARKDOWN_REPORT = INVOICES_ROOT / "invoice_mapping_report.md"

sys.path.insert(0, str(SERVICE_ROOT))

from ai_extractor import extract_multi_invoice_with_llm  # noqa: E402
from config import settings  # noqa: E402
from spreadsheet_extractor import extract_document_text_professionally  # noqa: E402


def _clean_container(value: Any) -> str | None:
    """Return the first ISO container number from a possibly combined value."""
    match = re.search(r"\b[A-Z]{4}\d{7}\b", str(value or "").upper())
    return match.group(0) if match else None


def _console_safe(value: Any) -> str:
    return str(value).encode("ascii", "backslashreplace").decode("ascii")


def _crm_mapping(data: dict[str, Any]) -> dict[str, Any]:
    """Mirror the fields consumed by the invoice-to-cost-line endpoint."""
    vendor_invoice_number = data.get("vendor_invoice_number")
    vendor_name = data.get("vendor_name")
    container_number = _clean_container(data.get("container_number"))
    groups = data.get("groups") or []
    mapped_lines: list[dict[str, Any]] = []

    for group in groups:
        hbl = group.get("house_bl_number")
        for item in group.get("line_items") or []:
            mapped_lines.append(
                {
                    "xollsp_name": item.get("service_description") or "Invoice Charge",
                    "xollsp_quantity": item.get("quantity") or 1,
                    "xollsp_unitamount": item.get("unit_price") or 0,
                    "xollsp_totalamount": item.get("total_amount"),
                    "mesco_vendorinvoicenumber": vendor_invoice_number,
                    "invoice_vendor_source": vendor_name,
                    "house_bl_number": hbl,
                    "currency_source": data.get("currency"),
                    "container_source": container_number,
                }
            )

    return {
        "header": {
            "vendor_name_source": vendor_name,
            "vendor_invoice_number": vendor_invoice_number,
            "master_bl_number": data.get("master_bl_number"),
            "container_number": container_number,
            "seal_number": data.get("seal_number"),
            "currency": data.get("currency"),
        },
        "lookup_targets": {
            "vendor": "mesco_invoicevendor_shippingline",
            "currency": "transactioncurrencyid / xollsp_Currency",
            "service": "xollsp_LogisticService",
            "container": "mesco_Container",
            "operation_relation": "mesco_Master3 or mesco_Operation, resolved by HBL/MBL",
        },
        "cost_lines": mapped_lines,
    }


def _write_reports(results: list[dict[str, Any]]) -> None:
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "agent": {
            "provider": settings.llm_provider,
            "model": settings.gemini_model,
            "native_pdf_layout_enabled": settings.gemini_native_pdf,
        },
        "source_folder": str(INVOICES_ROOT),
        "files_examined": len(results),
        "results": results,
    }
    JSON_REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = [
        "# Invoice mapping report",
        "",
        f"Examined: **{len(results)}** PDF files",
        f"Agent: **{settings.llm_provider} / {settings.gemini_model}**",
        "",
        "This is a dry-run mapping report. No Dataverse records were created or changed.",
        "",
    ]
    for index, result in enumerate(results, start=1):
        name = result.get("source_file", "unknown")
        lines.extend([f"## {index}. `{name}`", ""])
        if result.get("error"):
            lines.extend([f"- Status: **error** - {result['error']}", ""])
            continue
        data = result.get("extracted", {})
        lines.extend(
            [
                f"- Status: **{result.get('status', 'review')}**",
                f"- Vendor: `{data.get('vendor_name') or '-'}`",
                f"- Invoice/debit note no.: `{data.get('vendor_invoice_number') or '-'}`",
                f"- Master B/L: `{data.get('master_bl_number') or '-'}`",
                f"- Container: `{data.get('container_number') or '-'}`",
                f"- Seal: `{data.get('seal_number') or '-'}`",
                f"- Currency: `{data.get('currency') or '-'}`",
                f"- HBL groups: **{len(data.get('groups') or [])}**",
                f"- Cost lines: **{sum(len(g.get('line_items') or []) for g in data.get('groups') or [])}**",
                "",
            ]
        )
        for group in data.get("groups") or []:
            lines.append(f"### HBL `{group.get('house_bl_number') or '-'}`")
            lines.append("")
            lines.append("| Service | Qty | Unit price | Total |")
            lines.append("|---|---:|---:|---:|")
            for item in group.get("line_items") or []:
                lines.append(
                    f"| {item.get('service_description') or 'Invoice Charge'} | "
                    f"{item.get('quantity') or 1} | {item.get('unit_price') or 0} | "
                    f"{item.get('total_amount') if item.get('total_amount') is not None else '-'} |"
                )
            lines.append("")

    MARKDOWN_REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    pdf_by_key: dict[str, Path] = {}
    for candidate in (*INVOICES_ROOT.rglob("*.pdf"), *INVOICES_ROOT.rglob("*.PDF")):
        pdf_by_key[str(candidate.resolve()).casefold()] = candidate
    pdfs = sorted(pdf_by_key.values(), key=lambda path: str(path).lower())
    if not pdfs:
        raise SystemExit(f"No PDF files found under {INVOICES_ROOT}")

    results: list[dict[str, Any]] = []
    if JSON_REPORT.exists():
        try:
            prior = json.loads(JSON_REPORT.read_text(encoding="utf-8"))
            prior_results = list(prior.get("results") or [])
            deduplicated: dict[str, dict[str, Any]] = {}
            for item in prior_results:
                key = str(item.get("source_file") or "").casefold()
                if not key:
                    continue
                if item.get("status") == "mapped" or key not in deduplicated:
                    deduplicated[key] = item
            results = list(deduplicated.values())
        except (OSError, json.JSONDecodeError):
            results = []
    completed = {item.get("source_file"): item for item in results if item.get("status") == "mapped"}

    for index, path in enumerate(pdfs, start=1):
        relative_name = str(path.relative_to(WORKSPACE_ROOT))
        if relative_name in completed:
            print(f"[{index}/{len(pdfs)}] reusing {relative_name}", flush=True)
            continue

        results = [item for item in results if item.get("source_file") != relative_name]
        print(f"[{index}/{len(pdfs)}] examining {relative_name}", flush=True)
        try:
            file_bytes = path.read_bytes()
            extracted = extract_document_text_professionally(file_bytes, path.name)
            raw_text = extracted.get("text") or ""
            model_data = extract_multi_invoice_with_llm(
                raw_text,
                file_bytes=file_bytes,
                filename=path.name,
            )
            groups = model_data.get("groups") or []
            line_count = sum(len(group.get("line_items") or []) for group in groups)
            status = "review" if not model_data.get("vendor_invoice_number") or not line_count else "mapped"
            results.append(
                {
                    "source_file": relative_name,
                    "status": status,
                    "extraction_method": extracted.get("method"),
                    "extraction_quality": extracted.get("quality"),
                    "raw_text_chars": len(raw_text),
                    "extracted": model_data,
                    "crm_mapping": _crm_mapping(model_data),
                }
            )
            print(
                f"    mapped: vendor={_console_safe(model_data.get('vendor_name'))!r}, "
                f"invoice={_console_safe(model_data.get('vendor_invoice_number'))!r}, "
                f"groups={len(groups)}, lines={line_count}",
                flush=True,
            )
        except Exception as exc:  # keep the remaining PDFs moving
            results.append({"source_file": relative_name, "status": "error", "error": str(exc)})
            print(f"    ERROR: {str(exc).encode('ascii', 'backslashreplace').decode('ascii')}", flush=True)
        _write_reports(results)

    print(f"Wrote {JSON_REPORT}", flush=True)
    print(f"Wrote {MARKDOWN_REPORT}", flush=True)


if __name__ == "__main__":
    main()
