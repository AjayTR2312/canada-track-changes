"""
Pipeline 9 — Retry failed inline deletions from Failed_revisions.xlsx.

Reads the 'Inline Changes' sheet, filters for inline_deletion rows with
'REVISION FAILED' in reason, looks up the section XML, sends to LLM to
find the deleted text and wrap it with <trace.deleted>...<trace.deleted/>,
then applies via smart_replace.
"""

import os
import re
import pandas as pd
from .paragraph_level_trackchanges import (
    parse_pxml_sections_from_str_new,
    smart_replace,
    parse_llm_response,
    strip_tags,
    call_anthropic,
)
from .prompt_loader import load_prompt


def retry_failed_inline_deletions(
    failed_inline_changes: list,
    pxml_content: str,
    client,
    model_name: str,
    chapter_number: str = None,
    page_texts: dict = None,
    image_paths: dict = None,
):
    """
    Retry failed inline_deletion rows by sending the section XML to LLM
    and applying the trace.deleted tags via smart_replace.

    Parameters
    ----------
    failed_inline_changes : list of dicts
        The failed rows (from process_with_llm excel_rows output).
        Expected keys: 'Change Type', 'Reason', 'Deleted Text',
        'Section', 'Designator', 'Previous Context', 'Post Context',
        'Docx_Paragraph', 'Row Index'
    pxml_content : str
        Current final PXML content.
    client : anthropic.Anthropic
        The Anthropic client.
    model_name : str
        Model to use for LLM calls.
    chapter_number : str, optional
        Chapter number for section_dict preamble extraction.

    Returns
    -------
    tuple (updated_pxml_content, success_count, total_attempted)
    """
    # Filter for inline_deletion with REVISION FAILED
    retry_rows = [
        row for row in failed_inline_changes
        if str(row.get('Change Type', '')).strip() == 'inline_deletion'
        and 'REVISION FAILED' in str(row.get('Reason', ''))
        and str(row.get('Deleted Text', '')).strip()
    ]

    print(f"\n{'='*60}")
    print(f"[PIPELINE 9] Retry Failed Inline Deletions")
    print(f"{'='*60}")
    print(f"  Found {len(retry_rows)} failed inline_deletion rows to retry.")

    if not retry_rows:
        return pxml_content, 0, 0, [], retry_rows

    # Parse section_dict from current content
    section_dict = parse_pxml_sections_from_str_new(pxml_content, chapter_number)
    success_count = 0
    succeeded_row_indices = []  # Track which rows were successfully recovered
    failed_log_p9 = []  # dummy log for smart_replace

    for ri, failed_row in enumerate(retry_rows, 1):
        row_section = str(failed_row.get('Section', '') or failed_row.get('Designator', '')).strip()
        deleted_text = str(failed_row.get('Deleted Text', '')).strip()
        prev_ctx = str(failed_row.get('Previous Context', '')).strip()
        post_ctx = str(failed_row.get('Post Context', '')).strip()
        docx_para = str(failed_row.get('Docx_Paragraph', '')).strip()
        row_idx = failed_row.get('Row Index', f'p9_{ri}')

        print(f"\n  [{ri}/{len(retry_rows)}] idx={row_idx} | Section='{row_section}' | "
              f"Deleted Text='{deleted_text[:60]}...'")

        # Find section XML
        section_xml = None
        matched_key = None
        if row_section in section_dict:
            section_xml = section_dict[row_section]
            matched_key = row_section
        else:
            # Try partial match
            for k, v in section_dict.items():
                if row_section and row_section in k:
                    section_xml = v
                    matched_key = k
                    break

        if not section_xml:
            print(f"    ⚠️  Could not find section '{row_section}' in PXML. Skipping.")
            continue

        print(f"    Section matched: key='{matched_key}' ({len(section_xml)} chars)")

        # Build context hints for LLM
        context_hint = ""
        if prev_ctx:
            context_hint += f"\nprev_context (text immediately BEFORE the deletion): ...{prev_ctx}"
        if post_ctx:
            context_hint += f"\npost_context (text immediately AFTER the deletion): {post_ctx}..."
        if docx_para:
            context_hint += f"\ntext_before (full paragraph containing the deletion):\n{docx_para[:1500]}"

        changes_block = f"\nEXPLICIT CHANGE LIST (use these as ground truth for tagging):\n"
        changes_block += f"\n  Change 1 — type: inline_deletion\n"
        if prev_ctx:
            changes_block += f"    prev_context  : ...{prev_ctx}\n"
        if post_ctx:
            changes_block += f"    post_context  : {post_ctx}...\n"
        changes_block += f"    deleted_text  : {deleted_text}\n"

        # Resolve page image if available
        _page_img = None
        if page_texts and image_paths:
            try:
                from .revisions_images import get_image_for_revision
                _page_img = get_image_for_revision(docx_para or deleted_text, page_texts, image_paths)
            except Exception:
                pass

        image_cue_block = ""
        if _page_img and os.path.isfile(_page_img):
            image_cue_block = """
6. A PAGE IMAGE showing the actual document with track changes visible.
   USE THIS IMAGE AS A VISUAL CUE:
   - Strikethrough text = DELETED content
   - Text in any color other than black/gray (i.e. colored text without strikethrough) = INSERTED content
   - Use the image ONLY to confirm changes in the EXPLICIT CHANGE LIST.
   - NEVER use the image to delete content from CHILD elements not in the change list.
"""

        prompt = load_prompt(
            "failed_revision_retry.txt",
            changes_block=changes_block,
            context_hint=context_hint,
            image_cue_block=image_cue_block,
            section_xml=section_xml,
        )

        try:
            # Build messages — include image if available
            messages_content = []
            if _page_img and os.path.isfile(_page_img):
                import base64
                with open(_page_img, "rb") as img_f:
                    img_data = base64.b64encode(img_f.read()).decode("utf-8")
                messages_content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": img_data}
                })
            messages_content.append({"type": "text", "text": prompt})

            response = client.messages.create(
                model=model_name,
                max_tokens=8000,
                temperature=0,
                messages=[
                    {"role": "user", "content": messages_content},
                    {"role": "assistant", "content": "FIND:\n<"},
                ],
            )
            llm_text = "FIND:\n<" + response.content[0].text.strip()

            # Check for structural leakage (code fences or self-correction)
            _leakage_reason = None
            if '```' in llm_text:
                _leakage_reason = "code fences"
            elif llm_text.count('FIND:') > 1:
                _leakage_reason = f"multiple FIND blocks ({llm_text.count('FIND:')}) — model self-corrected"
            if _leakage_reason:
                print(f"    \u26a0\ufe0f  LLM leakage detected: {_leakage_reason}. Skipping.")
                continue

            # Parse FIND/REPLACE using the same parser from paragraph_level_trackchanges
            find_str, replace_str = parse_llm_response(llm_text)

            if not find_str or not replace_str:
                print(f"    ⚠️  Could not parse FIND/REPLACE from LLM response. Skipping.")
                print(f"    [DEBUG] Raw LLM response:\n{llm_text[:1000]}")
                continue

            if '<trace.deleted>' not in replace_str:
                print(f"    ⚠️  LLM response missing <trace.deleted> tags. Skipping.")
                print(f"    [DEBUG] REPLACE was: {replace_str[:500]}")
                continue

            # Use smart_replace (same multi-step strategy as paragraph_level_trackchanges)
            print(f"    [REPLACE] Attempting smart_replace...")
            print(f"    ┌─ FULL FIND ({len(find_str)} chars):")
            print(f"    │  {find_str}")
            print(f"    ├─ FULL REPLACE ({len(replace_str)} chars):")
            print(f"    │  {replace_str}")
            print(f"    └─────────────────────────────────────")

            before_content = pxml_content
            pxml_content = smart_replace(
                pxml_content, find_str, replace_str,
                idx=f"P9_{row_idx}",
                failed_log=failed_log_p9,
            )

            if pxml_content != before_content:
                success_count += 1
                succeeded_row_indices.append(ri - 1)  # index into retry_rows
                print(f"    ✅ Successfully applied trace.deleted tag.")
            else:
                print(f"    ⚠️  smart_replace could not apply the change.")

        except Exception as e:
            print(f"    ❌ LLM/replace error: {e}")

    print(f"\n  [PIPELINE 9 DONE] {success_count}/{len(retry_rows)} "
          f"failed deletions recovered.")

    return pxml_content, success_count, len(retry_rows), succeeded_row_indices, retry_rows
