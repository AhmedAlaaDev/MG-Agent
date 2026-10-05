# Configuration

Copy `.env.example` to `.env`. `app.core.config.Settings` loads `{project_root}/.env` regardless of the working directory of the current module.

## Gemini

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `GEMINI_API_KEY` | empty | Required for extraction |
| `GEMINI_MODEL` | `gemini-3-pro-preview` | Default model id |
| `LLM_PROVIDER` | `gemini` | Only `gemini` / `google` are accepted |
| `GEMINI_MAX_INPUT_CHARS` | `900000` | Chunk + merge above this |
| `GEMINI_NATIVE_PDF` | `true` | Send original PDF bytes to Gemini |
| `GEMINI_NATIVE_SPREADSHEET` | `true` | Send original workbook bytes |
| `GEMINI_INLINE_PDF_MAX_BYTES` | `18000000` | Larger files use the Files API |
| `GEMINI_WORKBOOK_LLM_MIN_ROWS` | `3` | Prefer one whole-workbook LLM call |

Allowed model ids are listed in `app.core.config.GEMINI_MODELS` and exposed at `GET /llm/models`. Invalid per-request `llm_model` values return `ExtractResponse.success=false`.

## OCR and PDF

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `OCR_DPI` | `300` | Page rasterization for Tesseract |
| `TESSERACT_LANG` | `eng` | Tesseract language |
| `TESSERACT_CMD` | empty | Windows: path to `tesseract.exe`. Docker sets `/usr/bin/tesseract` |
| `MAX_INPUT_CHARS` | `90000` | Legacy text budget (Gemini uses the larger Gemini cap) |
| `NATIVE_MIN_CHARS` | `600` | Below this, native PDF text is treated as too sparse → OCR |
| `NATIVE_MIN_FIELD_HITS` | `5` | Label hits required to trust native text |

## Excel

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `EXCEL_MAX_ROWS_PER_SHEET` | `2500` | Row cap per sheet |
| `EXCEL_MAX_COLS_PER_SHEET` | `80` | Column cap |
| `EXCEL_MAX_CELL_CHARS` | `500` | Cell truncation |

## Dataverse

| Variable | Purpose |
| -------- | ------- |
| `TENANT_ID` | Azure AD tenant |
| `CLIENT_ID` | App registration |
| `CLIENT_SECRET` | App secret |
| `BASE_URL` | Dynamics org URL (token audience + fallback API root) |
| `AZURE_APP_API_URL` | Full OData root (`.../api/data/v9.2`) |

`GET /health` reports `dataverse_configured` when the four Dataverse vars plus API URL are set.

## Business rules and audit

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `CUSTOM_BUSINESS_RULES_ENABLED` | `true` | Prepaid/Collect, FCL/LCL, TEUs, house totals |
| `BL_AUDIT_DIR` | `upload_audit` | Writable audit directory; falls back to `/tmp/bl_upload_audit` |
| `API_PORT` | `8000` | Host port in Docker Compose |
| `PORT` | `8080` in Dockerfile | Listen port inside the default Debian image |

Per-request override for rules: form/query `apply_custom_rules`.

## Docker apt mirrors

Build args `APT_MIRROR` and `APT_SECURITY_MIRROR` default to `ftp.debian.org` / `security.debian.org`. Override in `.env` if `apt-get update` returns 403, or use `docker-compose.ubuntu.yml`.

## Runtime notes

- `.env`, `.venv`, `upload_audit/`, `logs/`, `tmp/`, and `*.pdf` are gitignored.
- Do not put secrets in code or in `samples/`.
- Puter.js browser extraction is **not** used by the API anymore (`uses_puter()` is a compatibility stub that returns `false`). `app/web/puter_extract.html` is a leftover UI file.
