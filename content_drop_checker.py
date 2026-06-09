"""
Content Drop Checker
====================
Compares a prevendor PXML (ground truth) with a post-vendor PXML to detect
if any existing content has been dropped during the track-changes pipeline.

Usage:
    python content_drop_checker.py

Provide the two PXML file paths below. The script will:
1. Chunk both XMLs into sections (by designator)
2. For each section in prevendor, extract plain text (stripping tags)
3. For each section in post-vendor, extract plain text (stripping trace tags content)
4. Compare and report content drops per section
5. Output an Excel report
"""

import re
import os
import time
import pandas as pd
from config import API_KEY, MODEL_OPUS
import anthropic


# ─────────────────────────────────────────────
# USER INPUTS
# ─────────────────────────────────────────────
PREVENDOR_PXML_PATH = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\Test_9 (May 12, 2026)_batch_running\CDN_FI_EDRM_1\CDN_FI_EDRM 1 (chapter and all children).pxml"  # Path to original prevendor PXML
POSTVENDOR_PXML_PATH = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\test_10\save_before_postprocessing.pxml"  # Path to post-vendor (processed) PXML
OUTPUT_EXCEL_PATH = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\test_10\content_drop_validation.xlsx"  # Output report path (leave empty for auto)

# ─────────────────────────────────────────────
# SECTION PARSING (same logic as parse_pxml_sections_from_str_dict_positions)
# ─────────────────────────────────────────────

def parse_pxml_sections(pxml_content):
    """Parse PXML content into a dict of designator -> section XML."""
    section_dict = {}
    pattern = r'<section\s+(?:uuid="[^"]+"\s+)?[^>]*>.*?</section>'
    section_matches = re.finditer(pattern, pxml_content, re.DOTALL)

    for match in section_matches:
        section_xml = match.group(0)
        designator_match = re.search(r'<designator[^>]*>([\d:.A-Za-z]+)</designator>', section_xml)
        if designator_match:
            designator = designator_match.group(1)
            section_dict[designator] = section_xml

    return section_dict


def strip_all_tags(text):
    """Remove all XML/HTML tags, decode common entities, normalize whitespace."""
    text = re.sub(r'<[^>]+>', ' ', text)
    # Decode common XML entities
    entity_map = {
        '&ldquo;': '"', '&rdquo;': '"',
        '&lsquo;': "'", '&rsquo;': "'",
        '&amp;': '&', '&lt;': '<', '&gt;': '>',
        '&mdash;': '—', '&ndash;': '–',
        '&hellip;': '…', '&sect;': '§',
        '&lsqb;': '[', '&rsqb;': ']',
        '&num;': '#', '&percnt;': '%',
        '&dollar;': '$', '&ast;': '*',
    }
    for ent, char in entity_map.items():
        text = text.replace(ent, char)
    # Remove any remaining numeric/named entities
    text = re.sub(r'&[a-zA-Z]+;', '', text)
    text = re.sub(r'&#\d+;', '', text)
    # Normalize whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def extract_text_from_section_prevendor(section_xml):
    """Extract plain text from prevendor section (all content is ground truth)."""
    return strip_all_tags(section_xml)


def extract_text_from_section_postvendor(section_xml):
    """
    Extract plain text from post-vendor section.
    - Content inside <trace.deleted>...</trace.deleted> is DELETED (expected removal)
    - Content inside <trace>...</trace> is INSERTED (new, not in prevendor)
    - Self-closing <trace.deleted/> markers are ignored
    - Everything else should match prevendor
    """
    text = section_xml

    # Remove inserted content (trace insertions) — these are NEW, not in prevendor
    text = re.sub(r'<trace>(.*?)</trace>', '', text, flags=re.DOTALL)

    # Remove trace.deleted markers (the content between them was deleted intentionally)
    # Keep the deleted text since it WAS in prevendor (we want to verify it's still represented)
    # Actually: <trace.deleted>CONTENT<trace.deleted/> means CONTENT was deleted
    # For comparison with prevendor, we KEEP the deleted content (it was in prevendor)
    text = re.sub(r'<trace\.deleted>(.*?)<trace\.deleted\s*/>', r'\1', text, flags=re.DOTALL)

    # Remove self-closing trace.deleted markers
    text = re.sub(r'<trace\.deleted\s*/>', '', text)

    # Remove any remaining trace tags
    text = re.sub(r'</?trace\.deleted>', '', text)

    return strip_all_tags(text)


