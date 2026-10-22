import pandas as pd
import re
import json
import difflib
import html
import os
import io
import base64
import time
from .config import find_section_key
from .xml_entity_rules import XML_ENTITY_RULES, CASE_CITATION_RULES, URL_TAGGING_RULES
from .prompt_loader import load_prompt

try:
    import fitz  # PyMuPDF
    FITZ_AVAILABLE = True
except ImportError:
    FITZ_AVAILABLE = False


def _encode_xml_entities(text: str) -> str:
    """Encode special characters as XML entities for PXML content."""
    if not text:
        return text
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
    parts = text.split('"')
    if len(parts) > 1:
        rebuilt = parts[0]
        for i, part in enumerate(parts[1:], 1):
            rebuilt += ('&ldquo;' if i % 2 == 1 else '&rdquo;') + part
        text = rebuilt
    text = text.replace("'", '&apos;')
    text = text.replace('\u2014', '&mdash;')
    text = text.replace('\u2013', '&ndash;')
    return text


def _strip_tags(text: str) -> str:
    """Remove XML tags, entities, special chars, normalize whitespace."""
    if not text:
        return ''
    text = html.unescape(text)
    text = text.replace('\u2018', "'").replace('\u2019', "'")
    text = text.replace('\u201c', '"').replace('\u201d', '"')
    text = text.replace('\u2013', '-').replace('\u2014', '-')
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'[►▶→•§]+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _normalize_for_match(text: str) -> str:
    """Normalize plain text for fuzzy anchor validation comparisons."""
    text = _strip_tags(text)
    if not text:
        return ''
    text = text.lower()
    text = re.sub(r'["\']+', ' ', text)
    text = re.sub(r'[^\w\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _normalize_split_italic_case_names(text: str) -> str:
    """Merge split italic spans around case-name 'v.' into one continuous <ital> span."""
    if not text:
        return text

    pattern = re.compile(
        r'<ital>([^<]*?)</ital>\s*v\.\s*<ital>\s*([^<]*?)</ital>',
        re.IGNORECASE,
    )

    previous = None
    while previous != text:
        previous = text
        text = pattern.sub(lambda m: f'<ital>{m.group(1).rstrip()} v. {m.group(2).lstrip()}</ital>', text)

    return text


# ─────────────────────────────────────────────────────────────────────────────
# PDF IMAGE CUE: Find best matching pages and convert to base64 images
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_for_pdf_search(text: str) -> str:
    """Normalize text for fuzzy matching against PDF page text."""
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&[a-zA-Z0-9#]+;', ' ', text)
    text = text.replace('\u2018', "'").replace('\u2019', "'")
    text = text.replace('\u201c', '"').replace('\u201d', '"')
    text = text.replace('\u2013', '-').replace('\u2014', '-')
    text = re.sub(r'\s+', ' ', text).strip().lower()
    return text


def _score_page_for_text(page_text_norm: str, query_norm: str) -> float:
    """Score how well a page matches the query text using word overlap and n-grams."""
    if not page_text_norm or not query_norm:
        return 0.0

    query_words = query_norm.split()
    page_words_set = set(page_text_norm.split())

    if not query_words:
        return 0.0

    # Word overlap score
    word_hits = sum(1 for w in query_words if w in page_words_set and len(w) > 2)
    word_score = word_hits / max(len([w for w in query_words if len(w) > 2]), 1)

    # N-gram (5-word) substring score
    ngram_size = 5
    ngram_hits = 0
    total_ngrams = 0
    for i in range(len(query_words) - ngram_size + 1):
        ngram = ' '.join(query_words[i:i + ngram_size])
        total_ngrams += 1
        if ngram in page_text_norm:
            ngram_hits += 1

    ngram_score = ngram_hits / max(total_ngrams, 1)

    # Combined score: n-gram matches are stronger signal
    return 0.4 * word_score + 0.6 * ngram_score


def find_best_matching_pages(new_text: str, previous_para: str, pdf_path: str,
                             top_n: int = 2) -> list:
    """
    Find the top_n PDF pages that best match the new_text + previous_para content
    using fuzzy matching.

    Args:
        new_text: The new paragraph text being inserted
        previous_para: The previous paragraph context
        pdf_path: Path to the PDF file
        top_n: Number of best matching pages to return (default 2)

    Returns:
        list of (page_number_1based, score) tuples, sorted by score descending
    """
    if not FITZ_AVAILABLE:
        print("  [IMAGE CUE] PyMuPDF (fitz) not available, skipping image cue.")
        return []

    if not pdf_path or not os.path.isfile(pdf_path):
        print(f"  [IMAGE CUE] PDF not found: {pdf_path}")
        return []

    try:
        doc = fitz.open(pdf_path)
        page_texts = [page.get_text("text") for page in doc]
        doc.close()
    except Exception as e:
        print(f"  [IMAGE CUE] Error reading PDF: {e}")
        return []

    if not page_texts:
        return []

    # Normalize page texts
    norm_pages = [_normalize_for_pdf_search(pt) for pt in page_texts]

    # Build combined query from new_text + previous_para
    combined_query = f"{previous_para or ''} {new_text or ''}"
    norm_query = _normalize_for_pdf_search(combined_query)

    if len(norm_query.split()) < 3:
        return []

    # Score each page
    page_scores = []
    for i, norm_page in enumerate(norm_pages):
        score = _score_page_for_text(norm_page, norm_query)
        if score > 0.05:  # Minimum threshold
            page_scores.append((i + 1, score))  # 1-based page number

    # Sort by score descending and take top_n
    page_scores.sort(key=lambda x: x[1], reverse=True)
    return page_scores[:top_n]


def convert_pdf_pages_to_base64_images(pdf_path: str, page_numbers: list,
                                        dpi: int = 150) -> list:
    """
    Convert specific PDF pages to base64-encoded PNG images.

    Args:
        pdf_path: Path to the PDF file
        page_numbers: List of 1-based page numbers to convert
        dpi: Resolution for rendering (default 150 for good quality without too much size)

    Returns:
        list of dicts: [{"page": page_num, "base64": base64_string}, ...]
    """
    if not FITZ_AVAILABLE:
        return []

    if not pdf_path or not os.path.isfile(pdf_path):
        return []

    try:
        doc = fitz.open(pdf_path)
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        results = []

        for page_num in page_numbers:
            if page_num < 1 or page_num > len(doc):
                continue
            page = doc[page_num - 1]  # 0-based index
            pix = page.get_pixmap(matrix=matrix)
            img_bytes = pix.tobytes("png")
            b64_str = base64.standard_b64encode(img_bytes).decode("utf-8")
            results.append({"page": page_num, "base64": b64_str})

        doc.close()
        return results
    except Exception as e:
        print(f"  [IMAGE CUE] Error converting pages to images: {e}")
        return []


def get_image_cue_for_insertion(new_text: str, previous_para: str,
                                pdf_path: str) -> list:
    """
    Main entry point: find the 2 best matching PDF pages for the given
    new_text + previous_para, convert them to base64 images.

    Args:
        new_text: The new paragraph text being inserted
        previous_para: The previous paragraph context
        pdf_path: Path to the PDF file

    Returns:
        list of base64 image dicts: [{"page": N, "base64": "..."}, ...]
        Empty list if no match or PDF not available.
    """
    best_pages = find_best_matching_pages(new_text, previous_para, pdf_path, top_n=2)

    if not best_pages:
        print("  [IMAGE CUE] No matching pages found in PDF.")
        return []

    page_numbers = [p[0] for p in best_pages]
    print(f"  [IMAGE CUE] Best matching pages: {[(p, f'{s:.3f}') for p, s in best_pages]}")

    images = convert_pdf_pages_to_base64_images(pdf_path, page_numbers)
    print(f"  [IMAGE CUE] Converted {len(images)} page(s) to images for LLM cue.")
    return images


def return_closest_previous_par(result_with_ba, all_newline_insertions):
    """
    For each row in all_newline_insertions, find the closest previous idx
    from result_with_ba and extract the appropriate text based on type:
      - inline_deletion   → text_before
      - inline_insertion   → text_after
      - inline_replacement → text_after
      - '' (no change)     → text
    If the extracted value is empty, keep walking backwards until a non-empty one is found.
    """
    # Prepare a sorted, deduplicated lookup from result_with_ba
    lookup_df = result_with_ba.drop_duplicates(subset=['idx'], keep='first').sort_values('idx').reset_index(drop=True)

    def _get_effective_text(row):
        """Get the appropriate text column based on row type."""
        row_type = str(row.get('type', '')).strip()
        if row_type == 'inline_deletion':
            return str(row.get('text_before', '') or '').strip()
        elif row_type == 'inline_insertion':
            return str(row.get('text_after', '') or '').strip()
        elif row_type == 'inline_replacement':
            return str(row.get('text_after', '') or '').strip()
        else:
            # No change or other type → use 'text' column
            return str(row.get('text', '') or '').strip()

    def get_previous_text(current_idx):
        print(f"\n{'='*60}")
        print(f"Looking for previous idx for current idx: {current_idx}")

        # Get all rows with idx < current_idx, sorted descending
        candidates = lookup_df[lookup_df['idx'] < current_idx].sort_values('idx', ascending=False)

        # Filter out footnotes if the column exists
        if 'is_footnote_x' in candidates.columns:
            candidates = candidates[candidates['is_footnote_x'] == False]
        elif 'is_footnote' in candidates.columns:
            candidates = candidates[candidates['is_footnote'] == False]

        for _, row in candidates.iterrows():
            effective_text = _get_effective_text(row)
            if effective_text and len(effective_text) >=1:
                print(f"  WINNER -> idx: {row['idx']} | type: {row.get('type', '')} | text: {effective_text[:80]}")
                return effective_text
            else:
                print(f"  SKIP -> idx: {row['idx']} | type: {row.get('type', '')} | text is empty, checking next...")

        print(f"  RESULT -> No non-empty previous idx found. Returning None.")
        return None

    print("Starting previous_para lookup...\n")
    all_newline_insertions['previous_para'] = all_newline_insertions['idx'].apply(get_previous_text)

    print(f"\n{'='*60}")
    print("DONE!")
    print(f"Total rows processed : {len(all_newline_insertions)}")
    print(f"previous_para filled : {all_newline_insertions['previous_para'].notnull().sum()}")
    print(f"previous_para null   : {all_newline_insertions['previous_para'].isnull().sum()}")
    return all_newline_insertions


# ── Rate limit handling settings ──────────────────────────────────────────────
MAX_RETRIES_RATE_LIMIT = 5
INITIAL_RETRY_DELAY = 10  # seconds
MAX_RETRY_DELAY = 120  # seconds


def call_anthropic(client, model_name: str, prompt: str, max_tokens: int = 10000) -> str:
    """Call Anthropic API with exponential backoff retry on rate limit errors."""
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
                    retry_delay = min(retry_delay * 2, MAX_RETRY_DELAY)
                else:
                    print(f"  [RATE LIMIT] Max retries ({MAX_RETRIES_RATE_LIMIT}) exceeded.")
                    raise
            else:
                raise
    
    raise Exception("Max retries exceeded")


def call_anthropic_with_vision(client, model_name: str, content, max_tokens: int = 16000) -> str:
    """
    Call Anthropic API with vision support (images + text).
    
    Args:
        client: Anthropic client
        model_name: Model to use
        content: Either a plain string (text-only) or a list of content blocks 
                 (for multimodal with images)
        max_tokens: Maximum response tokens
    
    Returns:
        str: The LLM response text
    """
    retry_delay = INITIAL_RETRY_DELAY

    for attempt in range(MAX_RETRIES_RATE_LIMIT):
        try:
            message = client.messages.create(
                model=model_name,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": content}],
            )
            return message.content[0].text.strip()

        except Exception as e:
            error_str = str(e)
            if "rate_limit" in error_str.lower() or "429" in error_str:
                if attempt < MAX_RETRIES_RATE_LIMIT - 1:
                    print(f"  [RATE LIMIT] Attempt {attempt + 1}/{MAX_RETRIES_RATE_LIMIT} - Waiting {retry_delay}s before retry...")
                    time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, MAX_RETRY_DELAY)
                else:
                    print(f"  [RATE LIMIT] Max retries ({MAX_RETRIES_RATE_LIMIT}) exceeded.")
                    raise
            else:
                raise

    raise Exception("Max retries exceeded")


# ─────────────────────────────────────────────────────────────────────────────
# ANCHOR CLASSIFICATION
# ─────────────────────────────────────────────────────────────────────────────

# Tags whose closing marks a clean paragraph-level boundary.
# Insertion AFTER the verbatim anchor is correct when the anchor ends with one
# of these tags — no block-close walking needed.
_PARA_BOUNDARY_CLOSE_TAGS = {
    '</para>', '</stat.item.para>', '</heading.para>',
    '</list.item>', '</clause>', '</head>',
}

# Tags that are valid boundaries but are CHILD containers — the anchor ends
# here only when the LLM explicitly chose this as the outer boundary.
_CHILD_BOUNDARY_CLOSE_TAGS = {
    '</quote.block>', '</list>', '</table>',
}

_ALL_BOUNDARY_CLOSE_TAGS = _PARA_BOUNDARY_CLOSE_TAGS | _CHILD_BOUNDARY_CLOSE_TAGS

# Tags that are NOT paragraph boundaries — insertion after their close should
# stay verbatim (mid-para, e.g. after </footnote> inside <para.text>).
_MID_PARA_CLOSE_TAGS = {
    '</footnote>', '</footnote.body>', '</footnote.reference>',
    '</cite>', '</ref>', '</ital>', '</bold>',
    '</para.text>',
}


def _anchor_ends_at_boundary(insert_after: str) -> bool:
    """
    Return True when insert_after ends at a clean paragraph-level boundary tag,
    meaning the new paragraph should go AFTER the entire enclosing block.

    Return False when insert_after ends mid-paragraph (e.g. after </footnote>
    inside <para.text>) — in that case we insert verbatim at anchor end.
    """
    stripped = insert_after.rstrip()
    for tag in _ALL_BOUNDARY_CLOSE_TAGS:
        if stripped.endswith(tag):
            return True
    return False


def _anchor_ends_mid_para(insert_after: str) -> bool:
    """
    Return True when insert_after ends with a mid-paragraph tag like </footnote>.
    In this case we insert directly after the verbatim anchor position.
    """
    stripped = insert_after.rstrip()
    for tag in _MID_PARA_CLOSE_TAGS:
        if stripped.endswith(tag):
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# LLM VALIDATION
# ─────────────────────────────────────────────────────────────────────────────

def _extract_paragraph_plain_text_ending_at(section_xml: str, insert_pos: int) -> str:
    """
    Walk backwards from insert_pos to find the opening tag of the paragraph
    that ends at insert_pos, then extract the full plain text of that paragraph.
    """
    xml_before = section_xml[:insert_pos]
    opening_tags = ['<para ', '<para>', '<stat.item.para ', '<stat.item.para>',
                    '<heading.para ', '<heading.para>', '<list.item ', '<list.item>',
                    '<clause ', '<clause>']

    best_start = -1
    depth = 0
    i = len(xml_before) - 1
    while i >= 0:
        for ctag in ['</para>', '</stat.item.para>', '</heading.para>', '</list.item>', '</clause>']:
            end_idx   = i + 1
            start_idx = end_idx - len(ctag)
            if start_idx >= 0 and xml_before[start_idx:end_idx] == ctag:
                depth += 1
                i = start_idx - 1
                break
        else:
            for otag in opening_tags:
                if i - len(otag) + 1 >= 0 and xml_before[i - len(otag) + 1:i + 1].startswith(otag.rstrip(' >')):
                    tag_start = xml_before.rfind('<', 0, i + 1)
                    if tag_start >= 0:
                        depth -= 1
                        if depth <= 0:
                            best_start = tag_start
                            break
                        i = tag_start - 1
                        break
            else:
                i -= 1
                continue
            if best_start != -1:
                break

    if best_start == -1:
        best_start = max(0, insert_pos - 3000)

    para_xml = section_xml[best_start:insert_pos]
    return _strip_tags(para_xml)


def _validate_llm_anchor_position(
    client,
    model_name: str,
    section_xml: str,
    insert_after: str,
    previous_para: str,
    new_text: str,
    max_retries: int = 2,
) -> bool:
    """
    Validate that insert_after is the correct anchor by checking that
    previous_para content appears in the XML window before the anchor.
    """
    if not insert_after:
        print("    [LLM VALIDATE] Empty anchor received.")
        return False

    previous_plain = _strip_tags(previous_para).strip()
    if not previous_plain:
        print("    [LLM VALIDATE] previous_para empty after cleaning; accepting.")
        return True

    insert_after_plain = _strip_tags(insert_after).strip()
    norm_previous      = _normalize_for_match(previous_para)
    norm_anchor_plain  = _normalize_for_match(insert_after)

    def _match_previous_in_window(window_plain_text: str, label: str) -> bool:
        if not window_plain_text or not norm_previous:
            return False
        for tail_len in [220, 180, 140, 100, 80, 60, 40, 20]:
            tail = norm_previous if len(norm_previous) < tail_len else norm_previous[-tail_len:]
            if tail and tail in window_plain_text:
                print(f"    [LLM VALIDATE] ✓ previous_para tail ({tail_len} chars) found in {label}.")
                return True
        for head_len in [80, 60, 40, 20]:
            head = norm_previous if len(norm_previous) < head_len else norm_previous[:head_len]
            if head and head in window_plain_text:
                print(f"    [LLM VALIDATE] ✓ previous_para head ({head_len} chars) found in {label}.")
                return True
        words = norm_previous.split()
        for n in [18, 14, 10, 8, 6]:
            if len(words) >= n:
                phrase = ' '.join(words[-n:])
                if phrase and phrase in window_plain_text:
                    print(f"    [LLM VALIDATE] ✓ previous_para last {n} words found in {label}.")
                    return True
        return False

    if norm_anchor_plain and norm_previous:
        if norm_anchor_plain.endswith(norm_previous):
            print("    [LLM VALIDATE] ✓ insert_after plain text ends with previous_para; accepting.")
            return True
        if len(norm_previous) >= 40 and norm_previous[-80:] in norm_anchor_plain:
            print("    [LLM VALIDATE] ✓ insert_after plain text contains previous_para tail; accepting.")
            return True
        if _match_previous_in_window(norm_anchor_plain, "insert_after plain text"):
            return True

    anchor_pos = section_xml.find(insert_after)
    norm_section = re.sub(r'\s+', ' ', section_xml)
    norm_anchor  = re.sub(r'\s+', ' ', insert_after)
    anchor_pos_n = norm_section.find(norm_anchor)
    context_plain_hint = ""

    if anchor_pos != -1:
        anchor_end   = anchor_pos + len(insert_after)
        window_size  = max(3000, len(previous_plain) * 4)
        window_start = max(0, anchor_end - window_size)
        window_xml   = section_xml[window_start:anchor_end]
        window_plain = _normalize_for_match(window_xml)
        if _match_previous_in_window(window_plain, "XML window around anchor"):
            return True
        context_plain_hint = window_plain
    elif anchor_pos_n != -1:
        anchor_end_n   = anchor_pos_n + len(norm_anchor)
        window_size_n  = max(3000, len(previous_plain) * 4)
        window_start_n = max(0, anchor_end_n - window_size_n)
        window_n       = norm_section[window_start_n:anchor_end_n]
        window_plain_n = _normalize_for_match(window_n)
        if _match_previous_in_window(window_plain_n, "whitespace-normalized XML window"):
            return True
        context_plain_hint = window_plain_n
        print("    [LLM VALIDATE] Debug mismatch snapshot:")
        print(f"      previous_plain tail : {norm_previous[-120:]}")
        print(f"      insert_after_plain  : {norm_anchor_plain[-120:]}")
        print(f"      window_plain tail   : {context_plain_hint[-180:]}")
        print("    [LLM VALIDATE] Deterministic check inconclusive — proceeding to LLM check.")
    else:
        print("    [LLM VALIDATE] Anchor not found verbatim in section XML; using LLM fallback.")
        print(f"      insert_after_plain  : {norm_anchor_plain[-120:]}")
        print(f"      previous_plain tail : {norm_previous[-120:]}")

    anchor_plain  = _strip_tags(insert_after).strip()
    previous_tail = previous_plain[-200:].strip()
    anchor_end_xml   = (anchor_pos + len(insert_after)) if anchor_pos != -1 else len(section_xml)
    context_window   = section_xml[max(0, anchor_end_xml - 2000):anchor_end_xml]
    context_plain    = context_plain_hint if context_plain_hint else _strip_tags(context_window).strip()

    context_plain_tail = context_plain[-1500:]
    prompt = load_prompt(
        "newline_anchor_validation.txt",
        previous_tail=previous_tail,
        context_plain_tail=context_plain_tail,
    )

    for attempt in range(1, max_retries + 1):
        retry_delay = INITIAL_RETRY_DELAY
        rate_limit_attempts = 0
        
        while rate_limit_attempts < MAX_RETRIES_RATE_LIMIT:
            try:
                response = client.messages.create(
                    model=model_name,
                    max_tokens=5,
                    temperature=0,
                    messages=[{"role": "user", "content": prompt}],
                )
                answer = response.content[0].text.strip().lower().rstrip('.')
                print(f"    [LLM VALIDATE] attempt {attempt} → '{answer}'")
                if answer in ("yes", "no"):
                    return answer == "yes"
                print("    [LLM VALIDATE] unexpected answer, retrying...")
                break  # Exit rate limit loop, continue to next attempt
            except Exception as e:
                error_str = str(e)
                if "rate_limit" in error_str.lower() or "429" in error_str:
                    rate_limit_attempts += 1
                    if rate_limit_attempts < MAX_RETRIES_RATE_LIMIT:
                        print(f"    [RATE LIMIT] Attempt {rate_limit_attempts}/{MAX_RETRIES_RATE_LIMIT} - Waiting {retry_delay}s...")
                        time.sleep(retry_delay)
                        retry_delay = min(retry_delay * 2, MAX_RETRY_DELAY)
                    else:
                        print(f"    [LLM VALIDATE] attempt {attempt} error: {e}")
                        break
                else:
                    print(f"    [LLM VALIDATE] attempt {attempt} error: {e}")
                    break

    print("    [LLM VALIDATE] exhausted retries, defaulting to True (proceed)")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# LLM: GET INSERT_AFTER ANCHOR
# ─────────────────────────────────────────────────────────────────────────────

def call_llm_to_get_paragraph_insert_after(client,
                                            model_name: str,
                                            section_xml: str,
                                            previous_para: str,
                                            new_text: str,
                                            max_retries: int = 2,
                                            image_cues: list = None) -> dict:

    def _extract_first_json_object(raw_text: str) -> str | None:
        if not raw_text:
            return None
        start = raw_text.find('{')
        if start == -1:
            return None
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(raw_text)):
            ch = raw_text[i]
            if in_string:
                if escape:      escape = False
                elif ch == '\\': escape = True
                elif ch == '"':  in_string = False
                continue
            if ch == '"':   in_string = True
            elif ch == '{': depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return raw_text[start:i + 1]
        return None

    cleaned_previous_para = previous_para.replace('►►', '').replace('►', '').strip()

    new_text_stripped = new_text.strip()
    prompt = load_prompt(
        "newline_paragraph_insertion.txt",
        CASE_CITATION_RULES=CASE_CITATION_RULES,
        URL_TAGGING_RULES=URL_TAGGING_RULES,
        XML_ENTITY_RULES=XML_ENTITY_RULES,
        cleaned_previous_para=cleaned_previous_para,
        section_xml=section_xml,
        new_text_stripped=new_text_stripped,
    )

    retry_suffix = """

YOUR PREVIOUS RESPONSE WAS INVALID. RETRY WITH THESE REMINDERS:

1. insert_after MUST end with the correct closing tag for the identified CASE.
2. For CASE B2: end at </footnote> when the new text goes BETWEEN the footnote
   and child block elements inside the same <para>.
3. For CASE E: end at </para.text> when insertion is between prose and immediate child block.
4. For CASE B1/A: end at the OUTERMOST closing paragraph tag.
5. NEVER end inside a <footnote.body>.
6. insert_after must be verbatim text from the XML.
7. CRITICAL: Check if NEW_TEXT starts with a bullet (•, ►, ▶, →, etc).
   If YES, use rule a1 and wrap as <list.item> with <name.block><designator>.
   Do NOT wrap as plain <para>.
8. For CASE F, when PREVIOUS_PARA is inside <list.item>, insert_after must end
    at the OUTER </list.item> of the previous item, not an inner </para>.
9. The XML may contain <trace>, <trace.deleted>, <trace.deleted/> tags — treat
   them as invisible when locating closing tags like </list.item>.
10. Escape all double quotes as \\" and newlines as \\n in JSON values.
11. Return ONLY a valid JSON object. Nothing else.
⚠️ CRITICAL OVERRIDE — READ THIS FIRST BEFORE ANYTHING ELSE:
If PREVIOUS_PARA ends with a colon (:), NEW_TEXT MUST be wrapped as <list.item>
inside a <list>. This OVERRIDES all sibling context entirely.
Even if every surrounding sibling is <quote.para>, you MUST use <list.item>.
Even if every surrounding sibling is <para>, you MUST use <list.item>.
No exceptions. No excuses. Use rule a2.

12. If PREVIOUS_PARA ends with a colon (:), wrap as <list.item> — NOT <quote.para>,
    NOT <para>. Sibling context does NOT matter. Rule a2 is absolute.
13. Only add <name.block><designator> inside <list.item> when NEW_TEXT explicitly
    starts with a bullet or numbered designator AND sibling <list.item> elements
    also use <name.block><designator>. Plain text list items must NOT have <name.block>.
14. NEVER generate <heading.para> as new_para_tag. Always use <para> with
    <para.text> regardless of whether NEW_TEXT contains <bold> tags.
    <heading.para> is a structural tag for pre-existing section headings only.
15. If PREVIOUS_PARA is a section heading/designator (from <section.front>),
    insert_after must end at the opening <section.body...> tag, NOT at
    </section.front> or </outline.name.block>. Use CASE G.
16. ⚠️ HEADING DETECTION (CASE H): If NEW_TEXT is a standalone legislation/act/
    regulation name (e.g. "The Management and Reduction of Greenhouse Gases..."),
    it MUST be wrapped as <head>, NOT <para>. Check the image cue if provided.
    Use: <head align="l" date.trace="1" date.updated="1" toc.include="y"><name.block
    date.updated="0" toc.include="y"><name date.updated="0"><ital>TEXT</ital></name>
    </name.block></head>
"""

    last_raw = None

    for attempt in range(1, max_retries + 1):
        use_prompt = prompt if attempt == 1 else prompt + retry_suffix
        print(f"  [LLM] Attempt {attempt}/{max_retries}...")

        try:
            # Build message content — include images if available
            if image_cues:
                user_content = []
                for img_data in image_cues:
                    user_content.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": img_data["base64"],
                        },
                    })
                # Add image context instruction
                image_instruction = (
                    "\n\n[VISUAL CUE FROM PDF]\n"
                    "The above image(s) show the relevant page(s) from the source PDF document. "
                    "Use these to determine the CORRECT TAG TYPE for the new text:\n"
                    "- If the new text appears as a HEADING in the PDF (bold, italic title, "
                    "legislation name, act name, regulation name, centered/prominent formatting), "
                    "wrap it as a <head> element:\n"
                    '  <head align="l" date.trace="1" date.updated="1" toc.include="y">'
                    '<name.block date.updated="0" toc.include="y">'
                    '<name date.updated="0"><ital>HEADING TEXT HERE</ital></name>'
                    '</name.block></head>\n'
                    "- If the new text is regular body text/paragraph in the PDF, "
                    "use the standard <para> tag as specified in the rules.\n"
                    "- IMPORTANT: Legislation/Act/Regulation NAMES that appear as standalone "
                    "headings (not inline within a sentence) MUST use <head> tag, NOT <para>.\n\n"
                )
                user_content.append({"type": "text", "text": image_instruction + use_prompt})
                print(f"  [IMAGE CUE] Attached {len(image_cues)} page image(s) to LLM call.")
            else:
                user_content = use_prompt

            raw = call_anthropic_with_vision(
                client=client,
                model_name=model_name,
                content=user_content,
                max_tokens=16000,
            )
        except Exception as e:
            print(f"  [ERROR] Attempt {attempt} — API call failed: {e}")
            last_raw = None
            continue

        raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.IGNORECASE)
        raw = re.sub(r'\s*```$', '', raw)

        try:
            json_candidate = _extract_first_json_object(raw) or raw
            result       = json.loads(json_candidate)
            insert_after = result.get("insert_after", "") or ""
            llm_case     = result.get("case", "?")
            print(f'  [LLM CASE] {llm_case}')
            print(f'  Insert after text from LLM: {insert_after}')
            print(f"  [LLM] Valid JSON received on attempt {attempt}.")
            return result
        except json.JSONDecodeError as e:
            last_raw = raw
            print(f"  [ERROR] Attempt {attempt} — JSON decode error: {e}")
            if attempt < max_retries:
                print(f"  [RETRY] Retrying...")

    print(f"  [FAIL] All {max_retries} attempts failed.")
    if last_raw:
        print(f"  [RAW LLM OUTPUT]:\n{last_raw}")
    return {"insert_after": None, "new_para_tag": None, "case": None}


