import pandas as pd
import numpy as np
import re



def remaining_insertions_df(inline_insertion_df,sections):
    df=inline_insertion_df.copy()
    for sec in sections:
        start = sec['start_paragraph_index']
        end = sec['end_paragraph_index']
        df = exclude_by_idx_range(df, start, end)
    return df

def exclude_by_idx_range(df, start, end):
    return df[~df['idx'].between(start, end)]



def create_edgecase_section_dict(df, chapter_number, result_with_ba=None):
    """
    Creates section dictionaries for a given chapter number.
    
    Args:
        df: inline_insertions_df dataframe (already filtered to inline_insertion)
        chapter_number: int or str, e.g. 7 for chapter 7
        result_with_ba: full dataframe with all types, used to detect non-inline_insertion rows between paragraphs
    
    Returns:
        list of section dictionaries
    """
    
    # Pattern to match section headers like § 7:1, § 7:2, 7.1, etc. — only at the START of text
    section_pattern = re.compile(rf'^§?\s*{chapter_number}[:.\s][\dA-Za-z]+')
    
    # Find all rows where text STARTS WITH the section header pattern
    section_header_rows = df[df['text'].str.match(section_pattern, na=False)]
    
    # Filter: only keep header rows where ALL rows at that idx in result_with_ba are inline_insertion
    # (if there's any inline_deletion or inline_replacement at the same idx, it's an edit, not a new section)
    if result_with_ba is not None and not section_header_rows.empty:
        valid_header_indices = []
        for h_idx in section_header_rows.index:
            para_idx = section_header_rows.loc[h_idx, 'paragraph_index']
            # Get all rows at this paragraph index from the full dataframe
            rows_at_idx = result_with_ba[result_with_ba['idx'] == para_idx]
            all_types = rows_at_idx['type'].unique().tolist()
            # Only consider as new section if ALL types are inline_insertion
            if all(t == 'inline_insertion' for t in all_types):
                valid_header_indices.append(h_idx)
            else:
                print(f"  [EDGE CASE] Skipping idx={para_idx} as new section — has types: {all_types}")
        section_header_rows = section_header_rows.loc[valid_header_indices]
    
    sections = []
    section_header_list = section_header_rows.index.tolist()
    
    for i, header_idx in enumerate(section_header_list):
        header_row = df.loc[header_idx]
        
        # Extract section header (e.g., § 7:1, § 7:2)
        match = section_pattern.search(str(header_row['text']))
        section_header = match.group() if match else None
        
        start_df_index = header_idx
        start_paragraph_index = header_row['paragraph_index']
        
        # -------------------------------------------------------
        # Find end of section by detecting gap > 5 in paragraph_index
        # Get all rows from this header onwards (until next section header)
        # -------------------------------------------------------
        
        # Rows from current header onwards
        remaining_df = df.loc[header_idx:]
        
        # Skip the header row itself for gap detection, work with paragraph indices
        para_indices = remaining_df['paragraph_index'].dropna().unique()
        para_indices_sorted = sorted(para_indices)
        
        end_paragraph_index = start_paragraph_index  # default
        
        for j in range(1, len(para_indices_sorted)):
            gap = para_indices_sorted[j] - para_indices_sorted[j - 1]
            if gap > 5:
                end_paragraph_index = para_indices_sorted[j - 1]
                break
        else:
            # No gap found, section goes till last paragraph
            end_paragraph_index = para_indices_sorted[-1]
        
        # Get all rows within the section range
        section_rows = remaining_df[
            remaining_df['paragraph_index'] <= end_paragraph_index
        ]
        
        end_df_index = section_rows.index[-1]
        
        # -------------------------------------------------------
        # Build content list
        # First row: use 'text' column
        # All other rows: use 'inserted_text' column
        # Stop if has_changes=False or row starts with designator pattern
        # -------------------------------------------------------
        section_content = []
        actual_end_paragraph_index = start_paragraph_index
        actual_end_df_index = header_idx
        last_collected_para_idx = start_paragraph_index
        
        for row_idx, row in section_rows.iterrows():
            if row_idx == header_idx:
                # First row of section → use 'text' column
                section_content.append({
                    'paragraph_index': row['paragraph_index'],
                    'text': row['text']
                })
                actual_end_paragraph_index = row['paragraph_index']
                actual_end_df_index = row_idx
                last_collected_para_idx = row['paragraph_index']
            else:
                current_para_idx = row['paragraph_index']
                
                # Check result_with_ba for any non-inline_insertion rows between
                # last collected paragraph and current paragraph
                if result_with_ba is not None and current_para_idx > last_collected_para_idx:
                    between_rows = result_with_ba[
                        (result_with_ba['idx'] > last_collected_para_idx) &
                        (result_with_ba['idx'] < current_para_idx)
                    ].drop_duplicates(subset=['idx'], keep='first')
                    # If any row between has an explicit blocking type (deletion or replacement), stop
                    blocking_found = False
                    blocking_types = {'inline_deletion', 'inline_replacement'}
                    for _, brow in between_rows.iterrows():
                        brow_type = brow.get('type', None)
                        # Skip rows with no type (NaN/empty) — those are unchanged paragraphs
                        if pd.isna(brow_type) or str(brow_type).strip() == '':
                            continue
                        if str(brow_type).strip() in blocking_types:
                            blocking_found = True
                            break
                    if blocking_found:
                        break
                
                # Also check if current paragraph itself in result_with_ba has non-inline_insertion
                if result_with_ba is not None:
                    current_para_rows = result_with_ba[
                        result_with_ba['idx'] == current_para_idx
                    ].drop_duplicates(subset=['idx'], keep='first')
                    if not current_para_rows.empty:
                        first_type = current_para_rows.iloc[0].get('type', None)
                        # Only block on inline_deletion (not replacement which may coexist)
                        if pd.notna(first_type) and str(first_type).strip() == 'inline_deletion':
                            break
                
                # Stop if row text starts with a designator pattern (new section header)
                row_text = str(row.get('text', ''))
                if section_pattern.match(row_text):
                    break
                
                # All other rows → use 'text' column (full paragraph text)
                row_full_text = row.get('text', None)
                if pd.notna(row_full_text) and str(row_full_text).strip() != '':
                    section_content.append({
                        'paragraph_index': row['paragraph_index'],
                        'text': str(row_full_text)
                    })
                    actual_end_paragraph_index = row['paragraph_index']
                    actual_end_df_index = row_idx
                    last_collected_para_idx = row['paragraph_index']
        
        section_dict = {
            'section_header': section_header,
            'start_paragraph_index': start_paragraph_index,
            'end_paragraph_index': np.float64(actual_end_paragraph_index),
            'start_df_index': start_df_index,
            'end_df_index': actual_end_df_index,
            'content': section_content,
            'full_text': '\n'.join([item['text'] for item in section_content])
        }
        
        sections.append(section_dict)
    
    return sections

