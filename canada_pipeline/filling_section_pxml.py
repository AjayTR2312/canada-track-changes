import re
import anthropic
from pathlib import Path
import json
import pandas as pd
import os
from .prompt_loader import load_prompt

def extract_designator_from_content(content):
    """
    Extract the section designator from content like '§ 4:2 Slander Per Se...'
    Returns: '4:2'
    """
    match = re.search(r'§\s*(\d+:\d+)', content)
    if match:
        return match.group(1)
    return None

def find_designator_block_in_pxml(pxml_content, designator_value):
    """
    Find the designator tag and its following name tag in pxml content.
    
    Args:
        pxml_content: String content of the pxml file
        designator_value: The designator to search for (e.g., '4:2')
    
    Returns:
        The XML block containing designator and name tags, or None if not found
    """
    # Pattern to match designator tag with the value and the following name tag
    pattern = rf'(<designator[^>]*>{re.escape(designator_value)}</designator>\s*<name[^>]*>.*?</name>)'
    
    match = re.search(pattern, pxml_content, re.DOTALL)
    if match:
        return match.group(1)
    return None

# def find_designator_block_in_pxml(pxml_content, designator_value):
#     """
#     Find the designator tag and its following name tag in pxml content.
    
#     Args:
#         pxml_content: String content of the pxml file
#         designator_value: The designator to search for (e.g., '4:2')
    
#     Returns:
#         The XML block containing designator and name tags where data.trace="1", 
#         or None if not found
#     """
#     # Pattern to match designator tag with the value and the following name tag
#     pattern = rf'(<designator[^>]*>{re.escape(designator_value)}</designator>\s*<name[^>]*>.*?</name>)'
    
#     # Find all matches
#     matches = re.findall(pattern, pxml_content, re.DOTALL)
    
#     # Return the match that contains data.trace="1"
#     for match in matches:
#         if 'data.trace="1"' not in match:
#             return match
    
#     return None



# def get_claude_replacement(original_block, what_got_deleted, inserted_text, api_key):
#     """
#     Send to Claude with context to determine the replacement.
    
#     Args:
#         original_block: Original XML block found in pxml
#         what_got_deleted: The content from what_got_deleted column (e.g., "§ 4:2 Slander Per Se...")
#         inserted_text: The inserted text from dataframe (e.g., "3 3" or "3")
#         api_key: Anthropic API key
    
#     Returns:
#         Dictionary with 'find' and 'replace' keys
#     """
#     client = anthropic.Anthropic(api_key=api_key)
    
#     # Clean up inserted_text (handle duplicates like "3 3" -> "3")
#     inserted_clean = inserted_text.strip()
#     parts = inserted_clean.split()
#     if len(parts) > 1 and len(set(parts)) == 1:
#         inserted_clean = parts[0]
    
#     prompt = f"""You are analyzing a legal document update where section designators have changed.

# **What got deleted (old version):**
# {what_got_deleted}

# **What got inserted (new value):**
# {inserted_clean}

# **Current XML block in the file:**
# {original_block}

# **Analysis Task:**
# 1. Look at "what got deleted" - it shows the OLD section reference (e.g., "§ 4:2 Slander Per Se...")
# 2. The XML block contains a designator tag with a value like "4:2" 
# 3. The "inserted" value ({inserted_clean}) represents what NUMBER should replace the SECOND part of the designator (the part after the colon)
# 4. The FIRST part (chapter number before the colon) stays the same

# **Example:**
# - If what_got_deleted shows "§ 4:2 ...", the designator is "4:2"
# - If inserted value is "3", then the NEW designator should be "4:3"
# - The chapter number (4) remains unchanged, only the section number after the colon changes from 2 to 3

# **Your task:**
# Determine the correct replacement XML block where ONLY the number after the colon in the designator is updated to the inserted value. Everything else (attributes, name tag content, formatting) stays exactly the same.

# **important**
# if in designator the change is then u must add date.trace=1 <designator date.updated="0" date.trace="1">4:3</designator>

# Return ONLY a JSON response in this exact format:
# {{
#     "find": "<the exact XML block to find>",
#     "replace": "<the exact XML block to replace with, with updated designator>"
# }}

# Do not include any other text, explanation, or markdown formatting."""

#     message = client.messages.create(
#         model="claude-opus-4-20250514",
#         max_tokens=1024,
#         messages=[
#             {"role": "user", "content": prompt}
#         ]
#     )
    
#     response_text = message.content[0].text
    
#     # Extract JSON from response
#     json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
#     if json_match:
#         try:
#             return json.loads(json_match.group())
#         except json.JSONDecodeError as e:
#             print(f"Error parsing JSON: {e}")
#             print(f"Response text: {response_text}")
#             return None
    
