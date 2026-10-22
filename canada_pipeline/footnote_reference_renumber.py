"""
Deterministic handler for FOOTNOTE REFERENCE NUMBER renumbering.

When a NEW footnote is inserted in the middle of a section, all following
footnote reference numbers shift by one (1→2, 2→3, 3→4, ...). These show up in
the track-changes dataframe as:

    is_footnote_x == True
    type          == 'inline_replacement'
    text_before   == '1'   (old number)   / deleted_text  == '1'
    text          == '2'   (new number)   / inserted_text == '2'

The LLM footnote pass can't handle a bare digit as footnote *body* text, so
these rows are effectively skipped. This module applies them deterministically.

Target transformation (trace tags are NOT allowed inside <footnote.reference>,
so the number is replaced directly and the element is flagged as changed via
the date.updated attribute only — a cascade renumber is not an authored
track-change, so date.trace is NOT set):

    <footnote.reference date.updated="0">1</footnote.reference>
        →
    <footnote.reference date.updated="1">2</footnote.reference>

Matching uses `text_before_superscript` to LOCATE which `<footnote.reference>`
to target (the one nearest to the anchor text). This handles the case where the
same footnote number appears multiple times in a section.

After transformation the ref no longer matches the clean pattern, so applying
rules in any order cannot cascade.
"""

import re
import pandas as pd

from .paragraph_level_trackchanges import parse_pxml_sections_from_str_new
from .new_footnotes_insertions import normalize_text, xml_encode


def _is_pure_digits(value) -> bool:
    """True if value is a non-empty string/number of digits only (e.g. '2', '12')."""
    if value is None:
        return False
    s = str(value).strip()
    return s.isdigit() and len(s) > 0


def _set_ref_change_attrs(open_tag: str) -> str:
    """
    Return the <footnote.reference ...> opening tag with the change-tracking
    attribute set to date.updated="1".

    - If date.updated already exists (e.g. ="0"), its value is set to "1".
    - If date.updated is absent, it is added.
    - date.trace is intentionally NOT added: a cascade renumber (shifting the
      following footnote numbers by one) is not an authored track-change, so it
      must NOT be flagged with date.trace="1". Any pre-existing date.trace
      attribute is left untouched.
    Other existing attributes are preserved untouched.
    """
    tag = open_tag

    # date.updated → "1"
    if re.search(r'\bdate\.updated\s*=\s*"[^"]*"', tag):
        tag = re.sub(r'\bdate\.updated\s*=\s*"[^"]*"', 'date.updated="1"', tag, count=1)
    else:
        tag = re.sub(r'(<footnote\.reference\b)', r'\1 date.updated="1"', tag, count=1)

    return tag


def _find_anchor_position(section_xml: str, text_before_superscript: str) -> int:
    """
    Locate the end-position of `text_before_superscript` within section_xml.
    Tries multiple normalization strategies (raw, normalized, xml-encoded, tail).
    Returns the character position or -1 if not found.
    """
    if not text_before_superscript or not text_before_superscript.strip():
        return -1

    tbs = text_before_superscript.strip()
    tbs_norm = normalize_text(tbs)
    tbs_xmlenc = xml_encode(tbs_norm)

    candidates = [
        tbs,
        tbs_norm,
        tbs_xmlenc,
        tbs[-80:].strip() if len(tbs) > 80 else None,
        tbs_norm[-80:].strip() if len(tbs_norm) > 80 else None,
        tbs[-40:].strip() if len(tbs) > 40 else None,
        tbs_norm[-40:].strip() if len(tbs_norm) > 40 else None,
    ]

    for c in candidates:
        if not c or len(c) < 5:
            continue
        pos = section_xml.find(c)
        if pos != -1:
            return pos + len(c)

    # Fallback: try stripping XML tags from section for a plain-text match,
    # but return -1 (we need position in the raw XML, not stripped text).
    return -1