# ─────────────────────────────────────────────────────────────────────────────
# XML POSITION UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def _map_plain_index_to_xml_pos(xml: str, plain_index: int) -> int:
    if plain_index <= 0:
        return 0
    plain_to_xml: list[int] = []
    in_tag = False
    pending_space = False
    i = 0
    n = len(xml)
    while i < n:
        ch = xml[i]
        if ch == '<':
            in_tag = True; i += 1; continue
        if ch == '>':
            in_tag = False; i += 1; continue
        if in_tag:
            i += 1; continue
        token = ch
        token_end_idx = i
        if ch == '&':
            semi = xml.find(';', i + 1)
            if semi != -1:
                token = html.unescape(xml[i:semi + 1])
                token_end_idx = semi
                i = semi + 1
            else:
                i += 1
        else:
            i += 1
        token = token.replace('\u2018', "'").replace('\u2019', "'")
        token = token.replace('\u201c', '"').replace('\u201d', '"')
        token = token.replace('\u2013', '-').replace('\u2014', '-')
        token = re.sub(r'[►▶→•§]+', ' ', token)
        for out_ch in token:
            if out_ch.isspace():
                pending_space = True; continue
            if pending_space and plain_to_xml:
                plain_to_xml.append(token_end_idx + 1)
                pending_space = False
            plain_to_xml.append(token_end_idx + 1)
    if not plain_to_xml:
        return len(xml)
    if plain_index > len(plain_to_xml):
        return len(xml)
    return plain_to_xml[plain_index - 1]


