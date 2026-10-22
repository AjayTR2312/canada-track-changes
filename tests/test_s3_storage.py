"""Storage-layer tests: everything that used to live in a pod's memory or disk (or in SharePoint)
must now round-trip through S3 and be visible to a "second pod" (a fresh in-memory state)."""
import io
import json
import os
import time
import zipfile

import pytest


# ─────────────────────────────── s3_utils primitives ───────────────────────────────
def test_primitives_roundtrip(s3):
    s3.put_bytes("a/b/c.txt", b"hello")
    assert s3.get_bytes("a/b/c.txt") == b"hello"
    assert s3.get_bytes("a/b/missing.txt") is None
    assert s3.object_exists("a/b/c.txt") and not s3.object_exists("a/b/nope")
    info = s3.object_info("a/b/c.txt")
    assert info["size"] == 5 and info["last_modified"] > 0
    s3.put_json("a/j.json", {"x": 1})
    assert s3.get_json("a/j.json") == {"x": 1}
    assert s3.get_json("a/none.json", default="d") == "d"
    assert [o["key"] for o in s3.list_objects("a/")] == ["a/b/c.txt", "a/j.json"]
    assert s3.list_common_prefixes("a/") == ["b"]


def test_prefix_delete_does_not_touch_siblings(s3):
    s3.put_bytes("sessions/abc/f1", b"1")
    s3.put_bytes("sessions/abcd/f1", b"2")       # a different session whose id merely starts the same
    assert s3.delete_prefix("sessions/abc") == 1
    assert s3.get_bytes("sessions/abcd/f1") == b"2"


def test_copy_and_stream_with_range(s3):
    s3.put_bytes("x/data.bin", bytes(range(100)))
    s3.copy_prefix("x", "y/z")
    assert s3.get_bytes("y/z/data.bin") == bytes(range(100))
    chunks, info = s3.open_stream("x/data.bin", (10, 19))
    assert b"".join(chunks) == bytes(range(10, 20)) and info["size"] == 100
    assert s3.open_stream("x/none") is None


def test_directory_roundtrip_and_excludes(s3, tmp_path):
    d = tmp_path / "out"
    (d / "sub").mkdir(parents=True)
    (d / "a.txt").write_text("A")
    (d / "sub" / "b.txt").write_text("B")
    (d / "_cancel.flag").write_text("x")
    (d / "_job_status.json").write_text("{}")
    keys = s3.upload_job_outputs("job1", str(d))
    assert keys == ["outputs/job1/a.txt", "outputs/job1/sub/b.txt"]          # housekeeping files stay local
    dest = tmp_path / "back"
    s3.download_directory_from_s3("outputs/job1", str(dest))
    assert (dest / "sub" / "b.txt").read_text() == "B"
    assert [f["filename"] for f in s3.list_output_files("job1")] == ["a.txt", "sub/b.txt"]


def test_key_safety(s3):
    assert s3.safe_id("../../etc/passwd") == "_.._etc_passwd".strip(".") or "/" not in s3.safe_id("../../etc/passwd")
    assert "/" not in s3.safe_filename("..\\..\\evil.docx") and s3.safe_filename("a/b/c.docx") == "c.docx"
    assert s3.safe_filename("..") == "file"
    with pytest.raises(ValueError):
        s3.put_bytes("../escape", b"x")


def test_key_prefix_is_transparent(s3, monkeypatch):
    monkeypatch.setenv("S3_KEY_PREFIX", "canada")
    s3.put_bytes("users/1.json", b"{}")
    import boto3
    raw = boto3.client("s3", region_name="us-east-1")
    assert raw.list_objects_v2(Bucket="test-bucket")["Contents"][0]["Key"] == "canada/users/1.json"
    assert [o["key"] for o in s3.list_objects("users/")] == ["users/1.json"]


def test_local_dev_backend(tmp_path, monkeypatch):
    import s3_utils
    monkeypatch.setenv("S3_LOCAL_DIR", str(tmp_path / "fake"))
    s3_utils.put_bytes("a/b.txt", b"hi")
    assert s3_utils.get_bytes("a/b.txt") == b"hi"
    assert [o["key"] for o in s3_utils.list_objects("a/")] == ["a/b.txt"]
    chunks, _ = s3_utils.open_stream("a/b.txt", (1, 1))
    assert b"".join(chunks) == b"i"
    assert s3_utils.delete_prefix("a") == 1 and s3_utils.get_bytes("a/b.txt") is None
    with pytest.raises(ValueError):
        s3_utils.put_bytes("../../escape.txt", b"x")


