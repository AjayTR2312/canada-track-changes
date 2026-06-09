"""
Track Changes Validation Checker
=================================
Validates that every track change visible in the revision PDF/images has been
correctly applied in the post-vendor PXML.

Approach:
1. Extract text from each page image (OCR)
2. Match page text to section chunks in the post-vendor PXML
3. For each page, send the image + matched section XML to Claude
4. Claude validates:
   - Struck-through text → has <trace.deleted>...<trace.deleted/> or <trace.deleted/>
   - Colored/inserted text → is wrapped in <trace>...</trace>
   - Reports any missed changes
5. Generates an Excel validation report

Usage:
    python track_changes_validator.py
"""

import re
import os
import time
import base64
import pandas as pd
from pathlib import Path
from config import API_KEY, MODEL_OPUS, MODEL_SONNET
from config import build_anthropic_client
import difflib


# ─────────────────────────────────────────────
# USER INPUTS
# ─────────────────────────────────────────────
POSTVENDOR_PXML_PATH = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\test_10\CDN_FI_EDRM 1 (chapter and all children)_20260527_124438_final.pxml" # Path to post-vendor (processed) PXML
IMAGES_FOLDER = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\test_10\revision_images"  # Folder containing page images (PNG)
OUTPUT_EXCEL_PATH = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\test_10\validation_report_ajay.xlsx"  # Output report path (leave empty for auto)


# ─────────────────────────────────────────────
# SECTION PARSING
# ─────────────────────────────────────────────

def parse_pxml_sections(pxml_content):
    """Parse PXML content into a dict of designator -> section XML."""
    section_dict = {}
    # Try standard <section> tags first
    pattern = r'<section\s+(?:uuid="[^"]+"\s+)?[^>]*>.*?</section>'
    section_matches = list(re.finditer(pattern, pxml_content, re.DOTALL))

    # If no standard sections found, try <editorial.commentary.section> tags
    if not section_matches:
        pattern = r'<editorial\.commentary\.section(?=[\s>])[^>]*>.*?</editorial\.commentary\.section>'
        section_matches = list(re.finditer(pattern, pxml_content, re.DOTALL))

    for match in section_matches:
        section_xml = match.group(0)
        designator_match = re.search(r'<designator[^>]*>([\d:.A-Za-z]+)</designator>', section_xml)
        if designator_match:
            designator = designator_match.group(1)
            section_dict[designator] = section_xml

    return section_dict


def strip_xml_tags(text):
    """Remove XML tags and decode entities for plain text comparison."""
    text = re.sub(r'<[^>]+>', ' ', text)
    entity_map = {
        '&ldquo;': '"', '&rdquo;': '"',
        '&lsquo;': "'", '&rsquo;': "'",
        '&amp;': '&', '&lt;': '<', '&gt;': '>',
        '&mdash;': '—', '&ndash;': '–',
        '&hellip;': '…', '&sect;': '§',
        '&lsqb;': '[', '&rsqb;': ']',
        '&apos;': "'", '&dblsect;': '§§',
    }
    for ent, char in entity_map.items():
        text = text.replace(ent, char)
    text = re.sub(r'&[a-zA-Z]+;', '', text)
    text = re.sub(r'&#\d+;', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


# ─────────────────────────────────────────────
# IMAGE HANDLING
# ─────────────────────────────────────────────

def get_sorted_image_paths(images_folder):
    """Get all PNG images sorted by page number."""
    image_files = []
    for f in Path(images_folder).glob("*.png"):
        # Extract page number from filename (e.g., "page_3.png" or "doc_page_3.png")
        m = re.search(r'_page_(\d+)\.png$', str(f))
        if m:
            image_files.append((int(m.group(1)), str(f)))
        else:
            # Try simple numeric sort
            m2 = re.search(r'(\d+)\.png$', str(f))
            if m2:
                image_files.append((int(m2.group(1)), str(f)))

    image_files.sort(key=lambda x: x[0])
    return image_files


def encode_image_base64(image_path):
    """Encode image to base64 for API."""
    with open(image_path, 'rb') as f:
        return base64.b64encode(f.read()).decode('utf-8')


# ─────────────────────────────────────────────
# TEXT EXTRACTION FROM IMAGE (via LLM)
# ─────────────────────────────────────────────

def extract_text_from_image(image_path, client, model_name):
    """Use Claude to extract plain text from a page image."""
    img_b64 = encode_image_base64(image_path)

    response = client.messages.create(
        model=model_name,
        max_tokens=4000,
        temperature=0,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": img_b64}
                },
                {
                    "type": "text",
                    "text": "Extract ALL the text visible on this page. Include both regular text and struck-through text. Output plain text only, preserving paragraph breaks. Do not add any commentary."
                }
            ]
        }]
    )
    return response.content[0].text.strip()


