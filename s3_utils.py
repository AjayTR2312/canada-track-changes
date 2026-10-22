"""
S3 storage layer -- the ONE place that owns persistence for this service.

Every piece of state that must survive a pod restart lives in a single S3 bucket
(job records, batch records, validation sessions, uploads/outputs, usage + user
records, feedback logs, triage reports).  Pods keep only throw-away scratch files.

Credentials: none are configured here.  boto3's default credential chain is used
(IRSA / EKS Pod Identity / EC2 instance role / env vars if someone sets them).

Configuration (all optional):
    S3_BUCKET_NAME        bucket (default: the organisation bucket below)
    AWS_REGION            region (default us-east-1)
    S3_KEY_PREFIX         prepended to EVERY key -- use it to share one bucket between
                          deployments, e.g. "canada/" (default: none)
    S3_ENDPOINT_URL       custom endpoint (VPC endpoint / MinIO / LocalStack)
    S3_LOCAL_DIR          DEV/TEST ONLY: use this folder as a fake bucket instead of S3
    PRESIGNED_URL_EXPIRY  presigned URL lifetime in seconds (default 3600)

Key layout (logical keys; S3_KEY_PREFIX is added transparently):
    uploads/<job_id>/...                         job inputs
    outputs/<job_id>/...                         job outputs (pipeline files, logs, validation report)
    jobs/<job_id>/job.json | status.json | heartbeat.json | cancel.flag
    jobs_index/<emp_id>/<job_id>.json            per-user job index (recent jobs)
    batches/<batch_id>/batch.json | heartbeat.json | input/<file>
    published/<publication>_Output_<ts>/<task>/  batch outputs published for people
    validation_sessions/<session_id>/...         validation session files + _session.json
    validation_index/<emp_id>/<session_id>.json  per-user session index
    draft_revisions/Failed_revisions.xlsx        shared "Draft" panel workbook
    users/<emp_id>.json                          registered users
    usage/<emp_id>/<YYYYMMDD>/<session>.json     one record per processing run
    feedback/<region>/<kind>/...                 false positives, corrections, resolver runs, triage
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

# ─── Configuration ────────────────────────────────────────────────────────────
DEFAULT_BUCKET = "a209309-s3-cdntrackchanges-653514985368-us-east-1-an"
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", DEFAULT_BUCKET) or DEFAULT_BUCKET
S3_REGION = os.getenv("AWS_REGION", "us-east-1") or "us-east-1"
PRESIGNED_URL_EXPIRY = int(os.getenv("PRESIGNED_URL_EXPIRY", "3600"))

S3_PREFIX_UPLOADS = "uploads"
S3_PREFIX_OUTPUTS = "outputs"
S3_PREFIX_JOBS = "jobs"
S3_PREFIX_JOBS_INDEX = "jobs_index"
S3_PREFIX_BATCHES = "batches"
S3_PREFIX_PUBLISHED = "published"
S3_PREFIX_SESSIONS = "validation_sessions"
S3_PREFIX_SESSIONS_INDEX = "validation_index"
S3_PREFIX_USERS = "users"
S3_PREFIX_USAGE = "usage"
S3_PREFIX_FEEDBACK = "feedback"
S3_PREFIX_DRAFT = "draft_revisions"

# Prefixes a user-supplied "S3 folder" (batch input) may never point into.
RESERVED_PREFIXES = (
    S3_PREFIX_UPLOADS, S3_PREFIX_OUTPUTS, S3_PREFIX_JOBS, S3_PREFIX_JOBS_INDEX,
    S3_PREFIX_BATCHES, S3_PREFIX_PUBLISHED, S3_PREFIX_SESSIONS, S3_PREFIX_SESSIONS_INDEX,
    S3_PREFIX_USERS, S3_PREFIX_USAGE, S3_PREFIX_FEEDBACK, S3_PREFIX_DRAFT,
)

_TRANSFER_WORKERS = 8


# ─── Key helpers ──────────────────────────────────────────────────────────────
_ID_BAD = re.compile(r"[^A-Za-z0-9._@\-]")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def safe_id(value, default: str = "unknown") -> str:
    """Make an id (emp_id, session_id, region ...) safe to use as ONE key segment."""
    s = _ID_BAD.sub("_", str(value or "").strip())
    s = s.strip(".")           # no '.', '..'
    return s[:120] or default


def safe_filename(name, default: str = "file") -> str:
    """A user-supplied file name reduced to a single, harmless key segment."""
    s = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    s = _CTRL.sub("", s).strip()
    if s in ("", ".", ".."):
        return default
    return s[:200]


def join_key(*parts: str) -> str:
    return "/".join(str(p).strip("/") for p in parts if str(p).strip("/"))


# ─── Backends ─────────────────────────────────────────────────────────────────
class _NotFound(Exception):
    pass


def _key_prefix() -> str:
    p = os.getenv("S3_KEY_PREFIX", "").strip().strip("/")
    return p + "/" if p else ""


def _bucket() -> str:
    return os.getenv("S3_BUCKET_NAME", DEFAULT_BUCKET) or DEFAULT_BUCKET


_clients: dict = {}
_clients_lock = threading.Lock()


def get_s3_client():
    """boto3 S3 client, one per process (so it is safe after ProcessPoolExecutor forks)."""
    pid = os.getpid()
    cli = _clients.get(pid)
    if cli is None:
        with _clients_lock:
            cli = _clients.get(pid)
            if cli is None:
                import boto3
                from botocore.config import Config
                kwargs = {}
                if os.getenv("S3_ENDPOINT_URL"):
                    kwargs["endpoint_url"] = os.getenv("S3_ENDPOINT_URL")
                cli = boto3.client(
                    "s3", region_name=S3_REGION,
                    config=Config(retries={"max_attempts": 6, "mode": "adaptive"},
                                  max_pool_connections=64, signature_version="s3v4"),
                    **kwargs,
                )
                _clients.clear()
                _clients[pid] = cli
    return cli


def _local_root() -> Optional[str]:
    d = os.getenv("S3_LOCAL_DIR", "").strip()
    return d or None


def _is_not_found(exc: Exception) -> bool:
    try:
        from botocore.exceptions import ClientError
    except Exception:  # pragma: no cover
        return False
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        return code in ("404", "NoSuchKey", "NotFound", "NoSuchBucket")
    return False


def _local_path(key: str) -> str:
    root = os.path.abspath(_local_root())
    path = os.path.abspath(os.path.join(root, *(_key_prefix() + key).split("/")))
    if not (path == root or path.startswith(root + os.sep)):
        raise ValueError(f"Illegal key: {key!r}")
    return path


def _norm(key: str) -> str:
    key = str(key).replace("\\", "/").lstrip("/")
    if ".." in key.split("/"):
        raise ValueError(f"Illegal key: {key!r}")
    return key


def _strip(full_key: str) -> str:
    pre = _key_prefix()
    return full_key[len(pre):] if pre and full_key.startswith(pre) else full_key


# ─── Primitives ───────────────────────────────────────────────────────────────
def put_bytes(key: str, data: bytes, content_type: Optional[str] = None) -> str:
    key = _norm(key)
    ctype = content_type or mimetypes.guess_type(key)[0] or "application/octet-stream"
    if _local_root():
        path = _local_path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return key
    get_s3_client().put_object(Bucket=_bucket(), Key=_key_prefix() + key, Body=data, ContentType=ctype)
    return key


def upload_file_to_s3(local_path: str, s3_key: str) -> str:
    """Upload a local file (multipart for big files)."""
    key = _norm(s3_key)
    if _local_root():
        path = _local_path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shutil.copyfile(local_path, path)
        return key
    ctype = mimetypes.guess_type(key)[0] or "application/octet-stream"
    get_s3_client().upload_file(local_path, _bucket(), _key_prefix() + key, ExtraArgs={"ContentType": ctype})
    return key


def get_bytes(key: str) -> Optional[bytes]:
    """Object body, or None when the key does not exist."""
    key = _norm(key)
    if _local_root():
        try:
            with open(_local_path(key), "rb") as f:
                return f.read()
        except (FileNotFoundError, IsADirectoryError):
            return None
    try:
        return get_s3_client().get_object(Bucket=_bucket(), Key=_key_prefix() + key)["Body"].read()
    except Exception as e:
        if _is_not_found(e):
            return None
        raise


def download_file_from_s3(s3_key: str, local_path: str) -> str:
    """Download one object to a local file. Raises FileNotFoundError when it does not exist."""
    key = _norm(s3_key)
    d = os.path.dirname(local_path)
    if d:
        os.makedirs(d, exist_ok=True)
    if _local_root():
        src = _local_path(key)
        if not os.path.isfile(src):
            raise FileNotFoundError(f"s3://{_bucket()}/{key}")
        shutil.copyfile(src, local_path)
        return local_path
    try:
        get_s3_client().download_file(_bucket(), _key_prefix() + key, local_path)
    except Exception as e:
        if _is_not_found(e):
            raise FileNotFoundError(f"s3://{_bucket()}/{key}") from e
        raise
    return local_path


def object_info(key: str) -> Optional[dict]:
    """{size, last_modified (epoch seconds), content_type} or None when missing."""
    key = _norm(key)
    if _local_root():
        p = _local_path(key)
        if not os.path.isfile(p):
            return None
        st = os.stat(p)
        return {"size": st.st_size, "last_modified": st.st_mtime,
                "content_type": mimetypes.guess_type(key)[0] or "application/octet-stream"}
    try:
        r = get_s3_client().head_object(Bucket=_bucket(), Key=_key_prefix() + key)
    except Exception as e:
        if _is_not_found(e):
            return None
        raise
    return {"size": r.get("ContentLength", 0), "last_modified": r["LastModified"].timestamp(),
            "content_type": r.get("ContentType") or "application/octet-stream"}


def object_exists(key: str) -> bool:
    return object_info(key) is not None


def delete_object(key: str) -> bool:
    """Delete one object; True if it existed."""
    key = _norm(key)
    if _local_root():
        p = _local_path(key)
        if os.path.isfile(p):
            os.remove(p)
            return True
        return False
    existed = object_info(key) is not None
    get_s3_client().delete_object(Bucket=_bucket(), Key=_key_prefix() + key)
    return existed


def list_objects(prefix: str, limit: Optional[int] = None) -> list:
    """Every object under `prefix` as [{key, size, last_modified}] (logical keys)."""
    prefix = _norm(prefix) if prefix else ""
    out: list = []
    if _local_root():
        # Dev/test backend: walk the fake bucket and filter with true prefix semantics.
        root = os.path.abspath(_local_root())
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                full = os.path.join(dirpath, fn)
                logical = _strip(os.path.relpath(full, root).replace(os.sep, "/"))
                if logical.startswith(prefix):
                    st = os.stat(full)
                    out.append({"key": logical, "size": st.st_size, "last_modified": st.st_mtime})
        out.sort(key=lambda o: o["key"])
        return out[:limit] if limit else out
    paginator = get_s3_client().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=_bucket(), Prefix=_key_prefix() + prefix):
        for o in page.get("Contents", []):
            if o["Key"].endswith("/") and o["Size"] == 0:
                continue  # console-created "folder" marker
            out.append({"key": _strip(o["Key"]), "size": o["Size"], "last_modified": o["LastModified"].timestamp()})
            if limit and len(out) >= limit:
                return out
    return out


def list_common_prefixes(prefix: str) -> list:
    """Immediate 'sub-folder' names under prefix (which should end with '/')."""
    prefix = _norm(prefix) if prefix else ""
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    if _local_root():
        d = _local_path(prefix.rstrip("/")) if prefix else os.path.abspath(_local_root())
        return sorted(n for n in os.listdir(d) if os.path.isdir(os.path.join(d, n))) if os.path.isdir(d) else []
    names = []
    paginator = get_s3_client().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=_bucket(), Prefix=_key_prefix() + prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            names.append(cp["Prefix"][len(_key_prefix() + prefix):].rstrip("/"))
    return names


def delete_prefix(prefix: str) -> int:
    """Delete every object under prefix. Returns how many were removed."""
    prefix = _norm(prefix)
    if not prefix.endswith("/"):
        prefix += "/"          # never let "abc" also wipe "abcd/..."
    keys = [o["key"] for o in list_objects(prefix)]
    if _local_root():
        for k in keys:
            try:
                os.remove(_local_path(k))
            except OSError:
                pass
        return len(keys)
    cli = get_s3_client()
    for i in range(0, len(keys), 1000):
        chunk = keys[i:i + 1000]
        cli.delete_objects(Bucket=_bucket(), Delete={"Objects": [{"Key": _key_prefix() + k} for k in chunk], "Quiet": True})
    return len(keys)


def copy_object(src_key: str, dst_key: str) -> str:
    """Server-side copy (no data passes through the pod)."""
    src, dst = _norm(src_key), _norm(dst_key)
    if _local_root():
        d = _local_path(dst)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copyfile(_local_path(src), d)
        return dst
    get_s3_client().copy({"Bucket": _bucket(), "Key": _key_prefix() + src}, _bucket(), _key_prefix() + dst)
    return dst


def copy_prefix(src_prefix: str, dst_prefix: str, exclude=None) -> list:
    """Server-side copy of everything under src_prefix to dst_prefix. Returns the destination keys."""
    src_prefix = _norm(src_prefix).rstrip("/") + "/"
    dst_prefix = _norm(dst_prefix).rstrip("/") + "/"
    copied = []
    for o in list_objects(src_prefix):
        rel = o["key"][len(src_prefix):]
        if not rel or (exclude and exclude(rel)):
            continue
        copied.append(copy_object(o["key"], dst_prefix + rel))
    return copied


def put_json(key: str, obj) -> str:
    return put_bytes(key, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"), "application/json")


def get_json(key: str, default=None):
    raw = get_bytes(key)
    if raw is None:
        return default
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        return default


def read_many_json(keys: list, workers: int = 16) -> list:
    """Fetch many small JSON objects in parallel; unreadable/missing ones are skipped."""
    if not keys:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(keys))) as pool:
        results = list(pool.map(lambda k: get_json(k), keys))
    return [r for r in results if r is not None]


def open_stream(key: str, byte_range: Optional[tuple] = None):
    """(chunk iterator, info) for proxying an object to a client. byte_range=(start, end|None) inclusive.
    Returns None when the key does not exist."""
    key = _norm(key)
    info = object_info(key)
    if info is None:
        return None
    start, end = byte_range if byte_range else (0, None)
    end = info["size"] - 1 if end is None or end >= info["size"] else end
    if _local_root():
        def _gen():
            with open(_local_path(key), "rb") as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = f.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
        return _gen(), info
    kwargs = {"Range": f"bytes={start}-{end}"} if byte_range else {}
    body = get_s3_client().get_object(Bucket=_bucket(), Key=_key_prefix() + key, **kwargs)["Body"]

    def _gen():
        try:
            for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                yield chunk
        finally:
            body.close()
    return _gen(), info


def generate_presigned_url(s3_key: str, expiry: int = None, filename: str = None) -> str:
    """Presigned GET URL (or a file:// URL in S3_LOCAL_DIR dev mode)."""
    key = _norm(s3_key)
    if _local_root():
        return "file:///" + _local_path(key).replace(os.sep, "/")
    params = {"Bucket": _bucket(), "Key": _key_prefix() + key}
    if filename:
        params["ResponseContentDisposition"] = f'attachment; filename="{safe_filename(filename)}"'
    return get_s3_client().generate_presigned_url("get_object", Params=params, ExpiresIn=expiry or PRESIGNED_URL_EXPIRY)


# ─── Health ───────────────────────────────────────────────────────────────────
_health_cache = {"at": 0.0, "value": (True, "")}


def check_storage(max_age: float = 30.0) -> tuple:
    """(ok, message): can this pod reach the bucket? Cached so health probes stay cheap."""
    now = time.time()
    if now - _health_cache["at"] < max_age:
        return _health_cache["value"]
    try:
        if _local_root():
            os.makedirs(_local_root(), exist_ok=True)
            value = (True, f"local dir {_local_root()} (dev mode)")
        else:
            get_s3_client().head_bucket(Bucket=_bucket())
            value = (True, f"s3://{_bucket()}")
    except Exception as e:
        value = (False, f"{type(e).__name__}: {e}")
    _health_cache.update(at=now, value=value)
    return value


# ─── Directory helpers ────────────────────────────────────────────────────────
def upload_directory_to_s3(local_dir: str, s3_prefix: str, exclude=None) -> list:
    """Upload a directory tree (parallel). `exclude(relative_posix_path) -> bool` skips files."""
    jobs = []
    for root, _dirs, files in os.walk(local_dir):
        for fn in files:
            local = os.path.join(root, fn)
            rel = os.path.relpath(local, local_dir).replace("\\", "/")
            if exclude and exclude(rel):
                continue
            jobs.append((local, join_key(s3_prefix, rel)))
    if not jobs:
        return []
    with ThreadPoolExecutor(max_workers=_TRANSFER_WORKERS) as pool:
        list(pool.map(lambda j: upload_file_to_s3(j[0], j[1]), jobs))
    return sorted(k for _l, k in jobs)


def download_directory_from_s3(s3_prefix: str, local_dir: str) -> list:
    """Download everything under a prefix into local_dir (parallel). Returns local paths."""
    prefix = _norm(s3_prefix).rstrip("/") + "/"
    jobs = []
    for o in list_objects(prefix):
        rel = o["key"][len(prefix):]
        if not rel or ".." in rel.split("/"):
            continue
        jobs.append((o["key"], os.path.join(local_dir, *rel.split("/"))))
    if not jobs:
        return []
    with ThreadPoolExecutor(max_workers=_TRANSFER_WORKERS) as pool:
        list(pool.map(lambda j: download_file_from_s3(j[0], j[1]), jobs))
    return [p for _k, p in jobs]


# ─── Job inputs / outputs ─────────────────────────────────────────────────────
# Housekeeping files a worker writes into the output folder that must not be published.
def _is_internal_output(rel: str) -> bool:
    return rel.startswith("_cancel") or rel.startswith("_job_status")


def upload_job_inputs(job_id: str, docx_path: str, pxml_path: str, images_dir: str = None) -> dict:
    """Upload all job input files under uploads/<job_id>/. Returns the S3 keys."""
    prefix = f"{S3_PREFIX_UPLOADS}/{safe_id(job_id)}"
    keys = {
        "docx": upload_file_to_s3(docx_path, f"{prefix}/{safe_filename(docx_path)}"),
        "pxml": upload_file_to_s3(pxml_path, f"{prefix}/{safe_filename(pxml_path)}"),
    }
    if images_dir and os.path.isdir(images_dir):
        keys["images"] = upload_directory_to_s3(images_dir, f"{prefix}/images")
    return keys


def upload_job_outputs(job_id: str, output_dir: str) -> list:
    """Upload (or refresh) every output file under outputs/<job_id>/."""
    return upload_directory_to_s3(output_dir, f"{S3_PREFIX_OUTPUTS}/{safe_id(job_id)}", exclude=_is_internal_output)


def get_output_download_url(job_id: str, filename: str) -> str:
    return generate_presigned_url(f"{S3_PREFIX_OUTPUTS}/{safe_id(job_id)}/{filename}")


def list_output_files(job_id: str) -> list:
    """All output files of a job: [{key, filename, size}]."""
    prefix = f"{S3_PREFIX_OUTPUTS}/{safe_id(job_id)}/"
    return [{"key": o["key"], "filename": o["key"][len(prefix):], "size": o["size"]}
            for o in list_objects(prefix) if o["key"][len(prefix):]]


def new_scratch_dir(prefix: str = "pxml_") -> str:
    """A fresh throw-away directory (pod-local, never relied on across requests)."""
    return tempfile.mkdtemp(prefix=prefix)
