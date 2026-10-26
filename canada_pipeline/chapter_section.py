import re
import json
import anthropic
from canada_pipeline.config import build_anthropic_client, MODEL_SONNET


def _extract_designators_from_section_front_blocks(pxml_content):
    """
    Extract <designator> values from:
    - <section.front> ... </section.front>
    - <appendix.section.front> ... </appendix.section.front>
    - any tag that ends with 'section.front'
    """
    block_pattern = r'<([A-Za-z0-9_.-]*section\.front)\b[^>]*>.*?</\1>'
    section_front_blocks = re.findall(block_pattern, pxml_content, re.DOTALL)

    # re.findall with one capturing group returns only group contents;
    # so fetch complete blocks with finditer.
    blocks = [m.group(0) for m in re.finditer(block_pattern, pxml_content, re.DOTALL)]

    designator_matches = []
    for block in blocks:
        desigs = re.findall(r'<designator[^>]*>([^<]+)</designator>', block)
        designator_matches.extend(d.strip() for d in desigs if d and d.strip())

    return designator_matches

def extract_chapter_number_from_pxml(pxml_content):
    front_block = re.search(r'<front[^>]*>(.*?)</front>', pxml_content, re.DOTALL)
    if front_block:
       front_content = front_block.group(1)
       designator = re.search(r'<designator[^>]*>(.*?)</designator>', front_content)
       if designator:
           print(designator.group(1))  # Output: 3
           return designator.group(1)
       
def build_section_pattern_from_pxml(pxml_content):
    """
    Read the PXML content, extract all <designator> values,
    filter those matching section_start_number, and send the
    matching designators to Claude LLM to generate a regex pattern.
    
    e.g. if designators are '3:1', '3:2' → pattern uses ':'
         if designators are '3.1', '3.2' → pattern uses '.'
         if designators are '3A:24'      → pattern includes letter+colon
    """

    section_start_number = extract_chapter_number_from_pxml(pxml_content)
    designator_matches = _extract_designators_from_section_front_blocks(pxml_content)

    chapter_str = str(section_start_number).strip() if section_start_number is not None else ""
    default_pattern = rf'§?\s*{chapter_str}[:.][\dA-Za-z]+[^§\n]*' if chapter_str else r'§?\s*\d+[A-Za-z]*[:.][\dA-Za-z]+[^§\n]*'

    if not designator_matches:
        print("⚠️  No designators found in tags ending with <section.front>. Falling back to default pattern.")
        if not chapter_str:
            print("⚠️  Chapter number not found in PXML <front>. Using generic section pattern.")
        return section_start_number, default_pattern, []
    
    # Filter designators that start with the section_start_number
    matching_designators = [d for d in designator_matches if chapter_str and d.startswith(chapter_str)]
    
    if not matching_designators:
        if chapter_str:
            print(f"⚠️  No designators matching chapter '{chapter_str}' found. All designators: {designator_matches[:10]}")
        else:
            print(f"⚠️  Chapter number not found in PXML <front>. Using generic pattern from available designators: {designator_matches[:10]}")
        return section_start_number, default_pattern, designator_matches
    
    print(f"📋 Found {len(matching_designators)} designators for chapter '{chapter_str}': {matching_designators[:10]}")
    
    # Send matching_designators to Claude to build the regex pattern
    client = build_anthropic_client(MODEL_SONNET)
    
    prompt = f"""You are an expert in regex patterns. I have a list of section designator values extracted from a legal XML document. I need you to build a Python regex pattern that will match these designators (and similar ones) when they appear in plain text.

**Designator values (sample):**
{json.dumps(matching_designators[:30])}

**Chapter/section start number:** {chapter_str}

**Requirements for the regex pattern:**
1. The pattern should match all the provided designator values
2. The § symbol before the number should be optional: §?
3. Allow optional whitespace after §: \\s*
4. Detect the separator used (colon :, period ., or both) from the designators
5. If letters appear before the separator (e.g., '3A:1'), include [A-Za-z]* before the separator
6. If letters appear after the separator (e.g., '3:1a'), include [A-Za-z] after the digit portion
7. After the main designator portion, append [^§\\n]* to capture trailing text until the next § or newline
8. The pattern should be generalizable to match similar designators not in the list
9. The pattern must be a valid Python raw regex string

**Examples of expected output format:**
- For designators ['3:1', '3:2', '3:10']: §?\\s*3[:]\\d+[^§\\n]*
- For designators ['3.1', '3.2', '3.10']: §?\\s*3[.]\\d+[^§\\n]*
- For designators ['3:1', '3.2']: §?\\s*3[:.]\\d+[^§\\n]*
- For designators ['3A:1', '3A:2']: §?\\s*3[A-Za-z]*[:]\\d+[^§\\n]*
- For designators ['3:1a', '3:2b']: §?\\s*3[:]\\d+[A-Za-z]*[^§\\n]*

**Return ONLY the regex pattern string, nothing else. No explanation, no code fences, no quotes. Just the raw pattern.**"""

    try:
        print(f"🤖 Asking Claude to build section pattern from {len(matching_designators)} designators...")
        
        message = client.messages.create(
            model=MODEL_SONNET,
            max_tokens=256,
            temperature=0,
            messages=[{"role": "user", "content": prompt}]
        )
        
        pattern_from_llm = message.content[0].text.strip()
        
        # Clean up: remove any accidental quotes or code fences
        pattern_from_llm = pattern_from_llm.strip('`"\' ')
        if pattern_from_llm.startswith('r"') or pattern_from_llm.startswith("r'"):
            pattern_from_llm = pattern_from_llm[2:-1]
        
        # Validate the pattern by compiling it
        try:
            re.compile(pattern_from_llm)
        except re.error as e:
            print(f"⚠️  LLM returned invalid regex: {pattern_from_llm} — Error: {e}")
            print(f"   Falling back to default pattern.")
            return section_start_number, default_pattern, designator_matches
        
        # Validate the pattern actually matches the designators
        match_count = sum(1 for d in matching_designators if re.search(pattern_from_llm, d))
        match_pct = match_count / len(matching_designators) * 100
        
        print(f"🔧 LLM-built section pattern: {pattern_from_llm}")
        print(f"   Pattern matches {match_count}/{len(matching_designators)} designators ({match_pct:.0f}%)")
        
        if match_pct < 50:
            print(f"⚠️  Pattern matches less than 50% of designators. Falling back to default.")
            return section_start_number, default_pattern, designator_matches
        
        return section_start_number,pattern_from_llm,designator_matches
        
    except Exception as e:
        print(f"⚠️  Error calling Claude for pattern building: {e}")
        print(f"   Falling back to default pattern.")
        return section_start_number, default_pattern, designator_matches
    
       

def extract_designator_pattern_using_llm(pxml_content):
    chapter_number = None
    section_pattern = r'§?\s*\d+[A-Za-z]*[:.][\dA-Za-z]+[^§\n]*'
    designator_matches = []

    if not pxml_content:
        print("⚠️  Empty PXML content received. Using generic section pattern.")
        return chapter_number, section_pattern, designator_matches

    chapter_number, section_pattern, designator_matches = build_section_pattern_from_pxml(pxml_content)
    print(f"Final section regex pattern: {section_pattern}")
    print(f"Chapter number extracted: {chapter_number}")

    return chapter_number, section_pattern, designator_matches