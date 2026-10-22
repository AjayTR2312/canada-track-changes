import React, { useState, useEffect } from 'react';
import { useJob } from '../context/JobContext';
import { getJobLog } from '../services/api';
import './Tabs.css';

export default function SystemLogsTab() {
  const { jobId } = useJob();
  const [logText, setLogText] = useState('');
  const [loading, setLoading] = useState(false);

  const fetchLog = async () => {
    if (!jobId) return;
    setLoading(true);
    try {
      const log = await getJobLog(jobId);
      setLogText(log);
    } catch {
      setLogText('Could not fetch log.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchLog();
  }, [jobId]);

  return (
    <div className="tab-section">
      <h2>📜 Complete Processing Log</h2>
      {!jobId ? (
        <div className="info-box">📭 No log entries yet — start a job first.</div>
      ) : (
        <>
          <button className="btn-secondary" onClick={fetchLog} disabled={loading} style={{ marginBottom: '1rem' }}>
            {loading ? '⏳ Loading...' : '🔄 Refresh Log'}
          </button>
          <pre className="log-output-full">{logText || '(empty)'}</pre>
        </>
      )}
    </div>
  );
}
