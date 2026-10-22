import React, { useEffect, useRef, useState, useCallback } from 'react';
import { Document, Page, pdfjs } from 'react-pdf';
import 'react-pdf/dist/Page/AnnotationLayer.css';
import 'react-pdf/dist/Page/TextLayer.css';
import type { DocxParagraph, TrackChange, FootnoteMap } from '../types';
import './DocxViewer.css';

// Set up PDF.js worker
pdfjs.GlobalWorkerOptions.workerSrc = `//unpkg.com/pdfjs-dist@${pdfjs.version}/build/pdf.worker.min.mjs`;

const API_URL = 'http://localhost:8000';

interface Props {
  paragraphs: DocxParagraph[];
  selectedChange: TrackChange | null;
  onTextSearch: (text: string) => void;
  docxFile: File | null;
  footnotes: FootnoteMap;
  endnotes: FootnoteMap;
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
 * Uses multi-strategy: exact match, half-segment match, word overlap + sequence bonus.
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
 * Uses multiple signals: context text, paragraph text, word overlap, and adjacent paragraph presence.
 * Returns the top-5 page candidates with detailed scoring so the user can pick the correct page.
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

    // Signal A: context match (prevContext + changeText + postContext)
    const contextScore = ctxText.length > 5 ? fuzzyContainsScore(ctxText, low) : 0;

    // Signal B: full paragraph match
    const paragraphScore = paraText.length > 5 ? fuzzyContainsScore(paraText, low) : 0;

    // Signal C: individual word overlap
    const allWords = (ctxText + ' ' + paraText).split(/\s+/).filter((w) => w.length > 3);
    const uniq = [...new Set(allWords)];
    const wordOverlap =
      uniq.length > 0
        ? (uniq.filter((w) => low.includes(w)).length / uniq.length) * 100
        : 0;

    // Signal D: adjacent paragraph bonus (prev/next paragraph on same page)
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

    // Weighted combination
    const score = Math.min(
      100,
      contextScore * 0.35 + paragraphScore * 0.30 + wordOverlap * 0.20 + adjacentBonus
    );

