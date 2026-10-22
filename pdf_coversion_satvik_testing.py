"""Convert a Word .docx to PDF with revision markup, but WITHOUT formatting
change balloons ("Formatted: Font: 12 pt").

Insertions and deletions are kept visible in the PDF; only the formatting
revisions are suppressed -- exactly like unchecking Review > Show Markup >
Formatting in Word.

Requirements:
    - Microsoft Word installed (this drives Word via COM automation).
    - pywin32:  pip install pywin32

Usage:
    Edit DOCX_PATH below (and optionally PDF_PATH), then run:
        python test.py
"""

import os
import shutil
import tempfile
import win32com.client as win32

# ---------------------------------------------------------------------------
# INPUT: set your .docx path here. PDF_PATH is optional -- if left as None the
# PDF is written next to the docx with the same name.
# ---------------------------------------------------------------------------
DOCX_PATH = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\test13\CDN_MA_LCCIC_7_bindu\CDN_MA_LCCIC 7 2026-1 update.docx"
PDF_PATH =   r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\test13\CDN_MA_LCCIC_7_bindu\CDN_MA_LCCIC 7 2026-1 update-clean.pdf"# e.g. r"C:\path\to\output.pdf"

# Word constants (avoids needing the generated COM typelib).
WD_EXPORT_FORMAT_PDF = 17
WD_EXPORT_DOCUMENT_WITH_MARKUP = 7  # include tracked-change markup in the PDF

# Revision types (WdRevisionType) that represent FORMATTING-only changes.
# These are the ones that produce "Formatted: ..." balloons. We reject them so
# they vanish from the PDF, while insertions/deletions are left untouched.
WD_REVISION_PROPERTY = 3            # wdRevisionProperty (character formatting)
WD_REVISION_PARAGRAPH_PROPERTY = 10  # wdRevisionParagraphProperty
WD_REVISION_STYLE = 8              # wdRevisionStyle
WD_REVISION_STYLE_DEFINITION = 13   # wdRevisionStyleDefinition
WD_REVISION_SECTION_PROPERTY = 12   # wdRevisionSectionProperty
WD_REVISION_TABLE_PROPERTY = 11     # wdRevisionTableProperty

_FORMATTING_REVISION_TYPES = {
    WD_REVISION_PROPERTY,
    WD_REVISION_PARAGRAPH_PROPERTY,
    WD_REVISION_STYLE,
    WD_REVISION_STYLE_DEFINITION,
    WD_REVISION_SECTION_PROPERTY,
    WD_REVISION_TABLE_PROPERTY,
}


def _reject_formatting_revisions(doc) -> int:
    """Reject only the formatting-type revisions in the document.

    Insertions and deletions are kept. Iterates the Revisions collection in
    reverse because rejecting a revision removes it from the collection.
    """
    rejected = 0
    revisions = doc.Revisions
    total = revisions.Count
    print(f"Scanning {total} revision(s)...")
    for i in range(total, 0, -1):
        rev = revisions.Item(i)
        if rev.Type in _FORMATTING_REVISION_TYPES:
            rev.Reject()
            rejected += 1
    return rejected


def convert_docx_to_pdf(docx_path: str, pdf_path: str | None = None) -> str:
    docx_path = os.path.abspath(docx_path)
    if not os.path.isfile(docx_path):
        raise FileNotFoundError(f"DOCX not found: {docx_path}")

    if pdf_path is None:
        pdf_path = os.path.splitext(docx_path)[0] + ".pdf"
    pdf_path = os.path.abspath(pdf_path)

    os.makedirs(os.path.dirname(pdf_path), exist_ok=True)

    # Work on a TEMP COPY so we can open it read-write without fighting any
    # existing lock (e.g. the file is open in Word, or it's OneDrive-synced).
    tmp_dir = tempfile.mkdtemp(prefix="docx2pdf_")
    work_docx = os.path.join(tmp_dir, "work.docx")
    shutil.copy2(docx_path, work_docx)
    print(f"Working on temp copy: {work_docx}")

    word = win32.DispatchEx("Word.Application")
    word.Visible = False
    word.DisplayAlerts = 0  # wdAlertsNone
    # Speed: stop Word from repaginating / redrawing after every Reject().
    try:
        word.ScreenUpdating = False
        word.Options.Pagination = False
        word.Options.CheckGrammarAsYouType = False
        word.Options.CheckSpellingAsYouType = False
    except Exception:
        pass

    doc = None
    try:
        # Open the temp copy read-write so we can reject formatting revisions.
        # AddToRecentFiles/ConfirmConversions off to avoid any prompts.
        print(f"Opening: {work_docx}")
        doc = word.Documents.Open(
            work_docx, ReadOnly=False, AddToRecentFiles=False,
            ConfirmConversions=False,
        )

        # Actually remove the formatting revisions so they can't appear at all.
        n = _reject_formatting_revisions(doc)
        print(f"Rejected {n} formatting revision(s).")

        # Show tracked changes (markup) in general...
        doc.ShowRevisions = True

        # ...and as a belt-and-braces measure, also hide format changes in view.
        view = word.ActiveWindow.View
        view.RevisionsMode = 1              # wdInLineRevisions — force ALL inline, no balloons
        view.ShowInsertionsAndDeletions = True   # keep insertions + deletions
        view.ShowFormatChanges = False           # hide "Formatted: ..." balloons
        view.ShowComments = False                # optional: drop comment balloons
        view.ShowRevisionsAndComments = True
        view.RevisionsBalloonWidth = 0

        doc.ExportAsFixedFormat(
            OutputFileName=pdf_path,
            ExportFormat=WD_EXPORT_FORMAT_PDF,
            Item=WD_EXPORT_DOCUMENT_WITH_MARKUP,
        )
    finally:
        if doc is not None:
            doc.Close(SaveChanges=False)  # discard the in-memory rejections
        word.Quit()
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print(f"Saved PDF: {pdf_path}")
    return pdf_path


if __name__ == "__main__":
    convert_docx_to_pdf(DOCX_PATH, PDF_PATH)

 