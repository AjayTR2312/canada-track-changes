import re
import json
import os
import time
import httpx
import requests
import anthropic
import pandas as pd
from typing import Optional
from .config  import build_anthropic_client,find_section_key, API_KEY, MODEL_SONNET
from .xml_entity_rules import XML_ENTITY_RULES, CASE_CITATION_RULES, URL_TAGGING_RULES
from .prompt_loader import load_prompt

# ── Rate limit handling settings ──────────────────────────────────────────────
MAX_RETRIES_RATE_LIMIT = 5
INITIAL_RETRY_DELAY = 10  # seconds
MAX_RETRY_DELAY = 120  # seconds

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────


def _encode_xml_entities(text: str) -> str:
    """Encode special characters as XML entities for PXML content."""
    if not text:
        return text

    # Protect URLs so hyphens inside them are NOT converted to &hyphen;.
    # We restore them after general entity encoding.
    url_map: dict[str, str] = {}

    def _protect_url(m: re.Match) -> str:
        token = f"__URL_TOKEN_{len(url_map)}__"
        url_map[token] = m.group(0)
        return token

    text = re.sub(r'(https?://[^\s<>"\']+|www\.[^\s<>"\']+)', _protect_url, text)

    # Ampersand first (but skip existing entities)
    text = re.sub(r'&(?!amp;|lt;|gt;|apos;|quot;|ldquo;|rdquo;|lsquo;|rsquo;|lsqb;|rsqb;|sect;|dblsect;|para;|num;|dollar;|percnt;|ast;|hyphen;|mdash;|ndash;|hellip;|#\d+;|#x[0-9a-fA-F]+;)', '&amp;', text)
    text = text.replace('§§', '&dblsect;')
    text = text.replace('§', '&sect;')
    text = text.replace('¶', '&para;')
    text = text.replace('[', '&lsqb;')
    text = text.replace(']', '&rsqb;')
    text = text.replace('#', '&num;')
    text = text.replace('$', '&dollar;')
    text = text.replace('%', '&percnt;')
    text = text.replace('*', '&ast;')
    text = text.replace('-', '&hyphen;')
    text = text.replace('\u2018', '&lsquo;')
    text = text.replace('\u2019', '&rsquo;')
    text = text.replace('\u201c', '&ldquo;')
    text = text.replace('\u201d', '&rdquo;')
    # Handle straight double quotes - alternate between opening and closing
    parts = text.split('"')
    if len(parts) > 1:
        result = parts[0]
        for i, part in enumerate(parts[1:], 1):
            entity = '&ldquo;' if i % 2 == 1 else '&rdquo;'
            result += entity + part
        text = result
    text = text.replace("'", '&apos;')
    text = text.replace('\u2014', '&mdash;')
    text = text.replace('\u2013', '&ndash;')

    # Restore protected URLs (keep '-' unchanged; XML-escape '&' if present)
    for token, url in url_map.items():
        safe_url = re.sub(r'&(?!amp;|lt;|gt;|apos;|quot;|#\d+;|#x[0-9a-fA-F]+;)', '&amp;', url)
        text = text.replace(token, safe_url)

    return text


def call_anthropic(client, model_name: str, prompt: str, max_tokens: int = 10000) -> str:
    """
    Unified Anthropic call wrapper with exponential backoff retry on rate limit errors.
    Accepts a plain prompt string, returns the response text.
    Use this for ALL LLM calls in this codebase.
    """
    retry_delay = INITIAL_RETRY_DELAY
    
    for attempt in range(MAX_RETRIES_RATE_LIMIT):
        try:
            message = client.messages.create(
                model=model_name,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return message.content[0].text.strip()
            
        except Exception as e:
            error_str = str(e)
            
            # Check if it's a rate limit error (429)
            if "rate_limit" in error_str.lower() or "429" in error_str:
                if attempt < MAX_RETRIES_RATE_LIMIT - 1:
                    print(f"  [RATE LIMIT] Attempt {attempt + 1}/{MAX_RETRIES_RATE_LIMIT} - Waiting {retry_delay}s before retry...")
                    time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, MAX_RETRY_DELAY)  # Exponential backoff with cap
                else:
                    print(f"  [RATE LIMIT] Max retries ({MAX_RETRIES_RATE_LIMIT}) exceeded.")
                    raise
            else:
                # Not a rate limit error, don't retry
                raise
    
    raise Exception("Max retries exceeded")


def _call_llm_prefilled(client, model_name: str, prompt: str, max_tokens: int = 300) -> str:
    """
    Call LLM with assistant turn prefilled to '' to prevent reasoning preamble.
    Forces the model to output the anchor directly without explanation.
    """
    retry_delay = INITIAL_RETRY_DELAY
    for attempt in range(MAX_RETRIES_RATE_LIMIT):
        try:
            message = client.messages.create(
                model=model_name,
                max_tokens=max_tokens,
                messages=[
                    {"role": "user",      "content": prompt},
                    {"role": "assistant", "content": ""},  # ← forces direct output
                ],
            )
            return message.content[0].text.strip()
        except Exception as e:
            error_str = str(e)
            if "rate_limit" in error_str.lower() or "429" in error_str:
                if attempt < MAX_RETRIES_RATE_LIMIT - 1:
                    print(f"  [RATE LIMIT] Attempt {attempt + 1}/{MAX_RETRIES_RATE_LIMIT} - Waiting {retry_delay}s...")
                    time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, MAX_RETRY_DELAY)
                else:
                    print(f"  [RATE LIMIT] Max retries exceeded.")
                    raise
            else:
                raise
    raise Exception("Max retries exceeded")


def _extract_clean_anchor(anchor: str) -> str:
    """
    Strip LLM reasoning preamble/suffix from anchor response.
    Handles: <anchor>...</anchor> wrappers, ```xml...``` fences, reasoning text.
    """
    if not anchor:
        return anchor

    # 1. If LLM wrapped in <anchor>...</anchor>, extract that
    m = re.search(r'<anchor>(.*?)</anchor>', anchor, re.DOTALL)
    if m:
        return m.group(1).strip()

    # 2. If LLM used ```xml ... ``` or ``` ... ``` code fence, extract content
    m = re.search(r'```(?:xml)?\s*(.*?)```', anchor, re.DOTALL)
    if m:
        return m.group(1).strip()

    # 3. Collect only lines that look like XML content, skip reasoning lines
    lines = anchor.splitlines()
    xml_lines = []
    reasoning_keywords = [
        'looking at', 'i need', 'found it', 'the target', 'let me',
        'anchor should', 'the anchor', 'i can find', 'searching',
        'this text', 'the xml', 'the last', 'the longest',
    ]
    for line in lines:
        stripped = line.strip()
        if re.search(r'<[^>]+>|&[a-zA-Z]+;', stripped):
            xml_lines.append(stripped)
        elif xml_lines and stripped and not any(
            kw in stripped.lower() for kw in reasoning_keywords
        ):
            xml_lines.append(stripped)

    if xml_lines:
        return ' '.join(xml_lines).strip()

    # 4. Last resort: last 200 chars
    return anchor[-200:].strip()


