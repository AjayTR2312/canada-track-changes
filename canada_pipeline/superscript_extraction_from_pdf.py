import subprocess
import os
import sys
import base64
import io
import json
import re
import pdfplumber
import pandas as pd
import fitz  # PyMuPDF
from zipfile import ZipFile
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from .track_changes_df import process_docx
from .config import build_anthropic_client, MODEL_SONNET

# ─── XML namespace for WordprocessingML ───────────────────────────────
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False


class Tee:
    """Writes output to both console and a log file simultaneously."""
    def __init__(self, *streams):
        self.streams = streams
    def write(self, data):
        for stream in self.streams:
            stream.write(data)
            stream.flush()
    def flush(self):
        for stream in self.streams:
            stream.flush()


# ─────────────────────────────────────────
# STEP 1: Convert DOCX to PDF via LibreOffice
# ─────────────────────────────────────────

def find_libreoffice():
    """Find LibreOffice executable path (checks portable in dist folder first)."""
    # Determine base directory (next to EXE if frozen, else script dir)
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))

    possible_paths = [
        # System-installed (preferred)
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "libreoffice",
        "soffice",
        "/usr/bin/libreoffice",
        "/usr/bin/soffice",
        # Fallback: LibreOffice copied into dist folder
        os.path.join(base_dir, "LibreOffice", "program", "soffice.exe"),
    ]
    for path in possible_paths:
        if os.path.isabs(path):
            if os.path.isfile(path):
                return path
            continue
        try:
            result = subprocess.run([path, "--version"], capture_output=True, timeout=10)
            if result.returncode == 0:
                return path
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


def convert_docx_to_pdf(docx_path, output_dir=None):
    """
    Converts a .docx file to .pdf using LibreOffice headless.
    Output PDF will be saved in the same directory as docx unless output_dir is specified.
    """
    if output_dir is None:
        output_dir = os.path.dirname(os.path.abspath(docx_path))

    lo_path = find_libreoffice()
    if lo_path is None:
        raise RuntimeError(
            "LibreOffice not found. Install it:\n"
            "  Linux/Docker: apt-get install libreoffice\n"
            "  Windows: Download from https://www.libreoffice.org/"
        )

    result = subprocess.run(
        [
            lo_path,
            "--headless",
            "--convert-to", "pdf",
            "--outdir", output_dir,
            docx_path
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120
    )

    if result.returncode != 0:
        raise RuntimeError(f"LibreOffice conversion failed:\n{result.stderr.decode()}")

    # Build expected PDF path
    docx_filename = os.path.basename(docx_path)
    pdf_filename = os.path.splitext(docx_filename)[0] + ".pdf"
    pdf_path = os.path.join(output_dir, pdf_filename)

    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"Expected PDF not found at: {pdf_path}")

    print(f"✅ PDF created: {pdf_path}")
    return pdf_path


# ─────────────────────────────────────────
# STEP 2: Extract all words with font metadata from PDF
# ─────────────────────────────────────────

def extract_words_with_metadata(pdf_path):
    """
    Extracts all words from all pages of the PDF along with
    their font size and vertical position (top).
    Returns a flat list of word dicts with page number included.
    """
    all_words = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            words = page.extract_words(extra_attrs=["size", "top"])
            for word in words:
                word["page"] = page_num
                all_words.append(word)
    return all_words


# ─────────────────────────────────────────
# STEP 2B: Image-based LLM superscript extraction (fallback)
# ─────────────────────────────────────────

def render_pdf_page_to_image(pdf_path, page_num, dpi=200):
    """
    Renders a specific PDF page as a PNG image using PyMuPDF.
    Returns the image as bytes.
    """
    doc = fitz.open(pdf_path)
    page = doc[page_num - 1]  # 0-indexed
    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    img_bytes = pix.tobytes("png")
    doc.close()
    return img_bytes


def split_image_into_chunks(img_bytes, num_chunks=3):
    """
    Splits a page image into vertical chunks (top, middle, bottom).
    Returns list of base64-encoded PNG strings.
    """
    from PIL import Image
    img = Image.open(io.BytesIO(img_bytes))
    width, height = img.size
    chunk_height = height // num_chunks
    chunks_b64 = []
    for i in range(num_chunks):
        top = i * chunk_height
        bottom = (i + 1) * chunk_height if i < num_chunks - 1 else height
        chunk = img.crop((0, top, width, bottom))
        buf = io.BytesIO()
        chunk.save(buf, format="PNG")
        chunks_b64.append(base64.b64encode(buf.getvalue()).decode("utf-8"))
    return chunks_b64


def find_superscript_page(all_words, search_text, previous_para=None):
    """
    Determines which PDF page likely contains the superscript.
    Returns (page_number, approximate_vertical_position_fraction).
    """
    search_lower = search_text.lower().strip()
    search_tokens = search_lower.split()
    
    # Try to find the last few words of search_text in the word list
    for tail_len in [4, 3, 2, 1]:
        if len(search_tokens) < tail_len:
            continue
        tail = search_tokens[-tail_len:]
        for i in range(len(all_words) - tail_len):
            window = [all_words[i + j]["text"].lower() for j in range(tail_len)]
            if window == tail:
                page = all_words[i]["page"]
                top = all_words[i].get("top", 400)
                return page, top
    
    # Fallback: search by joining
    word_texts = [w["text"].lower() for w in all_words]
    joined = " ".join(word_texts)
    last_30 = search_lower[-30:] if len(search_lower) > 30 else search_lower
    pos = joined.find(last_30)
    if pos != -1:
        # Approximate word index
        char_count = 0
        for i, w in enumerate(all_words):
            char_count += len(w["text"]) + 1
            if char_count > pos:
                return w["page"], w.get("top", 400)
    
    return 1, 400  # default to page 1


