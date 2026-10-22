"""
Streamlit Frontend for the Canada PXML Track Changes Pipeline.
Connects to the FastAPI backend (pipeline_api.py) over HTTP; downloads via the API / S3.
Each user session is isolated via st.session_state. Sign-in + usage logging via src/.
"""
import os
import io
import json
import zipfile
import logging
from dotenv import load_dotenv
# Load the .env next to this script (independent of cwd) and override any
# stale/blank values already in the environment.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)
import streamlit as st
import requests
import time

from src.usage_tracker import log_usage, lookup_user, save_user, update_user, read_usage_logs

API_URL = os.getenv("API_URL", "http://localhost:8000")
REGION = "Canada"

# Service name used as service_type in usage logs
try:
    with open(os.path.join(os.path.dirname(__file__), "config.json"), encoding="utf-8") as _cf:
        SERVICE_NAME = json.load(_cf).get("service_name", "PXML Track Changes")
except Exception:
    SERVICE_NAME = "PXML Track Changes"

# Pipeline steps shown in the checklist (11 backend steps + validation as step 12)
STEPS = [
    "Track Changes Extraction", "Section Deletion", "Section Number Replacement",
    "Section Insertion & Edge Cases", "Paragraph-Level Inline Changes",
    "Full Paragraph Deletions", "Newline Paragraph Insertions",
    "New Footnote Insertions", "Failed Revisions Report",
    "Retry Failed Deletions", "Post Processing", "Validation Report",
]


def _log_run_once(job_id, outcome, product="", llm_calls=0):
    """Record one usage row per job (success/error). Safe no-op if already logged
    or if the logging backend is unavailable (falls back / fails silently)."""
    flag = f"_logged_{job_id}"
    if st.session_state.get(flag):
        return
    st.session_state[flag] = True
    try:
        _p = st.session_state.get("user_profile", {})
        _elapsed = time.time() - st.session_state.get(f"_job_start_{job_id}", time.time())
        log_usage(
            emp_id=_p.get("emp_id", os.getenv("USERNAME", "")),
            name=_p.get("name", ""),
            email=_p.get("email", ""),
            dept=_p.get("dept", ""),
            title="",
            service_type=SERVICE_NAME,
            files_uploaded=1,
            inserts_count=0,
            inserts_completed=0,
            llm_calls=int(llm_calls or 0),
            files_completed=1 if outcome == "success" else 0,
            files_failed=0 if outcome == "success" else 1,
            run_outcome=outcome,
            processing_time_sec=_elapsed,
            region=REGION,
            product=product,
            session_id=job_id,
        )
    except Exception as _log_err:
        logging.warning("Usage logging failed: %s", _log_err)


def _classify_uploads(files):
    """Bucket a list of uploaded files by extension. Returns (docx, pxml, pdf, zip)."""
    docx = pxml = pdf = zip_ = None
    for f in files or []:
        ext = os.path.splitext(f.name)[1].lower()
        if ext in (".docx", ".doc"):
            docx = f
        elif ext in (".pxml", ".xml"):
            pxml = f
        elif ext == ".pdf":
            pdf = f
        elif ext == ".zip":
            zip_ = f
    return docx, pxml, pdf, zip_


def _build_images_payload(images_zip, pdf_file):
    """Combine the optional images ZIP and a standalone PDF into a single ZIP to
    send as images_zip. The backend extracts this into revision_images, where the
    validation step looks for a PDF. Returns (filename, bytes) or None."""
    if not images_zip and not pdf_file:
        return None
    if images_zip and not pdf_file:
        return images_zip.name, images_zip.getvalue()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if images_zip:
            try:
                with zipfile.ZipFile(io.BytesIO(images_zip.getvalue())) as src:
                    for n in src.namelist():
                        zf.writestr(n, src.read(n))
            except zipfile.BadZipFile:
                pass
        if pdf_file:
            zf.writestr(pdf_file.name, pdf_file.getvalue())
    return (images_zip.name if images_zip else "images_with_pdf.zip"), buf.getvalue()


