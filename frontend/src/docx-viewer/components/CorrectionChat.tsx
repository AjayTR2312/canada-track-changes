import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import './CorrectionChat.css';
import { API_BASE } from '../../services/basePath';

const API_URL = API_BASE;

/** The block the reviewer is correcting, captured from the editor when the chat opened. */
export interface CapturedBlock {
  text: string;
  from: number;
  to: number;
  parentTag: string;
  before: string;
  after: string;
  kind: 'selection' | 'element';
  label: string;
}

interface Issue { severity: 'error' | 'warning'; message: string }

interface Proposal {
  proposal_id: string;
  xml: string;
  explanation: string;
  issues: Issue[];
  valid: boolean;
  text_changed: boolean;
  model: string;
  attempts: number;
}

type Turn =
  | { role: 'user'; content: string }
  | { role: 'assistant'; proposal: Proposal; before: string; applied?: boolean }
  | { role: 'error'; content: string };

interface Props {
  block: CapturedBlock;
  anchorEl: HTMLElement | null;
  /** First ~600 chars of the document (carries the DOCTYPE). */
  pxmlHead: string;
  dtdFamily: string;
  sessionId?: string | null;
  docName?: string | null;
  /** Replace the captured range with new XML; resolves to an error message, or '' on success. */
  onApply: (block: CapturedBlock, newXml: string) => string;
  onApplied: () => void;
  onClose: () => void;
}

interface AttachedImage {
  id: string;
  name: string;
  mimeType: string;
  objectUrl: string;
  file: File;
}

const PANEL_WIDTH = 460;
const PANEL_MAX_HEIGHT = 620;
const MARGIN = 8;

// ── tiny token diff (blocks are small): common tokens kept, the rest marked ──────────────
function tokenize(s: string): string[] {
  return s.match(/<[^>]+>|\s+|[^\s<]+/g) || [];
}

function diffTokens(a: string[], b: string[]): { removed: boolean[]; added: boolean[] } {
  const n = a.length, m = b.length;
  if (n * m > 400000) return { removed: a.map(() => false), added: b.map(() => false) };   // too big to diff
  const dp: Uint16Array[] = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const removed = a.map(() => true), added = b.map(() => true);
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { removed[i] = false; added[j] = false; i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) i++;
    else j++;
  }
  return { removed, added };
}

const DiffView: React.FC<{ before: string; after: string }> = ({ before, after }) => {
  const { a, b, d } = useMemo(() => {
    const a = tokenize(before), b = tokenize(after);
    return { a, b, d: diffTokens(a, b) };
  }, [before, after]);
  return (
    <div className="cc-diff">
      <div className="cc-diff-label">Before</div>
      <pre className="cc-pre">{a.map((t, i) => (d.removed[i] ? <span key={i} className="cc-del">{t}</span> : <React.Fragment key={i}>{t}</React.Fragment>))}</pre>
      <div className="cc-diff-label">After</div>
      <pre className="cc-pre">{b.map((t, i) => (d.added[i] ? <span key={i} className="cc-add">{t}</span> : <React.Fragment key={i}>{t}</React.Fragment>))}</pre>
    </div>
  );
};