def _preview_for_log(text: str, max_len: int = 320) -> str:
    """Single-line preview for logs with length guard."""
    if text is None:
        return ""
    compact = re.sub(r'\s+', ' ', str(text)).strip()
    if len(compact) <= max_len:
        return compact
    return compact[:max_len] + " ...[truncated]"


# def normalize_text(text: str) -> str:
#     """Normalize smart quotes, whitespace, and HTML entities."""
#     text = text.replace('\u2018', "'").replace('\u2019', "'")
#     text = text.replace('\u201c', '"').replace('\u201d', '"')
#     # Decode HTML entities
#     text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
#     text = text.replace('&apos;', "'").replace('&quot;', '"')
#     text = re.sub(r'\s+', ' ', text).strip()
#     return text

def normalize_text(text: str) -> str:
    """Normalize smart quotes, whitespace, and HTML entities."""
    text = text.replace('\u2018', "'").replace('\u2019', "'")
    text = text.replace('\u201c', '"').replace('\u201d', '"')
    # Decode HTML entities
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&apos;', "'").replace('&quot;', '"')
    # ── ADD THESE: decode typographic quote entities ──────────────────────────
    text = text.replace('&ldquo;', '"').replace('&rdquo;', '"')
    text = text.replace('&lsquo;', "'").replace('&rsquo;', "'")
    text = text.replace('&mdash;', '\u2014').replace('&ndash;', '\u2013')
    # ─────────────────────────────────────────────────────────────────────────
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def xml_encode(text: str) -> str:
    """Encode & to &amp; for searching inside raw XML."""
    return text.replace('&', '&amp;')


def strip_xml_tags(text: str) -> str:
    """Remove all XML tags, leaving only text content."""
    return re.sub(r'<[^>]+>', '', text)


# Block-level tags that should NEVER be walked past
_BLOCK_LEVEL_TAGS = {
    'para', 'para.text', 'section', 'heading', 'heading.para',
    'footnote', 'footnote.body', 'footnote.reference',
    'list', 'list.item', 'quote.block', 'stat.item.para',
    'clause', 'table', 'table.row', 'table.entry',
    'section.body', 'document', 'body',
    'trace',  # footnotes inside <trace> (inserted text) must stay inside
}


def _strip_footnotes_from_content(content: str) -> tuple[str, list[tuple[int, int, str]]]:
    """
    Remove all <footnote>...</footnote> blocks from content for searching.
    Returns:
      - cleaned content with footnotes replaced by empty string
      - list of (start_pos, end_pos, footnote_text) tuples for position adjustment
    """
    footnote_pattern = re.compile(r'<footnote\b[^>]*>.*?</footnote>', re.DOTALL)
    footnotes_found = []
    
    for m in footnote_pattern.finditer(content):
        footnotes_found.append((m.start(), m.end(), m.group(0)))
    
    # Remove footnotes from content (reverse order to preserve positions)
    cleaned = content
    for start, end, _ in reversed(footnotes_found):
        cleaned = cleaned[:start] + cleaned[end:]
    
    if footnotes_found:
        print(f"  [STRIP FOOTNOTES] Removed {len(footnotes_found)} existing footnote(s) for search")
    
    return cleaned, footnotes_found


def _strip_footnotes_from_fragment(text: str) -> str:
    """
    Remove <footnote>...</footnote> blocks from a small XML/text fragment.
    Used to sanitize LLM anchors when they include prior footnote markup.
    """
    if not text:
        return text
    return re.sub(r'<footnote\b[^>]*>.*?</footnote>', '', text, flags=re.DOTALL)


def _trim_trailing_block_tags(anchor: str) -> str:
    """
    Strip trailing BLOCK-LEVEL closing tags (e.g. </para.text>, </para>,
    </footnote.body>, ...) from an LLM anchor.

    The LLM is instructed to return an anchor ending EXACTLY at the target text,
    but it sometimes appends the paragraph/section closing tags that come AFTER
    the target (e.g. '...estate.</para.text></para>'). If we match on that, the
    insertion point lands AFTER </para>, placing the footnote OUTSIDE the
    paragraph. We remove only trailing block-level closing tags (and any
    whitespace between them) so the anchor once again ends at the visible text.

    Inline closing tags (</ital>, </cite>, etc.) are intentionally preserved.
    """
    if not anchor:
        return anchor

    trimmed = anchor
    # Repeatedly peel a single trailing block-level closing tag (with optional
    # surrounding whitespace) until none remain.
    close_tag_re = re.compile(r'\s*</([a-zA-Z][a-zA-Z0-9._-]*)\s*>\s*$')
    while True:
        m = close_tag_re.search(trimmed)
        if not m:
            break
        tag_name = m.group(1).lower()
        if tag_name not in _BLOCK_LEVEL_TAGS:
            break  # inline tag — keep it
        trimmed = trimmed[:m.start()].rstrip()

    if trimmed != anchor:
        print(f"  [ANCHOR TRIM] Removed trailing block-closing tag(s): "
              f"'...{anchor[-40:]}' → '...{trimmed[-40:]}'")
    return trimmed


def _adjust_position_for_stripped_footnotes(
    pos_in_cleaned: int,
    footnotes: list[tuple[int, int, str]],
) -> int:
    adjusted_pos = pos_in_cleaned
    cleaned_offset = 0

    for start, end, footnote_text in footnotes:
        footnote_len = end - start
        cleaned_start = start - cleaned_offset

        if cleaned_start < pos_in_cleaned:   # ← was <=, now strictly <
            adjusted_pos += footnote_len
            cleaned_offset += footnote_len
        else:
            break

    return adjusted_pos


def _find_context_window_in_cleaned_content(
    cleaned_content: str,
    full_para_where_footnote_is_present: str,
) -> tuple[int, int, str]:
    """
    Find a paragraph-bounded search window in cleaned section XML using full paragraph text.
    Returns (window_start, window_end, strategy_label) in cleaned_content coordinates.
    """
    if not full_para_where_footnote_is_present or not str(full_para_where_footnote_is_present).strip():
        return -1, -1, "empty"

    para_raw = str(full_para_where_footnote_is_present).strip()
    para_norm = normalize_text(para_raw)
    para_norm_xml = xml_encode(para_norm)

    tail240 = para_norm[-240:].strip() if len(para_norm) > 240 else para_norm
    tail180 = para_norm[-180:].strip() if len(para_norm) > 180 else para_norm
    tail120 = para_norm[-120:].strip() if len(para_norm) > 120 else para_norm

    para_candidates = [
        ("full para", para_raw),
        ("full para normalized", para_norm),
        ("full para xml encoded", para_norm_xml),
        ("full para tail240", tail240),
        ("full para tail180", tail180),
        ("full para tail120", tail120),
    ]

    for label, candidate in para_candidates:
        if not candidate or len(candidate) < 20:
            continue

        hit_start = cleaned_content.find(candidate)
        if hit_start == -1:
            continue

        hit_end = hit_start + len(candidate)

        para_open = cleaned_content.rfind('<para ', 0, hit_start)
        para_close = cleaned_content.find('</para>', hit_end)

        if para_open != -1 and para_close != -1 and para_close > para_open:
            window_start = para_open
            window_end = para_close + len('</para>')
            return window_start, window_end, f"{label} + para-bounds"

        # Fallback if explicit para boundaries are not found
        window_start = max(0, hit_start - 500)
        window_end = min(len(cleaned_content), hit_end + 500)
        return window_start, window_end, f"{label} + +/-500"

    return -1, -1, "not-found"


