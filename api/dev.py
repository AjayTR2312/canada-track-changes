"""Developer-only triage page and its data endpoints (hidden unless DEVELOPER_MODE=1 and a direct local request)."""
from __future__ import annotations

import asyncio
import json as _json
import os
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from canada_pipeline.region import get_region

router = APIRouter()
_REGION = get_region()
APP_REGION = _REGION.name      # label written to the feedback logs


# ─────────────────────────────────────────────
# Developer triage page — only when DEVELOPER_MODE=1 AND the request comes from this machine.
# Not linked from the app; normal users (and a deployed server without the flag) get a 404.
# ─────────────────────────────────────────────

def _require_dev(request: Request):
    import ipaddress
    on = os.getenv("DEVELOPER_MODE", "").strip().lower() in ("1", "true", "yes")
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host)
        local = ip.is_loopback or (getattr(ip, "ipv4_mapped", None) is not None and ip.ipv4_mapped.is_loopback)
    except ValueError:
        local = host == "localhost"
    # A reverse proxy on the same server also appears as 127.0.0.1, so any proxy header means "not a
    # direct local connection" and the page stays hidden even if the flag was left on.
    proxied = any(h in request.headers for h in ("x-forwarded-for", "forwarded", "x-real-ip"))
    if not (on and local) or proxied:
        raise HTTPException(404, "Not found")


@router.get("/dev/triage", include_in_schema=False)
async def dev_triage_page(request: Request):
    _require_dev(request)
    from pathlib import Path as _P
    return Response((_P(__file__).parent / "canada_pipeline" / "dev_triage_page.html").read_text(encoding="utf-8"), media_type="text/html")


@router.get("/api/dev/triage/info", include_in_schema=False)
async def dev_triage_info(request: Request):
    _require_dev(request)
    from src import correction_log, false_positive_log
    import s3_utils
    fps, corr = await asyncio.to_thread(false_positive_log.read_all), await asyncio.to_thread(correction_log.read_all)
    return {"region": APP_REGION, "false_positives": len(fps), "corrections": len(corr),
            "source": f"the shared logs in S3 (s3://{s3_utils.S3_BUCKET_NAME}/feedback/{_REGION.id}/)"}


@router.get("/api/dev/triage/false-positives", include_in_schema=False)
async def dev_triage_false_positives(request: Request):
    _require_dev(request)
    from src import false_positive_log
    rows = await asyncio.to_thread(false_positive_log.read_all)
    rows = sorted(rows, key=lambda r: r.get("timestamp", ""), reverse=True)[:500]
    return {"rows": rows}


@router.get("/api/dev/triage/clusters", include_in_schema=False)
async def dev_triage_clusters(request: Request, since_days: str = ""):
    _require_dev(request)
    from src import correction_log, false_positive_log
    from canada_pipeline.triage_agent import build_clusters
    fps, corr = await asyncio.to_thread(false_positive_log.read_all), await asyncio.to_thread(correction_log.read_all)
    return {"clusters": build_clusters(fps, corr, int(since_days) if since_days.isdigit() else None)}


@router.get("/api/dev/triage/resolver", include_in_schema=False)
async def dev_triage_resolver(request: Request):
    _require_dev(request)
    from src import resolver_log
    rows = await asyncio.to_thread(resolver_log.read_all)
    return {"summary": resolver_log.summarize(rows),
            "recent": sorted(rows, key=lambda r: r.get("timestamp", ""), reverse=True)[:100]}


@router.get("/api/dev/triage/latest", include_in_schema=False)
async def dev_triage_latest(request: Request):
    _require_dev(request)
    from canada_pipeline.triage_agent import latest_report
    return {"report": await asyncio.to_thread(latest_report)}


class DevTriageRunRequest(BaseModel):
    since_days: Optional[int] = None


@router.post("/api/dev/triage/run", include_in_schema=False)
async def dev_triage_run(request: Request, body: DevTriageRunRequest):
    _require_dev(request)
    import json as _json
    import queue
    import threading
    from canada_pipeline.triage_agent import run_triage

    q: "queue.Queue" = queue.Queue()

    def _work():
        try:
            report = run_triage(progress=lambda m: q.put({"type": "progress", "message": m}), since_days=body.since_days)
            q.put({"type": "report", "report": report})
        except Exception as e:
            print(f"[TRIAGE] failed: {e}")
            q.put({"type": "error", "message": str(e)})
        finally:
            q.put(None)

    threading.Thread(target=_work, daemon=True).start()

    async def _stream():
        while True:
            ev = await asyncio.to_thread(q.get)
            if ev is None:
                return
            yield _json.dumps(ev, ensure_ascii=False) + "\n"

    return StreamingResponse(_stream(), media_type="application/x-ndjson")