def _renumber_reference_nearest_to(
    section_xml: str, old_num: str, new_num: str, anchor_pos: int,
) -> tuple[str, bool]:
    """
    Find ALL clean <footnote.reference>OLD</footnote.reference> in section_xml,
    pick the one CLOSEST to `anchor_pos`, and replace only that one.

    Returns (updated_section_xml, changed).
    """
    pattern = re.compile(
        r'(<footnote\.reference\b[^>]*>)\s*' + re.escape(old_num) + r'\s*(</footnote\.reference>)'
    )

    matches = list(pattern.finditer(section_xml))
    if not matches:
        return section_xml, False

    if anchor_pos >= 0 and len(matches) > 1:
        # Pick match closest to (and ideally just after) the anchor position.
        best = min(matches, key=lambda m: abs(m.start() - anchor_pos))
        print(f"    [FN-RENUM] {len(matches)} matches for ref {old_num}; "
              f"picked the one at pos {best.start()} (anchor_pos={anchor_pos})")
    else:
        # Only one match, or no anchor — use the only/first match.
        best = matches[0]
        if len(matches) > 1:
            print(f"    [FN-RENUM] WARNING: {len(matches)} matches for ref {old_num} "
                  f"but no anchor to disambiguate; using first match at {best.start()}")

    open_tag = best.group(1)
    close_tag = best.group(2)
    # Trace tags are NOT allowed inside <footnote.reference>. Instead of wrapping
    # the old number in <trace.deleted>, replace the content with the NEW number
    # directly and mark the reference element as changed via the attribute:
    #   date.updated="1"   (date.trace is NOT set — cascade renumber is not an
    #                        authored track-change)
    new_open_tag = _set_ref_change_attrs(open_tag)
    replacement = f'{new_open_tag}{new_num}{close_tag}'

    new_xml = section_xml[:best.start()] + replacement + section_xml[best.end():]
    return new_xml, True


def _renumber_reference_in_section(
    section_xml: str, old_num: str, new_num: str,
    text_before_superscript: str = "",
) -> tuple[str, bool]:
    """
    Within a single section's XML, replace the CLEAN footnote reference whose text
    is `old_num` with `new_num`, setting date.updated="1" on the
    <footnote.reference> element (no date.trace and no trace tags are inserted).

    Uses `text_before_superscript` as a positional anchor to disambiguate when the
    same footnote number appears multiple times in the section.

    Only matches a reference that still contains the plain old number
    (i.e. not already tracked), so re-runs / overlapping rules cannot cascade.

    Returns (updated_section_xml, changed).
    """
    anchor_pos = _find_anchor_position(section_xml, text_before_superscript)
    if anchor_pos >= 0:
        print(f"    [FN-RENUM] Anchor text found at pos {anchor_pos}")
    else:
        print(f"    [FN-RENUM] Anchor text NOT found; will use first clean match")

    return _renumber_reference_nearest_to(section_xml, old_num, new_num, anchor_pos)


