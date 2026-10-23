import React, { useState, useRef, useCallback, useEffect } from 'react';
import { useAuth } from '../context/AuthContext';
import { useRegion } from '../region';
import {
  planBatch, planBatchFromS3, startBatch, getBatchStatus, cancelBatch,
  retryBatchPush, retryBatchChapter, createSessionFromJob, revalidateJob,
  type BatchGroup, type BatchStatusResponse,
} from '../services/api';
import ErrorGuidance from './ErrorGuidance';
import { withBase } from '../services/basePath';
import '../components/Tabs.css';
import './BatchTab.css';

const POLL_MS = 3000;

// A started batch can run for a long time (many chapters, each LLM-bound) —
// losing track of it on an accidental refresh or a closed tab would mean no
// way back to its progress or download links short of asking someone to look
// at server logs. The batch itself already lives in the server's S3 bucket
// regardless of this tab's lifetime; this key is only how the BROWSER remembers
// which one to reconnect to. Not persisted until Start is pressed — an
// unsubmitted review table is cheap to redo and not worth restoring.
const BATCH_STORAGE_KEY = 'usforms_active_batch_id';

type InputMode = 'files' | 'zip' | 's3';
type Role = 'docx' | 'xml' | 'zip';
const ROLE_EXT: Record<Role, string[]> = {
  docx: ['.docx', '.doc'],
  xml: ['.xml', '.pxml'],
  zip: ['.zip'],
};

function extnameOf(name: string): string {
  const idx = name.lastIndexOf('.');
  return idx <= 0 ? '' : name.slice(idx).toLowerCase();
}

