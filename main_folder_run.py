# In another file (e.g., main.py)
import os
from dotenv import load_dotenv
load_dotenv()
from canada_pipeline.chapter_section import extract_designator_pattern_using_llm
from canada_pipeline.full_section_insertion_prevendor import PXMLInserter
from canada_pipeline.inline_replacement_section import SectionLineReplacement
from canada_pipeline.new_footnotes_insertions import process_all_footnote_insertions
from canada_pipeline.section_insertion import SectionExtractor
from canada_pipeline.track_changes_df import process_docx
from canada_pipeline.section_deletion import section_deletion_filling
import pandas as pd
import anthropic
import time
import re
import sys
import os
import ctypes
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from canada_pipeline.config import API_KEY, MODEL_OPUS, MODEL_SONNET
from canada_pipeline.continous_ncont_insertions import *
from canada_pipeline.newline_insertions import *
from canada_pipeline.insertions_paragraphs import *
from canada_pipeline.section_name_edge_cases import *
from canada_pipeline.section_split_ import handle_section_split
from canada_pipeline.postprocessing import postprocessing_fortags
from canada_pipeline.paragraph_level_trackchanges import parse_pxml_sections_from_str_new
from canada_pipeline.inline_footnotes_insertion import process_all_footnotes_with_llm,parse_pxml_sections_from_str_dict_positions
from canada_pipeline.track_changes_df  import process_docx
from canada_pipeline.newline_insertions_new import return_closest_previous_par,process_all_newline_insertions_with_llm
from canada_pipeline.sectionnumber_filling import fill_sections_backward_verbose
from canada_pipeline.inline_footnotes_insertion import process_all_footnotes_with_llm,parse_pxml_sections_from_str_dict_positions
from canada_pipeline.inline_footnotes_insertion import build_anthropic_client
from canada_pipeline.paragraph_level_trackchanges import process_with_llm, process_with_llm_footnotes
from canada_pipeline.revisions_images import convert_revision_pages_to_images, extract_page_texts

# from collapse_fully_deleted_elements import collapse_empty_para_with_trace_deleted


# ─────────────────────────────────────────────
# USER INPUTS — only these need to be set
# ─────────────────────────────────────────────

# PRODUCT_TYPE is determined per-folder inside run_pipeline:
#   If folder name contains "GO_DDDC" → pamphlet
#   Otherwise → looseleaf

# ─────────────────────────────────────────────
# BATCH FOLDER INPUT
# ─────────────────────────────────────────────
# Provide the parent folder. Each subfolder should contain:
#   - One .docx or .doc file
#   - One .pxml file
# An output folder "<subfolder_name>_output" will be created inside each subfolder.

BATCH_FOLDER = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\Test_9 (May 12, 2026)_batch_running_new"


def discover_file_pairs(batch_folder):
    """
    Scan batch_folder for subfolders, each containing a .docx/.doc and a .pxml file.
    Returns list of dicts: {'docx_path', 'pxml_path', 'output_dir', 'folder_name'}
    """
    pairs = []
    for entry in sorted(os.listdir(batch_folder)):
        subfolder = os.path.join(batch_folder, entry)
        if not os.path.isdir(subfolder):
            continue

        docx_files = [f for f in os.listdir(subfolder)
                      if f.lower().endswith(('.docx', '.doc')) and not f.startswith('~')]
        pxml_files = [f for f in os.listdir(subfolder)
                      if f.lower().endswith('.pxml')]

        if not docx_files:
            print(f"  [SKIP] '{entry}' — no .docx/.doc file found")
            continue
        if not pxml_files:
            print(f"  [SKIP] '{entry}' — no .pxml file found")
            continue

        docx_path = os.path.join(subfolder, docx_files[0])
        pxml_path = os.path.join(subfolder, pxml_files[0])
        output_dir = os.path.join(subfolder, f"{entry}_output")

        pairs.append({
            'docx_path': docx_path,
            'pxml_path': pxml_path,
            'output_dir': output_dir,
            'folder_name': entry,
        })
        print(f"  [FOUND] '{entry}' — docx: {docx_files[0]}, pxml: {pxml_files[0]}")

    return pairs


