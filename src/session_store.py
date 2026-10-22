"""Validation sessions, stored in S3.

A validation session is the set of files a reviewer works on in the Visual Validator (DOCX, PXML,
PDF, validation Excel, Failed_revisions.xlsx, false_positives.xlsx) plus its progress record
(`_session.json`).  Everything lives under

    validation_sessions/<session_id>/<file>
    validation_index/<emp_id>/<session_id>.json      (copy of the progress record, for "my sessions")

so any API pod can serve, update or resume any session, and a pod restart loses nothing.
Pods never keep session files: they stream them from / to S3 on demand.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import uuid
from typing import Optional

import s3_utils as s3

META_NAME = "_session.json"


def new_session_id() -> str:
    return uuid.uuid4().hex[:8]


def _prefix(session_id: str) -> str:
    return f"{s3.S3_PREFIX_SESSIONS}/{s3.safe_id(session_id)}/"


def key(session_id: str, name: str) -> str:
    return _prefix(session_id) + s3.safe_filename(name)


def _index_key(emp_id: str, session_id: str) -> str:
    return f"{s3.S3_PREFIX_SESSIONS_INDEX}/{s3.safe_id(emp_id, '_unassigned')}/{s3.safe_id(session_id)}.json"


# ---- progress record -----------------------------------------------------------
def exists(session_id: str) -> bool:
    return s3.object_exists(_prefix(session_id) + META_NAME)


def load_meta(session_id: str) -> Optional[dict]:
    return s3.get_json(_prefix(session_id) + META_NAME)


def save_meta(meta: dict) -> dict:
    """Persist the progress record (and its per-user index copy)."""
    sid = meta["session_id"]
    meta["storage_prefix"] = f"s3://{s3.S3_BUCKET_NAME}/{_prefix(sid)}"
    s3.put_json(_prefix(sid) + META_NAME, meta)
    s3.put_json(_index_key(meta.get("emp_id", ""), sid), meta)
    return meta


def touch(meta: dict) -> dict:
    meta["updated_at"] = time.time()
    return save_meta(meta)


def list_sessions(emp_id: str = "") -> list:
    """Progress records of one user's sessions (or everyone's when emp_id is blank)."""
    prefix = f"{s3.S3_PREFIX_SESSIONS_INDEX}/" + (f"{s3.safe_id(emp_id, '_unassigned')}/" if emp_id else "")
    keys = [o["key"] for o in s3.list_objects(prefix) if o["key"].endswith(".json")]
    return s3.read_many_json(keys)


def delete_session(session_id: str) -> bool:
    """Remove every file of the session and its index entry. True if anything was there."""
    meta = load_meta(session_id) or {}
    removed = s3.delete_prefix(_prefix(session_id))
    s3.delete_object(_index_key(meta.get("emp_id", ""), session_id))
    return bool(removed or meta)


# ---- files ---------------------------------------------------------------------
def _guard(name: str) -> str:
    n = s3.safe_filename(name)
    if n == META_NAME:
        raise ValueError(f"'{META_NAME}' is reserved")
    return n


def put_file(session_id: str, name: str, data: bytes) -> str:
    return s3.put_bytes(_prefix(session_id) + _guard(name), data)


def put_file_from_path(session_id: str, name: str, path: str) -> str:
    return s3.upload_file_to_s3(path, _prefix(session_id) + _guard(name))


def get_file(session_id: str, name: str) -> Optional[bytes]:
    return s3.get_bytes(key(session_id, name))


def file_exists(session_id: str, name: str) -> bool:
    return s3.object_exists(key(session_id, name))


def file_info(session_id: str, name: str) -> Optional[dict]:
    return s3.object_info(key(session_id, name))


def stream_file(session_id: str, name: str, byte_range: Optional[tuple] = None):
    """(chunk iterator, info) or None when the file does not exist."""
    return s3.open_stream(key(session_id, name), byte_range)


def download_file(session_id: str, name: str, dest_path: str) -> bool:
    try:
        s3.download_file_from_s3(key(session_id, name), dest_path)
        return True
    except FileNotFoundError:
        return False


def delete_file(session_id: str, name: str) -> bool:
    return s3.delete_object(key(session_id, name))


def edit_file(session_id: str, name: str, fn) -> bool:
    """Edit a session file that needs a real path (e.g. openpyxl): download it to a scratch folder,
    let `fn(local_path)` modify it, upload the result. False when the file does not exist."""
    scratch = tempfile.mkdtemp(prefix=f"sessfile_{s3.safe_id(session_id)}_")
    try:
        local = os.path.join(scratch, s3.safe_filename(name))
        if not download_file(session_id, name, local):
            return False
        fn(local)
        put_file_from_path(session_id, name, local)
        return True
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def list_files(session_id: str) -> list:
    """File names in the session (the progress record is not listed)."""
    prefix = _prefix(session_id)
    return [o["key"][len(prefix):] for o in s3.list_objects(prefix)
            if o["key"][len(prefix):] and o["key"][len(prefix):] != META_NAME]


def new_meta(session_id: str, emp_id: str, session_name: str, **fields) -> dict:
    now = time.time()
    meta = {
        "session_id": session_id,
        "session_name": session_name,
        "emp_id": emp_id,
        "created_at": now,
        "updated_at": now,
        "docx_filename": None,
        "pxml_filename": None,
        "excel_filename": None,
        "pdf_filename": None,
        "failed_revisions_filename": None,
        "current_revision_index": 0,
        "total_revisions": 0,
        "total_invalid": 0,
        "revision_statuses": {},
    }
    meta.update(fields)
    return meta
