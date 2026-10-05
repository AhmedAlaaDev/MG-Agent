"""Static HTML explorer pages."""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.core.paths import read_web_page

router = APIRouter()

@router.get("/", response_class=HTMLResponse)
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
      <h2>Gemini AI Extractor</h2>
      <p>Server-side Gemini API extraction is used for all uploaded documents.</p>
      <form action="/docs" method="get">
        <button type="submit">Open API Docs</button>
      </form>
      <h2>Quick Test</h2>
      <form action="/extract/file" method="post" enctype="multipart/form-data">
        <input type="file" name="file" accept=".pdf,.xlsx,.xls,.csv" required>
        <button type="submit">Extract B/L</button>
      </form>
      <h2>Invoice Review</h2>
      <p><a href="/invoice">Open invoice upload and review</a> (dry-run extraction, editable results, and explicit Dynamics posting).</p>
      <p><a href="/invoices/storage">Open SACO / Globelink storage invoices</a> (choose the vendor; Arabic and English PDFs).</p>
      <h2>Tariff Calculator</h2>
      <p><a href="/tariffs/test">Open tariff calculator</a> (editable SACO and GLOBELINK rates, inputs, and charge breakdown).</p>
      <h2>Batch PDF Test</h2>
      <p><a href="/test/pdf">Open batch upload page</a> (multipart form, multiple PDFs, no Dataverse).</p>
      <h2>Audit</h2>
      <p><a href="/audit">Open upload audit dashboard</a> (live upload log, saved files, and responses).</p>
    </body>
    </html>
    """


@router.get("/invoice", response_class=HTMLResponse, include_in_schema=False)
async def invoice_review_page() -> str:
    """Serve the invoice upload, dry-run review, and posting page."""
    try:
        return read_web_page("invoice_review.html")
    except FileNotFoundError:
        return """
        <!doctype html>
        <html><body>
          <h1>Invoice review is not available</h1>
          <p>The invoice_review.html file was not found on this deployment.</p>
        </body></html>
        """


@router.get("/invoices/storage", response_class=HTMLResponse, include_in_schema=False)
async def storage_invoice_page() -> str:
    """Serve the SACO / Globelink storage-invoice chooser."""
    try:
        return read_web_page("vendor_storage_invoice.html")
    except FileNotFoundError:
        return """
        <!doctype html>
        <html><body>
          <h1>Storage invoice upload is not available</h1>
          <p>The vendor_storage_invoice.html file was not found on this deployment.</p>
        </body></html>
        """


@router.get("/tariffs/test", response_class=HTMLResponse, include_in_schema=False)
async def tariff_test_page() -> str:
    """Serve the interactive SACO and GLOBELINK tariff calculator."""
    try:
        return read_web_page("tariff_test.html")
    except FileNotFoundError:
        return """
        <!doctype html>
        <html><body>
          <h1>Tariff calculator is not available</h1>
          <p>The tariff_test.html file was not found on this deployment.</p>
        </body></html>
        """


@router.get("/audit", response_class=HTMLResponse, include_in_schema=False)
async def upload_audit_page() -> str:
    """Serve the realtime upload audit dashboard."""
    try:
        return read_web_page("audit_view.html")
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


@router.get("/test/pdf", response_class=HTMLResponse, include_in_schema=False)
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



@router.get("/operation", response_class=HTMLResponse, include_in_schema=False)
async def operation_view_page() -> str:
    """Serve the operation review page that mirrors the Dynamics operation form."""
    try:
        return read_web_page("operation_view.html")
    except FileNotFoundError:
        return "<h1>operation_view.html not found</h1>"
