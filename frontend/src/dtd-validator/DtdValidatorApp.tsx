import React, { useCallback, useEffect, useRef, useState } from 'react';
import PxmlEditor from '../docx-viewer/components/PxmlEditor';
import type { DtdResult, DtdSchema, DtdFamily } from '../docx-viewer/components/PxmlEditor';
import '../docx-viewer/DocxViewerApp.css';
import { useRegion } from '../region';

const API_URL = (import.meta as any).env?.VITE_API_URL || '';

const API_ERROR_RESULT = (msg: string): DtdResult => ({
  available: false,
  wellformed: false,
  validation_passed: false,
  dtd_label: null,
  error_count: 0,
  warning_count: 0,
  errors: [{
    line: 0, lines: [], severity: 'error', rule: 'SERVER',
    title: 'DTD validation service unavailable', message: msg, fix: '', location: '', count: 1,
  }],
});

/**
 * Standalone DTD validator: upload (or paste into) a PXML and validate it against the
 * region's DTD (PAX / PIX for US Forms) — no DOCX, Excel or session needed. Reuses the Visual Validator's
 * PXML editor (tag palette, find/replace, repair, per-rule fixes) as-is.
 */
export default function DtdValidatorApp() {
  const region = useRegion();
  const [content, setContent] = useState('');
  const [fileName, setFileName] = useState<string | null>(null);
  // Which DTD(s) to VALIDATE against: 'both' (every family of the region, default), 'auto' (the
  // DOCTYPE's own) or one family id. The tag palette's family is separate (paletteFamily).
  const [family, setFamily] = useState<DtdFamily>('both');
  const [paletteFamily, setPaletteFamily] = useState<DtdFamily>('auto');
  const [full, setFull] = useState(false);
  const [result, setResult] = useState<DtdResult | null>(null);
  const [validating, setValidating] = useState(false);
  const [schema, setSchema] = useState<DtdSchema | null>(null);
  const [theme, setTheme] = useState<'dark' | 'light'>(() => {
    try { return (localStorage.getItem('vv-theme') as 'dark' | 'light') || 'light'; } catch { return 'light'; }
  });
  const [dragOver, setDragOver] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const contentRef = useRef('');
  const familyRef = useRef<DtdFamily>('both');
  const paletteFamilyRef = useRef<DtdFamily>('auto');
  const fullRef = useRef(false);

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    try { localStorage.setItem('vv-theme', theme); } catch { /* storage unavailable */ }
  }, [theme]);

  const postJson = async (path: string, body: unknown) => {
    const res = await fetch(`${API_URL}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      throw new Error(d?.detail || `HTTP ${res.status}`);
    }
    return res.json();
  };

  const fetchSchema = useCallback(async (text: string, fam: DtdFamily) => {
    if (!text.trim()) return;
    try { setSchema(await postJson('/api/validation/dtd-schema', { pxml_content: text, dtd_family: fam })); }
    catch (err) { console.error('Failed to fetch DTD schema:', err); }
  }, []);

  const validate = useCallback(async (text: string, fam: DtdFamily = familyRef.current, withRules = fullRef.current) => {
    if (!text.trim()) return;
    setValidating(true);
    try {
      setResult(await postJson(`/api/validation/validate-dtd${withRules ? '?full=true' : ''}`, { pxml_content: text, dtd_family: fam }));
    } catch (err) {
      setResult(API_ERROR_RESULT(String((err as Error)?.message || err)));
    } finally {
      setValidating(false);
    }
  }, []);

  const loadText = useCallback((text: string, name: string | null) => {
    contentRef.current = text;
    setContent(text);
    setFileName(name);
    setResult(null);
    fetchSchema(text, paletteFamilyRef.current);
    validate(text);
  }, [fetchSchema, validate]);

  const readFile = useCallback((file: File | null | undefined) => {
    if (!file) return;
    file.text().then((t) => loadText(t, file.name));
  }, [loadText]);

  const changeFamily = (fam: DtdFamily) => {
    familyRef.current = fam;
    setFamily(fam);
    if (contentRef.current) validate(contentRef.current, fam);
  };

  const changePaletteFamily = (fam: DtdFamily) => {
    paletteFamilyRef.current = fam;
    setPaletteFamily(fam);
    fetchSchema(contentRef.current, fam);
  };

  const changeFull = (v: boolean) => {
    fullRef.current = v;
    setFull(v);
    if (contentRef.current) validate(contentRef.current, familyRef.current, v);
  };

  const hasDoc = content.trim().length > 0;

  return (
    <div className="app">
      <div className="app-header">
        <h1>🧬 DTD Validator</h1>
        <span style={{ fontSize: 12, opacity: 0.7 }}>
          {fileName ? `📄 ${fileName}` : 'Upload a PXML — no DOCX or Excel needed'}
        </span>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 12, alignItems: 'center', fontSize: 12 }}>
          <label title="Which DTD to validate against">
            Validate against{' '}
            <select value={family} onChange={(e) => changeFamily(e.target.value as DtdFamily)}>
              <option value="both">Both ({region.dtd_families.filter((f) => f.selectable).map((f) => f.label).join(' + ') || 'all'})</option>
              <option value="auto">Declared DTD only (from DOCTYPE)</option>
              {region.dtd_families.filter((f) => f.selectable).map((f) => <option key={f.id} value={f.id}>{f.label}</option>)}
            </select>
          </label>
          <label title="Also run the citation, heading, form, footnote, attribute and reference rules (slower on large files)">
            <input type="checkbox" checked={full} onChange={(e) => changeFull(e.target.checked)} /> Include business rules
          </label>
          <button className="btn-theme-toggle" onClick={() => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))}
            title={theme === 'dark' ? 'Switch to Light Mode' : 'Switch to Dark Mode'}>
            {theme === 'dark' ? '☀️' : '🌙'}
          </button>
        </div>
      </div>

      <input ref={fileRef} type="file" accept=".xml,.pxml,.txt" hidden onChange={(e) => { readFile(e.target.files?.[0]); e.target.value = ''; }} />

      {!hasDoc ? (
        <div
          className="recent-sessions-view"
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => { e.preventDefault(); setDragOver(false); readFile(e.dataTransfer.files?.[0]); }}
        >
          <div className="sessions-intro">
            <h2>Validate a PXML against the DTD</h2>
            <p>Drop a PXML / XML file here or browse. It is checked for well-formedness and DTD conformance
              ({region.dtd_families.filter((f) => f.selectable).map((f) => f.label).join(' or ') || 'DTD'}, picked from its DOCTYPE or chosen above), and you can fix issues right in the editor.</p>
            <div
              className="file-upload-bar"
              style={{ cursor: 'pointer', padding: 32, border: `2px dashed ${dragOver ? '#89b4fa' : '#6c7086'}`, borderRadius: 10, marginTop: 16, textAlign: 'center' }}
              onClick={() => fileRef.current?.click()}
            >
              📂 Click to browse, or drop a .pxml / .xml file
            </div>
          </div>
        </div>
      ) : (
        <div className="panels-container">
          <div className="panel panel-right grow" style={{ borderLeft: 'none' }}>
            <PxmlEditor
              key={fileName || 'doc'}
              onPxmlLoaded={(text) => loadText(text, fileName)}
              onPxmlSaved={(text) => { contentRef.current = text; setContent(text); }}
              matches={[]}
              isSearching={false}
              initialContent={content}
              initialFileName={fileName}
              dtdResult={result}
              dtdValidating={validating}
              onValidateDtd={(text) => { contentRef.current = text; validate(text); }}
              onRepair={(text) => postJson('/api/validation/repair', { pxml_content: text })}
              onFix={(text, line, msg) => postJson('/api/validation/fix', { pxml_content: text, error_line: line, error_msg: msg })}
              onFixAll={(text, rule) => postJson('/api/validation/fix-all', { pxml_content: text, rule })}
              onClearDtd={() => setResult(null)}
              dtdSchema={schema}
              dtdFamily={paletteFamily}
              onDtdFamilyChange={changePaletteFamily}
            />
          </div>
        </div>
      )}
    </div>
  );
}