def _build_normalized_text_with_xml_map(xml: str) -> tuple[str, list[int]]:
    plain_chars: list[str] = []
    plain_to_xml: list[int] = []
    in_tag = False
    pending_space = False
    i = 0
    n = len(xml)
    while i < n:
        ch = xml[i]
        if ch == '<':
            in_tag = True; i += 1; continue
        if ch == '>':
            in_tag = False; i += 1; continue
        if in_tag:
            i += 1; continue
        token = ch
        token_end_idx = i
        if ch == '&':
            semi = xml.find(';', i + 1)
            if semi != -1:
                token = html.unescape(xml[i:semi + 1])
                token_end_idx = semi
                i = semi + 1
            else:
                i += 1
        else:
            i += 1
        token = token.replace('\u2018', "'").replace('\u2019', "'")
        token = token.replace('\u201c', '"').replace('\u201d', '"')
        token = token.replace('\u2013', '-').replace('\u2014', '-')
        token = re.sub(r'[►▶→•§]+', ' ', token)
        for c in token:
            if c.isspace():
                pending_space = True; continue
            if pending_space and plain_chars:
                plain_chars.append(' ')
                plain_to_xml.append(token_end_idx + 1)
                pending_space = False
            plain_chars.append(c)
            plain_to_xml.append(token_end_idx + 1)

    norm_chars: list[str] = []
    norm_to_xml: list[int] = []
    pending_space = False
    for c, xml_pos in zip(plain_chars, plain_to_xml):
        c = c.lower()
        if c in ('"', "'"):
            c = ' '
        elif not (c.isalnum() or c == '_' or c.isspace()):
            c = ' '
        if c.isspace():
            pending_space = True; continue
        if pending_space and norm_chars:
            norm_chars.append(' ')
            norm_to_xml.append(xml_pos)
            pending_space = False
        norm_chars.append(c)
        norm_to_xml.append(xml_pos)
    return ''.join(norm_chars), norm_to_xml