def run_pipeline(docx_path, pxml_path, output_dir):
    """Run the full pipeline for a single docx+pxml pair."""
    # ─────────────────────────────────────────────
    # PRODUCT TYPE — determined from folder name
    # ─────────────────────────────────────────────
    folder_name = os.path.basename(os.path.dirname(output_dir))
    if "GO_DDDC" in folder_name:
        PRODUCT_TYPE = "pamphlet"
    else:
        PRODUCT_TYPE = "looseleaf"
    DATE_UPDATED_VALUE = "1" if PRODUCT_TYPE == "looseleaf" else "0"
    DATE_TRACE_VALUE = "1"
    print(f"  Product type: {PRODUCT_TYPE} (folder: {folder_name})")

    # ─────────────────────────────────────────────
    # DERIVED PATHS — auto-generated from output_dir
    # ─────────────────────────────────────────────
    os.makedirs(output_dir, exist_ok=True)
    pxml_basename            = os.path.splitext(os.path.basename(pxml_path))[0]
    timestamp                = time.strftime('%Y%m%d_%H%M%S')
    output_file              = os.path.join(output_dir, f"{pxml_basename}_{timestamp}_final.pxml")
    missing_revisions_output = os.path.join(output_dir, "missing_revisions.txt")
    log_file                 = os.path.join(output_dir, "pipeline_log.txt")
    failed_log_path          = os.path.join(output_dir, "failed_items.txt")

    # ─────────────────────────────────────────────
    # Logger Setup - Tee output to both console and log file
    # ─────────────────────────────────────────────

    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    log_f = open(log_file, 'w', encoding='utf-8')
    sys.stdout = Tee(sys.__stdout__, log_f)
    sys.stderr = Tee(sys.__stderr__, log_f)

    try:
        # ══════════════════════════════════════════════════════════
        # PIPELINE-1: Track Changes → DataFrame
        # ══════════════════════════════════════════════════════════
        start_time = time.time()
        print(f"\n{'='*80}")
        print(f"Pipeline started at: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*80}")

        results   = process_docx(docx_path)
        extractor_obj = results['extractor']

        result_with_ba = extractor_obj.build_before_after_df()
        try:
            result_with_ba = extractor_obj.enrich_footnote_anchors()
        except RuntimeError as e:
            print(f"  [WARN] enrich_footnote_anchors skipped: {e}")
            print(f"  [INFO] Continuing pipeline — footnote enrichment not available.")

        no_changes_df  = results['no_changes_df']
        df_remaining   = results['df_remaining']
        result         = results['result']
        df_changes     = results['df_changes']
        chapter_number = results['chapter_number']
        # result_with_ba.to_excel(os.path.join(output_dir, "result_with_ba.xlsx"), index=False)
        print(f"\nChapter from DOCX: {chapter_number}")

        # ── Superscript extraction from PDF ──
        print("\n[SUPERSCRIPT] Extracting footnote superscript numbers from PDF...")
        from canada_pipeline.superscript_extraction_from_pdf import convert_docx_to_pdf, process_excel as process_superscripts
        pdf_path_for_superscript = None
        try:
            pdf_path_for_superscript = convert_docx_to_pdf(docx_path, output_dir=output_dir)
            superscript_excel = os.path.join(output_dir, "superscript_extraction.xlsx")
            _, result_with_ba = process_superscripts(
                docx_path=docx_path,
                pdf_path=pdf_path_for_superscript,
                output_excel_path=superscript_excel,
                result_with_ba=result_with_ba
            )
            print(f"[SUPERSCRIPT] Done. Columns added: pdf_superscript, footnote_ref_confidence, extraction_method")
        except Exception as e:
            print(f"[SUPERSCRIPT] WARNING: Superscript extraction failed: {e}")
            print(f"[SUPERSCRIPT] Continuing without PDF superscript data.")

        newdf = df_changes[['type', 'paragraph_index', 'deleted_text',
                                'inserted_text', 'prev_context', 'post_context']]
        inline_insertion_df = result[result['type'] == 'inline_insertion']


        with open(pxml_path, 'r', encoding='utf-8') as f:
          pxml_content = f.read()
        # Get section pattern from PXML via LLM
        pxml_chapter_number, section_pattern, designator_patterns = \
            extract_designator_pattern_using_llm(pxml_content)
        print(f"Final section regex pattern: {section_pattern}")
        print(f"Chapter number from PXML: {pxml_chapter_number}")

        separator = ':'
        if designator_patterns:
            sep_match = re.search(r'[^0-9A-Za-z]', designator_patterns[0])
            if sep_match:
                separator = re.escape(sep_match.group())
        else:
            print("⚠️  No designator patterns extracted from PXML. Using default separator ':'")

        if chapter_number is None or (isinstance(chapter_number, str) and chapter_number.strip() == ''):
            print(f"⚠️  Chapter number from DOCX was None/empty. Using PXML chapter number: {pxml_chapter_number}")
            chapter_number = pxml_chapter_number
        print(f"✅ Final chapter number: {chapter_number}")

        print('---------------pipeline 1 completed, moving to pipeline 2 (Section Deletion)-------------------')

        print('all section patterns from pxml--',designator_patterns)
        # ══════════════════════════════════════════════════════════
        # PIPELINE-2: Section Deletion
        # ══════════════════════════════════════════════════════════
        print(f"\n{'='*80}")
        print("PIPELINE-2: Section Deletion")
        print(f"{'='*80}")

        with open(pxml_path, 'r', encoding='utf-8') as f:
            pxml_content = f.read()

        updated_pxmlcontent, remaining_rows_inlinedeletion, rows_to_delete = \
            section_deletion_filling(result, df_changes, chapter_number,
                                        no_changes_df, df_remaining, pxml_content, verbose=True)
        print(f"Rows to delete from result: {rows_to_delete['paragraph_index'].tolist()}")

        print('---------------pipeline 2 completed, moving to pipeline 3 (Section Replacement)-------------------')

        # ══════════════════════════════════════════════════════════
        # PIPELINE-3: Section (Number) Replacement
        # ══════════════════════════════════════════════════════════
        print(f"\n{'='*80}")
        print("PIPELINE-3: Section (Number) Replacement")
        print(f"{'='*80}")

        inlinereplacement_df = result_with_ba[(result_with_ba['type'] == 'inline_replacement')&(result_with_ba['is_footnote_x'] == False)]
        processor = SectionLineReplacement(inlinereplacement_df, API_KEY, output_file)
        processed_df = processor.process_dataframe(df_changes)
        updated_pxmlcontent, updated_inlinereplacement_df, replaced_indices = \
            processor.create_deletion_column(chapter_number, updated_pxmlcontent, missing_revisions_output)
        print(f"Replacement happened at paragraph indices: {replaced_indices}")
        # with open(os.path.join(output_dir, "updated_pxmlcontent.pxml"), 'w', encoding='utf-8') as f:
        #     f.write(updated_pxmlcontent)
        print('---------------pipeline 3 completed, moving to pipeline 4 (Section Insertion)-------------------')

        # ══════════════════════════════════════════════════════════
        # PIPELINE-4: Section Insertion & Edge Cases
        # ══════════════════════════════════════════════════════════
        print(f"\n{'='*80}")
        print("PIPELINE-4: Section Insertion & Edge Cases")
        print(f"{'='*80}")
        result_with_ba.to_excel(os.path.join(output_dir, "result_with_ba_before_section_insertion.xlsx"), index=False)
        sec_extractor = SectionExtractor(pxml_path)
        inline_insertion_df_without_footnotes=result_with_ba[(result_with_ba['type'] == 'inline_insertion')&(result_with_ba['is_footnote_x'] == False)]
        inline_insertion_df_without_footnotes = inline_insertion_df_without_footnotes.drop_duplicates(subset=['idx'], keep='first')
        # inline_insertion_df_without_footnotes = inline_insertion_df[inline_insertion_df['is_footnote'] == False]
        sections = sec_extractor.extract_inserted_sections(inline_insertion_df_without_footnotes, chapter_number)
        print('intial sections extracted from inline insertions--', sections)
        for sec in sections:
            print(f"\nSection: {sec['section_header']}")
            print(f"  Paragraph range : {sec['start_paragraph_index']} → {sec['end_paragraph_index']}")
            print(f"  DF index range  : {sec['start_df_index']} → {sec['end_df_index']}")
            print(f"  Content rows    : {len(sec['content'])}")

        edgecases_in_section_insertion_df = remaining_insertions_df(inline_insertion_df_without_footnotes, sections)
        # Exclude paragraph indices that also have inline_replacement (existing sections with edits, not new sections)
        replacement_para_indices = set(
            result_with_ba[result_with_ba['type'] == 'inline_replacement']['idx'].tolist()
        )
        edgecases_in_section_insertion_df = edgecases_in_section_insertion_df[
            ~edgecases_in_section_insertion_df['idx'].isin(replacement_para_indices)
        ]
        edgecases_in_section_insertion_df.to_excel(os.path.join(output_dir, "edgecases_in_section_insertion_df.xlsx"), index=False)
        new_sections = create_edgecase_section_dict(edgecases_in_section_insertion_df, chapter_number, result_with_ba=result_with_ba)
        sections.extend(new_sections)
        print('new_sections-------',new_sections)
        for sec in new_sections:
            print(f"\nedge case Section: {sec['section_header']}")
            print(f"  Paragraph range : {sec['start_paragraph_index']} → {sec['end_paragraph_index']}")
            print(f"  DF index range  : {sec['start_df_index']} → {sec['end_df_index']}")
            print(f"  Content rows    : {len(sec['content'])}")


        new_sections = [
        sec for sec in new_sections
        if re.match(r'^(?:§\s*)?\d+:\d+', sec['section_header'])
        ]

        edgecase_processed_indexes = []
        if len(new_sections) == 1:
            for con in new_sections[0]['content']:
                # Only process content items that start with a section pattern like § 7:4
                if con.get('text') and re.match(r'^(?:§\s*)?\d+:\d+', con['text']):
                    section_number, section_text = extract_sectionedgecase_key(con['text'])
                    print('section_number--from edge case', section_number)
                    print('section_text--from edge case', section_text)
                    updated_pxmlcontent = update_designator_by_name_match(
                        updated_pxmlcontent, section_text, section_number
                    )
                    edgecase_processed_indexes.append(int(con['paragraph_index']))
                    break  # Only one section header expected per section, stop after finding it
        else:
            print(f"⚠️  Expected exactly 1 new section from edge cases, but found {len(new_sections)}. Skipping edge case section insertion.")

        xml_context = sec_extractor.getting_contextxml()
        sections_insertion_indexes = list(edgecase_processed_indexes)
        print(f"📋 Edge-case processed indexes added: {edgecase_processed_indexes}")

        # Build section_dict and re-extract designator patterns from current PXML
        # (needed for small-section context lookup)
        pxml_chapter_number_p4, section_pattern_p4, designator_patterns_p4 = \
            extract_designator_pattern_using_llm(updated_pxmlcontent)
        section_dict_p4 = parse_pxml_sections_from_str_new(updated_pxmlcontent, chapter_number)
        print(f"[Pipeline 4] section_dict_p4 has {len(section_dict_p4)} keys: {list(section_dict_p4.keys())}")
        print(f"[Pipeline 4] designator_patterns_p4: {designator_patterns_p4}")

        # Build an Anthropic client for LLM-based key matching
        from canada_pipeline.config import build_anthropic_client as _build_client_config
        _client_p4 = _build_client_config()

        if len(sections) > 0:
            for each_section in sections:
                if each_section['start_df_index'] == each_section['end_df_index']:
                    print(f"Skipping section '{each_section['section_header']}' — start and end df_index are equal.")
                    continue

                para_range = abs(each_section['end_paragraph_index'] - each_section['start_paragraph_index'])

                # ── Look up previous section XML and check for section split ──
                previous_section_xml = None
                is_section_split = False

                # Extract the designator from the section header (e.g. "14:11A" from "§ 14:11A ...")
                header_desig = re.sub(r'^[§\s]+', '', str(each_section['section_header'])).strip()
                header_desig = header_desig.split()[0] if header_desig else ''
                print(f"\n  Section '{each_section['section_header']}' (para_range={para_range})")
                print(f"  Section header designator: '{header_desig}'")

                # Use LLM to find the closest PREVIOUS designator in designator_patterns_p4
                prev_desig = None
                if header_desig and designator_patterns_p4:
                    keys_list_str = "\n".join(f"  - '{k}'" for k in designator_patterns_p4)
                    prompt = (
                        f"Given a NEW section designator '{header_desig}' that is being inserted,\n"
                        f"which existing designator from the list below would come immediately BEFORE it "
                        f"in logical/numerical order?\n\n"
                        f"Available designators (in order):\n{keys_list_str}\n\n"
                        f"Rules:\n"
                        f"- '14:11A' comes after '14:11' but before '14:12'\n"
                        f"- '14:9.50' comes after '14:9' but before '14:9.75'\n"
                        f"- Reply with ONLY the designator that comes immediately before '{header_desig}'\n"
                        f"- Reply with ONLY the key exactly as shown above, or 'NONE' if none comes before it.\n"
                        f"- Do not add any explanation."
                    )
                    try:
                        response = _client_p4.messages.create(
                            model=MODEL_SONNET,
                            max_tokens=30,
                            temperature=0,
                            messages=[{"role": "user", "content": prompt}],
                        )
                        answer = response.content[0].text.strip().strip("'\"")
                        print(f"    [LLM PREV DESIG] answer: '{answer}'")
                        if answer.upper() != "NONE" and answer in designator_patterns_p4:
                            prev_desig = answer
                            print(f"  LLM found previous designator: '{prev_desig}' for new section '{header_desig}'")
                        else:
                            print(f"  LLM could not find previous designator for '{header_desig}' (answer='{answer}')")
                    except Exception as e:
                        print(f"  LLM error finding previous designator: {e}")

                if prev_desig and prev_desig in section_dict_p4:
                    previous_section_xml = section_dict_p4[prev_desig]
                    print(f"  ✅ Found previous section XML for key '{prev_desig}' "
                          f"(length={len(previous_section_xml)} chars)")

                    # Section split detection: check rows after start_paragraph_index
                    # until we hit a row starting with a designator pattern.
                    # If ALL those rows have has_changes=True → NOT a split (all new content)
                    # If any row has has_changes=False → IS a split (existing content from prev section)
                    start_pi_check = int(each_section['start_paragraph_index'])
                    end_pi_check = int(each_section['end_paragraph_index'])
                    desig_pattern_re = re.compile(r'^§?\s*\d+' + separator + r'\d+', re.IGNORECASE)

                    rows_after = result_with_ba[(result_with_ba['idx'] > start_pi_check) & (result_with_ba['idx'] <= end_pi_check)].drop_duplicates(subset=['idx'], keep='first').sort_values('idx')

                    checked_rows = []
                    print(f"  📝 Checking rows after idx={start_pi_check} until end idx={end_pi_check}:")
                    for _, row in rows_after.iterrows():
                        raw_text = row.get('text', '')
                        row_text = '' if pd.isna(raw_text) else str(raw_text).strip()
                        # Stop at next designator
                        if row_text and desig_pattern_re.match(row_text):
                            print(f"     ⛔ idx={row['idx']}: starts with designator — stopping")
                            break
                        has_change = bool(row.get('has_changes', False))
                        # Skip empty rows with has_changes=False (unchanged empty paragraphs)
                        if not has_change and not row_text:
                            print(f"     ⏭️ idx={row['idx']}: has_changes=False with empty text — skipping")
                            continue
                        # Skip unchanged rows whose text already exists in prevendor PXML
                        # (these are pre-existing headings/content, NOT evidence of a section split)
                        if not has_change and row_text:
                            # Strip tags and check plain text
                            plain_check = re.sub(r'<[^>]+>', '', row_text).strip()
                            # Skip if text starts with a Roman numeral heading (e.g. "XII. The Tenant...")
                            if plain_check and re.match(r'^[IVXLCDM]+[\.\s]', plain_check):
                                print(f"     ⏭️ idx={row['idx']}: has_changes=False, starts with Roman numeral heading — skipping (not a split indicator)")
                                continue
                        checked_rows.append((row['idx'], has_change, row_text[:80]))
                        status = "🔄" if has_change else "📄"
                        print(f"     {status} idx={row['idx']}: has_changes={has_change} | '{row_text[:80]}'")

                    if checked_rows:
                        all_changed = all(hc for _, hc, _ in checked_rows)
                        unchanged_count = sum(1 for _, hc, _ in checked_rows if not hc)
                        print(f"  📈 Summary: {len(checked_rows)} rows checked, "
                              f"{unchanged_count} unchanged, all_changed={all_changed}")
                        if not all_changed:
                            is_section_split = True
                            print(f"  🔀 SECTION SPLIT DETECTED — {unchanged_count} unchanged rows found "
                                  f"(existing content from previous section)")
                            # Handle the section split
                            updated_pxmlcontent, split_success = handle_section_split(
                                each_section=each_section,
                                previous_section_xml=previous_section_xml,
                                result_with_ba=result_with_ba,
                                sec_extractor=sec_extractor,
                                xml_context=xml_context,
                                updated_pxmlcontent=updated_pxmlcontent,
                                client=_client_p4,
                                model_name=MODEL_OPUS,
                                output_file=output_file,
                            )
                            if split_success:
                                start_pi = int(each_section['start_paragraph_index'])
                                end_pi = int(each_section['end_paragraph_index'])
                                sections_insertion_indexes.extend(range(start_pi, end_pi + 1))
                        else:
                            print(f"  ✅ NOT a section split — all {len(checked_rows)} rows have changes (new content)")
                    else:
                        print(f"  ⚠️  No rows found between start and next designator")
                elif prev_desig:
                    print(f"  ⚠️  '{prev_desig}' not found in section_dict_p4 keys")
                else:
                    print(f"  ⚠️  No previous designator found for '{header_desig}'")

                # ── If NOT a section split, do normal section tagging + insertion ──
                if not is_section_split:
                    start_pi = int(each_section['start_paragraph_index'])
                    end_pi   = int(each_section['end_paragraph_index'])
                    sections_insertion_indexes.extend(range(start_pi, end_pi + 1))

                    plaintext_tobe_converted_xml = each_section['full_text']
                    bullet_points = each_section.get('bullet_points', [])
                    print(f'  plaintext_tobe_converted_xml: {plaintext_tobe_converted_xml[:500]}')
                    print(f"  bullet_points detected: {len(bullet_points)}")
                    tagged_xml = sec_extractor.convert_text_to_xml_tags(
                        xml_context,
                        plaintext_tobe_converted_xml,
                        bullet_points=bullet_points,
                        section_header=each_section['section_header'],
                    )
                    print(f'  tagged_xml: {tagged_xml[:1000]}')
                    cleanedup_xml, designator = sec_extractor.cleanup_xml_fences(
                        tagged_xml, fallback_designator=each_section['section_header']
                    )
                    print(f'  designator extracted after cleanup fence: {designator}')
                    if designator:
                        designator = designator.replace('§', '').strip()

                    insertionsection_obj = PXMLInserter(output_file, updated_pxmlcontent)
                    result_pxml, designator_num = insertionsection_obj.insert_xml_by_designator(
                        designator, cleanedup_xml, write_to_file=False
                    )
                    if result_pxml is not None and designator_num is not None:
                        updated_pxmlcontent = result_pxml
                        print(f"  ✅ Section inserted via designator '{designator}'")
                    else:
                        print(f"  ⚠️  insert_xml_by_designator returned None for '{designator}' — skipping.")

        print(f"📋 Section insertion paragraph indices ({len(sections_insertion_indexes)}): {sections_insertion_indexes}")
        print('---------------pipeline 4 completed, moving to pipeline 5 (Paragraph-Level Changes)-------------------')
        with open(os.path.join(output_dir, 'save_afternewsection_insertions.pxml'), 'w', encoding='utf-8') as f:
            f.write(updated_pxmlcontent)
        # ══════════════════════════════════════════════════════════════════
        # PIPELINE 5 — Paragraph-level inline track changes
        # ══════════════════════════════════════════════════════════════════
        print("\n[PIPELINE 5] Paragraph-level inline changes...")
        inline_replacement_df = result[result['type'] == 'inline_replacement']
        inline_insertion_df   = result[result['type'] == 'inline_insertion']
        inline_deletion_df    = result[result['type'] == 'inline_deletion']

        pxml_chapter_number, section_pattern, designator_patterns = \
            extract_designator_pattern_using_llm(updated_pxmlcontent)

        inline_replacement_df.to_excel(os.path.join(output_dir, "inline_replacement_df.xlsx"), index=False)
        inline_insertion_df.to_excel(os.path.join(output_dir, "inline_insertion_df.xlsx"), index=False)
        inline_deletion_df.to_excel(os.path.join(output_dir, "inline_deletion_df.xlsx"), index=False)
        no_changes_df.to_excel(os.path.join(output_dir, "no_changes_df.xlsx"), index=False)

        section_dict = parse_pxml_sections_from_str_new(updated_pxmlcontent, chapter_number)
        print(f"Found {len(section_dict)} sections (including chapter preamble if present).")

        WORKSPACE_ID = os.getenv("TR_WORKSPACE_ID", "")
        MODEL_NAME   = MODEL_OPUS
        client, model_name = build_anthropic_client(WORKSPACE_ID, MODEL_NAME)

        if 'text_before' in result_with_ba.columns:
            # Normalize punctuation-only values like "." or "," to empty text
            text_before_mask = result_with_ba['text_before'].notna()
            result_with_ba.loc[text_before_mask, 'text_before'] = (
                result_with_ba.loc[text_before_mask, 'text_before']
                .astype(str)
                .str.replace(r'^\s*[\.,]+\s*$', '', regex=True)
            )

        result_with_ba = result_with_ba.drop_duplicates()
        result_with_ba.to_excel(os.path.join(output_dir, "result_with_ba_before_paragraph_processing.xlsx"), index=False)
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
            (
                (all_revisions_except_newline_insertions_new['text_before'] != '') |
                (all_revisions_except_newline_insertions_new['type'] == 'inline_deletion')
            ) &
            (all_revisions_except_newline_insertions_new['has_changes'] == True) &
            (all_revisions_except_newline_insertions_new['is_footnote_x'] == False)
        ]
        all_revisions_except_newline_insertions1 = fill_sections_backward_verbose(
            all_revisions_except_newline_insertions1, no_changes_df,
            inline_replacement_df, inline_insertion_df, chapter_number,
            verbose=True, client=client, model_name=model_name,
            designator_patterns=designator_patterns
        )
        all_revisions_except_newline_insertions1.to_excel(os.path.join(output_dir, "all_revisions_except_newline_insertions1.xlsx"), index=False)

        # ── Build page images from DOCX PDF for LLM visual context ──
        print("[PAGE IMAGES] Converting DOCX to PDF and extracting page images (colored pages only)...")
        try:
            revision_img_folder = os.path.join(output_dir, "revision_images")
            saved_image_paths = convert_revision_pages_to_images(docx_path, revision_img_folder, save_all_pages=False)
            # Build page_texts and image_paths dict from saved images
            from pathlib import Path
            pdf_in_folder = os.path.join(revision_img_folder, Path(docx_path).stem + ".pdf")
            if os.path.isfile(pdf_in_folder):
                page_texts = extract_page_texts(pdf_in_folder)
            else:
                page_texts = []
            # Build image_paths dict: page_number (1-based) -> image file path
            image_paths = {}
            for img_path in saved_image_paths:
                import re as _re
                m = _re.search(r'_page_(\d+)\.png$', img_path)
                if m:
                    image_paths[int(m.group(1))] = img_path
            print(f"[PAGE IMAGES] Built {len(image_paths)} colored page images, {len(page_texts)} page texts")
        except Exception as e:
            print(f"[PAGE IMAGES] WARNING: Could not build page images: {e}")
            page_texts, image_paths = None, None

        updated_pxmlcontent, processed_idxs_main, failed_inline_changes_main = process_with_llm(
            result_df=all_revisions_except_newline_insertions1,
            section_dict=section_dict,
            pxml_content=updated_pxmlcontent,
            client=client,
            failed_log_path=failed_log_path,
            match_threshold=0.5,
            model_name=model_name,
            chapter_number=chapter_number,
            page_texts=page_texts,
            image_paths=image_paths,
        )

        all_revisions_except_newline_insertions_footnotes = all_revisions_except_newline_insertions_new[
            (all_revisions_except_newline_insertions_new['text_before'] != '') &
            (all_revisions_except_newline_insertions_new['has_changes'] == True) &
            (all_revisions_except_newline_insertions_new['is_footnote_x'] == True)
        ]

        # Build parent paragraph text for footnote-specific process_with_llm flow
        if 'parent_paragraph_idx_x' in all_revisions_except_newline_insertions_footnotes.columns:
            parent_para_text_map = (
                result_with_ba[['idx', 'text']]
                .dropna(subset=['idx'])
                .drop_duplicates(subset=['idx'], keep='first')
                .set_index('idx')['text']
            )
            all_revisions_except_newline_insertions_footnotes['parent_paragraph_text'] = (
                all_revisions_except_newline_insertions_footnotes['parent_paragraph_idx_x']
                .map(parent_para_text_map)
                .fillna('')
            )
        else:
            all_revisions_except_newline_insertions_footnotes['parent_paragraph_text'] = ''

        all_revisions_except_newline_insertions_footnotes = fill_sections_backward_verbose(
            all_revisions_except_newline_insertions_footnotes, no_changes_df,
            inline_replacement_df, inline_insertion_df, chapter_number,
            verbose=True, client=client, model_name=model_name,
            designator_patterns=designator_patterns
        )



        updated_pxmlcontent, processed_idxs_footnotes, failed_inline_changes_footnotes = process_with_llm_footnotes(
            result_df=all_revisions_except_newline_insertions_footnotes,
            section_dict=section_dict,
            pxml_content=updated_pxmlcontent,
            client=client,
            failed_log_path=failed_log_path,
            match_threshold=0.5,
            model_name=model_name,
            chapter_number=chapter_number,
            page_texts=page_texts,
            image_paths=image_paths,
        )

        # Keep failures from BOTH passes (main + footnotes) for consolidated Excel
        processed_idxs = (processed_idxs_main or []) + (processed_idxs_footnotes or [])
        failed_inline_changes = (failed_inline_changes_main or []) + (failed_inline_changes_footnotes or [])

        print(f"Processed indices: {processed_idxs}")
        print(
            f"Inline failure rows collected: main={len(failed_inline_changes_main or [])}, "
            f"footnotes={len(failed_inline_changes_footnotes or [])}, "
            f"total={len(failed_inline_changes)}"
        )

        # with open(os.path.join(output_dir, 'save_after_paragraph_level_changes.pxml'), 'w', encoding='utf-8') as f:
        #     f.write(updated_pxmlcontent)


        # ══════════════════════════════════════════════════════════════════
        # PIPELINE 6 — Newline (new paragraph) insertions
        # ══════════════════════════════════════════════════════════════════

        print("\n[PIPELINE 6] Newline paragraph insertions...")
        result_with_ba_new.to_excel(os.path.join(output_dir, "result_with_ba_before_newline_insertion.xlsx"), index=False)
        all_newline_insertions = result_with_ba_new[
            (result_with_ba_new['text_before'] == '') &
            (result_with_ba_new['has_changes'] == True) &
            (result_with_ba_new['is_footnote_x'] == False)
        ].drop_duplicates(subset=['idx'], keep='first')
        all_newline_insertions.to_excel(os.path.join(output_dir, "all_newline_insertions_checking_after.xlsx"), index=False)
        all_newline_insertions = all_newline_insertions[
            ['idx', 'text', 'has_changes', 'type', 'paragraph_index']
        ]
        all_newline_insertions = return_closest_previous_par(
            result_with_ba, all_newline_insertions
        )
        all_newline_insertions_sections = fill_sections_backward_verbose(
            all_newline_insertions, no_changes_df,
            inline_replacement_df, inline_insertion_df, chapter_number,
            verbose=True, client=client, model_name=model_name,
            designator_patterns=designator_patterns
        )
        # inline_insertion_df.to_excel(os.path.join(output_dir, "inline_insertion_df.xlsx"), index=False)
        # inline_deletion_df.to_excel(os.path.join(output_dir, "inline_deletion_df.xlsx"), index=False)
        # no_changes_df.to_excel(os.path.join(output_dir, "no_changes_df.xlsx"), index=False)
        all_newline_insertions_sections.to_excel(os.path.join(output_dir, "all_newline_insertions_sections.xlsx"), index=False)
        # inline_replacement_df.to_excel(os.path.join(output_dir, "inline_replacement_df.xlsx"), index=False)

        section_dict, section_positions, pxml_content = parse_pxml_sections_from_str_dict_positions(updated_pxmlcontent)
        updated_pxmlcontent, failed_newline_insertions = process_all_newline_insertions_with_llm(
            pxml_content=pxml_content,
            section_dict=section_dict,
            section_positions=section_positions,
            all_newline_insertions=all_newline_insertions_sections,
            client=client,
            model_name=model_name,
            failed_log_path=os.path.join(output_dir, "failed_newline_insertions.txt"),
            pdf_path=pdf_path_for_superscript,
        )

        # with open(os.path.join(output_dir, 'save_afternew_line_insertions.pxml'), 'w', encoding='utf-8') as f:
        #     f.write(updated_pxmlcontent)

        # ══════════════════════════════════════════════════════════════════
        # PIPELINE 7 — New footnote insertions
        # ══════════════════════════════════════════════════════════════════


        all_inserted_footnote_revisions = result_with_ba[
                (result_with_ba['is_footnote_x'] == True) &
                (result_with_ba['has_changes'] == True) &
                (result_with_ba['text_before'] == '')
            ].drop_duplicates(subset=['idx'], keep='first')

        if 'parent_paragraph_idx_x' in all_inserted_footnote_revisions.columns:
            parent_para_text_map = (
                result_with_ba[['idx', 'text']]
                .dropna(subset=['idx'])
                .drop_duplicates(subset=['idx'], keep='first')
                .set_index('idx')['text']
            )
            all_inserted_footnote_revisions['full_para_where_footnote_is_present'] = (
                all_inserted_footnote_revisions['parent_paragraph_idx_x']
                .map(parent_para_text_map)
                .fillna('')
            )
        else:
            all_inserted_footnote_revisions['full_para_where_footnote_is_present'] = ''


        # all_inserted_footnote_revisions.to_excel(os.path.join(output_dir, "all_inserted_footnote_revisions.xlsx"), index=False)
        section_dict, section_positions, pxml_content = parse_pxml_sections_from_str_dict_positions(
            updated_pxmlcontent
        )

        print('section_dict------', section_dict.keys())

        all_inserted_footnote_revisions_sections = fill_sections_backward_verbose(
            all_inserted_footnote_revisions, no_changes_df,
            inline_replacement_df, inline_insertion_df, chapter_number,
            verbose=True, client=client, model_name=model_name,
            designator_patterns=designator_patterns
        )

        updated_pxmlcontent, failed_footnote_insertions = process_all_footnote_insertions(
            all_inserted_footnote_revisions=all_inserted_footnote_revisions_sections,
            pxml_content=pxml_content,
            model_name=MODEL_OPUS,
            failed_log_path=os.path.join(output_dir, "failed_footnote_insertions.txt")
        )
        print('---------------pipeline 7 completed, moving to pipeline 8 (Post Processing)-------------------')

        # with open(os.path.join(output_dir, 'save_afterfootnotes_insertions.pxml'), 'w', encoding='utf-8') as f:
        #     f.write(updated_pxmlcontent)

        # Save current state before failed revisions retry
        final_pxml_content = updated_pxmlcontent

        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(final_pxml_content)

        # ══════════════════════════════════════════════════════════
        # ── Structural changes: paragraph converted to list (bullet point) ──
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

        # CONSOLIDATED FAILED REVISIONS EXCEL
        # ══════════════════════════════════════════════════════════
        print(f"\n{'='*80}")
        print("Writing Consolidated Failed Revisions Excel...")
        print(f"{'='*80}")

        consolidated_excel_path = os.path.join(output_dir, "Failed_revisions.xlsx")

        # Check if any failures exist
        has_inline_failures = len(failed_inline_changes) > 0
        has_newline_failures = len(failed_newline_insertions) > 0
        has_footnote_failures = len(failed_footnote_insertions) > 0

        if has_inline_failures or has_newline_failures or has_footnote_failures:
            try:
                # Remove existing corrupt file if present
                if os.path.exists(consolidated_excel_path):
                    os.remove(consolidated_excel_path)
                with pd.ExcelWriter(consolidated_excel_path, engine='openpyxl') as writer:
                    # Define common styles
                    header_font = Font(bold=True, color='FFFFFF', size=11)
                    header_fill = PatternFill(start_color='2E7D32', end_color='2E7D32', fill_type='solid')
                    header_alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
                    cell_alignment = Alignment(horizontal='left', vertical='top', wrap_text=True)
                    thin_border = Border(
                        left=Side(style='thin'),
                        right=Side(style='thin'),
                        top=Side(style='thin'),
                        bottom=Side(style='thin')
                    )

                    def apply_sheet_formatting(ws, df, col_widths):
                        """Apply consistent formatting to a worksheet."""
                        for col_num in range(1, len(df.columns) + 1):
                            cell = ws.cell(row=1, column=col_num)
                            cell.font = header_font
                            cell.fill = header_fill
                            cell.alignment = header_alignment
                            cell.border = thin_border
                        for row_num in range(2, len(df) + 2):
                            for col_num in range(1, len(df.columns) + 1):
                                cell = ws.cell(row=row_num, column=col_num)
                                cell.alignment = cell_alignment
                                cell.border = thin_border
                        for col_letter, width in col_widths.items():
                            ws.column_dimensions[col_letter].width = width
                        ws.row_dimensions[1].height = 25
                        for row_num in range(2, len(df) + 2):
                            ws.row_dimensions[row_num].height = 50
                        ws.freeze_panes = 'A2'
                        ws.auto_filter.ref = ws.dimensions

                    # Sheet 1: Failed Inline Changes
                    if has_inline_failures:
                        df_inline = pd.DataFrame(failed_inline_changes)
                        col_order = ["Row Index", "Docx_Paragraph", "Section", "Designator", "Reason", 
                                     "Change Type", "Inserted Text", "Deleted Text", "Previous Context", "Post Context"]
                        col_order = [c for c in col_order if c in df_inline.columns]
                        df_inline = df_inline[col_order]
                        df_inline.to_excel(writer, index=False, sheet_name='Inline Changes')
                        ws = writer.sheets['Inline Changes']
                        col_widths = {'A': 10, 'B': 50, 'C': 15, 'D': 12, 'E': 55, 'F': 18, 'G': 40, 'H': 40, 'I': 30, 'J': 30}
                        apply_sheet_formatting(ws, df_inline, col_widths)
                        print(f"  ✓ Inline Changes: {len(failed_inline_changes)} rows")

                    # Sheet 2: Failed Newline Insertions
                    if has_newline_failures:
                        df_newline = pd.DataFrame(failed_newline_insertions)
                        df_newline = df_newline.rename(columns={
                            "idx": "Row Index",
                            "section": "Section",
                            "reason": "Reason",
                            "previous_para": "Previous Paragraph",
                            "new_text": "New Text",
                        })
                        df_newline.to_excel(writer, index=False, sheet_name='Newline Insertions')
                        ws = writer.sheets['Newline Insertions']
                        col_widths = {'A': 10, 'B': 15, 'C': 55, 'D': 50, 'E': 50}
                        apply_sheet_formatting(ws, df_newline, col_widths)
                        print(f"  ✓ Newline Insertions: {len(failed_newline_insertions)} rows")

                    # Sheet 3: Failed Footnote Insertions
                    if has_footnote_failures:
                        df_footnotes = pd.DataFrame(failed_footnote_insertions)
                        df_footnotes = df_footnotes.rename(columns={
                            "idx": "Row Index",
                            "section": "Section",
                            "superscript": "Superscript",
                            "reason": "Reason",
                            "text_before_superscript": "Text Before Superscript",
                            "footnote_text": "Footnote Text",
                        })
                        df_footnotes.to_excel(writer, index=False, sheet_name='Footnote Insertions')
                        ws = writer.sheets['Footnote Insertions']
                        col_widths = {'A': 12, 'B': 15, 'C': 12, 'D': 50, 'E': 60, 'F': 60}
                        apply_sheet_formatting(ws, df_footnotes, col_widths)
                        print(f"  ✓ Footnote Insertions: {len(failed_footnote_insertions)} rows")

                total_failures = len(failed_inline_changes) + len(failed_newline_insertions) + len(failed_footnote_insertions)
                print(f"\n📊 Consolidated Excel saved to: {consolidated_excel_path}")
                print(f"   Total failed revisions: {total_failures}")
            except Exception as excel_err:
                print(f"  ⚠️  Could not write Failed_revisions.xlsx: {excel_err}")
        else:
            print("✅ No failed revisions to report!")

        # ══════════════════════════════════════════════════════════════════
        # PIPELINE 9 — Retry failed inline deletions via LLM
        # ══════════════════════════════════════════════════════════════════
        from canada_pipeline.failed_revisions_handler import retry_failed_inline_deletions

        final_pxml_content, retry_success_count, retry_total, succeeded_indices, retry_rows_list = retry_failed_inline_deletions(
            failed_inline_changes=failed_inline_changes,
            pxml_content=final_pxml_content,
            client=client,
            model_name=model_name,
            chapter_number=chapter_number,
        )

        # Re-save the final output with recovered deletions
        if retry_success_count > 0:
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(final_pxml_content)
            print(f"  📄 Updated output file with {retry_success_count} recovered deletions.")

            # Color-mark rows in Failed_revisions Excel: green=recovered, red=still failed
            if os.path.exists(consolidated_excel_path):
                try:
                    from openpyxl import load_workbook
                    from openpyxl.styles import PatternFill

                    green_fill = PatternFill(start_color='C6EFCE', end_color='C6EFCE', fill_type='solid')
                    red_fill = PatternFill(start_color='FFC7CE', end_color='FFC7CE', fill_type='solid')

                    recovered_keys = set()
                    for si in succeeded_indices:
                        r = retry_rows_list[si]
                        recovered_keys.add((
                            str(r.get('Row Index', '')),
                            str(r.get('Deleted Text', '')).strip()
                        ))
                    all_attempted_keys = set()
                    for r in retry_rows_list:
                        all_attempted_keys.add((
                            str(r.get('Row Index', '')),
                            str(r.get('Deleted Text', '')).strip()
                        ))

                    if not os.path.exists(consolidated_excel_path):
                        print(f"  ⚠️  Failed_revisions.xlsx not found, skipping color update.")
                    else:
                        wb = load_workbook(consolidated_excel_path)
                        if 'Inline Changes' in wb.sheetnames:
                            ws = wb['Inline Changes']
                            headers = {cell.value: cell.column for cell in ws[1]}
                            row_idx_col = headers.get('Row Index')
                            del_text_col = headers.get('Deleted Text')

                            green_count = 0
                            red_count = 0
                            if row_idx_col and del_text_col:
                                for row_num in range(2, ws.max_row + 1):
                                    key = (
                                        str(ws.cell(row=row_num, column=row_idx_col).value or ''),
                                        str(ws.cell(row=row_num, column=del_text_col).value or '').strip()
                                    )
                                    if key in recovered_keys:
                                        for col in range(1, ws.max_column + 1):
                                            ws.cell(row=row_num, column=col).fill = green_fill
                                        green_count += 1
                                    elif key in all_attempted_keys:
                                        for col in range(1, ws.max_column + 1):
                                            ws.cell(row=row_num, column=col).fill = red_fill
                                        red_count += 1

                            wb.save(consolidated_excel_path)
                            print(f"  📊 Marked {green_count} rows GREEN (recovered), {red_count} rows RED (still failed) in Failed_revisions.xlsx")

                except Exception as e:
                    print(f"  ⚠️  Could not update Failed_revisions.xlsx colors: {e}")

        # ══════════════════════════════════════════════════════════
        # PIPELINE-10: Post Processing (FINAL step)
        # ══════════════════════════════════════════════════════════
        print(f"\n{'='*80}")
        print("PIPELINE-10: Post Processing (Final)")
        print(f"{'='*80}")
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

        print(f"\n{'='*80}")
        print(f"✅ Output saved to: {output_file}")
        print(f"{'='*80}")

        end_time = time.time()
        elapsed = end_time - start_time
        print(f"\nTotal runtime: {elapsed:.2f} seconds")
        print(f"Pipeline finished at: {time.strftime('%Y-%m-%d %H:%M:%S')}")


    except Exception as e:
        print(f"\nPIPELINE FAILED: {e}")
        raise

    finally:
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
        log_f.close()
        print(f"Log saved to: {log_file}")


