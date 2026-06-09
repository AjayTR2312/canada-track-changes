"""
FastAPI backend for PXML Document Processor Pipeline.
Uses ProcessPoolExecutor (6 workers) for concurrency and S3 for file storage.
Designed for AWS EC2 deployment.
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
from typing import Optional, Dict
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from s3_utils import (
    upload_file_to_s3, upload_job_inputs, upload_job_outputs,
    download_file_from_s3, download_directory_from_s3,
    generate_presigned_url, list_output_files,
    S3_PREFIX_UPLOADS, S3_PREFIX_OUTPUTS, S3_BUCKET_NAME,
)

# ─────────────────────────────────────────────
# App Setup
# ─────────────────────────────────────────────
app = FastAPI(title="PXML Document Processor API", version="3.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ─────────────────────────────────────────────
# ExecutorPool — 6 workers for concurrent pipeline jobs
# ─────────────────────────────────────────────
MAX_WORKERS = int(os.getenv("PIPELINE_MAX_WORKERS", "6"))
executor = ProcessPoolExecutor(max_workers=MAX_WORKERS)

# ─────────────────────────────────────────────
# In-memory job store
# ─────────────────────────────────────────────
jobs: Dict[str, dict] = {}

PIPELINE_STEP_NAMES = {
    1: "Track Changes Extraction",
    2: "Section Deletion",
    3: "Section Number Replacement",
    4: "Section Insertion & Edge Cases",
    5: "Paragraph-Level Inline Changes",
    6: "Newline Paragraph Insertions",
    7: "New Footnote Insertions",
    8: "Failed Revisions Report",
    9: "Retry Failed Deletions",
    10: "Post Processing",
}


# ─────────────────────────────────────────────
# Status file helper (worker process writes, API reads)
# ─────────────────────────────────────────────
def _write_job_status(output_dir: str, step: int, step_name: str, progress: int, status: str = "running", error: str = ""):
    """Write job status to a JSON file so the API can read live progress from any process."""
    import json
    status_file = os.path.join(output_dir, "_job_status.json")
    data = {
        "current_step": step,
        "current_step_name": step_name,
        "progress_pct": progress,
        "status": status,
        "error": error,
    }
    try:
        with open(status_file, 'w', encoding='utf-8') as f:
            json.dump(data, f)
    except:
        pass


def _read_job_status(output_dir: str) -> dict:
    """Read live job status from the worker's status file."""
    import json
    status_file = os.path.join(output_dir, "_job_status.json")
    if os.path.isfile(status_file):
        try:
            with open(status_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return {}


# ─────────────────────────────────────────────
# Pipeline runner (runs in executor worker process)
# ─────────────────────────────────────────────
def _run_pipeline_job(job_id: str, docx_path: str, pxml_path: str, output_dir: str, product_type: str, images_dir: str = None):
    """Execute the full pipeline in a worker process. Uploads outputs to S3 on completion."""
    # Ensure project directory is in sys.path for imports
    project_dir = os.path.dirname(os.path.abspath(__file__))
    if project_dir not in sys.path:
        sys.path.insert(0, project_dir)
    os.chdir(project_dir)

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
        import continous_ncont_insertions
        import newline_insertions
        import insertions_paragraphs
        from section_name_edge_cases import (
            remaining_insertions_df, create_edgecase_section_dict,
            extract_sectionedgecase_key, update_designator_by_name_match
        )
        from finaloutputs.chapter_section import extract_designator_pattern_using_llm
        from full_section_insertion_prevendor import PXMLInserter
        from inline_replacement_section import SectionLineReplacement
        from new_footnotes_insertions import process_all_footnote_insertions
        from section_insertion import SectionExtractor
        from track_changes_df import process_docx
        from section_deletion import section_deletion_filling
        from config import API_KEY, MODEL_OPUS, MODEL_SONNET, build_anthropic_client as _build_client_config
        from section_split_ import handle_section_split
        from postprocessing import postprocessing_fortags
        from paragraph_level_trackchanges import parse_pxml_sections_from_str_new, process_with_llm
        from footnotes_level_trackchanges import process_with_llm_footnotes
        from inline_footnotes_insertion import (
            process_all_footnotes_with_llm, parse_pxml_sections_from_str_dict_positions, build_anthropic_client
        )
        from newline_insertions_new import return_closest_previous_par, process_all_newline_insertions_with_llm
        from sectionnumber_filling import fill_sections_backward_verbose
        from revisions_images import extract_page_texts
        from failed_revisions_handler import retry_failed_inline_deletions
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

        from superscript_extraction_from_pdf import convert_docx_to_pdf, process_excel as process_superscripts

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

        # Move the PDF from images folder to output dir (validation needs it later)
        if _images_pdf_path and os.path.isfile(_images_pdf_path):
            try:
                _pdf_dest = os.path.join(output_dir, os.path.basename(_images_pdf_path))
                shutil.move(_images_pdf_path, _pdf_dest)
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

        # PIPELINE 2
        _write_job_status(output_dir, 2, "Section Deletion", 20)
        print(f"\n{'='*80}\nPIPELINE-2: Section Deletion\n{'='*80}")
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
                    updated_pxmlcontent = update_designator_by_name_match(updated_pxmlcontent, section_text, section_number)
                    edgecase_processed_indexes.append(int(con['paragraph_index']))
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
                    desig_pattern_re = re.compile(r'^§?\s*\d+' + separator + r'\d+', re.IGNORECASE)
                    rows_after = result_with_ba[result_with_ba['idx'] > start_pi_check].drop_duplicates(subset=['idx'], keep='first').sort_values('idx')
                    checked_rows = []
                    for _, row in rows_after.iterrows():
                        row_text = str(row.get('text', '')).strip()
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
            (all_revisions_except_newline_insertions_new['has_changes'] == True) & (all_revisions_except_newline_insertions_new['is_footnote_x'] == False)
        ]
        all_revisions_except_newline_insertions1 = fill_sections_backward_verbose(
            all_revisions_except_newline_insertions1, no_changes_df, inline_replacement_df, inline_insertion_df, chapter_number,
            verbose=True, client=client, model_name=model_name, designator_patterns=designator_patterns
        )

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
        updated_pxmlcontent, processed_idxs_footnotes, failed_inline_changes_footnotes = process_with_llm_footnotes(
            result_df=all_revisions_except_newline_insertions_footnotes, section_dict=section_dict,
            pxml_content=updated_pxmlcontent, client=client, failed_log_path=failed_log_path,
            match_threshold=0.5, model_name=model_name, chapter_number=chapter_number,
            page_texts=page_texts, image_paths=image_paths,
        )
        failed_inline_changes = (failed_inline_changes_main or []) + (failed_inline_changes_footnotes or [])

        # PIPELINE 6
        _write_job_status(output_dir, 6, "Newline Paragraph Insertions", 60)
        print("\n[PIPELINE 6] Newline paragraph insertions...")
        all_newline_insertions = result_with_ba_new[
            (result_with_ba_new['text_before'] == '') & (result_with_ba_new['has_changes'] == True) & (result_with_ba_new['is_footnote_x'] == False)
        ].drop_duplicates(subset=['idx'], keep='first')
        all_newline_insertions = all_newline_insertions[['idx', 'text', 'has_changes', 'type', 'paragraph_index']]
        all_newline_insertions = return_closest_previous_par(result_with_ba, all_newline_insertions)
        all_newline_insertions_sections = fill_sections_backward_verbose(
            all_newline_insertions, no_changes_df, inline_replacement_df, inline_insertion_df, chapter_number,
            verbose=True, client=client, model_name=model_name, designator_patterns=designator_patterns
        )

        section_dict, section_positions, pxml_content = parse_pxml_sections_from_str_dict_positions(updated_pxmlcontent)
        updated_pxmlcontent, failed_newline_insertions = process_all_newline_insertions_with_llm(
            pxml_content=pxml_content, section_dict=section_dict, section_positions=section_positions,
            all_newline_insertions=all_newline_insertions_sections, client=client, model_name=model_name,
            failed_log_path=os.path.join(output_dir, "failed_newline_insertions.txt"),
        )

        # PIPELINE 7
        all_inserted_footnote_revisions = result_with_ba[
            (result_with_ba['is_footnote_x'] == True) & (result_with_ba['has_changes'] == True) & (result_with_ba['text_before'] == '')
        ].drop_duplicates(subset=['idx'], keep='first')
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
        updated_pxmlcontent, failed_footnote_insertions = process_all_footnote_insertions(
            all_inserted_footnote_revisions=all_inserted_footnote_revisions_sections,
            pxml_content=pxml_content, model_name=MODEL_OPUS,
            failed_log_path=os.path.join(output_dir, "failed_footnote_insertions.txt")
        )

        final_pxml_content = updated_pxmlcontent
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(final_pxml_content)

        # PIPELINE 8: Failed Revisions Report
        _write_job_status(output_dir, 8, "Failed Revisions Report", 80)
        print(f"\n{'='*80}\nWriting Consolidated Failed Revisions Excel...\n{'='*80}")
        consolidated_excel_path = os.path.join(output_dir, "Failed_revisions.xlsx")
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

        # PIPELINE 9
        _write_job_status(output_dir, 9, "Retry Failed Deletions", 90)
        final_pxml_content, retry_success_count, retry_total, succeeded_indices, retry_rows_list = retry_failed_inline_deletions(
            failed_inline_changes=failed_inline_changes, pxml_content=final_pxml_content,
            client=client, model_name=model_name, chapter_number=chapter_number,
            page_texts=page_texts, image_paths=image_paths,
        )
        if retry_success_count > 0:
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(final_pxml_content)

        # PIPELINE 10
        _write_job_status(output_dir, 10, "Post Processing", 95)
        print(f"\n{'='*80}\nPIPELINE-10: Post Processing (Final)\n{'='*80}")
        with open(os.path.join(output_dir, 'save_before_postprocessing.pxml'), 'w', encoding='utf-8') as f:
            f.write(final_pxml_content)
        final_pxml_content = postprocessing_fortags(final_pxml_content, product_type=PRODUCT_TYPE)
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(final_pxml_content)

        end_time = time.time()
        print(f"\n✅ Output saved to: {output_file}")
        print(f"Total runtime: {end_time - start_time:.2f} seconds")

        # Upload outputs to S3 (optional)
        try:
            print(f"\n[S3] Uploading outputs to S3...")
            upload_job_outputs(job_id, output_dir)
            print(f"[S3] Upload complete.")
        except Exception as s3_err:
            print(f"[S3] Upload skipped (no credentials): {s3_err}")

        return {"status": "completed", "output_file": output_file, "job_id": job_id}

    except Exception as e:
        print(f"\n❌ PIPELINE FAILED: {e}")
        try:
            upload_job_outputs(job_id, output_dir)
        except:
            pass
        return {"status": "failed", "error": str(e), "job_id": job_id}

    finally:
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
        log_f.close()


