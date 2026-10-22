"""
Testing Footnote Superscript Extraction
========================================
Standalone script to test superscript extraction from PDF.
Uses fuzzy matching to find top 5 candidate superscript locations,
then LLM picks the correct one.

Two modes:
  1. Streamlit UI:   streamlit run testing_footnote_superscript.py
  2. Command line:   python testing_footnote_superscript.py --docx <path> --pdf <path> [--output <path>]
"""

import os
import sys
import re
import json
import tempfile
import argparse
import pandas as pd
from difflib import SequenceMatcher
from zipfile import ZipFile
import xml.etree.ElementTree as ET

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from canada_pipeline.superscript_extraction_from_pdf import (
    extract_words_with_metadata,
    find_superscript_after_text,
    find_superscript_via_llm,
    convert_docx_to_pdf,
)
from canada_pipeline.track_changes_df import process_docx
from canada_pipeline.config import build_anthropic_client, MODEL_SONNET


# ─── XML namespace for WordprocessingML ───────────────────────────────
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def extract_footnote_ref_from_xml(docx_path):
    """
    Extract footnote reference numbers directly from the DOCX XML.
    
    Two sources are checked:
    1. word/footnotes.xml — For each <w:footnote w:id="X">, looks for the first 
       <w:r> with <w:rStyle w:val="FootnoteReference"/> and extracts <w:t> text.
    2. word/document.xml — For <w:footnoteReference w:customMarkFollows="1">, the
       explicit number follows as <w:t> in the same run.
    
    When <w:t> is present, it's the explicit footnote number (e.g., "1", "23").
    When absent, Word uses auto-numbering via <w:footnoteRef/> and we return None
    for that footnote (requiring PDF fallback).
    
    Returns:
        dict: {footnote_id_str: number_str_or_None}
              e.g. {"193": "1", "194": None, "195": "3"}
    """
    fn_ref_map = {}  # fn_id -> extracted number or None
    
    try:
        with ZipFile(docx_path) as z:
            # ── Source 1: Check footnotes.xml for explicit <w:t> in FootnoteReference runs ──
            try:
                xml_bytes = z.read("word/footnotes.xml")
                fn_root = ET.fromstring(xml_bytes)
                
                for fn_elem in fn_root.findall(f"{{{W_NS}}}footnote"):
                    fn_id = fn_elem.attrib.get(f"{{{W_NS}}}id", "?")
                    try:
                        if int(fn_id) < 1:
                            continue
                    except ValueError:
                        continue
                    
                    # Walk paragraphs in this footnote
                    found_ref_number = None
                    for p_elem in fn_elem.findall(f".//{{{W_NS}}}p"):
                        for r_elem in p_elem.findall(f"{{{W_NS}}}r"):
                            # Check if this run has FootnoteReference style
                            rpr = r_elem.find(f"{{{W_NS}}}rPr")
                            if rpr is None:
                                continue
                            rstyle = rpr.find(f"{{{W_NS}}}rStyle")
                            if rstyle is None:
                                continue
                            style_val = rstyle.attrib.get(f"{{{W_NS}}}val", "")
                            if style_val != "FootnoteReference":
                                continue
                            
                            # Found a FootnoteReference run — check for <w:t>
                            wt = r_elem.find(f"{{{W_NS}}}t")
                            if wt is not None and wt.text and wt.text.strip().isdigit():
                                found_ref_number = wt.text.strip()
                            else:
                                # Has <w:footnoteRef/> (auto-numbered) — no explicit text
                                found_ref_number = None
                            break  # Only check the first FootnoteReference run
                        if found_ref_number is not None:
                            break
                    
                    fn_ref_map[fn_id] = found_ref_number
            except KeyError:
                pass  # No footnotes.xml
            
            # ── Source 2: Check document.xml for customMarkFollows refs ──
            # These have the number as <w:t> in the same <w:r> as the reference
            try:
                doc_bytes = z.read("word/document.xml")
                doc_root = ET.fromstring(doc_bytes)
                
                for r_elem in doc_root.iter(f"{{{W_NS}}}r"):
                    fn_ref = r_elem.find(f"{{{W_NS}}}footnoteReference")
                    if fn_ref is None:
                        continue
                    
                    fn_id = fn_ref.attrib.get(f"{{{W_NS}}}id")
                    if not fn_id:
                        continue
                    
                    # Already found from footnotes.xml? Skip.
                    if fn_ref_map.get(fn_id) is not None:
                        continue
                    
                    # Check for customMarkFollows="1"
                    custom_mark = fn_ref.attrib.get(f"{{{W_NS}}}customMarkFollows", "")
                    if custom_mark == "1":
                        # The <w:t> in the same run has the explicit number
                        wt = r_elem.find(f"{{{W_NS}}}t")
                        if wt is not None and wt.text and wt.text.strip().isdigit():
                            fn_ref_map[fn_id] = wt.text.strip()
            except KeyError:
                pass  # No document.xml (shouldn't happen)
    
    except Exception as e:
        print(f"  [WARN] Could not read DOCX for XML extraction: {e}")
    
    return fn_ref_map


