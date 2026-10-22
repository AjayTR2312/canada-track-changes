# Storage: everything lives in S3

The service is **stateless**. A pod keeps only throw-away scratch files (`/tmp`); every piece of state
that must survive a restart is in one S3 bucket, so pods can be killed, rescheduled or scaled out
without losing anything that was stored. SharePoint is no longer used for storage.

Bucket: `a209309-s3-cdntrackchanges-653514985368-us-east-1-an` (`S3_BUCKET_NAME`, region `us-east-1`).
No access keys: boto3 uses the pod's IAM role (IRSA / Pod Identity / instance profile).

## What is stored where

| Data | S3 key | Written by |
|---|---|---|
| Job inputs | `uploads/<job_id>/…` | `/api/process`, batch driver |
| Job outputs (PXML, reports, log) | `outputs/<job_id>/…` | worker at the end of the run, again after validation |
| Job record | `jobs/<job_id>/job.json` | owning pod, on every state change |
| Live progress / cancel flag / heartbeat | `jobs/<job_id>/status.json`, `cancel.flag`, `heartbeat.json` | worker / any pod / owning pod |
| "My recent jobs" index | `jobs_index/<emp_id>/<job_id>.json` | owning pod |
| Batch record + staged inputs | `batches/<batch_id>/batch.json`, `input/…`, `cancel.flag` | batch endpoints / driver |
| Published batch output | `published/<emp_id>/<publication>_Output_<ts>/<chapter>/…` (or next to the input folder for an S3-folder batch) | batch driver |
| Validation sessions | `validation_sessions/<session_id>/…` (+ `_session.json` progress record) | validation endpoints |
| "My sessions" index | `validation_index/<emp_id>/<session_id>.json` | validation endpoints |
| Draft panel workbook | `draft_revisions/Failed_revisions.xlsx` | `/api/draft-revisions/upload` |
| Users | `users/<emp_id>.json` | register / profile update |
| Usage (one record per run) | `usage/<emp_id>/<YYYYMMDD>/<session>.json` | job completion |
| False positives / corrections / resolver runs | `feedback/<region>/<kind>/…json` | reviewer actions |
| Triage reports | `feedback/<region>/triage/triage_<ts>.{json,md,xlsx}` | developer triage page |

Logs, users and usage are **one object per record**, not CSV files: several pods write at once, and an
upsert/removal is then a single atomic PUT/DELETE (a shared CSV would be downloaded, edited and
re-uploaded, silently losing a concurrent writer's rows).

## How multiple pods cooperate

* Any pod answers for any job, batch or session by reading S3 (status, logs, downloads, recent jobs, resume).
* A job runs inside the pod that received it. That pod writes a heartbeat; progress goes to S3 at every step.
* "Stop" works from any pod: it raises a flag in S3 that the worker checks at each step boundary.
* Downloads are built/streamed from S3 (HTTP Range supported for PDFs); nothing is served from a pod's disk.
* If a pod is killed **while running a job**, that run cannot continue. On a graceful shutdown the pod marks
  its running jobs/batches *interrupted* right away; after a hard kill another pod does so once the heartbeat is
  `JOB_STALE_SECONDS` (120 s) old. The inputs are already in S3, so the chapter can simply be run again
  (batch chapters: *Run again*).
* Anything that needs a job's files locally (re-validation, e-mail) restores them from S3 on demand and removes
  them afterwards.

## Configuration

See `.env.example`: `S3_BUCKET_NAME`, `AWS_REGION`, optional `S3_KEY_PREFIX` (share one bucket between
deployments), `S3_ENDPOINT_URL`, and `S3_LOCAL_DIR` (**dev/test only**: use a folder as a fake bucket).
`GET /api/health` always answers 200 and reports `storage.ok` so a transient S3 error never gets a pod killed.

## Cluster setup checklist

1. Attach `k8s/s3-policy.json` to an IAM role trusted by the cluster (IRSA) and put its ARN in
   `k8s/serviceaccount.yaml` (or use EKS Pod Identity / a node instance profile).
2. `kubectl apply -f k8s/namespace.yaml -f k8s/serviceaccount.yaml -f k8s/api.yaml -f k8s/streamlit.yaml`.
3. Do **not** bake static `AWS_ACCESS_KEY_ID/SECRET` into the image (they would override the role).
4. Add S3 **lifecycle rules** – nothing deletes old data any more (a pod restart used to wipe `/tmp`):
   expire `uploads/`, `outputs/`, `jobs/`, `jobs_index/`, `batches/` (e.g. after 30–90 days); optionally abort
   incomplete multipart uploads after 7 days.
   ```json
   {"Rules": [
     {"ID": "expire-job-data", "Status": "Enabled", "Filter": {"Prefix": "outputs/"}, "Expiration": {"Days": 60}},
     {"ID": "expire-job-inputs", "Status": "Enabled", "Filter": {"Prefix": "uploads/"}, "Expiration": {"Days": 60}},
     {"ID": "expire-job-records", "Status": "Enabled", "Filter": {"Prefix": "jobs/"}, "Expiration": {"Days": 60}},
     {"ID": "expire-batches", "Status": "Enabled", "Filter": {"Prefix": "batches/"}, "Expiration": {"Days": 60}},
     {"ID": "abort-multipart", "Status": "Enabled", "Filter": {"Prefix": ""}, "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}}
   ]}
   ```
   (Keep `users/`, `usage/`, `feedback/` and `validation_sessions/` unless you have a retention policy.)

## Moving existing data

`python tools/migrate_to_s3.py --dry-run`, then without `--dry-run`, imports `local_users.json`, `usage_log.csv`,
`data/<region>/*.csv`, `validation_sessions/` and the draft workbook. For history that only exists in SharePoint,
download the CSVs and pass `--users-csv`, `--usage-csv`, `--false-positive-csv`, … (see the script's help).

## Tests

`pip install -r requirements-dev.txt && pytest tests` – runs the whole storage layer, the job / validation-session /
batch lifecycles and the "a different pod answers" cases against a fake S3 (moto); no AWS account needed.
