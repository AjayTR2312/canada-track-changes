import React, { createContext, useContext, useState, useCallback, ReactNode } from 'react';
import { UserProfile, loginUser, registerUser, updateUserProfile } from '../services/api';

interface AuthContextType {
  user: UserProfile | null;
  isLoggedIn: boolean;
  login: (empId: string) => Promise<boolean>;
  register: (profile: { emp_id: string; name: string; email: string; dept: string }) => Promise<boolean>;
  updateProfile: (profile: { emp_id: string; name: string; email: string; dept: string }) => Promise<boolean>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<UserProfile | null>(() => {
    const stored = localStorage.getItem('user_profile');
    return stored ? JSON.parse(stored) : null;
  });

  const isLoggedIn = !!user && !!user.name;

  const login = useCallback(async (empId: string): Promise<boolean> => {
    const profile = await loginUser(empId);
    if (profile && profile.name) {
      setUser(profile);
      localStorage.setItem('user_profile', JSON.stringify(profile));
      return true;
    }
    return false;
  }, []);

  const register = useCallback(async (profile: { emp_id: string; name: string; email: string; dept: string }): Promise<boolean> => {
    try {
      const saved = await registerUser(profile);
      setUser(saved);
      localStorage.setItem('user_profile', JSON.stringify(saved));
      return true;
    } catch {
      return false;
    }
  }, []);

  const updateProfile = useCallback(async (profile: { emp_id: string; name: string; email: string; dept: string }): Promise<boolean> => {
    try {
      const updated = await updateUserProfile(profile);
      setUser(updated);
      localStorage.setItem('user_profile', JSON.stringify(updated));
      return true;
    } catch {
      return false;
    }
  }, []);

  const logout = useCallback(() => {
    setUser(null);
    localStorage.removeItem('user_profile');
    // Clear ALL per-tab job state so a fresh login does not resurrect the
    // previous job's data. JobContext stores the active job under `job_<tabUid>`
    // keys (plus the `_tab_uid` seed), so removing only 'current_job_id' was not
    // enough — the old job survived logout and reappeared on the next sign-in.
    Object.keys(sessionStorage).forEach((k) => {
      if (k === '_tab_uid' || k === 'current_job_id' || k.startsWith('job_')) {
        sessionStorage.removeItem(k);
      }
    });
  }, []);

  return (
    <AuthContext.Provider value={{ user, isLoggedIn, login, register, updateProfile, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within AuthProvider');
  return ctx;
}
