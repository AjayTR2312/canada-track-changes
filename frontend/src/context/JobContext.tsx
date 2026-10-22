import React, { createContext, useContext, useState, useCallback, ReactNode } from 'react';
import { JobStatus, getJobStatus } from '../services/api';

interface JobContextType {
  jobId: string | null;
  status: JobStatus | null;
  setJobId: (id: string | null) => void;
  setStatus: (s: JobStatus | null) => void;
  pollStatus: () => Promise<void>;
  resetJob: () => void;
}

const JobContext = createContext<JobContextType | undefined>(undefined);

// Unique key per browser tab — prevents parallel tabs sharing the same job ID.
const _TAB_JOB_KEY = (() => {
  let k = sessionStorage.getItem('_tab_uid');
  if (!k) { k = Math.random().toString(36).slice(2, 10); sessionStorage.setItem('_tab_uid', k); }
  return `job_${k}`;
})();

export function JobProvider({ children }: { children: ReactNode }) {
  const [jobId, setJobId] = useState<string | null>(() => {
    return sessionStorage.getItem(_TAB_JOB_KEY);
  });
  const [status, setStatus] = useState<JobStatus | null>(null);

  const setJobIdPersist = useCallback((id: string | null) => {
    setJobId(id);
    if (id) {
      sessionStorage.setItem(_TAB_JOB_KEY, id);
    } else {
      sessionStorage.removeItem(_TAB_JOB_KEY);
    }
  }, []);

  const pollStatus = useCallback(async () => {
    if (!jobId) return;
    try {
      const s = await getJobStatus(jobId);
      setStatus(s);
    } catch (err: any) {
      // Job lost after backend restart — clear it so the UI shows Ready
      if (err?.response?.status === 404) {
        setJobId(null);
        setStatus(null);
        sessionStorage.removeItem(_TAB_JOB_KEY);
      }
    }
  }, [jobId]);

  const resetJob = useCallback(() => {
    setJobId(null);
    setStatus(null);
    sessionStorage.removeItem(_TAB_JOB_KEY);
  }, []);

  return (
    <JobContext.Provider value={{ jobId, status, setJobId: setJobIdPersist, setStatus, pollStatus, resetJob }}>
      {children}
    </JobContext.Provider>
  );
}

export function useJob() {
  const ctx = useContext(JobContext);
  if (!ctx) throw new Error('useJob must be used within JobProvider');
  return ctx;
}
