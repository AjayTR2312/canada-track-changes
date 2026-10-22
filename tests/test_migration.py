"""tools/migrate_to_s3.py: legacy local / SharePoint-export data lands where the app looks for it."""
import csv
import importlib.util
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_tool():
    spec = importlib.util.spec_from_file_location("migrate_to_s3", os.path.join(ROOT, "tools", "migrate_to_s3.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def test_migration_roundtrip(s3, tmp_path):
    from pathlib import Path
    from src import usage_tracker as ut, session_store as ss, false_positive_log as fp, correction_log as cl
    mig = _load_tool()
    m = mig.Migrator(dry=False)

    (tmp_path / "local_users.json").write_text(json.dumps({"1": {"emp_id": "1", "name": "Ann", "email": "a@x", "dept": "Ops", "title": ""}}))
    m.users_json(tmp_path / "local_users.json")
    assert ut.lookup_user("1")["name"] == "Ann"

    _write_csv(tmp_path / "usage.csv", [
        {"timestamp": "2026-01-02T03:04:05+05:30", "date": "2026-01-02", "emp_id": "1", "session_id": "s1", "run_outcome": "success"},
        {"timestamp": "2026-01-02T04:00:00+05:30", "date": "2026-01-02", "emp_id": "1", "session_id": "", "run_outcome": "error"}])
    m.usage_csv(tmp_path / "usage.csv")
    m.usage_csv(tmp_path / "usage.csv")                                   # re-run: same keys, no duplicates
    assert len(ut.read_usage_logs("1")) == 2

    _write_csv(tmp_path / "fp.csv", [{"timestamp": "t", "session_id": "S", "revision_key": "k", "emp_id": "1", "severity": "low"}])
    m.feedback_csv(tmp_path / "fp.csv", "canada", "false_positives", ("session_id", "revision_key", "emp_id"), True)
    assert fp.session_flags("S")[0]["severity"] == "low"
    _write_csv(tmp_path / "cl.csv", [{"proposal_id": "p9", "applied": "yes"}])
    m.feedback_csv(tmp_path / "cl.csv", "canada", "corrections", ("proposal_id",), False)
    assert any(r["proposal_id"] == "p9" for r in cl.read_all())

    sdir = tmp_path / "validation_sessions" / "abc123"
    sdir.mkdir(parents=True)
    (sdir / "a.docx").write_bytes(b"D")
    (sdir / "_session.json").write_text(json.dumps({"session_id": "abc123", "emp_id": "1", "sharepoint_folder_url": "x", "docx_filename": "a.docx"}))
    m.sessions(tmp_path / "validation_sessions")
    meta = ss.load_meta("abc123")
    assert "sharepoint_folder_url" not in meta and ss.get_file("abc123", "a.docx") == b"D"
    assert [x["session_id"] for x in ss.list_sessions("1")] == ["abc123"]


def test_dry_run_writes_nothing(s3, tmp_path):
    mig = _load_tool()
    (tmp_path / "local_users.json").write_text(json.dumps({"1": {"name": "Ann"}}))
    m = mig.Migrator(dry=True)
    m.users_json(tmp_path / "local_users.json")
    assert m.counts == {"users": 1} and s3.list_objects("users/") == []
