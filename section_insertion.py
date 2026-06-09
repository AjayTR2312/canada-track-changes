from time import time

import pandas as pd
import re
import anthropic
from typing import List, Dict, Optional
from pathlib import Path
import httpx
from config import MODEL_OPUS
import time
from config import API_KEY,build_anthropic_client
from xml_entity_rules import XML_ENTITY_RULES, CASE_CITATION_RULES, URL_TAGGING_RULES





class SectionExtractor:
    """
    Extract and clean inserted sections from DOCX tracked changes.
    """
    
    def __init__(self, pxmlpath,api_key: Optional[str] = None):
        """
        Initialize the SectionExtractor.
        
        Parameters:
        -----------
        api_key : str, optional
            Anthropic API key for Claude. If None, uses default key.
        """
        self.api_key = api_key or API_KEY
        self.client = build_anthropic_client()
        self.sections = []
        self.cleaned_sections = []
        self.pxmlpath=pxmlpath
        self.final_xml_content=None
        self.canada_prompt_text = self._load_canada_pamphlets_prompt()

    def _load_canada_pamphlets_prompt(self) -> str:
        """Load the Canada pamphlets tagging prompt from workspace root."""
        prompt_path = Path(__file__).resolve().parent / "Canada_Pamphlets_Prompt 1.md"
        try:
            return prompt_path.read_text(encoding="utf-8").strip()
        except Exception as e:
            print(f"⚠️  Could not load Canada pamphlets prompt from {prompt_path}: {e}")
            return ""

    def find_chapter_continuation(self,df, chapter_number):
        """
        Finds paragraphs starting with a SPECIFIC chapter pattern (e.g., '3:1', '3:2') 
        followed by alphabetic text, and tracks their continuation until another section 
        of the SAME chapter is found.

        Parameters:
        df: DataFrame with columns 'paragraph_index' and 'inserted_text'
        chapter_number: The specific chapter number to search for (e.g., 3)

        Returns:
        List of dictionaries with chapter section details
        """
        # df.to_excel('debug_find_chapter_continuation.xlsx', index=False)  # Debug output to inspect the DataFrame
        # Pattern to match the specified chapter sections like "3:1", "3:2", etc.
        # MUST be followed by alphabetic text (words with letters)
        # specific_chapter_pattern = rf'§?\s*{chapter_number}[:.\s]\s*[\dA-Za-z]+\s+[A-Za-z]+'
        specific_chapter_pattern = rf'^(?:§\s*)?{chapter_number}:\s*\d+(?:\.\d+)?\s+[A-Za-z]+'
        results = []

        # Sort by paragraph_index to ensure order
        df_sorted = df.sort_values('paragraph_index').reset_index(drop=True)

        for idx, row in df_sorted.iterrows():
            inserted_text = str(row['text_after']).strip()
            # Strip XML/HTML tags for pattern matching
            inserted_text_clean = re.sub(r'<[^>]+>', '', inserted_text).strip()
            
            # Check if this paragraph contains the SPECIFIC chapter pattern with alphabetic text
            match = re.search(specific_chapter_pattern, inserted_text_clean)
            
            if match:
                start_index = row['paragraph_index']
                print('start_index:', start_index)
                # Extract just the section marker (§ 3:1) without the following text
                chapter_section = re.search(rf'§?\s*{chapter_number}[:.\s]\s*[\dA-Za-z]+', inserted_text_clean).group()
                print('chapter_section:', chapter_section)
                # Initialize end as start
                end_index = start_index
                last_valid_index = idx
                
                # Look ahead for continuation (max gap of 5)
                for next_idx in range(idx + 1, len(df_sorted)):
                    next_para_index = df_sorted.loc[next_idx, 'paragraph_index']
                    next_text = str(df_sorted.loc[next_idx, 'text_after']).strip()
                    next_text_clean = re.sub(r'<[^>]+>', '', next_text).strip()
                    
                    # Check if next paragraph contains a NEW section of the SAME chapter
                    # (with alphabetic text following it)
                    if re.search(specific_chapter_pattern, next_text_clean):
                        # New section of the SAME chapter found, stop BEFORE this
                        break
                    
                    # Calculate gap from the last valid index we found
                    gap = next_para_index - end_index
                    
                    # If gap is more than 5, break
                    if gap > 5:
                        break
                    
                    # Update end_index and last_valid_index
                    end_index = next_para_index
                    last_valid_index = next_idx
                
                results.append({
                    'chapter_section': chapter_section,
                    'start_paragraph_index': start_index,
                    'end_paragraph_index': end_index,
                    'start_df_index': idx,
                    'end_df_index': last_valid_index
                })

        return results

    def extract_inserted_sections(self, inline_insertion_df, chapter_number):
        """
        Extract complete sections that were inserted based on chapter number pattern.
        
        Parameters:
        - inline_insertion_df: DataFrame with columns ['type', 'paragraph_index', 'deleted_text', 'inserted_text']
        - chapter_number: The chapter number to search for (e.g., 4)
        
        Returns:
        - List of dictionaries containing section information and content
        """
        
        # Get chapter continuations using the working function
        chapter_results = self.find_chapter_continuation(inline_insertion_df, chapter_number)
        print('chapter_results------',chapter_results)
        sections_found = []
        
        for result in chapter_results:
            para_index = result['start_paragraph_index']
            check_index = para_index + 20
            
            # Count records below check_index (from paragraph_index 9 onwards)
            count = len(inline_insertion_df[
                (inline_insertion_df['paragraph_index'] >= para_index) & 
                (inline_insertion_df['paragraph_index'] < check_index)
            ])
            
            # Confirmation: count > 1 means complete section inserted
            if count > 0:
                print(f"✓ Complete section found at paragraph_index {para_index}")
                
                # Extract section content from the range we already found
                section_content = []
                bullet_points = []
                
                # Get the rows from start to end index
                section_rows = inline_insertion_df[
                    (inline_insertion_df['paragraph_index'] >= result['start_paragraph_index']) &
                    (inline_insertion_df['paragraph_index'] <= result['end_paragraph_index'])
                ].sort_values('paragraph_index')
                
                for _, row in section_rows.iterrows():
                    if pd.notna(row['text_after']):
                        has_bullet_point = bool(
                            row.get('has_bullet_point', row.get('has_bullet_point_x', False))
                        )
                        section_content.append({
                            'paragraph_index': row['paragraph_index'],
                            'text': row['text_after'],
                            'has_bullet_point': has_bullet_point
                        })
                        if has_bullet_point:
                            bullet_points.append(str(row['text_after']).strip())
                
                # Compile section information
                section_info = {
                    'section_header': result['chapter_section'],
                    'start_paragraph_index': result['start_paragraph_index'],
                    'end_paragraph_index': result['end_paragraph_index'],
                    'start_df_index': result['start_df_index'],
                    'end_df_index': result['end_df_index'],
                    'content': section_content,
                    'bullet_points': bullet_points,
                    'full_text': '\n'.join([item['text'] for item in section_content])
                }
                
                sections_found.append(section_info)
        self.sections=sections_found
        return sections_found
        
    
    
    def clean_section_duplicates(self, section_text: str) -> str:
        """
        Remove duplicate text from section content using Claude Opus 4.1.
        
        Parameters:
        -----------
        section_text : str
            The text containing duplicates
        
        Returns:
        --------
        str
            Cleaned text with duplicates removed
        """
        
        # Create the prompt
        prompt = f"""You are tasked with cleaning up duplicate text from a legal document section. The text appears to have duplicates where the same content is repeated side by side or in close proximity.

Here is the text that needs cleaning:

{section_text}

Please:
1. Identify and remove all duplicate content
2. Keep only one instance of each unique piece of information
3. Maintain the original structure and formatting
4. Preserve the logical flow and order of information
5. Do not add any new content or explanations
6. Return only the cleaned text without any preamble or commentary

Cleaned text:"""
        
        # Call Claude Opus
        message = self.client.messages.create(
            model=MODEL_OPUS,
            max_tokens=10000,
            temperature=0,  # Use 0 for consistent, deterministic output
            stream=True,
            messages=[
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        )
        
        cleaned_text = ""
        for event in message:
            if event.type == "content_block_delta":
                if hasattr(event.delta, "text"):
                    cleaned_text += event.delta.text
        
        return cleaned_text.strip()
    
    def clean_multiple_sections(self, sections_list: Optional[List[Dict]] = None) -> List[Dict]:
        """
        Clean multiple sections in batch.
        
        Parameters:
        -----------
        sections_list : list, optional
            List of section dictionaries. If None, uses self.sections
        
        Returns:
        --------
        list
            List of sections with cleaned text
        """
        
        # if sections_list is None:
        #     sections_list = self.sections
        
        cleaned_sections = []
        
        for i, section in enumerate(sections_list, 1):
            print(f"Cleaning section {i}/{len(sections_list)}: {section['section_header']}")
            
            # Get the full text
            original_text = section['full_text']
            
            # Only clean if text is substantial
            if len(original_text) > 50:
                # Clean it
                cleaned_text = self.clean_section_duplicates(original_text)
                
                # Create updated section
                cleaned_section = section.copy()
                cleaned_section['original_text'] = original_text
                cleaned_section['cleaned_text'] = cleaned_text
                
                cleaned_sections.append(cleaned_section)
            else:
                # Keep original if too short
                cleaned_section = section.copy()
                cleaned_section['original_text'] = original_text
                cleaned_section['cleaned_text'] = original_text
                cleaned_sections.append(cleaned_section)
        
        # self.cleaned_sections = cleaned_sections
        return cleaned_sections
    
    def process(self, inline_insertion_df: pd.DataFrame, chapter_number: int) -> Dict:
        """
        Complete pipeline: extract and clean sections.
        
        Parameters:
        -----------
        inline_insertion_df : pd.DataFrame
            DataFrame with tracked changes
        chapter_number : int
            Chapter number to search for
        
        Returns:
        --------
        dict
            Dictionary containing:
            - sections: Raw extracted sections
            - cleaned_sections: Sections with duplicates removed
        """
        
        print(f"Extracting sections for chapter {chapter_number}...")
        sections = self.extract_inserted_sections(inline_insertion_df, chapter_number)
        print(f"Found {len(sections)} sections\n")
        
        print("Cleaning sections...")
        cleaned_sections = self.clean_multiple_sections(sections)
        print(f"Cleaned {len(cleaned_sections)} sections\n")
        
        return {
            'sections': sections,
            'cleaned_sections': cleaned_sections
        }
    
    def getting_contextxml(self) -> str:
        """
        Reads an XML file and returns the first <section uuid=...>...</section> element as a string.
        
        Args:
            xml_file_path: Path to the XML file
            
        Returns:
            str: The first complete <section uuid=...>...</section> element found in the file
        """
        
        # Read the XML file
        with open(self.pxmlpath, 'r', encoding='utf-8') as file:
            xml_content = file.read()
        
        # Find the first <section uuid= tag
        start_tag = '<section uuid='
        end_tag = '</section>'
        
        # Find the start position of <section uuid=
        start_pos = xml_content.find(start_tag)
        
        # If no <section uuid= found, try <section followed by space (handles <section date.revised=... etc.)
        if start_pos == -1:
            start_pos = xml_content.find('<section ')
        
        # If still no match, try <editorial.commentary.section> tag format
        if start_pos == -1:
            start_pos = xml_content.find('<editorial.commentary.section ')
            if start_pos == -1:
                start_pos = xml_content.find('<editorial.commentary.section>')
            end_tag = '</editorial.commentary.section>'
        
        end_pos = xml_content.find(end_tag)
        print('start_pos------',start_pos)
        print('end_pos------',end_pos)
        self.final_xml_content=xml_content[start_pos:end_pos]+'</section>'
        return self.final_xml_content
    
    

    def convert_text_to_xml_tags(self, xml_context: str, plain_text: str, bullet_points: Optional[List[str]] = None, section_header: Optional[str] = None) -> str:
        """
        Converts plain text into XML tags using the provided XML context as a template.
        
        Args:
            xml_context: Sample XML string showing the desired tag structure
            plain_text: Plain text to be converted into XML format
            section_header: Optional section header hint (e.g. '§ 14:12 Title')
            
        Returns:
            str: The plain text converted to XML format following the context structure
        """
        
        bullet_points = bullet_points or []
        has_bullets = len(bullet_points) > 0
        bullet_points_block = "\n".join([f"- {bp}" for bp in bullet_points]) if has_bullets else "(none)"

        # Extract designator hint from section_header
        designator_hint = None
        section_name_hint = ""
        if section_header:
            m = re.search(r'§?\s*([0-9A-Za-z:.\-]+)', str(section_header))
            if m:
                designator_hint = m.group(1).strip()
            section_name_hint = re.sub(r'^\s*§?\s*[0-9A-Za-z:.\-]+\s*', '', str(section_header)).strip()

        base_prompt = self.canada_prompt_text or "You are an XML formatting expert."

        prompt = f"""{base_prompt}

INPUT FOR THIS CONVERSION TASK:

SAMPLE XML CONTEXT (for structural consistency with target file):
{xml_context}

PLAIN TEXT TO CONVERT:
{plain_text}

BULLET POINTS DETECTED (authoritative):
{bullet_points_block}

SECTION HEADER HINT: {section_header or '(none)'}
SECTION DESIGNATOR HINT: {designator_hint or '(none)'}

ADDITIONAL PIPELINE RULES:
- This is for section insertion output expected as a <section>...</section> block.
- OUTPUT CONTRACT (MANDATORY): Return exactly one root <section> element.
- Output must start with <section and end with </section>.
- Include <section.front> with designator and name from the hints above.
- Put converted content inside <section.body>...</section.body>.
- Prefer the Canada Pamphlets instructions above for tagging decisions.
- Preserve exact word order and punctuation from input text.

{XML_ENTITY_RULES}

{CASE_CITATION_RULES}

{URL_TAGGING_RULES}

Return ONLY valid XML output for the converted content, with no explanation or markdown fences."""

        max_retries = 3

        for attempt in range(max_retries):
            try:
                xml_output = ""

                with self.client.messages.stream(
                    model=MODEL_OPUS,
                    max_tokens=32000,
                    temperature=0,
                    messages=[
                        {
                            "role": "user",
                            "content": prompt
                        }
                    ]
                ) as stream:
                    for text in stream.text_stream:
                        xml_output += text

                return xml_output.strip()

            except httpx.RemoteProtocolError as e:
                if attempt < max_retries - 1:
                    wait = 2 ** attempt  # 1s, 2s, 4s
                    print(f"[Attempt {attempt + 1}] Stream interrupted, retrying in {wait}s... ({e})")
                    time.sleep(wait)
                else:
                    print(f"[Attempt {attempt + 1}] Stream failed after {max_retries} attempts.")
                    raise

            except anthropic.APIStatusError as e:
                # Non-retryable API errors (e.g. 400 bad request, 401 auth)
                print(f"API error (not retrying): {e.status_code} - {e.message}")
                raise

            except anthropic.APIConnectionError as e:
                # Network-level connection failure
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    print(f"[Attempt {attempt + 1}] Connection error, retrying in {wait}s... ({e})")
                    time.sleep(wait)
                else:
                    raise

            except (httpx.ReadTimeout, httpx.TimeoutException) as e:
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    print(f"[Attempt {attempt + 1}] Read timeout, retrying in {wait}s... ({e})")
                    time.sleep(wait)
                else:
                    print(f"[Attempt {attempt + 1}] Read timeout after {max_retries} attempts.")
                    raise
    
   
    # def convert_text_to_xml_tags(self,xml_context: str, plain_text: str) -> str:
    #     """
    #     Converts plain text into XML tags using the provided XML context as a template.
        
    #     Args:
    #         xml_context: Sample XML string showing the desired tag structure
    #         plain_text: Plain text to be converted into XML format
            
    #     Returns:
    #         str: The plain text converted to XML format following the context structure
    #     """
        
        
    #     prompt = f"""You are an XML formatting expert. I will provide you with:
    # 1. A sample XML document showing the structure and tags to use
    # 2. Plain text that needs to be converted into the same XML format

    # Your task is to convert the plain text into properly formatted XML following the exact structure, tag names, and attributes shown in the sample XML.

    # SAMPLE XML CONTEXT:
    # {xml_context}

    # PLAIN TEXT TO CONVERT:
    # {plain_text}

    # Please convert the plain text into XML format following the structure shown in the sample. Use appropriate tags, attributes, and nesting based on the content type. 
    
    # IMPORTANT: When creating section and designator tags, always add date.trace="1" attribute to them. For example:
    # <section uuid="" date.revised="0" date.updated="0" date.trace="1">
    # <designator date.updated="0" >7:7</designator>

    # **date.trace is not required for designator as section uuid we are already giving date.trace.
    # **lets keep uuid empty for now as we will fill it later.
    
    # Return ONLY the XML output, no explanations."""

    #     # Use streaming to handle potential timeout issues
    #     response = self.client.messages.create(
    #         model="claude-opus-4-20250514",  # Claude Opus 4.1
    #         max_tokens=32000,
    #         temperature=0,  # Deterministic output
    #         stream=True,
    #         messages=[
    #             {
    #                 "role": "user",
    #                 "content": prompt
    #             }
    #         ]
    #     )
        
    #     # Collect the streamed response
    #     xml_output = ""
    #     for event in response:
    #         if event.type == "content_block_delta":
    #             if hasattr(event.delta, "text"):
    #                 xml_output += event.delta.text
        
    #     return xml_output.strip()

    def cleanup_xml_fences(self, tagged_xml, fallback_designator: Optional[str] = None):
        """
        Remove markdown code fences and extract clean XML.
        Returns tuple: (cleaned_xml, designator_number)
        If the LLM returned bare paragraphs instead of a <section>, wraps them automatically.
        """
        # Strip whitespace
        cleaned = tagged_xml.strip()

        # Remove code fences using regex
        cleaned = re.sub(r'^```(?:xml)?\s*\n?', '', cleaned)
        cleaned = re.sub(r'\n?```\s*$', '', cleaned)
        cleaned = cleaned.strip()

        # If model returned body fragments instead of a full section, wrap safely.
        if not cleaned.startswith('<section'):
            designator_number = None
            if fallback_designator:
                m = re.search(r'§?\s*([0-9A-Za-z:.\-]+)', str(fallback_designator))
                if m:
                    designator_number = m.group(1).strip()

            if not designator_number:
                m = re.search(r'&sect;\s*([0-9A-Za-z:.\-]+)|§\s*([0-9A-Za-z:.\-]+)', cleaned)
                if m:
                    designator_number = (m.group(1) or m.group(2) or '').strip()

            if not designator_number:
                raise ValueError("XML does not start with <section and no fallback designator found")

            print(f"  [WRAP] LLM returned bare fragments — wrapping in <section> with designator '{designator_number}'")
            cleaned = (
                f'<section date.revised="0" date.updated="0" date.trace="1">'
                f'<section.front><outline.name.block><label>&sect;</label>'
                f'<designator date.updated="0">{designator_number}</designator>'
                f'<name></name></outline.name.block></section.front>'
                f'<section.body>{cleaned}</section.body></section>'
            )

        if not cleaned.endswith('</section>'):
            raise ValueError("XML does not end with </section>")

        # Extract designator number
        designator_match = re.search(r'<designator[^>]*>(.*?)</designator>', cleaned)
        designator_number = designator_match.group(1) if designator_match else None
        
        return cleaned, designator_number


#
     
# -----------------------------------------------------
# CONVENIENCE FUNCTION FOR EXTERNAL USE
# -----------------------------------------------------

# def process_sections(inline_insertion_df: pd.DataFrame, 
#                      chapter_number: int, 
#                      api_key: Optional[str] = None) -> Dict:
#     """
#     Extract and clean inserted sections from tracked changes.
    
#     Parameters:
#     -----------
#     inline_insertion_df : pd.DataFrame
#         DataFrame with columns ['type', 'paragraph_index', 'deleted_text', 'inserted_text']
#     chapter_number : int
#         The chapter number to search for (e.g., 4)
#     api_key : str, optional
#         Anthropic API key for Claude
    
#     Returns:
#     --------
#     dict
#         Dictionary containing:
#         - sections: Raw extracted sections
#         - cleaned_sections: Sections with duplicates removed
#     """
    
#     extractor = SectionExtractor(api_key=api_key)
#     return extractor.process(inline_insertion_df, chapter_number)

 
 