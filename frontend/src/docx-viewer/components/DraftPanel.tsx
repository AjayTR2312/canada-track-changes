import React, { useState, useCallback, useEffect, useRef, useMemo } from 'react';
// Reuse the Revisions pane styling so this view looks identical to the
// final-validation card navigator (single card at a time, same colors/chips).
import './TrackChangesPanel.css';
import './DraftPanel.css';
import ResolverAgent from './ResolverAgent';
import type { PxmlEditorApi } from './PxmlEditor';
import { API_BASE } from '../../services/basePath';

const API_URL = API_BASE;

export interface DraftRevision {
  'Row Index': number;
  'Docx_Paragraph': string;
  'Section': string;
  'Designator': string;
  'Reason': string;
  'Change Type': string;
  'Inserted Text': string;
  'Deleted Text': string;
  'Previous Context': string;
  'Post Context': string;
}

type DraftFilter = 'all' | 'insertion' | 'deletion' | 'replacement';

interface Props {
  onNavigate: (text: string, prevContext: string, postContext: string) => void;
  /** When set, the panel auto-pulls the failed-revisions Excel already stored in
   *  this validation session — no manual upload needed. */
  sessionId?: string | null;
  /** Live editor handle + DTD family for the resolver agent. */
  editorApiRef?: React.MutableRefObject<PxmlEditorApi | null>;
  dtdFamily?: string;
}

