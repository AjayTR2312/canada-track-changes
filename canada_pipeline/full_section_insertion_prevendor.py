import re
import xml.etree.ElementTree as ET
from typing import Optional, Tuple


class PXMLInserter:
    """
    Insert XML sections into PXML files based on designator logic.
    """
    
    def __init__(self, pxml_path: str, updated_pxmlcontent: str, debug: bool = True):
        """
        Initialize the PXML inserter.
        
        Parameters:
        -----------
        pxml_path : str
            Path to the PXML file
        debug : bool
            Enable debug logging (default: True)
        """
        self.pxml_path = pxml_path
        self.debug = debug
        self.pxml_content = updated_pxmlcontent
        self.updated_pxml = None
        
    def _log(self, message: str, level: str = "INFO"):
        """Print debug messages if debug mode is enabled."""
        if self.debug:
            symbols = {
                "INFO": "ℹ",
                "SUCCESS": "✓",
                "WARNING": "⚠",
                "ERROR": "✗",
                "HEADER": "="
            }
            symbol = symbols.get(level, "•")
            
            if level == "HEADER":
                print(f"\n{'='*80}")
                print(f"{message}")
                print(f"{'='*80}")
            else:
                print(f"{symbol} {message}")
    
    def read_pxml(self) -> bool:
        """
        Read PXML file content.
        
        Returns:
        --------
        bool
            True if successful, False otherwise
        """
        try:
            with open(self.pxml_path, 'r', encoding='utf-8') as f:
                self.pxml_content = f.read()
            self._log(f"Successfully read PXML file ({len(self.pxml_content)} characters)", "SUCCESS")
            return True
        except Exception as e:
            self._log(f"ERROR reading PXML file: {e}", "ERROR")
            return False
    
    def write_pxml(self) -> bool:
        """
        Write updated PXML content back to file.
        
        Returns:
        --------
        bool
            True if successful, False otherwise
        """
        if self.updated_pxml is None:
            self._log("No updated PXML content to write", "ERROR")
            return False
        
        try:
            with open(self.pxml_path, 'w', encoding='utf-8') as f:
                f.write(self.updated_pxml)
            self._log("Successfully wrote updated PXML to file", "SUCCESS")
            return True
        except Exception as e:
            self._log(f"ERROR writing to file: {e}", "ERROR")
            return False
    
    @staticmethod
    def cleanup_xml_fences(xml_string: str) -> str:
        """
        Remove markdown code fences and extract clean XML.
        
        Parameters:
        -----------
        xml_string : str
            XML string potentially wrapped in markdown fences
        
        Returns:
        --------
        str
            Cleaned XML string
        """
        # Strip whitespace
        cleaned = xml_string.strip()
        
        # Remove code fences using regex
        cleaned = re.sub(r'^```(?:xml)?\s*\n?', '', cleaned)
        cleaned = re.sub(r'\n?```\s*$', '', cleaned)
        
        # Strip any remaining whitespace
        cleaned = cleaned.strip()
        
        # Validate it starts and ends correctly
        if not cleaned.startswith('<section') and not cleaned.startswith('<editorial.commentary.section'):
            raise ValueError("XML does not start with <section or <editorial.commentary.section")
        if not cleaned.endswith('</section>') and not cleaned.endswith('</editorial.commentary.section>'):
            raise ValueError("XML does not end with </section> or </editorial.commentary.section>")
        
        return cleaned
    
    @staticmethod
    def extract_designator(xml_string: str) -> Optional[str]:
        """
        Extract designator number from XML string.
        
        Parameters:
        -----------
        xml_string : str
            XML string containing designator tag
        
        Returns:
        --------
        str or None
            Designator value or None if not found
        """
        match = re.search(r'<designator[^>]*>(.*?)</designator>', xml_string)
        return match.group(1).strip() if match else None
    
    def find_closing_section_tag(self, start_pos: int) -> int:
        """
        Find the matching closing </section> tag using a more robust approach.
        """
        print(f"\n  DEBUG find_closing_section_tag_robust:")
        print(f"    Starting search from position {start_pos}")
        
        # Find the end of opening tag
        opening_tag_end = self.pxml_content.find('>', start_pos)
        if opening_tag_end == -1:
            print(f"    ✗ Could not find end of opening tag")
            return -1
        
        print(f"    Opening tag: {self.pxml_content[start_pos:opening_tag_end+1]}")
        
        # Use a stack-based approach
        depth = 1
        i = opening_tag_end + 1
        
        while i < len(self.pxml_content) and depth > 0:
            # Look for any '<' character
            next_tag_start = self.pxml_content.find('<', i)
            
            if next_tag_start == -1:
                print(f"    ✗ No more tags found")
                return -1
            
            # Check what kind of tag it is
            # Get the next few characters after '<'
            tag_preview = self.pxml_content[next_tag_start:next_tag_start+20]
            
            if self.pxml_content[next_tag_start:next_tag_start+10] == '</section>':
                # Closing section tag
                depth -= 1
                print(f"    Found </section> at {next_tag_start}, depth = {depth}")
                
                if depth == 0:
                    print(f"    ✓ Found matching closing tag!")
                    return next_tag_start
                
                i = next_tag_start + 10
                
            elif self.pxml_content[next_tag_start:next_tag_start+8] == '<section':
                # Check if it's really a <section> tag (not <section.block etc)
                next_char_pos = next_tag_start + 8
                if next_char_pos < len(self.pxml_content):
                    next_char = self.pxml_content[next_char_pos]
                    if next_char in [' ', '>', '\n', '\t', '\r']:
                        # It's a real <section> tag
                        depth += 1
                        print(f"    Found <section> at {next_tag_start}, depth = {depth}")
                
                i = next_tag_start + 8
                
            else:
                # Some other tag, skip it
                i = next_tag_start + 1
        
        print(f"    ✗ Could not find matching closing tag")
        return -1
    
    # def find_closing_section_tag(self, start_pos: int) -> int:
    #     """
    #     Find the matching closing </section> tag for a <section> starting at start_pos.
    #     Handles nested sections properly.
        
    #     Parameters:
    #     -----------
    #     start_pos : int
    #         Starting position of the opening <section> tag
        
    #     Returns:
    #     --------
    #     int
    #         Position of the closing </section> tag, or -1 if not found
    #     """
    #     depth = 0
    #     pos = start_pos
        
    #     print('pxml_content----------',self.pxml_content)
    #     # Move past the opening <section tag first
    #     pos = self.pxml_content.find('>', pos)
    #     if pos == -1:
    #         return -1
    #     pos += 1
        
    #     while pos < len(self.pxml_content):
    #         # Look for next <section or </section>
    #         open_tag = self.pxml_content.find('<section', pos)
    #         close_tag = self.pxml_content.find('</section>', pos)
            
    #         if close_tag == -1:
    #             return -1  # No closing tag found
            
    #         if open_tag != -1 and open_tag < close_tag:
    #             # Found nested opening tag
    #             depth += 1
    #             pos = open_tag + 8  # Move past '<section'
    #         else:
    #             # Found closing tag
    #             if depth == 0:
    #                 return close_tag
    #             else:
    #                 depth -= 1
    #                 pos = close_tag + 10  # Move past '</section>'
        
    #     return -1
    
    # def parse_designator(self, designator: str) -> Tuple[int, int]:
    #     """
    #     Parse designator string into chapter and section numbers.
        
    #     Parameters:
    #     -----------
    #     designator : str
    #         Designator in format "chapter:section" (e.g., "4:1")
        
    #     Returns:
    #     --------
    #     tuple
    #         (chapter, section) as integers
    #     """
    #     if designator is not None:

    #         if ':' not in designator or '.' not in designator:
    #             return None, None
            
    #         chapter, section = designator.split(':')
    #         chapter = int(chapter)
    #         section = int(section)
            
    #         self._log(f"Parsed designator - Chapter: {chapter}, Section: {section}", "SUCCESS")
    #         return chapter, section
    #     else:
    #         return None, None

    def parse_designator(self, designator: str) -> Tuple:
        """
        Parse designator string into chapter and section numbers.
        
        Parameters:
        -----------
        designator : str
            Designator in format "chapter:section" (e.g., "4:1") 
            or "chapter:section.subsection" (e.g., "7:23.10")
            or "chapter.section" (e.g., "4.1")
        
        Returns:
        --------
        tuple
            (chapter, section) — section is kept as string to preserve "23.10" vs "23.1"
        """
        if designator is None:
            return None, None

        try:
            if ':' in designator:
                chapter_str, section_str = designator.split(':', 1)
            elif '.' in designator:
                chapter_str, section_str = designator.split('.', 1)
            else:
                self._log(f"Designator '{designator}' has no ':' or '.' separator", "ERROR")
                return None, None

            chapter = int(chapter_str)
            # Keep section as string to preserve trailing zeros (e.g., "23.10" != "23.1")
            # Only convert to int if it's a plain integer with no dots
            if '.' not in section_str:
                try:
                    section = int(section_str)
                except ValueError:
                    section = section_str  # e.g. "14A" alpha suffix
            else:
                section = section_str  # e.g. "23.10" — keep as-is

            self._log(f"Parsed designator - Chapter: {chapter}, Section: {section}", "SUCCESS")
            return chapter, section

        except (ValueError, TypeError) as e:
            self._log(f"Failed to parse designator '{designator}': {e}", "ERROR")
            return None, None
    
    def find_all_designators(self) -> list:
        """
        Find all designator tags in PXML content.
        
        Returns:
        --------
        list
            List of regex match objects for designators
        """
        designator_pattern = r'<designator[^>]*>(.*?)</designator>'
        matches = list(re.finditer(designator_pattern, self.pxml_content))
        
        self._log(f"Found {len(matches)} designators in PXML:", "SUCCESS")
        for i, match in enumerate(matches[:10]):  # Show first 10
            self._log(f"  {i+1}. '{match.group(1).strip()}' at position {match.start()}")
        if len(matches) > 10:
            self._log(f"  ... and {len(matches) - 10} more")
        
        return matches
    
    def check_designator_exists(self, target_designator: str, matches: list) -> bool:
        """
        Check if a designator already exists in PXML.
        
        Parameters:
        -----------
        target_designator : str
            Designator to check (e.g., "4:1")
        matches : list
            List of designator matches from find_all_designators()
        
        Returns:
        --------
        bool
            True if exists, False otherwise
        """
        for match in matches:
            if match.group(1).strip() == target_designator:
                self._log(f"WARNING: Designator {target_designator} already exists at position {match.start()}", "WARNING")
                return True
        
        self._log(f"Designator {target_designator} does not exist yet (good)", "SUCCESS")
        return False
    
    def insert_as_first_section(self, cleaned_xml: str) -> bool:
        """
        Insert XML as the first section in section.block.
        
        Parameters:
        -----------
        cleaned_xml : str
            Cleaned XML content to insert
        
        Returns:
        --------
        bool
            True if successful, False otherwise
        """
        self._log("SECTION 1 LOGIC: Inserting as FIRST section in section.block", "HEADER")
        
        # Find <section.block>
        section_block_match = re.search(r'<section\.block[^>]*>', self.pxml_content)
        
        if section_block_match:
            self._log(f"Found <section.block> at position {section_block_match.start()}", "SUCCESS")
            search_start = section_block_match.end()
            
            # Find the first <section or <editorial.commentary.section tag after <section.block>
            first_section_match = re.search(r'<(?:editorial\.commentary\.)?section(?=[\s>])', self.pxml_content[search_start:])
            
            if first_section_match:
                insert_pos = search_start + first_section_match.start()
                self._log(f"Found first section tag at position {insert_pos}", "SUCCESS")
                self._log(f"  Context before insertion:")
                self._log(f"  ...{self.pxml_content[insert_pos-100:insert_pos]}<<<INSERT HERE>>>")
                
                self.updated_pxml = (self.pxml_content[:insert_pos] + 
                                    cleaned_xml + '\n' + 
                                    self.pxml_content[insert_pos:])
                self._log("XML inserted successfully", "SUCCESS")
            else:
                # No section found, insert right after <section.block>
                insert_pos = search_start
                self._log("No existing section tag found, inserting right after <section.block>", "WARNING")
                self.updated_pxml = (self.pxml_content[:insert_pos] + 
                                    '\n' + cleaned_xml + '\n' + 
                                    self.pxml_content[insert_pos:])
            return True
        else:
            self._log("Could not find <section.block> in PXML", "ERROR")
            self.updated_pxml = self.pxml_content + '\n' + cleaned_xml
            return False
    
    def insert_after_previous_section(self, cleaned_xml: str, chapter: int, section: int) -> bool:
        """
        Insert XML after the previous section.
        
        Parameters:
        -----------
        cleaned_xml : str
            Cleaned XML content to insert
        chapter : int
            Chapter number
        section : int
            Section number
        
        Returns:
        --------
        bool
            True if successful, False otherwise
        """
        self._log("SECTION > 1 LOGIC: Looking for previous section to insert after", "HEADER")
        
        designator_matches = self.find_all_designators()
        
        # Build ordered list of candidate previous designators to try
        prev_designators = []
        section_str = str(section)
        
        # Handle sub-section designators like "23.10" — look for parent "23" and siblings
        if '.' in section_str:
            # e.g. section_str = "23.10"
            parent_section, sub_section = section_str.split('.', 1)
            # Try previous sub-sections first (23.9, 23.8, ..., 23.1), then the parent (23)
            try:
                sub_num = int(sub_section)
                for i in range(sub_num - 1, 0, -1):
                    # Try with leading zeros matching (e.g., 23.09, 23.08... or 23.9, 23.8...)
                    if len(sub_section) > 1 and sub_section.startswith('0'):
                        prev_designators.append(f"{chapter}:{parent_section}.{str(i).zfill(len(sub_section))}")
                    prev_designators.append(f"{chapter}:{parent_section}.{i}")
                    # Also try with trailing zero variants (23.10 format)
                    prev_designators.append(f"{chapter}:{parent_section}.{i}0")
            except ValueError:
                pass
            # Finally try the parent section itself
            prev_designators.append(f"{chapter}:{parent_section}")
            # And then sections before the parent
            try:
                parent_num = int(parent_section)
                for i in range(parent_num - 1, 0, -1):
                    prev_designators.append(f"{chapter}:{i}")
            except ValueError:
                pass
        else:
            # Extract numeric prefix and optional alpha suffix (e.g., "1B" -> 1, "B")
            alpha_match = re.match(r'^(\d+)([A-Za-z]+)$', section_str)
            if alpha_match:
                base_num = int(alpha_match.group(1))
                alpha_suffix = alpha_match.group(2)
                # For "1B": try "13:1A" first, then "13:1", then "13:0", ...
                # For "1C": try "13:1B", "13:1A", "13:1", ...
                # Walk backwards through alpha suffixes before this one
                for ch in range(ord(alpha_suffix[0]) - 1, ord('A') - 1, -1):
                    prev_designators.append(f"{chapter}:{base_num}{chr(ch)}")
                # Then try the bare numeric (e.g., "13:1")
                prev_designators.append(f"{chapter}:{base_num}")
                # Then try sections before this number
                for i in range(base_num - 1, 0, -1):
                    prev_designators.append(f"{chapter}:{i}")
            elif isinstance(section, (int, float)):
                for i in range(int(section) - 1, 0, -1):
                    prev_designators.append(f"{chapter}:{i}")
            else:
                self._log(f"Cannot determine previous section for '{section}'", "ERROR")
                return self.insert_as_first_section(cleaned_xml)
        
        found_previous = False
        
        for prev_designator in prev_designators:
            self._log(f"Searching for previous section: {prev_designator}")
            
            for match in designator_matches:
                if match.group(1).strip() == prev_designator:
                    self._log(f"Found previous section {prev_designator} at position {match.start()}", "SUCCESS")
                    found_previous = True
                    
                    # Find the <section or <editorial.commentary.section tag before this designator
                    section_start = self.pxml_content.rfind('<editorial.commentary.section', 0, match.start())
                    if section_start == -1:
                        section_start = self.pxml_content.rfind('<section', 0, match.start())
                    self._log(f"  Section starts at position {section_start}")
                    
                    # Find the closing tag for this section
                    section_end = self.find_closing_section_tag(section_start)
                    
                    if section_end != -1:
                        # Determine the correct closing tag length
                        if self.pxml_content[section_start:section_start+30].startswith('<editorial.commentary.section'):
                            close_tag_len = len('</editorial.commentary.section>')
                        else:
                            close_tag_len = len('</section>')
                        insert_pos = section_end + close_tag_len
                        self._log(f"  Section ends at position {section_end}")
                        self._log(f"  Will insert at position {insert_pos}")
                        self._log(f"  Context around insertion:")
                        self._log(f"  ...{self.pxml_content[insert_pos-100:insert_pos]}<<<INSERT HERE>>>{self.pxml_content[insert_pos:insert_pos+100]}...")
                        
                        self.updated_pxml = (self.pxml_content[:insert_pos] + 
                                            '\n' + cleaned_xml + 
                                            self.pxml_content[insert_pos:])
                        
                        return True
                    else:
                        self._log("Could not find closing section tag", "ERROR")
            
            if found_previous:
                break
            
            self._log(f"  Not found, trying next candidate...")
        
        if not found_previous:
            self._log(f"WARNING: No previous section found for {chapter}:{section}", "WARNING")
            self._log("  Falling back to inserting as first section in section.block")
            return self.insert_as_first_section(cleaned_xml)
        
        return False
    
    def insert_xml_by_designator(self, designator: str, input_xml_string: str, write_to_file: bool = False) -> Tuple[Optional[str], Optional[str]]:
        """
        Insert XML content into PXML file based on designator logic.
        
        Logic:
        - For section 1 (e.g., 4:1): Insert as the very first section in section.block
        - For section > 1 (e.g., 4:2, 4:3, etc.): Look for previous section (section-1) 
          and insert after that section's closing </section> tag
        
        Parameters:
        -----------
        designator : str
            Target designator (e.g., "4:1")
        input_xml_string : str
            XML content to insert
        write_to_file : bool
            Whether to write changes to file (default: False)
        
        Returns:
        --------
        tuple
            (updated_pxml_content, designator_number) or (None, None) on error
        """
        self._log(f"Starting insertion for designator: {designator}", "HEADER")
        
        # # Read PXML file
        # if not self.read_pxml():
        #     return None, None
        
        # Clean up input XML
        try:
            cleaned_xml = self.cleanup_xml_fences(input_xml_string)
            self._log(f"Cleaned XML ({len(cleaned_xml)} characters)", "SUCCESS")
            self._log(f"  First 500 chars: {cleaned_xml[:500]}...")
        except Exception as e:
            self._log(f"ERROR cleaning XML: {e}", "ERROR")
            return None, None
        
        # Extract designator from cleaned XML
        designator_number = self.extract_designator(cleaned_xml)
        self._log(f"Extracted designator from XML: {designator_number}", "SUCCESS")
        
        # Parse the designator
        try:
            chapter, section = self.parse_designator(designator)
        except ValueError as e:
            self._log(str(e), "ERROR")
            return None, None
        if chapter is None or section is None:
            self._log("Invalid designator format. Expected 'chapter:section'", "ERROR")
            return None, None
        target_designator = f"{chapter}:{section}"
        
        # Find all designators and check if target exists
        designator_matches = self.find_all_designators()
        self.check_designator_exists(target_designator, designator_matches)
        
        # Insert based on section number
        # Only insert as first section if designator is exactly "X:1" (integer 1, no suffix)
        is_first_section = (isinstance(section, int) and section == 1)
        success = False
        if is_first_section:
            success = self.insert_as_first_section(cleaned_xml)
        else:
            success = self.insert_after_previous_section(cleaned_xml, chapter, section)
        
        if not success:
            self._log("Insertion failed", "ERROR")
            return None, None
        
        # Write to file if requested
        if write_to_file:
            if not self.write_pxml():
                return None, None
        
        self._log("FINAL RESULT:", "HEADER")
        self._log(f"Designator: {designator_number}")
        self._log(f"Success: {self.updated_pxml is not None}")
        
        return self.updated_pxml, designator_number


# -----------------------------------------------------
# CONVENIENCE FUNCTION FOR EXTERNAL USE
# -----------------------------------------------------

# def insert_xml_into_pxml(designator: str, 
#                          xml_content: str, 
#                          pxml_path: str, 
#                          write_to_file: bool = False,
#                          debug: bool = True) -> Tuple[Optional[str], Optional[str]]:
#     """
#     Insert XML content into PXML file based on designator.
    
#     Parameters:
#     -----------
#     designator : str
#         Target designator (e.g., "4:1")
#     xml_content : str
#         XML content to insert
#     pxml_path : str
#         Path to PXML file
#     write_to_file : bool
#         Whether to write changes to file (default: False)
#     debug : bool
#         Enable debug logging (default: True)
    
#     Returns:
#     --------
#     tuple
#         (updated_pxml_content, designator_number) or (None, None) on error
#     """
#     inserter = PXMLInserter(pxml_path, debug=debug)
#     return inserter.insert_xml_by_designator(designator, xml_content, write_to_file)


# -----------------------------------------------------
# EXAMPLE USAGE
# -----------------------------------------------------

