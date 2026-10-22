import React, { useState } from 'react';
import { useAuth } from '../context/AuthContext';
import { useRegionTitle } from '../region';
import './LoginPage.css';

export default function LoginPage() {
  const { login, register } = useAuth();
  const region = useRegionTitle();
  const [empId, setEmpId] = useState('');
  const [showRegister, setShowRegister] = useState(false);
  const [loginError, setLoginError] = useState('');
  const [regForm, setRegForm] = useState({ emp_id: '', name: '', email: '', dept: '' });
  const [regError, setRegError] = useState('');
  const [loading, setLoading] = useState(false);

  const validateEmpId = (id: string): string | null => {
    const trimmed = id.trim();
    if (!trimmed) return 'Please enter your Employee ID.';
    if (!/^[A-Za-z0-9]+$/.test(trimmed))
      return 'Employee ID must contain only letters and numbers (no spaces or special characters).';
    if (!/\d/.test(trimmed))
      return 'Employee ID must contain at least one number (e.g. 6121007 or C302473).';
    if (trimmed.length < 4)
      return 'Employee ID must be at least 4 characters.';
    return null;
  };

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    const err = validateEmpId(empId);
    if (err) { setLoginError(err); return; }
    setLoading(true);
    setLoginError('');
    try {
      const success = await login(empId.trim());
      if (!success) {
        setLoginError(`Employee ID "${empId.trim()}" not found. Please register below.`);
        setShowRegister(true);
        setRegForm((prev) => ({ ...prev, emp_id: empId.trim() }));
      }
    } catch {
      // Network / 503: the AI Users Log couldn't be reached. Do NOT prompt an
      // existing user to register — surface a retryable service error instead.
      setLoginError('Sign-in service is temporarily unavailable. Please try again in a moment.');
      setShowRegister(false);
    } finally {
      setLoading(false);
    }
  };

  const handleRegister = async (e: React.FormEvent) => {
    e.preventDefault();
    const empErr = validateEmpId(regForm.emp_id);
    if (empErr) { setRegError(empErr); return; }
    if (!regForm.name.trim() || !regForm.email.trim()) {
      setRegError('Full Name and Email are required.');
      return;
    }
    if (!regForm.email.includes('@')) {
      setRegError('Please enter a valid email address.');
      return;
    }
    setLoading(true);
    setRegError('');
    const success = await register(regForm);
    setLoading(false);
    if (!success) {
      setRegError('Registration failed. Please try again.');
    }
  };

  return (
    <div className="login-page">
      <div className="login-container">
        <div className="login-header">
          <h1>XML Track Changes{region.name ? ` - ${region.name}` : ''}</h1>
          <p className="login-subtitle">Thomson Reuters{region.name ? ` · ${region.name}` : ''}</p>
        </div>

        <div className="login-card">
          <h2>👤 Sign In</h2>
          <p className="login-info">Enter your Employee ID to continue. New users can register below.</p>

          <form onSubmit={handleLogin} className="login-form">
            <div className="form-group">
              <label htmlFor="empId">Employee ID *</label>
              <input
                id="empId"
                type="text"
                placeholder="e.g., 6121007"
                value={empId}
                onChange={(e) => setEmpId(e.target.value)}
                disabled={loading}
              />
            </div>
            <button type="submit" className="btn-primary" disabled={loading}>
              {loading ? 'Signing in...' : 'Sign In'}
            </button>
          </form>

          {loginError && <p className="error-message">{loginError}</p>}
        </div>

        {showRegister && (
          <div className="login-card register-card">
            <h2>📝 New User Registration</h2>
            <form onSubmit={handleRegister} className="login-form">
              <div className="form-group">
                <label htmlFor="regEmpId">Employee ID *</label>
                <input
                  id="regEmpId"
                  type="text"
                  placeholder="e.g., 6121007"
                  value={regForm.emp_id}
                  onChange={(e) => setRegForm({ ...regForm, emp_id: e.target.value })}
                  disabled={loading}
                />
              </div>
              <div className="form-group">
                <label htmlFor="regName">Full Name *</label>
                <input
                  id="regName"
                  type="text"
                  value={regForm.name}
                  onChange={(e) => setRegForm({ ...regForm, name: e.target.value })}
                  disabled={loading}
                />
              </div>
              <div className="form-group">
                <label htmlFor="regEmail">Email *</label>
                <input
                  id="regEmail"
                  type="email"
                  placeholder="firstname.lastname@thomsonreuters.com"
                  value={regForm.email}
                  onChange={(e) => setRegForm({ ...regForm, email: e.target.value })}
                  disabled={loading}
                />
              </div>
              <div className="form-group">
                <label htmlFor="regDept">Department</label>
                <input
                  id="regDept"
                  type="text"
                  value={regForm.dept}
                  onChange={(e) => setRegForm({ ...regForm, dept: e.target.value })}
                  disabled={loading}
                />
              </div>
              <button type="submit" className="btn-primary" disabled={loading}>
                {loading ? 'Registering...' : '✅ Register & Sign In'}
              </button>
            </form>
            {regError && <p className="error-message">{regError}</p>}
          </div>
        )}
      </div>
    </div>
  );
}
