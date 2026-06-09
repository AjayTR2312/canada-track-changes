import pandas as pd
from collections import defaultdict
from config import API_KEY, MODEL_OPUS


def convert_insertions_df_json(inline_insertions_newdf):
    
    # Assuming your dataframe is called 'df'
    result_json = defaultdict(list)

    for index, row in inline_insertions_newdf.iterrows():
        section = row['section']

        # Create a dictionary with all columns except 'section'
        row_data = {
            'text': row['text'],
            'has_changes': row['has_changes'],
            'type': row['type'],
            'paragraph_index': row['paragraph_index'],
            'deleted_text': row['deleted_text'],
            'inserted_text': row['inserted_text'],
            # 'what_got_deleted': row['what_got_deleted'],
            'previous_context': row['previous_context'],
            'post_context': row['post_context'],
            'previous_words':row['prev_words'],
            'post_words':row['post_words']
        }

        result_json[section].append(row_data)

    # Convert defaultdict to regular dict
    result_json = dict(result_json)

    return result_json


##working##

import anthropic
import os
import re

def find_and_mark_inlineinsertions(change, xml_section_chunk):
    """
    Uses Claude Opus 4.1 to find the exact location in XML and create tracked changes
    """
    client = anthropic.Anthropic(
        api_key=API_KEY
    )

    prompt = f"""You are an expert in XML document editing and legal document formatting.
Your task is to:
1. Find where the 'text' text is located in the XML section provided
2. Identify the exact XML element containing the change
3. Create a tracked change using <trace> for insertions
CHANGE INFORMATION:
- Inserted text in paragraph: {change['text']}
- What got inserted: {change['inserted_text']}
- Previous words: {change['previous_words']}
- Post words: {change['post_words']}
- Previous context: {change['previous_context']}
- Post context: {change['post_context']}
XML SECTION:
{xml_section_chunk}
INSTRUCTIONS:
1. Locate the exact XML element containing the text that matches  'text'
2. Within that element, find where '{change['inserted_text']}' needs to be inserted
3. Insert it with the tracked change format:
   For partial/inline text changes within an element:

	- Wrap the inserted text with <trace>{change['inserted_text']}</trace> tags
	- Example: If changing part of the text inside an element, use <trace> around just the changed portion
   For complete text replacement within an element:

	- When replacing ALL text content inside an element, do NOT add <trace> tags
	- Simply replace the entire text content within the original element structure
	- Keep the original element tags intact, only change the text content inside
4. Maintain all other XML structure and attributes exactly as they are
OUTPUT FORMAT:
Provide your response in this exact format:
FIND:
[paste the complete original XML element that needs to be changed]
INSERT:
[paste the complete XML element with tracked changes applied]
Only provide the FIND and REPLACE sections, nothing else
Dont add any fences or extra text, just the raw XML in the FIND and INSERT sections."""

    message = client.messages.create(
        model=MODEL_OPUS,
        max_tokens=4096,
        messages=[
            {"role": "user", "content": prompt}
        ]
    )

    return message.content[0].text


import re
 
import re
import difflib

