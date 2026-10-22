"""
Paragraph Split Handler (Pre-processing)
==========================================
Detects and handles cases where multiple consecutive rows in the DataFrame
have their text_before values inside the SAME <para> element in the prevendor XML.

This means the author split one paragraph into multiple paragraphs in the DOCX.
We must split the <para> in the XML BEFORE applying trace tags, so each row
gets its own clean <para> to work with.

Called from within process_with_llm() before the main LLM loop.
"""

import re
from difflib import SequenceMatcher


def _strip_tags(text):
    """Remove XML tags for plain text comparison."""
    if not text:
        return ""
    return re.sub(r'<[^>]+>', '', str(text)).strip()


def _normalize_ws(text):
    """Collapse whitespace for matching."""
    return re.sub(r'\s+', ' ', text).strip() if text else ""


def detect_shared_paragraph_groups(collapsed_items, section_dict, parse_sections_fn, pxml_content):
    """
    Detect groups of consecutive collapsed items that share the same <para> element.
    
    For each group, we know the original single <para> needs to be split into
    N separate <para> elements.
    
    Args:
        collapsed_items: list of dicts from collapse_changes(), each has:
            idx, text_before, text_after, section, changes, etc.
        section_dict: current section_dict
        parse_sections_fn: function to re-parse sections (parse_pxml_sections_from_str_new)
        pxml_content: current pxml content
        
    Returns:
        List of "split groups": each is a list of consecutive items that share a paragraph.
        Items NOT part of any split are returned as single-item groups.
    """
    if not collapsed_items:
        return []
    
    groups = []
    current_group = [collapsed_items[0]]
    
    for i in range(1, len(collapsed_items)):
        prev_item = collapsed_items[i - 1]
        curr_item = collapsed_items[i]
        
        # Only check items in the same section
        if prev_item.get("section") != curr_item.get("section"):
            groups.append(current_group)
            current_group = [curr_item]
            continue
        
        # Check if both text_before values are in the same <para> element
        section_key = curr_item.get("section", "")
        if not section_key:
            groups.append(current_group)
            current_group = [curr_item]
            continue
            
        section_xml = section_dict.get(section_key, "")
        if not section_xml:
            # Try to find it via the dict keys
            for k, v in section_dict.items():
                if section_key in k or k in section_key:
                    section_xml = v
                    break
        
        if not section_xml:
            groups.append(current_group)
            current_group = [curr_item]
            continue
        
        prev_text = _normalize_ws(_strip_tags(prev_item.get("text_before", "")))
        curr_text = _normalize_ws(_strip_tags(curr_item.get("text_before", "")))
        
        if not prev_text or not curr_text or len(prev_text) < 20 or len(curr_text) < 20:
            groups.append(current_group)
            current_group = [curr_item]
            continue
        
        # Find all <para> elements in this section and check if both texts are in the same one
        shared = _texts_share_para(prev_text, curr_text, section_xml)
        
        if shared:
            current_group.append(curr_item)
        else:
            groups.append(current_group)
            current_group = [curr_item]
    
    groups.append(current_group)
    return groups


def _texts_share_para(text_a, text_b, section_xml):
    """
    Check if text_a and text_b are both found in the same <para> element.
    """
    # Extract all <para>...</para> blocks
    para_pattern = re.compile(r'<para\b[^>]*>.*?</para>', re.DOTALL)
    
    for para_match in para_pattern.finditer(section_xml):
        para_xml = para_match.group(0)
        para_plain = _normalize_ws(_strip_tags(para_xml))
        
        # Check if both texts' first 50 chars appear in this paragraph
        anchor_a = text_a[:60]
        anchor_b = text_b[:60]
        
        if anchor_a in para_plain and anchor_b in para_plain:
            return True
    
    return False


