"""
Shared PXML section parsing utilities.
All pipeline modules should import from here instead of defining their own parsers.
"""

import re


def parse_pxml_sections(pxml_content, chapter_number=None, track_duplicates=False):
    """
    Parse PXML content and extract sections mapped by designator.
    Handles both regular and analytical.level file structures.

    Parameters:
    -----------
    pxml_content : str
        Full PXML file content
    chapter_number : str or None
        If provided, also extracts chapter preamble
    track_duplicates : bool
        If True, duplicate designators get __dup1, __dup2 suffixes instead of being overwritten

    Returns:
    --------
    tuple: (section_dict, section_positions, pxml_content)
        section_dict: {designator: section_xml_string}
        section_positions: {designator: (start_pos, end_pos)}
        pxml_content: original content (pass-through for compatibility)
    """
    section_dict = {}
    section_positions = {}

    has_analytical = '<analytical.level' in pxml_content

    if has_analytical:
        section_dict, section_positions = _parse_sections_stack_based(
            pxml_content, track_duplicates=track_duplicates
        )
    else:
        # Try standard <section> tags
        pattern = r'<section\s+(?:uuid="[^"]+"\s+)?[^>]*>.*?</section>'
        section_matches = list(re.finditer(pattern, pxml_content, re.DOTALL))

        # If no standard sections found, try <editorial.commentary.section> tags
        if not section_matches:
            pattern = r'<editorial\.commentary\.section(?=[\s>])[^>]*>.*?</editorial\.commentary\.section>'
            section_matches = list(re.finditer(pattern, pxml_content, re.DOTALL))

        for match in section_matches:
            section_xml = match.group(0)
            start_pos = match.start()
            end_pos = match.end()

            designator_match = re.search(
                r'<designator[^>]*>([\d:.A-Za-z]+)</designator>', section_xml
            )
            if designator_match:
                designator = designator_match.group(1)
                _store_designator(
                    section_dict, section_positions,
                    designator, section_xml, start_pos, end_pos,
                    track_duplicates
                )

    # Parse appendixes inside <chapter.back> and key them by their designator
    # (e.g. "10A", "10B"). This mirrors how appendix headings ("Appendix 10A")
    # are detected on the docx side, so the keys line up.
    _parse_chapter_back_appendices(
        pxml_content, section_dict, section_positions, track_duplicates
    )

    # Add chapter preamble if requested
    if chapter_number and section_dict:
        try:
            from canada_pipeline.paragraph_level_trackchanges import extract_chapter_preamble
            section_dict = extract_chapter_preamble(pxml_content, chapter_number, section_dict)
        except (ImportError, Exception):
            pass

    return section_dict, section_positions, pxml_content


def _parse_chapter_back_appendices(pxml_content, section_dict, section_positions,
                                   track_duplicates=False):
    """
    Find <chapter.back> ... </chapter.back> and extract each top-level
    <appendix> ... </appendix> block inside it. Each appendix is stored in
    section_dict keyed by its designator read from:
        <appendix.front> -> <outline.name.block> -> <designator>10A</designator>

    Nested designators inside the appendix body (e.g. "1)", "A") are ignored —
    only the appendix.front designator is used as the key.
    """
    back_match = re.search(
        r'<chapter\.back\b[^>]*>.*?</chapter\.back>', pxml_content, re.DOTALL
    )
    if not back_match:
        return

    back_start = back_match.start()

    # Walk each top-level <appendix ...> ... </appendix> using depth matching
    # to correctly handle any nested <appendix.*> containers.
    pos = back_start
    back_end = back_match.end()
    while pos < back_end:
        app_start = pxml_content.find('<appendix', pos)
        if app_start == -1 or app_start >= back_end:
            break

        tag_end = pxml_content.find('>', app_start)
        if tag_end == -1:
            break

        # Verify it's a real <appendix> tag (not <appendix.block>, <appendix.front> etc.)
        check_pos = app_start + len('<appendix')
        if check_pos < len(pxml_content) and pxml_content[check_pos] not in (' ', '>', '\n', '\t', '\r'):
            pos = tag_end + 1
            continue

        # Depth-based matching for the closing </appendix>
        depth = 1
        i = tag_end + 1
        section_end = -1
        while i < len(pxml_content) and depth > 0:
            next_open = pxml_content.find('<appendix', i)
            next_close = pxml_content.find('</appendix>', i)

            if next_close == -1:
                break

            if next_open != -1 and next_open < next_close:
                cp = next_open + len('<appendix')
                if cp < len(pxml_content) and pxml_content[cp] in (' ', '>', '\n', '\t', '\r'):
                    depth += 1
                i = next_open + len('<appendix')
            else:
                depth -= 1
                if depth == 0:
                    section_end = next_close + len('</appendix>')
                i = next_close + len('</appendix>')

        if section_end == -1:
            pos = tag_end + 1
            continue

        appendix_xml = pxml_content[app_start:section_end]

        # Designator from the appendix.front outline.name.block
        front_match = re.search(
            r'<appendix\.front\b[^>]*>.*?</appendix\.front>',
            appendix_xml, re.DOTALL
        )
        search_scope = front_match.group(0) if front_match else appendix_xml
        designator_match = re.search(
            r'<designator[^>]*>([^<]+)</designator>', search_scope
        )
        if designator_match:
            designator = designator_match.group(1).strip()
            _store_designator(
                section_dict, section_positions,
                designator, appendix_xml, app_start, section_end,
                track_duplicates
            )

        pos = section_end


