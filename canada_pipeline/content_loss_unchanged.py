"""
content_loss_unchanged.py

Content-loss detector using the "unchanged text" approach.

Idea (very simple, tag/diff based):

    1. From the DOCX, take ALL the *unchanged* text.
       -> every <w:t> that is NOT inside a tracked change
          (<w:ins> insertion / <w:del> deletion / <w:moveFrom>).
       This is the text that MUST survive into the final XML.

    2. From the final XML, remove all text bound between <trace> ... </trace>
       (those are the newly inserted words, they are not part of the
       original/unchanged content).

    3. Then remove every remaining tag from the final XML, keeping only
       the plain text (this is the "final baseline" text).
       <trace.deleted/> markers are self-closing, so they naturally
       disappear when tags are stripped.

    4. Compare the DOCX unchanged text against the final baseline text.

    5. Whatever is present in the DOCX unchanged text but MISSING from the
       final baseline text is reported as *content drop* (unintended loss).

Notes
-----
* Comparison is done at the WORD level (runs of non-whitespace) so that pure
  whitespace / line-wrapping differences never count as content loss, and so
  that difflib stays fast on large documents.
* A small amount of character folding is applied to both sides (curly quotes,
  non-breaking spaces, dashes, ellipsis) so cosmetic punctuation differences
  are not reported as drops.
* By default only 'word/document.xml' is read from the DOCX. Footnotes,
  endnotes, headers and footers live in separate parts; add them to
  `docx_parts` if your final XML is expected to contain them too.

Requires: lxml   ->   pip install lxml
Run:      python content_loss_unchanged.py  [DOCX] [FINAL_XML] [REPORT_TXT]
"""

import os
import re
import sys
import bisect
import zipfile
import difflib
from lxml import etree


# ----------------------------------------------------------------------
# Generic helpers
# ----------------------------------------------------------------------

def local_name(tag):
    # Comment / ProcessingInstruction / Entity nodes have a callable .tag
    # (a cython function like lxml.etree.Comment) instead of a string.
    if not isinstance(tag, str):
        return getattr(tag, "__name__", "special")
    return tag.split('}')[-1] if '}' in tag else tag


def load_xml_bytes(data):
    parser = etree.XMLParser(recover=True, huge_tree=True)
    return etree.fromstring(data, parser)


def load_xml_file(path):
    parser = etree.XMLParser(recover=True, huge_tree=True)
    with open(path, "rb") as f:
        return etree.parse(f, parser).getroot()


# ----------------------------------------------------------------------
# Normalisation + tokenisation (shared by both sides)
# ----------------------------------------------------------------------

_WORD_RE = re.compile(r"\S+")

# Characters that frequently differ between a DOCX and generated XML but do
# NOT represent real content loss -> fold them to a canonical form so cosmetic
# punctuation differences are never flagged as drops.
_CHAR_FOLDS = {
    "\u00a0": " ",                  # non-breaking space
    "\u2018": "'", "\u2019": "'",   # curly single quotes
    "\u201c": '"', "\u201d": '"',   # curly double quotes
    "\u2013": "-", "\u2014": "-",   # en / em dash
    "\u2212": "-",                  # minus sign
    "\u2026": "...",                # ellipsis
}


def normalize_text(text):
    if not text:
        return ""
    for a, b in _CHAR_FOLDS.items():
        if a in text:
            text = text.replace(a, b)
    return text


def tokenize_words(text):
    """Return [(word, start_char, end_char), ...] for every run of non-space."""
    return [(m.group(), m.start(), m.end()) for m in _WORD_RE.finditer(text)]


# ----------------------------------------------------------------------
# STEP 1 - DOCX unchanged text extraction
# ----------------------------------------------------------------------

# Ancestors that mark text as a *tracked change* (i.e. NOT unchanged).
#   ins       -> tracked insertion (new text)         -> exclude
#   del       -> tracked deletion (removed text)       -> exclude
#   moveFrom  -> original location of moved text        -> exclude
# (moveTo text is retained in the document, so it is treated as unchanged.)
_DOCX_CHANGE_ANCESTORS = {"ins", "del", "moveFrom"}


def _inside_tracked_change(el):
    parent = el.getparent()
    while parent is not None:
        if isinstance(parent.tag, str) and local_name(parent.tag) in _DOCX_CHANGE_ANCESTORS:
            return True
        parent = parent.getparent()
    return False


