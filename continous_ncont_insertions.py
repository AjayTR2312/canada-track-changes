##for new line insertions continous and non continous

import re
import json
import anthropic
from config import build_anthropic_client, MODEL_OPUS
client = build_anthropic_client(MODEL_OPUS)


# ─────────────────────────────────────────────
# STEP 1: Claude ONLY returns insertion plan
# No XML modification — just anchor + tagged text
# ─────────────────────────────────────────────
def build_claude_prompt(section_xml, group_rows):
    insertions_text = ""
    for i, (_, row) in enumerate(group_rows.iterrows()):
        insertions_text += f"""
Insertion #{i}:
  previous_context : {row['previous_context']}
  inserted_text    : {row['inserted_text']}
  post_context     : {row['post_context']}
"""

    prompt = f"""You are an XML analyst working with a legal XML document section.

YOUR ONLY JOB:
For each insertion, return a JSON plan with anchor_after and tagged_text.

STEP BY STEP INSTRUCTIONS:
1. Find the XML block in SECTION XML that contains the previous_context text
2. From that block, identify the OUTERMOST tag and its attributes (e.g. <para date.updated="0" ct.id="...">)
3. Identify the direct child tag wrapping the text (e.g. <para.text date.updated="0">)
4. For tagged_text:
   - Use the same outermost tag and ALL its existing attributes
   - ADD date.trace="1" as an additional attribute to the outermost tag
   - Use the same direct child text wrapper tag with its attributes
   - inserted_text goes inside the text wrapper as plain text — no extra tags
5. Do NOT copy any inner tags like <cite>, <ref>, or citation-specific tags
6. For anchor_after: pick a short unique substring (20-40 chars) from the XML just BEFORE the insertion point
7. For anchor_before: pick a short unique substring (20-40 chars) from the XML just AFTER the insertion point

ATTRIBUTE RULE:
- Outermost tag: copy all existing attributes + add date.trace="1"
- Example: <para date.updated="0" ct.id="abc123" date.trace="1">

ANCHOR RULES:
- anchor_after must exist verbatim in the XML — it is the content BEFORE the insertion point
- anchor_before must exist verbatim in the XML — it is the content AFTER the insertion point
- Together they form: anchor_after + [INSERT HERE] + anchor_before
- MINIMUM 40 chars each — short anchors like just '</entry>' or '</row>' WILL FAIL
- Include actual text content along with tags, not just closing tags alone
- Good anchor example: 'evidence in this case.</para.text></para>' (has text + tags)
- Bad anchor example: '</entry>\n</row>' (too short, only closing tags)
- Do NOT include excessive whitespace (tabs, newlines) inside anchors — keep them compact
- Both must be unique within the section XML

json_safety_instruction = 
CRITICAL JSON RULES:
- All string values must use only escaped double-quotes (\\") inside them — never raw unescaped double quotes.
- Do not include newlines inside string values — use \\n if needed.
- Return ONLY valid JSON. No markdown fences, no commentary.


OUTPUT FORMAT (strict JSON list, no markdown, no explanation):
[
  {{
    "insertion_index": 0,
    "anchor_after": "exact substring from XML just before insertion point",
    "anchor_before": "exact substring from XML just after insertion point",
    "tagged_text": "outermost tag with date.trace=1 and text wrapper with plain inserted_text inside"
  }}
]

--- SECTION XML ---
{section_xml}

--- INSERTIONS TO PLAN ---
{insertions_text}

Return ONLY the JSON list.
"""
    return prompt


# def build_claude_prompt(section_xml, group_rows):
#     insertions_text = ""
#     for i, (_, row) in enumerate(group_rows.iterrows()):
#         insertions_text += f"""
# Insertion #{i}:
#   previous_context : {row['previous_context']}
#   inserted_text    : {row['inserted_text']}
#   post_context     : {row['post_context']}
# """

#     prompt = f"""You are an XML analyst working with a legal XML document section.

# YOUR ONLY JOB:
# For each insertion, return a JSON plan with anchor_before, anchor_after and tagged_text.

# STEP BY STEP INSTRUCTIONS:
# 1. Find the XML block in SECTION XML that contains the previous_context text
# 2. From that block, identify the OUTERMOST tag and its attributes (e.g. <para date.updated="0" ct.id="...">)
# 3. Identify the direct child tag wrapping the text (e.g. <para.text date.updated="0">)
# 4. For tagged_text:
#    - Use the same outermost tag and ALL its existing attributes
#    - ADD date.trace="1" as an additional attribute to the outermost tag
#    - Use the same direct child text wrapper tag with its attributes
#    - inserted_text goes inside the text wrapper as plain text — no extra tags
# 5. Do NOT copy any inner tags like <cite>, <ref>, or citation-specific tags
# 6. For anchor_after: pick a short unique substring (20-40 chars) from the XML just BEFORE the insertion point
# 7. For anchor_before: pick a short unique substring (20-40 chars) from the XML just AFTER the insertion point

