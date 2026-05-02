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
        autoconf \
        libtool \
        pkg-config \
        wget \
        xz-utils \
        libpcre2-dev \
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

# ─── Build libredwg from source for DWG (AutoCAD) support ────────────
# libredwg isn't packaged with CLI tools in Debian Bookworm, so we compile
# it from upstream. Provides `dwg2dxf` used by app/dwg_parser.py.
WORKDIR /tmp/libredwg
RUN wget -q https://ftp.gnu.org/gnu/libredwg/libredwg-0.13.3.tar.xz \
    && tar xf libredwg-0.13.3.tar.xz \
    && cd libredwg-0.13.3 \
    && ./configure --disable-bindings --disable-trace --prefix=/usr/local \
    && make -j"$(nproc)" \
    && make install \
    && cd /tmp \
    && rm -rf /tmp/libredwg \
    && ldconfig

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
    NLTK_DATA=/usr/share/nltk_data \
    # ─── Memory tuning for Railway containers ─────────────────────
    # Glibc allocates a per-thread arena (~64 MB each by default) which
    # fragments badly under unstructured + ML-inference workloads. Capping
    # to 2 arenas saves 200-500 MB of RSS on long-running containers and
    # is the single biggest fix for the OOM-kill loop on Railway.
    MALLOC_ARENA_MAX=2 \
    # BLAS/OMP libraries default to spawning one thread per CPU. On a
    # Railway 4-vCPU host that means 4× duplicated thread pools across
    # numpy / torch / onnxruntime — each 50-200 MB. We're I/O-bound on
    # downloads + embeddings anyway, so single-thread BLAS is fine.
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    # HuggingFace tokenizers fork-detector + parallel decoder leaks RAM
    # in long-running web workers. unstructured uses tokenizers heavily.
    TOKENIZERS_PARALLELISM=false \
    # onnxruntime (used by unstructured-inference layout models) likewise
    # benefits from a tight thread budget on small containers.
    ORT_DISABLE_ALL=0 \
    # Stop matplotlib from picking a GUI backend if any display lib
    # leaks into the container (Agg is forced again in dwg_parser.py).
    MPLBACKEND=Agg

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libmagic1 \
        libxml2 \
        libxslt1.1 \
        libpcre2-8-0 \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-nor \
        libreoffice \
        pandoc \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Copy libredwg (built from source in builder stage) — provides dwg2dxf CLI
# used by app/dwg_parser.py to convert DWG → DXF before parsing with ezdxf.
COPY --from=builder /usr/local/bin/dwg2dxf /usr/local/bin/dwg2dxf
COPY --from=builder /usr/local/bin/dwgread /usr/local/bin/dwgread
COPY --from=builder /usr/local/lib/libredwg* /usr/local/lib/
RUN ldconfig

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
