"""
DOCX Track Changes Image Converter (Standalone EXE)

This tool converts a DOCX file with track changes into page images.
Users run this locally on their Windows machine (requires Microsoft Word),
then upload the resulting images folder as a ZIP alongside the PXML/DOCX.

Usage:
    python image_conversion_exe.py <docx_path> [--output_folder <folder>]

    OR (as compiled EXE):
    image_conversion_exe.exe <docx_path> [--output_folder <folder>]

Output:
    Creates a folder named '<docx_stem>_images/' containing:
      - page_1.png, page_2.png, ... (track change markup images)
      - page_texts.json (extracted text per page for matching)
    
    The user should ZIP this folder and upload it via the Streamlit UI.

Requirements (for building EXE):
    pip install pywin32 pymupdf python-docx pyinstaller
    pyinstaller --onefile image_conversion_exe.py
"""

import os
import sys
import json
import time
import shutil
import tempfile
import subprocess
import argparse
from pathlib import Path

# ─── Check for required libraries ───
try:
    import win32com.client
    WIN32COM_AVAILABLE = True
except ImportError:
    WIN32COM_AVAILABLE = False
    print("[ERROR] pywin32 is required. Install with: pip install pywin32")
    sys.exit(1)

try:
    import fitz  # PyMuPDF
    USE_PYMUPDF = True
except ImportError:
    USE_PYMUPDF = False
    try:
        from pdf2image import convert_from_path
    except ImportError:
        print("[ERROR] Install either pymupdf or pdf2image:")
        print("  pip install pymupdf")
        print("  pip install pdf2image (requires poppler-utils)")
        sys.exit(1)


def convert_docx_to_pdf_with_markup(docx_path: str, output_dir: str) -> str:
    """Convert DOCX to PDF with track changes markup visible using MS Word COM."""
    import pythoncom
    pythoncom.CoInitialize()

    word = None
    try:
        # Kill any existing Word processes
        try:
            subprocess.call("taskkill /f /im WINWORD.EXE", shell=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2)
        except Exception:
            pass

        print("[INFO] Starting Microsoft Word...")
        try:
            word = win32com.client.dynamic.Dispatch("Word.Application")
        except Exception:
            word = win32com.client.Dispatch("Word.Application")

        word.Visible = False
        word.DisplayAlerts = False

        abs_docx_path = str(Path(docx_path).resolve())

        # Create temp copy to avoid locking original
        temp_docx = os.path.join(tempfile.gettempdir(), f"imgconv_{os.path.basename(docx_path)}")
        shutil.copy2(abs_docx_path, temp_docx)

        print("[INFO] Opening document...")
        doc = word.Documents.Open(temp_docx, ReadOnly=False, Visible=False,
                                  ConfirmConversions=False, AddToRecentFiles=False)

        try:
            doc.Activate()
        except Exception:
            pass

        # Show track changes with colored markup
        try:
            doc.ActiveWindow.View.Type = 3  # wdPrintView
            doc.ActiveWindow.View.ShowRevisionsAndComments = True
            doc.ActiveWindow.View.RevisionsView = 0  # wdRevisionsViewFinal (Final: Show Markup)
            doc.ActiveWindow.View.ShowInsertionsAndDeletions = True
            doc.ActiveWindow.View.ShowFormatChanges = True
            doc.ActiveWindow.View.ShowComments = False
            print("[INFO] Track changes markup set to VISIBLE")
        except Exception as e:
            print(f"[WARN] Could not set revision view: {e}")

        # Export to PDF
        pdf_path = os.path.join(output_dir, Path(docx_path).stem + ".pdf")
        print(f"[INFO] Exporting to PDF: {pdf_path}")
        doc.ExportAsFixedFormat(
            OutputFileName=str(Path(pdf_path).resolve()),
            ExportFormat=17,  # wdExportFormatPDF
            OpenAfterExport=False,
            OptimizeFor=0,    # wdExportOptimizeForPrint
            Item=7,           # wdExportDocumentWithMarkup
            IncludeDocProps=False,
            CreateBookmarks=0,
        )

        doc.Close(SaveChanges=False)
        print("[INFO] PDF exported successfully")
        return pdf_path

    except Exception as e:
        print(f"[ERROR] Word conversion failed: {e}")
        raise
    finally:
        if word:
            try:
                word.Quit()
            except Exception:
                pass
        try:
            import pythoncom
            pythoncom.CoUninitialize()
        except Exception:
            pass
        # Cleanup temp file
        temp_docx = os.path.join(tempfile.gettempdir(), f"imgconv_{os.path.basename(docx_path)}")
        if os.path.exists(temp_docx):
            try:
                os.remove(temp_docx)
            except Exception:
                pass