def extract_docx_unchanged_text(doc_root):
    """
    Concatenate every <w:t> that is not inside a tracked change, in document
    order. Paragraph / tab / break elements are turned into whitespace so that
    words from different paragraphs do not accidentally merge.
    """
    parts = []
    for el in doc_root.iter():
        if not isinstance(el.tag, str):
            continue
        name = local_name(el.tag)
        if name == "p":                 # paragraph boundary
            parts.append("\n")
        elif name == "tab":
            parts.append("\t")
        elif name in ("br", "cr"):
            parts.append("\n")
        elif name == "t":               # <w:t> run text (delText is skipped by name)
            if el.text and not _inside_tracked_change(el):
                parts.append(el.text)
    return normalize_text("".join(parts))


# DOCX parts that can hold body-level text. Footnotes / endnotes and
# headers / footers live in their own parts, so they must be read explicitly
# for their unchanged text to be compared against the final XML.
# Header/footer part names are numbered (header1.xml, footer2.xml, ...), so
# they are matched by regex rather than exact name.
DEFAULT_DOCX_PARTS = (
    "word/document.xml",
    "word/footnotes.xml",
    "word/endnotes.xml",
)
_DOCX_PART_PATTERNS = (
    re.compile(r"^word/header\d+\.xml$"),
    re.compile(r"^word/footer\d+\.xml$"),
)


def _resolve_docx_parts(available, docx_parts):
    """
    Expand the requested part list against what actually exists in the DOCX,
    preserving order and also pulling in any numbered header/footer parts.
    """
    resolved = [p for p in docx_parts if p in available]
    for name in sorted(available):
        if name in resolved:
            continue
        if any(pat.match(name) for pat in _DOCX_PART_PATTERNS):
            resolved.append(name)
    return resolved


def read_docx_unchanged_text(path, docx_parts=DEFAULT_DOCX_PARTS):
    """
    Read the requested parts out of the DOCX (a zip) and return their combined
    unchanged text. Numbered header/footer parts are auto-included. Missing
    parts are silently skipped.
    """
    collected = []
    used_parts = []
    with zipfile.ZipFile(path) as z:
        available = set(z.namelist())
        for part in _resolve_docx_parts(available, docx_parts):
            root = load_xml_bytes(z.read(part))
            collected.append(extract_docx_unchanged_text(root))
            used_parts.append(part)
    return "\n".join(collected), used_parts


# ----------------------------------------------------------------------
# STEP 2 + 3 - Final XML: drop <trace> insertions, strip all tags
# ----------------------------------------------------------------------

TRACE_TAG = "trace"
TRACE_DEL_TAG = "trace.deleted"


def extract_final_baseline_text(root):
    """
    Return the plain baseline text of the final XML:
      * text inside <trace> ... </trace> (insertions) is DROPPED
      * <trace.deleted/> markers are self-closing -> contribute nothing
      * every other tag is stripped, leaving only its text and tails

    The tree height of these documents is small (chapter > section > para >
    run), so simple recursion is safe even for very large / wide files.
    """
    out = []

    def collect(el, inside_trace):
        if isinstance(el.tag, str):
            el_inside = inside_trace or (local_name(el.tag) == TRACE_TAG)
            if el.text and not el_inside:
                out.append(el.text)
        else:
            # comment / PI / entity: no baseline text of its own
            el_inside = inside_trace

        for child in el:
            collect(child, el_inside)
            # child.tail lives at THIS element's content level -> gated by el_inside
            if child.tail and not el_inside:
                out.append(child.tail)

    collect(root, False)
    return normalize_text("".join(out))


# ----------------------------------------------------------------------
# STEP 4 + 5 - Compare and find dropped content
# ----------------------------------------------------------------------

def _context(text, start, end, width=45):
    pre = text[max(0, start - width):start].replace("\n", " ").replace("\t", " ")
    post = text[end:end + width].replace("\n", " ").replace("\t", " ")
    return pre.strip(), post.strip()


