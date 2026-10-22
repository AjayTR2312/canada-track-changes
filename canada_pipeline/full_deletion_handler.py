"""
full_deletion_handler.py
────────────────────────
Dedicated pipeline step for FULL paragraph deletions (text_after == "").

Sends the full section XML to LLM with a focused prompt for wrapping
the target element in <trace.deleted>...</trace.deleted>.
"""

import os
import re
import json
import time
from datetime import datetime

from .config import MODEL_OPUS, build_anthropic_client, chapter_pattern
from .paragraph_level_trackchanges import (
    parse_pxml_sections_from_str_new,
    parse_llm_response,
    smart_replace,
    strip_tags,
    match_section_with_llm,
    collapse_changes,
)
from .prompt_loader import load_prompt

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
_LOG_PATH = None


def _init_log(output_dir: str):
    """Initialize the log file for this run."""
    global _LOG_PATH
    _LOG_PATH = os.path.join(output_dir, "full_deletion_log.txt")
    with open(_LOG_PATH, "w", encoding="utf-8") as f:
        f.write(f"FULL DELETION PROCESSING LOG\n")
        f.write(f"Started: {datetime.now().isoformat()}\n")
        f.write("=" * 70 + "\n\n")


def _log(msg: str):
    """Append a message to the log."""
    if _LOG_PATH:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(msg + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# JSON response extraction
# ─────────────────────────────────────────────────────────────────────────────

def _safe_parse(llm_response: str):
    """Try multiple strategies to extract find/replace from LLM response."""
    # Strategy 1: direct parse
    f, r = parse_llm_response(llm_response)
    if f and r:
        return f, r

    # Strategy 2: strip code fences
    cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', '', llm_response.strip(), flags=re.MULTILINE)
    cleaned = cleaned.strip()
    f, r = parse_llm_response(cleaned)
    if f and r:
        return f, r

    # Strategy 3: extract JSON object with brace matching
    match = re.search(r'\{.*\}', llm_response, re.DOTALL)
    if match:
        f, r = parse_llm_response(match.group(0))
        if f and r:
            return f, r

    return None, None


# ─────────────────────────────────────────────────────────────────────────────
# Structure validation
# ─────────────────────────────────────────────────────────────────────────────

def _validate_xml_structure(xml_text: str) -> tuple:
    """
    Validate that all opening tags have matching closing tags and vice versa.
    
    Returns:
        (is_valid: bool, error_message: str or None)
    """
    # Self-closing tags don't need matching
    # Find all opening and closing tags (skip self-closing, processing instructions, comments)
    tag_pattern = re.compile(r'<(/?)([a-zA-Z][a-zA-Z0-9._-]*)\b[^>]*(/?)\s*>')
    
    stack = []
    for match in tag_pattern.finditer(xml_text):
        is_closing = match.group(1) == '/'
        tag_name = match.group(2)
        is_self_closing = match.group(3) == '/'
        
        if is_self_closing:
            continue
        
        if not is_closing:
            stack.append((tag_name, match.start()))
        else:
            if not stack:
                return False, f"Closing tag </{tag_name}> at pos {match.start()} has no matching opening tag"
            top_name, top_pos = stack[-1]
            if top_name == tag_name:
                stack.pop()
            else:
                # Mismatch — check if it's a known tag that might be deeper in the stack
                # (could be a missing close tag for the top element)
                found_in_stack = False
                for i in range(len(stack) - 1, -1, -1):
                    if stack[i][0] == tag_name:
                        # Found it deeper — report the unclosed tags between
                        unclosed = [s[0] for s in stack[i+1:]]
                        return False, (
                            f"Tag mismatch: expected </{top_name}> but found </{tag_name}> at pos {match.start()}. "
                            f"Unclosed tags: {unclosed}"
                        )
                if not found_in_stack:
                    return False, (
                        f"Tag mismatch: expected </{top_name}> (opened at pos {top_pos}) "
                        f"but found </{tag_name}> at pos {match.start()}"
                    )
    
    if stack:
        unclosed = [(name, pos) for name, pos in stack[-5:]]  # Show last 5
        return False, f"Unclosed tags at end: {unclosed}"
    
    return True, None


# ─────────────────────────────────────────────────────────────────────────────
# LLM call for full deletion
# ─────────────────────────────────────────────────────────────────────────────

def _call_llm_for_full_deletion(xml_chunk: str, text_before: str, previous_para: str,
                                 post_para: str, client, model_name: str, idx: int,
                                 attempt: int = 1, retry_reason: str = "") -> str:
    """
    Call LLM to wrap the entire target element in <trace.deleted>...</trace.deleted>.
    
    For full deletions, text_after is empty — the entire paragraph is deleted.
    Uses system + user prompt separation for better instruction following.
    """

    text_before_len = len(strip_tags(text_before))

    text_before_len_x2 = text_before_len * 2
    system_prompt = load_prompt(
        "full_deletion_system.txt",
        text_before_len=text_before_len,
        text_before_len_x2=text_before_len_x2,
    )

    # On retry, add correction prefix
    correction_prefix = ""
    if attempt > 1:
        if retry_reason:
            correction_prefix = f"""⚠️ PREVIOUS ATTEMPT FAILED: {retry_reason}
You MUST fix this. Return ONLY a raw JSON object with a SMALLER "find" element.
Pick the SINGLE <list.item> or <para.text> that matches text_before — NOT the whole <list> or multiple items.
Start your response with {{ and end with }}. Nothing else.

"""
        else:
            correction_prefix = """⚠️ PREVIOUS ATTEMPT FAILED: Your last response was not valid JSON.
You MUST return ONLY a raw JSON object. No markdown. No code fences. No explanation.
Start your response with { and end with }. Nothing else.

"""

    previous_para_or_na = previous_para or 'N/A'
    post_para_or_na = post_para or 'N/A'
    text_before_len_x2 = text_before_len * 2
    short_deletion_alert = "" if text_before_len >= 5 else load_prompt("full_deletion_short_alert.txt")
    user_prompt = load_prompt(
        "full_deletion_user.txt",
        correction_prefix=correction_prefix,
        text_before=text_before,
        text_before_len=text_before_len,
        xml_chunk=xml_chunk,
        previous_para_or_na=previous_para_or_na,
        post_para_or_na=post_para_or_na,
        text_before_len_x2=text_before_len_x2,
        short_deletion_alert=short_deletion_alert,
    )

    from .paragraph_level_trackchanges import call_anthropic
    # call_anthropic doesn't support system param, so we call the API directly
    import time as _time
    retry_delay = 5
    max_retries = 5
    for _attempt in range(max_retries):
        try:
            message = client.messages.create(
                model=model_name,
                max_tokens=16000,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return message.content[0].text.strip()
        except Exception as e:
            error_str = str(e)
            if "rate_limit" in error_str.lower() or "429" in error_str:
                if _attempt < max_retries - 1:
                    print(f"  [RATE LIMIT] Full deletion idx={idx}, attempt {_attempt+1}/{max_retries} - Waiting {retry_delay}s...")
                    _time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 60)
                else:
                    raise
            else:
                raise
    raise Exception("Max retries exceeded")


# ─────────────────────────────────────────────────────────────────────────────
# Main orchestrator: process_full_deletions
# ─────────────────────────────────────────────────────────────────────────────

def process_full_deletions(result_df, section_dict, pxml_content, client, failed_log_path,
                           model_name=MODEL_OPUS, chapter_number=None):
    """
    Process all full paragraph deletions (text_after == "").
    
    This is a dedicated pipeline step that handles rows where the entire
    paragraph content is deleted (needs trace.deleted wrapping).
    
    Returns:
        (patched_content, processed_idxs, failed_rows_for_excel)
    """
    output_dir = os.path.dirname(failed_log_path)
    _init_log(output_dir)

    result_df = result_df.copy()
    result_df["text_after"] = result_df["text_after"].fillna("")

    # Filter: only full deletions (text_after is empty, text_before is not)
    # Also include inline_deletion type even if text_before is empty (e.g. hellip)
    full_del_df = result_df[
        (result_df["has_changes"] == True) &
        (
            (result_df["text_before"].notna() & (result_df["text_before"] != "")) |
            (result_df["type"] == "inline_deletion")
        ) &
        (result_df["text_after"].str.strip() == "")
    ].copy()

    if full_del_df.empty:
        print("[FULL DELETIONS] No full paragraph deletions found.")
        return pxml_content, [], []

    # Collapse changes per paragraph
    collapsed = (
        full_del_df
        .groupby("idx", sort=False)
        .apply(collapse_changes)
        .tolist()
    )

    print(f"\n{'='*60}")
    print(f"[FULL DELETIONS] Total paragraphs to process: {len(collapsed)}")
    print(f"{'='*60}\n")

    patched_content = pxml_content
    failed_log = []
    processed_idxs = []

    for item in collapsed:
        idx = item["idx"]
        text_before = item["text_before"]
        text_after = item["text_after"]
        previous_para = item["previous_para"]
        post_para = item["post_para"]
        row_section = item["section"]
        changes = item["changes"]

        # Fallback: if text_before is empty, use deleted_text from changes (inline_deletion rows)
        if not text_before or not str(text_before).strip():
            for ch in changes:
                if ch.get("deleted_text") and str(ch["deleted_text"]).strip():
                    text_before = str(ch["deleted_text"]).strip()
                    break

        # Skip if still no text to search for
        if not text_before or not str(text_before).strip():
            print(f"\n{'─'*60}")
            print(f"[FULL DEL] idx={idx} — SKIP: no text_before or deleted_text available")
            failed_log.append({
                "idx": idx, "reason": "No text_before or deleted_text available",
                "section": row_section, "text_before": text_before, "text_after": text_after,
                "changes": changes,
            })
            continue

        print(f"\n{'─'*60}")
        print(f"[FULL DEL] idx={idx}")
        print(f"  text_before   : {strip_tags(text_before)[:100]}")
        print(f"  previous_para : {strip_tags(str(previous_para))[:80] if previous_para else 'None'}")
        print(f"  post_para     : {strip_tags(str(post_para))[:80] if post_para else 'None'}")
        print(f"  section       : {row_section}")

        # ── Section matching ──
        designator = None
        section_xml = None

        # Override: if text_before IS a section heading
        _heading_override = None
        if chapter_number and text_before:
            _ch = chapter_pattern(chapter_number)
            _hm = re.match(rf'^§?\s*({_ch}[:.][\dA-Za-z]+(?:\.[\dA-Za-z]+)*)\.?\s', text_before)
            if _hm:
                _candidate = _hm.group(1)
                if _candidate in section_dict:
                    _heading_override = _candidate

        if _heading_override:
            designator = _heading_override
            section_xml = section_dict[designator]
        elif row_section:
            row_section_stripped = str(row_section).strip()
            if row_section_stripped in section_dict:
                designator = row_section_stripped
                section_xml = section_dict[designator]
            else:
                designator, section_xml = match_section_with_llm(
                    row_section, section_dict, client, model_name=model_name
                )

        if designator is None:
            print(f"  [SKIP] No matching section for '{row_section}'")
            failed_log.append({
                "idx": idx, "reason": f"No matching section key for '{row_section}'",
                "section": row_section, "text_before": text_before, "text_after": text_after,
                "previous_para": previous_para, "post_para": post_para, "changes": changes,
            })
            continue

        # Re-extract section from current state
        current_section_dict = parse_pxml_sections_from_str_new(patched_content)
        current_section_xml = current_section_dict.get(designator, section_xml)
        print(f"  Section '{designator}': {len(current_section_xml)} chars")

        xml_chunk = current_section_xml
        _log(f"  [SECTION] idx={idx}, designator={designator}, section_size={len(xml_chunk)}")

        # ── LLM call ──
        max_retries = 3
        llm_succeeded = False
        find_str = None
        replace_str = None
        retry_reason = ""

        for attempt in range(1, max_retries + 1):
            if attempt > 1:
                print(f"  [RETRY] Attempt {attempt}/{max_retries} (reason: {retry_reason})")

            try:
                llm_response = _call_llm_for_full_deletion(
                    xml_chunk, text_before, previous_para, post_para,
                    client, model_name, idx, attempt=attempt,
                    retry_reason=retry_reason
                )
            except Exception as e:
                print(f"  [ERROR] LLM call failed: {e}")
                if attempt == max_retries:
                    failed_log.append({
                        "idx": idx, "reason": f"LLM call failed: {e}",
                        "section": row_section, "designator": designator,
                        "text_before": text_before, "text_after": text_after,
                        "previous_para": previous_para, "post_para": post_para, "changes": changes,
                    })
                continue

            print(f"  [LLM] Response: {len(llm_response)} chars")
            _log(f"  LLM response ({len(llm_response)} chars): {llm_response[:500]}")

            # Parse JSON response (with fallback strategies)
            find_str, replace_str = _safe_parse(llm_response)

            if not find_str or not replace_str:
                print(f"  [FAIL] Could not parse FIND/REPLACE (attempt {attempt})")
                print(f"  [RAW RESPONSE]:\n{llm_response[:1000]}")
                _log(f"  [RAW RESPONSE FULL]:\n{llm_response}")
                retry_reason = "Could not parse JSON. Return ONLY a valid JSON object with find and replace keys."
                if attempt == max_retries:
                    failed_log.append({
                        "idx": idx, "reason": "Could not parse FIND/REPLACE",
                        "section": row_section, "designator": designator,
                        "text_before": text_before, "text_after": text_after,
                        "previous_para": previous_para, "post_para": post_para, "changes": changes,
                        "llm_response": llm_response,
                    })
                continue

            # Sanity: FIND must start with XML tag
            if not find_str.strip().startswith('<'):
                print(f"  [FAIL] FIND doesn't start with XML tag (attempt {attempt})")
                retry_reason = "FIND must start with an XML tag like <para.text> or <list.item>."
                if attempt == max_retries:
                    failed_log.append({
                        "idx": idx, "reason": "FIND doesn't start with XML tag",
                        "section": row_section, "designator": designator,
                        "text_before": text_before, "text_after": text_after,
                        "changes": changes, "llm_response": llm_response,
                    })
                continue

            # Sanity: REPLACE must contain trace.deleted
            if '<trace.deleted>' not in replace_str:
                print(f"  [FAIL] REPLACE missing trace.deleted (attempt {attempt})")
                retry_reason = "REPLACE must contain <trace.deleted>...<trace.deleted/> wrapping the deleted content."
                if attempt == max_retries:
                    failed_log.append({
                        "idx": idx, "reason": "REPLACE missing trace.deleted tag",
                        "section": row_section, "designator": designator,
                        "text_before": text_before, "text_after": text_after,
                        "changes": changes, "llm_response": llm_response,
                    })
                continue

            # ── Size validation: reject if find is way larger than text_before ──
            find_text_len = len(strip_tags(find_str))
            target_len = len(strip_tags(text_before))
            if find_text_len > target_len * 3:
                print(f"  [FAIL] find_str too large: {find_text_len} chars vs target {target_len} chars (attempt {attempt})")
                _log(f"  [SIZE FAIL] idx={idx}: find={find_text_len} chars, target={target_len} chars")
                retry_reason = f"TOO_LARGE: your find had {find_text_len} chars of text but target is only {target_len} chars. Pick the SINGLE list.item or para element, NOT the entire list or multiple items."
                if attempt == max_retries:
                    failed_log.append({
                        "idx": idx, "reason": f"find_str too large ({find_text_len} chars, expected ~{target_len})",
                        "section": row_section, "designator": designator,
                        "text_before": text_before, "text_after": text_after,
                        "changes": changes, "find_str": find_str, "replace_str": replace_str,
                    })
                continue

            # ── Log full FIND and REPLACE ──
            print(f"\n  [DEBUG] FULL FIND ({len(find_str)} chars):")
            print(f"  {find_str}")
            print(f"\n  [DEBUG] FULL REPLACE ({len(replace_str)} chars):")
            print(f"  {replace_str}\n")
            _log(f"  [FIND] ({len(find_str)} chars):\n{find_str}")
            _log(f"  [REPLACE] ({len(replace_str)} chars):\n{replace_str}")

            llm_succeeded = True
            break

        if not llm_succeeded:
            continue

        # ── Apply replacement in section ──
        print(f"  [REPLACE] Applying in section '{designator}'...")
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

        # ── Structure validation: check tag balance before splicing ──
        if modified_section != current_section_xml:
            structure_ok, structure_err = _validate_xml_structure(modified_section)
            if not structure_ok:
                print(f"  [STRUCT FAIL] idx={idx}: {structure_err}")
                _log(f"  [STRUCT FAIL] idx={idx}: {structure_err}")
                # Also check if the original section was already broken
                orig_ok, _ = _validate_xml_structure(current_section_xml)
                if orig_ok:
                    # Original was fine, our replacement broke it — reject
                    print(f"  [STRUCT FAIL] Original section was valid. Rejecting replacement.")
                    failed_log.append({
                        "idx": idx, "reason": f"Replacement broke XML structure: {structure_err}",
                        "section": row_section, "designator": designator,
                        "text_before": text_before, "text_after": text_after,
                        "changes": changes,
                    })
                    continue
                else:
                    print(f"  [STRUCT WARN] Original section already had structure issues. Proceeding anyway.")

        # Splice back
        if modified_section != current_section_xml:
            pos = patched_content.find(current_section_xml)
            if pos != -1:
                patched_content = (
                    patched_content[:pos] +
                    modified_section +
                    patched_content[pos + len(current_section_xml):]
                )
                print(f"  [OK] Section '{designator}' patched successfully.")
                processed_idxs.append(idx)
                _log(f"  [OK] idx={idx} patched successfully.")
            else:
                print(f"  [WARN] Could not splice section back. Trying full-content replace...")
                patched_content = smart_replace(
                    patched_content, find_str, replace_str, idx, failed_log,
                    extra_context=_ctx,
                )
                processed_idxs.append(idx)
        else:
            print(f"  [WARN] Section unchanged after smart_replace — replacement may have failed.")
            _log(f"  [WARN] idx={idx} section unchanged after smart_replace")

    # ── Write failed log ──
    if failed_log:
        print(f"\n[FULL DELETIONS] {len(failed_log)} failures. Writing log...")
        sep = "=" * 70
        fd_log_path = os.path.join(output_dir, "failed_full_deletions.txt")
        with open(fd_log_path, "w", encoding="utf-8") as f:
            f.write(f"FAILED FULL DELETIONS LOG\n{sep}\n\n")
            for item in failed_log:
                f.write(f"{sep}\n")
                f.write(f"  idx         : {item.get('idx')}\n")
                f.write(f"  reason      : {item.get('reason', 'N/A')}\n")
                f.write(f"  section     : {item.get('section', 'N/A')}\n")
                f.write(f"  designator  : {item.get('designator', 'N/A')}\n")
                f.write(f"  text_before : {str(item.get('text_before', ''))[:500]}\n")
                f.write(f"{sep}\n\n")

    # Build excel rows for consolidated report
    excel_rows = []
    for item in failed_log:
        changes_list = item.get('changes', [])
        if changes_list:
            for ch in changes_list:
                excel_rows.append({
                    "Row Index": item.get('idx'),
                    "Section": item.get('section', ''),
                    "Designator": item.get('designator', ''),
                    "Reason": f"REVISION FAILED - Full deletion: {item.get('reason', '')}",
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
                "Reason": f"REVISION FAILED - Full deletion: {item.get('reason', '')}",
                "Change Type": '', "Inserted Text": '', "Deleted Text": '',
                "Previous Context": '', "Post Context": '',
                "Docx_Paragraph": item.get('text_before', ''),
            })

    print(f"\n[FULL DELETIONS] Done. Processed: {len(processed_idxs)}, Failed: {len(failed_log)}")
    _log(f"\n{'='*70}")
    _log(f"SUMMARY: Processed={len(processed_idxs)}, Failed={len(failed_log)}")

    return patched_content, processed_idxs, excel_rows
