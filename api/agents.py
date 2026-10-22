"""Agent endpoints: the failed-revision resolver (streamed activity log) and its outcome feedback."""
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
# Revision resolver agent — proposes where a failed revision belongs (reviewer accepts / rejects)
# ─────────────────────────────────────────────

class ResolveRevisionRequest(BaseModel):
    row: dict
    pxml: str
    dtd_family: str = "auto"
    session_id: str = ""
    emp_id: str = ""
    user_name: str = ""
    doc_name: str = ""


class ResolverOutcomeRequest(BaseModel):
    run_id: str
    outcome: str                      # accepted | rejected
    reject_reason: str = ""           # wrong_paragraph | wrong_tags | text_changed | not_needed | other


@router.post("/api/revision-resolver/outcome")
async def revision_resolver_outcome(request: ResolverOutcomeRequest):
    from src import resolver_log
    ok = await asyncio.to_thread(resolver_log.mark_outcome, request.run_id, request.outcome, request.reject_reason)
    return {"status": "updated" if ok else "not_found"}


@router.post("/api/revision-resolver/run")
async def run_revision_resolver(request: ResolveRevisionRequest):
    """Stream the agent's activity as NDJSON (one event per line); the last event is type=final."""
    import json as _json
    import queue
    import threading
    from canada_pipeline.revision_resolver import run_resolver

    import uuid
    from src import resolver_log

    q: "queue.Queue" = queue.Queue()
    run_id = uuid.uuid4().hex[:12]

    def _log_run(ev: dict):
        try:
            prop = ev.get("proposal") or {}
            resolver_log.record_run({
                "run_id": run_id, "session_id": request.session_id, "doc_name": request.doc_name,
                "emp_id": request.emp_id, "user_name": request.user_name,
                "change_type": request.row.get("Change Type", ""), "section": request.row.get("Section", ""),
                "status": ev.get("status", ""), "confidence": prop.get("confidence", ""), "kind": prop.get("kind", ""),
                "steps": ev.get("steps", ""), "model": ev.get("model", ""),
                "detail": ev.get("reason") or prop.get("explanation", ""), "region": APP_REGION,
            })
        except Exception as e:   # logging must never break a run
            print(f"[RESOLVER] log failed: {e}")

    def _work():
        try:
            for ev in run_resolver(row=request.row, pxml=request.pxml, dtd_family=request.dtd_family):
                if ev.get("type") == "final":
                    _log_run(ev)
                    ev = {**ev, "run_id": run_id}
                q.put(ev)
        except ValueError as e:
            q.put({"type": "error", "message": str(e)})
        except Exception as e:
            print(f"[RESOLVER] failed: {e}")
            q.put({"type": "error", "message": f"The resolver is unavailable: {e}"})
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


