# syntax=docker/dockerfile:1.7

# ─── Builder ─────────────────────────────────────────────────────────
# Compile/install Python deps in a builder stage to keep the runtime image
# small-ish (still ~3GB because of unstructured's transitive deps + system
# libs for OCR & layout detection).
FROM python:3.11-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=180 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        g++ \
        git \
        libgl1 \
        libglib2.0-0 \
        libmagic1 \
        libxml2 \
        libxslt1.1 \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-nor \
        libreoffice \
        pandoc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip wheel \
    && /opt/venv/bin/pip install -r requirements.txt

# ─── Runtime ─────────────────────────────────────────────────────────
FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    # Don't try to download NLTK data at import time; unstructured handles
    # this gracefully if missing.
    NLTK_DATA=/usr/share/nltk_data

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libmagic1 \
        libxml2 \
        libxslt1.1 \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-nor \
        libreoffice \
        pandoc \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Copy the prepared venv from builder
COPY --from=builder /opt/venv /opt/venv

# Pre-download NLTK data unstructured commonly uses; failure is non-fatal.
RUN python -c "import nltk, os; \
    os.makedirs('/usr/share/nltk_data', exist_ok=True); \
    nltk.download('punkt_tab', download_dir='/usr/share/nltk_data'); \
    nltk.download('averaged_perceptron_tagger_eng', download_dir='/usr/share/nltk_data')" \
    || echo "NLTK download skipped"

WORKDIR /app
COPY app ./app
COPY scripts ./scripts

# State volume mount point. On Railway, attach a Volume with mount-path /data
# in the service settings — Railway doesn't support the VOLUME directive.
RUN mkdir -p /data /tmp/onedrive-sync

EXPOSE 8000

# Railway sets $PORT; default to 8000 for local runs.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
