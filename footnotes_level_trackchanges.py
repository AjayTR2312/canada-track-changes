"""
Footnote-specific trace-tagging pipeline.

Independent processing loop for footnote changes — does NOT call process_with_llm.
"""
import os
import re
import time

from xml_entity_rules import XML_ENTITY_RULES, CASE_CITATION_RULES
from config import MODEL_OPUS, MODEL_SONNET
from paragraph_level_trackchanges import (
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
)
from revisions_images import get_image_for_revision


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

    prompt = f"""You are an XML editor working on FOOTNOTE changes in a legal document.

You will be given:
1. An XML chunk from the document (contains the footnote to edit)
2. text_before: the original footnote text (plain text, no XML tags)
3. text_after:  the revised footnote text (plain text, no XML tags)
4. FOOTNOTE LOCATION context:
   - text_before_superscript: text right before the superscript number in the parent paragraph
   - parent_paragraph: the full body paragraph containing the footnote reference
   Use these to identify WHICH footnote in the XML chunk needs editing.
5. An EXPLICIT CHANGE LIST: each entry tells you exactly WHAT changed and WHERE
{image_cue_block}
Your job has TWO parts:

PART 1 — FIND:
- Locate the FOOTNOTE element whose <footnote.body> stripped text matches text_before
- Use the FOOTNOTE LOCATION context to identify the correct footnote (match the
  parent paragraph text and the text before the superscript)
- Copy the footnote's containing paragraph element EXACTLY from the XML CHUNK — this is FIND
- FIND must contain the full <para.text>...</para.text> that contains the target footnote
- ⚠ ENTITY COPYING RULE: Copy entities EXACTLY as they appear in the XML CHUNK.
  Do NOT decode entities into literal characters.

PART 2 — REPLACE:
- Use the EXPLICIT CHANGE LIST as your ground truth
- For EVERY change entry wrap the changed text in trace tags:
    * inline_deletion:    <trace.deleted>deleted_text<trace.deleted/>
    * inline_insertion:   <trace>inserted_text</trace>
    * inline_replacement: <trace.deleted>deleted_text<trace.deleted/><trace>inserted_text</trace>
- Apply changes INSIDE the <footnote.body> text content
- Words NOT mentioned in any change entry → leave completely untouched
- Preserve ALL existing child XML tags exactly as they are

CRITICAL RULES:
1. FIND must be an EXACT verbatim copy of a SINGLE element from the XML CHUNK.
2. FIND must match text_before — NOT text_after.
3. EVERY change MUST have <trace> or <trace.deleted> tags.
4. The closing tag for deletions MUST be <trace.deleted/> — NEVER </trace.deleted>.
5. NO WHITESPACE BETWEEN ADJACENT XML TAGS:
   ✗ WRONG: <trace.deleted>text<trace.deleted/> <trace>new</trace>
   ✓ RIGHT: <trace.deleted>text<trace.deleted/><trace>new</trace>
6. If the entire footnote is deleted (text_after is empty), wrap the ENTIRE
   <footnote>...</footnote> from OUTSIDE in <trace.deleted>...<trace.deleted/>.
   Do NOT go inside and only wrap footnote.body — that leaves an orphan superscript.
7. TRACE.DELETED must NOT span across child elements like <footnote>.
   Wrap each text segment individually, with footnotes sitting between trace blocks.
8. NEVER change capitalisation. Copy characters exactly as they appear.
9. NEVER insert <trace> or <trace.deleted> inside XML attribute values.

{XML_ENTITY_RULES}

{CASE_CITATION_RULES}

OUTPUT FORMAT — respond with a single JSON object, nothing else:
{{
  "reasoning": "<your brief internal reasoning about which footnote to target and what changes to apply>",
  "find": "<exact element from xml chunk>",
  "replace": "<same element with trace tags inserted>"
}}

⚠ CRITICAL OUTPUT RULES:
- Output ONLY the JSON object. No text before or after it.
- The "reasoning" field is for your thinking — put ALL observations, notes, issues there.
- The "find" and "replace" fields must contain ONLY raw XML (properly escaped for JSON strings).
- Escape double quotes inside XML attributes as \\" in the JSON string values.
- Do NOT use code fences, markdown, or any formatting — just the raw JSON object.

XML CHUNK:
{xml_chunk}

text_before:
{text_before}

text_after:
{text_after}
{context_block}{changes_block}
Respond with the JSON object only:"""

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
            {"role": "assistant", "content": '{"reasoning":'},
        ],
    )
    response_text = message.content[0].text.strip()
    return '{"reasoning":' + response_text


