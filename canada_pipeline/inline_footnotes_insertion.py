import os
import re
import json
import requests
import pandas as pd
import anthropic
import httpx
from .config import find_section_key, MODEL_OPUS, MODEL_SONNET
from .prompt_loader import load_prompt


# ──────────────────────────────────────────────
# 1. ANTHROPIC CLIENT
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


def call_anthropic(client, model_name: str, prompt: str, max_tokens: int = 10000) -> str:
    """
    Unified Anthropic call wrapper.
    Accepts a plain prompt string, returns the response text.
    Use this for ALL LLM calls in this codebase.
    """
    message = client.messages.create(
        model=model_name,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


# ──────────────────────────────────────────────
# 2. SECTION KEY HELPERS
# ──────────────────────────────────────────────

def extract_section_key(section_str: str) -> str:
    """
    Extracts numeric key from section column value.
    e.g. '33:1. Questions of Legislative Power' -> '33:1'
    """
    match = re.match(r'[\d:.]+', section_str.strip())
    return match.group(0) if match else section_str.strip()


def _llm_yes_no(client, prompt_text: str, model_name: str = MODEL_SONNET, max_retries: int = 3) -> str:
    """
    Ask the LLM a strict yes/no question. Retries if the answer is not
    a clean 'yes' or 'no'.

    Returns
    -------
    str – 'yes' or 'no'
    """
    for attempt in range(1, max_retries + 1):
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

            print(f"    [LLM YES/NO] attempt {attempt} got '{answer}', retrying...")
            prompt_text = (
                f"You gave an invalid response: '{answer}'. "
                "I strictly need only 'yes' or 'no'. Please answer again.\n\n"
                + prompt_text
            )
        except Exception as e:
            print(f"    [LLM YES/NO] attempt {attempt} error: {e}")

    print(f"    [LLM YES/NO] exhausted {max_retries} retries, defaulting to 'no'")
    return "no"



# ──────────────────────────────────────────────
# 3. FAILED ROW LOGGER
# ──────────────────────────────────────────────

def log_failed_footnote_row(failed_log_path: str,
                             idx_value,
                             fn_row: pd.Series,
                             reason: str) -> None:
    """
    Appends a human-readable block for a failed footnote row
    to the failed log file.
    """
    sep = "=" * 70

    lines = [
        sep,
        "FAILED FOOTNOTE ROW",
        sep,
        f"  idx                       : {idx_value}",
        f"  reason                    : {reason}",
        f"  section                   : {fn_row.get('section', 'N/A')}",
        f"  superscript / ref_number  : {fn_row.get('superscript', 'N/A')}",
        f"  last_word_before_superscript: {str(fn_row.get('last_word_before_superscript', 'N/A'))}",
        f"  text_before_superscript   : {str(fn_row.get('text_before_superscript', ''))[:300]}",
        f"  post_para                 : {str(fn_row.get('post_para', ''))[:200]}",
        f"  footnote_text             : {str(fn_row.get('text', ''))[:300]}",
        sep,
        "",
    ]

    with open(failed_log_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"  [LOG] Failed row written to: {failed_log_path}")


# ──────────────────────────────────────────────
# 4. LLM CALL WITH RETRY — ANTHROPIC
# ──────────────────────────────────────────────

def call_llm_to_get_footnote_and_anchor(client,
                                         model_name: str,
                                         section_xml: str,
                                         text_before_superscript: str,
                                         last_word_before_superscript: str,
                                         footnote_text: str,
                                         ref_number: str = "1",
                                         post_para: str = None,
                                         max_retries: int = 2) -> dict:

    post_para_block = ""
    if post_para:
        post_para_block = f"""
POST_PARA (the paragraph that comes AFTER the target paragraph — use for positioning only):
{post_para}
"""

    text_before_superscript_clean = text_before_superscript.replace('►', '').strip()
    last_word_before_superscript_clean = last_word_before_superscript.replace('►', '').strip() if last_word_before_superscript else 'N/A'
    prompt = load_prompt(
        "inline_footnote_insertion.txt",
        footnote_text=footnote_text,
        post_para_block=post_para_block,
        ref_number=ref_number,
        section_xml=section_xml,
        text_before_superscript_clean=text_before_superscript_clean,
        last_word_before_superscript_clean=last_word_before_superscript_clean,
    )

    retry_prompt_suffix = """

IMPORTANT — YOUR PREVIOUS RESPONSE WAS INVALID JSON.
Common causes and fixes:
  - Unescaped double quotes inside a JSON string value  → escape them as \\\"
  - Literal newlines inside a JSON string value         → replace with \\n
  - Truncated output (response was cut off)             → shorten footnote_tag if needed
  - Trailing commas or extra text outside the JSON object → remove them

Return ONLY a valid JSON object. Nothing else. No markdown. No explanation.
"""

    last_error = None
    last_raw   = None

    for attempt in range(1, max_retries + 1):
        use_prompt = prompt if attempt == 1 else prompt + retry_prompt_suffix

        print(f"  [LLM] Attempt {attempt}/{max_retries}...")

        try:
            raw = call_anthropic(
                client=client,
                model_name=model_name,
                prompt=use_prompt,
                max_tokens=10000,
            )
        except Exception as e:
            print(f"  [ERROR] Attempt {attempt} — Anthropic API call failed: {e}")
            last_error = e
            last_raw   = None
            if attempt < max_retries:
                print(f"  [RETRY] Retrying...")
            continue

        # Strip markdown code fences if model wraps in them despite instructions
        raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.IGNORECASE)
        raw = re.sub(r'\s*```$', '', raw)

        try:
            result = json.loads(raw)

            # Safety net: strip </para.text> from insert_after if present
            insert_after = result.get("insert_after", "") or ""
            if "</para.text>" in insert_after:
                insert_after = insert_after.replace("</para.text>", "").rstrip()
                print(f"  [FIX] Stripped </para.text> from insert_after automatically")
                result["insert_after"] = insert_after

            print(f"  [LLM] Valid JSON received on attempt {attempt}.")
            return result

        except json.JSONDecodeError as e:
            last_error = e
            last_raw   = raw
            print(f"  [ERROR] Attempt {attempt} — invalid JSON: {e}")
            if attempt < max_retries:
                print(f"  [RETRY] Retrying with corrective instructions...")

    # All retries exhausted
    print(f"  [FAIL] All {max_retries} attempts returned invalid JSON.")
    if last_raw:
        print(f"  [RAW LLM OUTPUT (last attempt)]:\n{last_raw}")
    return {"insert_after": None, "footnote_tag": None}


