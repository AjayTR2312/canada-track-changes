import React, { useEffect, useMemo, useRef, useState } from 'react';
import type { DocxParagraph, TrackChange } from '../types';
import './DocxViewer.css';

interface Props {
  paragraphs: DocxParagraph[];
  selectedChange: TrackChange | null;
  onParagraphClick: (paragraphIndex: number) => void;
}

const DocxViewer: React.FC<Props> = ({ paragraphs, selectedChange, onParagraphClick }) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const highlightedRef = useRef<HTMLSpanElement>(null);
  const [clickedParagraph, setClickedParagraph] = useState<number | null>(null);

  // ─── Find bar ───
  const [query, setQuery] = useState('');
  const [cur, setCur] = useState(0);
  const matchIdxs = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return [] as number[];
    return paragraphs
      .filter((p) => p.runs.map((r) => r.text).join('').toLowerCase().includes(q))
      .map((p) => p.paragraphIndex);
  }, [query, paragraphs]);

  // Wrap every occurrence of the query inside a run's text in <mark>, so the actual
  // words are highlighted (not just a faint tint on the whole paragraph).
  const queryRe = useMemo(() => {
    const q = query.trim();
    return q ? new RegExp(`(${q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'gi') : null;
  }, [query]);
  const renderRunText = (text: string): React.ReactNode => {
    if (!queryRe) return text;
    const parts = text.split(queryRe);
    if (parts.length === 1) return text;
    return parts.map((part, i) =>
      i % 2 === 1 ? <mark key={i} className="docx-find-mark">{part}</mark> : part
    );
  };

  const goToMatch = (i: number) => {
    if (!matchIdxs.length) return;
    const idx = (i + matchIdxs.length) % matchIdxs.length;
    setCur(idx);
    const el = document.getElementById(`para-${matchIdxs[idx]}`);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      el.classList.add('para-flash');
      setTimeout(() => el.classList.remove('para-flash'), 1600);
    }
  };
  useEffect(() => { setCur(0); if (matchIdxs.length) goToMatch(0); /* eslint-disable-next-line */ }, [query]);

  useEffect(() => {
    if (selectedChange && highlightedRef.current) {
      highlightedRef.current.scrollIntoView({
        behavior: 'smooth',
        block: 'center',
      });
    }
  }, [selectedChange]);

  if (paragraphs.length === 0) {
    return (
      <div className="docx-viewer">
        <div className="panel-header">
          <h3>Document Viewer</h3>
        </div>
        <div className="viewer-empty">
          <div className="empty-icon">DOC</div>
          <p>Upload a DOCX file to view its content</p>
        </div>
      </div>
    );
  }

  return (
    <div className="docx-viewer">
      <div className="panel-header">
        <h3>Document Viewer</h3>
        <div className="docx-find">
          <input
            className="docx-find-input"
            type="text"
            placeholder="Find in document…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') goToMatch(e.shiftKey ? cur - 1 : cur + 1); }}
          />
          {query && <span className="docx-find-count">{matchIdxs.length ? `${cur + 1}/${matchIdxs.length}` : '0/0'}</span>}
          <button className="docx-find-nav" disabled={!matchIdxs.length} onClick={() => goToMatch(cur - 1)} title="Previous match">▲</button>
          <button className="docx-find-nav" disabled={!matchIdxs.length} onClick={() => goToMatch(cur + 1)} title="Next match">▼</button>
        </div>
      </div>
      <div className="viewer-content" ref={containerRef}>
        {paragraphs.map((paragraph) => (
          <p
            key={paragraph.id}
            className={`docx-paragraph ${clickedParagraph === paragraph.paragraphIndex ? 'paragraph-active' : ''} ${matchIdxs.includes(paragraph.paragraphIndex) ? 'docx-search-hit' : ''} ${matchIdxs[cur] === paragraph.paragraphIndex && query ? 'docx-search-current' : ''}`}
            id={`para-${paragraph.paragraphIndex}`}
            onClick={() => {
              setClickedParagraph(paragraph.paragraphIndex);
              onParagraphClick(paragraph.paragraphIndex);
            }}
          >
            {paragraph.runs.length === 0 ? (
              <span className="empty-para">&nbsp;</span>
            ) : (
              paragraph.runs.map((run) => {
                const isHighlighted =
                  selectedChange !== null &&
                  run.trackChangeId === selectedChange.id;

                return (
                  <span
                    key={run.id}
                    ref={isHighlighted ? highlightedRef : undefined}
                    className={`docx-run ${run.type} ${isHighlighted ? 'highlighted' : ''}`}
                    title={
                      run.author
                        ? `${run.type} by ${run.author}${run.date ? ' on ' + new Date(run.date).toLocaleDateString() : ''}`
                        : undefined
                    }
                  >
                    {renderRunText(run.text)}
                  </span>
                );
              })
            )}
          </p>
        ))}
      </div>
    </div>
  );
};

export default DocxViewer;