def compute_content_drop(prevendor_text, postvendor_text):
    """
    Compare prevendor text against postvendor text.
    Returns (has_drop: bool, dropped_phrases: list, drop_percentage: float)
    """
    if not prevendor_text.strip():
        return False, [], 0.0

    # Tokenize into words
    pre_words = prevendor_text.split()
    post_words = postvendor_text.split()

    if not pre_words:
        return False, [], 0.0

    # Use set-based check for missing words (quick heuristic)
    post_word_set = set(post_words)
    missing_words = [w for w in pre_words if w not in post_word_set]

    # For more accurate detection, use sequence matching
    import difflib
    matcher = difflib.SequenceMatcher(None, pre_words, post_words, autojunk=False)
    matching = sum(block.size for block in matcher.get_matching_blocks())
    preservation_ratio = matching / len(pre_words) if pre_words else 1.0

    has_drop = preservation_ratio < 0.95  # Less than 95% preserved = content drop

    # Find dropped phrases (consecutive missing words)
    dropped_phrases = []
    if has_drop:
        opcodes = matcher.get_opcodes()
        for tag, i1, i2, j1, j2 in opcodes:
            if tag == 'delete':
                phrase = ' '.join(pre_words[i1:i2])
                if len(phrase) > 10:  # Only report significant drops
                    dropped_phrases.append(phrase[:200])

    drop_percentage = (1.0 - preservation_ratio) * 100
    return has_drop, dropped_phrases, drop_percentage


