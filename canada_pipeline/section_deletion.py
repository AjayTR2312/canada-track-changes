import datetime as dt  
import json  
import re  
import string  
from zipfile import ZipFile  
from collections import deque  
import xml.etree.ElementTree as ET  
import pandas as pd

from .config import chapter_pattern


# =====================================================  
# DOCX TRACK CHANGES EXTRACTOR  
# =====================================================

class DocxTrackChangesExtractor:  
    """Extract track changes from Word documents"""  
      
    W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"  
    NS = {"w": W_NS}  
      
    def __init__(self, docx_path, context_chars=40, replacement_time_tol_sec=60):  
        self.docx_path = docx_path  
        self.context_chars = context_chars  
        self.replacement_time_tol_sec = replacement_time_tol_sec  
        self.changes = []  
        self.all_paragraphs = []  
          
    @staticmethod  
    def _get_all_text(elem):  
        texts = []  
        ns = {"w": DocxTrackChangesExtractor.W_NS}  
        for t in elem.findall(".//w:t", ns):  
            texts.append(t.text or "")  
        return "".join(texts)  
      
    @staticmethod  
    def _get_all_deltext(elem):  
        texts = []  
        ns = {"w": DocxTrackChangesExtractor.W_NS}  
        for t in elem.findall(".//w:delText", ns):  
            texts.append(t.text or "")  
        return "".join(texts)  
      
    @staticmethod  
    def _normalize_whitespace(s):  
        return re.sub(r"\s+", " ", s or "").strip()  
      
    @staticmethod  
    def _is_trivial_gap(s):  
        if s is None:  
            return True  
        s = s.strip()  
        return len(s) == 0 or all(ch in string.whitespace + string.punctuation for ch in s)  
      
    @staticmethod  
    def _parse_dt_or_none(iso_str):  
        if not iso_str:  
            return None  
        try:  
            return dt.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))  
        except Exception:  
            return None  
      
    @staticmethod  
    def _same_minute(t1, t2, tol=60):  
        if not t1 or not t2:  
            return False  
        return abs((t1 - t2).total_seconds()) <= tol  
      
    def load_heading_styles(self, zipf):  
        style_outline_level = {}  
        style_names = {}  
          
        try:  
            styles_xml = zipf.read("word/styles.xml")  
        except KeyError:  
            return style_outline_level, style_names  
          
        root = ET.fromstring(styles_xml)  
        for s in root.findall(".//w:style", self.NS):  
            style_id = s.attrib.get(f"{{{self.W_NS}}}styleId")  
            if not style_id:  
                continue  
              
            name_el = s.find("w:name", self.NS)  
            if name_el is not None:  
                style_names[style_id] = name_el.attrib.get(f"{{{self.W_NS}}}val")  
              
            outline_el = s.find(".//w:outlineLvl", self.NS)  
            if outline_el is not None:  
                try:  
                    lvl = int(outline_el.attrib[f"{{{self.W_NS}}}val"]) + 1  
                    style_outline_level[style_id] = lvl  
                    continue  
                except:  
                    pass  
              
            nm = style_names.get(style_id, "")  
            m = re.search(r"heading\s*([1-9])", nm, re.IGNORECASE)  
            if m:  
                style_outline_level[style_id] = int(m.group(1))  
          
        return style_outline_level, style_names  
      
    def paragraph_outline_level(self, p, style_outline_level):  
        direct = p.find("./w:pPr/w:outlineLvl", self.NS)  
        if direct is not None:  
            try:  
                return int(direct.attrib[f"{{{self.W_NS}}}val"]) + 1  
            except:  
                pass  
          
        pstyle = p.find("./w:pPr/w:pStyle", self.NS)  
        if pstyle is not None:  
            style_id = pstyle.attrib.get(f"{{{self.W_NS}}}val")  
            return style_outline_level.get(style_id)  
          
        return None  
      
    def paragraph_text(self, p):  
        return self._normalize_whitespace(self._get_all_text(p))  
      
    def walk_content(self, elem, block_rev=None):  
        tag = elem.tag  
          
        if tag in {f"{{{self.W_NS}}}ins", f"{{{self.W_NS}}}moveTo"}:  
            rev = {  
                "type": "ins",  
                "author": elem.attrib.get(f"{{{self.W_NS}}}author"),  
                "date": self._parse_dt_or_none(elem.attrib.get(f"{{{self.W_NS}}}date")),  
                "id": elem.attrib.get(f"{{{self.W_NS}}}id")  
            }  
            for c in list(elem):  
                yield from self.walk_content(c, block_rev=rev)  
            return  
          
        if tag in {f"{{{self.W_NS}}}del", f"{{{self.W_NS}}}moveFrom"}:  
            rev = {  
                "type": "del",  
                "author": elem.attrib.get(f"{{{self.W_NS}}}author"),  
                "date": self._parse_dt_or_none(elem.attrib.get(f"{{{self.W_NS}}}date")),  
                "id": elem.attrib.get(f"{{{self.W_NS}}}id")  
            }  
            for c in list(elem):  
                yield from self.walk_content(c, block_rev=rev)  
            return  
          
        if tag == f"{{{self.W_NS}}}p":  
            yield ("paragraph", elem, block_rev)  
            return  
          
        for c in list(elem):  
            yield from self.walk_content(c, block_rev=block_rev)  
      
    def iter_para_segments(self, p):  
        def dfs(n, kind=None, attrs=None):  
            tag = n.tag  
              
            if tag in {f"{{{self.W_NS}}}ins", f"{{{self.W_NS}}}moveTo"}:  
                at = {  
                    "author": n.attrib.get(f"{{{self.W_NS}}}author"),  
                    "date": self._parse_dt_or_none(n.attrib.get(f"{{{self.W_NS}}}date")),  
                    "id": n.attrib.get(f"{{{self.W_NS}}}id")  
                }  
                txt = self._normalize_whitespace(self._get_all_text(n))  
                if txt:  
                    yield ("ins", txt, at)  
                for c in list(n):  
                    yield from dfs(c, "ins", at)  
                return  
              
            if tag in {f"{{{self.W_NS}}}del", f"{{{self.W_NS}}}moveFrom"}:  
                at = {  
                    "author": n.attrib.get(f"{{{self.W_NS}}}author"),  
                    "date": self._parse_dt_or_none(n.attrib.get(f"{{{self.W_NS}}}date")),  
                    "id": n.attrib.get(f"{{{self.W_NS}}}id")  
                }  
                txt = self._normalize_whitespace(self._get_all_deltext(n)) or self._normalize_whitespace(self._get_all_text(n))  
                if txt:  
                    yield ("del", txt, at)  
                for c in list(n):  
                    yield from dfs(c, "del", at)  
                return  
              
            if tag == f"{{{self.W_NS}}}t":  
                if kind is None:  
                    yield ("text", self._normalize_whitespace(n.text or ""), None)  
                elif kind == "ins":  
                    if n.text and n.text.strip():  
                        yield ("ins", self._normalize_whitespace(n.text), attrs)  
                return  
              
            for c in list(n):  
                yield from dfs(c, kind, attrs)  
          
        merged = []  
        for kind, text, attrs in dfs(p):  
            if not text:  
                continue  
            if merged and merged[-1][0] == kind and (kind == "text" or merged[-1][2] == attrs):  
                merged[-1] = (kind, merged[-1][1] + " " + text, attrs)  
            else:  
                merged.append((kind, text, attrs))  
        return merged  
      
    def extract(self):  
        """Main extraction method"""  
        with ZipFile(self.docx_path) as z:  
            document_xml = z.read("word/document.xml")  
            root = ET.fromstring(document_xml)  
            style_outline_level, _ = self.load_heading_styles(z)  
          
        body = root.find("w:body", self.NS)  
        current_section = [None] * 10  
        paragraphs = []  
        para_idx = -1  
          
        # First pass: collect paragraphs  
        for kind, p, block_rev in self.walk_content(body):  
            if kind != "paragraph":  
                continue  
              
            para_idx += 1  
            ptext = self.paragraph_text(p)  
            lvl = self.paragraph_outline_level(p, style_outline_level)  
              
            if lvl:  
                current_section[lvl] = ptext  
                for k in range(lvl + 1, 10):  
                    current_section[k] = None  
              
            section_path = " > ".join(t for t in current_section[1:] if t)  
              
            paragraphs.append({  
                "idx": para_idx,  
                "elem": p,  
                "text": ptext,  
                "block_rev": block_rev,  
                "section": section_path  
            })  
              
            self.all_paragraphs.append({  
                "idx": para_idx,  
                "text": ptext,  
                "section": section_path,  
                "has_changes": False  
            })  
          
        # Helper functions  
        def prev_nonempty(i):  
            for j in range(i - 1, -1, -1):  
                if paragraphs[j]["text"]:  
                    return paragraphs[j]["text"]  
            return ""  
          
        def next_nonempty(i):  
            for j in range(i + 1, len(paragraphs)):  
                if paragraphs[j]["text"]:  
                    return paragraphs[j]["text"]  
            return ""  
          
        # Second pass: extract changes  
        for para in paragraphs:  
            idx = para["idx"]  
            p = para["elem"]  
            ptext = para["text"]  
            section = para["section"]  
            block_rev = para["block_rev"]  
            para_has_changes = False  
              
            # Block-level insertions  
            if block_rev and block_rev["type"] == "ins":  
                para_has_changes = True  
                self.changes.append({  
                    "type": "paragraph_addition",  
                    "paragraph_index": idx,  
                    "section": section,  
                    "author": block_rev.get("author"),  
                    "date": block_rev.get("date").isoformat() if block_rev.get("date") else None,  
                    "inserted_text": ptext,  
                    "deleted_text": None,  
                    "prev_context": prev_nonempty(idx),  
                    "post_context": next_nonempty(idx)  
                })  
                self.all_paragraphs[idx]["has_changes"] = True  
                continue  
              
            # Block-level deletions  
            if block_rev and block_rev["type"] == "del":  
                para_has_changes = True  
                deltext = self._normalize_whitespace(self._get_all_deltext(para["elem"])) or ptext  
                self.changes.append({  
                    "type": "paragraph_deletion",  
                    "paragraph_index": idx,  
                    "section": section,  
                    "author": block_rev.get("author"),  
                    "date": block_rev.get("date").isoformat() if block_rev.get("date") else None,  
                    "inserted_text": None,  
                    "deleted_text": deltext,  
                    "prev_context": prev_nonempty(idx),  
                    "post_context": next_nonempty(idx)  
                })  
                self.all_paragraphs[idx]["has_changes"] = True  
                continue  
              
            # Inline changes  
            segs = self.iter_para_segments(p)  
            if not segs:  
                continue  
              
            visible = "".join(s[1] for s in segs if s[0] in ("text", "ins"))  
              
            def ctx(start, length):  
                a = max(0, start - self.context_chars)  
                b = min(len(visible), start + length + self.context_chars)  
                return visible[a:start], visible[start + length:b]  
              
            i = 0  
            cursor = 0  
              
            while i < len(segs):  
                kind, text, attrs = segs[i]  
                  
                if kind == "text":  
                    cursor += len(text)  
                    i += 1  
                    continue  
                  
                if kind == "del":  
                    para_has_changes = True  
                    deltxt = text  
                    del_auth = attrs.get("author") if attrs else None  
                    del_dt = attrs.get("date") if attrs else None  
                      
                    # Check for replacement  
                    j = i + 1  
                    gap = ""  
                    ins_seg = None  
                      
                    while j < len(segs) and segs[j][0] == "text" and self._is_trivial_gap(segs[j][1]):  
                        gap += segs[j][1]  
                        j += 1  
                      
                    if j < len(segs) and segs[j][0] == "ins":  
                        ins_seg = segs[j]  
                      
                    if ins_seg:  
                        _, instxt, insattrs = ins_seg  
                        same_author = insattrs.get("author") == del_auth  
                        close = self._same_minute(insattrs.get("date"), del_dt, self.replacement_time_tol_sec)  
                        if self._is_trivial_gap(gap) and (same_author or close):  
                            start = cursor  
                            prev, post = ctx(start, len(instxt))  
                            self.changes.append({  
                                "type": "inline_replacement",  
                                "paragraph_index": idx,  
                                "section": section,  
                                "author": insattrs.get("author") or del_auth,  
                                "date": (insattrs.get("date") or del_dt).isoformat() if (insattrs.get("date") or del_dt) else None,  
                                "deleted_text": deltxt,  
                                "inserted_text": instxt,  
                                "prev_context": prev,  
                                "post_context": post  
                            })  
                            cursor += len(instxt)  
                            i = j + 1  
                            continue  
                      
                    # Plain deletion  
                    start = max(0, cursor - 1)  
                    prev, post = ctx(start, 0)  
                    self.changes.append({  
                        "type": "inline_deletion",  
                        "paragraph_index": idx,  
                        "section": section,  
                        "author": del_auth,  
                        "date": del_dt.isoformat() if del_dt else None,  
                        "deleted_text": deltxt,  
                        "inserted_text": None,  
                        "prev_context": prev,  
                        "post_context": post  
                    })  
                    i += 1  
                    continue  
                  
                if kind == "ins":  
                    para_has_changes = True  
                    instxt = text  
                    ins_auth = attrs.get("author") if attrs else None  
                    ins_dt = attrs.get("date") if attrs else None  
                      
                    start = cursor  
                    prev, post = ctx(start, len(instxt))  
                      
                    self.changes.append({  
                        "type": "inline_insertion",  
                        "paragraph_index": idx,  
                        "section": section,  
                        "author": ins_auth,  
                        "date": ins_dt.isoformat() if ins_dt else None,  
                        "deleted_text": None,  
                        "inserted_text": instxt,  
                        "prev_context": prev,  
                        "post_context": post  
                    })  
                      
                    cursor += len(instxt)  
                    i += 1  
                    continue  
              
            if para_has_changes:  
                self.all_paragraphs[idx]["has_changes"] = True  
          
        return self.changes, self.all_paragraphs  
      
    def to_dataframes(self):  
        """Convert extracted data to DataFrames"""  
        df_changes = pd.DataFrame(self.changes)  
        df_paragraphs = pd.DataFrame(self.all_paragraphs)  
        return df_changes, df_paragraphs