def _adjust_past_inline_close(content: str, pos: int) -> int:
    """
    Ensures the insertion point lands OUTSIDE any open inline tag.

    Two cases handled:
      1. pos is inside an unclosed inline tag (e.g. mid-way through <ital>text</ital>)
         → advance to END of that closing tag
      2. pos lands exactly at one or more closing inline tags
         → advance past ALL of them

    Block-level tags (para, section, footnote, etc.) are never walked past.
    """
    original_pos = pos

    # ── Case 1: detect if pos is INSIDE an unclosed inline tag ───────────────
    open_tag_re  = re.compile(r'<([a-zA-Z][a-zA-Z0-9._-]*)\b[^>]*/?>') 
    close_tag_re = re.compile(r'</([a-zA-Z][a-zA-Z0-9._-]*)\s*>')

    # Build a stack of unclosed inline tags before pos
    open_stack = []  # list of tag names (strings)

    for m in re.finditer(r'<(/?)([a-zA-Z][a-zA-Z0-9._-]*)\b[^>]*>', content[:pos]):
        is_closing   = m.group(1) == '/'
        is_self_close = m.group(0).endswith('/>')
        tag_name     = m.group(2).lower()

        if tag_name in _BLOCK_LEVEL_TAGS:
            if is_closing:
                open_stack.clear()  # block close resets inline context
            continue

        if is_self_close:
            continue  # self-closing tags don't affect stack

        if is_closing:
            # Pop matching open tag
            for i in range(len(open_stack) - 1, -1, -1):
                if open_stack[i] == tag_name:
                    open_stack.pop(i)
                    break
        else:
            open_stack.append(tag_name)

    # If unclosed inline tags exist, jump past their closing tags
    if open_stack:
        search_pos = pos
        # Close innermost first (last in stack = innermost)
        for tag_name in reversed(open_stack):
            close_pattern = re.compile(rf'</{re.escape(tag_name)}\s*>', re.IGNORECASE)
            m = close_pattern.search(content, search_pos)
            if m:
                print(f"  [ADJUST] pos {search_pos} is inside unclosed <{tag_name}>, "
                      f"jumping past </{tag_name}> to {m.end()}")
                search_pos = m.end()
        pos = search_pos

    # ── Case 2: walk past any closing inline tags immediately at pos ──────────
    closing_tag_re = re.compile(r'</([a-zA-Z][a-zA-Z0-9._-]*)\s*>')
    while pos < len(content):
        m = closing_tag_re.match(content, pos)
        if not m:
            break
        tag_name = m.group(1).lower()
        if tag_name in _BLOCK_LEVEL_TAGS:
            break
        print(f"  [ADJUST] Skipping closing inline tag </{m.group(1)}> at pos {pos}")
        pos = m.end()

    if pos != original_pos:
        print(f"  [ADJUST] Insertion point moved {original_pos} → {pos}")
    return pos


def _adjust_out_of_trace_deleted(content: str, pos: int) -> int:
    """
    If insertion point is inside <trace.deleted> ... <trace.deleted/>, move it
    after the closing <trace.deleted/> so postprocessing doesn't remove inserted
    footnotes.
    """
    original_pos = pos

    open_pat = re.compile(r'<trace\.deleted\b[^>]*(?<!/)>', re.IGNORECASE)
    close_pat = re.compile(r'<trace\.deleted\s*/>', re.IGNORECASE)

    before = content[:pos]
    opens_before = len(open_pat.findall(before))
    closes_before = len(close_pat.findall(before))

    if opens_before > closes_before:
        m_close = close_pat.search(content, pos)
        if m_close:
            pos = m_close.end()
            print(
                f"  [ADJUST] pos {original_pos} is inside <trace.deleted>...<trace.deleted/>; "
                f"moving to {pos}"
            )

    if pos != original_pos:
        print(f"  [ADJUST] Insertion point moved {original_pos} → {pos}")
    return pos


# ─────────────────────────────────────────────────────────────────────────────
# DIRECT INSERTION POINT FINDER (Python only, no LLM)
# ─────────────────────────────────────────────────────────────────────────────

def find_insertion_point_directly(
    text_before_superscript: str,
    content: str,
    full_para_where_footnote_is_present: Optional[str] = None,
) -> int:
    """
    Try to find insertion point using text_before_superscript directly.
    Includes XML entity encoding variants to handle & vs &amp; mismatches.
    Ignores text inside existing footnote tags to prevent false matches.
    Returns insertion point (end position) or -1.
    """
    cleaned_content, footnotes = _strip_footnotes_from_content(content)

    # First, try to disambiguate repeated text using full paragraph context.
    if full_para_where_footnote_is_present and str(full_para_where_footnote_is_present).strip():
        print(f"  [DIRECT] full_para context provided (len={len(str(full_para_where_footnote_is_present))})")
        para_window_start, para_window_end, para_strategy = _find_context_window_in_cleaned_content(
            cleaned_content,
            full_para_where_footnote_is_present,
        )
        if para_window_start != -1:
            print(
                f"  [PARA CONTEXT] Found window using '{para_strategy}' "
                f"at {para_window_start}-{para_window_end}"
            )
        else:
            print(f"  [PARA CONTEXT] Not found (strategy={para_strategy})")

        if para_window_start != -1:
            window = cleaned_content[para_window_start:para_window_end]
            tbs = text_before_superscript
            tbs_norm = normalize_text(tbs)
            tbs_xmlenc = xml_encode(tbs)
            tbs_norm_xml = xml_encode(tbs_norm)

            window_candidates = [
                ("window direct", tbs),
                ("window normalized", tbs_norm),
                ("window xml encoded", tbs_xmlenc),
                ("window normalized+xml encoded", tbs_norm_xml),
                ("window last 80", tbs[-80:].strip()),
                ("window last 80 norm", tbs_norm[-80:].strip()),
                ("window last 40", tbs[-40:].strip()),
                ("window last 40 norm", tbs_norm[-40:].strip()),
            ]

            for label, candidate in window_candidates:
                if not candidate or len(candidate) < 10:
                    continue
                rel_pos = window.rfind(candidate)
                if rel_pos != -1:
                    abs_pos = para_window_start + rel_pos + len(candidate)
                    original_pos = _adjust_position_for_stripped_footnotes(abs_pos, footnotes)
                    print(f"  [DIRECT MATCH] Paragraph-window strategy '{label}': pos={abs_pos} (adjusted={original_pos})")
                    return original_pos

                    print("  [DIRECT] Context-window search failed to find text_before_superscript")

    tbs          = text_before_superscript
    tbs_norm     = normalize_text(tbs)
    tbs_xmlenc   = xml_encode(tbs)
    tbs_norm_xml = xml_encode(tbs_norm)

    candidates = [
        ("direct",                 tbs),
        ("normalized",             tbs_norm),
        ("xml encoded",            tbs_xmlenc),
        ("normalized+xml encoded", tbs_norm_xml),
        ("last 80",                tbs[-80:].strip()),
        ("last 80 norm",           tbs_norm[-80:].strip()),
        ("last 80 xmlenc",         tbs_xmlenc[-80:].strip()),
        ("last 80 norm+xmlenc",    tbs_norm_xml[-80:].strip()),
        ("last 40",                tbs[-40:].strip()),
        ("last 40 norm",           tbs_norm[-40:].strip()),
        ("last 40 xmlenc",         tbs_xmlenc[-40:].strip()),
        ("last 40 norm+xmlenc",    tbs_norm_xml[-40:].strip()),
    ]

    for label, candidate in candidates:
        if not candidate or len(candidate) < 10:
            continue
        pos = cleaned_content.find(candidate)
        if pos != -1:
            original_pos = _adjust_position_for_stripped_footnotes(
                pos + len(candidate), footnotes
            )
            print(f"  [DIRECT MATCH] Strategy '{label}': pos={pos} (adjusted={original_pos})")
            return original_pos

    print("  [DIRECT] Plain direct strategies failed")

    # ── FIX: try multiple tail lengths, longest first ────────────────────────
    for tail_len in [None, 120, 80, 60, 40]:
        if tail_len is None:
            tail = tbs_norm
        elif len(tbs_norm) > tail_len:
            tail = tbs_norm[-tail_len:].strip()
        else:
            continue  # already tried full string or shorter already covered

        if not tail or len(tail) < 10:
            continue

        result = _tag_tolerant_find(
            tail, cleaned_content, f"direct tag-tolerant tail={tail_len}"
        )
        if result != -1:
            original_pos = _adjust_position_for_stripped_footnotes(result, footnotes)
            print(f"  [DIRECT MATCH] Tag-tolerant adjusted: {result} → {original_pos}")
            return original_pos

    # Extra fallback for punctuation + inline-tag splits, e.g. "(<ital>PIPEDA</ital>)"
    for tail_len in [None, 120, 80, 60, 40]:
        if tail_len is None:
            tail = tbs_norm
        elif len(tbs_norm) > tail_len:
            tail = tbs_norm[-tail_len:].strip()
        else:
            continue

        if not tail or len(tail) < 8:
            continue

        result = _tag_tolerant_find_flexible(
            tail, cleaned_content, f"direct flex tail={tail_len}"
        )
        if result != -1:
            original_pos = _adjust_position_for_stripped_footnotes(result, footnotes)
            print(f"  [DIRECT MATCH] Flex tag-tolerant adjusted: {result} → {original_pos}")
            return original_pos

    print("  [DIRECT] Tag-tolerant strategies failed")

    return -1


