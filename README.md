# Intelligent Bill of Lading Extractor

FastAPI service that turns shipping documents (PDF, Excel, CSV) into structured Bill of Lading records, then optionally posts them to Microsoft Dynamics 365 Dataverse (`mesco_operation`, containers, cargo, and invoice cost lines).

The codebase follows a layered layout:

| Layer | Package | Responsibility |
| ----- | ------- | -------------- |
| HTTP | `app/api` | FastAPI routers, request/response models, HTML pages |
| Application | `app/application` | Extraction orchestration, CRM JSON shaping, invoice mapping |
| Domain | `app/domain` | B/L entity, validation, freight/booking/load-type rules |
| Infrastructure | `app/infrastructure` | Gemini, OCR/PDF parsers, spreadsheets, Dataverse, audit store |
| Core | `app/core` | Settings, paths, per-request LLM overrides |

Full documentation lives in [`docs/`](docs/README.md).

## What it does

1. Accept a PDF / XLSX / XLS / CSV upload (or raw OCR text).
2. Extract text natively from digital PDFs, or render pages and run Tesseract OCR for scans.
3. Run layout-specific deterministic parsers (master B/L, house B/L, sea waybill, cargo manifest, LCL consol, debit note, and others).
4. Call Gemini for flexible extraction, then reconcile LLM output against parser evidence.
5. Validate and correct common B/L mistakes (container ISO check digits, ACID numbers, BL numbers, IMO/UN, HS codes).
6. Apply CRM business rules (Prepaid/Collect → booking term and freight payable at, FCL vs LCL, TEUs, house totals).
7. Project records into Dynamics-shaped JSON (`master.json` / `house.json` style).
8. Optionally upsert the hierarchy into Dataverse with dedup, lookups, and option-set mapping.
9. Extract vendor invoices / debit notes and post cost lines against existing operations.

## Run locally

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt   # pytest
copy .env.example .env                # then fill in keys
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Open:

- API docs: http://localhost:8000/docs
- Health: http://localhost:8000/health
- Audit dashboard: http://localhost:8000/audit
- Operation review UI: http://localhost:8000/operation
- Batch PDF test page: http://localhost:8000/test/pdf

The ASGI target remains `main:app` (thin wrapper around `app.main:app`) so Docker, Vercel, Railway, and existing scripts keep working.

## Run with Docker

```powershell
copy .env.example .env
docker compose --env-file .env up -d --build
# or: .\scripts\docker-build.ps1
```

| Variable | Required | Description |
|----------|----------|-------------|
| `GEMINI_API_KEY` | Yes | Google Gemini API key |
| `GEMINI_MODEL` | No | Default `gemini-3-pro-preview` in code; `.env.example` uses `gemini-3.5-flash` |
| `TENANT_ID` / `CLIENT_ID` / `CLIENT_SECRET` | For Dataverse | Azure AD app registration |
| `BASE_URL` | For Dataverse | e.g. `https://mgc.crm4.dynamics.com` |
| `AZURE_APP_API_URL` | For Dataverse | e.g. `https://mgc.crm4.dynamics.com/api/data/v9.2` |
| `API_PORT` | No | Host port (default `8000`) |

If Debian apt mirrors return 403:

```powershell
docker compose -f docker-compose.yml -f docker-compose.ubuntu.yml --env-file .env up -d --build
```

## Tests

```bash
pytest
```

Live Dataverse / localhost smoke scripts live in `scripts/manual/` and are not collected by pytest.

## Layout

```
app/                  Application package (clean architecture layers)
docs/                 Architecture, API, pipeline, Dataverse, config, development
tests/                Pytest suite and OCR fixtures
scripts/              Docker helpers, debug tools, manual smoke tests
tools/                Invoice review / mapping utilities
samples/              Example master/house CRM JSON
integrations/         TypeScript Dynamics schema + client reference
main.py               ASGI entry (`from app.main import app`)
```

See [Architecture](docs/architecture.md) for module-by-module mapping.
