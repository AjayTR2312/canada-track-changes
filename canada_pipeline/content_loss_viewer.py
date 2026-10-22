"""
content_loss_viewer.py

Streamlit UI for the prevendor-vs-postvendor content-loss check.

It compares TWO XML files DIRECTLY (no tags removed):
  * LEFT  : the PREVENDOR XML  (red highlight = content DROP, i.e. text that
            exists in the prevendor XML but is MISSING from the postvendor XML)
  * RIGHT : the POSTVENDOR XML (amber highlight = text present only in the
            postvendor XML)

IMPORTANT - how the comparison works (this answers "why do I see spaces?"):
  The diff is done at the WORD level. Every run of non-whitespace is one token
  (regex \\S+) -> tags, attributes and words are ALL compared, nothing is
  stripped. ALL spaces, tabs and newlines are IGNORED when comparing.
  The whitespace you see in the text is only there for readability and never
  counts as a difference.

Run:
    streamlit run content_loss_viewer.py
"""

import os
import re
import html

import streamlit as st
import streamlit.components.v1 as components

from content_loss_unchanged import find_unaccounted_loss, mask_xml_tags


# ----------------------------------------------------------------------
# Defaults
# ----------------------------------------------------------------------
DEFAULT_PREVENDOR = ""

DEFAULT_POSTVENDOR = ""

DROP_COLOR = "#ff6b6b"      # red   -> real content loss (no <trace.deleted/> marker)
EXPECTED_COLOR = "#c7d2da"  # grey  -> expected deletion (has a <trace.deleted/> marker)
MOVED_COLOR = "#8ecae6"     # blue  -> moved/reordered text (still present in postvendor)
REWORDED_COLOR = "#b5e48c"  # green -> reworded in place (replaced by very similar text)
INSERT_COLOR = "#ffd479"    # amber -> inserted text (<trace> ... </trace>)


# ----------------------------------------------------------------------
# Rendering helpers
# ----------------------------------------------------------------------

def _escape_segment(s: str, show_ws: bool) -> str:
    s = html.escape(s)
    if show_ws:
        # make whitespace visible so it's obvious it is NOT part of the compare
        s = s.replace(" ", "&middot;").replace("\t", "&#8594;\t")
        s = s.replace("\n", "&para;\n")
    return s


def _merge_ranges(ranges):
    ranges = sorted((s, e) for s, e in ranges if e > s)
    merged = []
    for s, e in ranges:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def build_highlighted_html(text, ranges, color, show_ws):
    """Return HTML for `text` with the given char ranges wrapped in a <mark>."""
    merged = _merge_ranges(ranges)
    if not merged:
        return _escape_segment(text, show_ws)

    out = []
    pos = 0
    for s, e in merged:
        if s > pos:
            out.append(_escape_segment(text[pos:s], show_ws))
        seg = _escape_segment(text[s:e], show_ws)
        out.append(
            f'<mark style="background:{color};color:#111;'
            f'padding:0 1px;border-radius:2px;font-weight:600;">{seg}</mark>'
        )
        pos = e
    if pos < len(text):
        out.append(_escape_segment(text[pos:], show_ws))
    return "".join(out)


def build_multi_highlight_html(text, spans, show_ws):
    """Like build_highlighted_html but `spans` is a list of (start, end, color).
    Spans are expected to be non-overlapping; any accidental overlap is clipped
    (earlier spans win)."""
    spans = sorted((s, e, c) for s, e, c in spans if e > s)
    if not spans:
        return _escape_segment(text, show_ws)
    out = []
    pos = 0
    for s, e, color in spans:
        if s < pos:            # clip overlap with the previous span
            s = pos
        if s >= e:
            continue
        if s > pos:
            out.append(_escape_segment(text[pos:s], show_ws))
        seg = _escape_segment(text[s:e], show_ws)
        out.append(
            f'<mark style="background:{color};color:#111;'
            f'padding:0 1px;border-radius:2px;font-weight:600;">{seg}</mark>'
        )
        pos = e
    if pos < len(text):
        out.append(_escape_segment(text[pos:], show_ws))
    return "".join(out)


def scroll_box(inner_html, height):
    return (
        '<div style="white-space:pre-wrap;word-wrap:break-word;'
        "font-family:Consolas,'Courier New',monospace;font-size:13px;line-height:1.55;"
        "background:#ffffff;color:#111111;border:1px solid #cccccc;border-radius:6px;"
        f'padding:12px;height:{height}px;overflow:auto;">{inner_html}</div>'
    )


# ----------------------------------------------------------------------
# Data loading (cached)
# ----------------------------------------------------------------------

@st.cache_data(show_spinner=False)
def _read_file_bytes(path, mtime):
    with open(path, "rb") as f:
        return f.read()