def _footnote_body_depth_at(xml: str, pos: int) -> int:
    depth = 0
    for m in re.finditer(r'<[^>]+>', xml[:max(0, pos)]):
        tag = m.group(0)
        if re.match(r'<footnote\.body\b[^>]*>', tag):
            depth += 1
        elif re.match(r'</footnote\.body\s*>', tag):
            depth = max(0, depth - 1)
    return depth


def _find_block_close_after_text_pos(section_xml: str, start_pos: int,
                                     required_close_tag: str | None = None) -> int:
    """
    Walk forward from start_pos and find the OUTERMOST enclosing block close.
    If required_close_tag is given (e.g. '</list.item>'), only that tag
    is accepted as the boundary — inner </para> tags are skipped.
    """
    if start_pos < 0:
        return -1

    BLOCK_OPEN_NAMES = {
        'para', 'stat.item.para', 'heading.para',
        'list.item', 'clause',
        'quote.block', 'quote.para',
        'list', 'table', 'table.row',
    }
    BLOCK_CLOSE_MAP = {
        'para': '</para>',
        'stat.item.para': '</stat.item.para>',
        'heading.para': '</heading.para>',
        'list.item': '</list.item>',
        'clause': '</clause>',
        'quote.block': '</quote.block>',
        'quote.para': '</quote.para>',
        'list': '</list>',
        'table': '</table>',
        'table.row': '</table.row>',
    }
    OUTER_BOUNDARY_NAMES = {
        'para', 'stat.item.para', 'heading.para', 'list.item', 'clause'
    }

    footnote_body_depth = _footnote_body_depth_at(section_xml, start_pos)

    # Detect the outermost block currently enclosing start_pos
    xml_before = section_xml[:start_pos]
    outermost_tag_name = None
    depth_back = 0
    for m in reversed(list(re.finditer(r'</?[a-zA-Z][^>]*>', xml_before))):
        tag = m.group(0)
        if tag.startswith('</'):
            close_name_m = re.match(r'</([a-zA-Z][a-zA-Z0-9._-]*)', tag)
            if not close_name_m:
                continue
            close_name = close_name_m.group(1)
            if close_name in BLOCK_OPEN_NAMES:
                depth_back += 1
            continue

        open_name_m = re.match(r'<([a-zA-Z][a-zA-Z0-9._-]*)', tag)
        if not open_name_m:
            continue
        open_name = open_name_m.group(1)
        if open_name in BLOCK_OPEN_NAMES:
            if depth_back == 0:
                outermost_tag_name = open_name
                break
            depth_back -= 1

    expected_close = BLOCK_CLOSE_MAP.get(outermost_tag_name) if outermost_tag_name else None

    block_depth = 1

    for m in re.finditer(r'</?[a-zA-Z][^>]*>', section_xml[start_pos:]):
        tag = m.group(0)

        if re.match(r'<footnote\.body\b[^>]*>', tag):
            footnote_body_depth += 1
            continue
        if re.match(r'</footnote\.body\s*>', tag):
            footnote_body_depth = max(0, footnote_body_depth - 1)
            continue

        if not tag.startswith('</') and not tag.endswith('/>'):
            open_name_m = re.match(r'<([a-zA-Z][a-zA-Z0-9._-]*)', tag)
            if open_name_m and open_name_m.group(1) in BLOCK_OPEN_NAMES:
                block_depth += 1
                continue

        close_name_m = re.match(r'</([a-zA-Z][a-zA-Z0-9._-]*)\s*>', tag)
        if not close_name_m:
            continue
        tag_name = close_name_m.group(1)
        close_tag = f'</{tag_name}>'
        if close_tag in BLOCK_CLOSE_MAP.values():
            block_depth -= 1
            if block_depth == 0 and footnote_body_depth == 0:
                if required_close_tag and close_tag != required_close_tag:
                    block_depth += 1
                    continue
                if tag_name in OUTER_BOUNDARY_NAMES:
                    if expected_close is None or close_tag == expected_close:
                        return start_pos + m.end()

    return -1


