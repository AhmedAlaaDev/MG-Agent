# Intelligent Bill of Lading Extractor

FastAPI service that performs:

1. PDF upload
2. PDF page rendering
3. Tesseract OCR
4. Azure OpenAI intelligent extraction
5. Validation and correction of common B/L mistakes
6. JSON response suitable for Dynamics/Dataverse mapping

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

## Run with Docker

```bash
cp .env.example .env
docker compose up --build
```

Open:

```text
http://localhost:8000/docs
```

## Endpoints

### Health

```bash
curl http://localhost:8000/health
```

### Extract from PDF

```bash
curl -X POST http://localhost:8000/extract/pdf \
  -F "file=@sample.pdf" \
  -F "return_raw_text=false"
```

### Extract from OCR text

```bash
curl -X POST http://localhost:8000/extract/text \
  -H "Content-Type: application/json" \
  -d '{"ocr_text": "BILL OF LADING NO. OOLU2309868980 ..."}'
```

## Environment variables

Use `.env`; never hard-code keys in the code.

```env
AZURE_OPENAI_ENDPOINT=https://YOUR-RESOURCE.openai.azure.com/
AZURE_OPENAI_API_KEY=YOUR_KEY_HERE
AZURE_OPENAI_API_VERSION=2024-08-01-preview
AZURE_OPENAI_DEPLOYMENT=gpt-4o-mini
OCR_DPI=300
TESSERACT_LANG=eng
RETURN_RAW_TEXT=false
MAX_OCR_CHARS=60000
```