st.set_page_config(page_title="XML Track Changes - Canada", page_icon="📄", layout="wide")

# ─── Theme (also set in .streamlit/config.toml; runtime call is best-effort) ───
try:
    st._config.set_option("theme.base", "light")
    st._config.set_option("theme.backgroundColor", "#FFFFFF")
    st._config.set_option("theme.secondaryBackgroundColor", "#F0F2F6")
    st._config.set_option("theme.textColor", "#000000")
except Exception:
    pass

# ─── Custom CSS ───
st.markdown("""
    <style>
    :root {
        --primary-color: #0068C9;
        --background-color: #FFFFFF;
        --secondary-background-color: #F0F2F6;
        --text-color: #000000;
    }

    html, body, .stApp {
        background-color: white !important;
        color: #1a1a1a !important;
    }

    .stApp {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif !important;
    }

    h1 {
        color: #1a1a1a !important;
        font-weight: 600 !important;
        letter-spacing: -0.5px !important;
        font-size: 1.8rem !important;
    }

    h2 {
        color: #2c3e50 !important;
        font-weight: 500 !important;
        font-size: 1.1rem !important;
    }

    h3 {
        font-size: 1rem !important;
    }

    [data-testid="metric-container"] {
        background: linear-gradient(135deg, #f8f9fa 0%, #ffffff 100%) !important;
        border: 1px solid #dee2e6 !important;
        border-radius: 6px !important;
        padding: 0.5rem !important;
        box-shadow: 0 2px 4px rgba(0,0,0,0.05) !important;
    }

    [data-testid="metric-container"] > div:nth-child(1) {
        font-size: 0.7rem !important;
        color: #6c757d !important;
        font-weight: 600 !important;
    }

    [data-testid="metric-container"] [data-testid="stMetricValue"] {
        font-size: 0.9rem !important;
        color: #212529 !important;
        font-weight: 600 !important;
    }

    section[data-testid="stSidebar"] {
        background-color: #f8f9fa !important;
        border-right: 1px solid #dee2e6 !important;
    }

    [data-testid="stFileUploadDropzone"] {
        background-color: #ffffff !important;
        border: 2px dashed #ced4da !important;
        border-radius: 8px !important;
    }

    [data-testid="stFileUploadDropzone"]:hover {
        border-color: #0068C9 !important;
        background-color: #f8f9fa !important;
    }

    .stButton > button {
        background-color: #ffffff !important;
        color: #495057 !important;
        border: 1px solid #ced4da !important;
        border-radius: 4px !important;
        font-weight: 500 !important;
        padding: 0.5rem 1rem !important;
    }

    .stButton > button:hover {
        background-color: #f8f9fa !important;
        border-color: #adb5bd !important;
    }

    /* Primary (call-to-action) buttons — coral, like the reference apps */
    .stButton > button[kind="primary"],
    .stButton > button[data-testid="baseButton-primary"] {
        background-color: #FF4B4B !important;
        color: #ffffff !important;
        border: none !important;
    }
    .stButton > button[kind="primary"]:hover,
    .stButton > button[data-testid="baseButton-primary"]:hover {
        background-color: #e63e3e !important;
        border: none !important;
    }
    .stButton > button[kind="primary"]:disabled {
        background-color: #f1aeae !important;
        color: #ffffff !important;
    }

    .stDownloadButton > button {
        background-color: #28a745 !important;
        color: white !important;
        border: none !important;
    }

    .stDownloadButton > button:hover {
        background-color: #218838 !important;
    }

    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}

    /* Functional classes used by the Processing tab */
    .status-banner { border-radius: 8px; padding: 1rem 1.2rem; margin: 1rem 0; border-left: 4px solid #2E7D32; background: #f0f0f0; }
    .status-banner-running { border-left: 4px solid #F57C00; background: #f5f5f5; }
    .status-banner-failed { border-left: 4px solid #C62828; background: #f5f5f5; }
    .step-done { color: #2E7D32; }
    .step-active { color: #F57C00; font-weight: 600; }
    .step-pending { color: #9e9e9e; }
    </style>
    """, unsafe_allow_html=True)