def decode_xml_bytes(data):
    """Decode raw XML bytes to text (tags KEPT, nothing normalised so character
    offsets line up exactly with what is shown in the UI)."""
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


@st.cache_data(show_spinner=True)
def compute_all(prevendor_bytes, postvendor_bytes):
    """Trace-aware diff of prevendor xml vs postvendor xml. Expected deletions
    (those with a <trace.deleted/> marker) are separated from real loss."""
    pre_text = decode_xml_bytes(prevendor_bytes)
    post_text = decode_xml_bytes(postvendor_bytes)
    info = find_unaccounted_loss(pre_text, post_text)
    info["pre_text"] = pre_text
    info["post_text"] = post_text
    return info


def resolve_bytes(upload, path):
    """Return (bytes, label) from an uploaded file OR a path on disk."""
    if upload is not None:
        return upload.getvalue(), f"upload: {upload.name}"
    if path and os.path.exists(path):
        return _read_file_bytes(path, os.path.getmtime(path)), path
    return None, None


def _count_words(text, ranges):
    """Count plain words (tags ignored) inside the given char ranges."""
    return sum(len(re.findall(r"\S+", mask_xml_tags(text[s:e]))) for s, e in ranges)


# ----------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------

st.set_page_config(page_title="Content-Loss Viewer", layout="wide")
st.title("🔍 Content-Loss Viewer — Prevendor XML vs Postvendor XML")

with st.sidebar:
    st.header("Inputs")
    prevendor_upload = st.file_uploader("Prevendor XML file", type=["xml", "pxml"])
    postvendor_upload = st.file_uploader("Postvendor XML file", type=["xml", "pxml"])
    st.caption("…or point to files on disk:")
    prevendor_path = st.text_input("Prevendor XML path", value=DEFAULT_PREVENDOR)
    postvendor_path = st.text_input("Postvendor XML path", value=DEFAULT_POSTVENDOR)

    st.divider()
    st.header("View")
    height = st.slider("Panel height (px)", 300, 1200, 640, 20)
    show_ws = st.checkbox(
        "Show whitespace (· ¶ →)",
        value=False,
        help="Whitespace is NEVER used in the comparison. This only makes it "
             "visible so you can see it is ignored.",
    )
    show_expected = st.checkbox(
        "Also mark expected deletions (grey)", value=False,
        help="Content removed on purpose — a <trace.deleted/> marker sits where "
             "it used to be. Shown grey, never red.",
    )
    show_moved = st.checkbox(
        "Also mark moved / reordered text (blue)", value=False,
        help="Text that is missing at this position but still exists elsewhere in "
             "the postvendor XML — it was moved, not lost. Shown blue, never red.",
    )
    show_reworded = st.checkbox(
        "Also mark reworded text (green)", value=False,
        help="Text replaced in place by very similar text (e.g. a citation number "
             "changed, a synonym swapped) — a reword, not a loss. Shown green, never red.",
    )
    show_inserts = st.checkbox("Highlight insertions in postvendor (amber)", value=True)

with st.expander("ℹ️ How the comparison works", expanded=False):
    st.markdown(
        "- The **postvendor** XML records its own track changes inline: "
        "`<trace>…</trace>` = inserted text, `<trace.deleted/>` = a marker where "
        "text was deleted.\n"
        "- Prevendor and postvendor are compared **word-level**; whitespace and tags "
        "never count as a difference.\n"
        "- **Red** = prevendor content missing from the postvendor with **no** "
        "`<trace.deleted/>` marker to explain it — this is **real content loss**.\n"
        "- **Grey** (optional) = prevendor content deleted on purpose — a "
        "`<trace.deleted/>` marker sits exactly where it used to be, so it is **not** loss.\n"
        "- **Amber** = text inserted in the postvendor (inside `<trace>`)."
    )

# Resolve inputs
prevendor_bytes, prevendor_label = resolve_bytes(prevendor_upload, prevendor_path)
postvendor_bytes, postvendor_label = resolve_bytes(postvendor_upload, postvendor_path)

if not prevendor_bytes or not postvendor_bytes:
    st.info("Provide a prevendor XML and a postvendor XML (upload or path) to begin.")
    st.stop()

# Compute
try:
    result = compute_all(prevendor_bytes, postvendor_bytes)
except Exception as exc:  # pragma: no cover - surfaced to the user
    st.error(f"Failed to process files: {exc}")
    st.stop()

pre_text = result["pre_text"]
post_text = result["post_text"]
red_ranges = result["red_ranges"]              # real loss (no marker)
expected_ranges = result["expected_ranges"]    # deletions with a marker
moved_ranges = result.get("moved_ranges", [])  # still present elsewhere in postvendor
reworded_ranges = result.get("reworded_ranges", [])  # replaced in place by similar text
insertion_ranges = result["insertion_ranges"]  # <trace> insertions in postvendor