def find_content_loss(docx_text, final_text):
    """
    Word-level diff of DOCX unchanged text (a) vs final baseline text (b).

    Returns (drops, extras, ratio):
      drops  : segments present in DOCX but missing from final (content loss)
      extras : segments present in final but not in DOCX (for reference)
      ratio  : difflib similarity ratio between the two word streams
    """
    a = tokenize_words(docx_text)
    b = tokenize_words(final_text)
    a_words = [w for w, _, _ in a]
    b_words = [w for w, _, _ in b]

    sm = difflib.SequenceMatcher(a=a_words, b=b_words, autojunk=False)

    drops = []
    extras = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue

        if tag in ("delete", "replace") and i2 > i1:
            c_start, c_end = a[i1][1], a[i2 - 1][2]
            seg = docx_text[c_start:c_end]
            if seg.strip():
                pre, post = _context(docx_text, c_start, c_end)
                drops.append({
                    "text": seg,
                    "word_range": (i1, i2),
                    "char_range": (c_start, c_end),
                    "context_before": pre,
                    "context_after": post,
                })

        if tag in ("insert", "replace") and j2 > j1:
            c_start, c_end = b[j1][1], b[j2 - 1][2]
            seg = final_text[c_start:c_end]
            if seg.strip():
                extras.append({
                    "text": seg,
                    "word_range": (j1, j2),
                    "char_range": (c_start, c_end),
                })

    return drops, extras, sm.ratio(), a_words, b_words


# ----------------------------------------------------------------------
# STEP 6 - DOCX tracked DELETIONS (expected content removals)
# ----------------------------------------------------------------------
#
# When a PREVENDOR xml is compared against a POSTVENDOR xml, prevendor text
# that is missing from the postvendor xml normally counts as "content loss".
# But some of that text was *meant* to be removed: it is marked as a tracked
# deletion (<w:del><w:delText>...) in the source DOCX. We extract those
# deletions - each with a few words of unchanged context on either side - so a
# caller can LOCATE them in the prevendor text and skip flagging them as loss.

# Tracked-change wrappers in a DOCX. Text inside <w:ins>/<w:del>/<w:moveFrom>
# is not plain "kept" text; <w:moveTo> text is retained, so it counts as kept.
_DOCX_CHANGE_KINDS = {"ins", "del", "moveFrom", "moveTo"}


def _nearest_change_kind(el):
    """Return 'ins'/'del'/'moveFrom'/'moveTo' for the nearest tracked-change
    ancestor of `el`, or None when it is plain body text."""
    parent = el.getparent()
    while parent is not None:
        if isinstance(parent.tag, str):
            n = local_name(parent.tag)
            if n in _DOCX_CHANGE_KINDS:
                return n
        parent = parent.getparent()
    return None


def _docx_kept_deleted_tokens(doc_root):
    """
    Ordered token stream [(word, kind)] for one DOCX part, where kind is:
      'keep' -> unchanged / retained text (<w:t> not inside ins/del/moveFrom)
      'del'  -> tracked deletion text (<w:delText> inside <w:del>)
    Insertions and moveFrom text are ignored (they are not in the prevendor xml).

    Same-kind text from consecutive runs is joined before tokenising (so a word
    split across runs stays one word), and any structural boundary or skipped
    (inserted) text flushes the buffer so words never merge across it.
    """
    tokens = []
    buf = []
    buf_kind = None

    def flush():
        nonlocal buf, buf_kind
        if buf and buf_kind:
            for w in _WORD_RE.findall(normalize_text("".join(buf))):
                tokens.append((w, buf_kind))
        buf = []
        buf_kind = None

    for el in doc_root.iter():
        if not isinstance(el.tag, str):
            continue
        name = local_name(el.tag)
        if name == "t":
            if el.text:
                if _nearest_change_kind(el) in (None, "moveTo"):
                    if buf_kind != "keep":
                        flush()
                        buf_kind = "keep"
                    buf.append(el.text)
                else:
                    flush()   # inserted / other change text -> break the run
        elif name == "delText":
            if el.text:
                if buf_kind != "del":
                    flush()
                    buf_kind = "del"
                buf.append(el.text)
        elif name in ("p", "br", "cr", "tab"):
            flush()           # structural boundary -> break the run
    flush()
    return tokens


