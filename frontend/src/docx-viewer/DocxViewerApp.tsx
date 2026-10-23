import { useState, useCallback, useEffect, useRef, useMemo } from 'react';
import FileUpload from './components/FileUpload';
import TrackChangesPanel from './components/TrackChangesPanel';
import DocxViewer from './components/DocxViewer';
import PdfViewer from './components/PdfViewer';
import PxmlEditor, { type PxmlEditorApi } from './components/PxmlEditor';
import type { DtdResult, DtdSchema, DtdFamily } from './components/PxmlEditor';
import DraftPanel from './components/DraftPanel';
import PanelPortal from './components/PanelPortal';
import { parseDocxFile } from './docxParser';
import { parseExcelFile, matchExcelToTrackChanges } from './excelParser';
import type { ParsedDocx, TrackChange, ChunkMatch, ExcelValidationMap, ExcelMetaMap, ValidityFilter } from './types';
// eslint-disable-next-line @typescript-eslint/no-unused-vars
import type { MatchDetail } from './excelParser';
import './DocxViewerApp.css';
import { API_BASE } from '../services/basePath';

const API_URL = API_BASE;

type ViewerMode = 'docx' | 'pdf';

interface SessionMeta {
  session_id: string;
  session_name: string;
  created_at: number;
  updated_at: number;
  docx_filename: string;
  pxml_filename: string;
  excel_filename: string | null;
  pdf_filename: string | null;
  current_revision_index: number;
  total_revisions: number;
  total_invalid: number;
  revision_statuses: Record<string, string>;
  completed_count?: number;
  progress_text?: string;
}

interface ProcessedJob {
  session_id: string;
  date: string;
  time: string;
  product: string;
  run_outcome: string;
  download_available: boolean;
  validation_done?: boolean;
}

