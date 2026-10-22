import React, { useEffect, useRef, useState } from 'react';
import type { TrackChange, ExcelValidationMap, ExcelMetaMap, ValidityFilter, RevisionStats, FormatRevision } from '../types';
import type { MatchDetail } from '../excelParser';
import ResolverAgent from './ResolverAgent';
import type { PxmlEditorApi } from './PxmlEditor';
import './TrackChangesPanel.css';

// WIP: hide the "Locate" and "Fix with agent" controls from testers for now.
// Flip to true to re-enable them when the work resumes.
const SHOW_WIP_FEATURES = false;

interface Props {
  trackChanges: TrackChange[];
  allTrackChangesCount: number;
  /** Insertion / deletion counts for the whole document (not just the filtered list). */
  stats?: RevisionStats | null;
  /** Formatting-only revisions, listed for reference (not validated). */
  formatRevisions?: FormatRevision[];
  /** revision key -> severity, for revisions already flagged as false positives. */
  falsePositives?: Record<string, string>;
  fpKeyOf?: (tc: TrackChange) => string;
  canFlag?: boolean;
  onFalsePositive?: (tc: TrackChange, index: number, severity: string, note: string) => Promise<void>;
  onUnflagFalsePositive?: (tc: TrackChange) => Promise<void>;
  onSelectChange: (change: TrackChange) => void;
  /** Put text into the XML at the editor cursor as a tracked insertion (null = success, else an error message). */
  onInsertText?: (text: string, anchor?: string) => Promise<string | null>;
  /** Move the XML editor to this revision (cursor after the preceding text, or the deleted text selected). */
  onLocateInXml?: (change: TrackChange) => Promise<string | null>;
  /** Wrap the text currently selected in the XML as deleted. */
  onMarkDeleted?: () => string | null;
  /** For "Fix with agent": the live editor handle, DTD family and session. */
  editorApiRef?: React.MutableRefObject<PxmlEditorApi | null>;
  dtdFamily?: string;
  sessionId?: string | null;
  selectedChangeId: string | null;
  validityFilter: ValidityFilter;
  onFilterChange: (filter: ValidityFilter) => void;
  validationMap: ExcelValidationMap;
  metaMap: ExcelMetaMap;
  onExcelSelect: (file: File) => void;
  excelFileName: string | null;
  matchDetails: MatchDetail[];
  unmappedRevisions: TrackChange[];
  // Session / navigation props
  currentRevisionIndex: number;
  onNavigate: (index: number) => void;
  revisionStatuses: Record<string, string>;
  onMarkComplete: (index: number) => void;
  onMarkSkipped: (index: number) => void;
  onHide?: () => void;
}

/** Break a long validator explanation into short points: numbered items "(1) … (2) …", lines,
 *  semicolons, then sentences. The first few show; the rest sit behind "Show all". */
