"""
PXML Content-Drop Comparator
============================================================
Single-file Streamlit app to compare a PRE-VENDOR pxml against a
POST-VENDOR pxml (which contains tracked-change tags) and highlight — in RED —
any REAL content drop: prose that existed in the pre-vendor document but has
silently disappeared from the post-vendor document.

Tracked deletions (<trace.deleted>...<trace.deleted/>) are NOT drops — the text
is still present (struck-through) and intentional. Tracked insertions
(<trace>...</trace>) are NEW text that did not exist in the pre-vendor.

Effective "retained original" content of the post-vendor =
    normal text  +  <trace.deleted> text        (excludes <trace> insertions)

If a pre-vendor word is missing from that retained-original stream, it is a
REAL CONTENT DROP and is flagged red.

Run:
    streamlit run pxml_content_drop_compare.py
"""

import html
import re
import difflib
import streamlit as st

# ── Default paths (from the user's request) ──────────────────────────────────
DEFAULT_PRE = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\Test 17 (July 14, 2026)\CDN_OO_OW Ch 3\CDN_OO_OW 3 (chapter and all children).pxml"
DEFAULT_POST = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\Test 17 (July 14, 2026)\CDN_OO_OW Ch 3 - Copy\save_before_postprocessing.pxml"


# ─────────────────────────────────────────────────────────────────────────────
# Text extraction helpers
# ─────────────────────────────────────────────────────────────────────────────
_ENTITY_MAP = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&apos;": "'", "&quot;": '"',
    "&ldquo;": '"', "&rdquo;": '"', "&lsquo;": "'", "&rsquo;": "'",
    "&mdash;": "\u2014", "&ndash;": "\u2013", "&hellip;": "...",
    "&sect;": "\u00a7", "&dblsect;": "\u00a7\u00a7", "&para;": "\u00b6",
    "&bull;": "\u2022", "&dollar;": "$", "&percnt;": "%", "&num;": "#",
    "&ast;": "*", "&hyphen;": "-", "&lsqb;": "[", "&rsqb;": "]", "&nbsp;": " ",
    # accented letters common in legal text (Québec, café, etc.)
    "&eacute;": "\u00e9", "&egrave;": "\u00e8", "&ecirc;": "\u00ea",
    "&agrave;": "\u00e0", "&acirc;": "\u00e2", "&ccedil;": "\u00e7",
    "&icirc;": "\u00ee", "&iuml;": "\u00ef", "&ocirc;": "\u00f4",
    "&ucirc;": "\u00fb", "&uuml;": "\u00fc", "&ouml;": "\u00f6",
    "&auml;": "\u00e4", "&Eacute;": "\u00c9", "&deg;": "\u00b0",
    "&frac12;": "\u00bd", "&frac14;": "\u00bc", "&frac34;": "\u00be",
    "&copy;": "\u00a9", "&reg;": "\u00ae", "&trade;": "\u2122",
    "&times;": "\u00d7", "&middot;": "\u00b7",
}


def _decode_entities(text: str) -> str:
    for ent, ch in _ENTITY_MAP.items():
        text = text.replace(ent, ch)
    # numeric entities
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    text = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), text)
    return text


def strip_all_tags(xml: str) -> str:
    """Remove every XML tag, decode entities, normalize whitespace → plain text."""
    no_tags = re.sub(r"<[^>]+>", " ", xml)
    decoded = _decode_entities(no_tags)
    return re.sub(r"\s+", " ", decoded).strip()


def remove_trace_insertions(xml: str) -> str:
    """
    Return the post-vendor XML with <trace>...</trace> INSERTIONS removed
    (that text is NEW, not in pre-vendor), but KEEPING <trace.deleted> content
    (that text is retained/struck-through, not a drop).

    NOTE: <trace.deleted> uses a self-closing end tag <trace.deleted/>, whereas
    <trace> (insertion) uses a normal </trace> end tag. We must remove ONLY the
    insertion <trace>...</trace> pairs and never touch trace.deleted.
    """
    # First, protect trace.deleted blocks by masking them.
    placeholders = {}

    def _mask(m):
        key = f"\x00DEL{len(placeholders)}\x00"
        placeholders[key] = m.group(0)
        return key

    # trace.deleted ... trace.deleted/  (content between opening and self-closing end)
    masked = re.sub(
        r"<trace\.deleted\b[^>]*>.*?<trace\.deleted/>",
        _mask,
        xml,
        flags=re.DOTALL,
    )

    # Now remove insertion trace pairs: <trace ...>...</trace>
    masked = re.sub(r"<trace\b(?![.])[^>]*>.*?</trace>", " ", masked, flags=re.DOTALL)

    # Restore trace.deleted blocks
    for key, val in placeholders.items():
        masked = masked.replace(key, val)
    return masked