def run_content_drop_check(prevendor_path, postvendor_path, output_excel=None):
    """Main function to run the content drop check using LLM."""
    print(f"{'='*80}")
    print("CONTENT DROP CHECKER (LLM-based)")
    print(f"{'='*80}")
    print(f"  Prevendor:  {prevendor_path}")
    print(f"  Postvendor: {postvendor_path}")

    # Build Anthropic client
    from config import build_anthropic_client
    client = build_anthropic_client(MODEL_OPUS)
    model_name = MODEL_OPUS
    print(f"  Using model: {model_name}")

    # Read files
    with open(prevendor_path, 'r', encoding='utf-8') as f:
        prevendor_content = f.read()
    with open(postvendor_path, 'r', encoding='utf-8') as f:
        postvendor_content = f.read()

    # Parse sections
    pre_sections = parse_pxml_sections(prevendor_content)
    post_sections = parse_pxml_sections(postvendor_content)

    print(f"\n  Prevendor sections:  {len(pre_sections)}")
    print(f"  Postvendor sections: {len(post_sections)}")

    # Compare each section
    results = []
    total_drops = 0

    all_designators = sorted(
        set(list(pre_sections.keys()) + list(post_sections.keys())),
        key=lambda x: [int(c) if c.isdigit() else c for c in re.split(r'(\d+)', x)]
    )

    for i, designator in enumerate(all_designators):
        pre_xml = pre_sections.get(designator, '')
        post_xml = post_sections.get(designator, '')

        print(f"\n  [{i+1}/{len(all_designators)}] Section {designator}...")

        if not pre_xml:
            results.append({
                'Section': designator,
                'Content_Drop': False,
                'Drop_Percentage': 0.0,
                'Status': 'NEW_SECTION (not in prevendor)',
                'Dropped_Content': '',
                'LLM_Reason': 'New section — no prevendor content to compare.',
            })
            print(f"    ⏭️ NEW_SECTION — skipping")
            continue

        if not post_xml:
            results.append({
                'Section': designator,
                'Content_Drop': True,
                'Drop_Percentage': 100.0,
                'Status': 'SECTION_MISSING (entire section dropped)',
                'Dropped_Content': extract_text_from_section_prevendor(pre_xml)[:500],
                'LLM_Reason': 'Entire section is missing from postvendor.',
            })
            total_drops += 1
            print(f"    ❌ SECTION_MISSING — entire section dropped!")
            continue

        # Both exist — use LLM to compare
        llm_result = _compare_section_with_llm(
            pre_xml, post_xml, designator, client, model_name
        )

        has_drop = llm_result['content_drop']
        dropped_text = llm_result['dropped_text']
        reason = llm_result['reason']

        # Also run quick heuristic for drop percentage
        pre_text = extract_text_from_section_prevendor(pre_xml)
        post_text = extract_text_from_section_postvendor(post_xml)
        _, _, drop_pct = compute_content_drop(pre_text, post_text)

        status = 'CONTENT_DROP' if has_drop else 'OK'

        results.append({
            'Section': designator,
            'Content_Drop': has_drop,
            'Drop_Percentage': round(drop_pct, 2),
            'Status': status,
            'Dropped_Content': dropped_text[:1000] if dropped_text else '',
            'LLM_Reason': reason[:500] if reason else '',
        })

        if has_drop:
            total_drops += 1
            print(f"    ❌ CONTENT_DROP — {reason[:100]}")
        else:
            print(f"    ✅ OK")

    # Generate report
    df = pd.DataFrame(results)

    if not output_excel:
        output_dir = os.path.dirname(postvendor_path)
        timestamp = time.strftime('%Y%m%d_%H%M%S')
        output_excel = os.path.join(output_dir, f"content_drop_report_{timestamp}.xlsx")

    df.to_excel(output_excel, index=False, sheet_name='Content Drop Report')

    # Format the Excel
    try:
        from openpyxl import load_workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

        wb = load_workbook(output_excel)
        ws = wb.active

        header_fill = PatternFill(start_color='2E7D32', end_color='2E7D32', fill_type='solid')
        header_font = Font(bold=True, color='FFFFFF')
        red_fill = PatternFill(start_color='FFC7CE', end_color='FFC7CE', fill_type='solid')
        green_fill = PatternFill(start_color='C6EFCE', end_color='C6EFCE', fill_type='solid')

        for cell in ws[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal='center')

        for row_num in range(2, ws.max_row + 1):
            drop_val = ws.cell(row=row_num, column=2).value
            fill = red_fill if drop_val else green_fill
            for col in range(1, ws.max_column + 1):
                ws.cell(row=row_num, column=col).fill = fill

        ws.column_dimensions['A'].width = 15
        ws.column_dimensions['B'].width = 15
        ws.column_dimensions['C'].width = 18
        ws.column_dimensions['D'].width = 35
        ws.column_dimensions['E'].width = 80
        ws.column_dimensions['F'].width = 60

        ws.freeze_panes = 'A2'
        ws.auto_filter.ref = ws.dimensions
        wb.save(output_excel)
    except Exception as e:
        print(f"  ⚠️ Excel formatting error: {e}")

    print(f"\n{'='*80}")
    print(f"  Total sections checked: {len(results)}")
    print(f"  Sections with content drop: {total_drops}")
    print(f"  Report saved to: {output_excel}")
    print(f"{'='*80}")

    return df


# ─────────────────────────────────────────────
# LLM COMPARISON
# ─────────────────────────────────────────────