# ─────────────────────────────────────────────────────────────────────────────
# TAG-TOLERANT REGEX HELPER
# ─────────────────────────────────────────────────────────────────────────────

def _tag_tolerant_find(search_text: str, content: str, label: str = "") -> int:
    """
    Build a regex from search_text that:
      - tolerates XML tags between words  (e.g. <ital>, </cite>)
      - treats all quote characters as interchangeable (including XML entities)
      - allows flexible whitespace
    Returns the end position (insertion point) or -1.
    """
    try:
        # Normalize quotes to a placeholder, then split into words
        quote_chars = '"\'\u2018\u2019\u201c\u201d'
        normalized = re.sub('[' + re.escape(quote_chars) + ']+', '\x00', search_text)
        # Also normalize XML quote entities to the same placeholder
        normalized = re.sub(r'&(?:ldquo|rdquo|lsquo|rsquo|quot);', '\x00', normalized)
        words = normalized.split()
        if len(words) < 3:
            return -1

        footnote_block = r'<footnote\b[^>]*>.*?</footnote>'
        single_tag     = r'<[^>]*>'
        entity         = r'&[a-zA-Z0-9#]+;'
        tag_gap        = r'(?:\s*(?:' + footnote_block + r'|' + single_tag + r'|' + entity + r'))*\s*'

        # Quote class includes XML quote entities — use regular string, not raw string
        # so that unicode escapes are interpreted correctly
        quote_class = '(?:&(?:ldquo|rdquo|lsquo|rsquo|quot);|["\'\u2018\u2019\u201c\u201d])?'

        parts = []
        for w in words:
            escaped = re.escape(w).replace(re.escape('\x00'), quote_class)
            parts.append(escaped)

        pattern_str = tag_gap.join(parts)
        pattern = re.compile(pattern_str, re.DOTALL)
        m = pattern.search(content)
        if m:
            print(f"  [TAG-TOLERANT] {label}: matched at {m.start()}-{m.end()}")
            return m.end()
    except re.error as e:
        print(f"  [TAG-TOLERANT] Regex error: {e}")
    return -1


def _tag_tolerant_find_flexible(search_text: str, content: str, label: str = "") -> int:
    """
    More flexible matcher than _tag_tolerant_find.
    Allows tags/entities/whitespace between punctuation and words, which helps match
    patterns like: Act (<ital>PIPEDA</ital>)
    """
    try:
        normalized = normalize_text(search_text)
        if not normalized or len(normalized) < 8:
            return -1

        # Keep punctuation as tokens so gaps can occur between '(' and 'PIPEDA', etc.
        tokens = re.findall(r'[A-Za-z0-9]+|[^A-Za-z0-9\s]', normalized)
        if len(tokens) < 2:
            return -1

        footnote_block = r'<footnote\b[^>]*>.*?</footnote>'
        single_tag     = r'<[^>]*>'
        entity         = r'&[a-zA-Z0-9#]+;'
        token_gap      = r'(?:\s*(?:' + footnote_block + r'|' + single_tag + r'|' + entity + r'))*\s*'

        parts = [re.escape(tok) for tok in tokens]
        pattern_str = token_gap.join(parts)
        pattern = re.compile(pattern_str, re.DOTALL | re.IGNORECASE)
        m = pattern.search(content)
        if m:
            print(f"  [TAG-TOLERANT FLEX] {label}: matched at {m.start()}-{m.end()}")
            return m.end()
    except re.error as e:
        print(f"  [TAG-TOLERANT FLEX] Regex error: {e}")
    return -1

# ─────────────────────────────────────────────────────────────────────────────
# ANCHOR-BASED INSERTION POINT FINDER (LLM fallback)
# ─────────────────────────────────────────────────────────────────────────────

