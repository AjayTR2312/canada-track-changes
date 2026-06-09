import anthropic
import json
import pandas as pd
import re
import httpx
from config import build_anthropic_client, MODEL_SONNET
from filling_section_pxml import process_pxml_replacements


class TextCleaner:
    """Handles text cleaning operations using Claude API."""
    
    def __init__(self, api_key, pxml_path):
        """
        Initialize the TextCleaner with API credentials.
        
        Args:
            api_key (str): Anthropic API key
        """
        self.api_key = api_key
        self.client = build_anthropic_client()
        self.pxml_path = pxml_path
    
    def clean_texts(self, text_list):
        """
        Clean a list of texts by removing duplicates and fixing spacing.
        
        Args:
            text_list (list): List of text strings to clean
            
        Returns:
            list: Cleaned text list, or original list if error occurs
        """
        prompt = self._build_cleaning_prompt(text_list)
        
        try:
            response_text = self._call_api(prompt)
            cleaned_list = self._parse_response(response_text)
            
            if not self._validate_response(cleaned_list, text_list):
                return text_list
            
            return cleaned_list
        
        except json.JSONDecodeError as e:
            print(f"JSON decode error: {e}")
            print(f"Response was: {response_text[:200]}")
            return text_list
        except Exception as e:
            print(f"Error during cleaning: {e}")
            return text_list
    
    def _build_cleaning_prompt(self, text_list):
        """Build the prompt for text cleaning."""
        return f"""Clean these texts by removing duplicates and fixing spacing. Return a JSON array in the same order.

Rules:
1. Remove duplicate words (e.g., "trial trial" → "trial")
2. Fix split characters (e.g., "Form s" → "Forms")
3. Remove duplicate numbers/dates (e.g., "2024 202 4" → "2024")
4. Keep empty strings as empty strings
5. Return EXACTLY {len(text_list)} items

Input: {json.dumps(text_list)}

Output (JSON array only):"""
    
    def _call_api(self, prompt):
        """Make API call to Claude."""
        message = self.client.messages.create(
            model=MODEL_SONNET,
            max_tokens=8000,
            temperature=0,
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text.strip()
    
    def _parse_response(self, response_text):
        """Parse JSON response from API."""
        # Remove markdown code blocks if present
        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0].strip()
        elif "```" in response_text:
            response_text = response_text.split("```")[1].split("```")[0].strip()
        
        return json.loads(response_text)
    
    def _validate_response(self, cleaned_list, original_list):
        """Validate that response has correct length."""
        if len(cleaned_list) != len(original_list):
            print(f"Warning: Length mismatch. Expected {len(original_list)}, got {len(cleaned_list)}")
            return False
        return True