# =====================================================  
# SECTION ANALYZER  
# =====================================================

class SectionAnalyzer:  
    """Analyze and identify sections in deletion dataframe"""  
      
    def __init__(self, chapter_number):  
        self.chapter_number = str(chapter_number)  
        # OLD: self.section_pattern = rf'§\s*{self.chapter_number}:\d+[^§]*?(?=§|$)'
        # OLD: self.section_pattern = rf'§?\s*{self.chapter_number}[:.][\dA-Za-z]+(?:\.[\dA-Za-z]+)*\.?'
        # NEW: ^ anchors to start of text — only matches when designator IS the heading,
        # not when it appears mid-sentence (e.g. "introduction of the name 8:1")
        self.section_pattern = rf'^§?\s*{self.chapter_number}[:.][\dA-Za-z]+(?:\.[\dA-Za-z]+)*\.?'
        
    def identify_sections_with_names(self, inlinedeletion_df):  
        """Identify section names with full description from deleted_text"""  
        df = inlinedeletion_df.copy()  
        df['section'] = None  
          
        for idx in df.index:  
            deleted_text = df.loc[idx, 'deleted_text']  
              
            if pd.notna(deleted_text):  
                match = re.search(self.section_pattern, str(deleted_text))  
                  
                if match:  
                    section_text = match.group(0).strip()  
                    df.loc[idx, 'section'] = section_text  
          
        return df  
      
    def propagate_sections(self, inlinedeletion_df, df_remaining):  
        """Propagate section names forward until a new section appears"""  
        df = inlinedeletion_df.copy()  
        df = df.sort_values('idx').reset_index(drop=True)  
          
        section_pattern = self.section_pattern  
        current_section = None  
          
        for i in range(len(df)):  
            current_idx = df.loc[i, 'idx']  
              
            if pd.notna(df.loc[i, 'section']):  
                current_section = df.loc[i, 'section']  
            else:  
                if current_section is not None:  
                    next_rows = df_remaining[df_remaining['idx'] > current_idx].sort_values('idx')  
                    found_new_section = False  
                    next_inline_idx = df.loc[i+1, 'idx'] if i+1 < len(df) else float('inf')  
                      
                    for _, row in next_rows.iterrows():  
                        if row['idx'] >= next_inline_idx:  
                            break  
                          
                        if pd.notna(row.get('text')) or pd.notna(row.get('deleted_text')):  
                            text_to_check = str(row.get('text', '')) + ' ' + str(row.get('deleted_text', ''))  
                            match = re.search(section_pattern, text_to_check)  
                              
                            if match:  
                                found_new_section = True  
                                break  
                      
                    if not found_new_section:  
                        df.loc[i, 'section'] = current_section  
          
        return df  
      
    def fill_sections_backward(self, inlinedels_df, no_changes_df, inline_replacement_df, verbose=False):  
        """Fill sections backward by searching in both dataframes"""  
        df = inlinedels_df.copy()  
        section_pattern = self.section_pattern  
          
        for i in range(len(df)):  
            if df.loc[i, 'section'] is None or pd.isna(df.loc[i, 'section']):  
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
                      
                    text = str(row.get('text', '')) if pd.notna(row.get('text')) else ''  
                    deleted_text = str(row.get('deleted_text', '')) if pd.notna(row.get('deleted_text')) else ''  
                    inserted_text = str(row.get('inserted_text', '')) if pd.notna(row.get('inserted_text')) else ''  
                      
                    # Clean duplicate insertions  
                    inserted_text_clean = self._clean_duplicate_text(inserted_text)  
                      
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
                  
                # Compare and pick the closest  
                section_to_use = None  
                  
                if closest_no_change_idx is not None and closest_inline_idx is not None:  
                    if closest_inline_idx > closest_no_change_idx:  
                        section_to_use = section_from_inline  
                        if inline_row_data and inline_row_data['inserted_text'] in section_to_use:  
                            section_to_use = section_to_use.replace(  
                                inline_row_data['inserted_text'],  
                                inline_row_data['deleted_text']  
                            )  
                        if verbose:  
                            print(f"  Using inline_replacement (idx={closest_inline_idx} > {closest_no_change_idx})")  
                    else:  
                        section_to_use = section_from_no_change  
                        if verbose:  
                            print(f"  Using no_changes (idx={closest_no_change_idx} >= {closest_inline_idx})")  
                elif closest_inline_idx is not None:  
                    section_to_use = section_from_inline  
                    if inline_row_data and inline_row_data['inserted_text'] in section_to_use:  
                        section_to_use = section_to_use.replace(  
                            inline_row_data['inserted_text'],  
                            inline_row_data['deleted_text']  
                        )  
                    if verbose:  
                        print(f"  Using inline_replacement (only one found)")  
                elif closest_no_change_idx is not None:  
                    section_to_use = section_from_no_change  
                    if verbose:  
                        print(f"  Using no_changes (only one found)")  
                else:  
                    if verbose:  
                        print(f"  No section found")  
                  
                if section_to_use:  
                    df.loc[i, 'section'] = section_to_use  
          
        return df  
      
    def fill_remaining_sections_backward(self, inlinedels_df, df_remaining, verbose=False):  
        """Fill remaining sections backward from df_remaining"""  
        df = inlinedels_df.copy()  
        section_pattern = self.section_pattern  
          
        for i in range(len(df)):  
            if df.loc[i, 'section'] is None or pd.isna(df.loc[i, 'section']):  
                current_para_idx = df.loc[i, 'paragraph_index']  
                current_idx = df.loc[i, 'idx']  
                  
                if verbose:  
                    print(f"\nProcessing idx={current_idx}, para_idx={current_para_idx}")  
                  
                if pd.notna(current_idx):  
                    previous_rows = df_remaining[  
                        df_remaining['idx'] <= current_idx  
                    ].sort_values('idx', ascending=False)  
                      
                    section_found = None  
                      
                    for _, row in previous_rows.iterrows():  
                        text_to_check = ''  
                          
                        if pd.notna(row.get('text')) and str(row.get('text')).strip() != '':  
                            text_to_check += str(row['text'])  
                          
                        if pd.notna(row.get('deleted_text')) and str(row.get('deleted_text')).strip() != '':  
                            text_to_check += ' ' + str(row['deleted_text'])  
                          
                        if text_to_check.strip() == '':  
                            continue  
                          
                        match = re.search(section_pattern, text_to_check)  
                          
                        if match:  
                            section_found = match.group(0).strip()  
                            if verbose:  
                                print(f"  Found section: {section_found}")  
                                print(f"  In row idx={row['idx']}")  
                            break  
                      
                    if section_found:  
                        df.loc[i, 'section'] = section_found  
                    elif verbose:  
                        print(f"  No section found for idx={current_idx}")  
          
        return df  
      
    @staticmethod  
    def _clean_duplicate_text(text):  
        """Remove duplicate patterns from text"""  
        if not text or not text.strip():  
            return text  
          
        parts = text.strip().split()  
          
        # Check for simple duplicates like "5 5"  
        if len(parts) == 2 and parts[0] == parts[1]:  
            return parts[0]  
          
        # Check for complex duplicates  
        if len(parts) > 2 and len(parts) % 2 == 0:  
            mid = len(parts) // 2  
            first_half = ' '.join(parts[:mid])  
            second_half = ' '.join(parts[mid:])  
            if first_half == second_half:  
                return first_half  
          
        return text