def split_shared_paragraphs(split_groups, pxml_content, section_dict):
    """
    For each group with more than one item (shared paragraph), split the
    <para> in the XML into separate <para> elements.
    
    Args:
        split_groups: list of groups from detect_shared_paragraph_groups()
        pxml_content: current pxml content
        section_dict: current section_dict
        
    Returns:
        Updated pxml_content with paragraphs split
    """
    for group in split_groups:
        if len(group) <= 1:
            continue  # No split needed
        
        print(f"\n  [PARA SPLIT] Found {len(group)} rows sharing same paragraph:")
        for item in group:
            tb = _strip_tags(item.get('text_before', ''))
            print(f"    idx={item['idx']}: text_before={tb[:80]}...")
        
        # Find the shared paragraph in the XML
        section_key = group[0].get("section", "")
        section_xml = section_dict.get(section_key, "")
        if not section_xml:
            for k, v in section_dict.items():
                if section_key in k or k in section_key:
                    section_xml = v
                    break
        
        if not section_xml:
            print(f"    [SKIP] Could not find section XML for '{section_key}'")
            continue
        
        # Find the specific <para> that contains all text_before values
        text_befores = [_normalize_ws(_strip_tags(item.get("text_before", ""))) for item in group]
        
        para_pattern = re.compile(r'<para\b[^>]*>.*?</para>', re.DOTALL)
        target_para = None
        target_para_match = None
        
        for para_match in para_pattern.finditer(section_xml):
            para_xml = para_match.group(0)
            para_plain = _normalize_ws(_strip_tags(para_xml))
            
            # Check if ALL text_befores have their first 50 chars in this paragraph
            all_found = all(
                tb[:50] in para_plain 
                for tb in text_befores 
                if len(tb) >= 20
            )
            if all_found:
                target_para = para_xml
                target_para_match = para_match
                break
        
        if not target_para:
            print(f"    [SKIP] Could not find shared <para> element")
            continue
        
        print(f"    [FOUND] Shared paragraph ({len(target_para)} chars)")
        
        # Split the paragraph into N parts
        split_result = _split_para_by_text_befores(target_para, text_befores)
        
        if not split_result:
            print(f"    [SKIP] Could not determine split points")
            continue
        
        print(f"    [SPLIT] Paragraph split into {len(split_result)} parts")
        for i, part in enumerate(split_result):
            print(f"      Part {i+1}: {_strip_tags(part)[:60]}...")
        
        # Replace in pxml_content
        replacement = "\n".join(split_result)
        if target_para in pxml_content:
            pxml_content = pxml_content.replace(target_para, replacement, 1)
            print(f"    [SUCCESS] Paragraph split applied in PXML")
        else:
            # Try whitespace-flexible match
            pxml_content = _flexible_replace(pxml_content, target_para, replacement)
    
    return pxml_content


