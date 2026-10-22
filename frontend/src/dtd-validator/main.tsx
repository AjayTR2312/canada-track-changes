import { createRoot } from 'react-dom/client';
import '../docx-viewer/standalone.css';
import DtdValidatorApp from './DtdValidatorApp';

// Entry point for the standalone DTD validator document (dtd-validator.html): its own
// dark/light globals, isolated from the host app's CSS (same pattern as docx-viewer.html).
createRoot(document.getElementById('dtd-validator-root')!).render(<DtdValidatorApp />);
