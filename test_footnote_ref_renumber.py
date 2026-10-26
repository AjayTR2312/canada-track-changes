"""
Test script for footnote reference-number renumbering.

Runs the full extraction pipeline (track_changes_df → superscript extraction)
to get result_with_ba, then feeds the footnote rows into the renumbering handler.
Saves the updated PXML so you can inspect the result.

Usage:
    python test_footnote_ref_renumber.py

Edit DOCX_PATH and PXML_PATH below to point to your test files.
"""

import os, sys, re

# ── CONFIGURE THESE ──────────────────────────────────────────────────────────
DOCX_PATH = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\Test 17 (July 14, 2026)\CDN_OO_OW Ch 3\CR-Done_CDN_OO_OW Chapter 3.doc"
PXML_PATH = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\Test 17 (July 14, 2026)\CDN_OO_OW Ch 3\CDN_OO_OW 3 (chapter and all children).pxml"
OUTPUT_DIR = os.path.dirname(DOCX_PATH)
# ─────────────────────────────────────────────────────────────────────────────

# Ensure project root is on the path
project_dir = os.path.dirname(os.path.abspath(__file__))
if project_dir not in sys.path:
    sys.path.insert(0, project_dir)

import pandas as pd


def main():
    print(f"DOCX: {DOCX_PATH}")
    print(f"PXML: {PXML_PATH}")
    print(f"Output dir: {OUTPUT_DIR}")
    assert os.path.isfile(DOCX_PATH), f"DOCX not found: {DOCX_PATH}"
    assert os.path.isfile(PXML_PATH), f"PXML not found: {PXML_PATH}"

    # ── Step 1: Extract track changes ────────────────────────────────────────
    print("\n" + "=" * 70)
    print("STEP 1: Extracting track changes from DOCX...")
    from canada_pipeline.track_changes_df import process_docx
    results = process_docx(DOCX_PATH)
    extractor_obj = results["extractor"]
    result_with_ba = extractor_obj.build_before_after_df()
    try:
        result_with_ba = extractor_obj.enrich_footnote_anchors()
    except RuntimeError as e:
        print(f"  [WARN] enrich_footnote_anchors skipped: {e}")

    chapter_number = results["chapter_number"]
    print(f"  Chapter: {chapter_number}")
    print(f"  Total rows: {len(result_with_ba)}")

    # ── Step 2: Superscript extraction (for text_before_superscript) ─────────
    print("\n" + "=" * 70)
    print("STEP 2: Superscript extraction...")
    try:
        from canada_pipeline.superscript_extraction_from_pdf import (
            convert_docx_to_pdf, process_excel as process_superscripts,
        )
        pdf_path = convert_docx_to_pdf(DOCX_PATH, output_dir=OUTPUT_DIR)
        superscript_excel = os.path.join(OUTPUT_DIR, "test_renum_superscript.xlsx")
        _, result_with_ba = process_superscripts(
            docx_path=DOCX_PATH, pdf_path=pdf_path,
            output_excel_path=superscript_excel, result_with_ba=result_with_ba,
        )
        print(f"  Superscript extraction done. Excel: {superscript_excel}")
    except Exception as e:
        print(f"  [WARN] Superscript extraction failed: {e}")
        print("  Continuing without pdf_superscript (text_before_superscript may still be populated from DOCX).")

    # ── Step 3: Filter footnote reference-number rows ────────────────────────
    print("\n" + "=" * 70)
    print("STEP 3: Filtering footnote reference-number rows...")
    fn_rows = result_with_ba[
        (result_with_ba["is_footnote_x"] == True)
        & (result_with_ba["has_changes"] == True)
        & (result_with_ba["text_before"] != "")
        & (result_with_ba["type"] == "inline_replacement")
    ].copy()

    # Further filter to digit→digit only
    def _is_digit(v):
        return str(v).strip().isdigit() if v is not None else False

    fn_renum_rows = fn_rows[
        fn_rows["deleted_text"].apply(_is_digit) & fn_rows["inserted_text"].apply(_is_digit)
    ]

    print(f"  Total footnote rows (is_footnote_x & has_changes & text_before!=''): {len(fn_rows)}")
    print(f"  Footnote ref-number replacements (digit->digit): {len(fn_renum_rows)}")

    if fn_renum_rows.empty:
        print("\n  No footnote reference-number replacements found. Nothing to test.")
        return

    # Show the candidate rows
    cols = [c for c in ["idx", "section", "type", "deleted_text", "inserted_text",
                         "text_before_superscript", "superscript"] if c in fn_renum_rows.columns]
    print("\n  Candidate rows:")
    for _, r in fn_renum_rows.iterrows():
        tbs = str(r.get("text_before_superscript", ""))
        tbs_preview = ("..." + tbs[-50:]) if len(tbs) > 50 else tbs
        print(f"    idx={r['idx']} section={r.get('section','?')} "
              f"del={r['deleted_text']} ins={r['inserted_text']} "
              f"tbs='{tbs_preview}'")

    # ── Step 4: Fill sections ────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("STEP 4: Filling section designators...")
    from canada_pipeline.sectionnumber_filling import fill_sections_backward_verbose
    from canada_pipeline.inline_footnotes_insertion import build_anthropic_client
    from canada_pipeline.config import MODEL_SONNET
    from canada_pipeline.chapter_section import extract_designator_pattern_using_llm

    result = results["result"]
    no_changes_df = results["no_changes_df"]
    inline_replacement_df = result[result["type"] == "inline_replacement"]
    inline_insertion_df = result[result["type"] == "inline_insertion"]

    with open(PXML_PATH, "r", encoding="utf-8") as f:
        pxml_content = f.read()

    _, _, designator_patterns = extract_designator_pattern_using_llm(pxml_content)
    WORKSPACE_ID = os.getenv("TR_WORKSPACE_ID", "")
    client, model_name = build_anthropic_client(WORKSPACE_ID, MODEL_SONNET)

    fn_renum_rows_with_sections = fill_sections_backward_verbose(
        fn_renum_rows, no_changes_df, inline_replacement_df, inline_insertion_df,
        chapter_number, verbose=True, client=client, model_name=model_name,
        designator_patterns=designator_patterns,
    )

    # Show sections assigned
    print("\n  After section filling:")
    for _, r in fn_renum_rows_with_sections.iterrows():
        print(f"    idx={r['idx']} section='{r.get('section', '?')}' "
              f"del={r.get('deleted_text','')} ins={r.get('inserted_text','')}")

    # ── Step 5: Run the renumbering ──────────────────────────────────────────
    print("\n" + "=" * 70)
    print("STEP 5: Running footnote reference-number renumbering...")
    from canada_pipeline.footnote_reference_renumber import process_footnote_reference_renumbering

    updated_pxml, failed = process_footnote_reference_renumbering(
        result_df_footnotes=fn_renum_rows_with_sections,
        pxml_content=pxml_content,
        chapter_number=chapter_number,
        verbose=True,
    )

    # ── Step 6: Save & verify ────────────────────────────────────────────────
    output_pxml = os.path.join(OUTPUT_DIR, "test_renum_output.pxml")
    with open(output_pxml, "w", encoding="utf-8") as f:
        f.write(updated_pxml)
    print(f"\n  Output saved: {output_pxml}")

    # Count trace.deleted inside footnote.reference tags
    trace_count = len(re.findall(
        r'<footnote\.reference[^>]*>.*?<trace\.deleted>.*?</trace\.deleted>.*?</footnote\.reference>',
        updated_pxml,
    ))
    print(f"  Tracked footnote-reference changes in output: {trace_count}")

    if failed:
        print(f"\n  FAILED rows ({len(failed)}):")
        for f_row in failed:
            print(f"    idx={f_row['idx']} reason={f_row['reason']}")
    else:
        print(f"\n  All {len(fn_renum_rows)} rows applied successfully!")

    print("\n" + "=" * 70)
    print("DONE. Open test_renum_output.pxml and search for '<trace.deleted>' ")
    print("inside <footnote.reference> tags to verify the changes.")


if __name__ == "__main__":
    main()