# =====================================================  
# CONTEXT EXTRACTOR  
# =====================================================

class ContextExtractor:  
    """Extract context text for paragraphs"""  
      
    @staticmethod  
    def get_context_text(target_idx, no_changes_df, df_changes, direction='previous', max_search=10):  
        """Get context text by searching in no_changes_df first, then df_changes"""  
        step = -1 if direction == 'previous' else 1  
          
        for i in range(1, max_search + 1):  
            search_idx = target_idx + (i * step)  
              
            # Check no_changes_df first  
            match_no_changes = no_changes_df[no_changes_df['idx'] == search_idx]  
            if not match_no_changes.empty:  
                text = match_no_changes.iloc[0]['text']  
                if pd.notna(text) and str(text).strip() != '':  
                    return str(text).strip()  
              
            # Check df_changes for deleted_text  
            match_changes = df_changes[df_changes['paragraph_index'] == search_idx]  
            if not match_changes.empty:  
                deleted_text = match_changes.iloc[0]['deleted_text']  
                if pd.notna(deleted_text) and str(deleted_text).strip() != '':  
                    return str(deleted_text).strip()  
          
        return ''  
      
    @staticmethod  
    def add_context_columns(inlinedels_df, no_changes_df, df_changes):  
        """Add previous_context and post_context columns to dataframe"""  
        df = inlinedels_df.copy()  
          
        df['previous_context'] = df['paragraph_index'].apply(  
            lambda idx: ContextExtractor.get_context_text(idx, no_changes_df, df_changes, direction='previous')  
            if pd.notna(idx) else ''  
        )  
          
        df['post_context'] = df['paragraph_index'].apply(  
            lambda idx: ContextExtractor.get_context_text(idx, no_changes_df, df_changes, direction='post')  
            if pd.notna(idx) else ''  
        )  
          
        return df


