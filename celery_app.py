"""
Celery application for PXML Pipeline async task execution.
Workers run pipeline jobs independently from the API pod.

Start worker:  celery -A celery_app worker --loglevel=info --concurrency=2
"""
import os
import time
from celery import Celery

from job_store import job_store, JobStatus

# ─────────────────────────────────────────────
# Celery Configuration
# ─────────────────────────────────────────────
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "pxml_pipeline",
    broker=REDIS_URL,
    backend=REDIS_URL,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,  # Don't ack until task finishes (enables retry on worker crash)
    worker_prefetch_multiplier=1,  # Only grab 1 task at a time per worker
    task_reject_on_worker_lost=True,  # Re-queue task if worker dies
    task_soft_time_limit=7200,  # 2 hour soft limit
    task_time_limit=7500,  # 2h5m hard limit
    worker_max_tasks_per_child=5,  # Restart worker after 5 tasks (prevents memory leaks)
)


# ─────────────────────────────────────────────
# Pipeline Task
# ─────────────────────────────────────────────
@celery_app.task(bind=True, max_retries=1, default_retry_delay=60)
def run_pipeline_task(self, job_id: str, docx_path: str, pxml_path: str,
                      output_dir: str, product_type: str, images_dir: str = None):
    """
    Execute the full PXML pipeline as a Celery task.
    Delegates to _run_pipeline_logic which handles logging, status updates, etc.
    """
    try:
        from pipeline_api import _run_pipeline_logic
        _run_pipeline_logic(
            job_id=job_id,
            docx_path=docx_path,
            pxml_path=pxml_path,
            output_dir=output_dir,
            product_type=product_type,
            images_dir=images_dir,
        )
    except Exception as e:
        # If pipeline didn't mark itself failed, do it here
        job = job_store.get(job_id)
        if job and job.status not in ("failed", "completed"):
            job.status = "failed"
            job.error = str(e)
            job_store.update(job)

        # Retry once
        if self.request.retries < self.max_retries:
            job = job_store.get(job_id)
            if job:
                job.status = "queued"
                job.error = f"Retrying after error: {e}"
                job_store.update(job)
            raise self.retry(exc=e)


@celery_app.task(bind=True, max_retries=0)
def run_validation_task(self, job_id: str, docx_path: str, final_xml_path: str,
                        output_dir: str, prompt_path: str, validation_prompt_path: str,
                        images_folder: str = None):
    """Run validation as a Celery task."""
    from exe_track_changes_1 import run_validation_for_gui

    job = job_store.get(job_id)
    if job:
        job.current_step_name = "Running Validation"
        job_store.update(job)

    excel_path = run_validation_for_gui(
        docx_path=docx_path,
        final_xml_path=final_xml_path,
        output_dir=output_dir,
        prompt_path=prompt_path,
        validation_prompt_path=validation_prompt_path,
        images_folder=images_folder,
    )
    return excel_path
