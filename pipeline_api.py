"""
FastAPI backend for PXML Document Processor Pipeline.
Uses ProcessPoolExecutor (6 workers) for concurrency and S3 for ALL persistent state.

Stateless by design (Kubernetes-ready): a pod keeps only throw-away scratch files.  Job and batch
records, validation sessions, uploads/outputs, users, usage and feedback logs all live in the S3
bucket (see s3_utils.py for the key layout), so pods can be killed, rescheduled or scaled out
without losing anything that was stored.  (A pipeline run that is IN FLIGHT inside a killed pod
stops; it is reported as interrupted and can be re-run from its saved inputs.)
"""
import os
import sys
import time
import uuid
import shutil
import zipfile
import tempfile
import asyncio
from dotenv import load_dotenv
load_dotenv()
from concurrent.futures import ProcessPoolExecutor, Future
from pathlib import Path
from typing import Optional, Dict, List
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from starlette.background import BackgroundTask

import s3_utils
from s3_utils import (
    upload_file_to_s3, upload_job_inputs, upload_job_outputs,
    download_file_from_s3, download_directory_from_s3,
    generate_presigned_url, list_output_files,
    S3_PREFIX_UPLOADS, S3_PREFIX_OUTPUTS, S3_BUCKET_NAME,
)
from src import job_store, session_store
from src.job_store import jobs, batches   # S3-backed registries (dict-style access)
from canada_pipeline.region import get_region

_REGION = get_region()

