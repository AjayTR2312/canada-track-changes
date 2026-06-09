import os
import re
import json
import time
import pandas as pd
from typing import Optional

from inline_footnotes_insertion import build_anthropic_client
from config import MODEL_SONNET

# ── Module-level LLM client (lazy initialization) ─────────────────────────────
_client = None
_model_name = None

WORKSPACE_ID = os.getenv("TR_WORKSPACE_ID", "")
MODEL_NAME = MODEL_SONNET

# ── Rate limit handling settings ──────────────────────────────────────────────
MAX_RETRIES = 5
INITIAL_RETRY_DELAY = 10  # seconds
MAX_RETRY_DELAY = 120     # seconds


def _get_client():
    """Lazy initialization of Anthropic client."""
    global _client, _model_name
    if _client is None:
        _client, _model_name = build_anthropic_client(WORKSPACE_ID, MODEL_NAME)
    return _client, _model_name


def _call_llm_with_retry(
    client, model_name: str, messages: list,
    max_tokens: int = 50,
    max_retries: int = MAX_RETRIES,
    verbose: bool = False,
) -> Optional[str]:
    """
    Call LLM with exponential backoff retry on rate limit errors.
    Returns the response text or None on failure.
    """
    retry_delay = INITIAL_RETRY_DELAY

    for attempt in range(max_retries):
        try:
            message = client.messages.create(
                model=model_name,
                max_tokens=max_tokens,
                messages=messages,
            )
            return message.content[0].text.strip()

        except Exception as e:
            error_str = str(e)
            if "rate_limit" in error_str.lower() or "429" in error_str:
                if attempt < max_retries - 1:
                    print(f"  [RATE LIMIT] Attempt {attempt + 1}/{max_retries} - Waiting {retry_delay}s before retry...")
                    time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, MAX_RETRY_DELAY)
                else:
                    print(f"  [RATE LIMIT] Max retries ({max_retries}) exceeded. Giving up.")
                    return None
            else:
                print(f"  [LLM error] {e}")
                return None

    return None


# ── STEP 1: One-time LLM call to build the pre-filter regex ──────────────────

def build_prefilter_regex_with_llm(
    chapter_number: str,
    designator_patterns: list,
    client,
    model_name: str,
    verbose: bool = False,
) -> re.Pattern:
    """
    ONE-TIME LLM call at startup.
    Given the full list of known section designators, ask LLM to produce
    a broad regex that matches any string containing one of those designators.

    This regex is used purely as a cheap pre-filter to eliminate rows that
    cannot possibly contain a section reference — no LLM needed per row.
    Falls back to a safe hand-crafted pattern if the LLM fails.
    """
    if not designator_patterns:
        escaped = re.escape(str(chapter_number))
        fallback = re.compile(rf'§?§?\s*{escaped}[:\.][\d]', re.IGNORECASE)
        if verbose:
            print(f"  [PREFILTER] No designator_patterns provided, using fallback: {fallback.pattern}")
        return fallback

    patterns_str = json.dumps(designator_patterns)  # full list, no truncation

    prompt = f"""You are a regex expert. Given the complete list of known section designators for
chapter {chapter_number}, produce a single Python regex pattern that matches ANY string
containing one of those designators (or a closely related variant such as one prefixed
with '§' or '§§' and optional whitespace).

Known designators (complete list):
{patterns_str}

Requirements:
1. The pattern must be a valid Python regex (used with re.search, IGNORECASE flag).
2. It must match every designator in the list and reasonable variants (e.g. '§ 17:5', '§§17:5').
3. Keep it as SIMPLE and BROAD as possible — false positives are acceptable because a
   later LLM call will filter them out. False negatives are NOT acceptable.
4. Do NOT use lookaheads/lookbehinds or overly complex constructs.
5. Return ONLY the raw regex string — no explanation, no code fences, no quotes."""

    result = _call_llm_with_retry(
        client, model_name,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=200,
    )

    if result:
        result = result.strip().strip('`').strip('"').strip("'")
        try:
            compiled = re.compile(result, re.IGNORECASE)
            if verbose:
                print(f"  [PREFILTER] LLM-derived regex: {compiled.pattern}")
            return compiled
        except re.error as exc:
            print(f"  [PREFILTER] LLM regex invalid ({exc}), falling back to safe pattern.")

    # Safe fallback
    escaped = re.escape(str(chapter_number))
    fallback = re.compile(rf'§?§?\s*{escaped}[:\.][\d]', re.IGNORECASE)
    if verbose:
        print(f"  [PREFILTER] Using fallback regex: {fallback.pattern}")
    return fallback


