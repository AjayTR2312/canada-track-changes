"""
DOCX Track Changes Image Converter — GUI

A simple desktop application for business users to:
1. Select a DOCX file with track changes
2. Convert it to page images (using MS Word)
3. Download/save a ZIP of the images folder

The output ZIP is what gets uploaded to the PXML Processor web app.

Build as EXE:
    pip install pywin32 pymupdf pyinstaller
    pyinstaller --onefile --windowed --name "DOCX_Image_Converter" image_conversion_gui.py
"""

import os
import sys
import json
import time
import shutil
import tempfile
import subprocess
import zipfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path


# ─── Image conversion logic (same as image_conversion_exe.py) ───

def check_dependencies():
    """Check if required libraries are available."""
    errors = []
    try:
        import win32com.client
    except ImportError:
        errors.append("pywin32 (pip install pywin32)")

    global USE_PYMUPDF, fitz
    USE_PYMUPDF = False
    try:
        import fitz
        USE_PYMUPDF = True
    except ImportError:
        try:
            from pdf2image import convert_from_path
        except ImportError:
            errors.append("pymupdf OR pdf2image (pip install pymupdf)")

    return errors


def convert_docx_to_pdf_with_markup(docx_path: str, output_dir: str, progress_callback=None) -> str:
    """Convert DOCX to PDF with track changes markup visible."""
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    word = None
    try:
        try:
            subprocess.call("taskkill /f /im WINWORD.EXE", shell=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2)
        except Exception:
            pass

        if progress_callback:
            progress_callback("Starting Microsoft Word...")

        try:
            word = win32com.client.dynamic.Dispatch("Word.Application")
        except Exception:
            word = win32com.client.Dispatch("Word.Application")

        word.Visible = False
        word.DisplayAlerts = False

        abs_docx_path = str(Path(docx_path).resolve())
        temp_docx = os.path.join(tempfile.gettempdir(), f"imgconv_{os.path.basename(docx_path)}")
        shutil.copy2(abs_docx_path, temp_docx)

        if progress_callback:
            progress_callback("Opening document...")

        doc = word.Documents.Open(temp_docx, ReadOnly=False, Visible=False,
                                  ConfirmConversions=False, AddToRecentFiles=False)
        try:
            doc.Activate()
        except Exception:
            pass

        # Show track changes markup
        try:
            doc.ActiveWindow.View.Type = 3
            doc.ActiveWindow.View.ShowRevisionsAndComments = True
            doc.ActiveWindow.View.RevisionsView = 0
            doc.ActiveWindow.View.ShowInsertionsAndDeletions = True
            doc.ActiveWindow.View.ShowFormatChanges = True
            doc.ActiveWindow.View.ShowComments = False
        except Exception:
            pass

        pdf_path = os.path.join(output_dir, Path(docx_path).stem + ".pdf")

        if progress_callback:
            progress_callback("Exporting to PDF...")

        doc.ExportAsFixedFormat(
            OutputFileName=str(Path(pdf_path).resolve()),
            ExportFormat=17,
            OpenAfterExport=False,
            OptimizeFor=0,
            Item=7,
            IncludeDocProps=False,
            CreateBookmarks=0,
        )
        doc.Close(SaveChanges=False)
        return pdf_path

    finally:
        if word:
            try:
                word.Quit()
            except Exception:
                pass
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass
        temp_docx = os.path.join(tempfile.gettempdir(), f"imgconv_{os.path.basename(docx_path)}")
        if os.path.exists(temp_docx):
            try:
                os.remove(temp_docx)
            except Exception:
                pass


def pdf_to_page_images(pdf_path: str, output_dir: str, progress_callback=None) -> list:
    """Convert PDF pages to PNG images."""
    image_paths = []

    if USE_PYMUPDF:
        doc = fitz.open(pdf_path)
        total = len(doc)
        for page_num in range(total):
            page = doc.load_page(page_num)
            mat = fitz.Matrix(2.0, 2.0)
            pix = page.get_pixmap(matrix=mat)
            img_path = os.path.join(output_dir, f"page_{page_num + 1}.png")
            pix.save(img_path)
            image_paths.append(img_path)
            if progress_callback:
                progress_callback(f"Converting page {page_num + 1}/{total}...")
        doc.close()
    else:
        from pdf2image import convert_from_path
        images = convert_from_path(pdf_path, dpi=200)
        total = len(images)
        for i, img in enumerate(images):
            img_path = os.path.join(output_dir, f"page_{i + 1}.png")
            img.save(img_path, "PNG")
            image_paths.append(img_path)
            if progress_callback:
                progress_callback(f"Converting page {i + 1}/{total}...")

    return image_paths