# ─── Header ───
st.title("📄 XML Track Changes - Canada")
st.caption("Thomson Reuters XML Document Processing Platform · Canada")


# ─── User identity / auto-login ───
# Resolve the user profile once per session. emp_id defaults to the Windows
# USERNAME (TR machines); users live in the shared S3 bucket (src/usage_tracker.py), so the same
# profile is found no matter which pod serves the session.
if "user_profile" not in st.session_state:
    _emp_id = os.getenv("USERNAME", "")
    _profile = lookup_user(_emp_id) if _emp_id else None
    st.session_state.user_profile = _profile or {
        "emp_id": _emp_id, "name": "", "email": f"{_emp_id}@thomsonreuters.com" if _emp_id else "",
        "dept": "", "title": "", "region": "",
    }
    # Auto-login only when USERNAME resolved a fully-registered user
    st.session_state.user_logged_in = bool(_emp_id and _profile and _profile.get("name"))


# ─── Login Gate ───
def show_login_gate():
    """Sign-in / registration page shown until a session has been identified."""
    st.divider()
    _, col, _ = st.columns([1, 2, 1])
    with col:
        st.subheader("👤 Sign In")
        st.info("Enter your Employee ID to continue. New users can register below.")

        with st.form("login_gate_form"):
            _gate_emp_id = st.text_input(
                "Employee ID *", placeholder="e.g., 6121007",
                value=st.session_state.get("_gate_emp_id_prefill", ""),
            )
            _login_btn = st.form_submit_button("Sign In", width='stretch', type="primary")

        if _login_btn:
            _eid = _gate_emp_id.strip()
            if not _eid:
                st.error("Please enter your Employee ID.")
            else:
                _found = lookup_user(_eid)
                if _found and _found.get("name"):
                    st.session_state.user_profile = {**_found, "region": _found.get("region", "")}
                    st.session_state.user_logged_in = True
                    st.session_state.pop("_gate_emp_id_prefill", None)
                    st.rerun()
                else:
                    st.warning(f"Employee ID **{_eid}** not found. Please register below.")
                    st.session_state["_gate_emp_id_prefill"] = _eid

        st.divider()
        st.subheader("📝 New User Registration")
        _prefill = st.session_state.get("_gate_emp_id_prefill", "")
        with st.form("register_gate_form"):
            rg_emp_id = st.text_input("Employee ID *", value=_prefill, placeholder="e.g., 6121007")
            rg_name   = st.text_input("Full Name *")
            rg_email  = st.text_input("Email *", placeholder="e.g., firstname.lastname@thomsonreuters.com")
            rg_dept   = st.text_input("Department")
            _reg_btn  = st.form_submit_button("✅ Register & Sign In", width='stretch')

        if _reg_btn:
            if not rg_emp_id.strip() or not rg_name.strip() or not rg_email.strip():
                st.error("Employee ID, Full Name, and Email are required.")
            else:
                try:
                    save_user(rg_emp_id.strip(), rg_name.strip(), rg_email.strip(), rg_dept.strip(), "")
                except Exception as _reg_err:
                    logging.error("User registration failed: %s", _reg_err)
                    st.error("Registration could not be saved right now (storage unavailable). Please try again in a moment.")
                else:
                    st.session_state.user_profile = {
                        "emp_id": rg_emp_id.strip(), "name": rg_name.strip(),
                        "email": rg_email.strip(), "dept": rg_dept.strip(),
                        "title": "", "region": "",
                    }
                    st.session_state.user_logged_in = True
                    st.session_state.pop("_gate_emp_id_prefill", None)
                    st.rerun()


if not st.session_state.get("user_logged_in", False):
    show_login_gate()
    st.stop()


