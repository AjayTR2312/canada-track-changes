"""
Streamlit app: Compare Pre-Vendor XML vs Post-Vendor XML (fast, for large files).

- Upload PRE-VENDOR XML  (left)
- Upload POST-VENDOR XML (right)
- All XML tags are stripped, whitespace normalized, then split into SENTENCES
  so the diff runs on ~thousands of units instead of ~hundreds-of-thousands of
  words (orders of magnitude faster on 400k-word documents).
- Click Compare -> SIDE-BY-SIDE view:
    * content present in PRE  AND  POST            -> not marked
    * content present in PRE  but MISSING in POST  -> marked RED on the PRE (left) side
    * content only in POST (newly added)           -> light green on the POST (right) side
- Long runs of identical sentences are COLLAPSED so the page renders instantly.
- A bullet list of all "missing in post-vendor" content is shown below.

Usage: streamlit run tools/prevendor_postvendor_compare.py
"""
import re
import os
import sys
import html
import tempfile
import difflib

import streamlit as st
import streamlit.components.v1 as components

# Reuse the project's tracked-change extractor. It already pulls every author
# DELETION together with the paragraph it belongs to (inline_deletion /
# paragraph_deletion / the deleted side of inline_replacement), so we don't have
# to re-parse the DOCX by hand.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
try:
    from canada_pipeline.track_changes_df import DocxChangeExtractor
except Exception:  # pragma: no cover - tool still runs without DOCX support
    DocxChangeExtractor = None


# ─── XML Text Extraction ─────────────────────────────────────────────────────

_ENTITY_MAP = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&apos;": "'", "&quot;": '"',
    "&mdash;": "\u2014", "&ndash;": "\u2013",
    "&ldquo;": "\u201c", "&rdquo;": "\u201d",
    "&lsquo;": "\u2018", "&rsquo;": "\u2019",
    "&sect;": "\u00a7", "&para;": "\u00b6", "&bull;": "\u2022",
    "&hellip;": "\u2026", "&percnt;": "%", "&num;": "#", "&dollar;": "$",
    "&nbsp;": " ", "&emsp;": " ", "&ensp;": " ", "&thinsp;": " ",
    "&copy;": "\u00a9", "&reg;": "\u00ae", "&trade;": "\u2122",
    "&times;": "\u00d7", "&deg;": "\u00b0", "&plusmn;": "\u00b1",
}


def extract_xml_text(xml_content: str) -> str:
    """Strip ALL XML tags, returning only plain text (entities decoded)."""
    text = re.sub(r"<\?xml[^>]*\?>", "", xml_content)
    text = re.sub(r"<!DOCTYPE[^>]*>", "", text)
    text = re.sub(r"<\?[^>]*\?>", "", text)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)   # drop comments
    text = re.sub(r"<[^>]+>", " ", text)                       # remove all tags

    for ent, char in _ENTITY_MAP.items():
        text = text.replace(ent, char)
    text = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), text)
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    return text


def normalize_text(text: str) -> str:
    """Collapse all whitespace to single spaces so only content differences show."""
    return re.sub(r"\s+", " ", text).strip()


def split_sentences(text: str) -> list:
    """
    Split normalized text into sentence-like units.
    This turns a 400k-word blob into a few thousand units, which makes the
    diff dramatically faster. Splits after . ! ? ; followed by whitespace.
    Very long unpunctuated runs are capped into ~40-word chunks.
    """
    if not text:
        return []
    parts = re.split(r"(?<=[.!?;])\s+", text)
    units = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        words = p.split()
        if len(words) > 60:
            for k in range(0, len(words), 40):
                units.append(" ".join(words[k:k + 40]))
        else:
            units.append(p)
    return units


# ─── Cached heavy computation ────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def prepare_units(raw_text: str) -> list:
    """Extract -> normalize -> sentence-split, cached on the raw upload text."""
    return split_sentences(normalize_text(extract_xml_text(raw_text)))


