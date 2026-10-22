import { createRoot } from 'react-dom/client';
import './standalone.css';
import DocxViewerApp from './DocxViewerApp';

// Entry point for the isolated iframe document (docx-viewer.html). Mounts the
// viewer with its OWN dark globals, fully isolated from the host app's CSS.
createRoot(document.getElementById('docx-viewer-root')!).render(
  <DocxViewerApp />,
);
