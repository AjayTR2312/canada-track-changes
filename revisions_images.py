"""
revisions_images.py
Converts pages containing track changes from a DOCX file to images.

Primary method: pywin32 (MS Word COM automation) — renders track changes
with exact Word fidelity and full control over markup visibility.

Fallback: LibreOffice headless (for Linux/Docker environments without MS Word).

Requirements:
    pip install python-docx pdf2image pymupdf pywin32

System dependencies (fallback only):
    - LibreOffice (for DOCX -> PDF conversion when pywin32 is unavailable)
    - Poppler-utils (for pdf2image, or use pymupdf instead)
"""

import os
import sys
import re
import copy
import subprocess
import tempfile
import shutil
import time
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    from docx import Document
    from docx.oxml.ns import qn
except ImportError:
    print("Please install python-docx: pip install python-docx")
    sys.exit(1)

try:
    import fitz  # PyMuPDF
    USE_PYMUPDF = True
except ImportError:
    USE_PYMUPDF = False
    try:
        from pdf2image import convert_from_path
    except ImportError:
        print("Please install either pymupdf or pdf2image:")
        print("  pip install pymupdf")
        print("  pip install pdf2image (requires poppler-utils)")
        sys.exit(1)

# Try to import pywin32
try:
    import win32com.client
    WIN32COM_AVAILABLE = True
except ImportError:
    WIN32COM_AVAILABLE = False
    logger.info("win32com not available — will fall back to LibreOffice for DOCX->PDF.")


def docx_to_pdf_pywin32(docx_path, output_dir, show_markup=True):
    """
    Convert DOCX to PDF using pywin32 (MS Word COM automation).
    
    Advantages over LibreOffice:
    - Exact MS Word rendering (what you see in Word is what you get)
    - Full control over track change markup visibility
    - Better font/layout fidelity
    
    Args:
        docx_path: Path to the DOCX file
        output_dir: Directory to save the PDF
        show_markup: If True, renders with track changes visible (colored markup).
                     If False, accepts all changes first (clean PDF).
    
    Returns:
        str: Path to the generated PDF
    """
    if not WIN32COM_AVAILABLE:
        raise RuntimeError("pywin32 not available. Install with: pip install pywin32")

    import pythoncom
    pythoncom.CoInitialize()

    word = None
    try:
        # Kill any existing Word processes to avoid COM conflicts
        try:
            subprocess.call("taskkill /f /im WINWORD.EXE", shell=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2)
        except Exception:
            pass

        logger.info("Starting MS Word application for DOCX->PDF conversion (markup=%s)", show_markup)

        # Use dynamic dispatch to avoid gen_py CLSIDToClassMap errors
        try:
            word = win32com.client.dynamic.Dispatch("Word.Application")
        except Exception:
            try:
                import shutil as _shutil_retry
                gen_py_path = getattr(win32com, '__gen_path__', None)
                if gen_py_path and os.path.isdir(gen_py_path):
                    _shutil_retry.rmtree(gen_py_path, ignore_errors=True)
            except Exception:
                pass
            word = win32com.client.Dispatch("Word.Application")

        word.Visible = False
        word.DisplayAlerts = False

        abs_docx_path = str(Path(docx_path).resolve())

        # Create a temporary copy to avoid locking the original
        temp_docx = os.path.join(tempfile.gettempdir(), f"revimg_{os.path.basename(docx_path)}")
        shutil.copy2(abs_docx_path, temp_docx)

        doc = word.Documents.Open(temp_docx, ReadOnly=False, Visible=False,
                                  ConfirmConversions=False, AddToRecentFiles=False)

        try:
            doc.Activate()
        except Exception:
            pass

        if show_markup:
            # Show track changes with colored markup (for LLM image cues)
            try:
                doc.ActiveWindow.View.Type = 3  # wdPrintView
                doc.ActiveWindow.View.ShowRevisionsAndComments = True
                doc.ActiveWindow.View.RevisionsView = 0  # wdRevisionsViewFinal (Final: Show Markup)
                doc.ActiveWindow.View.ShowInsertionsAndDeletions = True
                doc.ActiveWindow.View.ShowFormatChanges = True
                doc.ActiveWindow.View.ShowComments = False  # Hide comment balloons (noisy)
                logger.info("Track changes markup set to VISIBLE")
            except Exception as e:
                logger.warning(f"Could not set revision view settings: {e}")
        else:
            # Accept all changes — produce clean PDF
            try:
                doc.ActiveWindow.View.ShowRevisionsAndComments = False
                doc.ActiveWindow.View.RevisionsView = 0  # wdRevisionsViewFinal
                doc.TrackRevisions = False
                if doc.Revisions.Count > 0:
                    doc.Revisions.AcceptAll()
                while doc.Comments.Count > 0:
                    doc.Comments.Item(1).Delete()
                logger.info("All revisions accepted, comments removed")
            except Exception as e:
                logger.warning(f"Could not accept revisions: {e}")

        # Export to PDF
        pdf_name = Path(docx_path).stem + ".pdf"
        pdf_path = str(Path(output_dir) / pdf_name)

        try:
            doc.ExportAsFixedFormat(
                OutputFileName=pdf_path,
                ExportFormat=17,  # wdExportFormatPDF
                OpenAfterExport=False,
                OptimizeFor=0,  # wdExportOptimizeForStandard
                Item=7 if show_markup else 0,  # 7=wdExportDocumentWithMarkup, 0=wdExportDocumentContent
                BitmapMissingFonts=True,
                DocStructureTags=False,
                UseISO19005_1=False
            )
            logger.info(f"PDF created via pywin32: {pdf_path}")
        except Exception as e:
            logger.warning(f"ExportAsFixedFormat failed: {e}, trying SaveAs2...")
            doc.SaveAs2(FileName=pdf_path, FileFormat=17)
            logger.info(f"PDF created via SaveAs2: {pdf_path}")

        doc.Close(SaveChanges=False)

        # Clean up temp file
        try:
            os.remove(temp_docx)
        except Exception:
            pass

        if not os.path.exists(pdf_path):
            raise RuntimeError(f"PDF not created at expected path: {pdf_path}")

        return pdf_path

    except Exception as e:
        logger.error(f"pywin32 DOCX->PDF failed: {e}")
        raise
    finally:
        if word:
            try:
                word.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()


