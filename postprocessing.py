import re
from footnote_cleanup import fix_zero_reference_footnotes


# def collapse_empty_deleted_elements(xml_content):
#     """
#     Dynamically and recursively collapse any XML element whose only meaningful
#     content is <trace.deleted/> markers, whitespace, and/or processing instructions.
    
#     Works bottom-up: first collapses innermost empty elements, then their parents
#     become empty too, and so on until no more can be collapsed.
    
#     Elements that are PRESERVED (never collapsed):
#     - Elements with real text content
#     - Elements with child elements that have real text content
#     - <footnote>, <footnote.body>, <footnote.reference> (footnotes are special)
#     - <section>, <chapter> (structural containers)
#     - Self-closing tags like <trace.deleted/>
    
#     An element is "empty" if after removing all <trace.deleted/> and <?...?> PIs
#     and whitespace, there's nothing left (no text, no non-empty children).
#     """
#     # Tags that should never be collapsed even if empty
#     PRESERVE_TAGS = {
#         'footnote', 'footnote.body', 'footnote.reference',
#         'section', 'chapter', 'document', 'WGXPPinfo',
#     }
    
#     # Pattern matches: <tag attrs>CONTENT</tag> where tag is NOT self-closing
#     # We process iteratively, collapsing from innermost outward
#     element_pattern = re.compile(
#         r'<(?P<tag>[a-zA-Z][a-zA-Z0-9._-]*)\b(?P<attrs>[^>]*)>'  # opening tag
#         r'(?P<content>(?:(?!<(?P=tag)\b).)*?)'                     # non-greedy content (no nested same-tag)
#         r'</(?P=tag)>',                                             # closing tag
#         re.DOTALL
#     )
    
#     def is_empty_content(content):
#         """Check if content only contains trace.deleted, PIs, and whitespace."""
#         # Remove all <trace.deleted/> variants
#         cleaned = re.sub(r'<trace\.deleted\s*/>', '', content)
#         # Remove processing instructions <?...?>
#         cleaned = re.sub(r'<\?[^?]*\?>', '', cleaned)
#         # Remove self-closing tags with no content significance (empty para.text etc)
#         cleaned = re.sub(r'<[a-zA-Z][a-zA-Z0-9._-]*\b[^>]*/>', '', cleaned)
#         # Check if anything meaningful remains
#         cleaned = cleaned.strip()
#         return len(cleaned) == 0
    
#     prev = None
#     iteration = 0
#     total_collapsed = 0
    
#     while prev != xml_content:
#         prev = xml_content
#         iteration += 1
#         collapsed_this_pass = 0
        
#         def replacer(m):
#             nonlocal collapsed_this_pass
#             tag = m.group('tag')
#             content = m.group('content')
            
#             # Never collapse preserved tags
#             if tag in PRESERVE_TAGS:
#                 return m.group(0)
            
#             # Check if content is effectively empty
#             if is_empty_content(content):
#                 collapsed_this_pass += 1
#                 return '<trace.deleted/>'
            
#             return m.group(0)
        
#         xml_content = element_pattern.sub(replacer, xml_content)
#         total_collapsed += collapsed_this_pass
        
#         if collapsed_this_pass > 0:
#             print(f"    Iteration {iteration}: collapsed {collapsed_this_pass} empty elements")
        
#         # Safety: limit iterations to prevent infinite loop
#         if iteration > 20:
#             print(f"    WARNING: Hit max iterations (20), stopping.")
#             break
    
#     # Final: consolidate consecutive <trace.deleted/> into a single one
#     xml_content = re.sub(r'(<trace\.deleted\s*/>)(\s*<trace\.deleted\s*/>)+', r'<trace.deleted/>', xml_content)
    
#     print(f"    Total collapsed: {total_collapsed} elements across {iteration-1} iterations")
#     return xml_content

from lxml import etree
import re
import copy