# ─────────────────────────────── users + usage ───────────────────────────────
def test_users_and_usage(s3):
    from src import usage_tracker as ut, local_auth
    ut.save_user("123", "Ann", "ann@x.com", "Ops", "")
    assert ut.lookup_user("123")["name"] == "Ann"
    assert ut.lookup_user("nobody") is None
    ut.update_user("123", "Ann B", "ann@x.com", "Ops", "")
    assert ut.lookup_user("123")["name"] == "Ann B"
    assert local_auth.is_registered("123") and "123" in local_auth.get_all_emp_ids()
    with pytest.raises(ValueError):
        local_auth.register_user("123", "Dup", "d@x.com")
    assert local_auth.login_user("123")["email"] == "ann@x.com"

    for sid in ("s1", "s2"):
        ut.log_usage("123", "Ann", "ann@x.com", "Ops", "", "svc", 1, 0, 0, 5, 1, 0, "success", 12.3, "Canada", "looseleaf", sid)
    ut.log_usage("123", "Ann", "ann@x.com", "Ops", "", "svc", 1, 0, 0, 5, 1, 0, "error", 1.0, "Canada", "looseleaf", "s1")  # same session: replaces
    ut.log_usage("999", "Bob", "", "", "", "svc", 1, 0, 0, 0, 0, 1, "error", 1.0, "Canada", "pamphlet", "")
    rows = ut.read_usage_logs("123")
    assert sorted(r["session_id"] for r in rows) == ["s1", "s2"]
    assert next(r for r in rows if r["session_id"] == "s1")["run_outcome"] == "error"
    assert {r["run_number_today"] for r in rows} == {1, 2}
    assert len(ut.read_usage_logs("")) == 3
    assert len(ut.read_usage_logs("123", since_days=1)) == 2


# ─────────────────────────────── feedback logs ───────────────────────────────
def test_feedback_logs(s3):
    from src import false_positive_log as fp, correction_log as cl, resolver_log as rl
    fp.upsert({"session_id": "S", "revision_key": "k1", "emp_id": "1", "severity": "low", "validator_reason": "r"})
    fp.upsert({"session_id": "S", "revision_key": "k1", "emp_id": "1", "severity": "critical"})   # same identity: update
    fp.upsert({"session_id": "S", "revision_key": "k2", "emp_id": "1"})
    fp.upsert({"session_id": "OTHER", "revision_key": "k1", "emp_id": "1"})
    assert {r["revision_key"]: r["severity"] for r in fp.session_flags("S")} == {"k1": "critical", "k2": "medium"}
    assert len(fp.read_all()) == 3
    assert fp.remove("S", "k2", "1") is True and fp.remove("S", "k2", "1") is False
    assert len(fp.session_flags("S")) == 1
    assert isinstance(fp.build_export_workbook(), bytes)

    cl.record_proposal({"proposal_id": "p1", "session_id": "S"})
    assert cl.mark_applied("p1") and not cl.mark_applied("nope")
    assert cl.read_all()[0]["applied"] == "yes"

    rl.record_run({"run_id": "r1", "status": "proposed"})
    assert rl.mark_outcome("r1", "rejected", "wrong_tags") and not rl.mark_outcome("r1", "bogus")
    row = rl.read_all()[0]
    assert row["outcome"] == "rejected" and row["reject_reason"] == "wrong_tags"
    assert rl.summarize(rl.read_all())["overall"]["decided"] == 1


def test_triage_report_storage(s3):
    from canada_pipeline import triage_agent as ta
    assert ta.latest_report() is None
    ta._save({"summary": "s", "findings": [], "clusters": [], "stats": {"false_positives": 0, "corrections": 0, "clusters": 0}})
    assert ta.latest_report()["summary"] == "s"


# ─────────────────────────────── validation sessions ───────────────────────────────
def test_session_store(s3):
    from src import session_store as ss, false_positive_log as fp
    sid = ss.new_session_id()
    meta = ss.new_meta(sid, "123", "Doc", docx_filename="a.docx", pxml_filename="a.pxml")
    ss.save_meta(meta)
    ss.put_file(sid, "a.docx", b"DOCX")
    ss.put_file(sid, "a.pxml", b"<x/>")
    assert ss.exists(sid) and ss.load_meta(sid)["emp_id"] == "123"
    assert sorted(ss.list_files(sid)) == ["a.docx", "a.pxml"]          # the progress record is not listed
    assert [m["session_id"] for m in ss.list_sessions("123")] == [sid]
    assert ss.list_sessions("someone-else") == []
    with pytest.raises(ValueError):
        ss.put_file(sid, "_session.json", b"x")
    # per-session false-positive workbook goes to the session folder
    fp.upsert({"session_id": sid, "revision_key": "k", "emp_id": "1"})
    assert fp.write_session_workbook(sid) == "false_positives.xlsx" and ss.file_exists(sid, "false_positives.xlsx")
    fp.remove(sid, "k", "1")
    assert fp.write_session_workbook(sid) is None and not ss.file_exists(sid, "false_positives.xlsx")
    assert ss.delete_session(sid) and not ss.exists(sid) and ss.list_sessions("123") == []