def extract_docx_deletions(doc_root, context_words=6):
    """
    Return the tracked deletions in one DOCX part as a list of:
      {
        "words":  [deleted words],
        "before": [up to context_words unchanged words before],
        "after":  [up to context_words unchanged words after],
        "text":   "deleted words joined by spaces",
      }
    Consecutive deleted words are grouped into a single deletion.
    """
    tokens = _docx_kept_deleted_tokens(doc_root)
    deletions = []
    n = len(tokens)
    i = 0
    while i < n:
        if tokens[i][1] != "del":
            i += 1
            continue
        j = i
        while j < n and tokens[j][1] == "del":
            j += 1
        dwords = [w for w, _ in tokens[i:j]]

        before = []
        b = i - 1
        while b >= 0 and len(before) < context_words:
            if tokens[b][1] == "keep":
                before.append(tokens[b][0])
            b -= 1
        before.reverse()

        after = []
        a = j
        while a < n and len(after) < context_words:
            if tokens[a][1] == "keep":
                after.append(tokens[a][0])
            a += 1

        deletions.append({
            "words": dwords,
            "before": before,
            "after": after,
            "text": " ".join(dwords),
        })
        i = j
    return deletions


def read_docx_deletions(path, context_words=6, docx_parts=DEFAULT_DOCX_PARTS):
    """
    Read all tracked deletions out of a DOCX (a zip). `path` may be a filename
    or a file-like object (e.g. io.BytesIO). Numbered header/footer parts are
    auto-included; missing parts are skipped.
    """
    deletions = []
    with zipfile.ZipFile(path) as z:
        available = set(z.namelist())
        for part in _resolve_docx_parts(available, docx_parts):
            root = load_xml_bytes(z.read(part))
            deletions.extend(extract_docx_deletions(root, context_words))
    return deletions


# ----------------------------------------------------------------------
# Locating deletions inside an XML string  (+ range utilities)
# ----------------------------------------------------------------------

_XML_TAG_RE = re.compile(r"<[^>]*>")


def mask_xml_tags(text):
    """
    Replace every <...> tag with an equal-length run of spaces. Character
    offsets are preserved, so word positions found in the masked text map back
    exactly onto the original text (tags simply become invisible whitespace).
    """
    return _XML_TAG_RE.sub(lambda m: " " * (m.end() - m.start()), text)


def plain_word_positions(text):
    """[(word, start, end), ...] for plain words in `text`, ignoring XML tags."""
    masked = mask_xml_tags(text)
    return [(m.group(), m.start(), m.end()) for m in _WORD_RE.finditer(masked)]


def has_plain_word(segment):
    """True if `segment` still contains a word once XML tags are removed."""
    return bool(_WORD_RE.search(mask_xml_tags(segment)))


def _context_score(words, i, j, before, after):
    """Count context words matching consecutively outward from the span [i, j)."""
    score = 0
    b = i - 1
    for w in reversed(before):
        if b < 0 or words[b] != w:
            break
        score += 1
        b -= 1
    a = j
    for w in after:
        if a >= len(words) or words[a] != w:
            break
        score += 1
        a += 1
    return score


def locate_deletions_in_text(text, deletions, context_words=6):
    """
    Find each DOCX deletion inside `text` (an xml string) at the word level,
    ignoring tags. When a deletion's words occur more than once, the occurrence
    whose surrounding unchanged context best matches is chosen.

    Returns (ranges, matched, unmatched):
      ranges    : [(char_start, char_end), ...] char spans of the located
                  deletions inside `text`
      matched   : number of deletions located
      unmatched : deletions that could not be found in `text`
    """
    plain = plain_word_positions(text)
    words = [w for w, _, _ in plain]
    used = [False] * len(words)
    ranges = []
    matched = 0
    unmatched = []

    for d in deletions:
        dwords = d.get("words") or []
        m = len(dwords)
        if m == 0 or m > len(words):
            if m:
                unmatched.append(d)
            continue

        before = d.get("before", [])
        after = d.get("after", [])
        want = len(before) + len(after)
        first = dwords[0]
        best = None  # (score, index)

        i = 0
        limit = len(words) - m
        while i <= limit:
            if words[i] == first and not any(used[i:i + m]) and words[i:i + m] == dwords:
                score = _context_score(words, i, i + m, before, after)
                if best is None or score > best[0]:
                    best = (score, i)
                    if want and score == want:
                        break  # cannot beat a full context match
            i += 1

        if best is None:
            unmatched.append(d)
            continue

        i = best[1]
        for k in range(i, i + m):
            used[k] = True
        ranges.append((plain[i][1], plain[i + m - 1][2]))
        matched += 1

    return ranges, matched, unmatched