def pdf_to_page_images(pdf_path: str, output_dir: str) -> list:
    """Convert PDF pages to PNG images."""
    image_paths = []

    if USE_PYMUPDF:
        print("[INFO] Converting PDF pages to images using PyMuPDF...")
        doc = fitz.open(pdf_path)
        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            # High DPI for readability
            mat = fitz.Matrix(2.0, 2.0)  # 2x zoom = ~144 DPI
            pix = page.get_pixmap(matrix=mat)
            img_path = os.path.join(output_dir, f"page_{page_num + 1}.png")
            pix.save(img_path)
            image_paths.append(img_path)
            print(f"  Page {page_num + 1}/{len(doc)} saved")
        doc.close()
    else:
        print("[INFO] Converting PDF pages to images using pdf2image...")
        images = convert_from_path(pdf_path, dpi=200)
        for i, img in enumerate(images):
            img_path = os.path.join(output_dir, f"page_{i + 1}.png")
            img.save(img_path, "PNG")
            image_paths.append(img_path)
            print(f"  Page {i + 1}/{len(images)} saved")

    return image_paths


def extract_page_texts_from_pdf(pdf_path: str) -> dict:
    """Extract text from each page of the PDF for matching."""
    page_texts = {}

    if USE_PYMUPDF:
        doc = fitz.open(pdf_path)
        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            page_texts[page_num + 1] = page.get_text("text")
        doc.close()
    else:
        try:
            import pdfplumber
            with pdfplumber.open(pdf_path) as pdf:
                for i, page in enumerate(pdf.pages):
                    page_texts[i + 1] = page.extract_text() or ""
        except ImportError:
            print("[WARN] pdfplumber not available, page_texts will be empty")

    return page_texts


def main():
    parser = argparse.ArgumentParser(
        description="Convert DOCX track changes to page images for PXML processing pipeline"
    )
    parser.add_argument("docx_path", help="Path to the DOCX file with track changes")
    parser.add_argument(
        "--output_folder", "-o",
        help="Output folder for images (default: <docx_stem>_images in same directory)",
        default=None
    )
    args = parser.parse_args()

    docx_path = os.path.abspath(args.docx_path)
    if not os.path.isfile(docx_path):
        print(f"[ERROR] File not found: {docx_path}")
        sys.exit(1)

    if not docx_path.lower().endswith(('.docx', '.doc')):
        print(f"[ERROR] File must be a DOCX/DOC file: {docx_path}")
        sys.exit(1)

    # Determine output folder
    if args.output_folder:
        output_dir = os.path.abspath(args.output_folder)
    else:
        docx_stem = Path(docx_path).stem
        output_dir = os.path.join(os.path.dirname(docx_path), f"{docx_stem}_images")

    os.makedirs(output_dir, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  DOCX Track Changes Image Converter")
    print(f"{'='*60}")
    print(f"  Input:  {docx_path}")
    print(f"  Output: {output_dir}")
    print(f"{'='*60}\n")

    # Step 1: Convert DOCX to PDF with markup
    print("[STEP 1/3] Converting DOCX to PDF with track changes markup...")
    pdf_path = convert_docx_to_pdf_with_markup(docx_path, output_dir)

    # Step 2: Convert PDF pages to images
    print("\n[STEP 2/3] Converting PDF pages to PNG images...")
    image_paths = pdf_to_page_images(pdf_path, output_dir)

    # Step 3: Extract page texts
    print("\n[STEP 3/3] Extracting page texts for matching...")
    page_texts = extract_page_texts_from_pdf(pdf_path)

    # Save page_texts as JSON (needed by pipeline)
    page_texts_path = os.path.join(output_dir, "page_texts.json")
    # Convert int keys to strings for JSON
    with open(page_texts_path, 'w', encoding='utf-8') as f:
        json.dump({str(k): v for k, v in page_texts.items()}, f, ensure_ascii=False, indent=2)

    # Optionally remove the PDF to save space
    # os.remove(pdf_path)

    print(f"\n{'='*60}")
    print(f"  ✅ DONE! Generated {len(image_paths)} page images")
    print(f"  Output folder: {output_dir}")
    print(f"{'='*60}")
    print(f"\n  Next steps:")
    print(f"  1. ZIP the folder: {output_dir}")
    print(f"  2. Upload the ZIP in the PXML Processor web app")
    print(f"     along with your DOCX and PXML files.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