def extract_page_texts(pdf_path: str) -> dict:
    """Extract text from each page."""
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
            pass
    return page_texts


def create_zip(folder_path: str, zip_path: str):
    """Create a ZIP file from a folder."""
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(folder_path):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, os.path.dirname(folder_path))
                zf.write(file_path, arcname)


# ─── GUI Application ───

class ImageConverterApp:
    def __init__(self, root):
        self.root = root
        self.root.title("DOCX Track Changes → Image Converter")
        self.root.geometry("650x480")
        self.root.resizable(False, False)
        self.root.configure(bg="#f0f0f5")

        self.docx_path = tk.StringVar()
        self.output_zip_path = None
        self.is_processing = False

        self._build_ui()

    def _build_ui(self):
        # Title
        title_frame = tk.Frame(self.root, bg="#4a54a8", height=60)
        title_frame.pack(fill="x")
        title_frame.pack_propagate(False)
        tk.Label(title_frame, text="📄 DOCX Track Changes Image Converter",
                 font=("Segoe UI", 14, "bold"), fg="white", bg="#4a54a8").pack(pady=15)

        # Main content
        content = tk.Frame(self.root, bg="#f0f0f5", padx=30, pady=20)
        content.pack(fill="both", expand=True)

        # Step 1: Select DOCX
        tk.Label(content, text="Step 1: Select DOCX file with track changes",
                 font=("Segoe UI", 10, "bold"), bg="#f0f0f5", anchor="w").pack(fill="x", pady=(0, 5))

        file_frame = tk.Frame(content, bg="#f0f0f5")
        file_frame.pack(fill="x", pady=(0, 15))

        self.file_entry = tk.Entry(file_frame, textvariable=self.docx_path, font=("Segoe UI", 9),
                                   state="readonly", bg="white", relief="solid", bd=1)
        self.file_entry.pack(side="left", fill="x", expand=True, ipady=4)

        self.browse_btn = tk.Button(file_frame, text="Browse...", font=("Segoe UI", 9),
                                    command=self._browse_file, bg="#4a54a8", fg="white",
                                    relief="flat", padx=15, cursor="hand2")
        self.browse_btn.pack(side="right", padx=(10, 0))

        # Step 2: Convert
        tk.Label(content, text="Step 2: Convert to images",
                 font=("Segoe UI", 10, "bold"), bg="#f0f0f5", anchor="w").pack(fill="x", pady=(10, 5))

        self.convert_btn = tk.Button(content, text="🚀 Convert DOCX to Images",
                                     font=("Segoe UI", 11, "bold"), command=self._start_conversion,
                                     bg="#28a745", fg="white", relief="flat", padx=20, pady=8,
                                     cursor="hand2", state="disabled")
        self.convert_btn.pack(pady=(0, 15))

        # Progress
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(content, variable=self.progress_var,
                                            maximum=100, length=580, mode="indeterminate")
        self.progress_bar.pack(pady=(0, 5))

        self.status_label = tk.Label(content, text="Ready — select a DOCX file to begin",
                                     font=("Segoe UI", 9), bg="#f0f0f5", fg="#666")
        self.status_label.pack(pady=(0, 15))

        # Step 3: Download ZIP
        tk.Label(content, text="Step 3: Save images ZIP (upload this to the web app)",
                 font=("Segoe UI", 10, "bold"), bg="#f0f0f5", anchor="w").pack(fill="x", pady=(10, 5))

        self.download_btn = tk.Button(content, text="💾 Save Images ZIP",
                                      font=("Segoe UI", 11, "bold"), command=self._save_zip,
                                      bg="#17a2b8", fg="white", relief="flat", padx=20, pady=8,
                                      cursor="hand2", state="disabled")
        self.download_btn.pack()

        # Info
        info_text = "This tool requires Microsoft Word installed.\nThe output ZIP should be uploaded alongside your DOCX & PXML in the web processor."
        tk.Label(content, text=info_text, font=("Segoe UI", 8), bg="#f0f0f5",
                 fg="#999", justify="center").pack(side="bottom", pady=(15, 0))

    def _browse_file(self):
        path = filedialog.askopenfilename(
            title="Select DOCX file",
            filetypes=[("Word Documents", "*.docx *.doc"), ("All Files", "*.*")]
        )
        if path:
            self.docx_path.set(path)
            self.convert_btn.config(state="normal")
            self.status_label.config(text=f"Selected: {os.path.basename(path)}")
            self.download_btn.config(state="disabled")
            self.output_zip_path = None

    def _start_conversion(self):
        if self.is_processing:
            return

        # Check dependencies
        errors = check_dependencies()
        if errors:
            messagebox.showerror("Missing Dependencies",
                                 f"Required packages not installed:\n\n" + "\n".join(f"• {e}" for e in errors))
            return

        self.is_processing = True
        self.convert_btn.config(state="disabled")
        self.browse_btn.config(state="disabled")
        self.progress_bar.start(10)

        # Run in background thread
        thread = threading.Thread(target=self._do_conversion, daemon=True)
        thread.start()

    def _do_conversion(self):
        try:
            docx_path = self.docx_path.get()
            docx_stem = Path(docx_path).stem

            # Create temp output folder
            temp_dir = tempfile.mkdtemp(prefix="docx_images_")
            images_dir = os.path.join(temp_dir, f"{docx_stem}_images")
            os.makedirs(images_dir, exist_ok=True)

            # Step 1: DOCX → PDF
            self._update_status("Converting DOCX to PDF with markup...")
            pdf_path = convert_docx_to_pdf_with_markup(
                docx_path, images_dir,
                progress_callback=self._update_status
            )

            # Step 2: PDF → Images
            self._update_status("Converting PDF pages to images...")
            image_paths = pdf_to_page_images(
                pdf_path, images_dir,
                progress_callback=self._update_status
            )

            # Step 3: Extract page texts
            self._update_status("Extracting page texts...")
            page_texts = extract_page_texts(pdf_path)
            page_texts_path = os.path.join(images_dir, "page_texts.json")
            with open(page_texts_path, 'w', encoding='utf-8') as f:
                json.dump({str(k): v for k, v in page_texts.items()}, f, ensure_ascii=False, indent=2)

            # Step 4: Create ZIP
            self._update_status("Creating ZIP file...")
            zip_path = os.path.join(temp_dir, f"{docx_stem}_images.zip")
            create_zip(images_dir, zip_path)

            self.output_zip_path = zip_path
            self._update_status(f"✅ Done! {len(image_paths)} page images generated. Click 'Save Images ZIP'.")
            self.root.after(0, lambda: self.download_btn.config(state="normal"))

        except Exception as e:
            self._update_status(f"❌ Error: {str(e)[:100]}")
            self.root.after(0, lambda: messagebox.showerror("Conversion Error", str(e)))

        finally:
            self.is_processing = False
            self.root.after(0, lambda: self.progress_bar.stop())
            self.root.after(0, lambda: self.convert_btn.config(state="normal"))
            self.root.after(0, lambda: self.browse_btn.config(state="normal"))

    def _update_status(self, text):
        self.root.after(0, lambda: self.status_label.config(text=text))

    def _save_zip(self):
        if not self.output_zip_path or not os.path.isfile(self.output_zip_path):
            messagebox.showwarning("No ZIP", "No images ZIP available. Run conversion first.")
            return

        default_name = os.path.basename(self.output_zip_path)
        save_path = filedialog.asksaveasfilename(
            title="Save Images ZIP",
            defaultextension=".zip",
            initialfile=default_name,
            filetypes=[("ZIP Files", "*.zip"), ("All Files", "*.*")]
        )
        if save_path:
            shutil.copy2(self.output_zip_path, save_path)
            messagebox.showinfo("Saved!", f"Images ZIP saved to:\n{save_path}\n\nUpload this file in the PXML Processor web app.")


def main():
    root = tk.Tk()
    app = ImageConverterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
