import streamlit as st
import os
import sys
import io
import time
import threading
import queue
import tempfile
import shutil
import re
import anthropic
import pandas as pd
from canada_pipeline.chapter_section import extract_designator_pattern_using_llm
from full_section_insertion_prevendor import PXMLInserter
from inline_replacement_section import SectionLineReplacement
from section_insertion import SectionExtractor
from track_changes_df import process_docx
from section_deletion import section_deletion_filling
from config import API_KEY, MODEL_OPUS
from continous_ncont_insertions import *
from newline_insertions import *
from insertions_paragraphs import *
from section_name_edge_cases import *
from postprocessing import postprocessing_fortags
from paragraph_level_trackchanges import parse_pxml_sections_from_str_new
from inline_footnotes_insertion import (
    process_all_footnotes_with_llm,
    parse_pxml_sections_from_str_dict_positions,
    build_anthropic_client,
)
from newline_insertions_new import (
    return_closest_previous_par,
    process_all_newline_insertions_with_llm,
)
from sectionnumber_filling import fill_sections_backward_verbose
from paragraph_level_trackchanges import process_with_llm
from new_footnotes_insertions import process_all_footnote_insertions

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="PXML Pipeline",
    page_icon="📄",
    layout="wide"
)

st.title("📄 PXML Track Changes Pipeline")
st.markdown("---")

# ── Session state init ────────────────────────────────────────────────────────
for key in ["pipeline_running", "pipeline_done", "output_pxml", "log_text", "error"]:
    if key not in st.session_state:
        st.session_state[key] = None if key not in ["pipeline_running", "pipeline_done"] else False


# ── Sidebar — Inputs ──────────────────────────────────────────────────────────
with st.sidebar:
    st.header("📂 Input Files")

    docx_file = st.file_uploader(
        "Upload DOCX file",
        type=["docx"],
        help="The track-changes Word document"
    )

    pxml_file = st.file_uploader(
        "Upload PXML file",
        type=["pxml", "xml"],
        help="The source PXML file to update"
    )

    st.markdown("---")
    st.header("📁 Output Folder")
    output_folder = st.text_input(
        "Output folder path",
        value=r"C:\projectdata\xmlproject\output",
        help="Folder where logs and intermediate files will be saved. Must already exist or will be created."
    )

    st.markdown("---")
    run_btn = st.button(
            "🚀 Run Pipeline",
            disabled=st.session_state.pipeline_running,
            use_container_width=True,
            type="primary"
    )

# ── Main area layout ──────────────────────────────────────────────────────────
col1, col2 = st.columns([1, 1])

with col1:
    st.subheader("📋 Status")
    status_box = st.empty()

with col2:
    st.subheader("⬇️ Download Output")
    download_box = st.empty()

st.markdown("---")
st.subheader("🖥️ Pipeline Log")
log_box = st.empty()


# ── Log capture utility ───────────────────────────────────────────────────────
class QueueStream:
    """Redirects stdout/stderr writes into a queue for live display."""
    def __init__(self, q: queue.Queue):
        self.q = q

    def write(self, data):
        if data.strip():
            self.q.put(data)

    def flush(self):
        pass