# ── STEP 2: Pure regex extraction — zero LLM calls ───────────────────────────

def extract_section_number_with_regex(
    text: str,
    chapter_number: str,
    designator_patterns: list = None,
) -> Optional[str]:
    """
    Extract a section number from text using pure regex — NO LLM call.

    Strategy:
      1. Try exact substring match against known designator_patterns
         (sorted longest-first so '17:32' wins over '17:3').
      2. Fall back to a general regex if no patterns provided.
    """
    if not text or not text.strip():
        return None

    if designator_patterns:
        # Longest first prevents '17:3' matching inside '17:32'
        for pattern in sorted(designator_patterns, key=len, reverse=True):
            if pattern in text:
                return pattern

    # Fallback: general regex
    escaped_chapter = re.escape(str(chapter_number))
    m = re.search(
        rf'§?§?\s*({escaped_chapter}[:\.][\d]+(?:[:\.][\d]+)*)',
        text, re.IGNORECASE
    )
    return m.group(1) if m else None


# ── STEP 2 (header check): Confirm match is a header, not a cross-reference ──

def is_section_header(text: str, matched_section: str) -> bool:
    """
    Returns True only if matched_section appears at the START of the text
    as a genuine section header, not buried inside a sentence (cross-reference).

    A cross-reference has meaningful text BEFORE the section number:
      ✗ "As discussed in §§17:23 to 17:24, the Federal Court..."
      ✗ "See § 17:5 for more details"
      ✗ "under ss. 17:23, the court held..."

    A header has the section number at the START (with optional §/§§/[ prefix):
      ✓ "17:6. General Principles..."
      ✓ "§ 17:6. Scope of Patent..."
      ✓ "§§ 17:6 - General Principles..."
      ✓ "[17:6] General Principles..."
      ✓ "§ 1:33Employee Privacy"      ← no delimiter between number and title
      ✓ "<bold>§ 1:33Employee Privacy</bold>"  ← wrapped in XML tags
    """
    if not text or not matched_section:
        return False

    stripped = text.strip()
    escaped  = re.escape(matched_section)

    # Strip leading XML tags (e.g. <bold>, <para.text ...>) before checking
    stripped = re.sub(r'^(<[^>]+>)+', '', stripped).strip()

    # Allow optional §/§§, whitespace, and opening bracket before section number.
    # NO delimiter required after — section number at start of text is sufficient.
    pattern = re.compile(
        rf'^(\[|§{{0,2}}\s*)?{escaped}',
        re.IGNORECASE
    )
    return bool(pattern.match(stripped))


# ── STEP 2 (scan): Use regex to collect all candidate rows ───────────────────