# ─────────────────────────────── job / batch registry ───────────────────────────────
def _fresh_pod():
    """Simulate a different pod: no local cache at all."""
    from src import job_store
    job_store.jobs._local.clear()
    job_store.batches._local.clear()


def test_job_registry_write_through_and_eviction(s3):
    from src import job_store as js
    rec = {"job_id": "j1", "status": "running", "emp_id": "7", "started_at": time.time(), "_future": object()}
    js.jobs["j1"] = rec
    assert js.jobs.owned("j1") and "_future" not in s3.get_json("jobs/j1/job.json")      # process-local objects never persisted
    _fresh_pod()                                                                          # another pod sees it
    assert js.jobs.get("j1")["status"] == "running" and not js.jobs.owned("j1")

    js.jobs.adopt(js.jobs.get("j1"))
    live = js.jobs["j1"]
    live["status"] = "completed"
    js.jobs.save(live)                                                                    # terminal -> dropped from the local cache
    assert not js.jobs.owned("j1") and js.jobs["j1"]["status"] == "completed"
    assert [j["job_id"] for j in js.jobs.list_for_user("7")] == ["j1"]
    assert js.jobs.list_for_user("8") == []
    assert js.jobs.get("missing") is None and "missing" not in js.jobs


def test_stale_running_job_is_marked_interrupted(s3, monkeypatch):
    from src import job_store as js
    s3.put_json("jobs/dead/job.json", {"job_id": "dead", "status": "running", "started_at": time.time() - 3600})
    s3.put_json("jobs/dead/heartbeat.json", {"at": time.time() - 3600})
    job = js.jobs.get("dead")
    assert job["status"] == "failed" and job["error_info"]["action"] == "rerun"
    assert s3.get_json("jobs/dead/job.json")["status"] == "failed"                        # and it was persisted
    # a job with a fresh heartbeat is left alone
    s3.put_json("jobs/alive/job.json", {"job_id": "alive", "status": "running", "started_at": time.time()})
    s3.put_json("jobs/alive/heartbeat.json", {"at": time.time()})
    assert js.jobs.get("alive")["status"] == "running"


def test_shutdown_marks_owned_running_work(s3):
    from src import job_store as js
    js.jobs["j2"] = {"job_id": "j2", "status": "running", "started_at": time.time()}
    js.batches["b2"] = {"batch_id": "b2", "status": "running", "items": [{"task_name": "t", "status": "running"}]}
    assert js.jobs.interrupt_owned_running("bye") == 1 and js.batches.interrupt_owned_running("bye") == 1
    _fresh_pod()
    assert js.jobs.get("j2")["status"] == "failed"
    b = js.batches.get("b2")
    assert b["status"] == "cancelled" and b["items"][0]["status"] == "failed"


def test_cancel_flag_and_status_are_shared(s3):
    from src import job_store as js
    assert not js.is_cancelled("j3")
    js.set_cancel("j3")
    assert js.is_cancelled("j3")
    js.write_status("j3", {"current_step": 4})
    assert js.read_status("j3")["current_step"] == 4
    js.clear_job_state("j3")
    assert not js.is_cancelled("j3") and js.read_status("j3") == {}


# ─────────────────────────────── batch S3 folder source ───────────────────────────────
def test_batch_s3_folder_source(s3, tmp_path):
    import batch_s3_io as b
    assert b.normalize_folder("s3://test-bucket/incoming/Pub/") == "incoming/Pub/"
    assert b.normalize_folder("\\incoming\\Pub") == "incoming/Pub/"
    for bad in ("", "../x", "jobs/anything", "users", "s3://other-bucket/incoming"):
        with pytest.raises(RuntimeError):
            b.normalize_folder(bad)
    s3.put_bytes("incoming/Pub/ch1/chapter1.docx", b"d")
    s3.put_bytes("incoming/Pub/ch1/chapter1.xml", b"x")
    s3.put_bytes("incoming/Pub/ch2/chapter1.xml", b"same name, other chapter")
    s3.put_bytes("incoming/Pub/notes.txt", b"ignored")
    s3.put_bytes("incoming/Pub/notes.pdf", b"ignored")
    got = b.download_s3_folder("incoming/Pub", str(tmp_path))
    assert sorted(got) == ["chapter1 (2).xml", "chapter1.docx", "chapter1.xml"]
