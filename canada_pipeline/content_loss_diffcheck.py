"""
trace_validator.py

Validates that a "final" XML (containing <trace>...</trace> for insertions
and self-closing <trace.deleted/> markers for deletions) correctly accounts
for ALL differences from the prevendor (pre-track-changes) XML.

Flags any text that disappeared from prevendor -> final WITHOUT a
corresponding <trace.deleted/> marker (i.e. unintended / unaccounted loss),
and any extra text in final that isn't explained by <trace> insertion tags.

Requires: lxml   ->   pip install lxml
Run:      python trace_validator.py
"""

import re
import bisect
import difflib
from lxml import etree


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def load_xml(path):
    parser = etree.XMLParser(recover=True, huge_tree=True)
    with open(path, "rb") as f:
        return etree.parse(f, parser).getroot()


def local_name(tag):
    # Comment / ProcessingInstruction / Entity nodes have a callable .tag
    # (a cython function like lxml.etree.Comment) instead of a string.
    # Normalize those to a readable name so downstream str joins don't break.
    if not isinstance(tag, str):
        return getattr(tag, "__name__", "special")
    return tag.split('}')[-1] if '}' in tag else tag


def tree_path(elem):
    parts = []
    e = elem
    while e is not None:
        parts.append(local_name(e.tag))
        e = e.getparent()
    return "/".join(reversed(parts))


# ----------------------------------------------------------------------
# Prevendor extraction (plain, no trace tags expected)
# ----------------------------------------------------------------------

def extract_prevendor_text(root):
    """
    Concatenate all text nodes (text + tail) in document order.
    Returns (full_text, offset_map) where offset_map = [(start, end, path, txt), ...]
    """
    parts = []
    offset_map = []
    pos = 0

    for elem in root.iter():
        if elem.text:
            start = pos
            parts.append(elem.text)
            pos += len(elem.text)
            offset_map.append((start, pos, tree_path(elem), elem.text))
        if elem.tail:
            start = pos
            parts.append(elem.tail)
            pos += len(elem.tail)
            offset_map.append((start, pos, tree_path(elem) + "[tail]", elem.tail))

    return "".join(parts), offset_map


# ----------------------------------------------------------------------
# Final XML extraction (trace-aware)
# ----------------------------------------------------------------------

TRACE_TAG = "trace"
TRACE_DEL_TAG = "trace.deleted"


def extract_final_baseline_view(root):
    """
    Walk final XML. Build a text stream representing what SHOULD align
    with prevendor text: skip anything inside <trace> (insertions),
    and record the character offset of every <trace.deleted/> marker.

    Returns:
        unchanged_text   : str
        offset_map       : [(start, end, path, txt), ...]  (for unchanged_text)
        del_marker_positions : [int, ...] offsets (into unchanged_text) where
                                a <trace.deleted/> marker occurred
        inserted_segments : [(path, txt), ...] all text found inside <trace>
    """
    unchanged_parts = []
    offset_map = []
    del_marker_positions = []
    inserted_segments = []
    pos = [0]

    def add_text(txt, path):
        if not txt:
            return
        start = pos[0]
        unchanged_parts.append(txt)
        pos[0] += len(txt)
        offset_map.append((start, pos[0], path, txt))

    def recurse(elem, inside_trace):
        tag = local_name(elem.tag)
        cur_inside_trace = inside_trace or (tag == TRACE_TAG)

        if tag == TRACE_DEL_TAG:
            # self-closing deletion marker -> record position, no text
            del_marker_positions.append(pos[0])
        else:
            if elem.text:
                if cur_inside_trace:
                    inserted_segments.append((tree_path(elem), elem.text))
                else:
                    add_text(elem.text, tree_path(elem))

        for child in elem:
            recurse(child, cur_inside_trace)
            # tail text belongs to the level of the PARENT context,
            # not the child's own (e.g. text right after </trace>)
            if child.tail:
                if cur_inside_trace:
                    inserted_segments.append((tree_path(child) + "[tail]", child.tail))
                else:
                    add_text(child.tail, tree_path(child) + "[tail]")

    recurse(root, False)
    return "".join(unchanged_parts), offset_map, del_marker_positions, inserted_segments


# ----------------------------------------------------------------------
# Diff + validation
# ----------------------------------------------------------------------

_WORD_RE = re.compile(r"\S+")