def find_anchor_in_content(anchor: str, content: str) -> int:
    """
    Try multiple strategies to find anchor position in content.
    Includes XML entity encoding variants to handle & vs &amp; mismatches.
    Ignores text inside existing footnote tags to prevent false matches.
    Returns the end position (insertion point) or -1 if not found.
    """
    # Strip existing footnotes from content to avoid false matches
    cleaned_content, footnotes = _strip_footnotes_from_content(content)

    def try_find(text: str, label: str) -> int:
        if not text or not text.strip() or len(text.strip()) < 10:
            return -1
        pos = cleaned_content.find(text)
        if pos != -1:
            # Adjust position back to original content
            original_pos = _adjust_position_for_stripped_footnotes(pos + len(text), footnotes)
            print(f"  [ANCHOR MATCH] {label}: pos={pos} (adjusted={original_pos})")
            return original_pos
        return -1

    # Pre-compute variants
    anchor_no_fn      = _strip_footnotes_from_fragment(anchor)
    if anchor_no_fn != anchor:
        print("  [ANCHOR SANITIZE] Removed footnote block(s) from anchor before matching")

    # Trim trailing block-level closing tags (e.g. </para.text></para>) that the
    # LLM sometimes appends after the target text. Without this, matching on the
    # full anchor lands the insertion point OUTSIDE the paragraph.
    anchor_no_fn      = _trim_trailing_block_tags(anchor_no_fn)

    norm              = normalize_text(anchor_no_fn)
    plain             = normalize_text(strip_xml_tags(anchor_no_fn))
    plain_xmlenc      = xml_encode(plain)
    before_first_tag  = normalize_text(re.split(r'<[^>]+>', anchor_no_fn)[0].strip())

    # Strategy 1: exact (sanitized anchor)
    result = try_find(anchor_no_fn, "exact(sanitized)")
    if result != -1:
        return result

    # Strategy 2: normalize quotes + whitespace
    result = try_find(norm, "normalized")
    if result != -1:
        return result

    # Strategy 3: strip XML tags + normalize
    result = try_find(plain, "stripped tags")
    if result != -1:
        return result

    # Strategy 4: strip tags + normalize + xml encode (& → &amp;)
    result = try_find(plain_xmlenc, "stripped+xml encoded")
    if result != -1:
        return result

    # Strategy 5: text before first XML tag in anchor
    result = try_find(before_first_tag, "before-first-tag")
    if result != -1:
        return result

    # Strategy 6: text before first tag + xml encoded
    result = try_find(xml_encode(before_first_tag), "before-first-tag+xml encoded")
    if result != -1:
        return result

    # Strategy 7: short tails with and without xml encoding
    for tail_len in [80, 40]:
        tail        = plain[-tail_len:].strip() if len(plain) > tail_len else plain
        tail_xmlenc = xml_encode(tail)

        result = try_find(tail, f"short tail ({tail_len})")
        if result != -1:
            return result

        result = try_find(tail_xmlenc, f"short tail ({tail_len})+xml encoded")
        if result != -1:
            return result

    # Strategy 8: split on newlines, try each line last-to-first
    lines = [normalize_text(strip_xml_tags(l)).strip() for l in anchor_no_fn.splitlines()]
    lines = [l for l in lines if len(l) > 15]
    for line in reversed(lines):
        result = try_find(line, f"line probe '{line[:40]}'")
        if result != -1:
            return result

        result = try_find(xml_encode(line), f"line probe xml encoded '{line[:40]}'")
        if result != -1:
            return result

    # Strategy 9: tag-tolerant + quote-flexible regex
    for search, slabel in [
        (plain, "tag-tolerant plain"),
        (plain_xmlenc, "tag-tolerant xmlenc"),
        (plain[-60:].strip() if len(plain) > 60 else plain, "tag-tolerant tail60"),
    ]:
        if search and len(search) >= 10:
            result = _tag_tolerant_find(search, cleaned_content, slabel)
            if result != -1:
                # Adjust position back to original content
                original_pos = _adjust_position_for_stripped_footnotes(result, footnotes)
                print(f"  [ANCHOR MATCH] Tag-tolerant adjusted: {result} → {original_pos}")
                return original_pos

    # Strategy 10: flexible tag-tolerant regex (handles punctuation around tagged tokens)
    for search, slabel in [
        (plain, "flex plain"),
        (plain_xmlenc, "flex xmlenc"),
        (plain[-100:].strip() if len(plain) > 100 else plain, "flex tail100"),
    ]:
        if search and len(search) >= 8:
            result = _tag_tolerant_find_flexible(search, cleaned_content, slabel)
            if result != -1:
                original_pos = _adjust_position_for_stripped_footnotes(result, footnotes)
                print(f"  [ANCHOR MATCH] Flex tag-tolerant adjusted: {result} → {original_pos}")
                return original_pos

    print(f"  [ANCHOR MATCH] All strategies failed for anchor: '{anchor}'")
    return -1


def find_anchor_in_content_with_context(
    anchor: str,
    content: str,
    full_para_where_footnote_is_present: str,
) -> int:
    """
    Try to find anchor only within paragraph context window first.
    Returns insertion point in ORIGINAL content coordinates, or -1.
    """
    cleaned_content, footnotes = _strip_footnotes_from_content(content)

    window_start, window_end, strategy = _find_context_window_in_cleaned_content(
        cleaned_content,
        full_para_where_footnote_is_present,
    )
    if window_start == -1:
        print(f"  [ANCHOR CONTEXT] No context window found (strategy={strategy})")
        return -1

    print(f"  [ANCHOR CONTEXT] Searching only within window {window_start}-{window_end} ({strategy})")
    window = cleaned_content[window_start:window_end]

    pos_in_window = find_anchor_in_content(anchor, window)
    if pos_in_window == -1:
        print("  [ANCHOR CONTEXT] Anchor not found inside context window")
        return -1

    pos_in_cleaned = window_start + pos_in_window
    original_pos = _adjust_position_for_stripped_footnotes(pos_in_cleaned, footnotes)
    print(f"  [ANCHOR CONTEXT] Matched in context window: cleaned={pos_in_cleaned}, adjusted={original_pos}")
    return original_pos


# ─────────────────────────────────────────────────────────────────────────────
# LLM ANCHOR PROMPT
# ─────────────────────────────────────────────────────────────────────────────

def build_footnote_anchor_prompt(
    section_xml: str,
    text_before_superscript: str,
    superscript: int,
    full_para_where_footnote_is_present: Optional[str] = None,
) -> str:
    paragraph_context_block = ""
    if full_para_where_footnote_is_present and str(full_para_where_footnote_is_present).strip():
        paragraph_context_block = f"""

FULL PARAGRAPH CONTEXT (where this footnote belongs):
"{str(full_para_where_footnote_is_present).strip()}"
"""

    return f"""You are analyzing a legal PXML XML snippet.

Your ONLY job is to find this EXACT text in the XML and return the XML substring that ends at it:

TARGET TEXT: "{text_before_superscript}"
{paragraph_context_block}

## Rules
- Search for the TARGET TEXT in the XML below (it may be split across lines or wrapped in tags like <ital>, <bold>, <cite>)
- If TARGET TEXT appears multiple times, choose the occurrence inside/closest to the FULL PARAGRAPH CONTEXT
- Return the last 60-80 characters of XML content that ends EXACTLY at the END of the TARGET TEXT
- The returned string must appear VERBATIM in the XML — copy character by character
- Do NOT include any <footnote>...</footnote> block in the returned anchor
- If footnote markup appears before the target, skip it and return anchor text that ends at TARGET TEXT but excludes footnote tags
- NEVER use '...' or ellipsis to skip content — if the passage is long, start closer to the end
- Do NOT return anything that comes AFTER the TARGET TEXT
- Do NOT include the next word, tag, or punctuation after the TARGET TEXT
- Do NOT make up or paraphrase — only copy from the XML
- Return ONLY the anchor string, nothing else, no explanation, no markdown, no code fences

{XML_ENTITY_RULES}

## XML:
{section_xml}
"""