def _split_para_by_text_befores(para_xml, text_befores):
    """
    Split a single <para> element into N <para> elements, one per text_before.
    
    Strategy: Find where each text_before starts in the paragraph's plain text,
    then map those positions back to the XML to find split points.
    """
    if len(text_befores) < 2:
        return None
    
    para_plain = _normalize_ws(_strip_tags(para_xml))
    
    # Find the start position of each text_before in the plain text
    positions = []
    for tb in text_befores:
        if not tb or len(tb) < 10:
            return None
        # Use first 50 chars as anchor
        anchor = tb[:50]
        pos = para_plain.find(anchor)
        if pos == -1:
            # Try shorter anchor
            anchor = tb[:30]
            pos = para_plain.find(anchor)
        if pos == -1:
            print(f"    [WARN] Could not find text_before anchor in paragraph: '{anchor}'")
            return None
        positions.append(pos)
    
    # Ensure positions are in order
    if positions != sorted(positions):
        print(f"    [WARN] text_before positions are not in order: {positions}")
        return None
    
    # Now we need to find split points in the actual XML
    # Extract the <para.text> content and find where to split
    para_text_match = re.search(
        r'(<para\b[^>]*>\s*<para\.text\b[^>]*>)(.*?)(</para\.text>\s*</para>)',
        para_xml, re.DOTALL
    )
    
    if not para_text_match:
        print(f"    [WARN] Could not parse <para><para.text> structure")
        return None
    
    para_open = para_text_match.group(1)   # <para ...><para.text ...>
    inner_content = para_text_match.group(2)  # content between para.text tags
    para_close = para_text_match.group(3)   # </para.text></para>
    
    # Extract the <para ...> and <para.text ...> opening tags separately
    para_tag_match = re.match(r'(<para\b[^>]*>)\s*(<para\.text\b[^>]*>)', para_open, re.DOTALL)
    if not para_tag_match:
        return None
    
    para_tag = para_tag_match.group(1)        # <para date.updated="0">
    para_text_tag = para_tag_match.group(2)   # <para.text date.updated="0">
    
    # Find split points in inner_content by mapping plain text positions to XML positions
    xml_split_positions = _find_xml_split_positions(inner_content, text_befores)
    
    if not xml_split_positions or len(xml_split_positions) != len(text_befores) - 1:
        print(f"    [WARN] Could not map split positions to XML (got {len(xml_split_positions) if xml_split_positions else 0}, need {len(text_befores)-1})")
        return None
    
    # Split inner_content at the determined positions
    parts = []
    prev_pos = 0
    for split_pos in xml_split_positions:
        parts.append(inner_content[prev_pos:split_pos].strip())
        prev_pos = split_pos
    parts.append(inner_content[prev_pos:].strip())
    
    # Wrap each part in <para><para.text>...</para.text></para>
    result = []
    for part in parts:
        wrapped = f"{para_tag}\n\t\t\t\t\t\t{para_text_tag}{part}</para.text>\n\t\t\t\t\t</para>"
        result.append(wrapped)
    
    return result


def _find_xml_split_positions(inner_content, text_befores):
    """
    Find positions in inner_content (XML with tags) where we should split,
    based on where each text_before starts in the plain text.
    
    For N text_befores, we need N-1 split positions.
    """
    # Build a mapping from plain-text position to XML position
    # Walk through inner_content tracking both plain-text and XML positions
    plain_pos = 0
    xml_pos = 0
    in_tag = False
    
    # Map: plain_text_index -> xml_index (only for text chars outside tags)
    plain_to_xml = {}
    
    for i, ch in enumerate(inner_content):
        if ch == '<':
            in_tag = True
        elif ch == '>':
            in_tag = False
            xml_pos = i + 1
            continue
        
        if not in_tag:
            plain_to_xml[plain_pos] = i
            plain_pos += 1
        
        xml_pos = i + 1
    
    # Now find where each text_before (except the first) starts in the plain text
    inner_plain = _strip_tags(inner_content)
    inner_plain_normalized = _normalize_ws(inner_plain)
    
    split_positions = []
    for tb in text_befores[1:]:  # Skip the first one (it starts at position 0)
        anchor = _normalize_ws(tb[:50])
        
        # Find in normalized plain text
        pos_in_normalized = inner_plain_normalized.find(anchor)
        if pos_in_normalized == -1:
            anchor = _normalize_ws(tb[:30])
            pos_in_normalized = inner_plain_normalized.find(anchor)
        if pos_in_normalized == -1:
            return None
        
        # Map back to XML position — find the corresponding position in inner_content
        # We need to walk the inner_content and count non-tag chars to find the right spot
        xml_split_pos = _map_plain_pos_to_xml_pos(inner_content, pos_in_normalized)
        if xml_split_pos is None:
            return None
        
        split_positions.append(xml_split_pos)
    
    return split_positions