def tokenize_words(text):
    """
    Split text into word tokens (runs of non-whitespace) with their char
    offsets:  tokens = [(word, start, end), ...].

    Diffing whole words instead of individual characters keeps difflib fast
    on large documents (the token 'alphabet' is huge, so match lists stay
    short) while still letting us report exact character offsets. Whitespace
    is intentionally ignored, which matches this validator's existing
    behaviour of skipping whitespace-only differences.
    """
    return [(m.group(), m.start(), m.end()) for m in _WORD_RE.finditer(text)]


def token_range_to_chars(tokens, text_len, i1, i2):
    """Map a token index range [i1, i2) back to a character span (start, end)."""
    if i1 >= i2:  # empty range -> an insertion / deletion point
        pos = tokens[i1][1] if i1 < len(tokens) else text_len
        return pos, pos
    return tokens[i1][1], tokens[i2 - 1][2]


def find_location(offset_map, char_index, starts=None):
    if not offset_map:
        return "UNKNOWN"
    if starts is not None:
        # offset_map is built in ascending, non-overlapping order, so we can
        # binary-search instead of scanning it for every reported change.
        i = bisect.bisect_right(starts, char_index) - 1
        if 0 <= i < len(offset_map):
            start, end, path, _ = offset_map[i]
            if start <= char_index < end:
                return path
        return "END_OF_DOCUMENT" if char_index >= offset_map[-1][1] else "UNKNOWN"
    for start, end, path, txt in offset_map:
        if start <= char_index < end:
            return path
    return "END_OF_DOCUMENT" if char_index >= offset_map[-1][1] else "UNKNOWN"


