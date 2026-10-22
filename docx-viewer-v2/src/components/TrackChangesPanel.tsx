import React, { useRef, useState } from 'react';
import type { TrackChange, ExcelValidationMap, ExcelMetaMap, ValidityFilter } from '../types';
import type { MatchDetail } from '../excelParser';
import './TrackChangesPanel.css';

interface Props {
  trackChanges: TrackChange[];
  allTrackChangesCount: number;
  onSelectChange: (change: TrackChange) => void;
  selectedChangeId: string | null;
  validityFilter: ValidityFilter;
  onFilterChange: (filter: ValidityFilter) => void;
  validationMap: ExcelValidationMap;
  metaMap: ExcelMetaMap;
  onExcelSelect: (file: File) => void;
  excelFileName: string | null;
  matchDetails: MatchDetail[];
  unmappedRevisions: TrackChange[];
}

const TrackChangesPanel: React.FC<Props> = ({
  trackChanges,
  allTrackChangesCount,
  onSelectChange,
  selectedChangeId,
  validityFilter,
  onFilterChange,
  validationMap,
  metaMap,
  onExcelSelect,
  excelFileName,
  matchDetails,
  unmappedRevisions,
}) => {
  const excelInputRef = useRef<HTMLInputElement>(null);
  const [showMatchInfo, setShowMatchInfo] = useState(false);
  const [openMenuId, setOpenMenuId] = useState<string | null>(null);
  const [activePopover, setActivePopover] = useState<{ id: string; type: 'reason' | 'suggestedFix' } | null>(null);

  const getTypeIcon = (type: string) => {
    switch (type) {
      case 'inline_insertion':
      case 'paragraph_addition':
        return '➕';
      case 'inline_deletion':
      case 'paragraph_deletion':
        return '➖';
      case 'inline_replacement':
        return '🔄';
      case 'moveTo':
        return '📥';
      case 'moveFrom':
        return '📤';
      default:
        return '•';
    }
  };

  const getTypeClass = (type: string) => {
    switch (type) {
      case 'inline_insertion':
      case 'paragraph_addition':
        return 'change-insertion';
      case 'inline_deletion':
      case 'paragraph_deletion':
        return 'change-deletion';
      case 'inline_replacement':
        return 'change-format';
      case 'moveTo':
        return 'change-move-to';
      case 'moveFrom':
        return 'change-move-from';
      default:
        return '';
    }
  };

  const getTypeLabel = (type: string) => {
    switch (type) {
      case 'inline_insertion':
        return 'Inline Insertion';
      case 'inline_deletion':
        return 'Inline Deletion';
      case 'inline_replacement':
        return 'Inline Replacement';
      case 'paragraph_addition':
        return 'Paragraph Addition';
      case 'paragraph_deletion':
        return 'Paragraph Deletion';
      case 'moveTo':
        return 'Move To';
      case 'moveFrom':
        return 'Move From';
      default:
        return type;
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
    if (file) {
      onExcelSelect(file);
    }
    // Reset the input so the same file can be re-selected
    e.target.value = '';
  };

  const getValidityBadge = (trackChangeId: string) => {
    if (!(trackChangeId in validationMap)) return null;
    const isValid = validationMap[trackChangeId];
    return (
      <span className={`validity-badge ${isValid ? 'valid' : 'invalid'}`}>
        {isValid ? '✓' : '✗'}
      </span>
    );
  };

  const matchedCount = matchDetails.filter((d) => d.matched).length;
  const unmatchedCount = matchDetails.filter((d) => !d.matched).length;

  return (
    <div className="track-changes-panel">
      <div className="panel-header">
        <h3>📋 Track Changes</h3>
        <span className="change-count">
          {trackChanges.length} changes
        </span>
      </div>

      {/* Excel upload + filter section */}
      <div className="filter-section">
        <div className="excel-upload-row">
          <button
            className="btn-excel-upload"
            onClick={() => excelInputRef.current?.click()}
          >
            📊 Upload Validation Excel
          </button>
          <input
            ref={excelInputRef}
            type="file"
            accept=".xlsx,.xls,.csv"
            onChange={handleExcelChange}
            hidden
          />
          {excelFileName && (
            <span className="excel-filename" title={excelFileName}>
              {excelFileName}
            </span>
          )}
        </div>

        {/* Validity filter dropdown — only visible once Excel is loaded */}
        {excelFileName && (
          <>
            <div className="filter-row">
              <span className="filter-label">Filter by is_valid:</span>
              <select
                className="filter-dropdown"
                value={validityFilter}
                onChange={(e) =>
                  onFilterChange(e.target.value as ValidityFilter)
                }
              >
                <option value="all">All</option>
                <option value="true">✔ Valid (true)</option>
                <option value="false">✗ Invalid (false)</option>
              </select>
            </div>

            {/* Match summary */}
            <div className="match-summary">
              <span className="match-stat matched">
                ✔ {matchedCount} matched
              </span>
              {unmatchedCount > 0 && (
                <span className="match-stat unmatched">
                  ⚠ {unmatchedCount} extra rows
                </span>
              )}
              {unmappedRevisions.length > 0 && (
                <span className="match-stat context-matched">
                  📌 {unmappedRevisions.length} unmapped
                </span>
              )}
              <button
                className="btn-match-info"
                onClick={() => setShowMatchInfo((v) => !v)}
              >
                {showMatchInfo ? 'Hide details' : 'Show details'}
              </button>
            </div>

            {/* Expandable match details */}
            {showMatchInfo && (
              <div className="match-details-panel">
                {matchDetails.map((d) => (
                  <div
                    key={d.rowIndex}
                    className={`match-detail-row ${d.matched ? 'text' : 'none'}`}
                  >
                    <span className="match-detail-score">#{d.rowIndex + 1}</span>
                    <span className="match-detail-valid">
                      {d.isValid ? '✔' : '✗'}
                    </span>
                    <span className="match-detail-text" title={d.excelText}>
                      {d.excelText.length > 60
                        ? d.excelText.substring(0, 60) + '...'
                        : d.excelText}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </div>

      {/* Track changes list */}
      <div className="changes-list">
        {trackChanges.length === 0 ? (
          <div className="no-changes">
            {allTrackChangesCount > 0 ? (
              <>
                <p>No track changes match the current filter.</p>
                <p className="hint">Try changing the filter dropdown above.</p>
              </>
            ) : (
              <>
                <p>No track changes found.</p>
                <p className="hint">Upload a DOCX file with tracked changes to see them here.</p>
              </>
            )}
          </div>
        ) : (
          trackChanges.map((change) => (
            <div
              key={change.id}
              className={`change-item ${getTypeClass(change.type)} ${selectedChangeId === change.id ? 'selected' : ''}`}
              onClick={() => onSelectChange(change)}
            >
              <div className="change-header">
                <span className="revision-order">#{change.revisionOrder}</span>
                <span className="change-icon">{getTypeIcon(change.type)}</span>
                <span className="change-type">{getTypeLabel(change.type)}</span>
                {getValidityBadge(change.id)}
                <span className="change-author">{change.author}</span>
                {/* Kebab menu - only show if we have meta for this change */}
                {change.id in metaMap && (
                  <div className="kebab-wrapper">
                    <button
                      className="kebab-btn"
                      onClick={(e) => {
                        e.stopPropagation();
                        setOpenMenuId(openMenuId === change.id ? null : change.id);
                        setActivePopover(null);
                      }}
                      title="More info"
                    >
                      ⋮
                    </button>
                    {openMenuId === change.id && (
                      <div className="kebab-menu">
                        <button
                          className="kebab-menu-item"
                          onClick={(e) => {
                            e.stopPropagation();
                            setActivePopover({ id: change.id, type: 'reason' });
                            setOpenMenuId(null);
                          }}
                        >
                          💡 Reason
                        </button>
                        <button
                          className="kebab-menu-item"
                          onClick={(e) => {
                            e.stopPropagation();
                            setActivePopover({ id: change.id, type: 'suggestedFix' });
                            setOpenMenuId(null);
                          }}
                        >
                          🔧 Suggested Fix
                        </button>
                      </div>
                    )}
                  </div>
                )}
              </div>
              <div className="change-text">
                {change.text.length > 80 ? change.text.substring(0, 80) + '...' : change.text}
              </div>
              {/* Context info — matching backend prev_context / post_context */}
              {(change.prevContext || change.postContext) && (
                <div className="change-context">
                  {change.prevContext && (
                    <span className="context-before" title="Previous context">…{change.prevContext}</span>
                  )}
                  <span className="context-separator">|</span>
                  {change.postContext && (
                    <span className="context-after" title="Post context">{change.postContext}…</span>
                  )}
                </div>
              )}
              {/* Popover for Reason / Suggested Fix */}
              {activePopover && activePopover.id === change.id && (
                <div className="info-popover">
                  <div className="info-popover-header">
                    <span className="info-popover-title">
                      {activePopover.type === 'reason' ? '💡 Reason' : '🔧 Suggested Fix'}
                    </span>
                    <button
                      className="info-popover-close"
                      onClick={(e) => {
                        e.stopPropagation();
                        setActivePopover(null);
                      }}
                    >
                      ✕
                    </button>
                  </div>
                  <div className="info-popover-content">
                    {activePopover.type === 'reason'
                      ? (metaMap[change.id]?.reason || 'No reason provided')
                      : (metaMap[change.id]?.suggestedFix || 'No suggested fix provided')}
                  </div>
                </div>
              )}
              {change.date && (
                <div className="change-date">{formatDate(change.date)}</div>
              )}
              <div className="change-location">
                Paragraph {change.paragraphIndex + 1}
              </div>
            </div>
          ))
        )}
      </div>

      {/* Unmapped revisions — track changes with no Excel row */}
      {unmappedRevisions.length > 0 && excelFileName && (
        <div className="unmatched-section">
          <div className="unmatched-section-header">
            <span>📌 Unmapped Revisions (no Excel row)</span>
            <span className="unmatched-count">{unmappedRevisions.length}</span>
          </div>
          <div className="unmatched-list">
            {unmappedRevisions.map((change, idx) => (
              <div
                key={change.id}
                className={`change-item unmatched-item ${getTypeClass(change.type)} ${selectedChangeId === change.id ? 'selected' : ''}`}
                onClick={() => onSelectChange(change)}
              >
                <div className="change-header">
                  <span className="revision-order">#{change.revisionOrder}</span>
                  <span className="change-icon">{getTypeIcon(change.type)}</span>
                  <span className="change-type">{getTypeLabel(change.type)}</span>
                  <span className="unmatched-label">#{matchedCount + idx + 1}</span>
                  <span className="change-author">{change.author}</span>
                </div>
                <div className="change-text">
                  {change.text.length > 80 ? change.text.substring(0, 80) + '...' : change.text}
                </div>
                <div className="change-location">
                  Paragraph {change.paragraphIndex + 1}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
};

export default TrackChangesPanel;