def collapse_empty_deleted_elements(xml_content):

    PRESERVE_TAGS = {
        'footnote', 'footnote.body', 'footnote.reference',
        'section', 'chapter', 'document', 'WGXPPinfo',
    }

    def element_is_empty(el):
        if el.text and el.text.strip():
            return False

        for child in el:
            if isinstance(child, etree._ProcessingInstruction):
                if child.tail and child.tail.strip():
                    return False
                continue

            # Only ignore trace.deleted if it is truly self-closing (no children, no text)
            if child.tag == 'trace.deleted':
                # Self-closing: no text, no children
                if (not child.text or not child.text.strip()) and len(child) == 0:
                    if child.tail and child.tail.strip():
                        return False
                    continue
                else:
                    # Has real content inside — treat as a real child, do NOT collapse
                    return False

            # Any other child element means NOT empty
            return False

        return True

    def collapse_tree(root):
        total_collapsed = 0
        changed = True
        iteration = 0

        while changed:
            changed = False
            iteration += 1

            all_elements = list(root.iter())
            all_elements.reverse()

            for el in all_elements:
                if el is root:
                    continue
                if isinstance(el, etree._ProcessingInstruction):
                    continue
                if el.tag in PRESERVE_TAGS:
                    continue
                if el.tag == 'trace.deleted':
                    continue

                if element_is_empty(el):
                    parent = el.getparent()
                    if parent is None:
                        continue

                    tail_text = el.tail
                    idx = list(parent).index(el)
                    parent.remove(el)

                    replacement = etree.Element('trace.deleted')
                    replacement.tail = tail_text
                    parent.insert(idx, replacement)

                    total_collapsed += 1
                    changed = True

            if changed:
                print(f"    Iteration {iteration}: made changes (running total: {total_collapsed})")

            if iteration > 50:
                print("    WARNING: Hit max iterations (50), stopping.")
                break

        return total_collapsed

    def consolidate_trace_deleted(parent):
        to_remove = []
        prev_was_deleted = False

        for child in list(parent):
            if isinstance(child, etree._ProcessingInstruction):
                prev_was_deleted = False
                continue

            if child.tag == 'trace.deleted':
                if prev_was_deleted:
                    to_remove.append(child)
                else:
                    prev_was_deleted = True
            else:
                prev_was_deleted = False
                consolidate_trace_deleted(child)

        for el in to_remove:
            el.getparent().remove(el)

    # --- Strip and save the XML declaration if present ---
    xml_declaration = ''
    declaration_match = re.match(r'^(<\?xml[^?]*\?>)\s*', xml_content)
    if declaration_match:
        xml_declaration = declaration_match.group(1)
        xml_content = xml_content[declaration_match.end():]

    # --- Wrap in a synthetic root to handle fragments ---
    wrapped = f'<__root__>{xml_content}</__root__>'

    try:
        # Use a parser that tolerates PIs and recovers from minor issues
        parser = etree.XMLParser(recover=True, remove_comments=False)
        root = etree.fromstring(wrapped.encode('utf-8'), parser)
    except etree.XMLSyntaxError as e:
        print(f"    ERROR: Could not parse XML: {e}")
        return xml_content

    # --- Collapse ---
    total = collapse_tree(root)
    print(f"    Total collapsed: {total} elements")

    # --- Consolidate consecutive trace.deleted ---
    consolidate_trace_deleted(root)

    # --- Serialize ---
    result = etree.tostring(root, encoding='unicode', xml_declaration=False)

    # Strip the wrapper tags
    result = re.sub(r'^<__root__>', '', result)
    result = re.sub(r'</__root__>$', '', result)

    # Re-attach the XML declaration if there was one
    if xml_declaration:
        result = xml_declaration + '\n' + result

    return result