# ATTRIBUTE RULE:
# - Outermost tag: copy all existing attributes + add date.trace="1"
# - Example: <para date.updated="0" ct.id="abc123" date.trace="1">

# ANCHOR RULES:
# - anchor_after must exist verbatim in the XML — it is the content BEFORE the insertion point
# - anchor_before must exist verbatim in the XML — it is the content AFTER the insertion point
# - Together they form a sandwich: anchor_after + [INSERT HERE] + anchor_before
# - For multiple insertions in the same area, anchors must be distinct to preserve correct ordering

# OUTPUT FORMAT (strict JSON list, no markdown, no explanation):
# [
#   {{
#     "insertion_index": 0,
#     "anchor_after": "exact substring from XML just before insertion point",
#     "anchor_before": "exact substring from XML just after insertion point",
#     "tagged_text": "outermost tag with date.trace=1 and text wrapper with plain inserted_text inside"
#   }}
# ]

# --- SECTION XML ---
# {section_xml}

# --- INSERTIONS TO PLAN ---
# {insertions_text}

# Return ONLY the JSON list.
# """
#     return prompt


# def call_claude(prompt):
#     """Call Claude Opus and return parsed JSON plan."""
#     response = client.messages.create(
#         model=MODEL_OPUS,
#         max_tokens=2048,
#         messages=[{"role": "user", "content": prompt}]
#     )
#     raw = response.content[0].text.strip()

#     # Strip markdown code block if Claude wraps it
#     raw = re.sub(r'^```(?:json)?\n?', '', raw)
#     raw = re.sub(r'\n?```$', '', raw)

#     return json.loads(raw)

# def call_claude(prompt):
#     """Call Claude Opus and return parsed JSON plan."""
#     response = client.messages.create(
#         model=MODEL_OPUS,
#         max_tokens=10000,
#         messages=[{"role": "user", "content": prompt}]
#     )
#     raw = response.content[0].text.strip()

#     # Strip markdown code block if Claude wraps it
#     raw = re.sub(r'^```(?:json)?\n?', '', raw)
#     raw = re.sub(r'\n?```$', '', raw)

#     try:
#         return json.loads(raw)
#     except json.JSONDecodeError as e:
#         print("JSON decode error:", e)
#         print("Raw response was:")
#         print(raw)
#         raise


import json_repair  # pip install json-repair