# ─── Sidebar: configuration + upload (top), identity (bottom) ───
with st.sidebar:
    st.markdown("### ⚙️ Configuration")
    st.divider()

    with st.expander("📤 Upload Files", expanded=True):
        uploaded = st.file_uploader(
            "Drop DOCX, PXML/XML, PDF & ZIP together",
            type=["docx", "doc", "pxml", "xml", "pdf", "zip"],
            accept_multiple_files=True,
            key="up_files",
            disabled="current_job" in st.session_state,
            help="DOCX (revision copy) and PXML (source XML) are required. "
                 "Images ZIP is optional; a PDF for superscripts must be inside that ZIP.",
        )
        docx_file, pxml_file, pdf_file, images_zip = _classify_uploads(uploaded)

        if uploaded:
            st.caption(
                f"Detected: **{1 if docx_file else 0}** DOCX · "
                f"**{1 if pxml_file else 0}** PXML · "
                f"**{1 if pdf_file else 0}** PDF · "
                f"**{1 if images_zip else 0}** ZIP"
            )

        if docx_file and pxml_file:
            _extra = []
            if images_zip:
                _extra.append("Images ZIP")
            if pdf_file:
                _extra.append("PDF")
            _suffix = f" (+ {', '.join(_extra)})" if _extra else ""
            st.success(f"✅ Ready — `{docx_file.name}` + `{pxml_file.name}`{_suffix}. "
                       "Click ▶️ Start in the Processing tab.")
        else:
            _missing = []
            if not docx_file:
                _missing.append("DOCX")
            if not pxml_file:
                _missing.append("PXML/XML")
            if uploaded:
                st.warning(f"⏳ Still need: {', '.join(_missing)}")
            else:
                st.info("Upload a DOCX + PXML (and optionally an Images ZIP) to begin.")

        if pdf_file:
            st.caption("ℹ️ PDF will be bundled into the images package "
                       "(used for superscript extraction + validation).")

    st.markdown(f"**Region:** {REGION}")
    product_type = st.selectbox("Product", ["looseleaf", "pamphlet"], index=0,
                                disabled="current_job" in st.session_state)

    # ── Identity (bottom of sidebar) ──
    st.divider()
    _p = st.session_state.get("user_profile", {})
    _name = _p.get("name") or _p.get("emp_id")
    if _name:
        st.caption(f"Signed in as **{_name}**")
    if st.button("🚪 Sign out", width='stretch', key="signout_sidebar"):
        for _k in ("user_logged_in", "user_profile"):
            st.session_state.pop(_k, None)
        st.rerun()
    st.markdown("<small style='color:#888;'>v3.1 · Canada · S3 + ExecutorPool</small>",
                unsafe_allow_html=True)


# ─── Fetch current job status once (shared across tabs) ───
job_id = st.session_state.get("current_job")
status = None
if job_id:
    try:
        _r = requests.get(f"{API_URL}/api/status/{job_id}", timeout=15)
        if _r.status_code == 200:
            status = _r.json()
    except (requests.ConnectionError, requests.Timeout):
        status = None


def _reset_job():
    for _k in list(st.session_state.keys()):
        if _k == "current_job" or _k.startswith(("_job_start_", "_logged_", "validation_")):
            st.session_state.pop(_k, None)
    st.session_state.pop("job_status", None)


# ─── Tabs ───
tab_proc, tab_dl, tab_val, tab_log, tab_acct = st.tabs(
    ["🎯 Processing", "📥 Downloads", "🔍 Validation", "📜 System Logs", "👤 Account & Usage"]
)

