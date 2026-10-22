import React, { useEffect, useRef, useState, useCallback } from 'react';
import { Document, Page, pdfjs } from 'react-pdf';
import { cleanPdfText } from '../textClean';
import 'react-pdf/dist/Page/AnnotationLayer.css';
import 'react-pdf/dist/Page/TextLayer.css';
import type { TrackChange } from '../types';
import './PdfViewer.css';

// Set up PDF.js worker
pdfjs.GlobalWorkerOptions.workerSrc = `//unpkg.com/pdfjs-dist@${pdfjs.version}/build/pdf.worker.min.mjs`;

const API_URL = '';

interface Props {
  selectedChange: TrackChange | null;
  onTextSearch: (text: string) => void;
  /** Insert cleaned PDF text into the XML at the editor cursor. Returns an error message, or null on success. */
  onInsertText?: (text: string) => Promise<string | null>;
  docxFile: File | null;
  sessionPdfUrl?: string | null;
  sessionId?: string | null;
  isVisible?: boolean;
}

/** Match result from fuzzy PDF page matching */
interface PageMatch {
  pageNumber: number;
  score: number;
  matchStrategy: string;
  matchedText: string;
  pageTextPreview: string;
  details: {
    contextScore: number;
    paragraphScore: number;
    wordOverlap: number;
    adjacentBonus: number;
  };
}

interface TextItem {
  str: string;
  transform: number[];
}

/** Normalize whitespace and lowercase for comparison */
function normalizeText(text: string): string {
  return (text || '').replace(/\s+/g, ' ').trim().toLowerCase();
}

/**
 * Fuzzy "contains" scorer — measures how well `search` appears within `target`.
 * Returns 0–100.
 */
function fuzzyContainsScore(search: string, target: string): number {
  if (!search || search.length < 3 || !target) return 0;
  if (target.includes(search)) return 100;

  const halfLen = Math.floor(search.length / 2);
  if (halfLen > 5) {
    const first = search.substring(0, halfLen);
    const second = search.substring(halfLen);
    if (target.includes(first) && target.includes(second)) return 85;
    if (target.includes(first) || target.includes(second)) return 55;
  }

  const words = search.split(/\s+/).filter((w) => w.length > 2);
  if (words.length === 0) return 0;

  let matched = 0;
  let consecutiveMax = 0;
  let cur = 0;
  let lastEnd = -1;

  for (const w of words) {
    const idx = target.indexOf(w, Math.max(0, lastEnd));
    if (idx >= 0) {
      matched++;
      if (lastEnd >= 0 && idx <= lastEnd + 80) {
        cur++;
        consecutiveMax = Math.max(consecutiveMax, cur);
      } else {
        cur = 1;
      }
      lastEnd = idx + w.length;
    } else {
      if (target.indexOf(w) >= 0) matched++;
      cur = 0;
    }
  }

  const wordRatio = matched / words.length;
  const seqBonus = words.length > 1 ? consecutiveMax / words.length : 0;
  return Math.min(100, wordRatio * 50 + seqBonus * 30);
}

/**
 * Fuzzy-match a track change against all PDF pages.
 * Returns the top-5 page candidates with detailed scoring.
 */
