import React, { useState, useEffect } from 'react';
import { useAuth } from '../context/AuthContext';
import { getUsageLogs, UsageLog } from '../services/api';
import './Tabs.css';

export default function AccountTab() {
  const { user, updateProfile } = useAuth();
  const [editing, setEditing] = useState(false);
  const [editForm, setEditForm] = useState({ name: '', email: '', dept: '' });
  const [saving, setSaving] = useState(false);

  // Usage logs
  const [logs, setLogs] = useState<UsageLog[]>([]);
  const [showAll, setShowAll] = useState(false);
  const [dateFilter, setDateFilter] = useState('');
  const [outcomeFilter, setOutcomeFilter] = useState('All');
  const [loadingLogs, setLoadingLogs] = useState(false);

  const fetchLogs = async () => {
    setLoadingLogs(true);
    try {
      const data = await getUsageLogs(showAll ? undefined : user?.emp_id);
      setLogs(Array.isArray(data) ? data : []);
    } catch {
      setLogs([]);
    } finally {
      setLoadingLogs(false);
    }
  };

  useEffect(() => {
    fetchLogs();
  }, [showAll, user?.emp_id]);

  const handleEditStart = () => {
    setEditForm({ name: user?.name || '', email: user?.email || '', dept: user?.dept || '' });
    setEditing(true);
  };

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!user) return;
    setSaving(true);
    await updateProfile({ emp_id: user.emp_id, ...editForm });
    setSaving(false);
    setEditing(false);
  };

  // Filter logs
  let filteredLogs = logs;
  if (dateFilter) {
    filteredLogs = filteredLogs.filter((l) => l.date?.startsWith(dateFilter));
  }
  if (outcomeFilter !== 'All') {
    filteredLogs = filteredLogs.filter((l) => l.run_outcome === outcomeFilter);
  }

  const handleDownloadCSV = () => {
    if (!filteredLogs.length) return;
    const headers = Object.keys(filteredLogs[0]);
    const csvRows = [
      headers.join(','),
      ...filteredLogs.map((row) => headers.map((h) => `"${(row as any)[h] ?? ''}"`).join(',')),
    ];
    const blob = new Blob([csvRows.join('\n')], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `usage_logs_${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="tab-section">
      {/* User Profile */}
      <h2>👤 User Profile</h2>
      {editing ? (
        <form className="edit-profile-form" onSubmit={handleSave}>
          <div className="form-group">
            <label>Full Name *</label>
            <input value={editForm.name} onChange={(e) => setEditForm({ ...editForm, name: e.target.value })} />
          </div>
          <div className="form-group">
            <label>Email *</label>
            <input value={editForm.email} onChange={(e) => setEditForm({ ...editForm, email: e.target.value })} />
          </div>
          <div className="form-group">
            <label>Department</label>
            <input value={editForm.dept} onChange={(e) => setEditForm({ ...editForm, dept: e.target.value })} />
          </div>
          <div className="form-actions">
            <button type="submit" className="btn-primary" disabled={saving}>
              {saving ? 'Saving...' : '💾 Save'}
            </button>
            <button type="button" className="btn-secondary" onClick={() => setEditing(false)}>Cancel</button>
          </div>
        </form>
      ) : (
        <div className="profile-display">
          <table className="profile-table">
            <tbody>
              <tr><td><strong>Employee ID</strong></td><td>{user?.emp_id}</td></tr>
              <tr><td><strong>Name</strong></td><td>{user?.name || '—'}</td></tr>
              <tr><td><strong>Email</strong></td><td>{user?.email || '—'}</td></tr>
              <tr><td><strong>Department</strong></td><td>{user?.dept || '—'}</td></tr>
            </tbody>
          </table>
          <button className="btn-secondary" onClick={handleEditStart}>✏️ Edit Profile</button>
        </div>
      )}

      <hr style={{ margin: '2rem 0' }} />

      {/* Usage Logs */}
      <h2>📊 Usage Logs</h2>
      <div className="logs-controls">
        <label className="toggle-label">
          <input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} />
          Show all users
        </label>
        <button className="btn-secondary" onClick={fetchLogs} disabled={loadingLogs}>
          🔄 Refresh
        </button>
      </div>

      <div className="logs-filters">
        <div className="form-group">
          <label>Filter by date (YYYY-MM-DD)</label>
          <input value={dateFilter} onChange={(e) => setDateFilter(e.target.value)} placeholder="2026-01-01" />
        </div>
        <div className="form-group">
          <label>Filter by outcome</label>
          <select value={outcomeFilter} onChange={(e) => setOutcomeFilter(e.target.value)}>
            <option value="All">All</option>
            <option value="success">success</option>
            <option value="stopped">stopped</option>
            <option value="error">error</option>
          </select>
        </div>
      </div>

      <p className="text-muted">{filteredLogs.length} record(s) shown</p>

      {filteredLogs.length > 0 ? (
        <div className="table-wrapper" style={{maxHeight:'420px', overflowY:'auto', border:'1px solid #dee2e6', borderRadius:'6px'}}>
          <table className="data-table">
            <thead>
              <tr>
                <th>Date</th>
                <th>Time</th>
                <th>Employee</th>
                <th>Name</th>
                <th>Service</th>
                <th>Region</th>
                <th>Product</th>
                <th>Outcome</th>
                <th>Duration(s)</th>
                <th>LLM Calls</th>
              </tr>
            </thead>
            <tbody>
              {filteredLogs.map((log, i) => (
                <tr key={i}>
                  <td>{log.date}</td>
                  <td>{log.time}</td>
                  <td>{log.emp_id}</td>
                  <td>{log.name}</td>
                  <td>{log.service_type}</td>
                  <td>{log.region}</td>
                  <td>{log.product}</td>
                  <td>
                    <span className={`outcome-badge ${log.run_outcome}`}>{log.run_outcome}</span>
                  </td>
                  <td>{log.processing_time_sec != null ? parseFloat(String(log.processing_time_sec)).toFixed(1) : '—'}</td>
                  <td>{log.llm_calls != null ? log.llm_calls : 0}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="info-box">No usage logs found yet.</div>
      )}

      <button className="btn-download" onClick={handleDownloadCSV} style={{ marginTop: '1rem' }} disabled={!filteredLogs.length}>
        ⬇️ Download as CSV
      </button>
    </div>
  );
}