def find_section_candidates_with_regex(
    candidates_df,
    current_para_idx,
    chapter_number: str,
    designator_patterns: list = None,
    apply_replacement: bool = False,
    deduplicate_func=None,
    verbose: bool = False,
    df_name: str = "unknown",
    prefilter_pattern: re.Pattern = None,
):
    """
    Scan all rows with idx < current_para_idx using the pre-built regex.
    Returns a list of dicts: {idx, text, matched_section}.

    NO LLM calls here — pure regex only.
    The pre-filter regex (built once by build_prefilter_regex_with_llm) is
    used to skip rows that clearly cannot contain a section reference.
    Then extract_section_number_with_regex does precise extraction.
    Then is_section_header filters out cross-references before LLM ever sees them.
    """
    candidates = (
        candidates_df[candidates_df['idx'] < current_para_idx]
        .sort_values('idx', ascending=False)
    )

    if verbose:
        # print(f"\n    [REGEX SCAN] DataFrame: {df_name}")
        # print(f"    [REGEX SCAN] Scanning {len(candidates)} rows (0 LLM calls)...")
        pass

    if prefilter_pattern is None:
        escaped_chapter = re.escape(str(chapter_number))
        prefilter_pattern = re.compile(rf'§?§?\s*{escaped_chapter}[:\.][\d]', re.IGNORECASE)

    found_candidates = []

    for _, row in candidates.iterrows():
        text = str(row.get('text', '')) if pd.notna(row.get('text')) else ''

        if apply_replacement and deduplicate_func:
            deleted_text  = str(row.get('deleted_text', ''))  if pd.notna(row.get('deleted_text'))  else ''
            inserted_text = str(row.get('inserted_text', '')) if pd.notna(row.get('inserted_text')) else ''
            inserted_text = deduplicate_func(inserted_text)
            if deleted_text.strip() and deleted_text in text:
                text = text.replace(deleted_text, inserted_text)

        if not text.strip():
            continue

        # Cheap pre-filter: skip rows with no section pattern at all.
        # IMPORTANT: give a second chance for exact known designator substrings,
        # because LLM-generated prefilter regex can be too strict and miss
        # compact headers like "§ 1:33Employee Privacy".
        if not prefilter_pattern.search(text):
            has_direct_designator = False
            if designator_patterns:
                for pattern in sorted(designator_patterns, key=len, reverse=True):
                    if pattern and pattern in text:
                        has_direct_designator = True
                        if verbose:
                            # print(f"      [PREFILTER BYPASS] idx={row['idx']} matched direct designator '{pattern}'")
                            pass
                        break

            if not has_direct_designator:
                continue

        # Precise extraction: pure regex, no LLM
        matched_section = extract_section_number_with_regex(text, chapter_number, designator_patterns)

        if matched_section:
            # ── Header check: discard cross-references before LLM sees them ──
            if not is_section_header(text, matched_section):
                if verbose:
                    # print(f"      [SKIP] idx={row['idx']} → '{matched_section}' is a cross-reference, not a header")
                    pass
                continue

            found_candidates.append({
                'idx': row['idx'],
                'text': text[:200],
                'matched_section': matched_section,
            })
            if verbose:
                # print(f"      [REGEX MATCH] idx={row['idx']} → '{matched_section}' in '{text[:60]}...'")
                pass

    if verbose:
        # print(f"    [REGEX SCAN] Found {len(found_candidates)} candidates")
        pass

    return found_candidates


# ── STEP 3: ONE LLM call per df row — pick best section from top 10 ──────────

def pick_best_section_with_llm(
    candidates: list,
    current_para_idx: float,
    chapter_number: str,
    client,
    model_name: str,
    designator_patterns: list = None,
    verbose: bool = False,
) -> tuple:
    """
    Always 1 LLM call per unique para_idx.
    Uses assistant turn prefill to force direct section number output,
    preventing reasoning preamble from being cut off by max_tokens.

    By this point all candidates are guaranteed to be genuine section headers
    (cross-references already removed by is_section_header in the regex scan),
    so the LLM only needs to pick the closest one by idx.
    """
    if not candidates:
        return None, None

    candidates_sorted = sorted(candidates, key=lambda x: x['idx'], reverse=True)
    top_10 = candidates_sorted[:10]

    candidates_text = "\n".join([
        f"  [{i+1}] idx={c['idx']}: '{c['matched_section']}' | \"{c['text'][:150]}\""
        for i, c in enumerate(top_10)
    ])

    patterns_context = ""
    if designator_patterns:
        patterns_context = f"""
KNOWN VALID SECTION NUMBERS (complete list):
{designator_patterns}

Only return a section number from this list.
"""

    prompt = f"""You are a legal document parser. A paragraph at index {current_para_idx}
needs to be assigned to its section for chapter {chapter_number}.

All candidates below are confirmed section headers (not cross-references).
They are sorted by proximity (closest to {current_para_idx} first).
Return the section number of the CLOSEST one (highest idx):

{candidates_text}
{patterns_context}
The section number is:"""

    # ── Prefill the assistant turn so LLM outputs the answer directly ─────────
    # This prevents reasoning preamble like "Looking at the candidates..."
    result = _call_llm_with_retry(
        client, model_name,
        messages=[
            {"role": "user",      "content": prompt},
            {"role": "assistant", "content": ""},   # ← prefill: forces direct output
        ],
        max_tokens=20,
    )

    if verbose:
        print(f"    [LLM PICK] LLM selected: '{result}'")

    if result is None or result.strip().upper() == "NONE" or not result.strip():
        return None, None

    # Take only first line in case of any extra output
    result = result.strip().split('\n')[0].strip()

    # Match back to candidate to get idx
    for c in top_10:
        if result in c['matched_section'] or c['matched_section'] in result:
            return c['idx'], result

    return top_10[0]['idx'], result


