from __future__ import annotations

import os
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st

from excel_validation_report_generator import (
    DEFAULT_CLAUDE_MODEL,
    generate_validation_report_from_excel,
    generate_validation_report_from_enriched,
)

APP_DIR = Path(__file__).resolve().parent
DEFAULT_PROMPT_PATH = APP_DIR / "validation_prompts" / "prompt_validation.txt"
RUNTIME_ROOT = APP_DIR / "streamlit_runs" / "excel_validation"


def _load_config_defaults() -> tuple[str, str]:
    try:
        from config import API_KEY, MODEL_OPUS

        return API_KEY or "", MODEL_OPUS or DEFAULT_CLAUDE_MODEL
    except Exception:
        return "", DEFAULT_CLAUDE_MODEL


def _save_uploaded_file(uploaded_file: Any, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(uploaded_file.getbuffer())
    return destination


def _materialize_revision_assets(asset_files: list[Any], assets_dir: Path) -> str | None:
    if not asset_files:
        return None

    assets_dir.mkdir(parents=True, exist_ok=True)
    for uploaded in asset_files:
        target = assets_dir / uploaded.name
        suffix = target.suffix.lower()
        if suffix == ".zip":
            zip_path = _save_uploaded_file(uploaded, target)
            with zipfile.ZipFile(zip_path, "r") as archive:
                archive.extractall(assets_dir)
        else:
            _save_uploaded_file(uploaded, target)

    return str(assets_dir)


def _build_run_folder() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = RUNTIME_ROOT / timestamp
    counter = 1
    while run_dir.exists():
        counter += 1
        run_dir = RUNTIME_ROOT / f"{timestamp}_{counter}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _render_generated_files(output_dir: Path) -> None:
    st.markdown("#### Generated files")
    files = sorted(path for path in output_dir.rglob("*") if path.is_file())
    if not files:
        st.caption("No output files found yet.")
        return

    for file_path in files:
        relative = file_path.relative_to(output_dir)
        st.write(str(relative))


def main() -> None:
    st.set_page_config(page_title="Excel Validation Report", page_icon="📊", layout="wide")
    st.title("📊 Track Change Validation Report")
    st.caption(
        "Upload DOCX, post-vendor XML, prevendor XML, and revision assets to validate track changes."
    )

    default_api_key, default_model = _load_config_defaults()

    with st.sidebar:
        st.markdown("### Mode")
        use_enricher = st.toggle(
            "Use Enriched Mode (recommended)",
            value=True,
            help="Enriched mode uses dataframe_enricher for richer context — "
                 "full paragraph text, accurate neighbors, LLM-filled sections. "
                 "Gives much better page and chunk locating. "
                 "Disable to use legacy Excel-based mode.",
        )

        st.markdown("### Claude Defaults")
        st.write(f"Model from config: `{default_model}`")
        if default_api_key:
            st.success("API key available from config.py")
        else:
            st.warning("API key not resolved from config.py; runtime will fall back to environment/token endpoint.")

        st.markdown("### Output")
        use_temp_output = st.checkbox("Use auto-generated output folder", value=True)
        custom_output_dir = st.text_input(
            "Custom output directory",
            value=str(APP_DIR / "streamlit_runs" / "manual_output"),
            disabled=use_temp_output,
        )
        max_workers = st.number_input("Validation workers", min_value=1, max_value=10, value=5, step=1)

        if use_enricher:
            context_chars = st.number_input(
                "Context characters (prev/post)",
                min_value=10, max_value=200, value=50, step=10,
                help="Number of characters for prev_context and post_context from the enricher.",
            )

    left, right = st.columns([1.2, 1])

    with left:
        docx_file = st.file_uploader("DOCX file (with track changes)", type=["docx", "doc"])
        postvendor_file = st.file_uploader("Post-vendor XML/PXML", type=["pxml", "xml"])
        prevendor_file = st.file_uploader(
            "Prevendor XML/PXML" + (" (required)" if use_enricher else " (optional)"),
            type=["pxml", "xml"],
        )
        if not use_enricher:
            excel_file = st.file_uploader("Excel file (legacy mode)", type=["xlsx", "xls"])
        else:
            excel_file = None
        asset_files = st.file_uploader(
            "Revision images (required)",
            type=["zip", "pdf", "png", "jpg", "jpeg", "tiff", "bmp"],
            accept_multiple_files=True,
            help="Upload the ZIP containing page images (PNGs). Include the revision PDF for page-text matching.",
        )

    with right:
        if use_enricher:
            st.markdown("#### 🚀 Enriched Mode")
            st.info(
                "Enriched mode extracts track changes directly from the DOCX with full context:\n"
                "- Full paragraph text (best anchor for locating)\n"
                "- Accurate previous/next paragraphs\n"
                "- LLM-filled section numbers\n"
                "- Better page & PXML chunk matching"
            )
            st.markdown("#### Required inputs")
            st.write("- Revision DOCX")
            st.write("- Post-vendor XML/PXML")
            st.write("- Prevendor XML/PXML")
            st.write("- Revision images (ZIP with PNGs + PDF)")
        else:
            st.markdown("#### 📋 Legacy Excel Mode")
            st.markdown("#### Required inputs")
            st.write("- Excel workbook")
            st.write("- Revision DOCX")
            st.write("- Post-vendor XML/PXML")
            st.write("- Revision images (ZIP with PNGs)")
            st.markdown("#### Optional inputs")
            st.write("- Prevendor XML/PXML")

        st.divider()
        st.markdown("#### Validation prompt")
        if DEFAULT_PROMPT_PATH.exists():
            st.success(f"Using: `{DEFAULT_PROMPT_PATH}`")
        else:
            st.error(f"NOT FOUND: `{DEFAULT_PROMPT_PATH}` — validation will fail without it.")

    # Readiness check
    if use_enricher:
        ready = bool(docx_file and postvendor_file and prevendor_file and asset_files)
        if ready:
            st.success("Ready to run (Enriched Mode).")
        else:
            missing = []
            if not docx_file:
                missing.append("DOCX")
            if not postvendor_file:
                missing.append("Post-vendor XML")
            if not prevendor_file:
                missing.append("Prevendor XML")
            if not asset_files:
                missing.append("Revision images")
            st.info(f"Still need: {', '.join(missing)}")
    else:
        ready = bool(excel_file and docx_file and postvendor_file and asset_files)
        if ready:
            st.success("Ready to run (Legacy Excel Mode).")
        else:
            missing = []
            if not excel_file:
                missing.append("Excel")
            if not docx_file:
                missing.append("DOCX")
            if not postvendor_file:
                missing.append("Post-vendor XML")
            if not asset_files:
                missing.append("Revision images")
            st.info(f"Still need: {', '.join(missing)}")

    if st.button("▶️ Generate Validation Report", type="primary", disabled=not ready):
        run_dir = _build_run_folder() if use_temp_output else Path(custom_output_dir)
        uploads_dir = run_dir / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)

        docx_path = _save_uploaded_file(docx_file, uploads_dir / docx_file.name)
        postvendor_path = _save_uploaded_file(postvendor_file, uploads_dir / postvendor_file.name)
        prevendor_path = None
        if prevendor_file:
            prevendor_path = _save_uploaded_file(prevendor_file, uploads_dir / prevendor_file.name)

        images_folder = _materialize_revision_assets(asset_files, uploads_dir / "revision_assets")
        if not images_folder:
            st.error("Failed to extract revision images from the uploaded files.")
            st.stop()

        with st.spinner("Generating validation report. This may take a few minutes..."):
            try:
                if use_enricher:
                    report_path = generate_validation_report_from_enriched(
                        docx_path=str(docx_path),
                        prevendor_xml_path=str(prevendor_path),
                        postvendor_xml_path=str(postvendor_path),
                        output_dir=str(run_dir),
                        images_folder=images_folder,
                        claude_api_key=default_api_key or None,
                        claude_model=default_model,
                        validation_prompt_path=str(DEFAULT_PROMPT_PATH) if DEFAULT_PROMPT_PATH.exists() else None,
                        max_workers=int(max_workers),
                        context_chars=int(context_chars),
                    )
                else:
                    excel_path = _save_uploaded_file(excel_file, uploads_dir / excel_file.name)
                    report_path = generate_validation_report_from_excel(
                        excel_path=str(excel_path),
                        docx_path=str(docx_path),
                        postvendor_xml_path=str(postvendor_path),
                        output_dir=str(run_dir),
                        images_folder=images_folder,
                        prevendor_xml_path=str(prevendor_path) if prevendor_path else None,
                        claude_api_key=default_api_key or None,
                        claude_model=default_model,
                        validation_prompt_path=str(DEFAULT_PROMPT_PATH) if DEFAULT_PROMPT_PATH.exists() else None,
                        max_workers=int(max_workers),
                    )
                st.session_state["excel_validation_report_path"] = report_path
                st.session_state["excel_validation_output_dir"] = str(run_dir)
                st.success("Validation report generated successfully.")
            except Exception as exc:
                st.exception(exc)

    report_path = st.session_state.get("excel_validation_report_path")
    output_dir = st.session_state.get("excel_validation_output_dir")

    if report_path and os.path.exists(report_path):
        st.divider()
        st.markdown("### Download")
        with open(report_path, "rb") as handle:
            st.download_button(
                "📥 Download validation report",
                data=handle.read(),
                file_name=Path(report_path).name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        if output_dir:
            _render_generated_files(Path(output_dir))


if __name__ == "__main__":
    main()