@st.cache_data(show_spinner=False)
def compute_opcodes(pre_units: tuple, post_units: tuple) -> list:
    """
    Run SequenceMatcher on SENTENCE units with autojunk enabled (fast).
    Cached so re-renders don't recompute. Returns list of opcodes.
    """
    sm = difflib.SequenceMatcher(None, pre_units, post_units, autojunk=True)
    return sm.get_opcodes()


# ─── Author deletions via the project's DocxChangeExtractor ──────────────────
#
# Matching by deleted TEXT alone is unreliable: the same words may appear in many
# places, so a global text/n-gram match can't tell whether THIS missing segment
# was actually the one the author struck off. Worse, exact character matching
# fails because the PXML decodes entities/smart-quotes while the DOCX keeps them.
#
# Instead we reuse DocxChangeExtractor, which already walks the tracked changes
# and emits each DELETION as a record carrying the deleted text AND the paragraph
# it belongs to (paragraph_index + prev/post context). We then align the
# PRE-VENDOR sentences against the POOL OF STRUCK-OFF TEXT only: every token in
# that pool is, by definition, something the author removed, so a missing
# PRE-VENDOR sentence that aligns to it was an intended deletion — not a real
# content drop. This sidesteps the "wrong duplicate occurrence" problem entirely.


def _looks_like_docx(data: bytes) -> bool:
    """DOCX is a ZIP archive; legacy .doc is an OLE binary (starts with D0 CF 11 E0)."""
    return data[:2] == b"PK"


def convert_doc_to_docx_bytes(doc_bytes: bytes) -> bytes:
    """
    Convert a legacy binary .doc to .docx using Word COM automation (Windows +
    Microsoft Word required). Returns the .docx bytes. Raises on failure.
    """
    try:
        import win32com.client
    except Exception as e:
        raise RuntimeError(
            "Reading a legacy .doc requires Microsoft Word + pywin32 on Windows. "
            "Please convert the file to .docx in Word (File → Save As → .docx) and "
            f"upload that instead. (import error: {e})"
        )

    tmpdir = tempfile.mkdtemp(prefix="docconv_")
    in_path = os.path.join(tmpdir, "input.doc")
    out_path = os.path.join(tmpdir, "output.docx")
    with open(in_path, "wb") as f:
        f.write(doc_bytes)

    word = None
    doc = None
    try:
        # Dynamic dispatch avoids gen_py CLSIDToClassMap errors (same as pipeline).
        try:
            word = win32com.client.dynamic.Dispatch("Word.Application")
        except Exception:
            word = win32com.client.Dispatch("Word.Application")
        word.Visible = False
        word.DisplayAlerts = False

        doc = word.Documents.Open(in_path, ReadOnly=False, Visible=False,
                                  ConfirmConversions=False, AddToRecentFiles=False)
        # 16 = wdFormatXMLDocument (.docx). Preserves tracked changes.
        doc.SaveAs2(out_path, FileFormat=16)
        doc.Close(SaveChanges=False)
        doc = None
    finally:
        try:
            if doc is not None:
                doc.Close(SaveChanges=False)
        except Exception:
            pass
        try:
            if word is not None:
                word.Quit()
        except Exception:
            pass

    with open(out_path, "rb") as f:
        return f.read()


@st.cache_data(show_spinner="Converting .doc to .docx via Word…")
def load_docx_bytes(uploaded_bytes: bytes, filename: str) -> bytes:
    """
    Return .docx bytes from an uploaded file, converting legacy .doc if needed.
    Cached on the raw upload bytes so conversion runs only once.
    """
    if _looks_like_docx(uploaded_bytes):
        return uploaded_bytes
    # Legacy binary .doc (or a .doc-renamed file) → convert via Word.
    return convert_doc_to_docx_bytes(uploaded_bytes)


