import React, { useEffect, useRef } from 'react';
import { useJob } from '../context/JobContext';
import { JobStatus, getJobLog, cancelJob, rerunJob, revalidateJob } from '../services/api';
import { withBase } from '../services/basePath';
import RecentJobs from './RecentJobs';
import ErrorGuidance from './ErrorGuidance';
import { useRegion } from '../region';
import './ProcessingTab.css';

const STEPS = [
  'Track Changes Extraction',
  'Section Deletion',
  'Section Number Replacement',
  'Section Insertion & Edge Cases',
  'Paragraph-Level Inline Changes',
  'Full Paragraph Deletions',
  'Newline Paragraph Insertions',
  'New Footnote Insertions',
  'Failed Revisions Report',
  'Retry Failed Deletions',
  'Post Processing',
  'Validation Report',
];

interface Props {
  jobId: string | null;
  status: JobStatus | null;
  files: { docx: File | null; pxml: File | null; zip: File | null };
  productType: string;
  uploading: boolean;
  onStart: () => void;
  onReset: () => void;
}

/** A bare 404 "Not Found" means the running backend predates this feature, not that the job is missing. */
function staleServer(err: any): string {
  return err?.response?.status === 404 && err?.response?.data?.detail === 'Not Found'
    ? 'The server is running an older version without this feature. Restart the backend, then try again.'
    : '';
}