function DocxViewerApp() {
  const [parsedDocx, setParsedDocx] = useState<ParsedDocx | null>(null);
  const [docxFile, setDocxFile] = useState<File | null>(null);
  const [fileName, setFileName] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [selectedChange, setSelectedChange] = useState<TrackChange | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pxmlContent, setPxmlContent] = useState<string>('');
  const [matches, setMatches] = useState<ChunkMatch[]>([]);
  const [isSearching, setIsSearching] = useState(false);
  const [viewerMode, setViewerMode] = useState<ViewerMode>('pdf');
  // Left pane view — the Revisions navigator or the Draft (Failed Revisions) list.
  // Both auto-load from the session Excel; they live in the SAME pane (tabbed),
  // rather than the Draft popping out as a separate column.
  const [leftTab, setLeftTab] = useState<'revisions' | 'draft'>('revisions');

  // Excel / filter state
  const [validationMap, setValidationMap] = useState<ExcelValidationMap>({});
  const [metaMap, setMetaMap] = useState<ExcelMetaMap>({});
  const [matchDetails, setMatchDetails] = useState<MatchDetail[]>([]);
  const [excelFileName, setExcelFileName] = useState<string | null>(null);
  const [validityFilter, setValidityFilter] = useState<ValidityFilter>('all');
  const [unmappedRevisions, setUnmappedRevisions] = useState<TrackChange[]>([]);

  // Session state
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [currentRevisionIndex, setCurrentRevisionIndex] = useState(0);
  const [revisionStatuses, setRevisionStatuses] = useState<Record<string, string>>({});
  const [recentSessions, setRecentSessions] = useState<SessionMeta[]>([]);
  const [processedJobs, setProcessedJobs] = useState<ProcessedJob[]>([]);
  const [loadingJobId, setLoadingJobId] = useState<string | null>(null);
  const [showRecentSessions, setShowRecentSessions] = useState(true);
  const [savingPxml, setSavingPxml] = useState(false);
  const [sessionPdfUrl, setSessionPdfUrl] = useState<string | null>(null);
  // DTD (structural) validation of the current PXML
  const [dtdResult, setDtdResult] = useState<DtdResult | null>(null);
  const [dtdValidating, setDtdValidating] = useState(false);
  // The DTD's own element/content-model map, for the tag-insertion palette.
  // Fetched once per load (see handlePxmlLoaded) — the DTD is static, so the
  // server caches the parse; only the transfer repeats on reload.
  const [dtdSchema, setDtdSchema] = useState<DtdSchema | null>(null);
  const [dtdFamily, setDtdFamily] = useState<DtdFamily>('auto');
  // Panel collapse (give the PXML editor + validation room during validation)
  const [leftCollapsed, setLeftCollapsed] = useState(false);
  const [centerCollapsed, setCenterCollapsed] = useState(false);
  // Pop-out: detach a panel into a separate browser window so testers with two
  // monitors can drag it onto the second screen. The detached panel stays live
  // (same session/state) because it is rendered through a React portal.
  const [poppedOut, setPoppedOut] = useState<{ left: boolean; center: boolean; right: boolean }>({
    left: false,
    center: false,
    right: false,
  });
  // Inline slots a panel occupies while docked. PanelPortal relocates the live
  // panel between these hosts and the detached windows without remounting it.
  const leftHostRef = useRef<HTMLDivElement | null>(null);
  const centerHostRef = useRef<HTMLDivElement | null>(null);
  const rightHostRef = useRef<HTMLDivElement | null>(null);
  const popOut = useCallback((panel: 'left' | 'center' | 'right') => {
    setPoppedOut(prev => ({ ...prev, [panel]: true }));
    // One-time tip: pop-outs open in whatever browser runs the app. Show it the
    // first time a tester pops a panel out, then remember the dismissal.
    try {
      if (!localStorage.getItem('vv-popout-tip-seen')) {
        localStorage.setItem('vv-popout-tip-seen', '1');
        setPopoutTip(true);
      }
    } catch { /* ignore */ }
  }, []);
  const dockBack = useCallback((panel: 'left' | 'center' | 'right') => {
    setPoppedOut(prev => ({ ...prev, [panel]: false }));
  }, []);
  const [popoutTip, setPopoutTip] = useState(false);
  const popoutBrowser = useMemo(() => {
    const ua = navigator.userAgent;
    if (/Edg\//.test(ua)) return 'Microsoft Edge';
    if (/OPR\//.test(ua) || /Opera/.test(ua)) return 'Opera';
    if (/Firefox\//.test(ua)) return 'Firefox';
    if (/Chrome\//.test(ua)) return 'Google Chrome';
    if (/Safari\//.test(ua)) return 'Safari';
    return 'your current browser';
  }, []);
  const creatingSessionRef = useRef(false);
  // Ref always holds latest pxmlContent — used in search callbacks to avoid stale closures
  const pxmlContentRef = useRef<string>('');
  // Live editor access for the resolver agent (read text / apply a verified edit).
  const editorApiRef = useRef<PxmlEditorApi | null>(null);
  // Store the actual Excel File object so it can be included when session is created
  const excelFileRef = useRef<File | null>(null);

  // Theme toggle (dark / light)
  const [theme, setTheme] = useState<'dark' | 'light'>(() => {
    try { return (localStorage.getItem('vv-theme') as 'dark' | 'light') || 'light'; } catch { return 'light'; }
  });
  useEffect(() => { document.documentElement.setAttribute('data-theme', theme); localStorage.setItem('vv-theme', theme); }, [theme]);



  // Resizable left panel
  const [leftPanelWidth, setLeftPanelWidth] = useState(320);
  const isDragging = useRef(false);
  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    isDragging.current = true;
    const startX = e.clientX;
    const startW = leftPanelWidth;
    const onMove = (ev: MouseEvent) => {
      if (!isDragging.current) return;
      const newW = Math.min(Math.max(startW + (ev.clientX - startX), 220), 600);
      setLeftPanelWidth(newW);
    };
    const onUp = () => { isDragging.current = false; document.removeEventListener('mousemove', onMove); document.removeEventListener('mouseup', onUp); };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  }, [leftPanelWidth]);

  // Resizable right (PXML) panel
  const [rightPanelWidth, setRightPanelWidth] = useState(380);
  const isDraggingRight = useRef(false);
  const handleRightMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    isDraggingRight.current = true;
    const startX = e.clientX;
    const startW = rightPanelWidth;
    const onMove = (ev: MouseEvent) => {
      if (!isDraggingRight.current) return;
      // Dragging left grows the panel (startX - ev.clientX), dragging right shrinks it
      const newW = Math.min(Math.max(startW + (startX - ev.clientX), 280), 900);
      setRightPanelWidth(newW);
    };
    const onUp = () => { isDraggingRight.current = false; document.removeEventListener('mousemove', onMove); document.removeEventListener('mouseup', onUp); };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  }, [rightPanelWidth]);

  const [pxmlFileName, setPxmlFileName] = useState<string | null>(null);

  // Get emp_id from localStorage (set by main app's AuthContext on login)
  const getEmpId = (): string => {
    try {
      const stored = localStorage.getItem('user_profile');
      if (stored) {
        const profile = JSON.parse(stored);
        if (profile.emp_id) return profile.emp_id;
      }
    } catch { /* ignore */ }
    // Fallback: ?emp_id= query param (viewer opened directly, not via main app)
    try { return new URLSearchParams(window.location.search).get('emp_id') || ''; } catch { return ''; }
  };

  // Load recent sessions on mount + check URL for ?session=xxx to auto-resume
  useEffect(() => {
    fetchRecentSessions();
    fetchProcessedJobs();

    // Check if URL has a session parameter to auto-resume
    const params = new URLSearchParams(window.location.search);
    const urlSessionId = params.get('session');
    if (urlSessionId) {
      // Fetch session meta and auto-resume
      fetch(`${API_URL}/api/validation/session/${urlSessionId}`)
        .then(res => { if (res.ok) return res.json(); throw new Error('not found'); })
        .then((meta: SessionMeta) => { resumeSession(meta); })
        .catch(() => { /* session not found, show landing */ });
    }
  }, []);

  const fetchRecentSessions = async () => {
    try {
      const empId = getEmpId();
      const qs = empId ? `?emp_id=${encodeURIComponent(empId)}` : '';
      const res = await fetch(`${API_URL}/api/validation/sessions${qs}`);
      if (res.ok) {
        const data = await res.json();
        setRecentSessions(data.sessions || []);
      }
    } catch { /* ignore */ }
  };

  // Fetch the user's processed pipeline jobs that have output available to load.
  const fetchProcessedJobs = async () => {
    try {
      const empId = getEmpId();
      if (!empId) return;
      const res = await fetch(`${API_URL}/api/auth/recent-jobs?emp_id=${encodeURIComponent(empId)}`);
      if (res.ok) {
        const rows = await res.json();
        setProcessedJobs((Array.isArray(rows) ? rows : []).filter((r: ProcessedJob) => r.download_available));
      }
    } catch { /* ignore */ }
  };

  // Build a validation session from a processed job and open it in the viewer.
  const loadJob = async (jobId: string) => {
    setLoadingJobId(jobId);
    setError(null);
    try {
      const res = await fetch(`${API_URL}/api/validation/create-session-from-job`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ job_id: jobId, emp_id: getEmpId() }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d?.detail || `HTTP ${res.status}`);
      }
      const meta: SessionMeta = await res.json();
      await resumeSession(meta);
    } catch (e: any) {
      setError(`Could not load job ${jobId}: ${e?.message || e}`);
    } finally {
      setLoadingJobId(null);
    }
  };

  // Auto-create session once all 3 mandatory files are ready (DOCX + PXML + Excel)
  useEffect(() => {
    if (sessionId) return; // Already have a session
    if (!docxFile || !pxmlContentRef.current || !excelFileRef.current) return;
    // All 3 files ready — create the session
    const pxmlFile = new File([pxmlContentRef.current], fileName?.replace('.docx', '.pxml') || 'document.pxml');
    const excel = excelFileRef.current;
    excelFileRef.current = null;
    createSession(docxFile, pxmlFile, excel);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docxFile, excelFileName, pxmlContent, sessionId]);

  // Create a session on the backend when files are uploaded
  const createSession = async (docx: File, pxml: File, excel?: File, pdf?: File) => {
    if (creatingSessionRef.current) return null; // Prevent duplicate session creation
    creatingSessionRef.current = true;

    const form = new FormData();
    form.append('docx_file', docx);
    form.append('pxml_file', pxml);
    if (excel) form.append('excel_file', excel);
    if (pdf) form.append('pdf_file', pdf);
    form.append('session_name', docx.name);
    form.append('emp_id', getEmpId());

    try {
      const res = await fetch(`${API_URL}/api/validation/create-session`, { method: 'POST', body: form });
      if (res.ok) {
        const meta: SessionMeta = await res.json();
        setSessionId(meta.session_id);
        setRevisionStatuses(meta.revision_statuses || {});
        setCurrentRevisionIndex(meta.current_revision_index || 0);
        setShowRecentSessions(false);
        localStorage.setItem('lastSessionId', meta.session_id);
        return meta;
      }
    } catch (err) {
      console.error('Failed to create session:', err);
    } finally {
      creatingSessionRef.current = false;
    }
    return null;
  };

  // Resume a session
  const resumeSession = async (session: SessionMeta) => {
    setIsLoading(true);
    setError(null);
    setShowRecentSessions(false);

    // Re-fetch latest session metadata — background PDF conversion may have updated pdf_filename
    try {
      const freshRes = await fetch(`${API_URL}/api/validation/session/${session.session_id}?emp_id=${getEmpId()}`);
      if (freshRes.ok) {
        const fresh: SessionMeta = await freshRes.json();
        Object.assign(session, fresh);
      }
    } catch { /* use provided meta */ }

    setSessionId(session.session_id);
    setRevisionStatuses(session.revision_statuses || {});
    setCurrentRevisionIndex(session.current_revision_index || 0);
    localStorage.setItem('lastSessionId', session.session_id);

    // Reset PXML state so PxmlEditor gets clean initial props on each resume
    setPxmlContent('');
    pxmlContentRef.current = '';
    setPxmlFileName(null);

    // Set session PDF URL BEFORE loading the DOCX so that PdfViewer's conversion
    // effect sees it and skips re-conversion when docxFile state updates.
    if (session.pdf_filename) {
      const pdfSessionUrl = `${API_URL}/api/validation/session/${session.session_id}/file/${session.pdf_filename}`;
      setSessionPdfUrl(pdfSessionUrl);
    } else {
      setSessionPdfUrl(null);
    }

    try {
      // Load DOCX
      let docxResult: ParsedDocx | null = null;
      const docxRes = await fetch(`${API_URL}/api/validation/session/${session.session_id}/file/${session.docx_filename}`);
      if (docxRes.ok) {
        const blob = await docxRes.blob();
        const file = new File([blob], session.docx_filename, { type: blob.type });
        const result = await parseDocxFile(file);
        docxResult = result;
        setParsedDocx(result);
        setDocxFile(file);
        setFileName(session.docx_filename);

        // Auto-select the current revision. current_revision_index indexes the panel's
        // visible list (formatting changes hidden), so select from the same filtered view.
        const visibleAtResume = result.trackChanges.filter((tc) => tc.type !== 'formatChange');
        if (visibleAtResume[session.current_revision_index]) {
          setSelectedChange(visibleAtResume[session.current_revision_index]);
        }

        // Report total revisions to backend
        await fetch(`${API_URL}/api/validation/set-total-revisions`, {
          method: 'POST',
          body: new URLSearchParams({ session_id: session.session_id, total_revisions: String(result.trackChanges.length) }),
        });
      }

      // Load PXML
      if (session.pxml_filename) {
        const pxmlRes = await fetch(`${API_URL}/api/validation/session/${session.session_id}/file/${session.pxml_filename}`);
        if (pxmlRes.ok) {
          const text = await pxmlRes.text();
          setPxmlContent(text);
          pxmlContentRef.current = text;
          setPxmlFileName(session.pxml_filename);
          handleFetchDtdSchema(text);
        }
      }

      // Load Excel if present
      if (session.excel_filename) {
        const excelRes = await fetch(`${API_URL}/api/validation/session/${session.session_id}/file/${session.excel_filename}`);
        if (excelRes.ok) {
          const blob = await excelRes.blob();
          const file = new File([blob], session.excel_filename, { type: blob.type });
          setExcelFileName(session.excel_filename);
          // Parse and match against the docx we just loaded
          if (docxResult) {
            const rows = await parseExcelFile(file);
            const { validationMap: vMap, metaMap: mMap, matchDetails: details, unmappedRevisions: unmapped } = matchExcelToTrackChanges(rows, docxResult.trackChanges);
            setValidationMap(vMap);
            setMetaMap(mMap);
            setMatchDetails(details);
            setUnmappedRevisions(unmapped);

            // Send invalid count to backend so session card shows completed/invalid
            const invalidCount = Object.values(vMap).filter(v => v === false).length;
            await fetch(`${API_URL}/api/validation/set-total-revisions`, {
              method: 'POST',
              body: new URLSearchParams({
                session_id: session.session_id,
                total_revisions: String(docxResult.trackChanges.length),
                total_invalid: String(invalidCount),
              }),
            });
          }
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to resume session');
    } finally {
      setIsLoading(false);
    }
  };

  const handleFileSelect = useCallback(async (file: File) => {
    setIsLoading(true);
    setError(null);
    setSelectedChange(null);
    setMatches([]);

    try {
      const result = await parseDocxFile(file);
      setParsedDocx(result);
      setDocxFile(file);
      setFileName(file.name);
      setShowRecentSessions(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to parse DOCX file');
      setParsedDocx(null);
      setDocxFile(null);
      setFileName(null);
    } finally {
      setIsLoading(false);
    }
  }, []);

  const handleExcelSelect = useCallback(async (file: File) => {
    if (!parsedDocx) {
      setError('Please upload a DOCX file first.');
      return;
    }
    try {
      setError(null);
      const excelRows = await parseExcelFile(file);
      const { validationMap: vMap, metaMap: mMap, matchDetails: details, unmappedRevisions: unmapped } = matchExcelToTrackChanges(excelRows, parsedDocx.trackChanges);
      setValidationMap(vMap);
      setMetaMap(mMap);
      setMatchDetails(details);
      setUnmappedRevisions(unmapped);
      setExcelFileName(file.name);

      // Store the Excel file for session creation
      excelFileRef.current = file;

      // If session already exists (e.g. resume), upload Excel to it directly
      if (sessionId) {
        const excelForm = new FormData();
        excelForm.append('file', file);
        fetch(`${API_URL}/api/validation/session/${sessionId}/upload-file`, {
          method: 'POST',
          body: excelForm,
        }).then(res => {
          if (!res.ok) console.error('[EXCEL] Upload failed:', res.status);
          else console.log('[EXCEL] Uploaded to session', sessionId);
        }).catch(err => console.error('[EXCEL] Upload error:', err));

        // Also send invalid count
        const invalidCount = Object.values(vMap).filter(v => v === false).length;
        fetch(`${API_URL}/api/validation/set-total-revisions`, {
          method: 'POST',
          body: new URLSearchParams({
            session_id: sessionId,
            total_revisions: String(parsedDocx.trackChanges.length),
            total_invalid: String(invalidCount),
          }),
        }).catch(() => {});
      }
      // If no session yet, the useEffect will auto-create once all 3 files are ready
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to parse Excel file');
    }
  }, [parsedDocx, sessionId]);

  // The DTD's element/content-model map for the tag-insertion palette — fetched
  // once per load rather than on every keystroke (mirrors handleValidateDtd's
  // pattern, just automatic instead of button-triggered, so the palette is
  // ready the moment the user starts editing). Silently no-ops on failure —
  // the palette itself degrades to "unavailable" rather than erroring the editor.
  const dtdFamilyRef = useRef<DtdFamily>('auto');
  const handleFetchDtdSchema = useCallback(async (content: string, family: DtdFamily = dtdFamilyRef.current) => {
    if (!content.trim()) return;
    try {
      const res = await fetch(`${API_URL}/api/validation/dtd-schema`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pxml_content: content, dtd_family: family }),
      });
      if (!res.ok) return;
      setDtdSchema(await res.json());
    } catch (err) {
      console.error('Failed to fetch DTD schema:', err);
    }
  }, []);

  // The palette's PAX/PIX switch: remember the choice (so a later reload of the
  // same editor keeps it) and refetch that family's tags/attributes/entities.
  // Roughly where in the XML a piece of Word text lives (the server's fuzzy search): used to pick the right
  // occurrence when a phrase appears more than once.
  const searchHint = useCallback(async (context: string): Promise<number | undefined> => {
    const pxml = pxmlContentRef.current;
    if (!pxml || !context.trim()) return undefined;
    try {
      const res = await fetch(`${API_URL}/api/search`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ context_text: context, pxml_content: pxml, chunk_size: 500, overlap_percent: 0.3 }),
      });
      if (!res.ok) return undefined;
      return (await res.json()).matches?.[0]?.start_index;
    } catch {
      return undefined;
    }
  }, []);

  // Put text into the XML as a tracked insertion. With an `anchor` (the Word text just before it) the spot is found
  // automatically; without one it goes at the editor cursor. The text is entity-encoded by the server with the same
  // rules the pipeline uses (&sect;, &ldquo; ...). Returns an error message, or null on success.
  const insertTextIntoXml = useCallback(async (text: string, anchor?: string): Promise<string | null> => {
    const api = editorApiRef.current;
    if (!api) return 'Load the XML in the editor first.';
    let encoded = text;
    try {
      const res = await fetch(`${API_URL}/api/xml/encode-text`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      });
      if (!res.ok) throw new Error(String(res.status));
      encoded = (await res.json()).xml;
    } catch {
      return 'Could not prepare the text for the XML. Check the server is running.';
    }
    if (anchor) {
      const err = api.locateInsertion(anchor, await searchHint(anchor));
      if (err) return err;
    }
    return api.insertTracked(encoded);
  }, [searchHint]);

  // Jump the XML editor to a revision: the cursor after the text that precedes an insertion, or the deleted text selected.
  const locateRevisionInXml = useCallback(async (change: TrackChange): Promise<string | null> => {
    const api = editorApiRef.current;
    if (!api) return 'Load the XML in the editor first.';
    if (change.type === 'deletion') {
      const hint = await searchHint([change.prevContext, change.text, change.postContext].filter(Boolean).join(' '));
      return api.locateText(change.prevContext, change.text, hint);
    }
    const hint = await searchHint([change.prevContext.slice(-200), change.postContext.slice(0, 100)].filter(Boolean).join(' '));
    return api.locateInsertion(change.prevContext, hint);
  }, [searchHint]);

  const markDeletedInXml = useCallback((): string | null => editorApiRef.current?.markSelectionDeleted() ?? 'Load the XML in the editor first.', []);

  const handleDtdFamilyChange = useCallback((family: DtdFamily) => {
    dtdFamilyRef.current = family;
    setDtdFamily(family);
    handleFetchDtdSchema(pxmlContentRef.current, family);
  }, [handleFetchDtdSchema]);

  const handlePxmlLoaded = useCallback((content: string) => {
    setPxmlContent(content);
    pxmlContentRef.current = content;
    handleFetchDtdSchema(content);
    // Session creation is handled by the useEffect that watches all 3 files
  }, [handleFetchDtdSchema]);

  // Save PXML to backend
  const handleSavePxml = useCallback(async (content: string) => {
    setPxmlContent(content);
    pxmlContentRef.current = content;
    if (!sessionId) return;
    setSavingPxml(true);
    try {
      await fetch(`${API_URL}/api/validation/save-pxml`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId, pxml_content: content }),
      });
    } catch (err) {
      console.error('Failed to save PXML:', err);
    } finally {
      setSavingPxml(false);
    }
  }, [sessionId]);

  // Validate the current PXML against the PAX DTD (structural check).
  const handleValidateDtd = useCallback(async (content: string) => {
    setDtdValidating(true);
    try {
      const res = await fetch(`${API_URL}/api/validation/validate-dtd`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pxml_content: content, session_id: sessionId }),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(detail?.detail || `HTTP ${res.status}`);
      }
      setDtdResult((await res.json()) as DtdResult);
      // Focus the validation view: collapse the Revisions sidebar so the errors +
      // editor get the space. (Document panel stays under the user's manual toggle.)
      setLeftCollapsed(true);
    } catch (err) {
      console.error('DTD validation failed:', err);
      setDtdResult({
        available: false,
        wellformed: false,
        validation_passed: false,
        dtd_label: null,
        error_count: 0,
        warning_count: 0,
        errors: [{
          line: 0, lines: [], severity: 'error', rule: 'SERVER',
          title: 'DTD validation service unavailable',
          message: String((err as Error)?.message || err),
          fix: '', location: '', count: 1,
        }],
      });
    } finally {
      setDtdValidating(false);
    }
  }, [sessionId]);

  // Validator actions — each posts the editor's current text and returns JSON;
  // PxmlEditor applies returned text and auto-revalidates.
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

  const handleRepair = useCallback(
    (content: string) => postJson('/api/validation/repair', { pxml_content: content }),
    []);

  const handleContentCheck = useCallback((content: string) => {
    // Compare against the REVISED source: keep insertions/normal text, drop tracked
    // deletions (deleted text is meant to be absent from the output, so including it
    // would falsely report it as "missing").
    const inputText = (parsedDocx?.paragraphs ?? [])
      .map((p) => p.runs
        .filter((r) => r.type !== 'deletion' && r.type !== 'moveFrom')
        .map((r) => r.text).join(''))
      .join('\n');
    return postJson('/api/validation/coverage', { pxml_content: content, input_text: inputText });
  }, [parsedDocx]);

  const handleFix = useCallback(
    (content: string, errorLine: number, errorMsg: string) =>
      postJson('/api/validation/fix', { pxml_content: content, error_line: errorLine, error_msg: errorMsg }),
    []);

  const handleFixAll = useCallback(
    (content: string, rule: string) => postJson('/api/validation/fix-all', { pxml_content: content, rule }),
    []);

  // The list actually shown in the Revisions panel (formatting changes always hidden,
  // plus the validity filter). currentRevisionIndex indexes INTO this list, so every
  // selectedChange assignment and navigation must use the SAME list — otherwise the
  // highlighted change drifts from the displayed one (e.g. under the "Invalid" filter).
  const filteredTrackChanges = useMemo(() => (parsedDocx?.trackChanges ?? []).filter((tc) => {
    if (tc.type === 'formatChange') return false;
    if (validityFilter === 'all') return true;
    if (!(tc.id in validationMap)) return false;
    if (validityFilter === 'true') return validationMap[tc.id] === true;
    if (validityFilter === 'false') return validationMap[tc.id] === false;
    return true;
  }), [parsedDocx, validationMap, validityFilter]);

  const handleSelectChange = useCallback((change: TrackChange) => {
    setSelectedChange((prev) => (prev?.id === change.id ? null : change));
  }, []);

  // Changing the filter re-indexes the list — reset to the first item so the displayed
  // and highlighted revision stay in sync.
  const handleFilterChange = useCallback((filter: ValidityFilter) => {
    setValidityFilter(filter);
    setCurrentRevisionIndex(0);
    setSelectedChange(null);
  }, []);

  // Navigation
  const handleNavigate = useCallback((index: number) => {
    setCurrentRevisionIndex(index);
    // Persist position to backend
    if (sessionId) {
      fetch(`${API_URL}/api/validation/update-position`, {
        method: 'POST',
        body: new URLSearchParams({ session_id: sessionId, current_revision_index: String(index) }),
      }).catch(() => {});
    }
  }, [sessionId]);

  // ─── False positives ───────────────────────────────────────────────────────
  // A validator flags a revision the checker wrongly called invalid; the backend logs
  // it (shared log + this job's copy) so a developer can see which ones recur.
  const [falsePositives, setFalsePositives] = useState<Record<string, string>>({});
  const fpKeyOf = useCallback((tc: { paragraphIndex: number; text: string }) => {
    let h = 5381;
    for (let i = 0; i < tc.text.length; i++) h = ((h << 5) + h + tc.text.charCodeAt(i)) | 0;
    return `${tc.paragraphIndex}:${(h >>> 0).toString(36)}`;
  }, []);
  const fpUser = () => {
    try { return JSON.parse(localStorage.getItem('user_profile') || '{}') as { emp_id?: string; name?: string }; }
    catch { return {}; }
  };

  useEffect(() => {
    setFalsePositives({});
    if (!sessionId) return;
    fetch(`${API_URL}/api/validation/false-positives?session_id=${encodeURIComponent(sessionId)}`)
      .then((r) => (r.ok ? r.json() : { flags: [] }))
      .then((d) => {
        const m: Record<string, string> = {};
        (d.flags || []).forEach((f: { revision_key: string; severity: string }) => { m[f.revision_key] = f.severity; });
        setFalsePositives(m);
      })
      .catch(() => {});
  }, [sessionId]);

  const excelRowOf = (tc: { id: string }) => matchDetails.find((d) => d.trackChangeId === tc.id)?.rowIndex ?? -1;

  const handleFalsePositive = useCallback(async (tc: TrackChange, index: number, severity: string, note: string) => {
    if (!sessionId) throw new Error('Open a session first');
    const key = fpKeyOf(tc);
    const u = fpUser();
    const res = await fetch(`${API_URL}/api/validation/false-positive`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: sessionId,
        revision_key: key,
        revision_index: index,
        excel_row: excelRowOf(tc),
        revision_type: tc.type,
        revision_text: tc.text,
        previous_context: tc.prevContext,
        paragraph_index: tc.paragraphIndex,
        validator_reason: metaMap[tc.id]?.reason || '',
        suggested_fix: metaMap[tc.id]?.suggestedFix || '',
        severity,
        note,
        emp_id: u.emp_id || '',
        user_name: u.name || '',
        doc_name: docxFile?.name || fileName || '',
      }),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({})))?.detail || `HTTP ${res.status}`);
    setFalsePositives((prev) => ({ ...prev, [key]: severity }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, metaMap, matchDetails, docxFile, fileName, fpKeyOf]);

  const handleUnflagFalsePositive = useCallback(async (tc: TrackChange) => {
    if (!sessionId) return;
    const key = fpKeyOf(tc);
    const u = fpUser();
    const qs = new URLSearchParams({ session_id: sessionId, revision_key: key, emp_id: u.emp_id || '', excel_row: String(excelRowOf(tc)) });
    const res = await fetch(`${API_URL}/api/validation/false-positive?${qs}`, { method: 'DELETE' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    setFalsePositives((prev) => { const n = { ...prev }; delete n[key]; return n; });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, matchDetails, fpKeyOf]);

  // Mark revision complete
  const handleMarkComplete = useCallback(async (index: number) => {
    const newStatuses = { ...revisionStatuses, [String(index)]: 'complete' };
    setRevisionStatuses(newStatuses);

    // Advance to next — index INTO the filtered list the panel shows.
    const total = filteredTrackChanges.length;
    let nextIdx = index + 1;
    while (nextIdx < total && newStatuses[String(nextIdx)] === 'complete') nextIdx++;
    if (nextIdx < total) {
      setCurrentRevisionIndex(nextIdx);
      if (filteredTrackChanges[nextIdx]) {
        setSelectedChange(filteredTrackChanges[nextIdx]);
      }
    }

    if (sessionId) {
      try {
        await fetch(`${API_URL}/api/validation/update-revision-status`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: sessionId, revision_index: index, status: 'complete' }),
        });
      } catch { /* ignore */ }
    }
  }, [revisionStatuses, sessionId, filteredTrackChanges]);

  // Mark revision skipped
  const handleMarkSkipped = useCallback(async (index: number) => {
    const newStatuses = { ...revisionStatuses, [String(index)]: 'skipped' };
    setRevisionStatuses(newStatuses);

    const total = filteredTrackChanges.length;
    let nextIdx = index + 1;
    while (nextIdx < total && (newStatuses[String(nextIdx)] === 'complete' || newStatuses[String(nextIdx)] === 'skipped')) nextIdx++;
    if (nextIdx < total) {
      setCurrentRevisionIndex(nextIdx);
      if (filteredTrackChanges[nextIdx]) {
        setSelectedChange(filteredTrackChanges[nextIdx]);
      }
    }

    if (sessionId) {
      try {
        await fetch(`${API_URL}/api/validation/update-revision-status`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: sessionId, revision_index: index, status: 'skipped' }),
        });
      } catch { /* ignore */ }
    }
  }, [revisionStatuses, sessionId, filteredTrackChanges]);

  const handleParagraphClick = useCallback(async (paragraphIndex: number) => {
    const currentPxml = pxmlContentRef.current;
    if (!parsedDocx || !currentPxml) return;
    const contextText = parsedDocx.paragraphs[paragraphIndex].runs.map((r) => r.text).join('');
    if (!contextText.trim()) return;

    setIsSearching(true);
    setMatches([]);
    try {
      const response = await fetch(`${API_URL}/api/search`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ context_text: contextText, pxml_content: currentPxml, chunk_size: 500, overlap_percent: 0.3 }),
      });
      if (!response.ok) throw new Error('Backend search failed');
      const data = await response.json();
      setMatches(data.matches || []);
    } catch (err) {
      console.error('Search error:', err);
      setMatches([]);
    } finally {
      setIsSearching(false);
    }
  }, [parsedDocx]);

  const handleTextSearch = useCallback(async (text: string) => {
    const currentPxml = pxmlContentRef.current;
    if (!currentPxml || !text.trim()) return;
    setIsSearching(true);
    setMatches([]);
    try {
      const response = await fetch(`${API_URL}/api/search`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ context_text: text, pxml_content: currentPxml, chunk_size: 500, overlap_percent: 0.3 }),
      });
      if (!response.ok) throw new Error('Backend search failed');
      const data = await response.json();
      setMatches(data.matches || []);
    } catch (err) {
      console.error('Text search error:', err);
      setMatches([]);
    } finally {
      setIsSearching(false);
    }
  }, []);

  // Draft panel item click — jump to that failed revision in the PDF (via a synthetic
  // TrackChange that drives PdfViewer navigation) and also fuzzy-search the PXML for it.
  const handleDraftNavigate = useCallback(async (text: string, prevContext: string, postContext: string) => {
    const syntheticChange: TrackChange = {
      id: `draft-${Date.now()}`,
      type: 'deletion',
      author: 'Draft',
      date: '',
      text: text.substring(0, 200),
      paragraphIndex: 0,
      runIndex: 0,
      prevContext: prevContext || '',
      postContext: postContext || '',
      paragraphText: text,
      previousPara: '',
      postPara: '',
    };
    setSelectedChange(syntheticChange);
    setViewerMode('pdf');   // show the PDF so navigation is visible
    setCenterCollapsed(false);

    const currentPxml = pxmlContentRef.current;
    if (!currentPxml) return;
    const searchText = [prevContext, text.substring(0, 200), postContext].filter(Boolean).join(' ');
    if (!searchText.trim()) return;

    setIsSearching(true);
    setMatches([]);
    try {
      const response = await fetch(`${API_URL}/api/search`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ context_text: searchText, pxml_content: currentPxml, chunk_size: 500, overlap_percent: 0.3 }),
      });
      if (!response.ok) throw new Error('Backend search failed');
      const data = await response.json();
      setMatches(data.matches || []);
    } catch (err) {
      console.error('Draft navigation search error:', err);
      setMatches([]);
    } finally {
      setIsSearching(false);
    }
  }, []);

  // Locate a snippet of source text in the DOCX view and scroll to it (used by
  // Content Check — "missing" text lives in the source document).
  const locateInDoc = useCallback((text: string) => {
    if (!parsedDocx || !text) return;
    const norm = (s: string) => s.toLowerCase().replace(/&[a-z]+;/g, ' ').replace(/[^a-z0-9 ]/g, ' ').replace(/\s+/g, ' ').trim();
    const words = norm(text).split(' ').filter(Boolean);
    if (!words.length) return;
    // Pre-normalize every paragraph once, then try progressively shorter/among needles.
    const paras = parsedDocx.paragraphs.map((p) => ({ p, t: norm(p.runs.map((r) => r.text).join('')) }));
    const tryNeedle = (n: string) => (n ? paras.find((x) => x.t.includes(n))?.p : undefined);
    const para =
      tryNeedle(words.slice(0, 8).join(' ')) ||
      tryNeedle(words.slice(0, 5).join(' ')) ||
      tryNeedle(words.slice(0, 3).join(' ')) ||
      tryNeedle(words.slice(Math.max(0, Math.floor(words.length / 2) - 2), Math.floor(words.length / 2) + 3).join(' '));
    if (!para) return;
    setCenterCollapsed(false);   // make sure the document panel is visible
    setViewerMode('docx');
    setTimeout(() => {
      const el = document.getElementById(`para-${para.paragraphIndex}`);
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        el.classList.add('para-flash');
        setTimeout(() => el.classList.remove('para-flash'), 2200);
      }
    }, 60);
  }, [parsedDocx]);

  // Widen the right panel when the errors/coverage box appears so the editor keeps a
  // usable width (the errors box docks beside the editor inside the right panel).
  const ensureValidationWidth = useCallback(() => {
    setRightPanelWidth((w) => {
      const target = Math.min(Math.round(window.innerWidth * 0.55), window.innerWidth - 340);
      return Math.max(w, target);
    });
  }, []);

  // filteredTrackChanges is defined above (useMemo) so navigation handlers can share it.

  // ─── Recent Sessions View ────────────────────
  if (showRecentSessions && !parsedDocx) {
    return (
      <div className="app">
        <div className="app-header">
          <h1>📋 Visual Validator</h1>
          <FileUpload onFileSelect={handleFileSelect} isLoading={isLoading} fileName={fileName} />
        </div>
        {error && <div className="error-bar">{error}</div>}
        <div className="recent-sessions-view">
          <div className="sessions-intro">
            <h2>Welcome Back</h2>
            <p>Load a processed file, resume a previous session, or upload new files to start.</p>
            {processedJobs.length > 0 && (
              <div className="load-job-row">
                <label htmlFor="load-job">📂 Load a processed file:</label>
                <select
                  id="load-job"
                  className="load-job-select"
                  value=""
                  disabled={!!loadingJobId}
                  onChange={(e) => { if (e.target.value) loadJob(e.target.value); }}
                >
                  <option value="">
                    {loadingJobId ? 'Loading…' : `Select from ${processedJobs.length} processed file(s)…`}
                  </option>
                  {processedJobs.map((j) => (
                    <option key={j.session_id} value={j.session_id}>
                      {j.product || 'file'} · {j.date} {j.time} · {j.run_outcome}
                      {j.validation_done ? ' · validated' : ''}
                    </option>
                  ))}
                </select>
              </div>
            )}
          </div>
          {recentSessions.length > 0 ? (
            <div className="sessions-grid">
              {recentSessions.map((s) => (
                <div key={s.session_id} className="session-card" onClick={() => resumeSession(s)}>
                  <div className="session-card-header">
                    <span className="session-name">{s.session_name}</span>
                    <span className="session-progress">{s.progress_text || '0/0'}</span>
                  </div>
                  <div className="session-card-meta">
                    <span>📄 {s.docx_filename}</span>
                    {s.excel_filename && <span>📊 {s.excel_filename}</span>}
                  </div>
                  <div className="session-card-footer">
                    <span className="session-date">
                      Last edited: {new Date(s.updated_at * 1000).toLocaleDateString()} {new Date(s.updated_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="no-sessions">
              <p>No recent sessions. Upload a DOCX file to begin.</p>
            </div>
          )}
        </div>
      </div>
    );
  }

  // ─── Main Viewer ─────────────────────────────
  // Each panel's content is rendered via a helper so it can live either inline
  // or inside a detached pop-out window (second-monitor support). `inline`
  // toggles the in-app controls (Pop out / Hide) that don't belong in the
  // detached window, where the pop-out window supplies its own "Dock back".
  const renderLeftPanel = (inline: boolean) => (
    <>
      {/* Tabs: both views live in this one pane — Draft no longer pops out as a
          separate column, and its Excel auto-loads from the session just like the
          validation report does. */}
      <div className="left-tabs">
        <button
          className={`left-tab ${leftTab === 'revisions' ? 'active' : ''}`}
          onClick={() => setLeftTab('revisions')}
        >
          📋 Revisions
        </button>
        <button
          className={`left-tab ${leftTab === 'draft' ? 'active' : ''}`}
          onClick={() => setLeftTab('draft')}
        >
          📝 Failed Revisions
        </button>
        {inline && (
          <>
            <button
              className="left-tab-popout"
              onClick={() => popOut('left')}
              title="Open this panel in a separate window — drag it to your other screen"
            >
              ⧉ Pop out
            </button>
            <button className="left-tab-hide" onClick={() => setLeftCollapsed(true)} title="Hide this panel">
              ⟨ Hide
            </button>
          </>
        )}
      </div>
      {leftTab === 'revisions' ? (
        <TrackChangesPanel
          trackChanges={filteredTrackChanges}
          allTrackChangesCount={parsedDocx?.trackChanges.length ?? 0}
          stats={parsedDocx?.stats ?? null}
          formatRevisions={parsedDocx?.formatRevisions ?? []}
          falsePositives={falsePositives}
          fpKeyOf={fpKeyOf}
          canFlag={!!sessionId}
          onFalsePositive={handleFalsePositive}
          onUnflagFalsePositive={handleUnflagFalsePositive}
          onSelectChange={handleSelectChange}
          onInsertText={insertTextIntoXml}
          onLocateInXml={locateRevisionInXml}
          onMarkDeleted={markDeletedInXml}
          editorApiRef={editorApiRef}
          dtdFamily={dtdFamily}
          sessionId={sessionId}
          selectedChangeId={selectedChange?.id ?? null}
          validityFilter={validityFilter}
          onFilterChange={handleFilterChange}
          validationMap={validationMap}
          metaMap={metaMap}
          onExcelSelect={handleExcelSelect}
          excelFileName={excelFileName}
          matchDetails={matchDetails}
          unmappedRevisions={unmappedRevisions}
          currentRevisionIndex={currentRevisionIndex}
          onNavigate={handleNavigate}
          revisionStatuses={revisionStatuses}
          onMarkComplete={handleMarkComplete}
          onMarkSkipped={handleMarkSkipped}
        />
      ) : (
        <DraftPanel onNavigate={handleDraftNavigate} sessionId={sessionId} editorApiRef={editorApiRef} dtdFamily={dtdFamily} />
      )}
    </>
  );

  const renderCenterPanel = (inline: boolean) => (
    <>
      {inline && (
        <>
          <button
            className="panel-popout-btn"
            onClick={() => popOut('center')}
            title="Open this panel in a separate window — drag it to your other screen"
          >
            ⧉ Pop out
          </button>
          <button className="panel-collapse-btn" onClick={() => setCenterCollapsed(true)}
            title="Hide the Document panel">⟨ Hide</button>
        </>
      )}
      <div style={{ display: viewerMode === 'docx' ? 'contents' : 'none' }}>
        <DocxViewer
          paragraphs={parsedDocx?.paragraphs ?? []}
          selectedChange={selectedChange}
          onParagraphClick={handleParagraphClick}
        />
      </div>
      <div style={{ display: viewerMode === 'pdf' ? 'contents' : 'none' }}>
        <PdfViewer
          selectedChange={selectedChange}
          onTextSearch={handleTextSearch}
          onInsertText={insertTextIntoXml}
          docxFile={docxFile}
          sessionPdfUrl={sessionPdfUrl}
          sessionId={sessionId}
          isVisible={viewerMode === 'pdf'}
        />
      </div>
    </>
  );

  const renderRightPanel = (inline: boolean) => (
    <>
      {inline && (
        <button
          className="panel-popout-btn right"
          onClick={() => popOut('right')}
          title="Open this panel in a separate window — drag it to your other screen"
        >
          ⧉ Pop out
        </button>
      )}
      <PxmlEditor
        onPxmlLoaded={handlePxmlLoaded}
        onPxmlSaved={handleSavePxml}
        matches={matches}
        isSearching={isSearching}
        sessionId={sessionId}
        initialContent={pxmlContent}
        initialFileName={pxmlFileName}
        dtdResult={dtdResult}
        dtdValidating={dtdValidating}
        onValidateDtd={handleValidateDtd}
        onRepair={handleRepair}
        onContentCheck={handleContentCheck}
        onFix={handleFix}
        onFixAll={handleFixAll}
        onLocateInDoc={locateInDoc}
        onEnsureWidth={ensureValidationWidth}
        onClearMatches={() => setMatches([])}
        onClearDtd={() => setDtdResult(null)}
        dtdSchema={dtdSchema}
        dtdFamily={dtdFamily}
        onDtdFamilyChange={handleDtdFamilyChange}
        editorApiRef={editorApiRef}
      />
    </>
  );

  // Layout bookkeeping that accounts for collapsed AND popped-out panels.
  const leftInline = !leftCollapsed && !poppedOut.left;
  const centerInline = !centerCollapsed && !poppedOut.center;
  const rightInline = !poppedOut.right;
  // When the Document panel is detached, let the PXML editor grow to fill its gap.
  const rightGrow = (centerCollapsed || poppedOut.center) && rightInline;
  const showLeftCenterHandle = leftInline && centerInline;
  const showCenterRightHandle = centerInline && rightInline && !rightGrow;
  // Nothing flexible left in the row? add a spacer so fixed panels don't stretch.
  const needsFlexSpacer = !(centerInline || rightGrow);

  return (
    <div className="app">
      <div className="app-header">
        <h1>📋 Visual Validator</h1>
        <FileUpload onFileSelect={handleFileSelect} isLoading={isLoading} fileName={fileName} />
        <div className="viewer-mode-toggle">
          <label className={`mode-option ${viewerMode === 'docx' ? 'active' : ''}`}>
            <input type="checkbox" checked={viewerMode === 'docx'} onChange={() => setViewerMode('docx')} />
            <span>📝 DOCX</span>
          </label>
          <label className={`mode-option ${viewerMode === 'pdf' ? 'active' : ''}`}>
            <input type="checkbox" checked={viewerMode === 'pdf'} onChange={() => setViewerMode('pdf')} />
            <span>📄 PDF</span>
          </label>
        </div>
        <button
          className="btn-theme-toggle"
          onClick={() => setTheme(t => t === 'dark' ? 'light' : 'dark')}
          title={theme === 'dark' ? 'Switch to Light Mode' : 'Switch to Dark Mode'}
        >
          {theme === 'dark' ? '☀️' : '🌙'}
        </button>
        {sessionId && (
          <span className="session-indicator" title={`Session: ${sessionId}`}>
            {savingPxml ? '💾 Saving...' : '🟢 Saved'}
          </span>
        )}
        <button className="btn-sessions" onClick={() => { setShowRecentSessions(true); setParsedDocx(null); setPxmlContent(''); pxmlContentRef.current = ''; setPxmlFileName(null); }} title="Back to sessions">
          📂
        </button>
      </div>

      {error && <div className="error-bar">{error}</div>}

      {/* File readiness banner — shown until all 3 mandatory files are uploaded and session is created */}
      {!sessionId && parsedDocx && (
        <div className="file-readiness-banner">
          <span className="readiness-title">⚠️ Upload all 3 files to start a session:</span>
          <span className={`readiness-item ${docxFile ? 'ready' : 'pending'}`}>{docxFile ? '✅' : '⬜'} DOCX</span>
          <span className={`readiness-item ${pxmlContent ? 'ready' : 'pending'}`}>{pxmlContent ? '✅' : '⬜'} PXML</span>
          <span className={`readiness-item ${excelFileName ? 'ready' : 'pending'}`}>{excelFileName ? '✅' : '⬜'} Excel</span>
        </div>
      )}

      <div className="panels-container">
        {/* LEFT — Revisions / Failed Revisions */}
        {leftCollapsed && (
          <div className="panel-rail" onClick={() => setLeftCollapsed(false)} title="Show Revisions panel">
            <span>▸ Revisions</span>
          </div>
        )}
        {!leftCollapsed && poppedOut.left && (
          <div className="panel-rail panel-rail-popped" onClick={() => dockBack('left')}
            title="Revisions panel is open in a separate window — click to dock it back">
            <span>⧉ Revisions ↗</span>
          </div>
        )}
        {leftInline && (
          <div ref={leftHostRef} className="panel panel-left" style={{ width: leftPanelWidth, minWidth: 220 }} />
        )}

        {showLeftCenterHandle && (
          <div className="panel-resize-handle" onMouseDown={handleMouseDown} />
        )}

        {/* CENTER — Document (DOCX / PDF) */}
        {centerCollapsed && (
          <div className="panel-rail" onClick={() => setCenterCollapsed(false)} title="Show Document panel">
            <span>▸ Document</span>
          </div>
        )}
        {!centerCollapsed && poppedOut.center && (
          <div className="panel-rail panel-rail-popped" onClick={() => dockBack('center')}
            title="Document panel is open in a separate window — click to dock it back">
            <span>⧉ Document ↗</span>
          </div>
        )}
        {centerInline && (
          <div ref={centerHostRef} className="panel panel-center" />
        )}

        {showCenterRightHandle && (
          <div className="panel-resize-handle" onMouseDown={handleRightMouseDown} />
        )}

        {/* RIGHT — PXML editor */}
        {poppedOut.right && (
          <div className="panel-rail panel-rail-popped" onClick={() => dockBack('right')}
            title="PXML editor is open in a separate window — click to dock it back">
            <span>⧉ PXML ↗</span>
          </div>
        )}
        {rightInline && (
          <div ref={rightHostRef} className={`panel panel-right${rightGrow ? ' grow' : ''}`}
            style={rightGrow ? undefined : { width: rightPanelWidth, minWidth: 280 }} />
        )}

        {needsFlexSpacer && <div className="panel-flex-spacer" />}
      </div>

      {/* Panels live in a stable container that PanelPortal relocates between the
          inline hosts above and detached windows (second-monitor support) WITHOUT
          remounting — so loaded files, the PDF/CodeMirror state and scroll position
          all persist across pop-out / dock-back. */}
      <PanelPortal
        popped={poppedOut.left}
        hostRef={leftHostRef}
        title="Revisions"
        theme={theme}
        onClose={() => dockBack('left')}
        width={560}
        height={900}
      >
        {renderLeftPanel(!poppedOut.left)}
      </PanelPortal>
      <PanelPortal
        popped={poppedOut.center}
        hostRef={centerHostRef}
        title="Document"
        theme={theme}
        onClose={() => dockBack('center')}
        width={1100}
        height={900}
      >
        {renderCenterPanel(!poppedOut.center)}
      </PanelPortal>
      <PanelPortal
        popped={poppedOut.right}
        hostRef={rightHostRef}
        title="PXML Editor"
        theme={theme}
        onClose={() => dockBack('right')}
        width={900}
        height={900}
      >
        {renderRightPanel(!poppedOut.right)}
      </PanelPortal>

      {popoutTip && (
        <div className="popout-tip" role="status">
          <span className="popout-tip-icon">💡</span>
          <div className="popout-tip-body">
            <strong>Pop-outs open in {popoutBrowser}.</strong>
            <span>
              A detached panel always opens in the same browser as this app. To use a
              different browser, open the Visual Validator there before popping out.
            </span>
          </div>
          <button className="popout-tip-close" onClick={() => setPopoutTip(false)} title="Dismiss this tip">
            Got it
          </button>
        </div>
      )}
    </div>
  );
}

export default DocxViewerApp;
