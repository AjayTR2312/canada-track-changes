#!/usr/bin/env sh
# -----------------------------------------------------------------------------
# Plexus entrypoint.
#
# Serves the FastAPI backend (static_react:app), which also serves the compiled
# React UI as static files, on the platform-provided $PORT. Pipeline jobs run
# in-process via a ProcessPoolExecutor, so no separate worker process is needed;
# durable state lives in S3 and all scratch goes under /tmp.
# -----------------------------------------------------------------------------
set -e

PORT="${PORT:-8501}"

echo "[entrypoint] starting uvicorn static_react:app on 0.0.0.0:${PORT}"
exec uvicorn static_react:app --host 0.0.0.0 --port "${PORT}"