def _is_inserted_inside_wrong_parent(modified_xml: str, new_para_tag: str) -> bool:
    """
    Returns True if new_para_tag appears nested inside a <list.item>
    when it is itself a <list.item> (i.e. list.item inside list.item).
    """
    if '<list.item' not in new_para_tag:
        return False
    pos = modified_xml.find(new_para_tag)
    if pos == -1:
        return False
    xml_before = modified_xml[:pos]
    open_list_items = len(re.findall(r'<list\.item\b[^>]*>', xml_before))
    close_list_items = len(re.findall(r'</list\.item\s*>', xml_before))
    return open_list_items > close_list_items


# ─────────────────────────────────────────────────────────────────────────────
# CANDIDATE FINDING & SCORING
# ─────────────────────────────────────────────────────────────────────────────

def _tail_word_overlap(reference_text: str, target_text: str, tail_words: int = 40) -> float:
    ref_words = [w for w in _normalize_for_match(reference_text).split() if len(w) > 2]
    tgt_words = set(_normalize_for_match(target_text).split())
    if not ref_words or not tgt_words:
        return 0.0
    tail = ref_words[-tail_words:]
    if not tail:
        return 0.0
    matched = sum(1 for w in tail if w in tgt_words)
    return matched / max(len(tail), 1)


def _find_verbatim_anchor_positions(section_xml: str, insert_after: str) -> list[int]:
    """
    Return all verbatim end-positions of insert_after in section_xml.
    Also tries whitespace-normalized and tag-fuzzy matching as fallbacks.
    """
    positions = []
    anchor_len = len(insert_after)

    # 1. Exact verbatim
    start = 0
    while True:
        p = section_xml.find(insert_after, start)
        if p == -1:
            break
        positions.append(p + anchor_len)
        start = p + 1

    if positions:
        return positions

    # 2. Tag-aware fuzzy (whitespace-tolerant between tokens)
    parts = re.findall(r'<[^>]+>|[^<]+', insert_after)
    pattern_parts = []
    for part in parts:
        if not part:
            continue
        if part.startswith('<') and part.endswith('>'):
            pattern_parts.append(re.escape(part))
        else:
            stripped = part.strip()
            if not stripped:
                continue
            tokens = re.split(r'\s+', stripped)
            txt_pat = r'\s+'.join(re.escape(t) for t in tokens if t)
            if txt_pat:
                pattern_parts.append(txt_pat)

    if pattern_parts:
        fuzzy_pattern = r'\s*'.join(pattern_parts)
        try:
            for m in re.finditer(fuzzy_pattern, section_xml, flags=re.DOTALL):
                positions.append(m.end())
        except re.error:
            pass

    return positions


def _score_verbatim_candidate(section_xml: str, insert_pos: int,
                               previous_para: str, insert_after: str) -> float:
    """
    Score a verbatim/mid-para insertion position.
    Uses a window BEFORE the insert point (not the enclosing paragraph),
    which is correct for mid-para positions (e.g. after </footnote>).
    """
    if insert_pos <= 0:
        return 0.0
    window_size  = max(3000, len(previous_para) * 4)
    window_xml   = section_xml[max(0, insert_pos - window_size):insert_pos]
    prev_overlap = _tail_word_overlap(previous_para, window_xml, tail_words=45)
    anchor_overlap = _tail_word_overlap(_strip_tags(insert_after), window_xml, tail_words=25)

    # ── PROXIMITY BONUS ───────────────────────────────────────────────────
    # Reward positions where the anchor text ends close to insert_pos.
    # Prevent far-away positions from winning only because previous_para
    # appears in a large lookback window.
    anchor_plain = _strip_tags(insert_after).strip()
    proximity_score = 0.0
    if anchor_plain:
        norm_anchor = _normalize_for_match(anchor_plain)
        norm_window = _normalize_for_match(window_xml)
        if norm_anchor and norm_window:
            anchor_key = norm_anchor[-60:] if len(norm_anchor) >= 60 else norm_anchor
            last_occ = norm_window.rfind(anchor_key)
            if last_occ != -1:
                distance_ratio = last_occ / max(len(norm_window), 1)
                proximity_score = distance_ratio

    score = (0.50 * prev_overlap) + (0.25 * anchor_overlap) + (0.25 * proximity_score)
    print(
        f"    [POS-SCORE-VERBATIM] insert_pos={insert_pos} "
        f"prev={prev_overlap:.3f} anchor={anchor_overlap:.3f} "
        f"proximity={proximity_score:.3f} total={score:.3f}"
    )
    return score


def _score_block_close_candidate(section_xml: str, insert_pos: int,
                                  previous_para: str, insert_after: str) -> float:
    """
    Score a block-close insertion position.
    Uses the enclosing paragraph's full plain text as the scoring window,
    which is correct for end-of-paragraph positions.
    """
    if insert_pos <= 0:
        return 0.0
    para_plain = _extract_paragraph_plain_text_ending_at(section_xml, insert_pos)
    para_norm  = _normalize_for_match(para_plain)

    prev_overlap   = _tail_word_overlap(previous_para, para_plain, tail_words=45)
    anchor_overlap = _tail_word_overlap(_strip_tags(insert_after), para_plain, tail_words=25)

    prev_words   = [w for w in _normalize_for_match(previous_para).split() if len(w) > 2]
    anchor_words = [w for w in _normalize_for_match(_strip_tags(insert_after)).split() if len(w) > 2]

    prev_phrase_hit = 0.0
    if len(prev_words) >= 12:
        if ' '.join(prev_words[-12:]) in para_norm:
            prev_phrase_hit = 1.0

    anchor_phrase_hit = 0.0
    if len(anchor_words) >= 10:
        if ' '.join(anchor_words[-10:]) in para_norm:
            anchor_phrase_hit = 1.0

    score = (0.60 * prev_overlap) + (0.20 * anchor_overlap) + (0.15 * prev_phrase_hit) + (0.05 * anchor_phrase_hit)
    print(
        f"    [POS-SCORE-BLOCK] insert_pos={insert_pos} "
        f"prev={prev_overlap:.3f} anchor={anchor_overlap:.3f} "
        f"prevPhrase={prev_phrase_hit:.1f} anchorPhrase={anchor_phrase_hit:.1f} total={score:.3f}"
    )
    return score


