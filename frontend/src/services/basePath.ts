// Runtime mount-prefix detection.
//
// The app may be served at the web root ("/") in local/dev runs, or under a
// platform sub-path in production — e.g. Plexus serves it at
// "/208321/canadatrackchanges/". Asset URLs are made relative at build time
// (vite build --base=./), but API calls and cross-page links are written as
// absolute paths in code ("/api/...", "/docx-viewer.html"). Without a prefix
// those resolve against the domain root and 404 under a sub-path mount.
//
// The host app is state-based (no client-side router), so each document is
// served at the mount root and window.location.pathname reflects it at load
// time. A VITE_API_URL env var (used by the dev proxy) always takes precedence.

function detectBasePrefix(): string {
  const envUrl = (import.meta as any).env?.VITE_API_URL;
  if (envUrl) return String(envUrl).replace(/\/+$/, '');
  if (typeof window === 'undefined') return '';
  let path = window.location.pathname;
  // Entry documents served as files (docx-viewer.html, dtd-validator.html) keep
  // their directory; directory mounts already end in "/".
  if (!path.endsWith('/')) path = path.slice(0, path.lastIndexOf('/') + 1);
  return path.replace(/\/+$/, '');
}

/** Mount prefix, e.g. "" at the web root or "/208321/canadatrackchanges". */
export const API_BASE = detectBasePrefix();

/** Prefix an absolute in-app path ("/api/...", "/docx-viewer.html") with the mount. */
export const withBase = (path: string): string => `${API_BASE}${path}`;
