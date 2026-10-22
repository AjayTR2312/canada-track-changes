import React, { useState, useCallback } from 'react';
import './DraftPanel.css';

const API_URL = 'http://localhost:8000';

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

interface Props {
  onNavigate: (text: string, prevContext: string, postContext: string) => void;
}

const DraftPanel: React.FC<Props> = ({ onNavigate }) => {
  const [revisions, setRevisions] = useState<DraftRevision[]>([]);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null);

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

  const handleItemClick = (rev: DraftRevision, index: number) => {
    setSelectedIndex(index);
    // Use the paragraph text for PDF navigation, along with context
    const searchText = rev['Docx_Paragraph'] || '';
    const prevContext = rev['Previous Context'] || '';
    const postContext = rev['Post Context'] || '';
    onNavigate(searchText, prevContext, postContext);
  };

  const getChangeTypeIcon = (type: string) => {
    switch (type?.toLowerCase()) {
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

  const getChangeTypeClass = (type: string) => {
    switch (type?.toLowerCase()) {
      case 'inline_insertion':
      case 'paragraph_addition':
        return 'draft-insertion';
      case 'inline_deletion':
      case 'paragraph_deletion':
        return 'draft-deletion';
      case 'inline_replacement':
        return 'draft-replacement';
      default:
        return '';
    }
  };

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
      // After upload, load the revisions
      await loadDraftRevisions();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to upload file');
      setLoading(false);
    }
  }, [loadDraftRevisions]);

  if (!loaded) {
    return (
      <div className="draft-panel">
        <div className="draft-panel-header">
          <h3>📝 Draft — Failed Revisions</h3>
        </div>
        <div className="draft-load-section">
          <p className="draft-description">
            Upload your Failed_revisions.xlsx to review and navigate in the PDF.
          </p>
          <label className="btn-load-draft" style={{ cursor: 'pointer', display: 'inline-block' }}>
            {loading ? '⏳ Uploading...' : '📂 Upload Failed Revisions (.xlsx)'}
            <input
              type="file"
              accept=".xlsx,.xls"
              onChange={handleFileUpload}
              style={{ display: 'none' }}
              disabled={loading}
            />
          </label>
          {error && <div className="draft-error">⚠️ {error}</div>}
        </div>
      </div>
    );
  }

  return (
    <div className="draft-panel">
      <div className="draft-panel-header">
        <h3>📝 Draft — Failed Revisions</h3>
        <span className="draft-count">{revisions.length} items</span>
      </div>

      {error && <div className="draft-error">⚠️ {error}</div>}

      <div className="draft-list">
        {revisions.length === 0 ? (
          <div className="draft-empty">No failed revisions found.</div>
        ) : (
          revisions.map((rev, idx) => (
            <div
              key={idx}
              className={`draft-item ${getChangeTypeClass(rev['Change Type'])} ${selectedIndex === idx ? 'selected' : ''}`}
              onClick={() => handleItemClick(rev, idx)}
            >
              <div className="draft-item-header">
                <span className="draft-item-index">#{idx + 1}</span>
                <span className="draft-item-icon">{getChangeTypeIcon(rev['Change Type'])}</span>
                <span className="draft-item-type">{rev['Change Type'] || 'unknown'}</span>
                <span className="draft-item-section">§{rev['Section'] || '?'}</span>
              </div>

              <div className="draft-item-reason">
                {rev['Reason'] || 'No reason provided'}
              </div>

              {rev['Deleted Text'] && (
                <div className="draft-item-deleted">
                  <span className="draft-label">Deleted:</span> {String(rev['Deleted Text']).length > 80
                    ? String(rev['Deleted Text']).substring(0, 80) + '...'
                    : String(rev['Deleted Text'])}
                </div>
              )}

              {rev['Inserted Text'] && (
                <div className="draft-item-inserted">
                  <span className="draft-label">Inserted:</span> {String(rev['Inserted Text']).length > 80
                    ? String(rev['Inserted Text']).substring(0, 80) + '...'
                    : String(rev['Inserted Text'])}
                </div>
              )}

              <div className="draft-item-context">
                {rev['Previous Context'] && (
                  <span className="draft-context-prev" title="Previous context">…{String(rev['Previous Context']).substring(0, 40)}</span>
                )}
                <span className="draft-context-sep">|</span>
                {rev['Post Context'] && (
                  <span className="draft-context-post" title="Post context">{String(rev['Post Context']).substring(0, 40)}…</span>
                )}
              </div>

              <div className="draft-item-para" title={rev['Docx_Paragraph']}>
                {rev['Docx_Paragraph']
                  ? rev['Docx_Paragraph'].substring(0, 120) + (rev['Docx_Paragraph'].length > 120 ? '...' : '')
                  : ''}
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
};

export default DraftPanel;