# ─────────────────────────────────────────────
# Callback when executor future completes
# ─────────────────────────────────────────────
def _on_job_done(job_id: str, future: Future):
    try:
        result = future.result()
        if result and result.get("status") == "completed":
            # Set output_file BEFORE status to avoid race condition where
            # status is "completed" but output_file is still empty
            jobs[job_id]["output_file"] = result.get("output_file", "")
            jobs[job_id]["progress_pct"] = 100
            jobs[job_id]["current_step"] = 10
            jobs[job_id]["current_step_name"] = "Completed"
            jobs[job_id]["completed_at"] = time.time()
            jobs[job_id]["status"] = "completed"  # Set LAST so all fields are ready
        else:
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["error"] = result.get("error", "Unknown") if result else "No result"
    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = str(e)


# ─────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────
@app.get("/api/health")
async def health_check():
    return {"status": "healthy", "workers": MAX_WORKERS, "active_jobs": len([j for j in jobs.values() if j.get("status") == "running"])}


@app.post("/api/process")
async def process_files(
    docx_file: UploadFile = File(...),
    pxml_file: UploadFile = File(...),
    images_zip: Optional[UploadFile] = File(None),
    product_type: str = Form("looseleaf"),
):
    """Upload files and start processing. Returns job_id for polling."""
    job_id = str(uuid.uuid4())[:8]
    work_dir = os.path.join(tempfile.gettempdir(), "pxml_pipeline_jobs", job_id)
    os.makedirs(work_dir, exist_ok=True)

    docx_path = os.path.join(work_dir, docx_file.filename)
    pxml_path = os.path.join(work_dir, pxml_file.filename)
    with open(docx_path, "wb") as f:
        f.write(await docx_file.read())
    with open(pxml_path, "wb") as f:
        f.write(await pxml_file.read())

    images_dir = None
    if images_zip and images_zip.filename:
        images_dir = os.path.join(work_dir, "revision_images")
        os.makedirs(images_dir, exist_ok=True)
        zip_path = os.path.join(work_dir, images_zip.filename)
        with open(zip_path, "wb") as f:
            f.write(await images_zip.read())
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(images_dir)
        os.remove(zip_path)
        extracted_items = os.listdir(images_dir)
        if len(extracted_items) == 1 and os.path.isdir(os.path.join(images_dir, extracted_items[0])):
            images_dir = os.path.join(images_dir, extracted_items[0])

    output_dir = os.path.join(work_dir, "output")
    os.makedirs(output_dir, exist_ok=True)

    # Upload inputs to S3 (optional — skips if no credentials)
    try:
        upload_job_inputs(job_id, docx_path, pxml_path, images_dir)
    except Exception as e:
        print(f"[S3] Skipping input upload (no credentials or S3 error): {e}")

    # Create job record
    jobs[job_id] = {
        "job_id": job_id,
        "status": "running",
        "current_step": 0,
        "current_step_name": "Starting...",
        "total_steps": 10,
        "progress_pct": 0,
        "file_name": docx_file.filename,
        "output_dir": output_dir,
        "output_file": "",
        "error": "",
        "log": "",
        "started_at": time.time(),
        "completed_at": None,
        "s3_output_prefix": f"{S3_PREFIX_OUTPUTS}/{job_id}",
    }

    # Submit to ProcessPoolExecutor (6 workers)
    future = executor.submit(_run_pipeline_job, job_id, docx_path, pxml_path, output_dir, product_type, images_dir)
    future.add_done_callback(lambda f: _on_job_done(job_id, f))

    return {"job_id": job_id, "status": "running"}