def find_libreoffice():
    """Find LibreOffice executable path (checks portable in dist folder first)."""
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))

    possible_paths = [
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "libreoffice",
        "soffice",
        "/usr/bin/libreoffice",
        "/usr/bin/soffice",
        os.path.join(base_dir, "LibreOffice", "program", "soffice.exe"),
    ]
    
    for path in possible_paths:
        if os.path.isabs(path):
            if os.path.isfile(path):
                return path
            continue
        try:
            result = subprocess.run(
                [path, "--version"],
                capture_output=True, timeout=10
            )
            if result.returncode == 0:
                return path
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    
    return None


def docx_to_pdf_libreoffice(docx_path, output_dir):
    """
    Fallback: Convert DOCX to PDF using LibreOffice headless.
    Used when pywin32/MS Word is not available (Linux/Docker).
    """
    lo_path = find_libreoffice()
    if lo_path is None:
        raise RuntimeError(
            "LibreOffice not found. Install it:\n"
            "  Linux/Docker: apt-get install libreoffice\n"
            "  Windows: Download from https://www.libreoffice.org/"
        )
    
    cmd = [
        lo_path,
        "--headless",
        "--convert-to", "pdf",
        "--outdir", str(output_dir),
        str(docx_path)
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    
    if result.returncode != 0:
        raise RuntimeError(f"LibreOffice conversion failed: {result.stderr}")
    
    pdf_name = Path(docx_path).stem + ".pdf"
    pdf_path = Path(output_dir) / pdf_name
    
    if not pdf_path.exists():
        raise RuntimeError(f"PDF not created at expected path: {pdf_path}")
    
    return str(pdf_path)


def docx_to_pdf(docx_path, output_dir, show_markup=True):
    """
    Convert DOCX to PDF — tries pywin32 first, falls back to LibreOffice.
    
    Args:
        docx_path: Path to the DOCX file
        output_dir: Directory to save the PDF
        show_markup: If True, keep track changes visible in the PDF
        
    Returns:
        str: Path to the generated PDF
    """
    if WIN32COM_AVAILABLE:
        try:
            return docx_to_pdf_pywin32(docx_path, output_dir, show_markup=show_markup)
        except Exception as e:
            logger.warning(f"pywin32 conversion failed ({e}), falling back to LibreOffice...")
    
    # Fallback to LibreOffice
    return docx_to_pdf_libreoffice(docx_path, output_dir)


# ─────────────────────────────────────────────────────────────────────────────
# PAGE-TEXT EXTRACTION & MATCHING
# ─────────────────────────────────────────────────────────────────────────────

def extract_page_texts(pdf_path):
    """
    Extract plain text from each page of a PDF using PyMuPDF.
    
    Returns:
        list[str]: List where index i = text of page i+1 (0-based index, 1-based page)
    """
    doc = fitz.open(pdf_path)
    page_texts = []
    for page in doc:
        page_texts.append(page.get_text("text"))
    doc.close()
    return page_texts


def _normalize_for_search(text):
    """Normalize text for fuzzy matching: lowercase, collapse whitespace, remove punctuation noise."""
    if not text:
        return ""
    if not isinstance(text, str):
        text = str(text)
    # Remove XML tags if any
    text = re.sub(r'<[^>]+>', ' ', text)
    # Remove XML entities
    text = re.sub(r'&[a-zA-Z0-9#]+;', ' ', text)
    # Normalize unicode quotes/dashes
    text = text.replace('\u2018', "'").replace('\u2019', "'")
    text = text.replace('\u201c', '"').replace('\u201d', '"')
    text = text.replace('\u2013', '-').replace('\u2014', '-')
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip().lower()
    return text


def find_page_for_text(text_before, page_texts, min_match_len=30):
    """
    Find which page contains the given text_before content.
    
    Strategy:
    1. Try exact substring match (normalized) on each page
    2. If no exact match, use sliding window of key phrases (last 80 chars, 
       first 80 chars, middle section) and score each page
    3. Return page number (1-based) with highest confidence
    
    Args:
        text_before: The paragraph text to locate
        page_texts: List of page texts from extract_page_texts()
        min_match_len: Minimum text length to attempt matching
        
    Returns:
        int: 1-based page number, or -1 if not found
    """
    if not text_before:
        return -1
    if not isinstance(text_before, str):
        text_before = str(text_before)
    if len(text_before.strip()) < min_match_len:
        return -1
    
    # Handle page_texts as dict (page_num -> text) or list
    if isinstance(page_texts, dict):
        # Convert dict to ordered list by page number
        max_page = max(page_texts.keys()) if page_texts else 0
        page_texts = [page_texts.get(i, "") for i in range(1, max_page + 1)]
    
    norm_text = _normalize_for_search(text_before)
    if len(norm_text) < 20:
        return -1
    
    norm_pages = [_normalize_for_search(pt) for pt in page_texts]
    
    # Strategy 1: Direct substring match (use a meaningful chunk)
    # Use multiple anchors of different lengths for robustness
    anchors = []
    if len(norm_text) >= 80:
        anchors.append(norm_text[:80])   # First 80 chars
        anchors.append(norm_text[-80:])  # Last 80 chars
    if len(norm_text) >= 50:
        mid = len(norm_text) // 2
        anchors.append(norm_text[mid-25:mid+25])  # Middle 50 chars
    if not anchors:
        anchors.append(norm_text)  # Use full text if short
    
    # Score each page by how many anchors match
    page_scores = [0] * len(norm_pages)
    for anchor in anchors:
        for i, page_norm in enumerate(norm_pages):
            if anchor in page_norm:
                page_scores[i] += 1
    
    max_score = max(page_scores) if page_scores else 0
    if max_score > 0:
        # If tied, prefer the page where more unique words match
        best_pages = [i for i, s in enumerate(page_scores) if s == max_score]
        if len(best_pages) == 1:
            return best_pages[0] + 1  # 1-based
        
        # Tiebreak: count word overlap
        text_words = set(norm_text.split())
        best_overlap = -1
        best_page = best_pages[0]
        for pi in best_pages:
            page_words = set(norm_pages[pi].split())
            overlap = len(text_words & page_words)
            if overlap > best_overlap:
                best_overlap = overlap
                best_page = pi
        return best_page + 1  # 1-based
    
    # Strategy 2: Word-level n-gram matching as fallback
    text_words = norm_text.split()
    if len(text_words) < 5:
        return -1
    
    # Use 5-word sliding windows
    ngram_size = 5
    text_ngrams = set()
    for i in range(len(text_words) - ngram_size + 1):
        text_ngrams.add(' '.join(text_words[i:i+ngram_size]))
    
    best_page = -1
    best_hits = 0
    for pi, page_norm in enumerate(norm_pages):
        hits = sum(1 for ng in text_ngrams if ng in page_norm)
        if hits > best_hits:
            best_hits = hits
            best_page = pi
    
    if best_hits >= max(2, len(text_ngrams) * 0.2):
        return best_page + 1  # 1-based
    
    return -1


def build_page_image_map(pdf_path, output_folder, dpi=200):
    """
    Convert PDF to per-page images and return mapping.
    
    Returns:
        tuple: (page_texts, image_paths)
            - page_texts: list[str] - text per page
            - image_paths: dict[int, str] - page_number (1-based) -> image file path
    """
    output_folder = Path(output_folder)
    output_folder.mkdir(parents=True, exist_ok=True)
    
    page_texts = extract_page_texts(pdf_path)
    
    doc = fitz.open(pdf_path)
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    
    image_paths = {}
    pdf_stem = Path(pdf_path).stem
    
    for page_num in range(len(doc)):
        pix = doc[page_num].get_pixmap(matrix=matrix)
        img_path = output_folder / f"{pdf_stem}_page_{page_num + 1}.png"
        pix.save(str(img_path))
        image_paths[page_num + 1] = str(img_path)
    
    doc.close()
    return page_texts, image_paths


def get_image_for_revision(text_before, page_texts, image_paths, previous_para=None):
    """
    Given a text_before string (and optionally previous_para for context),
    find the matching page and return its image path.
    
    Only colored pages (with track changes) have saved images. If text is found
    on a page without an image, checks adjacent pages that DO have images.
    
    Args:
        text_before: The paragraph text to locate
        page_texts: list of ALL page texts (from extract_page_texts on full PDF)
        image_paths: dict of page_num -> image path (only colored/revision pages)
        previous_para: Optional preceding paragraph for better matching
        
    Returns:
        str or None: Path to the page image, or None if not found
    """
    # Normalize page_texts: convert dict to list if needed
    if isinstance(page_texts, dict):
        max_page = max(page_texts.keys()) if page_texts else 0
        page_texts = [page_texts.get(i, "") for i in range(1, max_page + 1)]

    def _find_closest_image(page_num):
        """Given a page_num, return image for that page or closest colored page."""
        if page_num in image_paths:
            return image_paths[page_num]
        # Check adjacent pages (revision likely spans or is near a colored page)
        for offset in [1, -1, 2, -2]:
            adj = page_num + offset
            if adj in image_paths:
                return image_paths[adj]
        return None

    # Strategy 1: direct match on text_before
    page_num = find_page_for_text(text_before, page_texts)
    if page_num != -1:
        img = _find_closest_image(page_num)
        if img:
            return img
    
    # Strategy 2: match using previous_para (stable text is more reliable in track-changes PDF)
    if previous_para:
        page_num = find_page_for_text(previous_para, page_texts)
        if page_num != -1:
            # text_before likely on same page or next page
            norm_text = _normalize_for_search(text_before)
            if len(norm_text) >= 20:
                anchor = norm_text[:60] if len(norm_text) >= 60 else norm_text
                # Check current page
                if page_num - 1 < len(page_texts):
                    norm_page = _normalize_for_search(page_texts[page_num - 1])
                    if anchor in norm_page:
                        img = _find_closest_image(page_num)
                        if img:
                            return img
                # Check next page
                if page_num < len(page_texts):
                    norm_next = _normalize_for_search(page_texts[page_num])
                    if anchor in norm_next:
                        img = _find_closest_image(page_num + 1)
                        if img:
                            return img
            # Fallback: previous_para page is best guess
            img = _find_closest_image(page_num)
            if img:
                return img
    
    # Strategy 3: short anchor search
    norm_text = _normalize_for_search(text_before)
    if len(norm_text) >= 30:
        short_anchor = norm_text[:40]
        for pi, pt in enumerate(page_texts):
            if short_anchor in _normalize_for_search(pt):
                img = _find_closest_image(pi + 1)
                if img:
                    return img
    
    return None


def get_paragraphs_with_revisions(docx_path):
    """
    Parse the DOCX XML to find paragraphs that contain track changes.
    Returns list of paragraph indices that have revisions.
    """
    doc = Document(docx_path)
    revision_paragraph_indices = []
    
    for i, para in enumerate(doc.paragraphs):
        has_revision = False
        
        # Check for insertions (w:ins)
        if para._element.findall('.//' + qn('w:ins')):
            has_revision = True
        # Check for deletions (w:del)
        elif para._element.findall('.//' + qn('w:del')):
            has_revision = True
        # Check for format changes (w:rPrChange)
        elif para._element.findall('.//' + qn('w:rPrChange')):
            has_revision = True
        # Check for paragraph property changes (w:pPrChange)
        elif para._element.findall('.//' + qn('w:pPrChange')):
            has_revision = True
        # Check for section property changes
        elif para._element.findall('.//' + qn('w:sectPrChange')):
            has_revision = True
            
        if has_revision:
            revision_paragraph_indices.append(i)
    
    return revision_paragraph_indices


def accept_all_changes_docx(docx_path, output_path):
    """
    Create a copy of the DOCX with all track changes accepted.
    This processes the XML to remove revision markup.
    """
    doc = Document(docx_path)
    
    body = doc.element.body
    
    # Accept insertions: unwrap w:ins (keep the content inside)
    for ins in body.findall('.//' + qn('w:ins')):
        parent = ins.getparent()
        idx = list(parent).index(ins)
        for child in list(ins):
            ins.remove(child)
            parent.insert(idx, child)
            idx += 1
        parent.remove(ins)
    
    # Accept deletions: remove w:del entirely (content was deleted)
    for del_elem in body.findall('.//' + qn('w:del')):
        del_elem.getparent().remove(del_elem)
    
    # Accept formatting changes: remove w:rPrChange
    for rpr_change in body.findall('.//' + qn('w:rPrChange')):
        rpr_change.getparent().remove(rpr_change)
    
    # Accept paragraph property changes: remove w:pPrChange
    for ppr_change in body.findall('.//' + qn('w:pPrChange')):
        ppr_change.getparent().remove(ppr_change)
    
    # Accept section property changes: remove w:sectPrChange
    for sect_change in body.findall('.//' + qn('w:sectPrChange')):
        sect_change.getparent().remove(sect_change)
    
    doc.save(output_path)
    return output_path


def find_pages_with_color(pdf_path, dpi=72, color_threshold=20, min_colored_pixels=30):
    """
    Detect pages that contain colored pixels (non-black, non-white, non-gray).
    Track changes are always rendered in color (red, blue, green, etc.)
    while normal document text is black/gray on white background.
    
    Uses numpy for fast vectorized pixel scanning instead of slow Python loops.
    
    Args:
        pdf_path: Path to the PDF file
        dpi: Resolution for scanning (72 is enough for color detection)
        color_threshold: How different R/G/B channels must be to count as "colored".
                        Higher = less sensitive. Default 20.
        min_colored_pixels: Minimum number of colored pixels on a page to flag it.
                           Avoids false positives from tiny artifacts. Default 30.
    
    Returns:
        List of page numbers (1-based) that contain colored pixels.
    """
    import numpy as np
    
    doc = fitz.open(pdf_path)
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    
    pages_with_color = []
    
    for page_num in range(len(doc)):
        pix = doc[page_num].get_pixmap(matrix=matrix)
        n = pix.n  # Components per pixel (3=RGB, 4=RGBA)
        
        # Convert to numpy array for fast processing
        img_array = np.frombuffer(pix.samples, dtype=np.uint8).reshape(-1, n)
        
        # Only use RGB channels
        rgb = img_array[:, :3].astype(np.int16)
        
        # Saturation = max(R,G,B) - min(R,G,B)
        saturation = rgb.max(axis=1) - rgb.min(axis=1)
        
        colored_pixel_count = int((saturation > color_threshold).sum())
        
        if colored_pixel_count >= min_colored_pixels:
            pages_with_color.append(page_num + 1)
            print(f"  Page {page_num + 1}: has colored content ({colored_pixel_count} colored pixels)")
        else:
            print(f"  Page {page_num + 1}: no color (skipping)")
    
    doc.close()
    return pages_with_color


def pdf_to_images_pymupdf(pdf_path, dpi=200):
    """Convert PDF to images using PyMuPDF (no poppler needed)."""
    doc = fitz.open(pdf_path)
    images = []
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    
    for page in doc:
        pix = page.get_pixmap(matrix=matrix)
        images.append(pix)
    
    doc.close()
    return images


def convert_revision_pages_to_images(docx_path, output_folder=None, dpi=200, save_all_pages=False):
    """
    Main function: Finds pages with track changes and saves them as images.
    
    Works WITHOUT Microsoft Word. Uses LibreOffice (headless) for PDF conversion.
    Fully compatible with Linux containers and Kubernetes.
    
    Args:
        docx_path: Path to the DOCX file
        output_folder: Where to save images (default: same folder as docx)
        dpi: Resolution of output images
        save_all_pages: If True, saves ALL pages (useful if doc has track changes 
                        but you want complete output). If False, estimates which 
                        pages have revisions.
    
    Returns:
        List of saved image paths
    """
    docx_path = Path(docx_path).resolve()
    
    if not docx_path.exists():
        print(f"Error: File not found: {docx_path}")
        return []
    
    if output_folder is None:
        output_folder = docx_path.parent / f"{docx_path.stem}_revision_images"
    else:
        output_folder = Path(output_folder)
    
    output_folder.mkdir(parents=True, exist_ok=True)
    
    print(f"Processing: {docx_path.name}")
    print("-" * 50)
    
    # Step 1: Check if document has any track changes at all
    print("Checking for track changes...")
    revision_paragraphs = get_paragraphs_with_revisions(str(docx_path))
    
    if not revision_paragraphs:
        print("No track changes found in the document.")
        return []
    
    print(f"Found {len(revision_paragraphs)} paragraphs with track changes.")
    
    temp_dir = tempfile.mkdtemp()
    
    try:
        # Step 2: Convert DOCX to PDF (pywin32 preferred, LibreOffice fallback)
        print("Converting DOCX to PDF...")
        if WIN32COM_AVAILABLE:
            print("  Using pywin32 (MS Word) — track changes markup will be rendered exactly.")
        else:
            print("  Using LibreOffice headless (fallback).")
        original_pdf = docx_to_pdf(str(docx_path), temp_dir, show_markup=True)
        print(f"  PDF created.")
        
        # Save PDF to output folder
        saved_pdf_path = output_folder / (docx_path.stem + ".pdf")
        shutil.copy2(original_pdf, saved_pdf_path)
        print(f"  PDF saved to: {saved_pdf_path}")
        
        # Step 3: Detect pages with COLOR (track changes are always colored)
        print("Scanning pages for colored content (track changes)...")
        
        if save_all_pages:
            doc_orig = fitz.open(original_pdf)
            total_pages = len(doc_orig)
            doc_orig.close()
            pages_to_save = list(range(1, total_pages + 1))
            print(f"Saving ALL {total_pages} pages (save_all_pages=True)")
        else:
            pages_to_save = find_pages_with_color(original_pdf)
            print(f"Pages with track changes (colored): {pages_to_save}")
            
            # If color detection found very few pages but we know there are many
            # revision paragraphs, fall back to saving all pages
            if len(pages_to_save) < 3 and len(revision_paragraphs) > 10:
                doc_orig = fitz.open(original_pdf)
                total_pages = len(doc_orig)
                doc_orig.close()
                print(f"  WARNING: Color detection found only {len(pages_to_save)} pages "
                      f"but {len(revision_paragraphs)} revision paragraphs exist.")
                print(f"  Saving ALL {total_pages} pages to avoid missing revision images.")
                pages_to_save = list(range(1, total_pages + 1))
        
        if not pages_to_save:
            print("No visual differences found between versions.")
            return []
        
        # Step 5: Convert original PDF pages to images and save
        print("Saving revision page images...")
        
        if USE_PYMUPDF:
            pixmaps = pdf_to_images_pymupdf(original_pdf, dpi=dpi)
        else:
            all_images = convert_from_path(original_pdf, dpi=dpi)
        
        saved_paths = []
        for page_num in pages_to_save:
            img_filename = f"{docx_path.stem}_page_{page_num}.png"
            img_path = output_folder / img_filename
            
            if USE_PYMUPDF:
                if page_num - 1 < len(pixmaps):
                    pixmaps[page_num - 1].save(str(img_path))
            else:
                if page_num - 1 < len(all_images):
                    all_images[page_num - 1].save(str(img_path), "PNG")
            
            saved_paths.append(str(img_path))
            print(f"  Saved: {img_filename}")
        
        print("-" * 50)
        print(f"Done! {len(saved_paths)} images saved to: {output_folder}")
        return saved_paths
        
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    # ===== CONFIGURE PATHS HERE =====
    DOCX_FILE = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\Test_8 (April 23, 2026)\CDN_KN_EGWSI CH_1 (Version 6 exe)\Input\Updated Chapter 1 - Who are the Players in the Compensation Game_(2493590.1).docx"
    OUTPUT_FOLDER = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\Test_8 (April 23, 2026)\CDN_KN_EGWSI CH_1 (Version 6 exe)\revision_images"    # Folder where images will be saved
    SAVE_ALL_PAGES = False  # Set True to save ALL pages, False for only revision pages
    # ================================

    convert_revision_pages_to_images(DOCX_FILE, OUTPUT_FOLDER, save_all_pages=SAVE_ALL_PAGES)
