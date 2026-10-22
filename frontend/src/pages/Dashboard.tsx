import React, { useState, useRef, useCallback, useEffect } from 'react';
import { useAuth } from '../context/AuthContext';
import { useJob } from '../context/JobContext';
import { startProcessing, healthCheck } from '../services/api';
import ProcessingTab from '../components/ProcessingTab';
import BatchTab from '../components/BatchTab';
import DownloadsTab from '../components/DownloadsTab';
import ValidationTab from '../components/ValidationTab';
import DtdValidatorTab from '../components/DtdValidatorTab';
import SystemLogsTab from '../components/SystemLogsTab';
import AccountTab from '../components/AccountTab';
import ErrorBoundary from '../components/ErrorBoundary';
import { useRegionTitle } from '../region';
import './Dashboard.css';

type TabKey = 'processing' | 'batch' | 'downloads' | 'validation' | 'dtd' | 'logs' | 'account';

interface UploadedFiles {
  docx: File | null;
  pxml: File | null;
  zip: File | null;
}

export default function Dashboard() {
  const { user, logout } = useAuth();
  const { jobId, status, setJobId, resetJob } = useJob();
  const [activeTab, setActiveTab] = useState<TabKey>('processing');
  const region = useRegionTitle();
  const [productType, setProductType] = useState('looseleaf');
  // Use the region's own product list; if the default isn't one of them, pick its first.
  useEffect(() => {
    if (region.products.length && !region.products.some((p) => p.id === productType)) setProductType(region.products[0].id);
  }, [region, productType]);
  const [processingMode, setProcessingMode] = useState('chapter');
  const [files, setFiles] = useState<UploadedFiles>({ docx: null, pxml: null, zip: null });
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState('');
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Live backend capacity (free slots / total workers / queued jobs) — lets
  // anyone see whether the pool is actually free before starting several
  // jobs at once across tabs, instead of guessing.
  const [health, setHealth] = useState<
    { available_slots?: number; max_workers?: number; queued?: number } | null
  >(null);

  useEffect(() => {
    let alive = true;
    const fetchHealth = () =>
      healthCheck()
        .then((h) => { if (alive) setHealth(h); })
        .catch(() => { /* health is best-effort */ });
    fetchHealth();
    const id = setInterval(fetchHealth, 10000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  const handleFilesSelected = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const selected = e.target.files;
    if (!selected) return;
    const newFiles: UploadedFiles = { docx: null, pxml: null, zip: null };
    for (let i = 0; i < selected.length; i++) {
      const f = selected[i];
      const ext = f.name.split('.').pop()?.toLowerCase() || '';
      if (ext === 'docx' || ext === 'doc') newFiles.docx = f;
      else if (ext === 'pxml' || ext === 'xml') newFiles.pxml = f;
      else if (ext === 'zip') newFiles.zip = f;
    }
    setFiles(newFiles);
    setUploadError('');
  }, []);

  const handleFilesDropped = useCallback((fileList: FileList) => {
    const fakeEvent = { target: { files: fileList } } as unknown as React.ChangeEvent<HTMLInputElement>;
    handleFilesSelected(fakeEvent);
  }, [handleFilesSelected]);

  const handleStart = async () => {
    if (!files.docx || !files.pxml) {
      setUploadError('DOCX and PXML files are required.');
      return;
    }
    setUploading(true);
    setUploadError('');
    try {
      const result = await startProcessing(
        files.docx,
        files.pxml,
        productType,
        user?.email || '',
        user?.emp_id || '',
        files.zip || null,
        processingMode
      );
      setJobId(result.job_id);
    } catch (err: any) {
      setUploadError(err?.response?.data?.detail || err?.message || 'Upload failed');
    } finally {
      setUploading(false);
    }
  };

  const tabs: { key: TabKey; label: string; icon: string }[] = [
    { key: 'processing', label: 'Processing', icon: '🎯' },
    { key: 'batch', label: 'Batch', icon: '📚' },
    { key: 'downloads', label: 'Downloads', icon: '📥' },
    { key: 'validation', label: 'Validation', icon: '🔍' },
    { key: 'dtd', label: 'DTD Validation', icon: '🧬' },
    { key: 'logs', label: 'System Logs', icon: '📜' },
    { key: 'account', label: 'Account & Usage', icon: '👤' },
  ];

  return (
    <div className="dashboard">
      {/* Sidebar */}
      <aside className="sidebar">
        <div className="sidebar-top">
          <h3 className="sidebar-title">⚙️ Configuration</h3>
          <hr className="sidebar-divider" />

          <div className="upload-section">
            <h4>📤 Upload Files</h4>
            <div
              className={`dropzone ${jobId ? 'disabled' : ''}`}
              onClick={() => !jobId && fileInputRef.current?.click()}
              onDragOver={(e) => { e.preventDefault(); if (!jobId) e.currentTarget.classList.add('drag-over'); }}
              onDragLeave={(e) => e.currentTarget.classList.remove('drag-over')}
              onDrop={(e) => {
                e.preventDefault();
                e.currentTarget.classList.remove('drag-over');
                if (!jobId && e.dataTransfer.files) handleFilesDropped(e.dataTransfer.files);
              }}
            >
              <input
                ref={fileInputRef}
                type="file"
                multiple
                accept=".docx,.doc,.pxml,.xml,.zip"
                onChange={handleFilesSelected}
                disabled={!!jobId}
                style={{ display: 'none' }}
              />
              <p>Drop DOCX, PXML/XML & ZIP together</p>
              <small>Click to browse files</small>
            </div>

            {(files.docx || files.pxml || files.zip) && (
              <div className="file-status">
                <span>Detected: </span>
                <strong>{files.docx ? 1 : 0}</strong> DOCX ·{' '}
                <strong>{files.pxml ? 1 : 0}</strong> PXML ·{' '}
                <strong>{files.zip ? 1 : 0}</strong> ZIP
              </div>
            )}

            {files.docx && files.pxml && (
              <div className="file-ready">
                ✅ Ready — {files.docx.name} + {files.pxml.name}
              </div>
            )}

            {uploadError && <div className="file-error">{uploadError}</div>}
          </div>

          <hr className="sidebar-divider" />

          <div className="config-section">
            <label className="config-label">Region</label>
            <div className="config-value">{region.name}</div>
          </div>

          <div className="config-section">
            <label className="config-label">Product</label>
            <select
              value={productType}
              onChange={(e) => setProductType(e.target.value)}
              disabled={!!jobId}
              className="config-select"
            >
              {region.products.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
            </select>
          </div>

          <div className="config-section">
            <label className="config-label">Document Type</label>
            <select
              value={processingMode}
              onChange={(e) => setProcessingMode(e.target.value)}
              disabled={!!jobId}
              className="config-select"
            >
              <option value="chapter">Chapter (single chapter)</option>
              <option value="volume">Volume (multiple chapters)</option>
            </select>
          </div>

          <hr className="sidebar-divider" />

          <div className="config-section">
            <label className="config-label">⚙️ Backend capacity</label>
            {health ? (
              <div className="config-value">
                <div>
                  <strong>{health.available_slots ?? '—'}</strong> of{' '}
                  <strong>{health.max_workers ?? '—'}</strong> slots free
                </div>
                <div>
                  <strong>{health.queued ?? 0}</strong> in queue
                </div>
              </div>
            ) : (
              <div className="config-value">Checking…</div>
            )}
          </div>
        </div>

        <div className="sidebar-bottom">
          <hr className="sidebar-divider" />
          {user && (
            <p className="signed-in-as">Signed in as <strong>{user.name || user.emp_id}</strong></p>
          )}
          <button className="btn-signout" onClick={logout}>🚪 Sign out</button>
          <small className="version-text">v3.1 · {region.name} · ExecutorPool</small>
        </div>
      </aside>

      {/* Main content */}
      <main className="main-content">
        <header className="main-header">
          <h1>📄 XML Track Changes{region.name ? ` - ${region.name}` : ''}</h1>
          <p className="header-subtitle">Thomson Reuters XML Document Processing Platform{region.name ? ` · ${region.name}` : ''}</p>
        </header>

        {/* Tabs */}
        <nav className="tabs-nav">
          {tabs.map((tab) => (
            <button
              key={tab.key}
              className={`tab-btn ${activeTab === tab.key ? 'active' : ''}`}
              onClick={() => setActiveTab(tab.key)}
            >
              {tab.icon} {tab.label}
            </button>
          ))}
        </nav>

        {/* Tab content */}
        <div className="tab-content">
          <ErrorBoundary key={activeTab} label={activeTab}>
          {activeTab === 'processing' && (
            <ProcessingTab
              jobId={jobId}
              status={status}
              files={files}
              productType={productType}
              uploading={uploading}
              onStart={handleStart}
              onReset={resetJob}
            />
          )}
          {activeTab === 'batch' && <BatchTab />}
          {activeTab === 'downloads' && <DownloadsTab />}
          {activeTab === 'validation' && <ValidationTab />}
          {activeTab === 'dtd' && <DtdValidatorTab />}
          {activeTab === 'logs' && <SystemLogsTab />}
          {activeTab === 'account' && <AccountTab />}
          </ErrorBoundary>
        </div>
      </main>
    </div>
  );
}
