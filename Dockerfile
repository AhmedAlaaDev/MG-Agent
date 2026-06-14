# Intelligent B/L Extractor — FastAPI + Tesseract OCR + Azure OpenAI + Dataverse
FROM python:3.11-slim-bookworm

# deb.debian.org often returns 403 / TLS errors behind corporate proxies; ftp.debian.org usually works.
# Override: docker build --build-arg APT_MIRROR=https://deb.debian.org/debian .
ARG APT_MIRROR=https://ftp.debian.org/debian
ARG APT_SECURITY_MIRROR=https://security.debian.org/debian-security

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    TESSERACT_CMD=/usr/bin/tesseract \
    TESSERACT_LANG=eng

WORKDIR /app

# Default slim image uses http://deb.debian.org → 403 on some networks. Force HTTPS mirrors.
RUN rm -f /etc/apt/sources.list.d/debian.sources \
    && printf '%s\n' \
        'Types: deb' \
        "URIs: ${APT_MIRROR}" \
        'Suites: bookworm bookworm-updates' \
        'Components: main' \
        'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
        '' \
        'Types: deb' \
        "URIs: ${APT_SECURITY_MIRROR}" \
        'Suites: bookworm-security' \
        'Components: main' \
        'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
        > /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import os, urllib.request; port=os.environ.get('PORT','8080'); urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=5)"

CMD ["sh", "-c", "python -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
