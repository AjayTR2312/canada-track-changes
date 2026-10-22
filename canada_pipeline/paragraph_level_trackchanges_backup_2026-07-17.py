import re
import os
import difflib
import html
import unicodedata
import base64
import io
import anthropic
from PIL import Image
import httpx
import requests
import json
import time
import pandas as pd
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from .xml_entity_rules import XML_ENTITY_RULES, CASE_CITATION_RULES, URL_TAGGING_RULES
from .config import MODEL_OPUS, MODEL_SONNET
from .prompt_loader import load_prompt
# from collapse_fully_deleted_elements import collapse_fully_deleted_elements


def _call_footnote_full_deletion(*args, **kwargs):
    """Lazy import wrapper to avoid circular import with footnotes_level_trackchanges."""
    from .footnotes_level_trackchanges import call_llm_for_footnote_full_deletion
    return call_llm_for_footnote_full_deletion(*args, **kwargs)


# ── Rate limit handling settings ──────────────────────────────────────────────
MAX_RETRIES_RATE_LIMIT = 5
INITIAL_RETRY_DELAY = 10  # seconds
MAX_RETRY_DELAY = 120  # seconds

# ──────────────────────────────────────────────
# 0. ANTHROPIC CLIENT SETUP
# ──────────────────────────────────────────────

def build_anthropic_client(workspace_id, model_name=MODEL_OPUS):
    """
    Fetches credentials from the TR Anthropic token endpoint and returns
    a configured Anthropic client plus the resolved model_name.
    """
    payload = {"workspace_id": workspace_id}
    url = os.getenv("TR_ANTHROPIC_TOKEN_URL", "")

    resp = requests.post(url, headers=None, json=payload)
    credentials = json.loads(resp.content)

    if "anthropic_api_key" not in credentials:
        raise RuntimeError(
            f"Failed to retrieve Anthropic credentials. Response: {credentials}"
        )

    client = anthropic.Anthropic(
        api_key=credentials["anthropic_api_key"],
        http_client=httpx.Client(verify=False),
    )

    print(f"[INFO] Anthropic client built successfully. Model: {model_name}")
    return client, model_name


def call_anthropic(client, model_name: str, prompt: str, max_tokens: int = 16000) -> str:
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
                    retry_delay = min(retry_delay * 2, MAX_RETRY_DELAY)
                else:
                    print(f"  [RATE LIMIT] Max retries ({MAX_RETRIES_RATE_LIMIT}) exceeded.")
                    raise
            else:
                raise
    
    raise Exception("Max retries exceeded")


# ──────────────────────────────────────────────
# 1. UTILITIES
# ──────────────────────────────────────────────

def _split_image_to_strips(image_path: str, num_strips: int = 3, overlap_pct: float = 0.15, save_dir: str = None, idx: int = None) -> list:
    """
    Split a page image into overlapping horizontal strips for better
    LLM vision readability. Returns a list of base64-encoded PNG strings.

    Args:
        image_path: Path to the PNG image file
        num_strips: Number of horizontal bands (default 3)
        overlap_pct: Fraction of each strip height to overlap with neighbors (default 15%)
        save_dir: If provided, save strip images to this directory for debugging
        idx: Revision index (used in saved filenames)

    Returns:
        List of base64-encoded PNG strings (one per strip)
    """
    img = Image.open(image_path)
    width, height = img.size
    strip_height = height // num_strips
    overlap_px = int(strip_height * overlap_pct)

    strips_b64 = []
    for i in range(num_strips):
        top = max(0, i * strip_height - overlap_px)
        bottom = min(height, (i + 1) * strip_height + overlap_px)
        strip_img = img.crop((0, top, width, bottom))

        buf = io.BytesIO()
        strip_img.save(buf, format="PNG")
        strips_b64.append(base64.standard_b64encode(buf.getvalue()).decode("utf-8"))

        # Save strip to disk for debugging/verification
        if save_dir:
            os.makedirs(save_dir, exist_ok=True)
            page_stem = os.path.splitext(os.path.basename(image_path))[0]
            strip_filename = f"idx{idx}_{page_stem}_strip{i+1}.png" if idx is not None else f"{page_stem}_strip{i+1}.png"
            strip_img.save(os.path.join(save_dir, strip_filename), format="PNG")

    return strips_b64