def process_footnote_reference_renumbering(
    result_df_footnotes: pd.DataFrame,
    pxml_content: str,
    chapter_number,
    verbose: bool = True,
) -> tuple[str, list]:
    """
    Apply footnote-reference-number replacements deterministically.

    Args:
        result_df_footnotes: DataFrame of footnote rows (is_footnote_x == True).
                             Only digit→digit inline_replacement rows are used.
        pxml_content:        Current PXML string.
        chapter_number:      Chapter number (for section parsing).
        verbose:             Print progress.

    Returns:
        (updated_pxml_content, failed_rows_list)
    """
    if result_df_footnotes is None or result_df_footnotes.empty:
        if verbose:
            print("[FN-RENUM] No footnote rows provided; nothing to do.")
        return pxml_content, []

    df = result_df_footnotes.copy()

    def _old_new(row) -> tuple[str, str]:
        # Prefer deleted_text/inserted_text; fall back to text_before/text.
        old = row.get("deleted_text")
        new = row.get("inserted_text")
        if not _is_pure_digits(old):
            old = row.get("text_before")
        if not _is_pure_digits(new):
            new = row.get("text")
        return (str(old).strip() if old is not None else "",
                str(new).strip() if new is not None else "")

    # Select only footnote-reference-number replacement rows (digit → digit).
    candidates = []
    for _, row in df.iterrows():
        rtype = str(row.get("type", "")).strip().lower()
        if rtype != "inline_replacement":
            continue
        old_num, new_num = _old_new(row)
        if not (_is_pure_digits(old_num) and _is_pure_digits(new_num)):
            continue
        if old_num == new_num:
            continue
        tbs = str(row.get("text_before_superscript", "") or "").strip()
        candidates.append({
            "idx": row.get("idx", "?"),
            "section": str(row.get("section", "")).strip(),
            "old_num": old_num,
            "new_num": new_num,
            "text_before_superscript": tbs,
        })

    if not candidates:
        if verbose:
            print("[FN-RENUM] No footnote-reference-number replacements found.")
        return pxml_content, []

    if verbose:
        print(f"\n{'='*60}")
        print(f"[FN-RENUM] Footnote reference-number replacements: {len(candidates)}")
        for c in candidates:
            tbs_preview = c['text_before_superscript'][-60:] if c['text_before_superscript'] else '(none)'
            print(f"  idx={c['idx']} section='{c['section']}' : {c['old_num']} → {c['new_num']} | anchor='...{tbs_preview}'")

    # Parse sections so we can scope each replacement to its own section.
    section_dict = parse_pxml_sections_from_str_new(pxml_content, chapter_number)

    updated_content = pxml_content
    failed = []

    for c in candidates:
        idx = c["idx"]
        section_str = c["section"]
        old_num = c["old_num"]
        new_num = c["new_num"]
        tbs = c["text_before_superscript"]

        # Resolve section XML (exact key, else designator-suffix match).
        section_key = None
        if section_str in section_dict:
            section_key = section_str
        else:
            base = section_str.lstrip("§").strip()
            for k in section_dict:
                if k == base or k.lstrip("§").strip() == base:
                    section_key = k
                    break

        if section_key is None:
            msg = f"section '{section_str}' not found in document"
            if verbose:
                print(f"  [FN-RENUM] idx={idx} FAILED — {msg}")
            failed.append({"idx": idx, "section": section_str,
                           "reason": f"REVISION FAILED - footnote ref renumber: {msg}",
                           "old_num": old_num, "new_num": new_num})
            continue

        section_xml = section_dict[section_key]
        new_section_xml, changed = _renumber_reference_in_section(section_xml, old_num, new_num, tbs)

        if not changed:
            msg = f"clean <footnote.reference>{old_num}</footnote.reference> not found in section '{section_key}'"
            if verbose:
                print(f"  [FN-RENUM] idx={idx} FAILED — {msg}")
            failed.append({"idx": idx, "section": section_str,
                           "reason": f"REVISION FAILED - footnote ref renumber: {msg}",
                           "old_num": old_num, "new_num": new_num})
            continue

        # Splice the modified section back into the full document.
        pos = updated_content.find(section_xml)
        if pos == -1:
            msg = f"could not splice section '{section_key}' back into document"
            if verbose:
                print(f"  [FN-RENUM] idx={idx} FAILED — {msg}")
            failed.append({"idx": idx, "section": section_str,
                           "reason": f"REVISION FAILED - footnote ref renumber: {msg}",
                           "old_num": old_num, "new_num": new_num})
            continue

        updated_content = (
            updated_content[:pos] + new_section_xml + updated_content[pos + len(section_xml):]
        )
        # Keep section_dict in sync for subsequent rows in the same section.
        section_dict[section_key] = new_section_xml

        if verbose:
            print(f"  [FN-RENUM] idx={idx} OK — section '{section_key}': "
                  f"{old_num} → <trace.deleted>{old_num}</trace.deleted>{new_num}")

    if verbose:
        print(f"[FN-RENUM] Done. Applied {len(candidates) - len(failed)}/{len(candidates)}; "
              f"failed {len(failed)}.")

    return updated_content, failed
