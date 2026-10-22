import datetime as dt
import json
from logging import root
import re
import string
from zipfile import ZipFile
from collections import deque
import xml.etree.ElementTree as ET
import pandas as pd


class DocxChangeExtractor:
    """
    A class to extract tracked changes and paragraphs from DOCX files.
    """

    W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    NS = {"w": W_NS}

    def __init__(self, docx_path, context_chars=40, replacement_time_tol_sec=60):
        self.docx_path = docx_path
        self.context_chars = context_chars
        self.replacement_time_tol_sec = replacement_time_tol_sec

        self.changes = None
        self.all_paragraphs = None
        self.df_changes = None
        self.df_remaining = None
        self.no_changes_df = None
        self.result = None
        self.chapter_number = None

        # Built during build_before_after_df, used in enrich_footnote_anchors
        # fn_id (str) -> displayed superscript number (int)
        # e.g. {"20": 20, "21": 21, "30": 1}
        self._footnote_display_number_map = {}
        # fn_id (str) values whose anchor reference is inside w:ins / w:moveTo
        self._inserted_footnote_ids = set()
        self._style_font_sizes = {}
        self._doc_default_font_size = None

    # -----------------------------------------------------
    # UTILITIES
    # -----------------------------------------------------

    def _w(self, name):
        return f"{{{self.W_NS}}}{name}"

    def _run_is_vanish(self, run):
        """
        True if `run` is HIDDEN text (w:rPr/w:vanish) that is not toggled off.

        Word does not render hidden (vanish) text in the document view or the
        exported PDF, so it must be excluded from extracted insertion/deletion
        text. In these files the smart-quote wrappers (&ldquo; / &rdquo;) are
        emitted as vanish runs alongside the visible curly quotes; without this
        filter the hidden ones leak into the extracted text (and asymmetrically,
        since only one end may have a hidden twin).
        """
        rPr = run.find(self._w("rPr"))
        if rPr is None:
            return False
        v = rPr.find(self._w("vanish"))
        if v is None:
            return False
        val = v.attrib.get(self._w("val"), "true")
        return str(val).lower() not in ("0", "false", "off")

    def _get_all_text(self, elem):
        texts = []
        for run in elem.iter(self._w("r")):
            if self._run_is_vanish(run):
                continue
            for t in run.findall(self._w("t")):
                texts.append(t.text or "")
        return "".join(texts)

    def _get_all_deltext(self, elem):
        texts = []
        for run in elem.iter(self._w("r")):
            if self._run_is_vanish(run):
                continue
            for t in run.findall(self._w("delText")):
                texts.append(t.text or "")
        return "".join(texts)

    def _get_formatted_text(self, elem, text_tag="t"):
        """
        Walk runs inside elem, check w:rPr/w:i (italic) and w:rPr/w:b (bold),
        and wrap text in <ital>/<bold> tags accordingly.
        text_tag: 't' for normal text, 'delText' for deleted text.
        Adjacent runs with the same formatting are merged.
        """
        W = self.W_NS
        r_tag   = f"{{{W}}}r"
        t_tag   = f"{{{W}}}{text_tag}"
        rPr_tag = f"{{{W}}}rPr"
        i_tag   = f"{{{W}}}i"
        b_tag   = f"{{{W}}}b"

        segments = []  # list of (text, is_italic, is_bold)

        for run in elem.iter(r_tag):
            if self._run_is_vanish(run):
                continue  # hidden text is not rendered by Word / in the PDF
            is_italic = False
            is_bold   = False
            rPr = run.find(rPr_tag)
            if rPr is not None:
                i_elem = rPr.find(i_tag)
                if i_elem is not None:
                    val = i_elem.attrib.get(f"{{{W}}}val", "true")
                    is_italic = val.lower() not in ("0", "false")
                b_elem = rPr.find(b_tag)
                if b_elem is not None:
                    val = b_elem.attrib.get(f"{{{W}}}val", "true")
                    is_bold = val.lower() not in ("0", "false")
            run_text = ""
            for t in run.findall(t_tag):
                run_text += (t.text or "")
            if run_text:
                segments.append((run_text, is_italic, is_bold))

        if not segments:
            return ""

        # Merge adjacent segments with same formatting
        merged = [segments[0]]
        for txt, is_i, is_b in segments[1:]:
            if is_i == merged[-1][1] and is_b == merged[-1][2]:
                merged[-1] = (merged[-1][0] + txt, is_i, is_b)
            else:
                merged.append((txt, is_i, is_b))

        parts = []
        for txt, is_i, is_b in merged:
            if is_i and is_b:
                parts.append(f"<bold><ital>{txt}</ital></bold>")
            elif is_i:
                parts.append(f"<ital>{txt}</ital>")
            elif is_b:
                parts.append(f"<bold>{txt}</bold>")
            else:
                parts.append(txt)

        return "".join(parts)
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

    def _is_pure_del_paragraph(self, p):
        """True if paragraph contains only deletion segments (no normal or ins text)."""
        segs = self.iter_para_segments(p)
        if not segs:
            return False
        return all(s[0] == "del" for s in segs)

    def _is_pure_ins_paragraph(self, p):
        """True if paragraph contains only insertion segments (no normal or del text)."""
        segs = self.iter_para_segments(p)
        if not segs:
            return False
        return all(s[0] == "ins" for s in segs)

    # -----------------------------------------------------
    # HEADING STYLE LOADING
    # -----------------------------------------------------

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
                    # OOXML w:outlineLvl: val 0-8 => Heading 1-9; val 9 => body text (not a heading)
                    _v = int(outline_el.attrib[f"{{{self.W_NS}}}val"])
                    if 0 <= _v <= 8:
                        style_outline_level[style_id] = _v + 1
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
                # OOXML w:outlineLvl: val 0-8 => Heading 1-9; val 9 => body text (not a heading)
                _v = int(direct.attrib[f"{{{self.W_NS}}}val"])
                return (_v + 1) if 0 <= _v <= 8 else None
            except:
                pass

        pstyle = p.find("./w:pPr/w:pStyle", self.NS)
        if pstyle is not None:
            style_id = pstyle.attrib.get(f"{{{self.W_NS}}}val")
            return style_outline_level.get(style_id)

        return None

    def paragraph_text(self, p):
        fmt = self._get_formatted_text(p, text_tag="t")
        return self._normalize_whitespace(fmt) if fmt else self._normalize_whitespace(self._get_all_text(p))

    def paragraph_has_bullet_point(self, p):
        """
        Detect whether a paragraph is a list/bullet paragraph.
        - Primary signal: Word numbering properties (w:pPr/w:numPr).
        - Fallback signal: visible text starts with a bullet glyph.
        """
        numpr = p.find("./w:pPr/w:numPr", self.NS)
        if numpr is not None:
            return True

        txt = self.paragraph_text(p)
        if not txt:
            return False

        bullet_prefixes = ("•", "◦", "▪", "●", "○", "‣", "⁃")
        stripped = txt.lstrip()
        return stripped.startswith(bullet_prefixes)

    # -----------------------------------------------------
    # CONTENT WALKER
    # -----------------------------------------------------

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

        # ── Table row merging: yield all cell paragraphs as a single "table_row" ──
        if tag == f"{{{self.W_NS}}}tr":
            row_paras = []  # list of (p_elem, block_rev)
            for tc in elem.findall(f"{{{self.W_NS}}}tc"):
                for item in self.walk_content(tc, block_rev=block_rev):
                    if item[0] == "paragraph":
                        row_paras.append((item[1], item[2]))
            if row_paras:
                yield ("table_row", row_paras, block_rev)
            return

        for c in list(elem):
            yield from self.walk_content(c, block_rev=block_rev)

    # -----------------------------------------------------
    # INLINE SEGMENTS
    # -----------------------------------------------------

    def iter_para_segments(self, p):
        def dfs(n, kind=None, attrs=None):
            tag = n.tag

            # Hidden (vanish) normal runs are not rendered -> skip the whole run.
            if tag == f"{{{self.W_NS}}}r" and self._run_is_vanish(n):
                return

            if tag in {f"{{{self.W_NS}}}ins", f"{{{self.W_NS}}}moveTo"}:
                at = {
                    "author": n.attrib.get(f"{{{self.W_NS}}}author"),
                    "date": self._parse_dt_or_none(n.attrib.get(f"{{{self.W_NS}}}date")),
                    "id": n.attrib.get(f"{{{self.W_NS}}}id")
                }
                fmt = self._normalize_whitespace(self._get_formatted_text(n, text_tag="t"))
                txt = fmt if fmt else self._normalize_whitespace(self._get_all_text(n))
                if txt:
                    yield ("ins", txt, at)
                else:
                    # Whitespace-only insertion (e.g. a single inserted space).
                    # _normalize_whitespace strips it to '', but it IS a real change.
                    # Preserve it as a single space so the paragraph is flagged changed.
                    raw = self._get_all_text(n)
                    if raw and raw.strip() == "" and len(raw) > 0:
                        yield ("ins", " ", at)
                return

            if tag in {f"{{{self.W_NS}}}del", f"{{{self.W_NS}}}moveFrom"}:
                at = {
                    "author": n.attrib.get(f"{{{self.W_NS}}}author"),
                    "date": self._parse_dt_or_none(n.attrib.get(f"{{{self.W_NS}}}date")),
                    "id": n.attrib.get(f"{{{self.W_NS}}}id")
                }
                fmt = self._normalize_whitespace(self._get_formatted_text(n, text_tag="delText"))
                txt = fmt if fmt else (self._normalize_whitespace(self._get_all_deltext(n)) or self._normalize_whitespace(self._get_all_text(n)))
                if txt:
                    yield ("del", txt, at)
                return

            if tag == f"{{{self.W_NS}}}t":
                if kind is None:
                    yield ("text", self._normalize_whitespace(n.text or ""), None)
                elif kind == "ins":
                    if n.text and n.text.strip():
                        yield ("ins", self._normalize_whitespace(n.text), attrs)
                    elif n.text and n.text.strip() == "" and len(n.text) > 0:
                        # Whitespace-only inserted run — preserve as a single space
                        yield ("ins", " ", attrs)
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

    # -----------------------------------------------------
    # MAIN EXTRACTION FUNCTION
    # -----------------------------------------------------

    def extract_changes(self):
        with ZipFile(self.docx_path) as z:
            document_xml = z.read("word/document.xml")
            root = ET.fromstring(document_xml)
            style_outline_level, _ = self.load_heading_styles(z)

            footnote_map = {}
            try:
                xml_bytes = z.read("word/footnotes.xml")
                fn_root = ET.fromstring(xml_bytes)
                for fn in fn_root.findall("w:footnote", self.NS):
                    fn_id = fn.attrib.get(self._w("id"), "?")
                    try:
                        if int(fn_id) < 1:
                            continue
                    except ValueError:
                        pass
                    footnote_map[fn_id] = fn
            except KeyError:
                pass

        changes = []
        all_paragraphs = []
        para_counter = [0]

        def next_idx():
            idx = para_counter[0]
            para_counter[0] += 1
            return idx

        def _prev_text(context_paras):
            for p in reversed(context_paras[:-1]):
                if p["text"]:
                    return p["text"]
            return ""

        def process_paragraph(p, block_rev, section, context_paras,
                               is_footnote=False,
                               parent_paragraph_idx=None,
                               footnote_id=None):
            idx = next_idx()
            ptext = self.paragraph_text(p)
            has_bullet_point = self.paragraph_has_bullet_point(p)

            all_paragraphs.append({
                "idx": idx,
                "text": ptext,
                "section": section,
                "has_changes": False,
                "has_bullet_point": has_bullet_point,
                "is_footnote": is_footnote,
                "parent_paragraph_idx": parent_paragraph_idx,
                "footnote_id": footnote_id
            })
            context_paras.append({"idx": idx, "text": ptext})

            para_has_changes = False

            if block_rev and block_rev["type"] == "ins":
                changes.append({
                    "type": "inline_insertion",
                    "paragraph_index": idx,
                    "section": section,
                    "author": block_rev.get("author"),
                    "date": block_rev.get("date").isoformat() if block_rev.get("date") else None,
                    "has_bullet_point": has_bullet_point,
                    "inserted_text": ptext,
                    "deleted_text": None,
                    "prev_context": _prev_text(context_paras),
                    "post_context": ""
                })
                all_paragraphs[idx]["has_changes"] = True
                return idx, ptext

            if block_rev and block_rev["type"] == "del":
                fmt_del = self._normalize_whitespace(self._get_formatted_text(p, text_tag="delText"))
                deltext = fmt_del if fmt_del else (self._normalize_whitespace(self._get_all_deltext(p)) or ptext)
                changes.append({
                    "type": "inline_deletion",
                    "paragraph_index": idx,
                    "section": section,
                    "author": block_rev.get("author"),
                    "date": block_rev.get("date").isoformat() if block_rev.get("date") else None,
                    "has_bullet_point": has_bullet_point,
                    "inserted_text": None,
                    "deleted_text": deltext,
                    "prev_context": _prev_text(context_paras),
                    "post_context": ""
                })
                all_paragraphs[idx]["has_changes"] = True
                return idx, ptext

            segs = self.iter_para_segments(p)
            if not segs:
                return idx, ptext

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
                            changes.append({
                                "type": "inline_replacement",
                                "paragraph_index": idx,
                                "section": section,
                                "author": insattrs.get("author") or del_auth,
                                "date": (insattrs.get("date") or del_dt).isoformat() if (insattrs.get("date") or del_dt) else None,
                                "has_bullet_point": has_bullet_point,
                                "deleted_text": deltxt,
                                "inserted_text": instxt,
                                "prev_context": prev,
                                "post_context": post
                            })
                            cursor += len(instxt)
                            i = j + 1
                            continue

                    start = max(0, cursor - 1)
                    prev, post = ctx(start, 0)
                    changes.append({
                        "type": "inline_deletion",
                        "paragraph_index": idx,
                        "section": section,
                        "author": del_auth,
                        "date": del_dt.isoformat() if del_dt else None,
                        "has_bullet_point": has_bullet_point,
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

                    changes.append({
                        "type": "inline_insertion",
                        "paragraph_index": idx,
                        "section": section,
                        "author": ins_auth,
                        "date": ins_dt.isoformat() if ins_dt else None,
                        "has_bullet_point": has_bullet_point,
                        "deleted_text": None,
                        "inserted_text": instxt,
                        "prev_context": prev,
                        "post_context": post
                    })

                    cursor += len(instxt)
                    i += 1
                    continue

            if para_has_changes:
                all_paragraphs[idx]["has_changes"] = True

            return idx, ptext

        def process_footnote(fn_id, fn_elem, section_prefix, context_paras,
                              parent_paragraph_idx=None, is_inserted_ref=False):
            fn_section = (f"{section_prefix} > Footnote {fn_id}"
                          if section_prefix else f"Footnote {fn_id}")

            # Collect all footnote paragraphs to detect del+ins pairs
            fn_paras = []
            for kind, p, block_rev in self.walk_content(fn_elem):
                if kind != "paragraph":
                    continue
                # If the footnote reference was inside w:ins but the body
                # paragraph itself has no block_rev, treat it as an insertion
                # so it gets type="inline_insertion" instead of NaN.
                if is_inserted_ref and block_rev is None:
                    block_rev = {
                        "type": "ins",
                        "author": None,
                        "date": None,
                        "id": None
                    }
                fn_paras.append((p, block_rev))

            # Process paragraphs, merging consecutive pure-del + pure-ins pairs
            i = 0
            while i < len(fn_paras):
                p, block_rev = fn_paras[i]

                # Detect: current paragraph is pure deletion, next is pure insertion
                if i + 1 < len(fn_paras):
                    next_p, next_rev = fn_paras[i + 1]
                    cur_is_del = (block_rev and block_rev["type"] == "del") or \
                                 (block_rev is None and self._is_pure_del_paragraph(p))
                    next_is_ins = (next_rev and next_rev["type"] == "ins") or \
                                  (next_rev is None and self._is_pure_ins_paragraph(next_p))

                    if cur_is_del and next_is_ins:
                        # Merge into a single inline_replacement
                        idx = next_idx()
                        fmt_del = self._normalize_whitespace(self._get_formatted_text(p, text_tag="delText"))
                        deltext = fmt_del if fmt_del else (self._normalize_whitespace(self._get_all_deltext(p)) or self.paragraph_text(p))
                        instxt = self.paragraph_text(next_p)
                        has_bullet_point = self.paragraph_has_bullet_point(p)

                        all_paragraphs.append({
                            "idx": idx,
                            "text": instxt,
                            "section": fn_section,
                            "has_changes": True,
                            "has_bullet_point": has_bullet_point,
                            "is_footnote": True,
                            "parent_paragraph_idx": parent_paragraph_idx,
                            "footnote_id": fn_id
                        })
                        context_paras.append({"idx": idx, "text": instxt})

                        author = (next_rev.get("author") if next_rev else None) or \
                                 (block_rev.get("author") if block_rev else None)
                        date_val = (next_rev.get("date") if next_rev else None) or \
                                   (block_rev.get("date") if block_rev else None)

                        changes.append({
                            "type": "inline_replacement",
                            "paragraph_index": idx,
                            "section": fn_section,
                            "author": author,
                            "date": date_val.isoformat() if date_val else None,
                            "has_bullet_point": has_bullet_point,
                            "deleted_text": deltext,
                            "inserted_text": instxt,
                            "prev_context": "",
                            "post_context": ""
                        })
                        i += 2
                        continue

                process_paragraph(
                    p, block_rev, fn_section, context_paras,
                    is_footnote=True,
                    parent_paragraph_idx=parent_paragraph_idx,
                    footnote_id=fn_id
                )
                i += 1

        def process_body(body, section_prefix=""):
            current_section = [None] * 10
            context_paras = []
            processed_footnotes = set()

            for kind, p, block_rev in self.walk_content(body):
                if kind == "table_row":
                    # p is actually a list of (p_elem, block_rev) tuples for each cell
                    row_paras = p
                    # Merge all cell texts with tab separator into a single paragraph
                    cell_texts = []
                    merged_block_rev = block_rev
                    all_p_elems = []

                    for p_elem, cell_rev in row_paras:
                        cell_texts.append(self.paragraph_text(p_elem))
                        all_p_elems.append(p_elem)
                        if cell_rev and cell_rev.get("type") in ("ins", "del"):
                            merged_block_rev = cell_rev

                    # Create a synthetic merged paragraph record
                    merged_text = "\t".join(cell_texts)
                    idx = next_idx()
                    # Use first cell for bullet detection
                    has_bullet_point = self.paragraph_has_bullet_point(all_p_elems[0]) if all_p_elems else False

                    # Determine section_path for table row
                    tr_section_path = " > ".join(t for t in current_section[1:] if t)
                    if section_prefix:
                        tr_section_path = f"{section_prefix} > {tr_section_path}" if tr_section_path else section_prefix

                    all_paragraphs.append({
                        "idx": idx,
                        "text": merged_text,
                        "section": tr_section_path,
                        "has_changes": False,
                        "has_bullet_point": has_bullet_point,
                        "is_footnote": False,
                        "parent_paragraph_idx": None,
                        "footnote_id": None
                    })
                    context_paras.append({"idx": idx, "text": merged_text})

                    if merged_block_rev and merged_block_rev.get("type") == "ins":
                        changes.append({
                            "type": "inline_insertion",
                            "paragraph_index": idx,
                            "section": tr_section_path,
                            "author": merged_block_rev.get("author"),
                            "date": merged_block_rev.get("date").isoformat() if merged_block_rev.get("date") else None,
                            "has_bullet_point": has_bullet_point,
                            "inserted_text": merged_text,
                            "deleted_text": None,
                            "prev_context": _prev_text(context_paras),
                            "post_context": ""
                        })
                        all_paragraphs[idx]["has_changes"] = True
                    elif merged_block_rev and merged_block_rev.get("type") == "del":
                        # Merge deleted text from all cells
                        del_texts = []
                        for p_elem in all_p_elems:
                            fmt_del = self._normalize_whitespace(self._get_formatted_text(p_elem, text_tag="delText"))
                            del_texts.append(fmt_del if fmt_del else self._normalize_whitespace(self._get_all_deltext(p_elem)))
                        merged_del = "\t".join(del_texts)
                        changes.append({
                            "type": "inline_deletion",
                            "paragraph_index": idx,
                            "section": tr_section_path,
                            "author": merged_block_rev.get("author"),
                            "date": merged_block_rev.get("date").isoformat() if merged_block_rev.get("date") else None,
                            "has_bullet_point": has_bullet_point,
                            "deleted_text": merged_del,
                            "inserted_text": None,
                            "prev_context": _prev_text(context_paras),
                            "post_context": ""
                        })
                        all_paragraphs[idx]["has_changes"] = True
                    else:
                        # Process inline changes within each cell and record them
                        for p_elem in all_p_elems:
                            segs = self.iter_para_segments(p_elem)
                            visible = "".join(s[1] for s in segs if s[0] in ("text", "ins"))

                            cursor = 0
                            for seg_kind, seg_text, seg_attrs in segs:
                                if seg_kind == "text":
                                    cursor += len(seg_text)
                                elif seg_kind == "ins":
                                    all_paragraphs[idx]["has_changes"] = True
                                    changes.append({
                                        "type": "inline_insertion",
                                        "paragraph_index": idx,
                                        "section": tr_section_path,
                                        "author": seg_attrs.get("author") if seg_attrs else None,
                                        "date": seg_attrs.get("date").isoformat() if seg_attrs and seg_attrs.get("date") else None,
                                        "has_bullet_point": has_bullet_point,
                                        "deleted_text": None,
                                        "inserted_text": seg_text,
                                        "prev_context": visible[max(0, cursor - self.context_chars):cursor],
                                        "post_context": visible[cursor + len(seg_text):cursor + len(seg_text) + self.context_chars]
                                    })
                                    cursor += len(seg_text)
                                elif seg_kind == "del":
                                    all_paragraphs[idx]["has_changes"] = True
                                    changes.append({
                                        "type": "inline_deletion",
                                        "paragraph_index": idx,
                                        "section": tr_section_path,
                                        "author": seg_attrs.get("author") if seg_attrs else None,
                                        "date": seg_attrs.get("date").isoformat() if seg_attrs and seg_attrs.get("date") else None,
                                        "has_bullet_point": has_bullet_point,
                                        "deleted_text": seg_text,
                                        "inserted_text": None,
                                        "prev_context": visible[max(0, cursor - self.context_chars):cursor],
                                        "post_context": visible[cursor:cursor + self.context_chars]
                                    })

                    # Process footnotes from table cells
                    for p_elem in all_p_elems:
                        fn_refs = self._get_visible_footnote_refs(p_elem, footnote_map, processed_footnotes)
                        ins_fn_ids = self._get_inserted_footnote_ref_ids(p_elem, footnote_map)
                        for fn_id in fn_refs:
                            processed_footnotes.add(fn_id)
                            process_footnote(
                                fn_id, footnote_map[fn_id], tr_section_path, context_paras,
                                parent_paragraph_idx=idx,
                                is_inserted_ref=(fn_id in ins_fn_ids)
                            )
                        # Also process deleted footnote refs in table cells
                        del_fn_refs = self._get_deleted_footnote_refs(p_elem, footnote_map, processed_footnotes)
                        for fn_id in del_fn_refs:
                            processed_footnotes.add(fn_id)
                            process_footnote(
                                fn_id, footnote_map[fn_id], tr_section_path, context_paras,
                                parent_paragraph_idx=idx
                            )

                    continue

                if kind != "paragraph":
                    continue

                ptext = self.paragraph_text(p)
                lvl = self.paragraph_outline_level(p, style_outline_level)

                if lvl and 1 <= lvl < len(current_section):
                    current_section[lvl] = ptext
                    for k in range(lvl + 1, len(current_section)):
                        current_section[k] = None

                section_path = " > ".join(t for t in current_section[1:] if t)
                if section_prefix:
                    section_path = f"{section_prefix} > {section_path}" if section_path else section_prefix

                fn_refs = self._get_visible_footnote_refs(p, footnote_map, processed_footnotes)
                ins_fn_ids = self._get_inserted_footnote_ref_ids(p, footnote_map)

                body_idx, _ = process_paragraph(p, block_rev, section_path, context_paras)

                for fn_id in fn_refs:
                    processed_footnotes.add(fn_id)
                    process_footnote(
                        fn_id, footnote_map[fn_id], section_path, context_paras,
                        parent_paragraph_idx=body_idx,
                        is_inserted_ref=(fn_id in ins_fn_ids)
                    )

                # Also process footnotes whose references are inside w:del
                del_fn_refs = self._get_deleted_footnote_refs(p, footnote_map, processed_footnotes)
                for fn_id in del_fn_refs:
                    processed_footnotes.add(fn_id)
                    process_footnote(
                        fn_id, footnote_map[fn_id], section_path, context_paras,
                        parent_paragraph_idx=body_idx
                    )

        body = root.find("w:body", self.NS)
        process_body(body, section_prefix="")

        return changes, all_paragraphs

    # -----------------------------------------------------
    # CHAPTER NUMBER EXTRACTION
    # -----------------------------------------------------

    @staticmethod
    def get_chapter_number(no_changes_df):
        for text in no_changes_df['text']:
            if pd.isna(text) or not text or not text.strip():
                continue

            text = str(text).strip()

            match = re.search(r'chapter\s+(\d+)', text, re.IGNORECASE)
            if match:
                return match.group(1)

            match = re.search(r'(\d+)\s+chapter', text, re.IGNORECASE)
            if match:
                return match.group(1)

            match = re.search(r'chapter\s+([ivxlcdm]+)', text, re.IGNORECASE)
            if match:
                return match.group(1).upper()

            match = re.search(r'([ivxlcdm]+)\s+chapter', text, re.IGNORECASE)
            if match:
                return match.group(1).upper()

        return None

    # -----------------------------------------------------
    # ── CORE FIX: BUILD FOOTNOTE DISPLAY NUMBER MAP ──
    # -----------------------------------------------------

    def _collect_footnote_refs_in_order(self, elem, footnote_map, result, visited):
        """
        Recursively walks elem in XML document order.
        Skips entire w:del / w:moveFrom subtrees (deleted content not visible).
        Collects w:footnoteReference ids in the order Word would display them.

        This is the KEY difference from using p.iter():
            p.iter() finds ALL descendants including inside w:del
            This method SKIPS w:del subtrees entirely, matching Word's rendering
        """
        del_tags = {self._w("del"), self._w("moveFrom")}
        ref_tag  = self._w("footnoteReference")
        r_tag    = self._w("r")

        tag = elem.tag

        # ── Skip deleted content entirely ──
        # Deleted footnote references must NOT be counted
        if tag in del_tags:
            return

        # ── Found a run — check for footnoteReference ──
        if tag == r_tag:
            ref = elem.find(ref_tag)
            if ref is not None:
                ref_id = ref.attrib.get(self._w("id"))
                if ref_id and ref_id in footnote_map and ref_id not in visited:
                    visited.add(ref_id)
                    result.append(ref_id)
            return

        # ── Recurse into children in document order ──
        for child in elem:
            self._collect_footnote_refs_in_order(child, footnote_map, result, visited)

    def _get_visible_footnote_refs(self, p, footnote_map, processed_footnotes):
        """
        Returns list of footnote ref IDs that are VISIBLE (not inside w:del / w:moveFrom).
        Uses recursive walk — same logic as _collect_footnote_refs_in_order — so
        deleted footnote references (e.g. old IDs like 167, 256) are never included.
        """
        del_tags = {self._w("del"), self._w("moveFrom")}
        ins_like = {self._w("ins"), self._w("moveTo")}
        ref_tag  = self._w("footnoteReference")
        r_tag    = self._w("r")
        result   = []

        def walk(elem):
            tag = elem.tag
            if tag in del_tags:
                return  # skip entire deleted subtree
            if tag == r_tag:
                ref = elem.find(ref_tag)
                if ref is not None:
                    ref_id = ref.attrib.get(self._w("id"))
                    if ref_id and ref_id in footnote_map and ref_id not in processed_footnotes:
                        result.append(ref_id)
                return
            for child in elem:
                walk(child)

        walk(p)
        return result

    def _get_inserted_footnote_ref_ids(self, p, footnote_map):
        """
        Returns set of footnote ref IDs whose <w:footnoteReference> is inside a
        w:ins / w:moveTo element in paragraph p.  Used to determine that the
        footnote body should be treated as newly inserted content.
        """
        ins_tags = {self._w("ins"), self._w("moveTo")}
        del_tags = {self._w("del"), self._w("moveFrom")}
        ref_tag  = self._w("footnoteReference")
        r_tag    = self._w("r")
        result   = set()

        def walk(elem, inside_ins=False):
            tag = elem.tag
            if tag in del_tags:
                return
            if tag in ins_tags:
                for child in elem:
                    walk(child, inside_ins=True)
                return
            if tag == r_tag:
                ref = elem.find(ref_tag)
                if ref is not None and inside_ins:
                    ref_id = ref.attrib.get(self._w("id"))
                    if ref_id and ref_id in footnote_map:
                        result.add(ref_id)
                return
            for child in elem:
                walk(child, inside_ins=inside_ins)

        walk(p)
        return result

    def _get_deleted_footnote_refs(self, p, footnote_map, processed_footnotes):
        """
        Returns list of footnote ref IDs that are INSIDE w:del / w:moveFrom subtrees.
        These are footnotes whose references were deleted — their body content still
        needs to be extracted so downstream processing can mark them as deleted.
        """
        del_tags = {self._w("del"), self._w("moveFrom")}
        ref_tag  = self._w("footnoteReference")
        r_tag    = self._w("r")
        result   = []

        def walk_deleted(elem):
            """Walk only inside deleted subtrees looking for footnoteReference."""
            tag = elem.tag
            if tag == r_tag:
                ref = elem.find(ref_tag)
                if ref is not None:
                    ref_id = ref.attrib.get(self._w("id"))
                    if ref_id and ref_id in footnote_map and ref_id not in processed_footnotes:
                        result.append(ref_id)
                return
            for child in elem:
                walk_deleted(child)

        def walk(elem):
            """Top-level walk: only descend into w:del subtrees."""
            tag = elem.tag
            if tag in del_tags:
                walk_deleted(elem)
                return
            for child in elem:
                walk(child)

        walk(p)
        return result

    def _build_footnote_display_number_map(self, root, footnote_map):
        """
        Builds fn_id (str) -> displayed superscript number (int).
    
        Word uses TWO separate counters in Track Changes view:
          1. Normal counter (numStart=1, continuous) for existing footnotes
             whose w:footnoteReference is NOT inside w:ins
          2. A SEPARATE counter starting from 1 for newly INSERTED footnotes
             whose w:footnoteReference IS inside w:ins
    
        This is why fn_id "20" -> 20, "21" -> 21 (existing)
        but fn_id "30" -> 1 (newly inserted, separate counter)
        """
        num_start = 1
        num_restart = "continuous"
    
        try:
            with ZipFile(self.docx_path) as z:
                settings_xml = z.read("word/settings.xml")
                settings_root = ET.fromstring(settings_xml)
                fn_pr = settings_root.find(".//w:footnotePr", self.NS)
                if fn_pr is not None:
                    el = fn_pr.find("w:numStart", self.NS)
                    if el is not None:
                        v = el.attrib.get(self._w("val"))
                        if v:
                            num_start = int(v)
                    el = fn_pr.find("w:numRestart", self.NS)
                    if el is not None:
                        v = el.attrib.get(self._w("val"))
                        if v:
                            num_restart = v
        except Exception:
            pass
    
        print(f"[DEBUG] numStart={num_start}  numRestart={num_restart}")
    
        display_number_map = {}
        body = root.find("w:body", self.NS)
    
        ins_tags = {self._w("ins"), self._w("moveTo")}
        del_tags = {self._w("del"), self._w("moveFrom")}
    
        # ── Walk body and collect refs with their insertion status ──
        # Each entry: (fn_id_str, is_inserted: bool)
        ordered_refs = []
        visited = set()
    
        def walk_elem(elem, inside_ins=False, inside_del=False):
            tag = elem.tag
    
            # Skip deleted content — deleted refs are not displayed
            if tag in del_tags:
                return
    
            # Track if we enter an insertion wrapper
            if tag in ins_tags:
                for child in elem:
                    walk_elem(child, inside_ins=True, inside_del=inside_del)
                return
    
            # Found a run — check for footnoteReference
            if tag == self._w("r"):
                ref = elem.find(self._w("footnoteReference"))
                if ref is not None:
                    ref_id = ref.attrib.get(self._w("id"))
                    if ref_id and ref_id in footnote_map and ref_id not in visited:
                        visited.add(ref_id)
                        ordered_refs.append((ref_id, inside_ins))
                return
    
            # Recurse
            for child in elem:
                walk_elem(child, inside_ins=inside_ins, inside_del=inside_del)
    
        for child in body:
            walk_elem(child)
    
        print(f"[DEBUG] ordered_refs = {ordered_refs}")
    
        # ── Assign display numbers using TWO separate counters ──
        # Counter 1: for existing (non-inserted) footnote references
        existing_counter = num_start
    
        # Counter 2: for newly inserted footnote references (always starts at 1)
        inserted_counter = 1
        inserted_footnote_ids = set()
    
        for fn_id, is_inserted in ordered_refs:
            if is_inserted:
                display_number_map[fn_id] = inserted_counter
                inserted_counter += 1
                inserted_footnote_ids.add(fn_id)
            else:
                display_number_map[fn_id] = existing_counter
                existing_counter += 1

        self._inserted_footnote_ids = inserted_footnote_ids
    
        print(f"[DEBUG] display_number_map = {display_number_map}")
        print(f"[DEBUG] inserted_footnote_ids = {sorted(self._inserted_footnote_ids)}")
        return display_number_map
    # -----------------------------------------------------
    # BUILD BEFORE/AFTER DF
    # -----------------------------------------------------

    def _load_style_font_sizes(self, zipf) -> tuple[dict, float | None]:
        """
        Returns:
            style_font_sizes : dict[style_id -> font_size_pt (float)]
            doc_default_size : float | None   (fallback for everything)
        """
        style_font_sizes = {}
        doc_default_size = None
        W = self.W_NS

        try:
            styles_xml = zipf.read("word/styles.xml")
            styles_root = ET.fromstring(styles_xml)

            doc_defaults = styles_root.find(f"{{{W}}}docDefaults", {})
            if doc_defaults is None:
                doc_defaults = styles_root.find("w:docDefaults", self.NS)
            if doc_defaults is not None:
                sz_el = doc_defaults.find(f".//{{{W}}}sz")
                if sz_el is not None:
                    val = sz_el.attrib.get(f"{{{W}}}val")
                    if val:
                        try:
                            doc_default_size = int(val) / 2
                        except ValueError:
                            pass

            raw = {}
            based = {}

            for s in styles_root.findall(f"{{{W}}}style"):
                sid = s.attrib.get(f"{{{W}}}styleId")
                if not sid:
                    continue
                based_on = s.find(f"{{{W}}}basedOn")
                based[sid] = based_on.attrib.get(f"{{{W}}}val") if based_on is not None else None

                sz_el = s.find(f".//{{{W}}}sz")
                if sz_el is not None:
                    val = sz_el.attrib.get(f"{{{W}}}val")
                    if val:
                        try:
                            raw[sid] = int(val) / 2
                            continue
                        except ValueError:
                            pass
                raw[sid] = None

            def resolve(sid, seen=None):
                if seen is None:
                    seen = set()
                if sid in seen:
                    return None
                seen.add(sid)
                if raw.get(sid) is not None:
                    return raw[sid]
                parent = based.get(sid)
                if parent:
                    return resolve(parent, seen)
                return doc_default_size

            for sid in raw:
                style_font_sizes[sid] = resolve(sid)

        except KeyError:
            pass

        return style_font_sizes, doc_default_size

    def _get_paragraph_font_size(self, p) -> float | None:
        """
        Return the effective font size (points) for paragraph p, resolving
        the full inheritance chain:

            1. Explicit w:sz on the first visible run's w:rPr
            2. Paragraph-level w:rPr w:sz  (rPr directly under w:pPr)
            3. The paragraph's applied style (w:pStyle -> styles.xml)
            4. Document default size

        Skips runs inside w:del/w:moveFrom so it reflects the 'after' state.
        """
        W = self.W_NS
        del_tags = {self._w('del'), self._w('moveFrom')}
        r_tag = f'{{{W}}}r'
        rPr_tag = f'{{{W}}}rPr'
        sz_tag = f'{{{W}}}sz'
        szCs_tag = f'{{{W}}}szCs'

        def _sz_from_rPr(rPr_elem):
            if rPr_elem is None:
                return None
            sz = rPr_elem.find(sz_tag)
            if sz is None:
                sz = rPr_elem.find(szCs_tag)
            if sz is not None:
                val = sz.attrib.get(f'{{{W}}}val')
                if val:
                    try:
                        return int(val) / 2
                    except ValueError:
                        pass
            return None

        def _walk_runs(node):
            if node.tag in del_tags:
                return None
            if node.tag == r_tag:
                rPr = node.find(rPr_tag)
                result = _sz_from_rPr(rPr)
                return result if result is not None else -1
            for child in node:
                result = _walk_runs(child)
                if result is not None:
                    return result
            return None

        run_result = _walk_runs(p)
        if run_result is not None and run_result != -1:
            return run_result

        pPr = p.find(f'{{{W}}}pPr')
        if pPr is not None:
            para_rPr = pPr.find(rPr_tag)
            sz = _sz_from_rPr(para_rPr)
            if sz is not None:
                return sz

        style_map = getattr(self, '_style_font_sizes', {})
        if pPr is not None:
            pStyle = pPr.find(f'{{{W}}}pStyle')
            if pStyle is not None:
                sid = pStyle.attrib.get(f'{{{W}}}val')
                if sid and sid in style_map and style_map[sid] is not None:
                    return style_map[sid]

        doc_default = getattr(self, '_doc_default_font_size', None)
        return doc_default

    def build_before_after_df(self):
        if self.result is None:
            raise RuntimeError("Call process() before build_before_after_df().")

        para_records = []
        para_counter = [0]

        def next_idx():
            idx = para_counter[0]
            para_counter[0] += 1
            return idx

        with ZipFile(self.docx_path) as z:
            document_xml = z.read("word/document.xml")
            root = ET.fromstring(document_xml)
            style_outline_level, _ = self.load_heading_styles(z)
            self._style_font_sizes, self._doc_default_font_size = self._load_style_font_sizes(z)

            footnote_map = {}
            try:
                xml_bytes = z.read("word/footnotes.xml")
                fn_root = ET.fromstring(xml_bytes)
                for fn in fn_root.findall("w:footnote", self.NS):
                    fn_id = fn.attrib.get(self._w("id"), "?")
                    try:
                        if int(fn_id) < 1:
                            continue
                    except ValueError:
                        pass
                    footnote_map[fn_id] = fn
            except KeyError:
                pass

        # ── Build and store display number map ──
        self._footnote_display_number_map = self._build_footnote_display_number_map(
            root, footnote_map
        )

        def handle_paragraph(p, is_footnote=False,
                              parent_paragraph_idx=None,
                              footnote_id=None):
            idx = next_idx()
            ba = self._reconstruct_para_before_after(p)
            para_records.append({
                "idx": idx,
                "text_before": ba["before"],
                "text_after": ba["after"],
                "text_after_font_size": self._get_paragraph_font_size(p),
                "is_footnote": is_footnote,
                "parent_paragraph_idx": parent_paragraph_idx,
                "footnote_id": footnote_id
            })
            return idx

        def handle_footnote(fn_elem, fn_id, parent_paragraph_idx=None):
            # Collect all footnote paragraphs to detect del+ins pairs
            fn_paras = []
            for kind, p, block_rev in self.walk_content(fn_elem):
                if kind == "paragraph":
                    fn_paras.append((p, block_rev))

            # Process paragraphs, merging consecutive pure-del + pure-ins pairs
            i = 0
            while i < len(fn_paras):
                p, block_rev = fn_paras[i]

                # Detect: current paragraph is pure deletion, next is pure insertion
                if i + 1 < len(fn_paras):
                    next_p, next_rev = fn_paras[i + 1]
                    cur_is_del = (block_rev and block_rev["type"] == "del") or \
                                 (block_rev is None and self._is_pure_del_paragraph(p))
                    next_is_ins = (next_rev and next_rev["type"] == "ins") or \
                                  (next_rev is None and self._is_pure_ins_paragraph(next_p))

                    if cur_is_del and next_is_ins:
                        # Merge into single record: text_before from del, text_after from ins
                        idx = next_idx()
                        fmt_del = self._normalize_whitespace(self._get_formatted_text(p, text_tag="delText"))
                        del_text = fmt_del if fmt_del else (self._normalize_whitespace(self._get_all_deltext(p)) or self.paragraph_text(p))
                        ins_text = self.paragraph_text(next_p)

                        para_records.append({
                            "idx": idx,
                            "text_before": del_text,
                            "text_after": ins_text,
                            "text_after_font_size": self._get_paragraph_font_size(next_p),
                            "is_footnote": True,
                            "parent_paragraph_idx": parent_paragraph_idx,
                            "footnote_id": fn_id
                        })
                        i += 2
                        continue

                handle_paragraph(
                    p,
                    is_footnote=True,
                    parent_paragraph_idx=parent_paragraph_idx,
                    footnote_id=fn_id
                )
                i += 1

        processed_footnotes = set()

        body = root.find("w:body", self.NS)
        for kind, p, block_rev in self.walk_content(body):
            if kind == "table_row":
                # p is a list of (p_elem, cell_block_rev) tuples
                row_paras = p
                cell_texts_before = []
                cell_texts_after = []
                all_p_elems = []
                merged_block_rev = block_rev

                for p_elem, cell_rev in row_paras:
                    all_p_elems.append(p_elem)
                    if cell_rev:
                        merged_block_rev = cell_rev

                if merged_block_rev and merged_block_rev.get("type") == "del":
                    idx = next_idx()
                    for p_elem in all_p_elems:
                        fmt_del = self._normalize_whitespace(self._get_formatted_text(p_elem, text_tag="delText"))
                        cell_texts_before.append(fmt_del if fmt_del else (self._normalize_whitespace(self._get_all_deltext(p_elem)) or self.paragraph_text(p_elem)))
                    para_records.append({
                        "idx": idx,
                        "text_before": "\t".join(cell_texts_before),
                        "text_after": "",
                        "text_after_font_size": None,
                        "is_footnote": False,
                        "parent_paragraph_idx": None,
                        "footnote_id": None
                    })
                elif merged_block_rev and merged_block_rev.get("type") == "ins":
                    idx = next_idx()
                    for p_elem in all_p_elems:
                        cell_texts_after.append(self.paragraph_text(p_elem))
                    para_records.append({
                        "idx": idx,
                        "text_before": "",
                        "text_after": "\t".join(cell_texts_after),
                        "text_after_font_size": self._get_paragraph_font_size(all_p_elems[0]) if all_p_elems else None,
                        "is_footnote": False,
                        "parent_paragraph_idx": None,
                        "footnote_id": None
                    })
                else:
                    idx = next_idx()
                    for p_elem in all_p_elems:
                        ba = self._reconstruct_para_before_after(p_elem)
                        cell_texts_before.append(ba["before"])
                        cell_texts_after.append(ba["after"])
                    joined_before = "\t".join(cell_texts_before)
                    joined_after = "\t".join(cell_texts_after)
                    # If all cells are empty, don't leave a bare tab separator
                    if not joined_before.replace("\t", "").strip():
                        joined_before = ""
                    if not joined_after.replace("\t", "").strip():
                        joined_after = ""
                    para_records.append({
                        "idx": idx,
                        "text_before": joined_before,
                        "text_after": joined_after,
                        "text_after_font_size": self._get_paragraph_font_size(all_p_elems[0]) if all_p_elems else None,
                        "is_footnote": False,
                        "parent_paragraph_idx": None,
                        "footnote_id": None
                    })

                # Process footnotes from table cells
                for p_elem in all_p_elems:
                    fn_refs = self._get_visible_footnote_refs(p_elem, footnote_map, processed_footnotes)
                    for fn_id in fn_refs:
                        processed_footnotes.add(fn_id)
                        handle_footnote(
                            footnote_map[fn_id], fn_id,
                            parent_paragraph_idx=idx
                        )
                    # Also process deleted footnote refs
                    del_fn_refs = self._get_deleted_footnote_refs(p_elem, footnote_map, processed_footnotes)
                    for fn_id in del_fn_refs:
                        processed_footnotes.add(fn_id)
                        handle_footnote(
                            footnote_map[fn_id], fn_id,
                            parent_paragraph_idx=idx
                        )
                continue

            if kind != "paragraph":
                continue

            fn_refs = self._get_visible_footnote_refs(p, footnote_map, processed_footnotes)

            if block_rev and block_rev["type"] == "del":
                idx = next_idx()
                fmt_del = self._normalize_whitespace(self._get_formatted_text(p, text_tag="delText"))
                deltext = fmt_del if fmt_del else (self._normalize_whitespace(self._get_all_deltext(p))
                           or self.paragraph_text(p))
                para_records.append({
                    "idx": idx,
                    "text_before": deltext,
                    "text_after": "",
                    "text_after_font_size": None,
                    "is_footnote": False,
                    "parent_paragraph_idx": None,
                    "footnote_id": None
                })
            elif block_rev and block_rev["type"] == "ins":
                idx = next_idx()
                para_records.append({
                    "idx": idx,
                    "text_before": "",
                    "text_after": self.paragraph_text(p),
                    "text_after_font_size": self._get_paragraph_font_size(p),
                    "is_footnote": False,
                    "parent_paragraph_idx": None,
                    "footnote_id": None
                })
            else:
                idx = handle_paragraph(p)

            for fn_id in fn_refs:
                processed_footnotes.add(fn_id)
                handle_footnote(
                    footnote_map[fn_id], fn_id,
                    parent_paragraph_idx=idx
                )

            # Also process deleted footnote refs
            del_fn_refs = self._get_deleted_footnote_refs(p, footnote_map, processed_footnotes)
            for fn_id in del_fn_refs:
                processed_footnotes.add(fn_id)
                handle_footnote(
                    footnote_map[fn_id], fn_id,
                    parent_paragraph_idx=idx
                )

        ba_df = pd.DataFrame(para_records)
        self.result = self.result.merge(ba_df, on="idx", how="left")
        return self.result

    # -----------------------------------------------------
    # RECONSTRUCT BEFORE/AFTER
    # -----------------------------------------------------

    def _reconstruct_para_before_after(self, p):
        W = self.W_NS
        ins_tags = {self._w("ins"), self._w("moveTo")}
        del_tags = {self._w("del"), self._w("moveFrom")}
        r_tag    = f"{{{W}}}r"
        t_tag    = f"{{{W}}}t"
        dt_tag   = f"{{{W}}}delText"
        rPr_tag  = f"{{{W}}}rPr"
        i_tag    = f"{{{W}}}i"
        b_tag    = f"{{{W}}}b"

        def _run_formatting(run):
            """Return (is_italic, is_bold) for a run element."""
            is_italic = False
            is_bold   = False
            rPr = run.find(rPr_tag)
            if rPr is not None:
                ie = rPr.find(i_tag)
                if ie is not None:
                    val = ie.attrib.get(f"{{{W}}}val", "true")
                    is_italic = val.lower() not in ("0", "false")
                be = rPr.find(b_tag)
                if be is not None:
                    val = be.attrib.get(f"{{{W}}}val", "true")
                    is_bold = val.lower() not in ("0", "false")
            return is_italic, is_bold

        def _wrap(text, is_i, is_b):
            if is_i and is_b:
                return f"<bold><ital>{text}</ital></bold>"
            if is_i:
                return f"<ital>{text}</ital>"
            if is_b:
                return f"<bold>{text}</bold>"
            return text

        def collect_formatted(node, mode, inside_ins=False, inside_del=False):
            tag = node.tag
            segments = []  # list of (text, is_italic, is_bold)

            if tag in ins_tags:
                if mode == "after":
                    for child in node:
                        segments.extend(collect_formatted(child, mode, inside_ins=True, inside_del=inside_del))
                return segments

            if tag in del_tags:
                if mode == "before":
                    for child in node:
                        segments.extend(collect_formatted(child, mode, inside_ins=inside_ins, inside_del=True))
                return segments

            if tag == r_tag:
                if self._run_is_vanish(node):
                    return segments  # hidden text is not rendered by Word / in the PDF
                is_i, is_b = _run_formatting(node)
                text_tag = dt_tag if inside_del else t_tag
                run_text = ""
                for t in node.findall(text_tag):
                    run_text += (t.text or "")
                if not run_text and inside_del:
                    # fallback: try w:t if delText not found
                    for t in node.findall(t_tag):
                        run_text += (t.text or "")
                if run_text:
                    segments.append((run_text, is_i, is_b))
                return segments

            for child in node:
                segments.extend(collect_formatted(child, mode, inside_ins=inside_ins, inside_del=inside_del))

            return segments

        def _merge_and_build(segments):
            if not segments:
                return ""
            merged = [segments[0]]
            for txt, is_i, is_b in segments[1:]:
                if is_i == merged[-1][1] and is_b == merged[-1][2]:
                    merged[-1] = (merged[-1][0] + txt, is_i, is_b)
                else:
                    merged.append((txt, is_i, is_b))
            return self._normalize_whitespace("".join(_wrap(t, i, b) for t, i, b in merged))

        before_segs = collect_formatted(p, mode="before")
        after_segs  = collect_formatted(p, mode="after")

        before = _merge_and_build(before_segs)
        after  = _merge_and_build(after_segs)

        # ── Trivial-separator guard ──────────────────────────────────────────
        # Some source documents leave a separator run (em/en dash, bullet, §,
        # etc.) as UNCHANGED text between fully-inserted runs — e.g. a case-law
        # entry "<ins>citation</ins> — <ins>summary</ins>" where the "—" is not
        # wrapped in <w:ins>. That untracked separator leaks into "before" and
        # makes a genuine NEW-paragraph insertion look like an inline edit.
        # If the pre-edit "before" content has NO alphanumeric characters (only
        # dashes/bullets/punctuation/whitespace) while "after" DOES have real
        # content, treat "before" as empty so the row is classified as a new
        # paragraph insertion (text_before empty, text_after present).
        if before:
            before_plain = re.sub(r"<[^>]+>", "", before)
            after_plain  = re.sub(r"<[^>]+>", "", after)
            if (not re.search(r"[A-Za-z0-9]", before_plain)
                    and re.search(r"[A-Za-z0-9]", after_plain)):
                before = ""

        return {"before": before, "after": after}

    # -----------------------------------------------------
    # FOOTNOTE ANCHOR EXTRACTION
    # -----------------------------------------------------

    def get_footnote_anchor_text(self, p, fn_id):
        """
        Given a paragraph element and a footnote XML id (str), returns:
            - text_before_superscript      : ALL text (visible + deleted) before the superscript
            - last_word_before_superscript : last word of that text

        Collects both w:t (normal text) and w:delText (deleted text) so the
        result matches what appears in the OLD PDF (pre-changes).
        """
        fn_id        = str(fn_id)
        ref_tag      = self._w("footnoteReference")
        r_tag        = self._w("r")
        t_tag        = self._w("t")
        del_text_tag = self._w("delText")

        def _walk(elem):
            """
            Walk paragraph collecting ALL text (visible + deleted) until we
            find the target footnoteReference. Returns True if found.
            """
            tag = elem.tag

            # Found a run — check for footnoteReference, then collect text
            if tag == r_tag:
                ref = elem.find(ref_tag)
                if ref is not None:
                    ref_id = ref.attrib.get(self._w("id"))
                    if ref_id == fn_id:
                        # Found our target — stop here
                        return True
                # Not our target ref — collect text from w:t and w:delText
                for t in elem.findall(t_tag):
                    text_so_far.append(t.text or "")
                for dt in elem.findall(del_text_tag):
                    text_so_far.append(dt.text or "")
                return False

            # Recurse into children (including w:del, w:ins, etc.)
            for child in elem:
                if _walk(child):
                    return True
            return False

        text_so_far = []
        _walk(p)

        full_text = self._normalize_whitespace("".join(text_so_far))
        words     = full_text.split()
        last_word = words[-1] if words else ""

        return {
            "text_before_superscript":      full_text,
            "last_word_before_superscript": last_word
        }

    def enrich_footnote_anchors(self):
        """
        For every footnote row in self.result, populates:
            - superscript                  : actual displayed number (1, 2, 3...)
            - text_before_superscript      : visible text before the superscript
            - last_word_before_superscript : last word before the superscript

        Must be called after build_before_after_df().
        """
        if self.result is None:
            raise RuntimeError("Call build_before_after_df() first.")

        if not self._footnote_display_number_map:
            raise RuntimeError(
                "Display number map is empty — "
                "ensure build_before_after_df() ran and document has footnotes."
            )

        # ── Step 1: Rebuild idx -> paragraph element map ──
        with ZipFile(self.docx_path) as z:
            document_xml = z.read("word/document.xml")
            root = ET.fromstring(document_xml)

            footnote_map = {}
            try:
                xml_bytes = z.read("word/footnotes.xml")
                fn_root = ET.fromstring(xml_bytes)
                for fn in fn_root.findall("w:footnote", self.NS):
                    fn_id = fn.attrib.get(self._w("id"), "?")
                    try:
                        if int(fn_id) < 1:
                            continue
                    except ValueError:
                        pass
                    footnote_map[fn_id] = fn
            except KeyError:
                pass

        para_elem_map       = {}
        counter             = [0]
        processed_footnotes = set()

        def next_idx():
            idx = counter[0]
            counter[0] += 1
            return idx

        body = root.find("w:body", self.NS)

        for kind, p, block_rev in self.walk_content(body):
            if kind == "table_row":
                # p is list of (p_elem, cell_block_rev) — merged into one idx
                row_paras = p
                idx = next_idx()
                # Store all cell p_elems under this idx (use first for footnote anchor lookup)
                if row_paras:
                    para_elem_map[idx] = row_paras[0][0]
                for p_elem, _ in row_paras:
                    fn_refs = self._get_visible_footnote_refs(p_elem, footnote_map, processed_footnotes)
                    for fn_id in fn_refs:
                        processed_footnotes.add(fn_id)
                        fn_paras_list = [(fp, fr) for fk, fp, fr in self.walk_content(footnote_map[fn_id]) if fk == "paragraph"]
                        fi = 0
                        while fi < len(fn_paras_list):
                            fp, fr = fn_paras_list[fi]
                            if fi + 1 < len(fn_paras_list):
                                next_fp, next_fr = fn_paras_list[fi + 1]
                                cur_is_del = (fr and fr["type"] == "del") or (fr is None and self._is_pure_del_paragraph(fp))
                                next_is_ins = (next_fr and next_fr["type"] == "ins") or (next_fr is None and self._is_pure_ins_paragraph(next_fp))
                                if cur_is_del and next_is_ins:
                                    fn_idx = next_idx()
                                    para_elem_map[fn_idx] = fp
                                    fi += 2
                                    continue
                            fn_idx = next_idx()
                            para_elem_map[fn_idx] = fp
                            fi += 1
                    del_fn_refs = self._get_deleted_footnote_refs(p_elem, footnote_map, processed_footnotes)
                    for fn_id in del_fn_refs:
                        processed_footnotes.add(fn_id)
                        fn_paras_list = [(fp, fr) for fk, fp, fr in self.walk_content(footnote_map[fn_id]) if fk == "paragraph"]
                        fi = 0
                        while fi < len(fn_paras_list):
                            fp, fr = fn_paras_list[fi]
                            if fi + 1 < len(fn_paras_list):
                                next_fp, next_fr = fn_paras_list[fi + 1]
                                cur_is_del = (fr and fr["type"] == "del") or (fr is None and self._is_pure_del_paragraph(fp))
                                next_is_ins = (next_fr and next_fr["type"] == "ins") or (next_fr is None and self._is_pure_ins_paragraph(next_fp))
                                if cur_is_del and next_is_ins:
                                    fn_idx = next_idx()
                                    para_elem_map[fn_idx] = fp
                                    fi += 2
                                    continue
                            fn_idx = next_idx()
                            para_elem_map[fn_idx] = fp
                            fi += 1
                continue

            if kind != "paragraph":
                continue

            fn_refs = self._get_visible_footnote_refs(p, footnote_map, processed_footnotes)

            idx = next_idx()
            para_elem_map[idx] = p

            for fn_id in fn_refs:
                processed_footnotes.add(fn_id)
                fn_paras_list = [(fp, fr) for fk, fp, fr in self.walk_content(footnote_map[fn_id]) if fk == "paragraph"]
                fi = 0
                while fi < len(fn_paras_list):
                    fp, fr = fn_paras_list[fi]
                    if fi + 1 < len(fn_paras_list):
                        next_fp, next_fr = fn_paras_list[fi + 1]
                        cur_is_del = (fr and fr["type"] == "del") or (fr is None and self._is_pure_del_paragraph(fp))
                        next_is_ins = (next_fr and next_fr["type"] == "ins") or (next_fr is None and self._is_pure_ins_paragraph(next_fp))
                        if cur_is_del and next_is_ins:
                            fn_idx = next_idx()
                            para_elem_map[fn_idx] = fp
                            fi += 2
                            continue
                    fn_idx = next_idx()
                    para_elem_map[fn_idx] = fp
                    fi += 1

            # Also process deleted footnote refs
            del_fn_refs = self._get_deleted_footnote_refs(p, footnote_map, processed_footnotes)
            for fn_id in del_fn_refs:
                processed_footnotes.add(fn_id)
                fn_paras_list = [(fp, fr) for fk, fp, fr in self.walk_content(footnote_map[fn_id]) if fk == "paragraph"]
                fi = 0
                while fi < len(fn_paras_list):
                    fp, fr = fn_paras_list[fi]
                    if fi + 1 < len(fn_paras_list):
                        next_fp, next_fr = fn_paras_list[fi + 1]
                        cur_is_del = (fr and fr["type"] == "del") or (fr is None and self._is_pure_del_paragraph(fp))
                        next_is_ins = (next_fr and next_fr["type"] == "ins") or (next_fr is None and self._is_pure_ins_paragraph(next_fp))
                        if cur_is_del and next_is_ins:
                            fn_idx = next_idx()
                            para_elem_map[fn_idx] = fp
                            fi += 2
                            continue
                    fn_idx = next_idx()
                    para_elem_map[fn_idx] = fp
                    fi += 1

        # ── Step 2: Resolve column names after merge (_x/_y suffixes) ──
        is_fn_col  = "is_footnote_x"          if "is_footnote_x"          in self.result.columns else "is_footnote"
        parent_col = "parent_paragraph_idx_x"  if "parent_paragraph_idx_x"  in self.result.columns else "parent_paragraph_idx"
        fn_id_col  = "footnote_id_x"           if "footnote_id_x"           in self.result.columns else "footnote_id"

        anchors = []

        for _, row in self.result.iterrows():
            if not row.get(is_fn_col, False):
                anchors.append({
                    "idx": row["idx"],
                    "superscript":                  None,
                    "is_new_footnote":              None,
                    "text_before_superscript":      None,
                    "last_word_before_superscript": None
                })
                continue

            # ── Safe fn_id: handles pandas float coercion 30.0 -> "30" ──
            raw_fn_id = row.get(fn_id_col)
            if raw_fn_id is None or (isinstance(raw_fn_id, float) and pd.isna(raw_fn_id)):
                fn_id_str = ""
            else:
                try:
                    fn_id_str = str(int(float(raw_fn_id)))
                except (ValueError, TypeError):
                    fn_id_str = str(raw_fn_id)

            # ── Look up the REAL displayed superscript number ──
            # e.g. fn_id "30" -> 1   (newly inserted footnote shown as ¹)
            #      fn_id "20" -> 20  (pre-existing shown as 20)
            #      fn_id "21" -> 21  (pre-existing shown as 21)
            display_num = self._footnote_display_number_map.get(fn_id_str)
            is_new_footnote = fn_id_str in self._inserted_footnote_ids

            # Force non-new footnotes to superscript 0 so downstream
            # footnote insertion pipeline can safely ignore them.
            if is_new_footnote is False:
                display_num = 0

            parent_idx = row.get(parent_col)
            if parent_idx is None or (isinstance(parent_idx, float) and pd.isna(parent_idx)):
                anchors.append({
                    "idx": row["idx"],
                    "superscript":                  display_num,
                    "is_new_footnote":              is_new_footnote,
                    "text_before_superscript":      None,
                    "last_word_before_superscript": None
                })
                continue

            parent_idx_int = int(parent_idx)

            if parent_idx_int not in para_elem_map:
                anchors.append({
                    "idx": row["idx"],
                    "superscript":                  display_num,
                    "is_new_footnote":              is_new_footnote,
                    "text_before_superscript":      None,
                    "last_word_before_superscript": None
                })
                continue

            parent_p = para_elem_map[parent_idx_int]
            anchor   = self.get_footnote_anchor_text(parent_p, fn_id_str)
            anchor["idx"]        = row["idx"]
            anchor["superscript"] = display_num   # ← 1, not 30
            anchor["is_new_footnote"] = is_new_footnote
            anchors.append(anchor)

        anchor_df = pd.DataFrame(anchors)

        # ── Step 3: Merge back, drop stale columns if re-running ──
        for col in ["superscript", "is_new_footnote", "text_before_superscript", "last_word_before_superscript"]:
            if col in self.result.columns:
                self.result = self.result.drop(columns=[col])

        self.result = self.result.merge(anchor_df, on="idx", how="left")
        return self.result

    # -----------------------------------------------------
    # PROCESS METHOD
    # -----------------------------------------------------

    def process(self):
        self.changes, self.all_paragraphs = self.extract_changes()

        self.df_changes   = pd.DataFrame(self.changes)
        self.df_remaining = pd.DataFrame(self.all_paragraphs)

        self.no_changes_df = self.df_remaining[
            self.df_remaining["has_changes"] == False
        ].copy()

        self.chapter_number = self.get_chapter_number(self.no_changes_df)

        newdf = (
            self.df_changes[[
                'type', 'paragraph_index', 'deleted_text',
                'inserted_text', 'prev_context', 'post_context'
            ]].copy()
            if not self.df_changes.empty else pd.DataFrame()
        )

        df_remaining_simple = self.df_remaining[[
            'idx', 'text', 'has_changes', 'has_bullet_point', 'is_footnote',
            'parent_paragraph_idx', 'footnote_id'
        ]].copy()

        self.result = df_remaining_simple.merge(
            newdf,
            left_on='idx',
            right_on='paragraph_index',
            how='left'
        )

        return {
            'no_changes_df':  self.no_changes_df,
            'df_remaining':   df_remaining_simple,
            'result':         self.result,
            'df_changes':     self.df_changes,
            'chapter_number': self.chapter_number
        }


# -----------------------------------------------------
# CONVENIENCE FUNCTION
# -----------------------------------------------------

def process_docx(docx_path, context_chars=40, replacement_time_tol_sec=60):
    extractor = DocxChangeExtractor(docx_path, context_chars, replacement_time_tol_sec)
    results   = extractor.process()
    results['extractor'] = extractor
    return results


