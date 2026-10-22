"""
Dataframe Enricher Module
─────────────────────────
Takes a DOCX file and a prevendor PXML file as input and returns a SINGLE
enriched dataframe with ALL track changes in the exact order they appear
in MS Word's revision side pane.

For each track change row:
  - type: inline_deletion / inline_insertion / inline_replacement / paragraph_addition / paragraph_deletion
  - paragraph_index: which paragraph it belongs to
  - paragraph_text: the full text of the paragraph it belongs to
  - deleted_text: what was deleted
  - inserted_text: what was inserted
  - prev_context: 20 characters before the change (within the paragraph)
  - post_context: 20 characters after the change (within the paragraph)
  - previous_para: closest non-empty previous paragraph text
  - post_para: closest non-empty next paragraph text
  - section: section number it belongs to (from prevendor PXML)
  - author: who made the change
  - date: when the change was made
"""

import os
import re
import pandas as pd
from typing import Optional, Dict, Any


def _get_previous_para_text(para_idx: int, all_paragraphs_df: pd.DataFrame) -> Optional[str]:
    """Get the closest non-empty previous paragraph text before para_idx."""
    candidates = all_paragraphs_df[
        (all_paragraphs_df['idx'] < para_idx) &
        (all_paragraphs_df['text'].notna()) &
        (all_paragraphs_df['text'].str.strip() != '') &
        (all_paragraphs_df['text'].str.len() > 3) &
        (all_paragraphs_df['is_footnote'] == False)
    ].sort_values('idx', ascending=False)

    if candidates.empty:
        return None
    return candidates.iloc[0]['text']


def _get_post_para_text(para_idx: int, all_paragraphs_df: pd.DataFrame) -> Optional[str]:
    """Get the closest non-empty next paragraph text after para_idx."""
    candidates = all_paragraphs_df[
        (all_paragraphs_df['idx'] > para_idx) &
        (all_paragraphs_df['text'].notna()) &
        (all_paragraphs_df['text'].str.strip() != '') &
        (all_paragraphs_df['text'].str.len() > 3) &
        (all_paragraphs_df['is_footnote'] == False)
    ].sort_values('idx', ascending=True)

    if candidates.empty:
        return None
    return candidates.iloc[0]['text']


def enrich_with_section_number(
    df: pd.DataFrame,
    no_changes_df: pd.DataFrame,
    inline_replacement_df: pd.DataFrame,
    inline_insertion_df: pd.DataFrame,
    chapter_number: str,
    designator_patterns: list,
    client=None,
    model_name: str = None,
    verbose: bool = True,
) -> pd.DataFrame:
    """
    Fill section numbers for each row using backward scan + LLM.
    Wrapper around fill_sections_backward_verbose from sectionnumber_filling.
    """
    from sectionnumber_filling import fill_sections_backward_verbose

    df = fill_sections_backward_verbose(
        df,
        no_changes_df,
        inline_replacement_df,
        inline_insertion_df,
        chapter_number,
        verbose=verbose,
        client=client,
        model_name=model_name,
        designator_patterns=designator_patterns,
    )
    return df


