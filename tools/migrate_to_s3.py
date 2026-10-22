"""One-off migration: copy data that used to live on a machine (or in SharePoint) into the S3 bucket.

Run it ONCE from a checkout that still has the old local files, with AWS credentials that can write
to the bucket (your usual SSO/role/profile -- the app itself needs none):

    python tools/migrate_to_s3.py --dry-run          # show what would be copied
    python tools/migrate_to_s3.py                    # copy it

What it picks up (all optional; missing files are skipped):
    local_users.json                          -> users/<emp_id>.json
    usage_log.csv                             -> usage/<emp_id>/<date>/<session>.json
    data/<region>/false_positive_log.csv      -> feedback/<region>/false_positives/...
    data/<region>/correction_log.csv          -> feedback/<region>/corrections/...
    data/<region>/resolver_log.csv            -> feedback/<region>/resolver/...
    validation_sessions/<id>/*                -> validation_sessions/<id>/*  (+ the per-user index)
    pipeline_output/Failed_revisions.xlsx     -> draft_revisions/Failed_revisions.xlsx

SharePoint history: the app no longer talks to SharePoint, so download the CSVs from the site
(AI_Users_Log.csv, AI_Usage_Log.csv, AI_FalsePositive_Log_*.csv ...) and pass them in:

    python tools/migrate_to_s3.py --users-csv AI_Users_Log.csv --usage-csv AI_Usage_Log.csv ^
        --false-positive-csv AI_FalsePositive_Log_Canada.csv --region canada

It is safe to run twice: every record has a deterministic key, so a re-run overwrites with identical data.
"""
import argparse
import csv
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import s3_utils  # noqa: E402


def read_csv(path: Path) -> list:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


class Migrator:
    def __init__(self, dry: bool):
        self.dry = dry
        self.counts = {}

    def put_json(self, kind: str, key: str, obj) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        if not self.dry:
            s3_utils.put_json(key, obj)

    def put_file(self, kind: str, key: str, path: Path) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        if not self.dry:
            s3_utils.upload_file_to_s3(str(path), key)

    # ---- users -------------------------------------------------------------------
    def users_json(self, path: Path) -> None:
        for emp_id, u in json.loads(path.read_text(encoding="utf-8")).items():
            self.put_json("users", f"users/{s3_utils.safe_id(emp_id)}.json", {
                "emp_id": emp_id, "name": u.get("name", ""), "email": u.get("email", ""),
                "department": u.get("dept", ""), "job_title": u.get("title", "")})

    def users_csv(self, path: Path) -> None:
        for r in read_csv(path):
            emp_id = str(r.get("emp_id", "")).strip()
            if emp_id:
                self.put_json("users", f"users/{s3_utils.safe_id(emp_id)}.json", {
                    "emp_id": emp_id, "name": r.get("name", ""), "email": r.get("email", ""),
                    "department": r.get("department", ""), "job_title": r.get("job_title", "")})

    # ---- usage -------------------------------------------------------------------
    def usage_csv(self, path: Path) -> None:
        for r in read_csv(path):
            emp = s3_utils.safe_id(r.get("emp_id") or "unknown")
            day = (r.get("date") or str(r.get("timestamp", ""))[:10]).replace("-", "")[:8] or "00000000"
            sid = (r.get("session_id") or "").strip()
            name = (s3_utils.safe_id(sid) if sid else
                    hashlib.sha1(json.dumps(r, sort_keys=True).encode()).hexdigest()[:12])
            self.put_json("usage", f"usage/{emp}/{day}/{name}.json", dict(r))

    # ---- feedback logs -----------------------------------------------------------
    def feedback_csv(self, path: Path, region: str, kind: str, identity: tuple, partition: bool) -> None:
        os.environ["REGION_ID"] = region
        from src.shared_log import SharedLog
        log = SharedLog(kind=kind, headers=list(read_csv(path)[0].keys()) if read_csv(path) else [],
                        identity_fields=identity, partition_field=identity[0] if partition else None)
        for r in read_csv(path):
            self.counts[kind] = self.counts.get(kind, 0) + 1
            if not self.dry:
                log.upsert(r)

    # ---- validation sessions -----------------------------------------------------
    def sessions(self, folder: Path) -> None:
        for sdir in sorted(p for p in folder.iterdir() if p.is_dir()):
            sid = s3_utils.safe_id(sdir.name)
            meta_path = sdir / "_session.json"
            for f in sorted(sdir.iterdir()):
                if f.is_file() and f.name != "_session.json":
                    self.put_file("session files", f"validation_sessions/{sid}/{s3_utils.safe_filename(f.name)}", f)
            if meta_path.is_file():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                for k in ("sharepoint_folder_url", "sharepoint_uploads", "resumed_from_sharepoint"):
                    meta.pop(k, None)
                meta["storage_prefix"] = f"s3://{s3_utils.S3_BUCKET_NAME}/validation_sessions/{sid}/"
                self.put_json("sessions", f"validation_sessions/{sid}/_session.json", meta)
                self.put_json("session index",
                              f"validation_index/{s3_utils.safe_id(meta.get('emp_id', ''), '_unassigned')}/{sid}.json", meta)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="only count what would be copied")
    ap.add_argument("--region", default=os.getenv("REGION_ID", "canada"), help="region id for feedback logs (default: canada)")
    ap.add_argument("--users-csv", help="AI_Users_Log.csv exported from SharePoint")
    ap.add_argument("--usage-csv", action="append", default=[], help="AI_Usage_Log.csv exported from SharePoint (repeatable)")
    ap.add_argument("--false-positive-csv", help="false-positive log CSV (SharePoint export)")
    ap.add_argument("--correction-csv", help="corrections log CSV (SharePoint export)")
    ap.add_argument("--resolver-csv", help="resolver log CSV (SharePoint export)")
    a = ap.parse_args()

    ok, detail = s3_utils.check_storage(0)
    print(f"Target: {detail} {'(reachable)' if ok else '(NOT reachable)'}")
    if not ok and not a.dry_run:
        sys.exit("Cannot reach the bucket -- check your AWS credentials / region.")

    m = Migrator(a.dry_run)
    if (ROOT / "local_users.json").is_file():
        m.users_json(ROOT / "local_users.json")
    if a.users_csv:
        m.users_csv(Path(a.users_csv))
    for p in [ROOT / "usage_log.csv", *map(Path, a.usage_csv)]:
        if p.is_file():
            m.usage_csv(p)

    feedback = (("false_positives", "false_positive_log.csv", ("session_id", "revision_key", "emp_id"), True, a.false_positive_csv),
                ("corrections", "correction_log.csv", ("proposal_id",), False, a.correction_csv),
                ("resolver", "resolver_log.csv", ("run_id",), False, a.resolver_csv))
    for kind, local_name, identity, partition, explicit in feedback:
        for p in [ROOT / "data" / a.region / local_name, Path(explicit) if explicit else None]:
            if p and p.is_file():
                m.feedback_csv(p, a.region, kind, identity, partition)

    if (ROOT / "validation_sessions").is_dir():
        m.sessions(ROOT / "validation_sessions")
    draft = ROOT / "pipeline_output" / "Failed_revisions.xlsx"
    if draft.is_file():
        m.put_file("draft revisions", "draft_revisions/Failed_revisions.xlsx", draft)

    print(("Would copy" if a.dry_run else "Copied") + ":")
    for kind, n in m.counts.items():
        print(f"  {kind:16} {n}")
    if not m.counts:
        print("  nothing found")


if __name__ == "__main__":
    main()
