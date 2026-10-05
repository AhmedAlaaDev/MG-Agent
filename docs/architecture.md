# Architecture

The service is organized as a FastAPI application with **strict layering**. Outer layers may depend on inner layers; inner layers must not import HTTP routers.

```
HTTP (app.api)
    → Application (app.application)
        → Domain (app.domain)
    → Infrastructure (app.infrastructure)
        → Domain, Core
Core (app.core) is shared configuration used by every layer.
```

`main.py` at the repository root is only an ASGI shim:

```python
from app.main import app
```

`app/main.py` builds the FastAPI instance and includes routers. Docker, Vercel, Railway, and `uvicorn main:app` are unchanged.

## Directory map

```
app/
  main.py                 FastAPI app + CORS + router includes
  api/
    schemas.py            Shared Pydantic request/response models
    routers/
      ui.py               HTML explorer pages
      health.py           /health, /business-rules, /llm/models
      audit.py            Upload audit REST + websocket
      extract.py          B/L extraction endpoints + pipeline helpers
      dynamics.py         Dataverse upload and operation read-back
      invoices.py         Invoice / debit-note extract + cost posting
    web/                  Static HTML (audit, operation review, legacy Puter page)
  core/
    config.py             pydantic-settings (env file at project root)
    paths.py              PROJECT_ROOT, WEB_DIR
    llm_context.py        Per-request Gemini provider/model overrides
    llm_models.py         OpenAPI enums for model ids
  domain/
    models.py             BLEntity / ContainerItem
    imo_extractor.py      IMO class / UN number
    ocr_cargo_fields.py   Cargo field hints from OCR
    rules/
      validator.py        Corrections, regex fallbacks, enrichment hooks
      bl_number_rules.py  Master/house number canonicalization
      bl_status_rules.py  Original vs telex
      custom_business_rules.py  Freight term, load type, TEUs, totals
  application/
    document_parser.py    Intelligent parse (LLM + deterministic parsers)
    intelligent_extractor.py / intelligent_reconciler.py
    pdf_deterministic_registry.py
    pdf_batch_processor.py / pdf_bl_enrichment.py
    record_reconciliation.py / extraction_report.py
    crm_mapper.py         Dynamics JSON → internal records
    crm_output_formatter.py  Internal records → master/house CRM JSON
    invoice_dataverse_mapper.py
  infrastructure/
    ai/ai_extractor.py    Gemini calls, chunking, invoice LLM
    pdf/                  Native text + Tesseract OCR
    pdf/parsers/          Layout-specific PDF parsers
    spreadsheet/          Excel/CSV + WE-CAN proxy bill workbook
    dataverse/            OAuth client, uploader, metadata, field caps
    audit/                SQLite upload log + saved files
```

Supporting folders outside `app/`:

| Path | Role |
| ---- | ---- |
| `tests/` | Pytest suite. Fixtures in `tests/test_fixtures/` |
| `scripts/debug/` | One-off debug scripts |
| `scripts/manual/` | Live HTTP / Dataverse smoke tests (not collected by pytest) |
| `tools/` | Invoice folder mapping and contact-sheet helpers |
| `samples/` | Example `master.json` / `house.json` / `sample_output.json` |
| `integrations/typescript/` | Generated Dynamics schema and Nest-style Dataverse client (reference only) |
| `Invoices/` | Sample invoice PDFs/workbooks used by some tests and tools |
| `upload_audit/` | Runtime SQLite + saved uploads (`BL_AUDIT_DIR` override) |

## Request flow (Bill of Lading)

```
POST /extract/file
  api.routers.extract
    spreadsheet_extractor / pdf_extractor   (text + quality)
    document_parser.parse_document_intelligently
        deterministic registry (pdf parsers)
        Gemini (ai_extractor)
        validator + bl_number_rules + enrichment
    intelligent_reconciler
    crm_output_formatter.records_to_master_json / records_to_house_json
    custom_business_rules
    dataverse.field_limits.cap_nested_payload
    optional dataverse.uploader.upload_crm_json
    audit_store.finish_upload
```

## Request flow (invoice)

```
POST /extract/invoice  or  /extract/invoice/multi  or  /extract/invoice/excel
  api.routers.invoices
    OCR/text or wecan_proxy_bill_extractor (deterministic XLS)
    Gemini multi-invoice extract when needed
    invoice_dataverse_mapper (lookups, option values)
    Dataverse quote cost lines (idempotent post)
```

## Dependency rules

1. **Routers** may import application, domain, infrastructure, and core.
2. **Application** may import domain, infrastructure, and core. It must not import `app.api`.
3. **Domain** may import other domain modules and core. It currently also reaches a few PDF enrichment helpers (`pdf_bl_enrichment`, debit-note repair) from `validator.py` — treat those as domain-adjacent parsers, not HTTP.
4. **Infrastructure** implements I/O: Gemini HTTP, Tesseract, PyMuPDF, Dataverse OData, SQLite.
5. **Do not** add new modules at the repository root. New parsers go in `app/infrastructure/pdf/parsers/` and must be registered in `pdf_deterministic_registry.py`.

## Why this split

The previous layout kept ~50 Python modules and a 4,000-line `main.py` at the repo root. That mixed HTTP adapters, spreadsheet heuristics, invoice posting, and Dynamics reads in one file, and made imports (`from validator import …`) collide with the idea of a package.

Routers are still large (`extract.py`, `invoices.py`) because the endpoint logic is tightly coupled to upload handling. Further extraction of helpers into `app/application` is the next refactoring step; the package boundaries already isolate HTTP from parsers and Dataverse.
