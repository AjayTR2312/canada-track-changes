from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import re
import os
import tempfile
import subprocess
from pathlib import Path
from rapidfuzz import fuzz

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── PXML Search (existing logic) ──────────────────────────────────────────


class SearchRequest(BaseModel):
    context_text: str
    pxml_content: str
    chunk_size: int = 500
    overlap_percent: float = 0.3


class ChunkMatch(BaseModel):
    chunk_text: str
    plain_text: str
    score: float
    start_index: int
    end_index: int


class SearchResponse(BaseModel):
    matches: list[ChunkMatch]
    total_chunks: int
    query_text: str


def strip_xml_tags(text: str) -> str:
    """Remove all XML/HTML tags, keeping only text content."""
    clean = re.sub(r'<[^>]+>', ' ', text)
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean


def chunk_pxml(content: str, chunk_size: int, overlap_percent: float) -> list[dict]:
    """Split PXML content into chunks with overlap."""
    overlap = int(chunk_size * overlap_percent)
    step = chunk_size - overlap
    chunks = []

    i = 0
    while i < len(content):
        end = min(i + chunk_size, len(content))
        chunk_text = content[i:end]
        plain_text = strip_xml_tags(chunk_text)
        chunks.append({
            "chunk_text": chunk_text,
            "plain_text": plain_text,
            "start_index": i,
            "end_index": end,
        })
        if end >= len(content):
            break
        i += step

    return chunks


@app.post("/api/search", response_model=SearchResponse)
async def search_pxml(request: SearchRequest):
    """Find best matching chunks in PXML for given context text."""
    chunks = chunk_pxml(request.pxml_content, request.chunk_size, request.overlap_percent)
    query_clean = strip_xml_tags(request.context_text).lower()

    scored = []
    for chunk in chunks:
        score = fuzz.partial_ratio(query_clean, chunk["plain_text"].lower())
        if score > 50:
            scored.append(ChunkMatch(
                chunk_text=chunk["chunk_text"],
                plain_text=chunk["plain_text"],
                score=score,
                start_index=chunk["start_index"],
                end_index=chunk["end_index"],
            ))

    scored.sort(key=lambda x: x.score, reverse=True)
    top_matches = scored[:5]

    return SearchResponse(
        matches=top_matches,
        total_chunks=len(chunks),
        query_text=query_clean[:100],
    )


# ─── DOCX to PDF Conversion ────────────────────────────────────────────────


# Directory to store converted PDFs temporarily
PDF_CACHE_DIR = tempfile.mkdtemp(prefix="docx_pdf_")


@app.post("/api/convert-to-pdf")
async def convert_docx_to_pdf(file: UploadFile = File(...)):
    """
    Convert DOCX to PDF using Microsoft Word (COM automation).
    Track changes are shown as markup in the PDF.
    """
    # Save uploaded file to temp location
    temp_docx = os.path.join(PDF_CACHE_DIR, file.filename or "document.docx")
    with open(temp_docx, "wb") as f:
        content = await file.read()
        f.write(content)

    # Output PDF path
    pdf_filename = Path(temp_docx).stem + ".pdf"
    temp_pdf = os.path.join(PDF_CACHE_DIR, pdf_filename)

    try:
        # Use win32com to convert via MS Word
        import win32com.client
        import pythoncom

        pythoncom.CoInitialize()
        try:
            word = win32com.client.Dispatch("Word.Application")
            word.Visible = False
            word.DisplayAlerts = False

            # CRITICAL: Set global balloon options BEFORE opening the document.
            # This ensures PDF export renders deletions inline with strikethrough,
            # not as balloon annotations in the right margin.
            try:
                word.Options.RevisionsBalloonPrintAndWebLayout = 0  # wdNoBalloons
            except Exception:
                pass

            doc = word.Documents.Open(os.path.abspath(temp_docx))

            # Show track changes as inline markup (strikethrough for deletions)
            doc.ShowRevisions = True
            doc.ActiveWindow.View.ShowRevisionsAndComments = True

            # Force ALL revisions inline — wdInLineRevisions = 0
            try:
                doc.ActiveWindow.View.RevisionsMode = 0
            except Exception:
                pass

            # Ensure deletions show inline (strikethrough), not in balloons
            try:
                doc.ActiveWindow.View.ShowInsertionsAndDeletions = True
            except Exception:
                pass

            # Hide formatting changes and comments (they cause balloon annotations)
            try:
                doc.ActiveWindow.View.ShowFormatChanges = False
            except Exception:
                pass
            try:
                doc.ActiveWindow.View.ShowComments = False
            except Exception:
                pass

            # Set balloon width to zero and disable balloons completely
            try:
                word.Options.RevisionsBalloonPrintAndWebLayout = 0  # wdNoBalloons
            except Exception:
                pass
            try:
                doc.ActiveWindow.View.RevisionsBalloonWidthType = 1  # wdBalloonWidthPercent
                doc.ActiveWindow.View.RevisionsBalloonWidth = 0
            except Exception:
                pass
            try:
                doc.ActiveWindow.View.RevisionsBalloonSide = 0  # hide
            except Exception:
                pass

            # Save as PDF (wdFormatPDF = 17)
            doc.SaveAs(os.path.abspath(temp_pdf), FileFormat=17)
            doc.Close(False)
        finally:
            word.Quit()
            pythoncom.CoUninitialize()

    except ImportError:
        # Fallback: try LibreOffice if win32com not available
        try:
            subprocess.run(
                [
                    "soffice",
                    "--headless",
                    "--convert-to", "pdf",
                    "--outdir", PDF_CACHE_DIR,
                    temp_docx,
                ],
                check=True,
                capture_output=True,
                timeout=60,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            return {"error": f"Conversion failed: {str(e)}"}

    if not os.path.exists(temp_pdf):
        return {"error": "PDF conversion failed - output file not created"}

    return FileResponse(
        temp_pdf,
        media_type="application/pdf",
        filename=pdf_filename,
    )


# ─── Health check ──────────────────────────────────────────────────────────


@app.get("/api/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