const CorrectionChat: React.FC<Props> = ({
  block, anchorEl, pxmlHead, dtdFamily, sessionId, docName, onApply, onApplied, onClose,
}) => {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [showBlock, setShowBlock] = useState(false);
  const [attachedImages, setAttachedImages] = useState<AttachedImage[]>([]);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useLayoutEffect(() => {
    if (!anchorEl) return;
    const place = () => {
      const rect = anchorEl.getBoundingClientRect();
      let left = rect.right - PANEL_WIDTH;
      left = Math.max(MARGIN, Math.min(left, window.innerWidth - PANEL_WIDTH - MARGIN));
      let top = rect.bottom + 4;
      if (top + PANEL_MAX_HEIGHT > window.innerHeight - MARGIN) top = Math.max(MARGIN, window.innerHeight - PANEL_MAX_HEIGHT - MARGIN);
      setPos({ top, left });
    };
    place();
    window.addEventListener('resize', place);
    return () => window.removeEventListener('resize', place);
  }, [anchorEl]);

  useEffect(() => { inputRef.current?.focus(); }, []);
  useEffect(() => { listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: 'smooth' }); }, [turns, busy]);
  useEffect(() => {
    const onEsc = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onEsc);
    return () => document.removeEventListener('keydown', onEsc);
  }, [onClose]);

  const user = (() => {
    try { return JSON.parse(localStorage.getItem('user_profile') || '{}') as { emp_id?: string; name?: string }; }
    catch { return {}; }
  })();

  // The XML a refinement should start from: the latest proposal, else the original block.
  const latestProposal = [...turns].reverse().find((t): t is Extract<Turn, { role: 'assistant' }> => t.role === 'assistant');

  const handleImageSelection = (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files || []);
    if (!files.length) return;

    const valid = files.filter((file) => file.type.startsWith('image/'));
    const imageRecords = valid.map((file) => ({
      id: `${file.name}-${file.size}-${Date.now()}-${Math.random()}`,
      name: file.name,
      mimeType: file.type || 'image/png',
      objectUrl: URL.createObjectURL(file),
      file,
    }));

    setAttachedImages((prev) => [...prev, ...imageRecords].slice(-5));
    if (event.target) event.target.value = '';
  };

  const removeImage = (id: string) => {
    setAttachedImages((prev) => {
      const match = prev.find((img) => img.id === id);
      if (match) URL.revokeObjectURL(match.objectUrl);
      return prev.filter((img) => img.id !== id);
    });
  };

  const send = async () => {
    const text = input.trim();
    if ((!text && attachedImages.length === 0) || busy) return;
    setInput('');
    setBusy(true);
    const sentBlockText = block.text;   // what the model is shown (and what the diff compares against)
    const history = turns.flatMap((t) =>
      t.role === 'user' ? [{ role: 'user', content: t.content }]
      : t.role === 'assistant' ? [{ role: 'assistant', content: `${t.proposal.explanation}\n${t.proposal.xml}` }]
      : []);
    const userMessage = text || (attachedImages.length ? `Review the attached image(s) and fix this block.` : '');
    setTurns((prev) => [...prev, { role: 'user', content: userMessage }]);
    try {
      const formData = new FormData();
      formData.append('block_xml', block.text);
      formData.append('instruction', text || 'Please review the attached image(s) and fix this XML block based on them.');
      formData.append('context_before', block.before);
      formData.append('context_after', block.after);
      formData.append('parent_tag', block.parentTag);
      formData.append('pxml_head', pxmlHead);
      formData.append('dtd_family', dtdFamily);
      formData.append('history', JSON.stringify(history));
      formData.append('session_id', sessionId || '');
      formData.append('emp_id', user.emp_id || '');
      formData.append('user_name', user.name || '');
      formData.append('doc_name', docName || '');
      attachedImages.forEach((image) => {
        formData.append('images', image.file, image.name);
      });

      const res = await fetch(`${API_URL}/api/correction/propose`, {
        method: 'POST',
        body: formData,
      });
      if (!res.ok) {
        const errBody = await res.json().catch(() => ({}));
        throw new Error(errBody?.detail || `HTTP ${res.status}`);
      }
      const proposal = (await res.json()) as Proposal;
      setTurns((prev) => [...prev, { role: 'assistant', proposal, before: sentBlockText }]);
      setAttachedImages([]);
    } catch (e: any) {
      setTurns((prev) => [...prev, { role: 'error', content: e?.message || 'Something went wrong' }]);
    } finally {
      setBusy(false);
    }
  };

  const apply = async (idx: number) => {
    const t = turns[idx];
    if (t.role !== 'assistant') return;
    const err = onApply(block, t.proposal.xml);
    if (err) {
      setTurns((prev) => [...prev, { role: 'error', content: err }]);
      return;
    }
    setTurns((prev) => prev.map((x, i) => (i === idx && x.role === 'assistant' ? { ...x, applied: true } : x)));
    fetch(`${API_URL}/api/correction/applied`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ proposal_id: t.proposal.proposal_id, applied: true }),
    }).catch(() => {});
    onApplied();
  };

  if (!pos) return null;

  return createPortal(
    <div className="cc-panel" ref={rootRef} style={{ top: pos.top, left: pos.left, width: PANEL_WIDTH, maxHeight: PANEL_MAX_HEIGHT }}>
      <div className="cc-header">
        <span className="cc-title">🤖 Fix a block with AI</span>
        <button className="cc-close" onClick={onClose} title="Close (Esc)">✕</button>
      </div>

      <div className="cc-block">
        <button className="cc-block-toggle" onClick={() => setShowBlock((v) => !v)}>
          {showBlock ? '▾' : '▸'} {block.label} · {block.text.length.toLocaleString()} chars · inside &lt;{block.parentTag || '?'}&gt;
        </button>
        {showBlock && <pre className="cc-pre cc-block-pre">{block.text.slice(0, 1500)}{block.text.length > 1500 ? '\n…' : ''}</pre>}
      </div>

      <div className="cc-list" ref={listRef}>
        {turns.length === 0 && (
          <div className="cc-hint">
            Say what this block must become, e.g.<br />
            <em>“Tag the first line as a note.heading”</em>, <em>“this is a statute cite, not a case”</em>,{' '}
            <em>“wrap items 1–4 in a list”</em>. I’ll return the retagged block, checked against the DTD, for you to apply.
          </div>
        )}
        {turns.map((t, i) => {
          if (t.role === 'user') return <div key={i} className="cc-msg cc-user">{t.content}</div>;
          if (t.role === 'error') return <div key={i} className="cc-msg cc-error">⚠️ {t.content}</div>;
          const p = t.proposal;
          const errors = p.issues.filter((x) => x.severity === 'error');
          const warns = p.issues.filter((x) => x.severity === 'warning');
          return (
            <div key={i} className="cc-msg cc-assistant">
              {p.explanation && <div className="cc-expl">{p.explanation}</div>}
              <div className="cc-chips">
                <span className={`cc-chip ${p.valid ? 'cc-ok' : 'cc-bad'}`}>{p.valid ? '✓ valid for the DTD' : `✗ ${errors.length} DTD issue${errors.length === 1 ? '' : 's'}`}</span>
                {p.text_changed && <span className="cc-chip cc-warn">text changed</span>}
                {p.attempts > 1 && <span className="cc-chip">auto-repaired</span>}
              </div>
              {[...errors, ...warns].map((x, k) => <div key={k} className={`cc-issue cc-issue-${x.severity}`}>• {x.message}</div>)}
              <DiffView before={t.before} after={p.xml} />
              <div className="cc-actions">
                <button className="cc-apply" onClick={() => apply(i)} disabled={t.applied} title={p.valid ? 'Replace the selected block in the editor' : 'This proposal still has DTD issues — apply only if you will fix them'}>
                  {t.applied ? '✓ Applied' : p.valid ? 'Apply to editor' : 'Apply anyway'}
                </button>
                <button className="cc-copy" onClick={() => navigator.clipboard?.writeText(p.xml)}>Copy XML</button>
                <span className="cc-model">{p.model}</span>
              </div>
            </div>
          );
        })}
        {busy && <div className="cc-msg cc-assistant cc-busy">⏳ Retagging…</div>}
      </div>

      <div className="cc-attachment-row">
        <input
          ref={fileInputRef}
          type="file"
          accept="image/*"
          multiple
          hidden
          onChange={handleImageSelection}
        />
        <button className="cc-attach" type="button" onClick={() => fileInputRef.current?.click()} disabled={busy}>
          + image
        </button>
        {attachedImages.length > 0 && (
          <div className="cc-image-strip">
            {attachedImages.map((img) => (
              <div key={img.id} className="cc-image-wrap">
                <img src={img.objectUrl} alt={img.name} className="cc-image-thumb" />
                <button type="button" className="cc-image-remove" onClick={() => removeImage(img.id)}>×</button>
              </div>
            ))}
          </div>
        )}
      </div>
      <div className="cc-input-row">
        <textarea
          ref={inputRef}
          className="cc-input"
          rows={2}
          placeholder={latestProposal ? 'Refine it… (Enter to send)' : 'What should this block be? (Enter to send, Shift+Enter for a new line)'}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } }}
          disabled={busy}
        />
        <button className="cc-send" onClick={send} disabled={busy || (!input.trim() && attachedImages.length === 0)}>Send</button>
      </div>
    </div>,
    document.body,
  );
};

export default CorrectionChat;
