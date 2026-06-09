def find_and_mark_inlinereplacements(change, xml_section_chunk):
    """
    Call Claude API to generate FIND/REPLACE output for inline replacements.
    """
    deleted_text = change.get('deleted_text', '')
    inserted_text = change.get('inserted_text', '')
    deleted_text_para = change.get('deleted_text_para', '')
    previous_words = change.get('previous_words', '')
    post_words = change.get('post_words', '')
    previous_context = change.get('previous_context', '')
    post_context = change.get('post_context', '')

    prompt = f"""You are an XML editor. Your task is to locate a specific text fragment inside an XML section and produce a FIND/REPLACE block.

## Change Details
- DELETED TEXT (old text to remove): "{deleted_text}"
- INSERTED TEXT (new text to put in its place): "{inserted_text}"
- PARAGRAPH (the full paragraph containing the change): "{deleted_text_para}"
- PREVIOUS WORDS (words just before the change): "{previous_words}"
- POST WORDS (words just after the change): "{post_words}"
- PREVIOUS CONTEXT (section/paragraph before): "{previous_context}"
- POST CONTEXT (section/paragraph after): "{post_context}"

## XML Section
{xml_section_chunk}

## Instructions
1. Find the exact location in the XML where "{deleted_text}" appears, using PREVIOUS WORDS and POST WORDS as anchors.
2. Extract a short but unique surrounding XML snippet (including tags if needed) that contains "{deleted_text}".
3. Produce the FIND block: the snippet exactly as it appears in the XML (with "{deleted_text}" present).
4. Produce the REPLACE block: the same snippet but with "{deleted_text}" replaced by "{inserted_text}".
5. Do NOT change anything else — preserve all XML tags, attributes, and surrounding text exactly.
6. The FIND text must be verbatim from the XML. The REPLACE text must only differ by the deletion/insertion.

## Output Format (strictly follow this)
FIND:
<exact snippet from XML containing the deleted text>
REPLACE:
<same snippet with deleted text swapped for inserted text>
"""

    import anthropic
    from config import MODEL_OPUS
    client = anthropic.Anthropic(api_key=API_KEY)
    message = client.messages.create(
        model=MODEL_OPUS,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text


def apply_tracked_inlinereplacements(xml_content, tracked_change_result, missing_revisions_output):
    """
    Parse the FIND/REPLACE output and apply it to the XML content.
    Matching strategy:
        1. Exact match
        2. Flexible whitespace regex pattern
        3. Fuzzy match using difflib SequenceMatcher
    """
    try:
        # Extract FIND and REPLACE sections
        find_match = re.search(r'FIND:\s*\n(.*?)\n\s*REPLACE:', tracked_change_result, re.DOTALL)
        replace_match = re.search(r'REPLACE:\s*\n(.*?)(?:\n\s*$|$)', tracked_change_result, re.DOTALL)

        if not find_match or not replace_match:
            print("Warning: Could not parse FIND/REPLACE format")
            os.makedirs(os.path.dirname(missing_revisions_output), exist_ok=True)
            with open(missing_revisions_output, 'a', encoding='utf-8') as f:
                f.write("Could not parse FIND/REPLACE format\n")
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
            f.write("INLINE REPLACEMENT: NOT FOUND after all match stages\n")
            f.write(f"FIND TEXT:\n{find_text}\n")
            f.write(f"REPLACE TEXT:\n{replace_text}\n")
            f.write("-" * 60 + "\n")
        return xml_content, False

    except Exception as e:
        print(f"Error applying tracked replacement: {e}")
        import traceback
        traceback.print_exc()
        return xml_content, False


def process_all_changes_complete(api_key, updated_result, section_dict, section_positions, pxml_content, missing_revisions_output, section_pattern):
    """
    Process all inline replacement changes across all sections.
    Mirrors process_all_changes_inlineinsertions but for type='inline_replacement'.
    Returns updated_pxmlcontent with all replacements applied.
    """
    replacements = []

    for k, v in updated_result.items():
        designator = extract_designator(k, section_pattern=section_pattern)

        if not designator:
            print(f"Warning: Could not extract designator from key: {k}")
            continue

        print('section--keys------', section_dict.keys())

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
            # Only process inline_replacement type
            if change.get('type') != 'inline_replacement':
                print(f"Skipping non-replacement change type: {change.get('type')}")
                continue

            print(f"\n{'='*80}")
            print(f"Processing change {idx}/{len(v)}: '{change['deleted_text']}' -> '{change['inserted_text']}'")
            print(f"{'='*80}")

            # Skip if inserted_text is empty or single character
            if not change['inserted_text'] or len(str(change['inserted_text'])) <= 1:
                print(f"Skipping trivial insertion: '{change['inserted_text']}'")
                with open(missing_revisions_output, 'a', encoding='utf-8') as f:
                    f.write(f"{'='*80}\n")
                    f.write(f"SINGLE-LETTER REPLACEMENT IGNORED\n")
                    f.write(f"{'-'*40}\n")
                    f.write(f"Section          : {k}\n")
                    f.write(f"Deleted Text     : {change['deleted_text']}\n")
                    f.write(f"Inserted Text    : {change['inserted_text']}\n")
                    f.write(f"{'='*80}\n\n")
                continue

            # Also skip if deleted_text is empty — nothing to find
            if not change['deleted_text'] or len(str(change['deleted_text'])).strip() == '':
                print(f"Skipping change with empty deleted_text.")
                continue

            result = find_and_mark_inlinereplacements(change, xml_section_chunk)
            xml_section_chunk, success = apply_tracked_inlinereplacements(
                xml_section_chunk, result, missing_revisions_output
            )

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