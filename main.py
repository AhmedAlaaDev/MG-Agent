r"""
Professional Native + Scanned PDF Bill of Lading Extractor
=======================================================

Run:
    pip install fastapi uvicorn python-multipart pymupdf pillow pytesseract numpy openai pydantic-settings
    uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

from dataverse.client_service import DataverseClientService, RetryConfig
from dataverse_uploader import (
    _CARGO_ENTITY,
    _CONTAINER_ENTITY,
    _ENTITY,
    _create_entity,
    _find_existing_container,
    _normalize_container_number,
    _resolve_containerno_lookup,
    _update_entity,
)
from dataverse_field_limits import cap_nested_payload

from spreadsheet_extractor import extract_document_text_professionally
from ai_extractor import MULTI_BL_JSON_SCHEMA, SYSTEM_PROMPT, extract_with_azure_openai
from document_parser import parse_document_intelligently
from pdf_deterministic_registry import best_deterministic_parse
from pdf_batch_processor import process_pdf_bytes
from record_reconciliation import merge_record_fields, reconcile_record_lists
from crm_mapper import map_crm_operation_to_records
from config import GEMINI_MODELS, settings
from llm_context import (
    llm_extraction_prefix,
    llm_meta,
    llm_request_overrides,
    normalize_llm_provider,
    uses_gemini,
    uses_puter,
    validate_llm_request,
)
from llm_models import GeminiModelQuery, LlmProviderQuery
from validator import validate_and_correct
from crm_output_formatter import (
    apply_bl_type_to_crm_payload,
    normalize_bl_type,
    records_to_house_json,
    records_to_master_json,
)
from pdf_attached_list import build_house_records_from_attached_list, extract_attached_list_house_refs
from pdf_lcl_export_manifest import is_export_lcl_manifest, parse_export_lcl_manifest
from pdf_tur_cargo_manifest import is_tur_cargo_manifest, parse_tur_cargo_manifest
from pdf_cargo_manifest import (
    is_cargo_manifest_hbl_blocks,
    parse_cargo_manifest_hbl_blocks,
)
from pdf_consolidated_lcl import (
    is_consolidated_lcl_multi_hbl,
    parse_consolidated_lcl_multi_hbl,
)
from pdf_debit_note import is_freight_debit_note, parse_freight_debit_note
from pdf_house_bl import is_standard_house_bl, parse_standard_house_bl
from pdf_standard_master_bl import is_standard_master_bl, parse_standard_master_bl
from pdf_sea_waybill import (
    build_house_records_for_consolidation_sea_waybill,
    is_consolidation_sea_waybill,
    master_record_without_house_cargo,
    parse_consolidation_sea_waybill,
)
from upload_audit import audit_store


_PUTER_DETERMINISTIC_FORCE_KEYS = {
    "mesco_masterblno",
    "mesco_bookingnumber",
    "mesco_acidnumber",
    "cr401_totalpackages",
    "cr401_totalgrossweight",
    "cr401_totalvolume",
    "mesco_origin",
    "mesco_destination",
    "mesco_vessel",
    "mesco_voytruckno",
    "container_number",
    "seal_number",
    "containers",
    "mesco_containertype",
    "mesco_transporttype",
    "mesco_loadtype",
    "mesco_pcfreightterm",
    "mesco_bookingterm",
    "mesco_freightpayableat",
    "mesco_consolidation",
    "mesco_shippingline",
    "mesco_shipper",
    "mesco_consignee",
    "mesco_country",
    "mesco_countryoforigin",
    "mesco_importerstaxno",
    "mesco_foreignsupplierregistrationnumber",
    "mesco_typeofregistrationnumber",
    "mesco_dateofissue",
    "mesco_shippedonboarddate",
    "mesco_placeofissue",
    "mesco_nooforgbls",
}


class BlTypeQuery(str, Enum):
    """Dynamics mesco_bltype: master (886150001) or house (886150002)."""

    master = "master"
    house = "house"


app = FastAPI(
    title="Professional PDF + OCR + Excel Bill of Lading Extractor",
    version="4.0.0",
    description="Extracts native/OCR PDF text or Excel sheet data, then extracts structured B/L JSON.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", response_class=HTMLResponse)
async def root() -> str:
    return """
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8">
      <title>B/L Extractor</title>
      <style>
        body{font-family:Arial,sans-serif;margin:40px;max-width:900px}
        input,button{font-size:16px;padding:8px;margin:6px 0}
        pre{white-space:pre-wrap;background:#f7f7f7;padding:16px;border-radius:8px}
      </style>
    </head>
    <body>
      <h1>B/L Extractor v4.0</h1>
      <p>FastAPI service for extracting Bill of Lading data from PDF and Excel files.</p>
      <h2>Default Puter AI Extractor</h2>
      <p><a href="/puter">Open Puter Gemini extractor</a> (browser-side Puter.js, no Gemini API key).</p>
      <form action="/docs" method="get">
        <button type="submit">Open API Docs</button>
      </form>
      <h2>Quick Test</h2>
      <form action="/extract/file" method="post" enctype="multipart/form-data">
        <input type="file" name="file" accept=".pdf,.xlsx,.xls,.csv" required>
        <button type="submit">Extract B/L</button>
      </form>
      <h2>Batch PDF Test</h2>
      <p><a href="/test/pdf">Open batch upload page</a> (multipart form, multiple PDFs, no Dataverse).</p>
      <h2>Audit</h2>
      <p><a href="/audit">Open upload audit dashboard</a> (live upload log, saved files, and responses).</p>
    </body>
    </html>
    """


@app.get("/audit", response_class=HTMLResponse, include_in_schema=False)
async def upload_audit_page() -> str:
    """Serve the realtime upload audit dashboard."""
    import os

    html_path = os.path.join(os.path.dirname(__file__), "audit_view.html")
    try:
        with open(html_path, "r", encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return """
        <!doctype html>
        <html>
        <body>
          <h1>Upload audit dashboard is not available</h1>
          <p>The audit_view.html file was not found on this deployment.</p>
        </body>
        </html>
        """


@app.get("/puter", response_class=HTMLResponse, include_in_schema=False)
async def puter_extractor_page() -> str:
    """Serve the browser-side Puter.js Gemini extractor."""
    import os

    html_path = os.path.join(os.path.dirname(__file__), "puter_extract.html")
    try:
        with open(html_path, "r", encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return """
        <!doctype html>
        <html>
        <body>
          <h1>Puter extractor is not available</h1>
          <p>The puter_extract.html file was not found on this deployment.</p>
        </body>
        </html>
        """


@app.get("/audit/uploads", tags=["Audit"])
async def list_upload_audit(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Return recent B/L upload audit records."""
    return {
        "success": True,
        "data": audit_store.list_uploads(limit=limit, offset=offset),
    }


@app.get("/audit/uploads/{audit_id}", tags=["Audit"])
async def get_upload_audit(audit_id: str):
    """Return one audit record including the stored response payload."""
    item = audit_store.get_upload(audit_id, include_response=True)
    if not item:
        raise HTTPException(status_code=404, detail="Upload log not found")
    return {"success": True, "data": item}


@app.get("/audit/uploads/{audit_id}/file", tags=["Audit"])
async def download_audit_file(audit_id: str):
    """Download the original uploaded document saved for this audit record."""
    item = audit_store.get_upload(audit_id, include_response=False)
    if not item or not item.get("saved_path"):
        raise HTTPException(status_code=404, detail="Upload file not found")
    path = item["saved_path"]
    return FileResponse(
        path,
        filename=item.get("original_filename") or item.get("saved_filename") or "upload.bin",
        media_type=item.get("content_type") or "application/octet-stream",
    )


@app.websocket("/audit/ws")
async def upload_audit_socket(websocket: WebSocket):
    """Realtime stream of B/L upload audit changes."""
    await audit_store.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        audit_store.disconnect(websocket)


@app.get("/test/pdf", response_class=HTMLResponse, include_in_schema=False)
async def test_pdf_upload_page() -> str:
    return """
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Batch PDF Test</title>
      <style>
        :root { --ok: #0d7a3f; --bad: #b42318; --warn: #b54708; --bg: #f4f6f8; }
        body { font-family: Segoe UI, Arial, sans-serif; margin: 0; background: var(--bg); color: #1a1a1a; }
        .wrap { max-width: 960px; margin: 0 auto; padding: 24px; }
        h1 { margin: 0 0 8px; font-size: 1.5rem; }
        p.sub { margin: 0 0 20px; color: #444; }
        .card { background: #fff; border-radius: 10px; padding: 20px; box-shadow: 0 1px 4px rgba(0,0,0,.08); margin-bottom: 20px; }
        label { display: block; font-weight: 600; margin-bottom: 8px; }
        input[type=file] { width: 100%; padding: 10px; border: 1px dashed #888; border-radius: 8px; background: #fafafa; }
        .opts { margin: 16px 0; display: flex; flex-wrap: wrap; gap: 16px; }
        .opts label { font-weight: normal; display: flex; align-items: center; gap: 8px; margin: 0; }
        button { background: #1565c0; color: #fff; border: none; padding: 12px 20px; font-size: 1rem;
          border-radius: 8px; cursor: pointer; }
        button:disabled { opacity: .6; cursor: wait; }
        button.secondary { background: #555; margin-left: 8px; }
        #status { margin-top: 12px; font-size: .95rem; }
        .summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 12px; margin-bottom: 16px; }
        .stat { background: #eef2f7; padding: 12px; border-radius: 8px; text-align: center; }
        .stat b { display: block; font-size: 1.4rem; }
        .file-row { border: 1px solid #e0e0e0; border-radius: 8px; padding: 14px; margin-bottom: 12px; }
        .file-row.pass { border-left: 4px solid var(--ok); }
        .file-row.fail { border-left: 4px solid var(--bad); }
        .file-row.warn { border-left: 4px solid var(--warn); }
        .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: .8rem; font-weight: 600; }
        .badge.pass { background: #d4edda; color: var(--ok); }
        .badge.fail { background: #f8d7da; color: var(--bad); }
        .issues { margin: 8px 0 0; padding-left: 18px; font-size: .9rem; color: #333; }
        .issues li.critical { color: var(--bad); }
        .issues li.warning { color: var(--warn); }
        pre.json { font-size: 11px; max-height: 200px; overflow: auto; background: #f7f7f7; padding: 10px; border-radius: 6px; }
        a { color: #1565c0; }
      </style>
    </head>
    <body>
      <div class="wrap">
        <h1>Batch PDF test</h1>
        <p class="sub">Upload one or more PDFs via multipart form. Each file is extracted and validated (no Dataverse upload).</p>

        <div class="card">
          <form id="uploadForm" enctype="multipart/form-data">
            <label for="pdfFiles">PDF files</label>
            <input type="file" id="pdfFiles" name="files" accept=".pdf,application/pdf" multiple required>

            <div class="opts">
              <label><input type="checkbox" id="includeCrm" checked> Include CRM JSON in response</label>
              <label><input type="checkbox" id="includeRaw"> Include OCR text preview</label>
            </div>

            <button type="submit" id="submitBtn">Upload &amp; process</button>
            <button type="button" class="secondary" id="clearBtn">Clear results</button>
            <div id="status"></div>
          </form>
        </div>

        <div id="results" class="card" style="display:none">
          <h2 style="margin-top:0">Results</h2>
          <div class="summary" id="summary"></div>
          <div id="fileList"></div>
        </div>

        <p><a href="/">Home</a> &middot; <a href="/docs">API docs</a></p>
      </div>

      <script>
        const form = document.getElementById('uploadForm');
        const statusEl = document.getElementById('status');
        const resultsEl = document.getElementById('results');
        const summaryEl = document.getElementById('summary');
        const fileListEl = document.getElementById('fileList');
        const submitBtn = document.getElementById('submitBtn');

        document.getElementById('clearBtn').onclick = () => {
          resultsEl.style.display = 'none';
          summaryEl.innerHTML = '';
          fileListEl.innerHTML = '';
          statusEl.textContent = '';
        };

        form.onsubmit = async (e) => {
          e.preventDefault();
          const input = document.getElementById('pdfFiles');
          if (!input.files.length) {
            statusEl.textContent = 'Select at least one PDF.';
            return;
          }

          const fd = new FormData();
          for (const f of input.files) {
            fd.append('files', f);
          }

          const params = new URLSearchParams();
          params.set('include_crm_json', document.getElementById('includeCrm').checked);
          params.set('include_raw_text', document.getElementById('includeRaw').checked);

          submitBtn.disabled = true;
          statusEl.textContent = 'Processing ' + input.files.length + ' file(s)... this may take several minutes.';

          try {
            const res = await fetch('/test/pdf/batch?' + params.toString(), {
              method: 'POST',
              body: fd
            });
            const data = await res.json();
            if (!res.ok) {
              statusEl.textContent = 'Error: ' + (data.detail || res.statusText);
              return;
            }
            statusEl.textContent = 'Done in ' + (data.total_processing_ms / 1000).toFixed(1) + 's.';
            renderResults(data);
          } catch (err) {
            statusEl.textContent = 'Request failed: ' + err.message;
          } finally {
            submitBtn.disabled = false;
          }
        };

        function renderResults(data) {
          resultsEl.style.display = 'block';
          summaryEl.innerHTML = [
            stat('Total', data.total),
            stat('Succeeded', data.succeeded),
            stat('Passed', data.passed),
            stat('Failed validation', data.failed_validation),
            stat('Avg score', data.average_score)
          ].join('');

          fileListEl.innerHTML = (data.results || []).map(renderFile).join('');
        }

        function stat(label, value) {
          return '<div class="stat"><b>' + value + '</b>' + label + '</div>';
        }

        function renderFile(item) {
          const cls = !item.success ? 'fail' : (item.passed ? 'pass' : 'warn');
          const badge = !item.success ? 'FAIL' : (item.passed ? 'PASS' : 'REVIEW');
          const issues = (item.validation && item.validation.issues) || [];
          const issueHtml = issues.length
            ? '<ul class="issues">' + issues.map(i =>
                '<li class="' + i.level + '"><b>' + i.level + '</b>: ' + esc(i.message) + '</li>'
              ).join('') + '</ul>'
            : '<p style="color:var(--ok);margin:8px 0 0">No issues reported.</p>';

          const recs = (item.records_summary || []).map(r =>
            '<div style="font-size:.9rem;margin-top:6px">' +
            '<strong>B/L</strong> ' + esc(r.mesco_masterblno || '-') +
            ' &middot; <strong>Cnee</strong> ' + esc(r.mesco_consigneenamecontactno || '-') +
            ' &middot; <strong>Pkgs</strong> ' + esc(r.cr401_totalpackages || '-') +
            ' &middot; <strong>GW</strong> ' + esc(r.cr401_totalgrossweight || '-') +
            '</div>'
          ).join('');

          let crm = '';
          if (item.crm_masters) {
            crm = '<details style="margin-top:8px"><summary>CRM JSON</summary><pre class="json">' +
              esc(JSON.stringify(item.crm_masters, null, 2)) + '</pre></details>';
          }

          return '<div class="file-row ' + cls + '">' +
            '<div><strong>' + esc(item.filename) + '</strong> ' +
            '<span class="badge ' + (item.passed ? 'pass' : 'fail') + '">' + badge + '</span> ' +
            'score ' + item.score + ' &middot; ' + item.record_count + ' record(s) &middot; ' +
            item.processing_ms + ' ms</div>' +
            (item.error ? '<p style="color:var(--bad)">' + esc(item.error) + '</p>' : '') +
            recs + issueHtml + crm + '</div>';
        }

        function esc(s) {
          return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        }
      </script>
    </body>
    </html>
    """


@app.get("/health")
async def health():
    import os

    from dotenv import load_dotenv

    load_dotenv()
    meta = llm_meta()
    return {
        "status": "ok",
        "version": "4.0.0",
        **meta,
        "azure_in_use": not uses_gemini() and not uses_puter(),
        "puter_in_use": uses_puter(),
        "azure_openai_configured": bool(
            settings.azure_openai_endpoint and settings.azure_openai_api_key
        ),
        "gemini_configured": bool(settings.gemini_api_key),
        "azure_openai_deployment": settings.azure_openai_deployment or None,
        "default_puter_model": settings.puter_model,
        "default_gemini_model": settings.gemini_model,
        "gemini_models": list(GEMINI_MODELS),
        "dataverse_configured": bool(
            os.environ.get("AZURE_APP_API_URL")
            and os.environ.get("TENANT_ID")
            and os.environ.get("CLIENT_ID")
            and os.environ.get("CLIENT_SECRET")
        ),
    }


@app.get("/business-rules", tags=["Extraction"])
async def business_rules_status():
    """Describe toggleable CRM business rules (env + per-request override)."""
    from custom_business_rules import custom_rules_enabled

    return {
        "enabled_by_default": custom_rules_enabled(),
        "env_var": "CUSTOM_BUSINESS_RULES_ENABLED",
        "request_param": "apply_custom_rules",
        "rules": [
            "Prepaid → Booking Term Freehand, Freight Payable At Origin",
            "Collect → Booking Term Nomination, Freight Payable At Destination",
            "Load type FCL vs LCL from document meaning (consolidation/CFS/manifest)",
            "LCL house operations: Total TEUs = 0",
            "Multi-house master: totals = sum of house rows",
        ],
    }


@app.get("/llm/models", tags=["Extraction"])
async def list_llm_models():
    """Gemini model ids available for per-request selection (same ids as Puter.js)."""
    return {
        "providers": ["puter", "azure", "gemini"],
        "default_provider": (settings.llm_provider or "azure").strip().lower(),
        "default_puter_model": settings.puter_model,
        "default_gemini_model": settings.gemini_model,
        "default_azure_deployment": settings.azure_openai_deployment,
        "gemini_models": list(GEMINI_MODELS),
        "puter_note": "Puter.js runs in the browser. Use /puter for the default no-key Gemini extraction flow.",
    }


class CrmExtractRequest(BaseModel):
    crm_json: Dict[str, Any]


class ExtractRequest(BaseModel):
    ocr_text: str
    bl_type: BlTypeQuery = BlTypeQuery.master
    llm_provider: Optional[LlmProviderQuery] = None
    llm_model: Optional[str] = None


class PuterFormatRequest(BaseModel):
    puter_payload: Dict[str, Any]
    raw_text: Optional[str] = ""
    bl_type: BlTypeQuery = BlTypeQuery.master
    post_to_dataverse: bool = True
    llm_model: Optional[str] = None
    visual_page_count: Optional[int] = None
    pdf_page_count: Optional[int] = None


class ExtractResponse(BaseModel):
    success: bool
    data: Optional[Dict[str, Any]] = None
    house_data: Optional[Dict[str, Any]] = None
    records: Optional[List[Dict[str, Any]]] = None
    error: Optional[str] = None
    raw_text: Optional[str] = None
    extraction_quality: Optional[Dict[str, Any]] = None
    dataverse_result: Optional[Dict[str, Any]] = None
    dataverse_error: Optional[str] = None


class BatchPdfTestResponse(BaseModel):
    total: int
    succeeded: int
    failed: int
    passed: int
    failed_validation: int
    average_score: float
    total_processing_ms: int
    results: List[Dict[str, Any]]


def upload_crm_json(crm_data: Dict[str, Any]) -> Dict[str, Any]:
    from dataverse_uploader import upload_crm_json as _upload_crm_json

    return _upload_crm_json(crm_data)


@app.post("/extract/crm", response_model=ExtractResponse)
async def extract_crm(request: CrmExtractRequest):
    """Extract B/L records from a Dynamics CRM mesco_operation JSON payload."""
    try:
        crm_data = request.crm_json
        records = map_crm_operation_to_records(crm_data)
        if not records:
            return ExtractResponse(success=False, error="No records could be generated from the CRM data.")

        validated_records = []
        for rec in records:
            rec_text = f"CRM Operation: {rec.get('_master_code') or 'N/A'} / House: {rec.get('_house_code') or 'N/A'}"
            validated = validate_and_correct(rec, rec_text)
            validated_records.append(validated)

        extraction_quality = {
            "source": "crm_operation_json",
            "record_count": len(validated_records),
            "master_code": crm_data.get("mesco_code"),
            "master_bl": crm_data.get("mesco_masterblno"),
        }

        return ExtractResponse(
            success=True,
            records=validated_records,
            extraction_quality=extraction_quality,
        )
    except Exception as exc:
        return ExtractResponse(success=False, error=str(exc))


class DataverseUploadResponse(BaseModel):
    success: bool
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


@app.post(
    "/upload/dataverse",
    response_model=DataverseUploadResponse,
    summary="Upload CRM JSON file to Dynamics 365 Dataverse",
    description="Upload a CRM JSON file (previously downloaded from /extract/pdf?download=true or /extract/excel?download=true) to Dynamics 365 Dataverse. Creates the full hierarchy: master operation, houses, containers, and cargo.",
)
async def upload_to_dataverse(
    file: UploadFile = File(..., description="CRM JSON file (the file downloaded with ?download=true)"),
    bl_type: BlTypeQuery = Query(
        BlTypeQuery.master,
        description="B/L type for the created operation: master or house (sets mesco_bltype on upload)",
    ),
    apply_custom_rules: bool = Query(
        True,
        description="Apply CRM business rules (freight→booking, load type, LCL TEUs, house totals)",
    ),
):
    try:
        content = await file.read()
        crm_data = json.loads(content.decode("utf-8"))
        from custom_business_rules import prepare_crm_payload_for_upload, use_custom_rules

        with use_custom_rules(apply_custom_rules):
            if isinstance(crm_data, dict):
                masters = crm_data.get("masters")
                if isinstance(masters, list):
                    for m in masters:
                        if isinstance(m, dict):
                            apply_bl_type_to_crm_payload(m, bl_type.value)
                            prepare_crm_payload_for_upload(m)
                else:
                    apply_bl_type_to_crm_payload(crm_data, bl_type.value)
                    prepare_crm_payload_for_upload(crm_data)
            result = upload_crm_json(crm_data)
        return DataverseUploadResponse(success=True, result=result)
    except json.JSONDecodeError:
        return DataverseUploadResponse(success=False, error="Invalid JSON file.")
    except Exception as exc:
        return DataverseUploadResponse(success=False, error=str(exc))


@app.post(
    "/upload/dataverse/json",
    response_model=DataverseUploadResponse,
    summary="Upload CRM JSON payload to Dynamics 365 Dataverse (JSON body)",
    description="Send the CRM JSON structure directly as the request body to upload to Dynamics 365 Dataverse. The body should be the same structure as the 'data' field returned by /extract/pdf or /extract/excel.",
)
async def upload_to_dataverse_json(
    payload: Dict[str, Any] = Body(..., description="CRM JSON structure (the 'data' field from extract endpoints)"),
    bl_type: BlTypeQuery = Query(
        BlTypeQuery.master,
        description="B/L type for the created operation: master or house (sets mesco_bltype on upload)",
    ),
    apply_custom_rules: bool = Query(
        True,
        description="Apply CRM business rules (freight→booking, load type, LCL TEUs, house totals)",
    ),
):
    try:
        from custom_business_rules import prepare_crm_payload_for_upload, use_custom_rules

        with use_custom_rules(apply_custom_rules):
            if isinstance(payload, dict):
                apply_bl_type_to_crm_payload(payload, bl_type.value)
                prepare_crm_payload_for_upload(payload)
            result = upload_crm_json(payload)
        return DataverseUploadResponse(success=True, result=result)
    except Exception as exc:
        return DataverseUploadResponse(success=False, error=str(exc))


def process_single_record(
    record_text: str,
    source_info: str,
    *,
    file_bytes: Optional[bytes] = None,
    filename: Optional[str] = None,
) -> Dict[str, Any]:
    """Process a single record through intelligent AI extraction and validation."""
    try:
        parse_result = parse_document_intelligently(
            record_text,
            file_bytes=file_bytes,
            filename=filename,
        )
        if not parse_result.records:
            raise ValueError("No records extracted from spreadsheet row text.")
        validated = parse_result.records[0]
        validated["_source_info"] = source_info
        validated["extraction_method"] = validated.get("extraction_method") or f"{llm_extraction_prefix()}_intelligent_record"
        return validated
    except Exception as exc:
        return {"_source_info": source_info, "_error": str(exc)}


def process_workbook_with_azure(
    raw_text: str,
    extracted: Dict[str, Any],
    extraction_quality: Dict[str, Any],
    *,
    file_bytes: Optional[bytes] = None,
    filename: Optional[str] = None,
) -> Dict[str, Any]:
    """Intelligent parse for unknown workbook/PDF layouts; returns first validated record."""
    parse_result = parse_document_intelligently(
        raw_text,
        extracted,
        file_bytes=file_bytes,
        filename=filename,
    )
    if not parse_result.records:
        raise ValueError("Intelligent parser returned no B/L records.")
    validated = parse_result.records[0]
    validated["extraction_method"] = validated.get("extraction_method") or f"{llm_extraction_prefix()}_intelligent_workbook"
    validated["source_extraction_method"] = extracted.get("method", "unknown")
    validated["extraction_quality"] = {**extraction_quality, **parse_result.quality}
    validated["_document_layout"] = parse_result.document_layout
    if len(parse_result.records) > 1:
        validated["_additional_records"] = parse_result.records[1:]
    return validated


def _should_use_gemini_workbook_llm(records: List[Dict[str, Any]], raw_text: str) -> bool:
    """Prefer one whole-workbook Gemini call for long manifests instead of per-row LLM."""
    if not uses_gemini():
        return False
    if len(records) >= settings.gemini_workbook_llm_min_rows:
        return True
    if len(raw_text or "") >= 25_000:
        return True
    upper = (raw_text or "").upper()
    manifest_markers = ("MANIFEST", "H/BL", "HOUSE B/L", "LOADING SHEET", "PROXY BILL")
    if any(m in upper for m in manifest_markers) and len(records) >= 2:
        return True
    return False


def _response_from_intelligent_parse(
    parse_result,
    raw_text: str,
    extraction_quality: Dict[str, Any],
    post_to_dataverse: bool,
    download: bool,
    bl_type: BlTypeQuery,
    *,
    routing_policy: str = "gemini_intelligent_workbook",
) -> Any:
    """Build ExtractResponse from parse_document_intelligently output."""
    extraction_quality.update(parse_result.quality)
    if parse_result.azure_warnings:
        extraction_quality["azure_warnings"] = parse_result.azure_warnings
    records = parse_result.records
    layout = parse_result.document_layout or "unknown"
    extraction_quality.setdefault("record_routing", {})
    extraction_quality["record_routing"].update({
        "policy": routing_policy,
        "mode": layout,
        "azure_fallback": len(records),
    })

    house_recs = [r for r in records if r.get("mesco_houseblno")]
    master_recs = [
        r for r in records
        if r.get("mesco_masterblno") and not r.get("mesco_houseblno")
    ]

    if layout in ("manifest", "master_with_houses") and len(house_recs) >= 2:
        master_record = master_recs[0] if master_recs else None
        crm_output = records_to_master_json(house_recs, master_record=master_record)
        house_output = records_to_house_json(house_recs, master_record=master_record)
        extraction_quality["consolidated_house_count"] = len(house_recs)
        return _build_response(
            crm_output, raw_text, extraction_quality,
            post_to_dataverse, download, house_output, bl_type=bl_type,
        )

    if len(records) >= 2 and layout == "multi_bl_pages":
        crm_masters = [records_to_master_json([v]) for v in records]
        return _build_response(
            crm_masters[0], raw_text, extraction_quality,
            post_to_dataverse, download, house_output={"value": []},
            crm_records=crm_masters, bl_type=bl_type,
        )

    if len(records) >= 2:
        crm_output = records_to_master_json(records)
        house_output = records_to_house_json(records)
    else:
        crm_output = records_to_master_json(records)
        house_output = records_to_house_json(records)
    return _build_response(
        crm_output, raw_text, extraction_quality,
        post_to_dataverse, download, house_output, bl_type=bl_type,
    )


def _drop_empty_values(value: Any) -> Any:
    """Remove null/empty fields from direct spreadsheet records."""
    if isinstance(value, dict):
        cleaned = {k: _drop_empty_values(v) for k, v in value.items()}
        return {
            k: v
            for k, v in cleaned.items()
            if v is not None and v != "" and v != [] and v != {}
        }
    if isinstance(value, list):
        return [
            cleaned
            for item in value
            if (cleaned := _drop_empty_values(item)) not in (None, "", [], {})
        ]
    return value


def _has_value(record: Dict[str, Any], key: str) -> bool:
    value = record.get(key)
    return value is not None and value != "" and value != [] and value != {}


def _direct_record_confidence(record: Dict[str, Any]) -> Dict[str, Any]:
    """Decide whether deterministic spreadsheet extraction is safe enough to use."""
    method = record.get("extraction_method")
    has_hbl = _has_value(record, "mesco_houseblno") or _has_value(record, "unique_key")

    if method == "spreadsheet_direct_proxy_bill":
        required = ["mesco_masterblno", "mesco_houseblno", "container_number", "financial_processing"]
        present = [key for key in required if _has_value(record, key)]
        accepted = has_hbl and len(present) >= 3
        return {
            "accepted": accepted,
            "score": len(present),
            "required_present": present,
            "required_missing": [key for key in required if key not in present],
            "reason": "recognized_proxy_bill" if accepted else "proxy_bill_low_confidence",
        }

    if method == "spreadsheet_direct_manifest":
        useful = [
            "mesco_shippernamecontactno",
            "mesco_consigneenamecontactno",
            "mesco_origin",
            "mesco_destination",
            "cr401_totalpackages",
            "cr401_totalgrossweight",
            "cr401_totalvolume",
            "mesco_acidnumber",
            "mesco_incoterm",
            "mesco_pcfreightterm",
            "hbl_type",
            "cargo_value",
            "mesco_hscode",
        ]
        present = [key for key in useful if _has_value(record, key)]
        accepted = has_hbl and len(present) >= 4
        return {
            "accepted": accepted,
            "score": len(present),
            "required_present": present,
            "required_missing": [key for key in useful if key not in present],
            "reason": "recognized_manifest_or_loading_sheet" if accepted else "manifest_low_confidence",
        }

    return {
        "accepted": False,
        "score": 0,
        "required_present": [],
        "required_missing": [],
        "reason": "unknown_spreadsheet_layout",
    }


def _record_hbl(rec: Dict[str, Any]) -> Optional[str]:
    values = rec.get("values_by_header", {}) or {}
    for key in (
        "mesco_houseblno",
        "hbl_no",
        "HB/L NO.",
        "HB/L NO",
        "HB/L",
        "HBL NO.",
        "HBL NO",
        "H/BL No.",
        "H/BL Nos.",
        "HOUSE B/L",
    ):
        value = rec.get(key) or values.get(key)
        if value:
            return str(value).strip()
    return None


def _record_source_info(rec: Dict[str, Any]) -> str:
    hbl = _record_hbl(rec) or "N/A"
    return f"Sheet: {rec.get('sheet_name')}, Row: {rec.get('source_row')}, HBL: {hbl}"


def _normalize_manifest_key(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def _manifest_value(values: Dict[str, Any], *keys: str) -> Optional[str]:
    normalized = {_normalize_manifest_key(k): v for k, v in values.items()}
    for key in keys:
        value = normalized.get(_normalize_manifest_key(key))
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _parse_record_cell_map(record_text: str) -> Dict[str, str]:
    cells: Dict[str, str] = {}
    for cell, value in re.findall(r"\b([A-Z]{1,3}\d+)=([^|\n]+)", record_text or ""):
        value = value.strip()
        value = re.split(
            r"\s+(?:\[[A-Z ]+\]|[A-Za-z0-9()_\- /]+(?:ROW|CELLS)\s+\d+:)",
            value,
            maxsplit=1,
        )[0].strip()
        if value:
            cells[cell.upper()] = value
    return cells


def _cell_ref_parts(ref: str) -> Optional[tuple[str, int]]:
    match = re.fullmatch(r"([A-Z]+)(\d+)", ref.upper())
    if not match:
        return None
    return match.group(1), int(match.group(2))


def _value_below_label(cells: Dict[str, str], *label_patterns: str) -> Optional[str]:
    compiled = [re.compile(pattern, re.I) for pattern in label_patterns]
    for ref, value in cells.items():
        if not any(pattern.search(value or "") for pattern in compiled):
            continue
        parts = _cell_ref_parts(ref)
        if not parts:
            continue
        col, row = parts
        below = cells.get(f"{col}{row + 1}")
        if below and below.strip():
            return below.strip()
    return None


def _excel_serial_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        serial = float(text)
        if 30000 <= serial <= 60000:
            return (datetime(1899, 12, 30) + timedelta(days=serial)).strftime("%Y-%m-%d")
    return text


def _parse_header_value_map(cells: Dict[str, str], header_row: int = 4, data_row: int = 5) -> Dict[str, str]:
    """Read column header labels from header_row and their values from data_row."""
    hdr_to_val: Dict[str, str] = {}
    col_letters: set[str] = set()
    for ref, val in cells.items():
        parts = _cell_ref_parts(ref)
        if parts and parts[1] == header_row:
            col_letters.add(parts[0])
    for col in sorted(col_letters):
        header = cells.get(f"{col}{header_row}")
        if header:
            value = cells.get(f"{col}{data_row}")
            if value:
                hdr_to_val[header.strip()] = value.strip()
    return hdr_to_val


def _is_address_text(text: Optional[str]) -> bool:
    """Check if text looks like an address rather than vessel/voyage."""
    if not text:
        return True
    upper = text.upper()
    address_indicators = [
        "HONG KONG", "ROAD", "STREET", "LANE", "DRIVE", "AVENUE",
        " BUILDING", "FLOOR", "SUITE", "P.O. BOX", "PO BOX",
        "C/O ", "C/O", "ATTN:", "ATTENTION",
    ]
    if any(ind in upper for ind in address_indicators):
        return True
    if len(text) > 40:
        return True
    if re.match(r"^\d{6,}", text):
        return True
    return False


def _parse_manifest_context(record_text: str) -> Dict[str, Optional[str]]:
    cells = _parse_record_cell_map(record_text)
    header_map = _parse_header_value_map(cells, header_row=4, data_row=5)

    vessel = voyage = None
    vsl_match = re.search(r"\b([A-Z][A-Z ]{2,50})/([A-Z0-9]{4,12})\b", record_text.upper())
    vsl_voy = cells.get("F3")
    if vsl_match:
        v = vsl_match.group(1).strip()
        vo = vsl_match.group(2).strip()
        if not _is_address_text(v) and not _is_address_text(vo):
            vessel = v
            voyage = vo
    elif vsl_voy and "/" in vsl_voy:
        v, vo = (part.strip() for part in vsl_voy.split("/", 1))
        if not _is_address_text(v) and not _is_address_text(vo):
            vessel = v
            voyage = vo

    container = seal = None
    container_match = re.search(r"\b([A-Z]{4}\d{7})/([A-Z0-9]{4,20})\b", record_text.upper())
    if container_match:
        container = container_match.group(1)
        seal = container_match.group(2)
    container_seal = cells.get("M3")
    if not container and container_seal:
        parts = [part.strip() for part in container_seal.split("/", 1)]
        container = parts[0] if parts else None
        seal = parts[1] if len(parts) > 1 else None

    job_match = re.search(r"\b(ALY[A-Z0-9]{6,})\b", record_text.upper())
    mbl_match = re.search(r"\b([A-Z]{4}\d{9,12})\b", record_text.upper())

    # Use header_map to get values by column label instead of hardcoded positions
    etd_value = header_map.get("ETD") or cells.get("E5")
    container_type = None
    if not container_type:
        type_match = re.search(r"\b\d+X\d{2}[A-Z]{0,3}\b", record_text.upper())
        container_type = type_match.group(0) if type_match else None

    mbl_no = (mbl_match.group(1) if mbl_match else None)
    # Try M/BL header label if present in row 3
    mbl_val = cells.get("I3")
    if mbl_val and mbl_val.upper() != "M/BL" and not mbl_no:
        mbl_no = mbl_val

    agent = cells.get("M4")
    if agent:
        agent = re.split(r"\s+(?:\[|[A-Z][A-Z0-9 ()/\-]*\s+ROW\s+\d+:)", agent, 1)[0].strip()

    consol_job_no = cells.get("C3") or (job_match.group(1) if job_match else None)
    job_no = _value_below_label(cells, r"JOB\s*NO") or header_map.get("JOB NO.")
    # An LCL loading sheet may have no ocean M/BL number — only a JOB NO that
    # identifies the consolidation. Use it as the master B/L so the master
    # operation has a B/L and every house links back to it via
    # mesco_masterbllinkno.
    master_bl = mbl_no or consol_job_no or job_no

    return {
        "consol_job_no": consol_job_no,
        "mesco_masterblno": master_bl,
        "mesco_vessel": vessel,
        "mesco_voytruckno": voyage,
        "pod": cells.get("J3") or _value_below_label(cells, r"POD", r"PORT\s+OF\s+DISCHARG") or header_map.get("Port Of Discharging"),
        "origin": _value_below_label(cells, r"PORT\s+OF\s+LOADING", r"PLACE\s+OF\s+RECEIPT", r"ORIGIN") or header_map.get("Port Of Loading"),
        "container_number": container,
        "seal_number": seal,
        "mesco_containertype": container_type,
        "mesco_etdorigin": _excel_serial_date(etd_value),
        "carrier": _value_below_label(cells, r"CARRIER") or header_map.get("Carrier"),
        "job_no": job_no,
        "mbl_shipper": _value_below_label(cells, r"M/?BL\s+SHIPPER") or header_map.get("M/BL Shipper"),
        "delivery_agent": _value_below_label(cells, r"DELIVERY\s+AGENT") or header_map.get("Delivery Agent"),
        "mbl_acid": _value_below_label(cells, r"M/?BL\s+ACID") or header_map.get("M/BL ACID"),
        "agent": agent or _value_below_label(cells, r"AGENT"),
        "schedule": cells.get("A4"),
    }


def _is_manifest_record(rec: Dict[str, Any]) -> bool:
    values = rec.get("values_by_header", {}) or {}
    keys = {_normalize_manifest_key(k) for k in values}
    return (
        bool({"HBLNO", "HBLNOS", "HB/LNO"} & keys)
        or "HBLNO" in keys
        or "HBLTYPE" in keys
    ) and bool({
        "SHIPPER", "CONSIGNEE", "CNEE", "HSCODE", "CARGOVALUE", "PKGS",
        "NOSOFPACKAGES", "GROSSWEIGHTKG", "MEASURMENTSCBM", "MEASUREMENTSCBM",
        "PLACEOFDELIVERY", "PLACEOFRECEIPT", "FREIGHT",
    } & keys)


def _direct_manifest_record(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    values = rec.get("values_by_header", {}) or {}
    if not _is_manifest_record(rec):
        return None

    context = _parse_manifest_context(rec.get("text", ""))
    hbl = _record_hbl(rec) or _manifest_value(values, "HB/L NO.", "HBL NO.", "H/BL No.")
    origin = _manifest_value(values, "PLACE OF RECEIPT", "ORIGIN", "POL") or context.get("origin")
    destination = (
        _manifest_value(values, "DESTINATION", "PLACE OF DELIVERY")
        or _manifest_value(values, "POD")
        or context.get("pod")
    )
    pod = _manifest_value(values, "POD") or context.get("pod")
    freight_text = (_manifest_value(values, "FREIGHT") or "").upper()
    freight_term = None
    if "COLLECT" in freight_text:
        freight_term = "COLLECT"
    elif "PREPAID" in freight_text:
        freight_term = "PREPAID"

    hbl_type = _manifest_value(values, "HBL'S TYPE", "HBL TYPE")
    remarks = _manifest_value(values, "REMARKS", "STATUS")
    package_count = _manifest_value(values, "PKGS", "NOS. OF PACKAGES", "NOS OF PACKAGES")
    gross_weight = _manifest_value(values, "GW", "GROSS WEIGHT (KG)", "GROSS WEIGHT", "WEIGHT")
    measurement = _manifest_value(values, "CBM", "MEASURMENTS (CBM)", "MEASUREMENTS (CBM)", "MEASUREMENT (CBM)")
    delivery_term = _manifest_value(values, "DELIVERY TERM", "TERM")
    cargo_type = _manifest_value(values, "CARGO TYPE")
    hscode = _manifest_value(values, "HS CODE")

    container = {
        "container_number": context.get("container_number"),
        "seal_number": context.get("seal_number"),
        "container_type": context.get("mesco_containertype"),
        "packages": package_count,
        "gross_weight_kg": gross_weight,
        "measurement_cbm": measurement,
    }

    # Clean vessel/voyage — manifest Excel sheets often have no vessel info,
    # and the regex may pick up false matches from address text
    vessel = context.get("mesco_vessel")
    voyage = context.get("mesco_voytruckno")
    if vessel and _is_address_text(vessel):
        vessel = None
    if voyage and _is_address_text(voyage):
        voyage = None

    # Determine TELEX release from HBL TYPE or REMARKS column
    is_telex = (
        (hbl_type and "TELEX" in hbl_type.upper())
        or (remarks and "TELEX" in remarks.upper())
    )
    is_original = hbl_type and "ORIGINAL" in hbl_type.upper()

    output: Dict[str, Any] = {
        "document_type": "Bill of Lading",
        "record_index": rec.get("record_index"),
        "sheet_name": rec.get("sheet_name"),
        "source_row": rec.get("source_row"),
        "mesco_masterblno": context.get("mesco_masterblno"),
        "mesco_houseblno": hbl,
        "mesco_bookingnumber": context.get("consol_job_no") or context.get("job_no"),
        "mesco_acidnumber": _manifest_value(values, "H/BL ACID", "HBL ACID"),
        "mesco_customerreference": _manifest_value(values, "REF NO"),
        "mesco_shippernamecontactno": _manifest_value(values, "SHIPPER"),
        "mesco_consigneenamecontactno": _manifest_value(values, "CNEE", "CONSIGNEE"),
        "mesco_vessel": vessel,
        "mesco_voytruckno": voyage,
        "mesco_origin": origin,
        "mesco_destination": destination,
        "mesco_transhipmentport": pod,
        "cr401_totalpackages": package_count,
        "package_type": _manifest_value(values, "PACKAGES", "PACKING"),
        "cr401_totalgrossweight": gross_weight,
        "cr401_totalvolume": measurement,
        "mesco_containertype": context.get("mesco_containertype"),
        "mesco_pcfreightterm": freight_term or _manifest_value(values, "FREIGHT"),
        "mesco_incoterm": delivery_term,
        "mesco_hscode": hscode,
        "cargo_value": _manifest_value(values, "CARGO VALUE"),
        "consignee_contact_details": _manifest_value(values, "CNEE'S CONTACT DETAILS"),
        "hbl_type": hbl_type,
        "nomination_term": _manifest_value(values, "TERM (NOMINATED / FREE HAND)", "TERM NOMINATED FREE HAND"),
        "delivery_term": delivery_term,
        "shipment_status": _manifest_value(values, "STATUS"),
        "cargo_type": cargo_type,
        "rate": _manifest_value(values, "RATE"),
        "carrier": context.get("carrier"),
        # Master-context fields (stored as meta for records_to_master_json to use)
        "_mbl_shipper": context.get("mbl_shipper"),
        "_mbl_consignee": context.get("delivery_agent"),
        "_mbl_acid": context.get("mbl_acid"),
        "_mbl_bookingno": context.get("job_no"),
        "_mbl_masterblno": context.get("mesco_masterblno"),
        "mbl_shipper": context.get("mbl_shipper"),
        "delivery_agent": context.get("delivery_agent"),
        "mbl_acid": context.get("mbl_acid"),
        "schedule": context.get("schedule"),
        "mesco_bltype": 886150001 if is_original else None,
        "mesco_telexrelease": is_telex,
        "mesco_transporttype": 300000000,
        "mesco_loadtype": 300000001,
        "mesco_direction": 300000000,
        "mesco_etdorigin": context.get("mesco_etdorigin"),
        "agent": context.get("agent"),
        "container_number": context.get("container_number"),
        "seal_number": context.get("seal_number"),
        "containers": [container],
        "manifest_values": values,
        "extraction_method": "spreadsheet_direct_manifest",
        "unique_key": hbl,
        "_source_info": _record_source_info(rec),
        "confidence": {
            "post_validation": "not_needed",
            "source": "spreadsheet_extractor",
            "house_bl_rule": "accepted" if hbl else "missing",
            "container_number_rule": "accepted" if context.get("container_number") else "missing",
        },
    }
    return _drop_empty_values(output)


def _direct_spreadsheet_record(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return deterministic output for records parsed directly from spreadsheet layouts."""
    mesco_payload = rec.get("mesco_payload")
    financial_processing = rec.get("financial_processing")
    if not isinstance(mesco_payload, dict):
        return _direct_manifest_record(rec)

    hbl = _record_hbl(rec)
    output: Dict[str, Any] = dict(mesco_payload)
    if hbl:
        output["mesco_houseblno"] = hbl

    output.update({
        "record_index": rec.get("record_index"),
        "sheet_name": rec.get("sheet_name"),
        "source_row": rec.get("source_row"),
        "unique_key": rec.get("unique_key") or hbl,
        "cargo_type": rec.get("cargo_type") or output.get("mesco_incoterm"),
        "extraction_method": "spreadsheet_direct_proxy_bill",
        "_source_info": _record_source_info(rec),
        "confidence": {
            "post_validation": "not_needed",
            "source": "spreadsheet_extractor",
            "house_bl_rule": "accepted" if hbl else "missing",
            "container_number_rule": "accepted" if output.get("container_number") else "missing",
        },
    })

    if isinstance(financial_processing, dict):
        output["spreadsheet_record"] = {
            key: value
            for key, value in financial_processing.items()
            if key not in {"debit", "credit"}
        }
        output["financial_processing"] = {
            "debit": financial_processing.get("debit", {}),
            "credit": financial_processing.get("credit", {}),
        }

    return _drop_empty_values(output)


@app.post(
    "/extract/file",
    response_model=ExtractResponse,
    summary="Extract from any supported file (PDF, XLSX, XLS, CSV)",
    description=(
        "Upload a PDF, Excel, or CSV file to extract B/L data. "
        "Choose **bl_type** (master or house) in the form below."
    ),
    tags=["Extraction"],
)
async def extract_file(
    request: Request,
    file: UploadFile = File(..., description="PDF, XLSX, XLS, or CSV file"),
    bl_type: BlTypeQuery = Form(
        BlTypeQuery.master,
        description="B/L type: master (886150001) or house (886150002)",
    ),
    llm_provider: Optional[LlmProviderQuery] = Form(
        None,
        description="AI backend: puter (browser page), azure, or gemini (defaults to LLM_PROVIDER in .env)",
    ),
    llm_model: Optional[GeminiModelQuery] = Form(
        None,
        description="Gemini model id when llm_provider=gemini (same list as Puter.js free tier)",
    ),
    post_to_dataverse: bool = Form(
        True,
        description="Automatically upload extracted data to Dynamics 365 Dataverse",
    ),
    download: bool = Form(
        False,
        description="Download the CRM JSON as a file instead of returning the normal response",
    ),
    apply_custom_rules: bool = Form(
        True,
        description="Apply CRM business rules (freight→booking, load type, LCL TEUs, house totals)",
    ),
):
    provider_val = llm_provider.value if llm_provider else None
    model_val = llm_model.value if llm_model else None
    if normalize_llm_provider(provider_val or settings.llm_provider) == "puter":
        return ExtractResponse(
            success=False,
            error=(
                "Puter is the default AI extractor and runs in the browser. "
                "Open /puter to upload the PDF with Puter.js, or submit this "
                "API request with llm_provider=azure or llm_provider=gemini for server-side extraction."
            ),
        )
    try:
        validate_llm_request(provider_val, model_val)
    except ValueError as exc:
        return ExtractResponse(success=False, error=str(exc))

    audit_id: Optional[str] = None
    audit_started_at = datetime.now(timezone.utc)
    try:
        file_bytes = await file.read()
        await file.seek(0)
        audit_id = audit_store.start_upload(
            request=request,
            file=file,
            file_bytes=file_bytes,
            bl_type=bl_type,
            post_to_dataverse=post_to_dataverse,
            llm_provider=llm_provider,
            llm_model=llm_model,
            apply_custom_rules=apply_custom_rules,
        )
    except Exception as audit_exc:
        logger.warning("Upload audit start failed: %s", audit_exc)

    try:
        with llm_request_overrides(provider_val, model_val):
            result = await _extract_file_inner(
                file,
                bl_type=bl_type,
                post_to_dataverse=post_to_dataverse,
                download=download,
                apply_custom_rules=apply_custom_rules,
            )
            audit_store.finish_upload(audit_id, result, started_at=audit_started_at)
            return result
    except Exception as exc:
        audit_store.fail_upload(audit_id, exc, started_at=audit_started_at)
        return ExtractResponse(success=False, error=str(exc))


async def _extract_file_inner(
    file: UploadFile,
    *,
    bl_type: BlTypeQuery,
    post_to_dataverse: bool,
    download: bool,
    apply_custom_rules: bool = True,
):
    from custom_business_rules import use_custom_rules

    try:
        with use_custom_rules(apply_custom_rules):
            return await _extract_file_inner_impl(
                file,
                bl_type=bl_type,
                post_to_dataverse=post_to_dataverse,
                download=download,
            )
    except Exception as exc:
        return ExtractResponse(success=False, error=str(exc))


async def _extract_file_inner_impl(
    file: UploadFile,
    *,
    bl_type: BlTypeQuery,
    post_to_dataverse: bool,
    download: bool,
):
    try:
        file_bytes = await file.read()
        extracted = extract_document_text_professionally(file_bytes, file.filename)
        
        raw_text = extracted.get("text", "")
        extraction_quality = extracted.get("quality", {})
        records = extracted.get("records", [])

        if not raw_text.strip():
            return ExtractResponse(success=False, error="No text extracted from file.")

        extracted["filename"] = file.filename

        # If spreadsheet has individual records, process each separately
        if records and len(records) > 0:
            # Gemini: read the whole workbook natively (xlsx layout + long text).
            if _should_use_gemini_workbook_llm(records, raw_text):
                parse_result = parse_document_intelligently(
                    raw_text,
                    extracted,
                    file_bytes=file_bytes,
                    filename=file.filename,
                )
                if parse_result.records:
                    extraction_quality["record_routing"] = {
                        "direct": 0,
                        "azure_fallback": len(parse_result.records),
                        "skipped": 0,
                        "policy": "gemini_whole_workbook",
                        "mode": "native_spreadsheet_or_chunked_text",
                    }
                    return _response_from_intelligent_parse(
                        parse_result,
                        raw_text,
                        extraction_quality,
                        post_to_dataverse,
                        download,
                        bl_type,
                        routing_policy="gemini_whole_workbook",
                    )

            extracted_records = []
            route_counts = {
                "direct": 0,
                "azure_fallback": 0,
                "skipped": 0,
                "policy": "direct_when_confident_else_azure",
            }
            for rec in records:
                direct_result = _direct_spreadsheet_record(rec)
                if direct_result:
                    direct_confidence = _direct_record_confidence(direct_result)
                    direct_result.setdefault("confidence", {})
                    direct_result["confidence"]["direct_extraction"] = direct_confidence
                    if direct_confidence["accepted"]:
                        route_counts["direct"] += 1
                        validated = validate_and_correct(direct_result, rec.get("text", ""))
                        # Spreadsheet manifest records don't have vessel/voyage;
                        # the raw_text may contain addresses that regex falsely matches.
                        if validated.get("mesco_vessel") and _is_address_text(validated.get("mesco_vessel")):
                            validated["mesco_vessel"] = None
                        if validated.get("mesco_voytruckno") and _is_address_text(validated.get("mesco_voytruckno")):
                            validated["mesco_voytruckno"] = None
                        extracted_records.append(validated)
                        continue

                record_text = rec.get("text", "")
                source_info = _record_source_info(rec)
                
                if record_text:
                    result = process_single_record(
                        record_text,
                        source_info,
                        file_bytes=file_bytes,
                        filename=file.filename,
                    )
                    result["_routing"] = {
                        "route": "azure_fallback",
                        "reason": (
                            _direct_record_confidence(direct_result)["reason"]
                            if direct_result else "unknown_spreadsheet_layout"
                        ),
                    }
                    route_counts["azure_fallback"] += 1
                    extracted_records.append(result)
                else:
                    route_counts["skipped"] += 1

            extraction_quality["record_routing"] = route_counts
            if not extracted_records:
                parse_result = parse_document_intelligently(
                    raw_text,
                    extracted,
                    file_bytes=file_bytes,
                    filename=file.filename,
                )
                if not parse_result.records:
                    return ExtractResponse(
                        success=False,
                        error="No processable records in spreadsheet/workbook.",
                        extraction_quality=extraction_quality,
                    )
                return _response_from_intelligent_parse(
                    parse_result,
                    raw_text,
                    extraction_quality,
                    post_to_dataverse,
                    download,
                    bl_type,
                    routing_policy="intelligent_workbook_fallback",
                )

            crm_output = records_to_master_json(extracted_records)
            house_output = records_to_house_json(extracted_records)
            return _build_response(crm_output, raw_text, extraction_quality, post_to_dataverse, download, house_output, bl_type=bl_type)
        
        if is_consolidation_sea_waybill(raw_text):
            sea_waybill = parse_consolidation_sea_waybill(raw_text)
            if sea_waybill:
                validated = validate_and_correct(sea_waybill, raw_text)
                extraction_quality["document_type_detected"] = "consolidation_sea_waybill_pdf"
                extraction_quality["record_routing"] = {
                    "direct": 1,
                    "azure_fallback": 0,
                    "skipped": 0,
                    "policy": "pdf_consolidation_sea_waybill",
                    "mode": "master_with_attached_list",
                }
                house_records = build_house_records_for_consolidation_sea_waybill(
                    validated,
                    raw_text,
                )
                if house_records:
                    extraction_quality["attached_list_house_count"] = len(house_records)
                    master_for_crm = (
                        master_record_without_house_cargo(validated)
                        if any(r.get("_per_house_cargo") for r in house_records)
                        else validated
                    )
                    crm_output = records_to_master_json(
                        house_records,
                        master_record=master_for_crm,
                    )
                    house_output = records_to_house_json(
                        house_records,
                        master_record=master_for_crm,
                    )
                else:
                    crm_output = records_to_master_json([validated])
                    house_output = records_to_house_json([validated])
                resolved_bl = normalize_bl_type(getattr(bl_type, "value", bl_type))
                if resolved_bl == "house" and not house_records:
                    bl_type = BlTypeQuery.master
                    extraction_quality["bl_type_corrected"] = "master_consolidation_sea_waybill"
                return _build_response(
                    crm_output,
                    raw_text,
                    extraction_quality,
                    post_to_dataverse,
                    download,
                    house_output,
                    bl_type=bl_type,
                )

        # No individual records: future/unknown Excel layouts go to Azure as one workbook.
        extraction_quality["record_routing"] = {
            "direct": 0,
            "azure_fallback": 1,
            "skipped": 0,
            "policy": "direct_when_confident_else_azure",
            "mode": "whole_document_or_workbook",
        }
        if is_tur_cargo_manifest(raw_text):
            manifest = parse_tur_cargo_manifest(raw_text)
            if manifest:
                house_records = [
                    validate_and_correct(rec, raw_text)
                    for rec in manifest["house_records"]
                ]
                master_record = validate_and_correct(
                    manifest["master_record"],
                    raw_text,
                )
                extraction_quality["document_type_detected"] = "tur_cargo_manifest_pdf"
                extraction_quality["manifest_row_count"] = len(house_records)
                extraction_quality["record_routing"] = {
                    "direct": len(house_records),
                    "azure_fallback": 0,
                    "skipped": 0,
                    "policy": "pdf_tur_cargo_manifest",
                    "mode": "manifest_rows",
                }
                crm_output = records_to_master_json(
                    house_records,
                    master_record=master_record,
                )
                house_output = records_to_house_json(
                    house_records,
                    master_record=master_record,
                )
                return _build_response(
                    crm_output,
                    raw_text,
                    extraction_quality,
                    post_to_dataverse,
                    download,
                    house_output,
                    bl_type=bl_type,
                )

        if is_export_lcl_manifest(raw_text):
            manifest = parse_export_lcl_manifest(raw_text)
            if manifest:
                house_records = [
                    validate_and_correct(rec, raw_text)
                    for rec in manifest["house_records"]
                ]
                master_record = validate_and_correct(
                    manifest["master_record"],
                    raw_text,
                )
                extraction_quality["document_type_detected"] = "export_lcl_manifest_pdf"
                extraction_quality["manifest_row_count"] = len(house_records)
                extraction_quality["record_routing"] = {
                    "direct": len(house_records),
                    "azure_fallback": 0,
                    "skipped": 0,
                    "policy": "pdf_export_lcl_manifest",
                    "mode": "manifest_rows",
                }
                crm_output = records_to_master_json(
                    house_records,
                    master_record=master_record,
                )
                house_output = records_to_house_json(
                    house_records,
                    master_record=master_record,
                )
                return _build_response(
                    crm_output,
                    raw_text,
                    extraction_quality,
                    post_to_dataverse,
                    download,
                    house_output,
                    bl_type=bl_type,
                )

        if is_freight_debit_note(raw_text):
            debit_record = parse_freight_debit_note(raw_text)
            if debit_record:
                validated = validate_and_correct(debit_record, raw_text)
                extraction_quality["document_type_detected"] = "freight_debit_note_pdf"
                extraction_quality["record_routing"] = {
                    "direct": 1,
                    "azure_fallback": 0,
                    "skipped": 0,
                    "policy": "pdf_freight_debit_note",
                    "mode": "single_house_from_debit_note",
                }
                house_output = records_to_house_json([validated])
                resolved_bl = normalize_bl_type(
                    getattr(bl_type, "value", bl_type),
                )
                if resolved_bl == "house" and house_output.get("value"):
                    crm_output = house_output["value"][0]
                else:
                    crm_output = records_to_master_json([validated])
                return _build_response(
                    crm_output,
                    raw_text,
                    extraction_quality,
                    post_to_dataverse,
                    download,
                    house_output,
                    bl_type=bl_type,
                )

        if is_standard_house_bl(raw_text):
            house_record = parse_standard_house_bl(raw_text)
            if house_record:
                # This direct parser reads already-labelled House B/L fields.
                # The broad PDF validator can over-enrich noisy OCR on this
                # layout and invent a master link from phone/reference numbers.
                validated = house_record
                extraction_quality["document_type_detected"] = "standard_house_bl_pdf"
                extraction_quality["record_routing"] = {
                    "direct": 1,
                    "azure_fallback": 0,
                    "skipped": 0,
                    "policy": "pdf_standard_house_bl",
                    "mode": "single_house_with_linking_evidence",
                }
                house_output = records_to_house_json([validated])
                resolved_bl = normalize_bl_type(
                    getattr(bl_type, "value", bl_type),
                )
                if resolved_bl == "house" and house_output.get("value"):
                    crm_output = house_output["value"][0]
                else:
                    crm_output = records_to_master_json([validated])
                return _build_response(
                    crm_output,
                    raw_text,
                    extraction_quality,
                    post_to_dataverse,
                    download,
                    house_output,
                    bl_type=bl_type,
                )

        if is_standard_master_bl(raw_text):
            master_record = parse_standard_master_bl(raw_text)
            if master_record:
                resolved_bl = normalize_bl_type(
                    getattr(bl_type, "value", bl_type),
                )
                # When user explicitly sets bl_type=house, skip the deterministic
                # master parser and let LLM/intelligent parsing extract the house.
                if resolved_bl == "house":
                    extraction_quality["deterministic_master_parser_skipped"] = \
                        "bl_type=house overrides standard master BL detection"
                else:
                    validated = validate_and_correct(
                        master_record,
                        raw_text,
                        enrichment_text=raw_text,
                    )
                    extraction_quality["document_type_detected"] = "standard_master_bl_pdf"
                    extraction_quality["record_routing"] = {
                        "direct": 1,
                        "azure_fallback": 0,
                        "skipped": 0,
                        "policy": "pdf_standard_master_bl",
                        "mode": "single_master_bl",
                    }
                    crm_output = records_to_master_json([validated])
                    house_output = records_to_house_json([validated])
                    bl_type = BlTypeQuery.master
                    extraction_quality["bl_type_corrected"] = "master_standard_bl"
                    return _build_response(
                        crm_output,
                        raw_text,
                        extraction_quality,
                        post_to_dataverse,
                        download,
                        house_output,
                        bl_type=bl_type,
                    )

        if is_cargo_manifest_hbl_blocks(raw_text):
            manifest = parse_cargo_manifest_hbl_blocks(raw_text)
            if manifest and len(manifest["house_records"]) >= 2:
                # House blocks are already self-contained and per-house clean.
                # Deliberately skip validate_and_correct here: its whole-document
                # enrichment (consignee block, merged HS codes, cargo description)
                # would bleed the master header / other houses into every record.
                house_records = manifest["house_records"]
                master_record = manifest["master_record"]
                extraction_quality["document_type_detected"] = "cargo_manifest_hbl_blocks_pdf"
                extraction_quality["consolidated_house_count"] = len(house_records)
                extraction_quality["record_routing"] = {
                    "direct": len(house_records),
                    "azure_fallback": 0,
                    "skipped": 0,
                    "policy": "pdf_cargo_manifest_hbl_blocks",
                    "mode": "one_master_with_house_records",
                    "document_layout": "master_with_labelled_house_blocks",
                }
                crm_output = records_to_master_json(
                    house_records,
                    master_record=master_record,
                )
                house_output = records_to_house_json(
                    house_records,
                    master_record=master_record,
                )
                return _build_response(
                    crm_output,
                    raw_text,
                    extraction_quality,
                    post_to_dataverse,
                    download,
                    house_output,
                    bl_type=bl_type,
                )

        if is_consolidated_lcl_multi_hbl(raw_text):
            consolidated = parse_consolidated_lcl_multi_hbl(raw_text)
            if consolidated and len(consolidated["house_records"]) >= 2:
                house_records = [
                    validate_and_correct(rec, raw_text)
                    for rec in consolidated["house_records"]
                ]
                master_record = validate_and_correct(
                    consolidated["master_record"],
                    raw_text,
                )
                extraction_quality["document_type_detected"] = "consolidated_lcl_multi_hbl_pdf"
                extraction_quality["consolidated_house_count"] = len(house_records)
                extraction_quality["record_routing"] = {
                    "direct": len(house_records),
                    "azure_fallback": 0,
                    "skipped": 0,
                    "policy": "pdf_consolidated_lcl_multi_hbl",
                    "mode": "one_master_with_house_records",
                    "document_layout": "master_with_houses",
                }
                crm_output = records_to_master_json(
                    house_records,
                    master_record=master_record,
                )
                house_output = records_to_house_json(
                    house_records,
                    master_record=master_record,
                )
                return _build_response(
                    crm_output,
                    raw_text,
                    extraction_quality,
                    post_to_dataverse,
                    download,
                    house_output,
                    bl_type=bl_type,
                )

        parse_result = parse_document_intelligently(
            raw_text,
            extracted,
            file_bytes=file_bytes,
            filename=file.filename,
        )
        extraction_quality.update(parse_result.quality)
        if parse_result.azure_warnings:
            extraction_quality["azure_warnings"] = parse_result.azure_warnings

        if not parse_result.records:
            return ExtractResponse(
                success=False,
                error="Could not extract any Bill of Lading records from the document.",
                raw_text=raw_text[:5000] + "..." if len(raw_text) > 5000 else raw_text,
                extraction_quality=extraction_quality,
            )

        extraction_quality["record_routing"] = {
            "direct": 0,
            "azure_fallback": len(parse_result.records),
            "skipped": 0,
            "policy": "azure_intelligent_with_fallback",
            "mode": "one_master_per_bl_record",
            "document_layout": parse_result.document_layout,
        }

        if len(parse_result.records) >= 2:
            extraction_quality["multi_bl_count"] = len(parse_result.records)
            crm_masters = [records_to_master_json([v]) for v in parse_result.records]
            return _build_response(
                crm_masters[0],
                raw_text,
                extraction_quality,
                post_to_dataverse,
                download,
                house_output={"value": []},
                crm_records=crm_masters,
                bl_type=bl_type,
            )

        validated = parse_result.records[0]
        house_records = build_house_records_for_consolidation_sea_waybill(
            validated,
            raw_text,
        )
        if house_records:
            extraction_quality["attached_list_house_count"] = len(house_records)
            master_for_crm = (
                master_record_without_house_cargo(validated)
                if any(r.get("_per_house_cargo") for r in house_records)
                else validated
            )
            crm_output = records_to_master_json(
                house_records,
                master_record=master_for_crm,
            )
            house_output = records_to_house_json(
                house_records,
                master_record=master_for_crm,
            )
        else:
            extracted_records = [validated]
            crm_output = records_to_master_json(extracted_records)
            house_output = records_to_house_json(extracted_records)

        return _build_response(crm_output, raw_text, extraction_quality, post_to_dataverse, download, house_output, bl_type=bl_type)
    except Exception as exc:
        return ExtractResponse(success=False, error=str(exc))


def _build_response(
    crm_output: Dict[str, Any],
    raw_text: str,
    extraction_quality: Dict[str, Any],
    post_to_dataverse: bool,
    download: bool = False,
    house_output: Optional[Dict[str, Any]] = None,
    crm_records: Optional[List[Dict[str, Any]]] = None,
    bl_type: str = "master",
) -> Any:
    dataverse_result = None
    dataverse_error = None
    masters = crm_records if crm_records else None
    resolved_bl = getattr(bl_type, "value", bl_type)

    from custom_business_rules import apply_crm_payload_rules, custom_rules_enabled

    # When user explicitly sets bl_type=house for a single record, use
    # standalone house payload instead of master-level payload with nested
    # houses. This prevents cargo/containers from being placed on the wrong
    # operation level and avoids creating an extra empty nested house.
    if resolved_bl == "house" and isinstance(house_output, dict):
        house_values = house_output.get("value") or []
        if len(house_values) == 1 and not masters:
            crm_output = house_values[0]

    if isinstance(crm_output, dict) and crm_output:
        apply_bl_type_to_crm_payload(crm_output, resolved_bl)
    if custom_rules_enabled() and isinstance(crm_output, dict) and crm_output:
        apply_crm_payload_rules(crm_output)
    if masters:
        for m in masters:
            if isinstance(m, dict):
                apply_bl_type_to_crm_payload(m, resolved_bl)

    # Final defensive guard: enforce Dataverse string-column length caps on
    # every master payload before either Dataverse POST or response download.
    # This prevents a single oversized field (e.g. mesco_cargodescription >
    # 1500 chars) from blocking the entire save with a 0x80048d19 / 400 error.
    if isinstance(crm_output, dict) and crm_output:
        cap_nested_payload(crm_output)
    if masters:
        for m in masters:
            if isinstance(m, dict):
                cap_nested_payload(m)

    if post_to_dataverse:
        try:
            if masters and len(masters) > 1:
                uploaded = []
                for idx, crm in enumerate(masters):
                    uploaded.append({"index": idx, **upload_crm_json(crm)})
                dataverse_result = {"masters": uploaded, "count": len(uploaded)}
            elif crm_output:
                dataverse_result = upload_crm_json(crm_output)
        except Exception as exc:
            dataverse_error = str(exc)
            logger.warning("Dataverse upload failed: %s", dataverse_error)

    if download:
        payload: Any = crm_output
        if masters and len(masters) > 1:
            payload = {"multi_bl": True, "masters": masters}
        json_bytes = json.dumps(payload, indent=2, default=str).encode("utf-8")
        return Response(
            content=json_bytes,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="crm_output.json"',
                "Content-Length": str(len(json_bytes)),
            },
        )

    if isinstance(extraction_quality, dict):
        resolved_bl_type = getattr(bl_type, "value", bl_type)
        extraction_quality["bl_type"] = resolved_bl_type
        extraction_quality["mesco_bltype"] = (
            886150002 if resolved_bl_type == "house" else 886150001
        )
        extraction_quality.update(llm_meta())
        if "llm_usage" not in extraction_quality:
            if extraction_quality.get("llm_attempted"):
                extraction_quality["llm_usage_available"] = False
            else:
                extraction_quality["llm_usage"] = {
                    "provider": extraction_quality.get("llm_provider"),
                    "model": extraction_quality.get("llm_model"),
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                }
                extraction_quality["llm_usage_available"] = True

    return ExtractResponse(
        success=True,
        data=crm_output,
        records=masters,
        house_data=house_output,
        raw_text=raw_text[:5000] + "..." if len(raw_text) > 5000 else raw_text,
        extraction_quality=extraction_quality,
        dataverse_result=dataverse_result,
        dataverse_error=dataverse_error,
    )


def _merge_puter_records_with_deterministic_pdf(
    records: List[Dict[str, Any]],
    raw_text: str,
    extraction_quality: Dict[str, Any],
) -> List[Dict[str, Any]]:
    deterministic = best_deterministic_parse(raw_text)
    if not deterministic:
        return records

    fallback_records = deterministic.reconciliation_records()
    if not fallback_records:
        return records

    extraction_quality.update(
        {
            "deterministic_parser": deterministic.parser,
            "deterministic_confidence": deterministic.confidence,
            "deterministic_record_count": len(fallback_records),
            "deterministic_layout": deterministic.layout,
            "deterministic_document_type": deterministic.document_type,
            "deterministic_reconciled": True,
        }
    )

    if not records:
        return list(fallback_records)

    if len(records) == 1 and len(fallback_records) == 1 and deterministic.layout in {
        "single_bl",
        "single_house",
    }:
        return [
            merge_record_fields(
                records[0],
                fallback_records[0],
                prefer_secondary_keys=_PUTER_DETERMINISTIC_FORCE_KEYS,
            )
        ]

    return reconcile_record_lists(
        records,
        fallback_records,
        prefer_fallback_keys=_PUTER_DETERMINISTIC_FORCE_KEYS,
    )


@app.get("/puter/config", tags=["Extraction"])
async def puter_config():
    """Return the browser-side Puter.js extraction prompt and model list."""
    from ai_extractor import INVOICE_JSON_SCHEMA, INVOICE_SYSTEM_PROMPT, MULTI_INVOICE_JSON_SCHEMA, MULTI_INVOICE_SYSTEM_PROMPT
    schema_hint = json.dumps(MULTI_BL_JSON_SCHEMA.get("schema") or MULTI_BL_JSON_SCHEMA)
    prompt_prefix = (
        SYSTEM_PROMPT
        + "\n\nReturn ONLY one valid JSON object matching this schema. "
        + "No markdown fences, no commentary, no extra text after the JSON:\n"
        + schema_hint
        + "\n\nThe attached PDF page images are the authoritative source. "
        + "Use Gemini vision/OCR to read the layout, tables, field labels, and stamps. "
        + "The browser-extracted text below is only a backup hint and may be incomplete:\n\n"
    )

    inv_schema_hint = json.dumps(INVOICE_JSON_SCHEMA.get("schema") or INVOICE_JSON_SCHEMA)
    inv_prompt_prefix = (
        INVOICE_SYSTEM_PROMPT
        + "\n\nReturn ONLY one valid JSON object matching this schema. "
        + "No markdown fences, no commentary, no extra text after the JSON:\n"
        + inv_schema_hint
        + "\n\nThe attached PDF page images are the authoritative source. "
        + "Use Gemini vision/OCR to read the layout, tables, field labels, and stamps. "
        + "The browser-extracted text below is only a backup hint and may be incomplete:\n\n"
    )

    multi_inv_schema_hint = json.dumps(MULTI_INVOICE_JSON_SCHEMA.get("schema") or MULTI_INVOICE_JSON_SCHEMA)
    multi_inv_prompt_prefix = (
        MULTI_INVOICE_SYSTEM_PROMPT
        + "\n\nReturn ONLY one valid JSON object matching this schema. "
        + "No markdown fences, no commentary, no extra text after the JSON:\n"
        + multi_inv_schema_hint
        + "\n\nThe attached PDF page images are the authoritative source. "
        + "Use Gemini vision/OCR to read the layout, tables, field labels, and stamps. "
        + "The browser-extracted text below is only a backup hint and may be incomplete:\n\n"
    )

    return {
        "provider": "puter",
        "default_model": settings.puter_model,
        "models": list(GEMINI_MODELS),
        "prompt_prefix": prompt_prefix,
        "invoice_prompt_prefix": inv_prompt_prefix,
        "multi_invoice_prompt_prefix": multi_inv_prompt_prefix,
        "max_text_chars": settings.gemini_max_input_chars,
        "max_visual_pages": 8,
        "pdf_render_scale": 2.0,
        "pdf_max_canvas_width": 1800,
        "pdf_image_quality": 0.86,
    }


@app.post("/puter/format", response_model=ExtractResponse, tags=["Extraction"])
async def puter_format(request: PuterFormatRequest):
    """Validate Puter.js Gemini JSON, map it to CRM JSON, and optionally post to Dataverse."""
    try:
        payload = request.puter_payload or {}
        raw_text = request.raw_text or ""
        records = payload.get("records")
        if not isinstance(records, list):
            records = [payload] if payload else []
        records = [dict(rec) for rec in records if isinstance(rec, dict)]

        deterministic_quality: Dict[str, Any] = {}
        records = _merge_puter_records_with_deterministic_pdf(
            records,
            raw_text,
            deterministic_quality,
        )
        if not records:
            return ExtractResponse(success=False, error="Puter did not return any B/L records.")

        validated_records: List[Dict[str, Any]] = []
        for rec in records:
            rec.setdefault("extraction_method", "puter_gemini_browser")
            validated_records.append(validate_and_correct(rec, raw_text))

        layout = str(payload.get("document_layout") or "").lower()
        master_values = {
            str(rec.get("mesco_masterblno") or "").strip().upper()
            for rec in validated_records
            if rec.get("mesco_masterblno")
        }
        house_count = sum(1 for rec in validated_records if rec.get("mesco_houseblno"))
        one_master_with_houses = (
            layout in {"manifest", "master_with_houses", "consolidated_lcl", "multi_house"}
            or (len(master_values) == 1 and house_count >= 1)
        )

        extraction_quality = {
            "source": "puter_js",
            "llm_provider": "puter",
            "llm_model": request.llm_model or settings.puter_model,
            "visual_input": "pdf_page_images",
            "visual_page_count": request.visual_page_count,
            "pdf_page_count": request.pdf_page_count,
            "document_layout": payload.get("document_layout"),
            "record_count": len(validated_records),
        }
        extraction_quality.update(deterministic_quality)

        if len(validated_records) > 1 and not one_master_with_houses:
            crm_masters = [records_to_master_json([rec]) for rec in validated_records]
            return _build_response(
                crm_masters[0],
                raw_text,
                extraction_quality,
                request.post_to_dataverse,
                False,
                house_output={"value": []},
                crm_records=crm_masters,
                bl_type=request.bl_type,
            )

        crm_output = records_to_master_json(validated_records)
        house_output = records_to_house_json(validated_records)
        return _build_response(
            crm_output,
            raw_text,
            extraction_quality,
            request.post_to_dataverse,
            False,
            house_output,
            bl_type=request.bl_type,
        )
    except Exception as exc:
        logger.exception("Puter format failed")
        return ExtractResponse(
            success=False,
            error="We could not format the Puter extraction for Dataverse. Please review the extracted JSON and try again.",
            extraction_quality={"technical_error": str(exc), "llm_provider": "puter"},
        )


@app.post(
    "/test/pdf/batch",
    response_model=BatchPdfTestResponse,
    summary="Batch test multiple PDFs (extract + validate, no Dataverse)",
    description=(
        "Upload one or more PDF files. Each file is processed through the same extraction "
        "pipeline as /extract/file (OCR, intelligent parse, validation). Returns per-file "
        "pass/fail, quality score, issues, and optional CRM JSON. Does not upload to Dataverse."
    ),
)
async def test_pdf_batch(
    files: List[UploadFile] = File(..., description="One or more PDF files"),
    include_crm_json: bool = Query(
        True,
        description="Include full CRM master JSON per file in each result",
    ),
    include_raw_text: bool = Query(
        False,
        description="Include OCR text preview (first 3000 chars) per file",
    ),
):
    if not files:
        return BatchPdfTestResponse(
            total=0,
            succeeded=0,
            failed=0,
            passed=0,
            failed_validation=0,
            average_score=0.0,
            total_processing_ms=0,
            results=[],
        )

    results: List[Dict[str, Any]] = []
    succeeded = failed = passed_count = failed_validation = 0
    total_ms = 0
    scores: List[int] = []

    for upload in files:
        file_bytes = await upload.read()
        item = process_pdf_bytes(
            file_bytes,
            upload.filename or "upload.pdf",
            include_raw_text_preview=include_raw_text,
        )
        result_dict = item.to_dict(include_crm=include_crm_json, include_raw=include_raw_text)
        results.append(result_dict)

        total_ms += item.processing_ms
        if item.success:
            succeeded += 1
            scores.append(item.score)
            if item.passed:
                passed_count += 1
            else:
                failed_validation += 1
        else:
            failed += 1
            scores.append(0)

    avg_score = round(sum(scores) / len(scores), 1) if scores else 0.0

    return BatchPdfTestResponse(
        total=len(files),
        succeeded=succeeded,
        failed=failed,
        passed=passed_count,
        failed_validation=failed_validation,
        average_score=avg_score,
        total_processing_ms=total_ms,
        results=results,
    )


@app.post("/extract/text", response_model=ExtractResponse, tags=["Extraction"])
async def extract_text(request: ExtractRequest):
    provider_val = request.llm_provider.value if request.llm_provider else None
    model_val = (request.llm_model or "").strip() or None
    if normalize_llm_provider(provider_val or settings.llm_provider) == "puter":
        return ExtractResponse(
            success=False,
            error=(
                "Puter is the default AI extractor and runs in the browser. "
                "Open /puter for Puter.js extraction, or set llm_provider=azure/gemini "
                "for server-side text extraction."
            ),
        )
    try:
        validate_llm_request(provider_val, model_val)
    except ValueError as exc:
        return ExtractResponse(success=False, error=str(exc))

    try:
        with llm_request_overrides(provider_val, model_val):
            raw_text = request.ocr_text
            if not raw_text.strip():
                return ExtractResponse(success=False, error="No text provided.")

            parse_result = parse_document_intelligently(raw_text)
            if not parse_result.records:
                return ExtractResponse(success=False, error="No B/L records extracted.", raw_text=raw_text)

            if len(parse_result.records) >= 2:
                crm_masters = [records_to_master_json([v]) for v in parse_result.records]
                for m in crm_masters:
                    apply_bl_type_to_crm_payload(m, request.bl_type.value)
                return ExtractResponse(
                    success=True,
                    data=crm_masters[0],
                    records=crm_masters,
                    raw_text=raw_text,
                    extraction_quality=parse_result.quality,
                )

            validated = parse_result.records[0]
            crm_output = records_to_master_json([validated])
            apply_bl_type_to_crm_payload(crm_output, request.bl_type.value)
            return ExtractResponse(
                success=True,
                data=crm_output,
                raw_text=raw_text,
                extraction_quality=parse_result.quality,
            )
    except Exception as exc:
        return ExtractResponse(success=False, error=str(exc))


@app.post(
    "/extract/pdf",
    response_model=ExtractResponse,
    summary="Extract from PDF (Master or House B/L)",
    description=(
        "Upload a PDF and extract B/L data. Use **bl_type** to post as "
        "**master** (886150001) or **house** (886150002) in Dynamics.\n\n"
        "**Default AI:** use `/puter` for browser-side Puter.js Gemini extraction "
        "with no Gemini API key. For server-side API extraction only, set "
        "`llm_provider=azure` or `llm_provider=gemini` explicitly."
    ),
    tags=["Extraction"],
)
async def extract_pdf(
    request: Request,
    file: UploadFile = File(..., description="PDF file to extract"),
    bl_type: BlTypeQuery = Query(
        BlTypeQuery.master,
        title="B/L Type",
        description="Post as Master B/L (886150001) or House B/L (886150002)",
    ),
    llm_provider: Optional[LlmProviderQuery] = Query(
        LlmProviderQuery.puter,
        description="AI backend: puter browser page, azure, or gemini (defaults to LLM_PROVIDER in .env)",
    ),
    llm_model: Optional[GeminiModelQuery] = Query(
        GeminiModelQuery.gemini_3_pro_preview,
        description="Gemini model id when llm_provider=gemini (Puter.js-compatible ids)",
    ),
    post_to_dataverse: bool = Query(
        True,
        description="Automatically upload extracted data to Dynamics 365 Dataverse",
    ),
    download: bool = Query(
        False,
        description="Download the CRM JSON as a file instead of returning the normal response",
    ),
    apply_custom_rules: bool = Query(
        True,
        description="Apply CRM business rules (freight→booking, load type, LCL TEUs, house totals)",
    ),
):
    return await extract_file(
        request,
        file,
        bl_type=bl_type,
        llm_provider=llm_provider,
        llm_model=llm_model,
        post_to_dataverse=post_to_dataverse,
        download=download,
        apply_custom_rules=apply_custom_rules,
    )


@app.post(
    "/extract/excel",
    response_model=ExtractResponse,
    summary="Extract from Excel file",
    description=(
        "Upload an Excel file (.xlsx, .xls, .csv). Choose **bl_type** (master or house) in the form.\n\n"
        "**Default AI:** use `/puter` for browser-side Puter.js Gemini extraction "
        "with no Gemini API key. For server-side API extraction only, set "
        "`llm_provider=azure` or `llm_provider=gemini` explicitly."
    ),
    tags=["Extraction"],
)
async def extract_excel(
    request: Request,
    file: UploadFile = File(..., description="Excel or CSV file (.xlsx, .xls, .csv)"),
    bl_type: BlTypeQuery = Form(
        BlTypeQuery.master,
        description="B/L type: master (886150001) or house (886150002)",
    ),
    llm_provider: Optional[LlmProviderQuery] = Form(
        LlmProviderQuery.puter,
        description="AI backend: puter browser page, azure, or gemini (defaults to LLM_PROVIDER in .env)",
    ),
    llm_model: Optional[GeminiModelQuery] = Form(
        GeminiModelQuery.gemini_3_pro_preview,
        description="Gemini model id when llm_provider=gemini",
    ),
    post_to_dataverse: bool = Form(
        True,
        description="Automatically upload extracted data to Dynamics 365 Dataverse",
    ),
    download: bool = Form(
        False,
        description="Download the CRM JSON as a file instead of returning the normal response",
    ),
    apply_custom_rules: bool = Form(
        True,
        description="Apply CRM business rules (freight→booking, load type, LCL TEUs, house totals)",
    ),
):
    return await extract_file(
        request,
        file,
        bl_type=bl_type,
        llm_provider=llm_provider,
        llm_model=llm_model,
        post_to_dataverse=post_to_dataverse,
        download=download,
        apply_custom_rules=apply_custom_rules,
    )


@app.post(
    "/extract/master",
    response_model=ExtractResponse,
    summary="Extract B/L as Master operation",
    description=(
        "Extract from PDF, Excel, or CSV and stamp **mesco_bltype = 886150001 (Master)** "
        "before upload. Same pipeline as POST /extract/file?bl_type=master."
    ),
    tags=["Extraction — B/L type"],
)
async def extract_as_master(
    request: Request,
    file: UploadFile = File(..., description="PDF, XLSX, XLS, or CSV file"),
    post_to_dataverse: bool = Form(True, description="Automatically upload to Dynamics 365 Dataverse"),
    download: bool = Form(False, description="Download CRM JSON instead of JSON response"),
):
    return await extract_file(
        request,
        file,
        bl_type=BlTypeQuery.master,
        post_to_dataverse=post_to_dataverse,
        download=download,
    )


@app.post(
    "/extract/house",
    response_model=ExtractResponse,
    summary="Extract B/L as House operation",
    description=(
        "Extract from PDF, Excel, or CSV and stamp **mesco_bltype = 886150002 (House)** "
        "before upload. Same pipeline as POST /extract/file?bl_type=house."
    ),
    tags=["Extraction — B/L type"],
)
async def extract_as_house(
    request: Request,
    file: UploadFile = File(..., description="PDF, XLSX, XLS, or CSV file"),
    post_to_dataverse: bool = Form(True, description="Automatically upload to Dynamics 365 Dataverse"),
    download: bool = Form(False, description="Download CRM JSON instead of JSON response"),
):
    return await extract_file(
        request,
        file,
        bl_type=BlTypeQuery.house,
        post_to_dataverse=post_to_dataverse,
        download=download,
    )


class CompareRequest(BaseModel):
    master_id: str
    sent_payload: Dict[str, Any]


class CompareResponse(BaseModel):
    success: bool
    master_id: str
    saved: Optional[Dict[str, Any]] = None
    comparison: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


def _flatten(obj: Any, parent_key: str = "") -> Dict[str, Any]:
    """Flatten nested dict for field-level comparison."""
    items: Dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            new_key = f"{parent_key}.{k}" if parent_key else k
            if isinstance(v, (dict, list)):
                items.update(_flatten(v, new_key))
            else:
                items[new_key] = v
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            new_key = f"{parent_key}[{i}]"
            if isinstance(v, (dict, list)):
                items.update(_flatten(v, new_key))
            else:
                items[new_key] = v
    return items


def _clean_dataverse_response(obj: Any) -> Any:
    """Remove OData metadata fields from Dataverse response."""
    if isinstance(obj, dict):
        return {
            k: _clean_dataverse_response(v)
            for k, v in obj.items()
            if not k.startswith("@") and not k.startswith("_") and not k.endswith("@OData.Community.Display.V1.FormattedValue")
        }
    if isinstance(obj, list):
        return [_clean_dataverse_response(item) for item in obj]
    return obj


@app.get("/dynamics/operation/{master_id}")
async def get_dynamics_operation(master_id: str):
    """Fetch a master operation with its houses, containers, and cargo from Dataverse."""
    try:
        client = DataverseClientService.get_instance()
        expand = (
            "mesco_Operation_mesco_Operation_mesco_Operation,"
            "mesco_Container_MasterOperation_mesco_Operation,"
            "mesco_Cargo_MasterOperation_mesco_Operation"
        )
        resp = client.get(f"{_ENTITY}({master_id})?$expand={expand}")
        data = resp.json()
        return {"success": True, "data": _clean_dataverse_response(data)}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


@app.get("/operation", response_class=HTMLResponse, include_in_schema=False)
async def operation_view_page() -> str:
    """Serve the React-based Operation review page that mirrors the Dynamics
    operation form (all fields, lookups, option sets, houses, containers, cargo).
    """
    import os

    html_path = os.path.join(os.path.dirname(__file__), "operation_view.html")
    try:
        with open(html_path, "r", encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return "<h1>operation_view.html not found</h1>"


@app.get("/dynamics/operation/{master_id}/full")
async def get_dynamics_operation_full(master_id: str):
    """Fetch a master operation with ALL fields, lookups (GUID + display name),
    option-set labels, and nested houses/containers/cargo.

    Unlike /dynamics/operation/{id}, this keeps the OData annotations
    (`@OData.Community.Display.V1.FormattedValue`, `_<field>_value`, lookup
    logical names) so the operation-review React page can resolve lookups and
    option sets exactly as Dynamics does.
    """
    try:
        client = DataverseClientService.get_instance()
        expand = (
            "mesco_Operation_mesco_Operation_mesco_Operation("
            "$expand=mesco_Container_mesco_houses,"
            "mesco_Cargo_HouseOperation_mesco_Operation),"
            "mesco_Container_MasterOperation_mesco_Operation($expand=mesco_ContainerNo),"
            "mesco_Cargo_MasterOperation_mesco_Operation"
        )
        resp = client.get(f"{_ENTITY}({master_id})?$expand={expand}")
        return {"success": True, "data": resp.json()}
    except Exception as exc:
        logger.exception("Failed to fetch full operation %s", master_id)
        return {"success": False, "error": str(exc)}


@app.get("/dynamics/operation/{master_id}/houses")
async def get_dynamics_houses(master_id: str):
    """Fetch house bills under a master operation from Dataverse.

    Queries mesco_operations where _mesco_operation_value = master_id
    (the lookup that links a house to its master). 
    Returns each house with its containers and cargo.
    """
    try:
        client = DataverseClientService.get_instance()
        expand = (
            "mesco_Container_mesco_houses,"
            "mesco_Cargo_HouseOperation_mesco_Operation"
        )
        filter_query = f"_mesco_operation_value eq {master_id}"
        resp = client.get(
            f"{_ENTITY}?$filter={filter_query}&$expand={expand}"
        )
        data = resp.json()
        houses = data.get("value", []) if isinstance(data, dict) else []
        return {
            "success": True,
            "master_id": master_id,
            "count": len(houses),
            "houses": [_clean_dataverse_response(h) for h in houses],
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


@app.get("/dynamics/house/{house_id}")
async def get_dynamics_house(house_id: str):
    """Fetch a single house bill by its ID with containers and cargo."""
    try:
        client = DataverseClientService.get_instance()
        expand = (
            "mesco_Container_mesco_houses,"
            "mesco_Cargo_HouseOperation_mesco_Operation"
        )
        resp = client.get(f"{_ENTITY}({house_id})?$expand={expand}")
        data = resp.json()
        return {"success": True, "house": _clean_dataverse_response(data)}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


@app.post("/dynamics/compare", response_model=CompareResponse)
async def compare_dynamics(request: CompareRequest):
    """Compare the sent payload against what was actually saved in Dataverse."""
    try:
        client = DataverseClientService.get_instance()
        expand = (
            "mesco_Operation_mesco_Operation_mesco_Operation,"
            "mesco_Container_MasterOperation_mesco_Operation,"
            "mesco_Cargo_MasterOperation_mesco_Operation"
        )
        resp = client.get(f"{_ENTITY}({request.master_id})?$expand={expand}")
        saved_raw = resp.json()
        saved = _clean_dataverse_response(saved_raw)

        sent_flat = _flatten(request.sent_payload)
        saved_flat = _flatten(saved)

        saved_keys = set()
        not_saved_keys = set()
        different_keys = {}

        for key in sent_flat:
            if key in saved_flat:
                sent_val = sent_flat[key]
                saved_val = saved_flat[key]
                if str(sent_val) == str(saved_val):
                    saved_keys.add(key)
                else:
                    different_keys[key] = {
                        "sent": sent_val,
                        "saved": saved_val,
                    }
            else:
                not_saved_keys.add(key)

        # Some fields exist in saved but not in sent (Dataverse defaults)
        extra_keys = set(saved_flat.keys()) - set(sent_flat.keys())

        comparison = {
            "fields_saved": sorted(saved_keys) or None,
            "fields_not_saved": sorted(not_saved_keys) or None,
            "fields_different": different_keys or None,
            "fields_saved_count": len(saved_keys),
            "fields_not_saved_count": len(not_saved_keys),
            "fields_different_count": len(different_keys),
            "fields_extra_in_dataverse": sorted(extra_keys)[:50] if extra_keys else None,
        }

        return CompareResponse(
            success=True,
            master_id=request.master_id,
            saved=saved,
            comparison=comparison,
        )
    except Exception as exc:
        return CompareResponse(success=False, master_id=request.master_id, error=str(exc))


class InvoiceExtractResponse(BaseModel):
    success: bool
    data: Optional[Dict[str, Any]] = None
    is_bl_matched: Optional[bool] = None
    error: Optional[str] = None
    dataverse_results: Optional[List[Dict[str, Any]]] = None
    dataverse_error: Optional[str] = None
    resolved_operation_id: Optional[str] = None
    resolved_operation_code: Optional[str] = None
    resolved_operation_bl: Optional[str] = None
    dynamics_url: Optional[str] = None


def _invoice_container_parts(extracted_data: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Normalize container/seal/type values extracted from an invoice."""
    raw_container = str(extracted_data.get("container_number") or "").strip()
    raw_seal = str(extracted_data.get("seal_number") or "").strip()
    container_type = str(
        extracted_data.get("container_type")
        or extracted_data.get("container_size")
        or ""
    ).strip()

    parts = [p.strip() for p in re.split(r"[/,;|]", raw_container) if p.strip()]
    container_no = ""
    for part in parts or [raw_container]:
        candidate = _normalize_container_number(part)
        if re.fullmatch(r"[A-Z]{4}\d{7}", candidate):
            container_no = candidate
            break

    if not container_no:
        container_no = _normalize_container_number(raw_container)

    if not raw_seal and len(parts) > 1:
        for part in parts[1:]:
            if not re.search(r"\d+\s*x\s*\d+", part, re.I):
                raw_seal = part.strip()
                break

    if not container_type and len(parts) > 1:
        for part in parts[1:]:
            compact = re.sub(r"\s+", "", part.upper())
            match = re.search(r"(?:\d+X)?(20|40|45)(HC|HQ|GP|DV|DC|RF|OT|FR)?", compact)
            if match:
                container_type = "".join(g for g in match.groups() if g)
                break

    return {
        "container_no": container_no or None,
        "seal_no": raw_seal or None,
        "container_type": container_type or None,
    }


def _ensure_invoice_container(
    client: DataverseClientService,
    operation_id: str,
    extracted_data: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Create or update the operation container found on an invoice."""
    parts = _invoice_container_parts(extracted_data)
    container_no = parts["container_no"]
    if not container_no:
        return None

    container_no_id = _resolve_containerno_lookup(
        client,
        container_no,
        parts.get("container_type"),
    )
    if not container_no_id:
        logger.warning("Could not resolve/create ContainerNo lookup for %s", container_no)
        return None

    fields: Dict[str, Any] = {
        "mesco_name": container_no,
        "mesco_containernumber": container_no,
        "mesco_ContainerNo@odata.bind": f"/mesco_containernos({container_no_id})",
        "mesco_MasterOperation@odata.bind": f"/mesco_operations({operation_id})",
    }
    if parts.get("seal_no"):
        fields["mesco_carrierseal"] = parts["seal_no"]

    existing_id = _find_existing_container(client, operation_id, container_no)
    if existing_id:
        _update_entity(client, _CONTAINER_ENTITY, existing_id, fields)
        return {
            "id": existing_id,
            "container_no": container_no,
            "reused": True,
            "seal_no": parts.get("seal_no"),
        }

    container_id = _create_entity(client, _CONTAINER_ENTITY, fields)
    return {
        "id": container_id,
        "container_no": container_no,
        "reused": False,
        "seal_no": parts.get("seal_no"),
    }


def _clean_invoice_bl(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _parse_invoice_charge_rows(raw_text: str) -> List[Dict[str, Any]]:
    """Parse table-style invoice charge rows that include an HBL/CNT column."""
    rows: List[Dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    lines = [line.strip() for line in raw_text.splitlines()]
    in_table = False
    pending_desc: List[str] = []
    index = 0

    def parse_row(line: str) -> Optional[Dict[str, Any]]:
        tokens = line.split()
        if len(tokens) < 9:
            return None
        hbl = tokens[0].upper()
        if not re.fullmatch(r"[A-Z]{3,}\d{5,}[A-Z0-9]*", hbl):
            return None
        try:
            kgs = float(tokens[1])
            cbm = float(tokens[2])
            credit = float(tokens[-1])
            debit = float(tokens[-2])
            currency = tokens[-3].upper()
            unit_price = float(tokens[-4])
            quantity = float(tokens[-5])
            unit = tokens[-6].upper()
        except (IndexError, ValueError):
            return None
        if not re.fullmatch(r"[A-Z]{3}", currency):
            return None
        return {
            "house_bl_number": hbl,
            "kgs": kgs,
            "cbm": cbm,
            "service_description": re.sub(r"\s+", " ", " ".join(tokens[3:-6])).strip().upper(),
            "unit": unit,
            "quantity": quantity,
            "unit_price": unit_price,
            "currency": currency,
            "total_amount": debit,
            "credit_amount": credit,
        }

    def is_table_noise(line: str) -> bool:
        upper = line.upper()
        return (
            not line
            or upper.startswith("[")
            or upper.startswith("HBL/CNT")
            or upper.startswith("SUB TOTAL")
            or upper.startswith("TOTAL:")
            or upper.startswith("WWW.")
            or "COPYRIGHT" in upper
        )

    while index < len(lines):
        line = lines[index]
        if "HBL/CNT" in line.upper() and "DEBIT" in line.upper():
            in_table = True
            pending_desc = []
            index += 1
            continue
        if in_table and line.upper().startswith(("SUB TOTAL", "TOTAL:")):
            in_table = False
            pending_desc = []
            index += 1
            continue
        if not in_table:
            index += 1
            continue

        parsed = parse_row(line)
        if parsed:
            desc_parts = [part for part in pending_desc if part]
            if parsed["service_description"]:
                desc_parts.append(parsed["service_description"])
            pending_desc = []

            lookahead = index + 1
            while lookahead < len(lines):
                next_line = lines[lookahead]
                if parse_row(next_line) or is_table_noise(next_line):
                    break
                desc_parts.append(next_line)
                lookahead += 1

            if desc_parts:
                parsed["service_description"] = re.sub(
                    r"\s+",
                    " ",
                    " ".join(desc_parts),
                ).strip().upper()

            signature = (
                parsed["house_bl_number"],
                parsed["service_description"],
                parsed["quantity"],
                parsed["unit_price"],
                parsed["total_amount"],
            )
            if signature not in seen:
                seen.add(signature)
                rows.append(parsed)
            index = lookahead
            continue

        if not is_table_noise(line):
            pending_desc.append(line)
        index += 1

    return rows


def _apply_operation_invoice_scope(
    extracted_data: Dict[str, Any],
    raw_text: str,
    operation_bl: Optional[str],
) -> Dict[str, Any]:
    """When an invoice contains many HBLs, keep only rows for the selected operation."""
    target_bl = _clean_invoice_bl(operation_bl)
    if not target_bl:
        return extracted_data

    rows = _parse_invoice_charge_rows(raw_text)
    matching_rows = [
        row for row in rows
        if _clean_invoice_bl(row.get("house_bl_number")) == target_bl
    ]
    if not matching_rows:
        return extracted_data

    scoped = dict(extracted_data)
    scoped["house_bl_number"] = operation_bl
    scoped["currency"] = matching_rows[0].get("currency") or scoped.get("currency")
    scoped["line_items"] = [
        {
            "service_description": row["service_description"],
            "quantity": row["quantity"],
            "unit_price": row["unit_price"],
            "total_amount": row["total_amount"],
        }
        for row in matching_rows
    ]
    scoped["invoice_scope"] = {
        "operation_bl": operation_bl,
        "source": "deterministic_hbl_table_filter",
        "original_line_items_count": len(extracted_data.get("line_items") or []),
        "matched_line_items_count": len(scoped["line_items"]),
        "document_hbl_count": len({_clean_invoice_bl(row.get("house_bl_number")) for row in rows}),
    }
    return scoped


def _invoice_groups_from_table(raw_text: str) -> List[Dict[str, Any]]:
    rows = _parse_invoice_charge_rows(raw_text)
    grouped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        hbl = row["house_bl_number"]
        group = grouped.setdefault(
            hbl,
            {
                "house_bl_number": hbl,
                "cbm": row.get("cbm"),
                "kgs": row.get("kgs"),
                "line_items": [],
            },
        )
        group["line_items"].append({
            "service_description": row["service_description"],
            "quantity": row["quantity"],
            "unit_price": row["unit_price"],
            "total_amount": row["total_amount"],
        })
    return list(grouped.values())


def _invoice_cost_signature(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": str(item.get("service_description") or "Invoice Charge").strip(),
        "quantity": float(item.get("quantity") or 1),
        "unit_price": float(item.get("unit_price") or 0),
    }


def _find_existing_invoice_cost_line(
    client: DataverseClientService,
    operation_id: str,
    is_master: bool,
    vendor_invoice_number: Optional[str],
    item: Dict[str, Any],
) -> Optional[str]:
    if not vendor_invoice_number:
        return None
    sig = _invoice_cost_signature(item)
    op_field = "_mesco_master3_value" if is_master else "_mesco_operation_value"
    name = sig["name"].replace("'", "''")
    invoice = str(vendor_invoice_number).replace("'", "''")
    query = (
        "xollsp_quotecostlines?"
        "$select=xollsp_quotecostlineid"
        f"&$filter={op_field} eq {operation_id} "
        f"and mesco_vendorinvoicenumber eq '{invoice}' "
        f"and xollsp_name eq '{name}' "
        f"and xollsp_quantity eq {sig['quantity']} "
        f"and xollsp_unitamount eq {sig['unit_price']}"
        "&$top=1"
    )
    rows = client.get(query).json().get("value", [])
    if not rows:
        return None
    return rows[0].get("xollsp_quotecostlineid")


@app.post("/extract/invoice", response_model=InvoiceExtractResponse, tags=["Invoice Extraction"])
async def extract_invoice(
    file: Optional[UploadFile] = File(None, description="PDF or Image invoice file"),
    operation_id: Optional[str] = Form(None, description="Dynamics operation ID to post cost lines under"),
    current_bl: Optional[str] = Form(None, description="Current operation BL number to match against"),
    post_to_dataverse: bool = Form(False, description="Whether to post extracted items to Dynamics Dataverse"),
    llm_provider: Optional[LlmProviderQuery] = Form(
        None,
        description="AI backend: puter (browser page), azure, or gemini (defaults to LLM_PROVIDER in .env)",
    ),
    llm_model: Optional[GeminiModelQuery] = Form(
        None,
        description="Gemini model id when llm_provider=gemini",
    ),
    extracted_data_json: Optional[str] = Form(
        None,
        description="Optional pre-extracted JSON data (e.g. from Puter browser). If provided, LLM extraction is skipped."
    ),
):
    provider_val = llm_provider.value if llm_provider else None
    model_val = llm_model.value if llm_model else None
    try:
        import json
        from ai_extractor import extract_invoice_with_llm
        
        file_bytes = None
        filename = "browser_extracted.pdf"
        if file:
            file_bytes = await file.read()
            filename = file.filename
            extracted = extract_document_text_professionally(file_bytes, filename)
            raw_text = extracted.get("text", "")
            if not raw_text.strip() and not extracted_data_json:
                return InvoiceExtractResponse(success=False, error="No text extracted from file.")
        else:
            raw_text = ""
            if not extracted_data_json:
                return InvoiceExtractResponse(success=False, error="No file and no extracted data provided.")
        
        if extracted_data_json:
            extracted_data = json.loads(extracted_data_json)
        else:
            with llm_request_overrides(provider_val, model_val):
                extracted_data = extract_invoice_with_llm(raw_text, file_bytes=file_bytes, filename=filename)
        
        extracted_mbl = extracted_data.get("master_bl_number") or ""
        extracted_hbl = extracted_data.get("house_bl_number") or ""
        
        # Initialize variables for lookup
        resolved_op_id = operation_id
        resolved_op_code = None
        resolved_op_bl = current_bl
        op_bl_number = current_bl
        is_master = True
        tariff_quote_id = None
        
        from dataverse.client_service import DataverseClientService
        client = None
        try:
            client = DataverseClientService.get_instance()
        except Exception:
            pass

        # If no operation_id is provided, resolve by B/L in a deterministic order.
        # Prefer House B/L over Master B/L so multi-HBL debit notes do not post
        # every invoice row to the master operation.
        if not resolved_op_id and client and (current_bl or extracted_hbl or extracted_mbl):
            def find_operation_by_bl(bl_value: str, bl_type: Optional[int] = None) -> Optional[Dict[str, Any]]:
                safe_bl = str(bl_value or "").replace("'", "''").strip()
                if not safe_bl:
                    return None
                conditions = [
                    f"(mesco_masterblno eq '{safe_bl}' or mesco_code eq '{safe_bl}')",
                ]
                if bl_type is not None:
                    conditions.append(f"mesco_bltype eq {bl_type}")
                filter_str = " and ".join(conditions)
                url = (
                    "mesco_operations?"
                    "$select=mesco_operationid,mesco_code,mesco_bltype,mesco_masterblno,mesco_xollsp_TariffQuote"
                    f"&$filter={filter_str}&$top=1"
                )
                resp = client.get(url)
                if resp.status_code != 200:
                    return None
                rows = resp.json().get("value", [])
                return rows[0] if rows else None

            try:
                candidates: List[tuple[str, Optional[int]]] = []
                if current_bl:
                    candidates.append((current_bl, None))
                if extracted_hbl:
                    candidates.append((extracted_hbl, 886150002))
                    candidates.append((extracted_hbl, None))
                if extracted_mbl:
                    candidates.append((extracted_mbl, 886150001))
                    candidates.append((extracted_mbl, None))

                for bl_value, bl_type in candidates:
                    op = find_operation_by_bl(bl_value, bl_type)
                    if not op:
                        continue
                    resolved_op_id = op["mesco_operationid"]
                    resolved_op_code = op.get("mesco_code")
                    resolved_op_bl = op.get("mesco_masterblno")
                    is_master = op.get("mesco_bltype") == 886150001
                    op_bl_number = resolved_op_bl
                    if op.get("_mesco_xollsp_tariffquote_value"):
                        tariff_quote_id = op["_mesco_xollsp_tariffquote_value"]
                    break
            except Exception as search_err:
                logger.warning("Failed to lookup operation in Dataverse: %s", search_err)

        # Auto-create Operation structure if not found and post_to_dataverse is True
        if not resolved_op_id and post_to_dataverse and client and (extracted_mbl or extracted_hbl):
            try:
                master_id = None
                
                # 1. Resolve or create Master Operation if MBL is present
                if extracted_mbl:
                    # Search for existing Master Operation
                    mbl_url = f"mesco_operations?$select=mesco_operationid,mesco_code,mesco_xollsp_TariffQuote&$filter=mesco_masterblno eq '{extracted_mbl}' and mesco_bltype eq 886150001&$top=1"
                    mbl_resp = client.get(mbl_url)
                    if mbl_resp.status_code == 200:
                        mbl_results = mbl_resp.json().get("value", [])
                        if mbl_results:
                            master_id = mbl_results[0]["mesco_operationid"]
                            tariff_quote_id = mbl_results[0].get("_mesco_xollsp_tariffquote_value")
                    
                    if not master_id:
                        # Create Master Operation
                        op_fields = {
                            "mesco_code": extracted_mbl,
                            "mesco_masterblno": extracted_mbl,
                            "mesco_bltype": 886150001,  # Master B/L
                        }
                        master_id = _create_entity(client, "mesco_operations", op_fields)
                        logger.info("Auto-created Master Operation record: %s for MBL %s", master_id, extracted_mbl)
                
                # 2. Resolve or create House Operation if HBL is present
                if extracted_hbl:
                    house_id = None
                    hbl_url = f"mesco_operations?$select=mesco_operationid,mesco_code,mesco_xollsp_TariffQuote&$filter=mesco_masterblno eq '{extracted_hbl}' and mesco_bltype eq 886150002&$top=1"
                    hbl_resp = client.get(hbl_url)
                    if hbl_resp.status_code == 200:
                        hbl_results = hbl_resp.json().get("value", [])
                        if hbl_results:
                            house_id = hbl_results[0]["mesco_operationid"]
                            tariff_quote_id = hbl_results[0].get("_mesco_xollsp_tariffquote_value")
                    
                    if not house_id:
                        # Create House Operation
                        op_fields = {
                            "mesco_code": extracted_hbl,
                            "mesco_masterblno": extracted_hbl,
                            "mesco_bltype": 886150002,  # House B/L
                        }
                        if master_id:
                            op_fields["mesco_Operation@odata.bind"] = f"/mesco_operations({master_id})"
                        house_id = _create_entity(client, "mesco_operations", op_fields)
                        logger.info("Auto-created House Operation record: %s for HBL %s", house_id, extracted_hbl)
                    
                    resolved_op_id = house_id
                    resolved_op_code = extracted_hbl
                    resolved_op_bl = extracted_hbl
                    is_master = False
                    op_bl_number = extracted_hbl
                else:
                    # No HBL, use the Master Operation
                    resolved_op_id = master_id
                    resolved_op_code = extracted_mbl
                    resolved_op_bl = extracted_mbl
                    is_master = True
                    op_bl_number = extracted_mbl
                    
            except Exception as create_err:
                logger.exception("Failed to auto-create Operation structure: %s", create_err)

        # Fetch operation details if not resolved yet (e.g. if operation_id was passed explicitly)
        if resolved_op_id and client and not resolved_op_code:
            try:
                op_url = f"mesco_operations({resolved_op_id})?$select=mesco_code,mesco_bltype,mesco_masterblno,mesco_xollsp_TariffQuote"
                op_resp = client.get(op_url)
                if op_resp.status_code == 200:
                    op_data = op_resp.json()
                    resolved_op_code = op_data.get("mesco_code")
                    resolved_op_bl = op_data.get("mesco_masterblno")
                    op_bl_number = resolved_op_bl
                    is_master = op_data.get("mesco_bltype") == 886150001
                    if op_data.get("_mesco_xollsp_tariffquote_value"):
                        tariff_quote_id = op_data["_mesco_xollsp_tariffquote_value"]
            except Exception as metadata_err:
                logger.warning("Failed to fetch resolved operation details: %s", metadata_err)

        if resolved_op_bl:
            extracted_data = _apply_operation_invoice_scope(
                extracted_data,
                raw_text,
                resolved_op_bl,
            )

        # Build direct web URL to the Operation record in Dynamics
        dynamics_url = None
        if resolved_op_id:
            dynamics_url = f"{settings.base_url}/main.aspx?pagetype=entityrecord&etn=mesco_operation&id={resolved_op_id}"

        # Compare BL number
        is_matched = None
        if op_bl_number:
            clean_curr = re.sub(r"[^a-z0-9]", "", op_bl_number.lower())
            clean_mbl = re.sub(r"[^a-z0-9]", "", extracted_mbl.lower()) if extracted_mbl else ""
            clean_hbl = re.sub(r"[^a-z0-9]", "", extracted_hbl.lower()) if extracted_hbl else ""
            
            is_matched = (
                (clean_mbl and (clean_mbl in clean_curr or clean_curr in clean_mbl)) or
                (clean_hbl and (clean_hbl in clean_curr or clean_curr in clean_hbl))
            )
        
        dataverse_results = []
        dataverse_error = None
        
        if post_to_dataverse and resolved_op_id and client:
            try:
                invoice_container = _ensure_invoice_container(
                    client,
                    resolved_op_id,
                    extracted_data,
                )
                if invoice_container:
                    dataverse_results.append({
                        "type": "container",
                        "success": True,
                        **invoice_container,
                    })
                
                # Fetch currencies list and services list from crm to map
                services_resp = client.get("xollsp_servicedefinitions?$select=xollsp_servicedefinitionid,xollsp_name")
                services_list = services_resp.json().get("value", [])
                
                currencies_resp = client.get("transactioncurrencies?$select=transactioncurrencyid,currencyname,isocurrencycode,exchangerate")
                currencies_list = currencies_resp.json().get("value", [])
                
                vendors_resp = client.get("mesco_shippinglines?$select=mesco_shippinglineid,mesco_name")
                vendors_list = vendors_resp.json().get("value", [])
                
                def fuzzy_match(q, options, key_id, key_name, second_key=None, fallback_first=False):
                    if not q:
                        return None
                    q_clean = re.sub(r"[^a-z0-9]", "", q.lower())
                    if not q_clean:
                        return None
                    for opt in options:
                        lbl = opt.get(key_name) or ""
                        lbl_clean = re.sub(r"[^a-z0-9]", "", lbl.lower())
                        if lbl_clean and lbl_clean == q_clean:
                            return opt[key_id]
                        if second_key and opt.get(second_key):
                            scnd = opt[second_key]
                            scnd_clean = re.sub(r"[^a-z0-9]", "", scnd.lower())
                            if scnd_clean and scnd_clean == q_clean:
                                return opt[key_id]
                    # Partial match
                    for opt in options:
                        lbl = opt.get(key_name) or ""
                        lbl_clean = re.sub(r"[^a-z0-9]", "", lbl.lower())
                        if lbl_clean and (q_clean in lbl_clean or lbl_clean in q_clean):
                            return opt[key_id]
                    return options[0][key_id] if fallback_first and options else None

                # Extract and post each item
                ext_currency = extracted_data.get("currency")
                currency_id = fuzzy_match(
                    ext_currency,
                    currencies_list,
                    "transactioncurrencyid",
                    "isocurrencycode",
                    "currencyname",
                    fallback_first=True,
                )
                
                # Find exchange rate
                ex_rate = 1.0
                if currency_id:
                    for cur in currencies_list:
                        if cur.get("transactioncurrencyid") == currency_id:
                            ex_rate = float(cur.get("exchangerate") or 1.0)
                            break

                ext_vendor = extracted_data.get("vendor_name")
                vendor_id = fuzzy_match(ext_vendor, vendors_list, "mesco_shippinglineid", "mesco_name")

                for item in extracted_data.get("line_items", []):
                    desc = item.get("service_description") or "Invoice Charge"
                    matched_srv = fuzzy_match(desc, services_list, "xollsp_servicedefinitionid", "xollsp_name")
                    
                    qty = float(item.get("quantity") or 1)
                    u_price = float(item.get("unit_price") or 0)
                    
                    payload = {
                        "xollsp_name": desc,
                        "xollsp_quantity": qty,
                        "xollsp_unitamount": u_price,
                        "xollsp_unitamountbase": u_price / ex_rate if ex_rate else u_price,
                        "xollsp_fixedamount": 0,
                        "xollsp_fixedamountbase": 0,
                        "mesco_servicecategory": 886150006, # Others
                        "mesco_vendorinvoicenumber": extracted_data.get("vendor_invoice_number"),
                    }
                    
                    # Bind lookups
                    if matched_srv:
                        payload["xollsp_LogisticService@odata.bind"] = f"/xollsp_servicedefinitions({matched_srv})"
                    if currency_id:
                        payload["transactioncurrencyid@odata.bind"] = f"/transactioncurrencies({currency_id})"
                        payload["xollsp_Currency@odata.bind"] = f"/transactioncurrencies({currency_id})"
                    if vendor_id:
                        payload["mesco_invoicevendor_shippingline@odata.bind"] = f"/mesco_shippinglines({vendor_id})"
                    if tariff_quote_id:
                        payload["xollsp_TariffQuote@odata.bind"] = f"/xollsp_tariffquotes({tariff_quote_id})"
                    if invoice_container and invoice_container.get("id"):
                        payload["mesco_Container@odata.bind"] = f"/mesco_containers({invoice_container['id']})"
                    
                    if is_master:
                        payload["mesco_Master3@odata.bind"] = f"/mesco_operations({resolved_op_id})"
                    else:
                        payload["mesco_Operation@odata.bind"] = f"/mesco_operations({resolved_op_id})"
                    
                    existing_cost_line_id = _find_existing_invoice_cost_line(
                        client,
                        resolved_op_id,
                        is_master,
                        extracted_data.get("vendor_invoice_number"),
                        item,
                    )
                    if existing_cost_line_id:
                        update_payload = dict(payload)
                        if not matched_srv:
                            update_payload["xollsp_LogisticService@odata.bind"] = None
                        patch_resp = client.patch(
                            f"xollsp_quotecostlines({existing_cost_line_id})",
                            json=update_payload,
                        )
                        dataverse_results.append({
                            "success": True,
                            "status_code": patch_resp.status_code,
                            "action": "updated",
                            "id": existing_cost_line_id,
                        })
                    else:
                        post_resp = client.post("xollsp_quotecostlines", json=payload)
                        if post_resp.status_code in (200, 201):
                            created_body = post_resp.json()
                            created_body.setdefault("success", True)
                            created_body.setdefault("action", "created")
                            dataverse_results.append(created_body)
                        elif post_resp.status_code == 204:
                            dataverse_results.append({
                                "success": True,
                                "status_code": post_resp.status_code,
                                "action": "created",
                                "entity_url": post_resp.headers.get("OData-EntityId"),
                            })
                        else:
                            dataverse_results.append({
                                "success": False,
                                "status_code": post_resp.status_code,
                                "error": post_resp.text,
                            })
            except Exception as e:
                response = getattr(e, "response", None)
                response_text = getattr(response, "text", None)
                dataverse_error = f"{e}: {response_text}" if response_text else str(e)
                logger.exception("Failed to post cost lines to Dynamics")
        
        # Include resolved_op_id in response metadata if needed
        if resolved_op_id and not extracted_data.get("operation_id"):
            extracted_data["resolved_operation_id"] = resolved_op_id
        
        return InvoiceExtractResponse(
            success=True,
            data=extracted_data,
            is_bl_matched=is_matched,
            dataverse_results=dataverse_results,
            dataverse_error=dataverse_error,
            resolved_operation_id=resolved_op_id,
            resolved_operation_code=resolved_op_code,
            resolved_operation_bl=resolved_op_bl,
            dynamics_url=dynamics_url,
        )
    except Exception as exc:
        logger.exception("Invoice extraction endpoint failed")
        return InvoiceExtractResponse(success=False, error=str(exc))


# ---------------------------------------------------------------------------
# Multi-HBL Invoice / Debit Note Extraction Endpoint
# ---------------------------------------------------------------------------

class MultiInvoiceGroupResult(BaseModel):
    house_bl_number: Optional[str] = None
    cbm: Optional[float] = None
    kgs: Optional[float] = None
    line_items_count: int = 0
    resolved_operation_id: Optional[str] = None
    resolved_operation_code: Optional[str] = None
    dynamics_url: Optional[str] = None
    posted_count: int = 0
    errors: List[str] = []


class MultiInvoiceExtractResponse(BaseModel):
    success: bool
    vendor_name: Optional[str] = None
    vendor_invoice_number: Optional[str] = None
    master_bl_number: Optional[str] = None
    container_number: Optional[str] = None
    seal_number: Optional[str] = None
    currency: Optional[str] = None
    groups_count: int = 0
    total_line_items: int = 0
    total_posted: int = 0
    groups: List[MultiInvoiceGroupResult] = []
    error: Optional[str] = None
    dataverse_error: Optional[str] = None


@app.post("/extract/invoice/multi", response_model=MultiInvoiceExtractResponse, tags=["Invoice Extraction"])
async def extract_invoice_multi(
    file: Optional[UploadFile] = File(None, description="PDF or Image invoice/debit note file"),
    operation_id: Optional[str] = Form(None, description="Fallback Dynamics operation ID if HBL lookup fails"),
    post_to_dataverse: bool = Form(False, description="Whether to post extracted items to Dynamics Dataverse"),
    llm_provider: Optional[LlmProviderQuery] = Form(
        None,
        description="AI backend: puter (browser page), azure, or gemini (defaults to LLM_PROVIDER in .env)",
    ),
    llm_model: Optional[GeminiModelQuery] = Form(
        None,
        description="Gemini model id when llm_provider=gemini",
    ),
    extracted_data_json: Optional[str] = Form(
        None,
        description="Optional pre-extracted JSON data (e.g. from Puter browser). If provided, LLM extraction is skipped."
    ),
):
    """Extract a multi-HBL invoice/debit note and post cost lines per HBL group."""
    provider_val = llm_provider.value if llm_provider else None
    model_val = llm_model.value if llm_model else None
    try:
        import json
        from ai_extractor import extract_multi_invoice_with_llm

        file_bytes = None
        filename = "browser_extracted.pdf"
        if file:
            file_bytes = await file.read()
            filename = file.filename
            extracted = extract_document_text_professionally(file_bytes, filename)
            raw_text = extracted.get("text", "")
            if not raw_text.strip() and not extracted_data_json:
                return MultiInvoiceExtractResponse(success=False, error="No text extracted from file.")
        else:
            raw_text = ""
            if not extracted_data_json:
                return MultiInvoiceExtractResponse(success=False, error="No file and no extracted data provided.")

        if extracted_data_json:
            extracted_data = json.loads(extracted_data_json)
        else:
            with llm_request_overrides(provider_val, model_val):
                extracted_data = extract_multi_invoice_with_llm(raw_text, file_bytes=file_bytes, filename=filename)

        vendor_name = extracted_data.get("vendor_name")
        vendor_invoice_number = extracted_data.get("vendor_invoice_number")
        master_bl_number = extracted_data.get("master_bl_number") or ""
        container_number = extracted_data.get("container_number")
        seal_number = extracted_data.get("seal_number")
        currency = extracted_data.get("currency")
        groups_raw = extracted_data.get("groups") or []
        deterministic_groups = _invoice_groups_from_table(raw_text)
        if deterministic_groups:
            groups_raw = deterministic_groups
            extracted_data["groups"] = deterministic_groups
            extracted_data["invoice_scope"] = {
                "source": "deterministic_hbl_table_groups",
                "groups_count": len(deterministic_groups),
                "line_items_count": sum(len(g.get("line_items") or []) for g in deterministic_groups),
            }

        total_line_items = sum(len(g.get("line_items", [])) for g in groups_raw)

        # Initialize Dataverse client
        from dataverse.client_service import DataverseClientService
        client = None
        try:
            client = DataverseClientService.get_instance()
        except Exception:
            pass

        # Resolve fallback operation from MBL or provided operation_id
        fallback_op_id = operation_id
        fallback_op_code = None
        fallback_is_master = True
        fallback_tariff_quote_id = None

        if client and (fallback_op_id or master_bl_number):
            if fallback_op_id and not fallback_op_code:
                try:
                    op_url = f"mesco_operations({fallback_op_id})?$select=mesco_code,mesco_bltype,mesco_masterblno,mesco_xollsp_TariffQuote"
                    op_resp = client.get(op_url)
                    if op_resp.status_code == 200:
                        op_data = op_resp.json()
                        fallback_op_code = op_data.get("mesco_code")
                        fallback_is_master = op_data.get("mesco_bltype") == 886150001
                        if op_data.get("_mesco_xollsp_tariffquote_value"):
                            fallback_tariff_quote_id = op_data["_mesco_xollsp_tariffquote_value"]
                except Exception:
                    pass

            if not fallback_op_id and master_bl_number:
                try:
                    search_url = (
                        f"mesco_operations?$select=mesco_operationid,mesco_code,mesco_bltype,mesco_masterblno,mesco_xollsp_TariffQuote"
                        f"&$filter=mesco_masterblno eq '{master_bl_number}'&$top=1"
                    )
                    search_resp = client.get(search_url)
                    if search_resp.status_code == 200:
                        results = search_resp.json().get("value", [])
                        if results:
                            op = results[0]
                            fallback_op_id = op["mesco_operationid"]
                            fallback_op_code = op.get("mesco_code")
                            fallback_is_master = op.get("mesco_bltype") == 886150001
                            if op.get("_mesco_xollsp_tariffquote_value"):
                                fallback_tariff_quote_id = op["_mesco_xollsp_tariffquote_value"]
                except Exception as e:
                    logger.warning("Failed to lookup fallback operation by MBL: %s", e)

        # Auto-create fallback Master Operation if not found and post_to_dataverse is True
        if not fallback_op_id and post_to_dataverse and client and master_bl_number:
            try:
                op_fields = {
                    "mesco_code": master_bl_number,
                    "mesco_masterblno": master_bl_number,
                    "mesco_bltype": 886150001,  # Master B/L
                }
                fallback_op_id = _create_entity(client, "mesco_operations", op_fields)
                fallback_op_code = master_bl_number
                fallback_is_master = True
                logger.info("Auto-created fallback Master Operation record: %s for B/L %s", fallback_op_id, master_bl_number)
            except Exception as create_err:
                logger.exception("Failed to auto-create fallback Master Operation record: %s", create_err)

        # Pre-fetch reference lists for Dataverse posting
        services_list = []
        currencies_list = []
        vendors_list = []
        currency_id = None
        vendor_id = None
        ex_rate = 1.0

        if post_to_dataverse and client:
            try:
                services_resp = client.get("xollsp_servicedefinitions?$select=xollsp_servicedefinitionid,xollsp_name")
                services_list = services_resp.json().get("value", [])

                currencies_resp = client.get("transactioncurrencies?$select=transactioncurrencyid,currencyname,isocurrencycode,exchangerate")
                currencies_list = currencies_resp.json().get("value", [])

                vendors_resp = client.get("mesco_shippinglines?$select=mesco_shippinglineid,mesco_name")
                vendors_list = vendors_resp.json().get("value", [])
            except Exception as e:
                logger.warning("Failed to pre-fetch reference lists: %s", e)

            def fuzzy_match(q, options, key_id, key_name, second_key=None, fallback_first=False):
                if not q:
                    return None
                q_clean = re.sub(r"[^a-z0-9]", "", q.lower())
                if not q_clean:
                    return None
                for opt in options:
                    lbl = opt.get(key_name) or ""
                    lbl_clean = re.sub(r"[^a-z0-9]", "", lbl.lower())
                    if lbl_clean and lbl_clean == q_clean:
                        return opt[key_id]
                    if second_key and opt.get(second_key):
                        scnd_clean = re.sub(r"[^a-z0-9]", "", opt[second_key].lower())
                        if scnd_clean and scnd_clean == q_clean:
                            return opt[key_id]
                for opt in options:
                    lbl = opt.get(key_name) or ""
                    lbl_clean = re.sub(r"[^a-z0-9]", "", lbl.lower())
                    if lbl_clean and (q_clean in lbl_clean or lbl_clean in q_clean):
                        return opt[key_id]
                return options[0][key_id] if fallback_first and options else None

            currency_id = fuzzy_match(currency, currencies_list, "transactioncurrencyid", "isocurrencycode", "currencyname", fallback_first=True)
            if currency_id:
                for cur in currencies_list:
                    if cur.get("transactioncurrencyid") == currency_id:
                        ex_rate = float(cur.get("exchangerate") or 1.0)
                        break

            vendor_id = fuzzy_match(vendor_name, vendors_list, "mesco_shippinglineid", "mesco_name")

        # Process each HBL group
        group_results: List[MultiInvoiceGroupResult] = []
        total_posted = 0
        dataverse_error = None

        for group in groups_raw:
            hbl = group.get("house_bl_number")
            line_items = group.get("line_items") or []
            gr = MultiInvoiceGroupResult(
                house_bl_number=hbl,
                cbm=group.get("cbm"),
                kgs=group.get("kgs"),
                line_items_count=len(line_items),
            )

            # Resolve operation for this HBL
            group_op_id = None
            group_op_code = None
            group_is_master = True
            group_tariff_quote_id = None

            if client and hbl:
                try:
                    hbl_filter = f"mesco_masterblno eq '{hbl}'"
                    hbl_url = (
                        f"mesco_operations?$select=mesco_operationid,mesco_code,mesco_bltype,mesco_masterblno,mesco_xollsp_TariffQuote"
                        f"&$filter={hbl_filter}&$top=1"
                    )
                    hbl_resp = client.get(hbl_url)
                    if hbl_resp.status_code == 200:
                        hbl_results = hbl_resp.json().get("value", [])
                        if hbl_results:
                            hop = hbl_results[0]
                            group_op_id = hop["mesco_operationid"]
                            group_op_code = hop.get("mesco_code")
                            group_is_master = hop.get("mesco_bltype") == 886150001
                            if hop.get("_mesco_xollsp_tariffquote_value"):
                                group_tariff_quote_id = hop["_mesco_xollsp_tariffquote_value"]
                except Exception as e:
                    logger.warning("Failed to resolve operation for HBL %s: %s", hbl, e)

            # Auto-create House Operation if not found and post_to_dataverse is True
            if not group_op_id and post_to_dataverse and client and hbl:
                try:
                    group_fields = {
                        "mesco_code": hbl,
                        "mesco_masterblno": hbl,
                        "mesco_bltype": 886150002,  # House B/L
                    }
                    if fallback_op_id:
                        group_fields["mesco_Operation@odata.bind"] = f"/mesco_operations({fallback_op_id})"
                    group_op_id = _create_entity(client, "mesco_operations", group_fields)
                    group_op_code = hbl
                    group_is_master = False
                    logger.info("Auto-created House Operation record: %s for HBL %s", group_op_id, hbl)
                except Exception as create_err:
                    logger.exception("Failed to auto-create House Operation record: %s", create_err)

            # Fallback to MBL/provided operation
            if not group_op_id:
                group_op_id = fallback_op_id
                group_op_code = fallback_op_code
                group_is_master = fallback_is_master
                group_tariff_quote_id = fallback_tariff_quote_id

            gr.resolved_operation_id = group_op_id
            gr.resolved_operation_code = group_op_code
            if group_op_id:
                gr.dynamics_url = f"{settings.base_url}/main.aspx?pagetype=entityrecord&etn=mesco_operation&id={group_op_id}"

            # Post cost lines for this group
            if post_to_dataverse and group_op_id and client:
                # Ensure container for this group
                container_data = {"container_number": container_number, "seal_number": seal_number}
                invoice_container = None
                try:
                    invoice_container = _ensure_invoice_container(client, group_op_id, container_data)
                except Exception as e:
                    gr.errors.append(f"Container creation failed: {e}")

                for item in line_items:
                    desc = item.get("service_description") or "Invoice Charge"
                    try:
                        matched_srv = fuzzy_match(desc, services_list, "xollsp_servicedefinitionid", "xollsp_name") if services_list else None

                        qty = float(item.get("quantity") or 1)
                        u_price = float(item.get("unit_price") or 0)

                        payload = {
                            "xollsp_name": desc,
                            "xollsp_quantity": qty,
                            "xollsp_unitamount": u_price,
                            "xollsp_unitamountbase": u_price / ex_rate if ex_rate else u_price,
                            "xollsp_fixedamount": 0,
                            "xollsp_fixedamountbase": 0,
                            "mesco_servicecategory": 886150006,
                            "mesco_vendorinvoicenumber": vendor_invoice_number,
                        }

                        if matched_srv:
                            payload["xollsp_LogisticService@odata.bind"] = f"/xollsp_servicedefinitions({matched_srv})"
                        if currency_id:
                            payload["transactioncurrencyid@odata.bind"] = f"/transactioncurrencies({currency_id})"
                            payload["xollsp_Currency@odata.bind"] = f"/transactioncurrencies({currency_id})"
                        if vendor_id:
                            payload["mesco_invoicevendor_shippingline@odata.bind"] = f"/mesco_shippinglines({vendor_id})"
                        if group_tariff_quote_id:
                            payload["xollsp_TariffQuote@odata.bind"] = f"/xollsp_tariffquotes({group_tariff_quote_id})"
                        if invoice_container and invoice_container.get("id"):
                            payload["mesco_Container@odata.bind"] = f"/mesco_containers({invoice_container['id']})"

                        if group_is_master:
                            payload["mesco_Master3@odata.bind"] = f"/mesco_operations({group_op_id})"
                        else:
                            payload["mesco_Operation@odata.bind"] = f"/mesco_operations({group_op_id})"

                        existing_cost_line_id = _find_existing_invoice_cost_line(
                            client,
                            group_op_id,
                            group_is_master,
                            vendor_invoice_number,
                            item,
                        )
                        if existing_cost_line_id:
                            update_payload = dict(payload)
                            if not matched_srv:
                                update_payload["xollsp_LogisticService@odata.bind"] = None
                            client.patch(
                                f"xollsp_quotecostlines({existing_cost_line_id})",
                                json=update_payload,
                            )
                            gr.posted_count += 1
                            total_posted += 1
                        else:
                            post_resp = client.post("xollsp_quotecostlines", json=payload)
                            if post_resp.status_code not in (200, 201, 204):
                                gr.errors.append(f"POST {desc}: {post_resp.status_code} {post_resp.text[:200]}")
                                continue
                            gr.posted_count += 1
                            total_posted += 1
                    except Exception as e:
                        gr.errors.append(f"POST {desc}: {e}")

            group_results.append(gr)

        return MultiInvoiceExtractResponse(
            success=True,
            vendor_name=vendor_name,
            vendor_invoice_number=vendor_invoice_number,
            master_bl_number=master_bl_number,
            container_number=container_number,
            seal_number=seal_number,
            currency=currency,
            groups_count=len(group_results),
            total_line_items=total_line_items,
            total_posted=total_posted,
            groups=group_results,
        )
    except Exception as exc:
        logger.exception("Multi-invoice extraction endpoint failed")
        return MultiInvoiceExtractResponse(success=False, error=str(exc))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