@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    job = jobs[job_id]
    # Read live progress from worker's status file
    if job["status"] == "running" and job.get("output_dir"):
        live = _read_job_status(job["output_dir"])
        if live:
            job["current_step"] = live.get("current_step", job["current_step"])
            job["current_step_name"] = live.get("current_step_name", job["current_step_name"])
            job["progress_pct"] = live.get("progress_pct", job["progress_pct"])
    return job


@app.get("/api/download/{job_id}")
async def download_output(job_id: str):
    """Download output as ZIP."""
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    job = jobs[job_id]
    if job["status"] != "completed":
        raise HTTPException(400, f"Job not complete. Status: {job['status']}")

    output_dir = job["output_dir"]
    # Build download filename: job_id + truncated original filename
    raw_name = os.path.splitext(job.get("file_name", "output"))[0]
    short_name = raw_name[:40] if len(raw_name) > 40 else raw_name
    download_filename = f"{job_id}_{short_name}.zip"

    zip_path = os.path.join(tempfile.gettempdir(), f"{job_id}_output.zip")
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(output_dir):
            for file in files:
                filepath = os.path.join(root, file)
                arcname = os.path.relpath(filepath, output_dir)
                zf.write(filepath, arcname)
    return FileResponse(zip_path, filename=download_filename, media_type="application/zip")