def apply_tracked_inlineinsertions(xml_content, tracked_change_result, missing_revisions_output):
    """
    Parse the FIND/INSERT output and apply it to the XML content.
    Matching strategy:
        1. Exact match
        2. Flexible whitespace regex pattern
        3. Fuzzy match using difflib SequenceMatcher
    """
    try:
        # Extract FIND and INSERT sections
        find_match = re.search(r'FIND:\s*\n(.*?)\n\s*INSERT:', tracked_change_result, re.DOTALL)
        replace_match = re.search(r'INSERT:\s*\n(.*?)(?:\n\s*$|$)', tracked_change_result, re.DOTALL)

        if not find_match or not replace_match:
            print("Warning: Could not parse FIND/INSERT format")
            os.makedirs(os.path.dirname(missing_revisions_output), exist_ok=True)
            with open(missing_revisions_output, 'a', encoding='utf-8') as f:
                f.write("Could not parse FIND/INSERT format\n")
                f.write(f"Raw input:\n{tracked_change_result[:300]}\n")
                f.write("-" * 60 + "\n")
            return xml_content, False

        find_text = find_match.group(1).strip()
        replace_text = replace_match.group(1).strip()

        # ── Stage 1: Exact Match ──────────────────────────────────────────
        match_result = _exact_match(xml_content, find_text)
        if match_result:
            print("[Stage 1] Exact match found.")
            return _apply_replacement(xml_content, match_result, replace_text, mode='exact'), True

        # ── Stage 2: Flexible Whitespace Regex ───────────────────────────
        match_result = _flexible_pattern_match(xml_content, find_text)
        if match_result:
            print("[Stage 2] Flexible pattern match found.")
            return _apply_replacement(xml_content, match_result, replace_text, mode='regex'), True

        # ── Stage 3: Fuzzy Match via difflib ─────────────────────────────
        match_result = _fuzzy_match(xml_content, find_text)
        if match_result:
            print(f"[Stage 3] Fuzzy match found with ratio: {match_result['ratio']:.2f}")
            return _apply_replacement(xml_content, match_result, replace_text, mode='fuzzy'), True

        # ── All Stages Failed ─────────────────────────────────────────────
        print("Warning: Could not find the text to replace in XML after all stages.")
        print(f"Looking for: {find_text[:100]}...")
        os.makedirs(os.path.dirname(missing_revisions_output), exist_ok=True)
        with open(missing_revisions_output, 'a', encoding='utf-8') as f:
            f.write("INLINE INSERTION: NOT FOUND after all match stages\n")
            f.write(f"FIND TEXT:\n{find_text}\n")
            f.write(f"INSERT TEXT:\n{replace_text}\n")
            f.write("-" * 60 + "\n")
        return xml_content, False

    except Exception as e:
        print(f"Error applying tracked change: {e}")
        import traceback
        traceback.print_exc()
        return xml_content, False

# def apply_tracked_inlineinsertions(xml_content, tracked_change_result, missing_revisions_output):
#     """
#     Parse the FIND/INSERT output and apply it to the XML content.
#     Matching strategy:
#         1. Exact match
#         2. Flexible whitespace regex pattern
#         3. Fuzzy match using difflib SequenceMatcher
#     """
#     try:
#         # Extract FIND and INSERT sections
#         find_match = re.search(r'FIND:\s*\n(.*?)\n\s*INSERT:', tracked_change_result, re.DOTALL)
#         replace_match = re.search(r'INSERT:\s*\n(.*?)(?:\n\s*$|$)', tracked_change_result, re.DOTALL)

#         if not find_match or not replace_match:
#             print("Warning: Could not parse FIND/INSERT format")
#             return xml_content, False

#         find_text = find_match.group(1).strip()
#         replace_text = replace_match.group(1).strip()

#         # ── Stage 1: Exact Match ──────────────────────────────────────────
#         match_result = _exact_match(xml_content, find_text)
#         if match_result:
#             print("[Stage 1] Exact match found.")
#             return _apply_replacement(xml_content, match_result, replace_text, mode='exact'), True

#         # ── Stage 2: Flexible Whitespace Regex ───────────────────────────
#         match_result = _flexible_pattern_match(xml_content, find_text)
#         if match_result:
#             print("[Stage 2] Flexible pattern match found.")
#             return _apply_replacement(xml_content, match_result, replace_text, mode='regex'), True

#         # ── Stage 3: Fuzzy Match via difflib ─────────────────────────────
#         match_result = _fuzzy_match(xml_content, find_text)
#         if match_result:
#             print(f"[Stage 3] Fuzzy match found with ratio: {match_result['ratio']:.2f}")
#             return _apply_replacement(xml_content, match_result, replace_text, mode='fuzzy'), True

#         print("Warning: Could not find the text to replace in XML after all stages.")
#         print(f"Looking for: {find_text[:100]}...")
#         return xml_content, False

#     except Exception as e:
#         print(f"Error applying tracked change: {e}")
#         import traceback
#         traceback.print_exc()
#         return xml_content, False


# ── Match Helpers ─────────────────────────────────────────────────────────────