# =====================================================  
# PXML SECTION REMOVER  
# =====================================================

class PXMLSectionRemover:  
    """Remove sections from PXML files"""  
      
    def __init__(self, chapter_number):  
        self.chapter_number = str(chapter_number)  
        # OLD: self.section_pattern = rf'§?\s*{self.chapter_number}[:.\s][\dA-Za-z]+[^§]*?(?=§|$)'  
        # OLD: self.section_pattern = rf'§\s*{self.chapter_number}:\d+[^§]*?(?=§|$)'
    def remove_sections_from_dataframe(self, inlinedels_df, content):  
        """Remove all sections identified in the dataframe and return remaining deletions"""  
        sections_to_delete = {}  
        
        # Step 1: Extract sections from deleted_text  
        for idx, row in inlinedels_df.iterrows():  
            deleted_text = row['deleted_text']  
            paragraph_index = row['paragraph_index']  
            section_text = row.get('section', '')  
            
            if pd.notna(deleted_text):  
                deleted_str = str(deleted_text).strip()  
               
                # Pattern: section designator at START, followed by a section name (alphabetic text).
                # § is optional, separator can be : or .
                # The designator MUST be followed by at least one word (the section name).
                # This prevents bare numbers like "7.6" (percentages) from matching.
                # Valid: "7:6 Environmental Issues", "§ 7:6 The Management...",
                #        "10:27. New IAD Rules..." (trailing '.' delimiter after the number)
                # Invalid: "7.6", "7.6%", "7.6 tonnes"
                # NOTE: an optional trailing delimiter [.:] is allowed after the
                # number so headings like "10:27. New ..." match (previously the
                # '.' broke the required \s+[A-Za-z] and the deletion was missed).
                section_pattern = rf'^§?\s*{chapter_pattern(self.chapter_number)}[:.](\d+(?:\.\d+)?[A-Za-z]?)[.:]?\s+[A-Za-z]'  
                match_obj = re.search(section_pattern, deleted_str)
                
                if not match_obj:
                    continue

                section_num = match_obj.group(1)

                # Additional validation: for a true section deletion,
                # text_before should have content but text_after should be empty.
                # This confirms the entire section was removed, not just inline text.
                text_before = row.get('text_before', '')
                text_after = row.get('text_after', '')
                text_before_str = str(text_before).strip() if pd.notna(text_before) else ''
                text_after_str = str(text_after).strip() if pd.notna(text_after) else ''

                if not text_before_str:
                    # No text_before — can't confirm section existed
                    continue

                if text_after_str:
                    # text_after has content — section wasn't fully deleted,
                    # this is inline text deletion, not a section removal
                    continue

                # Verify the designator is at the START of text_before (it's a section heading).
                # If the designator appears mid-text (e.g., "environmental issues 7:6 is not..."),
                # then it's a reference in running text, NOT a section row.
                heading_pattern = rf'^§?\s*{chapter_pattern(self.chapter_number)}[:.][\dA-Za-z]'
                if not re.search(heading_pattern, text_before_str):
                    continue

                full_section = f"{self.chapter_number}:{section_num}"  
                sections_to_delete[full_section] = (paragraph_index, section_text)  
        
        if not sections_to_delete:  
            print(f"No sections found to delete for chapter {self.chapter_number}")  
            return None, inlinedels_df.copy(), inlinedels_df.iloc[0:0].copy()
        
        print(f"Found {len(sections_to_delete)} section(s) to potentially delete for chapter {self.chapter_number}:")  
        for section in sorted(sections_to_delete.keys()):  
            para_idx, sec_text = sections_to_delete[section]  
            print(f"  - § {section} (at paragraph index {para_idx}, section: '{sec_text}')")  
        
        # Step 2: Confirmation - check if section text matches in next 10 rows  
        confirmed_sections = []  
        
        for section_num, (para_idx, section_text) in sections_to_delete.items():  
            range_start = para_idx + 1  
            range_end = para_idx + 10  
            
            next_rows = inlinedels_df[  
                (inlinedels_df['paragraph_index'] >= range_start) &  
                (inlinedels_df['paragraph_index'] <= range_end)  
            ]  
            
            section_confirmed = False  
            matching_indices = []  
            
            for _, next_row in next_rows.iterrows():  
                next_section = next_row.get('section', '')  
                if pd.notna(next_section) and str(next_section).strip() == str(section_text).strip():  
                    section_confirmed = True  
                    matching_indices.append(next_row['paragraph_index'])  
            
            if section_confirmed:  
                confirmed_sections.append(section_num)  
                print(f"✓ CONFIRMED: Section § {section_num} - section text '{section_text}' found in rows {matching_indices}")  
            else:  
                print(f"✗ SKIPPED: Section § {section_num} - section text '{section_text}' NOT found in next 10 rows ({range_start}-{range_end})")  
        
        if not confirmed_sections:  
            print(f"\n⚠ No sections confirmed for deletion. Exiting without changes.")  
            return None, inlinedels_df.copy(), inlinedels_df.iloc[0:0].copy()
        
        print(f"\n{'='*60}")  
        print(f"CONFIRMATION SUMMARY:")  
        print(f"{'='*60}")  
        print(f"Total sections found: {len(sections_to_delete)}")  
        print(f"Confirmed for deletion: {len(confirmed_sections)}")  
        print(f"\nSections to be deleted:")  
        for section in sorted(confirmed_sections):  
            print(f"  - § {section}")  
        print(f"{'='*60}\n")  
        
        # Step 3: Read file and process deletions  
          
        
        sections_with_positions = []  
        
        for section_num in confirmed_sections:  
            designator_pattern = rf'<designator[^>]*>{re.escape(section_num)}</designator>'  
            match = re.search(designator_pattern, content)  
            if match:  
                sections_with_positions.append((match.start(), section_num))  
            else:  
                print(f"⚠ Section {section_num} not found in PXML document")  
        
        sections_with_positions.sort(reverse=True)  
        
        removed_count = 0  
        for pos, section_num in sections_with_positions:  
            success, content = self._remove_section_by_designator_inline(content, section_num)  
            if success:  
                removed_count += 1  
        
        # with open(output_file, 'w', encoding='utf-8') as f:  
        #     f.write(content)  
        
        print(f"\n{'='*60}")  
        print(f"✓ Processing complete. Removed {removed_count} section(s)")  
        # print(f"✓ Modified PXML saved to: {output_file}")  
        print(f"{'='*60}")  
        
        # Create remaining deletions DataFrame  
        # remaining_df_inline_deletions = inlinedels_df[~inlinedels_df['section'].isin(confirmed_sections)].copy()
        def section_number_in_section(section_str, confirmed_sections):
            if pd.isna(section_str):
                return False
            for sec_num in confirmed_sections:
                # Match whole section number (e.g., "3:9") in the string
                if re.search(rf'\b{re.escape(sec_num)}\b', str(section_str)):
                    return True
            return False

        mask = inlinedels_df['section'].apply(lambda x: section_number_in_section(x, confirmed_sections))
        remaining_df_inline_deletions = inlinedels_df[~mask].copy()
        rows_to_delete_from_result = inlinedels_df[mask].copy()
        return content, remaining_df_inline_deletions, rows_to_delete_from_result
      
    
      
    def _remove_section_by_designator_inline(self, content, section_number):  
        """Remove section from content string"""  
        designator_pattern = rf'<designator[^>]*>{re.escape(section_number)}</designator>'  
        designator_match = re.search(designator_pattern, content)  
          
        if not designator_match:  
            print(f"⚠ Section {section_number} not found")  
            return False, content  
          
        search_start = designator_match.start()  
        content_before = content[:search_start]  
          
        # OLD: required uuid attribute, fails on PXML without uuid
        # section_pattern = r'<section\s+uuid="[^"]*"[^>]*>'
        # OLD: <section\b[^>]*> also matched <section.front>, <section.body> (wrong!)
        # NEW: lookahead ensures we only match <section> or <section ...>, NOT <section.front> etc.
        section_pattern = r'<section(?=[\s>])[^>]*>'  
        section_matches = list(re.finditer(section_pattern, content_before))  
          
        if not section_matches:  
            print(f"⚠ Could not find opening <section> for {section_number}")  
            return False, content  
          
        section_start_match = section_matches[-1]  
        section_start_pos = section_start_match.start()  
          
        closing_pos = self._find_matching_closing_tag(content, section_start_pos)  
          
        if closing_pos == -1:  
            print(f"⚠ Could not find closing </section> for {section_number}")  
            return False, content  
          
        modified_content = content[:section_start_pos] + '</trace.deleted>' +content[closing_pos:]  
        print(f"✓ Removed section § {section_number}")  
          
        return True, modified_content  
      
    @staticmethod  
    def _find_matching_closing_tag(content, start_pos):  
        """Find the matching </section> tag"""  
        pos = start_pos  
        depth = 0  
        remaining_content = content[pos:]  
        # OLD: <section\b[^>]*> also matched <section.front>, <section.body> (wrong!)
        # NEW: lookahead (?=[\s>]) ensures only <section> or <section attr> are counted,
        #      NOT <section.front>, <section.body> etc.
        combined_pattern = r'(<section(?=[\s>])[^>]*>|</section>)'  
          
        for match in re.finditer(combined_pattern, remaining_content):  
            tag = match.group(0)  
              
            if tag.startswith('<section'):  
                depth += 1  
            elif tag == '</section>':  
                depth -= 1  
                  
                if depth == 0:  
                    return pos + match.end()  
          
        return -1


