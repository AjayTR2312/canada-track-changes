"""Resolver log: one row per run of the failed-revision resolver agent, and what the reviewer did
with the proposal (accepted / rejected + a one-click reason). This is the data behind the
acceptance rate: no survey needed, the reviewers' normal clicks are the feedback.

Stored in S3, one record per run (see shared_log.SharedLog), under feedback/<region id>/resolver/.
"""
import time

from src.shared_log import SharedLog

HEADERS = [
    "timestamp", "run_id", "session_id", "doc_name", "emp_id", "user_name",
    "change_type", "section", "status", "confidence", "kind", "steps", "model",
    "outcome", "reject_reason", "detail", "region",
]

_log = SharedLog(
    kind="resolver",
    headers=HEADERS,
    identity_fields=("run_id",),
)

REJECT_REASONS = ("wrong_paragraph", "wrong_tags", "text_changed", "not_needed", "other")


def _clean(row: dict) -> dict:
    out = {h: str(row.get(h, "") if row.get(h, "") is not None else "") for h in HEADERS}
    out["detail"] = out["detail"][:600]
    out["section"] = out["section"][:200]
    return out


def record_run(row: dict) -> dict:
    """Log a finished run. outcome starts as 'pending' for a proposal, 'none' otherwise."""
    outcome = "pending" if row.get("status") == "proposed" else "none"
    return _log.upsert(_clean({**row, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "outcome": outcome}))


def mark_outcome(run_id: str, outcome: str, reject_reason: str = "") -> bool:
    if outcome not in ("accepted", "rejected"):
        return False
    r = _log.get((run_id,))
    if not r:
        return False
    r["outcome"] = outcome
    r["reject_reason"] = reject_reason if outcome == "rejected" and reject_reason in REJECT_REASONS else ""
    _log.upsert(r)
    return True


def read_all() -> list:
    return _log.read_all()


def summarize(rows: list) -> dict:
    """Acceptance rate overall and by change type / confidence; reject reasons; give-up count."""
    def rate(items):
        decided = [r for r in items if r.get("outcome") in ("accepted", "rejected")]
        acc = sum(1 for r in decided if r["outcome"] == "accepted")
        return {"runs": len(items), "proposed": sum(1 for r in items if r.get("status") == "proposed"),
                "decided": len(decided), "accepted": acc, "rate": round(acc / len(decided), 3) if decided else None}

    def group(key):
        out = {}
        for r in rows:
            out.setdefault(r.get(key) or "(none)", []).append(r)
        return {k: rate(v) for k, v in sorted(out.items(), key=lambda kv: -len(kv[1]))}

    reasons = {}
    for r in rows:
        if r.get("outcome") == "rejected":
            reasons[r.get("reject_reason") or "(no reason)"] = reasons.get(r.get("reject_reason") or "(no reason)", 0) + 1
    return {
        "overall": rate(rows),
        "by_change_type": group("change_type"),
        "by_confidence": group("confidence"),
        "reject_reasons": reasons,
        "no_proposal": sum(1 for r in rows if r.get("status") != "proposed"),
        "failures": [{"change_type": r.get("change_type"), "status": r.get("status"), "detail": r.get("detail")}
                     for r in rows if r.get("status") != "proposed"][-15:],
    }
