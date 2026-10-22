"""
Redis-backed job store for pipeline job state persistence.
Replaces the in-memory `jobs = {}` dict so state survives pod restarts.
"""
import os
import json
import time
from typing import Optional, Dict
from pydantic import BaseModel

import redis

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
JOB_KEY_PREFIX = "pxml:job:"
JOB_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days


# ─────────────────────────────────────────────
# Job Model
# ─────────────────────────────────────────────
class JobStatus(BaseModel):
    job_id: str
    status: str = "queued"  # queued | running | completed | failed
    current_step: int = 0
    current_step_name: str = ""
    total_steps: int = 10
    progress_pct: int = 0
    file_name: str = ""
    output_dir: str = ""
    output_file: str = ""
    error: str = ""
    log: str = ""
    started_at: Optional[float] = None
    completed_at: Optional[float] = None


# ─────────────────────────────────────────────
# Redis Job Store
# ─────────────────────────────────────────────
class JobStore:
    """Thread-safe Redis-backed store for job state."""

    def __init__(self, redis_url: str = REDIS_URL):
        self._redis = redis.Redis.from_url(redis_url, decode_responses=True)

    def _key(self, job_id: str) -> str:
        return f"{JOB_KEY_PREFIX}{job_id}"

    def create(self, job: JobStatus) -> None:
        """Create a new job entry."""
        self._redis.setex(
            self._key(job.job_id),
            JOB_TTL_SECONDS,
            job.model_dump_json(),
        )

    def get(self, job_id: str) -> Optional[JobStatus]:
        """Retrieve a job by ID. Returns None if not found."""
        data = self._redis.get(self._key(job_id))
        if data is None:
            return None
        return JobStatus.model_validate_json(data)

    def update(self, job: JobStatus) -> None:
        """Update an existing job (full overwrite). Resets TTL."""
        self._redis.setex(
            self._key(job.job_id),
            JOB_TTL_SECONDS,
            job.model_dump_json(),
        )

    def exists(self, job_id: str) -> bool:
        return self._redis.exists(self._key(job_id)) > 0

    def update_field(self, job_id: str, **kwargs) -> Optional[JobStatus]:
        """Atomically update specific fields of a job."""
        job = self.get(job_id)
        if job is None:
            return None
        for k, v in kwargs.items():
            if hasattr(job, k):
                setattr(job, k, v)
        self.update(job)
        return job

    def mark_stale_jobs_failed(self) -> int:
        """
        On startup, find any jobs stuck in 'running' or 'queued' state
        (orphaned by a pod restart) and mark them failed.
        Returns count of cleaned jobs.
        """
        pattern = f"{JOB_KEY_PREFIX}*"
        cleaned = 0
        for key in self._redis.scan_iter(match=pattern, count=100):
            data = self._redis.get(key)
            if data:
                job = JobStatus.model_validate_json(data)
                if job.status in ("running", "queued"):
                    job.status = "failed"
                    job.error = "Job interrupted by pod restart. Please resubmit."
                    job.completed_at = time.time()
                    self._redis.setex(key, JOB_TTL_SECONDS, job.model_dump_json())
                    cleaned += 1
        return cleaned

    def list_jobs(self, limit: int = 50) -> list[JobStatus]:
        """List recent jobs (for admin/debug)."""
        pattern = f"{JOB_KEY_PREFIX}*"
        result = []
        for key in self._redis.scan_iter(match=pattern, count=100):
            data = self._redis.get(key)
            if data:
                result.append(JobStatus.model_validate_json(data))
            if len(result) >= limit:
                break
        return result


# Singleton instance
job_store = JobStore()