# ── Pipeline runner ───────────────────────────────────────────────────────────
def run_pipeline(docx_path: str,
                 pxml_path: str,
                 output_folder: str,
                 log_queue: queue.Queue,
                 result_holder: dict):
    """
    Runs the full pipeline in a background thread.
    Writes final pxml to result_holder['output_pxml'].
    Writes any exception to result_holder['error'].
    """
    

    # Redirect stdout into queue
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sys.stdout = QueueStream(log_queue)
    sys.stderr = QueueStream(log_queue)

    try:
        # ── Lazy imports (all your pipeline modules) ──────────────────────
        

        # ── Paths ─────────────────────────────────────────────────────────
        os.makedirs(output_folder, exist_ok=True)
        log_file              = os.path.join(output_folder, "pipeline_log.txt")
        missing_revisions_out = os.path.join(output_folder, "missing_revisions.txt")
        failed_log_path       = os.path.join(output_folder, "failed_items.txt")
        sections_dump_path    = os.path.join(output_folder, "sections_dump.txt")
        output_file           = os.path.join(output_folder, "integration_testing.pxml")

        start_time = time.time()
        print(f"\n{'='*80}")
        print(f"Pipeline started at: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"DOCX : {docx_path}")
        print(f"PXML : {pxml_path}")
        print(f"OUT  : {output_folder}")
        print(f"{'='*80}")

        # ══════════════════════════════════════════════════════════════════
        # PIPELINE 1 — Track changes → DataFrame
        # ══════════════════════════════════════════════════════════════════
        print("\n[PIPELINE 1] Processing DOCX track changes...")
        results        = process_docx(docx_path)
        extractor      = results['extractor']
        result_with_ba = extractor.build_before_after_df()
        try:
            result_with_ba = extractor.enrich_footnote_anchors()
        except RuntimeError as e:
            print(f"  [WARN] enrich_footnote_anchors skipped: {e}")
            print(f"  [INFO] Continuing pipeline — footnote enrichment not available for this document.")
        # result_with_ba = extractor.enrich_footnote_anchors()
        no_changes_df  = results['no_changes_df']
        df_remaining   = results['df_remaining']
        result         = results['result']
        df_changes     = results['df_changes']
        chapter_number = results['chapter_number']
        print(f"Chapter from DOCX: {chapter_number}")

        newdf              = df_changes[['type','paragraph_index','deleted_text','inserted_text','prev_context','post_context']]
        inline_insertion_df = result[result['type'] == 'inline_insertion']

        # ── Section pattern from PXML ──────────────────────────────────
        pxml_chapter_number, section_pattern, designator_patterns = extract_designator_pattern_using_llm(pxml_path)
        print(f"Section pattern : {section_pattern}")
        print(f"PXML chapter    : {pxml_chapter_number}")

        separator = re.search(r'[^0-9A-Za-z]', designator_patterns[0]).group()
        separator = re.escape(separator)

        if chapter_number is None or (isinstance(chapter_number, str) and chapter_number.strip() == ''):
            chapter_number = pxml_chapter_number
        print(f"Final chapter   : {chapter_number}")

        # ══════════════════════════════════════════════════════════════════
        # PIPELINE 2 — Section Deletion
        # ══════════════════════════════════════════════════════════════════
        print("\n[PIPELINE 2] Section Deletion...")
        with open(pxml_path, 'r', encoding='utf-8') as f:
            pxml_content = f.read()

        updated_pxmlcontent, remaining_rows_inlinedeletion, rows_to_delete = section_deletion_filling(
            result, df_changes, chapter_number, no_changes_df, df_remaining, pxml_content, verbose=True
        )
        print(f"Rows to delete: {rows_to_delete['paragraph_index'].tolist()}")

        # ══════════════════════════════════════════════════════════════════
        # PIPELINE 3 — Section (Number) Replacement
        # ══════════════════════════════════════════════════════════════════
        print("\n[PIPELINE 3] Section Replacement...")
        inlinereplacement_df = result[(result['type'] == 'inline_replacement')&(result['is_footnote'] == False)]
        processor            = SectionLineReplacement(inlinereplacement_df, API_KEY, output_file)
        processed_df         = processor.process_dataframe(df_changes)
        updated_pxmlcontent, updated_inlinereplacement_df, replaced_indices = processor.create_deletion_column(
            chapter_number, updated_pxmlcontent, missing_revisions_out
        )
        print(f"Replacement indices: {replaced_indices}")

        # ══════════════════════════════════════════════════════════════════
        # PIPELINE 4 — Section Insertion & Edge Cases
        # ══════════════════════════════════════════════════════════════════
        print("\n[PIPELINE 4] Section Insertion & Edge Cases...")
        sec_extractor = SectionExtractor(pxml_path)
        inline_insertion_df_without_footnotes = inline_insertion_df[inline_insertion_df['is_footnote'] == False]
        sections      = sec_extractor.extract_inserted_sections(inline_insertion_df_without_footnotes, chapter_number)
        leftover_rows = find_insertion_edgecase_section(sections, chapter_number)

        print(f"Edge case rows: {len(leftover_rows)}")
        for row in leftover_rows:
            idx       = row['paragraph_index']
            text_c    = result[result['paragraph_index'] == idx]['text'].values[0]
            section_number, section_text = extract_sectionedgecase_key(text_c)
            updated_pxmlcontent = update_designator_by_name_match(
                updated_pxmlcontent, section_text, section_number
            )

        edgecases_df  = remaining_insertions_df(inline_insertion_df_without_footnotes, sections)
        new_sections  = create_edgecase_section_dict(edgecases_df, chapter_number)
        sections.extend(new_sections)
        print(f"Total sections to insert: {len(sections)}")

        # Dump sections
        with open(sections_dump_path, 'w', encoding='utf-8') as sf:
            for i, sec in enumerate(sections):
                sf.write(f"{'='*80}\nSection #{i+1}\n{'='*80}\n")
                for key, val in sec.items():
                    if key == 'content':
                        sf.write(f"  {key}: ({len(val)} rows)\n")
                        for r in val:
                            sf.write(f"    {r}\n")
                    elif key == 'full_text':
                        sf.write(f"  {key}:\n{val}\n")
                    else:
                        sf.write(f"  {key}: {val}\n")
                sf.write("\n")
        print(f"Sections dumped to: {sections_dump_path}")

        xml_context              = sec_extractor.getting_contextxml()
        sections_insertion_indexes = []

        if len(sections) > 0:
            for each_section in sections:
                if each_section['start_df_index'] == each_section['end_df_index']:
                    print(f"Skipping '{each_section['section_header']}' — same start/end df_index")
                    continue
                if abs(each_section['end_paragraph_index'] - each_section['start_paragraph_index']) < 20:
                    print(f"Skipping '{each_section['section_header']}' — paragraph range too small")
                    continue

                start_pi = int(each_section['start_paragraph_index'])
                end_pi   = int(each_section['end_paragraph_index'])
                sections_insertion_indexes.extend(range(start_pi, end_pi + 1))

                plaintext = each_section['full_text']
                tagged_xml = sec_extractor.convert_text_to_xml_tags(xml_context, plaintext)
                cleanedup_xml, designator = sec_extractor.cleanup_xml_fences(tagged_xml)
                if designator:
                    designator = designator.replace('§', '').strip()

                inserter = PXMLInserter(output_file, updated_pxmlcontent)
                result_pxml, designator_num = inserter.insert_xml_by_designator(
                    designator, cleanedup_xml, write_to_file=False
                )
                if result_pxml is not None and designator_num is not None:
                    updated_pxmlcontent = result_pxml
                else:
                    print(f"⚠️  insert_xml_by_designator returned None for '{designator}' — skipping")

        print(f"Section insertion indices ({len(sections_insertion_indexes)}): {sections_insertion_indexes}")

        # ══════════════════════════════════════════════════════════════════
        # PIPELINE 5 — Paragraph-level inline track changes
        # ══════════════════════════════════════════════════════════════════
        print("\n[PIPELINE 5] Paragraph-level inline changes...")
        inline_replacement_df = result[result['type'] == 'inline_replacement']
        inline_insertion_df   = result[result['type'] == 'inline_insertion']
        inline_deletion_df    = result[result['type'] == 'inline_deletion']

        section_dict = parse_pxml_sections_from_str_new(updated_pxmlcontent)
        print(f"Found {len(section_dict)} sections.")

        WORKSPACE_ID = os.getenv("TR_WORKSPACE_ID", "")
        MODEL_NAME   = MODEL_OPUS
        client, model_name = build_anthropic_client(WORKSPACE_ID, MODEL_NAME)

        result_with_ba = result_with_ba.drop_duplicates()

        already_processed = (
            set(rows_to_delete['paragraph_index'].tolist()) |
            set(replaced_indices) |
            set(sections_insertion_indexes)
        )
        print(f"Removing {len(already_processed)} already-processed indices")
        result_with_ba_new = result_with_ba[~result_with_ba['idx'].isin(already_processed)]
        print(f"result_with_ba after filter: {len(result_with_ba_new)} rows")

        all_revisions_except_newline_insertions_new = result_with_ba_new
        result_with_ba_sorted = result_with_ba_new.sort_values('idx').reset_index(drop=True)
        valid_text = result_with_ba_sorted[
            result_with_ba_sorted['text_before'].notna() &
            (result_with_ba_sorted['text_before'] != '') &
            (result_with_ba_sorted['text_before'].str.len() > 5)
        ][['idx', 'text_before']].reset_index(drop=True)

        def get_previous_para(idx):
            candidates = valid_text[valid_text['idx'] < idx]
            return None if candidates.empty else candidates.iloc[-1]['text_before']

        def get_post_para(idx):
            candidates = valid_text[valid_text['idx'] > idx]
            return None if candidates.empty else candidates.iloc[0]['text_before']

        all_revisions_except_newline_insertions_new['previous_para'] = (
            all_revisions_except_newline_insertions_new['idx'].apply(get_previous_para)
        )
        all_revisions_except_newline_insertions_new['post_para'] = (
            all_revisions_except_newline_insertions_new['idx'].apply(get_post_para)
        )

        all_revisions_except_newline_insertions1 = all_revisions_except_newline_insertions_new[
            (all_revisions_except_newline_insertions_new['text_before'] != '') &
            (all_revisions_except_newline_insertions_new['has_changes'] == True)
        ]
        all_revisions_except_newline_insertions1 = fill_sections_backward_verbose(
            all_revisions_except_newline_insertions1, no_changes_df,
            inline_replacement_df, inline_insertion_df, chapter_number
        )

        updated_pxmlcontent, processed_idxs = process_with_llm(
            result_df=all_revisions_except_newline_insertions1,
            section_dict=section_dict,
            pxml_content=updated_pxmlcontent,
            client=client,
            failed_log_path=failed_log_path,
            match_threshold=0.5,
            model_name=model_name,
        )
        print(f"Processed indices: {processed_idxs}")

        
        # ══════════════════════════════════════════════════════════════════
        # PIPELINE 6 — Newline (new paragraph) insertions
        # ══════════════════════════════════════════════════════════════════

        print("\n[PIPELINE 6] Newline paragraph insertions...")
        all_newline_insertions = result_with_ba_new[
            (result_with_ba_new['text_before'] == '') &
            (result_with_ba_new['has_changes'] == True) &
            (result_with_ba_new['is_footnote_x'] == False)
        ].drop_duplicates(subset=['idx'], keep='first')

        all_newline_insertions = all_newline_insertions[
            ['idx', 'text', 'has_changes', 'type', 'paragraph_index']
        ]
        all_newline_insertions = return_closest_previous_par(
            inline_insertion_df, inline_deletion_df, no_changes_df, inline_replacement_df, all_newline_insertions
        )
        all_newline_insertions_sections = fill_sections_backward_verbose(
            all_newline_insertions, no_changes_df,
            inline_replacement_df, inline_insertion_df, chapter_number
        )
        all_newline_insertions_sections.to_excel(os.path.join(output_folder, "newline_insertions_sections.xlsx"), index=False)
        section_dict, section_positions, pxml_content = parse_pxml_sections_from_str_dict_positions(updated_pxmlcontent)
        updated_pxmlcontent = process_all_newline_insertions_with_llm(
            pxml_content=pxml_content,
            section_dict=section_dict,
            section_positions=section_positions,
            all_newline_insertions=all_newline_insertions_sections,
            client=client,
            model_name=model_name,
            failed_log_path=os.path.join(output_folder, "failed_newline_insertions.txt"),
        )

        # ══════════════════════════════════════════════════════════════════
        # PIPELINE 7 — New footnote insertions
        # ══════════════════════════════════════════════════════════════════
        

        all_inserted_footnote_revisions = result_with_ba[
                (result_with_ba['is_footnote_x'] == True) &
                (result_with_ba['has_changes'] == True) &
                (result_with_ba['text_before'] == '')
            ].drop_duplicates(subset=['idx'], keep='first')



        section_dict, section_positions, pxml_content = parse_pxml_sections_from_str_dict_positions(
            updated_pxmlcontent
        )

        all_inserted_footnote_revisions_sections = fill_sections_backward_verbose(
            all_inserted_footnote_revisions, no_changes_df,
            inline_replacement_df, inline_insertion_df, chapter_number
        )

        updated_pxml = process_all_footnote_insertions(
            all_inserted_footnote_revisions=all_inserted_footnote_revisions_sections,
            pxml_content=pxml_content,
            model_name=MODEL_OPUS,
            failed_log_path=os.path.join(output_folder, "failed_footnote_insertions.txt")
        )

        # ── Save final output ──────────────────────────────────────────

        # final_updated_pxml=postprocessing_fortags(final_updated_pxml)
        final_output_path = os.path.join(output_folder, "final_output.pxml")
        with open(final_output_path, 'w', encoding='utf-8') as f:
            f.write(updated_pxml)
        print(f"\n[DONE] Output written to: {final_output_path}")

        elapsed = time.time() - start_time
        print(f"Total runtime: {elapsed:.2f} seconds")
        print(f"Pipeline finished at: {time.strftime('%Y-%m-%d %H:%M:%S')}")

        result_holder['output_pxml'] = updated_pxml
        result_holder['error']       = None

    except Exception as e:
        import traceback
        print(f"\n❌ PIPELINE FAILED: {e}")
        print(traceback.format_exc())
        result_holder['output_pxml'] = None
        result_holder['error']       = str(e)

    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr
        log_queue.put(None)  # sentinel — pipeline finished


# ── Run pipeline on button click ──────────────────────────────────────────────
if run_btn:
    if not docx_file:
        st.sidebar.error("Please upload a DOCX file.")
    elif not pxml_file:
        st.sidebar.error("Please upload a PXML file.")
    elif not output_folder.strip():
        st.sidebar.error("Please enter an output folder path.")
    else:
        # Save uploaded files to temp dir
        tmp_dir = tempfile.mkdtemp()
        docx_path = os.path.join(tmp_dir, docx_file.name)
        pxml_path = os.path.join(tmp_dir, pxml_file.name)

        with open(docx_path, 'wb') as f:
            f.write(docx_file.read())
        with open(pxml_path, 'wb') as f:
            f.write(pxml_file.read())

        os.makedirs(output_folder, exist_ok=True)

        # Set up shared state
        log_queue     = queue.Queue()
        result_holder = {'output_pxml': None, 'error': None}

        st.session_state.pipeline_running = True
        st.session_state.pipeline_done    = False
        st.session_state.output_pxml      = None
        st.session_state.log_text         = ""
        st.session_state.error            = None

        # Launch pipeline in background thread
        thread = threading.Thread(
            target=run_pipeline,
            args=(docx_path, pxml_path, output_folder, log_queue, result_holder),
            daemon=True
        )
        thread.start()

        # ── Live log streaming ─────────────────────────────────────────
        status_box.info("⏳ Pipeline running...")
        log_lines = []

        while True:
            try:
                msg = log_queue.get(timeout=0.3)
                if msg is None:
                    break
                log_lines.append(msg)
                log_box.code("".join(log_lines), language="")
            except queue.Empty:
                continue

        thread.join()

        # ── Done ───────────────────────────────────────────────────────
        st.session_state.pipeline_running = False
        st.session_state.pipeline_done    = True
        st.session_state.log_text         = "".join(log_lines)
        st.session_state.output_pxml      = result_holder['output_pxml']
        st.session_state.error            = result_holder['error']

        # Write full log to pipeline_log.txt
        log_file_path = os.path.join(output_folder, "pipeline_log.txt")
        with open(log_file_path, 'w', encoding='utf-8') as lf:
            lf.write(st.session_state.log_text)
        print(f"📋 Log saved to: {log_file_path}")

        # Cleanup temp files
        shutil.rmtree(tmp_dir, ignore_errors=True)

# ── Status + Download rendering ───────────────────────────────────────────────
if st.session_state.pipeline_done:
    if st.session_state.error:
        status_box.error(f"❌ Pipeline failed: {st.session_state.error}")
    else:
        status_box.success("✅ Pipeline completed successfully!")

    if st.session_state.output_pxml:
        download_box.download_button(
            label="⬇️ Download final_output.pxml",
            data=st.session_state.output_pxml.encode("utf-8"),
            file_name="final_output.pxml",
            mime="application/xml",
            use_container_width=True
        )

    if st.session_state.log_text:
        log_box.code(st.session_state.log_text, language="")

elif st.session_state.pipeline_running:
    status_box.info("⏳ Pipeline running...")

else:
    status_box.info("Upload files and click **Run Pipeline** to start.")