async function findTextInPdfFuzzy(
  pdfDoc: any,
  change: TrackChange
): Promise<PageMatch[]> {
  const numPages = pdfDoc.numPages;

  // 1. Extract text from every page
  const pages: { n: number; text: string }[] = [];
  for (let i = 1; i <= numPages; i++) {
    const pg = await pdfDoc.getPage(i);
    const tc = await pg.getTextContent();
    const t = tc.items
      .filter((it: any) => 'str' in it)
      .map((it: TextItem) => it.str)
      .join(' ')
      .replace(/\s+/g, ' ');
    pages.push({ n: i, text: t });
  }

  // 2. Build search signals from track change context
  const changeText = normalizeText(change.text);
  const ctxText = normalizeText(
    (change.prevContext || '') + ' ' + change.text + ' ' + (change.postContext || '')
  );
  const paraText = normalizeText(change.paragraphText || '');
  const prevPara = normalizeText(change.previousPara || '');
  const postPara = normalizeText(change.postPara || '');

  // 3. Score every page using multiple signals
  const scored: PageMatch[] = [];

  for (const { n, text } of pages) {
    const low = text.toLowerCase();

    const contextScore = ctxText.length > 5 ? fuzzyContainsScore(ctxText, low) : 0;
    const paragraphScore = paraText.length > 5 ? fuzzyContainsScore(paraText, low) : 0;

    const allWords = (ctxText + ' ' + paraText).split(/\s+/).filter((w) => w.length > 3);
    const uniq = [...new Set(allWords)];
    const wordOverlap =
      uniq.length > 0
        ? (uniq.filter((w) => low.includes(w)).length / uniq.length) * 100
        : 0;

    let adjacentBonus = 0;
    const checkAdj = (p: string, full: number, partial: number) => {
      if (p.length < 10) return;
      const snip = p.substring(0, Math.min(60, p.length));
      if (low.includes(snip)) { adjacentBonus += full; return; }
      const ws = snip.split(/\s+/).filter((w) => w.length > 3);
      const m = ws.filter((w) => low.includes(w)).length;
      if (ws.length > 0 && m / ws.length > 0.5) adjacentBonus += partial;
    };
    checkAdj(prevPara, 15, 8);
    checkAdj(postPara, 15, 8);

    const score = Math.min(
      100,
      contextScore * 0.35 + paragraphScore * 0.30 + wordOverlap * 0.20 + adjacentBonus
    );

    let matchStrategy = 'fuzzy-words';
    if (contextScore === 100) matchStrategy = 'exact-context';
    else if (paragraphScore === 100) matchStrategy = 'exact-paragraph';
    else if (low.includes(changeText) && changeText.length > 5) matchStrategy = 'exact-text';
    else if (wordOverlap > 70) matchStrategy = 'word-overlap';
    else if (contextScore > 50 || paragraphScore > 50) matchStrategy = 'partial-match';

    if (score > 5) {
      scored.push({
        pageNumber: n,
        score: Math.round(score * 10) / 10,
        matchStrategy,
        matchedText: changeText.substring(0, 60),
        pageTextPreview: text.substring(0, 120).trim(),
        details: {
          contextScore: Math.round(contextScore),
          paragraphScore: Math.round(paragraphScore),
          wordOverlap: Math.round(wordOverlap),
          adjacentBonus: Math.round(adjacentBonus),
        },
      });
    }
  }

  scored.sort((a, b) => b.score - a.score);
  return scored.slice(0, 5);
}