def _exact_match(xml_content, find_text):
    """Stage 1: Plain string match."""
    idx = xml_content.find(find_text)
    if idx != -1:
        return {
            'start': idx,
            'end': idx + len(find_text),
            'matched_text': find_text
        }
    return None


def _flexible_pattern_match(xml_content, find_text):
    """Stage 2: Regex match ignoring whitespace differences between tags/lines."""
    lines = [line.strip() for line in find_text.split('\n') if line.strip()]
    pattern = r'\s*'.join(re.escape(line) for line in lines)
    match = re.search(pattern, xml_content, re.DOTALL)
    if match:
        return {
            'start': match.start(),
            'end': match.end(),
            'matched_text': match.group(0)
        }
    return None


def _fuzzy_match(xml_content, find_text, threshold=0.6, window_multiplier=3):
    """
    Stage 3: Sliding window fuzzy match using difflib SequenceMatcher.
    Scans xml_content with a window sized as a multiple of find_text length.
    Returns the best match above the threshold.
    """
    find_len = len(find_text)
    window_size = min(find_len * window_multiplier, len(xml_content))
    step = max(1, find_len // 2)  # 50% overlap between windows

    best = {'ratio': 0, 'start': -1, 'end': -1, 'matched_text': ''}

    for start in range(0, len(xml_content) - find_len + 1, step):
        end = min(start + window_size, len(xml_content))
        window = xml_content[start:end]

        matcher = difflib.SequenceMatcher(None, find_text, window, autojunk=False)
        ratio = matcher.ratio()

        if ratio > best['ratio']:
            best = {
                'ratio': ratio,
                'start': start,
                'end': start + find_len,  # keep replacement length consistent
                'matched_text': window[:find_len]
            }

    if best['ratio'] >= threshold:
        return best

    return None


# ── Replacement Helper ────────────────────────────────────────────────────────

def _apply_replacement(xml_content, match_result, replace_text, mode='exact'):
    """
    Apply the replacement while preserving original indentation.
    """
    start = match_result['start']
    end = match_result['end']
    matched_text = match_result['matched_text']

    # Detect base indentation from the matched block
    first_line_match = re.match(r'^(\s*)', matched_text)
    base_indent = first_line_match.group(1) if first_line_match else ''

    # Re-indent replace_text to match
    replace_lines = replace_text.split('\n')
    indented_replace = '\n'.join(
        base_indent + line.strip() if line.strip() else ''
        for line in replace_lines
    )

    return xml_content[:start] + indented_replace + xml_content[end:]
    
def parse_pxml_sections_from_str(pxml_content):
    """Read PXML file and extract sections mapped by designator"""
    
    
    section_dict = {}
    section_positions = {}
    
    pattern = r'<section\s+(?:uuid="[^"]+"\s+)?[^>]*>.*?</section>'
    section_matches = re.finditer(pattern, pxml_content, re.DOTALL)
    
    for match in section_matches:
        section_xml = match.group(0)
        start_pos = match.start()
        end_pos = match.end()
        
        designator_match = re.search(r'<designator[^>]*>([\d:.A-Za-z]+)</designator>', section_xml)
        
        if designator_match:
            designator = designator_match.group(1)
            section_dict[designator] = section_xml
            section_positions[designator] = (start_pos, end_pos)
    
    return section_dict, section_positions, pxml_content
 


def extract_designator(section_key, section_pattern=None):
    """Extract designator like '3:1' or '3.1' from section string"""
    if section_key is None:
        return None
    # if section_pattern:
    #     match = re.search(section_pattern, str(section_key))
    #     if match:
    #         return match.group(0).strip()
    # else:
    match = re.search(r'§?\s*(\d+[A-Za-z]*[:.][\dA-Za-z]+)', str(section_key))
    if match:
        return match.group(1)
    


def find_section_in_pxml(pxml_content, designator):
    """
    Find the complete <section>...</section> block containing the given designator
    Returns: (start_pos, end_pos, section_content) or (None, None, None) if not found
    """
    # Escape special regex characters in designator
    escaped_designator = re.escape(designator)

    # Find the designator tag
    designator_pattern = rf'<designator[^>]*>{escaped_designator}</designator>'
    designator_match = re.search(designator_pattern, pxml_content)

    if not designator_match:
        print(f"Warning: Could not find designator '{designator}' in pxml")
        return None, None, None

    designator_pos = designator_match.start()

    # Search backwards from designator to find the opening <section tag
    # Look for <section uuid="..." ...>
    section_start_pattern = r'<section\s+uuid='

    # Find all section starts before the designator
    section_starts = list(re.finditer(section_start_pattern, pxml_content[:designator_pos]))

    if not section_starts:
        print(f"Warning: Could not find <section> tag before designator '{designator}'")
        return None, None, None

    # Get the last section start before the designator
    section_start_match = section_starts[-1]
    start_pos = section_start_match.start()

    # Now find the matching closing </section> tag
    # We need to handle nested sections, so we'll count opening and closing tags
    section_depth = 0
    search_pos = start_pos

    while search_pos < len(pxml_content):
        # Find next section tag (opening or closing)
        open_match = re.search(r'<section\s', pxml_content[search_pos:])
        close_match = re.search(r'</section>', pxml_content[search_pos:])

        # Adjust positions to be relative to full content
        open_pos = search_pos + open_match.start() if open_match else float('inf')
        close_pos = search_pos + close_match.start() if close_match else float('inf')

        if open_pos < close_pos:
            section_depth += 1
            search_pos = open_pos + 1
        elif close_pos < float('inf'):
            if section_depth == 1:
                # Found the matching closing tag
                end_pos = close_pos + len('</section>')
                section_content = pxml_content[start_pos:end_pos]
                return start_pos, end_pos, section_content
            else:
                section_depth -= 1
                search_pos = close_pos + 1
        else:
            # No more tags found
            break

    print(f"Warning: Could not find closing </section> tag for designator '{designator}'")
    return None, None, None


# def process_all_changes_inlineinsertions(updated_result, section_dict,missing_revisions_output):
#     """
#     Process ALL changes for all sections in updated_result
#     Updates xml_section_chunk after each change is applied
#     Does NOT modify the original section_dict
#     """
#     results = {}

#     for k, v in updated_result.items():
#         # Extract designator from the key
#         designator = extract_designator(k)

#         if not designator:
#             print(f"Warning: Could not extract designator from key: {k}")
#             continue

#         # Find matching key in section_dict
#         xml_section_chunk = None
#         matched_section_key = None
#         for section_key in section_dict.keys():
#             if designator in section_key:
#                 xml_section_chunk = section_dict[section_key]
#                 matched_section_key = section_key
#                 break

#         if xml_section_chunk is None:
#             print(f"Warning: Could not find section for designator '{designator}' in section_dict")
#             continue

#         print(f"\n{'#'*80}")
#         print(f"Processing section: {k} (Designator: {designator})")
#         print(f"Matched section_dict key: {matched_section_key}")
#         print(f"{'#'*80}")

#         section_results = []

#         # Process ALL changes
#         for idx, change in enumerate(v, 1):
#             print(f"\n{'='*80}")
#             print(f"Processing change {idx}/{len(v)}: {change['deleted_text']} -> {change['inserted_text']}")
#             print(f"{'='*80}")

#             # Get tracked change from Claude
#             result = find_and_mark_inlineinsertions(change, xml_section_chunk)
#             print(result)

#             # Apply the change to get updated XML
#             xml_section_chunk, success = apply_tracked_inlineinsertions(xml_section_chunk, result,missing_revisions_output)

#             section_results.append({
#                 'change': change,
#                 'tracked_change': result,
#                 'applied_successfully': success
#             })

#             print(f"\nClaude's Response:")
#             print(result)
#             print(f"\nChange applied: {success}")
#             print("-" * 80)

#         results[k] = {
#             'designator': designator,
#             'original_section_key': matched_section_key,
#             'changes': section_results,
#             'final_xml': xml_section_chunk  # Store the final updated XML
#         }

#     return results

def process_all_changes_inlineinsertions(updated_result, section_dict, section_positions, pxml_content, missing_revisions_output,section_pattern):

    replacements = []

    for k, v in updated_result.items():
        designator = extract_designator(k,section_pattern=section_pattern)

        if not designator:
            print(f"Warning: Could not extract designator from key: {k}")
            continue
        print('section--keys------',section_dict.keys())
        xml_section_chunk = None
        matched_section_key = None
        for section_key in section_dict.keys():
            if designator in section_key:
                xml_section_chunk = section_dict[section_key]
                matched_section_key = section_key
                break

        if xml_section_chunk is None:
            print(f"Warning: Could not find section for designator '{designator}' in section_dict")
            continue

        print(f"\n{'#'*80}")
        print(f"Processing section: {k} (Designator: {designator})")
        print(f"Matched section_dict key: {matched_section_key}")
        print(f"{'#'*80}")


        for idx, change in enumerate(v, 1):
            print(f"\n{'='*80}")
            print(f"Processing change {idx}/{len(v)}: {change['deleted_text']} -> {change['inserted_text']}")
            print(f"{'='*80}")

            # Skip single-letter insertions
            if not change['inserted_text'] or len(str(change['inserted_text'])) <= 1:
                print(f"Skipping single-letter insertion: {change['inserted_text']}")
                with open(missing_revisions_output, 'a', encoding='utf-8') as f:
                    f.write(f"{'='*80}\n")
                    f.write(f"SINGLE-LETTER INSERTION IGNORED\n")
                    f.write(f"{'-'*40}\n")
                    f.write(f"Section          : {k}\n")
                    f.write(f"Inserted Text    : {change['inserted_text']}\n")
                    # f.write(f"Deleted Text     : {change['deleted_text']}\n")
                    f.write(f"{'='*80}\n\n")
                continue

            result = find_and_mark_inlineinsertions(change, xml_section_chunk)
            xml_section_chunk, success = apply_tracked_inlineinsertions(xml_section_chunk, result, missing_revisions_output)

            print(f"\nClaude's Response:\n{result}")
            print(f"\nChange applied: {success}")
            print("-" * 80)

        # Collect replacement only if section actually changed
        original_xml = section_dict[matched_section_key]
        if xml_section_chunk != original_xml:
            start_pos, end_pos = section_positions[matched_section_key]
            replacements.append((start_pos, end_pos, xml_section_chunk))

    # Apply replacements end-to-start to preserve positional integrity
    replacements.sort(key=lambda x: x[0], reverse=True)
    updated_pxmlcontent = pxml_content
    for start_pos, end_pos, final_xml in replacements:
        updated_pxmlcontent = updated_pxmlcontent[:start_pos] + final_xml + updated_pxmlcontent[end_pos:]

    return updated_pxmlcontent


def update_pxml_with_inlineinsertions(pxml_content, all_results):
    """
    Update the pxml file with all the tracked changes

    Args:
        pxml_path: Path to the original pxml file
        all_results: Results dictionary from process_all_changes_complete
        output_path: Optional output path. If None, creates a new file with '_tracked' suffix

    Returns:
        Path to the updated pxml file
    """
    

    # Create a copy to work with
    updated_pxml = pxml_content

    # Sort sections by their position in the file (process from end to start to maintain positions)
    sections_to_update = []

    for section_key, section_data in all_results.items():
        designator = section_data['designator']
        final_xml = section_data['final_xml']

        # Find the section in pxml
        start_pos, end_pos, original_section = find_section_in_pxml(updated_pxml, designator)

        if start_pos is not None:
            sections_to_update.append({
                'designator': designator,
                'start_pos': start_pos,
                'end_pos': end_pos,
                'original': original_section,
                'replacement': final_xml
            })
        else:
            print(f"Warning: Could not locate section for designator '{designator}' in pxml")

    # Sort by start position in reverse order (process from end to start)
    sections_to_update.sort(key=lambda x: x['start_pos'], reverse=True)

    # Apply replacements
    for section in sections_to_update:
        print(f"\nReplacing section with designator {section['designator']} at position {section['start_pos']}-{section['end_pos']}")
        updated_pxml = (
            updated_pxml[:section['start_pos']] + 
            section['replacement'] + 
            updated_pxml[section['end_pos']:]
        )

    # Determine output path
    return updated_pxml



 

 