def _find_insert_pos_via_plain_anchor(section_xml: str, insert_after: str,
                                      previous_para: str = "",
                                      required_close_tag: str | None = None) -> int:
    """
    Fallback: find insertion position via plain-text anchor matching.
    Only used when verbatim anchor lookup fails entirely.
    Always uses block-close walking (for end-of-paragraph cases).
    """
    plain_anchor = _strip_tags(insert_after).strip()
    if len(plain_anchor) < 20:
        return -1

    norm_sec, norm_map = _build_normalized_text_with_xml_map(section_xml)
    norm_anchor = _normalize_for_match(insert_after)
    all_candidates = []

    for anchor_len in [120, 100, 80, 60, 40, 30]:
        if len(norm_anchor) < anchor_len:
            continue
        tail = norm_anchor[-anchor_len:]
        start = 0
        while True:
            p = norm_sec.find(tail, start)
            if p == -1:
                break
            all_candidates.append((p + len(tail), anchor_len))
            start = p + 1
        if all_candidates:
            break

    if not all_candidates or not norm_map:
        return -1

    insert_after_plain = _strip_tags(insert_after)
    norm_prev_words   = [w for w in _normalize_for_match(previous_para).split() if len(w) > 2]
    norm_anchor_words = [w for w in _normalize_for_match(insert_after_plain).split() if len(w) > 2]

    best_score     = -1.0
    best_insert_pos = -1

    for plain_end, _ in all_candidates:
        if plain_end <= 0:
            continue
        mapped_idx   = min(plain_end, len(norm_map)) - 1
        approx_xml_pos = norm_map[mapped_idx]
        insert_pos   = _find_block_close_after_text_pos(
            section_xml,
            approx_xml_pos,
            required_close_tag=required_close_tag,
        )
        if insert_pos == -1:
            continue

        para_plain = _extract_paragraph_plain_text_ending_at(section_xml, insert_pos)
        para_norm  = _normalize_for_match(para_plain)

        prev_overlap   = _tail_word_overlap(previous_para, para_plain, tail_words=45)
        anchor_overlap = _tail_word_overlap(insert_after_plain, para_plain, tail_words=30)

        prev_phrase_hit = 0.0
        if len(norm_prev_words) >= 12:
            if ' '.join(norm_prev_words[-12:]) in para_norm:
                prev_phrase_hit = 1.0

        anchor_phrase_hit = 0.0
        if len(norm_anchor_words) >= 12:
            if ' '.join(norm_anchor_words[-12:]) in para_norm:
                anchor_phrase_hit = 1.0

        score = (0.50 * prev_overlap) + (0.25 * anchor_overlap) + (0.10 * prev_phrase_hit) + (0.15 * anchor_phrase_hit)
        print(
            f"    [ANCHOR-DISAMBIG] candidate plain_end={plain_end} "
            f"prev={prev_overlap:.3f} anchor={anchor_overlap:.3f} "
            f"prevPhrase={prev_phrase_hit:.1f} anchorPhrase={anchor_phrase_hit:.1f} total={score:.3f}"
        )
        if score > best_score:
            best_score      = score
            best_insert_pos = insert_pos

    return best_insert_pos


def _find_insert_pos_from_previous_para(section_xml: str, previous_para: str,
                                        required_close_tag: str | None = None) -> int:
    """
    Resolve insertion position directly from previous_para text.
    Always uses block-close walking.
    """
    norm_prev = _normalize_for_match(previous_para)
    norm_sec, norm_map = _build_normalized_text_with_xml_map(section_xml)
    if not norm_prev or not norm_sec:
        return -1

    prev_words = [w for w in norm_prev.split() if len(w) > 2]
    if len(prev_words) < 6:
        return -1

    phrases = []
    for n in [24, 20, 16, 12, 10, 8, 6]:
        if len(prev_words) >= n:
            phrases.append(' '.join(prev_words[-n:]))

    candidates = []
    for phrase in phrases:
        start = 0
        while True:
            p = norm_sec.find(phrase, start)
            if p == -1:
                break
            candidates.append(p + len(phrase))
            start = p + 1
        if candidates:
            break

    if not candidates:
        return -1

    tail_words = prev_words[-40:]
    best_score = -1
    best_end   = candidates[0]
    for end_n in candidates:
        win_start = max(0, end_n - 3000)
        window = norm_sec[win_start:end_n]
        score  = sum(1 for w in tail_words if w in window)
        print(f"    [PREV-DISAMBIG] candidate norm_end={end_n}, score={score}/{len(tail_words)}")
        if score > best_score:
            best_score = score
            best_end   = end_n

    if not norm_map or best_end <= 0:
        return -1
    mapped_idx     = min(best_end, len(norm_map)) - 1
    approx_xml_pos = norm_map[mapped_idx]
    return _find_block_close_after_text_pos(
        section_xml,
        approx_xml_pos,
        required_close_tag=required_close_tag,
    )



# ─────────────────────────────────────────────────────────────────────────────
# MAIN INSERTION FUNCTION
# ─────────────────────────────────────────────────────────────────────────────

# def _is_inside_quote_block_or_list(section_xml: str, pos: int) -> bool:
#     """
#     Returns True if pos is nested inside a <quote.block> or <list> element.
#     Used to filter CASE E verbatim candidates that landed inside a quote/list.
#     """
#     xml_before = section_xml[:pos]
#     quote_depth = (len(re.findall(r'<quote\.block\b[^>]*>', xml_before)) -
#                    len(re.findall(r'</quote\.block\s*>', xml_before)))
#     list_depth  = (len(re.findall(r'<list\b[^>]*>', xml_before)) -
#                    len(re.findall(r'</list\s*>', xml_before)))
#     return quote_depth > 0 or list_depth > 0

def _is_inside_quote_block_or_list(section_xml: str, pos: int) -> bool:
    """
    Returns True if pos is strictly nested inside a <quote.block> or <list>
    element. A position between </para.text> and an upcoming <quote.block>
    correctly returns False because the quote.block opens AFTER pos.
    """
    xml_before = section_xml[:pos]

    quote_depth = (
        len(re.findall(r'<quote\.block\b[^>]*>', xml_before)) -
        len(re.findall(r'</quote\.block\s*>', xml_before))
    )

    # ⚠️ Must use negative lookahead to avoid matching <list.item>
    list_depth = (
        len(re.findall(r'<list(?!\.item\b)[^>]*>', xml_before)) -
        len(re.findall(r'</list\s*>', xml_before))
    )

    inside = quote_depth > 0 or list_depth > 0
    print(f"  [CASE-E DEPTH CHECK] pos={pos} quote_depth={quote_depth} list_depth={list_depth} inside={inside}")
    return inside

