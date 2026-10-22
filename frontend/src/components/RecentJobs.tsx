import React, { useState } from 'react';
import { useAuth } from '../context/AuthContext';
import { getRecentJobs, downloadBySession, resumeValidationSession, deleteValidationSession, createSessionFromJob, RecentJob } from '../services/api';
import './RecentJobs.css';

const DOCX_VIEWER_URL = '/docx-viewer.html';

export default function RecentJobs() {
  const { user } = useAuth();
  const [jobs, setJobs] = useState<RecentJob[]>([]);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [downloading, setDownloading] = useState<string | null>(null);
  const [resuming, setResuming] = useState<string | null>(null);
  const [validating, setValidating] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [error, setError] = useState('');

  const handleFetch = async () => {
    if (!user?.emp_id) return;
    setLoading(true);
    setError('');
    try {
      const data = await getRecentJobs(user.emp_id);
      setJobs(data);
      setLoaded(true);
    } catch (err: any) {
      setError(err?.response?.data?.detail || err?.message || 'Failed to load recent jobs.');
    } finally {
      setLoading(false);
    }
  };

  const handleDownload = async (sessionId: string, fileName?: string) => {
    setDownloading(sessionId);
    try {
      const blob = await downloadBySession(sessionId);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      // Same "<session_id>_<partial original file name>.zip" convention as the
      // Processing tab's own Downloads tab — a bare session id alone gives no
      // way to tell which document a downloaded zip actually came from.
      const rawName = fileName?.replace(/\.[^.]+$/, '') || 'output';
      a.href = url;
      a.download = `${sessionId}_${rawName.slice(0, 40)}.zip`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err: any) {
      alert(err?.response?.data?.detail || 'Download failed. Output may no longer be available.');
    } finally {
      setDownloading(null);
    }
  };

  // Validate a finished job: auto-build a session from its on-disk files
  // (DOCX + final PXML + PDF + Failed_revisions.xlsx) and open the Visual Validator.
  const handleValidate = async (sessionId: string) => {
    setValidating(sessionId);
    try {
      const meta = await createSessionFromJob(sessionId, user?.emp_id || '');
      const sid = meta?.session_id || sessionId;
      window.open(`${DOCX_VIEWER_URL}?session=${sid}`, '_blank', 'noopener,noreferrer');
    } catch (err: any) {
      alert(err?.response?.data?.detail || 'Could not prepare validation. Job output may no longer be on disk.');
    } finally {
      setValidating(null);
    }
  };

  const handleResumeValidation = async (sessionId: string) => {
    setResuming(sessionId);
    try {
      // Resume the session — its files live in S3, so the backend just confirms they are there
      await resumeValidationSession(sessionId, user?.emp_id || '');
      // Open visual validator with the session
      window.open(`${DOCX_VIEWER_URL}?session=${sessionId}`, '_blank', 'noopener,noreferrer');
    } catch (err: any) {
      alert(err?.response?.data?.detail || 'Resume failed. Session files may no longer be available.');
    } finally {
      setResuming(null);
    }
  };

  const handleDelete = async (sessionId: string) => {
    if (!confirm('Delete this validation session? Its files will be removed from storage.')) return;
    setDeleting(sessionId);
    try {
      await deleteValidationSession(sessionId, user?.emp_id || '');
      // Remove from local list
      setJobs((prev) => prev.filter((j) => j.session_id !== sessionId));
    } catch (err: any) {
      alert(err?.response?.data?.detail || 'Delete failed.');
    } finally {
      setDeleting(null);
    }
  };

  return (
    <div className="recent-jobs">
      <div className="recent-jobs-header">
        <h3>📋 Recent Processings (Last 24 Hours)</h3>
        <button className="btn-secondary" onClick={handleFetch} disabled={loading}>
          {loading ? '⏳ Loading...' : loaded ? '🔄 Refresh' : '📂 View My Recent Jobs'}
        </button>
      </div>

      {error && <div className="rj-error">{error}</div>}

      {loaded && jobs.length === 0 && (
        <div className="rj-empty">No documents processed in the last 24 hours.</div>
      )}

      {loaded && jobs.length > 0 && (
        <div className="rj-table-wrapper">
          <table className="rj-table">
            <thead>
              <tr>
                <th>Date</th>
                <th>Time</th>
                <th>File</th>
                <th>Region</th>
                <th>Product</th>
                <th>Duration</th>
                <th>Status</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((job, idx) => (
                <tr key={idx}>
                  <td>{job.date}</td>
                  <td>{job.time}</td>
                  <td
                    title={job.file_name || ''}
                    style={{ maxWidth: '220px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                  >
                    {job.file_name || '—'}
                  </td>
                  <td>{job.region || '—'}</td>
                  <td>{job.product || '—'}</td>
                  <td>{job.processing_time_sec ? `${Math.round(job.processing_time_sec)}s` : '—'}</td>
                  <td>
                    {job.run_outcome === 'success' ? (
                      <span className="rj-badge success">✅ Success</span>
                    ) : (
                      <span className={`rj-badge ${job.run_outcome}`}>
                        {job.run_outcome === 'error' ? '❌' : '⚠️'} {job.run_outcome}
                      </span>
                    )}
                  </td>
                  <td>
                    <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap', alignItems: 'center' }}>
                      {job.run_outcome === 'success' && job.download_available && (
                        <button
                          className="btn-download btn-sm"
                          onClick={() => handleDownload(job.session_id, job.file_name)}
                          disabled={downloading === job.session_id}
                        >
                          {downloading === job.session_id ? '⏳' : '📥'} Download
                        </button>
                      )}
                      {job.run_outcome === 'success' && job.download_available && (
                        <button
                          className="btn-resume btn-sm"
                          onClick={() => handleValidate(job.session_id)}
                          disabled={validating === job.session_id}
                          title="Open the Visual Validator with this job's files loaded automatically"
                        >
                          {validating === job.session_id ? '⏳' : '🔍'} Validate
                        </button>
                      )}
                      {job.run_outcome === 'success' && job.validation_resumable && !job.validation_done && (
                        <button
                          className="btn-resume btn-sm"
                          onClick={() => handleResumeValidation(job.session_id)}
                          disabled={resuming === job.session_id}
                          title="Resume validation from where you left off"
                        >
                          {resuming === job.session_id ? '⏳' : '▶'} Resume
                        </button>
                      )}
                      {job.run_outcome === 'success' && !job.download_available && !job.validation_resumable && (
                        <span className="rj-unavailable">⏳ Pending...</span>
                      )}
                      {job.run_outcome !== 'success' && (
                        <span className="rj-status-text">{job.run_outcome}</span>
                      )}
                      <button
                        className="btn-delete btn-sm"
                        onClick={() => handleDelete(job.session_id)}
                        disabled={deleting === job.session_id}
                        title="Delete this validation session"
                      >
                        {deleting === job.session_id ? '⏳' : '🗑️'}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