# ─────────────────────────────────────────────
# SECTION MATCHING
# ─────────────────────────────────────────────

def match_page_to_sections(page_text, section_dict, top_n=2):
    """
    Match extracted page text to section chunks.
    Returns top N matching sections ranked by similarity.
    """
    page_text_lower = page_text.lower()
    scores = []

    for designator, section_xml in section_dict.items():
        section_text = strip_xml_tags(section_xml).lower()
        # Use SequenceMatcher for similarity
        # But for speed, first check word overlap
        page_words = set(page_text_lower.split())
        section_words = set(section_text.split())

        if not section_words:
            continue

        overlap = len(page_words & section_words) / max(len(page_words), 1)

        # For high-overlap candidates, compute more precise ratio
        if overlap > 0.1:
            # Use first 500 chars for quick ratio
            ratio = difflib.SequenceMatcher(
                None,
                page_text_lower[:1000],
                section_text[:1000]
            ).ratio()
        else:
            ratio = overlap

        scores.append((designator, ratio))

    scores.sort(key=lambda x: x[1], reverse=True)
    return scores[:top_n]


# ─────────────────────────────────────────────
# LLM VALIDATION
# ─────────────────────────────────────────────

def validate_page_with_llm(image_path, section_xml, designator, client, model_name):
    """
    Send page image + section XML to Claude for validation.
    Returns structured validation result.
    """
    img_b64 = encode_image_base64(image_path)

    # Normalize section XML indentation for token efficiency
    section_normalized = re.sub(r'[\t ]*\n[\t ]*', '\n', section_xml)
    section_normalized = re.sub(r'\n+', '\n', section_normalized)
    section_normalized = re.sub(r'\t+', ' ', section_normalized)

    # Truncate if too large
    max_section_chars = 40000
    if len(section_normalized) > max_section_chars:
        section_normalized = section_normalized[:max_section_chars]

    prompt = f"""You are a track-changes auditor for XML legal documents.

I am showing you:
1. A PAGE IMAGE from a revision document with visible track changes
2. The POST-VENDOR XML for section {designator}

FIRST: Confirm this XML section matches the content visible in the image.
If it does NOT match, respond with: WRONG_SECTION: true

If it DOES match, validate each visible track change:

FOR EACH STRUCK-THROUGH TEXT in the image:
- Check that it is wrapped in <trace.deleted>TEXT<trace.deleted/> in the XML
- OR that a <trace.deleted/> marker exists where the text was removed
- If the struck-through text is MISSING from the XML (no trace.deleted marker), report it as MISSED_DELETION

FOR EACH COLORED/INSERTED TEXT in the image (text in red/blue/color without strikethrough):
- Check that it is wrapped in <trace>TEXT</trace> in the XML
- If the inserted text is MISSING from the XML (no trace tags), report it as MISSED_INSERTION

FOR EACH REPLACEMENT (struck-through + colored adjacent text):
- Check both <trace.deleted>old<trace.deleted/> AND <trace>new</trace> exist
- If either is missing, report as MISSED_REPLACEMENT

Respond in this EXACT format:

WRONG_SECTION: true/false
TOTAL_CHANGES_IN_IMAGE: <number of visible track changes>
CORRECTLY_APPLIED: <number correctly found in XML>
MISSED_CHANGES: <number of changes NOT found in XML>

For each missed change, list:
MISSED:
- TYPE: deletion/insertion/replacement
- TEXT: <the text that was missed>
- CONTEXT: <surrounding text for location>
- REASON: <why it's missed — e.g. "no trace.deleted found", "text absent from XML">

If ALL changes are correctly applied:
MISSED:
- none

POST-VENDOR XML (section {designator}):
{section_normalized}"""

    response = client.messages.create(
        model=model_name,
        max_tokens=3000,
        temperature=0,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": img_b64}
                },
                {
                    "type": "text",
                    "text": prompt
                }
            ]
        }]
    )

    return response.content[0].text.strip()