def validate(prevendor_path, final_path, report_path="trace_validation_report.txt",
             marker_tolerance=0):
    """
    marker_tolerance: allowed char distance between a diff 'delete' gap
                       and the nearest <trace.deleted/> marker to still
                       count as "matched" (helps absorb whitespace noise).
    """
    prevendor_root = load_xml(prevendor_path)
    final_root = load_xml(final_path)

    text_pre, pre_map = extract_prevendor_text(prevendor_root)
    text_final_unchanged, final_map, del_markers, inserted_segments = \
        extract_final_baseline_view(final_root)

    # Diff at the WORD level rather than character-by-character. Character-level
    # difflib on large XML is close to O(n^2) (tiny alphabet -> huge match
    # lists) and can hang for minutes; word-level tokens keep the exact same
    # character offsets for reporting while running in seconds.
    pre_tokens = tokenize_words(text_pre)
    final_tokens = tokenize_words(text_final_unchanged)
    pre_words = [t[0] for t in pre_tokens]
    final_words = [t[0] for t in final_tokens]

    sm = difflib.SequenceMatcher(a=pre_words, b=final_words, autojunk=False)
    opcodes = sm.get_opcodes()

    # Precompute segment start offsets so find_location can binary-search
    # instead of scanning the whole map for every reported change.
    pre_map_starts = [e[0] for e in pre_map]
    final_map_starts = [e[0] for e in final_map]

    unaccounted_deletions = []   # text lost, no nearby trace.deleted marker
    accounted_deletions = []     # text lost, marker confirmed
    unexplained_insertions = []  # text appeared in final-unchanged, not in prevendor, not tagged

    used_markers = set()

    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            continue

        # Map token index ranges back to character offsets.
        ci1, ci2 = token_range_to_chars(pre_tokens, len(text_pre), i1, i2)
        cj1, cj2 = token_range_to_chars(final_tokens, len(text_final_unchanged), j1, j2)

        seg_pre = text_pre[ci1:ci2]
        seg_final = text_final_unchanged[cj1:cj2]

        if tag in ("delete", "replace") and seg_pre.strip():
            # gap in final-unchanged stream is at char position cj1 (==cj2 for pure delete)
            gap_pos = cj1
            nearest_marker = min(del_markers, key=lambda m: abs(m - gap_pos)) if del_markers else None
            matched = (nearest_marker is not None and
                       abs(nearest_marker - gap_pos) <= marker_tolerance and
                       nearest_marker not in used_markers)

            record = {
                "prevendor_text": seg_pre,
                "prevendor_range": (ci1, ci2),
                "location": find_location(pre_map, ci1, pre_map_starts),
                "final_gap_position": gap_pos,
                "nearest_marker_offset": nearest_marker,
            }
            if matched:
                used_markers.add(nearest_marker)
                accounted_deletions.append(record)
            else:
                unaccounted_deletions.append(record)

        if tag in ("insert", "replace") and seg_final.strip():
            unexplained_insertions.append({
                "final_text": seg_final,
                "final_range": (cj1, cj2),
                "location": find_location(final_map, cj1, final_map_starts),
            })

    unused_markers = [m for m in del_markers if m not in used_markers]

    # ------------------------------------------------------------
    # Write report
    # ------------------------------------------------------------
    with open(report_path, "w", encoding="utf-8") as r:
        def w(line=""):
            r.write(line + "\n")

        w("=== TRACE-BASED VALIDATION REPORT ===\n")

        w(f"Total <trace.deleted/> markers found in final XML : {len(del_markers)}")
        w(f"Markers matched to an actual text-loss gap         : {len(used_markers)}")
        w(f"Markers with NO corresponding text loss (orphan)   : {len(unused_markers)}")
        w(f"Deletions confirmed by a marker                    : {len(accounted_deletions)}")
        w(f"Deletions with NO marker (UNACCOUNTED LOSS)        : {len(unaccounted_deletions)}")
        w(f"Unexplained extra text in final (not <trace>-tagged): {len(unexplained_insertions)}\n")

        w("---- 🚩 UNACCOUNTED TEXT LOSS (present in prevendor, missing in final, NO <trace.deleted/> marker) ----\n")
        if not unaccounted_deletions:
            w("None found. ✅\n")
        else:
            for idx, rec in enumerate(unaccounted_deletions, 1):
                w(f"[{idx}] Lost text: {rec['prevendor_text']!r}")
                w(f"    Prevendor char range: {rec['prevendor_range']}")
                w(f"    Prevendor location:   {rec['location']}")
                w(f"    Gap position in final: {rec['final_gap_position']}")
                if rec['nearest_marker_offset'] is not None:
                    w(f"    Nearest marker offset (not close enough / already used): {rec['nearest_marker_offset']}")
                else:
                    w(f"    No <trace.deleted/> markers exist anywhere in final XML.")
                w()

        w("---- ✅ DELETIONS PROPERLY MARKED (sanity check, should match your track-change list) ----\n")
        if not accounted_deletions:
            w("None found.\n")
        else:
            for idx, rec in enumerate(accounted_deletions, 1):
                w(f"[{idx}] Deleted text: {rec['prevendor_text']!r} at {rec['location']} "
                  f"(marker offset {rec['nearest_marker_offset']})")
            w()

        w("---- ⚠️ ORPHAN <trace.deleted/> MARKERS (marker exists but no text loss detected nearby) ----\n")
        if not unused_markers:
            w("None found. ✅\n")
        else:
            for idx, m in enumerate(unused_markers, 1):
                w(f"[{idx}] Orphan marker at final-stream offset {m}")
            w()

        w("---- 🚩 UNEXPLAINED EXTRA TEXT (present in final, not in prevendor, NOT wrapped in <trace>) ----\n")
        if not unexplained_insertions:
            w("None found. ✅\n")
        else:
            for idx, rec in enumerate(unexplained_insertions, 1):
                w(f"[{idx}] Extra text: {rec['final_text']!r}")
                w(f"    Final XML location: {rec['location']}")
                w()

        w("---- <trace> (INSERTION) SEGMENTS FOUND IN FINAL XML (for reference) ----\n")
        if not inserted_segments:
            w("None found.\n")
        else:
            for idx, (path, txt) in enumerate(inserted_segments, 1):
                w(f"[{idx}] {txt!r}  at {path}")
            w()

        w("---- SUMMARY ----")
        if unaccounted_deletions or unexplained_insertions:
            w("⚠️  ACTION NEEDED: Review flagged items above — text was altered outside of tracked changes.")
        else:
            w("✅ All differences between prevendor and final XML are properly accounted for by <trace> / <trace.deleted/> markers.")

    print(f"Report written to: {report_path}")
    return {
        "unaccounted_deletions": unaccounted_deletions,
        "accounted_deletions": accounted_deletions,
        "unused_markers": unused_markers,
        "unexplained_insertions": unexplained_insertions,
        "inserted_segments": inserted_segments,
    }


# ----------------------------------------------------------------------
if __name__ == "__main__":
    # ---- EDIT THESE PATHS ----
    PREVENDOR_XML = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\test13\anamika\CDN_MA_LCCIC 7 (chapter and all children).pxml"   # baseline XML before any changes
    FINAL_XML     = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\test13\anamika\testing\CDN_MA_LCCIC 7 (chapter and all children)_20260702_161505_final.pxml"       # your generated output with <trace> / <trace.deleted/>

    validate(
        PREVENDOR_XML,
        FINAL_XML,
        report_path="trace_validation_report.txt",
        marker_tolerance=0,   # increase (e.g. 2-5) if whitespace/formatting noise causes near-misses
    )