def postprocessing_fortags(updated_pxmlcontent, product_type="pamphlet"):

    # Step 0-pre: Apply product-type date.updated values for new (traced) elements
    if product_type == "looseleaf":
        updated_pxmlcontent = updated_pxmlcontent.replace(
            'date.updated="0" date.trace="1"',
            'date.updated="1" date.trace="1"'
        )
        print('  [POSTPROCESS] Step 0-pre: Replaced date.updated="0" date.trace="1" → date.updated="1" date.trace="1" (looseleaf)')
    else:
        print('  [POSTPROCESS] Step 0-pre: Keeping date.updated="0" date.trace="1" as-is (pamphlet)')

    # Step 0-fn: Collapse <trace.deleted><footnote>...</footnote><trace.deleted/> → <trace.deleted/>
    # When an entire footnote (opening to closing) is wrapped in trace.deleted,
    # collapse it to a single <trace.deleted/> marker.
    pattern_fn_deleted = r'<trace\.deleted>\s*<footnote\b[^>]*>[\s\S]*?</footnote>\s*<trace\.deleted\s*/>'
    matches_fn_deleted = re.findall(pattern_fn_deleted, updated_pxmlcontent)
    print(f"  [POSTPROCESS] Step 0-fn: Found {len(matches_fn_deleted)} fully trace.deleted <footnote> elements to collapse.")
    updated_pxmlcontent = re.sub(pattern_fn_deleted, r'<trace.deleted/>', updated_pxmlcontent)

    # Step 0: Normalize </trace.deleted/> → <trace.deleted/>
    updated_pxmlcontent = updated_pxmlcontent.replace('</trace.deleted/>', '<trace.deleted/>')

    # Step 0C: Unwrap <trace.deleted> that wraps structural container elements,
    # BUT ONLY when the inner content already has its own <trace.deleted> tags.
    # If inner content has no trace tags, the outer wrapper is the ONLY deletion
    # marker — removing it would cause content loss.
    _STRUCTURAL_UNWRAP_TAGS = ['quote\\.block', 'quote\\.para', 'list']
    for _tag_pat in _STRUCTURAL_UNWRAP_TAGS:
        _tag_plain = _tag_pat.replace('\\.', '.')
        _unwrap_re = re.compile(
            r'<trace\.deleted>\s*(<' + _tag_pat + r'\b[^>]*>[\s\S]*?</' + _tag_plain + r'>)\s*<trace\.deleted\s*/>',
            re.DOTALL
        )
        def _safe_unwrap(m):
            inner = m.group(1)
            # Only unwrap if inner content already has its own trace.deleted tags
            if '<trace.deleted>' in inner or '<trace.deleted/>' in inner:
                return inner
            # Otherwise keep the outer wrapper — it's the only deletion marker
            return m.group(0)
        _before_0c = updated_pxmlcontent
        updated_pxmlcontent = _unwrap_re.sub(_safe_unwrap, updated_pxmlcontent)
        if updated_pxmlcontent != _before_0c:
            print(f"  [POSTPROCESS] Step 0C: Unwrapped redundant <trace.deleted> around <{_tag_plain}> (inner already has trace tags).")

   
    # Step 0B: Strip orphan closing tags that appear INSIDE <para> blocks.
    # These are closing tags with no matching opener — artifacts from upstream
    # PXML generation. We only strip them inside <para>...</para> to avoid
    # accidentally touching valid structure elsewhere.
    ORPHAN_TAGS_TO_STRIP = [
        'para.text',
        'list',
        'list.item',
    ]
    def strip_orphan_closers_in_para(content):
        def fix_para(m):
            para_inner = m.group(0)
            for tag in ORPHAN_TAGS_TO_STRIP:
                open_pat  = re.compile(rf'<{re.escape(tag)}\b[^>]*(?<!/)>')
                close_pat = re.compile(rf'</{re.escape(tag)}>')
                opens  = len(open_pat.findall(para_inner))
                closes = len(close_pat.findall(para_inner))
                if closes > opens:
                    excess = closes - opens
                    # Find all close tag positions
                    positions = [m2.start() for m2 in close_pat.finditer(para_inner)]
                    # Remove the LAST `excess` closing tags (orphans are always trailing)
                    to_remove = set(positions[-excess:])
                    result = []
                    i = 0
                    for m2 in close_pat.finditer(para_inner):
                        result.append(para_inner[i:m2.start()])
                        if m2.start() not in to_remove:
                            result.append(m2.group(0))
                        i = m2.end()
                    result.append(para_inner[i:])
                    para_inner = ''.join(result)
            return para_inner
        return re.sub(r'<para\b[^>]*>[\s\S]*?</para>', fix_para, content)

    print("  [POSTPROCESS] Step 0B: Stripping orphan closing tags inside <para> blocks...")
    before = updated_pxmlcontent
    updated_pxmlcontent = strip_orphan_closers_in_para(updated_pxmlcontent)
    if updated_pxmlcontent != before:
        print("    Orphan tags removed.")
    else:
        print("    No orphan tags found.")

    # Step 1: Collapse <trace.deleted>CONTENT<trace.deleted/> → <trace.deleted/>
    # Uses a negative lookahead so the match never jumps over another
    # <trace.deleted> opening tag — each pair is matched individually,
    # preventing the regex from swallowing content across elements.
    # Runs iteratively: after inner pairs collapse, outer pairs become matchable.
    # SAFETY: Only collapse if CONTENT has balanced XML tags (no orphan openers/closers).

    def _has_balanced_tags(content):
        """Check that content has balanced XML opening/closing tags."""
        # Find all opening tags (not self-closing)
        opens = re.findall(r'<([a-zA-Z][a-zA-Z0-9._-]*)\b[^>]*(?<!/)>', content)
        # Find all closing tags
        closes = re.findall(r'</([a-zA-Z][a-zA-Z0-9._-]*)\s*>', content)
        # Count each tag name
        from collections import Counter
        open_counts = Counter(opens)
        close_counts = Counter(closes)
        # Remove trace.deleted from consideration (it uses self-closing, not </trace.deleted>)
        open_counts.pop('trace.deleted', None)
        open_counts.pop('trace', None)
        close_counts.pop('trace.deleted', None)
        close_counts.pop('trace', None)
        return open_counts == close_counts

    def _safe_collapse(m):
        """Only collapse if inner content has balanced XML tags."""
        content = m.group(1)
        if _has_balanced_tags(content):
            return '<trace.deleted/>'
        return m.group(0)  # leave unchanged

    updated_content = updated_pxmlcontent
    step1_iter = 0
    while True:
        step1_iter += 1
        prev_content = updated_content
        updated_content = re.sub(
            r'<trace\.deleted>((?:(?!<trace\.deleted[ >/])[\s\S])*?)<trace\.deleted\s*/>',
            _safe_collapse,
            updated_content
        )
        if updated_content == prev_content:
            break
        print(f"  [POSTPROCESS] Step 1 (iter {step1_iter}): collapsed trace.deleted pairs")
        if step1_iter > 20:
            print("  [POSTPROCESS] Step 1: hit max iterations (20), stopping.")
            break

    # Step 1a: If a <cite>...</cite> block contains any <trace> or <trace.deleted> tags,
    # wrap the entire <cite>...</cite> in <trace>...</trace>.
    # The inner trace tags will be cleaned up by subsequent steps.
    def wrap_cite_with_trace(match):
        cite_text = match.group(0)
        if '<trace' in cite_text:
            return f'<trace>{cite_text}</trace>'
        return cite_text

    cite_with_trace_count = sum(1 for m in re.finditer(r'<cite\b[^>]*>.*?</cite>', updated_content, re.DOTALL) if '<trace' in m.group(0))
    updated_content = re.sub(
        r'<cite\b[^>]*>.*?</cite>',
        wrap_cite_with_trace,
        updated_content,
        flags=re.DOTALL
    )
    print(f"  [POSTPROCESS] Step 1a: Found {cite_with_trace_count} <cite> blocks with trace tags — wrapped in <trace>.")

    # Step 2: Remove any <trace.deleted/>, <trace>, </trace> that appear inside <cite ...>...</cite>
    def clean_cite_content(match):
        cite_text = match.group(0)
        cite_text = re.sub(r'<trace\.deleted\s*/>', '', cite_text)
        cite_text = re.sub(r'</?trace>', '', cite_text)
        # Remove <ital> and </ital> tags inside cite/ref blocks (preserve inner text)
        cite_text = re.sub(r'</?ital>', '', cite_text)
        return cite_text

    updated_content = re.sub(
        r'<cite\b[^>]*>.*?</cite>',
        clean_cite_content,
        updated_content,
        flags=re.DOTALL
    )

    # Step 3-pre: Collapse <para.text ...><trace.deleted/></para.text> → <trace.deleted/>
    pattern_paratext_only_trace = r'<para\.text\b[^>]*>\s*<trace\.deleted\s*/>\s*</para\.text>'
    matches_paratext = re.findall(pattern_paratext_only_trace, updated_content)
    print(f"  [POSTPROCESS] Step 3-pre: Found {len(matches_paratext)} <para.text> with only <trace.deleted/> to collapse.")
    updated_content = re.sub(pattern_paratext_only_trace, r'<trace.deleted/>', updated_content)

    # Step 3-post: Collapse <para> where ALL children are only <trace.deleted/>
    pattern_para_only_trace = r'<para\b[^>]*>(?:\s*<trace\.deleted\s*/>)+\s*</para>'
    matches_para = re.findall(pattern_para_only_trace, updated_content)
    print(f"  [POSTPROCESS] Step 3-post: Found {len(matches_para)} <para> with only <trace.deleted/> children to collapse.")
    updated_content = re.sub(pattern_para_only_trace, r'<trace.deleted/>', updated_content)

    # Step 3 (designator): Collapse <designator><trace.deleted/></designator> → <trace.deleted/>
    pattern_step2 = r'<designator[^>]*>\s*<trace\.deleted\s*/>\s*</designator>'
    matches = re.findall(pattern_step2, updated_content)
    print("Step 2 matches:", matches)
    result = re.sub(pattern_step2, r'<trace.deleted/>', updated_content)
   

    # Step 4 (name.block): Collapse <name.block><trace.deleted/></name.block> → <trace.deleted/>
    pattern_step4 = r'<name\.block[^>]*>\s*<trace\.deleted\s*/>\s*</name\.block>'
    matches = re.findall(pattern_step4, result)
    print("Step 4 matches:", matches)
    final = re.sub(pattern_step4, r'<trace.deleted/>', result)

    # Step 4b: Collapse orphan <trace.deleted> opening tags whose only remaining
    # content is <trace.deleted/> markers and whitespace. This happens when the LLM
    # wraps an element's children in <trace.deleted>...<trace.deleted/> and the inner
    # elements have already been collapsed to <trace.deleted/> by earlier steps.
    # Pattern: <trace.deleted> followed by one or more <trace.deleted/> (with whitespace).
    pattern_orphan_open = r'<trace\.deleted>(?:\s*<trace\.deleted\s*/>)+\s*'
    prev_orphan = None
    while prev_orphan != final:
        prev_orphan = final
        matches_orphan = re.findall(pattern_orphan_open, final)
        if matches_orphan:
            print(f"  [POSTPROCESS] Step 4b: Collapsing {len(matches_orphan)} orphan <trace.deleted> with only <trace.deleted/> content.")
        final = re.sub(pattern_orphan_open, r'<trace.deleted/>', final)

    # Step A: Collapse <list.item> where ALL children are <trace.deleted/>
    pattern_listitem = r'<list\.item[^>]*>(?:\s*<trace\.deleted\s*/>)+\s*</list\.item>'
    matches_listitem = re.findall(pattern_listitem, final)
    print(f"  [POSTPROCESS] Step A: Found {len(matches_listitem)} <list.item> with all <trace.deleted/> to collapse.")
    final = re.sub(pattern_listitem, r'<trace.deleted/>', final)

    # Step B: Collapse <list> where ALL children are <trace.deleted/>
    pattern_list = r'<list[^>]*>(?:\s*<trace\.deleted\s*/>)+\s*</list>'
    matches_list = re.findall(pattern_list, final)
    print(f"  [POSTPROCESS] Step B: Found {len(matches_list)} <list> with all <trace.deleted/> to collapse.")
    final = re.sub(pattern_list, r'<trace.deleted/>', final)

    # Step C: Collapse <quote.para> with <para.text><trace.deleted/></para.text> → <trace.deleted/>
    pattern_quotepara = r'<quote\.para[^>]*>\s*<para\.text[^>]*><trace\.deleted\s*/></para\.text>\s*</quote\.para>'
    matches_quotepara = re.findall(pattern_quotepara, final)
    print(f"  [POSTPROCESS] Step C: Found {len(matches_quotepara)} empty <quote.para> (with para.text) to collapse.")
    final = re.sub(pattern_quotepara, r'<trace.deleted/>', final)

    # Step C-pre: Collapse any remaining <para.text><trace.deleted/></para.text>
    # inside quote structures before the C1/D2 loop runs
    prev = None
    while prev != final:
        prev = final
        matches = re.findall(pattern_paratext_only_trace, final)
        if matches:
            print(f"  [POSTPROCESS] Step C-pre: Found {len(matches)} <para.text> with only <trace.deleted/> inside quote structures to collapse.")
        final = re.sub(pattern_paratext_only_trace, r'<trace.deleted/>', final)

    # Step C1/D2: Iteratively collapse <quote.para> and <quote.block> with only
    # <trace.deleted/> children (bare, no <para.text> wrapper).
    # Loop handles arbitrary nesting depth — runs until no more changes.
    pattern_quotepara_bare = r'<quote\.para\b[^>]*>(?:\s*<trace\.deleted\s*/>)+\s*</quote\.para>'
    pattern_quoteblock = r'<quote\.block\b[^>]*>(?:\s*<trace\.deleted\s*/>)+\s*</quote\.block>'
    
    prev = None
    iteration = 0
    while prev != final:
        prev = final
        iteration += 1
    
        matches_qp = re.findall(pattern_quotepara_bare, final)
        if matches_qp:
            print(f"  [POSTPROCESS] Step C1 (iter {iteration}): Found {len(matches_qp)} bare <quote.para> with only <trace.deleted/> to collapse.")
        final = re.sub(pattern_quotepara_bare, r'<trace.deleted/>', final)
    
        matches_qb = re.findall(pattern_quoteblock, final)
        if matches_qb:
            print(f"  [POSTPROCESS] Step D2 (iter {iteration}): Found {len(matches_qb)} <quote.block> with only <trace.deleted/> to collapse.")
        final = re.sub(pattern_quoteblock, r'<trace.deleted/>', final)

    # Post-quote cleanup: re-run para.text and para collapse in case quote
    # collapsing has left <para> or <para.text> with only <trace.deleted/> children
    prev = None
    iteration = 0
    while prev != final:
        prev = final
        iteration += 1
    
        matches_paratext = re.findall(pattern_paratext_only_trace, final)
        if matches_paratext:
            print(f"  [POSTPROCESS] Step 3-pre cleanup (iter {iteration}): Found {len(matches_paratext)} <para.text> with only <trace.deleted/>.")
        final = re.sub(pattern_paratext_only_trace, r'<trace.deleted/>', final)
    
        matches_para = re.findall(pattern_para_only_trace, final)
        if matches_para:
            print(f"  [POSTPROCESS] Step 3-post cleanup (iter {iteration}): Found {len(matches_para)} <para> with only <trace.deleted/>.")
        final = re.sub(pattern_para_only_trace, r'<trace.deleted/>', final)

    final = merge_consecutive_trace_lists(final)

    # Step D3: Merge adjacent <ital>...</ital> fragments into a single block.
    # This commonly occurs when LLM emits per-party italics in case names.
    # Example:
    #   <ital>Dermaspark ... </ital><ital>Pollogen ...</ital>
    # → <ital>Dermaspark ... Pollogen ...</ital>
    final = merge_adjacent_ital_tags(final)

    # Step E: Remove unnecessary <ital> </ital> tags (empty or whitespace-only)
    pattern_empty_ital = r'<ital>\s*</ital>'
    matches_empty_ital = re.findall(pattern_empty_ital, final)
    print(f"  [POSTPROCESS] Step E: Found {len(matches_empty_ital)} empty <ital> tags to remove.")
    final = re.sub(pattern_empty_ital, ' ', final)

    # Step F: Remove extra space between </trace> and <trace.deleted/>
    pattern_trace_space = r'(</trace>)\s+(<trace\.deleted\s*/>)'
    matches_trace_space = re.findall(pattern_trace_space, final)
    print(f"  [POSTPROCESS] Step F: Found {len(matches_trace_space)} spacing issues between </trace> and <trace.deleted/>.")
    final = re.sub(pattern_trace_space, r'</trace><trace.deleted/>', final)

    # Step F2: Remove extra space between </ital> and <trace.deleted/>
    pattern_ital_trace_space = r'(</ital>)\s+(<trace\.deleted\s*/>)'
    matches_ital_trace_space = re.findall(pattern_ital_trace_space, final)
    print(f"  [POSTPROCESS] Step F2: Found {len(matches_ital_trace_space)} spacing issues between </ital> and <trace.deleted/>.")
    final = re.sub(pattern_ital_trace_space, r'</ital><trace.deleted/>', final)

    # Step F3: Remove extra space between </footnote> and <trace>
    pattern_footnote_trace_space = r'(</footnote>)\s+(<trace>)'
    matches_footnote_trace_space = re.findall(pattern_footnote_trace_space, final)
    print(f"  [POSTPROCESS] Step F3: Found {len(matches_footnote_trace_space)} spacing issues between </footnote> and <trace>.")
    final = re.sub(pattern_footnote_trace_space, r'</footnote><trace>', final)

    # Step F4: Remove extra space between </ital> and <cite
    pattern_ital_cite_space = r'(</ital>)\s+(<cite\b)'
    matches_ital_cite_space = re.findall(pattern_ital_cite_space, final)
    print(f"  [POSTPROCESS] Step F4: Found {len(matches_ital_cite_space)} spacing issues between </ital> and <cite>.")
    final = re.sub(pattern_ital_cite_space, r'</ital><cite', final)

    # Step F5: Remove extra space between <trace.deleted/> and <trace>
    pattern_tracedel_trace_space = r'(<trace\.deleted\s*/>)\s+(<trace>)'
    matches_tracedel_trace_space = re.findall(pattern_tracedel_trace_space, final)
    print(f"  [POSTPROCESS] Step F5: Found {len(matches_tracedel_trace_space)} spacing issues between <trace.deleted/> and <trace>.")
    final = re.sub(pattern_tracedel_trace_space, r'<trace.deleted/><trace>', final)

    # Step F6: Remove extra space between </trace> and <trace.deleted> (opening, not self-closing)
    pattern_trace_tracedel_open_space = r'(</trace>)\s+(<trace\.deleted>)'
    matches_trace_tracedel_open = re.findall(pattern_trace_tracedel_open_space, final)
    print(f"  [POSTPROCESS] Step F6: Found {len(matches_trace_tracedel_open)} spacing issues between </trace> and <trace.deleted>.")
    final = re.sub(pattern_trace_tracedel_open_space, r'</trace><trace.deleted>', final)

    # Step G: Fix zero-reference footnotes paired with trace-deleted footnotes
    final = fix_zero_reference_footnotes(final)

    # Step H: Absorb <list date.trace="1"> into preceding <para date.trace="1">
    matches_step_h = re.findall(
        r'<para\b[^>]*date\.trace="1"[^>]*>[\s\S]*?</para>\s*<list\b[^>]*date\.trace="1"[^>]*>',
        final
    )
    print(f"  [POSTPROCESS] Step H: Found {len(matches_step_h)} <para date.trace='1'> immediately followed by <list date.trace='1'> to absorb.")
    final = absorb_trace_list_into_trace_para(final)

    # final=collapse_empty_deleted_elements(final)

    # Final cleanup: Collapse any remaining <para> with only <trace.deleted/> children
    pattern_para_only_trace = r'<para\b[^>]*>(?:\s*<trace\.deleted\s*/>)+\s*</para>'
    final = re.sub(pattern_para_only_trace, r'<trace.deleted/>', final)

    # Step H2: Collapse elements containing only <trace.deleted/> markers
    # Covers: <omit.para>, <quote.para>, <list>, <list.item>, <head>, <name.block>
    pattern_only_trace_extended = r'<(omit\.para|quote\.para|list|list\.item|head|name\.block)\b[^>]*>(?:\s*<trace\.deleted\s*/>)+\s*</\1>'
    _before_h2 = final
    final = re.sub(pattern_only_trace_extended, r'<trace.deleted/>', final)
    # Second pass to catch parent elements now empty after first collapse
    final = re.sub(pattern_only_trace_extended, r'<trace.deleted/>', final)
    _h2_count = _before_h2.count('<trace.deleted/>') - final.count('<trace.deleted/>')
    if _h2_count:
        print(f"  [POSTPROCESS] Step H2: Collapsed {abs(_h2_count)} empty elements (omit.para/quote.para/list/list.item/head/name.block) containing only <trace.deleted/>")

    # Step I: Remove <ital><trace.deleted/></ital> — ital tags containing only a trace.deleted marker
    before_step_i = final
    final = re.sub(r'<ital>\s*<trace\.deleted\s*/>\s*</ital>', '', final, flags=re.DOTALL)
    count_i = before_step_i.count('<ital>') - final.count('<ital>')
    print(f"  [POSTPROCESS] Step I: Removed {count_i} <ital><trace.deleted/></ital> instances.")

    # Step K: Collapse fully-deleted footnotes where both <footnote.reference> and
    # <footnote.body> contain only <trace.deleted/> (and optionally whitespace).
    # These are footnotes where the LLM deleted the content inside each child
    # but the structure remains. Collapse the entire <footnote> → <trace.deleted/>
    pattern_empty_footnote = (
        r'<footnote\b[^>]*>\s*'
        r'<footnote\.reference\b[^>]*>\s*<trace\.deleted\s*/>\s*</footnote\.reference>\s*'
        r'<footnote\.body\b[^>]*>\s*<trace\.deleted\s*/>\s*</footnote\.body>\s*'
        r'</footnote>'
    )
    matches_empty_fn = re.findall(pattern_empty_footnote, final)
    print(f"  [POSTPROCESS] Step K: Found {len(matches_empty_fn)} fully-deleted <footnote> elements to collapse.")
    final = re.sub(pattern_empty_footnote, r'<trace.deleted/>', final)

    # Step J: REMOVED — empty element collapsing is now handled at the LLM level
    # in paragraph_level_trackchanges.py (Rule 13). The LLM collapses the entire
    # element hierarchy to <trace.deleted/> when all content is deleted, avoiding
    # content loss from regex-based recursive collapsing.

    return final