@st.cache_data(show_spinner="Reading author deletions from DOCX…")
def extract_author_deletions(docx_bytes: bytes):
    """
    Pull every tracked DELETION the author made, together with the paragraph it
    belongs to, using the project's DocxChangeExtractor.

    Returns list[dict]: {text, paragraph_index, section, prev_context,
    post_context, type}. Only deletion-type changes are kept:
        inline_deletion     – words struck inside a paragraph
        paragraph_deletion  – a whole paragraph struck
        inline_replacement  – the struck (deleted_text) side of a replacement
    """
    if DocxChangeExtractor is None:
        return []

    tmpdir = tempfile.mkdtemp(prefix="docxchg_")
    path = os.path.join(tmpdir, "doc.docx")
    with open(path, "wb") as f:
        f.write(docx_bytes)

    try:
        changes, _paragraphs = DocxChangeExtractor(path).extract_changes()
    except Exception:
        return []

    del_types = {"inline_deletion", "paragraph_deletion", "inline_replacement"}
    deletions = []
    for c in changes:
        if c.get("type") not in del_types:
            continue
        txt = c.get("deleted_text") or ""
        # DocxChangeExtractor wraps runs in <ital>/<bold> tags — drop them so the
        # tokens line up with the tag-stripped pre-vendor text.
        txt = re.sub(r"<[^>]+>", " ", txt)
        txt = re.sub(r"\s+", " ", txt).strip()
        if not txt:
            continue
        deletions.append({
            "text": txt,
            "paragraph_index": c.get("paragraph_index"),
            "section": c.get("section"),
            "prev_context": c.get("prev_context", "") or "",
            "post_context": c.get("post_context", "") or "",
            "type": c.get("type"),
        })
    return deletions


_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _tokenize(text: str):
    """Lowercase alphanumeric tokens — same scheme used for the deletion pool."""
    return _WORD_RE.findall(text.lower())


# How specific each sentence/deletion overlap test is. 3-token shingles are
# specific enough to avoid coincidental matches but tolerant of small edits.
_SHINGLE_N = 3


def _shingles(tokens, n=_SHINGLE_N):
    """Set of contiguous n-token tuples (shingles) for `tokens`."""
    if len(tokens) < n:
        return set()
    return {tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}


@st.cache_data(show_spinner=False)
def retrieve_deletion_candidates(pre_units, deletion_texts):
    """
    RETRIEVAL ONLY. For every PRE-VENDOR sentence, find its NEAREST author
    deletion (the struck-off snippet with the highest token-shingle overlap) and
    the overlap fraction.

    This narrows each missing sentence down to the single most relevant deletion
    so the LLM can make a focused yes/no decision without us shipping the whole
    deletion pool on every call. It is deliberately NOT the final decision — the
    LLM (or, as a fallback, a threshold) decides using these candidates.

    Returns list per pre unit of (best_text, frac, best_del_idx):
      best_text   : nearest deletion's text ('' if nothing overlaps)
      frac        : fraction of the sentence's shingles found in that deletion
      best_del_idx: index into deletion_texts (-1 if none)
    """
    del_shingle_sets, del_token_sets = [], []
    inv = {}  # shingle -> list[deletion idx]   (inverted index for fast lookup)
    for di, dt in enumerate(deletion_texts):
        toks = _tokenize(dt)
        sset = _shingles(toks)
        del_shingle_sets.append(sset)
        del_token_sets.append(set(toks))
        for s in sset:
            inv.setdefault(s, []).append(di)

    out = []
    for u in pre_units:
        toks = _tokenize(u)
        if not toks:
            out.append(("", 0.0, -1))
            continue
        if len(toks) >= _SHINGLE_N:
            seg_sh = set(_shingles(toks))
            cand = set()
            for s in seg_sh:
                cand.update(inv.get(s, ()))
            best_idx, best_frac = -1, 0.0
            for di in cand:
                ov = len(seg_sh & del_shingle_sets[di]) / len(seg_sh)
                if ov > best_frac:
                    best_frac, best_idx = ov, di
            out.append((deletion_texts[best_idx], best_frac, best_idx)
                       if best_idx >= 0 else ("", 0.0, -1))
        else:
            # Too short to shingle — score by token membership instead.
            seg_tok = set(toks)
            best_idx, best_frac = -1, 0.0
            for di, dts in enumerate(del_token_sets):
                ov = len(seg_tok & dts) / len(seg_tok)
                if ov > best_frac:
                    best_frac, best_idx = ov, di
            out.append((deletion_texts[best_idx], best_frac, best_idx)
                       if best_idx >= 0 and best_frac > 0 else ("", 0.0, -1))
    return out