# ──────────────────────────────────────────────
# FOOTNOTE FULL DELETION LLM CALL
# ──────────────────────────────────────────────

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

    prompt = f"""You are an XML editor performing a FULL FOOTNOTE DELETION in a legal document.

You will be given:
1. An XML chunk from the document (contains the footnote to delete)
2. text_before: the original footnote text (plain text, no XML tags) — this footnote is being FULLY DELETED
3. FOOTNOTE LOCATION context to identify WHICH footnote to delete
4. An EXPLICIT CHANGE LIST confirming the deletion
{image_cue_block}
Your job has TWO parts:

PART 1 — FIND:
- Locate the FOOTNOTE element whose <footnote.body> stripped text matches text_before
- Use the FOOTNOTE LOCATION context to identify the correct footnote
- Copy the ENTIRE <footnote>...</footnote> element EXACTLY from the XML CHUNK — this is FIND
- FIND must be ONLY the <footnote>...</footnote> element — NOT the surrounding <para.text>
- ⚠ ENTITY COPYING RULE: Copy entities EXACTLY as they appear in the XML CHUNK.

PART 2 — REPLACE:
- Wrap the ENTIRE <footnote>...</footnote> in <trace.deleted>...<trace.deleted/>
- REPLACE = <trace.deleted> + FIND + <trace.deleted/>
- Nothing else changes — just add the trace.deleted wrapper around the footnote

CRITICAL RULES:
1. FIND must be EXACTLY the <footnote>...</footnote> element — starts with <footnote and ends with </footnote>.
2. FIND must contain text_before inside its <footnote.body>.
3. The closing tag for deletions MUST be <trace.deleted/> — NEVER </trace.deleted>.
4. Do NOT include the parent <para.text> or <para> in FIND — only the <footnote> element.
5. NEVER change capitalisation. Copy characters exactly as they appear.
6. NEVER modify anything inside the footnote — just wrap it from outside.

EXAMPLE:
FIND:
<footnote date.updated="0"><footnote.reference date.updated="0">5</footnote.reference><footnote.body date.updated="0">The footnote content here.</footnote.body></footnote>

REPLACE:
<trace.deleted><footnote date.updated="0"><footnote.reference date.updated="0">5</footnote.reference><footnote.body date.updated="0">The footnote content here.</footnote.body></footnote><trace.deleted/>

{XML_ENTITY_RULES}

OUTPUT FORMAT — respond with a single JSON object, nothing else:
{{
  "reasoning": "<your brief reasoning about which footnote to target>",
  "find": "<footnote element from xml chunk>",
  "replace": "<trace.deleted><same footnote element><trace.deleted/>"
}}

⚠ CRITICAL OUTPUT RULES:
- Output ONLY the JSON object. No text before or after it.
- The "reasoning" field is for your thinking — put ALL observations, notes, issues there.
- The "find" and "replace" fields must contain ONLY raw XML (properly escaped for JSON strings).
- Escape double quotes inside XML attributes as \\" in the JSON string values.
- Do NOT use code fences, markdown, or any formatting — just the raw JSON object.

XML CHUNK:
{xml_chunk}

text_before (footnote being FULLY DELETED):
{text_before}
{context_block}
EXPLICIT CHANGE LIST:
  Change 1 — type: inline_deletion (FULL FOOTNOTE DELETION)
    deleted_text: {text_before}

Respond with the JSON object only:"""

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
            {"role": "assistant", "content": '{"reasoning":'},
        ],
    )
    response_text = message.content[0].text.strip()
    return '{"reasoning":' + response_text


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
                        model_name=model_name, fn_superscript_before=fn_superscript_before or None,
                        fn_parent_para=fn_parent_para or None, page_image_path=_page_img,
                        strip_save_dir=os.path.join(os.path.dirname(failed_log_path), "image_strips"),
                        idx=idx,
                    )
                else:
                    llm_response = call_llm_for_footnote_trace_tagging(
                        xml_chunk, text_before, text_after, client, changes=changes,
                        model_name=model_name, fn_superscript_before=fn_superscript_before or None,
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