def build_enriched_dataframe(
    docx_path: str,
    pxml_path: str,
    client=None,
    model_name: str = None,
    context_chars: int = 20,
    verbose: bool = True,
    skip_section_filling: bool = False,
) -> Dict[str, Any]:
    """
    Main entry point: Takes a DOCX file and prevendor PXML file and returns
    a SINGLE enriched dataframe with all track changes in MS Word revision
    pane order.

    Parameters
    ----------
    docx_path : str
        Path to the DOCX file with tracked changes.
    pxml_path : str
        Path to the prevendor PXML file.
    client : optional
        Anthropic client instance. If None, will be created internally.
    model_name : str, optional
        Model name to use for LLM calls. If None, uses default from config.
    context_chars : int
        Number of characters for prev_context/post_context (default 20).
    verbose : bool
        Whether to print verbose output.
    skip_section_filling : bool
        If True, skip the LLM-based section number filling (Steps 3 & 4).
        Useful when section numbers are not needed (e.g. validation flow).

    Returns
    -------
    dict with keys:
        - 'revisions_df': Single DataFrame with ALL track changes in order,
            enriched with previous_para, post_para, section.
        - 'no_changes_df': Paragraphs with no changes.
        - 'df_changes': Raw track changes dataframe.
        - 'all_paragraphs_df': All paragraphs dataframe.
        - 'chapter_number': Extracted chapter number.
        - 'designator_patterns': List of designator patterns from PXML.
        - 'pxml_content': The PXML file content.
    """
    from track_changes_df import process_docx
    from finaloutputs.chapter_section import extract_designator_pattern_using_llm
    from inline_footnotes_insertion import build_anthropic_client
    from config import MODEL_OPUS

    # ─── Step 1: Extract track changes from DOCX ─────────────────────────────
    if verbose:
        print(f"\n{'='*80}")
        print(f"[ENRICHER] Processing DOCX: {os.path.basename(docx_path)}")
        print(f"[ENRICHER] PXML: {os.path.basename(pxml_path)}")
        print(f"{'='*80}")

    results = process_docx(docx_path, context_chars=context_chars)
    extractor_obj = results['extractor']

    no_changes_df = results['no_changes_df']
    df_remaining = results['df_remaining']
    result = results['result']
    df_changes = results['df_changes']
    chapter_number = results['chapter_number']

    if verbose:
        print(f"  Chapter from DOCX: {chapter_number}")
        print(f"  Total track changes (df_changes): {len(df_changes)} rows")
        print(f"  Total paragraphs: {len(df_remaining)} rows")

    # ─── Step 2: Build the revisions dataframe in MS Word side-pane order ─────
    # df_changes is already in document order (= MS Word revision pane order)
    if df_changes.empty:
        if verbose:
            print("  [WARN] No track changes found in DOCX.")
        revisions_df = pd.DataFrame()
    else:
        # Start from df_changes — this is the exact revision pane order
        revisions_df = df_changes.copy().reset_index(drop=True)

        # Split inline_replacement into two rows: one deletion + one insertion
        expanded_rows = []
        for _, row in revisions_df.iterrows():
            if row['type'] == 'inline_replacement':
                # Row 1: the deletion part
                del_row = row.copy()
                del_row['type'] = 'inline_deletion'
                del_row['inserted_text'] = None
                del_row['track_change'] = str(row.get('deleted_text', '') or '')
                expanded_rows.append(del_row)
                # Row 2: the insertion part
                ins_row = row.copy()
                ins_row['type'] = 'inline_insertion'
                ins_row['deleted_text'] = None
                ins_row['track_change'] = str(row.get('inserted_text', '') or '')
                expanded_rows.append(ins_row)
            else:
                row_copy = row.copy()
                # track_change column: show the relevant text for this change type
                if row['type'] in ('inline_deletion', 'paragraph_deletion'):
                    row_copy['track_change'] = str(row.get('deleted_text', '') or '')
                else:
                    row_copy['track_change'] = str(row.get('inserted_text', '') or '')
                expanded_rows.append(row_copy)

        revisions_df = pd.DataFrame(expanded_rows).reset_index(drop=True)

        # Add a revision_order column (1-based, matches Word side pane order)
        revisions_df.insert(0, 'revision_order', range(1, len(revisions_df) + 1))

        # Add paragraph text (the full text of the paragraph this change belongs to)
        para_text_map = df_remaining.set_index('idx')['text'].to_dict()
        revisions_df['paragraph_text'] = revisions_df['paragraph_index'].map(para_text_map).fillna('')

        # Trim prev_context and post_context to requested char length
        if 'prev_context' in revisions_df.columns:
            revisions_df['prev_context'] = revisions_df['prev_context'].fillna('').astype(str).str[-context_chars:]
        if 'post_context' in revisions_df.columns:
            revisions_df['post_context'] = revisions_df['post_context'].fillna('').astype(str).str[:context_chars]

        # Add previous_para and post_para
        if verbose:
            print(f"\n[ENRICHER] Adding previous_para and post_para...")
        all_paragraphs_df = df_remaining.copy()

        revisions_df['previous_para'] = revisions_df['paragraph_index'].apply(
            lambda pidx: _get_previous_para_text(pidx, all_paragraphs_df)
        )
        revisions_df['post_para'] = revisions_df['paragraph_index'].apply(
            lambda pidx: _get_post_para_text(pidx, all_paragraphs_df)
        )

        if verbose:
            print(f"  previous_para filled: {revisions_df['previous_para'].notna().sum()}/{len(revisions_df)}")
            print(f"  post_para filled: {revisions_df['post_para'].notna().sum()}/{len(revisions_df)}")

    # ─── Step 3: Read PXML and extract designator patterns ────────────────────
    with open(pxml_path, 'r', encoding='utf-8') as f:
        pxml_content = f.read()

    designator_patterns = []

    if not skip_section_filling:
        pxml_chapter_number, section_pattern, designator_patterns = extract_designator_pattern_using_llm(pxml_content, client=client)

        if chapter_number is None or (isinstance(chapter_number, str) and chapter_number.strip() == ''):
            chapter_number = pxml_chapter_number

        if verbose:
            print(f"  Designator patterns found: {len(designator_patterns) if designator_patterns else 0}")
            print(f"  Chapter number: {chapter_number}")

        # ─── Step 4: Fill section numbers ─────────────────────────────────────────
        if not revisions_df.empty:
            # Setup LLM client
            if client is None:
                WORKSPACE_ID = os.getenv("TR_WORKSPACE_ID", "")
                MODEL_NAME = model_name or MODEL_OPUS
                client, model_name = build_anthropic_client(WORKSPACE_ID, MODEL_NAME)

            # Prepare source dataframes for section filling
            inline_replacement_df = result[result['type'] == 'inline_replacement']
            inline_insertion_df = result[result['type'] == 'inline_insertion']

            if verbose:
                print(f"\n[ENRICHER] Filling section numbers ({len(revisions_df)} rows)...")

            # fill_sections_backward_verbose expects 'idx' column — use paragraph_index
            if 'idx' not in revisions_df.columns:
                revisions_df['idx'] = revisions_df['paragraph_index']

            revisions_df = enrich_with_section_number(
                revisions_df, no_changes_df, inline_replacement_df, inline_insertion_df,
                chapter_number, designator_patterns, client=client, model_name=model_name, verbose=verbose
            )
    else:
        if verbose:
            print(f"\n[ENRICHER] Skipping section number filling (skip_section_filling=True)")

    # ─── Step 5: Reorder columns for clarity ──────────────────────────────────
    if not revisions_df.empty:
        preferred_order = [
            'revision_order', 'type', 'track_change', 'paragraph_index', 'section',
            'deleted_text', 'inserted_text',
            'prev_context', 'post_context',
            'paragraph_text', 'previous_para', 'post_para',
            'author', 'date', 'has_bullet_point',
        ]
        # Keep only columns that exist, then append any extras
        existing_cols = [c for c in preferred_order if c in revisions_df.columns]
        extra_cols = [c for c in revisions_df.columns if c not in preferred_order]
        revisions_df = revisions_df[existing_cols + extra_cols]

    if verbose:
        print(f"\n{'='*80}")
        print(f"[ENRICHER] DONE - Summary:")
        print(f"  Total revisions: {len(revisions_df)}")
        if not revisions_df.empty:
            type_counts = revisions_df['type'].value_counts()
            for t, c in type_counts.items():
                print(f"    {t}: {c}")
            print(f"  Sections filled: {revisions_df['section'].notna().sum()}/{len(revisions_df)}")
        print(f"{'='*80}\n")

    return {
        'revisions_df': revisions_df,
        'no_changes_df': no_changes_df,
        'df_changes': df_changes,
        'all_paragraphs_df': df_remaining,
        'chapter_number': chapter_number,
        'designator_patterns': designator_patterns,
        'pxml_content': pxml_content,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Standalone usage / testing
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":

    # ── Set file paths here ──────────────────────────────────────────────────
    docx_path = r"C:\Users\C302473\Downloads\20260612 Chapter 5 UPDATES2026-1.docx"
    pxml_path = r"C:\Users\C302473\Downloads\CDN_OM_TFF 5 (chapter and all children).pxml"
    output_excel = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\test12\CDN_HW_CLC ch 45\enriched_output_newfile_gopal.xlsx"

    enriched = build_enriched_dataframe(docx_path, pxml_path, context_chars=20, verbose=True)

    # Save the single revisions dataframe
    revisions_df = enriched['revisions_df']
    if not revisions_df.empty:
        revisions_df.to_excel(output_excel, index=False)
        print(f"\n✅ Revisions dataframe saved to: {output_excel}")
        print(f"   Rows: {len(revisions_df)}")
        print(f"   Columns: {list(revisions_df.columns)}")
    else:
        print("No track changes found in DOCX.")

