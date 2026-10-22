"""
postprocessing_test.py  —  FEASIBILITY / EXPERIMENT SCRIPT
===========================================================

New business rule to validate:

    If a TOP-LEVEL <para> (the paragraph the reader sees, i.e. a <para>
    that is NOT nested inside another <para>) contains ANY inline track
    change anywhere in its subtree — that is, any of:
        <trace ...> ... </trace>
        <trace.deleted/>   (or <trace.deleted> ... )
    — then that paragraph's OPENING tag must be updated to:
        date.updated="1" date.trace="1"

This is the same stamping we previously applied only to brand-new
paragraphs / looseleaf inserts, now extended to any paragraph that has
inline changes inside it (including changes buried inside its footnotes).

This script is deliberately standalone so we can confirm feasibility on a
real post-vendor file BEFORE wiring it into postprocessing.postprocessing_fortags.

It uses a whitespace-preserving, tokenizer-based rewrite (NOT an lxml
round-trip) so the file's exact formatting is left untouched except for the
edited opening <para> tags.

Run:
    python -m canada_pipeline.postprocessing_test  <input.pxml>  [output.pxml]
or with no args it defaults to the ravali test file below.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


# Matches any tag: captures  slash?, name, attrs, selfclose?
_TAG_RE = re.compile(r'<(/?)([a-zA-Z][\w.\-]*)((?:[^>"\']|"[^"]*"|\'[^\']*\')*?)(/?)>')

# Detects an inline track-change marker: <trace>, <trace ...>, <trace.deleted/>,
# <trace.deleted>. \b after "trace" also matches the "." in "trace.deleted".
_TRACE_MARKER_RE = re.compile(r'<trace\b')


def _span_has_trace_change(span: str) -> bool:
    """True if the para span contains any inline trace change marker."""
    return bool(_TRACE_MARKER_RE.search(span))


def _stamp_para_open_tag(open_tag: str) -> str:
    """
    Rewrite a <para ...> opening tag so it carries
    date.updated="1" date.trace="1", preserving all other attributes.
    """
    # Extract name + attrs (open_tag looks like: <para ...>  — never self-closing here)
    m = re.match(r'<([a-zA-Z][\w.\-]*)((?:[^>"\']|"[^"]*"|\'[^\']*\')*)>', open_tag)
    if not m:
        return open_tag
    name = m.group(1)
    attrs = m.group(2)

    # Update or insert date.updated
    if re.search(r'\bdate\.updated\s*=', attrs):
        attrs = re.sub(r'\bdate\.updated\s*=\s*"[^"]*"', 'date.updated="1"', attrs)
        attrs = re.sub(r"\bdate\.updated\s*=\s*'[^']*'", 'date.updated="1"', attrs)
    else:
        attrs = attrs.rstrip() + ' date.updated="1"'

    # Update or insert date.trace
    if re.search(r'\bdate\.trace\s*=', attrs):
        attrs = re.sub(r'\bdate\.trace\s*=\s*"[^"]*"', 'date.trace="1"', attrs)
        attrs = re.sub(r"\bdate\.trace\s*=\s*'[^']*'", 'date.trace="1"', attrs)
    else:
        attrs = attrs.rstrip() + ' date.trace="1"'

    # Normalize spacing: ensure single leading space before attrs
    attrs = ' ' + attrs.strip()
    return f'<{name}{attrs}>'


def stamp_paras_with_inline_changes(xml: str, verbose: bool = True) -> tuple[str, int, list]:
    """
    Find every TOP-LEVEL <para> (no <para> ancestor) that contains an inline
    trace change anywhere in its subtree, and stamp its opening tag with
    date.updated="1" date.trace="1".

    Returns (modified_xml, num_stamped, examples).
    """
    # ── Pass 1: locate top-level <para> spans via a para-depth walk ──────────
    # We record (open_start, open_end, close_end) for each OUTERMOST para.
    top_level_spans: list[tuple[int, int, int]] = []

    para_depth = 0
    cur_open_start = None
    cur_open_end = None

    for m in _TAG_RE.finditer(xml):
        is_close = bool(m.group(1))
        name = m.group(2)
        is_selfclose = bool(m.group(4))

        if name != 'para':
            continue  # ignore para.text, footnote, etc. for nesting purposes

        if is_selfclose:
            # A self-closing <para/> at top level has no content — skip.
            continue

        if not is_close:
            # opening <para ...>
            if para_depth == 0:
                cur_open_start = m.start()
                cur_open_end = m.end()
            para_depth += 1
        else:
            # closing </para>
            para_depth -= 1
            if para_depth == 0 and cur_open_start is not None:
                top_level_spans.append((cur_open_start, cur_open_end, m.end()))
                cur_open_start = None
                cur_open_end = None
            if para_depth < 0:
                # Malformed nesting guard — reset.
                para_depth = 0

    # ── Pass 2: for each top-level para span, check for trace + stamp ───────
    examples = []
    edits: list[tuple[int, int, str]] = []  # (open_start, open_end, new_open_tag)

    for open_start, open_end, close_end in top_level_spans:
        span = xml[open_start:close_end]
        if not _span_has_trace_change(span):
            continue
        old_open = xml[open_start:open_end]
        new_open = _stamp_para_open_tag(old_open)
        if new_open != old_open:
            edits.append((open_start, open_end, new_open))
            if len(examples) < 8:
                examples.append((old_open.strip(), new_open.strip()))

    # ── Apply edits back-to-front so positions stay valid ───────────────────
    modified = xml
    for open_start, open_end, new_open in sorted(edits, key=lambda e: e[0], reverse=True):
        modified = modified[:open_start] + new_open + modified[open_end:]

    if verbose:
        print(f"[TRACE-STAMP] Top-level <para> elements scanned : {len(top_level_spans)}")
        print(f"[TRACE-STAMP] Paragraphs with inline changes     : {len(edits)}")
        for old, new in examples:
            print("  ---")
            print(f"   BEFORE: {old[:110]}")
            print(f"   AFTER : {new[:110]}")

    return modified, len(edits), examples


def main(argv: list[str]) -> None:
    default_in = (
        r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs"
        r"\production_testing_issues\ravali\testing"
        r"\CDN_BR_CA 1 (chapter and all children)_20260810_125534_final.pxml"
    )

    in_path = Path(argv[1]) if len(argv) > 1 else Path(default_in)
    if len(argv) > 2:
        out_path = Path(argv[2])
    else:
        out_path = in_path.with_name(in_path.stem + "_traceupdated.pxml")

    print(f"[INPUT ] {in_path}")
    xml = in_path.read_text(encoding="utf-8")

    modified, n, _ = stamp_paras_with_inline_changes(xml, verbose=True)

    out_path.write_text(modified, encoding="utf-8")
    print(f"[OUTPUT] {out_path}")
    print(f"[DONE  ] Stamped {n} paragraph(s).")


if __name__ == "__main__":
    main(sys.argv)