def merge_ranges(ranges):
    """Merge overlapping / adjacent (start, end) ranges."""
    ranges = sorted((s, e) for s, e in ranges if e > s)
    merged = []
    for s, e in ranges:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def subtract_ranges(ranges, holes):
    """Return `ranges` with every span in `holes` removed (char-level cut-out)."""
    ranges = merge_ranges(ranges)
    holes = merge_ranges(holes)
    if not holes:
        return ranges
    result = []
    for s, e in ranges:
        cur = s
        for hs, he in holes:
            if he <= cur or hs >= e:
                continue
            if hs > cur:
                result.append((cur, hs))
            cur = max(cur, he)
            if cur >= e:
                break
        if cur < e:
            result.append((cur, e))
    return result


# ----------------------------------------------------------------------
# TRACE-AWARE compare: prevendor XML vs postvendor XML (no DOCX needed)
# ----------------------------------------------------------------------
#
# The POSTVENDOR xml records every tracked change inline:
#   <trace> ... </trace>   -> INSERTED text (new content)
#   <trace.deleted/>        -> self-closing marker WHERE text was DELETED
#
# So we don't need the DOCX to know what was deleted on purpose: a piece of
# prevendor text that is missing from the postvendor is an EXPECTED deletion
# when a <trace.deleted/> marker sits exactly where it used to be, and is a
# REAL (unaccounted) loss when there is no marker there.
#
# Everything below works on the RAW xml strings (tags masked with regex) so the
# character offsets returned map straight back onto the text shown in the UI.

# <trace> / </trace> / <trace.deleted/>  (attributes tolerated, case-insensitive)
_TRACE_OPEN_RE = re.compile(r"<trace(?:\s[^>]*)?>", re.I)
_TRACE_CLOSE_RE = re.compile(r"</trace\s*>", re.I)
_TRACE_DEL_RE = re.compile(r"<trace\.deleted(?:\s[^>]*)?/?>", re.I)


def _tokens_with_offsets(masked, raw):
    """
    Word tokens of `raw` using `masked` (same length, tags turned to spaces) to
    find word boundaries. Returns [(norm_word, start, end), ...] where the
    offsets index into `raw` and norm_word is folded only for comparison.
    """
    return [(normalize_text(raw[m.start():m.end()]), m.start(), m.end())
            for m in _WORD_RE.finditer(masked)]


def build_postvendor_baseline(post_text):
    """
    Turn the raw postvendor xml into its "baseline" view (what SHOULD line up
    with the prevendor text), keeping character offsets intact.

    Returns (baseline_masked, del_positions, insertion_ranges):
      baseline_masked : same length as post_text, with every tag AND every
                        <trace> INSERTION text blanked to spaces (so only the
                        original/retained words remain as tokens)
      del_positions   : sorted char offsets where a <trace.deleted/> marker is
      insertion_ranges: (start, end) char spans of <trace> insertion text
                        (for optional amber highlighting)
    """
    baseline = list(mask_xml_tags(post_text))   # tags already blanked
    del_positions = []
    insertion_ranges = []
    trace_depth = 0
    last = 0

    for m in _XML_TAG_RE.finditer(post_text):
        # text sitting between the previous tag and this one
        if trace_depth > 0 and m.start() > last:
            for k in range(last, m.start()):
                baseline[k] = " "
            insertion_ranges.append((last, m.start()))

        tag = m.group()
        if _TRACE_DEL_RE.match(tag):
            del_positions.append(m.start())
        elif _TRACE_CLOSE_RE.match(tag):
            if trace_depth > 0:
                trace_depth -= 1
        elif _TRACE_OPEN_RE.match(tag):
            trace_depth += 1
        last = m.end()

    if trace_depth > 0 and len(post_text) > last:
        for k in range(last, len(post_text)):
            baseline[k] = " "
        insertion_ranges.append((last, len(post_text)))

    return "".join(baseline), sorted(del_positions), insertion_ranges