def retained_original_text(post_xml: str) -> str:
    """Plain text that the post-vendor still carries from the ORIGINAL document."""
    return strip_all_tags(remove_trace_insertions(post_xml))


def remove_trace_deletions(xml: str) -> str:
    """
    Remove <trace.deleted>...<trace.deleted/> blocks ENTIRELY (their struck-through
    content AND the surrounding tags), leaving nothing behind.

    This models what POSTPROCESSING is *supposed* to do: strip the deleted content
    and keep only the empty <trace.deleted/> marker. Therefore the words that
    SHOULD survive postprocessing = everything EXCEPT the trace.deleted content
    (i.e. normal text  +  <trace> insertions).

    Used for STEP 2 (save_before_postprocessing → final): any word that is present
    here but missing from the FINAL pxml is a REAL DROP caused by the postprocessing
    regex — NOT the intentional business-rule removal of deleted content.
    """
    return re.sub(
        r"<trace\.deleted\b[^>]*>.*?<trace\.deleted/>",
        " ",
        xml,
        flags=re.DOTALL,
    )


def build_before_expected_words(before_xml: str):
    """
    Words expected to SURVIVE postprocessing (STEP 2) =
    normal text  +  <trace> insertions   (EXCLUDES <trace.deleted> content).
    """
    plain = strip_all_tags(remove_trace_deletions(before_xml))
    return [_norm_word(w) for w in tokenize(plain)]


# ─────────────────────────────────────────────────────────────────────────────
# Diff / drop detection (word level) — KEEP TAGS, highlight only real drops
# ─────────────────────────────────────────────────────────────────────────────
_WORD_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)


def tokenize(text: str):
    return _WORD_RE.findall(text)


def _norm_word(w: str) -> str:
    """Normalize a word for matching (decode entities, casefold)."""
    return _decode_entities(w).casefold()


def tokenize_xml_keep_tags(xml: str):
    """
    Split pxml into an ordered list of tokens WITHOUT removing tags.
    Each token is a tuple:
        ('tag',  '<...>')            → an XML tag (shown, never a drop)
        ('word', 'Estates')          → a text word (candidate for drop check)
        ('ws',   ' ')                → whitespace / punctuation gap (shown as-is)
    This preserves the tags so nothing is removed from the pre-vendor view.
    """
    tokens = []
    pos = 0
    for m in re.finditer(r"<[^>]+>", xml):
        # text before this tag
        if m.start() > pos:
            _emit_text_tokens(xml[pos:m.start()], tokens)
        tokens.append(("tag", m.group(0)))
        pos = m.end()
    if pos < len(xml):
        _emit_text_tokens(xml[pos:], tokens)
    return tokens


def _emit_text_tokens(text: str, tokens: list):
    """Break a text run into word / whitespace tokens (entities DECODED first)."""
    # Decode entities up-front so "Qu&eacute;bec" becomes one word "Québec"
    # instead of fragmenting into "Qu" + "&eacute;" + "bec".
    text = _decode_entities(text)
    for piece in re.split(r"(\s+)", text):
        if piece == "":
            continue
        if piece.isspace():
            tokens.append(("ws", piece))
        else:
            for w in re.findall(r"\w+|[^\w\s]", piece, re.UNICODE):
                tokens.append(("word", w))


def build_post_retained_words(post_xml: str):
    """
    Words the post-vendor STILL carries from the original document =
    normal text  +  <trace.deleted> text   (EXCLUDES <trace> insertions).
    A word present here (even if inside trace.deleted) is NOT a drop.
    """
    retained_xml = remove_trace_insertions(post_xml)
    plain = strip_all_tags(retained_xml)
    return [_norm_word(w) for w in tokenize(plain)]


