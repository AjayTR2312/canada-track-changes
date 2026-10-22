import React from 'react';
import { AuthProvider, useAuth } from './context/AuthContext';
import { JobProvider } from './context/JobContext';
import LoginPage from './pages/LoginPage';
import Dashboard from './pages/Dashboard';

function AppContent() {
  const { isLoggedIn } = useAuth();

  if (!isLoggedIn) {
    return <LoginPage />;
  }

  return (
    <JobProvider>
      <Dashboard />
    </JobProvider>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <AppContent />
    </AuthProvider>
  );
}