class Tee:
    """Writes output to both console and a log file simultaneously."""
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for stream in self.streams:
            stream.write(data)
            stream.flush()

    def flush(self):
        for stream in self.streams:
            stream.flush()


if __name__ == "__main__":
    # Prevent Windows from sleeping during batch run
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    print("[KEEP-AWAKE] Sleep prevention enabled.")

    separator = "=" * 80
    print(f"\n{separator}")
    print(f"BATCH PROCESSING: {BATCH_FOLDER}")
    print(f"{separator}")

    pairs = discover_file_pairs(BATCH_FOLDER)
    print(f"\nFound {len(pairs)} file pairs to process.\n")

    results_summary = []
    for i, pair in enumerate(pairs, 1):
        print(f"\n{separator}")
        print(f"[{i}/{len(pairs)}] Processing: {pair['folder_name']}")
        print(f"{separator}")
        try:
            run_pipeline(pair["docx_path"], pair["pxml_path"], pair["output_dir"])
            results_summary.append((pair["folder_name"], "SUCCESS"))
        except Exception as e:
            results_summary.append((pair["folder_name"], f"FAILED: {e}"))
            print(f"\nFAILED: {pair['folder_name']} - {e}")

    print(f"\n\n{separator}")
    print("BATCH RESULTS SUMMARY")
    print(f"{separator}")
    for name, status in results_summary:
        print(f"  {name}: {status}")
    print(f"{separator}")

    # Restore normal sleep behavior
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    print("[KEEP-AWAKE] Sleep prevention disabled. Safe to sleep now.")