def render_pre_keep_tags_mark_drops(pre_xml: str, post_words: list):
    """
    Render the pre-vendor pxml WITH all tags visible. Only REAL content-drop
    words (present in pre, absent from post-retained) are wrapped in red.
    Returns (html_string, dropped_count, dropped_samples).
    """
    tokens = tokenize_xml_keep_tags(pre_xml)

    # Sequence of pre-vendor words (normalized) + back-references to token idx
    pre_word_idx = [i for i, (k, _) in enumerate(tokens) if k == "word"]
    pre_words = [_norm_word(tokens[i][1]) for i in pre_word_idx]

    # Diff pre-words vs post-retained-words → which pre word positions are drops
    sm = difflib.SequenceMatcher(a=pre_words, b=post_words, autojunk=False)
    is_drop = [False] * len(pre_words)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("delete", "replace"):
            for k in range(i1, i2):
                is_drop[k] = True

    # Map: token index → drop?
    drop_token = {}
    for wi, tok_i in enumerate(pre_word_idx):
        drop_token[tok_i] = is_drop[wi]

    dropped_count = sum(is_drop)
    samples = []

    out = []
    _cur_sample = []

    def _flush_sample():
        if _cur_sample:
            samples.append(" ".join(_cur_sample))
            _cur_sample.clear()

    for i, (kind, val) in enumerate(tokens):
        if kind == "tag":
            _flush_sample()
            # Show tags in muted grey so structure is visible but unobtrusive
            out.append(
                f'<span style="color:#9aa0a6;">{html.escape(val)}</span>'
            )
        elif kind == "ws":
            out.append(html.escape(val))
        else:  # word
            disp = _decode_entities(val)
            # Only flag REAL prose drops: token must contain an alphanumeric
            # character. Pure punctuation/symbols (".", "...", "§") are never
            # counted as content drops (they cause noise, not real loss).
            is_real = drop_token.get(i, False) and bool(re.search(r"\w", disp, re.UNICODE))
            if is_real:
                _cur_sample.append(disp)
                out.append(
                    f'<span style="background:#ffd6d6;color:#b00000;'
                    f'text-decoration:underline;font-weight:600;">'
                    f"{html.escape(disp)}</span>"
                )
            else:
                _flush_sample()
                out.append(html.escape(disp))
    _flush_sample()

    # Recount using the same "real prose" rule so the banner matches the marks
    real_dropped = sum(len(s.split()) for s in samples)
    return "".join(out), real_dropped, samples


def render_before_keep_tags_mark_drops(before_xml: str, final_words: list):
    """
    STEP 2 renderer (save_before_postprocessing → final).

    Render the save_before pxml WITH all tags visible. Words INSIDE
    <trace.deleted>…<trace.deleted/> are shown struck-through but are NEVER
    flagged as drops — they are *supposed* to be removed by postprocessing.

    Only NON-deleted words (normal text + <trace> insertions) are drop
    candidates: if such a word is missing from the FINAL pxml, it is a REAL
    content drop caused by the postprocessing regex, and is highlighted red.

    Returns (html_string, dropped_count, dropped_samples).
    """
    tokens = tokenize_xml_keep_tags(before_xml)

    # Walk tokens tracking trace.deleted depth so we can (a) exclude deleted
    # words from the drop check and (b) style them as struck-through.
    in_del_flags = []          # per-token: is this token inside trace.deleted?
    in_del = 0
    for kind, val in tokens:
        if kind == "tag":
            low = val.lower()
            if re.match(r"<trace\.deleted\b", low) and not low.endswith("/>"):
                in_del_flags.append(in_del)  # the opening tag itself: pre-increment view
                in_del += 1
                continue
            elif low.startswith("<trace.deleted/"):
                in_del = max(0, in_del - 1)
                in_del_flags.append(in_del)
                continue
        in_del_flags.append(in_del)

    # Candidate (drop-checkable) words = words OUTSIDE trace.deleted.
    cand_token_idx = [
        i for i, (k, _) in enumerate(tokens)
        if k == "word" and in_del_flags[i] == 0
    ]
    cand_words = [_norm_word(tokens[i][1]) for i in cand_token_idx]

    # Diff expected-survivor words vs FINAL words.
    sm = difflib.SequenceMatcher(a=cand_words, b=final_words, autojunk=False)
    is_drop = [False] * len(cand_words)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("delete", "replace"):
            for k in range(i1, i2):
                is_drop[k] = True

    drop_token = {}
    for wi, tok_i in enumerate(cand_token_idx):
        drop_token[tok_i] = is_drop[wi]

    samples = []
    _cur_sample = []

    def _flush_sample():
        if _cur_sample:
            samples.append(" ".join(_cur_sample))
            _cur_sample.clear()

    out = []
    for i, (kind, val) in enumerate(tokens):
        if kind == "tag":
            _flush_sample()
            out.append(f'<span style="color:#9aa0a6;">{html.escape(val)}</span>')
        elif kind == "ws":
            out.append(html.escape(val))
        else:  # word
            disp = _decode_entities(val)
            if in_del_flags[i] > 0:
                # Intentionally-removed deleted content — struck, never a drop.
                _flush_sample()
                out.append(
                    f'<span style="color:#b00000;text-decoration:line-through;'
                    f'opacity:0.6;">{html.escape(disp)}</span>'
                )
                continue
            is_real = drop_token.get(i, False) and bool(re.search(r"\w", disp, re.UNICODE))
            if is_real:
                _cur_sample.append(disp)
                out.append(
                    f'<span style="background:#ffd6d6;color:#b00000;'
                    f'text-decoration:underline;font-weight:600;">'
                    f"{html.escape(disp)}</span>"
                )
            else:
                _flush_sample()
                out.append(html.escape(disp))
    _flush_sample()

    real_dropped = sum(len(s.split()) for s in samples)
    return "".join(out), real_dropped, samples