# ─────────────────────────────────────────────
# App Setup
# ─────────────────────────────────────────────
app = FastAPI(title="PXML Document Processor API", version="3.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ─────────────────────────────────────────────
# ExecutorPool — workers for concurrent pipeline jobs (override via PIPELINE_MAX_WORKERS)
# ─────────────────────────────────────────────
MAX_WORKERS = int(os.getenv("PIPELINE_MAX_WORKERS", "30"))
executor = ProcessPoolExecutor(max_workers=MAX_WORKERS)

# ─────────────────────────────────────────────
# Job + batch records live in S3 (src/job_store.py): `jobs[...]` and `batches[...]` below read
# and write through to the bucket, so any pod can answer for any job.  After changing a record,
# call `_save_job(job)` / `_save_batch(batch)` to persist it.
# ─────────────────────────────────────────────
_JOBS_ROOT = os.path.join(tempfile.gettempdir(), "pxml_pipeline_jobs")      # pod-local scratch only


def _save_job(job: dict) -> None:
    jobs.save(job)


def _save_batch(batch: dict) -> None:
    batches.save(batch)


@app.on_event("shutdown")
async def _on_shutdown():
    """Pod is going away (rolling update / scale-down): say so for anything it was still running,
    instead of leaving those jobs 'running' forever."""
    reason = "The server was shut down while this was running."
    try:
        j = jobs.interrupt_owned_running(reason)
        b = batches.interrupt_owned_running(reason)
        if j or b:
            print(f"[SHUTDOWN] Marked {j} job(s) and {b} batch(es) as interrupted.")
    except Exception as exc:
        print(f"[SHUTDOWN] could not mark running work as interrupted: {exc}")

PIPELINE_STEP_NAMES = {
    1: "Track Changes Extraction",
    2: "Section Deletion",
    3: "Section Number Replacement",
    4: "Section Insertion & Edge Cases",
    5: "Paragraph-Level Inline Changes",
    6: "Full Paragraph Deletions",
    7: "Newline Paragraph Insertions",
    8: "New Footnote Insertions",
    9: "Failed Revisions Report",
    10: "Retry Failed Deletions",
    11: "Post Processing",
}


# ─────────────────────────────────────────────
# Cooperative cancellation support
# ─────────────────────────────────────────────
class PipelineCancelled(Exception):
    """Raised inside a worker process when the user requests a stop."""
    pass


def _job_id_from_dir(output_dir: str) -> str:
    """Job workspace layout is <scratch>/<job_id>/output, so the job id is the parent folder's name."""
    return os.path.basename(os.path.dirname(os.path.abspath(output_dir)))


def _cancel_flag_path(output_dir: str) -> str:
    return os.path.join(output_dir, "_cancel.flag")


def _request_cancel(output_dir: str):
    """Raise the cancel flag so the worker stops at the next step boundary.

    The flag lives in S3 (the pod that gets the 'stop' click is not necessarily the pod running the
    job) and also next to the job's scratch files for the local fast path."""
    try:
        with open(_cancel_flag_path(output_dir), 'w', encoding='utf-8') as f:
            f.write("cancel")
    except Exception:
        pass
    job_store.set_cancel(_job_id_from_dir(output_dir))


def _is_cancel_requested(output_dir: str) -> bool:
    return os.path.isfile(_cancel_flag_path(output_dir)) or job_store.is_cancelled(_job_id_from_dir(output_dir))


# ─────────────────────────────────────────────
# Status helper (worker process writes, API reads — via S3 so any pod can answer a poll)
# ─────────────────────────────────────────────
_last_log_push = {"at": 0.0}


def _push_job_log(output_dir: str, force: bool = False):
    """Copy the worker's running log to S3 (throttled) so /api/log works from any pod."""
    now = time.time()
    if not force and now - _last_log_push["at"] < 10:
        return
    _last_log_push["at"] = now
    log_path = os.path.join(output_dir, "pipeline_log.txt")
    if os.path.isfile(log_path):
        try:
            upload_file_to_s3(log_path, f"{S3_PREFIX_OUTPUTS}/{_job_id_from_dir(output_dir)}/pipeline_log.txt")
        except Exception:
            pass


def _write_job_status(output_dir: str, step: int, step_name: str, progress: int, status: str = "running", error: str = ""):
    """Publish job status (S3 + a local scratch copy) so the API can read live progress from any
    process on any pod.

    Also acts as a cooperative cancellation checkpoint: because this is called at
    the start of every pipeline step, a pending cancel request raises
    PipelineCancelled here, stopping the pipeline cleanly at the next step boundary.
    """
    import json
    # Cancellation checkpoint — only interrupt while the job is still running.
    if status == "running" and _is_cancel_requested(output_dir):
        raise PipelineCancelled()
    status_file = os.path.join(output_dir, "_job_status.json")
    data = {
        "current_step": step,
        "current_step_name": step_name,
        "progress_pct": progress,
        "status": status,
        "error": error,
        "updated_at": time.time(),
    }
    try:
        with open(status_file, 'w', encoding='utf-8') as f:
            json.dump(data, f)
    except:
        pass
    job_store.write_status(_job_id_from_dir(output_dir), data)
    _push_job_log(output_dir)


def _read_job_status(output_dir: str) -> dict:
    """Read live job status: the worker's local file if this pod runs it, else the copy in S3."""
    import json
    status_file = os.path.join(output_dir, "_job_status.json")
    if os.path.isfile(status_file):
        try:
            with open(status_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return job_store.read_status(_job_id_from_dir(output_dir))


def _upload_outputs_with_retry(job_id: str, output_dir: str, attempts: int = 4):
    """Upload a job's output folder to S3, retrying transient failures. Raises if it never works."""
    last = None
    for n in range(1, attempts + 1):
        try:
            return upload_job_outputs(job_id, output_dir)
        except Exception as exc:
            last = exc
            print(f"[S3] Output upload attempt {n}/{attempts} failed: {exc}")
            if n < attempts:
                time.sleep(2 * n)
    raise RuntimeError(f"Could not store the job outputs in S3: {last}")


# ─────────────────────────────────────────────
# Pipeline runner (runs in executor worker process)
# ─────────────────────────────────────────────
def _run_pipeline_job(job_id: str, docx_path: str, pxml_path: str, output_dir: str, product_type: str, images_dir: str = None, processing_mode: str = "chapter"):
    """Execute the full pipeline in a worker process. Uploads outputs to S3 on completion."""
    # Ensure project directory is in sys.path for imports
    project_dir = os.path.dirname(os.path.abspath(__file__))
    if project_dir not in sys.path:
        sys.path.insert(0, project_dir)
    os.chdir(project_dir)

    # Reset LLM call counter for this job (process-isolated)
    from canada_pipeline.llm_metering import install_global_metering, reset_global_meter, get_global_meter
    from canada_pipeline.config import reset_llm_calls, get_llm_calls
    install_global_metering()
    reset_global_meter()
    reset_llm_calls()

    from s3_utils import upload_job_outputs

    os.makedirs(output_dir, exist_ok=True)
    pxml_basename = os.path.splitext(os.path.basename(pxml_path))[0]
    timestamp = time.strftime('%Y%m%d_%H%M%S')
    output_file = os.path.join(output_dir, f"{pxml_basename}_{timestamp}_final.pxml")
    missing_revisions_output = os.path.join(output_dir, "missing_revisions.txt")
    log_file = os.path.join(output_dir, "pipeline_log.txt")
    failed_log_path = os.path.join(output_dir, "failed_items.txt")

    log_f = open(log_file, 'w', encoding='utf-8')

    class Tee:
        def __init__(self, *streams):
            self.streams = streams
        def write(self, data):
            for s in self.streams:
                try: s.write(data)
                except: pass
        def flush(self):
            for s in self.streams:
                try: s.flush()
                except: pass

    sys.stdout = Tee(sys.__stdout__, log_f)
    sys.stderr = Tee(sys.__stderr__, log_f)

    try:
        # Pipeline imports
        import canada_pipeline.continous_ncont_insertions as continous_ncont_insertions
        import canada_pipeline.newline_insertions as newline_insertions
        import canada_pipeline.insertions_paragraphs as insertions_paragraphs
        from canada_pipeline.section_name_edge_cases import (
            remaining_insertions_df, create_edgecase_section_dict,
            extract_sectionedgecase_key, update_designator_by_name_match
        )
        from canada_pipeline.chapter_section import extract_designator_pattern_using_llm
        from canada_pipeline.full_section_insertion_prevendor import PXMLInserter
        from canada_pipeline.inline_replacement_section import SectionLineReplacement
        from canada_pipeline.new_footnotes_insertions import process_all_footnote_insertions
        from canada_pipeline.section_insertion import SectionExtractor
        from canada_pipeline.track_changes_df import process_docx
        from canada_pipeline.section_deletion import section_deletion_filling
        from canada_pipeline.config import API_KEY, MODEL_OPUS, MODEL_SONNET, build_anthropic_client as _build_client_config
        from canada_pipeline.section_split_ import handle_section_split
        from canada_pipeline.postprocessing import postprocessing_fortags
        from canada_pipeline.paragraph_level_trackchanges import parse_pxml_sections_from_str_new, process_with_llm
        from canada_pipeline.full_deletion_handler import process_full_deletions
        from canada_pipeline.footnotes_level_trackchanges import process_with_llm_footnotes
        from canada_pipeline.inline_footnotes_insertion import (
            process_all_footnotes_with_llm, parse_pxml_sections_from_str_dict_positions, build_anthropic_client
        )
        from canada_pipeline.newline_insertions_new import return_closest_previous_par, process_all_newline_insertions_with_llm
        from canada_pipeline.sectionnumber_filling import fill_sections_backward_verbose
        from canada_pipeline.revisions_images import extract_page_texts
        from canada_pipeline.failed_revisions_handler import retry_failed_inline_deletions
        import pandas as pd
        import re
        from openpyxl.styles import Font, Alignment, PatternFill, Border, Side

        PRODUCT_TYPE = product_type
        start_time = time.time()
        _write_job_status(output_dir, 1, "Track Changes Extraction", 10)
        print(f"\n{'='*80}\nPipeline-1 started at: {time.strftime('%Y-%m-%d %H:%M:%S')}\n{'='*80}")

        results = process_docx(docx_path)
        extractor_obj = results['extractor']
        result_with_ba = extractor_obj.build_before_after_df()
        try:
            result_with_ba = extractor_obj.enrich_footnote_anchors()
        except RuntimeError as e:
            print(f"  [WARN] enrich_footnote_anchors skipped: {e}")

        no_changes_df = results['no_changes_df']
        df_remaining = results['df_remaining']
        result = results['result']
        df_changes = results['df_changes']
        chapter_number = results['chapter_number']
        print(f"\nChapter from DOCX: {chapter_number}")

        from canada_pipeline.superscript_extraction_from_pdf import convert_docx_to_pdf, process_excel as process_superscripts

        # ── Extract PDF from images ZIP (or convert via LibreOffice) ──
        # Also extract page_texts now before we delete the PDF
        _images_pdf_path = None
        _extracted_page_texts = None
        if images_dir and os.path.isdir(images_dir):
            _pdf_candidates = [f for f in os.listdir(images_dir) if f.lower().endswith('.pdf')]
            if _pdf_candidates:
                _images_pdf_path = os.path.join(images_dir, _pdf_candidates[0])
                print(f"[PDF] Found PDF in images ZIP: {_images_pdf_path}")
                # Extract page texts from PDF before we delete it
                try:
                    _extracted_page_texts = extract_page_texts(_images_pdf_path)
                    print(f"[PDF] Extracted page texts from PDF ({len(_extracted_page_texts)} pages)")
                except Exception as _e:
                    print(f"[PDF] WARNING: Could not extract page texts: {_e}")

        try:
            # Use PDF from images ZIP if available, else fall back to LibreOffice
            pdf_path_for_superscript = None
            if _images_pdf_path:
                pdf_path_for_superscript = _images_pdf_path
                print(f"[SUPERSCRIPT] Using PDF from images ZIP: {pdf_path_for_superscript}")
            else:
                pdf_path_for_superscript = convert_docx_to_pdf(docx_path, output_dir=output_dir)

            superscript_excel = os.path.join(output_dir, "superscript_extraction.xlsx")
            _, result_with_ba = process_superscripts(
                docx_path=docx_path, pdf_path=pdf_path_for_superscript,
                output_excel_path=superscript_excel, result_with_ba=result_with_ba
            )
        except Exception as e:
            print(f"[SUPERSCRIPT] WARNING: {e}")

        # ── Backfill text_before for footnote REPLACEMENTS misdetected as NEW footnotes ──
        # A footnote insertion into an EXISTING footnote sometimes arrives with an
        # empty text_before. Downstream that makes it fall into the NEW-footnote
        # bucket (is_footnote_x & has_changes & text_before==''), producing a bad
        # <footnote.reference>0</footnote.reference>. To fix it, copy text_before
        # from the closest previous EXISTING footnote row (has_changes==False) that
        # shares the same parent paragraph and superscript anchor.
        try:
            def _tb_empty(v):
                s = str(v).strip()
                return s == '' or s.lower() == 'nan'

            if {'is_footnote_x', 'has_changes', 'type', 'text_before',
                'parent_paragraph_idx_x', 'text_before_superscript',
                'last_word_before_superscript'}.issubset(result_with_ba.columns):

                _uni = result_with_ba.drop_duplicates(subset=['idx'], keep='first').sort_values('idx')

                _problem = _uni[
                    (_uni['is_footnote_x'] == True) &
                    (_uni['has_changes'] == True) &
                    (_uni['type'].astype(str) == 'inline_insertion') &
                    (_uni['text_before'].apply(_tb_empty))
                ]
                if 'is_new_footnote' in _problem.columns:
                    _problem = _problem[
                        (_problem['is_new_footnote'] == 0) | (_problem['is_new_footnote'] == False)
                    ]

                _backfilled = 0
                for _, _prow in _problem.iterrows():
                    _cur = _prow['idx']
                    _src = _uni[
                        (_uni['idx'] < _cur) &
                        (_uni['is_footnote_x'] == True) &
                        (_uni['has_changes'] == False) &
                        (_uni['parent_paragraph_idx_x'] == _prow['parent_paragraph_idx_x']) &
                        (_uni['text_before_superscript'].astype(str) == str(_prow['text_before_superscript'])) &
                        (_uni['last_word_before_superscript'].astype(str) == str(_prow['last_word_before_superscript']))
                    ].sort_values('idx', ascending=False)

                    if _src.empty:
                        print(f"  [FN-BACKFILL] idx={_cur}: no source footnote found — skipping.")
                        continue

                    # Prefer the closest source row whose text_before is non-empty.
                    _src_val = None
                    for _, _srow in _src.iterrows():
                        if not _tb_empty(_srow['text_before']):
                            _src_val = str(_srow['text_before'])
                            _src_idx = _srow['idx']
                            break
                    if _src_val is None:
                        print(f"  [FN-BACKFILL] idx={_cur}: source footnote text_before empty — skipping.")
                        continue

                    # Update ALL duplicate rows for this problem idx.
                    _mask = result_with_ba['idx'] == _cur
                    result_with_ba.loc[_mask, 'text_before'] = _src_val
                    _backfilled += int(_mask.sum())
                    print(f"  [FN-BACKFILL] idx={_cur} <- source idx={_src_idx} "
                          f"({int(_mask.sum())} row(s)): {_src_val[:70]!r}")

                if _backfilled:
                    print(f"[FN-BACKFILL] Backfilled text_before on {_backfilled} row(s).")
        except Exception as _e:
            print(f"[FN-BACKFILL] WARNING: {_e}")

        # Move the PDF from images folder to output dir (validation needs it later)
        if _images_pdf_path and os.path.isfile(_images_pdf_path):
            try:
                _pdf_dest = os.path.join(output_dir, os.path.basename(_images_pdf_path))
                shutil.move(_images_pdf_path, _pdf_dest)
                pdf_path_for_superscript = _pdf_dest  # Update reference to new location
                print(f"[PDF] Moved PDF to output dir for validation: {os.path.basename(_images_pdf_path)}")
            except Exception:
                pass

        inline_insertion_df = result[result['type'] == 'inline_insertion']
        with open(pxml_path, 'r', encoding='utf-8') as f:
            pxml_content = f.read()

        pxml_chapter_number, section_pattern, designator_patterns = extract_designator_pattern_using_llm(pxml_content)
        separator = ':'
        if designator_patterns:
            sep_match = re.search(r'[^0-9A-Za-z]', designator_patterns[0])
            if sep_match:
                separator = re.escape(sep_match.group())
        if chapter_number is None or (isinstance(chapter_number, str) and chapter_number.strip() == ''):
            chapter_number = pxml_chapter_number

        # ── VOLUME MODE ──
        # A *volume* contains many chapters, so section matching must NOT be
        # restricted to a single chapter number. Replace chapter_number with a
        # wildcard regex fragment that matches ANY chapter; this flows through
        # every downstream section-pattern unchanged. Chapter mode is untouched.
        if str(processing_mode).strip().lower() == "volume":
            from canada_pipeline.config import VOLUME_CHAPTER_WILDCARD
            chapter_number = VOLUME_CHAPTER_WILDCARD
            print(f"\n[MODE] Volume mode — chapter number ignored; using wildcard '{chapter_number}' for section matching")
        else:
            print(f"\n[MODE] Chapter mode — chapter number: {chapter_number}")

        # PIPELINE 2
        _write_job_status(output_dir, 2, "Section Deletion", 20)
        print(f"\n{'='*80}\nPIPELINE-2: Section Deletion\n{'='*80}")

        # ── Save intermediate dataframes for debugging ──
        try:
            result_with_ba.to_excel(os.path.join(output_dir, "debug_1_result_with_ba.xlsx"), index=False)
            print(f"[SAVE] debug_1_result_with_ba.xlsx ({len(result_with_ba)} rows)")
        except Exception as _e:
            print(f"[SAVE] WARNING: Could not save result_with_ba: {_e}")

        with open(pxml_path, 'r', encoding='utf-8') as f:
            pxml_content = f.read()
        updated_pxmlcontent, remaining_rows_inlinedeletion, rows_to_delete = \
            section_deletion_filling(result, df_changes, chapter_number, no_changes_df, df_remaining, pxml_content, verbose=True)

        # PIPELINE 3
        _write_job_status(output_dir, 3, "Section Number Replacement", 30)
        print(f"\n{'='*80}\nPIPELINE-3: Section (Number) Replacement\n{'='*80}")
        inlinereplacement_df = result_with_ba[(result_with_ba['type'] == 'inline_replacement') & (result_with_ba['is_footnote_x'] == False)]
        processor = SectionLineReplacement(inlinereplacement_df, API_KEY, output_file)
        processed_df = processor.process_dataframe(df_changes)
        updated_pxmlcontent, updated_inlinereplacement_df, replaced_indices = \
            processor.create_deletion_column(chapter_number, updated_pxmlcontent, missing_revisions_output)

        # PIPELINE 4
        _write_job_status(output_dir, 4, "Section Insertion & Edge Cases", 40)
        print(f"\n{'='*80}\nPIPELINE-4: Section Insertion & Edge Cases\n{'='*80}")
        sec_extractor = SectionExtractor(pxml_path)
        inline_insertion_df_without_footnotes = result_with_ba[
            (result_with_ba['type'] == 'inline_insertion') & (result_with_ba['is_footnote_x'] == False)
        ].drop_duplicates(subset=['idx'], keep='first')
        sections = sec_extractor.extract_inserted_sections(inline_insertion_df_without_footnotes, chapter_number)

        edgecases_in_section_insertion_df = remaining_insertions_df(inline_insertion_df_without_footnotes, sections)
        replacement_para_indices = set(result_with_ba[result_with_ba['type'] == 'inline_replacement']['idx'].tolist())
        edgecases_in_section_insertion_df = edgecases_in_section_insertion_df[
            ~edgecases_in_section_insertion_df['idx'].isin(replacement_para_indices)
        ]
        new_sections = create_edgecase_section_dict(edgecases_in_section_insertion_df, chapter_number, result_with_ba=result_with_ba)
        sections.extend(new_sections)

        new_sections = [s for s in new_sections if re.match(r'^(?:§\s*)?\d+:\d+', s['section_header'])]
        edgecase_processed_indexes = []
        if len(new_sections) == 1:
            for con in new_sections[0]['content']:
                if con.get('text') and re.match(r'^(?:§\s*)?\d+:\d+', con['text']):
                    section_number, section_text = extract_sectionedgecase_key(con['text'])
                    _content_before_desig = updated_pxmlcontent
                    updated_pxmlcontent = update_designator_by_name_match(updated_pxmlcontent, section_text, section_number)
                    # Only mark this idx as "processed" when the designator update
                    # actually changed the document. When update_designator_by_name_match
                    # finds no confident name match (score below threshold) it returns the
                    # content UNCHANGED — in that case this row is NOT a section-designator
                    # renumber but rather an inline heading-NAME edit (e.g. idx=709:
                    # "Portfolio Insurance" → "Portfolio Insurance and Mortgage Funding").
                    # Marking it processed here would wrongly exclude it from Pipeline 5,
                    # silently dropping the heading-name insertion. So only append on change.
                    if updated_pxmlcontent != _content_before_desig:
                        edgecase_processed_indexes.append(int(con['paragraph_index']))
                    else:
                        print(f"  [EDGE CASE] No designator change applied for '{con['text'][:60]}' "
                              f"(para_idx={con.get('paragraph_index')}) — leaving for Pipeline 5.")
                    break

        xml_context = sec_extractor.getting_contextxml()
        sections_insertion_indexes = list(edgecase_processed_indexes)

        pxml_chapter_number_p4, section_pattern_p4, designator_patterns_p4 = extract_designator_pattern_using_llm(updated_pxmlcontent)
        section_dict_p4 = parse_pxml_sections_from_str_new(updated_pxmlcontent, chapter_number)
        _client_p4 = _build_client_config()

        if len(sections) > 0:
            for each_section in sections:
                if each_section['start_df_index'] == each_section['end_df_index']:
                    continue
                header_desig = re.sub(r'^[§\s]+', '', str(each_section['section_header'])).strip()
                header_desig = header_desig.split()[0] if header_desig else ''
                prev_desig = None
                is_section_split = False
                previous_section_xml = None

                if header_desig and designator_patterns_p4:
                    keys_list_str = "\n".join(f"  - '{k}'" for k in designator_patterns_p4)
                    prompt = (
                        f"Given a NEW section designator '{header_desig}' that is being inserted,\n"
                        f"which existing designator from the list below would come immediately BEFORE it "
                        f"in logical/numerical order?\n\nAvailable designators (in order):\n{keys_list_str}\n\n"
                        f"Rules:\n- '14:11A' comes after '14:11' but before '14:12'\n"
                        f"- Reply with ONLY the key exactly as shown above, or 'NONE' if none comes before it."
                    )
                    try:
                        response = _client_p4.messages.create(model=MODEL_SONNET, max_tokens=30, temperature=0, messages=[{"role": "user", "content": prompt}])
                        answer = response.content[0].text.strip().strip("'\"")
                        if answer.upper() != "NONE" and answer in designator_patterns_p4:
                            prev_desig = answer
                    except:
                        pass

                if prev_desig and prev_desig in section_dict_p4:
                    previous_section_xml = section_dict_p4[prev_desig]
                    start_pi_check = int(each_section['start_paragraph_index'])
                    end_pi_check = int(each_section['end_paragraph_index'])
                    desig_pattern_re = re.compile(r'^§?\s*\d+' + separator + r'\d+', re.IGNORECASE)
                    rows_after = result_with_ba[(result_with_ba['idx'] > start_pi_check) & (result_with_ba['idx'] <= end_pi_check)].drop_duplicates(subset=['idx'], keep='first').sort_values('idx')
                    checked_rows = []
                    for _, row in rows_after.iterrows():
                        raw_text = row.get('text', '')
                        row_text = '' if pd.isna(raw_text) else str(raw_text).strip()
                        if row_text and desig_pattern_re.match(row_text):
                            break
                        has_change = bool(row.get('has_changes', False))
                        if not has_change and not row_text:
                            continue
                        checked_rows.append((row['idx'], has_change, row_text[:80]))

                    if checked_rows and not all(hc for _, hc, _ in checked_rows):
                        is_section_split = True
                        updated_pxmlcontent, split_success = handle_section_split(
                            each_section=each_section, previous_section_xml=previous_section_xml,
                            result_with_ba=result_with_ba, sec_extractor=sec_extractor,
                            xml_context=xml_context, updated_pxmlcontent=updated_pxmlcontent,
                            client=_client_p4, model_name=MODEL_OPUS, output_file=output_file,
                        )
                        if split_success:
                            start_pi = int(each_section['start_paragraph_index'])
                            end_pi = int(each_section['end_paragraph_index'])
                            sections_insertion_indexes.extend(range(start_pi, end_pi + 1))

                if not is_section_split:
                    start_pi = int(each_section['start_paragraph_index'])
                    end_pi = int(each_section['end_paragraph_index'])
                    sections_insertion_indexes.extend(range(start_pi, end_pi + 1))
                    plaintext_tobe_converted_xml = each_section['full_text']
                    bullet_points = each_section.get('bullet_points', [])
                    tagged_xml = sec_extractor.convert_text_to_xml_tags(
                        xml_context, plaintext_tobe_converted_xml,
                        bullet_points=bullet_points, section_header=each_section['section_header'],
                    )
                    cleanedup_xml, designator = sec_extractor.cleanup_xml_fences(tagged_xml, fallback_designator=each_section['section_header'])
                    if designator:
                        designator = designator.replace('§', '').strip()
                    insertionsection_obj = PXMLInserter(output_file, updated_pxmlcontent)
                    result_pxml, designator_num = insertionsection_obj.insert_xml_by_designator(designator, cleanedup_xml, write_to_file=False)
                    if result_pxml is not None:
                        updated_pxmlcontent = result_pxml

        # PIPELINE 5
        _write_job_status(output_dir, 5, "Paragraph-Level Inline Changes", 50)
        print("\n[PIPELINE 5] Paragraph-level inline changes...")
        inline_replacement_df = result[result['type'] == 'inline_replacement']
        inline_insertion_df = result[result['type'] == 'inline_insertion']
        inline_deletion_df = result[result['type'] == 'inline_deletion']

        pxml_chapter_number, section_pattern, designator_patterns = extract_designator_pattern_using_llm(updated_pxmlcontent)
        section_dict = parse_pxml_sections_from_str_new(updated_pxmlcontent, chapter_number)

        WORKSPACE_ID = os.getenv("TR_WORKSPACE_ID", "")
        MODEL_NAME = MODEL_OPUS
        client, model_name = build_anthropic_client(WORKSPACE_ID, MODEL_NAME)

        if 'text_before' in result_with_ba.columns:
            text_before_mask = result_with_ba['text_before'].notna()
            result_with_ba.loc[text_before_mask, 'text_before'] = (
                result_with_ba.loc[text_before_mask, 'text_before'].astype(str).str.replace(r'^\s*[\.,]+\s*$', '', regex=True)
            )

        result_with_ba = result_with_ba.drop_duplicates()
        already_processed = set(rows_to_delete['paragraph_index'].tolist()) | set(replaced_indices) | set(sections_insertion_indexes)
        result_with_ba_new = result_with_ba[~result_with_ba['idx'].isin(already_processed)]

        all_revisions_except_newline_insertions_new = result_with_ba_new
        result_with_ba_sorted = result_with_ba_new.sort_values('idx').reset_index(drop=True)
        valid_text = result_with_ba_sorted[
            result_with_ba_sorted['text_before'].notna() & (result_with_ba_sorted['text_before'] != '') & (result_with_ba_sorted['text_before'].str.len() > 5)
        ][['idx', 'text_before']].reset_index(drop=True)

        def get_previous_para(idx):
            candidates = valid_text[valid_text['idx'] < idx]
            return None if candidates.empty else candidates.iloc[-1]['text_before']
        def get_post_para(idx):
            candidates = valid_text[valid_text['idx'] > idx]
            return None if candidates.empty else candidates.iloc[0]['text_before']

        all_revisions_except_newline_insertions_new['previous_para'] = all_revisions_except_newline_insertions_new['idx'].apply(get_previous_para)
        all_revisions_except_newline_insertions_new['post_para'] = all_revisions_except_newline_insertions_new['idx'].apply(get_post_para)

        all_revisions_except_newline_insertions1 = all_revisions_except_newline_insertions_new[
            ((all_revisions_except_newline_insertions_new['text_before'] != '') | (all_revisions_except_newline_insertions_new['type'] == 'inline_deletion')) &
            (all_revisions_except_newline_insertions_new['has_changes'] == True) & (all_revisions_except_newline_insertions_new['is_footnote_x'] == False) &
            (all_revisions_except_newline_insertions_new['text_after'].fillna('').str.strip() != '')  # Exclude full deletions (handled in step 6)
        ]
        all_revisions_except_newline_insertions1 = fill_sections_backward_verbose(
            all_revisions_except_newline_insertions1, no_changes_df, inline_replacement_df, inline_insertion_df, chapter_number,
            verbose=True, client=client, model_name=model_name, designator_patterns=designator_patterns
        )

        # ── Save pipeline 5 input dataframe ──
        try:
            all_revisions_except_newline_insertions1.to_excel(os.path.join(output_dir, "debug_5_inline_changes.xlsx"), index=False)
            print(f"[SAVE] debug_5_inline_changes.xlsx ({len(all_revisions_except_newline_insertions1)} rows)")
        except Exception as _e:
            print(f"[SAVE] WARNING: Could not save inline changes df: {_e}")

        page_texts, image_paths = None, None
        try:
            if images_dir and os.path.isdir(images_dir):
                import json as _json
                page_texts_json = os.path.join(images_dir, "page_texts.json")
                if os.path.isfile(page_texts_json):
                    with open(page_texts_json, 'r', encoding='utf-8') as _f:
                        _raw = _json.load(_f)
                        # Convert dict {page_num: text} to list ordered by page number
                        _page_dict = {int(k): v for k, v in _raw.items()}
                        max_page = max(_page_dict.keys()) if _page_dict else 0
                        page_texts = [_page_dict.get(i, "") for i in range(1, max_page + 1)]
                elif _extracted_page_texts:
                    # Use page texts already extracted from PDF earlier (already a list)
                    page_texts = _extracted_page_texts
                    print(f"[PAGE IMAGES] Using pre-extracted page texts ({len(page_texts)} pages)")
                image_paths = {}
                for fname in sorted(os.listdir(images_dir)):
                    if fname.lower().endswith('.png'):
                        m = re.search(r'page_(\d+)\.png$', fname, re.IGNORECASE)
                        if m:
                            image_paths[int(m.group(1))] = os.path.join(images_dir, fname)
        except Exception as e:
            print(f"[PAGE IMAGES] WARNING: {e}")

        updated_pxmlcontent, processed_idxs_main, failed_inline_changes_main = process_with_llm(
            result_df=all_revisions_except_newline_insertions1, section_dict=section_dict,
            pxml_content=updated_pxmlcontent, client=client, failed_log_path=failed_log_path,
            match_threshold=0.5, model_name=model_name, chapter_number=chapter_number,
            page_texts=page_texts, image_paths=image_paths,
        )

        # Footnotes pass
        all_revisions_except_newline_insertions_footnotes = all_revisions_except_newline_insertions_new[
            (all_revisions_except_newline_insertions_new['text_before'] != '') &
            (all_revisions_except_newline_insertions_new['has_changes'] == True) &
            (all_revisions_except_newline_insertions_new['is_footnote_x'] == True)
        ]
        if 'parent_paragraph_idx_x' in all_revisions_except_newline_insertions_footnotes.columns:
            _parent_src = result_with_ba[['idx', 'type', 'text', 'text_before', 'text_after']].dropna(subset=['idx']).drop_duplicates(subset=['idx'], keep='first').copy()
            def _get_parent_text(row):
                rtype = str(row.get('type', '')).lower()
                if 'deletion' in rtype: return str(row.get('text_before', '') or '') if str(row.get('text_before', '') or '').strip() else ''
                elif 'insertion' in rtype: return str(row.get('text_after', '') or '') if str(row.get('text_after', '') or '').strip() else ''
                return str(row.get('text', '') or '') if str(row.get('text', '') or '').strip() else str(row.get('text_before', '') or '')
            _parent_src['_parent_text'] = _parent_src.apply(_get_parent_text, axis=1)
            parent_para_text_map = _parent_src.set_index('idx')['_parent_text']
            all_revisions_except_newline_insertions_footnotes['parent_paragraph_text'] = all_revisions_except_newline_insertions_footnotes['parent_paragraph_idx_x'].map(parent_para_text_map).fillna('')
        else:
            all_revisions_except_newline_insertions_footnotes['parent_paragraph_text'] = ''

        all_revisions_except_newline_insertions_footnotes = fill_sections_backward_verbose(
            all_revisions_except_newline_insertions_footnotes, no_changes_df, inline_replacement_df, inline_insertion_df, chapter_number,
            verbose=True, client=client, model_name=model_name, designator_patterns=designator_patterns
        )

        # ── Save footnotes input dataframe ──
        try:
            all_revisions_except_newline_insertions_footnotes.to_excel(os.path.join(output_dir, "debug_5b_footnotes.xlsx"), index=False)
            print(f"[SAVE] debug_5b_footnotes.xlsx ({len(all_revisions_except_newline_insertions_footnotes)} rows)")
        except Exception as _e:
            print(f"[SAVE] WARNING: Could not save footnotes df: {_e}")

        updated_pxmlcontent, processed_idxs_footnotes, failed_inline_changes_footnotes = process_with_llm_footnotes(
            result_df=all_revisions_except_newline_insertions_footnotes, section_dict=section_dict,
            pxml_content=updated_pxmlcontent, client=client, failed_log_path=failed_log_path,
            match_threshold=0.5, model_name=model_name, chapter_number=chapter_number,
            page_texts=page_texts, image_paths=image_paths,
        )

        # ── Footnote REFERENCE NUMBER renumbering (deterministic) ──────────────
        # When a new footnote is inserted mid-section, existing footnote reference
        # numbers shift (1→2, 2→3, ...). These are inline_replacement rows with
        # is_footnote_x==True and bare-digit text_before→text. The LLM footnote
        # pass can't handle a bare digit as footnote body, so apply them here by
        # wrapping the old number in <trace.deleted> and appending the new number,
        # scoped to each row's section.
        from canada_pipeline.footnote_reference_renumber import process_footnote_reference_renumbering
        updated_pxmlcontent, failed_footnote_renumbers = process_footnote_reference_renumbering(
            result_df_footnotes=all_revisions_except_newline_insertions_footnotes,
            pxml_content=updated_pxmlcontent,
            chapter_number=chapter_number,
            verbose=True,
        )

        failed_inline_changes = (failed_inline_changes_main or [])

        # PIPELINE 6: Full Paragraph Deletions
        _write_job_status(output_dir, 6, "Full Paragraph Deletions", 55)
        print("\n[PIPELINE 6] Full paragraph deletions...")
        section_dict = parse_pxml_sections_from_str_new(updated_pxmlcontent, chapter_number)
        # Full deletions: text_after is empty (entire paragraph deleted)
        # Include inline_deletion type even if text_before is empty/short (mirrors step 5 OR logic)
        full_deletion_df = all_revisions_except_newline_insertions_new[
            (
                (all_revisions_except_newline_insertions_new['text_before'] != '') |
                (all_revisions_except_newline_insertions_new['type'] == 'inline_deletion')
            ) &
            (all_revisions_except_newline_insertions_new['has_changes'] == True) &
            (all_revisions_except_newline_insertions_new['is_footnote_x'] == False) &
            (all_revisions_except_newline_insertions_new['text_after'].fillna('').str.strip() == '')
        ]
        if not full_deletion_df.empty:
            full_deletion_df = fill_sections_backward_verbose(
                full_deletion_df, no_changes_df, inline_replacement_df, inline_insertion_df, chapter_number,
                verbose=True, client=client, model_name=model_name, designator_patterns=designator_patterns
            )

            # ── Save full deletions input dataframe ──
            try:
                full_deletion_df.to_excel(os.path.join(output_dir, "debug_6_full_deletions.xlsx"), index=False)
                print(f"[SAVE] debug_6_full_deletions.xlsx ({len(full_deletion_df)} rows)")
            except Exception as _e:
                print(f"[SAVE] WARNING: Could not save full deletions df: {_e}")

            updated_pxmlcontent, processed_idxs_full_del, failed_full_deletions = process_full_deletions(
                result_df=full_deletion_df, section_dict=section_dict,
                pxml_content=updated_pxmlcontent, client=client,
                failed_log_path=os.path.join(output_dir, "failed_full_deletions.txt"),
                model_name=model_name, chapter_number=chapter_number,
            )
            failed_inline_changes = failed_inline_changes + (failed_full_deletions or [])
        else:
            print("  No full paragraph deletions found.")

        # PIPELINE 7
        _write_job_status(output_dir, 7, "Newline Paragraph Insertions", 60)
        print("\n[PIPELINE 7] Newline paragraph insertions...")
        all_newline_insertions = result_with_ba_new[
            (result_with_ba_new['text_before'] == '') & (result_with_ba_new['has_changes'] == True) & (result_with_ba_new['is_footnote_x'] == False)
        ].drop_duplicates(subset=['idx'], keep='first')
        all_newline_insertions = all_newline_insertions[['idx', 'text', 'has_changes', 'type', 'paragraph_index']]
        all_newline_insertions = return_closest_previous_par(result_with_ba, all_newline_insertions)
        all_newline_insertions_sections = fill_sections_backward_verbose(
            all_newline_insertions, no_changes_df, inline_replacement_df, inline_insertion_df, chapter_number,
            verbose=True, client=client, model_name=model_name, designator_patterns=designator_patterns
        )

        # ── Save newline insertions input dataframe ──
        try:
            all_newline_insertions_sections.to_excel(os.path.join(output_dir, "debug_7_newline_insertions.xlsx"), index=False)
            print(f"[SAVE] debug_7_newline_insertions.xlsx ({len(all_newline_insertions_sections)} rows)")
        except Exception as _e:
            print(f"[SAVE] WARNING: Could not save newline insertions df: {_e}")

        section_dict, section_positions, pxml_content = parse_pxml_sections_from_str_dict_positions(updated_pxmlcontent)
        updated_pxmlcontent, failed_newline_insertions = process_all_newline_insertions_with_llm(
            pxml_content=pxml_content, section_dict=section_dict, section_positions=section_positions,
            all_newline_insertions=all_newline_insertions_sections, client=client, model_name=model_name,
            failed_log_path=os.path.join(output_dir, "failed_newline_insertions.txt"),
            pdf_path=pdf_path_for_superscript if pdf_path_for_superscript else None,
        )

        # PIPELINE 8: New Footnote Insertions
        all_inserted_footnote_revisions = result_with_ba[
            (result_with_ba['is_footnote_x'] == True) & (result_with_ba['has_changes'] == True) & (result_with_ba['text_before'] == '')
        ].drop_duplicates(subset=['idx'], keep='first')
        # Deduplicate: same superscript at same location should only insert once.
        # Keep the row with the longest text (actual content), drop empty duplicates.
        if not all_inserted_footnote_revisions.empty and 'superscript' in all_inserted_footnote_revisions.columns:
            all_inserted_footnote_revisions = all_inserted_footnote_revisions.copy()
            all_inserted_footnote_revisions['_text_len'] = all_inserted_footnote_revisions['text'].fillna('').str.len()
            all_inserted_footnote_revisions = all_inserted_footnote_revisions.sort_values('_text_len', ascending=False)
            _dedup_cols = ['superscript', 'text_before_superscript']
            _dedup_cols = [c for c in _dedup_cols if c in all_inserted_footnote_revisions.columns]
            if _dedup_cols:
                _before_dedup = len(all_inserted_footnote_revisions)
                all_inserted_footnote_revisions = all_inserted_footnote_revisions.drop_duplicates(subset=_dedup_cols, keep='first')
                _after_dedup = len(all_inserted_footnote_revisions)
                if _before_dedup != _after_dedup:
                    print(f"  [DEDUP] Removed {_before_dedup - _after_dedup} duplicate footnote rows (same superscript+location)")
            all_inserted_footnote_revisions = all_inserted_footnote_revisions.drop(columns=['_text_len']).sort_values('idx')
        if 'parent_paragraph_idx_x' in all_inserted_footnote_revisions.columns:
            _parent_src2 = result_with_ba[['idx', 'type', 'text', 'text_before', 'text_after']].dropna(subset=['idx']).drop_duplicates(subset=['idx'], keep='first').copy()
            def _get_parent_text2(row):
                rtype = str(row.get('type', '')).lower()
                if 'deletion' in rtype: return str(row.get('text_before', '') or '') if str(row.get('text_before', '') or '').strip() else ''
                elif 'insertion' in rtype: return str(row.get('text_after', '') or '') if str(row.get('text_after', '') or '').strip() else ''
                return str(row.get('text', '') or '') if str(row.get('text', '') or '').strip() else str(row.get('text_before', '') or '')
            _parent_src2['_parent_text'] = _parent_src2.apply(_get_parent_text2, axis=1)
            parent_para_text_map = _parent_src2.set_index('idx')['_parent_text']
            all_inserted_footnote_revisions['full_para_where_footnote_is_present'] = all_inserted_footnote_revisions['parent_paragraph_idx_x'].map(parent_para_text_map).fillna('')
        else:
            all_inserted_footnote_revisions['full_para_where_footnote_is_present'] = ''

        section_dict, section_positions, pxml_content = parse_pxml_sections_from_str_dict_positions(updated_pxmlcontent)
        all_inserted_footnote_revisions_sections = fill_sections_backward_verbose(
            all_inserted_footnote_revisions, no_changes_df, inline_replacement_df, inline_insertion_df, chapter_number,
            verbose=True, client=client, model_name=model_name, designator_patterns=designator_patterns
        )

        # ── Save footnote insertions input dataframe ──
        try:
            all_inserted_footnote_revisions_sections.to_excel(os.path.join(output_dir, "debug_8_footnote_insertions.xlsx"), index=False)
            print(f"[SAVE] debug_8_footnote_insertions.xlsx ({len(all_inserted_footnote_revisions_sections)} rows)")
        except Exception as _e:
            print(f"[SAVE] WARNING: Could not save footnote insertions df: {_e}")

        updated_pxmlcontent, failed_footnote_insertions = process_all_footnote_insertions(
            all_inserted_footnote_revisions=all_inserted_footnote_revisions_sections,
            pxml_content=pxml_content, model_name=MODEL_OPUS,
            failed_log_path=os.path.join(output_dir, "failed_footnote_insertions.txt")
        )
        # Merge failed footnote replacements (from pipeline 5b) into footnote failures sheet
        failed_footnote_insertions = (failed_footnote_insertions or []) + (failed_inline_changes_footnotes or []) + (failed_footnote_renumbers or [])

        final_pxml_content = updated_pxmlcontent
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(final_pxml_content)

        # ── Structural changes: paragraph converted to list (bullet point) ──
        # When has_changes=True, has_bullet_point=True, text_before is present, text_after is present
        # → mark as failed with reason "Structural change - paragraph converted to list"
        if 'has_bullet_point' in result_with_ba.columns:
            bullet_structural_mask = (
                (result_with_ba['has_changes'] == True) &
                (result_with_ba['has_bullet_point'] == True) &
                (result_with_ba['text_before'].fillna('').str.strip() != '') &
                (result_with_ba['text_after'].fillna('').str.strip() != '')
            )
            bullet_structural_df = result_with_ba[bullet_structural_mask]
            if not bullet_structural_df.empty:
                print(f"  📌 Found {len(bullet_structural_df)} structural changes (paragraph → list)")
                for _, row in bullet_structural_df.iterrows():
                    failed_inline_changes.append({
                        "Row Index": row.get('idx', ''),
                        "Docx_Paragraph": str(row.get('text_before', '') or ''),
                        "Section": str(row.get('section', '') or ''),
                        "Designator": str(row.get('designator', '') or ''),
                        "Reason": "Structural change - paragraph converted to list",
                        "Change Type": str(row.get('type', '') or ''),
                        "Inserted Text": str(row.get('text_after', '') or ''),
                        "Deleted Text": str(row.get('text', '') or ''),
                        "Previous Context": str(row.get('prev_context', '') or ''),
                        "Post Context": str(row.get('post_context', '') or ''),
                    })

        # PIPELINE 9: Failed Revisions Report
        _write_job_status(output_dir, 9, "Failed Revisions Report", 80)
        print(f"\n{'='*80}\nWriting Consolidated Failed Revisions Excel...\n{'='*80}")
        consolidated_excel_path = os.path.join(output_dir, "Failed_revisions.xlsx")

        # ── Deduplicate by idx (Row Index) — keep first occurrence only ──
        if failed_inline_changes:
            _seen_idx = set()
            _deduped_inline = []
            for item in failed_inline_changes:
                _idx = item.get("Row Index", "")
                if _idx not in _seen_idx:
                    _seen_idx.add(_idx)
                    _deduped_inline.append(item)
                else:
                    print(f"  [DEDUP] Skipping duplicate Row Index={_idx} in failed_inline_changes")
            print(f"  [DEDUP] Inline failures: {len(failed_inline_changes)} → {len(_deduped_inline)} (removed {len(failed_inline_changes) - len(_deduped_inline)} duplicates)")
            failed_inline_changes = _deduped_inline

        if failed_newline_insertions:
            _seen_idx_nl = set()
            _deduped_newline = []
            for item in failed_newline_insertions:
                _idx = item.get("idx", item.get("Row Index", ""))
                if _idx not in _seen_idx_nl:
                    _seen_idx_nl.add(_idx)
                    _deduped_newline.append(item)
                else:
                    print(f"  [DEDUP] Skipping duplicate idx={_idx} in failed_newline_insertions")
            print(f"  [DEDUP] Newline failures: {len(failed_newline_insertions)} → {len(_deduped_newline)} (removed {len(failed_newline_insertions) - len(_deduped_newline)} duplicates)")
            failed_newline_insertions = _deduped_newline

        if failed_footnote_insertions:
            _seen_idx_fn = set()
            _deduped_footnote = []
            for item in failed_footnote_insertions:
                _idx = item.get("idx", item.get("Row Index", ""))
                if _idx not in _seen_idx_fn:
                    _seen_idx_fn.add(_idx)
                    _deduped_footnote.append(item)
                else:
                    print(f"  [DEDUP] Skipping duplicate idx={_idx} in failed_footnote_insertions")
            print(f"  [DEDUP] Footnote failures: {len(failed_footnote_insertions)} → {len(_deduped_footnote)} (removed {len(failed_footnote_insertions) - len(_deduped_footnote)} duplicates)")
            failed_footnote_insertions = _deduped_footnote

        has_inline_failures = len(failed_inline_changes) > 0
        has_newline_failures = len(failed_newline_insertions) > 0
        has_footnote_failures = len(failed_footnote_insertions) > 0

        if has_inline_failures or has_newline_failures or has_footnote_failures:
            with pd.ExcelWriter(consolidated_excel_path, engine='openpyxl') as writer:
                header_font = Font(bold=True, color='FFFFFF', size=11)
                header_fill = PatternFill(start_color='2E7D32', end_color='2E7D32', fill_type='solid')
                thin_border = Border(left=Side(style='thin'), right=Side(style='thin'), top=Side(style='thin'), bottom=Side(style='thin'))
                if has_inline_failures:
                    df_inline = pd.DataFrame(failed_inline_changes)
                    col_order = [c for c in ["Row Index","Docx_Paragraph","Section","Designator","Reason","Change Type","Inserted Text","Deleted Text","Previous Context","Post Context"] if c in df_inline.columns]
                    df_inline[col_order].to_excel(writer, index=False, sheet_name='Inline Changes')
                if has_newline_failures:
                    pd.DataFrame(failed_newline_insertions).to_excel(writer, index=False, sheet_name='Newline Insertions')
                if has_footnote_failures:
                    pd.DataFrame(failed_footnote_insertions).to_excel(writer, index=False, sheet_name='Footnote Insertions')

        # PIPELINE 10
        _write_job_status(output_dir, 10, "Retry Failed Deletions", 90)
        final_pxml_content, retry_success_count, retry_total, succeeded_indices, retry_rows_list = retry_failed_inline_deletions(
            failed_inline_changes=failed_inline_changes, pxml_content=final_pxml_content,
            client=client, model_name=model_name, chapter_number=chapter_number,
            page_texts=page_texts, image_paths=image_paths,
        )
        if retry_success_count > 0:
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(final_pxml_content)

        # PIPELINE 11
        _write_job_status(output_dir, 11, "Post Processing", 95)
        print(f"\n{'='*80}\nPIPELINE-10: Post Processing (Final)\n{'='*80}")
        _content_before_postprocessing = final_pxml_content
        with open(os.path.join(output_dir, 'save_before_postprocessing.pxml'), 'w', encoding='utf-8') as f:
            f.write(final_pxml_content)
        final_pxml_content = postprocessing_fortags(final_pxml_content, product_type=PRODUCT_TYPE)
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(final_pxml_content)

        # Content-loss guard (additive, never fatal): compare section labels and
        # tag balance before vs after post-processing; writes CONTENT_LOSS_REPORT.txt
        # and prints a loud banner if any section was silently dropped.
        try:
            from canada_pipeline.content_loss_guard import verify_no_content_loss
            verify_no_content_loss(
                _content_before_postprocessing, final_pxml_content,
                output_dir=output_dir, context=os.path.basename(output_file),
            )
        except Exception as _guard_err:
            print(f"  [CONTENT-GUARD] skipped (non-fatal): {_guard_err}")

        end_time = time.time()
        print(f"\n✅ Output saved to: {output_file}")
        print(f"Total runtime: {end_time - start_time:.2f} seconds")

        # Upload outputs to S3. Not optional: S3 is the only place outputs are served from, so a
        # run whose outputs cannot be stored is a failed run (retried first, to ride out blips).
        print(f"\n[S3] Uploading outputs to S3...")
        _upload_outputs_with_retry(job_id, output_dir)
        print(f"[S3] Upload complete.")

        # Use whichever counter has the higher count (meter or legacy)
        _llm_meter = get_global_meter().snapshot().call_count
        _llm_legacy = get_llm_calls()
        _llm = max(_llm_meter, _llm_legacy)
        print(f"[LLM] meter={_llm_meter} legacy={_llm_legacy} → using {_llm}")
        return {"status": "completed", "output_file": output_file, "job_id": job_id,
                "llm_calls": _llm}

    except PipelineCancelled:
        print(f"\n🛑 PIPELINE CANCELLED by user at step boundary.")
        _write_job_status(output_dir, 0, "Cancelled", 0, status="cancelled", error="Cancelled by user")
        try:
            upload_job_outputs(job_id, output_dir)
        except:
            pass
        return {"status": "cancelled", "job_id": job_id,
                "llm_calls": get_global_meter().snapshot().call_count}

    except Exception as e:
        print(f"\n❌ PIPELINE FAILED: {e}")
        try:
            upload_job_outputs(job_id, output_dir)
        except:
            pass
        return {"status": "failed", "error": str(e), "job_id": job_id,
                "llm_calls": get_global_meter().snapshot().call_count}

    finally:
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
        log_f.close()
        _push_job_log(output_dir, force=True)


# ─────────────────────────────────────────────
# Callback when executor future completes
# ─────────────────────────────────────────────
def _on_job_done(job_id: str, future: Future):
    try:
        result = future.result()
        if result and result.get("status") == "cancelled":
            jobs[job_id]["status"] = "cancelled"
            jobs[job_id]["current_step_name"] = "Cancelled by user"
            jobs[job_id]["error"] = ""
            jobs[job_id]["completed_at"] = time.time()
            jobs[job_id]["llm_calls"] = int(result.get("llm_calls", 0) or 0)
            _save_job(jobs[job_id])
            _log_job_usage(job_id, "cancelled")
            _cleanup_workspace(job_id)
            return
        if result and result.get("status") == "completed":
            # Set output_file and progress but keep status as "running"
            # until validation completes (so frontend keeps polling)
            jobs[job_id]["output_file"] = result.get("output_file", "")
            jobs[job_id]["progress_pct"] = 92
            jobs[job_id]["current_step"] = 11
            jobs[job_id]["current_step_name"] = "Post Processing Complete — Running Validation..."
            jobs[job_id]["completed_at"] = time.time()
            jobs[job_id]["llm_calls"] = int(result.get("llm_calls", 0) or 0)
            _save_job(jobs[job_id])
            # Do NOT set status to "completed" yet — wait for validation to finish
            # Auto-trigger validation (step 12)
            _auto_run_validation(job_id)
        else:
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["error"] = result.get("error", "Unknown") if result else "No result"
            jobs[job_id]["completed_at"] = time.time()
            _save_job(jobs[job_id])
            _log_job_usage(job_id, "error")
            _cleanup_workspace(job_id)
    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = str(e)
        jobs[job_id]["completed_at"] = time.time()
        _save_job(jobs[job_id])
        _log_job_usage(job_id, "error")
        _cleanup_workspace(job_id)


def _auto_run_validation(job_id: str):
    """Automatically submit validation as a background task after pipeline completes."""
    try:
        job = jobs.get(job_id, {})
        output_file = job.get("output_file", "")
        output_dir = job.get("output_dir", "")

        # Discover output file if not set
        if not output_file or not os.path.isfile(output_file):
            from pathlib import Path
            discovered = list(Path(output_dir).glob("*_final.pxml"))
            if discovered:
                output_file = str(max(discovered, key=lambda p: p.stat().st_mtime))
                jobs[job_id]["output_file"] = output_file

        if not output_file or not os.path.isfile(output_file):
            print(f"[AUTO-VALIDATION] Skipping for {job_id} — output file not found")
            # Mark completed anyway so job doesn't stay stuck as "running"
            jobs[job_id]["status"] = "completed"
            jobs[job_id]["progress_pct"] = 100
            jobs[job_id]["current_step_name"] = "Completed (validation skipped)"
            _save_job(jobs[job_id])
            _log_job_usage(job_id, "success", llm_calls=jobs[job_id].get("llm_calls", 0))
            _cleanup_workspace(job_id)
            return

        # Update step tracking
        jobs[job_id]["current_step"] = 12
        jobs[job_id]["current_step_name"] = "Validation Report"
        jobs[job_id]["progress_pct"] = 95
        _save_job(jobs[job_id])

        # Submit validation to executor
        val_future = executor.submit(_run_validation_sync, job_id, output_dir, output_file)
        val_future.add_done_callback(lambda f: _on_validation_done(job_id, f))
        print(f"[AUTO-VALIDATION] Submitted validation for job {job_id}")
    except Exception as e:
        print(f"[AUTO-VALIDATION] Failed to submit for {job_id}: {e}")
        # Mark completed anyway so job doesn't stay stuck as "running"
        jobs[job_id]["status"] = "completed"
        jobs[job_id]["progress_pct"] = 100
        jobs[job_id]["current_step_name"] = "Completed (validation error)"
        _save_job(jobs[job_id])
        _log_job_usage(job_id, "success", llm_calls=jobs[job_id].get("llm_calls", 0))
        _cleanup_workspace(job_id)


def _on_validation_done(job_id: str, future: Future):
    """Callback after validation completes. Only NOW mark job as completed."""
    try:
        excel_path = future.result()
        jobs[job_id]["current_step"] = 12
        jobs[job_id]["current_step_name"] = "Completed"
        jobs[job_id]["progress_pct"] = 100
        jobs[job_id]["status"] = "completed"  # NOW mark as completed after validation
        _save_job(jobs[job_id])
        _log_job_usage(job_id, "success", llm_calls=jobs[job_id].get("llm_calls", 0))
        print(f"[AUTO-VALIDATION] ✓ Validation complete for job {job_id}: {excel_path}")

        # Send completion email
        job = jobs.get(job_id, {})
        _job_email = job.get("user_email", "")
        if _job_email:
            try:
                from canada_pipeline.email_notifier import send_completion_email
                send_completion_email(
                    to_email=_job_email,
                    job_id=job_id,
                    final_xml_path=job.get("output_file", ""),
                    validation_report_path=excel_path,
                    product_type=job.get("product_type", ""),
                    file_name=job.get("file_name", ""),
                )
            except Exception as _email_err:
                print(f"[EMAIL] Failed to send completion email: {_email_err}")
        _cleanup_workspace(job_id)       # everything is in S3 now
    except Exception as e:
        # Validation failed but pipeline succeeded — still mark completed
        # so user can download pipeline output
        jobs[job_id]["current_step_name"] = "Validation Failed"
        jobs[job_id]["progress_pct"] = 100
        jobs[job_id]["status"] = "completed"
        _save_job(jobs[job_id])
        _log_job_usage(job_id, "success", llm_calls=jobs[job_id].get("llm_calls", 0))
        print(f"[AUTO-VALIDATION] ✗ Validation failed for job {job_id}: {e}")
        _cleanup_workspace(job_id)


def _log_job_usage(job_id: str, outcome: str, llm_calls: int = 0):
    """Log a completed/failed job to the S3 usage log. Safe no-op on failure."""
    try:
        from src.usage_tracker import log_usage
        import json as _json
        job = jobs.get(job_id, {})
        elapsed = (job.get("completed_at") or time.time()) - job.get("started_at", time.time())
        # Read service name from config.json
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"), encoding="utf-8") as _cf:
                service_name = _json.load(_cf).get("service_name", "PXML Track Changes")
        except Exception:
            service_name = "PXML Track Changes"
        # Use actual emp_id from job record
        user_email = job.get("user_email", "")
        emp_id = job.get("emp_id", "") or (user_email.split("@")[0] if user_email else "")
        user_name, user_dept = job.get("user_name", ""), ""
        try:
            from src.usage_tracker import lookup_user
            _u = lookup_user(emp_id) or {}
            user_name = user_name or _u.get("name", "")
            user_dept = _u.get("dept", "")
        except Exception:
            pass
        log_usage(
            emp_id=emp_id,
            name=user_name,
            email=user_email,
            dept=user_dept,
            title="",
            service_type=service_name,
            files_uploaded=1,
            inserts_count=0,
            inserts_completed=0,
            llm_calls=int(llm_calls or 0),
            files_completed=1 if outcome == "success" else 0,
            files_failed=0 if outcome == "success" else 1,
            run_outcome=outcome,
            processing_time_sec=elapsed,
            region="Canada",
            product=job.get("product_type", ""),
            session_id=job_id,
        )
    except Exception as e:
        print(f"[USAGE-LOG] Warning: Failed to log usage for job {job_id}: {e}")


import threading as _thr_mod


def _normalize_log_row(row: dict) -> dict:
    """Normalize usage-log field names and the ISO timestamp format (rows may come from older exports)."""
    import re as _re
    field_map = {
        "emp_id": ["emp_id", "Emp_ID", "EmpID", "employee_id"],
        "name": ["name", "Name", "full_name"],
        "email": ["email", "Email"],
        "department": ["department", "Department", "dept"],
        "service_type": ["service_type", "service", "Service", "ServiceType"],
        "region": ["region", "Region"],
        "product": ["product", "Product"],
        "run_outcome": ["run_outcome", "outcome", "Outcome", "RunOutcome"],
        "processing_time_sec": ["processing_time_sec", "duration", "Duration"],
        "llm_calls": ["llm_calls", "LLM_Calls", "llm_count"],
        "session_id": ["session_id", "Session_ID", "SessionID"],
        "timestamp": ["timestamp", "Timestamp"],
        "date": ["date", "Date"],
        "time": ["time", "Time"],
    }
    normalized = {}
    for canonical, aliases in field_map.items():
        for alias in aliases:
            if alias in row:
                normalized[canonical] = row[alias]
                break
    for k, v in row.items():
        if k not in normalized:
            normalized[k] = v
    ts = normalized.get("timestamp", "")
    if ts and "T" in ts:
        ts_clean = ts.replace("T", " ")
        ts_clean = _re.sub(r"[+-]\d{2}:\d{2}.*$", "", ts_clean)
        ts_clean = ts_clean.strip().split(".")[0]
        normalized["timestamp"] = ts_clean
        if " " in ts_clean:
            parts = ts_clean.split(" ")
            if not normalized.get("date"):
                normalized["date"] = parts[0]
            if not normalized.get("time"):
                normalized["time"] = parts[1]
    return normalized


# ─────────────────────────────────────────────
# Auth & User API Endpoints (for React frontend)
# ─────────────────────────────────────────────
from pydantic import BaseModel as _BaseModel

class _LoginRequest(_BaseModel):
    emp_id: str

class _RegisterRequest(_BaseModel):
    emp_id: str
    name: str
    email: str
    dept: str = ""

class _UpdateRequest(_BaseModel):
    emp_id: str
    name: str
    email: str
    dept: str = ""


@app.post("/api/auth/login")
async def auth_login(req: _LoginRequest):
    """Login by employee ID (users are stored in the shared S3 bucket, so every replica agrees)."""
    from src.usage_tracker import lookup_user
    profile = None
    try:
        profile = await asyncio.wait_for(asyncio.to_thread(lookup_user, req.emp_id.strip()), timeout=10.0)
    except Exception as exc:
        print(f"[AUTH] login lookup failed: {exc}")
    if not profile or not profile.get("name"):
        raise HTTPException(404, "Employee not found")
    return profile


@app.post("/api/auth/register")
async def auth_register(req: _RegisterRequest):
    """Register a new user."""
    from src.usage_tracker import save_user
    try:
        await asyncio.to_thread(save_user, req.emp_id.strip(), req.name.strip(), req.email.strip(), req.dept.strip(), "")
    except Exception as e:
        raise HTTPException(503, f"Registration failed: {e}")
    return {"emp_id": req.emp_id.strip(), "name": req.name.strip(), "email": req.email.strip(), "dept": req.dept.strip(), "title": ""}


@app.put("/api/auth/update")
async def auth_update(req: _UpdateRequest):
    """Update user profile."""
    from src.usage_tracker import update_user
    try:
        await asyncio.to_thread(update_user, req.emp_id.strip(), req.name.strip(), req.email.strip(), req.dept.strip(), "")
    except Exception as e:
        raise HTTPException(503, f"Update failed: {e}")
    return {"emp_id": req.emp_id.strip(), "name": req.name.strip(), "email": req.email.strip(), "dept": req.dept.strip(), "title": ""}


@app.get("/api/auth/usage-logs")
async def auth_usage_logs(emp_id: str = ""):
    """Return usage logs (one user's, or everyone's when emp_id is blank), newest first."""
    from src.usage_tracker import read_usage_logs
    try:
        rows = await asyncio.to_thread(read_usage_logs, emp_id.strip())
    except Exception as e:
        raise HTTPException(503, f"Could not read usage logs: {e}")
    rows = [_normalize_log_row(r) for r in rows]
    rows.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return rows


def _job_storage_flags(session_id: str) -> dict:
    """What S3 holds for a finished job: is there output, a validation report, a resumable session?"""
    files = list_output_files(session_id) if session_id else []
    has_output = bool(files)
    validation_done = any(
        f["filename"].startswith("final_validation_report_") and f["filename"].endswith(".xlsx") for f in files
    )
    if session_id and session_store.exists(session_id):
        resumable = True
    else:
        # Pipeline completed but validation not done — the session can be created/resumed from S3
        resumable = has_output and not validation_done
    return {"has_output": has_output, "validation_done": validation_done, "validation_resumable": resumable}


@app.get("/api/auth/recent-jobs")
async def auth_recent_jobs(emp_id: str):
    """Return usage logs from last 24h — unified view across ALL regions."""
    from src.usage_tracker import read_usage_logs
    from datetime import datetime, timedelta, timezone
    import traceback

    who = emp_id.strip()
    # Usage rows are written in IST, so the 24h window is measured in IST too.
    cutoff = datetime.now(timezone(timedelta(hours=5, minutes=30))).replace(tzinfo=None) - timedelta(hours=24)

    logs = []
    try:
        # Exact emp_id first, then everyone's recent rows matched by email/emp_id fragments
        # (an older record may carry the email prefix as its emp_id).
        logs = await asyncio.to_thread(read_usage_logs, who, 2)
        print(f"[RECENT-JOBS] Fetched {len(logs)} rows for emp_id='{who}'")
        if not logs:
            all_logs = await asyncio.to_thread(read_usage_logs, "", 2)
            emp_lower = who.lower()
            logs = [r for r in all_logs if (
                str(r.get("emp_id", "")).strip().lower() == emp_lower or
                emp_lower in str(r.get("email", "")).lower() or
                emp_lower in str(r.get("emp_id", "")).lower()
            )]
            print(f"[RECENT-JOBS] No direct match; {len(logs)} of {len(all_logs)} recent rows match")
    except Exception as e:
        print(f"[RECENT-JOBS] Warning: read_usage_logs failed: {e}")
        traceback.print_exc()

    recent = []
    for row in logs:
        try:
            row_date = str(row.get("date", "")).strip()
            row_time = str(row.get("time", "")).strip()
            if not row_date:
                continue
            dt_str = f"{row_date} {row_time}" if row_time else row_date
            row_dt = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y"):
                try:
                    row_dt = datetime.strptime(dt_str, fmt)
                    break
                except ValueError:
                    continue
            if row_dt is None or row_dt < cutoff:
                continue
            session_id = (
                str(row.get("session_id", "")).strip() or
                str(row.get("Session ID", "")).strip() or
                str(row.get("Session_ID", "")).strip() or
                str(row.get("sessionid", "")).strip()
            )
            flags = await asyncio.to_thread(_job_storage_flags, session_id)
            row["session_id"] = session_id  # normalize the key
            # Allow download as soon as output exists; validation report is a bonus
            row["download_available"] = flags["has_output"]
            row["validation_done"] = flags["validation_done"]
            row["validation_resumable"] = flags["validation_resumable"]
            if session_id and not row.get("file_name"):
                _job = await asyncio.to_thread(jobs.get, session_id)
                if _job:
                    row["file_name"] = _job.get("file_name", "")
            recent.append(row)
        except Exception as ex:
            print(f"[RECENT-JOBS] Exception processing row: {ex}")
            continue

    # Also include this user's jobs from the job registry (covers a usage-log write that failed,
    # and jobs still running).
    try:
        seen = {str(r.get("session_id", "")).strip() for r in recent}
        for job in await asyncio.to_thread(jobs.list_for_user, who):
            jid = job.get("job_id", "")
            if not jid or jid in seen:
                continue
            started = job.get("started_at", 0) or 0
            flags = await asyncio.to_thread(_job_storage_flags, jid)
            recent.append({
                "date": time.strftime("%Y-%m-%d", time.localtime(started)),
                "time": time.strftime("%H:%M:%S", time.localtime(started)),
                "emp_id": who,
                "name": "",
                "service_type": "PXML Track Changes",
                "region": "Canada",
                "product": job.get("product_type", ""),
                "files_uploaded": 1,
                "files_completed": 1 if job.get("status") == "completed" else 0,
                "files_failed": 1 if job.get("status") == "failed" else 0,
                "run_outcome": "success" if job.get("status") == "completed" else job.get("status", "running"),
                "processing_time_sec": (job.get("completed_at") or time.time()) - started,
                "session_id": jid,
                "file_name": job.get("file_name", ""),
                "download_available": flags["has_output"],
                "validation_done": flags["validation_done"],
                "validation_resumable": flags["validation_resumable"],
            })
    except Exception as e:
        print(f"[RECENT-JOBS] job registry lookup failed: {e}")

    # Sort by date+time descending (most recent first)
    recent.sort(key=lambda r: f"{r.get('date', '')} {r.get('time', '')}", reverse=True)
    return recent


# ─────────────────────────────────────────────
# Serving files out of S3
# ─────────────────────────────────────────────
def _build_outputs_zip(job_id: str) -> Optional[str]:
    """ZIP of everything under outputs/<job_id>/ (streamed from S3 into a scratch file).
    Returns the zip path, or None when the job has no outputs."""
    objs = list_output_files(job_id)
    if not objs:
        return None
    fd, zip_path = tempfile.mkstemp(prefix=f"{job_id}_output_", suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for o in objs:
                stream = s3_utils.open_stream(o["key"])
                if stream is None:
                    continue
                chunks, _info = stream
                with zf.open(o["filename"], "w", force_zip64=True) as dst:
                    for chunk in chunks:
                        dst.write(chunk)
        return zip_path
    except Exception:
        try:
            os.remove(zip_path)
        except OSError:
            pass
        raise


async def _zip_outputs_response(job_id: str, download_filename: str):
    zip_path = await asyncio.to_thread(_build_outputs_zip, job_id)
    if not zip_path:
        raise HTTPException(404, f"No output found for job: {job_id}")
    return FileResponse(zip_path, filename=download_filename, media_type="application/zip",
                        background=BackgroundTask(os.remove, zip_path))


_RANGE_RE = None


def _parse_range(header: str, size: int):
    """'bytes=a-b' -> (start, end) inclusive, or None if absent/unsatisfiable-as-whole-file."""
    global _RANGE_RE
    import re
    if _RANGE_RE is None:
        _RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
    m = _RANGE_RE.match((header or "").strip())
    if not m or (m.group(1) == "" and m.group(2) == ""):
        return None
    if m.group(1) == "":                      # suffix range: last N bytes
        n = int(m.group(2))
        return max(0, size - n), size - 1
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else size - 1
    if start >= size:
        return "bad"
    return start, min(end, size - 1)


def _s3_file_response(key: str, filename: str, media_type: str, request: Optional[Request] = None,
                      attachment: bool = False):
    """Proxy one S3 object to the client (supports HTTP Range, which the PDF viewer uses)."""
    info = s3_utils.object_info(key)
    if info is None:
        raise HTTPException(404, "File not found")
    size = info["size"]
    rng = _parse_range(request.headers.get("range", ""), size) if request is not None else None
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'{"attachment" if attachment else "inline"}; filename="{s3_utils.safe_filename(filename)}"',
    }
    if rng == "bad":
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    if rng:
        start, end = rng
        chunks, _ = s3_utils.open_stream(key, (start, end))
        headers.update({"Content-Range": f"bytes {start}-{end}/{size}", "Content-Length": str(end - start + 1)})
        return StreamingResponse(chunks, status_code=206, media_type=media_type, headers=headers)
    chunks, _ = s3_utils.open_stream(key)
    headers["Content-Length"] = str(size)
    return StreamingResponse(chunks, media_type=media_type, headers=headers)


@app.get("/api/download-by-session/{session_id}")
async def download_by_session(session_id: str):
    """Download output ZIP for a job by session_id (from usage logs). Served from S3."""
    if not session_id or not session_id.strip():
        raise HTTPException(400, "session_id is required")
    session_id = session_id.strip()
    return await _zip_outputs_response(session_id, f"{session_id}_output.zip")


# ─────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────

@app.get("/api/health")
async def health_check():
    """Liveness + a storage check. Always answers 200 (a brief S3 hiccup must not get the pod
    killed); the 'storage' field shows whether this pod can currently reach the bucket."""
    ok, detail = await asyncio.to_thread(s3_utils.check_storage)
    return {"status": "healthy", "workers": MAX_WORKERS, "active_jobs": jobs.running_here(),
            "storage": {"ok": ok, "detail": detail}}


@app.get("/healthz")
async def liveness():
    """Lightweight liveness probe for the deployment platform (Plexus) load balancer.
    Always 200 and does no I/O, so a transient S3/network hiccup can't get the pod killed.
    Registered before the SPA catch-all in static_react so it is never shadowed."""
    return {"status": "ok"}


@app.get("/api/region")
async def region_info():
    """Active region pack's public info (name, products, DTD families) so the UI never hard-codes a region."""
    return _REGION.public_info()


def _new_workspace(job_id: str) -> dict:
    """Scratch folders for ONE job on this pod. Throw-away: S3 holds the real copies."""
    work_dir = os.path.join(_JOBS_ROOT, s3_utils.safe_id(job_id))
    output_dir = os.path.join(work_dir, "output")
    os.makedirs(output_dir, exist_ok=True)
    return {"work_dir": work_dir, "output_dir": output_dir}


def _cleanup_workspace(job_id: str) -> None:
    """Drop a finished job's scratch folder. S3 already holds its inputs and outputs, and anything
    that needs them again restores them on demand (_ensure_job_workspace), so keeping them here
    would only fill the pod's disk."""
    try:
        shutil.rmtree(os.path.join(_JOBS_ROOT, s3_utils.safe_id(job_id)), ignore_errors=True)
    except Exception:
        pass


def _ensure_job_workspace(job_id: str) -> dict:
    """Make sure THIS pod has the job's input + output files on scratch disk.

    A job may have been run by another pod (or by a pod that has since been replaced), so anything
    that needs the files locally (re-validation, e-mailing the result, re-running) restores them
    from S3 first. Cheap when the files are already here."""
    ws = _new_workspace(job_id)
    work_dir, output_dir = ws["work_dir"], ws["output_dir"]
    jid = s3_utils.safe_id(job_id)

    has_inputs = any(n.lower().endswith((".docx", ".doc")) for n in os.listdir(work_dir))
    if not has_inputs:
        prefix = f"{S3_PREFIX_UPLOADS}/{jid}/"
        for o in s3_utils.list_objects(prefix):
            rel = o["key"][len(prefix):]
            if not rel or ".." in rel.split("/"):
                continue
            if rel.startswith("images/"):
                dest = os.path.join(work_dir, "revision_images", *rel[len("images/"):].split("/"))
            else:
                dest = os.path.join(work_dir, *rel.split("/"))
            download_file_from_s3(o["key"], dest)

    has_outputs = any(not n.startswith("_") for n in os.listdir(output_dir))
    if not has_outputs:
        download_directory_from_s3(f"{S3_PREFIX_OUTPUTS}/{jid}", output_dir)

    files = os.listdir(work_dir)
    docx = next((os.path.join(work_dir, n) for n in files if n.lower().endswith((".docx", ".doc"))), None)
    pxml = next((os.path.join(work_dir, n) for n in files if n.lower().endswith((".pxml", ".xml"))), None)
    images_dir = None
    rev = os.path.join(work_dir, "revision_images")
    if os.path.isdir(rev):
        items = os.listdir(rev)
        images_dir = os.path.join(rev, items[0]) if len(items) == 1 and os.path.isdir(os.path.join(rev, items[0])) else rev
    return {**ws, "docx_path": docx, "pxml_path": pxml, "images_dir": images_dir}


def _find_final_pxml(output_dir: str) -> str:
    found = list(Path(output_dir).glob("*_final.pxml")) if os.path.isdir(output_dir) else []
    return str(max(found, key=lambda p: p.stat().st_mtime)) if found else ""


def _submit_job(job_id: str, *, file_name: str, work_dir: str, output_dir: str, docx_path: str,
                pxml_path: str, images_dir: Optional[str], product_type: str, processing_mode: str,
                user_email: str, emp_id: str, extra: Optional[dict] = None):
    """Store the inputs in S3, create the job record and hand the run to the worker pool.
    Shared by /api/process and the batch driver so both behave identically."""
    # S3 is the only durable copy of the inputs (it is what a re-run or another pod restores from),
    # so a failed upload must stop the job instead of silently running without a safety net.
    upload_job_inputs(job_id, docx_path, pxml_path, images_dir)

    rec = {
        "job_id": job_id,
        "status": "running",
        "current_step": 0,
        "current_step_name": "Starting...",
        "total_steps": 12,
        "progress_pct": 0,
        "file_name": file_name,
        "output_dir": output_dir,
        "output_file": "",
        "error": "",
        "log": "",
        "product_type": product_type,
        "processing_mode": processing_mode,
        "user_email": user_email,
        "emp_id": emp_id,
        "started_at": time.time(),
        "completed_at": None,
        "s3_output_prefix": f"{S3_PREFIX_OUTPUTS}/{job_id}",
        "s3_input_prefix": f"{S3_PREFIX_UPLOADS}/{job_id}",
    }
    if extra:
        rec.update(extra)
    jobs[job_id] = rec        # creates + persists the record (and starts its heartbeat)

    # Submit to ProcessPoolExecutor (6 workers)
    future = executor.submit(_run_pipeline_job, job_id, docx_path, pxml_path, output_dir, product_type, images_dir, processing_mode)
    rec["_future"] = future
    future.add_done_callback(lambda f: _on_job_done(job_id, f))
    return rec, future


@app.post("/api/process")
async def process_files(
    docx_file: UploadFile = File(...),
    pxml_file: UploadFile = File(...),
    images_zip: Optional[UploadFile] = File(None),
    product_type: str = Form("looseleaf"),
    processing_mode: str = Form("chapter"),
    user_email: str = Form(""),
    emp_id: str = Form(""),
):
    """Upload files and start processing. Returns job_id for polling."""
    job_id = str(uuid.uuid4())[:8]
    ws = _new_workspace(job_id)
    work_dir, output_dir = ws["work_dir"], ws["output_dir"]

    docx_name = s3_utils.safe_filename(docx_file.filename, "document.docx")
    pxml_name = s3_utils.safe_filename(pxml_file.filename, "document.pxml")
    docx_path = os.path.join(work_dir, docx_name)
    pxml_path = os.path.join(work_dir, pxml_name)
    with open(docx_path, "wb") as f:
        f.write(await docx_file.read())
    with open(pxml_path, "wb") as f:
        f.write(await pxml_file.read())

    images_dir = None
    if images_zip and images_zip.filename:
        images_dir = os.path.join(work_dir, "revision_images")
        os.makedirs(images_dir, exist_ok=True)
        zip_path = os.path.join(work_dir, s3_utils.safe_filename(images_zip.filename, "images.zip"))
        with open(zip_path, "wb") as f:
            f.write(await images_zip.read())
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(images_dir)
        os.remove(zip_path)
        extracted_items = os.listdir(images_dir)
        if len(extracted_items) == 1 and os.path.isdir(os.path.join(images_dir, extracted_items[0])):
            images_dir = os.path.join(images_dir, extracted_items[0])

    try:
        await asyncio.to_thread(
            _submit_job, job_id, file_name=docx_file.filename, work_dir=work_dir, output_dir=output_dir,
            docx_path=docx_path, pxml_path=pxml_path, images_dir=images_dir, product_type=product_type,
            processing_mode=processing_mode, user_email=user_email, emp_id=emp_id)
    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(503, f"Could not store the input files in S3: {e}")

    return {"job_id": job_id, "status": "running"}


@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    job = await asyncio.to_thread(jobs.get, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    # Read live progress from the worker's status (local file on the owning pod, S3 on any other)
    if job["status"] == "running":
        if jobs.owned(job_id) and job.get("output_dir"):
            live = _read_job_status(job["output_dir"])
        else:
            live = await asyncio.to_thread(job_store.read_status, job_id)
        if live:
            job["current_step"] = live.get("current_step", job["current_step"])
            job["current_step_name"] = live.get("current_step_name", job["current_step_name"])
            job["progress_pct"] = live.get("progress_pct", job["progress_pct"])
            # Reflect an in-flight cancellation so the UI updates before the
            # done-callback finalizes the job record.
            if live.get("status") == "cancelled":
                job["status"] = "cancelled"
                job["current_step_name"] = "Cancelled by user"
    # Return a serializable copy (the Future object is not JSON-serializable).
    return {k: v for k, v in job.items() if not k.startswith("_")}


@app.post("/api/cancel/{job_id}")
async def cancel_job(job_id: str):
    """Request cancellation of a running job.

    Cooperative cancellation: raises a flag (in S3, so it works whichever pod receives the click)
    that the worker checks at each pipeline step boundary. If the job is still queued (not started),
    the future is cancelled immediately. Returns the resulting job status.
    """
    job = await asyncio.to_thread(jobs.get, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    if job["status"] not in ("running",):
        return {"job_id": job_id, "status": job["status"], "message": "Job is not running."}

    # Raise the cancel flag so the worker stops at the next step boundary.
    output_dir = job.get("output_dir")
    if output_dir and jobs.owned(job_id):
        await asyncio.to_thread(_request_cancel, output_dir)
    else:
        await asyncio.to_thread(job_store.set_cancel, job_id)

    # If the job hasn't started yet, cancel the future directly (only this pod holds the future).
    future = job.get("_future")
    if future is not None and future.cancel():
        job["status"] = "cancelled"
        job["current_step_name"] = "Cancelled by user"
        job["completed_at"] = time.time()
        _save_job(job)
        _log_job_usage(job_id, "cancelled")
        return {"job_id": job_id, "status": "cancelled", "message": "Job cancelled before start."}

    # Otherwise it's already running — mark as cancelling; worker will stop soon.
    if jobs.owned(job_id):
        job["current_step_name"] = "Cancelling…"
        _save_job(job)
    return {"job_id": job_id, "status": "cancelling",
            "message": "Cancellation requested. The job will stop at the next step boundary."}


@app.get("/api/download/{job_id}")
async def download_output(job_id: str):
    """Download output as ZIP (built from S3, so any pod can serve it)."""
    job = await asyncio.to_thread(jobs.get, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job["status"] != "completed":
        raise HTTPException(400, f"Job not complete. Status: {job['status']}")

    # Build download filename: job_id + truncated original filename
    raw_name = os.path.splitext(job.get("file_name", "output"))[0]
    short_name = raw_name[:40] if len(raw_name) > 40 else raw_name
    return await _zip_outputs_response(job_id, f"{job_id}_{short_name}.zip")


@app.get("/api/download-s3/{job_id}/{filename:path}")
async def download_from_s3(job_id: str, filename: str):
    """Redirect to presigned S3 URL for a specific output file."""
    s3_key = f"{S3_PREFIX_OUTPUTS}/{s3_utils.safe_id(job_id)}/{filename}"
    try:
        if not await asyncio.to_thread(s3_utils.object_exists, s3_key):
            raise HTTPException(404, "File not found in S3")
        url = generate_presigned_url(s3_key)
        return RedirectResponse(url=url)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(404, f"File not found in S3: {e}")


@app.get("/api/list-outputs/{job_id}")
async def list_outputs(job_id: str):
    """List all output files for a job from S3."""
    if not await asyncio.to_thread(jobs.exists, job_id):
        raise HTTPException(404, "Job not found")
    files = await asyncio.to_thread(list_output_files, job_id)
    return {"job_id": job_id, "files": files}


def _run_validation_sync(job_id: str, output_dir: str, output_file: str):
    """Run validation synchronously — intended to be called via run_in_executor."""
    # Defensive checks: validate inputs before proceeding
    if not output_dir or not isinstance(output_dir, str):
        raise RuntimeError(f"Invalid output_dir: '{output_dir}'")
    if not output_file or not isinstance(output_file, str):
        raise RuntimeError(f"Invalid output_file: '{output_file}'")
    if not os.path.isdir(output_dir):
        raise RuntimeError(f"Output directory does not exist: '{output_dir}'")
    if not os.path.isfile(output_file):
        raise RuntimeError(f"Output file does not exist: '{output_file}'")

    project_dir = os.path.dirname(os.path.abspath(__file__))
    if project_dir not in sys.path:
        sys.path.insert(0, project_dir)

    from canada_pipeline.exe_track_changes_1 import run_validation_for_gui

    prompts_dir = os.path.join(project_dir, "validation_prompts")
    job_dir = os.path.dirname(output_dir)

    docx_files = list(Path(job_dir).glob("*.docx")) + list(Path(job_dir).glob("*.doc"))
    if not docx_files:
        raise RuntimeError("DOCX not found in job directory")

    images_folder = None
    _rev_dir = os.path.join(job_dir, "revision_images")
    if os.path.isdir(_rev_dir):
        _items = os.listdir(_rev_dir)
        images_folder = os.path.join(_rev_dir, _items[0]) if len(_items) == 1 and os.path.isdir(os.path.join(_rev_dir, _items[0])) else _rev_dir

    excel_path = run_validation_for_gui(
        docx_path=str(docx_files[0]),
        final_xml_path=output_file,
        output_dir=output_dir,
        prompt_path=os.path.join(prompts_dir, "prompt.txt"),
        validation_prompt_path=os.path.join(prompts_dir, "prompt_validation.txt"),
        images_folder=images_folder,
    )

    # Publish the validation report (and anything else validation wrote) to S3
    _upload_outputs_with_retry(job_id, output_dir)
    _push_job_log(output_dir, force=True)
    return excel_path


@app.post("/api/validate/{job_id}")
async def run_validation(job_id: str):
    job = await asyncio.to_thread(jobs.get, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    # Allow validation if pipeline is completed OR if pipeline steps are done (step >= 11)
    if job["status"] == "failed":
        raise HTTPException(400, "Pipeline failed — cannot run validation")
    if job["status"] == "running" and job.get("current_step", 0) < 11:
        raise HTTPException(400, "Pipeline must complete before validation")

    # The job may have been run by another pod: restore its files from S3, and take the record over
    # so progress updates made below are persisted by this pod.
    try:
        ws = await asyncio.to_thread(_ensure_job_workspace, job_id)
    except Exception as e:
        raise HTTPException(500, f"Could not restore the job's files from S3: {e}")
    if not jobs.owned(job_id):
        jobs.adopt(job)
    output_dir = ws["output_dir"]
    job["output_dir"] = output_dir

    # Discover the final PXML (the path stored on the record may belong to another pod)
    output_file = job.get("output_file", "")
    if not output_file or not os.path.isfile(output_file):
        output_file = _find_final_pxml(output_dir)
        if not output_file:
            raise HTTPException(400, "Output PXML file not found. Pipeline may not have completed successfully.")
        job["output_file"] = output_file

    try:
        # Update step to 12 — Validation running
        job["current_step"] = 12
        job["current_step_name"] = "Validation Report"
        job["progress_pct"] = 95
        _save_job(job)

        loop = asyncio.get_event_loop()
        excel_path = await loop.run_in_executor(
            executor,
            _run_validation_sync,
            job_id,
            output_dir,
            output_file,
        )

        # Mark step 12 complete
        job["current_step"] = 12
        job["current_step_name"] = "Completed"
        job["progress_pct"] = 100
        _save_job(job)

        # ── Auto-send completion email (runs even if user's browser disconnected) ──
        _job_email = job.get("user_email", "")
        if _job_email:
            try:
                from canada_pipeline.email_notifier import send_completion_email
                send_completion_email(
                    to_email=_job_email,
                    job_id=job_id,
                    final_xml_path=output_file,
                    validation_report_path=excel_path,
                    product_type=job.get("product_type", ""),
                    file_name=job.get("file_name", ""),
                )
            except Exception as _email_err:
                print(f"[EMAIL] Failed to send completion email: {_email_err}")

        return {"status": "completed", "excel_path": excel_path}
    except Exception as e:
        raise HTTPException(500, f"Validation failed: {e}")
    finally:
        _cleanup_workspace(job_id)       # restored from S3 for this run only; S3 keeps the real copies


def _latest_validation_report(job_id: str) -> Optional[dict]:
    reports = [f for f in list_output_files(job_id)
               if f["filename"].startswith("final_validation_report_") and f["filename"].endswith(".xlsx")]
    return max(reports, key=lambda f: f["filename"]) if reports else None      # names carry a timestamp


@app.get("/api/download-validation/{job_id}")
async def download_validation(job_id: str, request: Request):
    job = await asyncio.to_thread(jobs.get, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    report = await asyncio.to_thread(_latest_validation_report, job_id)
    if not report:
        raise HTTPException(404, "Validation report not found.")
    # Build download filename: ValidationReport + truncated original filename
    raw_name = os.path.splitext(job.get("file_name", "output"))[0]
    short_name = raw_name[:40] if len(raw_name) > 40 else raw_name
    return await asyncio.to_thread(
        _s3_file_response, report["key"], f"ValidationReport_{short_name}.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", request, True)


@app.get("/api/log/{job_id}")
async def get_log(job_id: str):
    job = await asyncio.to_thread(jobs.get, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    def _read():
        # The pod running the job has the freshest log on its scratch disk; everyone else reads S3
        if jobs.owned(job_id) and job.get("output_dir"):
            p = os.path.join(job["output_dir"], "pipeline_log.txt")
            if os.path.isfile(p):
                with open(p, 'r', encoding='utf-8', errors='ignore') as f:
                    return f.read()[-50000:]
        raw = s3_utils.get_bytes(f"{S3_PREFIX_OUTPUTS}/{s3_utils.safe_id(job_id)}/pipeline_log.txt")
        return raw.decode("utf-8", errors="ignore")[-50000:] if raw else ""

    return {"log": await asyncio.to_thread(_read)}


@app.post("/api/send-completion-email/{job_id}")
async def send_completion_email_endpoint(job_id: str, to_email: str = Form(...)):
    """Send the final XML + validation report as a ZIP to the user's email."""
    job = await asyncio.to_thread(jobs.get, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job["status"] != "completed":
        raise HTTPException(400, "Pipeline must complete before sending email")

    file_name = job.get("file_name", "")
    try:
        ws = await asyncio.to_thread(_ensure_job_workspace, job_id)
    except Exception as e:
        raise HTTPException(500, f"Could not restore the job's files from S3: {e}")
    output_dir = ws["output_dir"]
    output_file = _find_final_pxml(output_dir)

    # Find validation report
    validation_path = ""
    excel_files = sorted(Path(output_dir).glob("final_validation_report_*.xlsx"))
    if excel_files:
        validation_path = str(excel_files[-1])

    from canada_pipeline.email_notifier import send_completion_email
    success = await asyncio.to_thread(
        send_completion_email,
        to_email=to_email,
        job_id=job_id,
        final_xml_path=output_file,
        validation_report_path=validation_path,
        product_type=job.get("product_type", ""),
        file_name=file_name,
    )
    _cleanup_workspace(job_id)           # restored from S3 for this send only

    if success:
        return {"status": "sent", "to": to_email}
    else:
        raise HTTPException(500, "Failed to send email. Check the Microsoft Graph / EMAIL_FROM configuration.")


# ─────────────────────────────────────────────
# Post-vendor PXML navigation (Visual Validator search)
# ─────────────────────────────────────────────
# Integrated from the former standalone `backend_postxml_navigation` service so
# the DOCX Viewer V2 fuzzy-search ("click a paragraph → locate it in the PXML")
# is served by this single backend on port 8000. No second service/port needed.
import re as _re
from rapidfuzz import fuzz as _fuzz


class SearchRequest(BaseModel):
    context_text: str          # prev 3 paras + current + next 3 paras combined
    pxml_content: str          # raw PXML file content
    chunk_size: int = 500      # characters per chunk
    overlap_percent: float = 0.3  # 30% overlap


class ChunkMatch(BaseModel):
    chunk_text: str   # the raw XML chunk (with tags)
    plain_text: str   # the stripped text used for matching
    score: float      # fuzzy match percentage
    start_index: int  # character start position in the PXML file
    end_index: int    # character end position in the PXML file


class SearchResponse(BaseModel):
    matches: list[ChunkMatch]
    total_chunks: int
    query_text: str


def _strip_xml_tags(text: str) -> str:
    """Remove all XML/HTML tags, keeping only text content."""
    clean = _re.sub(r'<[^>]+>', ' ', text)
    clean = _re.sub(r'\s+', ' ', clean).strip()
    return clean


def _chunk_pxml(content: str, chunk_size: int, overlap_percent: float) -> list[dict]:
    """Split PXML content into overlapping chunks with start/end indices."""
    overlap = int(chunk_size * overlap_percent)
    step = max(1, chunk_size - overlap)
    chunks = []
    i = 0
    while i < len(content):
        end = min(i + chunk_size, len(content))
        chunks.append({
            "chunk_text": content[i:end],
            "start_index": i,
            "end_index": end,
        })
        if end >= len(content):
            break
        i += step
    return chunks


# ─────────────────────────────────────────────
# DOCX → PDF conversion (PDF Viewer)
# ─────────────────────────────────────────────
import tempfile as _tempfile

_PDF_CACHE_DIR = _tempfile.mkdtemp(prefix="docx_pdf_")


@app.post("/api/convert-to-pdf")
async def convert_docx_to_pdf(file: UploadFile = File(...), session_id: str = Form(None)):
    """
    Convert DOCX to PDF using Microsoft Word (COM automation).
    Track changes are shown as revision markup in the PDF (insertions + deletions),
    but formatting change balloons are suppressed by REJECTING formatting-only
    revisions on a temporary copy before export.
    
    If session_id is provided, saves the PDF into the session's S3 folder so it's available on
    resume (from any pod) without reconversion.
    """
    # If session already has a PDF (from background conversion), return it directly
    if session_id:
        _smeta = await asyncio.to_thread(session_store.load_meta, session_id)
        _existing_pdf = (_smeta or {}).get("pdf_filename")
        if _existing_pdf and await asyncio.to_thread(session_store.file_exists, session_id, _existing_pdf):
            print(f"[PDF] Returning cached PDF for session {session_id}: {_existing_pdf}")
            return await asyncio.to_thread(
                _s3_file_response, session_store.key(session_id, _existing_pdf), _existing_pdf, "application/pdf")

    temp_docx = os.path.join(_PDF_CACHE_DIR, f"{uuid.uuid4().hex}_{s3_utils.safe_filename(file.filename, 'document.docx')}")
    os.makedirs(_PDF_CACHE_DIR, exist_ok=True)
    with open(temp_docx, "wb") as f:
        content = await file.read()
        f.write(content)

    pdf_filename = Path(file.filename or "document.docx").stem + ".pdf"
    temp_pdf = os.path.join(_PDF_CACHE_DIR, f"{uuid.uuid4().hex}_{pdf_filename}")

    try:
        import win32com.client
        import pythoncom

        # Word constants
        WD_EXPORT_FORMAT_PDF = 17
        WD_EXPORT_DOCUMENT_WITH_MARKUP = 7  # include tracked-change markup

        # Revision types that represent FORMATTING-only changes.
        # These produce "Formatted: ..." balloons. We reject them so they
        # vanish from the PDF, while insertions/deletions are left untouched.
        WD_REVISION_PROPERTY = 3             # wdRevisionProperty (character formatting)
        WD_REVISION_PARAGRAPH_PROPERTY = 10  # wdRevisionParagraphProperty
        WD_REVISION_STYLE = 8               # wdRevisionStyle
        WD_REVISION_STYLE_DEFINITION = 13    # wdRevisionStyleDefinition
        WD_REVISION_SECTION_PROPERTY = 12    # wdRevisionSectionProperty
        WD_REVISION_TABLE_PROPERTY = 11      # wdRevisionTableProperty

        _FORMATTING_REVISION_TYPES = {
            WD_REVISION_PROPERTY,
            WD_REVISION_PARAGRAPH_PROPERTY,
            WD_REVISION_STYLE,
            WD_REVISION_STYLE_DEFINITION,
            WD_REVISION_SECTION_PROPERTY,
            WD_REVISION_TABLE_PROPERTY,
        }

        pythoncom.CoInitialize()
        try:
            word = win32com.client.DispatchEx("Word.Application")
            word.Visible = False
            word.DisplayAlerts = 0  # wdAlertsNone

            # Speed: stop Word from repaginating/redrawing after every Reject()
            try:
                word.ScreenUpdating = False
                word.Options.Pagination = False
                word.Options.CheckGrammarAsYouType = False
                word.Options.CheckSpellingAsYouType = False
            except Exception:
                pass

            # Open read-write so we can reject formatting revisions on the temp copy
            doc = word.Documents.Open(
                os.path.abspath(temp_docx),
                ReadOnly=False,
                AddToRecentFiles=False,
                ConfirmConversions=False,
            )

            # Reject formatting-only revisions (iterate in reverse since Reject removes items)
            revisions = doc.Revisions
            total_revs = revisions.Count
            rejected = 0
            for i in range(total_revs, 0, -1):
                rev = revisions.Item(i)
                if rev.Type in _FORMATTING_REVISION_TYPES:
                    rev.Reject()
                    rejected += 1
            print(f"[PDF] Rejected {rejected}/{total_revs} formatting revision(s).")

            # Show tracked changes (markup) in general
            doc.ShowRevisions = True

            # Belt-and-braces: also hide format changes in view settings
            view = word.ActiveWindow.View
            view.RevisionsMode = 1              # wdInLineRevisions — force ALL inline, no balloons
            view.ShowInsertionsAndDeletions = True   # keep insertions + deletions
            view.ShowFormatChanges = False           # hide "Formatted: ..." balloons
            view.ShowComments = False                # drop comment balloons
            view.ShowRevisionsAndComments = True
            view.RevisionsBalloonWidth = 0

            doc.ExportAsFixedFormat(
                OutputFileName=os.path.abspath(temp_pdf),
                ExportFormat=WD_EXPORT_FORMAT_PDF,
                Item=WD_EXPORT_DOCUMENT_WITH_MARKUP,
            )

            doc.Close(SaveChanges=False)  # discard the in-memory rejections
        finally:
            try:
                word.Quit()
            except Exception:
                pass
            pythoncom.CoUninitialize()
            # Clean up temp docx
            try:
                os.remove(temp_docx)
            except Exception:
                pass

    except ImportError:
        # Fallback: try LibreOffice if win32com not available
        try:
            import subprocess as _sp
            _sp.run(
                ["soffice", "--headless", "--convert-to", "pdf", "--outdir", _PDF_CACHE_DIR, temp_docx],
                check=True, capture_output=True, timeout=60,
            )
        except (Exception,) as e:
            return JSONResponse(status_code=500, content={"error": f"PDF conversion failed: {str(e)}"})

    if not os.path.exists(temp_pdf):
        return JSONResponse(status_code=500, content={"error": "PDF conversion failed - output file not created"})

    # If session_id provided, store the PDF in the session's S3 folder and record it on the session
    if session_id:
        def _store_pdf():
            meta = session_store.load_meta(session_id)
            if meta is None:
                return
            session_store.put_file_from_path(session_id, pdf_filename, temp_pdf)
            meta["pdf_filename"] = pdf_filename
            session_store.touch(meta)
            print(f"[PDF-SESSION] PDF stored for session {session_id}: {pdf_filename}")
        try:
            await asyncio.to_thread(_store_pdf)
        except Exception as e:
            print(f"[PDF-SESSION] Warning: Could not save PDF to session: {e}")

    # The converted PDF is only a scratch file: remove it once it has been sent.
    return FileResponse(temp_pdf, media_type="application/pdf", filename=pdf_filename,
                        background=BackgroundTask(lambda p=temp_pdf: os.path.exists(p) and os.remove(p)))


@app.post("/api/search", response_model=SearchResponse)
async def search_pxml(request: SearchRequest):
    """
    Locate a DOCX paragraph inside the post-vendor PXML:
      1. Chunk the PXML with overlap
      2. Strip tags from each chunk to get plain text
      3. Fuzzy match context_text against each chunk's plain text
      4. Return the top 5 matches with scores and character positions
    """
    chunks = _chunk_pxml(request.pxml_content, request.chunk_size, request.overlap_percent)

    # Clean the query text (collapse stray whitespace)
    query = _re.sub(r'\s+', ' ', request.context_text).strip()

    scored_chunks: list[ChunkMatch] = []
    for chunk in chunks:
        plain = _strip_xml_tags(chunk["chunk_text"])
        if not plain:
            continue
        # token_set_ratio handles partial & reordered matches well
        score = _fuzz.token_set_ratio(query, plain)
        scored_chunks.append(ChunkMatch(
            chunk_text=chunk["chunk_text"],
            plain_text=plain,
            score=round(score, 1),
            start_index=chunk["start_index"],
            end_index=chunk["end_index"],
        ))

    scored_chunks.sort(key=lambda x: x.score, reverse=True)
    top_matches = scored_chunks[:5]

    return SearchResponse(
        matches=top_matches,
        total_chunks=len(chunks),
        query_text=query,
    )


# ─────────────────────────────────────────────
# Validation Session Management
# ─────────────────────────────────────────────
import json as _json

# ─── Background DOCX → PDF conversion (runs in background thread) ───
def _convert_docx_to_pdf_sync(docx_path: str, pdf_path: str) -> bool:
    """Convert DOCX to PDF using Word COM automation. Designed for background threads."""
    try:
        import win32com.client
        import pythoncom

        WD_EXPORT_FORMAT_PDF = 17
        WD_EXPORT_DOCUMENT_WITH_MARKUP = 7
        _FMT_TYPES = {3, 10, 8, 13, 12, 11}  # formatting-only revision types

        pythoncom.CoInitialize()
        try:
            word = win32com.client.DispatchEx("Word.Application")
            word.Visible = False
            word.DisplayAlerts = 0
            try:
                word.ScreenUpdating = False
                word.Options.Pagination = False
                word.Options.CheckGrammarAsYouType = False
                word.Options.CheckSpellingAsYouType = False
            except Exception:
                pass

            doc = word.Documents.Open(
                os.path.abspath(docx_path), ReadOnly=False,
                AddToRecentFiles=False, ConfirmConversions=False,
            )

            revisions = doc.Revisions
            total_revs = revisions.Count
            rejected = 0
            for i in range(total_revs, 0, -1):
                if revisions.Item(i).Type in _FMT_TYPES:
                    revisions.Item(i).Reject()
                    rejected += 1
            print(f"[PDF-BG] Rejected {rejected}/{total_revs} formatting revision(s).")

            doc.ShowRevisions = True
            view = word.ActiveWindow.View
            view.RevisionsMode = 1
            view.ShowInsertionsAndDeletions = True
            view.ShowFormatChanges = False
            view.ShowComments = False
            view.ShowRevisionsAndComments = True
            view.RevisionsBalloonWidth = 0

            doc.ExportAsFixedFormat(
                OutputFileName=os.path.abspath(pdf_path),
                ExportFormat=WD_EXPORT_FORMAT_PDF,
                Item=WD_EXPORT_DOCUMENT_WITH_MARKUP,
            )
            doc.Close(SaveChanges=False)
            print(f"[PDF-BG] Converted: {os.path.basename(pdf_path)}")
            return True
        finally:
            try:
                word.Quit()
            except Exception:
                pass
            pythoncom.CoUninitialize()
    except Exception as e:
        print(f"[PDF-BG] Background conversion failed: {e}")
        return False


# ─── Session PDF (runs in a background thread) ───
def _finalize_session_pdf(session_id: str):
    """If the session has no PDF yet, convert its DOCX (Word COM) and store the PDF in S3."""
    scratch = None
    try:
        meta = session_store.load_meta(session_id)
        if not meta:
            return
        docx_filename = meta.get("docx_filename")
        if not docx_filename or meta.get("pdf_filename"):
            return
        scratch = tempfile.mkdtemp(prefix=f"sess_{session_id}_")
        docx_local = os.path.join(scratch, s3_utils.safe_filename(docx_filename))
        if not session_store.download_file(session_id, docx_filename, docx_local):
            return
        pdf_name = (docx_filename.rsplit(".", 1)[0] if "." in docx_filename else docx_filename) + ".pdf"
        pdf_local = os.path.join(scratch, s3_utils.safe_filename(pdf_name))
        if _convert_docx_to_pdf_sync(docx_local, pdf_local):
            session_store.put_file_from_path(session_id, pdf_name, pdf_local)
            meta = session_store.load_meta(session_id) or meta       # re-read: the reviewer may have progressed meanwhile
            meta["pdf_filename"] = pdf_name
            session_store.touch(meta)
            print(f"[SESSION] PDF stored for session {session_id}: {pdf_name}")
    except Exception as e:
        print(f"[SESSION] WARNING: PDF conversion/storage failed for session {session_id}: {e}")
    finally:
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)


def _with_session_file(session_id: str, filename: str, fn) -> bool:
    """Edit a session file that needs a real path (openpyxl): download it to scratch, let `fn(path)`
    modify it, upload the result. Returns False when the file does not exist."""
    return session_store.edit_file(session_id, filename, fn)


class ValidationSessionCreate(BaseModel):
    session_name: str
    docx_filename: str
    pxml_filename: str
    excel_filename: Optional[str] = None
    pdf_filename: Optional[str] = None
    total_revisions: int = 0


class RevisionStatusUpdate(BaseModel):
    session_id: str
    revision_index: int
    status: str  # "complete" | "skipped" | "pending"


class SavePxmlRequest(BaseModel):
    session_id: str
    pxml_content: str


def _require_session(session_id: str) -> dict:
    meta = session_store.load_meta(session_id)
    if meta is None:
        raise HTTPException(404, "Session not found")
    return meta


@app.post("/api/validation/create-session")
async def create_validation_session(
    docx_file: UploadFile = File(...),
    pxml_file: UploadFile = File(...),
    excel_file: Optional[UploadFile] = File(None),
    pdf_file: Optional[UploadFile] = File(None),
    failed_revisions_file: Optional[UploadFile] = File(None),
    session_name: str = Form(""),
    emp_id: str = Form(""),
):
    """Create a new validation session. The uploaded files are stored in the session's S3 folder."""
    session_id = session_store.new_session_id()

    docx_name = s3_utils.safe_filename(docx_file.filename, "document.docx")
    await asyncio.to_thread(session_store.put_file, session_id, docx_name, await docx_file.read())

    pxml_name = s3_utils.safe_filename(pxml_file.filename, "document.pxml")
    await asyncio.to_thread(session_store.put_file, session_id, pxml_name, await pxml_file.read())

    excel_name = None
    if excel_file and excel_file.filename:
        excel_name = s3_utils.safe_filename(excel_file.filename)
        await asyncio.to_thread(session_store.put_file, session_id, excel_name, await excel_file.read())

    pdf_name = None
    if pdf_file and pdf_file.filename:
        pdf_name = s3_utils.safe_filename(pdf_file.filename)
        await asyncio.to_thread(session_store.put_file, session_id, pdf_name, await pdf_file.read())

    # Failed_revisions.xlsx — either from dedicated upload or auto-detect from excel_file name
    failed_revisions_name = None
    if failed_revisions_file and failed_revisions_file.filename:
        failed_revisions_name = s3_utils.safe_filename(failed_revisions_file.filename)
        await asyncio.to_thread(session_store.put_file, session_id, failed_revisions_name, await failed_revisions_file.read())
    elif excel_name and "failed" in excel_name.lower():
        failed_revisions_name = excel_name  # already stored above

    meta = session_store.new_meta(
        session_id, emp_id, session_name or docx_file.filename or "Untitled",
        docx_filename=docx_name, pxml_filename=pxml_name, excel_filename=excel_name,
        pdf_filename=pdf_name, failed_revisions_filename=failed_revisions_name,
    )
    await asyncio.to_thread(session_store.save_meta, meta)

    # No PDF supplied: convert the DOCX in the background (non-blocking) and attach it when done
    if not pdf_name:
        asyncio.get_event_loop().run_in_executor(None, _finalize_session_pdf, session_id)

    return meta


@app.get("/api/validation/sessions")
async def list_validation_sessions(emp_id: str = ""):
    """List validation sessions, sorted by most recent. Filtered by emp_id when provided."""
    sessions = []
    for meta in await asyncio.to_thread(session_store.list_sessions, emp_id):
        try:
            # Compute progress
            statuses = meta.get("revision_statuses", {})
            completed = sum(1 for s in statuses.values() if s == "complete")
            total_invalid = meta.get("total_invalid", 0)
            meta["completed_count"] = completed
            meta["progress_text"] = f"{completed}/{total_invalid}" if total_invalid > 0 else "0/0"
            sessions.append(meta)
        except Exception:
            pass

    sessions.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
    return {"sessions": sessions}


@app.post("/api/validation/resume")
@app.post("/api/validation/resume-from-sharepoint")   # legacy path kept so older UI builds still work
async def resume_validation_session(session_id: str = Form(...), emp_id: str = Form("")):
    """
    Resume a validation session. Everything lives in S3, so this works from any pod and after any
    restart. If the progress record is missing but the session's files are still there, it is
    rebuilt from them.
    """
    def _resume():
        meta = session_store.load_meta(session_id)
        if meta:
            docx_ok = meta.get("docx_filename") and session_store.file_exists(session_id, meta["docx_filename"])
            pxml_ok = meta.get("pxml_filename") and session_store.file_exists(session_id, meta["pxml_filename"])
            if docx_ok and pxml_ok:
                return meta  # All good, resume as-is
            raise HTTPException(404, f"Session {session_id} is missing its DOCX or PXML file in storage")

        files = session_store.list_files(session_id)
        if not files:
            raise HTTPException(404, f"No files found in storage for session {session_id}")

        def _pick(*exts, exclude=()):
            return next((f for f in files if f.rsplit(".", 1)[-1].lower() in exts
                         and not any(x in f.lower() for x in exclude)), None)

        docx_filename = _pick("docx", "doc")
        pxml_filename = _pick("pxml", "xml")
        pdf_filename = _pick("pdf")
        excel_filename = _pick("xlsx", "xls", exclude=("failed", "false_positives"))
        failed_revisions_filename = next((f for f in files if "failed" in f.lower() and f.lower().endswith(".xlsx")), None)
        meta = session_store.new_meta(
            session_id, emp_id, docx_filename or "Resumed Session",
            docx_filename=docx_filename, pxml_filename=pxml_filename, excel_filename=excel_filename,
            pdf_filename=pdf_filename, failed_revisions_filename=failed_revisions_filename,
            resumed_from_storage=True,
        )
        return session_store.save_meta(meta)

    return await asyncio.to_thread(_resume)


@app.get("/api/validation/session/{session_id}")
async def get_validation_session(session_id: str, emp_id: str = ""):
    """Get session metadata."""
    meta = await asyncio.to_thread(session_store.load_meta, session_id)
    if meta is None:
        raise HTTPException(404, "Session not found")
    return meta


@app.delete("/api/validation/session/{session_id}")
async def delete_validation_session(session_id: str, emp_id: str = ""):
    """Delete a validation session — removes all of its files from S3."""
    try:
        await asyncio.to_thread(session_store.delete_session, session_id)
    except Exception as e:
        raise HTTPException(503, f"Could not delete the session: {e}")
    return {"status": "deleted", "session_id": session_id}


_SESSION_MEDIA_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pxml": "application/xml", "xml": "application/xml",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "pdf": "application/pdf",
}


@app.get("/api/validation/session/{session_id}/file/{filename}")
async def get_session_file(session_id: str, filename: str, request: Request):
    """Serve a file from the session's S3 folder (HTTP Range supported, as the PDF viewer needs)."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return await asyncio.to_thread(
        _s3_file_response, session_store.key(session_id, filename), filename,
        _SESSION_MEDIA_TYPES.get(ext, "application/octet-stream"), request)


@app.post("/api/validation/session/{session_id}/upload-file")
async def upload_session_file(session_id: str, file: UploadFile = File(...)):
    """Upload an additional file (e.g. Excel) to an existing session."""
    meta = await asyncio.to_thread(_require_session, session_id)

    fname = s3_utils.safe_filename(file.filename, "upload")
    data = await file.read()
    try:
        await asyncio.to_thread(session_store.put_file, session_id, fname, data)
    except ValueError as e:
        raise HTTPException(400, str(e))

    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
    if ext in ("xlsx", "xls"):
        meta["excel_filename"] = fname
    elif ext == "pxml":
        meta["pxml_filename"] = fname
    await asyncio.to_thread(session_store.touch, meta)

    return {"status": "uploaded", "filename": fname}


@app.post("/api/validation/save-pxml")
async def save_pxml(request: SavePxmlRequest):
    """Save edited PXML content back to the session's S3 folder."""
    meta = await asyncio.to_thread(_require_session, request.session_id)
    pxml_filename = meta.get("pxml_filename", "document.pxml")

    await asyncio.to_thread(session_store.put_file, request.session_id, pxml_filename,
                            request.pxml_content.encode("utf-8"))
    await asyncio.to_thread(session_store.touch, meta)

    return {"status": "saved", "filename": pxml_filename}


@app.post("/api/validation/update-revision-status")
async def update_revision_status(request: RevisionStatusUpdate):
    """Mark a revision as complete/skipped/pending and update the session + Excel."""
    meta = await asyncio.to_thread(_require_session, request.session_id)

    # Update status
    meta["revision_statuses"][str(request.revision_index)] = request.status

    # Move to next incomplete revision
    total = meta.get("total_revisions", 0)
    if request.status == "complete" and request.revision_index == meta.get("current_revision_index", 0):
        # Advance to next pending
        next_idx = request.revision_index + 1
        while next_idx < total:
            if meta["revision_statuses"].get(str(next_idx)) != "complete":
                break
            next_idx += 1
        meta["current_revision_index"] = min(next_idx, total - 1) if total > 0 else 0

    await asyncio.to_thread(session_store.touch, meta)

    # Update validation Excel if present
    excel_filename = meta.get("excel_filename")
    if excel_filename:
        try:
            await asyncio.to_thread(
                _with_session_file, request.session_id, excel_filename,
                lambda p: _update_excel_status(p, meta["revision_statuses"]))
        except Exception as e:
            print(f"[VALIDATION] Excel update failed: {e}")

    completed = sum(1 for s in meta["revision_statuses"].values() if s == "complete")
    total_invalid = meta.get("total_invalid", 0)
    denom = total_invalid if total_invalid > 0 else total

    return {
        "status": "updated",
        "current_revision_index": meta["current_revision_index"],
        "completed_count": completed,
        "total": total,
        "total_invalid": total_invalid,
        "progress_text": f"{completed}/{denom}",
    }


@app.post("/api/validation/update-position")
async def update_position(session_id: str = Form(...), current_revision_index: int = Form(...)):
    """Update the current revision index (user navigated manually)."""
    meta = await asyncio.to_thread(_require_session, session_id)
    meta["current_revision_index"] = current_revision_index
    await asyncio.to_thread(session_store.touch, meta)
    return {"status": "updated"}


@app.post("/api/validation/set-total-revisions")
async def set_total_revisions(session_id: str = Form(...), total_revisions: int = Form(...), total_invalid: int = Form(0)):
    """Set the total revision count once DOCX is parsed on the frontend."""
    meta = await asyncio.to_thread(_require_session, session_id)
    meta["total_revisions"] = total_revisions
    if total_invalid > 0:
        meta["total_invalid"] = total_invalid
    await asyncio.to_thread(session_store.touch, meta)
    return {"status": "updated", "total_revisions": total_revisions, "total_invalid": meta.get("total_invalid", 0)}


def _update_excel_status(excel_path: str, revision_statuses: dict):
    """Add/update a 'status' column in the validation Excel file."""
    import openpyxl
    wb = openpyxl.load_workbook(excel_path)
    ws = wb.active
    if not ws:
        return

    # Find or create the "status" column
    header_row = list(ws.iter_rows(min_row=1, max_row=1, values_only=True))[0]
    status_col = None
    for i, h in enumerate(header_row, 1):
        if h and str(h).strip().lower() == "status":
            status_col = i
            break
    if status_col is None:
        status_col = len(header_row) + 1
        ws.cell(row=1, column=status_col, value="status")

    # Write statuses (row 2 = revision 0, row 3 = revision 1, etc.)
    for idx_str, status_val in revision_statuses.items():
        row_num = int(idx_str) + 2  # row 1 is header
        if row_num <= ws.max_row + 1:
            ws.cell(row=row_num, column=status_col, value=status_val)

    wb.save(excel_path)


# ─────────────────────────────────────────────
# Draft — Failed Revisions Excel Viewer
# ─────────────────────────────────────────────

_DRAFT_EXCEL_KEY = f"{s3_utils.S3_PREFIX_DRAFT}/Failed_revisions.xlsx"      # shared workbook, kept in S3


@app.post("/api/draft-revisions/upload")
async def upload_draft_revisions(file: UploadFile = File(...)):
    """Upload a Failed_revisions.xlsx file for the Draft panel."""
    if not file.filename:
        raise HTTPException(400, "No file provided")
    data = await file.read()
    try:
        await asyncio.to_thread(s3_utils.put_bytes, _DRAFT_EXCEL_KEY, data,
                                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception as e:
        raise HTTPException(503, f"Could not store the file: {e}")
    return {"status": "uploaded", "filename": file.filename}


@app.get("/api/draft-revisions")
async def get_draft_revisions():
    """
    Read the Failed_revisions.xlsx and return all rows as JSON.
    Each row contains paragraph text, context, change type, etc.
    Used by the Draft button in the Visual Validator to show failed revisions
    and navigate to them in the PDF.
    """
    data = await asyncio.to_thread(s3_utils.get_bytes, _DRAFT_EXCEL_KEY)
    if data is None:
        raise HTTPException(404, "Draft Excel file not found — upload a Failed_revisions.xlsx first")

    try:
        import io as _io
        import openpyxl
        wb = openpyxl.load_workbook(_io.BytesIO(data), read_only=True, data_only=True)

        # Read ALL sheets (Inline Changes, Newline Insertions, Footnote Insertions)
        rows = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            sheet_headers = [cell.value for cell in ws[1]]
            if not sheet_headers or not any(sheet_headers):
                continue
            for row in ws.iter_rows(min_row=2, max_row=ws.max_row, values_only=True):
                row_dict = {"_sheet": sheet_name}
                for i, header in enumerate(sheet_headers):
                    if header and i < len(row):
                        key = str(header).strip()
                        row_dict[key] = row[i] if row[i] is not None else ""
                if len(row_dict) > 1:  # more than just _sheet
                    # Normalize fields from different sheets to consistent frontend keys
                    normalized = {"_sheet": sheet_name}
                    # Row Index
                    normalized["Row Index"] = row_dict.get("Row Index", row_dict.get("idx", ""))
                    # Section
                    normalized["Section"] = row_dict.get("Section", row_dict.get("section", ""))
                    # Reason
                    normalized["Reason"] = row_dict.get("Reason", row_dict.get("reason", ""))
                    # Change Type
                    normalized["Change Type"] = row_dict.get("Change Type", row_dict.get("type", sheet_name))
                    # Docx_Paragraph — use whichever text field is available
                    normalized["Docx_Paragraph"] = (
                        row_dict.get("Docx_Paragraph", "")
                        or row_dict.get("previous_para", "")
                        or row_dict.get("full_para_where_footnote_is_present", "")
                        or row_dict.get("text_before_superscript", "")
                        or ""
                    )
                    # Inserted Text
                    normalized["Inserted Text"] = (
                        row_dict.get("Inserted Text", "")
                        or row_dict.get("new_text", "")
                        or row_dict.get("footnote_text", "")
                        or ""
                    )
                    # Deleted Text
                    normalized["Deleted Text"] = row_dict.get("Deleted Text", "")
                    # Previous Context
                    normalized["Previous Context"] = (
                        row_dict.get("Previous Context", "")
                        or row_dict.get("previous_para", "")
                        or row_dict.get("text_before_superscript", "")
                        or ""
                    )
                    # Post Context
                    normalized["Post Context"] = row_dict.get("Post Context", "")
                    # Designator
                    normalized["Designator"] = row_dict.get("Designator", row_dict.get("superscript", ""))

                    # Skip rows where Reason contains 'PLEASE VERIFY'
                    reason_val = str(normalized.get("Reason", "")).upper()
                    if "PLEASE VERIFY" in reason_val:
                        continue
                    rows.append(normalized)

        wb.close()

        # Collect all unique headers across sheets
        all_headers = []
        seen = set()
        for r in rows:
            for k in r:
                if k not in seen and k != "_sheet":
                    seen.add(k)
                    all_headers.append(k)

        return {"rows": rows, "headers": all_headers, "total": len(rows)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to read Draft Excel: {str(e)}")


# Batch processing
#
# One publication's chapters, submitted together. Files arrive by multi-select
# upload, as a single zip containing them all, or from a folder in the S3
# bucket. They are grouped into per-chapter sets of docx + xml/pxml +
# images zip (NO separate pdf: this pipeline reads the chapter PDF from inside
# the images zip), the user reviews/corrects the grouping, then the chapters run
# ONE AT A TIME through the same _run_pipeline_job the single-file path uses â€”
# batch adds a queue, it does not re-implement processing, so there is no second
# orchestration to drift out of sync.
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

# The batch record (`batches[...]`, src/job_store.py) and the staged input files both live in S3
# (batches/<batch_id>/...). The folder below is only this pod's scratch copy of the inputs: any pod
# can restore a file from S3 on demand (see _batch_input_file).

def _batch_dir(batch_id: str) -> str:
    return os.path.join(tempfile.gettempdir(), "pxml_pipeline_batches", s3_utils.safe_id(batch_id))


def _batch_input_dir(batch_id: str) -> str:
    return os.path.join(_batch_dir(batch_id), "input")


def _batch_input_key(batch_id: str, name: str) -> str:
    return batches.input_prefix(batch_id) + s3_utils.safe_filename(name)


def _push_batch_input_to_s3(batch_id: str, input_dir: str) -> None:
    """Make S3 the durable home of the staged files (so a retry / another pod can find them)."""
    s3_utils.upload_directory_to_s3(input_dir, batches.input_prefix(batch_id).rstrip("/"))


def _batch_input_file(batch_id: str, name: str) -> str:
    """Local path of one staged input file, restored from S3 if this pod does not have it."""
    local = os.path.join(_batch_input_dir(batch_id), s3_utils.safe_filename(name))
    if not os.path.isfile(local):
        download_file_from_s3(_batch_input_key(batch_id, name), local)
    return local


def _stage_batch_uploads(batch_id: str, staged: list) -> str:
    """Write the uploaded files once into the batch's own input dir. If the user
    uploaded a single zip holding the whole publication, expand it and stage its
    contents instead â€” that is the "zipped & uploaded" route, and the files
    inside must be grouped exactly like individually-selected ones."""
    input_dir = _batch_input_dir(batch_id)
    os.makedirs(input_dir, exist_ok=True)

    for filename, data in staged:
        safe = s3_utils.safe_filename(filename)
        with open(os.path.join(input_dir, safe), "wb") as f:
            f.write(data)

    entries = [e for e in os.listdir(input_dir) if os.path.isfile(os.path.join(input_dir, e))]
    if len(entries) == 1 and entries[0].lower().endswith(".zip"):
        container = os.path.join(input_dir, entries[0])
        expanded = 0
        try:
            with zipfile.ZipFile(container, "r") as zf:
                members = [m for m in zf.namelist() if not m.endswith("/")]
                # A publication zip holds the chapter files; an images zip holds
                # page images + a pdf. Only expand the former, else we would
                # destroy the images zip that a chapter set needs as its own role.
                looks_like_publication = any(
                    os.path.splitext(m)[1].lower() in (".docx", ".doc", ".pxml", ".xml")
                    for m in members
                )
                if looks_like_publication:
                    for m in members:
                        target = os.path.join(input_dir, s3_utils.safe_filename(m))
                        with zf.open(m) as src, open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                    expanded = len(members)
        except zipfile.BadZipFile:
            print(f"[BATCH {batch_id}] '{entries[0]}' is not a readable zip â€” left as-is.")
        # Outside the `with`: on Windows the archive cannot be deleted while
        # ZipFile still holds it open.
        if expanded:
            os.remove(container)
            print(f"[BATCH {batch_id}] Expanded publication zip '{entries[0]}' "
                  f"({expanded} file(s))")
    return input_dir


def _plan_from_input_dir(input_dir: str) -> tuple:
    """Group whatever is staged in `input_dir` into reviewable chapter sets.

    Returns (plan, all_files) â€” `all_files` is every staged filename with a
    role-bearing extension, so the review UI can offer a per-role dropdown of
    every candidate file (not just the one the heuristic picked) and let the
    user fix a wrong pairing without re-uploading anything."""
    from canada_pipeline.batch_grouping import (
        group_files, missing_roles,
    )
    names = sorted(
        n for n in os.listdir(input_dir)
        if os.path.isfile(os.path.join(input_dir, n))
    )
    groups = group_files(names)
    plan = []
    for g in groups:
        if g.task_name == "(unclassified)":
            plan.append({
                "task_name": g.task_name, "docx": None, "xml": None, "zip": None,
                "missing": list(("docx", "xml", "zip")), "extras": g.extras,
                "confidence": 0.0, "low_confidence": False, "excluded": True,
            })
            continue
        plan.append({
            "task_name": g.task_name,
            "docx": g.docx, "xml": g.xml, "zip": g.zip,
            "missing": missing_roles(g),
            "extras": g.extras,
            "confidence": g.confidence,
            "low_confidence": bool(g.low_confidence),
            "excluded": False,
        })
    all_files = [n for n in names if os.path.splitext(n)[1].lower() in (".docx", ".doc", ".xml", ".pxml", ".zip")]
    return plan, all_files

class CoverageRequest(BaseModel):
    pxml_content: str
    input_text: str


@app.post("/api/validation/coverage")
async def coverage_pxml(request: CoverageRequest):
    """Content coverage: how much of the source text is present in the PXML output."""
    try:
        from canada_pipeline.content_check import content_coverage
        return await asyncio.to_thread(content_coverage, request.input_text, request.pxml_content)
    except Exception as e:
        print(f"[COVERAGE] Failed: {e}")
        raise HTTPException(503, f"Content check failed: {e}")


class PxmlTextRequest(BaseModel):
    pxml_content: str
    dtd_family: str = "auto"  # tag palette only: "auto" | "pax" | "pix"


class ValidateDtdRequest(BaseModel):
    # Validate the posted PXML directly; if omitted, fall back to the session's
    # saved .pxml on disk.
    pxml_content: Optional[str] = None
    session_id: Optional[str] = None
    # "both" (default) = check against every DTD family of the region and flag errors from each
    # (the document's own DTD decides pass/fail; the other family's findings are warnings);
    # "auto" = only the DOCTYPE's DTD; a family id ("pax" / "pix") forces that DTD alone.
    dtd_family: str = "both"


class FixRequest(BaseModel):
    pxml_content: str
    error_line: int
    error_msg: str
    window: int = 60


class FixAllRequest(BaseModel):
    pxml_content: str
    rule: str


@app.post("/api/validation/dtd-schema")
async def dtd_schema(request: PxmlTextRequest):
    """The auto-detected DTD's element/content-model map, for the editor's
    tag-insertion palette: which elements are valid children of which, and
    which are EMPTY (self-closing). Read live from the DTD file — never a
    hard-coded tag list — so it always matches whatever /api/validation/validate-dtd
    is validating against for the same document.
    """
    try:
        from canada_pipeline.dtd_schema import get_pxml_schema
        return await asyncio.to_thread(get_pxml_schema, request.pxml_content, request.dtd_family)
    except Exception as e:
        print(f"[DTD-SCHEMA] Failed: {e}")
        return {"available": False, "reason": str(e)}


@app.post("/api/validation/validate-dtd")
async def validate_dtd(request: ValidateDtdRequest, full: bool = False):
    """Validate PXML against the auto-detected DTD (well-formedness + structure).

    Uses the posted ``pxml_content`` if given; otherwise reads the session's saved
    ``.pxml``. Returns a summary plus a line-numbered, plain-English error list for
    the side-by-side viewer to display.
    """
    text = request.pxml_content
    if text is None and request.session_id:
        meta = await asyncio.to_thread(session_store.load_meta, request.session_id)
        if meta is None:
            raise HTTPException(404, "Session not found")
        raw = await asyncio.to_thread(session_store.get_file, request.session_id, meta.get("pxml_filename", "document.pxml"))
        if raw is None:
            raise HTTPException(404, "No PXML saved for this session")
        text = raw.decode("utf-8")
    if text is None:
        raise HTTPException(400, "Provide 'pxml_content' or 'session_id'")

    try:
        # DTD structural validation (well-formedness + DTD conformance). Fast and
        # reliable — this surfaces the structural errors (disallowed/unknown/unexpected
        # elements). The heavier full business-rule checks are opt-in via
        # ?full=1 to avoid slow validations wedging the server on large documents.
        from canada_pipeline.dtd_validation import validate_pxml_dtd, validate_pxml_full
        fn = validate_pxml_full if full else validate_pxml_dtd
        return await asyncio.to_thread(fn, text, (request.dtd_family or "auto").lower())
    except Exception as e:
        print(f"[VALIDATE] Validation failed: {e}")
        raise HTTPException(503, f"Validator unavailable: {e}")


@app.post("/api/validation/repair")
async def repair_pxml(request: PxmlTextRequest):
    """Deterministic structure repair — drop orphan close tags, close unclosed tags."""
    try:
        from canada_pipeline.xml_repair import repair_xml_structure, repair_summary
        fixed, report = await asyncio.to_thread(repair_xml_structure, request.pxml_content)
        return {"text": fixed, "summary": repair_summary(report), "report": report}
    except Exception as e:
        print(f"[REPAIR] Failed: {e}")
        raise HTTPException(503, f"Repair failed: {e}")


@app.post("/api/validation/format")
async def format_pxml(request: PxmlTextRequest):
    """Pretty-print the PXML (lxml, 2-space indent, no blank lines)."""
    try:
        from canada_pipeline.validation_extras import format_xml
        return await asyncio.to_thread(format_xml, request.pxml_content)
    except Exception as e:
        print(f"[FORMAT] Failed: {e}")
        raise HTTPException(503, f"Format failed: {e}")


@app.post("/api/validation/fix")
async def fix_pxml(request: FixRequest):
    """Ask Claude to repair the snippet around one error line. Returns a suggestion."""
    lines = request.pxml_content.splitlines(keepends=True)
    total = len(lines)
    start = max(0, request.error_line - request.window - 1)
    end = min(total, request.error_line + request.window)
    snippet = "".join(lines[start:end])

    def _run_fix():
        import anthropic as _anthropic
        import httpx as _httpx
        from canada_pipeline.validation_extras import FIX_SYSTEM_PROMPT, build_fix_prompt, extract_fix_xml
        from canada_pipeline.config import build_anthropic_client, get_primary_model, extract_text_from_message
        # build_anthropic_client fetches the TR workspace token (a real Anthropic key).
        base = build_anthropic_client(get_primary_model("processing"))
        # Force api.anthropic.com, bypassing any ANTHROPIC_BASE_URL proxy (e.g. LiteLLM)
        # that would 403 the workspace token.
        client = _anthropic.Anthropic(
            api_key=base.api_key,
            base_url="https://api.anthropic.com",
            http_client=_httpx.Client(verify=False, timeout=_httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=60.0)),
            max_retries=1,
        )
        msg = client.messages.create(
            model=get_primary_model("processing"),
            max_tokens=8192,
            system=FIX_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_fix_prompt(request.error_msg, request.error_line, snippet)}],
        )
        raw = extract_text_from_message(msg)
        # Strip any prose/markdown the model added — the suggestion is spliced straight
        # into the document, so a leaked sentence would corrupt the XML.
        return extract_fix_xml(raw)

    try:
        fixed = await asyncio.to_thread(_run_fix)
    except Exception as e:
        print(f"[FIX] LLM fix failed: {e}")
        raise HTTPException(502, f"Fix request failed: {e}")
    if not fixed or not fixed.strip():
        raise HTTPException(502, "Model returned an empty fix")
    # Guard: after sanitizing, the suggestion must be XML (start with a tag). If it is
    # not, the model returned prose we could not clean — refuse rather than corrupt the doc.
    if not fixed.lstrip().startswith("<"):
        raise HTTPException(502, "Model did not return valid XML for this error; no fix applied")
    return {"original_snippet": snippet, "suggestion": fixed, "start_line": start + 1, "end_line": end}


@app.post("/api/validation/fix-all")
async def fix_all_pxml(request: FixAllRequest):
    """Batch-fix all occurrences of a known DTD rule across the document."""
    from canada_pipeline.validation_extras import fix_all
    try:
        fixed, count = await asyncio.to_thread(fix_all, request.pxml_content, request.rule)
    except KeyError:
        raise HTTPException(400, f"No batch fix for rule: {request.rule}")
    except Exception as e:
        print(f"[FIX-ALL] Failed: {e}")
        raise HTTPException(503, f"Fix-all failed: {e}")
    return {"text": fixed, "fixes_applied": count, "rule": request.rule.upper().strip()}


@app.post("/api/batch/plan")
async def batch_plan(files: List[UploadFile] = File(...)):
    """Stage uploaded files and return the GUESSED chapter grouping for review.

    Nothing is processed here. Filenames in the wild are not consistent enough
    to pair reliably, so the response is a proposal the user confirms or edits
    before calling /api/batch/start.
    """
    batch_id = str(uuid.uuid4())[:8]
    staged = [(f.filename, await f.read()) for f in files if f and f.filename]
    if not staged:
        raise HTTPException(400, "No files uploaded")

    # Local disk I/O + zip extraction — much shorter than the S3 fetch,
    # but still blocking; off the event loop for the same reason.
    input_dir = await asyncio.to_thread(_stage_batch_uploads, batch_id, staged)
    plan, all_files = await asyncio.to_thread(_plan_from_input_dir, input_dir)
    try:
        await asyncio.to_thread(_push_batch_input_to_s3, batch_id, input_dir)
    except Exception as e:
        raise HTTPException(503, f"Could not store the batch files in S3: {e}")
    batches[batch_id] = {
        "batch_id": batch_id,
        "status": "planned",
        "source": "upload",
        "items": [],
        "all_files": all_files,
        "groups": plan,
        "created_at": time.time(),
    }
    ready = sum(1 for p in plan if not p["missing"] and not p["excluded"])
    print(f"[BATCH {batch_id}] Planned {len(plan)} group(s), {ready} ready to run.")
    return {"batch_id": batch_id, "groups": plan, "all_files": all_files}


@app.post("/api/batch/plan-s3")
async def batch_plan_s3(s3_folder: str = Form(...)):
    """Same as /api/batch/plan, but the files come from a folder in the S3 bucket
    (e.g. `incoming/Publication_6121007` or `s3://<bucket>/incoming/Publication_6121007`)
    instead of an upload."""
    from batch_s3_io import download_s3_folder, normalize_folder

    batch_id = str(uuid.uuid4())[:8]
    input_dir = _batch_input_dir(batch_id)
    os.makedirs(input_dir, exist_ok=True)
    try:
        prefix = normalize_folder(s3_folder)
        # Blocking boto3 calls: off the event loop, or this would freeze every other request
        # (health checks, other users' job-status polling) until the whole folder is fetched.
        fetched = await asyncio.to_thread(download_s3_folder, prefix, input_dir)
    except Exception as e:
        raise HTTPException(400, f"Could not read S3 folder: {e}")
    if not fetched:
        raise HTTPException(400, "No usable files found in that S3 folder")

    plan, all_files = await asyncio.to_thread(_plan_from_input_dir, input_dir)
    # Keep our own copy with the batch, so a retry still works if the source folder changes later.
    try:
        await asyncio.to_thread(_push_batch_input_to_s3, batch_id, input_dir)
    except Exception as e:
        raise HTTPException(503, f"Could not store the batch files in S3: {e}")
    batches[batch_id] = {
        "batch_id": batch_id,
        "status": "planned",
        "source": "s3",
        "s3_source_prefix": prefix,
        "items": [],
        "all_files": all_files,
        "groups": plan,
        "created_at": time.time(),
    }
    print(f"[BATCH {batch_id}] Fetched {len(fetched)} file(s) from s3://{S3_BUCKET_NAME}/{prefix}, "
          f"planned {len(plan)} group(s).")
    return {"batch_id": batch_id, "groups": plan, "all_files": all_files}


def _start_batch_item(batch_id: str, item: dict, product_type: str,
                      processing_mode: str, user_email: str, emp_id: str):
    """Submit ONE chapter through the same path /api/process uses, so job
    records, status polling, cancellation and S3 upload all behave identically."""
    job_id = str(uuid.uuid4())[:8]
    ws = _new_workspace(job_id)
    work_dir, output_dir = ws["work_dir"], ws["output_dir"]

    docx_path = os.path.join(work_dir, s3_utils.safe_filename(item["docx"]))
    pxml_path = os.path.join(work_dir, s3_utils.safe_filename(item["xml"]))
    shutil.copy2(_batch_input_file(batch_id, item["docx"]), docx_path)
    shutil.copy2(_batch_input_file(batch_id, item["xml"]), pxml_path)

    images_dir = None
    if item.get("zip"):
        images_dir = os.path.join(work_dir, "revision_images")
        os.makedirs(images_dir, exist_ok=True)
        with zipfile.ZipFile(_batch_input_file(batch_id, item["zip"]), "r") as zf:
            zf.extractall(images_dir)
        extracted = os.listdir(images_dir)
        if len(extracted) == 1 and os.path.isdir(os.path.join(images_dir, extracted[0])):
            images_dir = os.path.join(images_dir, extracted[0])

    _rec, future = _submit_job(
        job_id, file_name=item["docx"], work_dir=work_dir, output_dir=output_dir,
        docx_path=docx_path, pxml_path=pxml_path, images_dir=images_dir, product_type=product_type,
        processing_mode=processing_mode, user_email=user_email, emp_id=emp_id,
        extra={"batch_id": batch_id, "batch_task": item["task_name"]})
    return job_id, future


def _sanitize_folder_part(name: str) -> str:
    """Strip characters that are awkward in folder names (and in S3 consoles), so a
    publication name or task name is always safe to use as a key segment."""
    import re
    cleaned = re.sub(r'[~"#%&*:<>?/\\{|}]', "_", name or "").strip(" .")
    return cleaned or "untitled"


def _compute_batch_push_target(batch: dict, publication_name: str, emp_id: str) -> dict:
    """Where a completed batch's output gets published in the bucket (a tidy, human-friendly tree:
    <publication>_Output_<timestamp>/<chapter>/...).

    A batch fetched FROM an S3 folder publishes to a SIBLING folder next to that input folder --
    output lands right next to where the input came from. A batch built from a local upload/zip has
    no source folder, so it goes under published/<emp_id>/.

    Returns {} (no publish) when publication_name is blank -- the feature stays fully optional;
    every chapter's output is stored in S3 (outputs/<job_id>/) and downloadable either way.
    """
    if not publication_name.strip():
        return {}
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    root_name = f"{_sanitize_folder_part(publication_name.strip())}_Output_{timestamp}"

    src = (batch.get("s3_source_prefix") or "").rstrip("/")
    if batch.get("source") == "s3" and src:
        parent = src.rsplit("/", 1)[0] if "/" in src else ""
        publish_prefix = f"{parent}/{root_name}" if parent else root_name
    else:
        publish_prefix = f"{s3_utils.S3_PREFIX_PUBLISHED}/{s3_utils.safe_id(emp_id, 'shared')}/{root_name}"
    return {"s3_output_folder": root_name, "s3_publish_prefix": publish_prefix}


_PUSH_KEYS = ("s3_output_folder", "s3_publish_prefix")


def _batch_log(batch_ref, message: str, level: str = "info"):
    """Append one timestamped line to a batch's own event trail (and persist it).

    The driver already prints these to the server console, but that is only
    visible to whoever can read the server's stdout. Keeping the same trail on
    the batch record lets the person who actually started the batch watch it
    progress — which chapter is in flight, what finished, what failed and why —
    without needing access to the host at all (and from any pod).
    """
    batch = batch_ref if isinstance(batch_ref, dict) else batches.get(batch_ref)
    if batch is None:
        return
    batch.setdefault("log", []).append({
        "time": time.strftime("%H:%M:%S"),
        "level": level,
        "message": message,
    })
    # Bounded: a long publication's trail should never grow without limit.
    if len(batch["log"]) > 400:
        del batch["log"][:-400]
    _save_batch(batch)


def _publish_batch_item_output(batch: dict, item: dict, push: dict):
    """Publish one completed chapter's output to the batch's output folder in the bucket
    (server-side copy of outputs/<job_id>/), alongside the existing browser download.
    Best-effort: a failure here must not fail the chapter or the batch -- the chapter's
    output already sits in outputs/<job_id>/ and stays downloadable regardless."""
    batch_id = batch["batch_id"]
    job_id = item.get("job_id", "")
    if not job_id:
        item["s3_error"] = "No job output to publish"
        return
    try:
        dest = f"{push['s3_publish_prefix']}/{_sanitize_folder_part(item['task_name'])}"
        copied = s3_utils.copy_prefix(f"{S3_PREFIX_OUTPUTS}/{s3_utils.safe_id(job_id)}", dest)
        item["s3_published"] = len(copied)
        item["s3_location"] = f"s3://{S3_BUCKET_NAME}/{dest}/"
        if copied:
            item["s3_error"] = ""
            _batch_log(batch, f"{item['task_name']}: output published to {item['s3_location']}")
        else:
            item["s3_error"] = "No output files were found for this chapter"
            _batch_log(batch, f"{item['task_name']}: publish failed — {item['s3_error']}", "warning")
        print(f"[BATCH {batch_id}] Published: {item['task_name']} -> {dest} ({len(copied)} file(s))")
    except Exception as e:
        item["s3_error"] = str(e)
        _batch_log(batch, f"{item['task_name']}: publish failed — {e}", "warning")
        print(f"[BATCH {batch_id}] Publish failed for {item['task_name']}: {e}")


def _copy_job_outcome(item: dict, job: dict) -> None:
    """Carry the job's failure (and any validation problem) onto a batch row in a form the UI can act on:
    a plain-language category + hint + whether re-running is sensible (see canada_pipeline/error_info.py).
    The final progress fields are copied too, so listing a finished batch never needs the job records."""
    item["error"] = job.get("error", "")
    item["error_info"] = job.get("error_info") or None
    item["validation_error"] = job.get("validation_error", "")
    item["validation_error_info"] = job.get("validation_error_info") or None
    for k in ("current_step_name", "progress_pct", "current_step", "total_steps"):
        if k in job:
            item[k] = job[k]


def _validation_note(item: dict) -> str:
    """A finished chapter whose automatic validation did not run is 'completed' but not fully checked."""
    info = item.get("validation_error_info")
    return f" Validation did not run ({info['title']})." if info else ""


def _batch_cancel_key(batch_id: str) -> str:
    return f"{s3_utils.S3_PREFIX_BATCHES}/{s3_utils.safe_id(batch_id)}/cancel.flag"


def _batch_cancel_requested(batch: dict) -> bool:
    """A 'stop' click may land on a different pod than the one driving the batch, so the flag is in S3."""
    return bool(batch.get("cancel_requested")) or s3_utils.object_exists(_batch_cancel_key(batch["batch_id"]))


def _run_batch_driver(batch_id: str, items: list, product_type: str,
                      processing_mode: str, user_email: str, emp_id: str):
    """Run the batch's chapters strictly one at a time.

    Sequential by design: the pipeline is LLM-bound, and running chapters
    concurrently multiplies rate-limit pressure while making logs interleave.
    Each chapter is awaited before the next is submitted; a failure is recorded
    and the batch continues with the remaining chapters.
    """
    batch = batches[batch_id]
    push = {k: batch.get(k) for k in _PUSH_KEYS}
    _batch_log(batch, f"Batch started: {len(items)} chapter(s) queued.")
    for item in items:
        if _batch_cancel_requested(batch):
            item["status"] = "skipped"
            _batch_log(batch, f"{item['task_name']}: skipped (stop requested).", "warning")
            continue
        item["status"] = "running"
        _save_batch(batch)
        try:
            job_id, future = _start_batch_item(
                batch_id, item, product_type, processing_mode, user_email, emp_id)
            item["job_id"] = job_id
            _batch_log(batch, f"{item['task_name']}: submitting… (job {job_id})")
            print(f"[BATCH {batch_id}] â–¶ {item['task_name']} -> job {job_id}")
            future.result()  # raw pipeline steps done -- NOT the whole chapter yet
            # _on_job_done (fired by the future above) kicks off auto-validation
            # as a SEPARATE executor task and deliberately leaves the job
            # "running" until that finishes. Without waiting here too, this
            # loop would submit the NEXT chapter's pipeline while this one's
            # validation is still making its own LLM calls -- exactly the
            # concurrent LLM load "one at a time" exists to avoid, and it's
            # why the batch table could show more than one chapter "running"
            # simultaneously.
            while jobs.get(job_id, {}).get("status") == "running":
                time.sleep(1)
            job = jobs.get(job_id, {})
            item["status"] = job.get("status", "completed")
            _copy_job_outcome(item, job)
            item["output_dir"] = job.get("output_dir", "")
            if item["status"] == "completed":
                _batch_log(batch, f"{item['task_name']}: completed." + _validation_note(item),
                           "warning" if item.get("validation_error_info") else "info")
            else:
                _batch_log(batch,
                           f"{item['task_name']}: {item['status']} — "
                           f"{(item.get('error_info') or {}).get('title') or item.get('error') or 'no error message from backend'}"
                           f"{(' — ' + item['error_info']['hint']) if item.get('error_info') else ''} "
                           f"Moving to next chapter.", "warning")
            if item["status"] == "completed" and push.get("s3_publish_prefix"):
                _publish_batch_item_output(batch, item, push)
        except Exception as e:
            item["status"] = "failed"
            item["error"] = str(e)
            from canada_pipeline.error_info import classify_error
            item["error_info"] = classify_error(e)
            _batch_log(batch, f"{item['task_name']}: failed — {e}. Moving to next chapter.", "warning")
            print(f"[BATCH {batch_id}]✗ {item['task_name']} failed: {e}")
        _save_batch(batch)
        print(f"[BATCH {batch_id}] â–Â  {item['task_name']}: {item['status']}")

    batch["status"] = "cancelled" if _batch_cancel_requested(batch) else "completed"
    batch["completed_at"] = time.time()
    done = sum(1 for i in items if i["status"] == "completed")
    _batch_log(batch, f"Batch {batch['status']}: {done}/{len(items)} chapter(s) completed.")
    print(f"[BATCH {batch_id}] Finished: {done}/{len(items)} chapter(s) completed.")
    # The staged inputs are safe in S3 (a retry restores what it needs); free this pod's scratch copy.
    shutil.rmtree(_batch_dir(batch_id), ignore_errors=True)


@app.post("/api/batch/{batch_id}/start")
async def batch_start(
    batch_id: str,
    groups: str = Form(...),
    product_type: str = Form(_REGION.default_product),
    processing_mode: str = Form("chapter"),
    user_email: str = Form(""),
    emp_id: str = Form(""),
    publication_name: str = Form(""),
):
    """Run the reviewed plan. `groups` is the confirmed JSON from /api/batch/plan
    (possibly edited by the user), so what runs is what they approved.

    `publication_name`, if given, also names the output folder this run is published to in the
    bucket -- "<publication_name>_Output_<timestamp>", one subfolder per chapter inside it.
    Left blank, chapters are only retrievable via the existing per-chapter browser download."""
    batch = await asyncio.to_thread(batches.get, batch_id)
    if batch is None:
        raise HTTPException(404, "Batch not found")
    if batch["status"] == "running":
        raise HTTPException(400, "Batch already running")

    try:
        plan = _json.loads(groups)
    except _json.JSONDecodeError as e:
        raise HTTPException(400, f"Invalid groups JSON: {e}")

    items = []
    for g in plan:
        if g.get("excluded"):
            continue
        missing = [r for r in ("docx", "xml", "zip") if not g.get(r)]
        if missing:
            raise HTTPException(
                400,
                f"'{g.get('task_name')}' is missing {missing} — every included chapter "
                f"needs a docx, an xml/pxml and an images zip.",
            )
        items.append({
            "task_name": g.get("task_name") or g["docx"],
            "docx": g["docx"], "xml": g["xml"], "zip": g["zip"],
            "status": "queued", "job_id": "", "error": "", "output_dir": "",
        })
    if not items:
        raise HTTPException(400, "No chapters selected to run")

    push = await asyncio.to_thread(_compute_batch_push_target, batch, publication_name, emp_id)
    s3_output_folder = push.get("s3_output_folder", "")

    # run_opts are kept so a later single-chapter retry reruns that chapter with
    # exactly the settings the batch was started with, rather than defaults.
    batch.update({"status": "running", "items": items, "cancel_requested": False,
                  "started_at": time.time(), "product_type": product_type,
                  "run_opts": {"product_type": product_type, "processing_mode": processing_mode,
                               "user_email": user_email, "emp_id": emp_id},
                  **push})
    await asyncio.to_thread(s3_utils.delete_object, _batch_cancel_key(batch_id))   # a stop from an earlier run
    batches.adopt(batch)                     # this pod now drives the batch (and heartbeats for it)
    await asyncio.to_thread(_save_batch, batch)
    _thr_mod.Thread(
        target=_run_batch_driver,
        args=(batch_id, items, product_type, processing_mode, user_email, emp_id),
        name=f"BatchDriver-{batch_id}", daemon=True,
    ).start()
    print(f"[BATCH {batch_id}] Started — {len(items)} chapter(s), sequential."
          + (f" Output folder: {s3_output_folder}" if s3_output_folder else ""))
    return {"batch_id": batch_id, "status": "running", "items": items, "s3_output_folder": s3_output_folder}


def _live_job_status(job_id: str, job: dict) -> dict:
    """Live progress of a running job: the worker's file if this pod runs it, else the copy in S3."""
    if jobs.owned(job_id) and job.get("output_dir"):
        return _read_job_status(job["output_dir"])
    return job_store.read_status(job_id)


@app.get("/api/batch/{batch_id}")
async def batch_status(batch_id: str):
    """Progress for the whole batch. Each item carries its job_id, so the
    existing /api/status/{job_id} and /api/log/{job_id} work per chapter."""
    batch = await asyncio.to_thread(batches.get, batch_id)
    if batch is None:
        raise HTTPException(404, "Batch not found")

    def _build_items() -> list:
        out = []
        for item in batch.get("items", []):
            row = {**item}
            # The pipeline runs in a separate worker PROCESS (ProcessPoolExecutor), possibly on another
            # pod, so progress is read from the job's live status (refreshed on every poll). Finished
            # chapters carry their final numbers already (see _copy_job_outcome), so only the one
            # chapter in flight costs a lookup.
            if item.get("status") == "running" and item.get("job_id"):
                job = jobs.get(item["job_id"]) or {}
                live = _live_job_status(item["job_id"], job) if job.get("status") == "running" else {}
                row["current_step"] = live.get("current_step", job.get("current_step", 0))
                row["current_step_name"] = live.get("current_step_name", job.get("current_step_name", ""))
                row["progress_pct"] = live.get("progress_pct", job.get("progress_pct", 0))
                row["total_steps"] = job.get("total_steps", 0)
            row.setdefault("current_step_name", "")
            row.setdefault("progress_pct", 0)
            row.setdefault("current_step", 0)
            row.setdefault("total_steps", 0)
            row["revalidating"] = False
            out.append(row)
        return out

    items = await asyncio.to_thread(_build_items)
    return {
        "batch_id": batch_id,
        "status": batch["status"],
        "source": batch.get("source", ""),
        "items": items,
        "completed": sum(1 for i in items if i.get("status") == "completed"),
        "total": len(items),
        "s3_output_folder": batch.get("s3_output_folder", ""),
        # Only meaningful while status == "planned" -- lets a browser refresh
        # restore the review table too, not just a batch that already started.
        # The staged files are already durably in S3 (written at plan time), so
        # there is nothing here that a refresh -- or a pod restart -- could make unresumable.
        "groups": batch.get("groups", []),
        "all_files": batch.get("all_files", []),
        "log": batch.get("log", []),
    }


@app.post("/api/batch/{batch_id}/cancel")
async def batch_cancel(batch_id: str):
    """Stop after the current chapter: the running one is cancelled through the
    normal per-job cancel flag, and queued chapters are skipped."""
    batch = await asyncio.to_thread(batches.get, batch_id)
    if batch is None:
        raise HTTPException(404, "Batch not found")
    batch["cancel_requested"] = True
    # In S3 so the pod that is driving the batch sees it even if another pod got the click.
    await asyncio.to_thread(s3_utils.put_bytes, _batch_cancel_key(batch_id), b"cancel")
    for item in batch.get("items", []):
        if item.get("status") == "running" and item.get("job_id"):
            try:
                await cancel_job(item["job_id"])
            except HTTPException:
                pass
    return {"batch_id": batch_id, "status": "cancelling"}


def _run_batch_retry(batch_id: str, item: dict):
    """Re-run ONE chapter of an already-finished batch.

    Deliberately mirrors the driver's own per-chapter sequence (submit -> wait
    for the job to actually leave "running", i.e. including its validation pass
    -> publish) rather than reusing _run_batch_driver, so it cannot
    overwrite the batch's own completion bookkeeping for the other chapters.
    """
    batch = batches[batch_id]
    opts = batch.get("run_opts", {})
    _batch_log(batch, f"{item['task_name']}: retry requested.")
    try:
        job_id, future = _start_batch_item(
            batch_id, item, opts.get("product_type", _REGION.default_product),
            opts.get("processing_mode", "chapter"),
            opts.get("user_email", ""), opts.get("emp_id", ""))
        item["job_id"] = job_id
        _save_batch(batch)
        _batch_log(batch, f"{item['task_name']}: resubmitted (job {job_id}).")
        future.result()
        while jobs.get(job_id, {}).get("status") == "running":
            time.sleep(1)
        job = jobs.get(job_id, {})
        item["status"] = job.get("status", "completed")
        _copy_job_outcome(item, job)
        item["output_dir"] = job.get("output_dir", "")
        if item["status"] == "completed":
            _batch_log(batch, f"{item['task_name']}: completed on retry.")
            push = {k: batch.get(k) for k in _PUSH_KEYS}
            if push.get("s3_publish_prefix"):
                _publish_batch_item_output(batch, item, push)
        else:
            _batch_log(batch,
                       f"{item['task_name']}: retry {item['status']} — "
                       f"{item.get('error') or 'no error message from backend'}.", "warning")
    except Exception as e:
        item["status"] = "failed"
        item["error"] = str(e)
        from canada_pipeline.error_info import classify_error
        item["error_info"] = classify_error(e)
        _batch_log(batch, f"{item['task_name']}: retry failed — {e}", "warning")
    finally:
        batch["status"] = "cancelled" if _batch_cancel_requested(batch) else "completed"
        _save_batch(batch)
        shutil.rmtree(_batch_dir(batch_id), ignore_errors=True)


@app.post("/api/batch/{batch_id}/retry/{task_name}")
async def batch_retry_chapter(batch_id: str, task_name: str):
    """Re-run one chapter that failed, without re-running the rest of the batch.

    Costs a fresh pipeline run for that chapter (there is no way to know the
    outcome without actually processing it), so this is always an explicit,
    manual action -- never automatic. Allowed only once the batch is no longer
    running, so a retry can never execute alongside the driver's current
    chapter and break the one-at-a-time guarantee the whole design rests on.
    """
    batch = await asyncio.to_thread(batches.get, batch_id)
    if batch is None:
        raise HTTPException(404, "Batch not found")
    if batch.get("status") == "running":
        raise HTTPException(400, "This batch is still running — wait for it to finish, then retry.")
    item = next((i for i in batch.get("items", []) if i.get("task_name") == task_name), None)
    if item is None:
        raise HTTPException(404, f"No chapter named '{task_name}' in this batch")

    for role in ("docx", "xml", "zip"):
        name = item.get(role)
        if not name or not await asyncio.to_thread(s3_utils.object_exists, _batch_input_key(batch_id, name)):
            raise HTTPException(
                400,
                f"Cannot retry '{task_name}': its {role} file is no longer stored with this batch. "
                f"Start a new batch with these files instead.",
            )

    item.update({"status": "running", "error": "", "job_id": "", "output_dir": "",
                 "s3_published": 0, "s3_error": "", "s3_location": ""})
    batch["status"] = "running"
    await asyncio.to_thread(s3_utils.delete_object, _batch_cancel_key(batch_id))
    batch["cancel_requested"] = False
    batches.adopt(batch)                     # this pod drives the retry
    await asyncio.to_thread(_save_batch, batch)
    _thr_mod.Thread(target=_run_batch_retry, args=(batch_id, item),
                    name=f"BatchRetry-{batch_id}", daemon=True).start()
    return {"batch_id": batch_id, "task_name": task_name, "status": "running"}


@app.post("/api/batch/{batch_id}/retry-push/{task_name}")
async def batch_retry_push(batch_id: str, task_name: str):
    """Retry publishing one already-completed chapter's output to the batch's output folder --
    safe to click any time, since it only re-copies to the same destination
    and never touches the chapter's stored output or download link."""
    batch = await asyncio.to_thread(batches.get, batch_id)
    if batch is None:
        raise HTTPException(404, "Batch not found")
    if not batch.get("s3_publish_prefix"):
        raise HTTPException(400, "This batch has no output folder configured")
    item = next((i for i in batch.get("items", []) if i.get("task_name") == task_name), None)
    if item is None:
        raise HTTPException(404, f"No chapter named '{task_name}' in this batch")
    if item.get("status") != "completed":
        raise HTTPException(400, f"'{task_name}' has not completed yet (status: {item.get('status')})")

    push = {k: batch.get(k) for k in _PUSH_KEYS}
    await asyncio.to_thread(_publish_batch_item_output, batch, item, push)
    await asyncio.to_thread(_save_batch, batch)
    return {"task_name": task_name, "s3_published": item.get("s3_published", 0),
            "s3_error": item.get("s3_error", ""), "s3_location": item.get("s3_location", "")}


# ─────────────────────────────────────────────
# Routers split out of this file (see api/): feedback, agents, developer page, xml tools
# ─────────────────────────────────────────────
from api import feedback as _api_feedback, agents as _api_agents, dev as _api_dev, xmltools as _api_xmltools
for _r in (_api_feedback.router, _api_agents.router, _api_dev.router, _api_xmltools.router):
    app.include_router(_r)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, workers=1)