# ─────────────────────────────────────────────────────────────────────────────
# FOOTNOTE TAG BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build_footnote_tag(superscript: int, footnote_text: str, client=None, model_name: str = None) -> str:
    """
    Build the footnote XML tag with proper entity encoding and case citation tagging.
    If client and model_name are provided, uses LLM to properly tag case citations and URLs.

    ORDER MATTERS:
      1. LLM tagging runs FIRST on the raw plain text — so the citation regex works
         reliably and the LLM gets clean input.
      2. Entity-encoding runs SECOND but only on the TEXT NODES, not on LLM-generated
         XML markup (cite/ref attributes, tags). We therefore skip a second encoding
         pass when the LLM has already returned XML.
    """
    print(f"  [FOOTNOTE INPUT] len={len(footnote_text or '')} | '{_preview_for_log(footnote_text)}'")

    # Step 1: LLM citation/URL tagging on raw plain text (BEFORE any entity encoding)
    if client and model_name:
        tagged_text = _tag_footnote_content_with_llm(client, model_name, footnote_text)
        print(
            f"  [FOOTNOTE TAGGED] len={len(tagged_text or '')} | "
            f"'{_preview_for_log(tagged_text)}'"
        )
    else:
        tagged_text = footnote_text
        print("  [FOOTNOTE TAGGED] skipped (no LLM client/model) — using input text")

    # Step 2: Entity-encode ONLY if the LLM did NOT already produce XML markup.
    # If the LLM returned XML (contains < or &), trust its encoding and skip.
    if '<' in tagged_text or '&' in tagged_text:
        # LLM already produced XML — use as-is (it followed XML_ENTITY_RULES)
        encoded_text = tagged_text
        print("  [FOOTNOTE ENCODE] skipped (already contains XML/entity markup)")
    else:
        # No XML produced — apply entity encoding to the plain text
        encoded_text = _encode_xml_entities(tagged_text)
        print(
            f"  [FOOTNOTE ENCODE] applied | len={len(encoded_text or '')} | "
            f"'{_preview_for_log(encoded_text)}'"
        )

    footnote_xml = (
        f'<footnote date.updated="1" date.trace="1">'
        f'<footnote.reference date.updated="0">{superscript}</footnote.reference>'
        f'<footnote.body date.updated="0">'
        f'<para date.updated="0">'
        f'<para.text date.updated="0">{encoded_text}</para.text>'
        f'</para>'
        f'</footnote.body>'
        f'</footnote>'
    )

    print(
        f"  [FOOTNOTE XML READY] len={len(footnote_xml)} | "
        f"'{_preview_for_log(footnote_xml)}'"
    )

    return footnote_xml


def _sanitize_case_citation_markup(text: str) -> str:
    """
    Enforce house style for case citations returned by LLM:
      1) Remove <ital>...</ital> tags inside <cite> blocks.

    Note: Court parenthetical placement (e.g., (F.C.A.)) is intentionally
    left to the LLM output; no deterministic post-move is applied.
    """
    if not text:
        return text

    # Remove italics only inside cite blocks
    cite_block_pattern = re.compile(r'<cite\b[^>]*>[\s\S]*?</cite>', re.DOTALL | re.IGNORECASE)

    def _strip_ital_inside_cite(m: re.Match) -> str:
        block = m.group(0)
        # EXCEPTION: keep italics on the Latin editorial phrase "sub nom." — protect
        # it before the blanket strip, then restore it afterwards.
        _protected_ital = []

        def _protect_ital(pm: re.Match) -> str:
            _protected_ital.append(pm.group(0))
            return f"\x00ITAL{len(_protected_ital) - 1}\x00"

        block = re.sub(
            r'<ital\b[^>]*>\s*sub\s+nom(?:\.|ine)?\s*</ital>',
            _protect_ital, block, flags=re.IGNORECASE
        )
        block = re.sub(r'</?ital\b[^>]*>', '', block, flags=re.IGNORECASE)
        for _i, _val in enumerate(_protected_ital):
            block = block.replace(f"\x00ITAL{_i}\x00", _val)
        return block

    text = cite_block_pattern.sub(_strip_ital_inside_cite, text)
    return text


def _tag_footnote_content_with_llm(client, model_name: str, footnote_text: str) -> str:
    """
    Use LLM to properly tag case citations and URLs in footnote text.
    Returns the footnote text with proper <cite>/<ref> tagging.
    """
    if not footnote_text or not str(footnote_text).strip():
        return footnote_text
    
    prompt = load_prompt(
        "new_footnote_tagging.txt",
        CASE_CITATION_RULES=CASE_CITATION_RULES,
        URL_TAGGING_RULES=URL_TAGGING_RULES,
        XML_ENTITY_RULES=XML_ENTITY_RULES,
        footnote_text=footnote_text,
    )
    
    try:
        response = call_anthropic(client, model_name, prompt, max_tokens=2000)
        # Basic validation - make sure we got something back
        if response and len(response) > 10:
            return _sanitize_case_citation_markup(response.strip())
    except Exception as e:
        print(f"  [WARN] LLM tagging failed: {e}")
    
    return footnote_text


# ─────────────────────────────────────────────────────────────────────────────
# SAFE INSERT WITH VALIDATION
# ─────────────────────────────────────────────────────────────────────────────

def safe_insert_footnote(
    content: str,
    insertion_point: int,
    footnote_tag: str,
) -> str:
    """
    Inserts footnote_tag at insertion_point with full before/after validation.
    Raises AssertionError if any content loss is detected.
    """
    original_length = len(content)
    original_before = content[:insertion_point]
    original_after  = content[insertion_point:]

    updated = original_before + footnote_tag + original_after

    assert updated[:insertion_point] == original_before, \
        "CONTENT LOSS: text before insertion point was modified!"

    assert updated[insertion_point + len(footnote_tag):] == original_after, \
        "CONTENT LOSS: text after insertion point was modified!"

    assert len(updated) == original_length + len(footnote_tag), \
        f"LENGTH MISMATCH: expected {original_length + len(footnote_tag)}, got {len(updated)}"

    assert footnote_tag in updated, \
        "INSERTION FAILURE: footnote tag not found in updated content!"

    print(f"  [VALIDATE] Original: {original_length} | +{len(footnote_tag)} | Updated: {len(updated)} ✓")

    # Helpful preview around insertion point so operators can see exact inserted XML context.
    preview_start = max(0, insertion_point - 140)
    preview_end = min(len(updated), insertion_point + len(footnote_tag) + 140)
    insertion_preview = updated[preview_start:preview_end]
    print(f"  [INSERT PREVIEW] ...{_preview_for_log(insertion_preview, max_len=700)}...")
    return updated


# ─────────────────────────────────────────────────────────────────────────────
# ANCHOR ALIGNMENT VALIDATOR
# ─────────────────────────────────────────────────────────────────────────────