    // Label the primary matching strategy for display
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

const DocxViewer: React.FC<Props> = ({ paragraphs, selectedChange, onTextSearch, docxFile, footnotes, endnotes }) => {
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
  const [clickLog, setClickLog] = useState<string | null>(null);
  const clickLogTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [selectionInfo, setSelectionInfo] = useState<{ text: string; x: number; y: number } | null>(null);

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

  // Convert DOCX to PDF when file is uploaded (skip if a direct PDF was loaded)
  useEffect(() => {
    if (!docxFile) {
      if (!directPdfFile) setPdfUrl(null);
      return;
    }

    // If user uploaded a direct PDF, don't re-convert the DOCX
    if (directPdfFile) return;

    setPdfLoading(true);
    setPdfError(null);

    const formData = new FormData();
    formData.append('file', docxFile);

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
  }, [docxFile]);

  // Navigate to track change in PDF using fuzzy multi-signal matching
  useEffect(() => {
    if (!selectedChange || !pdfDoc) return;

    setMatchingInProgress(true);
    setTopPageMatches([]);

    findTextInPdfFuzzy(pdfDoc, selectedChange)
      .then((matches) => {
        setTopPageMatches(matches);
        setMatchingInProgress(false);

        // Auto-navigate to best match
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
    // Clear existing highlights
    document.querySelectorAll('.pdf-text-highlight').forEach((el) => {
      el.classList.remove('pdf-text-highlight');
    });

    if (!containerRef.current || !text) return;

    // Find text layer spans that contain the search text
    const pageEl = pageRefs.current.get(pageNum);
    if (!pageEl) return;

    const textLayerSpans = pageEl.querySelectorAll('.react-pdf__Page__textContent span');
    const searchLower = text.toLowerCase().slice(0, 40); // Use first 40 chars for matching

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

  // Navigate to a suggested page (user picks from top-5 list)
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

  // Show a click-log toast for a few seconds
  const showClickLog = useCallback((msg: string) => {
    setClickLog(msg);
    if (clickLogTimer.current) clearTimeout(clickLogTimer.current);
    clickLogTimer.current = setTimeout(() => setClickLog(null), 6000);
  }, []);

  // Auto-search: when selectionInfo is set, fire search immediately
  useEffect(() => {
    if (!selectionInfo) return;
    // Auto-fire the search after a short debounce
    const timer = setTimeout(() => {
      console.log(`🔍 Auto-searching PXML for selected text (${selectionInfo.text.length} chars)`);
      showClickLog(`🔍 Searching PXML: "${selectionInfo.text.substring(0, 60)}${selectionInfo.text.length > 60 ? '...' : ''}"`);
      onTextSearch(selectionInfo.text);
    }, 300);
    return () => clearTimeout(timer);
  }, [selectionInfo, onTextSearch, showClickLog]);

  // Listen for text selection via mouse drag on the PDF canvas
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const handleMouseUp = () => {
      // Delay to let browser finalize selection
      setTimeout(() => {
        const sel = window.getSelection();
        const text = sel?.toString().trim();

        if (!text || text.length < 5) {
          return;
        }

        // Check anchorNode exists
        if (!sel?.anchorNode) {
          return;
        }

        // Get the anchor element
        const anchor = sel.anchorNode instanceof HTMLElement
          ? sel.anchorNode
          : sel.anchorNode.parentElement;

        if (!anchor) {
          return;
        }

        // Check if anchor is inside our container OR inside a pdf page
        const isInContainer = container.contains(anchor);
        const isInPdfPage = !!anchor.closest('.react-pdf__Page__textContent');

        if (!isInContainer && !isInPdfPage) {
          return;
        }

        // Position the search info bar
        const range = sel.getRangeAt(0);
        const rect = range.getBoundingClientRect();
        const containerRect = container.getBoundingClientRect();

        const x = Math.min(
          Math.max(10, rect.right - containerRect.left),
          containerRect.width - 180
        );
        const y = rect.bottom - containerRect.top + container.scrollTop + 8;

        console.log(`✅ Text selected (${text.length} chars): "${text.substring(0, 100)}${text.length > 100 ? '...' : ''}"`);

        // Highlight selected spans persistently
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

        setSelectionInfo({ text, x, y });
      }, 50);
    };

    container.addEventListener('mouseup', handleMouseUp);

    return () => {
      container.removeEventListener('mouseup', handleMouseUp);
    };
  }, [pdfUrl, directPdfFile]);

  // Set up paragraph-level hover highlighting on PDF text.
  // Groups spans into paragraphs by detecting larger-than-normal Y-gaps between lines.
  useEffect(() => {
    if (!containerRef.current) return;

    let currentParaSpans: HTMLElement[] = [];

    const getSpanY = (span: HTMLElement): number => {
      const style = span.style.transform;
      const match = style?.match(/matrix\([^,]+,[^,]+,[^,]+,[^,]+,[^,]+,\s*([\d.]+)\)/);
      if (match) return parseFloat(match[1]);
      return span.getBoundingClientRect().top;
    };

    const clearParaHighlight = () => {
      for (const s of currentParaSpans) s.classList.remove('pdf-para-hover');
      currentParaSpans = [];
    };

    /** Build paragraph groups by detecting Y-gaps larger than 1.6× median line spacing */
    const buildParagraphGroups = (textLayer: Element): HTMLElement[][] => {
      const allSpans = Array.from(textLayer.querySelectorAll('span')).filter(
        (s) => s.textContent && s.textContent.trim().length > 0
      ) as HTMLElement[];
      if (allSpans.length === 0) return [];

      const items = allSpans.map((s) => ({ span: s, y: getSpanY(s) }));
      items.sort((a, b) => a.y - b.y);

      // Group into lines (same Y within 3px tolerance)
      const lines: { y: number; spans: HTMLElement[] }[] = [];
      for (const { span, y } of items) {
        const last = lines[lines.length - 1];
        if (last && Math.abs(last.y - y) < 3) {
          last.spans.push(span);
        } else {
          lines.push({ y, spans: [span] });
        }
      }

      if (lines.length <= 1) return [lines.flatMap((l) => l.spans)];

      // Compute line-to-line gaps and find median
      const gaps: number[] = [];
      for (let i = 1; i < lines.length; i++) {
        gaps.push(Math.abs(lines[i].y - lines[i - 1].y));
      }
      const sorted = [...gaps].sort((a, b) => a - b);
      const medianGap = sorted[Math.floor(sorted.length / 2)] || 14;
      const threshold = medianGap * 1.6;

      // Break into paragraphs at gaps exceeding threshold
      const paragraphs: HTMLElement[][] = [lines[0].spans];
      for (let i = 1; i < lines.length; i++) {
        if (Math.abs(lines[i].y - lines[i - 1].y) > threshold) {
          paragraphs.push([...lines[i].spans]);
        } else {
          paragraphs[paragraphs.length - 1].push(...lines[i].spans);
        }
      }
      return paragraphs;
    };

    // Cache paragraph groups per text layer to avoid re-computing on every hover
    const paraCache = new WeakMap<Element, HTMLElement[][]>();
    const getParas = (tl: Element) => {
      if (!paraCache.has(tl)) paraCache.set(tl, buildParagraphGroups(tl));
      return paraCache.get(tl)!;
    };

    const handleMouseOver = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      if (target.tagName !== 'SPAN') return;
      const textLayer = target.closest('.react-pdf__Page__textContent');
      if (!textLayer) return;

      clearParaHighlight();
      const paragraphs = getParas(textLayer);
      for (const para of paragraphs) {
        if (para.includes(target)) {
          for (const s of para) s.classList.add('pdf-para-hover');
          currentParaSpans = [...para];
          break;
        }
      }
    };

    const handleMouseOut = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      const related = e.relatedTarget as HTMLElement | null;
      if (
        target.closest('.react-pdf__Page__textContent') &&
        (!related || !related.closest?.('.react-pdf__Page__textContent'))
      ) {
        clearParaHighlight();
      }
    };

    const container = containerRef.current;
    container.addEventListener('mouseover', handleMouseOver);
    container.addEventListener('mouseout', handleMouseOut);

    return () => {
      container.removeEventListener('mouseover', handleMouseOver);
      container.removeEventListener('mouseout', handleMouseOut);
    };
  }, [pdfUrl]);

  if (!docxFile && !directPdfFile) {
    return (
      <div className="docx-viewer">
        <div className="pdf-toolbar">
          <span className="pdf-toolbar-title">Document Viewer</span>
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
    <div className="docx-viewer">
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
            â—€
          </button>
          <span className="pdf-page-info">
            Page {currentPage} of {numPages}
          </span>
          <button
            className="pdf-nav-btn"
            onClick={() => setCurrentPage((p) => Math.min(numPages, p + 1))}
            disabled={currentPage >= numPages}
          >
            â–¶
          </button>
        </div>
        <div className="pdf-toolbar-right">
          <button className="pdf-zoom-btn" onClick={() => setScale((s) => Math.max(0.5, s - 0.1))}>âˆ’</button>
          <span className="pdf-zoom-info">{Math.round(scale * 100)}%</span>
          <button className="pdf-zoom-btn" onClick={() => setScale((s) => Math.min(3, s + 0.1))}>+</button>
        </div>
      </div>

      {/* Page Suggestions — top 5 fuzzy-matched pages with scoring breakdown */}
      {selectedChange && (topPageMatches.length > 0 || matchingInProgress) && (
        <div className="page-suggestions-panel">
          <div className="page-suggestions-header">
            <span className="page-suggestions-title">
              🔍 Top Page Matches (fuzzy logic)
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
                  <span title="Context match (prevCtx+text+postCtx)">C:{match.details.contextScore}</span>
                  <span title="Full paragraph text match">P:{match.details.paragraphScore}</span>
                  <span title="Word-level overlap %">W:{match.details.wordOverlap}</span>
                  <span title="Adjacent paragraph bonus">A:{match.details.adjacentBonus}</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="pdf-canvas" ref={containerRef}>
        {/* Click log toast — visible feedback */}
        {clickLog && (
          <div className="click-log-toast" onClick={() => setClickLog(null)}>
            {clickLog}
          </div>
        )}

        {/* Selection search indicator — shows what text was auto-searched */}
        {selectionInfo && (
          <div className="selection-search-popup">
            <span className="selection-search-icon">🔍</span>
            <div className="selection-preview">
              Searching: "{selectionInfo.text.length > 80
                ? selectionInfo.text.substring(0, 80) + '...'
                : selectionInfo.text}"
            </div>
            <button
              className="btn-search-pxml"
              onClick={() => {
                setSelectionInfo(null);
                window.getSelection()?.removeAllRanges();
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
            <p>Converting document to PDF via MS Word...</p>
          </div>
        )}

        {pdfError && (
          <div className="pdf-error">
            <p>âš ï¸ {pdfError}</p>
            <p className="pdf-error-hint">Make sure the backend is running: <code>python backend/main.py</code></p>
          </div>
        )}

        {pdfUrl && (
          <Document
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

export default DocxViewer;