def absorb_trace_list_into_trace_para(xml_content):
    pattern = re.compile(
        r'(<para\b[^>]*date\.trace="1"[^>]*>)'
        r'(\s*)'
        r'(<para\.text\b[^>]*>)'
        r'((?:(?!</para\.text>)[\s\S])*?)'
        r'(</para\.text>)'
        r'(\s*)'
        r'(</para>)'
        r'(\s*)'
        r'(<list\b[^>]*date\.trace="1"[^>]*>)'
        r'([\s\S]*?)'
        r'(</list>)',
        re.DOTALL
    )

    def replacer(m):
        para_open      = m.group(1)
        ws1            = m.group(2)
        paratext_open  = m.group(3)
        paratext_body  = m.group(4)
        paratext_close = m.group(5)
        ws2            = m.group(6)
        para_close     = m.group(7)
        list_open      = m.group(9)
        list_body      = m.group(10)
        list_close     = m.group(11)

        indent = ws1 if ws1 else '\n                                '

        return (
            f"{para_open}"
            f"{ws1}"
            f"{paratext_open}"
            f"{paratext_body}"
            f"{paratext_close}"
            f"{indent}"
            f"{list_open}"
            f"{list_body}"
            f"{list_close}"
            f"{ws2}"
            f"{para_close}"
        )

    prev = None
    while prev != xml_content:
        prev = xml_content
        xml_content = pattern.sub(replacer, xml_content)
    return xml_content