# ──────────────────────────────────────────────
# 5. FOOTNOTE INSERTION
# ──────────────────────────────────────────────

def insert_footnote_using_anchor(section_xml: str,
                                  insert_after: str,
                                  footnote_tag: str) -> str:
    """
    Takes the verbatim `insert_after` string from LLM,
    finds it in section_xml, and splices footnote_tag right after it.
    Returns modified section_xml.
    """
    if not insert_after or not footnote_tag:
        print("  [WARN] insert_after or footnote_tag is empty, skipping insertion.")
        return section_xml

    pos = section_xml.find(insert_after)

    if pos == -1:
        # Try normalizing whitespace as fallback
        normalized_section = re.sub(r'\s+', ' ', section_xml)
        normalized_anchor  = re.sub(r'\s+', ' ', insert_after)
        pos = normalized_section.find(normalized_anchor)

        if pos == -1:
            print(f"  [WARN] Could not find insert_after anchor in section XML:")
            print(f"    >> {insert_after[:100]}")
            return section_xml

        # Walk original string to map normalized position back to raw position
        norm_end   = pos + len(normalized_anchor)
        orig_pos   = 0
        norm_count = 0
        prev_space = False
        for i, ch in enumerate(section_xml):
            if re.match(r'\s', ch):
                if not prev_space:
                    norm_count += 1
                    prev_space  = True
            else:
                norm_count += 1
                prev_space  = False
            if norm_count >= norm_end:
                orig_pos = i + 1
                break
        insert_pos = orig_pos
    else:
        insert_pos = pos + len(insert_after)

    modified = section_xml[:insert_pos] + footnote_tag + section_xml[insert_pos:]
    print(f"  [OK] Footnote inserted after: '...{insert_after[-60:]}'")
    return modified


# ──────────────────────────────────────────────
# 6. MAIN DRIVER
# ──────────────────────────────────────────────