export default function BatchTab() {
  const { user } = useAuth();
  const region = useRegion();
  const [productType, setProductType] = useState('looseleaf');
  useEffect(() => {
    if (region.products.length && !region.products.some((p) => p.id === productType)) setProductType(region.products[0].id);
  }, [region, productType]);
  const [processingMode, setProcessingMode] = useState('chapter');

  // Three explicit, separately-labeled routes — matches the sibling Topical
  // app's layout intentionally: this team already knows that pattern, and an
  // unlabeled single dropzone that silently handles both "loose files" and
  // "one zip" left the S3 folder option easy to miss below it.
  const [inputMode, setInputMode] = useState<InputMode>('files');
  const [s3Folder, setS3Folder] = useState('');
  const [planning, setPlanning] = useState(false);
  const [planError, setPlanError] = useState('');
  const [publicationName, setPublicationName] = useState('');

  const [batchId, setBatchId] = useState<string | null>(null);
  const [groups, setGroups] = useState<BatchGroup[]>([]);
  const [allFiles, setAllFiles] = useState<string[]>([]);
  const [reviewCollapsed, setReviewCollapsed] = useState(false);
  const [status, setStatus] = useState<BatchStatusResponse | null>(null);
  const [starting, setStarting] = useState(false);

  const [restoring, setRestoring] = useState(true);
  const [restoreNotice, setRestoreNotice] = useState('');

  const fileInputRef = useRef<HTMLInputElement>(null);
  const pollRef = useRef<number | null>(null);

  const resetPlan = () => {
    setBatchId(null);
    setGroups([]);
    setAllFiles([]);
    setStatus(null);
    setPlanError('');
    setRestoreNotice('');
    try { localStorage.removeItem(BATCH_STORAGE_KEY); } catch { /* storage unavailable — nothing to clear */ }
  };

  // Reconnect to a batch from before this page load, if the browser
  // remembers one and the server still knows about it. Covers both a batch
  // still being reviewed (never Started -- restores the review table) and
  // one already running/finished (restores the status table). This works
  // even for an "upload" source because the actual file bytes were staged to
  // the S3 bucket server-side back at plan time, unlike a purely browser-held
  // file pool that a refresh would make unresumable.
  useEffect(() => {
    let stored: string | null = null;
    try { stored = localStorage.getItem(BATCH_STORAGE_KEY); } catch { /* storage unavailable */ }
    if (!stored) { setRestoring(false); return; }
    getBatchStatus(stored)
      .then((res) => {
        setBatchId(stored);
        if (res.status === 'planned') {
          setGroups(res.groups || []);
          setAllFiles(res.all_files || []);
        } else {
          setStatus({ ...res, items: res.items || [] });
        }
      })
      .catch(() => {
        try { localStorage.removeItem(BATCH_STORAGE_KEY); } catch { /* nothing to clear */ }
        setRestoreNotice("Couldn't reconnect to your previous batch — it may have been cleared or the server restarted. Start a new one below.");
      })
      .finally(() => setRestoring(false));
  }, []);

  const applyPlan = (id: string, plan: BatchGroup[], files: string[]) => {
    setBatchId(id);
    setGroups(plan);
    setAllFiles(files);
    setStatus(null);
    // Remembered from plan time onward (not just after Start) — the staged
    // files already live in S3 server-side, so even an unstarted review is
    // safe to reconnect to after a refresh.
    try { localStorage.setItem(BATCH_STORAGE_KEY, id); } catch { /* storage unavailable — refresh just won't reconnect */ }
  };

  const handleFilesChosen = useCallback(async (fileList: FileList | null) => {
    if (!fileList || fileList.length === 0) return;
    resetPlan();
    setPlanning(true);
    try {
      const res = await planBatch(Array.from(fileList));
      applyPlan(res.batch_id, res.groups, res.all_files);
    } catch (err: any) {
      setPlanError(err?.response?.data?.detail || err?.message || 'Could not plan the batch.');
    } finally {
      setPlanning(false);
    }
  }, []);

  const handleS3Fetch = async () => {
    if (!s3Folder.trim()) return;
    resetPlan();
    setPlanning(true);
    try {
      const res = await planBatchFromS3(s3Folder.trim());
      applyPlan(res.batch_id, res.groups, res.all_files);
    } catch (err: any) {
      setPlanError(err?.response?.data?.detail || err?.message || 'Could not read that S3 folder.');
    } finally {
      setPlanning(false);
    }
  };

  const updateGroupField = (idx: number, field: keyof BatchGroup, value: unknown) => {
    setGroups((prev) => prev.map((g, i) => (i === idx ? { ...g, [field]: value } : g)));
  };

  const roleOptions = (role: Role) => allFiles.filter((f) => ROLE_EXT[role].includes(extnameOf(f)));

  const includedCount = groups.filter((g) => !g.excluded).length;
  const readyCount = groups.filter((g) => !g.excluded && g.missing.length === 0).length;
  const allIncludedReady = includedCount > 0 && readyCount === includedCount;
  const canStart = allIncludedReady && publicationName.trim().length > 0 && !starting;

  const handleStart = async () => {
    if (!batchId) return;
    setStarting(true);
    setPlanError('');
    try {
      const res = await startBatch(
        batchId, groups, productType, user?.email || '', user?.emp_id || '',
        processingMode, publicationName.trim()
      );
      setStatus(res);
    } catch (err: any) {
      setPlanError(err?.response?.data?.detail || err?.message || 'Could not start the batch.');
    } finally {
      setStarting(false);
    }
  };

  const handleCancel = async () => {
    if (!batchId) return;
    try {
      await cancelBatch(batchId);
    } catch { /* polling will reflect whatever the server ends up reporting */ }
  };

  // Open the Visual Validator on one finished chapter of this batch — same
  // auto-built session the Recent Jobs list's "Validate" button creates.
  const [validatingJob, setValidatingJob] = useState<string | null>(null);
  const handleValidateChapter = async (jobId: string) => {
    setValidatingJob(jobId);
    try {
      const meta = await createSessionFromJob(jobId, user?.emp_id || '');
      window.open(withBase(`/docx-viewer.html?session=${meta?.session_id || jobId}`), '_blank', 'noopener,noreferrer');
    } catch (err: any) {
      setPlanError(err?.response?.data?.detail || err?.message || 'Could not prepare this chapter for validation.');
    } finally {
      setValidatingJob(null);
    }
  };

  const handleRetryChapter = async (taskName: string) => {
    if (!batchId) return;
    setPlanError('');
    try {
      await retryBatchChapter(batchId, taskName);
      // Flip the row to running immediately so the click visibly registers;
      // the poll below takes over from here.
      setStatus((prev) => prev && ({
        ...prev,
        status: 'running',
        items: prev.items.map((it) =>
          it.task_name === taskName
            ? { ...it, status: 'running', error: '', progress_pct: 0, current_step_name: 'Starting…' }
            : it),
      }));
    } catch (err: any) {
      setPlanError(err?.response?.data?.detail || err?.message || 'Could not retry that chapter.');
    }
  };

  // Re-run ONLY the validation of a finished chapter (the conversion is not repeated).
  const handleRevalidate = async (item: { job_id: string; task_name: string }) => {
    if (!batchId || !item.job_id) return;
    setPlanError('');
    try {
      await revalidateJob(item.job_id);
      setStatus((prev) => prev && ({
        ...prev,
        items: prev.items.map((it) => (it.task_name === item.task_name
          ? { ...it, revalidating: true, validation_error_info: null, validation_error: '' } : it)),
      }));
    } catch (err: any) {
      setPlanError(err?.response?.data?.detail || err?.message || 'Could not start the validation again.');
    }
  };

  const handleRetryPush = async (taskName: string) => {
    if (!batchId) return;
    try {
      const res = await retryBatchPush(batchId, taskName);
      setStatus((prev) => prev && ({
        ...prev,
        items: prev.items.map((it) => (it.task_name === taskName ? { ...it, ...res } : it)),
      }));
    } catch { /* leave the row's existing failed/pending state showing */ }
  };

  // Poll while running — mirrors the single-job status polling pattern, just
  // for the whole batch record instead of one job.
  useEffect(() => {
    const busy = status?.status === 'running' || !!status?.items.some((i) => i.revalidating);
    if (!batchId || !status || !busy) {
      if (pollRef.current) window.clearInterval(pollRef.current);
      return;
    }
    pollRef.current = window.setInterval(async () => {
      try {
        const res = await getBatchStatus(batchId);
        setStatus(res);
      } catch { /* transient network hiccup — next tick retries */ }
    }, POLL_MS);
    return () => { if (pollRef.current) window.clearInterval(pollRef.current); };
  }, [batchId, status?.status, status?.items.some((i) => i.revalidating)]);

  const isRunning = status?.status === 'running';

  if (restoring) {
    return (
      <div className="tab-section batch-tab">
        <div className="panel"><p className="text-muted">Checking for a batch already in progress…</p></div>
      </div>
    );
  }

  return (
    <div className="tab-section batch-tab">
      <h2>📦 Batch Processing</h2>
      {restoreNotice && (
        <div className="panel"><div className="error-box">⚠️ {restoreNotice}</div></div>
      )}
      {!status && (
        <div className="panel">
          <h3>1. Choose input source</h3>
          <p className="text-muted" style={{ marginBottom: '0.9rem' }}>
            Upload the chapters' files together — individually selected, or as one zip
            containing them all — or pull them from a folder in the S3 bucket. Each
            chapter needs a <strong>DOCX</strong>, an <strong>XML/PXML</strong>, and an{' '}
            <strong>images ZIP</strong> (the chapter PDF is expected inside that zip, not
            as a separate file).
          </p>

          <div className="source-selector">
            <button
              className={`source-btn ${inputMode === 'files' ? 'active' : ''}`}
              onClick={() => setInputMode('files')}
              disabled={planning}
            >
              📁 Upload files
            </button>
            <button
              className={`source-btn ${inputMode === 'zip' ? 'active' : ''}`}
              onClick={() => setInputMode('zip')}
              disabled={planning}
            >
              🗜️ Upload zip
            </button>
            <button
              className={`source-btn ${inputMode === 's3' ? 'active' : ''}`}
              onClick={() => setInputMode('s3')}
              disabled={planning}
            >
              🪣 S3 folder
            </button>
          </div>

          {inputMode !== 's3' ? (
            <div
              className={`dropzone ${planning ? 'disabled' : ''}`}
              onClick={() => !planning && fileInputRef.current?.click()}
              onDragOver={(e) => { e.preventDefault(); if (!planning) e.currentTarget.classList.add('drag-over'); }}
              onDragLeave={(e) => e.currentTarget.classList.remove('drag-over')}
              onDrop={(e) => {
                e.preventDefault();
                e.currentTarget.classList.remove('drag-over');
                if (!planning) handleFilesChosen(e.dataTransfer.files);
              }}
            >
              <input
                ref={fileInputRef}
                type="file"
                multiple={inputMode === 'files'}
                accept={inputMode === 'zip' ? '.zip' : '.docx,.doc,.pxml,.xml,.zip'}
                onChange={(e) => handleFilesChosen(e.target.files)}
                disabled={planning}
                style={{ display: 'none' }}
              />
              {inputMode === 'zip' ? (
                <p>Drop one zip holding every chapter's DOCX, XML/PXML & images ZIP</p>
              ) : (
                <p>Drop DOCX, XML/PXML & ZIP files for every chapter at once</p>
              )}
              <small>{planning ? 'Reading files…' : 'Click to browse files'}</small>
            </div>
          ) : (
            <div className="sp-link-row">
              <input
                type="text"
                className="sp-link-input"
                placeholder="Folder in the S3 bucket, e.g. incoming/Publication_6121007 (or an s3:// link)…"
                value={s3Folder}
                onChange={(e) => setS3Folder(e.target.value)}
                disabled={planning}
                autoFocus
              />
              <button className="btn-secondary" onClick={handleS3Fetch} disabled={planning || !s3Folder.trim()}>
                {planning ? '⏳ Fetching…' : 'Fetch'}
              </button>
            </div>
          )}

          <p className="text-muted" style={{ marginTop: '0.9rem' }}>
            💡 Tip: filenames don't need to match exactly — pairing tolerates typos and
            different naming styles — but giving each chapter's files a shared, consistent
            identifier (e.g. the same chapter number or code in the DOCX, XML and ZIP
            filenames) gives the most reliable pairing and fewer rows to double-check below.
            Any pairing can also be corrected by hand in the table below.
          </p>

          {planError && <div className="error-box" style={{ marginTop: '0.9rem' }}>{planError}</div>}
        </div>
      )}

      {groups.length > 0 && !status && (
        <div className="panel">
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <h3 style={{ margin: 0 }}>2. Review &amp; confirm file sets</h3>
            <button
              type="button"
              className="btn-secondary btn-sm"
              onClick={() => setReviewCollapsed((v) => !v)}
            >
              {reviewCollapsed ? '▸ Expand' : '▾ Collapse'}
            </button>
          </div>
          <p className="text-muted" style={{ margin: '0.5rem 0 0' }}>
            Found <strong>{groups.length}</strong> file set{groups.length === 1 ? '' : 's'} —{' '}
            <strong>{readyCount}</strong> ready
            {includedCount - readyCount > 0 && (
              <>, <strong>{includedCount - readyCount}</strong> incomplete</>
            )}
            {includedCount - readyCount === 0 && ', none incomplete'}
            {groups.some((g) => g.excluded) && `, ${groups.filter((g) => g.excluded).length} excluded`}.
          </p>

          {!reviewCollapsed && (
            <div className="table-wrapper" style={{ marginTop: '0.9rem' }}>
              <table className="data-table batch-review-table">
                <thead>
                  <tr>
                    <th>Include</th>
                    <th>Task name</th>
                    <th>DOCX</th>
                    <th>XML/PXML</th>
                    <th>ZIP</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {groups.map((g, idx) => (
                    <tr key={idx}>
                      <td>
                        <input
                          type="checkbox"
                          checked={!g.excluded}
                          onChange={() => updateGroupField(idx, 'excluded', !g.excluded)}
                          disabled={starting}
                          title="Include in this batch"
                        />
                      </td>
                      <td>
                        <input
                          type="text"
                          className="task-name-input"
                          value={g.task_name}
                          onChange={(e) => updateGroupField(idx, 'task_name', e.target.value)}
                          disabled={starting}
                        />
                      </td>
                      {(['docx', 'xml', 'zip'] as Role[]).map((role) => (
                        <td key={role}>
                          <select
                            title={`${role} file`}
                            value={g[role] || ''}
                            onChange={(e) => updateGroupField(idx, role, e.target.value || null)}
                            disabled={starting}
                          >
                            <option value="">— none —</option>
                            {roleOptions(role).map((name) => (
                              <option key={name} value={name}>{name}</option>
                            ))}
                          </select>
                        </td>
                      ))}
                      <td>
                        {g.excluded ? (
                          <span className="text-muted">excluded</span>
                        ) : g.missing.length > 0 ? (
                          <span title={`Missing: ${g.missing.join(', ')}`}>⚠️ missing {g.missing.join(', ')}</span>
                        ) : g.low_confidence ? (
                          <span title="Pairing wasn't a strong match — double-check these files">
                            ⚠️ low match ({Math.round(g.confidence * 100)}%)
                          </span>
                        ) : (
                          <span title="Complete">✅</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="config-section" style={{ marginTop: '1rem' }}>
            <label className="config-label">Product</label>
            <select
              value={productType}
              onChange={(e) => setProductType(e.target.value)}
              disabled={starting}
              className="config-select"
            >
              {region.products.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
            </select>
          </div>

          <div className="config-section" style={{ marginTop: '0.75rem' }}>
            <label className="config-label">Document Type</label>
            <select
              value={processingMode}
              onChange={(e) => setProcessingMode(e.target.value)}
              disabled={starting}
              className="config-select"
            >
              <option value="chapter">Chapter (single chapter)</option>
              <option value="volume">Volume (multi-chapter)</option>
            </select>
          </div>

          <div className="config-section" style={{ marginTop: '0.75rem' }}>
            <label className="config-label">Publication name</label>
            <input
              type="text"
              className="config-select"
              placeholder="e.g. FLPRFMS"
              value={publicationName}
              onChange={(e) => setPublicationName(e.target.value)}
              disabled={starting}
            />
            <small className="text-muted">
              Names the S3 output folder for this batch: "&lt;Publication&gt;_Output_&lt;timestamp&gt;",
              with one subfolder per chapter inside it — in addition to the download link once a
              chapter finishes.
            </small>
          </div>

          {planError && <div className="error-box" style={{ marginTop: '0.9rem' }}>{planError}</div>}

          <button
            className="btn-primary btn-block"
            onClick={handleStart}
            disabled={!canStart}
            style={{ marginTop: '1rem' }}
          >
            {starting ? '⏳ Starting…' : `▶️ Start Batch (${readyCount} chapter${readyCount === 1 ? '' : 's'})`}
          </button>
          {!starting && allIncludedReady && !publicationName.trim() && (
            <small className="text-muted">Enter a publication name to enable Start Batch.</small>
          )}
          {includedCount > readyCount && (
            <p className="text-muted" style={{ color: '#c62828', marginTop: '0.5rem' }}>
              {includedCount - readyCount} included chapter(s) are missing a file — fix the row above or exclude it before starting.
            </p>
          )}
        </div>
      )}

      {status && (
        <div className="panel">
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <h3 style={{ margin: 0 }}>3. Batch status</h3>
            <span className="text-muted">
              {isRunning ? 'Running… ' : 'Finished — '}
              {status.completed} / {status.total} completed
            </span>
          </div>
          {status.s3_output_folder && (
            <p className="text-muted" style={{ marginTop: '0.5rem' }}>
              📤 Publishing to the S3 bucket under <strong>{status.s3_output_folder}</strong>
            </p>
          )}

          {/* After the batch: say plainly what needs attention and what to do about it. */}
          {!isRunning && (() => {
            const failed = status.items.filter((i) => i.status === 'failed');
            if (failed.length === 0) return null;
            const rerun = failed.filter((i) => i.error_info?.retryable !== false).length;
            const manual = failed.length - rerun;
            return (
              <div className="eg" role="status" style={{ marginTop: '0.75rem' }}>
                <div className="eg-head">⚠️ {failed.length} of {status.total} chapter{failed.length === 1 ? '' : 's'} did not finish</div>
                <div className="eg-hint">
                  {rerun > 0 && <>{rerun} can be run again: use <strong>Run again</strong> on the chapter. </>}
                  {manual > 0 && <>{manual} need{manual === 1 ? 's' : ''} manual work or a fix to the input files; see the note on each chapter. </>}
                  Chapters that finished are kept and can be downloaded or validated as normal.
                </div>
              </div>
            );
          })()}

          {/* Whole-batch progress. Chapters run one at a time, so this counts
              finished chapters plus the running one's own share — otherwise a
              long single chapter looks completely stalled at 0%. */}
          <div className="batch-overall-progress">
            <div className="progress-bar">
              <div
                className="progress-fill"
                style={{
                  width: `${status.total
                    ? Math.round(
                        ((status.completed +
                          (status.items.find((i) => i.status === 'running')?.progress_pct ?? 0) / 100) /
                          status.total) * 100
                      )
                    : 0}%`,
                }}
              />
            </div>
          </div>

          <div className="table-wrapper" style={{ marginTop: '0.9rem' }}>
            <table className="data-table batch-status-table">
              <thead>
                <tr>
                  <th>Task</th>
                  <th>Status</th>
                  <th>Download</th>
                  <th>Validate</th>
                  <th>S3 output</th>
                </tr>
              </thead>
              <tbody>
                {status.items.map((item, idx) => (
                  <tr key={idx}>
                    <td>{item.task_name}</td>
                    <td>
                      <span
                        className={`outcome-badge ${
                          item.status === 'completed' ? 'success' :
                          item.status === 'failed' ? 'error' :
                          item.status === 'running' ? 'stopped' : ''
                        }`}
                      >
                        {item.status}
                      </span>
                      {item.status === 'running' && (
                        <div className="batch-progress">
                          <div className="progress-bar">
                            <div className="progress-fill" style={{ width: `${item.progress_pct ?? 0}%` }} />
                          </div>
                          <div className="batch-progress-label">
                            {item.current_step && item.total_steps
                              ? `Step ${item.current_step}/${item.total_steps} · `
                              : ''}
                            {item.current_step_name || 'Starting…'}
                            <span className="batch-progress-pct">{item.progress_pct ?? 0}%</span>
                          </div>
                        </div>
                      )}
                      {item.status === 'failed' && (
                        <ErrorGuidance
                          compact
                          info={item.error_info}
                          rawError={item.error}
                          logUrl={item.job_id ? withBase(`/api/log/${item.job_id}`) : undefined}
                          onRetry={() => handleRetryChapter(item.task_name)}
                          retryDisabled={isRunning}
                          retryTitle={isRunning
                            ? 'Wait for the batch to finish, then run this chapter again'
                            : 'Re-run just this chapter (costs a fresh pipeline run)'}
                        />
                      )}
                      {item.status === 'skipped' && (
                        <button
                          className="btn-secondary btn-sm batch-retry-btn"
                          onClick={() => handleRetryChapter(item.task_name)}
                          disabled={isRunning}
                          title="This chapter was skipped when the batch was stopped; run it now"
                        >
                          🔁 Run now
                        </button>
                      )}
                      {item.status === 'completed' && item.validation_error_info && (
                        <ErrorGuidance
                          compact
                          warning
                          info={{ ...item.validation_error_info, title: `Finished, but validation did not run — ${item.validation_error_info.title}` }}
                          logUrl={item.job_id ? withBase(`/api/log/${item.job_id}`) : undefined}
                          extraAction={{
                            label: '🔍 Run validation only',
                            onClick: () => handleRevalidate(item),
                            disabled: isRunning || !!item.revalidating,
                            title: 'Re-run just the validation. The conversion is not repeated, so this costs far less than running the chapter again.',
                          }}
                        />
                      )}
                      {item.status === 'completed' && item.revalidating && (
                        <div className="batch-progress-label">⏳ Re-running validation…</div>
                      )}
                    </td>
                    <td>
                      {item.status === 'completed' && item.job_id ? (
                        <a className="btn-download" href={withBase(`/api/download/${item.job_id}`)} target="_blank" rel="noreferrer">
                          📥 Download
                        </a>
                      ) : (
                        <span className="text-muted">—</span>
                      )}
                    </td>
                    <td>
                      {item.status === 'completed' && item.job_id ? (
                        <button
                          className="btn-secondary btn-sm"
                          onClick={() => handleValidateChapter(item.job_id!)}
                          disabled={validatingJob === item.job_id}
                          title="Open the Visual Validator on this chapter"
                        >
                          {validatingJob === item.job_id ? '⏳' : '🔍'} Validate
                        </button>
                      ) : (
                        <span className="text-muted">—</span>
                      )}
                    </td>
                    <td>
                      {item.status === 'completed' && status.s3_output_folder ? (
                        item.s3_location && !item.s3_error ? (
                          <span className="text-muted" title={item.s3_location}>✅ published</span>
                        ) : (
                          <>
                            {item.s3_error ? (
                              <span className="text-muted" title={item.s3_error}>⚠️ publish failed</span>
                            ) : (
                              <span className="text-muted">publishing…</span>
                            )}
                            <button
                              className="btn-secondary btn-sm"
                              style={{ marginLeft: 6 }}
                              onClick={() => handleRetryPush(item.task_name)}
                              title="Retry publishing this chapter's output to the S3 output folder — safe to click even if it's stuck"
                            >
                              🔁
                            </button>
                          </>
                        )
                      ) : (
                        <span className="text-muted">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {status.log && status.log.length > 0 && (
            <div className="batch-log">
              <h4>Batch log</h4>
              <pre className="log-output-full">
                {status.log
                  .map((e) => `[${e.time}] ${e.level === 'warning' ? '⚠️ ' : ''}${e.message}`)
                  .join('\n')}
              </pre>
            </div>
          )}

          <div style={{ marginTop: '1rem', display: 'flex', gap: '0.6rem' }}>
            {isRunning ? (
              <button className="btn-secondary" onClick={handleCancel}>■ Stop after current chapter</button>
            ) : (
              <button className="btn-secondary" onClick={resetPlan}>Start a new batch</button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