def find_superscript_via_llm(pdf_path, all_words, search_text, previous_para=None, debug=True):
    """
    Uses a vision LLM to identify the superscript number from PDF page images.
    
    Strategy:
      1. Determine which page contains the target text
      2. Render that page as an image and split into 3 chunks
      3. Send chunks to LLM with context about what to look for
      4. LLM returns the superscript number
    
    Returns: superscript number as string, or None
    """
    if not OPENAI_AVAILABLE:
        if debug:
            print("      ⚠️  OpenAI not available for LLM fallback")
        return None
    
    # Check for API key
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        if debug:
            print("      ⚠️  OPENAI_API_KEY not set, skipping LLM fallback")
        return None
    
    try:
        # Find which page contains our text
        page_num, approx_top = find_superscript_page(all_words, search_text, previous_para)
        if debug:
            print(f"      🖼️  LLM fallback: rendering page {page_num}...")
        
        # Render the page
        img_bytes = render_pdf_page_to_image(pdf_path, page_num, dpi=200)
        
        # Split into 3 chunks
        chunks_b64 = split_image_into_chunks(img_bytes, num_chunks=3)
        
        # Determine which chunk(s) to send based on vertical position
        # A typical page at 200 DPI is ~2200px tall, so each chunk ~733px
        # approx_top is in PDF points (72 DPI), convert to fraction
        page_height_pts = 792  # standard letter page
        frac = min(approx_top / page_height_pts, 1.0)
        chunk_idx = min(int(frac * 3), 2)
        
        # Send the target chunk + adjacent chunk for context
        chunks_to_send = []
        labels = []
        if chunk_idx > 0:
            chunks_to_send.append(chunks_b64[chunk_idx - 1])
            labels.append(f"chunk {chunk_idx} of 3 (above target area)")
        chunks_to_send.append(chunks_b64[chunk_idx])
        labels.append(f"chunk {chunk_idx + 1} of 3 (target area)")
        if chunk_idx < 2:
            chunks_to_send.append(chunks_b64[chunk_idx + 1])
            labels.append(f"chunk {chunk_idx + 2} of 3 (below target area)")
        
        # Clean previous_para of XML tags
        prev_context = ""
        if previous_para and str(previous_para).strip() and str(previous_para).lower() != 'nan':
            prev_context = re.sub(r'<[^>]+>', '', str(previous_para)).strip()
            prev_context = prev_context[-150:]  # last 150 chars
        
        # Build the prompt
        prompt = (
            "You are analyzing a PDF page image to find a superscript footnote number.\n\n"
            "TASK: Find the superscript number that appears IMMEDIATELY AFTER the following text in the image.\n\n"
        )
        if prev_context:
            prompt += f"CONTEXT (text from the paragraph BEFORE the target):\n\"{prev_context}\"\n\n"
        
        clean_search = re.sub(r'<[^>]+>', '', search_text).strip()
        prompt += (
            f"TARGET TEXT (look for superscript immediately after this):\n\"{clean_search[-200:]}\"\n\n"
            "INSTRUCTIONS:\n"
            "1. Find the target text (or its ending portion) in the image\n"
            "2. Look for a small raised number (superscript) immediately after it\n"
            "3. Superscripts are smaller than normal text and positioned higher\n"
            "4. Return ONLY the number, nothing else\n"
            "5. If you cannot find it or are unsure, return \"NONE\"\n\n"
            "RESPOND WITH ONLY THE SUPERSCRIPT NUMBER (e.g. \"5\") OR \"NONE\":"
        )
        
        # Build message content with images
        content = [{"type": "text", "text": prompt}]
        for i, (chunk_b64, label) in enumerate(zip(chunks_to_send, labels)):
            content.append({
                "type": "text",
                "text": f"\n[Image: {label}]"
            })
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{chunk_b64}",
                    "detail": "high"
                }
            })
        
        # Call LLM
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": content}],
            max_tokens=10,
            temperature=0
        )
        
        answer = response.choices[0].message.content.strip()
        if debug:
            print(f"      🤖 LLM response: '{answer}'")
        
        # Parse response
        if answer.upper() == "NONE" or not answer:
            return None
        
        # Extract just the number
        match = re.match(r'(\d+)', answer)
        if match:
            return match.group(1)
        
        return None
        
    except Exception as e:
        if debug:
            print(f"      ❌ LLM fallback error: {e}")
        return None


# ─────────────────────────────────────────
# STEP 3: Find superscript after a given text
# ─────────────────────────────────────────