def insert_paragraph_using_anchor(section_xml: str,
                                   insert_after: str,
                                   new_para_tag: str,
                                   previous_para: str = "",
                                   llm_case: str = "") -> str:
    """
    Insert new_para_tag at the correct position in section_xml.

    Strategy depends on what the LLM anchor ends with:

    MID-PARA anchor (ends with </footnote>, </cite>, etc.):
      → Try verbatim positions directly. Score with window-before approach.
        This handles CASE B2 correctly — inserts right after </footnote>.

    BOUNDARY anchor (ends with </para>, </list.item>, etc.):
      → Try verbatim positions first (exact match is best).
        Fall back to block-close walking and plain-anchor if verbatim fails.
        Score with paragraph-text approach.

    Both paths deduplicate candidates and pick the highest-scoring one.
    """
    if not insert_after or not new_para_tag:
        print("  [WARN] insert_after or new_para_tag is empty, skipping insertion.")
        return section_xml

    is_mid_para  = _anchor_ends_mid_para(insert_after)
    is_boundary  = _anchor_ends_at_boundary(insert_after)

    # Default: treat as boundary if neither flag set
    if not is_mid_para and not is_boundary:
        is_boundary = True

    print(f"  [ANCHOR-TYPE] mid_para={is_mid_para}, boundary={is_boundary}, llm_case={llm_case!r}")

    required_close_for_fallback = None
    if str(llm_case).strip().upper() == "F":
        required_close_for_fallback = "</list.item>"

    candidates: dict[int, tuple[str, str]] = {}  # pos → (source, score_type)

    # ── Step 1: Verbatim anchor positions (always tried first) ───────────
    verbatim_positions = _find_verbatim_anchor_positions(section_xml, insert_after)
    for pos in verbatim_positions:
        if pos not in candidates:
            candidates[pos] = ("verbatim", "verbatim")

    # ── CASE E guard ──────────────────────────────────────────────────────
    # The LLM sometimes returns an anchor that overshoots past </para.text>
    # into <quote.block> or <list> opening tags. All verbatim positions then
    # land inside the quote/list. Instead of falling back to wrong positions,
    # we recover by trimming the anchor back to </para.text> and re-searching.
    _case = str(llm_case).strip().upper()
    _anchor_stripped = insert_after.rstrip()
    if _case == "E" or _anchor_stripped.endswith('</para.text>'):
        print("  [CASE-E] Filtering verbatim positions inside <quote.block>/<list>")
        filtered = {
            pos: meta
            for pos, meta in candidates.items()
            if not _is_inside_quote_block_or_list(section_xml, pos)
        }
        if filtered:
            candidates = filtered
            print(f"  [CASE-E] {len(candidates)} candidate(s) remain after filter.")
        else:
            print("  [CASE-E] All verbatim positions inside quote.block/list.")
            print("  [CASE-E] Attempting to recover correct position from </para.text> in anchor.")
            marker = '</para.text>'
            marker_idx = insert_after.rfind(marker)
            if marker_idx != -1:
                trimmed_anchor = insert_after[:marker_idx + len(marker)]
                print(f"  [CASE-E] Trimmed anchor to: ...{trimmed_anchor[-60:]!r}")
                recovered_positions = _find_verbatim_anchor_positions(section_xml, trimmed_anchor)
                recovered_outside = [
                    p for p in recovered_positions
                    if not _is_inside_quote_block_or_list(section_xml, p)
                ]
                if recovered_outside:
                    candidates = {p: ("verbatim_case_e_recovery", "verbatim") for p in recovered_outside}
                    print(f"  [CASE-E] Recovered {len(candidates)} position(s) outside quote.block/list.")
                else:
                    print("  [CASE-E] Recovery also failed — keeping original candidates as last resort.")
            else:
                print("  [CASE-E] No </para.text> found in anchor — keeping original candidates.")

    # ── Step 2: If boundary anchor, also try block-close walking ─────────
    if is_boundary and not is_mid_para:
        pos_prev = _find_insert_pos_from_previous_para(
            section_xml,
            previous_para,
            required_close_tag=required_close_for_fallback,
        )
        if pos_prev != -1 and pos_prev not in candidates:
            candidates[pos_prev] = ("previous_para", "block_close")

        pos_plain = _find_insert_pos_via_plain_anchor(
            section_xml,
            insert_after,
            previous_para,
            required_close_tag=required_close_for_fallback,
        )
        if pos_plain != -1 and pos_plain not in candidates:
            candidates[pos_plain] = ("plain_anchor", "block_close")

    if not candidates:
        print(f"  [WARN] Could not find insert_after anchor in section XML:")
        print(f"    >> {insert_after[:120]}")
        return section_xml

    # ── Step 3: Score all candidates ─────────────────────────────────────
    best_score  = -1.0
    best_pos    = -1
    best_source = ""

    for pos, (source, score_type) in candidates.items():
        if score_type == "verbatim" or is_mid_para:
            score = _score_verbatim_candidate(section_xml, pos, previous_para, insert_after)
        else:
            score = _score_block_close_candidate(section_xml, pos, previous_para, insert_after)

        # A verbatim anchor match is more reliable than fallback positions.
        if source == "verbatim":
            score += 0.10

        if score > best_score:
            best_score  = score
            best_pos    = pos
            best_source = source

    print(f"  [INSERT-SELECT] chose source={best_source}, score={best_score:.3f}, pos={best_pos}")

    if best_pos == -1:
        print(f"  [WARN] No valid insertion position found.")
        return section_xml

    # ── If insertion lands right after </section.front>, move it inside <section.body> ──
    # The XML structure is: </section.front><section.body>...
    # New paragraphs must go INSIDE <section.body>, not between front and body.
    after_pos = section_xml[best_pos:best_pos + 200].lstrip()
    section_body_match = re.match(r'<section\.body\b[^>]*>', after_pos)
    if section_body_match:
        # Find the actual position of <section.body> tag end
        whitespace_len = len(section_xml[best_pos:best_pos + 200]) - len(after_pos)
        best_pos = best_pos + whitespace_len + section_body_match.end()
        print(f"  [FIX] Moved insertion inside <section.body> (pos={best_pos})")

    modified = section_xml[:best_pos] + new_para_tag + section_xml[best_pos:]
    print(f"  [OK] New paragraph inserted after: '...{section_xml[max(0, best_pos-80):best_pos][-80:]}'")
    return modified


# ─────────────────────────────────────────────────────────────────────────────
# LOGGING & MAIN DRIVER
# ─────────────────────────────────────────────────────────────────────────────

def log_failed_newline_row(failed_log_path: str,
                            idx_value,
                            row: pd.Series,
                            reason: str,
                            failed_rows_list: list = None) -> None:
    """Log failed row to text file and optionally collect for Excel output."""
    sep = "=" * 70
    lines = [
        sep,
        "FAILED NEWLINE INSERTION ROW",
        sep,
        f"  idx           : {idx_value}",
        f"  reason        : {reason}",
        f"  section       : {row.get('section', 'N/A')}",
        f"  previous_para : {str(row.get('previous_para', ''))}",
        f"  text          : {str(row.get('text', ''))}",
        sep,
        "",
    ]
    with open(failed_log_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"  [LOG] Failed row written to: {failed_log_path}")
    
    # Collect for Excel output
    if failed_rows_list is not None:
        failed_rows_list.append({
            "idx": idx_value,
            "section": row.get('section', ''),
            "reason": reason,
            "previous_para": str(row.get('previous_para', ''))[:500],
            "new_text": str(row.get('text', ''))[:500],
        })