# ===========================================================================
# 🎯 PROCESSING
# ===========================================================================
with tab_proc:
    # ── Status banner ──
    if not job_id:
        st.markdown("<div class='status-banner'>🟢 <strong>Ready</strong> — "
                    "upload files in the sidebar and click Start.</div>", unsafe_allow_html=True)
    elif status and status["status"] == "running":
        st.markdown(
            f"<div class='status-banner status-banner-running'>⏳ <strong>Processing</strong> — "
            f"Step {status.get('current_step', 0)}/{status.get('total_steps', 11)}: "
            f"{status.get('current_step_name', '')}</div>", unsafe_allow_html=True)
    elif status and status["status"] == "completed":
        st.markdown("<div class='status-banner'>✅ <strong>Completed</strong></div>",
                    unsafe_allow_html=True)
    elif status and status["status"] == "failed":
        st.markdown(f"<div class='status-banner status-banner-failed'>❌ <strong>Failed</strong> — "
                    f"{status.get('error', 'Unknown error')}</div>", unsafe_allow_html=True)

    col_panel, col_metrics, col_cfg = st.columns([1.2, 2, 1], gap="large")

    # ── Control Panel ──
    with col_panel:
        with st.container(border=True):
            st.markdown("### 🎮 Control Panel")
            if not job_id:
                start_clicked = st.button(
                    "▶️ Start Processing", type="primary", width='stretch',
                    disabled=not (docx_file and pxml_file),
                )
                if start_clicked:
                    with st.spinner("Uploading files to server & S3..."):
                        files_payload = {
                            "docx_file": (docx_file.name, docx_file.getvalue(), "application/octet-stream"),
                            "pxml_file": (pxml_file.name, pxml_file.getvalue(), "application/octet-stream"),
                        }
                        _img = _build_images_payload(images_zip, pdf_file)
                        if _img is not None:
                            files_payload["images_zip"] = (_img[0], _img[1], "application/zip")
                        _user_email = st.session_state.get("user_profile", {}).get("email", "")
                        try:
                            resp = requests.post(
                                f"{API_URL}/api/process",
                                files=files_payload,
                                data={"product_type": product_type, "user_email": _user_email},
                            )
                        except requests.RequestException as e:
                            resp = None
                            st.error(f"❌ Could not reach the API: {e}")
                    if resp is not None and resp.status_code == 200:
                        _jid = resp.json()["job_id"]
                        st.session_state["current_job"] = _jid
                        st.session_state[f"_job_start_{_jid}"] = time.time()
                        st.rerun()
                    elif resp is not None:
                        st.error(f"❌ Upload failed: {resp.text}")
            else:
                st.markdown(f"<small>Job ID: <code>{job_id}</code></small>", unsafe_allow_html=True)
                _done = bool(status and status["status"] in ("completed", "failed"))
                if st.button("🔄 Process Another Document", width='stretch', disabled=not _done):
                    _reset_job()
                    st.rerun()

    # ── Live Processing Metrics (no AI Model) ──
    with col_metrics:
        with st.container(border=True):
            st.markdown("### 📈 Live Processing Metrics")
            m1, m2, m3, m4 = st.columns(4)
            _is_done = bool(status and status["status"] == "completed")
            _docs = "1 / 1" if _is_done else ("0 / 1" if job_id else "0 / 0")
            _step = f"{status.get('current_step', 0)}/{status.get('total_steps', 11)}" if status else "—"
            _pct = f"{status.get('progress_pct', 0)}%" if status else "0%"
            _llm = status.get("llm_calls", 0) if status else 0
            m1.metric("📄 Documents", _docs)
            m2.metric("🪜 Step", _step, help=status.get("current_step_name", "") if status else None)
            m3.metric("📊 Progress", _pct)
            m4.metric("📞 LLM Calls", _llm)

    # ── Configuration summary ──
    with col_cfg:
        with st.container(border=True):
            st.markdown("### 📋 Configuration")
            st.markdown(f"**Region:** {REGION}")
            st.markdown(f"**Product:** {product_type}")

    # ── Pipeline steps tracker ──
    st.markdown("")
    with st.container(border=True):
        cur = status.get("current_step", 0) if (status and status["status"] == "running") else (
            len(STEPS) if _is_done else 0)
        _hdr_l, _hdr_r = st.columns([3, 1])
        with _hdr_l:
            st.markdown("#### 📋 Pipeline Steps")
        with _hdr_r:
            st.markdown(
                f"<div style='text-align:right;color:#6c757d;font-weight:600;padding-top:0.4rem;'>"
                f"Step {min(cur, len(STEPS))} / {len(STEPS)}</div>", unsafe_allow_html=True)
        st.progress((status.get("progress_pct", 0) / 100) if status else 0)
        _grid = st.columns(2)
        for _idx, s in enumerate(STEPS):
            i = _idx + 1
            _col = _grid[_idx % 2]
            if i < cur or _is_done:
                _col.markdown(f"<span class='step-done'>✓ {i}. {s}</span>", unsafe_allow_html=True)
            elif i == cur:
                _col.markdown(f"<span class='step-active'>▸ {i}. {s}</span>", unsafe_allow_html=True)
            else:
                _col.markdown(f"<span class='step-pending'>○ {i}. {s}</span>", unsafe_allow_html=True)

    # ── Live log ──
    if job_id:
        try:
            log_resp = requests.get(f"{API_URL}/api/log/{job_id}", timeout=15)
            if log_resp.status_code == 200:
                with st.expander("📜 Live Log Output", expanded=False):
                    st.code(log_resp.json().get("log", "")[-5000:], language="text")
        except requests.RequestException:
            pass

    # ── Auto-run validation after the pipeline completes ──
    if status and status["status"] == "completed":
        if st.session_state.get("validation_ready") not in (True, "failed"):
            st.markdown("<div class='status-banner status-banner-running'>⏳ "
                        "<strong>Running Validation</strong> — Step 12/12</div>", unsafe_allow_html=True)
            time.sleep(2)
            with st.spinner("Running validation report (this may take a few minutes)..."):
                try:
                    val_resp = requests.post(f"{API_URL}/api/validate/{job_id}", timeout=None)
                    st.session_state["validation_ready"] = True if val_resp.status_code == 200 else "failed"
                    if val_resp.status_code != 200:
                        st.session_state["validation_error"] = val_resp.text
                except Exception as e:
                    st.session_state["validation_ready"] = "failed"
                    st.session_state["validation_error"] = str(e)

            _log_run_once(job_id, "success", product=product_type,
                          llm_calls=status.get("llm_calls", 0))
            st.rerun()
        else:
            st.success("✅ Processing complete. See the **Downloads** and **Validation** tabs.")
            _user_email = st.session_state.get("user_profile", {}).get("email", "")
            if _user_email:
                st.caption(f"📧 Results emailed to **{_user_email}**")

    # ── Failure logging ──
    if status and status["status"] == "failed":
        _log_run_once(job_id, "error", product=product_type,
                      llm_calls=status.get("llm_calls", 0))

    # ── Poll while running ──
    if status and status["status"] == "running":
        time.sleep(3)
        st.rerun()

