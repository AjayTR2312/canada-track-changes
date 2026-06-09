"""
Section Split Handler
---------------------
When a section split is detected (new section inserted in the middle of an existing section),
this module:
1. Finds the anchor paragraph (previous para before the insertion point)
2. Uses LLM to locate the anchor in the previous section's XML (same approach as newline_insertions_new.py)
3. Tags the new section content using Canada Pamphlets Prompt
4. Inserts the tagged XML at the correct position in the PXML
"""

import re
import json
import time
import pandas as pd
from config import build_anthropic_client, MODEL_OPUS
from xml_entity_rules import XML_ENTITY_RULES, CASE_CITATION_RULES, URL_TAGGING_RULES


def find_previous_para(result_with_ba, start_paragraph_index):
    """
    Find the previous paragraph text from result_with_ba based on start_paragraph_index.
    Looks for the closest row with idx < start_paragraph_index where:
    - type == 'inline_deletion' OR has_changes == False
    
    Returns:
        tuple: (idx, text) of the previous paragraph, or (None, None) if not found
    """
    candidates = result_with_ba[
        (result_with_ba['idx'] < start_paragraph_index) &
        (
            (result_with_ba['type'] == 'inline_deletion') |
            (result_with_ba['has_changes'] == False)
        )
    ].sort_values('idx', ascending=False)

    if candidates.empty:
        return None, None

    # Skip rows with empty text
    for _, row in candidates.iterrows():
        text = str(row.get('text', '')).strip()
        if text and len(text) > 5:
            return int(row['idx']), text

    return None, None


