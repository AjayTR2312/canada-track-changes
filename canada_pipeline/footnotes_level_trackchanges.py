"""
Footnote-specific trace-tagging pipeline.

Independent processing loop for footnote changes — does NOT call process_with_llm.
"""
import os
import re
import time

from .xml_entity_rules import XML_ENTITY_RULES, CASE_CITATION_RULES, URL_TAGGING_RULES
from .config import MODEL_OPUS, MODEL_SONNET, MODEL_OPUS_FALLBACK

# Footnote trace-tagging / full-deletion generation runs on the stronger Opus
# model. These calls emit large, attribute-heavy XML, so the extra capability
# improves reliability of the FIND/REPLACE output.
FOOTNOTE_MODEL = MODEL_OPUS_FALLBACK  # claude-opus-4-8
from .paragraph_level_trackchanges import (
    _split_image_to_strips,
    parse_pxml_sections_from_str_new,
    parse_llm_response,
    smart_replace,
    _validate_and_fix_trace_tags,
    _check_trace_nesting,
    _merge_adjacent_trace_tags,
    strip_tags,
    collapse_changes,
    match_section_with_llm,
    _model_deprecates_temperature,
)
from .revisions_images import get_image_for_revision
from .prompt_loader import load_prompt


# ──────────────────────────────────────────────
# FOOTNOTE-SPECIFIC LLM CALL
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
        "footnote_trackchanges_replace.txt",
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

    _create_kwargs = {
        "model": model_name,
        "max_tokens": 16000,
        "messages": [
            {"role": "user",      "content": user_content},
        ],
    }
    # claude-opus-4-8 deprecates the `temperature` parameter and rejects the
    # request if it is sent. Only include it for models that still accept it.
    if not _model_deprecates_temperature(model_name):
        _create_kwargs["temperature"] = 0

    message = client.messages.create(**_create_kwargs)
    response_text = message.content[0].text.strip()
    if response_text.startswith("```"):
        response_text = response_text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return response_text