def find_unaccounted_loss(pre_text, post_text, marker_slack=2):
    """
    Compare prevendor xml vs postvendor xml at the word level and split the
    prevendor content that is missing from the postvendor into:

      * EXPECTED deletions - a <trace.deleted/> marker sits where the text was
      * REAL loss          - the text vanished with NO marker to explain it

    All ranges are character spans into `pre_text` (for REAL loss / expected)
    or `post_text` (for insertions), so they map straight onto the raw xml.
    """
    pre_masked = mask_xml_tags(pre_text)
    pre_toks = _tokens_with_offsets(pre_masked, pre_text)
    pre_words = [t[0] for t in pre_toks]

    post_baseline, del_positions, insertion_ranges = build_postvendor_baseline(post_text)
    post_toks = _tokens_with_offsets(post_baseline, post_text)
    post_words = [t[0] for t in post_toks]

    # Index of postvendor baseline words -> positions, so we can quickly tell
    # whether a "missing" prevendor chunk still exists somewhere in the
    # postvendor (i.e. it was MOVED / reordered, not lost). This is essential
    # because the pipeline heavily restructures the document, and a single
    # global word diff reports moved text as deleted-here + inserted-there.
    from collections import defaultdict
    post_index = defaultdict(list)
    for pos, w in enumerate(post_words):
        post_index[w].append(pos)

    def _still_present(chunk_words):
        """True if the exact word sequence occurs contiguously in the postvendor."""
        if not chunk_words:
            return False
        L = len(chunk_words)
        first = chunk_words[0]
        for p in post_index.get(first, ()):
            if post_words[p:p + L] == chunk_words:
                return True
        return False

    used = [False] * len(del_positions)

    def claim_marker(lo, hi):
        """Claim (once) any unused <trace.deleted/> marker inside [lo, hi]."""
        left = bisect.bisect_left(del_positions, lo)
        right = bisect.bisect_right(del_positions, hi)
        for k in range(left, right):
            if not used[k]:
                used[k] = True
                return True
        return False

    def _reworded_ratio(pre_chunk, post_chunk):
        """
        Character-level similarity (0..1) between the removed prevendor text and
        the text that replaced it in the postvendor. Comparing characters (not
        word tokens) means a near-identical reword such as "6.100" -> "6.109"
        scores high, while a genuine swap like "litigants" -> "parties" stays
        low and is still reported as a real change.
        """
        if not pre_chunk or not post_chunk:
            return 0.0
        a = " ".join(pre_chunk)
        b = " ".join(post_chunk)
        return difflib.SequenceMatcher(a=a, b=b, autojunk=False).ratio()

    sm = difflib.SequenceMatcher(a=pre_words, b=post_words, autojunk=False)
    n_post = len(post_text)
    red_ranges = []
    expected_ranges = []
    moved_ranges = []
    reworded_ranges = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("delete", "replace") and i2 > i1:
            cs = pre_toks[i1][1]
            ce = pre_toks[i2 - 1][2]
            if not pre_text[cs:ce].strip():
                continue
            # window in the postvendor spanning from the last surviving word
            # BEFORE the change to the first surviving word AFTER it, so a
            # <trace.deleted/> marker sitting anywhere in a replaced region is found
            prev_end = post_toks[j1 - 1][2] if j1 - 1 >= 0 else 0
            next_start = post_toks[j2][1] if j2 < len(post_toks) else n_post
            pre_chunk = pre_words[i1:i2]
            if claim_marker(prev_end - marker_slack, next_start + marker_slack):
                # A <trace.deleted/> marker explains this removal -> expected.
                expected_ranges.append((cs, ce))
            elif _still_present(pre_chunk):
                # The exact text still exists elsewhere in the postvendor, so it
                # was MOVED / reordered, not lost. Do NOT flag it as red.
                moved_ranges.append((cs, ce))
            elif (tag == "replace" and j2 > j1 and
                  _reworded_ratio(pre_chunk, post_words[j1:j2]) >= 0.6):
                # The text at this position was replaced in place by very similar
                # text (e.g. "6.100" -> "6.109"): a REWORD, not a true loss.
                # Kept separate so the red list stays tight.
                reworded_ranges.append((cs, ce))
            else:
                # Genuinely missing, with no marker to explain it -> real loss.
                red_ranges.append((cs, ce))

    return {
        "red_ranges": merge_ranges(red_ranges),
        "expected_ranges": merge_ranges(expected_ranges),
        "moved_ranges": merge_ranges(moved_ranges),
        "reworded_ranges": merge_ranges(reworded_ranges),
        "insertion_ranges": merge_ranges(insertion_ranges),
        "num_markers": len(del_positions),
        "num_markers_matched": sum(used),
        "num_red": len(red_ranges),
        "num_expected": len(expected_ranges),
        "num_moved": len(moved_ranges),
        "num_reworded": len(reworded_ranges),
        "pre_words": len(pre_words),
        "post_words": len(post_words),
    }


# ----------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------