# ─── LLM judge (reuses the project's Anthropic Sonnet client) ─────────────────

@st.cache_resource(show_spinner="Connecting to the LLM…")
def _get_llm_client():
    """Build (and session-cache) the project's Anthropic client. Returns
    (client, model_name). Raises if credentials are unavailable."""
    from canada_pipeline.config import build_anthropic_client, MODEL_SONNET
    client = build_anthropic_client(MODEL_SONNET)
    return client, MODEL_SONNET


def _llm_complete(client, model, prompt, max_tokens=1500):
    """One Anthropic completion with a couple of rate-limit retries."""
    import time
    for attempt in range(3):
        try:
            msg = client.messages.create(
                model=model, max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return msg.content[0].text
        except Exception as e:
            s = str(e).lower()
            if attempt < 2 and ("rate" in s or "429" in s or "overload" in s):
                time.sleep(8 * (attempt + 1))
                continue
            raise
    return ""


_DELETED_RE = re.compile(r'"?deleted"?\s*:\s*\[([^\]]*)\]', re.IGNORECASE)


def _parse_deleted_indices(text):
    """Pull integer indices out of a {"deleted":[...]} reply, tolerating any
    reasoning preamble. Returns an empty set if the JSON can't be found (the
    safe default: when unsure, DON'T ignore content)."""
    m = _DELETED_RE.search(text or "")
    if not m:
        return set()
    return {int(n) for n in re.findall(r"\d+", m.group(1))}


@st.cache_data(show_spinner=False)
def llm_filter_author_deletions(items):
    """
    items: tuple of (missing_text, candidate_deletion_text). Ask the LLM which of
    the MISSING segments are genuinely the author's struck-off content (vs a real
    content drop). Returns a set of indices (into `items`) judged YES.

    Cached on `items` so re-renders don't re-call the model.
    """
    if not items:
        return set()
    client, model = _get_llm_client()
    deleted = set()
    BATCH = 20
    for start in range(0, len(items), BATCH):
        batch = items[start:start + BATCH]
        lines = []
        for i, (miss, cand) in enumerate(batch):
            miss = (miss or "").replace('"', "'")
            cand = (cand or "").replace('"', "'")
            if len(miss) > 600:
                miss = miss[:600] + "…"
            if len(cand) > 600:
                cand = cand[:600] + "…"
            lines.append(f'[{i}]\n  MISSING: "{miss}"\n  AUTHOR_DELETION: "{cand}"')
        prompt = (
            "You are auditing why text is missing from a POST-VENDOR document.\n"
            "Each item has:\n"
            "  MISSING         = text present in the source but absent from the post-vendor file\n"
            "  AUTHOR_DELETION = the nearest text the author intentionally STRUCK OFF "
            "(a tracked deletion)\n\n"
            "For each item decide: was the MISSING text removed BECAUSE the author struck it off?\n"
            "Answer YES if MISSING is essentially the SAME content as AUTHOR_DELETION, allowing "
            "for differences in punctuation, quotes, dashes, whitespace, capitalization, XML "
            "entities, and minor wording.\n"
            "Answer NO if AUTHOR_DELETION is unrelated or shares only a few common/filler words "
            "(meaning the content was really dropped, not intentionally deleted).\n\n"
            "Items:\n" + "\n".join(lines) + "\n\n"
            'Reply with ONLY a JSON object: {"deleted": [item numbers answered YES]}.'
        )
        try:
            resp = _llm_complete(client, model, prompt)
        except Exception:
            continue  # hard failure on this batch -> ignore nothing (safe default)
        for i in _parse_deleted_indices(resp):
            if 0 <= i < len(batch):
                deleted.add(start + i)
    return deleted


# Overlap bands for the retrieval prefilter.
_AUTO_IGNORE = 0.92   # near-verbatim match -> struck for certain, no LLM needed
_MIN_CANDIDATE = 0.12  # below this there's no real candidate -> a real content drop
_FALLBACK_THRESHOLD = 0.55  # used only when the LLM is unavailable


def classify_missing_segments(pre_units, missing_pairs, deletion_texts, use_llm):
    """
    Decide which 'missing in post-vendor' segments were the author's deletions.
    Returns (ignored_idx:set[int], method:str).

    Pipeline:
      1. RETRIEVE each missing segment's nearest author deletion (shingle overlap).
      2. No overlap (< _MIN_CANDIDATE)        -> real drop (kept red).
      3. Near-verbatim (>= _AUTO_IGNORE)      -> author-struck (ignored), no LLM.
      4. Ambiguous middle band                -> the LLM decides (if enabled);
         otherwise a text-overlap threshold is used as a fallback.
    """
    cands = retrieve_deletion_candidates(tuple(pre_units), tuple(deletion_texts))
    ignored = set()
    llm_items, llm_keys = [], []
    for k, seg in missing_pairs:
        best_text, frac, _ = cands[k]
        if frac >= _AUTO_IGNORE:
            ignored.add(k)
        elif frac >= _MIN_CANDIDATE and best_text:
            llm_items.append((seg, best_text))
            llm_keys.append(k)
        # else: nothing close enough -> real content drop

    if not llm_items:
        return ignored, ("llm" if use_llm else "shingle")

    if use_llm:
        try:
            sel = llm_filter_author_deletions(tuple(llm_items))
            for pos in sel:
                ignored.add(llm_keys[pos])
            return ignored, "llm"
        except Exception:
            pass  # creds/network problem -> threshold fallback below

    for pos, k in enumerate(llm_keys):
        if cands[k][1] >= _FALLBACK_THRESHOLD:
            ignored.add(k)
    return ignored, ("shingle-fallback" if use_llm else "shingle")



# ─── Word-level highlight inside a changed (replace) pair ─────────────────────

def _word_diff(pre_unit: str, post_unit: str):
    """Return (left_html, right_html) with word-level red/green highlighting."""
    a = pre_unit.split()
    b = post_unit.split()
    sm = difflib.SequenceMatcher(None, a, b, autojunk=True)
    left, right = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        la = html.escape(" ".join(a[i1:i2]))
        rb = html.escape(" ".join(b[j1:j2]))
        if tag == "equal":
            left.append(la)
            right.append(rb)
        elif tag == "delete":
            left.append(f'<span class="del">{la}</span>')
        elif tag == "insert":
            right.append(f'<span class="ins">{rb}</span>')
        elif tag == "replace":
            left.append(f'<span class="del">{la}</span>')
            right.append(f'<span class="ins">{rb}</span>')
    return " ".join(left), " ".join(right)


# ─── Side-by-Side Builder (collapses long equal runs) ─────────────────────────

EQUAL_CONTEXT = 2          # equal sentences to show around each change
COLLAPSE_MIN = 6           # collapse equal runs longer than this


def build_side_by_side(pre_units, post_units, opcodes, show_only_diffs: bool,
                       ignored_idx=None) -> str:
    rows = []  # (num, left_html, right_html, css_class)
    esc = html.escape
    ignored_idx = ignored_idx or set()

    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            count = i2 - i1
            if show_only_diffs:
                if count:
                    rows.append(("", f'<span class="muted">⋯ {count} identical sentence(s) ⋯</span>', "", "collapse"))
                continue
            if count <= COLLAPSE_MIN:
                for k in range(i1, i2):
                    rows.append((k + 1, esc(pre_units[k]), esc(pre_units[k]), "equal"))
            else:
                for k in range(i1, i1 + EQUAL_CONTEXT):
                    rows.append((k + 1, esc(pre_units[k]), esc(pre_units[k]), "equal"))
                hidden = count - 2 * EQUAL_CONTEXT
                msg = f'<span class="muted">⋯ {hidden} identical sentence(s) hidden ⋯</span>'
                rows.append(("", msg, msg, "collapse"))
                for k in range(i2 - EQUAL_CONTEXT, i2):
                    rows.append((k + 1, esc(pre_units[k]), esc(pre_units[k]), "equal"))

        elif tag == "delete":
            for k in range(i1, i2):
                if k in ignored_idx:
                    rows.append((k + 1, f'<span class="ignored">{esc(pre_units[k])}</span>', "", "ignoredrow"))
                else:
                    rows.append((k + 1, f'<span class="del">{esc(pre_units[k])}</span>', "", "delrow"))

        elif tag == "insert":
            for k in range(j1, j2):
                rows.append(("", "", f'<span class="ins">{esc(post_units[k])}</span>', "insrow"))

        elif tag == "replace":
            span = max(i2 - i1, j2 - j1)
            for off in range(span):
                ka = i1 + off
                a = pre_units[ka] if ka < i2 else ""
                b = post_units[j1 + off] if j1 + off < j2 else ""
                if a and b:
                    lh, rh = _word_diff(a, b)
                    rows.append(("", lh, rh, "reprow"))
                elif a:
                    if ka in ignored_idx:
                        rows.append((ka + 1, f'<span class="ignored">{esc(a)}</span>', "", "ignoredrow"))
                    else:
                        rows.append((ka + 1, f'<span class="del">{esc(a)}</span>', "", "delrow"))
                else:
                    rows.append(("", "", f'<span class="ins">{esc(b)}</span>', "insrow"))

    body_rows = "\n".join(
        f'<tr class="{cls}"><td class="num">{num}</td>'
        f'<td class="left">{l}</td><td class="right">{r}</td></tr>'
        for (num, l, r, cls) in rows
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; }}
  table {{ border-collapse: collapse; width: 100%; table-layout: fixed; font-size: 13px; }}
  th {{ position: sticky; top: 0; background: #2c3e50; color: #fff; padding: 8px;
        text-align: left; z-index: 2; }}
  td {{ vertical-align: top; padding: 6px 10px; border-bottom: 1px solid #eee;
        word-wrap: break-word; overflow-wrap: anywhere; line-height: 1.55; }}
  td.num {{ width: 52px; color: #aaa; text-align: right; font-size: 11px;
            background: #fafafa; user-select: none; }}
  td.left, td.right {{ width: 48%; }}
  td.left {{ border-right: 2px solid #ddd; }}
  .del {{ background: #ffd6d6; color: #b30000; text-decoration: line-through; }}
  .ins {{ background: #d6ffd6; color: #006600; font-weight: 600; }}
  .muted {{ color: #999; font-style: italic; font-size: 12px; }}
  .ignored {{ background: #ececec; color: #999; text-decoration: line-through; }}
  tr.delrow td.left {{ background: #fff5f5; }}
  tr.insrow td.right {{ background: #f5fff5; }}
  tr.reprow td.left {{ background: #fff5f5; }}
  tr.reprow td.right {{ background: #f5fff5; }}
  tr.collapse td {{ background: #f4f6f8; text-align: center; }}
  tr.ignoredrow td.left {{ background: #f6f6f6; }}
</style></head><body>
<table>
  <thead><tr><th class="num">#</th><th>PRE-VENDOR</th><th>POST-VENDOR</th></tr></thead>
  <tbody>
  {body_rows}
  </tbody>
</table>
</body></html>"""


# ─── Missing-content extraction (PRE present, POST absent) ────────────────────

def get_missing_segments(pre_units, post_units, opcodes) -> list:
    """(index, text) for sentences in PRE-VENDOR missing in POST-VENDOR (delete + replace 'from')."""
    missing = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag in ("delete", "replace"):
            for k in range(i1, i2):
                if pre_units[k].strip():
                    missing.append((k, pre_units[k].strip()))
    return missing


# ─── Stats ───────────────────────────────────────────────────────────────────

def compute_stats(pre_units, post_units, opcodes) -> dict:
    missing = added = 0
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "delete":
            missing += i2 - i1
        elif tag == "insert":
            added += j2 - j1
        elif tag == "replace":
            missing += i2 - i1
            added += j2 - j1
    matcher = difflib.SequenceMatcher(None, pre_units, post_units, autojunk=True)
    return {
        "similarity": matcher.ratio() * 100,
        "units_pre": len(pre_units),
        "units_post": len(post_units),
        "missing": missing,
        "added": added,
    }


# ─── Streamlit App ───────────────────────────────────────────────────────────

def main():
    st.set_page_config(page_title="Pre vs Post Vendor XML Compare", page_icon="🔍", layout="wide")

    st.title("🔍 Pre-Vendor vs Post-Vendor XML Comparison")
    st.caption("Optimized for large files: tags stripped, whitespace normalized, diffed at the "
               "sentence level. Content present in PRE-VENDOR but missing in POST-VENDOR is marked red.")

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("📄 Pre-Vendor XML")
        pre_file = st.file_uploader("Upload PRE-VENDOR XML", type=["xml", "pxml"], key="pre")
    with col2:
        st.subheader("📄 Post-Vendor XML")
        post_file = st.file_uploader("Upload POST-VENDOR XML", type=["xml", "pxml"], key="post")

    st.markdown("##### 📝 (Optional) Author's DOCX — to ignore struck-off deletions")
    docx_file = st.file_uploader(
        "Upload the tracked-changes DOCX (or legacy .doc). Content the author STRUCK OFF "
        "(deleted) will be treated as intended and excluded from the 'missing' report. "
        ".doc files are auto-converted via Microsoft Word.",
        type=["docx", "doc"], key="docx",
    )

    opt1, opt2 = st.columns(2)
    with opt1:
        show_only_diffs = st.checkbox(
            "⚡ Show only differences (hide matching content)", value=True,
        )
    with opt2:
        ignore_author_deletions = st.checkbox(
            "🚫 Ignore author deletions (from DOCX)", value=True,
        )

    use_llm = st.checkbox(
        "🤖 Use the LLM (Claude) to judge deletions — recommended",
        value=True,
        help="For each missing segment, the project's Anthropic model compares it "
             "against the nearest author deletion and decides whether it was struck "
             "off. Robust to punctuation, quotes, dashes and XML-entity differences. "
             "Requires the same Anthropic/TR credentials the pipeline uses.",
    )

    pre_units = post_units = []

    if pre_file:
        pre_units = prepare_units(pre_file.read().decode("utf-8", errors="replace"))
        with col1:
            st.success(f"✅ {len(pre_units):,} sentence units")

    if post_file:
        post_units = prepare_units(post_file.read().decode("utf-8", errors="replace"))
        with col2:
            st.success(f"✅ {len(post_units):,} sentence units")

    author_deletions = None
    if docx_file is not None:
        if DocxChangeExtractor is None:
            st.error("❌ The DOCX change extractor (canada_pipeline.track_changes_df) "
                     "could not be imported, so author deletions can't be read.")
        else:
            try:
                _docx_bytes = load_docx_bytes(docx_file.read(), docx_file.name)
                author_deletions = extract_author_deletions(_docx_bytes)
                _kind = "DOCX" if docx_file.name.lower().endswith(".docx") else "DOC (converted)"
                _struck_words = sum(len(_tokenize(d["text"])) for d in author_deletions)
                st.caption(f"✂️ Loaded author edits from **{docx_file.name}** [{_kind}] — "
                           f"{len(author_deletions):,} tracked deletion(s) "
                           f"(~{_struck_words:,} struck words).")
                if not author_deletions:
                    st.warning("⚠️ No tracked deletions were found in this file. Make sure the "
                               "track changes are still present (not already accepted) and that "
                               "you uploaded the AUTHOR's marked-up copy.")
                else:
                    with st.expander("🔎 Preview detected author deletions (with paragraph)"):
                        st.caption(f"{len(author_deletions):,} deletion(s). Showing first 30:")
                        for d in author_deletions[:30]:
                            _pi = d.get("paragraph_index")
                            _sec = d.get("section") or ""
                            _loc = f"¶{_pi}" + (f" · {_sec}" if _sec else "")
                            st.markdown(f"~~{d['text']}~~  \n<span style='color:#888;font-size:11px'>"
                                        f"{_loc} · {d['type']}</span>", unsafe_allow_html=True)
            except Exception as e:
                st.error(f"❌ Could not read the author file: {e}")
                author_deletions = None

    st.divider()

    if not (pre_file and post_file):
        st.info("⬆️ Upload both PRE-VENDOR and POST-VENDOR XML files, then click **Compare**.")
        return

    if st.button("🔄 Compare", type="primary", use_container_width=True):
        with st.spinner("Computing differences (sentence-level)…"):
            opcodes = compute_opcodes(tuple(pre_units), tuple(post_units))
            stats = compute_stats(pre_units, post_units, opcodes)

        # Classify missing PRE-VENDOR content into real-missing vs author-struck.
        missing_pairs = get_missing_segments(pre_units, post_units, opcodes)  # (idx, text)
        use_docx = bool(author_deletions) and ignore_author_deletions
        ignored_idx = set()
        classify_method = ""
        if use_docx:
            _spin = ("Asking the LLM which missing content the author struck off…"
                     if use_llm else "Matching missing content against author deletions…")
            with st.spinner(_spin):
                ignored_idx, classify_method = classify_missing_segments(
                    pre_units, missing_pairs,
                    tuple(d["text"] for d in author_deletions), use_llm)
            if use_llm and classify_method != "llm":
                st.warning("⚠️ Could not reach the LLM — fell back to text-overlap "
                           "matching. Check the Anthropic/TR credentials in this "
                           "environment.")
        real_missing = [seg for k, seg in missing_pairs if k not in ignored_idx]
        intended_deletions = [seg for k, seg in missing_pairs if k in ignored_idx]

        m = st.columns(5)
        m[0].metric("Similarity", f"{stats['similarity']:.1f}%")
        m[1].metric("Units (PRE)", f"{stats['units_pre']:,}")
        m[2].metric("Units (POST)", f"{stats['units_post']:,}")
        m[3].metric("Real missing", f"{len(real_missing):,}")
        m[4].metric("Author-struck (ignored)", f"{len(intended_deletions):,}")

        st.markdown(
            "**Legend:** "
            "<span style='background:#ffd6d6;color:#b30000;text-decoration:line-through;padding:2px 6px;'>red</span>"
            " = missing in POST-VENDOR (real) &nbsp;&nbsp; "
            "<span style='background:#ececec;color:#999;text-decoration:line-through;padding:2px 6px;'>gray</span>"
            " = author struck off in DOCX (ignored) &nbsp;&nbsp; "
            "<span style='background:#d6ffd6;color:#006600;font-weight:600;padding:2px 6px;'>green</span>"
            " = only in POST-VENDOR",
            unsafe_allow_html=True,
        )

        st.subheader("📋 Side-by-Side Comparison")
        with st.spinner("Rendering…"):
            html_doc = build_side_by_side(pre_units, post_units, opcodes,
                                          show_only_diffs, ignored_idx)
        components.html(html_doc, height=700, scrolling=True)

        # ── Real missing content (NOT explained by author deletions) ──
        st.divider()
        st.subheader(f"❌ Real Content Missing in Post-Vendor ({len(real_missing):,} segment(s))")
        if not real_missing:
            st.success("🎉 No unexplained content is missing in POST-VENDOR.")
        else:
            MAX_SHOW = 500
            for i, seg in enumerate(real_missing[:MAX_SHOW], 1):
                st.markdown(f"**{i}.** {seg}")
            if len(real_missing) > MAX_SHOW:
                st.warning(f"Showing first {MAX_SHOW:,} of {len(real_missing):,}. "
                           f"Download the full report below.")
            report = "\n".join(f"{i}. {seg}" for i, seg in enumerate(real_missing, 1))
            st.download_button(
                "⬇️ Download real-missing report (.txt)",
                data=report.encode("utf-8"),
                file_name="real_missing_in_postvendor.txt",
                mime="text/plain",
            )

        # ── Author-struck content that was ignored ──
        if use_docx:
            with st.expander(f"🚫 Author-struck content ignored ({len(intended_deletions):,} segment(s))"):
                if not intended_deletions:
                    st.write("None.")
                else:
                    for i, seg in enumerate(intended_deletions[:500], 1):
                        st.markdown(f"~~{i}. {seg}~~")
                    if len(intended_deletions) > 500:
                        st.caption(f"…and {len(intended_deletions) - 500:,} more.")
        elif not author_deletions:
            st.caption("ℹ️ Upload the author's DOCX above to auto-ignore struck-off deletions.")


if __name__ == "__main__":
    main()