def call_llm_for_footnote_full_deletion(xml_chunk, text_before,
                                         client, changes, model_name=MODEL_OPUS,
                                         fn_superscript_before=None, fn_parent_para=None,
                                         page_image_path=None, strip_save_dir=None, idx=None):
    """
    Prompt for FULL DELETION of a footnote.
    The entire <footnote>...</footnote> must be wrapped in <trace.deleted>...<trace.deleted/>.
    """
    context_block = ""
    if fn_superscript_before:
        context_block += (
            f"\nFOOTNOTE LOCATION — text immediately BEFORE the superscript number in the parent paragraph:\n"
            f"  \"{fn_superscript_before}\"\n"
            f"  → The footnote you need to delete is the one whose <footnote.reference> appears right AFTER this text.\n"
        )
    if fn_parent_para:
        context_block += (
            f"\nPARENT PARAGRAPH (the body paragraph that contains this footnote's superscript reference):\n"
            f"  \"{fn_parent_para}\"\n"
            f"  → Use this to confirm you have found the correct <para.text> element that contains the <footnote>.\n"
        )

    # Build image cue block
    image_cue_block = ""
    if page_image_path and os.path.isfile(page_image_path):
        image_cue_block = """
6. A PAGE IMAGE showing the actual document with track changes visible.
   - Strikethrough text = DELETED content
   - The image is ground truth for what was changed.
"""

    prompt = load_prompt(
        "footnote_full_deletion.txt",
        XML_ENTITY_RULES=XML_ENTITY_RULES,
        context_block=context_block,
        image_cue_block=image_cue_block,
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

    _create_kwargs = {
        "model": model_name,
        "max_tokens": 16000,
        "messages": [
            {"role": "user",      "content": user_content},
        ],
    }
    # claude-opus-4-8 deprecates the `temperature` parameter and rejects the
    # request if it is sent. Only include it for models that still accept it.
    if not _model_deprecates_temperature(model_name):
        _create_kwargs["temperature"] = 0

    message = client.messages.create(**_create_kwargs)
    response_text = message.content[0].text.strip()
    if response_text.startswith("```"):
        response_text = response_text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return response_text


# ──────────────────────────────────────────────
# FOOTNOTE PROCESS WRAPPER (INDEPENDENT LOOP)
# ──────────────────────────────────────────────

def process_with_llm_footnotes(result_df, section_dict, pxml_content, client, failed_log_path,
                               match_threshold=0.5, model_name=MODEL_OPUS, chapter_number=None,
                               page_texts=None, image_paths=None):
    """
    Independent footnote processing loop.
    Does NOT call process_with_llm — handles everything internally.

    Separates footnotes into:
      1. Full deletions (text_before present, text_after empty)
      2. Replacements (text_before AND text_after present)
    """
    df = result_df.copy()

    # Populate dedicated footnote context columns
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

    # Ensure fn_parent_para is never empty
    df["fn_parent_para"] = df["fn_parent_para"].apply(
        lambda v: v if v.strip() else "(footnote - parent paragraph not available)"
    )

    # Fill text_after for deletions
    df["text_after"] = df["text_after"].fillna("")

    # Filter to changed rows with text_before
    changed_df = df[
        (df["has_changes"] == True) &
        (df["text_before"].notna()) &
        (df["text_before"].str.strip() != "")
    ].copy()

    if changed_df.empty:
        print("[FOOTNOTE FLOW] No footnote changes to process.")
        return pxml_content, [], []

    # Collapse changes by idx
    collapsed = (
        changed_df
        .groupby("idx", sort=False)
        .apply(collapse_changes)
        .tolist()
    )

    print(f"\n{'='*60}")
    print(f"[FOOTNOTE FLOW] Total footnote paragraphs to process: {len(collapsed)}")
    print(f"{'='*60}\n")

    patched_content = pxml_content
    failed_log = []
    processed_idxs = []

    for item in collapsed:
        idx = item["idx"]
        text_before = item["text_before"]
        text_after = item["text_after"]
        row_section = item["section"]
        changes = item["changes"]
        fn_superscript_before = item.get("fn_superscript_before", "") or ""
        fn_parent_para = item.get("fn_parent_para", "") or ""

        # Determine if full deletion
        is_full_deletion = not (text_after or "").strip()

        print(f"\n{'─'*60}")
        print(f"[FOOTNOTE] idx={idx}  |  {'FULL DELETION' if is_full_deletion else 'REPLACEMENT'}  |  changes: {len(changes)}")
        print(f"  text_before   : {strip_tags(text_before)[:100]}")
        print(f"  text_after    : {strip_tags(text_after)[:100]}")
        print(f"  parent_para   : {strip_tags(str(fn_parent_para))[:100]}")
        print(f"  text_before_ss: {strip_tags(str(fn_superscript_before))[:100]}")
        print(f"  section       : {row_section}")

        # Skip digit-only text_before (footnote reference numbers)
        if strip_tags(text_before).strip().isdigit():
            print(f"  [SKIP] idx={idx}: text_before is just a number '{strip_tags(text_before).strip()}' — skipping.")
            continue

        # ── Section matching ──
        designator = None
        section_xml = None
        if row_section:
            row_section_stripped = str(row_section).strip()
            if row_section_stripped in section_dict:
                designator = row_section_stripped
                section_xml = section_dict[designator]
                print(f"  [SECTION MATCH] Direct key match: '{designator}'")
            else:
                designator, section_xml = match_section_with_llm(
                    row_section, section_dict, client, model_name=MODEL_SONNET
                )

        if designator is None:
            print(f"  [SKIP] No matching section key for '{row_section}'.")
            failed_log.append({
                "idx": idx, "reason": f"No matching section key for '{row_section}'",
                "section": row_section, "text_before": text_before, "text_after": text_after,
                "changes": changes,
            })
            continue

        # Re-extract section from current document state
        current_section_dict = parse_pxml_sections_from_str_new(patched_content)
        xml_chunk = current_section_dict.get(designator, section_xml)

        # Resolve page image
        _page_img = None
        if page_texts and image_paths:
            try:
                _page_img = get_image_for_revision(text_before, page_texts, image_paths)
            except Exception:
                pass

        # ── LLM Call ──
        max_llm_retries = 2
        llm_succeeded = False

        for llm_attempt in range(1, max_llm_retries + 1):
            if llm_attempt > 1:
                print(f"  [RETRY] LLM attempt {llm_attempt}/{max_llm_retries}...")

            try:
                if is_full_deletion:
                    llm_response = call_llm_for_footnote_full_deletion(
                        xml_chunk, text_before, client, changes=changes,
                        model_name=FOOTNOTE_MODEL, fn_superscript_before=fn_superscript_before or None,
                        fn_parent_para=fn_parent_para or None, page_image_path=_page_img,
                        strip_save_dir=os.path.join(os.path.dirname(failed_log_path), "image_strips"),
                        idx=idx,
                    )
                else:
                    llm_response = call_llm_for_footnote_trace_tagging(
                        xml_chunk, text_before, text_after, client, changes=changes,
                        model_name=FOOTNOTE_MODEL, fn_superscript_before=fn_superscript_before or None,
                        fn_parent_para=fn_parent_para or None, page_image_path=_page_img,
                        strip_save_dir=os.path.join(os.path.dirname(failed_log_path), "image_strips"),
                        idx=idx,
                    )
            except Exception as e:
                print(f"  [ERROR] LLM call failed (attempt {llm_attempt}): {e}")
                if llm_attempt == max_llm_retries:
                    failed_log.append({
                        "idx": idx, "reason": f"LLM call failed: {e}",
                        "section": row_section, "text_before": text_before,
                        "text_after": text_after, "changes": changes,
                    })
                continue

            print(f"  [LLM] Response received ({len(llm_response)} chars)")

            # Check for structural leakage (code fences or self-correction)
            _leakage_reason = None
            if '```' in llm_response:
                _leakage_reason = "code fences"
            elif llm_response.count('FIND:') > 1:
                _leakage_reason = f"multiple FIND blocks ({llm_response.count('FIND:')}) — model self-corrected"
            if _leakage_reason:
                print(f"  [FAIL] LLM response has leakage: {_leakage_reason} (attempt {llm_attempt}). Retrying...")
                if llm_attempt == max_llm_retries:
                    failed_log.append({
                        "idx": idx, "reason": f"LLM leakage after {max_llm_retries} attempts: {_leakage_reason}",
                        "section": row_section, "text_before": text_before,
                        "text_after": text_after, "changes": changes,
                        "llm_response": llm_response,
                    })
                continue

            # Parse FIND/REPLACE
            find_str, replace_str = parse_llm_response(llm_response)

            if not find_str or not replace_str:
                print(f"  [FAIL] Could not parse FIND/REPLACE (attempt {llm_attempt}).")
                if llm_attempt == max_llm_retries:
                    failed_log.append({
                        "idx": idx, "reason": "Could not parse FIND/REPLACE",
                        "section": row_section, "text_before": text_before,
                        "text_after": text_after, "changes": changes,
                        "llm_response": llm_response,
                    })
                continue

            # Sanity: FIND must start with XML tag
            if not find_str.strip().startswith('<'):
                print(f"  [FAIL] FIND does not start with XML tag (attempt {llm_attempt}).")
                if llm_attempt == max_llm_retries:
                    failed_log.append({
                        "idx": idx, "reason": "FIND not XML",
                        "section": row_section, "text_before": text_before,
                        "text_after": text_after, "changes": changes,
                    })
                continue

            print(f"  [DEBUG] FIND ({len(find_str)} chars): {find_str[:150]}")
            print(f"  [DEBUG] REPLACE ({len(replace_str)} chars): {replace_str[:150]}")

            # Validate trace tags
            find_str, replace_str, tags_valid, failed_changes = _validate_and_fix_trace_tags(
                find_str, replace_str, idx,
                client=client, model_name=model_name, changes=changes,
            )

            if not tags_valid:
                print(f"  [FAIL] Trace tag validation failed (attempt {llm_attempt}).")
                if llm_attempt == max_llm_retries:
                    failed_log.append({
                        "idx": idx, "reason": "Trace tag validation failed",
                        "section": row_section, "text_before": text_before,
                        "text_after": text_after, "changes": changes,
                    })
                continue

            if find_str.strip() == replace_str.strip():
                print(f"  [SKIP] FIND == REPLACE — already applied.")
                llm_succeeded = True
                break

            llm_succeeded = True
            break

        if not llm_succeeded:
            continue

        if find_str.strip() == replace_str.strip():
            continue

        # ── Smart replace ──
        _ctx = {
            "section": row_section, "designator": designator,
            "text_before": text_before, "text_after": text_after,
            "changes": changes,
        }
        modified_section = smart_replace(
            xml_chunk, find_str, replace_str, idx, failed_log, extra_context=_ctx,
        )

        if modified_section != xml_chunk:
            pos = patched_content.find(xml_chunk)
            if pos != -1:
                patched_content = (
                    patched_content[:pos] + modified_section + patched_content[pos + len(xml_chunk):]
                )
                print(f"  [SPLICE] Modified section '{designator}' spliced back.")
            else:
                patched_content = smart_replace(
                    patched_content, find_str, replace_str, idx, failed_log, extra_context=_ctx,
                )

        processed_idxs.append(idx)

    # ── Write failed log ──
    if failed_log:
        print(f"\n[FOOTNOTE FAILED] {len(failed_log)} items failed.")
        excel_rows = []
        for item in failed_log:
            changes_list = item.get('changes', [])
            if changes_list:
                for ch in changes_list:
                    excel_rows.append({
                        "Row Index": item.get('idx'),
                        "Section": item.get('section', ''),
                        "Designator": item.get('designator', ''),
                        "Reason": f"FOOTNOTE REVISION FAILED - {item.get('reason', '')}",
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
                    "Reason": f"FOOTNOTE REVISION FAILED - {item.get('reason', '')}",
                    "Docx_Paragraph": item.get('text_before', ''),
                })
    else:
        print(f"\n[FOOTNOTE FLOW] All {len(collapsed)} items processed successfully.")
        excel_rows = []

    return patched_content, processed_idxs, excel_rows