def call_claude(prompt):
    response = client.messages.create(
        model=MODEL_OPUS,
        max_tokens=10000,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = response.content[0].text.strip()

    raw = re.sub(r'^```(?:json)?\n?', '', raw)
    raw = re.sub(r'\n?```$', '', raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print("JSON decode error:", e)
        print("Attempting repair...")
        try:
            repaired = json_repair.repair_json(raw)
            return json.loads(repaired)
        except Exception as e2:
            print("Repair also failed:", e2)
            print("Raw response was:")
            print(raw)
            raise


# ─────────────────────────────────────────────
# STEP 2: Your code does the actual insertion
# Safe, deterministic, no hallucination risk
# ─────────────────────────────────────────────
def apply_insertion_plan(section_xml, insertion_plan, missing_revisions_output, section_key=None):
    """
    Apply the insertion plan to section_xml.
    For each item in plan:
      - Find anchor_after in the XML string
      - Insert tagged_text immediately after it
    Process in reverse order of position to preserve offsets.
    """
    # Resolve positions first
    resolved = []
    for item in insertion_plan:
        anchor = item['anchor_after']
        anchor_before = item.get('anchor_before', '')
        tagged = item['tagged_text']
        pos = section_xml.find(anchor)

        if pos != -1:
            insert_at = pos + len(anchor)
            resolved.append((insert_at, tagged, item['insertion_index']))
            continue

        # Fallback 1: flexible inter-tag whitespace matching on anchor_after
        escaped = re.escape(anchor.strip())
        flex_pattern = re.sub(r'>\s*<', r'>\\s*<', escaped)
        m = re.search(flex_pattern, section_xml, re.DOTALL)
        if m:
            print(f"  🔧 Anchor matched via flexible whitespace for insertion #{item['insertion_index']}")
            insert_at = m.end()
            resolved.append((insert_at, tagged, item['insertion_index']))
            continue

        # Fallback 1b: collapse ALL whitespace in anchor and XML, then map back
        collapsed_anchor = re.sub(r'\s+', '', anchor.strip())
        if len(collapsed_anchor) >= 10:
            collapsed_xml = re.sub(r'\s+', '', section_xml)
            collapsed_pos = collapsed_xml.find(collapsed_anchor)
            if collapsed_pos != -1:
                # Map collapsed position back to original XML position
                orig_pos = 0
                collapsed_count = 0
                while collapsed_count < collapsed_pos + len(collapsed_anchor) and orig_pos < len(section_xml):
                    if not section_xml[orig_pos].isspace():
                        collapsed_count += 1
                    orig_pos += 1
                print(f"  🔧 Anchor matched via collapsed whitespace for insertion #{item['insertion_index']}")
                insert_at = orig_pos
                resolved.append((insert_at, tagged, item['insertion_index']))
                continue

        # Fallback 2: try anchor_before (text just AFTER the insertion point)
        if anchor_before:
            pos_before = section_xml.find(anchor_before)
            if pos_before != -1:
                print(f"  🔧 anchor_after failed, using anchor_before for insertion #{item['insertion_index']}")
                insert_at = pos_before
                resolved.append((insert_at, tagged, item['insertion_index']))
                continue

            # Fallback 2b: flexible whitespace on anchor_before
            escaped_before = re.escape(anchor_before.strip())
            flex_pattern_before = re.sub(r'>\s*<', r'>\\s*<', escaped_before)
            m_before = re.search(flex_pattern_before, section_xml, re.DOTALL)
            if m_before:
                print(f"  🔧 anchor_before matched via flexible whitespace for insertion #{item['insertion_index']}")
                insert_at = m_before.start()
                resolved.append((insert_at, tagged, item['insertion_index']))
                continue

            # Fallback 2c: collapse ALL whitespace on anchor_before
            collapsed_before = re.sub(r'\s+', '', anchor_before.strip())
            if len(collapsed_before) >= 10:
                if 'collapsed_xml' not in locals():
                    collapsed_xml = re.sub(r'\s+', '', section_xml)
                collapsed_pos_b = collapsed_xml.find(collapsed_before)
                if collapsed_pos_b != -1:
                    orig_pos = 0
                    collapsed_count = 0
                    while collapsed_count < collapsed_pos_b and orig_pos < len(section_xml):
                        if not section_xml[orig_pos].isspace():
                            collapsed_count += 1
                        orig_pos += 1
                    # Walk back past any leading whitespace to the actual insertion point
                    while orig_pos > 0 and section_xml[orig_pos - 1].isspace():
                        orig_pos -= 1
                    print(f"  🔧 anchor_before matched via collapsed whitespace for insertion #{item['insertion_index']}")
                    insert_at = orig_pos
                    resolved.append((insert_at, tagged, item['insertion_index']))
                    continue

        print(f"⚠️  Anchor not found in XML: '{anchor}' — skipping insertion #{item['insertion_index']}")
        with open(missing_revisions_output, 'a', encoding='utf-8') as f:
            f.write(f"{'='*80}\n")
            f.write(f"FAILED NEWLINE INSERTION\n")
            f.write(f"{'-'*40}\n")
            f.write(f"Section          : {section_key or 'Unknown'}\n")
            f.write(f"Insertion Index  : {item['insertion_index']}\n")
            f.write(f"Tagged Text      : {tagged}\n")
            f.write(f"{'-'*40}\n")
            f.write(f"Anchor After     : {anchor}\n")
            f.write(f"Anchor Before    : {anchor_before}\n")
            f.write(f"{'='*80}\n\n")
        continue

    # Sort by position DESCENDING so earlier offsets stay valid
    # For same position, sort by insertion_index DESCENDING so that
    # when inserted back-to-front, the lowest index ends up first
    resolved.sort(key=lambda x: (x[0], x[2]), reverse=True)

    for insert_at, tagged, idx in resolved:
        section_xml = section_xml[:insert_at] + '\n' + tagged + section_xml[insert_at:]
        print(f"  ✅ Inserted #{idx} at position {insert_at}")

    return section_xml

# def apply_insertion_plan(section_xml, insertion_plan, missing_revisions_output):
#     resolved = []
#     for item in insertion_plan:
#         anchor_after = item['anchor_after']
#         anchor_before = item.get('anchor_before')  # optional fallback
#         tagged = item['tagged_text']

#         # Try sandwich match first if anchor_before is available
#         insert_at = None

#         if anchor_before:
#             pos_after = section_xml.find(anchor_after)
#             pos_before = section_xml.find(anchor_before)

#             if pos_after != -1 and pos_before != -1 and pos_after < pos_before:
#                 insert_at = pos_after + len(anchor_after)
#                 print(f"  🥪 Sandwich match found for #{item['insertion_index']}")
#             else:
#                 print(f"  ⚠️  Sandwich match failed for #{item['insertion_index']} — falling back to anchor_after only")

#         # Fallback to anchor_after only
#         if insert_at is None:
#             pos = section_xml.find(anchor_after)
#             if pos == -1:
#                 print(f"  ⚠️  Anchor not found in XML: '{anchor_after}' — skipping insertion #{item['insertion_index']}")
#                 with open(missing_revisions_output, 'a', encoding='utf-8') as f:
#                     f.write(f"Anchor not found: '{anchor_after}' — skipping insertion #{item['insertion_index']}\n")
#                 continue
#             insert_at = pos + len(anchor_after)

#         resolved.append((insert_at, item['insertion_index'], tagged))

#     # Primary: position DESCENDING, Secondary: insertion_index DESCENDING (tie-break)
#     resolved.sort(key=lambda x: (x[0], x[1]), reverse=True)

#     for insert_at, idx, tagged in resolved:
#         section_xml = section_xml[:insert_at] + '\n' + tagged + section_xml[insert_at:]
#         print(f"  ✅ Inserted #{idx} at position {insert_at}")

#     return section_xml

def extract_section_key(section_str, section_pattern=None):
    """Extract numeric key like '1:1' or '3.2' from section string.
    e.g. '§ 1:1 Abuse of Process Alleged as a Countercla...' → '1:1'
    e.g. '1.2 Some Section Title' → '1.2'
    """
    # if section_pattern:
    #     match = re.search(section_pattern, str(section_str))
    #     if match:
    #         # If the pattern has a capturing group, return it; else, return the whole match
    #         return match.group(1).strip() if match.lastindex else match.group(0).strip()
    #     else:

    #         return None
    # else:
    match = re.search(r'(\d+[A-Za-z]*[:.][\dA-Za-z]+)', str(section_str))
    return match.group(0) if match else None

def parse_pxml_sections_from_str(pxml_content):
    """Read PXML file and extract sections mapped by designator"""
    
    
    section_dict = {}
    section_positions = {}
    
    pattern = r'<section\s+(?:uuid="[^"]+"\s+)?[^>]*>.*?</section>'
    section_matches = re.finditer(pattern, pxml_content, re.DOTALL)
    
    for match in section_matches:
        section_xml = match.group(0)
        start_pos = match.start()
        end_pos = match.end()
        
        designator_match = re.search(r'<designator[^>]*>([\d:.A-Za-z]+)</designator>', section_xml)
        
        if designator_match:
            designator = designator_match.group(1)
            section_dict[designator] = section_xml
            section_positions[designator] = (start_pos, end_pos)
    
    return section_dict, section_positions, pxml_content


# ─────────────────────────────────────────────
# STEP 3: Main pipeline
# ─────────────────────────────────────────────
def insert_continous_insertions_into_pxml(newline_insertion_continuous_df, updated_pxml_content, missing_revisions_output, section_pattern=None):
    """
    Full pipeline:
    1. Parse PXML
    2. Group df by section_key
    3. Claude returns JSON insertion plan (anchor + tagged text only)
    4. Your code applies insertions deterministically
    5. Patch updated section back into pxml_content string
    """

    section_dict, section_positions, pxml_content = parse_pxml_sections_from_str(updated_pxml_content)

    df = newline_insertion_continuous_df.copy()
    df['section_key'] = df['section'].apply(lambda s: extract_section_key(s, section_pattern=section_pattern))

    modified_sections = {}  # section_key → updated section XML

    for section_key, group in df.groupby('section_key'):

        if section_key not in section_dict:
            print(f"⚠️  Section '{section_key}' not found in PXML. Skipping.")
            continue

        section_xml = section_dict[section_key]
        print(f"\n🔍 Section {section_key} — {len(group)} insertions to plan...")

        # Claude only returns WHERE and WHAT (tagged), never touches XML
        prompt = build_claude_prompt(section_xml, group)
        insertion_plan = call_claude(prompt)
        print(f"📋 Claude returned plan with {len(insertion_plan)} items.")

        # Your code does the actual insertion
        updated_section_xml = apply_insertion_plan(section_xml, insertion_plan, missing_revisions_output, section_key)
        modified_sections[section_key] = updated_section_xml

    # Patch sections back into pxml_content in reverse position order
    sorted_keys = sorted(
        [k for k in modified_sections if k in section_positions],
        key=lambda k: section_positions[k][0],
        reverse=True
    )

    for section_key in sorted_keys:
        start, end = section_positions[section_key]
        new_xml = modified_sections[section_key]
        pxml_content = pxml_content[:start] + new_xml + pxml_content[end:]
        print(f"📌 Patched section {section_key} into PXML.")

    return pxml_content


