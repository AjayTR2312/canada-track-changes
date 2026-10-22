"""Correction log: every proposal the correction chat produced, what the reviewer asked for,
what the pipeline had produced, and whether the correction was applied. Each row is a pipeline
mistake with the right answer attached -- the best input the triage agent has.

Stored in S3, one record per proposal (see shared_log.SharedLog), under
feedback/<region id>/corrections/. One row per proposal, keyed by proposal_id; "applied" is
updated when the reviewer applies it.
"""
import time

from src.shared_log import SharedLog

HEADERS = [
    "timestamp", "proposal_id", "session_id", "doc_name", "emp_id", "user_name",
    "dtd_family", "parent_tag", "instruction", "original_xml", "proposed_xml",
    "explanation", "valid", "issues", "model", "applied", "region",
]

_log = SharedLog(
    kind="corrections",
    headers=HEADERS,
    identity_fields=("proposal_id",),
)


def _clean(row: dict) -> dict:
    out = {h: str(row.get(h, "") if row.get(h, "") is not None else "") for h in HEADERS}
    for k, cap in (("original_xml", 4000), ("proposed_xml", 4000), ("instruction", 800), ("explanation", 600), ("issues", 800)):
        out[k] = out[k][:cap]
    return out


def record_proposal(row: dict) -> dict:
    return _log.upsert(_clean({**row, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "applied": "no"}))


def mark_applied(proposal_id: str, applied: bool = True) -> bool:
    r = _log.get((proposal_id,))
    if not r:
        return False
    r["applied"] = "yes" if applied else "no"
    _log.upsert(r)
    return True


def read_all() -> list:
    return _log.read_all()