def parse_pxml_sections_dict_only(pxml_content, chapter_number=None):
    """
    Convenience wrapper that returns only section_dict (no positions).
    Used by paragraph_level_trackchanges and related modules.
    """
    section_dict, _, _ = parse_pxml_sections(pxml_content, chapter_number=chapter_number)
    return section_dict


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _store_designator(section_dict, section_positions, designator, section_xml,
                      start_pos, end_pos, track_duplicates):
    """Store a designator, handling duplicates if configured."""
    if track_duplicates and designator in section_dict:
        dup_idx = 1
        while f"{designator}__dup{dup_idx}" in section_dict:
            dup_idx += 1
        dup_key = f"{designator}__dup{dup_idx}"
        section_dict[dup_key] = section_xml
        section_positions[dup_key] = (start_pos, end_pos)
        print(f"[WARN] Duplicate designator '{designator}' found — stored as '{dup_key}'")
    else:
        section_dict[designator] = section_xml
        section_positions[designator] = (start_pos, end_pos)


def _parse_sections_stack_based(pxml_content, track_duplicates=False):
    """
    Stack-based section parser for files with nested <section> tags
    (e.g., inside analytical.level structures).

    Finds <section.block> containers, then walks each top-level <section>
    using a depth counter to correctly match opening/closing tags.
    """
    section_dict = {}
    section_positions = {}

    block_pattern = r'<section\.block[^>]*>'
    for block_match in re.finditer(block_pattern, pxml_content):
        block_start = block_match.end()
        block_end = pxml_content.find('</section.block>', block_start)
        if block_end == -1:
            block_end = len(pxml_content)

        pos = block_start
        while pos < block_end:
            section_start = pxml_content.find('<section', pos)
            if section_start == -1 or section_start >= block_end:
                break

            # Verify it's a real <section> tag (not <section.block, <section.front etc.)
            tag_end = pxml_content.find('>', section_start)
            if tag_end == -1:
                break
            check_pos = section_start + 8  # len('<section')
            if check_pos < len(pxml_content) and pxml_content[check_pos] not in (' ', '>', '\n', '\t', '\r'):
                pos = tag_end + 1
                continue

            # Stack-based matching for </section>
            depth = 1
            i = tag_end + 1
            found_end = False
            while i < len(pxml_content) and depth > 0:
                next_open = pxml_content.find('<section', i)
                next_close = pxml_content.find('</section>', i)

                if next_close == -1:
                    break

                if next_open != -1 and next_open < next_close:
                    cp = next_open + 8
                    if cp < len(pxml_content) and pxml_content[cp] in (' ', '>', '\n', '\t', '\r'):
                        depth += 1
                    i = next_open + 8
                else:
                    depth -= 1
                    if depth == 0:
                        section_end = next_close + len('</section>')
                        section_xml = pxml_content[section_start:section_end]

                        designator_match = re.search(
                            r'<designator[^>]*>([\d:.A-Za-z]+)</designator>', section_xml
                        )
                        if designator_match:
                            designator = designator_match.group(1)
                            _store_designator(
                                section_dict, section_positions,
                                designator, section_xml, section_start, section_end,
                                track_duplicates
                            )

                        pos = section_end
                        found_end = True
                        break
                    i = next_close + 10

            if not found_end:
                pos = tag_end + 1

    return section_dict, section_positions