export default function ProcessingTab({ jobId, status, files, productType, uploading, onStart, onReset }: Props) {
  const region = useRegion();
  const { pollStatus, setJobId } = useJob();
  const [actionError, setActionError] = React.useState('');
  const [logText, setLogText] = React.useState('');
  const [showLog, setShowLog] = React.useState(false);
  const [stepsExpanded, setStepsExpanded] = React.useState(false);
  const [stopping, setStopping] = React.useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const handleStop = async () => {
    if (!jobId) return;
    if (!confirm('Stop this job? It stops before the next AI call, usually within seconds.')) return;
    setStopping(true);
    try {
      await cancelJob(jobId);
      pollStatus();
    } catch (err: any) {
      alert(err?.response?.data?.detail || 'Failed to stop the job.');
      setStopping(false);
    }
  };

  // Run the failed job again from the inputs saved on the server; fall back to the files still selected here.
  const handleRerun = async () => {
    if (!jobId) return;
    setActionError('');
    try {
      const res = await rerunJob(jobId);
      setJobId(res.job_id);
    } catch (err: any) {
      if (files.docx && files.pxml) {
        onReset();
        onStart();
      } else {
        setActionError(staleServer(err) || err?.response?.data?.detail || 'The saved files are no longer on the server: select them again in the sidebar.');
      }
    }
  };

  // Re-run only the validation of a finished conversion (cheaper than a full re-run).
  const handleRevalidate = async () => {
    if (!jobId) return;
    setActionError('');
    try {
      await revalidateJob(jobId);
      pollStatus();
    } catch (err: any) {
      setActionError(staleServer(err) || err?.response?.data?.detail || 'Could not start the validation again.');
    }
  };

  // Poll status while running
  useEffect(() => {
    if (jobId && (!status || status.status === 'running')) {
      pollRef.current = setInterval(() => {
        pollStatus();
      }, 3000);
    }
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [jobId, status?.status, pollStatus]);

  // Initial poll
  useEffect(() => {
    if (jobId) pollStatus();
  }, [jobId, pollStatus]);

  // Fetch log
  useEffect(() => {
    if (!jobId) return;
    const fetchLog = async () => {
      try {
        const log = await getJobLog(jobId);
        setLogText(log);
      } catch { /* ignore */ }
    };
    fetchLog();
    const logInterval = setInterval(fetchLog, 5000);
    return () => clearInterval(logInterval);
  }, [jobId]);

  const isDone = status?.status === 'completed';
  const isFailed = status?.status === 'failed';
  const isRunning = status?.status === 'running';
  const isStopped = status?.status === 'stopped';
  const currentStep = isRunning ? (status?.current_step || 0) : (isDone ? STEPS.length : 0);
  const progressPct = isDone ? 100 : (status?.progress_pct || 0);

  return (
    <div className="processing-tab">
      {/* Status Banner */}
      {!jobId && (
        <div className="status-banner ready">
          🟢 <strong>Ready</strong> — upload files in the sidebar and click Start.
        </div>
      )}
      {isRunning && (
        <div className="status-banner running">
          ⏳ <strong>Processing</strong> — Step {status?.current_step || 0}/{status?.total_steps || 11}: {status?.current_step_name || ''}
        </div>
      )}
      {isDone && (
        <div className="status-banner completed">
          ✅ <strong>Completed</strong> — See the Downloads and Validation tabs.
          {status?.validation_error && (
            <ErrorGuidance
              warning
              info={status.validation_error_info
                ? { ...status.validation_error_info, title: `Validation report could not be generated — ${status.validation_error_info.title}` }
                : undefined}
              rawError={`Validation report could not be generated: ${status.validation_error}`}
              logUrl={jobId ? withBase(`/api/log/${jobId}`) : undefined}
              extraAction={jobId ? {
                label: '🔍 Run validation only',
                onClick: handleRevalidate,
                title: 'Re-run just the validation. The conversion is finished and is not repeated, so this costs far less.',
              } : undefined}
            />
          )}
          {actionError && <div className="hint" style={{ marginTop: 6 }}>{actionError}</div>}
        </div>
      )}
      {isFailed && (
        <div className="status-banner failed">
          ❌ <strong>Failed</strong>
          <ErrorGuidance
            info={status?.error_info}
            rawError={status?.error || 'Unknown error'}
            logUrl={jobId ? withBase(`/api/log/${jobId}`) : undefined}
            onRetry={handleRerun}
            retryDisabled={uploading}
            retryTitle="Start a new run using the input files saved on the server (no re-upload)"
          />
          {actionError && <div className="hint" style={{ marginTop: 6 }}>{actionError}</div>}
        </div>
      )}
      {isStopped && (
        <div className="status-banner failed">
          ⏹ <strong>Stopped</strong> — job was cancelled. Partial output (if any) is in the job folder.
        </div>
      )}

      {/* Control + Metrics + Config Row */}
      <div className="processing-grid">
        {/* Control Panel */}
        <div className="panel">
          <h3>🎮 Control Panel</h3>
          {!jobId ? (
            <button
              className="btn-primary btn-block"
              onClick={onStart}
              disabled={!(files.docx && files.pxml) || uploading}
            >
              {uploading ? '⏳ Uploading...' : '▶️ Start Processing'}
            </button>
          ) : (
            <>
              <p className="job-id-text">Job ID: <code>{jobId}</code></p>
              {isRunning && (
                <button
                  className="btn-danger btn-block"
                  onClick={handleStop}
                  disabled={stopping}
                  style={{ marginBottom: '0.5rem' }}
                >
                  {stopping ? '⏳ Stopping…' : '⏹ Stop Processing'}
                </button>
              )}
              <button
                className="btn-secondary btn-block"
                onClick={onReset}
                disabled={isRunning}
              >
                🔄 Process Another Document
              </button>
            </>
          )}
        </div>

        {/* Live Metrics */}
        <div className="panel">
          <h3>📈 Live Processing Metrics</h3>
          <div className="metrics-grid">
            <div className="metric-card">
              <span className="metric-label">📄 Documents</span>
              <span className="metric-value">{isDone ? '1 / 1' : jobId ? '0 / 1' : '0 / 0'}</span>
            </div>
            <div className="metric-card">
              <span className="metric-label">🪜 Step</span>
              <span className="metric-value">{(status || isDone) ? `${currentStep}/${STEPS.length}` : '—'}</span>
            </div>
            <div className="metric-card">
              <span className="metric-label">📊 Progress</span>
              <span className="metric-value">{progressPct}%</span>
            </div>
            <div className="metric-card">
              <span className="metric-label">📞 LLM Calls</span>
              <span className="metric-value">{status?.llm_calls ?? 0}</span>
            </div>
          </div>
        </div>

        {/* Configuration */}
        <div className="panel">
          <h3>📋 Configuration</h3>
          <p><strong>Region:</strong> {region.name}</p>
          <p><strong>Product:</strong> {productType}</p>
        </div>
      </div>

      {/* Pipeline Steps */}
      <div className="panel steps-panel">
        <div className="steps-header" style={{cursor:'pointer'}} onClick={() => setStepsExpanded(!stepsExpanded)}>
          <h3>📋 Pipeline Steps {stepsExpanded ? '▾' : '▸'}</h3>
          <span className="steps-counter">Step {Math.min(currentStep, STEPS.length)} / {STEPS.length}</span>
        </div>
        <div className="progress-bar">
          <div className="progress-fill" style={{ width: `${progressPct}%` }} />
        </div>
        {stepsExpanded && <div className="steps-grid">
          {STEPS.map((step, idx) => {
            const i = idx + 1;
            let cls = 'step-pending';
            let icon = '○';
            if (i < currentStep || isDone) { cls = 'step-done'; icon = '✓'; }
            else if (i === currentStep && isRunning) { cls = 'step-active'; icon = '▸'; }
            return (
              <div key={i} className={`step-item ${cls}`}>
                {icon} {i}. {step}
              </div>
            );
          })}
        </div>}
      </div>

      {/* Live Log */}
      {jobId && (
        <div className="panel">
          <button className="btn-secondary" onClick={() => setShowLog(!showLog)}>
            {showLog ? '▾' : '▸'} 📜 Live Log Output
          </button>
          {showLog && (
            <pre className="log-output">{logText.slice(-5000) || '(empty)'}</pre>
          )}
        </div>
      )}

      {/* Recent Jobs (last 24h) */}
      <RecentJobs />
    </div>
  );
}