# ── Cache: para_idx → section result ─────────────────────────────────────────
_para_idx_section_cache: dict = {}


def fill_sections_backward_verbose(
    inlinedels_df,
    no_changes_df,
    inline_replacement_df,
    inline_insertion_df,
    section_start_number,
    verbose=False,
    client=None,
    model_name=None,
    designator_patterns=None,
):
    """
    Fill sections backward for each row in inlinedels_df.

    LLM call budget:
      - 1 call at startup  → build_prefilter_regex_with_llm
      - 1 call per UNIQUE para_idx → pick_best_section_with_llm
      - 0 calls for duplicate para_idx rows → served from cache
      - 0 calls per candidate row → pure regex scan only
      - 0 calls for cross-references → filtered by is_section_header before LLM

    Example: 6 rows but only 2 unique para_idx values → 1 + 2 = 3 total LLM calls.
    """
    global _para_idx_section_cache
    _para_idx_section_cache = {}  # reset cache for each fresh call

    df = inlinedels_df.copy().reset_index(drop=True)

    if client is None:
        client, model_name = _get_client()

    no_changes_df         = no_changes_df[no_changes_df['is_footnote'] == False].copy()
    inline_replacement_df = inline_replacement_df[inline_replacement_df['is_footnote'] == False].copy()
    inline_insertion_df   = inline_insertion_df[inline_insertion_df['is_footnote'] == False].copy()

    chapter = str(section_start_number)

    if 'section' not in df.columns:
        df['section'] = None
    df['section'] = None

    # ── 1 LLM call: build the pre-filter regex once for all rows ─────────────
    if verbose:
        print("=" * 80)
        print(f"  [STARTUP] Calling LLM once to build pre-filter regex...")
    prefilter_pattern = build_prefilter_regex_with_llm(
        chapter, designator_patterns, client, model_name, verbose=verbose
    )

    unique_para_idxs = df['paragraph_index'].dropna().nunique()

    if verbose:
        print("=" * 80)
        print("SECTION FILLING INFO:")
        print(f"  Columns : {df.columns.tolist()}")
        print(f"  Shape   : {df.shape}")
        print(f"  Chapter : {chapter}")
        print(f"  Model   : {model_name}")
        print(f"  Designator patterns: {len(designator_patterns) if designator_patterns else 0} total")
        if designator_patterns:
            print(f"  All patterns: {designator_patterns}")
        print(f"  Pre-filter regex: {prefilter_pattern.pattern}")
        print(f"  no_changes_df        : {len(no_changes_df)} rows")
        print(f"  inline_replacement_df: {len(inline_replacement_df)} rows")
        print(f"  inline_insertion_df  : {len(inline_insertion_df)} rows")
        print(f"  Total df rows        : {len(df)}")
        print(f"  Unique para_idx vals : {unique_para_idxs}")
        print(f"  LLM call budget      : 1 (startup) + {unique_para_idxs} (unique para_idx) = {1 + unique_para_idxs} max")
        print("=" * 80)

    def deduplicate_inserted_text(inserted_text: str) -> str:
        if not inserted_text.strip():
            return inserted_text
        parts = inserted_text.strip().split()
        if len(parts) >= 2 and len(parts) % 2 == 0:
            mid = len(parts) // 2
            if ' '.join(parts[:mid]) == ' '.join(parts[mid:]):
                return ' '.join(parts[:mid])
        return inserted_text

    for i in range(len(df)):
        current_section = df.loc[i, 'section']
        if not (current_section is None or pd.isna(current_section)):
            continue  # already filled

        current_para_idx = df.loc[i, 'paragraph_index']

        if pd.isna(current_para_idx):
            if verbose:
                print(f"Row {i}: Skipping — paragraph_index is NaN")
            continue

        # ── Cache check: same para_idx already resolved? ─────────────────────
        if current_para_idx in _para_idx_section_cache:
            cached_section = _para_idx_section_cache[current_para_idx]
            df.loc[i, 'section'] = cached_section
            if verbose:
                print(f"\nRow {i}: para_idx={current_para_idx}, idx={df.loc[i, 'idx']}"
                      f" → [CACHE HIT] '{cached_section}' (0 LLM calls)")
            continue
        # ─────────────────────────────────────────────────────────────────────

        if verbose:
            print(f"\n{'='*60}")
            print(f"Row {i}: para_idx={current_para_idx}, idx={df.loc[i, 'idx']}")
            print(f"  Step 1: REGEX SCAN across all source dataframes (0 LLM calls)...")

        # ── Step 1: pure regex scan — collect ALL candidates from all 3 sources
        all_candidates = []

        for source_df, df_name, apply_repl in [
            (no_changes_df,         "no_changes_df",         False),
            (inline_replacement_df, "inline_replacement_df", True),
            (inline_insertion_df,   "inline_insertion_df",   False),
        ]:
            found = find_section_candidates_with_regex(
                source_df,
                current_para_idx,
                chapter,
                designator_patterns=designator_patterns,
                apply_replacement=apply_repl,
                deduplicate_func=deduplicate_inserted_text if apply_repl else None,
                verbose=verbose,
                df_name=df_name,
                prefilter_pattern=prefilter_pattern,
            )
            all_candidates.extend(found)

        if verbose:
            print(f"\n  Step 2: LLM PICK — sending top 10 of {len(all_candidates)} candidates (1 LLM call)...")

        # ── Step 2: 1 LLM call — pick best section from top 10 candidates ────
        if all_candidates:
            best_idx, best_section = pick_best_section_with_llm(
                all_candidates,
                current_para_idx,
                chapter,
                client,
                model_name,
                designator_patterns=designator_patterns,
                verbose=verbose,
            )
            if best_section:
                df.loc[i, 'section'] = best_section
                _para_idx_section_cache[current_para_idx] = best_section  # ← cache
                if verbose:
                    print(f"  → Assigned section (idx={best_idx}): {best_section}")
            else:
                current_idx = df.loc[i, 'idx']
                result = str(chapter) if current_idx < 10 else "couldn't identify section"
                df.loc[i, 'section'] = result
                _para_idx_section_cache[current_para_idx] = result
                if current_idx < 10:
                    print(f"  [FALLBACK] Row {i}, idx={current_idx} < 10 → using chapter '{chapter}'")
                else:
                    print(f"  [FAIL] Row {i}, idx={df.loc[i, 'idx']}, para_idx={current_para_idx}")
                    print(f"    ✗ LLM returned NONE — no valid header found among {len(all_candidates)} candidates")
                    for c in all_candidates[:5]:
                        print(f"      - idx={c['idx']}: '{c['matched_section']}' | \"{c['text'][:80]}\"")
                    if len(all_candidates) > 5:
                        print(f"      ... and {len(all_candidates) - 5} more")
        else:
            current_idx = df.loc[i, 'idx']
            result = str(chapter) if current_idx < 10 else "couldn't identify section"
            df.loc[i, 'section'] = result
            _para_idx_section_cache[current_para_idx] = result
            if current_idx < 10:
                print(f"  [FALLBACK] Row {i}, idx={current_idx} < 10 → using chapter '{chapter}'")
            else:
                print(f"  [FAIL] Row {i}, idx={df.loc[i, 'idx']}, para_idx={current_para_idx}")
                print(f"    ✗ No candidates found — no section headers matching chapter {chapter} in any previous rows")
                print(f"    Searched: no_changes_df ({len(no_changes_df)}), "
                      f"inline_replacement_df ({len(inline_replacement_df)}), "
                      f"inline_insertion_df ({len(inline_insertion_df)}) rows")

    return df