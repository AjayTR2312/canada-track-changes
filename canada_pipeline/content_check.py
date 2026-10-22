"""Content-completeness check: detect input text missing from the XML output.

Deterministic, no LLM. Reduces both sides to normalized word tokens (decoding
PAX entities, stripping tags and pipeline-only markers), then:
  * coverage %  — fraction of input words present in the output (multiset)
  * missing segments — runs of consecutive input words absent from the output,
                       each tagged with the input line for jump-to-source.

Entity normalization mirrors compare_trackchanges_io/content_drop_checker.py.
"""

import re
from collections import Counter
from difflib import SequenceMatcher
from typing import Dict, List

# PAX / HTML entities → their textual form, so they don't read as "missing".
# Section symbols (§, §§) and the like are PUNCTUATION, not content words —
# map them to a space so "&sect;14:1" vs "§ 14:1" don't create phantom misses.
_ENTITY_MAP = {
    "&ldquo;": '"', "&rdquo;": '"', "&lsquo;": "'", "&rsquo;": "'",
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&apos;": "'",
    "&mdash;": "-", "&ndash;": "-", "&hellip;": "...",
    "&sect;": " ", "&dblsect;": " ", "&key;": " ",
    "&lsqb;": "[", "&rsqb;": "]", "&num;": "#", "&percnt;": "%",
    "&dollar;": "$", "&ast;": "*", "&nbsp;": " ",
}

# Input-only authoring markers the pipeline intentionally drops — not "missing"
_MARKER_RE = re.compile(
    r"<rr>|</rr>|\[NEW\]|\[New\]|<SECTION_HEADING>|</SECTION_HEADING>"
    r"|<ANALYTICAL_LEVEL_\d>|</ANALYTICAL_LEVEL_\d>|<CHECKLIST_START>|<FORMUNIT_START>",
    re.IGNORECASE,
)


def _decode_entities(text: str) -> str:
    for ent, ch in _ENTITY_MAP.items():
        text = text.replace(ent, ch)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)   # any remaining named entity
    text = re.sub(r"&#\d+;", " ", text)         # numeric entities
    return text


def _normalize_word(w: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", w.lower())


def _tokenize_with_lines(text: str):
    """Return (norm_tokens, orig_words, line_numbers) for the INPUT text."""
    norm, orig, lines = [], [], []
    for line_no, line in enumerate(text.splitlines(), start=1):
        clean = _decode_entities(_MARKER_RE.sub(" ", line))
        for w in re.findall(r"\S+", clean):
            nw = _normalize_word(w)
            if nw:
                norm.append(nw)
                orig.append(w)
                lines.append(line_no)
    return norm, orig, lines


def _output_tokens(xml: str):
    txt = re.sub(r"<[^>]+>", " ", xml)      # strip all tags
    txt = _decode_entities(txt)
    return [nw for w in re.findall(r"\S+", txt) if (nw := _normalize_word(w))]


def content_coverage(input_text: str, output_xml: str, min_run: int = 5) -> Dict:
    """Compare input text against output XML.

    Returns:
      coverage_pct       — % of input words present in output (count-aware)
      input_words        — total input word count
      missing_words      — input words not covered by output (count-aware)
      missing_segments   — [{text, input_line, words}] runs of >= min_run
                           consecutive input words absent from the output
    """
    in_norm, in_orig, in_lines = _tokenize_with_lines(input_text)
    out_norm = _output_tokens(output_xml)

    in_total = len(in_norm)
    if in_total == 0:
        return {"coverage_pct": 100.0, "input_words": 0, "missing_words": 0, "missing_segments": []}

    # count-aware coverage (a dropped duplicate still counts as missing)
    missing_count = sum((Counter(in_norm) - Counter(out_norm)).values())
    coverage = round(100.0 * (1 - missing_count / in_total), 2)

    # Normalized output as one string, for verifying candidate misses. Sequence
    # alignment marks a run "deleted" when it can't line it up IN PLACE, but the
    # pipeline reorders/re-wraps content — so a run may be present elsewhere in the
    # output. We drop any run whose normalized text actually appears in the output
    # (a reorder, not a real drop) to avoid false "missing" positives.
    out_str = " " + " ".join(out_norm) + " "

    def _present_in_output(i1: int, i2: int) -> bool:
        needle = " " + " ".join(in_norm[i1:i2]) + " "
        return needle in out_str

    # locate WHICH spans are missing via sequence alignment
    sm = SequenceMatcher(a=in_norm, b=out_norm, autojunk=False)
    segments: List[Dict] = []      # runs of >= min_run words (likely real drops)
    scattered: List[Dict] = []     # short runs (1..min_run-1) — usually formatting noise
    for tag, i1, i2, _j1, _j2 in sm.get_opcodes():
        if tag not in ("delete", "replace"):
            continue
        if _present_in_output(i1, i2):
            continue  # words exist elsewhere in the output — reordered, not missing
        run = in_orig[i1:i2]
        snippet = " ".join(run)
        entry = {
            "text": snippet[:200] + ("…" if len(snippet) > 200 else ""),
            "input_line": in_lines[i1] if i1 < len(in_lines) else 0,
            "words": i2 - i1,
        }
        (segments if (i2 - i1) >= min_run else scattered).append(entry)

    segments.sort(key=lambda s: s["words"], reverse=True)
    # de-dup scattered to distinct words for a compact reviewable sample
    seen, sample = set(), []
    for s in scattered:
        w = s["text"].strip()
        key = w.lower()
        if w and key not in seen:
            seen.add(key)
            sample.append(s)
    sample.sort(key=lambda s: s["input_line"])

    return {
        "coverage_pct": coverage,
        "input_words": in_total,
        "missing_words": missing_count,
        "missing_segments": segments[:60],          # passages — investigate
        "scattered_count": len(scattered),
        "scattered_sample": sample[:120],            # short gaps — usually noise
    }