def _compare_section_with_llm(prevendor_xml, postvendor_xml, designator, client, model_name):
    """
    Send both section XMLs to Claude and ask it to identify content drops.
    Uses plain text extraction to avoid indentation/tag bloat truncation issues.
    """
    # Extract plain text from both sections for comparison
    # This eliminates the indentation/tag overhead that causes truncation issues
    pre_text = extract_text_from_section_prevendor(prevendor_xml)
    
    # For postvendor: keep trace tags visible so LLM can distinguish deletions from drops
    # Remove excessive indentation but keep trace markup
    post_for_llm = postvendor_xml
    # Collapse all runs of whitespace (tabs, spaces, newlines) into single space
    # BUT preserve trace tag structure
    post_for_llm = re.sub(r'[\t ]*\n[\t ]*', '\n', post_for_llm)  # remove leading/trailing whitespace per line
    post_for_llm = re.sub(r'\n+', '\n', post_for_llm)  # collapse multiple newlines
    post_for_llm = re.sub(r'\t+', ' ', post_for_llm)  # tabs to single space

    # Truncate 
    max_chars = 50000
    pre_truncated = pre_text[:max_chars]
    post_truncated = post_for_llm[:max_chars]
    truncation_note = ""
    if len(pre_text) > max_chars or len(post_for_llm) > max_chars:
        truncation_note = f"\n\nNOTE: Content was truncated. Only check the provided portion."

    prompt = f"""You are a content auditor. Compare the PREVENDOR plain text (ground truth) with the POSTVENDOR XML to check for content drops.

RULES for POSTVENDOR:
- <trace.deleted>TEXT<trace.deleted/> — TEXT was intentionally DELETED. NOT a content drop.
- <trace>TEXT</trace> — TEXT was intentionally INSERTED (new). Ignore for comparison.
- <trace.deleted/> — Deletion marker. Ignore.
- All OTHER text in postvendor should match prevendor.

"Content Drop" = text in PREVENDOR that is COMPLETELY ABSENT from POSTVENDOR (not present as text AND not inside trace.deleted markers).

DO NOT flag:
- Text inside <trace.deleted>...<trace.deleted/> (intentional deletion)
- Minor whitespace/punctuation/entity differences
- Tag/attribute differences

PREVENDOR PLAIN TEXT (section {designator}):
{pre_truncated}

POSTVENDOR XML (section {designator}):
{post_truncated}
{truncation_note}

Respond in EXACTLY this format:
CONTENT_DROP: true OR false
DROPPED_TEXT: <exact dropped text from prevendor that is missing, or "none">
REASON: <one-line explanation>"""

    try:
        response = client.messages.create(
            model=model_name,
            max_tokens=1500,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        return _parse_llm_response(raw)
    except Exception as e:
        print(f"    ⚠️ LLM error: {e}")
        return {'content_drop': False, 'dropped_text': '', 'reason': f'LLM_ERROR: {e}'}


def _parse_llm_response(raw_text):
    """Parse the structured LLM response."""
    result = {'content_drop': False, 'dropped_text': '', 'reason': ''}

    # Extract CONTENT_DROP
    drop_match = re.search(r'CONTENT_DROP:\s*(true|false)', raw_text, re.IGNORECASE)
    if drop_match:
        result['content_drop'] = drop_match.group(1).lower() == 'true'

    # Extract DROPPED_TEXT
    drop_text_match = re.search(r'DROPPED_TEXT:\s*(.+?)(?=\nREASON:|\Z)', raw_text, re.DOTALL)
    if drop_text_match:
        text = drop_text_match.group(1).strip()
        if text.lower() != 'none':
            result['dropped_text'] = text

    # Extract REASON
    reason_match = re.search(r'REASON:\s*(.+)', raw_text, re.DOTALL)
    if reason_match:
        result['reason'] = reason_match.group(1).strip()

    return result


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    if not PREVENDOR_PXML_PATH or not POSTVENDOR_PXML_PATH:
        print("ERROR: Please set PREVENDOR_PXML_PATH and POSTVENDOR_PXML_PATH at the top of this file.")
    else:
        run_content_drop_check(PREVENDOR_PXML_PATH, POSTVENDOR_PXML_PATH, OUTPUT_EXCEL_PATH or None)
