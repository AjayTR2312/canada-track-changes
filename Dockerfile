# syntax=docker/dockerfile:1
#
# Single-container image for the Plexus (StreamLit template) deployment.
#
# Despite the template's name it hosts any single-container app that serves HTTP
# on one port. Here that is the FastAPI backend (pipeline_api), which also serves
# the built React UI as static files via static_react:app. Pipeline jobs run in
# an in-process ProcessPoolExecutor, so no separate worker is needed; durable
# state lives in S3, which lets the pod run with a read-only root filesystem and
# keep all scratch under /tmp.
#
# BUILD CONTEXT MUST BE THE REPOSITORY ROOT (the backend imports sibling packages
# such as canada_pipeline/, src/, api/ at runtime):
#
#   docker build -t canada-track-changes .
#
# -----------------------------------------------------------------------------
# Stage 1 — build the React frontend to static files
# -----------------------------------------------------------------------------
FROM public.ecr.aws/docker/library/node:20 AS frontend
WORKDIR /build
# Install dependencies first for better layer caching.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# Guard: if a host node_modules leaked in (e.g. a stale/missing .dockerignore),
# the copied Windows binaries shadow the Linux ones and `tsc` fails with
# "tsc: Permission denied". Wiping and reinstalling guarantees correct, executable
# Linux binaries regardless of what the build context shipped.
# Produces /build/dist (host app + docx-viewer + dtd-validator bundles), which
# static_react.py serves: /assets via StaticFiles and index.html as SPA fallback.
RUN rm -rf node_modules && npm ci && npm run build

# -----------------------------------------------------------------------------
# Stage 2 — Python runtime
# -----------------------------------------------------------------------------
# Full (non-slim) image: ships the shared libraries PyMuPDF / Pillow / NumPy need,
# reducing missing-.so surprises.
FROM public.ecr.aws/docker/library/python:3.12 AS runtime

# System libraries the PDF / image stack needs at runtime.
#   poppler-utils            - pdf2image / pdfplumber
#   libgomp1                 - OpenMP runtime for NumPy
#   libglib2.0-0, libsm6,    - image libs used by Pillow / PyMuPDF
#   libxext6, libxrender-dev
#   curl                     - container HEALTHCHECK
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl poppler-utils \
        libglib2.0-0 libsm6 libxext6 libxrender-dev libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Non-root user (Plexus runs containers unprivileged).
RUN groupadd -g 1234 deploymentgroup \
    && useradd -m -u 1234 -g deploymentgroup deployment

WORKDIR /workspace

# Install Python dependencies as root, into the system site-packages, BEFORE
# switching to the non-root user. This matters: at runtime HOME=/tmp, so a
# per-user install (~/.local) would not be importable. Windows-only packages in
# requirements.txt carry platform markers and are skipped on Linux.
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Application code + the compiled frontend.
COPY . /workspace
COPY --from=frontend /build/dist /workspace/frontend/dist

RUN chmod +x entrypoint.sh \
    && chown -R deployment:deploymentgroup /workspace

USER deployment
# Plexus runs the pod with a READ-ONLY root filesystem, so the app must keep its
# scratch/working files under a writable path (/tmp). The backend already writes
# all scratch under tempfile.gettempdir(); durable data lives in S3. HOME and the
# cache dirs are redirected to /tmp so tooling that writes to them still works.
# Everything here can be overridden via the Deployment Job environment tab.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8501 \
    PIPELINE_MAX_WORKERS=6 \
    HOME=/tmp \
    TMPDIR=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    MPLCONFIGDIR=/tmp/.mpl

EXPOSE 8501

# Health endpoint the platform / load balancer can probe. A longer start period
# accounts for the heavy first-import of the conversion stack (PyMuPDF, lxml).
HEALTHCHECK --interval=30s --timeout=10s --start-period=80s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT}/health" || exit 1

ENTRYPOINT ["./entrypoint.sh"]
