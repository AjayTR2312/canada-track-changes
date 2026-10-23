# syntax=docker/dockerfile:1

# ──────────────────────────────────────────────────────────────────────────
# Stage 1 — Build the React/Vite frontend
# Produces frontend/dist, which the FastAPI app (static_react.py) serves.
# ──────────────────────────────────────────────────────────────────────────
FROM public.ecr.aws/docker/library/node:20-slim AS frontend-build

WORKDIR /frontend

# Install dependencies first (better layer caching).
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

# Build the production bundle (tsc + vite build → /frontend/dist).
COPY frontend/ ./
RUN npm run build


# ──────────────────────────────────────────────────────────────────────────
# Stage 2 — Python backend (FastAPI) that also serves the built frontend
# ──────────────────────────────────────────────────────────────────────────
FROM public.ecr.aws/docker/library/python:3.12-slim AS runtime

# System libraries required by the pipeline:
#   poppler-utils  → pdf2image / pdfplumber
#   libGL/glib/... → Pillow, PyMuPDF, OpenCV-style image ops
#   curl           → container HEALTHCHECK
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl poppler-utils \
        libglib2.0-0 libsm6 libxext6 libxrender-dev libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user.
RUN groupadd -g 1234 deploymentgroup && \
    useradd -m -u 1234 -g deploymentgroup deployment

WORKDIR /workspace

RUN mkdir -p /workspace/pipeline_jobs /workspace/pipeline_output /workspace/logs && \
    chown -R deployment:deploymentgroup /workspace

# Install Python dependencies (Windows-only packages are skipped via markers).
COPY --chown=deployment:deploymentgroup requirements.txt /workspace/requirements.txt

USER deployment
ENV PATH="/home/deployment/.local/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Application source.
USER root
COPY --chown=deployment:deploymentgroup . /workspace

# Built React assets from the frontend stage → served by static_react.py.
COPY --from=frontend-build --chown=deployment:deploymentgroup /frontend/dist /workspace/frontend/dist

USER deployment

# FastAPI (API + React frontend) on 8000; Streamlit on 8501 (optional).
EXPOSE 8000 8501

HEALTHCHECK --interval=30s --timeout=10s --start-period=80s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

# static_react:app = FastAPI backend (pipeline_api) + mounted React SPA.
CMD ["uvicorn", "static_react:app", "--host", "0.0.0.0", "--port", "8000"]