def _map_plain_pos_to_xml_pos(xml_content, target_plain_pos):
    """
    Given a position in the normalized plain text, find the corresponding
    position in the XML content where we should split.
    
    We want to split BEFORE the character at target_plain_pos.
    Also ensure we don't split inside a tag.
    """
    plain_count = 0
    in_tag = False
    whitespace_run = False
    
    for i, ch in enumerate(xml_content):
        if ch == '<':
            in_tag = True
            continue
        if ch == '>':
            in_tag = False
            continue
        
        if in_tag:
            continue
        
        # We're in text content
        # Handle whitespace normalization: multiple whitespace chars = 1 plain char
        if ch in ' \t\n\r':
            if not whitespace_run:
                whitespace_run = True
                if plain_count == target_plain_pos:
                    # Find the start of this text run (backtrack past whitespace)
                    return _backtrack_to_safe_split(xml_content, i)
                plain_count += 1
        else:
            whitespace_run = False
            if plain_count == target_plain_pos:
                return _backtrack_to_safe_split(xml_content, i)
            plain_count += 1
    
    return None


def _backtrack_to_safe_split(xml_content, pos):
    """
    From position pos, backtrack to find a safe split point.
    A safe split point is:
    - Not inside an XML tag
    - Preferably at a whitespace boundary between tags
    - Before the text content at pos
    """
    # Look backwards for whitespace that's not inside a tag
    # The split should happen just before the text at 'pos'
    # Walk back to find the last '>' before pos (end of a tag) or start of whitespace
    i = pos - 1
    while i >= 0:
        if xml_content[i] == '>':
            # Split right after this closing tag bracket
            return i + 1
        elif xml_content[i] in ' \t\n\r':
            i -= 1
        else:
            # We hit text content — split here (before the current text)
            # Actually, let's split at the whitespace between prev text and current
            return i + 1
    return pos


def _flexible_replace(pxml_content, old_text, new_text):
    """Replace with flexible whitespace matching."""
    # Collapse whitespace in old_text to build a regex
    escaped = re.escape(old_text.strip())
    pattern = re.sub(r'\\\s+', r'\\s+', escaped)
    
    match = re.search(pattern, pxml_content, re.DOTALL)
    if match:
        pxml_content = pxml_content[:match.start()] + new_text + pxml_content[match.end():]
        print(f"    [SUCCESS] Paragraph split applied (flexible match)")
    else:
        print(f"    [FAIL] Could not find paragraph in PXML even with flexible matching")
    
    return pxml_content


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC API — called from process_with_llm before the main loop
# ──────────────────────────────────────────────────────────────────────────────

def preprocess_paragraph_splits(collapsed_items, pxml_content, section_dict, parse_sections_fn=None):
    """
    Detect and split shared paragraphs BEFORE the main LLM loop.
    
    Args:
        collapsed_items: list of collapsed change dicts
        pxml_content: current PXML content
        section_dict: current section dictionary
        parse_sections_fn: optional function to re-parse sections
        
    Returns:
        Tuple of (updated_pxml_content, split_indices)
        - updated_pxml_content: PXML with shared paragraphs split
        - split_indices: list of idx values involved in paragraph splits
    """
    print(f"\n{'='*60}")
    print(f"[PARA SPLIT PRE-PROCESSING] Checking {len(collapsed_items)} items for shared paragraphs...")
    print(f"{'='*60}")
    
    # Detect groups
    groups = detect_shared_paragraph_groups(
        collapsed_items, section_dict, parse_sections_fn, pxml_content
    )
    
    # Count how many groups have more than 1 item
    split_needed = [g for g in groups if len(g) > 1]
    
    if not split_needed:
        print(f"[PARA SPLIT] No shared paragraphs detected. Continuing normally.")
        return pxml_content, []
    
    print(f"[PARA SPLIT] Found {len(split_needed)} paragraph(s) that need splitting.")
    
    # Collect all indices involved in splits
    split_indices = []
    for group in split_needed:
        for item in group:
            split_indices.append(item['idx'])
    
    # Apply splits
    pxml_content = split_shared_paragraphs(groups, pxml_content, section_dict)
    
    print(f"[PARA SPLIT] Pre-processing complete. Affected indices: {split_indices}\n")
    return pxml_content, split_indices
