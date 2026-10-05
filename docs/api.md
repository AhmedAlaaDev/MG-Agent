# HTTP API

Interactive OpenAPI UI: `GET /docs`. ReDoc: `GET /redoc`.

Unless noted, JSON bodies use UTF-8. File uploads are `multipart/form-data`.

Gemini is the only AI provider. Per-request `llm_provider` / `llm_model` override `LLM_PROVIDER` / `GEMINI_MODEL` from `.env`.

## Health and configuration

### `GET /health`

Returns process status, configured Gemini model, whether a Gemini key is present, and whether Dataverse env vars are complete (`AZURE_APP_API_URL`, `TENANT_ID`, `CLIENT_ID`, `CLIENT_SECRET`).

### `GET /business-rules`

Describes CRM post-extraction rules and the `CUSTOM_BUSINESS_RULES_ENABLED` / `apply_custom_rules` toggles.

### `GET /llm/models`

Lists allowed Gemini model ids (`app.core.config.GEMINI_MODELS`).

## Bill of Lading extraction

Shared extract response (`ExtractResponse`):

| Field | Meaning |
| ----- | ------- |
| `success` | Extraction completed without a hard failure |
| `data` | Master-shaped CRM JSON (or the house payload when `bl_type=house` and there is a single house) |
| `house_data` | House-shaped CRM JSON when produced |
| `records` | List of master payloads when multiple B/Ls were found |
| `raw_text` | Truncated OCR/native text preview |
| `extraction_quality` | Source, scores, LLM usage, `bl_type` |
| `dataverse_result` / `dataverse_error` | Upload outcome when `post_to_dataverse=true` |

`bl_type` is `master` (Dynamics `mesco_bltype` 886150001) or `house` (886150002).

### `POST /extract/file`

Primary endpoint. Accepts PDF, XLSX, XLS, or CSV.

Form fields:

| Field | Default | Notes |
| ----- | ------- | ----- |
| `file` | required | Document bytes |
| `bl_type` | `master` | Master vs house stamp |
| `llm_provider` | `gemini` | Only `gemini` is valid |
| `llm_model` | settings default | Must be in `GEMINI_MODELS` |
| `post_to_dataverse` | `true` | Upsert after extraction |
| `download` | `false` | Return `crm_output.json` attachment instead of JSON body |
| `apply_custom_rules` | `true` | Freight/booking/load-type/TEU rules |

Uploads are logged in the audit store (original file + response).

### `POST /extract/pdf` and `POST /extract/excel`

Same pipeline as `/extract/file`. Query parameters instead of form fields for `bl_type`, LLM, Dataverse, and download flags.

### `POST /extract/text`

JSON body: `{ "ocr_text": "...", "bl_type": "master", "llm_provider": "gemini", "llm_model": null }`.

Does not upload to Dataverse.

### `POST /extract/master` and `POST /extract/house`

Convenience wrappers around `/extract/file` with `bl_type` fixed.

### `POST /extract/crm`

JSON body: `{ "crm_json": { ... } }` — a Dynamics `mesco_operation` payload (or house list). Maps CRM fields back into internal B/L records and validates them. Used to round-trip exported Dynamics JSON.

### `POST /test/pdf/batch`

Multiple PDFs. Extract + validate only (no Dataverse). Used by `GET /test/pdf`.

## Dataverse upload and read-back

### `POST /upload/dataverse`

Multipart file: previously downloaded CRM JSON (`?download=true`). Query: `bl_type`, `apply_custom_rules`.

### `POST /upload/dataverse/json`

Same as above with a JSON body equal to the `data` field from extract endpoints.

### `GET /dynamics/operation/{master_id}`

Master operation expanded with nested houses, containers, and cargo. OData annotations stripped.

### `GET /dynamics/operation/{master_id}/full`

Same expand, but **keeps** formatted-value and lookup annotations for the operation review UI.

### `GET /dynamics/operation/{master_id}/houses`

Houses where `_mesco_operation_value` equals the master id.

### `GET /dynamics/house/{house_id}`

Single house with containers and cargo.

### `POST /dynamics/compare`

Body: `{ "master_id": "...", "sent_payload": { ... } }`. Flattens sent vs saved fields for debugging lost mappings.

## Invoices

### `POST /extract/invoice`

Single invoice / debit note (PDF or image). Form: `file`, `current_bl`, `operation_id`, `post_to_dataverse`, LLM fields.

Looks up the Dynamics operation by MBL/HBL, maps vendor/service/currency, and can post quote cost lines.

### `POST /extract/invoice/multi`

Multi-HBL debit notes. Supports a `reviewed_json` form field: skip a second LLM call and post exactly the payload the user reviewed.

### `POST /extract/invoice/excel`

WE-CAN proxy invoice workbooks (`.xls` / `.xlsx`). Deterministic parser — no OCR and no LLM. Same posting path as multi-invoice.

## Audit and UI

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET | `/` | Simple upload form |
| GET | `/audit` | Live upload dashboard (`app/web/audit_view.html`) |
| GET | `/audit/uploads` | Paginated audit rows |
| GET | `/audit/uploads/{id}` | Full stored response |
| GET | `/audit/uploads/{id}/file` | Original upload |
| WS | `/audit/ws` | Realtime audit events |
| GET | `/operation` | Operation form mirror (`app/web/operation_view.html`) |
| GET | `/test/pdf` | Batch PDF tester |

## Examples

```bash
curl -X POST http://localhost:8000/extract/pdf \
  -F "file=@sample.pdf" \
  -F "bl_type=master" \
  -F "post_to_dataverse=false"

curl -X POST http://localhost:8000/extract/text \
  -H "Content-Type: application/json" \
  -d "{\"ocr_text\": \"BILL OF LADING NO. OOLU2309868980 ...\"}"

curl -X POST http://localhost:8000/extract/invoice/excel \
  -F "file=@wecan.xls" \
  -F "post_to_dataverse=false"
```