def process_all_footnotes_with_llm(pxml_content: str,
                                    section_dict: dict,
                                    section_positions: dict,
                                    all_footnote_revisions: pd.DataFrame,
                                    client,
                                    model_name: str,
                                    failed_log_path: str = "failed_footnotes.txt") -> str:
    """
    Main driver — iterates directly over all_footnote_revisions.
    No matched_revisions outer loop needed.

    Required columns in all_footnote_revisions:
      - text                        : footnote content to insert
      - text_before_superscript     : full plain text of paragraph up to superscript
      - last_word_before_superscript: single word/token immediately before superscript
      - section                     : section identifier string
      - superscript                 : footnote reference number
      - post_para (optional)        : paragraph after target, for positioning
    """

    # Track modified sections — multiple footnotes may go into same section
    section_modifications = {}

    # Initialise / clear the failed log at the start of each run
    with open(failed_log_path, "w", encoding="utf-8") as f:
        f.write(f"FAILED FOOTNOTE INSERTIONS LOG\n{'=' * 70}\n\n")

    total     = len(all_footnote_revisions)
    succeeded = 0
    failed    = 0

    for row_num, (_, fn_row) in enumerate(all_footnote_revisions.iterrows(), start=1):

        # ── Read all fields from the single row ──────────────────────────
        idx_value                    = fn_row.get("parent_paragraph_idx_x",
                                        fn_row.get("idx", row_num))
        footnote_text                = fn_row["text"]
        text_before_superscript      = fn_row["text_before_superscript"]
        last_word_before_superscript = fn_row.get("last_word_before_superscript", "")
        section_raw                  = fn_row["section"]
        post_para                    = fn_row.get("post_para", None)

        # Cast float -> int -> str to avoid "1.0" style values
        ref_number = (
            str(int(fn_row["superscript"]))
            if "superscript" in fn_row.index and pd.notna(fn_row["superscript"])
            else "1"
        )

        print(f"\n{'─' * 60}")
        print(f"[INFO] Row {row_num}/{total} | idx={idx_value} | "
              f"section={section_raw} | ref={ref_number}")
        print(f"  text_before_superscript      : {str(text_before_superscript)[:80]}")
        print(f"  last_word_before_superscript : {str(last_word_before_superscript)}")
        print(f"  footnote_text                : {str(footnote_text)[:80]}")
        print(f"  post_para                    : {str(post_para)[:60] if post_para else 'None'}")

        # ── Step 1: Resolve section key ──────────────────────────────────
        section_key = find_section_key(str(section_raw), section_dict,
                                        client=client, model_name=model_name)
        if section_key is None:
            print(f"  [WARN] No matching section key found for '{section_raw}'")
            log_failed_footnote_row(
                failed_log_path, idx_value, fn_row,
                reason=f"No matching section key found for '{section_raw}'"
            )
            failed += 1
            continue

        # ── Step 2: Use already-modified section if touched before ────────
        if section_key not in section_modifications:
            section_modifications[section_key] = section_dict[section_key]

        current_section_xml = section_modifications[section_key]

        # ── Step 3: Ask LLM for footnote_tag + insert_after (with retry) ──
        llm_result = call_llm_to_get_footnote_and_anchor(
            client=client,
            model_name=model_name,
            section_xml=current_section_xml,
            text_before_superscript=text_before_superscript,
            last_word_before_superscript=str(last_word_before_superscript),
            footnote_text=footnote_text,
            ref_number=ref_number,
            post_para=post_para,
            max_retries=2,
        )

        # ── Step 4: Validate LLM output ──────────────────────────────────
        if not llm_result.get("insert_after") or not llm_result.get("footnote_tag"):
            print(f"  [FAIL] LLM returned no usable output after all retries.")
            log_failed_footnote_row(
                failed_log_path, idx_value, fn_row,
                reason="LLM returned None for insert_after or footnote_tag after all retries"
            )
            failed += 1
            continue

        print(f"  insert_after : {str(llm_result.get('insert_after', ''))[:80]}")

        # ── Step 5: Python does the actual insertion ──────────────────────
        section_before   = section_modifications[section_key]
        modified_section = insert_footnote_using_anchor(
            section_xml=current_section_xml,
            insert_after=llm_result.get("insert_after", ""),
            footnote_tag=llm_result.get("footnote_tag", "")
        )

        # Detect silent failure — anchor not found, XML unchanged
        if modified_section == section_before:
            print(f"  [FAIL] Anchor not found — insertion had no effect on the XML.")
            log_failed_footnote_row(
                failed_log_path, idx_value, fn_row,
                reason=(
                    f"insert_after anchor not found in section XML. "
                    f"Anchor was: {str(llm_result.get('insert_after', ''))[:120]}"
                )
            )
            # Still store so subsequent footnotes in same section use latest state
            section_modifications[section_key] = modified_section
            failed += 1
            continue

        section_modifications[section_key] = modified_section
        succeeded += 1

    # ── Step 6: Splice modified sections back into pxml (reverse order) ──
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

    print(f"\n{'=' * 60}")
    print(f"[SUMMARY] Total rows     : {total}")
    print(f"[SUMMARY] Succeeded      : {succeeded}")
    print(f"[SUMMARY] Failed         : {failed}")
    print(f"[SUMMARY] Failed log     : {failed_log_path}")
    print(f"{'=' * 60}")

    return modified_pxml

# Import shared parser — single source of truth for all pipelines
from .pxml_parser import parse_pxml_sections as _parse_shared


def parse_pxml_sections_from_str_dict_positions(pxml_content):
    """Parse PXML sections. Delegates to shared parser."""
    return _parse_shared(pxml_content)