def process_all_newline_insertions_with_llm(pxml_content: str,
                                             section_dict: dict,
                                             section_positions: dict,
                                             all_newline_insertions: pd.DataFrame,
                                             client,
                                             model_name: str,
                                             failed_log_path: str = "failed_newline_insertions.txt",
                                             pdf_path: str = None) -> str:
    section_modifications = {}
    failed_rows_list = []  # Collect failed rows for Excel output

    with open(failed_log_path, "w", encoding="utf-8") as f:
        f.write(f"FAILED NEWLINE INSERTIONS LOG\n{'=' * 70}\n\n")

    before_dedup = len(all_newline_insertions)
    all_newline_insertions = (
        all_newline_insertions
        .drop_duplicates(subset=["idx"], keep="first")
        .reset_index(drop=True)
    )
    after_dedup = len(all_newline_insertions)
    if before_dedup != after_dedup:
        print(f"[DEDUP] Removed {before_dedup - after_dedup} duplicate rows "
              f"({before_dedup} → {after_dedup})")

    total     = len(all_newline_insertions)
    succeeded = 0
    failed    = 0
    skipped   = 0

    for row_num, (_, row) in enumerate(all_newline_insertions.iterrows(), start=1):

        idx_value     = row.get("idx", row_num)
        new_text      = row["text"]
        # Strip legacy formatting prefix &b; (and &b;&bull;) that appears at the
        # start of bullet-point text in source data. This prefix is replaced by
        # the <name.block><designator> structure in list.item insertions.
        new_text = re.sub(r'^(\s*&b;\s*)+', '', str(new_text)).strip()
        new_text = re.sub(r'^(\s*&bull;\s*)+', '', new_text).strip()
        previous_para = row.get("previous_para", None)

        if previous_para:
           
           previous_para = re.sub(r'^(\s*&b;\s*)+', '', str(previous_para)).strip()
           previous_para = re.sub(r'^(\s*&bull;\s*)+', '', previous_para).strip()

        section_raw   = row["section"]

        print(f"\n{'─' * 60}")
        print(f"[INFO] Row {row_num}/{total} | idx={idx_value} | section={section_raw}")
        print(f"  previous_para : {str(previous_para)[:80] if previous_para else 'None'}")
        print(f"  new_text      : {str(new_text)[:80]}")

        if not new_text or str(new_text).strip() == "":
            print(f"  [SKIP] new_text is empty, nothing to insert.")
            skipped += 1
            continue

        # If previous_para is just ellipsis (…, ..., &hellip;), it's too generic
        # to serve as a text-matching anchor. Instead, find the actual &hellip;
        # paragraph in the section XML (which may have been inserted by a prior row)
        # and use the paragraph BEFORE it as the anchor for the LLM, but tell the
        # LLM to insert AFTER the &hellip; paragraph.
        _is_ellipsis_prev = previous_para and re.match(r'^\s*[\.\…\u2026]+\s*$', str(previous_para))
        if _is_ellipsis_prev:
            print(f"  [WARN] previous_para is just ellipsis '{previous_para}' — searching for a better anchor.")
            # Walk backwards through earlier rows to find a meaningful text
            better_para = None
            earlier_rows = all_newline_insertions[all_newline_insertions['idx'] < idx_value].sort_values('idx', ascending=False)
            for _, earlier_row in earlier_rows.iterrows():
                # Check previous_para of earlier rows
                candidate = str(earlier_row.get('previous_para', '')).strip()
                if candidate and not re.match(r'^\s*[\.\…\u2026]+\s*$', candidate) and len(candidate) > 10:
                    better_para = candidate
                    print(f"  [FIX] Found better anchor from idx={earlier_row['idx']} previous_para: {better_para[:80]}")
                    break
                # Also check the text of earlier rows
                candidate_text = str(earlier_row.get('text', '')).strip()
                if candidate_text and not re.match(r'^\s*[\.\…\u2026]+\s*$', candidate_text) and len(candidate_text) > 10:
                    better_para = candidate_text
                    print(f"  [FIX] Using text from idx={earlier_row['idx']} as anchor: {better_para[:80]}")
                    break
            if better_para:
                # Tell the LLM: the anchor paragraph is better_para, but the new text
                # must go AFTER the &hellip; paragraph that follows it.
                previous_para = better_para + "\n\n[NOTE: There is a paragraph containing only \u2026 (ellipsis) immediately after this paragraph. The new text must be inserted AFTER that ellipsis paragraph, not before it.]"
                print(f"  [FIX] Updated previous_para with ellipsis hint.")
            else:
                print(f"  [WARN] Could not find a better anchor — proceeding with ellipsis.")

        if not previous_para or str(previous_para).strip() == "":
            print(f"  [WARN] previous_para is empty, cannot determine insertion point.")
            log_failed_newline_row(failed_log_path, idx_value, row,
                                   reason="REVISION FAILED - previous_para is empty or null",
                                   failed_rows_list=failed_rows_list)
            failed += 1
            continue

        section_key = find_section_key(str(section_raw), section_dict,
                                        client=client, model_name=model_name)
        if section_key is None:
            print(f"  [WARN] No matching section key found for '{section_raw}'")
            log_failed_newline_row(failed_log_path, idx_value, row,
                                   reason=f"REVISION FAILED - Could not find section '{section_raw}' in document",
                                   failed_rows_list=failed_rows_list)
            failed += 1
            continue

        if section_key not in section_modifications:
            section_modifications[section_key] = section_dict[section_key]

        current_section_xml = section_modifications[section_key]

        # ── Retry mechanism: up to 3 attempts with 3-second pauses ───────────
        max_insertion_retries = 3
        insertion_succeeded = False

        # ── Get image cues from PDF for heading vs para detection ─────────
        image_cues = []
        if pdf_path:
            try:
                image_cues = get_image_cue_for_insertion(
                    new_text=str(new_text),
                    previous_para=str(previous_para) if previous_para else "",
                    pdf_path=pdf_path,
                )
            except Exception as e:
                print(f"  [IMAGE CUE] Error getting image cue: {e}")
                image_cues = []
        
        for retry_attempt in range(1, max_insertion_retries + 1):
            if retry_attempt > 1:
                print(f"  [RETRY] Attempt {retry_attempt}/{max_insertion_retries} after 3-second pause...")
                time.sleep(3)

            llm_result = call_llm_to_get_paragraph_insert_after(
                client=client,
                model_name=model_name,
                section_xml=current_section_xml,
                previous_para=str(previous_para),
                new_text=str(new_text),
                max_retries=2,
                image_cues=image_cues,
            )

            if not llm_result.get("insert_after") or not llm_result.get("new_para_tag"):
                print(f"  [FAIL] LLM returned no usable output (attempt {retry_attempt}/{max_insertion_retries}).")
                if retry_attempt < max_insertion_retries:
                    continue  # Retry
                log_failed_newline_row(failed_log_path, idx_value, row,
                                       reason="REVISION FAILED - System could not determine insertion point",
                                       failed_rows_list=failed_rows_list)
                failed += 1
                break

            insert_after_str = llm_result.get("insert_after", "")
            new_para_tag     = llm_result.get("new_para_tag", "")
            llm_case         = llm_result.get("case", "")

            new_para_tag = _normalize_split_italic_case_names(new_para_tag)

            print(f"  insert_after  : {insert_after_str}")
            print(f"  new_para_tag  : {new_para_tag if new_para_tag else 'None'}...")

            is_valid = _validate_llm_anchor_position(
                client=client,
                model_name=model_name,
                section_xml=current_section_xml,
                insert_after=insert_after_str,
                previous_para=str(previous_para),
                new_text=str(new_text),
            )
            if not is_valid:
                print(f"  [VALIDATION FAILED] LLM rejected the anchor position (attempt {retry_attempt}/{max_insertion_retries}).")
                if retry_attempt < max_insertion_retries:
                    continue  # Retry
                log_failed_newline_row(failed_log_path, idx_value, row,
                                       reason="REVISION FAILED - Could not validate insertion position",
                                       failed_rows_list=failed_rows_list)
                failed += 1
                break
            print(f"  [VALIDATION OK] LLM confirmed anchor position.")

            modified_section = insert_paragraph_using_anchor(
                section_xml=current_section_xml,
                insert_after=insert_after_str,
                new_para_tag=new_para_tag,
                previous_para=str(previous_para),
                llm_case=llm_case,
            )

            if _is_inserted_inside_wrong_parent(modified_section, new_para_tag):
                print(f"  [FAIL] New list.item was inserted inside an existing list.item (attempt {retry_attempt}/{max_insertion_retries}).")
                if retry_attempt < max_insertion_retries:
                    continue  # Retry
                log_failed_newline_row(
                    failed_log_path,
                    idx_value,
                    row,
                    reason="REVISION FAILED - New paragraph was inserted in wrong location",
                    failed_rows_list=failed_rows_list
                )
                failed += 1
                break

            section_before = section_modifications[section_key]
            if modified_section == section_before:
                print(f"  [FAIL] Insertion had no effect on the XML (attempt {retry_attempt}/{max_insertion_retries}).")
                if retry_attempt < max_insertion_retries:
                    continue  # Retry
                log_failed_newline_row(failed_log_path, idx_value, row,
                                       reason="REVISION FAILED - Could not locate text in document to insert",
                                       failed_rows_list=failed_rows_list)
                section_modifications[section_key] = modified_section
                failed += 1
                break

            # ── Success! ─────────────────────────────────────────────────────
            section_modifications[section_key] = modified_section
            insertion_succeeded = True
            if retry_attempt > 1:
                print(f"  [OK] Insertion succeeded on retry attempt {retry_attempt}.")
            succeeded += 1
            break  # Exit retry loop on success

    sorted_sections = sorted(
        [(k, v) for k, v in section_modifications.items() if k in section_positions],
        key=lambda x: section_positions[x[0]][0],
        reverse=True
    )

    modified_pxml = pxml_content
    for section_key, modified_xml in sorted_sections:
        start, end = section_positions[section_key]
        modified_pxml = modified_pxml[:start] + modified_xml + modified_pxml[end:]
        print(f"[OK] Spliced section {section_key} back into pxml")

    # Write failed rows to formatted Excel - REMOVED (consolidated in main_new.py)
    if failed_rows_list:
        print(f"[FAILED LOG] {len(failed_rows_list)} rows prepared for consolidated Excel")

    print(f"\n{'=' * 60}")
    print(f"[SUMMARY] Total rows  : {total}")
    print(f"[SUMMARY] Succeeded   : {succeeded}")
    print(f"[SUMMARY] Skipped dup : {skipped}")
    print(f"[SUMMARY] Failed      : {failed}")
    print(f"[SUMMARY] Failed log  : {failed_log_path}")
    print(f"{'=' * 60}")

    return modified_pxml, failed_rows_list