const PdfViewer: React.FC<Props> = ({ selectedChange, onTextSearch, onInsertText, docxFile, sessionPdfUrl, sessionId, isVisible = true }) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const [pdfUrl, setPdfUrl] = useState<string | null>(null);
  const [pdfLoading, setPdfLoading] = useState(false);
  const [pdfError, setPdfError] = useState<string | null>(null);
  const [numPages, setNumPages] = useState<number>(0);
  const [currentPage, setCurrentPage] = useState<number>(1);
  const [pdfDoc, setPdfDoc] = useState<any>(null);
  const [highlightText, setHighlightText] = useState<string | null>(null);
  const [scale, setScale] = useState<number>(1.2);
  const [topPageMatches, setTopPageMatches] = useState<PageMatch[]>([]);
  const [matchingInProgress, setMatchingInProgress] = useState(false);
  const [directPdfFile, setDirectPdfFile] = useState<File | null>(null);
  const [pdfFileName, setPdfFileName] = useState<string | null>(null);
  const pdfInputRef = useRef<HTMLInputElement>(null);
  const pageRefs = useRef<Map<number, HTMLDivElement>>(new Map());
  const [selectionInfo, setSelectionInfo] = useState<{ text: string } | null>(null);
  const [selectionNote, setSelectionNote] = useState('');
  // Track when viewer becomes visible to force Document re-render for correct text layer
  const [renderKey, setRenderKey] = useState(0);

  // ─── Find bar (manual cross-page text search — DocxViewer has an equivalent
  // "Find in document…" input; this was previously missing here, leaving PDF view
  // with only the selection-driven "Searching: …" popup, which requires the user
  // to already have the text on screen to select rather than letting them search
  // for it). Reuses the same per-page getTextContent() extraction already used by
  // findTextInPdfFuzzy below, cached once per document instead of per keystroke. ───
  const [pdfQuery, setPdfQuery] = useState('');
  // One entry per OCCURRENCE (not per page): `occ` is the n-th hit on that page.
  const [pdfMatches, setPdfMatches] = useState<{ page: number; occ: number }[]>([]);
  const [pdfCur, setPdfCur] = useState(0);
  const [pdfFinding, setPdfFinding] = useState(false);
  const pageTextCacheRef = useRef<Map<number, string> | null>(null);
  const revealTimerRef = useRef<number | null>(null);
  const normalizeForFind = (s: string) => s.replace(/\s+/g, ' ').toLowerCase();

  useEffect(() => {
    // A new PDF document invalidates any previously cached page text.
    pageTextCacheRef.current = null;
    setPdfQuery('');
    setPdfMatches([]);
  }, [pdfDoc]);

  // Marks the n-th occurrence of `query` in a page's rendered text layer. The text
  // layer is a run of positioned <span>s, so the page's text is rebuilt from them (with
  // single-space joins) and each span that overlaps a hit gets a highlight class.
  const markFindHits = (pageNum: number, query: string, occ: number): HTMLElement | null => {
    (containerRef.current ?? document).querySelectorAll('.pdf-find-hit, .pdf-find-hit-current').forEach((el) => {
      el.classList.remove('pdf-find-hit', 'pdf-find-hit-current');
    });
    const pageEl = pageRefs.current.get(pageNum);
    // Only the leaf text spans: pdf.js wraps them in zero-size `span.markedContent`
    // containers whose textContent is the concatenation of their children, so
    // marking those would highlight nothing visible (and double-count the text).
    const spans = pageEl
      ? Array.from(pageEl.querySelectorAll<HTMLElement>('.react-pdf__Page__textContent span')).filter(
          (el) => !el.classList.contains('markedContent') && el.querySelector('span') === null
        )
      : [];
    if (!spans.length) return null;
    let acc = '';
    const ranges: { from: number; to: number; el: HTMLElement }[] = [];
    for (const el of spans) {
      const t = (el.textContent || '').replace(/\s+/g, ' ');
      if (!t.trim()) continue;
      if (acc && !acc.endsWith(' ') && !t.startsWith(' ')) acc += ' ';
      ranges.push({ from: acc.length, to: acc.length + t.length, el });
      acc += t;
    }
    const hay = acc.toLowerCase();
    const q = normalizeForFind(query).trim();
    if (!q) return null;
    const hits: number[] = [];
    for (let at = hay.indexOf(q); at !== -1; at = hay.indexOf(q, at + q.length)) hits.push(at);
    if (!hits.length) return null;
    const curAt = hits[Math.min(occ, hits.length - 1)];
    let first: HTMLElement | null = null;
    hits.forEach((at) => {
      const isCur = at === curAt;
      ranges.forEach((r) => {
        if (r.to > at && r.from < at + q.length) {
          r.el.classList.add(isCur ? 'pdf-find-hit-current' : 'pdf-find-hit');
          if (isCur && !first) first = r.el;
        }
      });
    });
    return first;
  };

  // Scrolls the page into view first (so a not-yet-rendered text layer gets built),
  // then retries until its spans exist and the hit can be marked and centered.
  const revealPdfMatch = (page: number, occ: number, query: string) => {
    if (revealTimerRef.current) window.clearTimeout(revealTimerRef.current);
    pageRefs.current.get(page)?.scrollIntoView({ behavior: 'auto', block: 'start' });
    let tries = 0;
    let scrolled = 0;
    const attempt = () => {
      const hit = markFindHits(page, query, occ);
      // A text-layer span exists before pdf.js has laid it out; a zero-size span
      // can't be scrolled to, so wait until it has real dimensions.
      const laidOut = hit && hit.getBoundingClientRect().width > 0;
      if (laidOut) {
        hit!.scrollIntoView({ behavior: 'auto', block: 'center' });
        // Pages above may still be settling their height — re-center once more.
        if (++scrolled < 3) revealTimerRef.current = window.setTimeout(attempt, 400);
      } else if (++tries < 30) {
        revealTimerRef.current = window.setTimeout(attempt, 200);
      }
    };
    revealTimerRef.current = window.setTimeout(attempt, 150);
  };

  const getAllPagesText = useCallback(async (): Promise<Map<number, string>> => {
    if (pageTextCacheRef.current) return pageTextCacheRef.current;
    if (!pdfDoc) return new Map();
    const cache = new Map<number, string>();
    for (let i = 1; i <= pdfDoc.numPages; i++) {
      const pg = await pdfDoc.getPage(i);
      const tc = await pg.getTextContent();
      const text = tc.items
        .filter((it: any) => 'str' in it)
        .map((it: TextItem) => it.str)
        .join(' ');
      cache.set(i, text);
    }
    pageTextCacheRef.current = cache;
    return cache;
  }, [pdfDoc]);

  useEffect(() => {
    let cancelled = false;
    const q = normalizeForFind(pdfQuery).trim();
    if (!q || !pdfDoc) {
      setPdfMatches([]);
      (containerRef.current ?? document).querySelectorAll('.pdf-find-hit, .pdf-find-hit-current').forEach((el) => {
        el.classList.remove('pdf-find-hit', 'pdf-find-hit-current');
      });
      return;
    }
    // Debounced: extracting every page's text is slow on the first search of a big PDF.
    setPdfFinding(true);
    const timer = window.setTimeout(() => {
      getAllPagesText().then((cache) => {
        if (cancelled) return;
        const matches: { page: number; occ: number }[] = [];
        for (let i = 1; i <= pdfDoc.numPages; i++) {
          const hay = normalizeForFind(cache.get(i) || '');
          let occ = 0;
          for (let at = hay.indexOf(q); at !== -1; at = hay.indexOf(q, at + q.length)) {
            matches.push({ page: i, occ: occ++ });
          }
        }
        setPdfMatches(matches);
        setPdfCur(0);
        setPdfFinding(false);
        if (matches.length) {
          setCurrentPage(matches[0].page);
          revealPdfMatch(matches[0].page, matches[0].occ, pdfQuery);
        }
      });
    }, 300);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pdfQuery, pdfDoc, getAllPagesText]);

  const goToPdfMatch = (i: number) => {
    if (!pdfMatches.length) return;
    const idx = ((i % pdfMatches.length) + pdfMatches.length) % pdfMatches.length;
    const m = pdfMatches[idx];
    setPdfCur(idx);
    setCurrentPage(m.page);
    revealPdfMatch(m.page, m.occ, pdfQuery);
  };
  const wasVisibleRef = useRef(isVisible);
  useEffect(() => {
    if (isVisible && !wasVisibleRef.current && pdfUrl) {
      // Became visible — force re-render of Document so text layer gets correct dimensions
      setRenderKey(k => k + 1);
    }
    wasVisibleRef.current = isVisible;
  }, [isVisible, pdfUrl]);

  // Handle direct PDF upload (skip conversion)
  const handleDirectPdfUpload = useCallback((file: File) => {
    if (pdfUrl) URL.revokeObjectURL(pdfUrl);
    const url = URL.createObjectURL(file);
    setDirectPdfFile(file);
    setPdfFileName(file.name);
    setPdfUrl(url);
    setPdfLoading(false);
    setPdfError(null);
  }, [pdfUrl]);

  const handlePdfInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file && file.type === 'application/pdf') {
      handleDirectPdfUpload(file);
    }
    e.target.value = '';
  };

  // Load PDF from session URL if available (skip conversion on resume)
  useEffect(() => {
    if (!sessionPdfUrl) return;
    if (directPdfFile) return; // user uploaded a PDF manually, keep that
    setPdfLoading(true);
    setPdfError(null);
    fetch(sessionPdfUrl)
      .then((res) => {
        if (!res.ok) throw new Error('Failed to load session PDF');
        return res.blob();
      })
      .then((blob) => {
        if (pdfUrl) URL.revokeObjectURL(pdfUrl);
        const url = URL.createObjectURL(blob);
        setPdfUrl(url);
        setPdfLoading(false);
      })
      .catch((err) => {
        setPdfError(err.message);
        setPdfLoading(false);
      });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionPdfUrl]);

  // Convert DOCX to PDF when file is uploaded (only if no session PDF)
  useEffect(() => {
    if (!docxFile) {
      if (!directPdfFile) setPdfUrl(null);
      return;
    }
    if (directPdfFile) return;
    if (sessionPdfUrl) return; // Already have a PDF from session, skip conversion

    setPdfLoading(true);
    setPdfError(null);

    const formData = new FormData();
    formData.append('file', docxFile);
    if (sessionId) {
      formData.append('session_id', sessionId);
    }

    fetch(`${API_URL}/api/convert-to-pdf`, {
      method: 'POST',
      body: formData,
    })
      .then((res) => {
        if (!res.ok) throw new Error('PDF conversion failed');
        return res.blob();
      })
      .then((blob) => {
        const url = URL.createObjectURL(blob);
        setPdfUrl(url);
        setPdfLoading(false);
      })
      .catch((err) => {
        setPdfError(err.message);
        setPdfLoading(false);
      });

    return () => {
      if (pdfUrl) URL.revokeObjectURL(pdfUrl);
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docxFile, sessionPdfUrl]);

  // Navigate to track change in PDF using fuzzy multi-signal matching
  useEffect(() => {
    if (!selectedChange || !pdfDoc) return;

    setMatchingInProgress(true);
    setTopPageMatches([]);

    findTextInPdfFuzzy(pdfDoc, selectedChange)
      .then((matches) => {
        setTopPageMatches(matches);
        setMatchingInProgress(false);

        if (matches.length > 0) {
          const best = matches[0];
          setCurrentPage(best.pageNumber);
          setHighlightText(best.matchedText);

          setTimeout(() => {
            const pageEl = pageRefs.current.get(best.pageNumber);
            if (pageEl) {
              pageEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
            }
          }, 100);

          setTimeout(() => {
            highlightInTextLayer(best.matchedText, best.pageNumber);
          }, 300);
        }
      })
      .catch(() => {
        setMatchingInProgress(false);
      });
  }, [selectedChange, pdfDoc]);

  const highlightInTextLayer = (text: string, pageNum: number) => {
    (containerRef.current ?? document).querySelectorAll('.pdf-text-highlight').forEach((el) => {
      el.classList.remove('pdf-text-highlight');
    });

    if (!containerRef.current || !text) return;

    const pageEl = pageRefs.current.get(pageNum);
    if (!pageEl) return;

    const textLayerSpans = pageEl.querySelectorAll('.react-pdf__Page__textContent span');
    const searchLower = text.toLowerCase().slice(0, 40);

    for (const span of textLayerSpans) {
      const spanText = (span.textContent || '').toLowerCase();
      if (spanText && searchLower.includes(spanText) && spanText.length > 2) {
        span.classList.add('pdf-text-highlight');
      }
    }
  };

  const onDocumentLoadSuccess = useCallback(({ numPages: total }: { numPages: number }) => {
    setNumPages(total);
  }, []);

  const onPdfLoadSuccess = useCallback((pdf: any) => {
    setPdfDoc(pdf);
  }, []);

  const handlePageSuggestionClick = (pageNum: number, matchText: string) => {
    setCurrentPage(pageNum);
    setHighlightText(matchText);

    setTimeout(() => {
      const pageEl = pageRefs.current.get(pageNum);
      if (pageEl) {
        pageEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
    }, 100);

    setTimeout(() => {
      highlightInTextLayer(matchText, pageNum);
    }, 300);
  };

  // Auto-search: fire PXML search when text is selected on PDF
  useEffect(() => {
    if (!selectionInfo) return;
    const timer = setTimeout(() => {
      onTextSearch(selectionInfo.text);
    }, 300);
    return () => clearTimeout(timer);
  }, [selectionInfo, onTextSearch]);

  // Listen for text selection via mouse drag on the PDF canvas
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const handleMouseUp = () => {
      setTimeout(() => {
        // Read the selection from the PDF's OWN document — when this panel is
        // popped out to another window, the global `window` is still the main
        // one, so window.getSelection() there would be empty.
        const sel = container.ownerDocument.getSelection();
        const text = sel?.toString().trim();

        if (!text || text.length < 5) return;
        if (!sel?.anchorNode) return;

        const anchor = sel.anchorNode instanceof HTMLElement
          ? sel.anchorNode
          : sel.anchorNode.parentElement;
        if (!anchor) return;

        const isInContainer = container.contains(anchor);
        const isInPdfPage = !!anchor.closest('.react-pdf__Page__textContent');
        if (!isInContainer && !isInPdfPage) return;

        // Highlight selected spans
        container.querySelectorAll('.pdf-selection-highlight').forEach((el) => {
          el.classList.remove('pdf-selection-highlight');
        });
        const rangeObj = sel.getRangeAt(0);
        const textLayers = container.querySelectorAll('.react-pdf__Page__textContent');
        textLayers.forEach((tl) => {
          const spans = tl.querySelectorAll('span');
          spans.forEach((span) => {
            if (rangeObj.intersectsNode(span)) {
              span.classList.add('pdf-selection-highlight');
            }
          });
        });

        setSelectionNote('');
        setSelectionInfo({ text });
      }, 50);
    };

    container.addEventListener('mouseup', handleMouseUp);
    return () => { container.removeEventListener('mouseup', handleMouseUp); };
  }, [pdfUrl, directPdfFile, docxFile]);

  if (!docxFile && !directPdfFile) {
    return (
      <div className="pdf-viewer">
        <div className="pdf-toolbar">
          <span className="pdf-toolbar-title">PDF Viewer</span>
        </div>
        <div className="viewer-empty">
          <div className="empty-icon">📄</div>
          <p>Upload a DOCX file to view its content as PDF</p>
          <p className="empty-or">— or —</p>
          <button
            className="btn-upload-pdf"
            onClick={() => pdfInputRef.current?.click()}
          >
            📎 Upload PDF directly
          </button>
          <input
            ref={pdfInputRef}
            type="file"
            accept=".pdf"
            onChange={handlePdfInputChange}
            hidden
          />
        </div>
      </div>
    );
  }

  return (
    <div className="pdf-viewer">
      <div className="pdf-toolbar">
        <div className="pdf-toolbar-left">
          <span className="pdf-toolbar-title">📄 {pdfFileName || docxFile?.name || 'Document'}</span>
          <button
            className="btn-toolbar-pdf-upload"
            onClick={() => pdfInputRef.current?.click()}
            title="Upload a PDF directly (replaces current view)"
          >
            📎 PDF
          </button>
          <input
            ref={pdfInputRef}
            type="file"
            accept=".pdf"
            onChange={handlePdfInputChange}
            hidden
          />
        </div>
        <div className="pdf-toolbar-center">
          <button
            className="pdf-nav-btn"
            onClick={() => setCurrentPage((p) => Math.max(1, p - 1))}
            disabled={currentPage <= 1}
          >
            ◀
          </button>
          <span className="pdf-page-info">
            Page {currentPage} of {numPages}
          </span>
          <button
            className="pdf-nav-btn"
            onClick={() => setCurrentPage((p) => Math.min(numPages, p + 1))}
            disabled={currentPage >= numPages}
          >
            ▶
          </button>
          <div className="pdf-find">
            <input
              className="pdf-find-input"
              placeholder="Find in document…"
              value={pdfQuery}
              onChange={(e) => setPdfQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  goToPdfMatch(pdfCur + (e.shiftKey ? -1 : 1));
                }
              }}
            />
            {pdfQuery && (
              <span className="pdf-find-count">
                {pdfFinding ? '…' : pdfMatches.length ? `${pdfCur + 1}/${pdfMatches.length}` : '0/0'}
              </span>
            )}
            {pdfQuery && pdfMatches.length > 1 && (
              <>
                <button className="pdf-find-nav" onClick={() => goToPdfMatch(pdfCur - 1)} title="Previous match">▲</button>
                <button className="pdf-find-nav" onClick={() => goToPdfMatch(pdfCur + 1)} title="Next match">▼</button>
              </>
            )}
          </div>
        </div>
        <div className="pdf-toolbar-right">
          <button className="pdf-zoom-btn" onClick={() => setScale((s) => Math.max(0.5, s - 0.1))}>−</button>
          <span className="pdf-zoom-info">{Math.round(scale * 100)}%</span>
          <button className="pdf-zoom-btn" onClick={() => setScale((s) => Math.min(3, s + 0.1))}>+</button>
        </div>
      </div>

      {/* Page Suggestions — top 5 fuzzy-matched pages */}
      {selectedChange && (topPageMatches.length > 0 || matchingInProgress) && (
        <div className="page-suggestions-panel">
          <div className="page-suggestions-header">
            <span className="page-suggestions-title">
              🔍 Top Page Matches
            </span>
            {matchingInProgress && <span className="matching-spinner">⏳ matching...</span>}
            <button
              className="page-suggestions-close"
              onClick={() => setTopPageMatches([])}
            >
              ✕
            </button>
          </div>
          <div className="page-suggestions-list">
            {topPageMatches.map((match, idx) => (
              <div
                key={match.pageNumber}
                className={`page-suggestion-item ${idx === 0 ? 'best-match' : ''} ${
                  currentPage === match.pageNumber ? 'active' : ''
                }`}
                onClick={() =>
                  handlePageSuggestionClick(match.pageNumber, match.matchedText)
                }
                title={`Context: ${match.details.contextScore}% | Paragraph: ${match.details.paragraphScore}% | Words: ${match.details.wordOverlap}% | Adjacent: ${match.details.adjacentBonus}%`}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                  <span className="suggestion-rank">#{idx + 1}</span>
                  <span className="suggestion-page">Page {match.pageNumber}</span>
                  <span
                    className={`suggestion-score ${
                      match.score > 70 ? 'high' : match.score > 40 ? 'medium' : 'low'
                    }`}
                  >
                    {match.score}%
                  </span>
                </div>
                <span className="suggestion-strategy">{match.matchStrategy}</span>
                <div className="suggestion-details">
                  <span title="Context match">C:{match.details.contextScore}</span>
                  <span title="Paragraph match">P:{match.details.paragraphScore}</span>
                  <span title="Word overlap">W:{match.details.wordOverlap}</span>
                  <span title="Adjacent bonus">A:{match.details.adjacentBonus}</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="pdf-canvas" ref={containerRef}>
        {/* Selection search indicator */}
        {selectionInfo && (
          <div className="selection-search-popup">
            <span className="selection-search-icon">🔍</span>
            <div className="selection-preview">
              {(() => { const t = cleanPdfText(selectionInfo.text); return <>Selected: &quot;{t.length > 80 ? t.substring(0, 80) + '...' : t}&quot;</>; })()}
            </div>
            <button
              className="btn-search-pxml"
              title="Copy the selection as clean text (line breaks and hyphenation fixed)"
              onClick={async () => {
                try { await navigator.clipboard.writeText(cleanPdfText(selectionInfo.text)); setSelectionNote('Copied clean text'); }
                catch { setSelectionNote('Copy was blocked by the browser'); }
              }}
            >
              📋 Copy
            </button>
            {onInsertText && (
              <button
                className="btn-search-pxml"
                title="Insert this text into the XML at the cursor, tagged as a tracked insertion"
                onClick={async () => {
                  const err = await onInsertText(cleanPdfText(selectionInfo.text));
                  setSelectionNote(err || 'Inserted into the XML at the cursor');
                }}
              >
                ➕ Insert in XML
              </button>
            )}
            {selectionNote && <span className="selection-note">{selectionNote}</span>}
            <button
              className="btn-search-pxml"
              onClick={() => {
                setSelectionNote('');
                setSelectionInfo(null);
                containerRef.current?.ownerDocument.getSelection()?.removeAllRanges();
                containerRef.current?.querySelectorAll('.pdf-selection-highlight').forEach((el) => {
                  el.classList.remove('pdf-selection-highlight');
                });
              }}
            >
              ✕
            </button>
          </div>
        )}

        {pdfLoading && (
          <div className="pdf-loading">
            <div className="loading-spinner" />
            <p>Converting to PDF via MS Word — first time only (~30–60s). Reused after this.</p>
          </div>
        )}

        {pdfError && (
          <div className="pdf-error">
            <p>⚠️ {pdfError}</p>
            <p className="pdf-error-hint">Make sure the backend is running and MS Word is installed.</p>
          </div>
        )}

        {pdfUrl && (
          <Document
            key={renderKey}
            file={pdfUrl}
            onLoadSuccess={(pdf) => {
              onDocumentLoadSuccess(pdf);
              onPdfLoadSuccess(pdf);
            }}
            loading={
              <div className="pdf-loading">
                <div className="loading-spinner" />
                <p>Loading PDF...</p>
              </div>
            }
          >
            {Array.from({ length: numPages }, (_, i) => i + 1).map((pageNum) => (
              <div
                key={pageNum}
                className={`pdf-page-wrapper ${pageNum === currentPage ? 'current-page' : ''}`}
                ref={(el) => {
                  if (el) pageRefs.current.set(pageNum, el);
                }}
              >
                <Page
                  pageNumber={pageNum}
                  scale={scale}
                  renderTextLayer={true}
                  renderAnnotationLayer={true}
                />
                <div className="pdf-page-number">Page {pageNum}</div>
              </div>
            ))}
          </Document>
        )}
      </div>
    </div>
  );
};

export default PdfViewer;
