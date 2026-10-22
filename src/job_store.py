"""Job and batch records, kept in S3 instead of a pod's memory.

Before: `jobs = {}` / `batches = {}` lived in the API process, so a pod restart forgot every job and
a second replica could not answer for a job started on the first.

Now every record is written through to S3 (jobs/<id>/job.json, batches/<id>/batch.json) and any
pod can read it.  The pod that STARTED a job additionally keeps the live record in a local dict (it
needs the running Future); that copy is authoritative while the job runs and is written through to
S3 whenever the code calls `save()`.

If the owning pod is killed the job simply stops.  To make that visible instead of leaving a job
"running" forever, the owner writes a small heartbeat object every few seconds; another pod that
finds a "running" record whose heartbeat has gone stale marks it interrupted (and a pod that is shut
down gracefully marks its own running records right away).

Live progress / cancellation also go through S3 so they work across pods:
    jobs/<id>/status.json   written by the worker process at every pipeline step
    jobs/<id>/cancel.flag   written by whichever pod receives "stop"; the worker checks it per step
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
from typing import Callable, Optional

import s3_utils as s3

logger = logging.getLogger(__name__)

OWNER_ID = f"{socket.gethostname()}:{os.getpid()}"
HEARTBEAT_SECONDS = int(os.getenv("JOB_HEARTBEAT_SECONDS", "20"))
STALE_SECONDS = int(os.getenv("JOB_STALE_SECONDS", "120"))

_RUNNING = "running"
_hb_thread: Optional[threading.Thread] = None
_hb_lock = threading.Lock()
_registries: list = []


def _persistable(rec: dict) -> dict:
    """The record minus process-local objects (keys starting with '_', e.g. the Future)."""
    return {k: v for k, v in rec.items() if not str(k).startswith("_")}


class Registry:
    """A write-through, S3-backed collection of records (jobs or batches)."""

    def __init__(self, kind: str, id_field: str, on_interrupt: Callable[[dict, str], None]):
        self.kind = kind                      # "jobs" | "batches"
        self.id_field = id_field              # "job_id" | "batch_id"
        self.on_interrupt = on_interrupt
        self._local: dict = {}
        self._lock = threading.RLock()
        _registries.append(self)

    # ---- keys ---------------------------------------------------------------
    def _dir(self, rid: str) -> str:
        return f"{self.kind}/{s3.safe_id(rid)}"

    def _key(self, rid: str) -> str:
        return f"{self._dir(rid)}/{'job' if self.kind == 'jobs' else 'batch'}.json"

    def _hb_key(self, rid: str) -> str:
        return f"{self._dir(rid)}/heartbeat.json"

    # ---- write --------------------------------------------------------------
    def create(self, rec: dict) -> dict:
        rid = rec[self.id_field]
        rec.setdefault("owner", OWNER_ID)
        with self._lock:
            self._local[rid] = rec
        self.save(rec)
        self._beat(rid)
        _ensure_heartbeat_thread()
        return rec

    def save(self, rec: dict) -> None:
        """Persist the record. Never raises: a storage hiccup must not kill a running job.

        The local copy is only authoritative while the record is "running" (this pod holds the live
        Future / thread). Once a record has been saved in any other state it is dropped from the
        local cache, so from then on every pod -- including this one -- reads the same S3 copy and
        no pod can keep serving a stale one."""
        rid = rec[self.id_field]
        try:
            s3.put_json(self._key(rid), _persistable(rec))
        except Exception as exc:
            logger.warning("Could not persist %s %s: %s", self.kind, rid, exc)
            return
        if rec.get("status") != _RUNNING:
            with self._lock:
                if self._local.get(rid) is rec:
                    del self._local[rid]

    def _beat(self, rid: str) -> None:
        try:
            s3.put_json(self._hb_key(rid), {"at": time.time(), "owner": OWNER_ID})
        except Exception as exc:
            logger.debug("heartbeat write failed for %s: %s", rid, exc)

    # ---- read ---------------------------------------------------------------
    def get(self, rid: str, default=None):
        with self._lock:
            rec = self._local.get(rid)
        if rec is not None:
            return rec                         # started by this pod: the live copy
        try:
            rec = s3.get_json(self._key(rid))
        except Exception as exc:
            logger.warning("Could not read %s %s: %s", self.kind, rid, exc)
            return default
        if rec is None:
            return default
        self._check_stale(rec)
        return rec

    def exists(self, rid: str) -> bool:
        return self.get(rid) is not None

    # dict-style access (the API code was written against plain dicts)
    def __getitem__(self, rid: str) -> dict:
        rec = self.get(rid)
        if rec is None:
            raise KeyError(rid)
        return rec

    def __contains__(self, rid) -> bool:
        return self.exists(rid)

    def __setitem__(self, rid: str, rec: dict) -> None:
        rec[self.id_field] = rid
        self.create(rec)

    def running_here(self) -> int:
        """How many records this pod is running right now."""
        return sum(1 for _r, rec in self.local_items() if rec.get("status") == _RUNNING)

    def owned(self, rid: str) -> bool:
        with self._lock:
            return rid in self._local

    def adopt(self, rec: dict) -> dict:
        """Take over a record another pod created (e.g. to run a re-validation here and persist its
        progress from this pod)."""
        rec["owner"] = OWNER_ID
        with self._lock:
            self._local[rec[self.id_field]] = rec
        _ensure_heartbeat_thread()
        return rec

    def local_items(self) -> list:
        with self._lock:
            return list(self._local.items())

    # ---- liveness -----------------------------------------------------------
    def _check_stale(self, rec: dict) -> None:
        if rec.get("status") != _RUNNING:
            return
        rid = rec[self.id_field]
        try:
            hb = s3.get_json(self._hb_key(rid)) or {}
        except Exception:
            return
        last = float(hb.get("at") or rec.get("started_at") or rec.get("created_at") or 0)
        if last and time.time() - last > STALE_SECONDS:
            self.interrupt(rec, "The server that was running this was restarted before it finished.")

    def interrupt(self, rec: dict, reason: str) -> None:
        self.on_interrupt(rec, reason)
        rec["completed_at"] = rec.get("completed_at") or time.time()
        self.save(rec)

    def interrupt_owned_running(self, reason: str) -> int:
        """Called on graceful shutdown: mark everything this pod was running as interrupted."""
        n = 0
        for _rid, rec in self.local_items():
            if rec.get("status") == _RUNNING:
                self.interrupt(rec, reason)
                n += 1
        return n

    def _heartbeat(self) -> None:
        for rid, rec in self.local_items():
            if rec.get("status") == _RUNNING:
                self._beat(rid)


def _ensure_heartbeat_thread() -> None:
    global _hb_thread
    with _hb_lock:
        if _hb_thread is not None and _hb_thread.is_alive():
            return

        def _loop():
            while True:
                time.sleep(HEARTBEAT_SECONDS)
                for reg in list(_registries):
                    try:
                        reg._heartbeat()
                    except Exception as exc:  # never let the thread die
                        logger.debug("heartbeat loop: %s", exc)

        _hb_thread = threading.Thread(target=_loop, name="JobHeartbeat", daemon=True)
        _hb_thread.start()


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
def _interrupt_job(rec: dict, reason: str) -> None:
    rec["status"] = "failed"
    rec["current_step_name"] = "Interrupted"
    rec["error"] = f"Processing was interrupted. {reason}"
    rec["error_info"] = {
        "category": "unknown",
        "title": "Processing was interrupted",
        "hint": "The server restarted while this ran, so it stopped part-way. Your input files are "
                "safe: run the chapter again.",
        "action": "rerun",
        "retryable": True,
        "detail": reason,
    }


def _interrupt_batch(rec: dict, reason: str) -> None:
    rec["status"] = "cancelled"
    rec.setdefault("log", []).append({
        "time": time.strftime("%H:%M:%S"), "level": "warning",
        "message": f"Batch interrupted: {reason} Finished chapters are kept; retry the others.",
    })
    for item in rec.get("items", []):
        if item.get("status") in ("running", "queued"):
            item["status"] = "failed"
            item["error"] = "Interrupted: the server restarted before this chapter finished."


class JobRegistry(Registry):
    def __init__(self):
        super().__init__("jobs", "job_id", _interrupt_job)

    @staticmethod
    def _index_key(rec: dict) -> str:
        who = rec.get("emp_id") or (rec.get("user_email", "").split("@")[0] if rec.get("user_email") else "")
        return f"{s3.S3_PREFIX_JOBS_INDEX}/{s3.safe_id(who, '_unassigned')}/{s3.safe_id(rec['job_id'])}.json"

    def create(self, rec: dict) -> dict:
        super().create(rec)
        try:
            s3.put_json(self._index_key(rec), {"job_id": rec["job_id"], "started_at": rec.get("started_at")})
        except Exception as exc:
            logger.warning("Could not index job %s: %s", rec.get("job_id"), exc)
        return rec

    def list_for_user(self, emp_id: str, within_seconds: float = 86400) -> list:
        """Job records started by this user in the last `within_seconds` (newest first)."""
        who = s3.safe_id(emp_id.strip(), "_unassigned")
        cutoff = time.time() - within_seconds
        ids = []
        for o in s3.list_objects(f"{s3.S3_PREFIX_JOBS_INDEX}/{who}/"):
            if o["last_modified"] >= cutoff and o["key"].endswith(".json"):
                ids.append(o["key"].rsplit("/", 1)[-1][:-5])
        recs = [r for r in (self.get(i) for i in ids) if r]
        recs.sort(key=lambda r: r.get("started_at") or 0, reverse=True)
        return recs


class BatchRegistry(Registry):
    def __init__(self):
        super().__init__("batches", "batch_id", _interrupt_batch)

    def input_prefix(self, batch_id: str) -> str:
        return f"{self._dir(batch_id)}/input/"


jobs = JobRegistry()
batches = BatchRegistry()


# ---------------------------------------------------------------------------
# Live progress + cancellation (shared across pods via S3)
# ---------------------------------------------------------------------------
def status_key(job_id: str) -> str:
    return f"{s3.S3_PREFIX_JOBS}/{s3.safe_id(job_id)}/status.json"


def cancel_key(job_id: str) -> str:
    return f"{s3.S3_PREFIX_JOBS}/{s3.safe_id(job_id)}/cancel.flag"


def write_status(job_id: str, data: dict) -> None:
    try:
        s3.put_json(status_key(job_id), data)
    except Exception as exc:
        logger.debug("status write failed for %s: %s", job_id, exc)


def read_status(job_id: str) -> dict:
    try:
        return s3.get_json(status_key(job_id)) or {}
    except Exception:
        return {}


def set_cancel(job_id: str) -> None:
    try:
        s3.put_bytes(cancel_key(job_id), b"cancel")
    except Exception as exc:
        logger.warning("could not write cancel flag for %s: %s", job_id, exc)


def is_cancelled(job_id: str) -> bool:
    try:
        return s3.object_exists(cancel_key(job_id))
    except Exception:
        return False


def clear_job_state(job_id: str) -> None:
    """Remove a job's live-state objects (status / cancel flag / heartbeat)."""
    for k in (status_key(job_id), cancel_key(job_id), jobs._hb_key(job_id)):
        try:
            s3.delete_object(k)
        except Exception:
            pass