def validate_anchor_alignment(
    anchor: str,
    text_before_superscript: str,
) -> bool:
    def normalize(text: str) -> str:
        text = text.replace('\u2018', "'").replace('\u2019', "'")
        text = text.replace('\u201c', '"').replace('\u201d', '"')
        text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
        text = text.replace('&apos;', "'").replace('&quot;', '"')
        # ── ADD THESE ─────────────────────────────────────────────────────────
        text = text.replace('&ldquo;', '"').replace('&rdquo;', '"')
        text = text.replace('&lsquo;', "'").replace('&rsquo;', "'")
        text = text.replace('&mdash;', '\u2014').replace('&ndash;', '\u2013')
        # ──────────────────────────────────────────────────────────────────────
        text = re.sub(r'\s+', ' ', text).strip().lower()
        return text

    anchor_plain      = normalize(strip_xml_tags(anchor))
    target_plain      = normalize(strip_xml_tags(text_before_superscript))

    # Take last 40 chars of target as the key signal
    target_tail       = target_plain[-40:].strip()
    target_tail_words = target_tail.split()

    if not target_tail_words:
        return False

    # Check: last 3 words of target appear in anchor
    last_3_words = " ".join(target_tail_words[-3:])
    if last_3_words in anchor_plain:
        print(f"  [ANCHOR VALID] Last 3 words '{last_3_words}' found in anchor ✓")
        return True

    # Check: last 2 words (looser fallback)
    last_2_words = " ".join(target_tail_words[-2:])
    if last_2_words in anchor_plain:
        print(f"  [ANCHOR VALID] Last 2 words '{last_2_words}' found in anchor ✓")
        return True

    # Check: tail overlap between anchor end and target tail
    anchor_tail = anchor_plain[-60:].strip()
    if target_tail in anchor_tail or anchor_tail in target_tail:
        print(f"  [ANCHOR VALID] Tail overlap found ✓")
        return True

    print(f"  [ANCHOR INVALID] Anchor does not align with text_before_superscript!")
    print(f"    Expected tail : '{target_tail}'")
    print(f"    Anchor plain  : '...{anchor_plain[-80:]}'")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# CORE ROW PROCESSOR
# ─────────────────────────────────────────────────────────────────────────────