def _dump_debug(report_path, suffix, text):
    base, _ = os.path.splitext(report_path)
    path = base + suffix
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def validate_content_loss(docx_path, final_xml_path,
                          report_path="content_loss_unchanged_report.txt",
                          docx_parts=DEFAULT_DOCX_PARTS,
                          write_debug_text=True):

    # STEP 1
    docx_text, used_parts = read_docx_unchanged_text(docx_path, docx_parts)
    # STEP 2 + 3
    final_root = load_xml_file(final_xml_path)
    final_text = extract_final_baseline_text(final_root)

    if write_debug_text:
        _dump_debug(report_path, "_docx_unchanged.txt", docx_text)
        _dump_debug(report_path, "_final_stripped.txt", final_text)

    # STEP 4 + 5
    drops, extras, ratio, a_words, b_words = find_content_loss(docx_text, final_text)

    dropped_word_count = sum(r["word_range"][1] - r["word_range"][0] for r in drops)

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------
    with open(report_path, "w", encoding="utf-8") as r:
        def w(line=""):
            r.write(line + "\n")

        w("=== CONTENT-LOSS REPORT (DOCX unchanged text vs final XML) ===\n")
        w(f"DOCX file                 : {docx_path}")
        w(f"DOCX parts read           : {', '.join(used_parts) or '(none found!)'}")
        w(f"Final XML file            : {final_xml_path}\n")

        w(f"DOCX unchanged words      : {len(a_words)}")
        w(f"Final baseline words      : {len(b_words)}")
        w(f"Word-stream similarity    : {ratio:.4f}")
        w(f"Dropped segments          : {len(drops)}  ({dropped_word_count} words)")
        w(f"Extra segments in final   : {len(extras)}\n")

        w("---- 🚩 CONTENT DROP (present in DOCX unchanged text, MISSING in final XML) ----\n")
        if not drops:
            w("None found. ✅  All unchanged DOCX text is present in the final XML.\n")
        else:
            for idx, rec in enumerate(drops, 1):
                w(f"[{idx}] Missing text: {rec['text']!r}")
                w(f"    DOCX word range : {rec['word_range']}")
                w(f"    DOCX char range : {rec['char_range']}")
                w(f"    ...{rec['context_before']} [[MISSING]] {rec['context_after']}...")
                w()

        w("---- ℹ️  EXTRA TEXT IN FINAL (present in final baseline, not in DOCX unchanged text) ----")
        w("     (not necessarily an error: leftover deletion text, moved text, "
          "formatting artefacts, or DOCX parts that were not read)\n")
        if not extras:
            w("None found. ✅\n")
        else:
            for idx, rec in enumerate(extras, 1):
                w(f"[{idx}] Extra text: {rec['text']!r}  (final words {rec['word_range']})")
            w()

        w("---- SUMMARY ----")
        if drops:
            w(f"⚠️  ACTION NEEDED: {len(drops)} segment(s) ({dropped_word_count} words) of "
              f"unchanged content were dropped from the final XML.")
        else:
            w("✅ No content drop detected: every unchanged word from the DOCX is present "
              "in the final XML baseline.")

    print(f"DOCX unchanged words : {len(a_words)}")
    print(f"Final baseline words : {len(b_words)}")
    print(f"Content drops        : {len(drops)}  ({dropped_word_count} words)")
    print(f"Extra-in-final       : {len(extras)}")
    print(f"Report written to    : {report_path}")

    return {
        "drops": drops,
        "extras": extras,
        "ratio": ratio,
        "docx_word_count": len(a_words),
        "final_word_count": len(b_words),
    }


# ----------------------------------------------------------------------
if __name__ == "__main__":
    # ---- EDIT THESE PATHS (or pass them on the command line) ----
    DOCX_FILE  = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\test13\anamika\CDN_MA_LCCIC 7 2026-1 update.doc"
    FINAL_XML  = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\test13\anamika\testing\CDN_MA_LCCIC 7 (chapter and all children)_20260702_161505_final.pxml"
    REPORT_TXT = "content_loss_unchanged_report.txt"

    if len(sys.argv) >= 3:
        DOCX_FILE = sys.argv[1]
        FINAL_XML = sys.argv[2]
        if len(sys.argv) >= 4:
            REPORT_TXT = sys.argv[3]

    validate_content_loss(DOCX_FILE, FINAL_XML, REPORT_TXT)