class SectionLineReplacement:
    """Handles section line replacement operations on DataFrames."""
    
    def __init__(self, inlinereplacement_df, api_key,pxmlpath):
        """
        Initialize with DataFrame and API key.
        
        Args:
            inlinereplacement_df (pd.DataFrame): DataFrame with inline replacement data
            api_key (str): Anthropic API key
        """
        self.inlinereplacement_df = inlinereplacement_df
        self.pxmlpath=pxmlpath
        self.api_key = api_key
        self.text_cleaner = TextCleaner(api_key,pxmlpath)
    
    # def process_dataframe(self, df_changes):
    #     """
    #     Process the DataFrame: clean texts and merge with changes.
        
    #     Args:
    #         df_changes (pd.DataFrame): DataFrame with change context
            
    #     Returns:
    #         pd.DataFrame: Processed DataFrame
    #     """
    #     # Clean inserted texts
    #     text_list = self.inlinereplacement_df['inserted_text'].fillna('').tolist()
    #     print("Original texts:", text_list)
        
    #     cleaned_list = self.text_cleaner.clean_texts(text_list)
    #     self.inlinereplacement_df['inserted_text'] = cleaned_list
    #     self.inlinereplacement_df['inserted_text'] = (
    #         self.inlinereplacement_df['inserted_text'].replace('', pd.NA)
    #     )
    #     print('--brefore------',self.inlinereplacement_df.columns)
    #     # Merge with changes DataFrame
    #     self.inlinereplacement_df = self._merge_with_changes(df_changes)
    #     print('--after------',self.inlinereplacement_df.columns)
        
    #     return self.inlinereplacement_df
    
    def process_dataframe(self, df_changes):
        """
        Process the DataFrame: ant texts and merge with changes.
        """
        # Clean inserted texts
        text_list = self.inlinereplacement_df['inserted_text'].fillna('').tolist()
        print("Original texts:", text_list)

        cleaned_list = self.text_cleaner.clean_texts(text_list)
        self.inlinereplacement_df['inserted_text'] = cleaned_list
        self.inlinereplacement_df['inserted_text'] = (
            self.inlinereplacement_df['inserted_text'].replace('', pd.NA)
        )

        # Drop prev_context and post_context columns if present
        for col in ['prev_context', 'post_context']:
            if col in self.inlinereplacement_df.columns:
                self.inlinereplacement_df = self.inlinereplacement_df.drop(columns=[col])

        print('--brefore------', self.inlinereplacement_df.columns)
        # Merge with changes DataFrame
        self.inlinereplacement_df = self._merge_with_changes(df_changes)
        print('--after------', self.inlinereplacement_df.columns)

        return self.inlinereplacement_df
    
    def _merge_with_changes(self, df_changes):
        """Merge replacement data with change context."""
        merged_df = self.inlinereplacement_df.merge(
            df_changes[['paragraph_index', 'deleted_text', 'prev_context', 'post_context']],
            on=['paragraph_index', 'deleted_text'],
            how='left'
        )
        
        # Rename columns
        merged_df = merged_df.rename(columns={
            'prev_context': 'prev_words',
            'post_context': 'post_words'
        })
        
        return merged_df
    
    # def _merge_with_changes(self, df_changes):
    #     # Merge only the columns you need from df_changes
    #     merged_df = self.inlinereplacement_df.merge(
    #         df_changes[['paragraph_index', 'deleted_text', 'prev_context', 'post_context']],
    #         on=['paragraph_index', 'deleted_text'],
    #         how='left'
    #     )
    #     # Rename merged columns to prev_words and post_words
    #     merged_df = merged_df.rename(columns={
    #         'prev_context': 'prev_words',
    #         'post_context': 'post_words'
    #     })
    #     # Drop any columns from the original DataFrame that have the same name
    #     for col in ['prev_context', 'post_context']:
    #         if col in merged_df.columns:
    #             merged_df = merged_df.drop(columns=[col])
    #     return merged_df
    
    def create_deletion_column(self,chapter_num,updated_pxmlcontent,missing_revisions_output):
        """
        Create 'what_got_deleted' column using text_before from the DataFrame.
        
        For rows with sections matching the chapter number:
        - Use text_before (the original text before changes) directly
        - Store in 'what_got_deleted' column
        
        Args:
            chapter_num (int): Chapter number to match
            
        Returns:
            tuple: (updated_content, result_df, replaced_indices)
        """
        result_df = self.inlinereplacement_df.copy().reset_index(drop=True)
        result_df['what_got_deleted'] = None
        # OLD: section_pattern = rf'^\s*§\s*{chapter_num}[:.\s][\dA-Za-z]+'
        section_pattern = rf'§?\s*{chapter_num}[:.][\dA-Za-z]+(?:\.[\dA-Za-z]+)*\.?'
        replaced_indices = []
        for i in range(len(result_df)):
            text = str(result_df.iloc[i]['text']) if pd.notna(result_df.iloc[i]['text']) else ''
            
            # Check if text contains a section matching the chapter
            section_match = re.search(section_pattern, text)
            if section_match:
                text_before = str(result_df.iloc[i]['text_before']) if pd.notna(result_df.iloc[i].get('text_before')) else ''
                result_df.iloc[i, result_df.columns.get_loc('what_got_deleted')] = text_before
                
                # Only mark as replaced if the designator can actually be extracted
                # (otherwise process_pxml_replacements will skip it with "Could not extract designator")
                from filling_section_pxml import extract_designator_from_content
                extracted_desig = extract_designator_from_content(text_before)
                if extracted_desig:
                    replaced_indices.append(result_df.iloc[i]['paragraph_index'])
                else:
                    print(f"  ⚠️  Row {i}: designator not extractable from text_before, will NOT mark as replaced")
                
                print(f"\nRow {i}:")
                print(f"  Original text (after change): {text}")
                print(f"  Found section: '{section_match.group(0)}'")
                print(f"  What got deleted (text_before): {text_before}")
        
        updated_content = process_pxml_replacements(result_df, updated_pxmlcontent, self.api_key,missing_revisions_output, verbose=True)
        return updated_content,result_df, replaced_indices
    
    def _extract_row_data(self, df, index):
        """Extract and clean row data."""
        text = str(df.iloc[index]['text']) if pd.notna(df.iloc[index]['text']) else ''
        deleted_text = str(df.iloc[index]['deleted_text']) if pd.notna(df.iloc[index]['deleted_text']) else ''
        inserted_text = str(df.iloc[index]['inserted_text']) if pd.notna(df.iloc[index]['inserted_text']) else ''
        
        # Clean duplicate inserted_text (e.g., "3 3" -> "3")
        inserted_text_clean = self._clean_duplicate_text(inserted_text, index)
        
        return {
            'text': text,
            'deleted_text': deleted_text,
            'inserted_text': inserted_text_clean
        }
    
    def _clean_duplicate_text(self, text, row_index):
        """Remove duplicate words from text (e.g., '1 1' -> '1')."""
        text_clean = text.strip()
        if text_clean != '':
            parts = text_clean.split()
            if len(parts) > 1 and len(set(parts)) == 1:
                text_clean = parts[0]
                print(f"Row {row_index}: Cleaned duplicate text '{text}' -> '{text_clean}'")
        return text_clean
    
    def _replace_section_number(self, text, full_section, chapter_num, deleted_text):
        """Replace section number in text."""
        replacement_section = re.sub(
            rf'(§?\s*{chapter_num}[:.\s])[\dA-Za-z]+',
            rf'\g<1>{deleted_text}',
            full_section
        )
        return text.replace(full_section, replacement_section)
    
    def _print_replacement_info(self, row_index, row_data, full_section, edited_text):
        """Print information about the replacement."""
        print(f"\nRow {row_index}:")
        print(f"  Original text: {row_data['text']}")
        print(f"  Found section: '{full_section}'")
        print(f"  Inserted: '{row_data['inserted_text']}' -> Deleted: '{row_data['deleted_text']}'")
        print(f"  What got deleted: {edited_text}")


# ============================================================================
# USAGE EXAMPLE
# ============================================================================




