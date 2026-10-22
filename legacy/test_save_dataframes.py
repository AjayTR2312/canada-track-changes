"""
Test script: Takes DOCX + PXML input and saves key DataFrames to Excel files.
Outputs:
  1) result_with_ba.xlsx
  2) all_revisions_except_newline_insertions1.xlsx
  3) all_revisions_except_newline_insertions_footnotes.xlsx
  4) all_newline_insertions_sections.xlsx
"""
import os
import sys
import re
import time
import pandas as pd

# Pipeline imports
from continous_ncont_insertions import *
from newline_insertions import *
from insertions_paragraphs import *
from section_name_edge_cases import *
from finaloutputs.chapter_section import extract_designator_pattern_using_llm
from inline_replacement_section import SectionLineReplacement
from section_insertion import SectionExtractor
from track_changes_df import process_docx
from section_deletion import section_deletion_filling
from config import API_KEY, build_anthropic_client as _build_client_config
from paragraph_level_trackchanges import parse_pxml_sections_from_str_new
from inline_footnotes_insertion import (
    parse_pxml_sections_from_str_dict_positions, build_anthropic_client
)
from newline_insertions_new import return_closest_previous_par
from sectionnumber_filling import fill_sections_backward_verbose
from config import MODEL_OPUS


def run_test(docx_path: str, pxml_path: str, output_dir: str):
    os.makedirs(output_dir, exist_ok=True)
    missing_revisions_output = os.path.join(output_dir, "missing_revisions.txt")

    print(f"\n{'='*80}")
    print(f"Test started at: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"DOCX: {docx_path}")
    print(f"PXML: {pxml_path}")
    print(f"Output: {output_dir}")
    print(f"{'='*80}")

    # ═══ PIPELINE 1: Track Changes → DataFrame ═══
    print("\n[STEP 1] Extracting track changes...")
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
    print(f"  Chapter: {chapter_number}")

    # Superscript extraction
    print("\n[STEP 1b] Superscript extraction...")
    from superscript_extraction_from_pdf import convert_docx_to_pdf, process_excel as process_superscripts
    try:
        pdf_path_for_superscript = convert_docx_to_pdf(docx_path, output_dir=output_dir)
        superscript_excel = os.path.join(output_dir, "superscript_extraction.xlsx")
        _, result_with_ba = process_superscripts(
            docx_path=docx_path, pdf_path=pdf_path_for_superscript,
            output_excel_path=superscript_excel, result_with_ba=result_with_ba
        )
    except Exception as e:
        print(f"  [WARN] Superscript extraction failed: {e}")

    inline_insertion_df = result[result['type'] == 'inline_insertion']
    inline_replacement_df = result[result['type'] == 'inline_replacement']

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

    # ═══ PIPELINE 2: Section Deletion ═══
    # print("\n[STEP 2] Section deletion...")
    # updated_pxmlcontent, remaining_rows_inlinedeletion, rows_to_delete = \
    #     section_deletion_filling(result, df_changes, chapter_number, no_changes_df, df_remaining, pxml_content, verbose=True)

    # # ═══ PIPELINE 3: Section Number Replacement ═══
    # print("\n[STEP 3] Section number replacement...")
    # inlinereplacement_df = result_with_ba[
    #     (result_with_ba['type'] == 'inline_replacement') & (result_with_ba['is_footnote_x'] == False)
    # ]
    # processor = SectionLineReplacement(inlinereplacement_df, API_KEY, os.path.join(output_dir, "temp.pxml"))
    # processed_df = processor.process_dataframe(df_changes)
    # updated_pxmlcontent, updated_inlinereplacement_df, replaced_indices = \
    #     processor.create_deletion_column(chapter_number, updated_pxmlcontent, missing_revisions_output)

    # # ═══ PIPELINE 4: Section Insertion (simplified - just get indices) ═══
    # print("\n[STEP 4] Section insertion (index collection)...")
    # sec_extractor = SectionExtractor(pxml_path)
    # inline_insertion_df_without_footnotes = result_with_ba[
    #     (result_with_ba['type'] == 'inline_insertion') & (result_with_ba['is_footnote_x'] == False)
    # ].drop_duplicates(subset=['idx'], keep='first')
    # sections = sec_extractor.extract_inserted_sections(inline_insertion_df_without_footnotes, chapter_number)

    # sections_insertion_indexes = []
    # for each_section in sections:
    #     start_pi = int(each_section['start_paragraph_index'])
    #     end_pi = int(each_section['end_paragraph_index'])
    #     sections_insertion_indexes.extend(range(start_pi, end_pi + 1))

    # # ═══ PIPELINE 5 prep: Build filtered DataFrames ═══
    # print("\n[STEP 5] Building revision DataFrames...")

    # WORKSPACE_ID = os.getenv("TR_WORKSPACE_ID", "")
    # MODEL_NAME = MODEL_OPUS
    # client, model_name = build_anthropic_client(WORKSPACE_ID, MODEL_NAME)

    # if 'text_before' in result_with_ba.columns:
    #     text_before_mask = result_with_ba['text_before'].notna()
    #     result_with_ba.loc[text_before_mask, 'text_before'] = (
    #         result_with_ba.loc[text_before_mask, 'text_before'].astype(str).str.replace(r'^\s*[\.,]+\s*$', '', regex=True)
    #     )

    # result_with_ba = result_with_ba.drop_duplicates()
    # already_processed = set(rows_to_delete['paragraph_index'].tolist()) | set(replaced_indices) | set(sections_insertion_indexes)
    # result_with_ba_new = result_with_ba[~result_with_ba['idx'].isin(already_processed)]

    # all_revisions_except_newline_insertions_new = result_with_ba_new
    # result_with_ba_sorted = result_with_ba_new.sort_values('idx').reset_index(drop=True)
    # valid_text = result_with_ba_sorted[
    #     result_with_ba_sorted['text_before'].notna() & (result_with_ba_sorted['text_before'] != '') & (result_with_ba_sorted['text_before'].str.len() > 5)
    # ][['idx', 'text_before']].reset_index(drop=True)

    # def get_previous_para(idx):
    #     candidates = valid_text[valid_text['idx'] < idx]
    #     return None if candidates.empty else candidates.iloc[-1]['text_before']

    # def get_post_para(idx):
    #     candidates = valid_text[valid_text['idx'] > idx]
    #     return None if candidates.empty else candidates.iloc[0]['text_before']

    # all_revisions_except_newline_insertions_new['previous_para'] = all_revisions_except_newline_insertions_new['idx'].apply(get_previous_para)
    # all_revisions_except_newline_insertions_new['post_para'] = all_revisions_except_newline_insertions_new['idx'].apply(get_post_para)

    # # DataFrame 2: all_revisions_except_newline_insertions1 (inline non-footnote)
    # all_revisions_except_newline_insertions1 = all_revisions_except_newline_insertions_new[
    #     ((all_revisions_except_newline_insertions_new['text_before'] != '') | (all_revisions_except_newline_insertions_new['type'] == 'inline_deletion')) &
    #     (all_revisions_except_newline_insertions_new['has_changes'] == True) & (all_revisions_except_newline_insertions_new['is_footnote_x'] == False)
    # ]
    # all_revisions_except_newline_insertions1 = fill_sections_backward_verbose(
    #     all_revisions_except_newline_insertions1, no_changes_df, inline_replacement_df, inline_insertion_df, chapter_number,
    #     verbose=True, client=client, model_name=model_name, designator_patterns=designator_patterns
    # )

    # # DataFrame 3: all_revisions_except_newline_insertions_footnotes
    # all_revisions_except_newline_insertions_footnotes = all_revisions_except_newline_insertions_new[
    #     (all_revisions_except_newline_insertions_new['text_before'] != '') &
    #     (all_revisions_except_newline_insertions_new['has_changes'] == True) &
    #     (all_revisions_except_newline_insertions_new['is_footnote_x'] == True)
    # ]
    # if 'parent_paragraph_idx_x' in all_revisions_except_newline_insertions_footnotes.columns:
    #     parent_para_text_map = result_with_ba[['idx', 'text']].dropna(subset=['idx']).drop_duplicates(subset=['idx'], keep='first').set_index('idx')['text']
    #     all_revisions_except_newline_insertions_footnotes['parent_paragraph_text'] = all_revisions_except_newline_insertions_footnotes['parent_paragraph_idx_x'].map(parent_para_text_map).fillna('')
    # else:
    #     all_revisions_except_newline_insertions_footnotes['parent_paragraph_text'] = ''

    # all_revisions_except_newline_insertions_footnotes = fill_sections_backward_verbose(
    #     all_revisions_except_newline_insertions_footnotes, no_changes_df, inline_replacement_df, inline_insertion_df, chapter_number,
    #     verbose=True, client=client, model_name=model_name, designator_patterns=designator_patterns
    # )

    # # DataFrame 4: all_newline_insertions_sections
    # all_newline_insertions = result_with_ba_new[
    #     (result_with_ba_new['text_before'] == '') & (result_with_ba_new['has_changes'] == True) & (result_with_ba_new['is_footnote_x'] == False)
    # ].drop_duplicates(subset=['idx'], keep='first')
    # all_newline_insertions = all_newline_insertions[['idx', 'text', 'has_changes', 'type', 'paragraph_index']]
    # all_newline_insertions = return_closest_previous_par(result_with_ba, all_newline_insertions)
    # all_newline_insertions_sections = fill_sections_backward_verbose(
    #     all_newline_insertions, no_changes_df, inline_replacement_df, inline_insertion_df, chapter_number,
    #     verbose=True, client=client, model_name=model_name, designator_patterns=designator_patterns
    # )

    # ═══ SAVE ALL DataFrames ═══
    print(f"\n{'='*80}")
    print("Saving DataFrames to Excel...")
    print(f"{'='*80}")

    path1 = os.path.join(output_dir, "1_result_with_ba.xlsx")
    # path2 = os.path.join(output_dir, "2_all_revisions_except_newline_insertions1.xlsx")
    # path3 = os.path.join(output_dir, "3_all_revisions_except_newline_insertions_footnotes.xlsx")
    # path4 = os.path.join(output_dir, "4_all_newline_insertions_sections.xlsx")

    result_with_ba.to_excel(path1, index=False)
    print(f"  ✅ Saved: {path1} ({len(result_with_ba)} rows)")

    # all_revisions_except_newline_insertions1.to_excel(path2, index=False)
    # print(f"  ✅ Saved: {path2} ({len(all_revisions_except_newline_insertions1)} rows)")

    # all_revisions_except_newline_insertions_footnotes.to_excel(path3, index=False)
    # print(f"  ✅ Saved: {path3} ({len(all_revisions_except_newline_insertions_footnotes)} rows)")

    # all_newline_insertions_sections.to_excel(path4, index=False)
    # print(f"  ✅ Saved: {path4} ({len(all_newline_insertions_sections)} rows)")

    print(f"\n✅ All done! Files saved in: {output_dir}")


if __name__ == "__main__":
    docx_path = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\Test_9 (May 12, 2026)_batch_running_new\CDN_FI_EDRM_1\CDN_FI_EDRM 1.docx"
    pxml_path = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\Test_9 (May 12, 2026)_batch_running_new\CDN_FI_EDRM_1\CDN_FI_EDRM 1 (chapter and all children).pxml"
    output_dir = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\Test_9 (May 12, 2026)_batch_running_new\CDN_FI_EDRM_1"

    run_test(docx_path, pxml_path, output_dir)