@app.get("/api/download-s3/{job_id}/{filename:path}")
async def download_from_s3(job_id: str, filename: str):
    """Redirect to presigned S3 URL for a specific output file."""
    s3_key = f"{S3_PREFIX_OUTPUTS}/{job_id}/{filename}"
    try:
        url = generate_presigned_url(s3_key)
        return RedirectResponse(url=url)
    except Exception as e:
        raise HTTPException(404, f"File not found in S3: {e}")


@app.get("/api/list-outputs/{job_id}")
async def list_outputs(job_id: str):
    """List all output files for a job from S3."""
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    files = list_output_files(job_id)
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

    from exe_track_changes_1 import run_validation_for_gui
    from s3_utils import upload_file_to_s3

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

    # Upload to S3
    upload_file_to_s3(excel_path, f"{S3_PREFIX_OUTPUTS}/{job_id}/{os.path.basename(excel_path)}")
    return excel_path


@app.post("/api/validate/{job_id}")
async def run_validation(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    job = jobs[job_id]
    if job["status"] != "completed":
        raise HTTPException(400, "Pipeline must complete before validation")

    output_file = job.get("output_file", "")
    output_dir = job.get("output_dir", "")

    # Validate that output_dir exists
    if not output_dir or not os.path.isdir(output_dir):
        raise HTTPException(400, f"Output directory not found: '{output_dir}'")

    # If output_file is empty, try to discover it before submitting
    if not output_file or not os.path.isfile(output_file):
        try:
            discovered = list(Path(output_dir).glob("*_final.pxml"))
            if discovered:
                output_file = str(max(discovered, key=lambda p: p.stat().st_mtime))
                job["output_file"] = output_file  # Update job record
            else:
                raise HTTPException(400, "Output PXML file not found. Pipeline may not have completed successfully.")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, f"Failed to discover output file: {e}")
    
    # Final validation: ensure output_file exists before passing to executor
    if not output_file or not os.path.isfile(output_file):
        raise HTTPException(400, f"Output PXML file does not exist: '{output_file}'")

    try:
        loop = asyncio.get_event_loop()
        excel_path = await loop.run_in_executor(
            executor,
            _run_validation_sync,
            job_id,
            output_dir,
            output_file,
        )
        return {"status": "completed", "excel_path": excel_path}
    except Exception as e:
        raise HTTPException(500, f"Validation failed: {e}")


@app.get("/api/download-validation/{job_id}")
async def download_validation(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    job = jobs[job_id]
    excel_files = list(Path(job["output_dir"]).glob("final_validation_report_*.xlsx"))
    if not excel_files:
        raise HTTPException(404, "Validation report not found.")
    # Build download filename: ValidationReport + truncated original filename
    raw_name = os.path.splitext(job.get("file_name", "output"))[0]
    short_name = raw_name[:40] if len(raw_name) > 40 else raw_name
    download_filename = f"ValidationReport_{short_name}.xlsx"
    return FileResponse(str(excel_files[0]), filename=download_filename, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/log/{job_id}")
async def get_log(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    log_path = os.path.join(jobs[job_id]["output_dir"], "pipeline_log.txt")
    if os.path.isfile(log_path):
        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            return {"log": f.read()[-50000:]}
    return {"log": ""}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, workers=1)