def find_superscript_after_text(all_words, search_text, previous_para=None, debug=True):
    """
    Searches for search_text in the extracted words list.
    Uses multiple strategies:
      0. If previous_para is provided, first locate it in PDF to narrow search area
      1. Exact match on last N words of the phrase
      2. Fuzzy substring match on joined PDF text
    Returns the superscript number as string, or None if not found.
    """
    import re as _re

    search_text = search_text.strip()
    if not search_text:
        return None

    word_texts = [w["text"] for w in all_words]
    word_texts_lower = [w.lower() for w in word_texts]

    # Build joined text and char_to_word_idx (used by multiple strategies)
    joined = ""
    char_to_word_idx = []
    for wi, wt in enumerate(word_texts):
        if joined:
            joined += " "
            char_to_word_idx.append(wi)
        for ch in wt:
            joined += ch
            char_to_word_idx.append(wi)
    joined_lower = joined.lower()

    # ── Strategy 0: Use previous_para to narrow search window ──
    # Find previous_para in PDF, then only search AFTER that position
    search_start_word_idx = 0
    if previous_para and str(previous_para).strip() and str(previous_para).lower() != 'nan':
        import re as _re2
        # Strip XML/markup tags from previous_para before searching in PDF
        prev_lower = _re2.sub(r'<[^>]+>', '', str(previous_para)).strip().lower()
        # Also collapse multiple spaces
        prev_lower = _re2.sub(r'\s+', ' ', prev_lower)
        # Try last 50 chars of previous_para as anchor (most unique part)
        prev_anchors = []
        if len(prev_lower) > 50:
            prev_anchors.append(prev_lower[-50:])
        if len(prev_lower) > 30:
            prev_anchors.append(prev_lower[-30:])
        prev_anchors.append(prev_lower[-20:] if len(prev_lower) > 20 else prev_lower)

        for anchor in prev_anchors:
            pos = joined_lower.find(anchor)
            if pos != -1:
                # Found previous_para — set search start to after this position
                end_pos = pos + len(anchor) - 1
                if end_pos < len(char_to_word_idx):
                    search_start_word_idx = char_to_word_idx[end_pos] + 1
                    if debug:
                        print(f"      📍 Located previous_para at word_idx={search_start_word_idx} (anchor: '{anchor[:30]}...')")
                        # Show what matched in PDF (context around the match)
                        pdf_context = joined[max(0, pos-20):min(len(joined), end_pos+50)]
                        print(f"      📍 PDF match context: '...{pdf_context}...'")
                break
        else:
            if debug:
                print(f"      ⚠️  Could not locate previous_para in PDF, searching full document")

    # Narrow down the word lists if we found previous_para
    # Search within a window of 500 words after previous_para (generous to cover the paragraph)
    search_end_word_idx = min(len(all_words), search_start_word_idx + 500)
    narrowed_words = all_words[search_start_word_idx:search_end_word_idx]
    narrowed_texts_lower = word_texts_lower[search_start_word_idx:search_end_word_idx]

    # ── Debug: Log what text_before_superscript looks like in the narrowed PDF window ──
    if debug:
        print(f"      ─── DEBUG MATCH INFO ───")
        print(f"      previous_para (df): '{str(previous_para)[:120]}...'")
        prev_cleaned = _re2.sub(r'<[^>]+>', '', str(previous_para)).strip() if previous_para else ''
        print(f"      previous_para (cleaned): '{prev_cleaned[-80:]}' ")
        print(f"      text_before_superscript (df): '{search_text[:120]}...'")
        # Try to find text_before_superscript in PDF and show surrounding context
        search_lower_check = _re2.sub(r'\s+', ' ', search_text.lower().strip())
        # Try last 40 chars
        check_frag = search_lower_check[-40:] if len(search_lower_check) > 40 else search_lower_check
        frag_pos = joined_lower.find(check_frag)
        if frag_pos != -1:
            # Show PDF text from match end + 30 chars (to see what comes after, i.e. the superscript)
            pdf_after = joined[frag_pos:min(len(joined), frag_pos + len(check_frag) + 40)]
            print(f"      text_before_superscript matched in PDF at char {frag_pos}: '{pdf_after}'")
            # Show which word_idx this corresponds to
            if frag_pos + len(check_frag) < len(char_to_word_idx):
                matched_word_idx = char_to_word_idx[frag_pos + len(check_frag) - 1]
                print(f"      → ends at word_idx={matched_word_idx}, search_start_word_idx={search_start_word_idx}")
                if matched_word_idx < search_start_word_idx:
                    print(f"      ⚠️  WARNING: text_before_superscript is BEFORE the narrowed window! previous_para localized too late.")
        else:
            print(f"      ⚠️  text_before_superscript last 40 chars NOT found in PDF: '{check_frag}'")
        print(f"      ─── END DEBUG ───")

    search_tokens = search_text.lower().split()

    # ── Strategy 1: Match using last 1-5 words in narrowed window ──
    def _try_tail_match(words_list, texts_lower_list, label=""):
        # Also build stripped versions (remove trailing punctuation like commas, periods)
        import re as _re
        texts_stripped = [_re.sub(r'[,.\-;:!?\'")\]]+$', '', t) for t in texts_lower_list]
        
        for tail_len in [5, 4, 3, 2, 1]:
            if len(search_tokens) < tail_len:
                continue
            tail = search_tokens[-tail_len:]
            # Also strip search tail tokens for comparison
            tail_stripped = [_re.sub(r'[,.\-;:!?\'")\]]+$', '', t) for t in tail]

            for i in range(len(texts_lower_list) - tail_len):
                # Try exact match first, then stripped match
                window = texts_lower_list[i: i + tail_len]
                window_stripped = texts_stripped[i: i + tail_len]
                if window != tail and window_stripped != tail_stripped:
                    continue
                    
                next_index = i + tail_len
                # Check next few words (superscript might be 1-3 words ahead due to punctuation/spaces)
                for offset in [0, 1, 2, 3]:
                    check_idx = next_index + offset
                    if check_idx >= len(words_list):
                        continue
                    next_word = words_list[check_idx]
                    matched_last = words_list[i + tail_len - 1]

                    curr_size = matched_last.get("size", 0)
                    next_size = next_word.get("size", 0)
                    curr_top = matched_last.get("top", 0)
                    next_top = next_word.get("top", 0)

                    if not next_word["text"].strip().isdigit():
                        continue

                    if next_size < curr_size and next_top < curr_top:
                        if debug:
                            print(f"      ✓ Strategy 1{label} (tail={tail_len}, offset={offset}): matched '{' '.join(tail)}' → superscript '{next_word['text'].strip()}'")
                        return next_word["text"].strip()

                    if next_size < curr_size * 0.85:
                        if debug:
                            print(f"      ✓ Strategy 1{label} (tail={tail_len}, offset={offset}, size-only): matched → superscript '{next_word['text'].strip()}'")
                        return next_word["text"].strip()
        return None

    # Try narrowed window first
    if search_start_word_idx > 0:
        result = _try_tail_match(narrowed_words, narrowed_texts_lower, " [narrowed]")
        if result:
            return (result, "high", "strategy1_narrowed")

    # Fallback to full document but start searching from near the expected area
    fallback_start = max(0, search_start_word_idx - 200) if search_start_word_idx > 0 else 0
    fallback_words = all_words[fallback_start:]
    fallback_texts = word_texts_lower[fallback_start:]
    result = _try_tail_match(fallback_words, fallback_texts, " [full-near]")
    if result:
        return (result, "medium", "strategy1_full_near")

    # Last resort: search entire document from position 0
    if fallback_start > 0:
        result = _try_tail_match(all_words, word_texts_lower, " [full-all]")
        if result:
            return (result, "low", "strategy1_full_all")

    # ── Strategy 2: Substring match in narrowed joined text, then full ──
    search_lower = search_text.lower()
    search_variants = [search_lower]
    if len(search_lower) > 60:
        search_variants.append(search_lower[-60:])
    if len(search_lower) > 30:
        search_variants.append(search_lower[-30:])

    def _try_substring(start_char_pos, label=""):
        for variant in search_variants:
            pos = joined_lower.find(variant, start_char_pos)
            if pos != -1:
                end_pos = pos + len(variant) - 1
                if end_pos < len(char_to_word_idx):
                    last_word_idx = char_to_word_idx[end_pos]
                    # Check next few words (skip punctuation tokens)
                    for offset in range(1, 5):
                        next_idx = last_word_idx + offset
                        if next_idx >= len(all_words):
                            break
                        next_word = all_words[next_idx]
                        if not next_word["text"].strip().isdigit():
                            continue
                        matched_last = all_words[last_word_idx]
                        curr_size = matched_last.get("size", 0)
                        next_size = next_word.get("size", 0)
                        if next_size < curr_size * 0.9:
                            if debug:
                                print(f"      ✓ Strategy 2{label} (substring len={len(variant)}, offset={offset}): found → superscript '{next_word['text'].strip()}'")
                            return next_word["text"].strip()
                        break  # found a digit but not superscript-sized, stop
        return None

    # Try from narrowed position first
    if search_start_word_idx > 0:
        # Convert word idx to char position
        start_char = 0
        for ci, wi in enumerate(char_to_word_idx):
            if wi >= search_start_word_idx:
                start_char = ci
                break
        result = _try_substring(start_char, " [narrowed]")
        if result:
            return (result, "medium", "strategy2_narrowed")

    # Fallback full document
    result = _try_substring(0, " [full]")
    if result:
        return (result, "low", "strategy2_full")

    if debug:
        print(f"      ✗ No match. Search tail: '{' '.join(search_tokens[-4:])}'")
        if len(search_tokens) >= 2:
            last2 = ' '.join(search_tokens[-2:])
            if last2 in joined_lower:
                pos = joined_lower.find(last2)
                context_start = max(0, pos - 20)
                context_end = min(len(joined), pos + len(last2) + 30)
                print(f"        Found partial '{last2}' in PDF at: ...{joined[context_start:context_end]}...")

    return (None, None, None)