def parse_validation_response(raw_text):
    """Parse the LLM validation response into structured data."""
    result = {
        'wrong_section': False,
        'total_changes': 0,
        'correctly_applied': 0,
        'missed_changes': 0,
        'missed_details': [],
        'raw_response': raw_text,
    }

    # WRONG_SECTION
    ws_match = re.search(r'WRONG_SECTION:\s*(true|false)', raw_text, re.IGNORECASE)
    if ws_match:
        result['wrong_section'] = ws_match.group(1).lower() == 'true'

    # TOTAL_CHANGES_IN_IMAGE
    tc_match = re.search(r'TOTAL_CHANGES_IN_IMAGE:\s*(\d+)', raw_text)
    if tc_match:
        result['total_changes'] = int(tc_match.group(1))

    # CORRECTLY_APPLIED
    ca_match = re.search(r'CORRECTLY_APPLIED:\s*(\d+)', raw_text)
    if ca_match:
        result['correctly_applied'] = int(ca_match.group(1))

    # MISSED_CHANGES
    mc_match = re.search(r'MISSED_CHANGES:\s*(\d+)', raw_text)
    if mc_match:
        result['missed_changes'] = int(mc_match.group(1))

    # Parse individual missed items
    missed_blocks = re.findall(
        r'- TYPE:\s*(.*?)\n\s*- TEXT:\s*(.*?)\n\s*- CONTEXT:\s*(.*?)\n\s*- REASON:\s*(.*?)(?=\n- TYPE:|\Z)',
        raw_text, re.DOTALL
    )
    for m_type, m_text, m_context, m_reason in missed_blocks:
        result['missed_details'].append({
            'type': m_type.strip(),
            'text': m_text.strip()[:200],
            'context': m_context.strip()[:200],
            'reason': m_reason.strip()[:200],
        })

    return result


# ─────────────────────────────────────────────
# MAIN ORCHESTRATION
# ─────────────────────────────────────────────

