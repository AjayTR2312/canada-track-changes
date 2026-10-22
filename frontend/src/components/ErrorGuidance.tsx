import { useState } from 'react';
import type { ErrorInfo } from '../services/api';
import './ErrorGuidance.css';

const ICON: Record<ErrorInfo['category'], string> = {
  llm_busy: '⏳', llm_auth: '🔑', llm_request: '📏', llm_config: '⚙️', stopped: '⏹',
  input: '📁', storage: '☁️', content: '🧩', unknown: '❓',
};

const NEXT_STEP: Record<ErrorInfo['action'], { label: string; cls: string }> = {
  rerun: { label: 'Safe to run again', cls: 'eg-rerun' },
  rerun_then_report: { label: 'Run again once; report if it repeats', cls: 'eg-rerun' },
  fix_input: { label: 'Fix the input files, then upload again', cls: 'eg-manual' },
  manual: { label: 'Work on this chapter manually', cls: 'eg-manual' },
  report: { label: 'Tell the tool owner', cls: 'eg-manual' },
};

interface Props {
  info?: ErrorInfo | null;
  /** Fallback when the server sent only plain text (older jobs). */
  rawError?: string;
  /** Link to the server log for this run, if there is one. */
  logUrl?: string;
  /** When given, the "run again" button is shown (disabled by `retryDisabled`). */
  onRetry?: () => void;
  retryDisabled?: boolean;
  retryTitle?: string;
  /** An extra button, e.g. "Run validation only". */
  extraAction?: { label: string; onClick: () => void; disabled?: boolean; title?: string };
  /** Compact = table cell; otherwise a full-width box. */
  compact?: boolean;
  /** Warning style for "finished, but a follow-up step (validation) did not run". */
  warning?: boolean;
}

/** Plain-language failure box: what happened, what to do next, and the technical detail on demand. */
export default function ErrorGuidance({ info, rawError, logUrl, onRetry, retryDisabled, retryTitle, extraAction, compact, warning }: Props) {
  const [open, setOpen] = useState(false);
  if (!info && !rawError) return null;
  const step = info ? NEXT_STEP[info.action] : null;
  const detail = info?.detail || rawError || '';

  return (
    <div className={`eg ${compact ? 'eg-compact' : ''} ${warning ? 'eg-warning' : ''}`} role="alert">
      <div className="eg-head">
        <span className="eg-icon" aria-hidden="true">{info ? ICON[info.category] : '❌'}</span>
        <span className="eg-title">{info?.title || 'The run failed'}</span>
      </div>
      {info && <div className="eg-hint">{info.hint}</div>}
      <div className="eg-actions">
        {step && <span className={`eg-step ${step.cls}`}>{step.label}</span>}
        {onRetry && (
          <button className="btn-secondary btn-sm" onClick={onRetry} disabled={retryDisabled} title={retryTitle}>
            🔁 {info && !info.retryable ? 'Retry anyway' : 'Run again'}
          </button>
        )}
        {extraAction && (
          <button className="btn-secondary btn-sm" onClick={extraAction.onClick} disabled={extraAction.disabled} title={extraAction.title}>
            {extraAction.label}
          </button>
        )}
        {logUrl && <a className="eg-link" href={logUrl} target="_blank" rel="noreferrer">View log</a>}
        {detail && (
          <button className="eg-link eg-toggle" onClick={() => setOpen(!open)}>
            {open ? 'Hide details' : 'Details'}
          </button>
        )}
      </div>
      {open && <pre className="eg-detail">{detail}</pre>}
    </div>
  );
}
