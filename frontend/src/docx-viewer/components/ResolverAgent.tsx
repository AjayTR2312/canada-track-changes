import React, { useCallback, useEffect, useRef, useState } from 'react';
import type { PxmlEditorApi } from './PxmlEditor';
import './ResolverAgent.css';
import { API_BASE } from '../../services/basePath';

const API_URL = API_BASE;

interface Proposal {
  kind: 'replace' | 'insert_after';
  block_id: string;
  find: string;
  replace: string;
  explanation: string;
  confidence: 'high' | 'medium' | 'low';
  warnings: string[];
  block_text: string;
}

interface AgentEvent {
  type: 'step' | 'final' | 'error';
  n?: number;
  kind?: 'think' | 'tool' | 'result';
  tool?: string;
  text?: string;
  ok?: boolean;
  data?: { block_id: string; score: number; text: string }[];
  status?: 'proposed' | 'gave_up' | 'exhausted';
  reason?: string;
  proposal?: Proposal;
  model?: string;
  steps?: number;
  message?: string;
  run_id?: string;
}

const REJECT_REASONS: { id: string; label: string }[] = [
  { id: 'wrong_paragraph', label: 'Wrong paragraph' },
  { id: 'wrong_tags', label: 'Wrong tags' },
  { id: 'text_changed', label: 'Changed other text' },
  { id: 'not_needed', label: 'Not needed' },
  { id: 'other', label: 'Other' },
];

/** Who is using the tool, for the feedback log (same source the false-positive flag uses). */
function reviewer(): { emp_id: string; user_name: string } {
  try {
    const p = JSON.parse(localStorage.getItem('user_profile') || '{}');
    return { emp_id: p.emp_id || new URLSearchParams(window.location.search).get('emp_id') || '', user_name: p.name || '' };
  } catch {
    return { emp_id: '', user_name: '' };
  }
}

type Phase = 'idle' | 'running' | 'proposed' | 'gave_up' | 'exhausted' | 'error' | 'accepted' | 'rejected';

interface Props {
  row: Record<string, unknown>;
  sessionId?: string | null;
  editorApiRef: React.MutableRefObject<PxmlEditorApi | null>;
  dtdFamily?: string;
  onAccepted?: () => void;
}

/** Renders proposed XML with the trace tags made visible: insertions green, deletions red. */
const ProposalXml: React.FC<{ xml: string }> = ({ xml }) => {
  const parts = xml.split(/(<trace>[\s\S]*?<\/trace>|<trace\.deleted>[\s\S]*?<trace\.deleted\s*\/>)/g);
  return (
    <pre className="ra-xml">
      {parts.map((p, i) =>
        p.startsWith('<trace.deleted>') ? <span key={i} className="ra-del">{p}</span>
          : p.startsWith('<trace>') ? <span key={i} className="ra-ins">{p}</span>
            : <span key={i}>{p}</span>)}
    </pre>
  );
};

const TOOL_LABEL: Record<string, string> = { search_blocks: 'Search', get_block: 'Read', propose_edit: 'Propose' };

