"""
footnote_cleanup.py
--------------------
Finds pairs of consecutive footnotes where:
  - Footnote A has reference = "0"  (newly inserted placeholder)
  - Footnote B (immediately after A) has a <trace.deleted/> body

For each such pair:
  1. Grabs the real reference number from Footnote B
  2. Assigns it to Footnote A (replacing the "0")
  3. Removes Footnote B entirely
"""

import re


def _find_footnote_blocks(pxml: str):
    """
    Yields (start, end, full_text) for every top-level <footnote ...>...</footnote> block.
    Works with multi-line footnote tags.
    """
    # Match <footnote ...>  (possibly with newlines in the opening tag)
    open_re = re.compile(r'<footnote\b', re.DOTALL)
    pos = 0
    while True:
        m = open_re.search(pxml, pos)
        if not m:
            break
        tag_start = m.start()
        # find the end of the opening tag (first '>')
        gt = pxml.index('>', tag_start)
        # now find the matching </footnote>
        close_idx = pxml.find('</footnote>', gt)
        if close_idx == -1:
            break
        end = close_idx + len('</footnote>')
        yield tag_start, end, pxml[tag_start:end]
        pos = end


def _get_reference_number(footnote_text: str):
    """Extract the content of <footnote.reference ...>...</footnote.reference>."""
    m = re.search(r'<footnote\.reference[^>]*>\s*(\S+)\s*</footnote\.reference>', footnote_text)
    return m.group(1).strip() if m else None


def _has_only_trace_deleted_body(footnote_text: str) -> bool:
    """
    Return True if the footnote body contains nothing but <trace.deleted/>.
    """
    body_m = re.search(
        r'<footnote\.body[^>]*>(.*?)</footnote\.body>',
        footnote_text, re.DOTALL
    )
    if not body_m:
        return False
    body_inner = body_m.group(1).strip()
    # Only whitespace + one or more <trace.deleted/> (self-closing variants)
    cleaned = re.sub(r'<trace\.deleted\s*/>', '', body_inner).strip()
    return cleaned == ''


def fix_zero_reference_footnotes(pxml: str) -> str:
    """
    Main function.  Scan all footnote blocks.
    When we find:
        footnote_A  with reference == "0"
        footnote_B  immediately after (only whitespace between them)
                    and footnote_B body is <trace.deleted/>

    → Replace footnote_A's reference with footnote_B's reference number
    → Delete footnote_B
    """
    blocks = list(_find_footnote_blocks(pxml))

    # Work backwards so indices stay valid as we make replacements
    replacements = []  # list of (a_ref_span, new_ref_number, b_start, b_end)

    for i in range(len(blocks) - 1):
        a_start, a_end, a_text = blocks[i]
        b_start, b_end, b_text = blocks[i + 1]

        # Must be adjacent (only whitespace between A and B)
        between = pxml[a_end:b_start]
        if between.strip():
            continue

        a_ref = _get_reference_number(a_text)
        if a_ref != '0':
            continue

        if not _has_only_trace_deleted_body(b_text):
            continue

        b_ref = _get_reference_number(b_text)
        if b_ref is None:
            print(f"  [FOOTNOTE-CLEANUP] Found zero-ref footnote at {a_start} but couldn't read B reference. Skipping.")
            continue

        print(f"  [FOOTNOTE-CLEANUP] Zero-ref footnote at offset {a_start}: "
              f"assigning reference '{b_ref}' (from trace-deleted footnote) and removing placeholder.")

        # Find the span of "0" inside the <footnote.reference> tag of A
        ref_m = re.search(
            r'(<footnote\.reference[^>]*>)\s*0\s*(</footnote\.reference>)',
            a_text
        )
        if not ref_m:
            continue

        # Absolute positions of the '0' value inside pxml
        zero_abs_start = a_start + ref_m.start(0)
        zero_abs_end   = a_start + ref_m.end(0)
        new_ref_tag    = f"{ref_m.group(1)}{b_ref}{ref_m.group(2)}"

        replacements.append((zero_abs_start, zero_abs_end, new_ref_tag, b_start, b_end))

    if not replacements:
        print("  [FOOTNOTE-CLEANUP] No zero-reference footnote pairs found.")
        return pxml

    # Apply in reverse order so positions don't shift
    for zero_start, zero_end, new_ref_tag, b_start, b_end in reversed(replacements):
        # Remove footnote B (including any whitespace between A and B)
        pxml = pxml[:b_start] + pxml[b_end:]
        # Replace the reference tag in footnote A
        pxml = pxml[:zero_start] + new_ref_tag + pxml[zero_end:]

    print(f"  [FOOTNOTE-CLEANUP] Fixed {len(replacements)} zero-reference footnote pair(s).")
    return pxml
