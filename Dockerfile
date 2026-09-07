# syntax=docker/dockerfile:1.7
#
# Multi-stage build producing ONE image that serves three roles selected at
# runtime via the SERVICE_ROLE env var (see docker/entrypoint.sh):
#   web      FastAPI service (uvicorn)                  -- docker-compose.yml `api`,     Helm deployment.yaml
#   worker   Celery worker (batch/CDC/webhook/ingestion) -- docker-compose.yml `worker`,  Helm worker-deployment.yaml
#   beat     Celery beat (scheduled SEBI ingestion poll) -- docker-compose.yml `beat`
#   migrate  One-shot `alembic upgrade head`             -- docker-compose.yml `migrate`, Helm migrate Job
#
# OPA itself is NOT built here -- it runs from the official
# openpolicyagent/opa image as a co-located sidecar (see this repo's
# app/execution/opa_engine.py module docstring for why: a persistent
# server process, not a per-request subprocess). Its version is pinned
# once, in docker-compose.yml and helm/regengine-ai/values.yaml.
#
# Air-gapped / restricted-egress deployments: the `builder` stage below
# pre-warms unstructured's hi_res layout-model weights (+ NLTK tokenizer
# data) at BUILD time and the `runtime` stage bakes them into the image, so
# the running container never needs network egress for them. This means
# the image itself MUST be built somewhere with internet access (once per
# unstructured version bump) before it's promoted into a restricted
# environment -- building this Dockerfile from inside that restricted
# environment will fail at the pre-warming RUN step. See app/parsing/
# extractor.py's _model_download_error for the runtime-side symptom if
# that pre-warming is ever skipped, and README's Production notes.

#############################################
# Stage: base -- shared OS runtime deps + non-root user, used by every
# later stage so builder and runtime never drift on system package versions.
#############################################
FROM python:3.11-slim-bookworm AS base

# poppler-utils, tesseract-ocr, libmagic1, libgl1, libglib2.0-0:
#   runtime requirements of unstructured[pdf]'s hi_res layout/OCR strategy,
#   AND of app.parsing.extractor's own OCR fallback for scanned/image-only
#   PDFs (poppler-utils for pdf2image's page rasterization, tesseract-ocr
#   for app.localization.ocr's Tesseract backend).
# curl:
#   container HEALTHCHECK below and Kubernetes-style manual debugging.
RUN apt-get update && apt-get install -y --no-install-recommends \
        poppler-utils \
        tesseract-ocr \
        libmagic1 \
        libgl1 \
        libglib2.0-0 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Fixed, non-root, non-login UID/GID shared by every stage and referenced
# again in Helm's securityContext.runAsUser -- keep these three in sync.
RUN groupadd --gid 10001 regengine \
    && useradd --uid 10001 --gid regengine --create-home --shell /usr/sbin/nologin regengine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Fixed, non-default cache locations for the ML model weights unstructured's
# hi_res strategy (detectron2 layout/table model, via huggingface_hub) and
# its text-splitting NLTK data pull down on first use -- set here (shared by
# `builder` and `runtime`) so the `builder` stage below can pre-warm them at
# BUILD time into a path the `runtime` stage then COPYs verbatim, instead of
# every container's first upload paying for (and, on restricted-egress
# networks, hanging or failing on) a first-use download. See app/parsing/
# extractor.py's _model_download_error for what a missing download here
# looks like from the app's side if this pre-warming step is ever skipped.
ENV HF_HOME=/opt/model-cache/huggingface \
    NLTK_DATA=/opt/model-cache/nltk_data

#############################################
# Stage: builder -- compiles/installs Python deps into an isolated venv so
# the runtime stage never carries a C compiler, headers, or pip's cache.
#############################################
FROM base AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        python3-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /build
COPY requirements.txt .

# CPU-only torch AND torchvision explicitly, BEFORE requirements.txt, in the
# SAME pip invocation: sentence-transformers otherwise resolves the default
# (CUDA-bundled) torch wheel -- several GB larger than this service needs,
# since embedding inference here runs on ordinary CPU worker pods, never a
# GPU node. torchvision must be pinned here too, not left for
# requirements.txt to resolve later: unstructured[pdf]'s hi_res strategy
# (table/layout detection, via unstructured-inference) pulls torchvision in
# transitively, and if that happens in a separate pip run it resolves from
# PyPI's default index against whatever's "latest" there -- built against a
# DIFFERENT libtorch ABI than the CPU-only torch already installed. Both
# packages import fine individually in that broken state; the mismatch only
# surfaces at first use, as `RuntimeError: operator torchvision::nms does
# not exist` inside transformers' DetrImageProcessor (used by
# unstructured's table-structure model) -- which extract_pdf's hi_res path
# hits on every upload. Installing both from the CPU index in one command
# lets pip's resolver pick a mutually ABI-compatible pair.
RUN pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cpu \
        torch torchvision \
    && pip install --no-cache-dir -r requirements.txt

# Pre-warm the hi_res strategy's model weights (+ NLTK tokenizer data) into
# $HF_HOME/$NLTK_DATA above, by actually running partition_pdf(strategy=
# "hi_res") once against a throwaway one-page PDF built on the fly with
# Pillow (already a dependency; no fixture file needs to ship in this repo).
# REQUIRES NETWORK ACCESS AT BUILD TIME. An air-gapped/restricted-egress
# deployment must build this image somewhere with egress (once, or whenever
# unstructured's pinned version changes its model), then ship/promote the
# resulting image -- the runtime container itself never needs egress for
# this. See README's Production notes for the full explanation.
RUN python -c "\
import os, tempfile; \
from PIL import Image; \
from unstructured.partition.pdf import partition_pdf; \
path = os.path.join(tempfile.mkdtemp(), 'warmup.pdf'); \
Image.new('RGB', (200, 200), 'white').save(path, 'PDF'); \
partition_pdf(filename=path, strategy='hi_res', infer_table_structure=True); \
print('hi_res model weights pre-warmed into ' + os.environ['HF_HOME']); \
"

#############################################
# Stage: runtime -- final image. OS deps from `base`, Python deps from
# `builder`, app source, non-root user, one entrypoint for every role.
#############################################
FROM base AS runtime

COPY --from=builder /opt/venv /opt/venv
# The hi_res weights + NLTK data `builder` pre-warmed into $HF_HOME/
# $NLTK_DATA (set on `base`, inherited here) -- this is what makes them
# baked into the image rather than downloaded on first upload.
COPY --from=builder /opt/model-cache /opt/model-cache
ENV PATH="/opt/venv/bin:${PATH}" \
    SERVICE_ROLE=web

WORKDIR /app
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY alembic.ini ./
COPY sql/ ./sql/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh \
    # Pre-create the ingestion PDF archive dir (app/config.py's
    # ingestion_pdf_download_dir) so a read-only-root-filesystem deployment
    # only needs this one path writable via a mounted volume (see
    # helm/regengine-ai/templates/deployment.yaml).
    && mkdir -p /app/data/ingested_pdfs \
    && chown -R regengine:regengine /app /opt/model-cache

USER regengine
EXPOSE 8000

# Meaningful for the `web` role (docker-compose's `api` service); the
# `worker`/`beat`/`migrate` services override or disable this in
# docker-compose.yml since they never serve HTTP. Kubernetes deployments
# use their own liveness/readiness probes (helm/regengine-ai/templates/
# deployment.yaml) instead of this Docker-native HEALTHCHECK.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT:-8000}/healthz" || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