const ResolverAgent: React.FC<Props> = ({ row, sessionId, editorApiRef, dtdFamily = 'auto', onAccepted }) => {
  const runIdRef = useRef('');
  const [askReason, setAskReason] = useState(false);
  const [phase, setPhase] = useState<Phase>('idle');
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [message, setMessage] = useState('');
  const [model, setModel] = useState('');
  const [open, setOpen] = useState(true);
  const abortRef = useRef<AbortController | null>(null);
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => () => abortRef.current?.abort(), []);
  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [events]);

  const run = useCallback(async () => {
    const pxml = editorApiRef.current?.getText() ?? '';
    if (!pxml.trim()) {
      setPhase('error');
      setMessage('Load the PXML in the editor first.');
      return;
    }
    setPhase('running');
    setEvents([]);
    setProposal(null);
    setMessage('');
    setOpen(true);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const res = await fetch(`${API_URL}/api/revision-resolver/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ row, pxml, dtd_family: dtdFamily, session_id: sessionId || '', ...reviewer() }),
        signal: ctrl.signal,
      });
      if (!res.ok || !res.body) {
        const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      let finished = false;
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const lines = buf.split('\n');
        buf = lines.pop() ?? '';
        for (const line of lines) {
          if (!line.trim()) continue;
          const ev = JSON.parse(line) as AgentEvent;
          if (ev.type === 'error') {
            setPhase('error');
            setMessage(ev.message || 'The resolver failed.');
            finished = true;
          } else if (ev.type === 'final') {
            finished = true;
            runIdRef.current = ev.run_id || '';
            setAskReason(false);
            setModel(ev.model || '');
            setEvents((prev) => [...prev, ev]);
            if (ev.status === 'proposed' && ev.proposal) {
              setProposal(ev.proposal);
              setPhase('proposed');
            } else {
              setPhase(ev.status === 'gave_up' ? 'gave_up' : 'exhausted');
              setMessage(ev.reason || '');
            }
          } else {
            setEvents((prev) => [...prev, ev]);
          }
        }
      }
      if (!finished) {
        setPhase('error');
        setMessage('The agent connection ended unexpectedly.');
      }
    } catch (e) {
      if ((e as Error).name === 'AbortError') {
        setPhase('idle');
        return;
      }
      setPhase('error');
      setMessage(e instanceof Error ? e.message : 'The resolver failed.');
    }
  }, [row, editorApiRef, dtdFamily]);

  const accept = () => {
    if (!proposal) return;
    const err = editorApiRef.current?.applyEdit(proposal.find, proposal.replace) ?? 'The editor is not ready.';
    if (err) {
      setMessage(err);
      return;
    }
    setPhase('accepted');
    setMessage('');
    report('accepted');
    onAccepted?.();
  };

  /** Tell the backend what the reviewer decided (fire and forget: never blocks the review). */
  const report = (outcome: 'accepted' | 'rejected', reason = '') => {
    if (!runIdRef.current) return;
    fetch(`${API_URL}/api/revision-resolver/outcome`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: runIdRef.current, outcome, reject_reason: reason }),
    }).catch(() => { /* feedback is best-effort */ });
  };

  const reject = (reason: string) => {
    report('rejected', reason);
    setAskReason(false);
    setPhase('rejected');
  };

  const stop = () => abortRef.current?.abort();

  const steps = events.filter((e) => e.type === 'step');
  const busy = phase === 'running';

  return (
    <div className={`ra ra-${phase}`}>
      {phase === 'idle' && (
        <button className="ra-run" onClick={run} title="The agent searches the XML for where this change belongs and proposes the edit. You review it before anything changes.">
          <span className="ra-spark">✦</span> Resolve with agent
        </button>
      )}

      {phase !== 'idle' && (
        <div className="ra-head">
          <span className={`ra-badge ra-badge-${phase}`}>
            {busy && <span className="ra-pulse" />}
            {{ running: 'Agent working', proposed: 'Proposal ready', gave_up: 'No safe placement', exhausted: 'No proposal', error: 'Failed', accepted: 'Applied', rejected: 'Rejected', idle: '' }[phase]}
          </span>
          {model && <span className="ra-model">{model}</span>}
          <button className="ra-toggle" onClick={() => setOpen(!open)}>{open ? 'Hide activity' : `Activity (${steps.length})`}</button>
          {busy && <button className="ra-stop" onClick={stop}>Stop</button>}
        </div>
      )}

      {phase !== 'idle' && open && (
        <div className="ra-log" ref={logRef}>
          {steps.map((e, i) => (
            <div key={i} className={`ra-step ra-${e.kind}${e.ok === false ? ' ra-bad' : ''}`}>
              <span className="ra-dot" />
              <div className="ra-step-body">
                {e.kind === 'tool' && <span className="ra-tool">{TOOL_LABEL[e.tool || ''] || e.tool}</span>}
                <span className="ra-text">{e.text}</span>
                {e.data && e.data.length > 0 && (
                  <ul className="ra-hits">
                    {e.data.map((h) => (
                      <li key={h.block_id}><b>{h.block_id}</b> <i>{Math.round(h.score * 100)}%</i> {h.text}</li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          ))}
          {busy && <div className="ra-step ra-think"><span className="ra-dot ra-dot-live" /><div className="ra-step-body"><span className="ra-text">Thinking…</span></div></div>}
        </div>
      )}

      {(phase === 'gave_up' || phase === 'exhausted' || phase === 'error') && (
        <div className="ra-note">
          {message || 'No result.'}
          <div className="ra-actions"><button className="ra-secondary" onClick={run}>Try again</button></div>
        </div>
      )}

      {proposal && (phase === 'proposed' || phase === 'accepted' || phase === 'rejected') && (
        <div className="ra-proposal">
          <div className="ra-prop-head">
            <span>{proposal.kind === 'insert_after' ? `Insert after block ${proposal.block_id}` : `Edit block ${proposal.block_id}`}</span>
            <span className={`ra-conf ra-conf-${proposal.confidence}`}>{proposal.confidence} confidence</span>
          </div>
          <div className="ra-expl">{proposal.explanation}</div>
          <div className="ra-prop-label">Target text</div>
          <div className="ra-target">{proposal.block_text}</div>
          <div className="ra-prop-label">{proposal.kind === 'insert_after' ? 'New XML' : 'Proposed XML'}</div>
          <ProposalXml xml={proposal.kind === 'insert_after' ? proposal.replace.slice(proposal.find.length) : proposal.replace} />
          {proposal.warnings.length > 0 && <div className="ra-warn">⚠ {proposal.warnings.join(' · ')}</div>}
          {message && <div className="ra-warn">⚠ {message}</div>}
          {phase === 'proposed' && !askReason && (
            <div className="ra-actions">
              <button className="ra-accept" onClick={accept}>Accept &amp; apply</button>
              <button className="ra-secondary" onClick={() => setAskReason(true)}>Reject</button>
              <button className="ra-secondary" onClick={run}>Run again</button>
            </div>
          )}
          {phase === 'proposed' && askReason && (
            <div className="ra-reasons">
              <div className="ra-prop-label">What was wrong? (one click, helps improve the agent)</div>
              <div className="ra-reason-row">
                {REJECT_REASONS.map((r) => <button key={r.id} className="ra-chip" onClick={() => reject(r.id)}>{r.label}</button>)}
              </div>
              <button className="ra-toggle" style={{ marginLeft: 0 }} onClick={() => reject('')}>Skip</button>
            </div>
          )}
          {phase === 'accepted' && <div className="ra-done">✓ Applied in the XML editor. Save and validate as usual.</div>}
          {phase === 'rejected' && <div className="ra-actions"><span className="ra-muted">Rejected, nothing changed.</span><button className="ra-secondary" onClick={run}>Run again</button></div>}
        </div>
      )}
    </div>
  );
};

export default ResolverAgent;