def render_post_with_traces(post_xml: str) -> str:
    """
    Render the RAW post-vendor pxml WITH every tag kept visible (muted grey,
    exactly like the pre-vendor side). Nothing is stripped. On top of that,
    the tracked-change regions are colour-cued so they are easy to spot:
        • <trace.deleted>…<trace.deleted/>  → red strike-through text
        • <trace>…</trace> (insertion)      → green underlined text
    The tags themselves ARE shown so both panes are true XML views.
    """
    # 1) Tokenize keeping tags (reuse the same tokenizer as the pre-vendor pane).
    tokens = tokenize_xml_keep_tags(post_xml)

    # 2) Walk the tokens, tracking whether we are inside a trace / trace.deleted
    #    region so the TEXT inside gets coloured while the tags stay grey.
    out = []
    in_del = 0   # depth inside <trace.deleted> … <trace.deleted/>
    in_ins = 0   # depth inside <trace> … </trace>

    for kind, val in tokens:
        if kind == "tag":
            low = val.lower()
            # Opening / closing detection for the two trace flavours.
            if re.match(r"<trace\.deleted\b", low) and not low.endswith("/>"):
                in_del += 1
            elif low.startswith("<trace.deleted/"):
                in_del = max(0, in_del - 1)
            elif re.match(r"<trace\b", low) and not low.startswith("<trace.") \
                    and not low.endswith("/>"):
                in_ins += 1
            elif low.startswith("</trace>"):
                in_ins = max(0, in_ins - 1)
            # Tag always shown in muted grey.
            out.append(f'<span style="color:#9aa0a6;">{html.escape(val)}</span>')
        elif kind == "ws":
            out.append(html.escape(val))
        else:  # word / text
            disp = html.escape(_decode_entities(val))
            if in_del:
                out.append(
                    f'<span style="color:#b00000;'
                    f'text-decoration:line-through;">{disp}</span>'
                )
            elif in_ins:
                out.append(
                    f'<span style="color:#0a7d00;'
                    f'text-decoration:underline;">{disp}</span>'
                )
            else:
                out.append(disp)
    return "".join(out)


# ─────────────────────────────────────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="PXML Content-Drop Comparator", layout="wide")
st.title("🔍 PXML Content-Drop Comparator")

MODE_STEP1 = "Step 1 · Pre-vendor → Save-before-postprocessing"
MODE_STEP2 = "Step 2 · Save-before-postprocessing → Final"
mode = st.radio(
    "Comparison stage",
    [MODE_STEP1, MODE_STEP2],
    horizontal=True,
    help=(
        "Step 1 checks that original prose survived the vendor/LLM stage "
        "(tracked deletions still count as present).\n"
        "Step 2 checks that POSTPROCESSING did not silently drop non-deleted "
        "content while stripping the <trace.deleted> text."
    ),
)