def _build_pdf_text_with_positions(all_words):
    """
    Build a joined text from PDF words and a mapping from char position to word index.
    Returns (joined_text, char_to_word_idx).
    """
    joined = ""
    char_to_word_idx = []
    for wi, w in enumerate(all_words):
        if joined:
            joined += " "
            char_to_word_idx.append(wi)
        for ch in w["text"]:
            joined += ch
            char_to_word_idx.append(wi)
    return joined, char_to_word_idx


def _find_top_matches_in_pdf(search_text, joined_text, top_n=5):
    """
    Fuzzy match text_before_superscript against the full PDF joined text.
    Find top N locations where the text matches, and grab +20 chars after each match.
    
    Returns list of dicts sorted by score descending:
        {match_start, match_end, score, text_matched, text_after_20}
    """
    search_lower = re.sub(r'<[^>]+>', '', search_text).lower().strip()
    joined_lower = joined_text.lower()
    
    if not search_lower:
        return []
    
    # Strategy: use sliding window with SequenceMatcher to find best match positions
    # Use the full search text for matching
    search_len = len(search_lower)
    joined_len = len(joined_lower)
    
    if search_len == 0 or joined_len == 0:
        return []
    
    candidates = []
    
    # Strategy A: Direct substring search (exact or near-exact)
    # Try progressively shorter suffixes of the search text
    for trim_len in [search_len, max(60, search_len // 2), 60, 40, 30]:
        fragment = search_lower[-trim_len:] if len(search_lower) > trim_len else search_lower
        start_pos = 0
        while True:
            pos = joined_lower.find(fragment, start_pos)
            if pos == -1:
                break
            match_end = pos + len(fragment)
            # Grab +20 chars after the match
            text_after = joined_text[match_end:match_end + 20]
            candidates.append({
                "match_start": pos,
                "match_end": match_end,
                "score": len(fragment) / search_len,  # longer match = higher score
                "text_matched": joined_text[pos:match_end],
                "text_after_20": text_after,
            })
            start_pos = pos + 1  # move forward to find other occurrences
        if candidates:
            break  # found matches at this trim level, no need to go shorter
    
    # Strategy B: If no exact matches, use fuzzy sliding window
    if not candidates:
        # Use a window size close to search_text length, slide across PDF
        window_size = min(search_len, 100)
        step = max(10, window_size // 4)
        
        for start in range(0, joined_len - window_size, step):
            window = joined_lower[start:start + window_size]
            score = SequenceMatcher(None, search_lower[-window_size:], window).ratio()
            if score > 0.5:  # only keep reasonable matches
                match_end = start + window_size
                text_after = joined_text[match_end:match_end + 20]
                candidates.append({
                    "match_start": start,
                    "match_end": match_end,
                    "score": score,
                    "text_matched": joined_text[start:match_end],
                    "text_after_20": text_after,
                })
    
    # Deduplicate overlapping matches (keep highest score for overlapping regions)
    candidates.sort(key=lambda x: x["score"], reverse=True)
    filtered = []
    used_positions = set()
    for cand in candidates:
        # Check if this position overlaps with an already-selected match
        pos_key = cand["match_end"] // 20  # bucket by ~20 char regions
        if pos_key not in used_positions:
            filtered.append(cand)
            used_positions.add(pos_key)
        if len(filtered) >= top_n:
            break
    
    return filtered


def _llm_pick_superscript(search_text, top_matches, client, model_name=MODEL_SONNET):
    """
    Give the LLM the text_before_superscript and top 5 match locations.
    Each match shows the matched text + 20 chars after (where the superscript should be).
    LLM picks which one contains the correct footnote superscript number.
    """
    candidates_text = ""
    for i, match in enumerate(top_matches, 1):
        # Show last 80 chars of matched text + the 20 chars after
        matched_tail = match['text_matched'][-80:]
        candidates_text += (
            f"\nMatch {i} (similarity: {match['score']:.3f}):\n"
            f"  Text ending at match: \"...{matched_tail}\"\n"
            f"  +20 chars after match: \"{match['text_after_20']}\"\n"
        )
    
    prompt = f"""You are identifying footnote superscript numbers from a PDF.

The DOCX track changes show that a footnote was inserted after this text:
"{search_text[-200:]}"

I searched for this text in the PDF and found {len(top_matches)} matching locations.
For each match, I show the end of the matched text and the 20 characters that come AFTER it in the PDF.
The footnote superscript number should appear in those 20 characters after the match.

{candidates_text}

Look at the "+20 chars after match" for each candidate. The superscript footnote number is a digit (e.g., 1, 2, 23) that appears right after the matching text.

Rules:
1. Pick the match whose text best corresponds to the DOCX text above.
2. From that match's "+20 chars after", extract the footnote number (the first digit/number you see).
3. If none match or no digit is found, reply "NONE".

Reply with ONLY the superscript number (e.g., "1", "5", "23") or "NONE".
"""

    try:
        response = client.messages.create(
            model=model_name,
            max_tokens=20,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        answer = response.content[0].text.strip().strip('"\'')
        if answer.upper() == "NONE":
            return None
        # Validate it's a number
        if answer.isdigit():
            return answer
        # Try to extract a number from the response
        num_match = re.search(r'\d+', answer)
        if num_match:
            return num_match.group()
        return None
    except Exception as e:
        print(f"    ⚠️  LLM pick error: {e}")
        return None


def run_superscript_test(docx_path, pdf_path=None, output_path=None):
    """
    Run the full superscript extraction pipeline on a DOCX/PDF pair.
    Mirrors what the main pipeline does, then saves the Excel for inspection.
    """
    output_dir = os.path.dirname(os.path.abspath(docx_path))
    docx_basename = os.path.splitext(os.path.basename(docx_path))[0]

    # ── Step 1: Convert DOCX to PDF if not provided ──
    if pdf_path is None:
        print("🔄 Converting DOCX to PDF...")
        pdf_path = convert_docx_to_pdf(docx_path, output_dir=output_dir)
    print(f"📄 PDF: {pdf_path}")

    # ── Step 2: Extract track changes from DOCX ──
    print("\n📄 Extracting track changes from DOCX...")
    results = process_docx(docx_path)
    extractor_obj = results['extractor']
    result_with_ba = extractor_obj.build_before_after_df()
    try:
        result_with_ba = extractor_obj.enrich_footnote_anchors()
    except RuntimeError as e:
        print(f"  [WARN] enrich_footnote_anchors skipped: {e}")
    print(f"   Total rows in result_with_ba: {len(result_with_ba)}")

    # ── Step 3: Filter to new inserted footnotes ──
    df = result_with_ba.copy()
    df = df[
        (df['is_footnote_x'] == True) &
        (df['type'].isin(['inline_insertion', 'paragraph_addition'])) &
        (df['is_new_footnote'] == 1)
    ].reset_index(drop=True)
    print(f"   Filtered to {len(df)} new inserted footnotes")

    if len(df) == 0:
        print("⚠️  No new inserted footnotes found. Nothing to test.")
        return

    # ── Step 4: Build previous_para column ──
    unfiltered = result_with_ba[
        result_with_ba['is_footnote_x'] == False
    ].drop_duplicates(subset=['idx'], keep='first').sort_values('idx').reset_index(drop=True)

    def get_previous_para(parent_idx):
        if pd.isna(parent_idx):
            return ""
        parent_idx = int(parent_idx)
        all_idxs = unfiltered['idx'].tolist()
        try:
            pos = all_idxs.index(parent_idx)
            pos -= 1
        except ValueError:
            candidates = [i for i, v in enumerate(all_idxs) if v < parent_idx]
            if not candidates:
                return ""
            pos = candidates[-1]
        while pos >= 0:
            row = unfiltered.iloc[pos]
            row_type = str(row.get('type', '')).strip()
            if row_type == 'inline_insertion':
                text = str(row.get('text_after', '')).strip()
            elif row_type == 'inline_deletion':
                text = str(row.get('text_before', '')).strip()
            else:
                text = str(row.get('text', '')).strip()
            if text and text.lower() != 'nan':
                return text
            pos -= 1
        return ""

    df['previous_para'] = df['parent_paragraph_idx_x'].apply(get_previous_para)
    print(f"   Built previous_para column")

    if 'text_before_superscript' not in df.columns:
        print("❌ Column 'text_before_superscript' not found. Cannot proceed.")
        return

    # ── Step 4.5: Try XML-based extraction first ──
    # Directly read footnote ref numbers from word/footnotes.xml where available
    print(f"\n📋 Attempting XML-based footnote ref extraction...")
    xml_ref_map = extract_footnote_ref_from_xml(docx_path)
    
    # Determine the footnote_id column name (may have _x suffix after merge)
    fn_id_col = 'footnote_id_x' if 'footnote_id_x' in df.columns else 'footnote_id'
    
    xml_resolved_count = 0
    xml_unresolved_indices = []  # indices that still need PDF fallback
    
    for i, (idx, row) in enumerate(df.iterrows()):
        raw_fn_id = row.get(fn_id_col)
        if raw_fn_id is None or (isinstance(raw_fn_id, float) and pd.isna(raw_fn_id)):
            xml_unresolved_indices.append(i)
            continue
        fn_id_str = str(int(float(raw_fn_id)))
        xml_number = xml_ref_map.get(fn_id_str)
        if xml_number is not None:
            xml_resolved_count += 1
        else:
            xml_unresolved_indices.append(i)
    
    print(f"   ✅ XML resolved: {xml_resolved_count}/{len(df)} footnotes")
    print(f"   ⏳ Needs PDF fallback: {len(xml_unresolved_indices)} footnotes")

    # ── Step 5: Extract words from PDF (only if needed) ──
    if xml_unresolved_indices:
        print(f"\n📄 Extracting words from PDF (for {len(xml_unresolved_indices)} unresolved)...")
        all_words = extract_words_with_metadata(pdf_path)
        print(f"   Total words extracted: {len(all_words)}")

        # Build joined text from PDF
        print(f"   Building PDF text index...")
        joined, char_to_word_idx = _build_pdf_text_with_positions(all_words)
        print(f"   PDF text length: {len(joined)} characters")
    else:
        all_words = None
        joined = ""
        char_to_word_idx = []

    # Build LLM client (only if PDF fallback is needed)
    client = None
    if xml_unresolved_indices:
        print(f"   Building LLM client...")
        client = build_anthropic_client(MODEL_SONNET)

    # ── Step 6: Find superscripts — XML first, then fuzzy match + LLM fallback ──
    # Load cache (keyed by idx) to avoid redundant LLM calls
    cache_path = os.path.join(os.path.dirname(os.path.abspath(docx_path)), ".superscript_cache.json")
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as cf:
                cache = json.load(cf)
            print(f"   Loaded cache with {len(cache)} entries from {cache_path}")
        except Exception:
            cache = {}

    print(f"\n🔍 Finding superscripts for {len(df)} footnotes (XML → PDF fuzzy → LLM)...\n")
    pdf_superscripts = []
    confidences = []
    methods = []

    for i, (idx, row) in enumerate(df.iterrows()):
        # ── Try XML extraction first ──
        raw_fn_id = row.get(fn_id_col)
        fn_id_str = None
        if raw_fn_id is not None and not (isinstance(raw_fn_id, float) and pd.isna(raw_fn_id)):
            fn_id_str = str(int(float(raw_fn_id)))
        
        if fn_id_str and xml_ref_map.get(fn_id_str) is not None:
            superscript = xml_ref_map[fn_id_str]
            pdf_superscripts.append(superscript)
            confidences.append("high")
            methods.append("xml_direct")
            print(f"  Row {i+1}/{len(df)} (idx={row.get('idx', '?')}): 📋 XML direct → superscript={superscript}")
            continue

        # ── Fallback: PDF fuzzy match + LLM ──
        search_text = str(row['text_before_superscript']).strip()
        row_idx = str(row.get('idx', idx))

        if not search_text or search_text.lower() == 'nan':
            pdf_superscripts.append(None)
            confidences.append(None)
            methods.append(None)
            print(f"  Row {i+1}/{len(df)}: [SKIP] empty text_before_superscript")
            continue

        # Check cache
        if row_idx in cache:
            cached = cache[row_idx]
            pdf_superscripts.append(cached.get("superscript"))
            confidences.append(cached.get("confidence"))
            methods.append("cached")
            print(f"  Row {i+1}/{len(df)} (idx={row_idx}): ⚡ CACHED superscript={cached.get('superscript')}")
            continue

        print(f"  Row {i+1}/{len(df)} (idx={row.get('idx', '?')}):")
        print(f"    text_before: '{search_text[:100]}...'")

        # Find top 5 locations in PDF where text_before_superscript matches
        top_matches = _find_top_matches_in_pdf(search_text, joined, top_n=5)

        if not top_matches:
            print(f"    ❌ No matching locations found in PDF")
            pdf_superscripts.append(None)
            confidences.append(None)
            methods.append(None)
            continue

        # Show matches
        for rank, match in enumerate(top_matches, 1):
            print(f"    Match {rank}: score={match['score']:.3f}, "
                  f"text_end='...{match['text_matched'][-40:]}', "
                  f"after='{match['text_after_20']}'")

        # Always let LLM pick from top matches
        print(f"    🤖 Asking LLM to pick from {len(top_matches)} matches...")
        superscript = _llm_pick_superscript(search_text, top_matches, client, MODEL_SONNET)
        if superscript:
            confidence = "high"
            method = "text_match_llm_pick"
            print(f"    ✅ LLM picked: superscript={superscript}")
        else:
            confidence = None
            method = None
            print(f"    ❌ LLM could not determine superscript")

        pdf_superscripts.append(superscript)
        confidences.append(confidence)
        methods.append(method)

        # Save to cache
        cache[row_idx] = {"superscript": superscript, "confidence": confidence, "method": method}
        print()

    # Persist cache to disk
    try:
        with open(cache_path, "w", encoding="utf-8") as cf:
            json.dump(cache, cf, indent=2)
        print(f"   💾 Cache saved ({len(cache)} entries) → {cache_path}")
    except Exception as e:
        print(f"   ⚠️  Could not save cache: {e}")

    df['pdf_superscript'] = pdf_superscripts
    df['footnote_ref_confidence'] = confidences
    df['extraction_method'] = methods

    # ── Step 7: Summary ──
    total = len(df)
    found = df['pdf_superscript'].notna().sum()
    missing = total - found
    xml_count = (df['extraction_method'] == 'xml_direct').sum()
    llm_count = (df['extraction_method'] == 'text_match_llm_pick').sum()
    cached_count = (df['extraction_method'] == 'cached').sum()

    print(f"\n{'='*70}")
    print(f"📊 SUMMARY")
    print(f"{'='*70}")
    print(f"  Total new footnotes:      {total}")
    print(f"  Superscripts found:       {found}")
    print(f"  Superscripts missing:     {missing}")
    print(f"  Success rate:             {found/total*100:.1f}%" if total > 0 else "")
    print(f"  ─── Breakdown by method ───")
    print(f"  📋 XML direct:            {xml_count}")
    print(f"  🤖 LLM pick (PDF):        {llm_count}")
    print(f"  ⚡ Cached:                 {cached_count}")
    print(f"{'='*70}")

    # Show results table
    display_cols = ['idx', 'text_before_superscript', 'pdf_superscript', 'footnote_ref_confidence', 'extraction_method']
    display_cols = [c for c in display_cols if c in df.columns]
    print(f"\n{'─'*70}")
    for _, row in df.iterrows():
        tbs = str(row.get('text_before_superscript', ''))[:80]
        sup = row.get('pdf_superscript', None)
        conf = row.get('footnote_ref_confidence', '')
        meth = row.get('extraction_method', '')
        status = "✅" if sup else "❌"
        print(f"  {status} idx={row.get('idx', '?'):<6} superscript={str(sup):<5} [{conf}, {meth}]  text='{tbs}...'")
    print(f"{'─'*70}")

    if missing > 0:
        print(f"\n⚠️  MISSING ({missing}):")
        for _, row in df[df['pdf_superscript'].isna()].iterrows():
            print(f"   idx={row.get('idx', '?')} | '{str(row['text_before_superscript'])[:120]}'")

    # ── Step 8: Save Excel ──
    if output_path is None:
        output_path = os.path.join(output_dir, f"{docx_basename}_superscript_test.xlsx")

    df.to_excel(output_path, index=False)
    print(f"\n💾 Results saved to: {output_path}")

    return df


# ─────────────────────────────────────────────────────────────────────────────
# Streamlit UI
# ─────────────────────────────────────────────────────────────────────────────

def run_streamlit_ui():
    import streamlit as st

    st.set_page_config(page_title="Footnote Superscript Tester", layout="wide")
    st.title("🔍 Footnote Superscript Extraction Tester")
    st.markdown("Upload a DOCX and PDF, select output folder, and verify superscript extraction.")

    # ── File inputs ──
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("📄 DOCX File")
        docx_file = st.file_uploader("Upload DOCX with track changes", type=["docx", "doc"])
    with col2:
        st.subheader("📄 PDF File")
        pdf_file = st.file_uploader("Upload corresponding PDF", type=["pdf"])

    # ── Output folder ──
    st.subheader("📂 Output Folder")
    output_folder = st.text_input(
        "Enter output folder path",
        value=r"C:\projectdata\xmlproject\superscript_test_output",
        help="The Excel results will be saved here"
    )

    # ── Run button ──
    both_uploaded = docx_file is not None and pdf_file is not None
    if not both_uploaded:
        st.info("Upload both DOCX and PDF files to proceed.")

    if st.button("🚀 Run Superscript Extraction", type="primary", disabled=(not both_uploaded)):
        if not output_folder.strip():
            st.error("Please enter an output folder path.")
            return

        os.makedirs(output_folder, exist_ok=True)

        # Save uploaded files to output folder
        docx_path = os.path.join(output_folder, docx_file.name)
        with open(docx_path, "wb") as f:
            f.write(docx_file.getbuffer())

        pdf_path = os.path.join(output_folder, pdf_file.name)
        with open(pdf_path, "wb") as f:
            f.write(pdf_file.getbuffer())

        docx_basename = os.path.splitext(docx_file.name)[0]
        output_excel = os.path.join(output_folder, f"{docx_basename}_superscript_test.xlsx")

        # Run extraction with progress
        st.info(f"Processing **{docx_file.name}**...")
        log_container = st.expander("📋 Processing Log", expanded=True)

        # Capture print output
        import io
        from contextlib import redirect_stdout

        log_buffer = io.StringIO()

        with log_container:
            with redirect_stdout(log_buffer):
                try:
                    result_df = run_superscript_test(docx_path, pdf_path, output_excel)
                except Exception as e:
                    import traceback
                    st.error(f"❌ Pipeline failed: {e}")
                    st.code(traceback.format_exc())
                    return

            # Show log
            st.code(log_buffer.getvalue(), language="text")

        if result_df is None or len(result_df) == 0:
            st.warning("No new inserted footnotes found in the document.")
            return

        # ── Results ──
        st.success(f"✅ Done! Results saved to: `{output_excel}`")

        # Summary metrics
        total = len(result_df)
        found = result_df['pdf_superscript'].notna().sum()
        missing = total - found

        col_a, col_b, col_c = st.columns(3)
        col_a.metric("Total Footnotes", total)
        col_b.metric("Found", int(found), delta=None)
        col_c.metric("Missing", int(missing), delta=None, delta_color="inverse")

        # Results table
        st.subheader("📊 Results")
        display_cols = ['idx', 'text_before_superscript', 'previous_para', 'pdf_superscript',
                        'footnote_ref_confidence', 'extraction_method']
        display_cols = [c for c in display_cols if c in result_df.columns]
        st.dataframe(
            result_df[display_cols],
            use_container_width=True,
            height=400,
        )

        # Highlight missing
        if missing > 0:
            st.subheader("⚠️ Missing Superscripts")
            missing_df = result_df[result_df['pdf_superscript'].isna()]
            st.dataframe(
                missing_df[['idx', 'text_before_superscript', 'previous_para']],
                use_container_width=True,
            )

        # Download button
        with open(output_excel, "rb") as f:
            st.download_button(
                "⬇️ Download Results Excel",
                data=f.read(),
                file_name=f"{docx_basename}_superscript_test.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def _is_streamlit():
    """Check if running under streamlit."""
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        return get_script_run_ctx() is not None
    except Exception:
        return False


if _is_streamlit():
    run_streamlit_ui()
elif __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test footnote superscript extraction from PDF")
    parser.add_argument("--docx", required=True, help="Path to the DOCX file")
    parser.add_argument("--pdf", default=None, help="Path to the PDF file (optional, will convert from DOCX if not provided)")
    parser.add_argument("--output", default=None, help="Path to save output Excel (optional)")
    args = parser.parse_args()

    if not os.path.exists(args.docx):
        print(f"❌ DOCX file not found: {args.docx}")
        sys.exit(1)

    if args.pdf and not os.path.exists(args.pdf):
        print(f"❌ PDF file not found: {args.pdf}")
        sys.exit(1)

    print(f"{'='*70}")
    print(f"  Footnote Superscript Extraction Test")
    print(f"{'='*70}")
    print(f"  DOCX:   {args.docx}")
    print(f"  PDF:    {args.pdf or '(will convert from DOCX)'}")
    print(f"  Output: {args.output or '(auto)'}")
    print(f"{'='*70}\n")

    run_superscript_test(args.docx, args.pdf, args.output)