def find_anchor_in_xml(previous_para_text, previous_section_xml, client, model_name=MODEL_OPUS):
    """
    Use LLM to find the anchor point in previous_section_xml where the new section
    should be inserted AFTER. Uses the same approach as newline_insertions_new.py.
    
    Returns:
        str: The verbatim XML snippet (insert_after) from section XML, or None if not found
    """
    prompt = f"""You are an XML editor working with legal document XML.

You will be given:
- A section of XML
- PREVIOUS_PARA: the plain text to locate in the XML

YOUR TASK:
Find the text in the XML whose plain text (ignoring all XML tags) closely matches PREVIOUS_PARA.
Then return the VERBATIM closing portion of that paragraph element.

RULES:
- PREVIOUS_PARA may be truncated — match by the beginning of the text.
- Strip all tags mentally when comparing.
- Find the <para> (or <stat.item.para>, <heading.para>, etc.) block containing this text.
- If the paragraph has child block elements (<quote.block>, <list>, <table>) after </para.text>,
  the insert_after MUST end at the OUTERMOST </para> that closes the ENTIRE structure
  (AFTER </quote.block> or other child elements).
- Return the last ~150 verbatim characters ending at the correct closing </para> tag.
- insert_after MUST be a verbatim substring that exists EXACTLY in the section XML.
- Include meaningful PLAIN TEXT CONTENT (at least 30-50 chars of plain text after stripping tags).
- If you cannot find it, reply with just the word NOT_FOUND.

{XML_ENTITY_RULES}

SECTION XML:
```xml
{previous_section_xml}
```

PREVIOUS_PARA:
{previous_para_text}

Return ONLY the verbatim insert_after string (the last ~150 chars ending at </para>).
No JSON, no explanation, no markdown fences. Just the raw XML snippet."""

    try:
        response = client.messages.create(
            model=model_name,
            max_tokens=8000,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        answer = response.content[0].text.strip()
        if answer.upper() == 'NOT_FOUND':
            return None
        # Clean markdown fences if present
        answer = re.sub(r'^```xml\s*', '', answer)
        answer = re.sub(r'\s*```$', '', answer)
        return answer.strip()
    except Exception as e:
        print(f"  ⚠️  LLM anchor detection failed: {e}")
        return None


def insert_section_at_anchor(pxml_content, anchor_xml, tagged_section_xml):
    """
    Insert the tagged section XML into pxml_content right after the anchor_xml.
    Uses whitespace-normalized matching as fallback.
    
    Returns:
        str: Updated PXML content, or None if anchor not found
    """
    if not anchor_xml or not tagged_section_xml:
        return None

    # Try exact find first
    pos = pxml_content.find(anchor_xml)
    if pos != -1:
        insert_pos = pos + len(anchor_xml)
        newline_pos = pxml_content.find('\n', insert_pos)
        if newline_pos != -1 and (newline_pos - insert_pos) < 5:
            insert_pos = newline_pos
        updated = pxml_content[:insert_pos] + '\n' + tagged_section_xml + pxml_content[insert_pos:]
        return updated

    # Try whitespace-normalized matching
    norm_pxml = re.sub(r'\s+', ' ', pxml_content)
    norm_anchor = re.sub(r'\s+', ' ', anchor_xml.strip())
    norm_pos = norm_pxml.find(norm_anchor)

    if norm_pos != -1:
        # Map normalized position back to original position
        # Walk through original content, tracking how many non-collapsed chars we've seen
        norm_end_target = norm_pos + len(norm_anchor)
        norm_count = 0
        orig_i = 0
        in_space = False
        while orig_i < len(pxml_content) and norm_count < norm_end_target:
            ch = pxml_content[orig_i]
            if ch in ' \t\n\r':
                if not in_space:
                    norm_count += 1  # collapsed whitespace = 1 space in norm
                    in_space = True
                orig_i += 1
            else:
                norm_count += 1
                in_space = False
                orig_i += 1

        # orig_i now points to right after the anchor in original
        # Find next newline
        newline_pos = pxml_content.find('\n', orig_i)
        if newline_pos != -1 and (newline_pos - orig_i) < 10:
            orig_i = newline_pos
        updated = pxml_content[:orig_i] + '\n' + tagged_section_xml + pxml_content[orig_i:]
        print(f"  📌 Inserted via normalized whitespace match at position {orig_i}")
        return updated

    # Fallback: try last line of anchor
    anchor_lines = [l.strip() for l in anchor_xml.strip().split('\n') if l.strip()]
    if anchor_lines:
        last_line = anchor_lines[-1]
        pos = pxml_content.find(last_line)
        if pos != -1:
            insert_pos = pos + len(last_line)
            newline_pos = pxml_content.find('\n', insert_pos)
            if newline_pos != -1 and (newline_pos - insert_pos) < 5:
                insert_pos = newline_pos
            updated = pxml_content[:insert_pos] + '\n' + tagged_section_xml + pxml_content[insert_pos:]
            print(f"  📌 Inserted via last-line fallback at position {insert_pos}")
            return updated

    # Final fallback: find a unique text snippet from anchor and locate the </para> after it
    plain_from_anchor = re.sub(r'<[^>]+>', '', anchor_xml).strip()
    # Take the last 60 chars of plain text as search key
    if len(plain_from_anchor) > 30:
        search_snippet = plain_from_anchor[-60:]
        # Find this in the stripped-of-tags PXML content? No — find in raw PXML by
        # looking for words that must appear near each other
        words = search_snippet.split()
        if len(words) >= 4:
            # Search for last few significant words in the PXML
            last_words = ' '.join(words[-5:])
            # Escape for regex, allow flexible whitespace
            pattern = re.sub(r'\s+', r'\\s+', re.escape(last_words))
            match = re.search(pattern, pxml_content)
            if match:
                # Found the text — now find the next </para> after this position
                after_pos = match.end()
                para_close = pxml_content.find('</para>', after_pos)
                if para_close != -1:
                    insert_pos = para_close + len('</para>')
                    newline_pos = pxml_content.find('\n', insert_pos)
                    if newline_pos != -1 and (newline_pos - insert_pos) < 10:
                        insert_pos = newline_pos
                    updated = pxml_content[:insert_pos] + '\n' + tagged_section_xml + pxml_content[insert_pos:]
                    print(f"  📌 Inserted via text-snippet + </para> fallback at position {insert_pos}")
                    return updated

    return None


def handle_section_split(
    each_section,
    previous_section_xml,
    result_with_ba,
    sec_extractor,
    xml_context,
    updated_pxmlcontent,
    client,
    model_name=MODEL_OPUS,
    output_file=None,
):
    """
    Full handler for a section split:
    1. Find previous para (anchor text)
    2. LLM finds anchor in previous section XML
    3. Tag the new section content using Canada Pamphlets Prompt (via sec_extractor)
    4. Insert at anchor point in PXML
    
    Returns:
        tuple: (updated_pxml_content, success_bool)
    """
    from full_section_insertion_prevendor import PXMLInserter

    start_pi = int(each_section['start_paragraph_index'])
    print(f"\n  🔀 Handling section split for '{each_section['section_header']}'")

    # Step 1: Find previous paragraph (anchor text)
    prev_idx, prev_text = find_previous_para(result_with_ba, start_pi)
    if not prev_text:
        print(f"  ⚠️  Could not find previous paragraph before idx={start_pi}")
        return updated_pxmlcontent, False
    print(f"  📄 Previous para (idx={prev_idx}): '{prev_text[:120]}'")

    # Step 2: Find anchor in previous section XML using LLM (claude-opus-4-5)
    print(f"  🔍 Finding anchor in previous section XML...")
    anchor_xml = find_anchor_in_xml(prev_text, previous_section_xml, client, model_name)
    if not anchor_xml:
        print(f"  ⚠️  LLM could not find anchor in previous section XML")
        return updated_pxmlcontent, False
    print(f"  🎯 Anchor found ({len(anchor_xml)} chars):\n     '{anchor_xml[:300]}'")

    # Verify anchor exists in the full PXML
    anchor_in_pxml = anchor_xml in updated_pxmlcontent
    norm_anchor = re.sub(r'\s+', ' ', anchor_xml.strip())
    norm_check = norm_anchor in re.sub(r'\s+', ' ', updated_pxmlcontent)
    print(f"  📋 Anchor in PXML: exact={anchor_in_pxml}, normalized={norm_check}")
    if not anchor_in_pxml and not norm_check:
        # Show what we're looking for to help debug
        print(f"  🔎 Anchor (first 150 repr): {repr(anchor_xml[:150])}")

    # Step 3: Tag the new section content using Canada Pamphlets Prompt
    plaintext = each_section['full_text']
    bullet_points = each_section.get('bullet_points', [])
    section_header = each_section['section_header']
    print(f"  📝 Tagging section content ({len(plaintext)} chars) with Canada Pamphlets Prompt...")

    tagged_xml = sec_extractor.convert_text_to_xml_tags(
        xml_context,
        plaintext,
        bullet_points=bullet_points,
        section_header=section_header,
    )
    cleanedup_xml, designator = sec_extractor.cleanup_xml_fences(
        tagged_xml, fallback_designator=section_header
    )
    print(f"  🏷️  Tagged XML ({len(cleanedup_xml)} chars), designator='{designator}'")
    print(f"     First 300 chars: '{cleanedup_xml[:300]}'")

    # Step 4: For section split, we need to:
    #   - Close the previous section (</section.body></section>)
    #   - Insert the new section WITHOUT its closing </section.body></section>
    #     because the remaining content below already belongs to the new section
    #     and has closing tags already present in the PXML.
    
    # Remove trailing </section.body></section> from tagged XML
    split_xml = cleanedup_xml.rstrip()
    # Remove </section> at end
    if split_xml.endswith('</section>'):
        split_xml = split_xml[:-len('</section>')].rstrip()
    # Remove </section.body> at end
    if split_xml.endswith('</section.body>'):
        split_xml = split_xml[:-len('</section.body>')].rstrip()

    # Prepend the closing of the previous section — but only if not already present after anchor
    # Check if </section.body> already follows the anchor in the PXML
    anchor_pos_in_pxml = updated_pxmlcontent.find(anchor_xml)
    if anchor_pos_in_pxml == -1:
        # Try normalized
        anchor_pos_in_pxml = re.sub(r'\s+', ' ', updated_pxmlcontent).find(re.sub(r'\s+', ' ', anchor_xml.strip()))
    
    already_has_closing = False
    if anchor_pos_in_pxml != -1:
        after_anchor = updated_pxmlcontent[anchor_pos_in_pxml + len(anchor_xml):anchor_pos_in_pxml + len(anchor_xml) + 200].lstrip()
        if after_anchor.startswith('</section.body>') or after_anchor.startswith('</section>'):
            already_has_closing = True
            print(f"  ℹ️  Previous section already has closing tags after anchor — skipping </section.body></section> prepend")

    if already_has_closing:
        insertion_block = split_xml
    else:
        insertion_block = '</section.body>\n\t\t\t\t\t</section>\n' + split_xml
    print(f"  📌 Insertion block: {'opens new section only' if already_has_closing else 'closes prev section + opens new section'} (no closing tags)")
    print(f"     Last 200 chars: '{insertion_block[-200:]}'")

    # Step 5: Insert at anchor point in PXML
    print(f"  📌 Inserting after anchor...")
    result_pxml = insert_section_at_anchor(updated_pxmlcontent, anchor_xml, insertion_block)
    if result_pxml:
        print(f"  ✅ Section split insertion successful — inserted after anchor in PXML")
        return result_pxml, True
    else:
        print(f"  ⚠️  Could not find anchor in full PXML — trying designator-based insertion")
        # Fallback: use designator-based insertion (with full tagged XML since we can't split)
        if designator:
            designator_clean = designator.replace('§', '').strip()
            inserter = PXMLInserter(output_file, updated_pxmlcontent)
            result_pxml, desig_num = inserter.insert_xml_by_designator(
                designator_clean, cleanedup_xml, write_to_file=False
            )
            if result_pxml is not None:
                print(f"  ✅ Fallback designator insertion successful for '{designator_clean}'")
                return result_pxml, True

        print(f"  ❌ Section split insertion FAILED — could not insert")
        return updated_pxmlcontent, False