# ===========================================================================
# 📥 DOWNLOADS
# ===========================================================================
with tab_dl:
    st.markdown("### 📦 Results")
    if not (status and status["status"] == "completed"):
        st.info("Downloads appear here once processing completes.")
    else:
        col_dl1, col_dl2 = st.columns(2, gap="medium")
        raw_name = os.path.splitext(status.get("file_name", "output"))[0]
        short_name = raw_name[:40]
        with col_dl1:
            try:
                dl_resp = requests.get(f"{API_URL}/api/download/{job_id}", timeout=60)
                if dl_resp.status_code == 200:
                    st.download_button(
                        "📥 Download Output ZIP", data=dl_resp.content,
                        file_name=f"{job_id}_{short_name}.zip", mime="application/zip",
                        width='stretch')
            except requests.RequestException as e:
                st.error(f"Could not fetch output: {e}")
        with col_dl2:
            if st.session_state.get("validation_ready") is True:
                try:
                    val_dl = requests.get(f"{API_URL}/api/download-validation/{job_id}", timeout=60)
                    if val_dl.status_code == 200:
                        st.download_button(
                            "📊 Download Validation Excel", data=val_dl.content,
                            file_name=f"ValidationReport_{short_name}.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            width='stretch')
                except requests.RequestException:
                    st.warning("Validation report not available yet.")
            else:
                st.caption("Validation report not ready — see the Validation tab.")

# ===========================================================================
# 🔍 VALIDATION
# ===========================================================================
with tab_val:
    st.markdown("### 🔍 Validation")
    if not (status and status["status"] == "completed"):
        st.info("Validation is available after processing completes.")
    else:
        _vr = st.session_state.get("validation_ready")
        if _vr is True:
            st.success("✅ Validation report generated.")
        elif _vr == "failed":
            st.error(f"⚠️ Validation failed: {st.session_state.get('validation_error', 'Unknown error')}")
        else:
            st.info("Validation has not been run yet.")

        if st.button("🔄 Run / Re-run Validation", width='stretch'):
            with st.spinner("Running validation report..."):
                try:
                    vr = requests.post(f"{API_URL}/api/validate/{job_id}", timeout=None)
                    st.session_state["validation_ready"] = True if vr.status_code == 200 else "failed"
                    if vr.status_code != 200:
                        st.session_state["validation_error"] = vr.text
                except Exception as e:
                    st.session_state["validation_ready"] = "failed"
                    st.session_state["validation_error"] = str(e)
            st.rerun()

        # Output file preview
        st.divider()
        st.markdown("#### 📄 Output Files")
        try:
            lr = requests.get(f"{API_URL}/api/list-outputs/{job_id}", timeout=15)
            if lr.status_code == 200:
                files = lr.json().get("files", [])
                if files:
                    st.write(files)
                else:
                    st.caption("No output files listed.")
        except requests.RequestException:
            st.caption("Could not list output files.")

# ===========================================================================
# 📜 SYSTEM LOGS
# ===========================================================================
with tab_log:
    st.markdown("### 📜 Complete Processing Log")
    if not job_id:
        st.info("📭 No log entries yet — start a job first.")
    else:
        try:
            lr = requests.get(f"{API_URL}/api/log/{job_id}", timeout=15)
            if lr.status_code == 200:
                st.code(lr.json().get("log", "")[-50000:] or "(empty)", language="log")
            else:
                st.warning("Log not available.")
        except requests.RequestException as e:
            st.warning(f"Could not fetch log: {e}")

# ===========================================================================
# 👤 ACCOUNT & USAGE  (no Job Title)
# ===========================================================================
with tab_acct:
    _emp_id = os.getenv("USERNAME", "")
    _profile = st.session_state.get("user_profile", {})
    _emp_id = _profile.get("emp_id") or _emp_id
    _registered = bool(_profile.get("name"))

    st.subheader("👤 User Profile")

    if not _registered:
        st.info("You are not registered yet. Fill in your details below to get started.")
        with st.form("register_form"):
            r_emp_id = st.text_input("Employee ID *", value=_emp_id or "", placeholder="e.g., 6121007")
            r_name  = st.text_input("Full Name *",  value=_profile.get("name", ""))
            r_email = st.text_input("Email *", value=_profile.get("email", f"{_emp_id}@thomsonreuters.com" if _emp_id else ""))
            r_dept  = st.text_input("Department",    value=_profile.get("dept", ""))
            submitted = st.form_submit_button("✅ Register", width='stretch')
            if submitted:
                if not r_emp_id.strip() or not r_name.strip() or not r_email.strip():
                    st.error("Employee ID, Full Name, and Email are required fields.")
                else:
                    try:
                        save_user(r_emp_id.strip(), r_name, r_email, r_dept, "")
                        st.session_state.user_profile = {
                            "emp_id": r_emp_id.strip(), "name": r_name, "email": r_email,
                            "dept": r_dept, "title": "", "region": "",
                        }
                        st.success(f"Registered successfully! Welcome, {r_name}.")
                        st.rerun()
                    except Exception as e:
                        logging.error("User registration failed: %s", e)
                        st.error("Registration could not be saved right now (storage unavailable). Please try again in a moment.")
    else:
        if st.session_state.get("edit_profile"):
            with st.form("edit_profile_form"):
                st.caption("Update your profile details:")
                e_name  = st.text_input("Full Name *",  value=_profile.get("name", ""))
                e_email = st.text_input("Email *",       value=_profile.get("email", ""))
                e_dept  = st.text_input("Department",    value=_profile.get("dept", ""))
                c1, c2 = st.columns(2)
                with c1:
                    save_btn = st.form_submit_button("💾 Save", width='stretch')
                with c2:
                    cancel_btn = st.form_submit_button("Cancel", width='stretch')
                if save_btn:
                    try:
                        update_user(_emp_id, e_name, e_email, e_dept, "")
                        st.session_state.user_profile = {
                            "emp_id": _emp_id, "name": e_name, "email": e_email,
                            "dept": e_dept, "title": "", "region": "",
                        }
                        st.session_state.edit_profile = False
                        st.success("Profile updated.")
                        st.rerun()
                    except Exception as ex:
                        logging.error("Profile update failed: %s", ex)
                        st.error("Profile update failed. Please try again later.")
                if cancel_btn:
                    st.session_state.edit_profile = False
                    st.rerun()
        else:
            col_p, col_btn = st.columns([3, 1])
            with col_p:
                st.markdown(f"""
                | Field | Value |
                |---|---|
                | **Employee ID** | {_profile.get('emp_id', _emp_id)} |
                | **Name** | {_profile.get('name', '—')} |
                | **Email** | {_profile.get('email', '—')} |
                | **Department** | {_profile.get('dept', '—')} |
                """)
            with col_btn:
                if st.button("✏️ Edit Profile", width='stretch'):
                    st.session_state.edit_profile = True
                    st.rerun()

    st.divider()

    # ── Usage Logs ──
    st.subheader("📊 Usage Logs")
    _logs_hdr_l, _logs_hdr_r = st.columns([4, 1])
    with _logs_hdr_l:
        _show_all = st.toggle("Show all users", value=False, key="logs_show_all",
                              help="View all users' logs")
    with _logs_hdr_r:
        _logs_refresh = st.button("🔄 Refresh", key="logs_refresh_btn", width='stretch')
    _filter_emp = "" if _show_all else _emp_id

    _logs_cache_key = f"_usage_logs_{_filter_emp}"
    if _logs_refresh or _logs_cache_key not in st.session_state:
        with st.spinner("Loading usage logs..."):
            try:
                st.session_state[_logs_cache_key] = read_usage_logs(emp_id=_filter_emp)
            except Exception as _le:
                st.session_state[_logs_cache_key] = []
                logging.warning("Could not load usage logs: %s", _le)
    _logs = st.session_state.get(_logs_cache_key, [])

    if not _logs:
        st.info("No usage logs found yet.")
    else:
        import pandas as pd
        _known_outcomes = {"success", "stopped", "error", ""}
        _logs = [r for r in _logs if str(r.get("run_outcome", "")).strip().lower() in _known_outcomes]
        _df = pd.DataFrame(_logs)
        _display_cols = [c for c in [
            "date", "time", "emp_id", "name", "service_type", "region", "product",
            "files_uploaded", "inserts_count", "inserts_completed", "llm_calls",
            "files_completed", "files_failed",
            "run_outcome", "processing_time_sec", "run_number_today",
        ] if c in _df.columns]
        _df = _df[_display_cols]

        fc1, fc2 = st.columns(2)
        with fc1:
            _date_filter = st.text_input("Filter by date (YYYY-MM-DD)", value="", key="log_date_filter")
        with fc2:
            _outcome_filter = st.selectbox("Filter by outcome", ["All", "success", "stopped", "error"], key="log_outcome_filter")
        if _date_filter:
            _df = _df[_df["date"].astype(str).str.startswith(_date_filter)]
        if _outcome_filter != "All" and "run_outcome" in _df.columns:
            _df = _df[_df["run_outcome"] == _outcome_filter]

        st.caption(f"{len(_df)} record(s) shown")
        st.dataframe(_df, width='stretch', hide_index=True)
        _csv_bytes = _df.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="⬇️ Download as CSV", data=_csv_bytes,
            file_name=f"usage_logs_{time.strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv", width='stretch')
