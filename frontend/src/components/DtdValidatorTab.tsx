import React from 'react';
import { withBase } from '../services/basePath';

// The validator runs as its own isolated document (dtd-validator.html, like the
// Visual Validator) so its editor styles and bundle stay out of the host app.
export default function DtdValidatorTab() {
  return (
    <div className="tab-section" style={{ padding: 0 }}>
      <iframe
        title="DTD Validator"
        src={withBase('/dtd-validator.html')}
        style={{ width: '100%', height: 'calc(100vh - 190px)', minHeight: 520, border: '1px solid #d0d5db', borderRadius: 8, background: '#fff' }}
      />
      <p className="text-muted" style={{ marginTop: 8 }}>
        Prefer a full window? <a href={withBase('/dtd-validator.html')} target="_blank" rel="noreferrer">Open the DTD Validator in a new tab</a>.
      </p>
    </div>
  );
}
