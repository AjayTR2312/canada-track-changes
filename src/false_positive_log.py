"""False-positive log: validators flag a revision the checker wrongly called invalid, and
the developer later reads the log to see which false positives happen most often.

Storage (see shared_log.SharedLog):
  * ONE log per region, kept in S3 -- one record per flag under
    feedback/<region id>/false_positives/<session_id>/, so the whole team's flags land together.
  * a per-job copy -- `false_positives.xlsx` regenerated into the validation session's S3 folder,
    so the flags travel with that job's other validation files.

Rows are upserted on (session_id, revision_key, emp_id): clicking again just changes the
severity/note instead of duplicating, and a flag can be withdrawn.
"""
import io
import time
from collections import Counter, defaultdict

from src.shared_log import SharedLog

SEVERITIES = ("critical", "medium", "low")
_SEVERITY_RANK = {"critical": 3, "medium": 2, "low": 1}

HEADERS = [
    "timestamp", "session_id", "revision_key", "doc_name", "emp_id", "user_name",
    "severity", "revision_type", "revision_index", "paragraph_index",
    "revision_text", "previous_context", "validator_reason", "suggested_fix", "note", "region",
]

_log = SharedLog(
    kind="false_positives",
    headers=HEADERS,
    identity_fields=("session_id", "revision_key", "emp_id"),
    partition_field="session_id",
)


def _clean(row: dict) -> dict:
    out = {h: str(row.get(h, "") if row.get(h, "") is not None else "") for h in HEADERS}
    out["revision_text"] = out["revision_text"][:1000]
    out["previous_context"] = out["previous_context"][:300]
    out["note"] = out["note"][:500]
    if out["severity"] not in SEVERITIES:
        out["severity"] = "medium"
    return out


def upsert(row: dict) -> dict:
    """Insert or update one flag; returns the stored row."""
    return _log.upsert(_clean({**row, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}))


def remove(session_id: str, revision_key: str, emp_id: str) -> bool:
    return _log.remove((session_id, revision_key, emp_id))


def session_flags(session_id: str) -> list:
    return [r for r in _log.read_partition(session_id) if r.get("session_id") == session_id]


def read_all() -> list:
    """Every team member's flags."""
    return _log.read_all()


# ── per-job Excel ─────────────────────────────────────────────────────────────

def _style_header(ws) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill
    fill = PatternFill("solid", fgColor="1F3864")
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = fill
        c.alignment = Alignment(vertical="center")
    ws.freeze_panes = "A2"
    for col in ws.columns:
        width = max((len(str(c.value)) for c in col[:200] if c.value is not None), default=8)
        ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 60)


SESSION_WORKBOOK_NAME = "false_positives.xlsx"


def write_session_workbook(session_id: str) -> str | None:
    """Regenerate `false_positives.xlsx` in the validation session's S3 folder from this
    session's flags (removed again when the last flag is withdrawn). Returns the file name, or
    None when the session has no flags."""
    from openpyxl import Workbook
    from src import session_store
    rows = session_flags(session_id)
    if not rows:
        session_store.delete_file(session_id, SESSION_WORKBOOK_NAME)
        return None
    wb = Workbook()
    ws = wb.active
    ws.title = "False Positives"
    ws.append(HEADERS)
    for r in sorted(rows, key=lambda r: (-_SEVERITY_RANK.get(r["severity"], 0), r["timestamp"])):
        ws.append([r.get(h, "") for h in HEADERS])
    _style_header(ws)
    buf = io.BytesIO()
    wb.save(buf)
    session_store.put_file(session_id, SESSION_WORKBOOK_NAME, buf.getvalue())
    return SESSION_WORKBOOK_NAME


# ── developer export (all jobs) ───────────────────────────────────────────────

def normalize_reason(reason: str) -> str:
    """Group near-identical validator reasons: lower-case, collapse whitespace, cap length."""
    return " ".join((reason or "(no reason recorded)").lower().split())[:160]


def build_export_workbook(rows: list | None = None) -> bytes:
    """All flags + a Summary sheet ranking the most frequent false positives, so a
    developer sees which rules/reasons to fix first."""
    from openpyxl import Workbook
    rows = rows if rows is not None else read_all()
    wb = Workbook()

    ws = wb.active
    ws.title = "Summary"
    groups = defaultdict(list)
    for r in rows:
        groups[(r.get("revision_type", ""), normalize_reason(r.get("validator_reason", "")))].append(r)
    ws.append(["Rank", "Occurrences", "Worst severity", "Revision type", "Validator reason",
               "Distinct documents", "Distinct reviewers", "Example text", "Latest note"])
    ordered = sorted(
        groups.items(),
        key=lambda kv: (-max(_SEVERITY_RANK.get(r["severity"], 0) for r in kv[1]), -len(kv[1])),
    )
    for rank, ((rtype, reason), items) in enumerate(ordered, 1):
        worst = max(items, key=lambda r: _SEVERITY_RANK.get(r["severity"], 0))["severity"]
        latest = max(items, key=lambda r: r["timestamp"])
        ws.append([
            rank, len(items), worst, rtype, reason,
            len({r["session_id"] for r in items}), len({r["emp_id"] for r in items}),
            (items[0].get("revision_text") or "")[:200], latest.get("note", ""),
        ])
    _style_header(ws)

    sev = wb.create_sheet("By severity")
    sev.append(["Severity", "Count"])
    counts = Counter(r.get("severity", "medium") for r in rows)
    for s in SEVERITIES:
        sev.append([s, counts.get(s, 0)])
    _style_header(sev)

    allws = wb.create_sheet("All flags")
    allws.append(HEADERS)
    for r in sorted(rows, key=lambda r: r["timestamp"], reverse=True):
        allws.append([r.get(h, "") for h in HEADERS])
    _style_header(allws)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
