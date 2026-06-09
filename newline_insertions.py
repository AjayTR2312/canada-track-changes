import pandas as pd
import re   



# def filter_inline_insertions(inline_insertion_df, sections):
#     exclude_indices = set()
#     for section in sections:
#         actual_labels = inline_insertion_df.loc[
#             int(section['start_df_index']): int(section['end_df_index'])
#         ].index.tolist()
#         exclude_indices.update(actual_labels)

#     filtered_df = inline_insertion_df[
#         ~inline_insertion_df.index.isin(exclude_indices)
#     ]

#     return filtered_df

def filter_inline_insertions(inline_insertion_df,sections):
    df=inline_insertion_df.copy()
    for sec in sections:
        start = sec['start_paragraph_index']
        end = sec['end_paragraph_index']
        df = exclude_by_idx_range(df, start, end)
    return df


def exclude_by_idx_range(df, start, end):
    return df[~df['idx'].between(start, end)]




def fill_sections_remaining_insertions(section_pattern,section_separator, inlinedels_df, no_changes_df, inline_replacement_df, inline_insertion_df, section_start_number, verbose=False):    
    """    
    Fill sections backward by searching in both no_changes_df, inline_replacement_df, and inline_insertion_df.    
    Finds the closest idx < current paragraph_index that contains a section.    
    For inline_replacement_df, applies deletions and insertions to get the final text.    
    If section is from inline_replacement, replaces inserted_text with deleted_text in the section.
    If section is from inline_insertion_df, marks as \"couldn't identify section\".
    """    
    df = inlinedels_df.copy()  
    df = df.reset_index(drop=True)
    
    if 'section' not in df.columns:
        df['section'] = None
    # OLD: section_pattern = rf'§?\s*{section_start_number}{section_separator}[\dA-Za-z]+[^§\n]*'
    section_pattern = rf'§?\s*{section_start_number}{section_separator}[\dA-Za-z]+(?:\.[\dA-Za-z]+)*\.?'
    for i in range(len(df)):    
        # FIX: Check for None, NaN, OR empty string
        section_value = df.loc[i, 'section']
        if section_value is None or pd.isna(section_value) or str(section_value).strip() == '':    
            current_para_idx = df.loc[i, 'paragraph_index']    
                
            if verbose:    
                print(f"\nProcessing para_idx={current_para_idx}")    
                
            if pd.isna(current_para_idx):    
                continue    
                
            # Search in no_changes_df    
            no_changes_candidates = no_changes_df[    
                no_changes_df['idx'] < current_para_idx    
            ].copy()    
                
            closest_no_change_idx = None    
            section_from_no_change = None    
                
            for idx in no_changes_candidates.sort_values('idx', ascending=False).index:    
                row = no_changes_candidates.loc[idx]    
                text = str(row.get('text', '')) if pd.notna(row.get('text')) else ''    
                    
                if text.strip() == '':    
                    continue    
                    
                match = re.search(section_pattern, text)    
                if match:    
                    closest_no_change_idx = row['idx']    
                    section_from_no_change = match.group(0).strip()    
                    if verbose:    
                        print(f"  Found in no_changes_df: idx={closest_no_change_idx}, section={section_from_no_change}")    
                    break    
                
            # Search in inline_replacement_df    
            inline_candidates = inline_replacement_df[    
                inline_replacement_df['idx'] < current_para_idx    
            ].copy()    
                
            closest_inline_idx = None    
            section_from_inline = None    
            inline_row_data = None    
                
            for idx in inline_candidates.sort_values('idx', ascending=False).index:    
                row = inline_candidates.loc[idx]    
                    
                # Start with original text    
                text = str(row.get('text', '')) if pd.notna(row.get('text')) else ''    
                deleted_text = str(row.get('deleted_text', '')) if pd.notna(row.get('deleted_text')) else ''    
                inserted_text = str(row.get('inserted_text', '')) if pd.notna(row.get('inserted_text')) else ''    
                  
                # Remove one duplicate from inserted_text  
                inserted_text_clean = inserted_text  
                if inserted_text.strip() != '':  
                    parts = inserted_text.strip().split()  
                    if len(parts) == 2 and parts[0] == parts[1]:  
                        inserted_text_clean = parts[0]  
                    elif len(parts) > 2 and len(parts) % 2 == 0:  
                        mid = len(parts) // 2  
                        first_half = ' '.join(parts[:mid])  
                        second_half = ' '.join(parts[mid:])  
                        if first_half == second_half:  
                            inserted_text_clean = first_half  
                  
                # Apply deletion and insertion    
                if deleted_text.strip() != '' and deleted_text in text:    
                    text = text.replace(deleted_text, inserted_text_clean)    
                    
                if text.strip() == '':    
                    continue    
                    
                match = re.search(section_pattern, text)    
                if match:    
                    closest_inline_idx = row['idx']    
                    section_from_inline = match.group(0).strip()    
                    inline_row_data = {'deleted_text': deleted_text, 'inserted_text': inserted_text_clean}    
                    if verbose:    
                        print(f"  Found in inline_replacement_df: idx={closest_inline_idx}, section={section_from_inline}")    
                    break    
                
            # Search in inline_insertion_df    
            insertion_candidates = inline_insertion_df[    
                inline_insertion_df['idx'] < current_para_idx    
            ].copy()    
                
            closest_insertion_idx = None    
            section_from_insertion = None    
                
            for idx in insertion_candidates.sort_values('idx', ascending=False).index:    
                row = insertion_candidates.loc[idx]    
                text = str(row.get('text', '')) if pd.notna(row.get('text')) else ''    
                    
                if text.strip() == '':    
                    continue    
                    
                match = re.search(section_pattern, text)    
                if match:    
                    closest_insertion_idx = row['idx']    
                    section_from_insertion = match.group(0).strip()    
                    if verbose:    
                        print(f"  Found in inline_insertion_df: idx={closest_insertion_idx}, section={section_from_insertion}")    
                    break    
                
            # Compare and pick the closest one    
            section_to_use = None    
            
            # Find the maximum idx among all three sources
            all_indices = []
            if closest_no_change_idx is not None:
                all_indices.append(('no_change', closest_no_change_idx, section_from_no_change))
            if closest_inline_idx is not None:
                all_indices.append(('inline_replacement', closest_inline_idx, section_from_inline, inline_row_data))
            if closest_insertion_idx is not None:
                all_indices.append(('inline_insertion', closest_insertion_idx, section_from_insertion))

            if all_indices:
                # Sort by idx (second element) in descending order and get the closest one
                all_indices.sort(key=lambda x: x[1], reverse=True)
                source_type = all_indices[0][0]
                closest_idx = all_indices[0][1]
                
                if source_type == 'inline_insertion':
                    # Use the section found in inline_insertion_df directly
                    section_to_use = all_indices[0][2]
                    if verbose:
                        print(f"  Closest source is inline_insertion_df (idx={closest_idx})")
                        print(f"  Using section: {section_to_use}")
                else:  # no_change or inline_replacement
                    section_to_use = all_indices[0][2]
                    if verbose:
                        print(f"  Using {source_type} (idx={closest_idx}): {section_to_use}")
            
            # if all_indices:
            #     # Sort by idx (second element) in descending order and get the closest one
            #     all_indices.sort(key=lambda x: x[1], reverse=True)
            #     source_type = all_indices[0][0]
            #     closest_idx = all_indices[0][1]
                
            #     if source_type == 'inline_insertion':
            #         # If inline_insertion is closest, mark as couldn't identify
            #         section_to_use = "couldn't identify section"
            #         if verbose:
            #             print(f"  Closest source is inline_insertion_df (idx={closest_idx})")
            #             print(f"  Marking as: couldn't identify section")
            #     elif source_type == 'inline_replacement':
            #         section_to_use = all_indices[0][2]
            #         inline_row_data = all_indices[0][3]
            #         # Replace inserted_text with deleted_text in the section
            #         if inline_row_data and inline_row_data['inserted_text'] in section_to_use:
            #             section_to_use = section_to_use.replace(
            #                 inline_row_data['inserted_text'], 
            #                 inline_row_data['deleted_text']
            #             )
            #         if verbose:
            #             print(f"  Using inline_replacement (idx={closest_idx})")
            #             print(f"  After reverting insertion: {section_to_use}")
            #     else:  # no_change
            #         section_to_use = all_indices[0][2]
            #         if verbose:
            #             print(f"  Using no_changes (idx={closest_idx}): {section_to_use}")
            # else:
            #     if verbose:
            #         print(f"  No section found")
                
            if section_to_use:    
                df.loc[i, 'section'] = section_to_use    
                if verbose:
                    print(f"  ✓ Section assigned: {section_to_use}")
        
    return df

