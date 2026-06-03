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

### 1. Prepare `.env`

```powershell
copy .env.example .env
# Edit .env — required variables:
```

| Variable | Required | Description |
|----------|----------|-------------|
| `AZURE_OPENAI_ENDPOINT` | Yes | `https://houseblreader-resource.openai.azure.com/` |
| `AZURE_OPENAI_API_KEY` | Yes | Azure OpenAI API key |
| `AZURE_OPENAI_API_VERSION` | Yes | e.g. `2024-08-01-preview` |
| `AZURE_OPENAI_DEPLOYMENT` | Yes | e.g. `gpt-4o` |
| `TENANT_ID` | For Dataverse upload | Azure AD tenant |
| `CLIENT_ID` | For Dataverse upload | App registration client ID |
| `CLIENT_SECRET` | For Dataverse upload | App secret |
| `BASE_URL` | For Dataverse upload | e.g. `https://mgc.crm4.dynamics.com` |
| `AZURE_APP_API_URL` | For Dataverse upload | e.g. `https://mgc.crm4.dynamics.com/api/data/v9.2` |
| `API_PORT` | No | Host port (default `8000`) |

### 2. Build and run (recommended)

```powershell
docker compose --env-file .env up -d --build
```

Or use the helper script:

```powershell
.\scripts\docker-build.ps1
```

### 3. Build only / run with explicit env file

```powershell
# Build image
docker build -t intelligent-bl-extractor:latest .

# Run container (all variables from .env)
docker run -d `
  --name intelligent-bl-extractor `
  --env-file .env `
  -e TESSERACT_CMD=/usr/bin/tesseract `
  -p 8000:8000 `
  intelligent-bl-extractor:latest
```

Linux/macOS:

```bash
docker build -t intelligent-bl-extractor:latest .
docker run -d --name intelligent-bl-extractor --env-file .env \
  -e TESSERACT_CMD=/usr/bin/tesseract \
  -p 8000:8000 intelligent-bl-extractor:latest
```

### 4. Verify

```text
http://localhost:8000/docs
http://localhost:8000/health
```

```powershell
docker compose logs -f intelligent-bl-extractor
docker compose down
```

### Build fails with `403 Forbidden` on `apt-get update`

Some networks block `deb.debian.org` (HTTP 403 or TLS errors). **Default mirrors** are now `ftp.debian.org` + `security.debian.org`.

**Option A** — override mirrors in `.env` if needed:

```env
APT_MIRROR=https://ftp.debian.org/debian
APT_SECURITY_MIRROR=https://security.debian.org/debian-security
```

```powershell
docker compose --env-file .env up -d --build
```

**Option B** — Ubuntu-based image (often works when Debian CDN is blocked):

```powershell
docker compose -f docker-compose.yml -f docker-compose.ubuntu.yml --env-file .env up -d --build
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