def merge_consecutive_trace_lists(xml_content):
    merged = True
    while merged:
        merged = False
        pattern = re.compile(
            r'(<list\s[^>]*date\.trace="1"[^>]*>)(.*?)(</list>)'
            r'(\s*)'
            r'(<list\s[^>]*date\.trace="1"[^>]*>)(.*?)(</list>)',
            re.DOTALL
        )

        def merge_two(m):
            opening_tag = m.group(1)
            inner1      = m.group(2).rstrip()
            inner2      = m.group(6).lstrip()
            separator   = m.group(4)
            return f"{opening_tag}{inner1}{separator}{inner2}</list>"

        new_content = pattern.sub(merge_two, xml_content)
        if new_content != xml_content:
            xml_content = new_content
            merged = True

    return xml_content


def merge_adjacent_ital_tags(xml_content: str) -> str:
    """
    Merge adjacent italic fragments:
      <ital>a</ital>\s*<ital>b</ital> -> <ital>ab</ital>

    Spacing rule at merge boundary:
      - If left ends with whitespace OR right starts with whitespace, keep no extra separator.
      - Else if inter-tag whitespace existed, add a single space separator.
      - Else add no separator.
    """
    pattern = re.compile(r'<ital>([\s\S]*?)</ital>(\s*)<ital>([\s\S]*?)</ital>', re.DOTALL)

    def _merge(m: re.Match) -> str:
        left = m.group(1)
        between = m.group(2)
        right = m.group(3)

        left_has_ws_tail = bool(left) and left[-1].isspace()
        right_has_ws_head = bool(right) and right[0].isspace()

        if left_has_ws_tail or right_has_ws_head:
            sep = ''
        elif between and between.strip() == '':
            sep = ' '
        else:
            sep = ''

        return f'<ital>{left}{sep}{right}</ital>'

    prev = None
    while prev != xml_content:
        prev = xml_content
        xml_content = pattern.sub(_merge, xml_content)

    return xml_content




# pxmlpath=r"C:\projectdata\xmlproject\new_approach_working_f1 - Canada_with_validation\pipeline_jobs\e43ad4cd\output\save_before_postprocessing.pxml"
# ouputpath=r"C:\projectdata\xmlproject\new_approach_working_f1 - Canada_with_validation\pipeline_jobs\e43ad4cd\output\CDN_FI_EDRM 1 (chapter and all children)_postprocess_testing.pxml"
# with open(pxmlpath, 'r', encoding='utf-8') as f:
#     updated_pxmlcontent = f.read()
# # final_content = postprocessing_fortags(updated_pxmlcontent)
# final_content = postprocessing_fortags(updated_pxmlcontent)
# # final_content = collapse_empty_deleted_elements(updated_pxmlcontent)
# with open(ouputpath, 'w', encoding='utf-8') as f:
#     f.write(final_content)

