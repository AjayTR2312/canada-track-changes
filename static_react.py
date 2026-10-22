"""
IIS Entry Point — serves both FastAPI backend + React frontend.

Usage (IIS web.config):
    python -m uvicorn static_react:app --host 127.0.0.1 --port %HTTP_PLATFORM_PORT%

Or manual:
    python -m uvicorn static_react:app --host 0.0.0.0 --port 8000
"""
import os
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pipeline_api import app  # Import the existing FastAPI app with all API routes

# ─────────────────────────────────────────────
# Serve React frontend static files
# ─────────────────────────────────────────────
_frontend_dist = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "dist")

if os.path.isdir(_frontend_dist) and os.path.isdir(os.path.join(_frontend_dist, "assets")):
    # Serve /assets (JS, CSS, images)
    app.mount("/assets", StaticFiles(directory=os.path.join(_frontend_dist, "assets")), name="static-assets")

    # SPA fallback: serve index.html for any non-API, non-file route
    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        file_path = os.path.join(_frontend_dist, full_path)
        if full_path and os.path.isfile(file_path):
            return FileResponse(file_path)
        return FileResponse(os.path.join(_frontend_dist, "index.html"))

    print(f"[STATIC] Serving React frontend from: {_frontend_dist}")
else:
    print(f"[STATIC] WARNING: frontend/dist not found at {_frontend_dist}")
    print(f"[STATIC] Run 'cd frontend && npm run build' first.")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, workers=1)
