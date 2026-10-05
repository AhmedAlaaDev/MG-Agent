# Extraction pipeline

This page describes how a document becomes Dynamics-ready JSON. The orchestrator is `app.application.document_parser.parse_document_intelligently`, called from `app.api.routers.extract` (and the batch processor).

## 1. Bytes → text

`app.infrastructure.spreadsheet.spreadsheet_extractor.extract_document_text_professionally` routes by extension:

| Input | Module | Behavior |
| ----- | ------ | -------- |
| PDF | `infrastructure.pdf.pdf_extractor` | PyMuPDF native text. If the page is too sparse, render at `OCR_DPI` and run Tesseract (`ocr_extractor`). |
| XLSX / XLS / CSV | same spreadsheet module | Sheet/row records with header maps, cell limits from settings. |

Quality metadata includes native character counts, field hits, page count, and OCR warnings. Gemini can also receive the **original file bytes** (`GEMINI_NATIVE_PDF` / `GEMINI_NATIVE_SPREADSHEET`) so the model sees tables and layout, not only OCR text.

Long text is chunked on `--- PAGE N ---` / `--- SHEET: ---` / line boundaries when it exceeds `GEMINI_MAX_INPUT_CHARS` (`ai_extractor._chunk_long_text`). Chunk JSON is merged so rows are not silently truncated.

## 2. Deterministic parsers

`app.application.pdf_deterministic_registry.best_deterministic_parse` tries layout-specific parsers and scores them. Evidence from a high-confidence parser is preferred over Gemini for identifiers, route, containers, and master/house structure.

| Parser module | Typical document |
| ------------- | ---------------- |
| `pdf_standard_master_bl` | Carrier master B/L (e.g. CMA CGM) |
| `pdf_house_bl` | Standard house B/L |
| `pdf_sea_waybill` | Consolidation sea waybill + attached list |
| `pdf_cargo_manifest` | NSA-style cargo manifest HBL blocks |
| `pdf_tur_cargo_manifest` | Turkish cargo manifest |
| `pdf_lcl_export_manifest` | Export LCL manifest |
| `pdf_consolidated_lcl` | One master + shared container + N houses |
| `pdf_isaly_draft_bl` | ISALY / STAR CONCORD draft (one B/L per page) |
| `pdf_multi_bl` | CamScanner-style multi-B/L PDFs |
| `pdf_debit_note` | Freight debit note |
| `pdf_msds_dg` | DG / MSDS IMO-UN |
| `pdf_groupage_cargo` | Groupage per-shipper cargo lines |
| `pdf_attached_list` | House refs on an attached list |

To add a parser: implement `is_*` + `parse_*` in `app/infrastructure/pdf/parsers/`, register it in `pdf_deterministic_registry.py`, and add tests under `tests/`.

## 3. Gemini

`app.infrastructure.ai.ai_extractor` sends a JSON schema (`MULTI_BL_JSON_SCHEMA`) and a system prompt. Older import names `extract_with_azure_openai` / `extract_records_with_azure_openai` are aliases for the Gemini implementations.

Invoice extraction uses `extract_invoice_with_llm` and `extract_multi_invoice_with_llm`. WE-CAN Excel invoices skip this step.

## 4. Validation and domain rules

`app.domain.rules.validator.validate_and_correct` fills gaps with regex (BL number, ACID, containers, vessel/voyage, ports), formats container numbers, merges IMO/UN, infers BL status, and calls:

- `bl_number_rules` — drop form/serial false positives; keep canonical page B/Ls
- `pdf_bl_enrichment` — cargo description, HS codes, consignee blocks from PDF text
- `custom_business_rules` — see below

## 5. Reconciliation

`intelligent_reconciler` and `record_reconciliation` merge LLM records with deterministic records field-by-field. Identifiers from parsers win when both sources have a value. Duplicate B/Ls are collapsed.

## 6. CRM projection

`crm_output_formatter.records_to_master_json` / `records_to_house_json` build the Dynamics graph:

- Master operation fields (`mesco_*`, `cr401_*`)
- Nested houses (`mesco_Operation_mesco_Operation_mesco_Operation`)
- Containers and cargo collections
- Lookups left as text for the uploader to resolve

`bl_type` stamps `mesco_bltype`. Custom rules then adjust booking term, freight payable at, load type, LCL TEUs (houses → 0), and master totals as the sum of houses.

`dataverse.field_limits.cap_nested_payload` truncates strings to Dataverse column lengths so one oversized cargo description cannot fail the entire POST.

## Custom business rules

Enabled by `CUSTOM_BUSINESS_RULES_ENABLED` and per-request `apply_custom_rules`.

| Document signal | CRM effect |
| --------------- | ---------- |
| Freight Prepaid | Booking term Freehand, freight payable at Origin |
| Freight Collect | Booking term Nomination, freight payable at Destination |
| Consolidation / CFS / manifest language | Load type LCL vs FCL |
| LCL house | `cr401_totalteus = 0` |
| Multi-house master | Totals = sum of house rows |

Implementation: `app.domain.rules.custom_business_rules`.

## Spreadsheet workbooks

If the workbook has per-row HBL records, the extractor either:

1. Sends the **whole workbook** to Gemini when row count ≥ `GEMINI_WORKBOOK_LLM_MIN_ROWS` (better for long LCL manifests), or
2. Processes rows individually (direct header map, then LLM fallback).

Helpers for Excel serial dates, manifest context, and address detection live in `app.api.routers.extract` today (still next to the HTTP handler).
