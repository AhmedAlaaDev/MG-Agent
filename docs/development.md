# Development

## Setup

Python 3.10+ (CI uses 3.10; Docker uses 3.11). Tesseract must be on `PATH` or set `TESSERACT_CMD`.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
pip install -r requirements-dev.txt
copy .env.example .env
```

Run the API:

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Or `python main.py`.

## Tests

```bash
pytest
```

`pytest.ini` sets `pythonpath = . tests` and `testpaths = tests`. Shared helpers: `tests/paths.py` (`PROJECT_ROOT`, `sample_file`).

- Unit tests do not need a Gemini key or Dataverse.
- Tests that open PDFs skip or use embedded OCR snippets when the file is missing.
- `tests/test_azure.py`, `tests/test_full.py`, and some Excel tests may call live services; keep secrets in `.env`.
- `scripts/manual/` hits `localhost:8000` or live Dataverse. Do not put new live scripts in `tests/`.

Azure Pipelines (`azure-pipelines.yml`) installs `requirements.txt` + `requirements-dev.txt` and runs `pytest`.

## Docker

```powershell
.\scripts\docker-build.ps1
docker compose logs -f intelligent-bl-extractor
```

Image: `Dockerfile` (Debian + Tesseract). Fallback: `Dockerfile.ubuntu`. Compose maps `${API_PORT:-8000}` on the host.

Vercel (`vercel.json`) and Railway (`railway.json`) still start `main:app` / the Dockerfile.

## Adding a PDF parser

1. Create `app/infrastructure/pdf/parsers/pdf_your_layout.py` with `is_your_layout(text)` and `parse_your_layout(text) -> dict` using the same record keys as `BLEntity` (`mesco_masterblno`, containers, totals, …).
2. Register the parser in `app/application/pdf_deterministic_registry.py` with a confidence score (higher = preferred over Gemini for that layout).
3. Add tests in `tests/test_your_layout.py` using an OCR fixture under `tests/test_fixtures/` (do not commit huge PDFs; `*.pdf` is gitignored).
4. If the layout is a master+houses document, return `{ "master_record": ..., "house_records": [...] }`.

## Adding an HTTP endpoint

1. Put the route on an existing router in `app/api/routers/` or add a new router and `include_router` in `app/main.py`.
2. Shared Pydantic models go in `app/api/schemas.py`.
3. Keep I/O (Gemini, Dataverse, disk) in `app/infrastructure`. Keep CRM field meaning in `app/domain` or `app/application`.

## Import style

Use absolute package imports:

```python
from app.domain.rules.validator import validate_and_correct
from app.infrastructure.ai.ai_extractor import extract_records_with_gemini
```

Do not add compatibility shims at the repository root.

## Tools

| Script | Purpose |
| ------ | ------- |
| `tools/map_invoice_folder.py` | Dry-run Gemini mapping over `Invoices/` (no Dataverse post) |
| `tools/render_invoice_review.py` | Render invoice first pages |
| `tools/create_invoice_contact_sheet.py` | Contact sheet of invoice thumbnails |
| `scripts/manual/test_gemini.py` | Gemini connectivity smoke test |
| `scripts/debug/` | Ad-hoc BL / Dataverse probes |

## Sample payloads

`samples/master.json`, `samples/house.json`, and `samples/sample_output.json` show CRM-shaped output. `scripts/manual/test_all.py` posts those files to `/extract/crm` against a running server.
