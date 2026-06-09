"""
Empty Tags Checker
==================
Checks a post-processed PXML file for empty/meaningless tags that should have
been collapsed during postprocessing.

An "empty tag" is an element that contains no meaningful text content —
only whitespace, <trace.deleted/> markers, or other empty child elements.

Usage:
    python empty_tags_checker.py

Provide the PXML path below. Outputs an Excel report with:
- Section, Tag Name, Tag Content (snippet), Reason
"""

import re
import os
import time
import pandas as pd
from config import API_KEY, MODEL_OPUS, MODEL_SONNET


# ─────────────────────────────────────────────
# USER INPUTS
# ─────────────────────────────────────────────
PXML_PATH = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\test_10\CDN_FI_EDRM 1 (chapter and all children)_20260527_124438_final.pxml"  # Path to post-processed PXML to check
OUTPUT_EXCEL_PATH = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\test_10\empty_tags_report.xlsx"  # Output report path (leave empty for auto)


# ─────────────────────────────────────────────
# SECTION PARSING
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


# ─────────────────────────────────────────────
# EMPTY TAG DETECTION (regex-based, no LLM needed)
# ─────────────────────────────────────────────

# Tags to check for emptiness
TAGS_TO_CHECK = [
    'para', 'para.text', 'quote.para', 'quote.block', 'quote.text',
    'list', 'list.item', 'name.block', 'designator', 'ital', 'bold',
    'footnote', 'footnote.body', 'footnote.reference',
]

def is_content_empty(inner_content):
    """
    Check if the inner content of a tag is effectively empty.
    Empty means: only whitespace, <trace.deleted/> markers, or PIs.
    """
    cleaned = inner_content
    # Remove <trace.deleted/> self-closing
    cleaned = re.sub(r'<trace\.deleted\s*/>', '', cleaned)
    # Remove processing instructions
    cleaned = re.sub(r'<\?[^?]*\?>', '', cleaned)
    # Remove comments
    cleaned = re.sub(r'<!--.*?-->', '', cleaned, flags=re.DOTALL)
    # Check if anything meaningful remains
    cleaned = cleaned.strip()
    if not cleaned:
        return True
    # Check if only contains other empty self-closing tags
    cleaned_no_selfclose = re.sub(r'<[a-zA-Z][a-zA-Z0-9._-]*\b[^>]*/>', '', cleaned)
    if not cleaned_no_selfclose.strip():
        return True
    return False


def find_empty_tags_in_section(section_xml, designator):
    """Find all empty tags in a section. Returns list of findings."""
    findings = []

    for tag in TAGS_TO_CHECK:
        # Match opening and closing tag pairs (non-greedy, innermost first)
        pattern = re.compile(
            rf'<{re.escape(tag)}\b([^>]*)>(.*?)</{re.escape(tag)}>',
            re.DOTALL
        )

        for m in pattern.finditer(section_xml):
            attrs = m.group(1)
            inner = m.group(2)

            # Skip if it has date.trace="1" (newly inserted, might legitimately be empty placeholder)
            if 'date.trace="1"' in attrs:
                continue

            if is_content_empty(inner):
                # Get a snippet for context
                full_match = m.group(0)
                snippet = full_match[:200] + ('...' if len(full_match) > 200 else '')

                # Determine the reason
                if '<trace.deleted' in inner:
                    reason = "Contains only <trace.deleted/> — should have been collapsed"
                elif not inner.strip():
                    reason = "Completely empty (no content)"
                else:
                    reason = "Contains only non-meaningful markers"

                findings.append({
                    'Section': designator,
                    'Tag': tag,
                    'Attributes': attrs.strip()[:100],
                    'Content_Snippet': snippet,
                    'Reason': reason,
                    'Line_Position': section_xml[:m.start()].count('\n') + 1,
                })

    return findings