export function toPoints(text: string): string[] {
  const t = (text || '').replace(/\r/g, '').trim();
  if (!t) return [];
  let parts: string[] = t.split(/\n+/);
  parts = parts.flatMap((p) => p.split(/\s*(?:\(\d{1,2}\)|\b\d{1,2}[.)])\s+(?=[A-Z(])/));
  parts = parts.flatMap((p) => p.split(/;\s+/));
  parts = parts.flatMap((p) => p.split(/(?<=[.!?])\s+(?=[A-Z(])/));
  return parts.map((p) => p.replace(/^[\s\-•*:]+/, '').trim()).filter((p) => p.length > 2);
}

const POINTS_SHOWN = 3;

/** A collapsible card section: click the header to open / close it. */
const Section: React.FC<{ title: string; open: boolean; onToggle: () => void; hint?: string; children: React.ReactNode }> = ({ title, open, onToggle, hint, children }) => (
  <div className={`card-sec ${open ? 'open' : ''}`}>
    <button className="card-sec-head" onClick={onToggle} aria-expanded={open}>
      <span className="card-sec-chev">{open ? '▾' : '▸'}</span>
      <span className="card-sec-title">{title}</span>
      {hint && <span className="card-sec-hint">{hint}</span>}
    </button>
    {open && <div className="card-sec-body">{children}</div>}
  </div>
);

const CrispPoints: React.FC<{ text: string; empty: string }> = ({ text, empty }) => {
  const [all, setAll] = useState(false);
  const points = toPoints(text);
  if (points.length === 0) return <div className="meta-popover-content">{empty}</div>;
  const shown = all ? points : points.slice(0, POINTS_SHOWN);
  return (
    <div className="meta-popover-content">
      <ul className="crisp-points">
        {shown.map((p, i) => <li key={i}>{p}</li>)}
      </ul>
      {points.length > POINTS_SHOWN && (
        <button className="crisp-more" onClick={() => setAll(!all)}>
          {all ? 'Show fewer' : `Show all ${points.length} points`}
        </button>
      )}
    </div>
  );
};

const TrackChangesPanel: React.FC<Props> = ({
  trackChanges,
  allTrackChangesCount,
  stats,
  formatRevisions = [],
  falsePositives = {},
  fpKeyOf,
  canFlag = false,
  onFalsePositive,
  onUnflagFalsePositive,
  onSelectChange,
  onInsertText,
  onLocateInXml,
  onMarkDeleted,
  editorApiRef,
  dtdFamily,
  sessionId,
  selectedChangeId,
  validityFilter,
  onFilterChange,
  validationMap,
  metaMap,
  onExcelSelect,
  excelFileName,
  matchDetails,
  unmappedRevisions,
  currentRevisionIndex,
  onNavigate,
  revisionStatuses,
  onMarkComplete,
  onMarkSkipped,
  onHide,
}) => {
  const excelInputRef = useRef<HTMLInputElement>(null);
  const [showMatchInfo, setShowMatchInfo] = useState(false);
  const [showFormat, setShowFormat] = useState(false);
  // Which card sections are expanded (remembered while moving between revisions).
  const [openSecs, setOpenSecs] = useState<{ text: boolean; reason: boolean; fix: boolean; agent: boolean }>({ text: true, reason: true, fix: false, agent: false });
  const [textNote, setTextNote] = useState('');
  useEffect(() => { setTextNote(''); }, [currentRevisionIndex]);
  const toggleSec = (k: 'text' | 'reason' | 'fix' | 'agent') => setOpenSecs((s) => ({ ...s, [k]: !s[k] }));

  // False-positive flagging: pick a severity (default medium), optional note.
  const [fpOpen, setFpOpen] = useState(false);
  const [fpSeverity, setFpSeverity] = useState<'critical' | 'medium' | 'low'>('medium');
  const [fpNote, setFpNote] = useState('');
  const [fpBusy, setFpBusy] = useState(false);
  const [fpError, setFpError] = useState('');

  const getTypeIcon = (type: string) => {
    switch (type) {
      case 'insertion': return '➕';
      case 'deletion': return '➖';
      case 'formatChange': return '🔄';
      case 'moveTo': return '📥';
      case 'moveFrom': return '📤';
      default: return '•';
    }
  };

  const getTypeClass = (type: string) => {
    switch (type) {
      case 'insertion': return 'change-insertion';
      case 'deletion': return 'change-deletion';
      case 'formatChange': return 'change-format';
      case 'moveTo': return 'change-move-to';
      case 'moveFrom': return 'change-move-from';
      default: return '';
    }
  };

  const formatDate = (dateStr: string) => {
    if (!dateStr) return '';
    try {
      const date = new Date(dateStr);
      return date.toLocaleDateString() + ' ' + date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    } catch {
      return dateStr;
    }
  };

  const handleExcelChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) onExcelSelect(file);
    e.target.value = '';
  };

  const completedCount = Object.values(revisionStatuses).filter(s => s === 'complete').length;
  const totalCount = trackChanges.length;
  const currentChange = trackChanges[currentRevisionIndex] || null;
  const currentStatus = revisionStatuses[String(currentRevisionIndex)] || 'pending';

  const matchedCount = matchDetails.filter((d) => d.matched).length;
  const unmatchedCount = matchDetails.filter((d) => !d.matched).length;

  const fpSeverityNow = currentChange && fpKeyOf ? falsePositives[fpKeyOf(currentChange)] : undefined;

  const submitFalsePositive = async () => {
    if (!currentChange || !onFalsePositive) return;
    setFpBusy(true);
    setFpError('');
    try {
      await onFalsePositive(currentChange, currentRevisionIndex, fpSeverity, fpNote.trim());
      setFpOpen(false);
      setFpNote('');
    } catch (e: any) {
      setFpError(e?.message || 'Could not save the flag');
    } finally {
      setFpBusy(false);
    }
  };

  const undoFalsePositive = async () => {
    if (!currentChange || !onUnflagFalsePositive) return;
    setFpBusy(true);
    setFpError('');
    try { await onUnflagFalsePositive(currentChange); }
    catch (e: any) { setFpError(e?.message || 'Could not remove the flag'); }
    finally { setFpBusy(false); }
  };

  // Navigate and auto-select the change
  const goTo = (idx: number) => {
    if (idx >= 0 && idx < totalCount) {
      onNavigate(idx);
      if (trackChanges[idx]) {
        onSelectChange(trackChanges[idx]);
      }
    }
  };

  // Go to next incomplete revision
  const goToNextIncomplete = () => {
    for (let i = currentRevisionIndex + 1; i < totalCount; i++) {
      if (revisionStatuses[String(i)] !== 'complete') {
        goTo(i);
        return;
      }
    }
    for (let i = 0; i < currentRevisionIndex; i++) {
      if (revisionStatuses[String(i)] !== 'complete') {
        goTo(i);
        return;
      }
    }
  };

  return (
    <div className="track-changes-panel">
      {/* Progress bar header */}
      <div className="panel-header">
        <h3>📋 Revisions</h3>
        <div className="panel-header-right">
          <span className="progress-badge">
            {completedCount}/{totalCount}
          </span>
          {onHide && (
            <button className="panel-hide-btn" onClick={onHide} title="Hide the Revisions panel">
              ⟨ Hide
            </button>
          )}
        </div>
      </div>

      {/* Actual revision counts: what the document really contains, split by type. */}
      {stats && (
        <div
          className="revision-stats"
          title={
            `Validated revisions: ${stats.grouped.insertions} insertions, ${stats.grouped.deletions} deletions` +
            (stats.grouped.moves ? `, ${stats.grouped.moves} moves` : '') +
            (stats.grouped.formatChanges ? `, ${stats.grouped.formatChanges} formatting changes` : '') +
            `\nWord recorded ${stats.raw.insertions} separate insertions and ${stats.raw.deletions} deletions; ` +
            `adjacent pieces are merged into the revisions above, matching the pipeline report.\n` +
            `${stats.insertedChars.toLocaleString()} characters inserted, ${stats.deletedChars.toLocaleString()} deleted.`
          }
        >
          <span className="rev-stat rev-stat-ins">➕ {stats.grouped.insertions} insertions</span>
          <span className="rev-stat rev-stat-del">➖ {stats.grouped.deletions} deletions</span>
          {stats.grouped.moves > 0 && <span className="rev-stat rev-stat-other">↔ {stats.grouped.moves} moves</span>}
          {stats.grouped.formatChanges > 0 && (
            <button
              className={`rev-stat rev-stat-other rev-stat-btn ${showFormat ? 'active' : ''}`}
              onClick={() => setShowFormat(!showFormat)}
              title="Show what was reformatted (not validated — for reference)"
            >
              🎨 {stats.grouped.formatChanges} formatting {showFormat ? '▾' : '▸'}
            </button>
          )}
          <span className="rev-stat rev-stat-raw">
            from {stats.raw.insertions + stats.raw.deletions} Word edits
          </span>
        </div>
      )}

      {showFormat && formatRevisions.length > 0 && (
        <div className="format-list">
          <div className="format-list-note">Formatting edits are listed for reference; they are not part of the validation report.</div>
          {formatRevisions.map((f, i) => (
            <button
              key={f.id}
              className={`format-item ${selectedChangeId === f.id ? 'active' : ''}`}
              onClick={() => onSelectChange({
                id: f.id, type: 'formatChange', author: f.author, date: f.date, text: f.text || f.paragraphText.slice(0, 80),
                paragraphIndex: f.paragraphIndex, runIndex: 0, prevContext: f.prevContext, postContext: f.postContext,
                paragraphText: f.paragraphText, previousPara: '', postPara: '',
              })}
              title="Find this in the document"
            >
              <span className="format-item-num">{i + 1}</span>
              <span className="format-item-main">
                <span className="format-item-chips">{f.changes.map((c, k) => <span key={k} className="format-chip">{c}</span>)}</span>
                <span className="format-item-text">{f.text ? `“${f.text.length > 90 ? f.text.slice(0, 90) + '…' : f.text}”` : f.kind === 'other' ? 'Document / table layout' : ''}</span>
              </span>
            </button>
          ))}
        </div>
      )}

      {/* Progress bar */}
      <div className="progress-bar-container">
        <div
          className="progress-bar-fill"
          style={{ width: totalCount > 0 ? `${(completedCount / totalCount) * 100}%` : '0%' }}
        />
        <span className="progress-bar-text">
          {totalCount > 0 ? `${Math.round((completedCount / totalCount) * 100)}%` : '0%'} complete
        </span>
      </div>

      {/* Excel upload + filter section */}
      <div className="filter-section">
        <div className="excel-upload-row">
          <button className="btn-excel-upload" onClick={() => excelInputRef.current?.click()}>
            📊 Upload Excel
          </button>
          <input ref={excelInputRef} type="file" accept=".xlsx,.xls,.csv" onChange={handleExcelChange} hidden />
          {excelFileName && <span className="excel-filename" title={excelFileName}>{excelFileName}</span>}
        </div>

        {excelFileName && (
          <>
            <div className="filter-row">
              <span className="filter-label">Filter:</span>
              <select className="filter-dropdown" value={validityFilter} onChange={(e) => onFilterChange(e.target.value as ValidityFilter)}>
                <option value="all">All</option>
                <option value="true">✔ Valid</option>
                <option value="false">✗ Invalid</option>
              </select>
            </div>
            <div className="match-summary">
              <span className="match-stat matched">✔ {matchedCount}</span>
              {unmatchedCount > 0 && <span className="match-stat unmatched">⚠ {unmatchedCount} extra</span>}
              {unmappedRevisions.length > 0 && <span className="match-stat context-matched">📌 {unmappedRevisions.length}</span>}
              <button className="btn-match-info" onClick={() => setShowMatchInfo(v => !v)}>
                {showMatchInfo ? 'Hide' : 'Details'}
              </button>
            </div>
            {showMatchInfo && (
              <div className="match-details-panel">
                {matchDetails.map((d) => (
                  <div key={d.rowIndex} className={`match-detail-row ${d.matched ? 'text' : 'none'}`}>
                    <span className="match-detail-score">#{d.rowIndex + 1}</span>
                    <span className="match-detail-valid">{d.isValid ? '✔' : '✗'}</span>
                    <span className="match-detail-text" title={d.excelText}>
                      {d.excelText.length > 50 ? d.excelText.substring(0, 50) + '...' : d.excelText}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </div>

      {/* ─── Single Revision Card (one at a time) ─── */}
      {totalCount === 0 ? (
        <div className="no-changes">
          {allTrackChangesCount > 0 ? (
            <p>No track changes match the current filter.</p>
          ) : (
            <>
              <p>No track changes found.</p>
              <p className="hint">Upload a DOCX file to see revisions here.</p>
            </>
          )}
        </div>
      ) : (
        <div className="revision-navigator">
          {/* Navigation controls */}
          <div className="nav-controls">
            <button className="nav-btn" onClick={() => goTo(currentRevisionIndex - 1)} disabled={currentRevisionIndex <= 0} title="Previous">◀</button>
            <select
              className="nav-jump"
              value={currentRevisionIndex}
              onChange={(e) => goTo(Number(e.target.value))}
              title="Jump to issue number"
            >
              {Array.from({ length: totalCount }, (_, i) => {
                const st = revisionStatuses[String(i)];
                const mark = st === 'complete' ? '✅' : st === 'skipped' ? '⏭' : '⏳';
                return <option key={i} value={i}>{mark} #{i + 1} / {totalCount}</option>;
              })}
            </select>
            <button className="nav-btn" onClick={() => goTo(currentRevisionIndex + 1)} disabled={currentRevisionIndex >= totalCount - 1} title="Next">▶</button>
            <button className="nav-btn nav-skip" onClick={goToNextIncomplete} title="Next incomplete">⏭</button>
          </div>

          {/* Current revision card */}
          {currentChange && (
            <div className={`revision-card ${getTypeClass(currentChange.type)} ${currentStatus === 'complete' ? 'card-complete' : ''}`}>
              <div className="revision-status-row">
                <span className="revision-index" title="Revision number in this list">#{currentRevisionIndex + 1}</span>
                <span className={`revision-status-badge status-${currentStatus}`}>
                  {currentStatus === 'complete' ? '✅ Complete' : currentStatus === 'skipped' ? '⏭ Skipped' : '⏳ Pending'}
                </span>
                {currentChange.id in validationMap && (
                  <span className={`validity-badge ${validationMap[currentChange.id] ? 'valid' : 'invalid'}`}>
                    {validationMap[currentChange.id] ? '✓ Valid' : '✗ Invalid'}
                  </span>
                )}
              </div>
              <div className="revision-actions">
                <button className="btn-mark-complete" onClick={() => onMarkComplete(currentRevisionIndex)} disabled={currentStatus === 'complete'}>✅ Complete</button>
                <button className="btn-mark-skip" onClick={() => onMarkSkipped(currentRevisionIndex)} disabled={currentStatus === 'skipped'}>⏭ Skip</button>
                {onFalsePositive && !fpSeverityNow && !fpOpen && (
                  <button
                    className="btn-fp btn-fp-inline"
                    onClick={() => { setFpOpen(true); setFpError(''); }}
                    disabled={!canFlag}
                    title={canFlag ? 'Report that the validator was wrong about this revision' : 'Open a session to flag false positives'}
                  >
                    🚩 False positive
                  </button>
                )}
              </div>
              {onFalsePositive && (fpSeverityNow || fpOpen || fpError) && (
                <div className="fp-box">
                  {fpSeverityNow ? (
                    <div className="fp-flagged">
                      <span className={`fp-badge fp-${fpSeverityNow}`}>🚩 False positive · {fpSeverityNow}</span>
                      <button className="btn-fp-undo" onClick={undoFalsePositive} disabled={fpBusy}>Undo</button>
                    </div>
                  ) : fpOpen ? (
                    <div className="fp-form">
                      <div className="fp-form-title">How much does this false positive hurt?</div>
                      <div className="fp-severities" role="radiogroup">
                        {(['critical', 'medium', 'low'] as const).map((s) => (
                          <button
                            key={s}
                            role="radio"
                            aria-checked={fpSeverity === s}
                            className={`fp-sev fp-sev-${s} ${fpSeverity === s ? 'active' : ''}`}
                            onClick={() => setFpSeverity(s)}
                            title={s === 'critical' ? 'Blocks or misleads review — fix first' : s === 'medium' ? 'Noticeable noise' : 'Minor / cosmetic'}
                          >
                            {s}
                          </button>
                        ))}
                      </div>
                      <input
                        className="fp-note"
                        type="text"
                        placeholder="Why is it a false positive? (optional)"
                        value={fpNote}
                        onChange={(e) => setFpNote(e.target.value)}
                        onKeyDown={(e) => { if (e.key === 'Enter') submitFalsePositive(); }}
                      />
                      <div className="fp-actions">
                        <button className="btn-fp-submit" onClick={submitFalsePositive} disabled={fpBusy}>{fpBusy ? '⏳' : 'Flag it'}</button>
                        <button className="btn-fp-cancel" onClick={() => setFpOpen(false)} disabled={fpBusy}>Cancel</button>
                      </div>
                    </div>
                  ) : null}
                  {fpError && <div className="fp-error">{fpError}</div>}
                </div>
              )}
              <div className="revision-type-row">
                <span className="change-icon">{getTypeIcon(currentChange.type)}</span>
                <span className="change-type">{currentChange.type}</span>
                <span className="change-author">{currentChange.author}</span>
                {currentChange.date && <span className="revision-meta-inline">{formatDate(currentChange.date)} · Para {currentChange.paragraphIndex + 1}</span>}
              </div>

              <div className="revision-card-body">
                <Section title="Changed text" open={openSecs.text} onToggle={() => toggleSec('text')} hint={currentChange.text ? `${currentChange.text.length} chars` : ''}>
                  <div className="revision-text">
                    {(currentChange.prevContext || currentChange.postContext) && <span className="rc-prev">…{currentChange.prevContext.slice(-80)}</span>}
                    {currentChange.text && currentChange.text.trim()
                      ? <span className="rc-hit">{currentChange.text}</span>
                      : <span className="revision-text-empty">(whitespace / formatting-only change)</span>}
                    {(currentChange.prevContext || currentChange.postContext) && <span className="rc-post">{currentChange.postContext.slice(0, 80)}…</span>}
                  </div>
                  {currentChange.text && currentChange.text.trim() && (
                    <div className="text-actions">
                      {currentChange.type !== 'deletion' && (
                      <button
                        className="chip-btn"
                        title="Copy this text exactly as Word has it"
                        onClick={async () => {
                          try { await navigator.clipboard.writeText(currentChange.text); setTextNote('Copied'); }
                          catch { setTextNote('Copy was blocked by the browser'); }
                        }}
                      >
                        📋 Copy
                      </button>
                      )}
                      {SHOW_WIP_FEATURES && onLocateInXml && (currentChange.type === 'insertion' || currentChange.type === 'deletion') && (
                        <button
                          className="chip-btn"
                          title={currentChange.type === 'insertion'
                            ? 'Move the XML cursor to where this text goes (right after the text that precedes it)'
                            : 'Select this deleted text in the XML'}
                          onClick={async () => setTextNote((await onLocateInXml(currentChange)) || 'Found it in the XML')}
                        >
                          📍 Locate
                        </button>
                      )}
                      {onInsertText && currentChange.type === 'insertion' && (
                        <button
                          className="chip-btn chip-btn-primary"
                          title="Find the spot in the XML from the text before it and insert this as a tracked insertion. Ctrl+Z in the editor undoes it."
                          onClick={async () => setTextNote((await onInsertText(currentChange.text, currentChange.prevContext)) || 'Inserted after the preceding text. Ctrl+Z undoes it.')}
                        >
                          ➕ Insert
                        </button>
                      )}
                      {onMarkDeleted && currentChange.type === 'deletion' && (
                        <button
                          className="chip-btn chip-btn-primary"
                          title="Replace the text selected in the XML with the deleted marker <trace.deleted/>. Use Locate first; Ctrl+Z undoes it."
                          onClick={() => setTextNote(onMarkDeleted() || 'Marked as deleted. Ctrl+Z undoes it.')}
                        >
                          ➖ Mark deleted
                        </button>
                      )}
                    </div>
                  )}
                  {currentChange.text && currentChange.text.trim() && textNote && (
                    <div className="text-note">{textNote}</div>
                  )}
                </Section>

                {SHOW_WIP_FEATURES && editorApiRef && (currentChange.type === 'insertion' || currentChange.type === 'deletion') && (
                  <Section title="🤖 Fix with agent" open={openSecs.agent} onToggle={() => toggleSec('agent')} hint="finds where it belongs">
                    <div style={{ padding: '0 10px 10px' }}>
                      <ResolverAgent
                        key={currentChange.id}
                        row={{
                          'Change Type': currentChange.type === 'insertion' ? 'inline_insertion' : 'inline_deletion',
                          'Inserted Text': currentChange.type === 'insertion' ? currentChange.text : '',
                          'Deleted Text': currentChange.type === 'deletion' ? currentChange.text : '',
                          'Docx_Paragraph': currentChange.paragraphText,
                          'Previous Context': currentChange.prevContext,
                          'Post Context': currentChange.postContext,
                          'Reason': metaMap[currentChange.id]?.reason || '',
                          'Section': '',
                          'Designator': '',
                        }}
                        sessionId={sessionId}
                        editorApiRef={editorApiRef}
                        dtdFamily={dtdFamily}
                      />
                    </div>
                  </Section>
                )}

                {currentChange.id in metaMap && (
                  <>
                    <Section title="💡 Reason" open={openSecs.reason} onToggle={() => toggleSec('reason')}>
                      <CrispPoints key={`${currentChange.id}-r`} text={metaMap[currentChange.id]?.reason || ''} empty="No reason" />
                    </Section>
                    <Section title="🔧 Suggested fix" open={openSecs.fix} onToggle={() => toggleSec('fix')}>
                      <CrispPoints key={`${currentChange.id}-f`} text={metaMap[currentChange.id]?.suggestedFix || ''} empty="No fix" />
                    </Section>
                  </>
                )}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export default TrackChangesPanel;