if mode == MODE_STEP1:
    st.caption(
        "Left = **pre-vendor** (original). Right = **save-before-postprocessing** "
        "(tracked changes). Words that silently vanished from the right "
        "(real content drops) are highlighted in **red** on the left. "
        "Tracked deletions are NOT drops."
    )
    left_label = "Pre-vendor PXML"
    right_label = "Save-before-postprocessing PXML"
    default_left = DEFAULT_PRE
    default_right = DEFAULT_POST
else:
    st.caption(
        "Left = **save-before-postprocessing**. Right = **final** pxml. "
        "Postprocessing is expected to REMOVE the <trace.deleted> content "
        "(kept as struck-through here, never flagged). Any NON-deleted word "
        "missing from the final pxml is a **red** real drop caused by "
        "postprocessing."
    )
    left_label = "Save-before-postprocessing PXML"
    right_label = "Final PXML"
    default_left = DEFAULT_POST
    default_right = ""

col_u1, col_u2 = st.columns(2)
with col_u1:
    up_pre = st.file_uploader(left_label, type=["pxml", "xml"], key="pre")
    pre_path = st.text_input(f"…or path to {left_label.split(' PXML')[0].lower()}", value=default_left)
with col_u2:
    up_post = st.file_uploader(right_label, type=["pxml", "xml"], key="post")
    post_path = st.text_input(f"…or path to {right_label.split(' PXML')[0].lower()}", value=default_right)


def _load(uploaded, path):
    if uploaded is not None:
        return uploaded.getvalue().decode("utf-8", errors="replace")
    if path:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception as e:
            st.error(f"Could not read {path}: {e}")
    return None


go = st.button("Compare", type="primary")

if go:
    pre_xml = _load(up_pre, pre_path)
    post_xml = _load(up_post, post_path)

    if not pre_xml or not post_xml:
        st.warning("Please provide both files (upload or valid path).")
        st.stop()

    if mode == MODE_STEP1:
        post_words = build_post_retained_words(post_xml)
        pre_html, dropped, samples = render_pre_keep_tags_mark_drops(pre_xml, post_words)
        post_html = render_post_with_traces(post_xml)
        left_title = "Pre-vendor (original)"
        right_title = "Save-before-postprocessing (raw XML · tracked changes)"
        ok_msg = (
            "✅ No real content drop detected. All pre-vendor prose is preserved "
            "(as normal text or tracked deletions)."
        )
    else:
        # STEP 2: expected survivors of postprocessing = non-deleted words.
        final_words = [_norm_word(w) for w in tokenize(strip_all_tags(post_xml))]
        pre_html, dropped, samples = render_before_keep_tags_mark_drops(pre_xml, final_words)
        post_html = render_post_with_traces(post_xml)
        left_title = "Save-before-postprocessing (deleted content struck)"
        right_title = "Final (raw XML)"
        ok_msg = (
            "✅ No real content drop detected. Postprocessing removed only the "
            "<trace.deleted> content; all other prose survived into the final pxml."
        )

    # ── Summary banner ──
    if dropped == 0:
        st.success(ok_msg)
    else:
        st.error(f"⚠️ {dropped} word(s) appear to be REAL CONTENT DROPS "
                 f"(present on the left, missing on the right).")
        with st.expander(f"Show {len(samples)} dropped segment(s)"):
            for s in samples:
                st.markdown(f"- 🔴 `{s}`")

    st.divider()

    # ── Side-by-side viewers ──
    box_style = (
        "border:1px solid #ccc;border-radius:8px;padding:16px;"
        "height:70vh;overflow:auto;line-height:1.6;font-size:13px;"
        "background:#fafafa;white-space:pre-wrap;word-break:break-word;"
        "font-family:Consolas,'Courier New',monospace;"
    )
    c1, c2 = st.columns(2)
    with c1:
        st.subheader(left_title)
        st.markdown(
            f'<div style="{box_style}">{pre_html}</div>',
            unsafe_allow_html=True,
        )
    with c2:
        st.subheader(right_title)
        st.markdown(
            '<span style="color:#9aa0a6;">&lt;tags&gt;</span> kept · '
            '<span style="color:#0a7d00;text-decoration:underline;">inserted</span> · '
            '<span style="color:#b00000;text-decoration:line-through;">deleted</span>',
            unsafe_allow_html=True,
        )
        st.markdown(
            f'<div style="{box_style}">{post_html}</div>',
            unsafe_allow_html=True,
        )
else:
    st.info("Set the file paths (defaults are pre-filled) and click **Compare**.")
