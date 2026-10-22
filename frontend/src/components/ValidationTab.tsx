import React, { useState, useEffect } from 'react';
import { useAuth } from '../context/AuthContext';
import { useJob } from '../context/JobContext';
import { listOutputs, deleteValidationSession } from '../services/api';
import './Tabs.css';

const DOCX_VIEWER_URL = '/docx-viewer.html';

interface RecentSession {
  session_id: string;
  session_name: string;
  updated_at: number;
  docx_filename: string;
  excel_filename: string | null;
  total_revisions: number;
  total_invalid: number;
  completed_count?: number;
  progress_text?: string;
}

export default function ValidationTab() {
  const { user } = useAuth();
  const { jobId, status } = useJob();
  const [outputFiles, setOutputFiles] = useState<string[]>([]);
  const [recentSessions, setRecentSessions] = useState<RecentSession[]>([]);
  const [loadingSessions, setLoadingSessions] = useState(true);
  const [deleting, setDeleting] = useState<string | null>(null);

  const isDone = status?.status === 'completed';

  useEffect(() => {
    if (!jobId || !isDone) return;
    listOutputs(jobId).then(setOutputFiles).catch(() => {});
  }, [jobId, isDone]);

  // Fetch recent validation sessions (filtered by logged-in user)
  useEffect(() => {
    setLoadingSessions(true);
    const empId = user?.emp_id || '';
    const qs = empId ? `?emp_id=${encodeURIComponent(empId)}` : '';
    fetch(`/api/validation/sessions${qs}`)
      .then(res => res.json())
      .then(data => setRecentSessions(data.sessions || []))
      .catch(() => {})
      .finally(() => setLoadingSessions(false));
  }, [user]);

  const openVisualValidator = () => {
    window.open(DOCX_VIEWER_URL, '_blank', 'noopener,noreferrer');
  };

  const resumeSession = (sessionId: string) => {
    window.open(`${DOCX_VIEWER_URL}?session=${sessionId}`, '_blank', 'noopener,noreferrer');
  };

  const handleDelete = async (sessionId: string) => {
    if (!confirm('Delete this validation session? Its files will be removed from storage.')) return;
    setDeleting(sessionId);
    try {
      await deleteValidationSession(sessionId, user?.emp_id || '');
      setRecentSessions((prev) => prev.filter((s) => s.session_id !== sessionId));
    } catch (err: any) {
      alert(err?.response?.data?.detail || 'Delete failed.');
    } finally {
      setDeleting(null);
    }
  };

  return (
    <div className="tab-section">
      <h2>🔍 Validation</h2>

      {/* Visual validator */}
      <div className="info-box">
        🧪 <strong>Visual Validator</strong> — inspect the DOCX tracked changes
        against the Excel validation report and the generated PXML side by side.
      </div>
      <button
        className="btn-primary"
        onClick={openVisualValidator}
        style={{ marginTop: '0.75rem' }}
      >
        🧪 Open Visual Validator (New Tab)
      </button>

      {/* Recent Validations */}
      <hr style={{ margin: '1.5rem 0' }} />
      <h3>📂 Recent Validations</h3>
      {loadingSessions ? (
        <p className="text-muted">Loading sessions...</p>
      ) : recentSessions.length === 0 ? (
        <div className="info-box">No recent validation sessions. Open the Visual Validator and upload files to start.</div>
      ) : (
        <div className="recent-sessions-list">
          {recentSessions.map((s) => (
            <div key={s.session_id} className="recent-session-row">
              <div className="recent-session-info">
                <span className="recent-session-name">{s.session_name}</span>
                <span className="recent-session-meta">
                  {s.progress_text || '0/0'} · Last edited: {new Date(s.updated_at * 1000).toLocaleDateString()}{' '}
                  {new Date(s.updated_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                </span>
              </div>
              <div className="session-actions">
                <button className="btn-resume" onClick={() => resumeSession(s.session_id)}>
                  ▶ Resume
                </button>
                <button
                  className="btn-delete"
                  onClick={() => handleDelete(s.session_id)}
                  disabled={deleting === s.session_id}
                  title="Delete this validation session"
                >
                  {deleting === s.session_id ? '⏳' : '🗑️ Delete'}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Output files */}
      <hr style={{ margin: '1.5rem 0' }} />
      {!isDone ? (
        <div className="info-box">Validation report is available after processing completes.</div>
      ) : (
        <>
          <h3>📄 Key Output Files</h3>
          {outputFiles.length > 0 ? (
            <ul className="file-list">
              {outputFiles
                .filter(f => {
                  const name = f.split('/').pop()?.toLowerCase() || '';
                  return name.endsWith('.pxml') ||
                         name.endsWith('.xml') ||
                         name.includes('validation_report') ||
                         name.includes('final_validation') ||
                         name.includes('failed_revision');
                })
                .map((f, i) => (
                  <li key={i}>{f.split('/').pop()}</li>
                ))}
            </ul>
          ) : (
            <p className="text-muted">No output files listed.</p>
          )}
        </>
      )}
    </div>
  );
}