def insert_footnote_for_row(
    row: pd.Series,
    section_dict: dict,
    pxml_content: str,
    client,
    model_name: str,
    failed_rows_list: list = None,
) -> str:

    row_idx                  = row.get("idx", "?")
    section_str              = str(row.get("section", "")).strip()
    text_before_superscript  = str(row.get("text_before_superscript", "")).strip()
    full_para_where_footnote_is_present = str(row.get("full_para_where_footnote_is_present", "")).strip()
    if full_para_where_footnote_is_present.lower() in {"nan", "none"}:
        full_para_where_footnote_is_present = ""
    # superscript              = int(row.get("superscript", 0))  # OLD: from 'superscript' column
    # NEW: Use pdf_superscript when confidence is high or medium
    _confidence = str(row.get("footnote_ref_confidence", "")).strip().lower()
    if _confidence in ("high", "medium") and pd.notna(row.get("pdf_superscript")) and str(row.get("pdf_superscript", "")).strip().isdigit():
        superscript = int(row.get("pdf_superscript"))
    else:
        superscript = int(row.get("superscript", 0))
    footnote_text            = str(row.get("text", "")).strip()

    print(f"\n[ROW {row_idx}] section='{section_str}' | superscript={superscript}")
    print(f"  [TEXT_BEFORE] '...{text_before_superscript[-80:]}'")
    print(f"  [FULL_PARA] {'present' if full_para_where_footnote_is_present else 'missing'}")

    # Skip rows with empty footnote text — nothing to insert
    if not footnote_text:
        print(f"  [SKIP] Footnote text is empty, nothing to insert.")
        return pxml_content

    def _log_failure(reason):
        if failed_rows_list is not None:
            failed_rows_list.append({
                "idx": row_idx,
                "section": section_str,
                "superscript": superscript,
                "reason": reason,
                "text_before_superscript": text_before_superscript[:300],
                "full_para_where_footnote_is_present": full_para_where_footnote_is_present[:500],
                "footnote_text": footnote_text[:300],
            })

    # 1. Resolve section key
    section_key = find_section_key(section_str, section_dict, client, model_name)
    if section_key is None:
        print(f"  [SKIP] Could not resolve section key.")
        _log_failure(f"REVISION FAILED - Could not find section '{section_str}' in document")
        return pxml_content

    # Gather all candidate keys (primary + any __dup variants) for fallback
    base_key = section_key.split('__dup')[0]
    all_candidate_keys = [k for k in section_dict if k == base_key or k.startswith(f"{base_key}__dup")]
    if len(all_candidate_keys) > 1:
        print(f"  [DUP] Found {len(all_candidate_keys)} sections for designator '{base_key}': {all_candidate_keys}")

    section_xml = section_dict[section_key]

    # 2. Try Python-first direct match on section_xml (not full pxml)
    if full_para_where_footnote_is_present:
        print("  [DIRECT] Attempt 1: context-aware direct match")
        insertion_point = find_insertion_point_directly(
            text_before_superscript,
            section_xml,
            full_para_where_footnote_is_present=full_para_where_footnote_is_present,
        )
        if insertion_point != -1:
            print("  [DIRECT] Context-aware direct match PASSED")
        else:
            print("  [DIRECT] Context-aware direct match FAILED")
    else:
        insertion_point = -1

    if insertion_point == -1:
        print("  [DIRECT] Attempt 2: generic direct match")
        insertion_point = find_insertion_point_directly(text_before_superscript, section_xml)
        if insertion_point != -1:
            print("  [DIRECT] Generic direct match PASSED")
        else:
            print("  [DIRECT] Generic direct match FAILED")

    # 3. LLM fallback only if Python couldn't find it
    if insertion_point == -1:
        print(f"  [FALLBACK] Direct match failed, calling LLM for anchor...")

        max_anchor_retries = 3
        anchor_prompt = build_footnote_anchor_prompt(
            section_xml,
            text_before_superscript,
            superscript,
            full_para_where_footnote_is_present=full_para_where_footnote_is_present,
        )

        for anchor_attempt in range(1, max_anchor_retries + 1):
            anchor_raw = _call_llm_prefilled(
                client, model_name, anchor_prompt, max_tokens=3000
            )
            print(f"  [ANCHOR RAW] attempt {anchor_attempt} → '{anchor_raw}'")

            anchor = _extract_clean_anchor(anchor_raw)
            if anchor != anchor_raw:
                print(f"  [ANCHOR CLEAN] Extracted: '{anchor}'")

            # ── NEW: reject ellipsis hallucination immediately ────────────────
            anchor_plain_check = strip_xml_tags(anchor)
            if re.search(r'(?<!\.)\.\.\.(?!\.)', anchor_plain_check):
                print(f"  [ANCHOR RETRY] Anchor contains ellipsis '...' (LLM hallucination), retrying...")
                anchor_prompt = (
                    f"Your previous answer was WRONG. You used '...' to skip content.\n\n"
                    f"You MUST copy the XML CHARACTER FOR CHARACTER — no ellipsis, no summarising.\n"
                    f"Find and return the last 80 characters of XML that end EXACTLY at:\n"
                    f'"{text_before_superscript[-80:]}"\n\n'
                    f"Do NOT use '...' under any circumstances.\n\n"
                    f"XML:\n{section_xml}"
                )
                continue
            # ─────────────────────────────────────────────────────────────────

            if not validate_anchor_alignment(anchor, text_before_superscript):
                print(f"  [ANCHOR RETRY] Anchor misaligned, retrying...")
                anchor_prompt = (
                    f"Your previous answer was WRONG. It did not end at the target text.\n\n"
                    f"You MUST find and return an anchor that ends EXACTLY at:\n"
                    f'"{text_before_superscript[-80:]}"\n\n'
                    f"The LAST words of your anchor MUST BE the LAST words of the t\arget text above.\n"
                    f"Do not return anything that comes after the target text.\n"
                    f"Do NOT use '...' — copy verbatim from the XML.\n\n"
                    f"XML:\n{section_xml}"
                )
                continue

            if full_para_where_footnote_is_present:
                insertion_point = find_anchor_in_content_with_context(
                    anchor,
                    section_xml,
                    full_para_where_footnote_is_present,
                )
                if insertion_point == -1:
                    print("  [ANCHOR] Context-constrained match failed; falling back to full section search")
                    insertion_point = find_anchor_in_content(anchor, section_xml)
                    if insertion_point != -1:
                        print("  [ANCHOR] Full section fallback PASSED")
            else:
                insertion_point = find_anchor_in_content(anchor, section_xml)

            if insertion_point != -1:
                break

            print(f"  [ANCHOR RETRY] Anchor aligned but not found in section, retrying...")

    # 3a. If not found and duplicates exist, try other sections with same designator
    if insertion_point == -1 and len(all_candidate_keys) > 1:
        for alt_key in all_candidate_keys:
            if alt_key == section_key:
                continue
            alt_xml = section_dict[alt_key]
            print(f"  [DUP FALLBACK] Trying alternate section '{alt_key}' (len={len(alt_xml)})...")

            # Try direct match on alternate section
            if full_para_where_footnote_is_present:
                insertion_point = find_insertion_point_directly(
                    text_before_superscript, alt_xml,
                    full_para_where_footnote_is_present=full_para_where_footnote_is_present,
                )
            if insertion_point == -1:
                insertion_point = find_insertion_point_directly(text_before_superscript, alt_xml)

            if insertion_point != -1:
                print(f"  [DUP FALLBACK] Found in alternate section '{alt_key}'!")
                section_key = alt_key
                section_xml = alt_xml
                break

    if insertion_point == -1:
        print(f"  [WARN] Could not locate insertion point in section. Skipping row {row_idx}.")
        _log_failure("REVISION FAILED - Could not locate insertion point in document")
        return pxml_content

    # 3c. Move out of trace-deleted spans (these are collapsed in postprocessing)
    insertion_point = _adjust_out_of_trace_deleted(section_xml, insertion_point)

    # 3d. Adjust past closing inline tags
    insertion_point = _adjust_past_inline_close(section_xml, insertion_point)

    # 4. Build footnote tag
    footnote_tag = build_footnote_tag(superscript, footnote_text, client, model_name)

    # 5. Insert into section_xml
    try:
        modified_section = safe_insert_footnote(section_xml, insertion_point, footnote_tag)
        print(f"  [OK] Footnote {superscript} inserted at position {insertion_point} in section '{section_key}'.")
    except AssertionError as e:
        print(f"  [ERROR] Insertion validation failed for row {row_idx}: {e}")
        _log_failure(f"REVISION FAILED - Insertion validation error")
        return pxml_content

    # 6. Replace old section in pxml_content with modified section
    pos = pxml_content.find(section_xml)
    if pos != -1:
        pxml_content = (
            pxml_content[:pos] + modified_section + pxml_content[pos + len(section_xml):]
        )
        print(f"  [OK] Section '{section_key}' spliced back into pxml at position {pos}.")

        # Post-splice verification logs (exact + relaxed markers)
        exact_in_section = footnote_tag in modified_section
        exact_in_doc = footnote_tag in pxml_content
        ref_marker = f"<footnote.reference date.updated=\"0\">{superscript}</footnote.reference>"
        ref_count_doc = pxml_content.count(ref_marker)
        print(
            f"  [VERIFY] exact_in_section={exact_in_section} | "
            f"exact_in_doc={exact_in_doc} | ref_marker_count_in_doc={ref_count_doc}"
        )

        if exact_in_doc:
            exact_pos = pxml_content.find(footnote_tag)
            verify_start = max(0, exact_pos - 120)
            verify_end = min(len(pxml_content), exact_pos + len(footnote_tag) + 120)
            verify_preview = pxml_content[verify_start:verify_end]
            print(f"  [VERIFY PREVIEW] ...{_preview_for_log(verify_preview, max_len=700)}...")
        else:
            print("  [VERIFY WARN] Exact footnote XML not found in full document after splice.")
            # Relaxed probe by superscript reference marker
            ref_pos = pxml_content.find(ref_marker)
            if ref_pos != -1:
                verify_start = max(0, ref_pos - 180)
                verify_end = min(len(pxml_content), ref_pos + 300)
                verify_preview = pxml_content[verify_start:verify_end]
                print(f"  [VERIFY PREVIEW REF] ...{_preview_for_log(verify_preview, max_len=700)}...")
    else:
        print(f"  [WARN] Could not find original section in pxml to splice back. Skipping.")
        _log_failure("REVISION FAILED - Could not splice section back into document")
        return pxml_content

    # 7. Update section_dict with modified section for subsequent rows
    section_dict[section_key] = modified_section

    return pxml_content

# Import shared parser — single source of truth for all pipelines
from .pxml_parser import parse_pxml_sections as _parse_shared


def parse_pxml_sections_from_str_dict_positions(pxml_content):
    """Parse PXML sections with duplicate handling. Delegates to shared parser."""
    return _parse_shared(pxml_content, track_duplicates=True)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def process_all_footnote_insertions(
    all_inserted_footnote_revisions: pd.DataFrame,
    pxml_content: str,
    model_name: str = MODEL_SONNET,
    failed_log_path: Optional[str] = None,
) -> str:
    """
    Main entry point. Iterates all rows and inserts footnotes into pxml_content.
    """
    client = build_anthropic_client(model_name)

    section_dict, section_positions, pxml_content = parse_pxml_sections_from_str_dict_positions(
        pxml_content
    )
    print(f"[INFO] Parsed {len(section_dict)} sections: {list(section_dict.keys())}")

    # Safety backup
    pxml_backup  = pxml_content
    failed_rows_list = []  # detailed failure tracking

    for _, row in all_inserted_footnote_revisions.iterrows():
        pxml_content = insert_footnote_for_row(
            row=row,
            section_dict=section_dict,
            pxml_content=pxml_content,
            client=client,
            model_name=model_name,
            failed_rows_list=failed_rows_list,
        )

    print(f"\n[DONE] Processed {len(all_inserted_footnote_revisions)} rows.")
    if failed_rows_list:
        print(f"[WARN] Skipped {len(failed_rows_list)} rows (no insertion)")
        print(f"[INFO] {len(failed_rows_list)} rows prepared for consolidated Excel")
    else:
        print("[INFO] All rows inserted successfully.")

    return pxml_content, failed_rows_list



