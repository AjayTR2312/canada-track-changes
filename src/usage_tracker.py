"""
usage_tracker.py  --  Standalone usage logging + user registry module, backed by S3.

Can be imported by any TR XML processing tool -- not specific to Insert Instructions.

All usage and user data is stored in the S3 bucket (see s3_utils), one small JSON object per
record, so any number of pods can write at the same time without overwriting each other and a pod
restart loses nothing:

    users/<emp_id>.json                        one per registered user
    usage/<emp_id>/<YYYYMMDD>/<session>.json   one per processing run

All timestamps are in IST (UTC+5:30).

--- Integration guide ---

    from src.usage_tracker import log_usage, lookup_user, save_user

1.  At session/login start -- resolve user profile:
        user = lookup_user(emp_id)        # returns dict or None

2.  On first-time registration -- persist user:
        save_user(emp_id, name, email, dept, title)

3.  At end of each processing run -- record outcome:
        log_usage(
            emp_id, name, email, dept, title,
            service_type,         # e.g. "Insert with Instructions"
            files_uploaded, inserts_count, inserts_completed, llm_calls,
            files_completed, files_failed,
            run_outcome,          # "success" | "stopped" | "error"
            processing_time_sec,
            region, product, session_id,
        )
"""

import logging
import uuid
from datetime import datetime, timezone, timedelta

import s3_utils

logger = logging.getLogger(__name__)

# IST = UTC+5:30
_IST = timezone(timedelta(hours=5, minutes=30))

HEADERS = [
    "timestamp",            # ISO 8601 with IST offset
    "date",                 # YYYY-MM-DD (IST)
    "time",                 # HH:MM:SS (IST)
    "emp_id",
    "name",
    "email",
    "department",
    "job_title",
    "service_type",         # which XML conversion app (e.g. "Insert with Instructions")
    "files_uploaded",
    "inserts_count",
    "inserts_completed",
    "llm_calls",
    "files_completed",
    "files_failed",
    "run_outcome",          # success / stopped / error
    "processing_time_sec",
    "region",
    "product",
    "session_id",
    "run_number_today",     # 1 = first run of the day for this user
]

USER_HEADERS = ["emp_id", "name", "email", "department", "job_title"]


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------
def _user_key(emp_id: str) -> str:
    return f"{s3_utils.S3_PREFIX_USERS}/{s3_utils.safe_id(emp_id)}.json"


def _usage_prefix(emp_id: str = "") -> str:
    base = f"{s3_utils.S3_PREFIX_USAGE}/"
    return base + (f"{s3_utils.safe_id(emp_id)}/" if emp_id else "")


# ---------------------------------------------------------------------------
# Row builder
# ---------------------------------------------------------------------------
def _make_row(now, emp_id, name, email, dept, title, service_type,
              files_uploaded, inserts_count, inserts_completed, llm_calls,
              files_completed, files_failed, run_outcome,
              processing_time_sec, region, product, session_id,
              run_number_today="") -> dict:
    return {
        "timestamp":           now.isoformat(),
        "date":                now.strftime("%Y-%m-%d"),
        "time":                now.strftime("%H:%M:%S"),
        "emp_id":              emp_id,
        "name":                name,
        "email":               email,
        "department":          dept,
        "job_title":           title,
        "service_type":        service_type,
        "files_uploaded":      files_uploaded,
        "inserts_count":       inserts_count,
        "inserts_completed":   inserts_completed,
        "llm_calls":           llm_calls,
        "files_completed":     files_completed,
        "files_failed":        files_failed,
        "run_outcome":         run_outcome,
        "processing_time_sec": round(processing_time_sec, 1),
        "region":              region,
        "product":             product,
        "session_id":          session_id,
        "run_number_today":    run_number_today,
    }


# ---------------------------------------------------------------------------
# User lookup / registration
# ---------------------------------------------------------------------------
def _user_row_to_profile(row: dict) -> dict:
    return {
        "emp_id": str(row.get("emp_id", "")).strip(),
        "name":   row.get("name", ""),
        "email":  row.get("email", ""),
        "dept":   row.get("department", ""),
        "title":  row.get("job_title", ""),
        "region": "",
    }