def run_track_changes_validation(postvendor_path, images_folder, output_excel=None):
    """Main function to validate track changes against page images."""
    print(f"{'='*80}")
    print("TRACK CHANGES VALIDATION CHECKER")
    print(f"{'='*80}")
    print(f"  Postvendor PXML: {postvendor_path}")
    print(f"  Images folder:   {images_folder}")

    # Build client
    client = build_anthropic_client(MODEL_OPUS)
    model_name = MODEL_OPUS
    print(f"  Model: {model_name}")

    # Read PXML and parse sections
    with open(postvendor_path, 'r', encoding='utf-8') as f:
        pxml_content = f.read()

    section_dict = parse_pxml_sections(pxml_content)
    print(f"  Sections found: {len(section_dict)}")

    # Get images
    image_files = get_sorted_image_paths(images_folder)
    print(f"  Page images found: {len(image_files)}")

    if not image_files:
        print("  ERROR: No images found in folder!")
        return None

    # Process each page
    all_results = []
    total_missed = 0

    for page_num, image_path in image_files:
        print(f"\n  [Page {page_num}] Processing {os.path.basename(image_path)}...")

        # Step 1: Extract text from image for section matching
        try:
            page_text = extract_text_from_image(image_path, client, MODEL_SONNET)
            print(f"    Extracted text: {len(page_text)} chars")
        except Exception as e:
            print(f"    ⚠️ Text extraction failed: {e}")
            all_results.append({
                'Page': page_num,
                'Section': 'ERROR',
                'Total_Changes': 0,
                'Correctly_Applied': 0,
                'Missed_Changes': 0,
                'Status': f'TEXT_EXTRACTION_ERROR: {e}',
                'Missed_Details': '',
            })
            continue

        # Step 2: Match to sections
        top_matches = match_page_to_sections(page_text, section_dict, top_n=2)
        if not top_matches:
            print(f"    ⚠️ No section match found")
            all_results.append({
                'Page': page_num,
                'Section': 'NO_MATCH',
                'Total_Changes': 0,
                'Correctly_Applied': 0,
                'Missed_Changes': 0,
                'Status': 'NO_SECTION_MATCH',
                'Missed_Details': '',
            })
            continue

        print(f"    Top matches: {[(d, f'{s:.3f}') for d, s in top_matches]}")

        # Step 3: Validate with LLM (try first match, fallback to second)
        validated = False
        for rank, (designator, score) in enumerate(top_matches):
            print(f"    Trying section {designator} (score={score:.3f})...")

            try:
                raw_response = validate_page_with_llm(
                    image_path, section_dict[designator], designator, client, model_name
                )
                parsed = parse_validation_response(raw_response)

                if parsed['wrong_section'] and rank == 0 and len(top_matches) > 1:
                    print(f"    ❌ Wrong section — trying next match...")
                    continue

                if parsed['wrong_section']:
                    print(f"    ❌ Wrong section — no more candidates")
                    all_results.append({
                        'Page': page_num,
                        'Section': designator,
                        'Total_Changes': 0,
                        'Correctly_Applied': 0,
                        'Missed_Changes': 0,
                        'Status': 'WRONG_SECTION_MATCH',
                        'Missed_Details': '',
                    })
                    validated = True
                    break

                # Valid match
                missed_str = ''
                if parsed['missed_details']:
                    missed_str = ' | '.join(
                        f"[{m['type']}] {m['text'][:80]} — {m['reason'][:60]}"
                        for m in parsed['missed_details']
                    )

                status = 'ALL_CORRECT' if parsed['missed_changes'] == 0 else 'HAS_MISSED_CHANGES'
                
                all_results.append({
                    'Page': page_num,
                    'Section': designator,
                    'Total_Changes': parsed['total_changes'],
                    'Correctly_Applied': parsed['correctly_applied'],
                    'Missed_Changes': parsed['missed_changes'],
                    'Status': status,
                    'Missed_Details': missed_str[:2000],
                })

                total_missed += parsed['missed_changes']

                if parsed['missed_changes'] > 0:
                    print(f"    ❌ {parsed['missed_changes']} missed changes!")
                    for m in parsed['missed_details']:
                        print(f"       [{m['type']}] {m['text'][:60]} — {m['reason'][:40]}")
                else:
                    print(f"    ✅ All {parsed['total_changes']} changes correctly applied")

                validated = True
                break

            except Exception as e:
                print(f"    ⚠️ Validation error: {e}")
                if rank == len(top_matches) - 1:
                    all_results.append({
                        'Page': page_num,
                        'Section': designator,
                        'Total_Changes': 0,
                        'Correctly_Applied': 0,
                        'Missed_Changes': 0,
                        'Status': f'LLM_ERROR: {str(e)[:100]}',
                        'Missed_Details': '',
                    })

        if not validated:
            all_results.append({
                'Page': page_num,
                'Section': 'UNVALIDATED',
                'Total_Changes': 0,
                'Correctly_Applied': 0,
                'Missed_Changes': 0,
                'Status': 'COULD_NOT_VALIDATE',
                'Missed_Details': '',
            })

    # Generate Excel report
    df = pd.DataFrame(all_results)

    if not output_excel:
        output_dir = os.path.dirname(postvendor_path)
        timestamp = time.strftime('%Y%m%d_%H%M%S')
        output_excel = os.path.join(output_dir, f"track_changes_validation_{timestamp}.xlsx")

    df.to_excel(output_excel, index=False, sheet_name='Validation Report')

    # Format Excel
    try:
        from openpyxl import load_workbook
        from openpyxl.styles import Font, PatternFill, Alignment

        wb = load_workbook(output_excel)
        ws = wb.active

        header_fill = PatternFill(start_color='1565C0', end_color='1565C0', fill_type='solid')
        header_font = Font(bold=True, color='FFFFFF')
        red_fill = PatternFill(start_color='FFC7CE', end_color='FFC7CE', fill_type='solid')
        green_fill = PatternFill(start_color='C6EFCE', end_color='C6EFCE', fill_type='solid')
        yellow_fill = PatternFill(start_color='FFEB9C', end_color='FFEB9C', fill_type='solid')

        for cell in ws[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal='center')

        # Color rows
        status_col = None
        for cell in ws[1]:
            if cell.value == 'Status':
                status_col = cell.column
                break

        if status_col:
            for row_num in range(2, ws.max_row + 1):
                status = ws.cell(row=row_num, column=status_col).value or ''
                if 'ALL_CORRECT' in status:
                    fill = green_fill
                elif 'MISSED' in status:
                    fill = red_fill
                else:
                    fill = yellow_fill
                for col in range(1, ws.max_column + 1):
                    ws.cell(row=row_num, column=col).fill = fill

        ws.column_dimensions['A'].width = 8
        ws.column_dimensions['B'].width = 12
        ws.column_dimensions['C'].width = 15
        ws.column_dimensions['D'].width = 18
        ws.column_dimensions['E'].width = 15
        ws.column_dimensions['F'].width = 20
        ws.column_dimensions['G'].width = 100

        ws.freeze_panes = 'A2'
        ws.auto_filter.ref = ws.dimensions
        wb.save(output_excel)
    except Exception as e:
        print(f"  ⚠️ Excel formatting error: {e}")

    # Summary
    print(f"\n{'='*80}")
    print(f"  Pages validated: {len(all_results)}")
    print(f"  Total missed changes: {total_missed}")
    correct_pages = sum(1 for r in all_results if r['Status'] == 'ALL_CORRECT')
    print(f"  Pages with all correct: {correct_pages}/{len(all_results)}")
    print(f"  Report saved to: {output_excel}")
    print(f"{'='*80}")

    return df


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    if not POSTVENDOR_PXML_PATH or not IMAGES_FOLDER:
        print("ERROR: Please set POSTVENDOR_PXML_PATH and IMAGES_FOLDER at the top of this file.")
    else:
        run_track_changes_validation(POSTVENDOR_PXML_PATH, IMAGES_FOLDER, OUTPUT_EXCEL_PATH or None)