#     print(f"Could not find JSON in response: {response_text}")
#     return None

import re
import json
import time
import anthropic
import httpx
from .config import API_KEY, build_anthropic_client, MODEL_OPUS

def get_claude_replacement(original_block, what_got_deleted, inserted_text, api_key, text_after=None, deleted_text=None, max_retries=3):
    """
    Send to Claude with context to determine the replacement.
    
    Args:
        original_block: Original XML block found in pxml
        what_got_deleted: The content from what_got_deleted column (text_before - old paragraph text)
        inserted_text: The inserted text from dataframe
        api_key: Anthropic API key
        text_after: The paragraph text after the change (text column)
        deleted_text: The specific text that was deleted
        max_retries: Number of retry attempts on timeout/connection errors
    
    Returns:
        Dictionary with 'find' and 'replace' keys, or None on failure
    """

    # ── Client with extended timeout ──────────────────────────────────────
    client = build_anthropic_client()

    # ── Clean up inserted_text (handle duplicates like "3 3" -> "3") ──────
    inserted_clean = inserted_text.strip()
    parts = inserted_clean.split()
    if len(parts) > 1 and len(set(parts)) == 1:
        inserted_clean = parts[0]

    # Build context about old vs new text
    context_lines = []
    context_lines.append(f"**Old paragraph text (before change):**")
    context_lines.append(what_got_deleted)
    if text_after:
        context_lines.append(f"\n**New paragraph text (after change):**")
        context_lines.append(text_after)
    if deleted_text:
        context_lines.append(f"\n**Specific text that was deleted:** {deleted_text}")
    context_lines.append(f"\n**Specific text that was inserted:** {inserted_clean}")
    diff_context = "\n".join(context_lines)

    prompt = load_prompt(
        "section_heading_change_analysis.txt",
        diff_context=diff_context,
        original_block=original_block,
    )

    # ── Retry loop ────────────────────────────────────────────────────────
    for attempt in range(max_retries):
        try:
            message = client.messages.create(
                model=MODEL_OPUS,
                max_tokens=4096,
                messages=[
                    {"role": "user", "content": prompt}
                ]
            )

            response_text = message.content[0].text

            # ── Parse JSON from response ──────────────────────────────────
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                try:
                    return json.loads(json_match.group())
                except json.JSONDecodeError as e:
                    print(f"[Attempt {attempt+1}] Error parsing JSON: {e}")
                    print(f"Response text: {response_text}")
                    return None  # JSON parse failure is not retryable

            print(f"[Attempt {attempt+1}] Could not find JSON in response: {response_text}")
            return None  # Unexpected response format, not retryable

        except (anthropic.APITimeoutError, anthropic.APIConnectionError) as e:
            wait = 2 ** attempt  # 1s, 2s, 4s
            if attempt < max_retries - 1:
                print(f"[Attempt {attempt+1}/{max_retries}] Timeout/Connection error: {e}. Retrying in {wait}s...")
                time.sleep(wait)
            else:
                print(f"[Attempt {attempt+1}/{max_retries}] All retries exhausted. Last error: {e}")
                return None

        except anthropic.APIStatusError as e:
            # Non-retryable API errors (4xx etc.)
            print(f"Non-retryable API error (status {e.status_code}): {e.message}")
            return None

        except Exception as e:
            print(f"Unexpected error on attempt {attempt+1}: {e}")
            return None

    return None