def lookup_user(emp_id: str) -> "dict | None":
    """Return user profile dict or None."""
    emp_id = str(emp_id).strip()
    if not emp_id:
        return None
    try:
        row = s3_utils.get_json(_user_key(emp_id))
    except Exception as exc:
        logger.warning("User lookup failed: %s", exc)
        return None
    return _user_row_to_profile(row) if row else None


def save_user(emp_id: str, name: str, email: str,
              dept: str = "", title: str = "", region: str = "") -> None:
    """Upsert a user profile (one atomic PUT -- no read-modify-write, so concurrent pods are safe)."""
    emp_id = str(emp_id).strip()
    if not emp_id:
        raise ValueError("Employee ID is required.")
    s3_utils.put_json(_user_key(emp_id), {
        "emp_id": emp_id, "name": name, "email": email, "department": dept, "job_title": title,
    })
    logger.info("User saved -- emp_id=%s", emp_id)


def update_user(emp_id: str, name: str, email: str,
                dept: str = "", title: str = "") -> None:
    """Update an existing user's profile."""
    save_user(emp_id, name.strip(), email.strip(), dept.strip(), title.strip())


def read_all_users() -> list:
    """
    Return all registered users as a list of dicts.
    Keys: emp_id, name, email, department, job_title.
    """
    keys = [o["key"] for o in s3_utils.list_objects(f"{s3_utils.S3_PREFIX_USERS}/") if o["key"].endswith(".json")]
    return s3_utils.read_many_json(keys)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def log_usage(
    emp_id: str,
    name: str,
    email: str,
    dept: str,
    title: str,
    service_type: str,
    files_uploaded: int,
    inserts_count: int,
    inserts_completed: int,
    llm_calls: int,
    files_completed: int,
    files_failed: int,
    run_outcome: str,
    processing_time_sec: float,
    region: str,
    product: str,
    session_id: str,
) -> None:
    """
    Record one usage row.

    Call this once per processing run, after the run completes.
    run_outcome should be one of: "success", "stopped", "error"
    Timestamps are recorded in IST (UTC+5:30).  Logging the same session_id twice on the
    same day replaces the earlier record instead of duplicating it.
    """
    now = datetime.now(_IST)
    day_prefix = f"{_usage_prefix(emp_id or 'unknown')}{now.strftime('%Y%m%d')}/"

    if session_id:
        key = f"{day_prefix}{s3_utils.safe_id(session_id)}.json"
    else:
        key = f"{day_prefix}{now.strftime('%H%M%S')}_{uuid.uuid4().hex[:6]}.json"

    existing = s3_utils.get_json(key)
    if existing and existing.get("run_number_today"):
        run_number = existing["run_number_today"]
    else:
        run_number = len(s3_utils.list_objects(day_prefix)) + 1

    row = _make_row(
        now, emp_id, name, email, dept, title, service_type,
        files_uploaded, inserts_count, inserts_completed, llm_calls,
        files_completed, files_failed, run_outcome,
        processing_time_sec, region, product, session_id,
        run_number,
    )
    s3_utils.put_json(key, row)

    logger.info(
        "Usage logged -- emp_id=%s  completed=%d  failed=%d"
        "  outcome=%s  run#%d today",
        emp_id, files_completed, files_failed, run_outcome, run_number,
    )


def read_usage_logs(emp_id: str = "", since_days: "int | None" = None) -> list:
    """
    Return usage rows as a list of dicts, newest first.
    Pass emp_id to read one user's rows only (leave blank for everyone's).
    Pass since_days to read only the last N days (keeps the "recent jobs" lookup cheap).
    """
    objs = s3_utils.list_objects(_usage_prefix(emp_id.strip() if emp_id else ""))
    if since_days is not None:
        cutoff = (datetime.now(_IST) - timedelta(days=since_days)).strftime("%Y%m%d")
        # key = usage/<emp>/<YYYYMMDD>/<file>.json
        objs = [o for o in objs if len(o["key"].split("/")) >= 4 and o["key"].split("/")[2] >= cutoff]
    rows = s3_utils.read_many_json([o["key"] for o in objs if o["key"].endswith(".json")])
    rows.sort(key=lambda r: str(r.get("timestamp", "")), reverse=True)
    return rows


def tracker_env_configured() -> bool:
    """True when the shared bucket is configured (always, unless S3_BUCKET_NAME was blanked)."""
    return bool(s3_utils.S3_BUCKET_NAME)