def find_empty_tags_global(pxml_content):
    """
    Additionally check for empty tags that span across section boundaries
    or are outside sections (e.g. in chapter preamble).
    """
    findings = []
    for tag in TAGS_TO_CHECK:
        pattern = re.compile(
            rf'<{re.escape(tag)}\b([^>]*)>(.*?)</{re.escape(tag)}>',
            re.DOTALL
        )
        for m in pattern.finditer(pxml_content):
            attrs = m.group(1)
            inner = m.group(2)
            if 'date.trace="1"' in attrs:
                continue
            if is_content_empty(inner):
                # Find which section this belongs to
                pos = m.start()
                # Quick check: find nearest designator before this position
                designators_before = list(re.finditer(
                    r'<designator[^>]*>([\d:.A-Za-z]+)</designator>',
                    pxml_content[:pos]
                ))
                section = designators_before[-1].group(1) if designators_before else 'PREAMBLE'

                full_match = m.group(0)
                snippet = full_match[:200] + ('...' if len(full_match) > 200 else '')

                if '<trace.deleted' in inner:
                    reason = "Contains only <trace.deleted/> — should have been collapsed"
                elif not inner.strip():
                    reason = "Completely empty (no content)"
                else:
                    reason = "Contains only non-meaningful markers"

                findings.append({
                    'Section': section,
                    'Tag': tag,
                    'Attributes': attrs.strip()[:100],
                    'Content_Snippet': snippet,
                    'Reason': reason,
                    'Char_Position': pos,
                })

    return findings


def run_empty_tags_check(pxml_path, output_excel=None):
    """Main function to check for empty tags."""
    print(f"{'='*80}")
    print("EMPTY TAGS CHECKER")
    print(f"{'='*80}")
    print(f"  PXML file: {pxml_path}")

    with open(pxml_path, 'r', encoding='utf-8') as f:
        pxml_content = f.read()

    print(f"  File size: {len(pxml_content)} chars")

    # Find empty tags globally
    print("\n  Scanning for empty tags...")
    findings = find_empty_tags_global(pxml_content)

    # Deduplicate by content snippet
    seen = set()
    unique_findings = []
    for f_item in findings:
        key = (f_item['Section'], f_item['Tag'], f_item['Content_Snippet'])
        if key not in seen:
            seen.add(key)
            unique_findings.append(f_item)

    print(f"  Found {len(unique_findings)} empty tags")

    # Summary by tag type
    from collections import Counter
    tag_counts = Counter(f['Tag'] for f in unique_findings)
    for tag, count in tag_counts.most_common():
        print(f"    {tag}: {count}")

    # Generate report
    df = pd.DataFrame(unique_findings)

    if not output_excel:
        output_dir = os.path.dirname(pxml_path)
        timestamp = time.strftime('%Y%m%d_%H%M%S')
        output_excel = os.path.join(output_dir, f"empty_tags_report_{timestamp}.xlsx")

    if df.empty:
        print("\n  ✅ No empty tags found!")
        # Write empty report
        df = pd.DataFrame({'Message': ['No empty tags found']})

    df.to_excel(output_excel, index=False, sheet_name='Empty Tags')

    # Format Excel
    try:
        from openpyxl import load_workbook
        from openpyxl.styles import Font, PatternFill, Alignment

        wb = load_workbook(output_excel)
        ws = wb.active

        header_fill = PatternFill(start_color='1565C0', end_color='1565C0', fill_type='solid')
        header_font = Font(bold=True, color='FFFFFF')

        for cell in ws[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal='center')

        ws.column_dimensions['A'].width = 12
        ws.column_dimensions['B'].width = 15
        ws.column_dimensions['C'].width = 30
        ws.column_dimensions['D'].width = 80
        ws.column_dimensions['E'].width = 50
        ws.column_dimensions['F'].width = 12

        ws.freeze_panes = 'A2'
        ws.auto_filter.ref = ws.dimensions
        wb.save(output_excel)
    except Exception as e:
        print(f"  ⚠️ Excel formatting error: {e}")

    print(f"\n  Report saved to: {output_excel}")
    print(f"{'='*80}")

    return df, unique_findings


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    if not PXML_PATH:
        print("ERROR: Please set PXML_PATH at the top of this file.")
    else:
        run_empty_tags_check(PXML_PATH, OUTPUT_EXCEL_PATH or None)