# =====================================================  
# HELPER FUNCTIONS  
# =====================================================

def get_chapter_number(no_changes_df):  
    """  
    Extract chapter number from the text column of the dataframe.  
      
    Parameters:  
    -----------  
    no_changes_df : pandas.DataFrame  
        DataFrame with a 'text' column  
      
    Returns:  
    --------  
    str or None  
        The chapter number found, or None if not found  
    """  
    for text in no_changes_df['text']:  
        if pd.isna(text) or not text or not text.strip():  
            continue  
          
        text = str(text).strip()  
          
        # Pattern 1: "Chapter" followed by number (Chapter 7)  
        match = re.search(r'chapter\s+(\d+)', text, re.IGNORECASE)  
        if match:  
            return match.group(1)  
          
        # Pattern 2: Number followed by "Chapter" (7 Chapter)  
        match = re.search(r'(\d+)\s+chapter', text, re.IGNORECASE)  
        if match:  
            return match.group(1)  
          
        # Pattern 3: "Chapter" followed by Roman numerals (Chapter VII)  
        match = re.search(r'chapter\s+([ivxlcdm]+)', text, re.IGNORECASE)  
        if match:  
            return match.group(1).upper()  
          
        # Pattern 4: Roman numerals followed by "Chapter" (VII Chapter)  
        match = re.search(r'([ivxlcdm]+)\s+chapter', text, re.IGNORECASE)  
        if match:  
            return match.group(1).upper()  
      
    return None