# ─────────────────────────────────────────
# STEP 3B: XML-based footnote ref extraction
# ─────────────────────────────────────────

def extract_footnote_ref_from_xml(docx_path):
    """
    Extract footnote reference numbers directly from the DOCX XML.
    
    Two sources are checked:
    1. word/footnotes.xml — For each <w:footnote w:id="X">, looks for the first 
       <w:r> with <w:rStyle w:val="FootnoteReference"/> and extracts <w:t> text.
    2. word/document.xml — For <w:footnoteReference w:customMarkFollows="1">, the
       explicit number follows as <w:t> in the same run.
    
    When <w:t> is present, it's the explicit footnote number (e.g., "1", "23").
    When absent, Word uses auto-numbering via <w:footnoteRef/> and we return None
    for that footnote (requiring PDF fallback).
    
    Returns:
        dict: {footnote_id_str: number_str_or_None}
              e.g. {"193": "1", "194": None, "195": "3"}
    """
    fn_ref_map = {}  # fn_id -> extracted number or None
    
    try:
        with ZipFile(docx_path) as z:
            # ── Source 1: Check footnotes.xml for explicit <w:t> in FootnoteReference runs ──
            try:
                xml_bytes = z.read("word/footnotes.xml")
                fn_root = ET.fromstring(xml_bytes)
                
                for fn_elem in fn_root.findall(f"{{{W_NS}}}footnote"):
                    fn_id = fn_elem.attrib.get(f"{{{W_NS}}}id", "?")
                    try:
                        if int(fn_id) < 1:
                            continue
                    except ValueError:
                        continue
                    
                    # Walk paragraphs in this footnote
                    found_ref_number = None
                    for p_elem in fn_elem.findall(f".//{{{W_NS}}}p"):
                        for r_elem in p_elem.findall(f"{{{W_NS}}}r"):
                            # Check if this run has FootnoteReference style
                            rpr = r_elem.find(f"{{{W_NS}}}rPr")
                            if rpr is None:
                                continue
                            rstyle = rpr.find(f"{{{W_NS}}}rStyle")
                            if rstyle is None:
                                continue
                            style_val = rstyle.attrib.get(f"{{{W_NS}}}val", "")
                            if style_val != "FootnoteReference":
                                continue
                            
                            # Found a FootnoteReference run — check for <w:t>
                            wt = r_elem.find(f"{{{W_NS}}}t")
                            if wt is not None and wt.text and wt.text.strip().isdigit():
                                found_ref_number = wt.text.strip()
                            else:
                                # Has <w:footnoteRef/> (auto-numbered) — no explicit text
                                found_ref_number = None
                            break  # Only check the first FootnoteReference run
                        if found_ref_number is not None:
                            break
                    
                    fn_ref_map[fn_id] = found_ref_number
            except KeyError:
                pass  # No footnotes.xml
            
            # ── Source 2: Check document.xml for customMarkFollows refs ──
            try:
                doc_bytes = z.read("word/document.xml")
                doc_root = ET.fromstring(doc_bytes)
                
                for r_elem in doc_root.iter(f"{{{W_NS}}}r"):
                    fn_ref = r_elem.find(f"{{{W_NS}}}footnoteReference")
                    if fn_ref is None:
                        continue
                    
                    fn_id = fn_ref.attrib.get(f"{{{W_NS}}}id")
                    if not fn_id:
                        continue
                    
                    # Already found from footnotes.xml? Skip.
                    if fn_ref_map.get(fn_id) is not None:
                        continue
                    
                    # Check for customMarkFollows="1"
                    custom_mark = fn_ref.attrib.get(f"{{{W_NS}}}customMarkFollows", "")
                    if custom_mark == "1":
                        # The <w:t> in the same run has the explicit number
                        wt = r_elem.find(f"{{{W_NS}}}t")
                        if wt is not None and wt.text and wt.text.strip().isdigit():
                            fn_ref_map[fn_id] = wt.text.strip()
            except KeyError:
                pass  # No document.xml (shouldn't happen)
    
    except Exception as e:
        print(f"  [WARN] Could not read DOCX for XML extraction: {e}")
    
    return fn_ref_map