def strip_tags(text):
    """Remove XML tags, entities, special chars, normalize whitespace."""
    if not text:
        return ''
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&[a-zA-Z0-9#]+;', ' ', text)
    text = re.sub(r'[►▶→•§]+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _normalize_entities_for_matching(text: str) -> str:
    """Normalize XML entities to their character equivalents for matching.
    This allows matching between LLM output (plain chars) and XML (entities)."""
    if not text:
        return ''
    # Common XML entities to their plain equivalents
    text = text.replace('&dollar;', '$')
    text = text.replace('&apos;', "'")
    text = text.replace('&quot;', '"')
    text = text.replace('&amp;', '&')
    text = text.replace('&lt;', '<')
    text = text.replace('&gt;', '>')
    text = text.replace('&ldquo;', '"')
    text = text.replace('&rdquo;', '"')
    text = text.replace('&lsquo;', "'")
    text = text.replace('&rsquo;', "'")
    text = text.replace('&mdash;', '—')
    text = text.replace('&ndash;', '–')
    text = text.replace('&hellip;', '...')
    text = text.replace('&sect;', '§')
    text = text.replace('&para;', '¶')
    text = text.replace('&num;', '#')
    text = text.replace('&percnt;', '%')
    text = text.replace('&ast;', '*')
    text = text.replace('&hyphen;', '-')
    text = text.replace('&lsqb;', '[')
    text = text.replace('&rsqb;', ']')
    # Decode numeric and named HTML/XML entities not covered above
    # (e.g., &#233;, &#xE9;, &eacute;)
    text = html.unescape(text)
    # Unicode smart quotes to plain
    text = text.replace('\u2018', "'").replace('\u2019', "'")
    text = text.replace('\u201c', '"').replace('\u201d', '"')
    text = text.replace('\u2013', '-').replace('\u2014', '-')
    # Normalize composed/decomposed unicode forms (e.g., e + ́ vs é)
    text = unicodedata.normalize('NFC', text)
    return text


def _map_entity_norm_pos_to_ws_norm(ws_norm: str, entity_pos: int) -> int:
    """
    Map a character position in the entity-normalized string back to the
    corresponding position in the whitespace-normalized (but NOT entity-normalized)
    string.

    When _normalize_entities_for_matching() shrinks multi-char entities like
    &dollar; (8 chars) → $ (1 char), positions in the entity-normalized string
    are shifted left relative to the ws-normalized string.  This function
    walks both strings in parallel to find the correct ws-norm position.
    """
    entity_pattern = re.compile(r'&(?:#[0-9]+|#[xX][0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]+);')

    e_idx = 0   # position in entity-normalized string (what we have)
    w_idx = 0   # position in ws-normalized string (what we want)

    while w_idx < len(ws_norm) and e_idx < entity_pos:
        # Check if an entity starts at current ws_norm position
        m = entity_pattern.match(ws_norm, w_idx)
        if m:
            entity = m.group(0)
            decoded = _normalize_entities_for_matching(entity)
            w_idx += len(entity)
            e_idx += len(decoded)
        else:
            w_idx += 1
            e_idx += 1

    return w_idx


def _norm_plain_text(text: str) -> str:
    """Normalize plain text for robust paragraph/anchor comparisons."""
    t = strip_tags(text or '')
    t = t.replace('&apos;', "'").replace('&amp;', '&')
    t = t.replace('&lt;', '<').replace('&gt;', '>')
    t = t.replace('&ldquo;', '"').replace('&rdquo;', '"')
    t = t.replace('&lsquo;', "'").replace('&rsquo;', "'")
    t = t.replace('\u2018', "'").replace('\u2019', "'")
    t = t.replace('\u201c', '"').replace('\u201d', '"')
    t = t.replace('\u2013', '-').replace('\u2014', '-')
    t = re.sub(r'\s+', ' ', t).strip().lower()
    return t


# ──────────────────────────────────────────────
# 2. PARSE SECTIONS FROM PXML
# ──────────────────────────────────────────────

def extract_chapter_preamble(pxml_content, chapter_number: str, section_dict: dict) -> dict:
    """
    Extract the content from the START of the PXML file up to the first <section> tag.
    This captures everything before section headers: XML declaration, DOCTYPE, chapter metadata,
    front matter, chapter title, author info, analytical levels (e.g., "I. Introduction"), etc.
    
    Adds the preamble to section_dict with chapter_number as the key.
    
    Args:
        pxml_content: The full PXML content
        chapter_number: The chapter number (e.g., "17") - used as the key
        section_dict: The existing section dictionary to add preamble to
    
    Returns:
        dict: The updated section_dict with preamble added
    """
    # Find the first <section> or <editorial.commentary.section> tag
    first_section_match = re.search(r'<section\b[^>]*>', pxml_content, re.DOTALL)
    if not first_section_match:
        first_section_match = re.search(r'<editorial\.commentary\.section(?=[\s>])[^>]*>', pxml_content, re.DOTALL)
    if not first_section_match:
        print(f"  [PREAMBLE] Could not find first <section> or <editorial.commentary.section> tag")
        return section_dict
    
    first_section_pos = first_section_match.start()
    
    # Extract everything from start of file up to the first <section> tag
    preamble_content = pxml_content[:first_section_pos].strip()
    
    if preamble_content:
        print(f"  [PREAMBLE] Extracted chapter preamble for key '{chapter_number}' ({len(preamble_content)} chars)")
        section_dict[str(chapter_number)] = preamble_content
    
    return section_dict


# Import shared parser — single source of truth for all pipelines
from .pxml_parser import parse_pxml_sections_dict_only as _parse_sections_shared


def parse_pxml_sections_from_str_new(pxml_content, chapter_number: str = None):
    """Parse PXML sections using the shared parser. Returns dict only (no positions)."""
    return _parse_sections_shared(pxml_content, chapter_number=chapter_number)


def get_best_matching_section(text_before, section_dict, previous_para=None,
                               post_para=None, top_n=5, anchor_len=80):
    text_clean = strip_tags(text_before)
    anchor = text_clean[:anchor_len].strip()
    results = []

    for designator, section_xml in section_dict.items():
        section_clean = strip_tags(section_xml)
        if anchor and anchor in section_clean:
            results.append((1.0, designator, section_xml))
            continue
        matcher = difflib.SequenceMatcher(None, text_clean, section_clean, autojunk=False)
        matching_chars = sum(block.size for block in matcher.get_matching_blocks())
        ratio = matching_chars / len(text_clean) if text_clean else 0
        results.append((ratio, designator, section_xml))

    results.sort(key=lambda x: x[0], reverse=True)

    print(f"    Top {top_n} section matches:")
    for score, desig, _ in results[:top_n]:
        print(f"      designator={desig}, score={score:.2%}")

    if not results:
        return None, 0, None

    top_score = results[0][0]
    top_results = [(score, desig, xml) for score, desig, xml in results if score == top_score]

    if len(top_results) == 1:
        return top_results[0][1], top_results[0][0], top_results[0][2]

    print(f"    [TIE] {len(top_results)} sections tied at {top_score:.2%}. Using context to break tie...")

    def context_score(section_xml):
        section_clean = strip_tags(section_xml)
        score = 0
        if previous_para:
            prev_clean = strip_tags(previous_para)
            if prev_clean and prev_clean[:60] in section_clean:
                score += 2
            else:
                matcher = difflib.SequenceMatcher(None, prev_clean[:100], section_clean, autojunk=False)
                score += matcher.ratio()
        if post_para:
            post_clean = strip_tags(post_para)
            if post_clean and post_clean[:60] in section_clean:
                score += 2
            else:
                matcher = difflib.SequenceMatcher(None, post_clean[:100], section_clean, autojunk=False)
                score += matcher.ratio()
        return score

    scored_tied = [(context_score(xml), score, desig, xml) for score, desig, xml in top_results]
    scored_tied.sort(key=lambda x: x[0], reverse=True)

    print(f"    [TIE-BREAK] Context scores:")
    for ctx_score, orig_score, desig, _ in scored_tied:
        print(f"      designator={desig}, context_score={ctx_score:.4f}")

    best = scored_tied[0]
    print(f"    [TIE-BREAK] Winner: designator={best[2]} with context_score={best[0]:.4f}")
    return best[2], best[1], best[3]


def collapse_changes(group):
    first = group.iloc[0]
    changes = []
    for _, row in group.iterrows():
        change = {
            "type":          row["type"],
            "deleted_text":  row["deleted_text"],
            "inserted_text": row["inserted_text"],
            "prev_context":  row["prev_context"],
            "post_context":  row["post_context"],
        }
        changes.append(change)
    return {
        "idx":                  first["idx"],
        "text_before":          first["text_before"],
        "text_after":           first["text_after"],
        "previous_para":        first["previous_para"],
        "post_para":            first["post_para"],
        "section":              first["section"],
        "changes":              changes,
        "fn_superscript_before": first.get("fn_superscript_before", "") if hasattr(first, 'get') else first["fn_superscript_before"] if "fn_superscript_before" in first.index else "",
        "fn_parent_para":        first.get("fn_parent_para", "")        if hasattr(first, 'get') else first["fn_parent_para"]        if "fn_parent_para"        in first.index else "",
        "_footnote_full_deletion": bool(first.get("_footnote_full_deletion", False)) if hasattr(first, 'get') else bool(first["_footnote_full_deletion"]) if "_footnote_full_deletion" in first.index else False,
    }


def _is_footnote_ref_misscoped_row(text_before: str, text_after: str, changes: list) -> bool:
    """
    Detect rows where the diff engine has confused a footnote reference
    number (e.g. '7.50') as text_after of a prose paragraph.

    Signature:
      - text_after strips to short digits/dots only (footnote ref number)
      - text_before is long prose (> 60 chars after stripping)
      - all changes are inline_deletions with no inserted_text
    """
    after_plain  = strip_tags(text_after or '').strip()
    before_plain = strip_tags(text_before or '').strip()

    after_is_ref = (
        bool(re.fullmatch(r'[\d.\s]+', after_plain))
        and len(after_plain) < 15
    )
    before_is_prose = len(before_plain) > 60

    all_deletions_no_insert = bool(changes) and all(
        'deletion' in (ch.get('type') or '')
        and not (ch.get('inserted_text') or '').strip()
        for ch in changes
    )

    return after_is_ref and before_is_prose and all_deletions_no_insert


# ──────────────────────────────────────────────
# 3. LLM CALL — ANTHROPIC
# ──────────────────────────────────────────────

def call_llm_for_trace_tagging(xml_chunk, text_before, text_after,
                                previous_para, post_para, client, changes,
                                model_name=MODEL_OPUS,
                                fn_superscript_before=None, fn_parent_para=None,
                                page_image_path=None, strip_save_dir=None, idx=None):
    """
    Builds the trace-tagging prompt and calls Anthropic via call_anthropic().
    fn_superscript_before : text immediately before the footnote superscript —
                            the target text_before appears AFTER this text.
    fn_parent_para        : full paragraph text that contains this footnote.
    page_image_path       : optional path to a PNG image of the PDF page containing
                            this paragraph — sent via vision API for extra context.
    """
    context_block = ""
    if previous_para:
        context_block += f"\nprevious_para (paragraph BEFORE the target):\n{previous_para}\n"
    if post_para:
        context_block += f"\npost_para (paragraph AFTER the target):\n{post_para}\n"
    if fn_superscript_before:
        context_block += (
            f"\nfootnote_context — text immediately BEFORE the superscript number "
            f"(the target text_before appears right AFTER this snippet):\n{fn_superscript_before}\n"
        )
    if fn_parent_para:
        context_block += (
            f"\nfootnote_parent_paragraph — the FULL paragraph that contains this footnote "
            f"(use this to confirm you have found the correct paragraph):\n{fn_parent_para}\n"
        )

    changes_block = "\nEXPLICIT CHANGE LIST (use these as ground truth for tagging):\n"
    for i, ch in enumerate(changes, 1):
        changes_block += f"\n  Change {i} — type: {ch['type']}\n"
        if ch["prev_context"]:
            changes_block += f"    prev_context  : ...{ch['prev_context']}\n"
        if ch["post_context"]:
            changes_block += f"    post_context  : {ch['post_context']}...\n"
        if ch["deleted_text"]:
            changes_block += f"    deleted_text  : {ch['deleted_text']}\n"
        if ch["inserted_text"]:
            changes_block += f"    inserted_text : {ch['inserted_text']}\n"

    # Build image cue block (only when image is provided)
    image_cue_block = ""
    if page_image_path and os.path.isfile(page_image_path):
        image_cue_block = """
6. A PAGE IMAGE showing the actual document with track changes visible.
   USE THIS IMAGE AS A VISUAL CUE:
   - Strikethrough text = DELETED content
   - Text in any color other than black/gray (i.e. colored text without strikethrough) = INSERTED content
   - Use the image ONLY to confirm changes in the EXPLICIT CHANGE LIST
     (e.g. verify correct positioning, confirm a character/entity deletion).
   - The image may show additional struck-through text that belongs to OTHER
     paragraphs or OTHER iterations — DO NOT tag those. Only tag changes that
     are in the EXPLICIT CHANGE LIST for THIS paragraph.
   - If the image shows a small entity or character (like &ldquo;) struck through
     that is part of the SAME text_before content and adjacent to a listed deletion,
     include it in that deletion's trace.deleted block.
   - NEVER use the image to delete content from CHILD elements (quote.block, list, etc.)
     that are NOT mentioned in the change list.
"""

    prompt = load_prompt(
        "paragraph_trackchanges_find_replace.txt",
        CASE_CITATION_RULES=CASE_CITATION_RULES,
        URL_TAGGING_RULES=URL_TAGGING_RULES,
        XML_ENTITY_RULES=XML_ENTITY_RULES,
        changes_block=changes_block,
        context_block=context_block,
        image_cue_block=image_cue_block,
        text_after=text_after,
        text_before=text_before,
        xml_chunk=xml_chunk,
    )

    # Build user message content — include page image as 3 overlapping strips
    if page_image_path and os.path.isfile(page_image_path):
        strips = _split_image_to_strips(page_image_path, num_strips=3, overlap_pct=0.15, save_dir=strip_save_dir, idx=idx)
        user_content = []
        for i, strip_b64 in enumerate(strips):
            user_content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": strip_b64}})
        user_content.append({"type": "text", "text": prompt})
        print(f"  [IMAGE] Attached page image as {len(strips)} strips: {os.path.basename(page_image_path)}")
    else:
        user_content = prompt

    # Call LLM — no assistant prefill (not supported by claude-opus-4-6+)
    message = client.messages.create(
        model=model_name,
        max_tokens=16000,
        temperature=0,
        messages=[
            {"role": "user",      "content": user_content},
        ],
    )
    response_text = message.content[0].text.strip()
    # Strip markdown fences if present
    if response_text.startswith("```"):
        response_text = response_text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return response_text


# ──────────────────────────────────────────────
# 3b. FOOTNOTE-SPECIFIC LLM CALL
# ──────────────────────────────────────────────

def call_llm_for_footnote_trace_tagging(xml_chunk, text_before, text_after,
                                         client, changes, model_name=MODEL_OPUS,
                                         fn_superscript_before=None, fn_parent_para=None,
                                         page_image_path=None, strip_save_dir=None, idx=None):
    """
    Footnote-specific prompt for trace-tagging.
    Uses ONLY:
      - fn_parent_para (the body paragraph containing the footnote reference)
      - fn_superscript_before (text immediately before the superscript number)
    to locate the correct footnote in the PXML.
    Does NOT use previous_para / post_para.
    """
    context_block = ""
    if fn_superscript_before:
        context_block += (
            f"\nFOOTNOTE LOCATION — text immediately BEFORE the superscript number in the parent paragraph:\n"
            f"  \"{fn_superscript_before}\"\n"
            f"  → The footnote you need to edit is the one whose <footnote.reference> appears right AFTER this text.\n"
        )
    if fn_parent_para:
        context_block += (
            f"\nPARENT PARAGRAPH (the body paragraph that contains this footnote's superscript reference):\n"
            f"  \"{fn_parent_para}\"\n"
            f"  → Use this to confirm you have found the correct <para.text> element that contains the <footnote>.\n"
        )

    changes_block = "\nEXPLICIT CHANGE LIST (use these as ground truth for tagging):\n"
    for i, ch in enumerate(changes, 1):
        changes_block += f"\n  Change {i} — type: {ch['type']}\n"
        if ch["prev_context"]:
            changes_block += f"    prev_context  : ...{ch['prev_context']}\n"
        if ch["post_context"]:
            changes_block += f"    post_context  : {ch['post_context']}...\n"
        if ch["deleted_text"]:
            changes_block += f"    deleted_text  : {ch['deleted_text']}\n"
        if ch["inserted_text"]:
            changes_block += f"    inserted_text : {ch['inserted_text']}\n"

    # Build image cue block
    image_cue_block = ""
    if page_image_path and os.path.isfile(page_image_path):
        image_cue_block = """
6. A PAGE IMAGE showing the actual document with track changes visible.
   - Strikethrough text = DELETED content
   - Colored text without strikethrough = INSERTED content
   - The image is ground truth for what was changed.
"""

    prompt = load_prompt(
        "paragraph_footnote_trackchanges.txt",
        CASE_CITATION_RULES=CASE_CITATION_RULES,
        XML_ENTITY_RULES=XML_ENTITY_RULES,
        changes_block=changes_block,
        context_block=context_block,
        image_cue_block=image_cue_block,
        text_after=text_after,
        text_before=text_before,
        xml_chunk=xml_chunk,
    )

    # Build user message content — include page image if available
    if page_image_path and os.path.isfile(page_image_path):
        strips = _split_image_to_strips(page_image_path, num_strips=3, overlap_pct=0.15, save_dir=strip_save_dir, idx=idx)
        user_content = []
        for strip_b64 in strips:
            user_content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": strip_b64}})
        user_content.append({"type": "text", "text": prompt})
        print(f"  [IMAGE] Attached page image as {len(strips)} strips: {os.path.basename(page_image_path)}")
    else:
        user_content = prompt

    message = client.messages.create(
        model=model_name,
        max_tokens=16000,
        temperature=0,
        messages=[
            {"role": "user",      "content": user_content},
        ],
    )
    response_text = message.content[0].text.strip()
    if response_text.startswith("```"):
        response_text = response_text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return response_text


# ──────────────────────────────────────────────
# 4. PARSE LLM RESPONSE INTO FIND / REPLACE
# ──────────────────────────────────────────────

def parse_llm_response(llm_response):
    find_str    = None
    replace_str = None

    # ── PRIMARY: Try JSON parsing ────────────────────────────────────────────
    # The LLM is instructed to return {"reasoning": ..., "find": ..., "replace": ...}
    json_parsed = False
    try:
        # Clean up: strip any leading/trailing whitespace or code fences
        cleaned_response = llm_response.strip()
        if cleaned_response.startswith('```'):
            # Strip code fences if LLM added them
            cleaned_response = re.sub(r'^```(?:json)?\s*', '', cleaned_response)
            cleaned_response = re.sub(r'\s*```$', '', cleaned_response)
            cleaned_response = cleaned_response.strip()

        parsed = json.loads(cleaned_response)
        if isinstance(parsed, dict) and 'find' in parsed and 'replace' in parsed:
            find_str = parsed['find'].strip() if parsed['find'] else None
            replace_str = parsed['replace'].strip() if parsed['replace'] else None
            reasoning = parsed.get('reasoning', '')
            if reasoning:
                print(f"  [JSON PARSE] LLM reasoning (contained safely): {reasoning[:200]}")
            json_parsed = True
            print(f"  [JSON PARSE] Successfully parsed structured JSON response.")
    except (json.JSONDecodeError, TypeError, KeyError) as e:
        # Try to extract JSON from within response (LLM may have prefixed reasoning text)
        _extracted = False
        _json_start = llm_response.find('{')
        if _json_start > 0:
            _json_candidate = llm_response[_json_start:]
            _last_brace = _json_candidate.rfind('}')
            while _last_brace > 0:
                _try_json = _json_candidate[:_last_brace + 1]
                try:
                    parsed = json.loads(_try_json)
                    if isinstance(parsed, dict) and 'find' in parsed and 'replace' in parsed:
                        find_str = parsed['find'].strip() if parsed['find'] else None
                        replace_str = parsed['replace'].strip() if parsed['replace'] else None
                        reasoning = parsed.get('reasoning', '')
                        if reasoning:
                            print(f"  [JSON PARSE] LLM reasoning (contained safely): {reasoning[:200]}")
                        json_parsed = True
                        _extracted = True
                        print(f"  [JSON PARSE] Extracted JSON from response (stripped {_json_start} chars of preamble).")
                        break
                except (json.JSONDecodeError, TypeError):
                    pass
                _last_brace = _json_candidate.rfind('}', 0, _last_brace)
        if not _extracted:
            print(f"  [JSON PARSE] JSON parsing failed ({e}), falling back to regex-based parsing...")

    # ── FALLBACK: Regex-based FIND:/REPLACE: parsing (backward compat) ───────
    if not json_parsed:
        # Use the LAST FIND:/REPLACE: pair in the response
        all_find_replace = list(re.finditer(
            r'FIND:\s*\n(.*?)(?=\nREPLACE:)', llm_response, re.DOTALL
        ))
        all_replace = list(re.finditer(
            r'REPLACE:\s*\n(.*?)(?=\nFIND:|\Z)', llm_response, re.DOTALL
        ))

        if all_find_replace:
            find_match = all_find_replace[-1]
            if len(all_find_replace) > 1:
                print(f"  [PARSE FIX] LLM produced {len(all_find_replace)} FIND blocks — "
                      f"using the last one (reasoning detected).")
            find_str = find_match.group(1).strip()
        else:
            find_match = re.search(r'FIND:\s*\n(.*?)(?=\nREPLACE:)', llm_response, re.DOTALL)
            if find_match:
                find_str = find_match.group(1).strip()

        if all_replace:
            replace_match = all_replace[-1]
            replace_str = replace_match.group(1).strip()
        else:
            replace_match = re.search(r'REPLACE:\s*\n(.*)', llm_response, re.DOTALL)
            if replace_match:
                replace_str = replace_match.group(1).strip()

    # ── Cleanup: strip preamble/suffix for both JSON and fallback ────────────
    if find_str:
        first_tag = find_str.find('<')
        if first_tag > 0:
            preamble = find_str[:first_tag].strip()
            if preamble:
                print(f"  [PARSE FIX] Stripped LLM preamble from FIND ({first_tag} chars): '{preamble[:80]}'")
            find_str = find_str[first_tag:]
        last_tag = find_str.rfind('>')
        if last_tag != -1 and last_tag < len(find_str) - 1:
            suffix = find_str[last_tag + 1:].strip()
            if suffix:
                print(f"  [PARSE FIX] Stripped LLM suffix from FIND: '{suffix[:80]}'")
            find_str = find_str[:last_tag + 1]

    if replace_str:
        first_tag = replace_str.find('<')
        if first_tag > 0:
            preamble = replace_str[:first_tag].strip()
            if preamble:
                print(f"  [PARSE FIX] Stripped LLM preamble from REPLACE ({first_tag} chars): '{preamble[:80]}'")
            replace_str = replace_str[first_tag:]
        opening_tag_match = re.match(r'^<([a-zA-Z][a-zA-Z0-9._-]*)', replace_str)
        if opening_tag_match:
            root_tag = opening_tag_match.group(1)
            close_pattern = f'</{root_tag}>'
            last_close_pos = replace_str.rfind(close_pattern)
            if last_close_pos != -1:
                end_pos = last_close_pos + len(close_pattern)
                if end_pos < len(replace_str):
                    suffix = replace_str[end_pos:].strip()
                    if suffix:
                        print(f"  [PARSE FIX] Stripped LLM suffix from REPLACE: '{suffix[:120]}'")
                    replace_str = replace_str[:end_pos]
            else:
                last_tag = replace_str.rfind('>')
                if last_tag != -1 and last_tag < len(replace_str) - 1:
                    suffix = replace_str[last_tag + 1:].strip()
                    if suffix:
                        print(f"  [PARSE FIX] Stripped LLM suffix from REPLACE: '{suffix[:80]}'")
                    replace_str = replace_str[:last_tag + 1]
        else:
            last_tag = replace_str.rfind('>')
            if last_tag != -1 and last_tag < len(replace_str) - 1:
                suffix = replace_str[last_tag + 1:].strip()
                if suffix:
                    print(f"  [PARSE FIX] Stripped LLM suffix from REPLACE: '{suffix[:80]}'")
                replace_str = replace_str[:last_tag + 1]

    # Normalize malformed trace.deleted closing tags
    if replace_str:
        replace_str = replace_str.replace('</trace.deleted>', '<trace.deleted/>')
    if find_str:
        find_str = find_str.replace('</trace.deleted>', '<trace.deleted/>')

    # ── Remove unwanted spaces between adjacent XML tags in trace regions (DYNAMIC) ──
    # The LLM frequently adds whitespace between closing/self-closing tags and
    # opening tags. This dynamic regex handles ANY tag combination.
    if replace_str:
        # Remove whitespace between any closing/self-closing tag and any opening tag
        # Pattern: (</tag> or <tag/>) + whitespace + (<tag>)
        replace_str = re.sub(
            r'(<(?:[a-zA-Z][a-zA-Z0-9._-]*)\b[^>]*/>' # self-closing tag like <trace.deleted/>
            r'|</[a-zA-Z][a-zA-Z0-9._-]*>)'            # OR closing tag like </trace>
            r'\s+'                                      # one or more whitespace chars
            r'(<'                                        # followed by opening < of next tag
            r'(?:/[a-zA-Z]'                              # either a closing tag </...
            r'|[a-zA-Z])'                                # or an opening tag <...
            r')',
            r'\1\2',
            replace_str
        )
        # Also remove space between opening tag's > and immediately following tag
        # e.g. <trace> <bold> → <trace><bold>
        replace_str = re.sub(
            r'(<[a-zA-Z][a-zA-Z0-9._-]*(?:\s[^>]*)?>)'  # opening tag like <trace> or <bold attr="x">
            r'\s+'                                       # whitespace
            r'(<[a-zA-Z/])',                              # next tag start
            r'\1\2',
            replace_str
        )
        # Remove trailing space before a closing tag when preceded by another closing tag
        # e.g. </bold> </trace> → </bold></trace>
        replace_str = re.sub(
            r'(</[a-zA-Z][a-zA-Z0-9._-]*>)'
            r'\s+'
            r'(</[a-zA-Z][a-zA-Z0-9._-]*>)',
            r'\1\2',
            replace_str
        )

    # ── Fix LLM entity duplication: entity + literal char (e.g. &ldquo;" → &ldquo;) ──
    # NOTE: For &ldquo;"/&rdquo;" with straight quotes, we must NOT strip the "
    # if it is actually an XML attribute-closing quote (e.g. reftext="...&rdquo;">).
    # Use regex-based replacement for these cases.
    if find_str:
        # Only replace &rdquo;" when NOT followed by > or whitespace (attribute boundary)
        find_str = re.sub(r'&rdquo;"(?![>\s])', '&rdquo;', find_str)
        find_str = re.sub(r'&ldquo;"(?![>\s])', '&ldquo;', find_str)
    if replace_str:
        replace_str = re.sub(r'&rdquo;"(?![>\s])', '&rdquo;', replace_str)
        replace_str = re.sub(r'&ldquo;"(?![>\s])', '&ldquo;', replace_str)

    _entity_dedup_map = {
        '&ldquo;\u201c': '&ldquo;',   # &ldquo; + left curly quote
        '&rdquo;\u201d': '&rdquo;',   # &rdquo; + right curly quote
        '&lsquo;\u2018': '&lsquo;',
        '&rsquo;\u2019': '&rsquo;',
        "&lsquo;'": '&lsquo;',
        "&rsquo;'": '&rsquo;',
        '&percnt;%': '&percnt;',
        '&dollar;$': '&dollar;',
        '&num;#': '&num;',
        '&ast;*': '&ast;',
        '&mdash;\u2014': '&mdash;',   # &mdash; + literal em-dash
        '&mdash;-': '&mdash;',        # &mdash; + hyphen (less likely but safe)
        '&ndash;\u2013': '&ndash;',
        '&ndash;-': '&ndash;',
        '&amp;&': '&amp;',
        '&sect;\u00a7': '&sect;',
        '&hellip;\u2026': '&hellip;',
        '&hellip;...': '&hellip;',
    }
    for text_ref in [find_str, replace_str]:
        if text_ref is None:
            continue
    # Apply in-place (need to reassign since strings are immutable)
    if find_str:
        for bad, good in _entity_dedup_map.items():
            if bad in find_str:
                print(f"  [PARSE FIX] Entity dedup in FIND: '{bad}' → '{good}'")
                find_str = find_str.replace(bad, good)
    if replace_str:
        for bad, good in _entity_dedup_map.items():
            if bad in replace_str:
                print(f"  [PARSE FIX] Entity dedup in REPLACE: '{bad}' → '{good}'")
                replace_str = replace_str.replace(bad, good)

    return find_str, replace_str


# ──────────────────────────────────────────────
# 4b. VALIDATE & FIX TRACE TAGS
# ──────────────────────────────────────────────

def _merge_adjacent_trace_tags(text: str) -> str:
    """
    Merge adjacent <trace>...</trace> <trace>...</trace> into a single
    <trace>...combined...</trace>.  Same for <trace.deleted>...<trace.deleted/>.

    Handles optional whitespace between the closing and opening tags.
    Always preserves at least one space at the merge boundary so words
    from split insertions don't get joined together.
    """
    # Merge adjacent <trace>...</trace>\s*<trace>...</trace>
    prev = None
    while prev != text:
        prev = text
        text = re.sub(
            r'</trace>(\s*)<trace>',
            lambda m: m.group(1) if m.group(1) else ' ',
            text,
        )

    # Merge adjacent <trace.deleted>...<trace.deleted/>\s*<trace.deleted>...<trace.deleted/>
    prev = None
    while prev != text:
        prev = text
        text = re.sub(
            r'<trace\.deleted/>(\s*)<trace\.deleted>',
            lambda m: m.group(1) if m.group(1) else ' ',
            text,
        )

    return text


def _validate_and_fix_trace_tags(find_str: str, replace_str: str, idx,
                                  client, model_name: str, changes: list,
                                  max_retries: int = 2) -> tuple:
    """
    Validate that the LLM REPLACE string actually contains trace tags,
    merge adjacent trace blocks, and verify each change's text is
    correctly wrapped via programmatic check.

    Returns
    -------
    (find_str, replace_str, is_valid, failed_changes)
      - find_str      : the (possibly corrected) FIND string — may have trace
                         tags stripped if the LLM hallucinated them into FIND.
      - replace_str   : the (possibly merge-fixed) REPLACE string
      - is_valid       : True if basic structure is OK (has trace tags,
                         FIND ≠ REPLACE) — even if some individual changes
                         failed validation.  False only when the REPLACE is
                         fundamentally broken (no tags at all, or identical
                         to FIND).
      - failed_changes : list of dicts for individual changes whose
                         trace-tag could NOT be verified.  Empty when all OK.
    """
    failed_changes = []

    has_trace         = '<trace>' in replace_str or '<trace ' in replace_str
    has_trace_deleted = '<trace.deleted>' in replace_str

    if not has_trace and not has_trace_deleted:
        print(f"  [VALIDATE] ✗ REPLACE string for idx={idx} has NO trace tags at all — LLM did not tag the changes.")
        # Everything failed
        for i, ch in enumerate(changes, 1):
            failed_changes.append({"change_num": i, **ch, "reason": "REPLACE has no trace tags at all"})
        return find_str, replace_str, False, failed_changes

    # Check REPLACE differs from FIND (LLM might have returned an identical copy)
    if find_str.strip() == replace_str.strip():
        # If FIND already contains trace.deleted tags, the change is already applied
        if '<trace.deleted>' in find_str or '<trace.deleted/>' in find_str:
            print(f"  [VALIDATE] ✓ FIND and REPLACE identical but trace tags already present for idx={idx} — already applied.")
            return find_str, replace_str, True, failed_changes  # Mark as valid (already done)
        print(f"  [VALIDATE] ✗ FIND and REPLACE are identical for idx={idx} — skipping.")
        return find_str, replace_str, False, failed_changes

    # Citation guard: if inserted text looks like a full case citation,
    # require case cite markup in REPLACE; otherwise force LLM retry.
    # Heuristic intentionally broad: "v." plus year and court/report token.
    full_case_like_pat = re.compile(
        r'\bv\.\b[\s\S]{0,120}?(?:19|20)\d{2}\s+[A-Z][A-Za-z\.]{1,12}\s+\d{1,6}',
        re.IGNORECASE,
    )
    has_case_cite_markup = bool(re.search(r'<cite\b[^>]*\btype="case"', replace_str, re.IGNORECASE))
    for i, ch in enumerate(changes, 1):
        ch_type = (ch.get('type') or '').lower()
        ins_text = (ch.get('inserted_text') or '').strip()
        if not ins_text:
            continue
        if ('insertion' in ch_type or 'replacement' in ch_type) and full_case_like_pat.search(ins_text):
            if not has_case_cite_markup:
                print(
                    f"  [VALIDATE] ✗ Change {i}: inserted_text looks like full case citation "
                    f"but REPLACE has no <cite type=\"case\"> markup."
                )
                failed_changes.append({
                    "change_num": i,
                    **ch,
                    "reason": "Full case citation insertion missing <cite type=\"case\"> markup",
                })
                return find_str, replace_str, False, failed_changes


    # Merge adjacent trace tags
    fixed = _merge_adjacent_trace_tags(replace_str)
    if fixed != replace_str:
        print(f"  [VALIDATE] Merged adjacent trace tags in REPLACE for idx={idx}.")
        replace_str = fixed

    # ── Programmatic per-change validation ────────────────────────────
    trace_insertions = re.findall(r'<trace>(.*?)</trace>', replace_str, re.DOTALL)
    trace_deletions  = re.findall(r'<trace\.deleted>(.*?)<trace\.deleted/>', replace_str, re.DOTALL)

    def _norm(t):
        """Normalize: strip XML tags, resolve common entities, collapse ws, lowercase."""
        if not t:
            return ''
        t = re.sub(r'<[^>]+>', ' ', t)
        t = t.replace('&apos;', "'").replace('&amp;', '&')
        t = t.replace('&lt;', '<').replace('&gt;', '>')
        t = t.replace('&ldquo;', '"').replace('&rdquo;', '"')
        t = t.replace('&lsquo;', "'").replace('&rsquo;', "'")
        t = t.replace('&lsqb;', '[').replace('&rsqb;', ']')
        t = t.replace('&sect;', '§').replace('&dblsect;', '§§')
        t = t.replace('&para;', '¶')
        t = t.replace('&num;', '#').replace('&dollar;', '$')
        t = t.replace('&percnt;', '%').replace('&ast;', '*')
        t = t.replace('&hyphen;', '-')
        t = t.replace('&mdash;', '-').replace('&ndash;', '-')
        t = re.sub(r'&[a-zA-Z0-9#]+;', ' ', t)  # any remaining entities
        # Normalize Unicode smart quotes and dashes to ASCII
        t = t.replace('\u2018', "'").replace('\u2019', "'")   # curly single quotes
        t = t.replace('\u201c', '"').replace('\u201d', '"')   # curly double quotes
        t = t.replace('\u2013', '-').replace('\u2014', '-')    # en/em dashes
        t = t.replace('\u00ab', '"').replace('\u00bb', '"')   # guillemets
        # Collapse duplicate adjacent quote chars introduced by entity + literal
        # combinations (e.g. &ldquo;" → "" after resolution → ")
        t = re.sub(r'"{2,}', '"', t)
        t = re.sub(r"'{2,}", "'", t)
        t = re.sub(r'\s+', ' ', t).strip().lower()
        return t

    norm_insertions = [_norm(t) for t in trace_insertions]
    norm_deletions  = [_norm(t) for t in trace_deletions]

    passed_count = 0
    for i, ch in enumerate(changes, 1):
        ch_type  = ch.get('type', '')
        del_text = ch.get('deleted_text') or ''
        ins_text = ch.get('inserted_text') or ''
        change_ok = True

        if ('deletion' in ch_type or 'replacement' in ch_type) and del_text.strip():
            norm_del = _norm(del_text)
            if norm_del:
                # Check if the normalized deleted text is in ANY trace.deleted block
                found = any(norm_del in d for d in norm_deletions)
                if found:
                    print(f"    [VALIDATE] ✓ Change {i}: deleted_text '{del_text[:50]}' in <trace.deleted>.")
                else:
                    print(f"    [VALIDATE] ✗ Change {i}: deleted_text '{del_text[:60]}' NOT found in any <trace.deleted> block.")
                    print(f"       norm_del='{norm_del}', norm_deletions={norm_deletions}")
                    change_ok = False

        if ('insertion' in ch_type or 'replacement' in ch_type) and ins_text.strip():
            norm_ins = _norm(ins_text)
            if norm_ins:
                # Check if the normalized inserted text is in ANY trace block
                found = any(norm_ins in t for t in norm_insertions)
                if found:
                    print(f"    [VALIDATE] ✓ Change {i}: inserted_text '{ins_text[:50]}' in <trace>.")
                else:
                    print(f"    [VALIDATE] ✗ Change {i}: inserted_text '{ins_text[:60]}' NOT found in any <trace> block.")
                    print(f"       norm_ins='{norm_ins}', norm_insertions={norm_insertions}")
                    change_ok = False

        if change_ok:
            passed_count += 1
        else:
            failed_changes.append({"change_num": i, **ch, "reason": "Trace tag not found for this change"})

    total = len(changes)
    if failed_changes:
        print(f"  [VALIDATE] {passed_count}/{total} changes verified for idx={idx}. "
              f"{len(failed_changes)} change(s) could not be verified — will proceed and log failures.")
    else:
        print(f"  [VALIDATE] ✓ All {total} changes verified programmatically for idx={idx}.")

    # is_valid = True  →  basic structure is fine, proceed with replacement
    return find_str, replace_str, True, failed_changes


def _check_trace_nesting(replace_str: str, idx) -> bool:
    """
    Reject REPLACE strings where <trace.deleted> blocks contain nested
    <trace> tags — this indicates the LLM incorrectly marked prior
    insertions as deleted.

    Returns True if clean, False if corrupt nesting detected.
    """
    deletions = re.findall(
        r'<trace\.deleted>(.*?)<trace\.deleted/>',
        replace_str,
        re.DOTALL
    )
    for d in deletions:
        if '<trace>' in d or '</trace>' in d:
            print(f"  [NESTING CHECK] ✗ idx={idx}: <trace.deleted> block contains "
                  f"nested <trace> — LLM incorrectly deleted prior insertions.")
            print(f"    Offending deletion content (first 200 chars): {d[:200]}")
            return False

    # Reject malformed output where trace tags are inserted INSIDE another
    # element's tag declaration / attributes, e.g.
    # <ref p2="Bell Helicopter Text<trace.deleted>e<trace.deleted/>ron ...">
    if re.search(r'<[A-Za-z][^>]*<(?:/?trace(?:\.deleted)?\b)', replace_str):
        print(f"  [NESTING CHECK] ✗ idx={idx}: trace tags appear inside XML attributes/tag declarations.")
        return False
    return True


def _verify_traces_with_image(replace_str, text_before, text_after, page_image_path, client, model_name):
    """
    Use the page image to verify that trace tags in REPLACE are correct.
    
    Sends the image + REPLACE to the LLM and asks it to check:
    - All strikethrough text in the image is wrapped in <trace.deleted>
    - All colored (non-black/gray) text without strikethrough is in <trace>
    - Nothing is missed or incorrectly tagged
    
    Returns the corrected REPLACE string (or original if no corrections needed).
    """
    strips = _split_image_to_strips(page_image_path, num_strips=3, overlap_pct=0.15)

    verify_prompt = load_prompt(
        "paragraph_trace_image_verify.txt",
        replace_str=replace_str,
        text_after=text_after,
        text_before=text_before,
    )

    user_content = []
    for strip_b64 in strips:
        user_content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": strip_b64}})
    user_content.append({"type": "text", "text": verify_prompt})

    # Retry up to 2 extra times if LLM returns a fragment instead of full element
    orig_open = re.match(r'^<([a-zA-Z][a-zA-Z0-9._-]*)\b', replace_str)
    orig_tag = orig_open.group(1) if orig_open else None
    messages = [{"role": "user", "content": user_content}]

    max_verify_attempts = 3
    for verify_attempt in range(1, max_verify_attempts + 1):
        message = client.messages.create(
            model=model_name,
            max_tokens=16000,
            messages=messages,
        )
        response = message.content[0].text.strip()

        if response.upper().startswith("VERIFIED"):
            print(f"  [IMAGE VERIFY] ✓ Trace tags verified correct by image.")
            return replace_str

        # LLM returned a corrected version
        corrected = response
        first_tag = corrected.find('<')
        if first_tag > 0:
            corrected = corrected[first_tag:]
        last_tag = corrected.rfind('>')
        if last_tag != -1 and last_tag < len(corrected) - 1:
            corrected = corrected[:last_tag + 1]

        # Normalize closing tags
        corrected = corrected.replace('</trace.deleted>', '<trace.deleted/>')

        # Sanity check: corrected must still have trace tags
        if '<trace' not in corrected:
            print(f"  [IMAGE VERIFY] ✗ Corrected response has no trace tags, ignoring.")
            return replace_str

        # Check outer element tag matches
        corr_open = re.match(r'^<([a-zA-Z][a-zA-Z0-9._-]*)\b', corrected)
        corr_tag = corr_open.group(1) if corr_open else None

        if orig_tag and corr_tag and corr_tag != orig_tag:
            # LLM returned a fragment — retry if attempts remain
            if verify_attempt < max_verify_attempts:
                print(f"  [IMAGE VERIFY] Attempt {verify_attempt}: LLM returned fragment starting with <{corr_tag}> "
                      f"instead of <{orig_tag}>. Retrying...")
                # Add assistant response + user correction to messages for retry
                messages.append({"role": "assistant", "content": response})
                messages.append({"role": "user", "content": (
                    f"You returned a fragment starting with <{corr_tag}>. "
                    f"I need the COMPLETE element starting with the same <{orig_tag}...> opening tag "
                    f"and ending with </{orig_tag}> — exactly like the REPLACE XML I gave you, "
                    f"just with your trace tag corrections applied. Please return the full element."
                )})
                continue
            else:
                print(f"  [IMAGE VERIFY] ✗ Still returning fragment after {max_verify_attempts} attempts, ignoring.")
                return replace_str

        # Tag matches — break out of retry loop
        break

    # Reject only if corrected is drastically SHORTER (lost content) — allow growth
    # since adding missing trace tags naturally increases size
    if len(corrected) < len(replace_str) * 0.5:
        print(f"  [IMAGE VERIFY] ✗ Corrected response too short ({len(corrected)} vs {len(replace_str)}), ignoring.")
        return replace_str

    # Reject if corrected is unreasonably large (more than 3x original)
    if len(corrected) > len(replace_str) * 3:
        print(f"  [IMAGE VERIFY] ✗ Corrected response too large ({len(corrected)} vs {len(replace_str)}), ignoring.")
        return replace_str

    print(f"  [IMAGE VERIFY] ✓ Corrections applied (delta: {len(corrected) - len(replace_str)} chars).")
    return corrected


# ──────────────────────────────────────────────
# 5. SMART REPLACE HELPERS
# ──────────────────────────────────────────────

def _expand_to_xml_boundaries(patched_content, best_start, best_end, window):
    raw_start = best_start
    raw_end   = best_end
    while raw_start > 0 and patched_content[raw_start] != '<':
        raw_start -= 1
    while raw_end < len(patched_content) and patched_content[raw_end - 1] != '>':
        raw_end += 1
    if (raw_end - raw_start) > window * 2:
        print(f"    [EXPAND] ⚠ Boundary expansion exceeded 2x window — reverting to original slice.")
        return best_start, best_end
    return raw_start, raw_end


def _map_normalized_pos_to_raw(raw_str, norm_pos):
    norm_count = 0
    in_space   = False
    for i, ch in enumerate(raw_str):
        if ch in (' ', '\t', '\n', '\r'):
            if not in_space:
                in_space = True
                norm_count += 1
                if norm_count > norm_pos:
                    return i
        else:
            in_space = False
            if norm_count == norm_pos:
                return i
            norm_count += 1
            if norm_count > norm_pos:
                return i
    return len(raw_str)


def _map_norm_range_to_orig(original: str, norm_start: int, norm_end: int) -> tuple:
    """
    Given a string *original* and start/end positions in its
    whitespace-normalized form (all runs collapsed to single space),
    return the corresponding (start, end) positions in *original*.
    """
    def _map_one(norm_target):
        n = 0
        in_space = False
        for i, ch in enumerate(original):
            if ch in (' ', '\t', '\n', '\r'):
                if not in_space:
                    in_space = True
                    n += 1
                    if n > norm_target:
                        return i
            else:
                in_space = False
                if n == norm_target:
                    return i
                n += 1
                if n > norm_target:
                    return i + 1
        return len(original)

    return _map_one(norm_start), _map_one(norm_end)


def _strip_trace_tags(text: str) -> str:
    """Remove <trace>...</trace> and <trace.deleted>...<trace.deleted/> tags only,
    keeping the text content inside them."""
    text = re.sub(r'<trace\.deleted/?>', '', text)
    text = re.sub(r'</?trace>', '', text)
    return text


def _remove_trace_insertions(text: str) -> str:
    """Remove <trace>...</trace> blocks entirely (inserted text that won't be
    in the original find_str), but keep <trace.deleted> content (which was
    in the original)."""
    # Remove inserted text entirely
    text = re.sub(r'<trace>.*?</trace>', '', text, flags=re.DOTALL)
    # Remove trace.deleted tags but keep their content (was in original)
    text = re.sub(r'<trace\.deleted/?>', '', text)
    text = re.sub(r'\s{2,}', ' ', text)
    return text


def _comparison_tokens(text: str) -> list[str]:
    """Build content-aware tokens for safety comparisons.

    Ignores punctuation-only spacing artifacts introduced when XML tags are
    stripped, so values like '(WT)' and '( WT )' normalize to the same tokens.
    """
    plain = strip_tags(text)
    return re.findall(r'[A-Za-z0-9]+(?:[:./-][A-Za-z0-9]+)*', plain)


def _verify_replacement_safety(xml_before: str, xml_after: str, idx,
                                find_str: str, replace_str: str,
                                step_name: str) -> bool:
    """
    Verify that the replacement did not lose or misplace content.

    Checks:
    1. The original words from find_str should still be present in replace_str
       (after removing inserted text from trace tags). Inserted text is new
       and expected to NOT match the original — that's fine.
    2. The overall xml_after should differ from xml_before only in the
       region that was replaced — the rest must be identical.
    3. Content OUTSIDE the replaced region must be exactly preserved.

    Returns True if safe, False if content loss/misplacement detected.
    """
    # Check 0: If FIND already contains trace blocks (from prior pipeline steps),
    # verify they are preserved in REPLACE — LLM must not remove prior work
    existing_traces = re.findall(r'<trace>(.*?)</trace>', find_str, re.DOTALL)
    existing_deletions = re.findall(r'<trace\.deleted>(.*?)<trace\.deleted/>', find_str, re.DOTALL)
    replace_norm = re.sub(r'\s+', ' ', replace_str)

    for et in existing_traces:
        et_norm = re.sub(r'\s+', ' ', et).strip()
        if et_norm and et_norm not in replace_norm:
            print(f"    [{step_name} SAFETY] ✗ Existing <trace> content from FIND was removed in REPLACE.")
            return False

    for ed in existing_deletions:
        ed_norm = re.sub(r'\s+', ' ', ed).strip()
        if ed_norm and ed_norm not in replace_norm:
            print(f"    [{step_name} SAFETY] ✗ Existing <trace.deleted> content from FIND was removed in REPLACE.")
            return False

    # Check 1: original content preservation
    # Strip trace insertions from BOTH sides so prior trace tags don't skew
    # the comparison (prior inserted text inflates find_plain otherwise)
    find_original_only = _remove_trace_insertions(find_str)
    find_plain = strip_tags(find_original_only)

    # For REPLACE comparison, remove inserted trace content and trace wrappers,
    # then collapse whitespace to avoid mid-word split artifacts from tags.
    replace_no_insertions = re.sub(r'<trace>(.*?)</trace>', '', replace_str, flags=re.DOTALL)
    replace_no_tags = re.sub(r'<trace\.deleted/?>', '', replace_no_insertions)
    replace_no_tags = re.sub(r'\s{2,}', ' ', replace_no_tags)
    replace_plain = strip_tags(replace_no_tags)

    # find_words = find_plain.split()
    # replace_words = replace_plain.split()

    find_words = _comparison_tokens(find_original_only)
    replace_words = _comparison_tokens(replace_no_tags)

    if not find_words:
        return True

    # Count how many words from find are present in replace (order-aware)
    matcher = difflib.SequenceMatcher(None, find_words, replace_words, autojunk=False)
    matching = sum(block.size for block in matcher.get_matching_blocks())
    preservation_ratio = matching / len(find_words) if find_words else 1.0

    # Secondary signal for large, heavily tagged paragraphs where token-order
    # matching can under-score even when content is largely preserved.
    norm_find_plain = re.sub(r'\s+', ' ', find_plain).strip()
    norm_replace_plain = re.sub(r'\s+', ' ', replace_plain).strip()
    char_ratio = difflib.SequenceMatcher(
        None,
        norm_find_plain,
        norm_replace_plain,
        autojunk=False,
    ).ratio()
    token_len_delta = abs(len(replace_words) - len(find_words))

    strict_ok = preservation_ratio >= 0.85
    long_para_soft_ok = (
        len(find_words) >= 250
        and char_ratio >= 0.90
        and token_len_delta <= max(120, int(0.30 * len(find_words)))
    )

    if not (strict_ok or long_para_soft_ok):
        print(
            f"    [{step_name} SAFETY] ✗ Content preservation too low: "
            f"token={preservation_ratio:.0%} ({matching}/{len(find_words)}), "
            f"char={char_ratio:.0%}, token_len_delta={token_len_delta}. "
            f"Rejecting replacement."
        )
        print(f"      find_plain   : {find_plain[:150]}")
        print(f"      replace_plain: {replace_plain[:150]}")
        return False

    if long_para_soft_ok and not strict_ok:
        print(
            f"    [{step_name} SAFETY] ⚠ Soft-pass for long paragraph: "
            f"token={preservation_ratio:.0%}, char={char_ratio:.0%}, "
            f"token_len_delta={token_len_delta}."
        )

    # Check 2: overall document should only differ by the replacement
    before_len = len(xml_before)
    after_len  = len(xml_after)
    expected_delta = len(replace_str) - len(find_str)
    actual_delta   = after_len - before_len

    # Allow some tolerance for whitespace normalization in Steps 2c/2d/3/4
    # But for large differences, be more strict
    max_tolerance = 500
    if "2c" in step_name or "2d" in step_name or "3" in step_name or "4" in step_name:
        # These steps may have whitespace differences - compute expected delta differently
        # The actual find region in xml_before may have more whitespace than find_str
        pass  # We'll rely on the content check below
    
    if abs(actual_delta - expected_delta) > max_tolerance:
        print(f"    [{step_name} SAFETY] ✗ Size delta mismatch: expected ~{expected_delta}, "
              f"got {actual_delta}. Possible content loss/duplication.")
        return False

    # Check 3: Content OUTSIDE the replaced region must be exactly preserved
    # Normalize both before and after, find where they differ
    norm_before = re.sub(r'\s+', ' ', xml_before)
    norm_after = re.sub(r'\s+', ' ', xml_after)
    norm_find = re.sub(r'\s+', ' ', find_str)
    norm_replace = re.sub(r'\s+', ' ', replace_str)
    
    # Find position of the change in normalized content
    find_pos = norm_before.find(norm_find)
    if find_pos == -1:
        # Try to find where the change happened by comparing before/after
        # The content before and after the change should match
        pass  # We already have preservation_ratio check above
    else:
        # Verify content before the change is identical
        before_prefix = norm_before[:find_pos]
        after_prefix = norm_after[:find_pos]
        if before_prefix != after_prefix:
            # Check if difference is just minor whitespace at boundaries
            if before_prefix.strip() != after_prefix.strip():
                print(f"    [{step_name} SAFETY] ✗ Content BEFORE replacement region was modified!")
                print(f"      Before prefix tail: ...{before_prefix[-100:]}")
                print(f"      After prefix tail:  ...{after_prefix[-100:]}")
                return False
        
        # Verify content after the change is identical
        before_suffix_start = find_pos + len(norm_find)
        after_suffix_start = find_pos + len(norm_replace)
        before_suffix = norm_before[before_suffix_start:]
        after_suffix = norm_after[after_suffix_start:]
        if before_suffix != after_suffix:
            if before_suffix.strip() != after_suffix.strip():
                print(f"    [{step_name} SAFETY] ✗ Content AFTER replacement region was modified!")
                print(f"      Before suffix head: {before_suffix[:100]}...")
                print(f"      After suffix head:  {after_suffix[:100]}...")
                return False

    print(
        f"    [{step_name} SAFETY] ✓ Passed "
        f"(token={preservation_ratio:.0%}, char={char_ratio:.0%}, "
        f"delta={actual_delta} vs expected ~{expected_delta})"
    )
    return True


def _build_loose_xml_pattern(text: str) -> str:
    """Build a regex pattern that tolerates pretty-print whitespace differences.

    Key behavior:
    - Any whitespace run in `text` -> ``\s*``
    - Optional whitespace is also allowed right after ``>`` and right before ``<``
      so values like ``>AIG`` and ``>\n    AIG`` both match.
    """
    if not text:
        return ''

    parts = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]

        if ch.isspace():
            while i < n and text[i].isspace():
                i += 1
            parts.append(r'\s*')
            continue

        parts.append(re.escape(ch))

        # Tolerate formatting whitespace around XML tag boundaries.
        if ch == '>':
            parts.append(r'\s*')
        elif ch == '<':
            parts.append(r'\s*')

        i += 1

    return ''.join(parts)


def smart_replace(xml_content, find_str, replace_str, idx, failed_log, extra_context=None):
    """
    Replace find_str with replace_str in xml_content using a multi-step strategy:
      1. Exact match
      2. Whitespace-flexible regex match (re.DOTALL)
    2b. XML-aware loose regex match (tolerates pretty-print around tags/text)
    3. Normalized-whitespace positional match
    4. Strip outer XML wrapper added by LLM and retry
    Falls back to logging on failure.
    Each successful replacement is verified for content preservation.

    Parameters
    ----------
    xml_content : str
        The full XML/PXML content string.
    find_str : str
        The literal text to find.
    replace_str : str
        The replacement text.
    idx : int/str
        Row/change index (used for logging).
    failed_log : list
        Accumulator for failed replacements.
    extra_context : dict, optional
        Additional context to include in failed_log entries
        (section, designator, text_before, text_after, etc.).

    Returns
    -------
    str
        The (possibly updated) xml_content.
    """
    # ── STEP 1: exact literal match ──
    print(f"    [STEP 1] Trying exact match...")
    if find_str in xml_content:
        result = xml_content.replace(find_str, replace_str, 1)
        if _verify_replacement_safety(xml_content, result, idx, find_str, replace_str, "STEP 1"):
            print(f"    [STEP 1] ✓ Exact match found. Replacing.")
            return result
        else:
            print(f"    [STEP 1] ✗ Safety check failed, trying next strategy.")
    else:
        print(f"    [STEP 1] ✗ Exact match failed.")

    # ── STEP 2: whitespace-flexible regex match ──
    # SKIP for large find_str to avoid regex catastrophic backtracking
    if len(find_str) > 2000:
        print(f"    [STEP 2] Skipping - find_str too large ({len(find_str)} chars) for regex approach.")
    else:
        print(f"    [STEP 2] Trying whitespace-flexible regex match...")
        try:
            # Escape the find_str for regex, then replace every whitespace run
            # with \s* so it tolerates newlines / extra spaces in the source XML.
            escaped = re.escape(find_str)
            flexible_pattern = re.sub(r'\\s|\\ ', r'\\s*', escaped)   # literal spaces
            flexible_pattern = re.sub(r'\s+', r'\\s*', flexible_pattern)  # any remaining ws
            pattern = re.compile(flexible_pattern, re.DOTALL)
            updated, count = re.subn(pattern, replace_str, xml_content, count=1)
            if count > 0:
                if _verify_replacement_safety(xml_content, updated, idx, find_str, replace_str, "STEP 2"):
                    print(f"    [STEP 2] ✓ Whitespace-flexible match found ({count} replacement).")
                    return updated
                else:
                    print(f"    [STEP 2] ✗ Safety check failed, trying next strategy.")
        except re.error as e:
            print(f"    [STEP 2] Regex compilation error: {e}")
        print(f"    [STEP 2] ✗ Whitespace-flexible match failed.")

    # ── STEP 2b: XML-aware loose regex (handles pretty-print around tags) ──
    # SKIP for large find_str to avoid regex catastrophic backtracking
    if len(find_str) > 2000:
        print(f"    [STEP 2b] Skipping - find_str too large ({len(find_str)} chars) for regex approach.")
    else:
        print(f"    [STEP 2b] Trying XML-aware loose regex match...")
        try:
            loose_pattern = _build_loose_xml_pattern(find_str)
            if loose_pattern:
                pattern2 = re.compile(loose_pattern, re.DOTALL)
                updated2, count2 = re.subn(pattern2, replace_str, xml_content, count=1)
                if count2 > 0:
                    if _verify_replacement_safety(xml_content, updated2, idx, find_str, replace_str, "STEP 2b"):
                        print(f"    [STEP 2b] ✓ XML-aware loose match found ({count2} replacement).")
                        return updated2
                    else:
                        print(f"    [STEP 2b] ✗ Safety check failed, trying next strategy.")
        except re.error as e:
            print(f"    [STEP 2b] Regex compilation error: {e}")
        print(f"    [STEP 2b] ✗ XML-aware loose regex match failed.")

    # ── STEP 2c: normalized find in normalized content, then map back ──
    # For large paragraphs where regex becomes too complex, use simple string
    # matching on whitespace-normalized versions, then reconstruct boundaries.
    print(f"    [STEP 2c] Trying normalized content substring match...")
    try:
        norm_content = re.sub(r'\s+', ' ', xml_content)
        norm_find = re.sub(r'\s+', ' ', find_str.strip())
        norm_pos = norm_content.find(norm_find)
        # If not found, also try with inter-tag whitespace removed (compact FIND from trace-strip recovery)
        if norm_pos == -1:
            norm_content_compact = re.sub(r'>\s+<', '><', norm_content)
            norm_find_compact = re.sub(r'>\s+<', '><', norm_find)
            norm_pos_compact = norm_content_compact.find(norm_find_compact)
            if norm_pos_compact != -1:
                # Map back: find the position in the non-compact normalized content
                # by searching for the same text with flexible whitespace
                loose_pat = _build_loose_xml_pattern(norm_find_compact)
                m = re.search(loose_pat, xml_content, re.DOTALL)
                if m:
                    result = xml_content[:m.start()] + replace_str + xml_content[m.end():]
                    if _verify_replacement_safety(xml_content, result, idx, find_str, replace_str, "STEP 2c-compact"):
                        print(f"    [STEP 2c] ✓ Compact inter-tag match found. Replacing.")
                        return result
                    else:
                        print(f"    [STEP 2c] ✗ Safety check failed (compact match), trying next strategy.")
        if norm_pos != -1:
            # Map normalized position back to original positions
            orig_start, orig_end = _map_norm_range_to_orig(xml_content, norm_pos, norm_pos + len(norm_find))
            
            # Extract the original substring and check it matches when normalized
            original_substring = xml_content[orig_start:orig_end]
            original_normalized = re.sub(r'\s+', ' ', original_substring.strip())
            
            # Use fuzzy comparison - allow small differences due to edge whitespace
            if original_normalized == norm_find or difflib.SequenceMatcher(None, original_normalized, norm_find).ratio() > 0.98:
                result = xml_content[:orig_start] + replace_str + xml_content[orig_end:]
                if _verify_replacement_safety(xml_content, result, idx, find_str, replace_str, "STEP 2c"):
                    print(f"    [STEP 2c] ✓ Normalized substring match found (orig {orig_start}:{orig_end}). Replacing.")
                    return result
                else:
                    print(f"    [STEP 2c] ✗ Safety check failed, trying next strategy.")
            else:
                print(f"    [STEP 2c] Sanity check failed (ratio={difflib.SequenceMatcher(None, original_normalized, norm_find).ratio():.3f}).")
        else:
            print(f"    [STEP 2c] ✗ Not found in normalized content.")
    except Exception as e:
        print(f"    [STEP 2c] Error: {e}")

    # ── STEP 2d: anchor-based matching using opening/closing tags ──
    # For large paragraphs with footnotes, find unique anchors at start and end
    print(f"    [STEP 2d] Trying anchor-based matching...")
    try:
        # Extract the opening tag from find_str
        open_tag_match = re.match(r'^(<[a-zA-Z][a-zA-Z0-9._-]*[^>]*>)', find_str.strip())
        # Extract closing tag - the last closing tag
        close_tag_match = re.search(r'(</[a-zA-Z][a-zA-Z0-9._-]*>)\s*$', find_str.strip())
        
        if open_tag_match and close_tag_match:
            open_tag = open_tag_match.group(1)
            close_tag = close_tag_match.group(1)
            
            # Get a unique anchor from near the start (after opening tag) - first 100 chars of content
            content_after_open = find_str[open_tag_match.end():open_tag_match.end()+150]
            # Normalize and take first chunk
            start_anchor = re.sub(r'\s+', ' ', content_after_open).strip()[:80]
            
            # Get a unique anchor from near the end (before closing tag) - last 100 chars of content  
            content_before_close = find_str[max(0, close_tag_match.start()-150):close_tag_match.start()]
            end_anchor = re.sub(r'\s+', ' ', content_before_close).strip()[-80:]
            
            print(f"      Open tag: {open_tag[:60]}...")
            print(f"      Close tag: {close_tag}")
            print(f"      Start anchor: {start_anchor[:50]}...")
            print(f"      End anchor: ...{end_anchor[-50:]}")
            
            # Search for start anchor in normalized content
            norm_content_2d = re.sub(r'\s+', ' ', xml_content)
            start_anchor_pos = norm_content_2d.find(start_anchor)
            end_anchor_pos = norm_content_2d.find(end_anchor, start_anchor_pos if start_anchor_pos != -1 else 0)
            
            if start_anchor_pos != -1 and end_anchor_pos != -1:
                # Find the opening tag before start_anchor
                open_tag_name = re.match(r'<([a-zA-Z][a-zA-Z0-9._-]*)', open_tag).group(1)
                # Search backwards from start_anchor_pos for the opening tag
                search_region = norm_content_2d[:start_anchor_pos + 10]
                open_tag_pattern = re.compile(rf'<{re.escape(open_tag_name)}[^>]*>')
                all_opens = list(open_tag_pattern.finditer(search_region))
                if all_opens:
                    last_open = all_opens[-1]
                    norm_start = last_open.start()
                    
                    # Find the closing tag after end_anchor
                    close_tag_name = re.match(r'</([a-zA-Z][a-zA-Z0-9._-]*)', close_tag).group(1)
                    search_region_end = norm_content_2d[end_anchor_pos:]
                    close_tag_pattern = re.compile(rf'</{re.escape(close_tag_name)}>')
                    close_match = close_tag_pattern.search(search_region_end)
                    if close_match:
                        norm_end = end_anchor_pos + close_match.end()
                        
                        # Map back to original
                        orig_start, orig_end = _map_norm_range_to_orig(xml_content, norm_start, norm_end)
                        
                        # Verify we found the right region
                        extracted = xml_content[orig_start:orig_end]
                        extracted_norm = re.sub(r'\s+', ' ', extracted)
                        find_norm = re.sub(r'\s+', ' ', find_str)
                        
                        sim_ratio = difflib.SequenceMatcher(None, extracted_norm, find_norm).ratio()
                        print(f"      Extracted region similarity: {sim_ratio:.3f}")
                        
                        if sim_ratio > 0.95:
                            result = xml_content[:orig_start] + replace_str + xml_content[orig_end:]
                            if _verify_replacement_safety(xml_content, result, idx, find_str, replace_str, "STEP 2d"):
                                print(f"    [STEP 2d] ✓ Anchor-based match found (orig {orig_start}:{orig_end}). Replacing.")
                                return result
                            else:
                                print(f"    [STEP 2d] ✗ Safety check failed.")
                        else:
                            print(f"    [STEP 2d] ✗ Similarity too low ({sim_ratio:.3f}).")
    except Exception as e:
        print(f"    [STEP 2d] Error: {e}")
    print(f"    [STEP 2d] ✗ Anchor-based matching failed.")

    # ── STEP 3: normalized-whitespace positional match ──
    print(f"    [STEP 3] Trying normalized-whitespace positional match...")
    norm_content = re.sub(r'\s+', ' ', xml_content)
    try:
        norm_find = re.sub(r'\s+', ' ', find_str.strip())
        norm_pos  = norm_content.find(norm_find)
        if norm_pos != -1:
            orig_start, orig_end = _map_norm_range_to_orig(xml_content, norm_pos, norm_pos + len(norm_find))
            o_check = re.sub(r'\s+', ' ', xml_content[orig_start:orig_end].strip())
            if o_check == norm_find:
                result = xml_content[:orig_start] + replace_str + xml_content[orig_end:]
                if _verify_replacement_safety(xml_content, result, idx, find_str, replace_str, "STEP 3"):
                    print(f"    [STEP 3] ✓ Normalized match found (orig {orig_start}:{orig_end}). Replacing.")
                    return result
                else:
                    print(f"    [STEP 3] ✗ Safety check failed, trying next strategy.")
            else:
                print(f"    [STEP 3] Sanity check failed.")
        else:
            print(f"    [STEP 3] ✗ Not found in normalized content.")
    except Exception as e:
        print(f"    [STEP 3] Error: {e}")

    # ── STEP 4: strip outer XML wrapper added by LLM and retry ──
    # The LLM sometimes wraps FIND in an outer tag (e.g. <para.text>, <para>,
    # <stat.item.para>) that isn't actually the boundary in the real XML.
    # Dynamically detect any outermost opening+closing tag pair and strip it.
    print(f"    [STEP 4] Checking for spurious outer XML wrapper...")
    outer_open  = re.match(r'^(<([a-zA-Z][a-zA-Z0-9._-]*)\b[^>]*>)', find_str)
    if outer_open:
        tag_name   = outer_open.group(2)
        close_tag  = f'</{tag_name}>'
        if find_str.rstrip().endswith(close_tag):
            inner_find = find_str[outer_open.end():]
            inner_find = inner_find[:inner_find.rfind(close_tag)]

            inner_replace = replace_str
            rep_open = re.match(r'^(<' + re.escape(tag_name) + r'\b[^>]*>)', replace_str)
            if rep_open and replace_str.rstrip().endswith(close_tag):
                inner_replace = replace_str[rep_open.end():]
                inner_replace = inner_replace[:inner_replace.rfind(close_tag)]

            print(f"    [STEP 4] Stripped <{tag_name}> wrapper. Inner FIND ({len(inner_find)} chars)")

            # 4a: exact
            if inner_find in xml_content:
                result = xml_content.replace(inner_find, inner_replace, 1)
                if _verify_replacement_safety(xml_content, result, idx, inner_find, inner_replace, "STEP 4a"):
                    print(f"    [STEP 4a] ✓ Exact inner match found. Replacing.")
                    return result
                else:
                    print(f"    [STEP 4a] ✗ Safety check failed, trying next strategy.")

            # 4b: normalized whitespace
            try:
                norm_inner = re.sub(r'\s+', ' ', inner_find.strip())
                norm_pos2  = norm_content.find(norm_inner)
                if norm_pos2 != -1:
                    o2_start, o2_end = _map_norm_range_to_orig(xml_content, norm_pos2, norm_pos2 + len(norm_inner))
                    o2_check = re.sub(r'\s+', ' ', xml_content[o2_start:o2_end].strip())
                    if o2_check == norm_inner:
                        result = xml_content[:o2_start] + inner_replace + xml_content[o2_end:]
                        if _verify_replacement_safety(xml_content, result, idx, inner_find, inner_replace, "STEP 4b"):
                            print(f"    [STEP 4b] ✓ Normalized inner match (orig {o2_start}:{o2_end}). Replacing.")
                            return result
                        else:
                            print(f"    [STEP 4b] ✗ Safety check failed.")
                    else:
                        print(f"    [STEP 4b] Sanity check failed.")
                else:
                    print(f"    [STEP 4b] ✗ Normalized inner not found.")
            except Exception as e:
                print(f"    [STEP 4b] Error: {e}")
        else:
            print(f"    [STEP 4] Opening <{tag_name}> found but no matching close, skipping.")
    else:
        print(f"    [STEP 4] No outer XML wrapper detected, skipping.")

    # ── STEP 5: LAST FALLBACK - Entity normalization matching ──
    # Handles cases where LLM uses plain chars ($, ') but XML has entities (&dollar;, &apos;)
    print(f"    [STEP 5] Trying entity-normalized matching (last fallback)...")
    try:
        # Whitespace-normalize both sides first
        norm_content_5 = re.sub(r'\s+', ' ', xml_content)
        norm_find_5 = re.sub(r'\s+', ' ', find_str.strip())

        # Convert entities to plain characters for matching on BOTH sides
        norm_content_entities = _normalize_entities_for_matching(norm_content_5)
        norm_find_entities = _normalize_entities_for_matching(norm_find_5)

        # ── DEBUG: print first difference to diagnose mismatches ──
        # Find approximate match position using a short anchor (no entities, no $)
        _anchor_match = re.search(
            r'incomplete or late reporting',
            norm_content_entities
        )
        if _anchor_match:
            _ap = _anchor_match.start()
            print(f"    [STEP 5 DEBUG] anchor found at {_ap} in norm_content_entities")
            print(f"    [STEP 5 DEBUG] content around anchor : {repr(norm_content_entities[_ap-20:_ap+120])}")
            print(f"    [STEP 5 DEBUG] find_entities snippet  : {repr(norm_find_entities[:140])}")
        else:
            print(f"    [STEP 5 DEBUG] anchor 'incomplete or late reporting' NOT found in entity-normalized content")

        norm_pos_5 = norm_content_entities.find(norm_find_entities)
        if norm_pos_5 == -1:
            # Try a more aggressive normalization pass:
            # Some LLM outputs use Unicode quotes/dashes that look identical
            # but have different code points. Strip ALL non-ASCII to spaces.
            def _ascii_only(t):
                return re.sub(r'[^\x00-\x7F]', ' ', t)
            ascii_content = re.sub(r'\s+', ' ', _ascii_only(norm_content_entities))
            ascii_find    = re.sub(r'\s+', ' ', _ascii_only(norm_find_entities))
            norm_pos_5 = ascii_content.find(ascii_find)
            if norm_pos_5 != -1:
                print(f"    [STEP 5] Found match via ASCII-only normalization at pos {norm_pos_5}")
                # For position mapping, use norm_content_entities (not ascii_content)
                # Re-find in entity-normalized content using a safe anchor
                _safe_anchor = ascii_find[10:60].strip()
                _recheck = norm_content_entities.find(_safe_anchor)
                if _recheck != -1:
                    # Walk back to find start of the match
                    norm_pos_5 = norm_content_entities.rfind(
                        norm_find_entities[:30] if norm_find_entities[:30] in norm_content_entities
                        else norm_content_entities[_recheck:_recheck+1],
                        0, _recheck + 10
                    )
                    if norm_pos_5 == -1:
                        norm_pos_5 = _recheck - (ascii_find.find(_safe_anchor.strip()) or 0)

        if norm_pos_5 != -1:
            print(f"    [STEP 5] Found match after entity normalization at pos {norm_pos_5}")

            norm_end_5 = norm_pos_5 + len(norm_find_entities)
            ws_start_5 = _map_entity_norm_pos_to_ws_norm(norm_content_5, norm_pos_5)
            ws_end_5   = _map_entity_norm_pos_to_ws_norm(norm_content_5, norm_end_5)

            orig_start_5, orig_end_5 = _map_norm_range_to_orig(xml_content, ws_start_5, ws_end_5)

            original_substring_5 = xml_content[orig_start_5:orig_end_5]
            original_normalized_5 = _normalize_entities_for_matching(
                re.sub(r'\s+', ' ', original_substring_5.strip())
            )

            similarity_5 = difflib.SequenceMatcher(
                None, original_normalized_5, norm_find_entities
            ).ratio()
            print(f"    [STEP 5] Similarity after entity normalization: {similarity_5:.3f}")

            if similarity_5 > 0.95:
                result = xml_content[:orig_start_5] + replace_str + xml_content[orig_end_5:]
                if _verify_replacement_safety(xml_content, result, idx, find_str, replace_str, "STEP 5"):
                    print(f"    [STEP 5] ✓ Entity-normalized match found (orig {orig_start_5}:{orig_end_5}). Replacing.")
                    return result
                else:
                    print(f"    [STEP 5] ✗ Safety check failed.")
            else:
                print(f"    [STEP 5] ✗ Similarity too low ({similarity_5:.3f}).")
        else:
            print(f"    [STEP 5] ✗ Not found even with entity normalization.")
    except Exception as e:
        print(f"    [STEP 5] Error: {e}")
        import traceback
        traceback.print_exc()

    # ── FAILURE ──
    print(f"    [FAIL] All replacement strategies (including STEP 2b) failed for idx={idx}. Logging.")
    fail_entry = {
        "idx": idx,
        "reason": "smart_replace: all strategies failed (including step 2b)",
        "find_str": find_str,
        "replace_str": replace_str,
    }
    if extra_context:
        fail_entry.update(extra_context)
    failed_log.append(fail_entry)
    return xml_content


# ──────────────────────────────────────────────
# 5b. LLM-BASED SECTION KEY MATCHING
# ──────────────────────────────────────────────

def _llm_yes_no(client, prompt_text, model_name=MODEL_SONNET, max_retries=3):
    """
    Call the LLM with a yes/no question. Retries up to *max_retries* times
    if the response is not a clean 'yes' or 'no'.
    Includes exponential backoff on rate limit errors.

    Returns
    -------
    str  – 'yes' or 'no'
    """
    for attempt in range(1, max_retries + 1):
        retry_delay = INITIAL_RETRY_DELAY
        rate_limit_attempts = 0
        
        while rate_limit_attempts < MAX_RETRIES_RATE_LIMIT:
            try:
                response = client.messages.create(
                    model=model_name,
                    max_tokens=5,
                    temperature=0,
                    messages=[{"role": "user", "content": prompt_text}],
                )
                answer = response.content[0].text.strip().lower().rstrip(".")
                if answer in ("yes", "no"):
                    print(f"    [LLM YES/NO] attempt {attempt} → '{answer}'")
                    return answer

                # Bad answer – build a retry prompt
                print(f"    [LLM YES/NO] attempt {attempt} got '{answer}', retrying...")
                prompt_text = (
                    f"You gave an invalid response: '{answer}'. "
                    "I strictly need only 'yes' or 'no'. Please answer again.\n\n"
                    + prompt_text
                )
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
                        print(f"    [LLM YES/NO] attempt {attempt} error: {e}")
                        break
                else:
                    print(f"    [LLM YES/NO] attempt {attempt} error: {e}")
                    break

    # Exhausted retries – default to 'no'
    print(f"    [LLM YES/NO] exhausted {max_retries} retries, defaulting to 'no'")
    return "no"


def match_section_with_llm(row_section, section_dict, client,
                           model_name=MODEL_SONNET):
    """
    Find the correct section_dict key for a given *row_section* value
    by sending ALL keys in a single LLM call and asking it to pick
    the exact match.

    Returns
    -------
    tuple (designator, section_xml)  or  (None, None)
    """
    row_section_str = str(row_section).strip()
    all_keys = list(section_dict.keys())

    if not all_keys:
        print(f"  [SECTION MATCH] section_dict is empty")
        return None, None

    keys_list_str = "\n".join(f"  - '{k}'" for k in all_keys)

    prompt = (
        "You are matching a section identifier to the correct designator key.\n\n"
        f"Section value: '{row_section_str}'\n\n"
        f"Available designator keys:\n{keys_list_str}\n\n"
        "Which designator key EXACTLY matches the section value?\n"
        "Rules:\n"
        "- A partial or substring match does NOT count "
        "(e.g. '5:1' is NOT the same as '5:13', '3:12' is NOT '3:12A').\n"
        "- The section value may contain extra text after the designator "
        "(e.g. '3:12 breach of contract' matches key '3:12').\n"
        "- Reply with ONLY the matching key exactly as shown above, "
        "or 'NONE' if no key matches.\n"
        "- Do not add any explanation."
    )

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        retry_delay = INITIAL_RETRY_DELAY
        rate_limit_attempts = 0
        
        while rate_limit_attempts < MAX_RETRIES_RATE_LIMIT:
            try:
                response = client.messages.create(
                    model=model_name,
                    max_tokens=30,
                    temperature=0,
                    messages=[{"role": "user", "content": prompt}],
                )
                answer = response.content[0].text.strip().strip("'\"")
                print(f"    [LLM MATCH] attempt {attempt} → '{answer}'")

                if answer.upper() == "NONE":
                    print(f"  [SECTION MATCH] LLM said NONE for '{row_section_str}'")
                    return None, None

                if answer in section_dict:
                    print(f"  [SECTION MATCH] LLM picked key='{answer}' for '{row_section_str}'")
                    return answer, section_dict[answer]

                # Answer wasn't a valid key — retry
                print(f"    [LLM MATCH] '{answer}' not in keys, retrying...")
                prompt = (
                    f"You gave '{answer}' which is not one of the available keys.\n"
                    "Reply with ONLY one of the keys listed below, or 'NONE'.\n\n"
                    f"Section value: '{row_section_str}'\n\n"
                    f"Available designator keys:\n{keys_list_str}\n"
                )
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
                        print(f"    [LLM MATCH] attempt {attempt} error: {e}")
                        break
                else:
                    print(f"    [LLM MATCH] attempt {attempt} error: {e}")
                    break

    print(f"  [SECTION MATCH] LLM failed after {max_retries} retries for '{row_section_str}'")
    return None, None


# ──────────────────────────────────────────────
# 6. MAIN PROCESS FUNCTION
# ──────────────────────────────────────────────

def process_with_llm(result_df, section_dict, pxml_content, client, failed_log_path, match_threshold=0.5,
                     model_name=MODEL_OPUS, chapter_number=None,
                     page_texts=None, image_paths=None):
    
    result_df = result_df.copy()

    # For pure deletions: text_after is NaN because nothing remains after deletion
    # Fill with empty string so they aren't dropped
    result_df["text_after"] = result_df["text_after"].fillna("")

    changed_df = (
        result_df[result_df["has_changes"] == True]
        .dropna(subset=["text_before", "text_after"])
    )
    changed_df = changed_df[
        (
            (changed_df["text_before"] != "") &
            (changed_df["text_before"].str.len() > 5)
        ) |
        (changed_df["type"] == "inline_deletion")
    ]
    if changed_df.empty:
        print('No track changes detected in any paragraph deletions,insertions,replacements')
        return pxml_content, [], []

    collapsed = (
        changed_df
        .groupby("idx", sort=False)
        .apply(collapse_changes)
        .tolist()
    )

    print(f"\n{'='*60}")
    print(f"[INFO] Total unique changed paragraphs to process: {len(collapsed)}")
    print(f"{'='*60}\n")

    patched_content = pxml_content
    failed_log      = []
    processed_idxs  = []

    # ── Image-revision mapping log ──
    _image_mapping_log = []

    # ── Pre-process: split paragraphs shared by multiple rows ──
    # COMMENTED OUT — para split can corrupt footnotes by splitting inside nested elements
    # from paragraph_split_handler import preprocess_paragraph_splits
    # patched_content, para_split_indices = preprocess_paragraph_splits(
    #     collapsed_items=collapsed,
    #     pxml_content=patched_content,
    #     section_dict=section_dict,
    # )
    # # Re-parse section_dict after any splits
    # section_dict = parse_pxml_sections_from_str_new(patched_content, chapter_number)
    para_split_indices = []

    for item in collapsed:
        idx                   = item["idx"]
        text_before            = item["text_before"]
        text_after             = item["text_after"]
        previous_para          = item["previous_para"]
        post_para              = item["post_para"]
        row_section            = item["section"]
        changes                = item["changes"]
        fn_superscript_before  = item.get("fn_superscript_before", "") or ""
        fn_parent_para         = item.get("fn_parent_para", "") or ""
        _is_full_fn_deletion   = item.get("_footnote_full_deletion", False)

        print(f"\n{'─'*60}")
        is_footnote_row = bool(fn_parent_para)
        print(f"[PROCESSING] idx={idx}  |  changes in this paragraph: {len(changes)}"
              f"{'  [FOOTNOTE]' if is_footnote_row else ''}")
        print(f"  text_before   : {strip_tags(text_before)[:100]}")
        print(f"  text_after    : {strip_tags(text_after)[:100]}")
        if is_footnote_row:
            print(f"  parent_para   : {strip_tags(str(fn_parent_para))[:100]}")
            print(f"  text_before_ss: {strip_tags(str(fn_superscript_before))[:100]}")
        else:
            print(f"  previous_para : {strip_tags(str(previous_para))[:80] if previous_para else 'None'}")
            print(f"  post_para     : {strip_tags(str(post_para))[:80] if post_para else 'None'}")
        print(f"  section       : {row_section}")

        for i, ch in enumerate(changes, 1):
            print(f"  change {i}: type={ch['type']} | "
                  f"del={str(ch['deleted_text'])[:40]} | "
                  f"ins={str(ch['inserted_text'])[:40]} | "
                  f"prev_ctx=...{str(ch['prev_context'])[:30]} | "
                  f"post_ctx={str(ch['post_context'])[:30]}...")

        # ── Guard: skip rows where diff engine mis-scoped a footnote reference ──
        if _is_footnote_ref_misscoped_row(text_before, text_after, changes):
            print(
                f"  [SKIP] idx={idx}: text_after='{strip_tags(text_after).strip()}' "
                f"looks like a footnote reference number but text_before is prose "
                f"({len(strip_tags(text_before))} chars). "
                f"Diff engine mis-scoped this row — skipping."
            )
            failed_log.append({
                "idx":          idx,
                "reason":       (
                    "REVISION FAILED - Diff engine produced a mis-scoped row: "
                    "text_after is a footnote reference number but text_before is "
                    "prose. The footnote body change should arrive as a separate "
                    "footnote-flow row. Please review manually."
                ),
                "section":       row_section,
                "designator":    None,
                "text_before":   text_before,
                "text_after":    text_after,
                "previous_para": previous_para,
                "post_para":     post_para,
                "changes":       changes,
            })
            continue

        # ── Skip footnote rows where text_before is just a number ──
        if is_footnote_row and strip_tags(text_before).strip().isdigit():
            print(f"  [SKIP] idx={idx}: footnote text_before is just a number '{strip_tags(text_before).strip()}' — skipping.")
            continue

        print(f"\n  [SECTION MATCH]")
        designator  = None
        section_xml = None
        print('row_section:---------------------------', row_section)

        # ── Override: if text_before IS a section heading, use that section ──
        # e.g. text_before="14:12. Limitation Period-- Discoverability"
        # was assigned to prev section 14:11A by backward fill, but the XML
        # for this designator block lives under section 14:12.
        _heading_override = None
        if chapter_number and text_before:
            _ch = re.escape(str(chapter_number))
            _hm = re.match(rf'^§?\s*({_ch}[:.][\dA-Za-z]+(?:\.[\dA-Za-z]+)*)\.?\s', text_before)
            if _hm:
                _candidate = _hm.group(1)
                if _candidate in section_dict:
                    _heading_override = _candidate
                    print(f"  [SECTION MATCH] text_before starts with designator '{_candidate}' — overriding section from '{row_section}' to '{_candidate}'")

        if _heading_override:
            designator = _heading_override
            section_xml = section_dict[designator]
        elif row_section:
            # Direct key lookup first (handles chapter preamble key like "17")
            row_section_stripped = str(row_section).strip()
            if row_section_stripped in section_dict:
                designator = row_section_stripped
                section_xml = section_dict[designator]
                print(f"  [SECTION MATCH] Direct key match: '{designator}'")
            else:
                designator, section_xml = match_section_with_llm(
                    row_section, section_dict, client, model_name=model_name
                )

        if designator is None:
            print(f"  [SKIP] No matching section key found in section column "
                  f"value '{row_section}'. Skipping.")
            failed_log.append({
                "idx": idx, "reason": f"No matching section key for '{row_section}'",
                "section": row_section, "text_before": text_before, "text_after": text_after,
                "previous_para": previous_para, "post_para": post_para, "changes": changes,
            })
            continue

        print(f"\n  [SECTION REFRESH] Re-extracting section {designator} "
              f"from current document state...")
        current_section_dict = parse_pxml_sections_from_str_new(patched_content)
        current_section_xml  = current_section_dict.get(designator, section_xml)
        print(f"  Section length: {len(current_section_xml)} chars")

        xml_chunk = current_section_xml
        print(f"  XML chunk length (full section): {len(xml_chunk)} chars")

        print(f"\n  [LLM] Sending to Anthropic (model={model_name})...")

        max_llm_retries = 3
        llm_succeeded = False

        for llm_attempt in range(1, max_llm_retries + 1):
            if llm_attempt > 1:
                print(f"\n  [RETRY] LLM attempt {llm_attempt}/{max_llm_retries} for idx={idx}...")

            try:
                # Resolve page image for this revision
                _page_img = None
                if page_texts and image_paths:
                    from .revisions_images import get_image_for_revision
                    _page_img = get_image_for_revision(text_before, page_texts, image_paths, previous_para=previous_para)

                # Log image mapping for verification
                _image_mapping_log.append({
                    "idx": idx,
                    "text_before": strip_tags(text_before)[:200],
                    "text_after": strip_tags(text_after)[:200],
                    "image_path": _page_img or "NO_MATCH",
                })

                # ── DEBUG: type-check all arguments before LLM call ──
                print(f"  [DEBUG-TYPES] xml_chunk: {type(xml_chunk).__name__} (len={len(xml_chunk) if isinstance(xml_chunk, str) else 'N/A'})")
                print(f"  [DEBUG-TYPES] text_before: {type(text_before).__name__} | value={repr(text_before)[:80]}")
                print(f"  [DEBUG-TYPES] text_after: {type(text_after).__name__} | value={repr(text_after)[:80]}")
                print(f"  [DEBUG-TYPES] previous_para: {type(previous_para).__name__} | value={repr(previous_para)[:80]}")
                print(f"  [DEBUG-TYPES] post_para: {type(post_para).__name__} | value={repr(post_para)[:80]}")
                print(f"  [DEBUG-TYPES] fn_superscript_before: {type(fn_superscript_before).__name__} | value={repr(fn_superscript_before)[:80]}")
                print(f"  [DEBUG-TYPES] fn_parent_para: {type(fn_parent_para).__name__} | value={repr(fn_parent_para)[:80]}")
                for ci, ch in enumerate(changes):
                    for k, v in ch.items():
                        if not isinstance(v, str) and v is not None:
                            print(f"  [DEBUG-TYPES] changes[{ci}]['{k}']: {type(v).__name__} | value={repr(v)[:80]}")

                llm_response = call_llm_for_trace_tagging(
                    xml_chunk,
                    text_before,
                    text_after,
                    previous_para,
                    post_para,
                    client,
                    changes=changes,
                    model_name=model_name,
                    fn_superscript_before=fn_superscript_before or None,
                    fn_parent_para=fn_parent_para or None,
                    page_image_path=_page_img,
                    strip_save_dir=os.path.join(os.path.dirname(failed_log_path), "image_strips"),
                    idx=idx,
                )
            except Exception as e:
                import traceback
                print(f"  [ERROR] LLM call failed (attempt {llm_attempt}): {e}")
                print(f"  [ERROR] Full traceback:")
                traceback.print_exc()
                if llm_attempt == max_llm_retries:
                    failed_log.append({
                        "idx": idx, "reason": f"LLM call failed after {max_llm_retries} attempts: {e}",
                        "section": row_section, "designator": designator,
                        "text_before": text_before, "text_after": text_after,
                        "previous_para": previous_para, "post_para": post_para, "changes": changes,
                    })
                continue

            print(f"  [LLM] Raw response received ({len(llm_response)} chars):")
            print(f"  {'·'*50}")
            print(f"  {llm_response[:400]}")
            print(f"  {'·'*50}")

            # Check for leakage — with JSON format, reasoning inside the JSON is OK
            # We only flag leakage if it's OUTSIDE the JSON structure
            _leakage_reason = None
            _is_valid_json = False
            try:
                _cleaned = llm_response.strip()
                if _cleaned.startswith('```'):
                    _cleaned = re.sub(r'^```(?:json)?\s*', '', _cleaned)
                    _cleaned = re.sub(r'\s*```$', '', _cleaned)
                _test_parsed = json.loads(_cleaned)
                if isinstance(_test_parsed, dict) and 'find' in _test_parsed and 'replace' in _test_parsed:
                    _is_valid_json = True
            except (json.JSONDecodeError, TypeError):
                pass

            # If direct parse failed, try to extract JSON object from within the response
            # (handles case where LLM prefixes reasoning text before the JSON)
            if not _is_valid_json:
                _json_start = llm_response.find('{')
                if _json_start > 0:
                    # Find the matching closing brace by scanning from the end
                    _json_candidate = llm_response[_json_start:]
                    # Try to find the last '}' that forms valid JSON
                    _last_brace = _json_candidate.rfind('}')
                    while _last_brace > 0:
                        _try_json = _json_candidate[:_last_brace + 1]
                        try:
                            _test_parsed = json.loads(_try_json)
                            if isinstance(_test_parsed, dict) and 'find' in _test_parsed and 'replace' in _test_parsed:
                                _is_valid_json = True
                                # Replace llm_response with the extracted JSON for downstream parsing
                                _preamble = llm_response[:_json_start].strip()
                                print(f"  [LEAKAGE FIX] Stripped reasoning preamble ({len(_preamble)} chars) before JSON object.")
                                print(f"    Preamble: '{_preamble[:120]}...'")
                                llm_response = _try_json
                                break
                        except (json.JSONDecodeError, TypeError):
                            pass
                        _last_brace = _json_candidate.rfind('}', 0, _last_brace)

            if not _is_valid_json:
                # Not valid JSON — check for old-style leakage patterns
                if '```' in llm_response:
                    _leakage_reason = "code fences (non-JSON response)"
                elif llm_response.count('FIND:') > 1:
                    _leakage_reason = f"multiple FIND blocks ({llm_response.count('FIND:')}) — model self-corrected"
                else:
                    # Only flag reasoning patterns if response is NOT valid JSON
                    # (reasoning inside JSON "reasoning" field is expected and fine)
                    _reasoning_patterns = [
                        "Let me ", "There's an issue", "I notice", "Wait,", "The issue is",
                        "Let me re-examine", "Let me provide", "The corrected XML",
                        "Here's the corrected", "I need to", "Actually,", "Hmm,",
                        "However,", "Note:", "Note that", "The problem", "issue with",
                    ]
                    _response_lower = llm_response.lower()
                    for _pat in _reasoning_patterns:
                        if _pat.lower() in _response_lower:
                            _leakage_reason = f"reasoning text detected outside JSON ('{_pat}')"
                            break
            if _leakage_reason:
                print(f"  [FAIL] LLM response has leakage: {_leakage_reason} (attempt {llm_attempt}). Retrying...")
                if llm_attempt == max_llm_retries:
                    failed_log.append({
                        "idx": idx, "reason": f"LLM leakage after {max_llm_retries} attempts: {_leakage_reason}",
                        "section": row_section, "designator": designator,
                        "text_before": text_before, "text_after": text_after,
                        "previous_para": previous_para, "post_para": post_para, "changes": changes,
                        "llm_response": llm_response,
                    })
                continue

            print(f"\n  [PARSE] Extracting FIND and REPLACE from LLM response...")
            find_str, replace_str = parse_llm_response(llm_response)

            if not find_str or not replace_str:
                print(f"  [FAIL] Could not parse FIND/REPLACE (attempt {llm_attempt}).")
                if llm_attempt == max_llm_retries:
                    failed_log.append({
                        "idx": idx, "reason": f"Could not parse FIND/REPLACE after {max_llm_retries} attempts",
                        "section": row_section, "designator": designator,
                        "text_before": text_before, "text_after": text_after,
                        "previous_para": previous_para, "post_para": post_para, "changes": changes,
                        "llm_response": llm_response,
                    })
                continue

            # ── Sanity check: FIND must start with an XML tag ────────────────────────
            if not find_str.strip().startswith('<'):
                print(f"  [FAIL] FIND does not start with XML tag (attempt {llm_attempt}). "
                      f"LLM returned reasoning text instead of XML.")
                print(f"    FIND starts with: '{find_str[:100]}'")
                if llm_attempt == max_llm_retries:
                    failed_log.append({
                        "idx": idx, "reason": "LLM returned reasoning text instead of XML in FIND",
                        "section": row_section, "designator": designator,
                        "text_before": text_before, "text_after": text_after,
                        "previous_para": previous_para, "post_para": post_para, "changes": changes,
                        "llm_response": llm_response,
                    })
                continue

            # ── Sanitize FIND: strip trace tags the LLM may have leaked ──
            # NOTE: Commented out — FIND can legitimately contain trace tags from prior edits in the same loop
            # _trace_tag_re = re.compile(
            #     r'</?trace(?:\.deleted)?(?:\s[^>]*)?>',
            #     re.IGNORECASE,
            # )
            # if _trace_tag_re.search(find_str):
            #     cleaned_find = _trace_tag_re.sub('', find_str)
            #     print(f"  [FIX] FIND contained trace tags — stripped them out.")
            #     print(f"    Before: {find_str[:150]}")
            #     print(f"    After : {cleaned_find[:150]}")
            #     find_str = cleaned_find

            # ── Reconstruct FIND from REPLACE if LLM leaked inserted content ──
            # If REPLACE has <trace>CONTENT</trace>, that CONTENT is newly inserted
            # and should NOT appear in FIND (the original XML). If FIND doesn't
            # match the section XML, reconstruct it by removing <trace> blocks
            # from REPLACE (keeps <trace.deleted> content which IS in the original).
            norm_find_check = re.sub(r'\s+', ' ', find_str).strip()
            norm_section_check = re.sub(r'\s+', ' ', current_section_xml)
            if norm_find_check not in norm_section_check:
                reconstructed = _remove_trace_insertions(replace_str)
                reconstructed = re.sub(r' {2,}', ' ', reconstructed)   # collapse double spaces
                recon_norm = re.sub(r'\s+', ' ', reconstructed).strip()
                if recon_norm != norm_find_check and recon_norm in norm_section_check:
                    print(f"  [FIX] FIND contained inserted text not in original XML — reconstructed from REPLACE.")
                    print(f"    Original FIND  ({len(find_str)} chars): {find_str[:120]}")
                    print(f"    Reconstructed  ({len(reconstructed)} chars): {reconstructed[:120]}")
                    find_str = reconstructed

          
            # ── Fix: strip trace tags wrapping or inside <footnote> elements ──
            # Footnote deletions are handled in footnotes_level_trackchanges, not here.
            # Fix 1: If LLM wrapped <footnote>...</footnote> in <trace.deleted>, unwrap it.
            _fn_in_trace_pat = re.compile(
                r'<trace\.deleted>(\s*<footnote\b[^>]*>[\s\S]*?</footnote>\s*)<trace\.deleted\s*/>',
                re.DOTALL
            )
            if _fn_in_trace_pat.search(replace_str):
                _before_fix = replace_str
                replace_str = _fn_in_trace_pat.sub(r'\1', replace_str)
                print(f"  [FIX] Stripped trace.deleted wrapping around <footnote> element(s) in REPLACE.")
                print(f"    Delta: {len(_before_fix) - len(replace_str)} chars removed")

            # Fix 2: If LLM inserted trace tags INSIDE <footnote>...</footnote>, strip them.
            def _strip_trace_inside_footnotes(text):
                def _clean_fn(m):
                    fn_content = m.group(0)
                    cleaned = re.sub(r'<trace\.deleted\s*/?>', '', fn_content)
                    cleaned = re.sub(r'</?trace>', '', cleaned)
                    return cleaned
                return re.sub(r'<footnote\b[^>]*>[\s\S]*?</footnote>', _clean_fn, text, flags=re.DOTALL)

            _before_fix2 = replace_str
            replace_str = _strip_trace_inside_footnotes(replace_str)
            if replace_str != _before_fix2:
                print(f"  [FIX] Stripped trace tags from inside <footnote> element(s) in REPLACE.")
                print(f"    Delta: {len(_before_fix2) - len(replace_str)} chars removed")

            # ── DEBUG: print FULL FIND and REPLACE strings ──────────────────
            print(f"\n  [DEBUG] FULL FIND ({len(find_str)} chars):")
            print(f"  {find_str}")
            print(f"\n  [DEBUG] FULL REPLACE ({len(replace_str)} chars):")
            print(f"  {replace_str}\n")

            # ── Validate trace tags & merge adjacent ones ─────────────────
            find_str, replace_str, tags_valid, failed_changes = _validate_and_fix_trace_tags(
                find_str, replace_str, idx,
                client=client, model_name=model_name, changes=changes,
            )
            # If FIND == REPLACE and already applied, skip without replacing
            if tags_valid and find_str.strip() == replace_str.strip():
                print(f"  [SKIP] idx={idx} — change already applied in document. No action needed.")
                llm_succeeded = True
                break
            if tags_valid:
                if not _check_trace_nesting(replace_str, idx):
                    print(f"  [FAIL] Nested trace corruption detected (attempt {llm_attempt}).")
                    tags_valid = False
            if not tags_valid:
                # Basic structure broken (no tags at all or FIND==REPLACE) → retry
                print(f"  [FAIL] Trace tag validation failed (attempt {llm_attempt}).")
                if llm_attempt == max_llm_retries:
                    failed_log.append({
                        "idx": idx, "reason": f"Trace tag validation failed after {max_llm_retries} attempts",
                        "section": row_section, "designator": designator,
                        "text_before": text_before, "text_after": text_after,
                        "previous_para": previous_para, "post_para": post_para, "changes": changes,
                        "find_str": find_str, "replace_str": replace_str,
                    })
                continue

            # Log individual changes that could not be verified
            if failed_changes:
                for fc in failed_changes:
                    failed_log.append({
                        "idx": idx,
                        "reason": f"Please verify this change - Change {fc['change_num']} trace tag not verified: {fc.get('reason', '')}",
                        "section": row_section, "designator": designator,
                        "text_before": text_before, "text_after": text_after,
                        "change_detail": fc,
                    })
                print(f"  [INFO] {len(failed_changes)} unverified change(s) logged. Proceeding with replacement.")

            # All basic checks passed — proceed
            # ── Image-based verification: DISABLED (causes LLM reasoning leakage into XML) ──
            # if _page_img and os.path.isfile(_page_img):
            #     print(f"  [IMAGE VERIFY] Verifying trace tags against page image...")
            #     try:
            #         replace_str = _verify_traces_with_image(
            #             replace_str, text_before, text_after, _page_img, client, model_name
            #         )
            #     except Exception as img_err:
            #         print(f"  [IMAGE VERIFY] WARNING: Verification failed, using original: {img_err}")
            #     _structural_unwrap_pat = re.compile(
            #         r'<trace\.deleted>\s*(<(?:quote\.block|quote\.para|list)\b[^>]*>[\s\S]*?</(?:quote\.block|quote\.para|list)>)\s*<trace\.deleted\s*/>',
            #         re.DOTALL
            #     )
            #     _before_unwrap = replace_str
            #     replace_str = _structural_unwrap_pat.sub(r'\1', replace_str)
            #     if replace_str != _before_unwrap:
            #         print(f"  [IMAGE VERIFY FIX] Stripped trace.deleted wrapping structural elements added by image verifier.")

            llm_succeeded = True
            break

        if not llm_succeeded:
            continue

        # Skip smart_replace if change was already applied (FIND == REPLACE)
        if find_str.strip() == replace_str.strip():
            print(f"  [SKIP] idx={idx} — already applied, no replacement needed.")
            continue

        print(f"\n  [REPLACE] Attempting smart replace within section XML...")
        _ctx = {
            "section": row_section, "designator": designator,
            "text_before": text_before, "text_after": text_after,
            "previous_para": previous_para, "post_para": post_para,
            "changes": changes,
        }
        modified_section = smart_replace(
            current_section_xml, find_str, replace_str, idx, failed_log,
            extra_context=_ctx,
        )

        # Splice modified section back into full content
        if modified_section != current_section_xml:
            pos = patched_content.find(current_section_xml)
            if pos != -1:
                patched_content = (
                    patched_content[:pos] +
                    modified_section +
                    patched_content[pos + len(current_section_xml):]
                )
                print(f"  [SPLICE] Modified section '{designator}' spliced back into full content.")
            else:
                print(f"  [WARN] Could not find section '{designator}' in full content for splicing. "
                      f"Falling back to full-content replace...")
                patched_content = smart_replace(
                    patched_content, find_str, replace_str, idx, failed_log,
                    extra_context=_ctx,
                )
        else:
            print(f"  [WARN] Section XML unchanged after smart_replace — replacement likely failed.")

        processed_idxs.append(idx)

    # ── Post-processing: collapse fully-deleted structural elements ──
    # Comment out the next 3 lines to disable this pass.
    # print(f"\n{'='*60}")
    # print(f"[POST-PROCESS] Collapsing fully-deleted structural elements...")
    # print(f"{'='*60}")
    # patched_content = collapse_fully_deleted_elements(patched_content)

    if failed_log:
        print(f"\n[FAILED LOG] {len(failed_log)} items failed. "
              f"Writing to: {failed_log_path}")
        sep = "=" * 70
        with open(failed_log_path, "w", encoding="utf-8") as f:
            f.write(f"FAILED TRACE-TAGGING LOG\n{sep}\n\n")
            for item in failed_log:
                f.write(f"{sep}\n")
                f.write(f"  idx           : {item.get('idx')}\n")
                f.write(f"  reason        : {item.get('reason', 'N/A')}\n")
                f.write(f"  section       : {item.get('section', 'N/A')}\n")
                f.write(f"  designator    : {item.get('designator', 'N/A')}\n")
                f.write(f"  text_before   : {str(item.get('text_before', ''))[:500]}\n")
                f.write(f"  text_after    : {str(item.get('text_after', ''))[:500]}\n")
                f.write(f"  previous_para : {str(item.get('previous_para', ''))[:300]}\n")
                f.write(f"  post_para     : {str(item.get('post_para', ''))[:300]}\n")
                changes_list = item.get('changes', [])
                if changes_list:
                    f.write(f"  changes ({len(changes_list)}):\n")
                    for i, ch in enumerate(changes_list, 1):
                        f.write(f"    change {i}: type={ch.get('type')} | "
                                f"del={ch.get('deleted_text')} | "
                                f"ins={ch.get('inserted_text')} | "
                                f"prev_ctx={ch.get('prev_context')} | "
                                f"post_ctx={ch.get('post_context')}\n")
                if item.get('find_str'):
                    f.write(f"  find_str      :\n{item['find_str']}\n")
                if item.get('replace_str'):
                    f.write(f"  replace_str   :\n{item['replace_str']}\n")
                if item.get('llm_response'):
                    f.write(f"  llm_response  :\n{item['llm_response'][:1000]}\n")
                f.write(f"{sep}\n\n")
        print(f"[FAILED LOG] Written.")

        # ── Build failed rows list for consolidated Excel output ─────────
        def _user_friendly_reason(reason):
            """Convert technical reason to user-friendly message for Excel output."""
            if not reason:
                return "REVISION FAILED - Unknown error"
            reason_lower = reason.lower()
            if "trace tag not verified" in reason_lower or "please verify" in reason_lower:
                return "PLEASE VERIFY - This revision may or may not have been applied correctly in Output XML"
            if "no trace tags at all" in reason_lower:
                return "REVISION FAILED - No track change tags were applied"
            if "find and replace are identical" in reason_lower:
                return "REVISION FAILED - No changes were applied"
            if "no matching section" in reason_lower:
                return "REVISION FAILED - Could not find the section in the document"
            if "llm call failed" in reason_lower or "system error" in reason_lower:
                return "REVISION FAILED - System error while processing"
            if "could not parse" in reason_lower:
                return "REVISION FAILED - System could not process this change"
            if "trace tag validation failed" in reason_lower:
                return "REVISION FAILED - Could not make inline changes due to trace tag validation failure"
            if "smart_replace" in reason_lower or "all strategies failed" in reason_lower:
                return "REVISION FAILED - all strategies failed to locate text in document to apply changes(may be searching in wrong section)"
            # Default: prefix with REVISION FAILED if not already clear
            if not reason.startswith(("REVISION", "PLEASE")):
                return f"REVISION FAILED - {reason}"
            return reason

        excel_rows = []
        for item in failed_log:
            # Entries with change_detail come from per-change validation
            cd = item.get('change_detail')
            if cd:
                excel_rows.append({
                    "Row Index": item.get('idx'),
                    "Section": item.get('section', ''),
                    "Designator": item.get('designator', ''),
                    "Reason": _user_friendly_reason(item.get('reason', '')),
                    "Change Type": cd.get('type', ''),
                    "Inserted Text": cd.get('inserted_text', ''),
                    "Deleted Text": cd.get('deleted_text', ''),
                    "Previous Context": cd.get('prev_context', ''),
                    "Post Context": cd.get('post_context', ''),
                    "Docx_Paragraph": item.get('text_before', ''),
                })
            else:
                # Whole-paragraph failures (no section match, LLM error, etc.)
                changes_list = item.get('changes', [])
                if changes_list:
                    for ch in changes_list:
                        excel_rows.append({
                            "Row Index": item.get('idx'),
                            "Section": item.get('section', ''),
                            "Designator": item.get('designator', ''),
                            "Reason": _user_friendly_reason(item.get('reason', '')),
                            "Change Type": ch.get('type', ''),
                            "Inserted Text": ch.get('inserted_text', ''),
                            "Deleted Text": ch.get('deleted_text', ''),
                            "Previous Context": ch.get('prev_context', ''),
                            "Post Context": ch.get('post_context', ''),
                            "Docx_Paragraph": item.get('text_before', ''),
                        })
                else:
                    excel_rows.append({
                        "Row Index": item.get('idx'),
                        "Section": item.get('section', ''),
                        "Designator": item.get('designator', ''),
                        "Reason": _user_friendly_reason(item.get('reason', '')),
                        "Change Type": '',
                        "Inserted Text": '',
                        "Deleted Text": '',
                        "Previous Context": '',
                        "Post Context": '',
                        "Docx_Paragraph": item.get('text_before', ''),
                    })

        print(f"[FAILED LOG] {len(excel_rows)} rows prepared for consolidated Excel")

    else:
        print(f"\n[FAILED LOG] No failures. All items processed successfully.")
        excel_rows = []

    print(f"\n[DONE] Total failed: {len(failed_log)} / {len(collapsed)}")

    # ── Write image-revision mapping log for verification ──
    if _image_mapping_log:
        mapping_path = os.path.join(os.path.dirname(failed_log_path), "image_revision_mapping.txt")
        # Append mode so footnote pass doesn't overwrite main pass
        write_mode = "a" if os.path.exists(mapping_path) else "w"
        with open(mapping_path, write_mode, encoding="utf-8") as mf:
            if write_mode == "w":
                mf.write("IMAGE-REVISION MAPPING LOG\n" + "=" * 70 + "\n\n")
            for entry in _image_mapping_log:
                mf.write(f"idx: {entry['idx']}\n")
                mf.write(f"  text_before : {entry['text_before']}\n")
                mf.write(f"  text_after  : {entry['text_after']}\n")
                mf.write(f"  image_path  : {entry['image_path']}\n")
                # Show saved strip filenames
                if entry['image_path'] and entry['image_path'] != "NO_MATCH":
                    page_stem = os.path.splitext(os.path.basename(entry['image_path']))[0]
                    mf.write(f"  strips      : idx{entry['idx']}_{page_stem}_strip1.png, strip2.png, strip3.png\n")
                mf.write("-" * 70 + "\n")
        print(f"[IMAGE MAP] Written {len(_image_mapping_log)} entries to: {mapping_path}")

    return patched_content, processed_idxs, excel_rows if excel_rows else []


def process_with_llm_footnotes(result_df, section_dict, pxml_content, client, failed_log_path,
                               match_threshold=0.5, model_name=MODEL_OPUS, chapter_number=None,
                               page_texts=None, image_paths=None):
    """
    Footnote-specific wrapper for process_with_llm.

    Context strategy for footnote rows:
      - previous_para := text_before_superscript
      - post_para     := parent paragraph text (if available)
                        from `parent_paragraph_text` or
                        `full_para_where_footnote_is_present`.
    """
    df = result_df.copy()

    # Populate dedicated footnote context columns (do NOT overwrite previous_para / post_para
    # which are used as generic positional anchors in the prompt — the fn_ columns get
    # their own clearly-labelled context block so the LLM is not confused).
    if "text_before_superscript" in df.columns:
        df["fn_superscript_before"] = df["text_before_superscript"].fillna("")
    else:
        df["fn_superscript_before"] = ""

    if "parent_paragraph_text" in df.columns:
        df["fn_parent_para"] = df["parent_paragraph_text"].fillna("")
    elif "full_para_where_footnote_is_present" in df.columns:
        df["fn_parent_para"] = df["full_para_where_footnote_is_present"].fillna("")
    else:
        df["fn_parent_para"] = ""

    # Ensure fn_parent_para is never empty so all rows are treated as footnotes
    # inside process_with_llm (is_footnote_row = bool(fn_parent_para))
    df["fn_parent_para"] = df["fn_parent_para"].apply(
        lambda v: v if v.strip() else "(footnote - parent paragraph not available)"
    )

    print(
        f"[FOOTNOTE FLOW] Using footnote context for {len(df)} row(s): "
        f"fn_superscript_before=text_before_superscript, fn_parent_para=parent paragraph"
    )

    return process_with_llm(
        result_df=df,
        section_dict=section_dict,
        pxml_content=pxml_content,
        client=client,
        failed_log_path=failed_log_path,
        match_threshold=match_threshold,
        model_name=model_name,
        chapter_number=chapter_number,
        page_texts=page_texts,
        image_paths=image_paths,
    )