def extract_sectionedgecase_key(section_str):
    """Extract numeric key like '1:1' and the text following it.
    e.g. '§ 1:1 Abuse of Process Alleged as a Countercla...' → ('1:1', 'Abuse of Process Alleged as a Countercla...')
    """
    match = re.search(r'(\d+[A-Za-z]*[:.\s][\dA-Za-z]+)\s*(.*)', str(section_str))
    if match:
        key = match.group(1)        # '3:1'
        text = match.group(2).strip()  # 'Abuse of Process Alleged as a Countercla...'
        return key, text
    return None, None

def find_insertion_edgecase_section(sections, chapter_number):
    section_pattern = re.compile(rf'§?\s*{chapter_number}[:.][\dA-Za-z]+')
    
    leftover_rows = []
    # print('sections last',sections[1])
    for section in sections:
        last_para_idx = section['end_paragraph_index']
        
        # Get all entries at last paragraph_index
        last_para_entries = [e for e in section['content'] if float(e['paragraph_index']) == float(last_para_idx)]
        
        for entry in last_para_entries:
            if section_pattern.search(str(entry['text'])):
                leftover_rows.append(entry)
    
    return leftover_rows

def parse_pxml_sections(pxml_content):
    """Read PXML file and extract sections mapped by designator"""
   
    
    section_dict = {}
    section_positions = {}
    
    # Try standard <section> tags first
    pattern = r'<section\s+(?:uuid="[^"]+"\s+)?[^>]*>.*?</section>'
    section_matches = list(re.finditer(pattern, pxml_content, re.DOTALL))
    
    # If no standard sections found, try <editorial.commentary.section> tags
    if not section_matches:
        pattern = r'<editorial\.commentary\.section(?=[\s>])[^>]*>.*?</editorial\.commentary\.section>'
        section_matches = list(re.finditer(pattern, pxml_content, re.DOTALL))
    
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




import re

from rapidfuzz import fuzz

def update_designator_by_name_match(
    updated_pxmlcontent,
    text_c,
    new_designator_value,
    match_threshold=80  # adjust sensitivity 0-100
):
    section_dict, section_positions, pxml_content = parse_pxml_sections(updated_pxmlcontent)
    updated_content = pxml_content

    best_match_designator = None
    best_match_score = 0
    best_match_name = None

    for designator, section_xml in section_dict.items():
        name_match = re.search(r'<name[^>]*>(.*?)</name>', section_xml, re.DOTALL)
        if not name_match:
            continue

        # Replace <sep/> with a space or dash variants before stripping all tags
        name_raw = name_match.group(1)
        name_normalized = re.sub(r'<sep\s*/>', ' ', name_raw)          # <sep/> → space
        name_text = re.sub(r'<[^>]+>', '', name_normalized).strip()    # strip remaining tags

        # Fuzzy match score
        score = fuzz.token_sort_ratio(text_c.strip().lower(), name_text.lower())
        print(f"Section {designator} | Score: {score} | Name: '{name_text}'")

        if score > best_match_score:
            best_match_score = score
            best_match_designator = designator
            best_match_name = name_text

    if best_match_score < match_threshold:
        print(f"No confident match found. Best was '{best_match_name}' at score {best_match_score}")
        return updated_content

    print(f"Best match: Designator '{best_match_designator}' -> '{best_match_name}' (score: {best_match_score})")

    start_pos, end_pos = section_positions[best_match_designator]
    section_in_full = updated_content[start_pos:end_pos]

    def replace_designator(m):
        old_tag_attrs = re.sub(r'\s*date\.trace="[^"]*"', '', m.group(1))
        return f'<designator{old_tag_attrs} date.trace="1">{new_designator_value}</designator>'

    updated_section = re.sub(
        r'<designator([^>]*)>' + re.escape(best_match_designator) + r'</designator>',
        replace_designator,
        section_in_full
    )

    updated_content = (
        updated_content[:start_pos]
        + updated_section
        + updated_content[end_pos:]
    )

    return updated_content