def get_context_text_inline_insertions(target_idx, no_changes_df, df_changes, direction='previous', max_search=10):  
    """  
    Get context text by searching in no_changes_df first, then df_changes  
      
    Parameters:  
    - target_idx: the index to search for  
    - no_changes_df: dataframe with unchanged text  
    - df_changes: dataframe with changes  
    - direction: 'previous' or 'post'  
    - max_search: maximum number of indices to search  
    """  
    step = -1 if direction == 'previous' else 1  
      
    for i in range(1, max_search + 1):  
        search_idx = target_idx + (i * step)  
          
        # First check no_changes_df  
        match_no_changes = no_changes_df[no_changes_df['idx'] == search_idx]  
        if not match_no_changes.empty:  
            text = match_no_changes.iloc[0]['text']  
            if pd.notna(text) and str(text).strip() != '':  
                return str(text).strip()  
          
        # Then check df_changes for deleted_text  
        match_changes = df_changes[df_changes['paragraph_index'] == search_idx]  
        if not match_changes.empty:  
            deleted_text = match_changes.iloc[0]['deleted_text']  
            if pd.notna(deleted_text) and str(deleted_text).strip() != '':  
                return str(deleted_text).strip()  
      
    return ''  # Return empty string if nothing found


def continous_noncontinous_insertions(filtered_df):
    # Sort by paragraph_index first
    filtered_df = filtered_df.sort_values('paragraph_index').reset_index(drop=True)

    # Work only with new_line insertion_type
    newline_df = filtered_df[filtered_df['insertion_type'] == 'new_line'].copy()

    # --- Identify continuous groups ---
    # A group is continuous if:
    # 1. paragraph_index is sequential (diff == 1)
    # 2. previous_context and post_context are the same within the group

    # Step 1: Mark where a new group starts
    newline_df['para_diff'] = newline_df['paragraph_index'].diff().fillna(1)
    newline_df['context_changed'] = (
        (newline_df['previous_context'] != newline_df['previous_context'].shift(1)) |
        (newline_df['post_context'] != newline_df['post_context'].shift(1))
    )

    # New group starts when paragraph_index is not continuous OR context changes
    newline_df['group_id'] = (
        (newline_df['para_diff'] != 1) | newline_df['context_changed']
    ).cumsum()

    # Step 2: Find groups with more than 1 row → continuous
    group_sizes = newline_df.groupby('group_id').size()
    continuous_group_ids = group_sizes[group_sizes > 1].index
    non_continuous_group_ids = group_sizes[group_sizes == 1].index

    # Step 3: Split into two dataframes
    newline_insertion_continuous_df = (
        newline_df[newline_df['group_id'].isin(continuous_group_ids)]
        .drop(columns=['para_diff', 'context_changed', 'group_id'])
        .reset_index(drop=True)
    )

    newline_insertion_non_continuous_df = (
        newline_df[newline_df['group_id'].isin(non_continuous_group_ids)]
        .drop(columns=['para_diff', 'context_changed', 'group_id'])
        .reset_index(drop=True)
    )

    print(f"Continuous groups    : {len(newline_insertion_continuous_df)} rows")
    print(f"Non-continuous groups: {len(newline_insertion_non_continuous_df)} rows")

    print("\n--- Continuous ---")
    print(newline_insertion_continuous_df[['idx', 'paragraph_index', 'previous_context', 'inserted_text', 'post_context']])

    print("\n--- Non-Continuous ---")
    print(newline_insertion_non_continuous_df[['idx', 'paragraph_index', 'previous_context', 'inserted_text', 'post_context']])

    return newline_insertion_continuous_df, newline_insertion_non_continuous_df

