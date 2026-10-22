import { useState, useCallback } from 'react';
import FileUpload from './components/FileUpload';
import TrackChangesPanel from './components/TrackChangesPanel';
import DocxViewer from './components/DocxViewer';
import PxmlEditor from './components/PxmlEditor';
import DraftPanel from './components/DraftPanel';
import { parseDocxFile } from './docxParser';
import { parseExcelFile, matchExcelToTrackChanges } from './excelParser';
import type { ParsedDocx, TrackChange, ChunkMatch, ExcelValidationMap, ExcelMetaMap, ValidityFilter } from './types';
// eslint-disable-next-line @typescript-eslint/no-unused-vars
import type { MatchDetail } from './excelParser';
import './App.css';

const API_URL = 'http://localhost:8000';

function App() {
  const [parsedDocx, setParsedDocx] = useState<ParsedDocx | null>(null);
  const [docxFile, setDocxFile] = useState<File | null>(null);
  const [fileName, setFileName] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [selectedChange, setSelectedChange] = useState<TrackChange | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pxmlContent, setPxmlContent] = useState<string>('');
  const [matches, setMatches] = useState<ChunkMatch[]>([]);
  const [isSearching, setIsSearching] = useState(false);

  // Excel / filter state
  const [validationMap, setValidationMap] = useState<ExcelValidationMap>({});
  const [metaMap, setMetaMap] = useState<ExcelMetaMap>({});
  const [matchDetails, setMatchDetails] = useState<MatchDetail[]>([]);
  const [excelFileName, setExcelFileName] = useState<string | null>(null);
  const [validityFilter, setValidityFilter] = useState<ValidityFilter>('all');
  const [unmappedRevisions, setUnmappedRevisions] = useState<TrackChange[]>([]);

  // Panel visibility state
  const [showDraftPanel, setShowDraftPanel] = useState(false);
  const [showRevisionPanel, setShowRevisionPanel] = useState(true);

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

      // Re-run Excel matching if we already have Excel data loaded
      if (matchDetails.length > 0) {
        // We need to re-match with new track changes
        // matchDetails has the excel rows info
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to parse DOCX file');
      setParsedDocx(null);
      setFileName(null);
    } finally {
      setIsLoading(false);
    }
  }, [matchDetails.length]);

  const handleExcelSelect = useCallback(async (file: File) => {
    if (!parsedDocx) {
      setError('Please upload a DOCX file first before uploading the Excel file.');
      return;
    }

    try {
      setError(null);
      const excelRows = await parseExcelFile(file);
      const { validationMap: vMap, metaMap: mMap, matchDetails: details, unmappedRevisions: unmapped } = matchExcelToTrackChanges(
        excelRows,
        parsedDocx.trackChanges
      );

      setValidationMap(vMap);
      setMetaMap(mMap);
      setMatchDetails(details);
      setUnmappedRevisions(unmapped);
      setExcelFileName(file.name);

      const matched = details.filter((d) => d.matched).length;
      const total = details.length;
      console.log(`Excel matching: ${matched}/${total} rows matched to track changes`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to parse Excel file');
    }
  }, [parsedDocx]);

  const handlePxmlLoaded = useCallback((content: string) => {
    setPxmlContent(content);
  }, []);

  const handleSelectChange = useCallback((change: TrackChange) => {
    setSelectedChange((prev) => (prev?.id === change.id ? null : change));
  }, []);

  const handleParagraphClick = useCallback(async (paragraphIndex: number) => {
    if (!parsedDocx || !pxmlContent) return;

    const contextText = parsedDocx.paragraphs[paragraphIndex].runs.map((r) => r.text).join('');
    if (!contextText.trim()) return;

    setIsSearching(true);
    setMatches([]);

    try {
      const response = await fetch(`${API_URL}/api/search`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          context_text: contextText,
          pxml_content: pxmlContent,
          chunk_size: 500,
          overlap_percent: 0.3,
        }),
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
  }, [parsedDocx, pxmlContent]);

  // Direct text search in PXML — triggered by drag-selecting text on PDF
  const handleTextSearch = useCallback(async (text: string) => {
    if (!pxmlContent) return;
    if (!text.trim()) return;

    setIsSearching(true);
    setMatches([]);

    try {
      const response = await fetch(`${API_URL}/api/search`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          context_text: text,
          pxml_content: pxmlContent,
          chunk_size: 500,
          overlap_percent: 0.3,
        }),
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
  }, [pxmlContent]);

  // Handle Draft panel item click — navigate in PDF using text search
  const handleDraftNavigate = useCallback(async (text: string, prevContext: string, postContext: string) => {
    // 1. Create a synthetic TrackChange to trigger PDF page navigation in DocxViewer
    const syntheticChange: TrackChange = {
      id: `draft-${Date.now()}`,
      type: 'inline_deletion',
      author: 'Draft',
      date: '',
      text: text.substring(0, 200),
      paragraphIndex: 0,
      runIndex: 0,
      revisionOrder: 0,
      prevContext: prevContext || '',
      postContext: postContext || '',
      paragraphText: text,
      previousPara: '',
      postPara: '',
    };
    setSelectedChange(syntheticChange);

    // 2. Also search PXML if content is available
    if (!pxmlContent) return;
    const searchText = [prevContext, text.substring(0, 200), postContext].filter(Boolean).join(' ');
    if (!searchText.trim()) return;

    setIsSearching(true);
    setMatches([]);

    try {
      const response = await fetch(`${API_URL}/api/search`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          context_text: searchText,
          pxml_content: pxmlContent,
          chunk_size: 500,
          overlap_percent: 0.3,
        }),
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
  }, [pxmlContent]);

  // Filter track changes based on validity filter + validation map
  const filteredTrackChanges = (parsedDocx?.trackChanges ?? []).filter((tc) => {
    if (validityFilter === 'all') return true;
    // If this track change has no mapping from Excel, show it only on 'all'
    if (!(tc.id in validationMap)) return false;
    if (validityFilter === 'true') return validationMap[tc.id] === true;
    if (validityFilter === 'false') return validationMap[tc.id] === false;
    return true;
  });

  return (
    <div className="app">
      <div className="app-header">
        <h1>DOCX Viewer V2</h1>
        <FileUpload
          onFileSelect={handleFileSelect}
          isLoading={isLoading}
          fileName={fileName}
        />
        <button
          className={`btn-draft ${showDraftPanel ? 'active' : ''}`}
          onClick={() => setShowDraftPanel((v) => !v)}
          title="Show/Hide Draft — Failed Revisions"
        >
          📝 Draft
        </button>
      </div>

      {error && (
        <div className="error-bar">
          {error}
        </div>
      )}

      <div className="panels-container">
        {showDraftPanel && (
          <div className="panel panel-draft">
            <DraftPanel onNavigate={handleDraftNavigate} />
          </div>
        )}
        {showRevisionPanel ? (
          <div className="panel panel-left">
            <button
              className="panel-hide-btn panel-hide-btn-right"
              onClick={() => setShowRevisionPanel(false)}
              title="Hide Revisions"
            >
              ◀
            </button>
            <TrackChangesPanel
              trackChanges={filteredTrackChanges}
              allTrackChangesCount={parsedDocx?.trackChanges.length ?? 0}
              onSelectChange={handleSelectChange}
              selectedChangeId={selectedChange?.id ?? null}
              validityFilter={validityFilter}
              onFilterChange={setValidityFilter}
              validationMap={validationMap}
              metaMap={metaMap}
              onExcelSelect={handleExcelSelect}
              excelFileName={excelFileName}
              matchDetails={matchDetails}
              unmappedRevisions={unmappedRevisions}
            />
          </div>
        ) : (
          <div
            className="panel-collapsed-tab panel-collapsed-tab-left"
            onClick={() => setShowRevisionPanel(true)}
            title="Show Revisions"
          >
            <span>📋 Revisions</span>
          </div>
        )}
        <div className="panel panel-center">
          <DocxViewer
            paragraphs={parsedDocx?.paragraphs ?? []}
            selectedChange={selectedChange}
            onTextSearch={handleTextSearch}
            docxFile={docxFile}
            footnotes={parsedDocx?.footnotes ?? {}}
            endnotes={parsedDocx?.endnotes ?? {}}
          />
        </div>
        <div className="panel panel-right">
          <PxmlEditor
            onPxmlLoaded={handlePxmlLoaded}
            matches={matches}
            isSearching={isSearching}
          />
        </div>
      </div>
    </div>
  );
}

export default App;