def process_pxml_replacements(updated_df, pxml_content, api_key, missing_revisions_output, verbose=True):
    """
    Process all rows with what_got_deleted and create replacement instructions.
    
    Args:
        updated_df: DataFrame with what_got_deleted and inserted_text columns
        pxml_path: Path to the pxml file
        api_key: Anthropic API key
        verbose: Print progress
    
    Returns:
        List of dictionaries with 'find' and 'replace' instructions
    """
    # Read pxml content
    # with open(output_path, 'r', encoding='utf-8') as f:
    #     pxml_content = f.read()
    
    replacements = []
    
    for i in range(len(updated_df)):
        what_got_deleted = updated_df.iloc[i]['what_got_deleted']
        inserted_text = updated_df.iloc[i]['inserted_text']
        
        if pd.notna(what_got_deleted):
            if verbose:
                print(f"\n{'='*80}")
                print(f"Processing row {i}")
                print(f"What got deleted: {what_got_deleted}")
                print(f"Inserted text: {inserted_text}")
            
            # Extract old designator from what_got_deleted
            old_designator = extract_designator_from_content(what_got_deleted)
            if not old_designator:
                if verbose:
                    print(f"  ❌ Could not extract designator from: {what_got_deleted}")
                continue
            
            if verbose:
                print(f"  📍 Old designator: {old_designator}")
            # print('pxmlcontent---------',pxml_content)
            print('***********************************************')
            # Find the original block in pxml
            original_block = find_designator_block_in_pxml(pxml_content, old_designator)
            print('originla block-------',original_block)
            if not original_block:
                if verbose:
                    print(f"  ❌ Could not find designator block in pxml for: {old_designator}")
                continue
            
            if verbose:
                print(f"  ✅ Found original block in pxml:")
                print(f"     {original_block}")
            
            # Send to Claude for replacement determination
            if verbose:
                print(f"  🤖 Sending to Claude Opus 4.1 for replacement...")
            
            text_after = updated_df.iloc[i]['text'] if 'text' in updated_df.columns and pd.notna(updated_df.iloc[i].get('text')) else None
            deleted_text = updated_df.iloc[i]['deleted_text'] if 'deleted_text' in updated_df.columns and pd.notna(updated_df.iloc[i].get('deleted_text')) else None
            claude_replacement = get_claude_replacement(
                original_block,
                what_got_deleted,
                inserted_text,
                api_key,
                text_after=text_after,
                deleted_text=deleted_text
            )
            
            if claude_replacement:
                replacements.append(claude_replacement)
                
                if verbose:
                    print(f"  ✅ Claude determined replacement:")
                    print(f"     FIND: {claude_replacement['find']}")
                    print(f"     REPLACE: {claude_replacement['replace']}")
            else:
                if verbose:
                    print(f"  ❌ Claude did not return valid replacement")

    updated_content=apply_replacements_to_pxml(pxml_content, replacements, missing_revisions_output,verbose=verbose)
    return updated_content

# def apply_replacements_to_pxml(pxml_content, replacements, missing_revisions_output, verbose=True):
#     """
#     Apply the replacements to the pxml file.
    
#     Args:
#         pxml_path: Path to the original pxml file
#         replacements: List of dictionaries with 'find' and 'replace' keys
#         output_path: Path to save the updated file (if None, overwrites original)
#         verbose: Print progress
    
#     Returns:
#         Updated pxml content
#     """
#     # with open(pxml_path, 'r', encoding='utf-8') as f:
#     #     pxml_content = f.read()
    
#     updated_content = pxml_content
    
#     for i, replacement in enumerate(replacements, 1):
#         find_text = replacement['find']
#         replace_text = replacement['replace']
        
#         if find_text in updated_content:
#             updated_content = updated_content.replace(find_text, replace_text)
#             if verbose:
#                 print(f"✅ Replacement {i}: FOUND and REPLACED")
#                 print(f"   Find: {find_text[:50]}..." if len(find_text) > 50 else f"   Find: {find_text}")
#                 print(f"   Replace: {replace_text[:50]}..." if len(replace_text) > 50 else f"   Replace: {replace_text}")
#         else:
#             if verbose:
#                 print(f"❌ Replacement {i}: NOT FOUND")
#                 print(f"   Searched for: {find_text[:50]}..." if len(find_text) > 50 else f"   Searched for: {find_text}")
    
    
    
#     # with open(output_path, 'w', encoding='utf-8') as f:
#     #     f.write(updated_content)
    
#     # if verbose:
#     #     print(f"\n💾 Saved updated pxml to: {output_path}")
    
#     return updated_content


def apply_replacements_to_pxml(pxml_content, replacements, missing_revisions_output, verbose=True):
    """
    Apply the replacements to the pxml file.
    
    Args:
        pxml_path: Path to the original pxml file
        replacements: List of dictionaries with 'find' and 'replace' keys
        output_path: Path to save the updated file (if None, overwrites original)
        verbose: Print progress
    
    Returns:
        Updated pxml content
    """
    updated_content = pxml_content
    
    for i, replacement in enumerate(replacements, 1):
        find_text = replacement['find']
        replace_text = replacement['replace']
        
        if find_text in updated_content:
            updated_content = updated_content.replace(find_text, replace_text)
            if verbose:
                print(f"✅ Replacement {i}: FOUND and REPLACED")
                print(f"   Find: {find_text[:50]}..." if len(find_text) > 50 else f"   Find: {find_text}")
                print(f"   Replace: {replace_text[:50]}..." if len(replace_text) > 50 else f"   Replace: {replace_text}")
        else:
            if verbose:
                print(f"❌ Replacement {i}: NOT FOUND")
                print(f"   Searched for: {find_text[:50]}..." if len(find_text) > 50 else f"   Searched for: {find_text}")
            
            os.makedirs(os.path.dirname(missing_revisions_output), exist_ok=True)
            with open(missing_revisions_output, 'a', encoding='utf-8') as f:
                f.write(f"section number Replacement {i}: NOT FOUND\n")
                f.write(f"Searched for section number: {find_text}\n")
                f.write("-" * 60 + "\n")
    
    return updated_content


    