red_words = _count_words(pre_text, red_ranges)
expected_words = _count_words(pre_text, expected_ranges)
moved_words = _count_words(pre_text, moved_ranges)
reworded_words = _count_words(pre_text, reworded_ranges)

# Metrics
m = st.columns(7)
m[0].metric("Prevendor words", f"{result['pre_words']:,}")
m[1].metric("Postvendor words", f"{result['post_words']:,}")
m[2].metric("Real content loss", f"{len(red_ranges):,}", f"{red_words} words", delta_color="inverse")
m[3].metric("Expected deletions", f"{result['num_expected']:,}", f"{expected_words} words")
m[4].metric("Moved / reordered", f"{len(moved_ranges):,}", f"{moved_words} words")
m[5].metric("Reworded", f"{len(reworded_ranges):,}", f"{reworded_words} words")
m[6].metric("Deletion markers", f"{result['num_markers_matched']}/{result['num_markers']}")

st.caption(f"Prevendor: {prevendor_label}  •  Postvendor: {postvendor_label}")

if red_ranges:
    st.error(f"🚩 {len(red_ranges)} real content drop(s) — {red_words} word(s) from the "
             f"prevendor XML are missing in the postvendor XML with NO <trace.deleted/> marker.")
elif result["num_markers"]:
    st.success("✅ No unexpected content loss: every missing prevendor word has a "
               "<trace.deleted/> marker in the postvendor XML.")
else:
    st.success("✅ No content drop: every prevendor word is present in the postvendor XML.")

# Legend
legend = (
    f'<span style="background:{DROP_COLOR};padding:2px 8px;border-radius:3px;color:#111;">'
    "red = real loss (no deletion marker)</span>&nbsp;&nbsp;"
)
if show_expected:
    legend += (
        f'<span style="background:{EXPECTED_COLOR};padding:2px 8px;border-radius:3px;color:#111;">'
        "grey = expected deletion (marker present)</span>&nbsp;&nbsp;"
    )
if show_moved:
    legend += (
        f'<span style="background:{MOVED_COLOR};padding:2px 8px;border-radius:3px;color:#111;">'
        "blue = moved / reordered (still in postvendor)</span>&nbsp;&nbsp;"
    )
if show_reworded:
    legend += (
        f'<span style="background:{REWORDED_COLOR};padding:2px 8px;border-radius:3px;color:#111;">'
        "green = reworded in place</span>&nbsp;&nbsp;"
    )
if show_inserts:
    legend += (
        f'<span style="background:{INSERT_COLOR};padding:2px 8px;border-radius:3px;color:#111;">'
        "amber = insertion (&lt;trace&gt;)</span>"
    )
st.markdown(legend, unsafe_allow_html=True)

# Side-by-side panels
left, right = st.columns(2)
with left:
    st.subheader("Prevendor XML (red = real content loss)")
    spans = [(s, e, DROP_COLOR) for s, e in red_ranges]
    if show_expected:
        spans += [(s, e, EXPECTED_COLOR) for s, e in expected_ranges]
    if show_moved:
        spans += [(s, e, MOVED_COLOR) for s, e in moved_ranges]
    if show_reworded:
        spans += [(s, e, REWORDED_COLOR) for s, e in reworded_ranges]
    inner = build_multi_highlight_html(pre_text, spans, show_ws)
    components.html(scroll_box(inner, height), height=height + 24, scrolling=False)
    st.download_button("⬇️ Download prevendor XML text",
                       pre_text, file_name="prevendor.xml")

with right:
    st.subheader("Postvendor XML")
    ins = insertion_ranges if show_inserts else []
    inner = build_highlighted_html(post_text, ins, INSERT_COLOR, show_ws)
    components.html(scroll_box(inner, height), height=height + 24, scrolling=False)
    st.download_button("⬇️ Download postvendor XML text",
                       post_text, file_name="postvendor.xml")

# Drop details
st.divider()
st.subheader(f"🚩 Real content drops ({len(red_ranges)})")
if not red_ranges:
    st.write("None.")
else:
    def _ctx(s, e, w=45):
        before = pre_text[max(0, s - w):s].replace("\n", " ").replace("\t", " ")
        after = pre_text[e:e + w].replace("\n", " ").replace("\t", " ")
        return before.strip(), after.strip()

    rows = []
    for i, (s, e) in enumerate(red_ranges, 1):
        cb, ca = _ctx(s, e)
        rows.append({
            "#": i,
            "missing text": pre_text[s:e],
            "before": cb,
            "after": ca,
            "prevendor char range": f"{s}–{e}",
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)