const DraftPanel: React.FC<Props> = ({ onNavigate, sessionId, editorApiRef, dtdFamily }) => {
  const [resolvedRows, setResolvedRows] = useState<Set<number>>(new Set());
  const [revisions, setRevisions] = useState<DraftRevision[]>([]);
  const [fileName, setFileName] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [currentIndex, setCurrentIndex] = useState(0);
  const [filter, setFilter] = useState<DraftFilter>('all');
  const [activePopover, setActivePopover] = useState<'reason' | 'context' | null>(null);
  const excelInputRef = useRef<HTMLInputElement>(null);

  // Auto-load the session's Excel on mount / when the session changes.
  useEffect(() => {
    if (!sessionId || loaded) return;
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const response = await fetch(`${API_URL}/api/validation/session/${sessionId}/draft-revisions`);
        if (!response.ok) {
          // 404 = no Excel in this session → fall back to manual upload silently.
          if (!cancelled) setLoading(false);
          return;
        }
        const data = await response.json();
        if (!cancelled) {
          setRevisions(data.rows || []);
          setFileName(data.filename || 'Failed_revisions.xlsx');
          setLoaded(true);
        }
      } catch {
        /* leave manual-upload fallback available */
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [sessionId, loaded]);

  const loadDraftRevisions = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetch(`${API_URL}/api/draft-revisions`);
      if (!response.ok) {
        const errData = await response.json().catch(() => ({ detail: 'Failed to load' }));
        throw new Error(errData.detail || 'Failed to load draft revisions');
      }
      const data = await response.json();
      setRevisions(data.rows || []);
      setLoaded(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load draft revisions');
    } finally {
      setLoading(false);
    }
  }, []);

  const handleFileUpload = useCallback(async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setLoading(true);
    setError(null);
    try {
      const formData = new FormData();
      formData.append('file', file);
      const uploadRes = await fetch(`${API_URL}/api/draft-revisions/upload`, {
        method: 'POST',
        body: formData,
      });
      if (!uploadRes.ok) {
        throw new Error('Upload failed');
      }
      setFileName(file.name);
      // After upload, load the revisions
      await loadDraftRevisions();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to upload file');
      setLoading(false);
    }
    if (excelInputRef.current) excelInputRef.current.value = '';
  }, [loadDraftRevisions]);

  const getChangeTypeIcon = (type: string) => {
    switch ((type || '').toLowerCase()) {
      case 'inline_insertion':
      case 'paragraph_addition':
        return '➕';
      case 'inline_deletion':
      case 'paragraph_deletion':
        return '➖';
      case 'inline_replacement':
        return '🔄';
      default:
        return '•';
    }
  };

  // Map the draft change type onto the same card colors as the Revisions view.
  const getChangeTypeClass = (type: string) => {
    const t = (type || '').toLowerCase();
    if (t.includes('insert') || t.includes('addition')) return 'change-insertion';
    if (t.includes('delet')) return 'change-deletion';
    if (t.includes('replace')) return 'change-format';
    return '';
  };

  const matchesFilter = useCallback((rev: DraftRevision) => {
    if (filter === 'all') return true;
    const t = String(rev['Change Type'] || '').toLowerCase();
    if (filter === 'insertion') return t.includes('insert') || t.includes('addition');
    if (filter === 'deletion') return t.includes('delet');
    if (filter === 'replacement') return t.includes('replace');
    return true;
  }, [filter]);

  const filteredRevisions = useMemo(() => revisions.filter(matchesFilter), [revisions, matchesFilter]);

  const totalCount = filteredRevisions.length;
  const current = filteredRevisions[currentIndex] || null;

  const goTo = (idx: number) => {
    if (idx >= 0 && idx < totalCount) {
      setCurrentIndex(idx);
      setActivePopover(null);
    }
  };

  const handleFilterChange = (f: DraftFilter) => {
    setFilter(f);
    setCurrentIndex(0);
    setActivePopover(null);
  };

  // The primary action: jump to this failed revision in the PDF (and fuzzy-search PXML).
  const locateInPdf = (rev: DraftRevision) => {
    const searchText = rev['Docx_Paragraph'] || rev['Inserted Text'] || rev['Deleted Text'] || '';
    onNavigate(searchText, rev['Previous Context'] || '', rev['Post Context'] || '');
  };

  // Card body text — mirror the validation card's monospace text block.
  const cardText = (rev: DraftRevision) => {
    const ins = String(rev['Inserted Text'] || '').trim();
    const del = String(rev['Deleted Text'] || '').trim();
    if (ins && del) return `− ${del}\n+ ${ins}`;
    if (ins) return ins;
    if (del) return del;
    return String(rev['Docx_Paragraph'] || '');
  };

  // ─── Landing state: not yet loaded ───
  if (!loaded) {
    return (
      <div className="track-changes-panel">
        <div className="panel-header">
          <h3>📝 Failed Revisions</h3>
        </div>
        <div className="no-changes">
          {loading && sessionId ? (
            <p>⏳ Loading failed revisions from this session…</p>
          ) : (
            <>
              <p>Upload your Failed_revisions.xlsx to review and navigate.</p>
              <button className="btn-excel-upload" style={{ marginTop: 10 }} onClick={() => excelInputRef.current?.click()} disabled={loading}>
                {loading ? '⏳ Uploading…' : '📊 Upload Failed Revisions (.xlsx)'}
              </button>
              <input ref={excelInputRef} type="file" accept=".xlsx,.xls" onChange={handleFileUpload} hidden />
              {error && <p className="hint" style={{ color: '#f38ba8' }}>⚠️ {error}</p>}
            </>
          )}
        </div>
      </div>
    );
  }

  // ─── Loaded state: card navigator (matches the Revisions view) ───
  return (
    <div className="track-changes-panel">
      <div className="panel-header">
        <h3>📝 Failed Revisions</h3>
        <div className="panel-header-right">
          <span className="progress-badge">{revisions.length} items</span>
        </div>
      </div>

      {/* Position progress bar (parallels the completion bar in Revisions) */}
      <div className="progress-bar-container">
        <div
          className="progress-bar-fill"
          style={{ width: totalCount > 0 ? `${((currentIndex + 1) / totalCount) * 100}%` : '0%' }}
        />
        <span className="progress-bar-text">
          {totalCount > 0 ? `#${currentIndex + 1} / ${totalCount}` : '0'}
        </span>
      </div>

      {/* Excel chip + filter (same layout as the validation view) */}
      <div className="filter-section">
        <div className="excel-upload-row">
          <button className="btn-excel-upload" onClick={() => excelInputRef.current?.click()}>
            📊 Upload Excel
          </button>
          <input ref={excelInputRef} type="file" accept=".xlsx,.xls" onChange={handleFileUpload} hidden />
          {fileName && <span className="excel-filename" title={fileName}>{fileName}</span>}
        </div>
        <div className="filter-row">
          <span className="filter-label">Filter:</span>
          <select className="filter-dropdown" value={filter} onChange={(e) => handleFilterChange(e.target.value as DraftFilter)}>
            <option value="all">All</option>
            <option value="insertion">➕ Insertions</option>
            <option value="deletion">➖ Deletions</option>
            <option value="replacement">🔄 Replacements</option>
          </select>
        </div>
      </div>

      {totalCount === 0 ? (
        <div className="no-changes">
          {revisions.length > 0 ? <p>No failed revisions match the current filter.</p> : <p>No failed revisions found.</p>}
        </div>
      ) : (
        <div className="revision-navigator">
          {/* Navigation controls */}
          <div className="nav-controls">
            <button className="nav-btn" onClick={() => goTo(currentIndex - 1)} disabled={currentIndex <= 0} title="Previous">◀</button>
            <select className="nav-jump" value={currentIndex} onChange={(e) => goTo(Number(e.target.value))} title="Jump to item">
              {Array.from({ length: totalCount }, (_, i) => (
                <option key={i} value={i}>#{i + 1} / {totalCount}</option>
              ))}
            </select>
            <button className="nav-btn" onClick={() => goTo(currentIndex + 1)} disabled={currentIndex >= totalCount - 1} title="Next">▶</button>
          </div>

          {current && (
            <div className={`revision-card ${getChangeTypeClass(current['Change Type'])}`}>
              <div className="revision-type-row">
                <span className="change-icon">{getChangeTypeIcon(current['Change Type'])}</span>
                <span className="change-type">{current['Change Type'] || 'unknown'}</span>
                <span className="change-author">§{current['Section'] || '?'}</span>
              </div>

              <div className="revision-text">{cardText(current)}</div>

              <div className="revision-meta">
                {current['Designator'] ? `${current['Designator']} · ` : ''}Row {current['Row Index'] ?? currentIndex + 1}
              </div>

              <div className="revision-meta-actions">
                {current['Reason'] && (
                  <button className={`btn-meta ${activePopover === 'reason' ? 'active' : ''}`} onClick={() => setActivePopover(activePopover === 'reason' ? null : 'reason')}>💡 Reason</button>
                )}
                {(current['Previous Context'] || current['Post Context']) && (
                  <button className={`btn-meta ${activePopover === 'context' ? 'active' : ''}`} onClick={() => setActivePopover(activePopover === 'context' ? null : 'context')}>🧭 Context</button>
                )}
              </div>

              {activePopover === 'reason' && (
                <div className="meta-popover">
                  <div className="meta-popover-title">💡 Reason</div>
                  <div className="meta-popover-content">{current['Reason'] || 'No reason provided'}</div>
                </div>
              )}
              {activePopover === 'context' && (
                <div className="meta-popover">
                  <div className="meta-popover-title">🧭 Context</div>
                  <div className="meta-popover-content">
                    {current['Previous Context'] ? `…${current['Previous Context']}` : ''}
                    {'  ⟨here⟩  '}
                    {current['Post Context'] ? `${current['Post Context']}…` : ''}
                  </div>
                </div>
              )}

              <div className="revision-actions">
                <button className="btn-mark-complete" onClick={() => locateInPdf(current)}>🔍 Locate in PDF</button>
                {resolvedRows.has(revisions.indexOf(current)) && <span className="draft-resolved">✓ Resolved</span>}
              </div>
              {editorApiRef && (
                <ResolverAgent
                  key={revisions.indexOf(current)}
                  row={current as unknown as Record<string, unknown>}
                  sessionId={sessionId}
                  editorApiRef={editorApiRef}
                  dtdFamily={dtdFamily}
                  onAccepted={() => setResolvedRows((s) => new Set(s).add(revisions.indexOf(current)))}
                />
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export default DraftPanel;