# ─────────────────────────────────────────
# STEP 3C: PDF fuzzy match + LLM pick helpers
# ─────────────────────────────────────────

def _build_pdf_text_with_positions(all_words):
    """
    Build a joined text from PDF words and a mapping from char position to word index.
    Returns (joined_text, char_to_word_idx).
    """
    joined = ""
    char_to_word_idx = []
    for wi, w in enumerate(all_words):
        if joined:
            joined += " "
            char_to_word_idx.append(wi)
        for ch in w["text"]:
            joined += ch
            char_to_word_idx.append(wi)
    return joined, char_to_word_idx


def _find_top_matches_in_pdf(search_text, joined_text, top_n=5):
    """
    Fuzzy match text_before_superscript against the full PDF joined text.
    Find top N locations where the text matches, and grab +20 chars after each match.
    
    Returns list of dicts sorted by score descending:
        {match_start, match_end, score, text_matched, text_after_20}
    """
    search_lower = re.sub(r'<[^>]+>', '', search_text).lower().strip()
    joined_lower = joined_text.lower()
    
    if not search_lower:
        return []
    
    search_len = len(search_lower)
    joined_len = len(joined_lower)
    
    if search_len == 0 or joined_len == 0:
        return []
    
    candidates = []
    
    # Strategy A: Direct substring search (exact or near-exact)
    for trim_len in [search_len, max(60, search_len // 2), 60, 40, 30]:
        fragment = search_lower[-trim_len:] if len(search_lower) > trim_len else search_lower
        start_pos = 0
        while True:
            pos = joined_lower.find(fragment, start_pos)
            if pos == -1:
                break
            match_end = pos + len(fragment)
            text_after = joined_text[match_end:match_end + 20]
            candidates.append({
                "match_start": pos,
                "match_end": match_end,
                "score": len(fragment) / search_len,
                "text_matched": joined_text[pos:match_end],
                "text_after_20": text_after,
            })
            start_pos = pos + 1
        if candidates:
            break
    
    # Strategy B: If no exact matches, use fuzzy sliding window
    if not candidates:
        window_size = min(search_len, 100)
        step = max(10, window_size // 4)
        
        for start in range(0, joined_len - window_size, step):
            window = joined_lower[start:start + window_size]
            score = SequenceMatcher(None, search_lower[-window_size:], window).ratio()
            if score > 0.5:
                match_end = start + window_size
                text_after = joined_text[match_end:match_end + 20]
                candidates.append({
                    "match_start": start,
                    "match_end": match_end,
                    "score": score,
                    "text_matched": joined_text[start:match_end],
                    "text_after_20": text_after,
                })
    
    # Deduplicate overlapping matches (keep highest score for overlapping regions)
    candidates.sort(key=lambda x: x["score"], reverse=True)
    filtered = []
    used_positions = set()
    for cand in candidates:
        pos_key = cand["match_end"] // 20
        if pos_key not in used_positions:
            filtered.append(cand)
            used_positions.add(pos_key)
        if len(filtered) >= top_n:
            break
    
    return filtered


def _llm_pick_superscript(search_text, top_matches, client, model_name=MODEL_SONNET):
    """
    Give the LLM the text_before_superscript and top 5 match locations.
    Each match shows the matched text + 20 chars after (where the superscript should be).
    LLM picks which one contains the correct footnote superscript number.
    """
    candidates_text = ""
    for i, match in enumerate(top_matches, 1):
        matched_tail = match['text_matched'][-80:]
        candidates_text += (
            f"\nMatch {i} (similarity: {match['score']:.3f}):\n"
            f"  Text ending at match: \"...{matched_tail}\"\n"
            f"  +20 chars after match: \"{match['text_after_20']}\"\n"
        )
    
    prompt = f"""You are identifying footnote superscript numbers from a PDF.

The DOCX track changes show that a footnote was inserted after this text:
"{search_text[-200:]}"

I searched for this text in the PDF and found {len(top_matches)} matching locations.
For each match, I show the end of the matched text and the 20 characters that come AFTER it in the PDF.
The footnote superscript number should appear in those 20 characters after the match.

{candidates_text}

Look at the "+20 chars after match" for each candidate. The superscript footnote number is a digit (e.g., 1, 2, 23) that appears right after the matching text.

Rules:
1. Pick the match whose text best corresponds to the DOCX text above.
2. From that match's "+20 chars after", extract the footnote number (the first digit/number you see).
3. If none match or no digit is found, reply "NONE".

Reply with ONLY the superscript number (e.g., "1", "5", "23") or "NONE".
"""

    try:
        response = client.messages.create(
            model=model_name,
            max_tokens=20,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        answer = response.content[0].text.strip().strip('"\'')
        if answer.upper() == "NONE":
            return None
        # Validate it's a number
        if answer.isdigit():
            return answer
        # Try to extract a number from the response
        num_match = re.search(r'\d+', answer)
        if num_match:
            return num_match.group()
        return None
    except Exception as e:
        print(f"    ⚠️  LLM pick error: {e}")
        return None


# ─────────────────────────────────────────
# STEP 4: Process Excel and add pdf_superscript column
# ─────────────────────────────────────────

def process_excel(docx_path, pdf_path, output_excel_path=None, result_with_ba=None):
    """
    Filters result_with_ba for new footnotes, finds superscript numbers using:
      1. XML-based extraction (direct from DOCX XML — fast, no PDF needed)
      2. PDF fuzzy match + LLM pick (for unresolved footnotes)
    
    Enriches result_with_ba with pdf_superscript + confidence columns.
    
    Args:
        docx_path: Path to DOCX file (used for XML extraction and PDF fallback)
        pdf_path: Path to the PDF file
        output_excel_path: Path to save the output Excel
        result_with_ba: Pre-built result_with_ba DataFrame. If None, will call process_docx.
    
    Returns:
        Tuple of (filtered_df_with_superscripts, enriched_result_with_ba)
    """
    if output_excel_path is None:
        output_excel_path = "output_with_superscripts.xlsx"

    # Build result_with_ba from DOCX if not provided
    if result_with_ba is None:
        print("📄 Extracting track changes from DOCX...")
        results = process_docx(docx_path)
        extractor_obj = results['extractor']
        result_with_ba = extractor_obj.build_before_after_df()
        try:
            result_with_ba = extractor_obj.enrich_footnote_anchors()
        except RuntimeError as e:
            print(f"  [WARN] enrich_footnote_anchors skipped: {e}")
    else:
        print("📄 Using provided result_with_ba DataFrame...")

    df = result_with_ba.copy()

    # Filter to only inserted footnotes
    df = df[
        (df['is_footnote_x'] == True) &
        (df['type'].isin(['inline_insertion', 'paragraph_addition'])) &
        (df['is_new_footnote'] == 1)
    ].reset_index(drop=True)
    print(f"   Filtered to {len(df)} rows (is_footnote_x=True, is_new_footnote=1)")

    if len(df) == 0:
        print("⚠️  No new inserted footnotes found. Nothing to process.")
        # Still return empty result with expected columns
        for col in ['pdf_superscript', 'footnote_ref_confidence', 'extraction_method']:
            if col not in result_with_ba.columns:
                result_with_ba[col] = None
        df.to_excel(output_excel_path, index=False)
        return df, result_with_ba

    # Build previous_para column from unfiltered result_with_ba
    unfiltered = result_with_ba[result_with_ba['is_footnote_x'] == False].drop_duplicates(subset=['idx'], keep='first').sort_values('idx').reset_index(drop=True)

    def get_previous_para(parent_idx):
        """Get text from the row before parent_paragraph_idx_x in unfiltered df."""
        if pd.isna(parent_idx):
            return ""
        parent_idx = int(parent_idx)
        all_idxs = unfiltered['idx'].tolist()
        
        try:
            pos = all_idxs.index(parent_idx)
            pos -= 1  # We want the row BEFORE parent_paragraph_idx_x
        except ValueError:
            candidates = [i for i, v in enumerate(all_idxs) if v < parent_idx]
            if not candidates:
                return ""
            pos = candidates[-1]
        
        while pos >= 0:
            row = unfiltered.iloc[pos]
            row_type = str(row.get('type', '')).strip()
            
            if row_type == 'inline_insertion':
                text = str(row.get('text_after', '')).strip()
            elif row_type == 'inline_deletion':
                text = str(row.get('text_before', '')).strip()
            else:
                text = str(row.get('text', '')).strip()
            
            if text and text.lower() != 'nan':
                return text
            pos -= 1
        
        return ""

    df['previous_para'] = df['parent_paragraph_idx_x'].apply(get_previous_para)
    print(f"   Built previous_para column")

    if "text_before_superscript" not in df.columns:
        raise ValueError("Column 'text_before_superscript' not found in DataFrame.")

    # ── Step A: Try XML-based extraction first ──
    print(f"\n📋 Attempting XML-based footnote ref extraction...")
    xml_ref_map = extract_footnote_ref_from_xml(docx_path)
    
    # Determine the footnote_id column name (may have _x suffix after merge)
    fn_id_col = 'footnote_id_x' if 'footnote_id_x' in df.columns else 'footnote_id'
    
    xml_resolved_count = 0
    xml_unresolved_indices = []  # row indices that still need PDF fallback
    
    for i in range(len(df)):
        row = df.iloc[i]
        raw_fn_id = row.get(fn_id_col)
        if raw_fn_id is None or (isinstance(raw_fn_id, float) and pd.isna(raw_fn_id)):
            xml_unresolved_indices.append(i)
            continue
        fn_id_str = str(int(float(raw_fn_id)))
        xml_number = xml_ref_map.get(fn_id_str)
        if xml_number is not None:
            xml_resolved_count += 1
        else:
            xml_unresolved_indices.append(i)
    
    print(f"   ✅ XML resolved: {xml_resolved_count}/{len(df)} footnotes")
    print(f"   ⏳ Needs PDF fallback: {len(xml_unresolved_indices)} footnotes")

    # ── Step B: Extract words from PDF (only if needed) ──
    if xml_unresolved_indices:
        print(f"\n📄 Extracting words from PDF (for {len(xml_unresolved_indices)} unresolved)...")
        all_words = extract_words_with_metadata(pdf_path)
        print(f"   Total words extracted: {len(all_words)}")

        # Build joined text from PDF
        print(f"   Building PDF text index...")
        joined, char_to_word_idx = _build_pdf_text_with_positions(all_words)
        print(f"   PDF text length: {len(joined)} characters")
    else:
        all_words = None
        joined = ""
        char_to_word_idx = []

    # Build LLM client (only if PDF fallback is needed)
    client = None
    if xml_unresolved_indices:
        print(f"   Building Anthropic LLM client...")
        client = build_anthropic_client(MODEL_SONNET)

    # ── Step C: Load cache to avoid redundant LLM calls ──
    cache_path = os.path.join(os.path.dirname(os.path.abspath(docx_path)), ".superscript_cache.json")
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as cf:
                cache = json.load(cf)
            print(f"   Loaded cache with {len(cache)} entries")
        except Exception:
            cache = {}

    # ── Step D: Find superscripts — XML first, then fuzzy match + LLM fallback ──
    print(f"\n🔍 Finding superscripts for {len(df)} footnotes (XML → PDF fuzzy → LLM)...\n")
    pdf_superscripts = []
    confidences = []
    methods = []

    for i in range(len(df)):
        row = df.iloc[i]
        
        # ── Try XML extraction first ──
        raw_fn_id = row.get(fn_id_col)
        fn_id_str = None
        if raw_fn_id is not None and not (isinstance(raw_fn_id, float) and pd.isna(raw_fn_id)):
            fn_id_str = str(int(float(raw_fn_id)))
        
        if fn_id_str and xml_ref_map.get(fn_id_str) is not None:
            superscript = xml_ref_map[fn_id_str]
            pdf_superscripts.append(superscript)
            confidences.append("high")
            methods.append("xml_direct")
            print(f"  Row {i+1}/{len(df)} (idx={row.get('idx', '?')}): 📋 XML direct → superscript={superscript}")
            continue

        # ── Fallback: PDF fuzzy match + LLM ──
        search_text = str(row['text_before_superscript']).strip()
        row_idx = str(row.get('idx', i))

        if not search_text or search_text.lower() == 'nan':
            pdf_superscripts.append(None)
            confidences.append(None)
            methods.append(None)
            print(f"  Row {i+1}/{len(df)}: [SKIP] empty text_before_superscript")
            continue

        # Check cache
        if row_idx in cache:
            cached = cache[row_idx]
            pdf_superscripts.append(cached.get("superscript"))
            confidences.append(cached.get("confidence"))
            methods.append("cached")
            print(f"  Row {i+1}/{len(df)} (idx={row_idx}): ⚡ CACHED superscript={cached.get('superscript')}")
            continue

        print(f"  Row {i+1}/{len(df)} (idx={row.get('idx', '?')}):")
        print(f"    text_before: '{search_text[:100]}...'")

        # Find top 5 locations in PDF where text_before_superscript matches
        top_matches = _find_top_matches_in_pdf(search_text, joined, top_n=5)

        if not top_matches:
            print(f"    ❌ No matching locations found in PDF")
            pdf_superscripts.append(None)
            confidences.append(None)
            methods.append(None)
            continue

        # Show matches
        for rank, match in enumerate(top_matches, 1):
            print(f"    Match {rank}: score={match['score']:.3f}, "
                  f"text_end='...{match['text_matched'][-40:]}', "
                  f"after='{match['text_after_20']}'")

        # Let LLM pick from top matches
        print(f"    🤖 Asking LLM to pick from {len(top_matches)} matches...")
        superscript = _llm_pick_superscript(search_text, top_matches, client, MODEL_SONNET)
        if superscript:
            confidence = "high"
            method = "text_match_llm_pick"
            print(f"    ✅ LLM picked: superscript={superscript}")
        else:
            confidence = None
            method = None
            print(f"    ❌ LLM could not determine superscript")

        pdf_superscripts.append(superscript)
        confidences.append(confidence)
        methods.append(method)

        # Save to cache
        cache[row_idx] = {"superscript": superscript, "confidence": confidence, "method": method}

    # Persist cache to disk
    try:
        with open(cache_path, "w", encoding="utf-8") as cf:
            json.dump(cache, cf, indent=2)
        print(f"\n   💾 Cache saved ({len(cache)} entries) → {cache_path}")
    except Exception as e:
        print(f"\n   ⚠️  Could not save cache: {e}")

    df["pdf_superscript"] = pdf_superscripts
    df["footnote_ref_confidence"] = confidences
    df["extraction_method"] = methods

    # ── Summary ──
    total = len(df)
    found = df['pdf_superscript'].notna().sum()
    missing = total - found
    xml_count = (df['extraction_method'] == 'xml_direct').sum()
    llm_count = (df['extraction_method'] == 'text_match_llm_pick').sum()
    cached_count = (df['extraction_method'] == 'cached').sum()

    print(f"\n{'='*70}")
    print(f"📊 SUPERSCRIPT EXTRACTION SUMMARY")
    print(f"{'='*70}")
    print(f"  Total new footnotes:      {total}")
    print(f"  Superscripts found:       {found}")
    print(f"  Superscripts missing:     {missing}")
    if total > 0:
        print(f"  Success rate:             {found/total*100:.1f}%")
    print(f"  ─── Breakdown by method ───")
    print(f"  📋 XML direct:            {xml_count}")
    print(f"  🤖 LLM pick (PDF):        {llm_count}")
    print(f"  ⚡ Cached:                 {cached_count}")
    print(f"{'='*70}")

    # Merge pdf_superscript, confidence, and method back into the full result_with_ba
    merge_cols = ['idx', 'pdf_superscript', 'footnote_ref_confidence', 'extraction_method']
    if 'idx' in df.columns:
        merge_df = df[merge_cols].drop_duplicates(subset=['idx'], keep='first')
        for col in ['pdf_superscript', 'footnote_ref_confidence', 'extraction_method']:
            if col in result_with_ba.columns:
                result_with_ba = result_with_ba.drop(columns=[col])
        result_with_ba = result_with_ba.merge(merge_df, on='idx', how='left')

    # Save output
    df.to_excel(output_excel_path, index=False)
    print(f"\n✅ Output saved to: {output_excel_path}")
    return df, result_with_ba


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────

if __name__ == "__main__":

    # # ── Single file mode (commented out) ──
    # DOCX_PATH  = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\canada_tested_files\CDN_CO_CBA\CDN_CO_CBA 1.docx"
    # OUTPUT_DIR = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\canada_tested_files\CDN_CO_CBA"
    # OUTPUT_EXCEL = os.path.join(OUTPUT_DIR, "output_with_superscripts.xlsx")
    # print("🔄 Converting DOCX to PDF...")
    # pdf_path = convert_docx_to_pdf(DOCX_PATH, output_dir=OUTPUT_DIR)
    # print("\n🔍 Processing...")
    # df_result = process_excel(DOCX_PATH, pdf_path, output_excel_path=OUTPUT_EXCEL)
    # print("\n📊 Preview of results:")
    # print(df_result[["text_before_superscript", "pdf_superscript"]].to_string(index=False))

    # ── Batch mode: iterate all chapter folders ──
    ROOT_DIR = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\canada_testing_missed_files"

    for chapter_folder in os.listdir(ROOT_DIR):
        chapter_path = os.path.join(ROOT_DIR, chapter_folder)
        if not os.path.isdir(chapter_path):
            continue

        # Find docx/doc file in the chapter folder (search root and Input subfolder)
        docx_file = None
        search_dirs = [chapter_path, os.path.join(chapter_path, "Input")]
        for search_dir in search_dirs:
            if not os.path.isdir(search_dir):
                continue
            for f in os.listdir(search_dir):
                if f.lower().endswith(('.docx', '.doc')) and not f.startswith('~'):
                    docx_file = os.path.join(search_dir, f)
                    break
            if docx_file:
                break

        if not docx_file:
            print(f"⚠️  No .docx/.doc found in '{chapter_folder}' — skipping")
            continue

        docx_basename = os.path.splitext(os.path.basename(docx_file))[0]
        output_excel = os.path.join(chapter_path, f"{docx_basename}_superscripts.xlsx")
        log_file = os.path.join(chapter_path, f"{docx_basename}_superscript_log.txt")

        print(f"\n{'='*80}")
        print(f"📂 Processing: {chapter_folder}")
        print(f"   DOCX: {docx_file}")
        print(f"   Log:  {log_file}")
        print(f"{'='*80}")

        # Redirect stdout/stderr to log file + console
        log_f = open(log_file, 'w', encoding='utf-8')
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout = Tee(sys.__stdout__, log_f)
        sys.stderr = Tee(sys.__stderr__, log_f)

        try:
            print(f"{'='*80}")
            print(f"SUPERSCRIPT EXTRACTION LOG")
            print(f"DOCX: {docx_file}")
            print(f"Output Excel: {output_excel}")
            print(f"{'='*80}\n")

            # Convert to PDF
            print("🔄 Converting DOCX to PDF...")
            pdf_path = convert_docx_to_pdf(docx_file, output_dir=chapter_path)

            # Process and save
            print("\n🔍 Processing...")
            df_result = process_excel(docx_file, pdf_path, output_excel_path=output_excel)

            if df_result is not None and len(df_result) > 0:
                # Summary stats
                total = len(df_result)
                found = df_result['pdf_superscript'].notna().sum()
                missing = total - found
                print(f"\n{'─'*60}")
                print(f"📊 SUMMARY: {found}/{total} superscripts found, {missing} missing")
                print(f"{'─'*60}")
                print(df_result[["text_before_superscript", "pdf_superscript"]].to_string(index=False))

                # Log missing ones explicitly
                if missing > 0:
                    print(f"\n⚠️  MISSING SUPERSCRIPTS ({missing}):")
                    missing_df = df_result[df_result['pdf_superscript'].isna()]
                    for _, row in missing_df.iterrows():
                        print(f"   idx={row.get('idx', '?')} | text_before_superscript: '{str(row['text_before_superscript'])[:120]}'")
                        if 'text_after' in row.index:
                            print(f"       text_after: '{str(row.get('text_after', ''))[:120]}'")
                        if 'footnote_number' in row.index:
                            print(f"       footnote_number: {row.get('footnote_number', '')}")
            else:
                print("   No matching footnote rows found.")

        except Exception as e:
            import traceback
            print(f"❌ Failed for '{chapter_folder}': {e}")
            traceback.print_exc()

        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            log_f.close()
            print(f"   📋 Log saved: {log_file}")

    print(f"\n{'='*80}")
    print("✅ Batch processing complete!")
    print(f"{'='*80}")