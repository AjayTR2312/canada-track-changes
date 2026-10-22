import React, { useState } from 'react';
import { useJob } from '../context/JobContext';
import { downloadOutput, downloadValidation } from '../services/api';
import './Tabs.css';

export default function DownloadsTab() {
  const { jobId, status } = useJob();
  const [downloading, setDownloading] = useState<string | null>(null);

  const isDone = status?.status === 'completed';

  const handleDownloadOutput = async () => {
    if (!jobId) return;
    setDownloading('output');
    try {
      const blob = await downloadOutput(jobId);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      const rawName = status?.file_name?.replace(/\.[^.]+$/, '') || 'output';
      a.href = url;
      a.download = `${jobId}_${rawName.slice(0, 40)}.zip`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      alert('Failed to download output.');
    } finally {
      setDownloading(null);
    }
  };

  const handleDownloadValidation = async () => {
    if (!jobId) return;
    setDownloading('validation');
    try {
      const blob = await downloadValidation(jobId);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      const rawName = status?.file_name?.replace(/\.[^.]+$/, '') || 'output';
      a.href = url;
      a.download = `ValidationReport_${rawName.slice(0, 40)}.xlsx`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      alert('Validation report not available yet.');
    } finally {
      setDownloading(null);
    }
  };

  return (
    <div className="tab-section">
      <h2>📦 Results</h2>
      {!isDone ? (
        <div className="info-box">Downloads appear here once processing completes.</div>
      ) : (
        <div className="downloads-grid">
          <div className="panel">
            <h4>📥 Output ZIP</h4>
            <p>Contains all pipeline outputs including the final PXML file.</p>
            <button
              className="btn-download btn-block"
              onClick={handleDownloadOutput}
              disabled={downloading === 'output'}
            >
              {downloading === 'output' ? '⏳ Downloading...' : '📥 Download Output ZIP'}
            </button>
          </div>
          <div className="panel">
            <h4>📊 Validation Report</h4>
            <p>Excel report comparing original vs processed document.</p>
            <button
              className="btn-download btn-block"
              onClick={handleDownloadValidation}
              disabled={downloading === 'validation'}
            >
              {downloading === 'validation' ? '⏳ Downloading...' : '📊 Download Validation Excel'}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