# =====================================================  
# MAIN PROCESSING FUNCTION  
# =====================================================
def section_deletion_filling(result, df_changes, chapter_num, no_changes_df, df_remaining, pxml_content, verbose=True):
    # Get inline deletions  
    inlinedeletion_df = result[result['type'] == 'inline_deletion']  
    inline_replacement_df = result[result['type'] == 'inline_replacement']  
    no_changes_df = df_remaining[df_remaining["has_changes"] == False]  
      
    print(f"  - Inline deletions: {len(inlinedeletion_df)}")  
    print(f"  - Inline replacements: {len(inline_replacement_df)}")  
    print(f"  - No changes: {len(no_changes_df)}")  
       
      
    # Step 2: Analyze sections  
    print("\n[3/5] Analyzing sections...")  
    analyzer = SectionAnalyzer(chapter_num)  
      
    # Identify sections  
    inlinedeletion_df_with_sections = analyzer.identify_sections_with_names(inlinedeletion_df)  
    # inlinedeletion_df_with_sections.to_excel('checking\\inlinedeletion_with_sections.xlsx')
    # Propagate sections  
    inlinedels_df = analyzer.propagate_sections(inlinedeletion_df_with_sections, df_remaining)  
    # inlinedels_df.to_excel('checking\\inlinedels_df.xlsx')

    # Fill sections backward  
    # inlinedels_df = analyzer.fill_sections_backward(  
    #     inlinedels_df,  
    #     no_changes_df,  
    #     inline_replacement_df,  
    #     verbose=verbose  
    # )  
    # inlinedels_df.to_excel('checking\\inlinedels_df1.xlsx')
      
    # # Fill remaining sections  
    # inlinedels_df = analyzer.fill_remaining_sections_backward(  
    #     inlinedels_df,  
    #     df_remaining,  
    #     verbose=verbose  
    # )  
    # inlinedels_df.to_excel('checking\\inlinedels_df2.xlsx')
    print(f"✓ Section analysis complete")  
      
    # Step 3: Add context  
    print("\n[4/5] Adding context information...")  
    inlinedels_df = ContextExtractor.add_context_columns(  
        inlinedels_df,  
        no_changes_df,  
        df_changes  
    )  
    print(f"✓ Context added")  
    
    # inlinedels_df.to_excel('checking\\inlinedels_df2.xlsx')
    # Step 4: Remove sections from PXML  
    print("\n[5/5] Removing sections from PXML...")  
    remover = PXMLSectionRemover(chapter_number=chapter_num)  
    updatedpxmlcontent,remaining_df_inline_deletions, rows_to_delete = remover.remove_sections_from_dataframe(  
        inlinedels_df,  
        pxml_content
    )  
      
    if updatedpxmlcontent:  
        print(f"\n{'='*60}")  
        print(f"✓ Processing complete!")  
        # print(f"✓ Output saved to: {result_path}")  
        print(f"✓ Remaining inline deletions: {len(remaining_df_inline_deletions)}")  
        print(f"✓ Rows to delete from result: {len(rows_to_delete)}")  
        print(f"{'='*60}")  
        return updatedpxmlcontent, remaining_df_inline_deletions, rows_to_delete  
    else:  
        print(f"\n⚠ Processing completed but no sections were removed")  
        return pxml_content, remaining_df_inline_deletions, rows_to_delete
