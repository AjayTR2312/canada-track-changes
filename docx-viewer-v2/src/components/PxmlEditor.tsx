import React, { useEffect, useRef, useState } from 'react';
import type { ChunkMatch } from '../types';
import { EditorState, Compartment, StateField, StateEffect } from '@codemirror/state';
import {
  EditorView,
  keymap,
  lineNumbers,
  highlightActiveLine,
  highlightActiveLineGutter,
  drawSelection,
  Decoration,
} from '@codemirror/view';
import type { DecorationSet } from '@codemirror/view';
import { defaultKeymap, history, historyKeymap } from '@codemirror/commands';
import { xml } from '@codemirror/lang-xml';
import {
  syntaxHighlighting,
  HighlightStyle,
  bracketMatching,
  indentOnInput,
} from '@codemirror/language';
import { tags as t } from '@lezer/highlight';
import './PxmlEditor.css';

interface Props {
  onPxmlLoaded: (content: string) => void;
  matches: ChunkMatch[];
  isSearching: boolean;
}

/**
* Pretty-print XML by indenting tag-to-tag boundaries only.
* Text content (mixed content) is preserved exactly — we only break between a
* '>' and a following '<' (optionally separated by whitespace), so inline text
* like <name>John</name> stays on one line.
*
* A single line can legitimately contain several tags (mixed content), e.g.
*   <bold>Note:</bold> see <cite>          (one complete pair + one open)
*   </cite> volume.</para.text>            (two closing tags on one line)
* so we count EVERY tag on the line and track the net depth change rather than
* assuming one open/close per line. The previous version looked only at the
* first/last tag, so lines that closed more than one element under-counted the
* dedent and the indentation drifted deeper and deeper down the document.
*/
function formatXmlString(xmlText: string): string {
  const PAD = '  ';
  const normalized = xmlText.replace(/>\s*</g, '>\n<').trim();
  const lines = normalized.split('\n');

  // Matches one tag: open / close / self-closing element, or declaration/PI/comment.
  const TAG_RE = /<\/?[\w.:-]+(?:\s[^>]*?)?\/?>|<[?!][^>]*?>/g;

  let depth = 0;
  const out: string[] = [];

  for (const raw of lines) {
    const line = raw.trim();
    if (!line) continue;

    // Walk every tag so multiple opens/closes on one line are all accounted
    // for. `cur` is the running depth; `min` is the shallowest level reached,
    // which is where the line itself is indented (so a leading </close> lines
    // up with its opener instead of with its content).
    let cur = depth;
    let min = depth;
    TAG_RE.lastIndex = 0;
    let m: RegExpExecArray | null;
    while ((m = TAG_RE.exec(line)) !== null) {
      const tag = m[0];
      if (tag.startsWith('</')) {
        cur -= 1;
        if (cur < min) min = cur;
      } else if (tag.startsWith('<?') || tag.startsWith('<!') || tag.endsWith('/>')) {
        // declaration / processing instruction / comment / self-closing: no change
      } else {
        cur += 1;
      }
    }

    out.push(PAD.repeat(Math.max(0, min)) + line);
    depth = Math.max(0, cur);
  }

  return out.join('\n');
}

/* ------------------------------------------------------------------ *
* CodeMirror configuration (module-level: independent of component)  *
* ------------------------------------------------------------------ */

// Catppuccin Mocha syntax colors mapped onto the XML language's lezer tags.
const pxmlHighlight = HighlightStyle.define([
  { tag: t.tagName, color: '#89b4fa', fontWeight: '500' },
  { tag: t.angleBracket, color: '#6c7086' },
  { tag: t.attributeName, color: '#f9e2af' },
  { tag: [t.attributeValue, t.string], color: '#a6e3a1' },
  { tag: t.comment, color: '#585b70', fontStyle: 'italic' },
  { tag: t.processingInstruction, color: '#cba6f7' },
  { tag: t.content, color: '#cdd6f4' },
  { tag: t.meta, color: '#cba6f7' },
]);

// Panel theme (dark) — background, caret, gutters, selection and the custom
// decoration classes used for search matches and trace tags.
const pxmlTheme = EditorView.theme(
  {
    '&': {
      color: '#cdd6f4',
      backgroundColor: '#11111b',
      height: '100%',
      fontSize: '12.5px',
    },
    '.cm-scroller': {
      fontFamily: "'Consolas', 'Fira Code', monospace",
      lineHeight: '1.6',
      overflow: 'auto',
    },
    '.cm-content': {
      fontFamily: "'Consolas', 'Fira Code', monospace",
      caretColor: '#f38ba8',
      padding: '8px 0',
    },
    '.cm-cursor, .cm-dropCursor': {
      borderLeftColor: '#f38ba8',
      borderLeftWidth: '2px',
    },
    '&.cm-focused .cm-cursor': { borderLeftColor: '#f38ba8' },
    '.cm-selectionBackground, .cm-content ::selection': {
      backgroundColor: '#585b7066',
    },
    '&.cm-focused .cm-selectionBackground': { backgroundColor: '#585b7088' },
    '.cm-gutters': {
      backgroundColor: '#181825',
      color: '#45475a',
      border: 'none',
      borderRight: '1px solid #313244',
    },
    '.cm-activeLineGutter': { backgroundColor: '#1e1e2e', color: '#cdd6f4' },
    '.cm-activeLine': { backgroundColor: '#1e1e2e55' },
    '.cm-pxml-match': {
      backgroundColor: '#89b4fa44',
      outline: '1px solid #89b4fa',
      borderRadius: '2px',
    },
    '.cm-pxml-trace': {
      color: '#a6e3a1',
      fontWeight: '700',
      backgroundColor: '#a6e3a122',
      borderRadius: '3px',
    },
    '.cm-pxml-trace-del': {
      color: '#f38ba8',
      fontWeight: '700',
      backgroundColor: '#f38ba822',
      borderRadius: '3px',
    },
  },
  { dark: true }
);

// --- Search-match highlight (controlled by a StateEffect) ---
const setMatch = StateEffect.define<{ from: number; to: number } | null>();
const matchMark = Decoration.mark({ class: 'cm-pxml-match' });

const matchField = StateField.define<DecorationSet>({
  create() {
    return Decoration.none;
  },
  update(deco, tr) {
    deco = deco.map(tr.changes);
    for (const e of tr.effects) {
      if (e.is(setMatch)) {
        deco =
          e.value && e.value.to > e.value.from
            ? Decoration.set([matchMark.range(e.value.from, e.value.to)])
            : Decoration.none;
      }
    }
    return deco;
  },
  provide: (f) => EditorView.decorations.from(f),
});

// --- Trace-tag coloring (recomputed whenever the doc changes) ---
const traceMark = Decoration.mark({ class: 'cm-pxml-trace' });
const traceDelMark = Decoration.mark({ class: 'cm-pxml-trace-del' });
const TRACE_RE = /<trace\.deleted\s*\/>|<\/?trace\s*\/?>/g;

function buildTraceDecorations(doc: string): DecorationSet {
  const ranges = [];
  let m: RegExpExecArray | null;
  TRACE_RE.lastIndex = 0;
  while ((m = TRACE_RE.exec(doc))) {
    const from = m.index;
    const to = from + m[0].length;
    const mark = m[0].includes('trace.deleted') ? traceDelMark : traceMark;
    ranges.push(mark.range(from, to));
  }
  return Decoration.set(ranges, true);
}

const traceField = StateField.define<DecorationSet>({
  create(state) {
    return buildTraceDecorations(state.doc.toString());
  },
  update(deco, tr) {
    if (tr.docChanged) return buildTraceDecorations(tr.state.doc.toString());
    return deco.map(tr.changes);
  },
  provide: (f) => EditorView.decorations.from(f),
});

/* ------------------------------------------------------------------ */

const PxmlEditor: React.FC<Props> = ({ onPxmlLoaded, matches, isSearching }) => {
  const [fileName, setFileName] = useState<string | null>(null);
  const [isEditing, setIsEditing] = useState(false);
  const [activeMatch, setActiveMatch] = useState<ChunkMatch | null>(null);
  const [isMaximized, setIsMaximized] = useState(false);
  const [matchesHidden, setMatchesHidden] = useState(false);
  // Custom left edge (px) for the expanded panel; null = default right-half.
  const [panelLeft, setPanelLeft] = useState<number | null>(null);

  const inputRef = useRef<HTMLInputElement>(null);
  const hostRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const editComp = useRef(new Compartment());
  const isEditingRef = useRef(false);
  const preEditRef = useRef<string>('');
  const pendingDocRef = useRef<string>('');
  const onPxmlLoadedRef = useRef(onPxmlLoaded);

  // Keep the latest onPxmlLoaded without re-creating the editor.
  useEffect(() => {
    onPxmlLoadedRef.current = onPxmlLoaded;
  }, [onPxmlLoaded]);

  // Build the extension list (closes over component refs).
  const buildExtensions = () => [
    lineNumbers(),
    highlightActiveLine(),
    highlightActiveLineGutter(),
    drawSelection(),
    history(),
    indentOnInput(),
    bracketMatching(),
    xml(),
    syntaxHighlighting(pxmlHighlight),
    pxmlTheme,
    EditorView.lineWrapping,
    traceField,
    matchField,
    keymap.of([...defaultKeymap, ...historyKeymap]),
    editComp.current.of(EditorState.readOnly.of(true)),
    EditorView.domEventHandlers({
      dblclick: () => {
        // In view (read-only) mode a double-click enters edit mode. CodeMirror
        // has already placed the caret/selection at the click, so editing
        // continues exactly where the user clicked — no manual offset math.
        if (!isEditingRef.current && viewRef.current) {
          preEditRef.current = viewRef.current.state.doc.toString();
          setIsEditing(true);
        }
        return false;
      },
    }),
  ];

  // Create the EditorView once the host is mounted (i.e. a file is loaded).
  useEffect(() => {
    if (!fileName || !hostRef.current || viewRef.current) return;
    viewRef.current = new EditorView({
      doc: pendingDocRef.current,
      parent: hostRef.current,
      extensions: buildExtensions(),
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fileName]);

  // Destroy the view on unmount.
  useEffect(() => {
    return () => {
      viewRef.current?.destroy();
      viewRef.current = null;
    };
  }, []);

  // Toggle read-only when switching between view and edit mode.
  useEffect(() => {
    isEditingRef.current = isEditing;
    const view = viewRef.current;
    if (!view) return;
    view.dispatch({
      effects: editComp.current.reconfigure(EditorState.readOnly.of(!isEditing)),
    });
    if (isEditing) view.focus();
  }, [isEditing]);

  // Apply / clear the search-match highlight and scroll it into view.
  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    if (!activeMatch) {
      view.dispatch({ effects: setMatch.of(null) });
      return;
    }
    const len = view.state.doc.length;
    const from = Math.max(0, Math.min(activeMatch.start_index, len));
    const to = Math.max(from, Math.min(activeMatch.end_index, len));
    view.dispatch({
      effects: [
        setMatch.of(to > from ? { from, to } : null),
        EditorView.scrollIntoView(from, { y: 'center' }),
      ],
    });
  }, [activeMatch]);

  // Re-measure after the panel resizes (maximize / restore / width drag).
  useEffect(() => {
    const view = viewRef.current;
    if (view) requestAnimationFrame(() => view.requestMeasure());
  }, [isMaximized, panelLeft]);

  /* ----------------------------- actions ----------------------------- */

  const loadContent = (content: string, name: string) => {
    const normalized = content.replace(/\r\n?/g, '\n');
    let formatted = normalized;
    try {
      formatted = formatXmlString(normalized);
    } catch {
      formatted = normalized;
    }
    pendingDocRef.current = formatted;
    setActiveMatch(null);
    setIsEditing(false);
    setFileName(name);
    onPxmlLoadedRef.current(formatted);

    const view = viewRef.current;
    if (view) {
      view.dispatch({
        changes: { from: 0, to: view.state.doc.length, insert: formatted },
        effects: [
          editComp.current.reconfigure(EditorState.readOnly.of(true)),
          setMatch.of(null),
        ],
      });
    }
  };

  const handleFileUpload = async (file: File) => {
    const text = await file.text();
    loadContent(text, file.name);
  };

  const handleInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) handleFileUpload(file);
  };

  const handleDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    const file = e.dataTransfer.files?.[0];
    if (file && (file.name.endsWith('.pxml') || file.name.endsWith('.xml'))) {
      handleFileUpload(file);
    }
  };

  const handleDragOver = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
  };

  const enterEdit = () => {
    const view = viewRef.current;
    if (view) preEditRef.current = view.state.doc.toString();
    setActiveMatch(null);
    setIsEditing(true);
  };

  const handleSave = () => {
    const view = viewRef.current;
    if (!view) return;
    const content = view.state.doc.toString();
    onPxmlLoadedRef.current(content);
    setIsEditing(false);
  };

  const handleCancel = () => {
    const view = viewRef.current;
    if (view) {
      view.dispatch({
        changes: { from: 0, to: view.state.doc.length, insert: preEditRef.current },
      });
    }
    setIsEditing(false);
  };

  const handleMatchClick = (match: ChunkMatch) => {
    setIsEditing(false);
    setActiveMatch(match);
  };

  const handleDownload = () => {
    const view = viewRef.current;
    const content = view ? view.state.doc.toString() : pendingDocRef.current;
    const blob = new Blob([content], { type: 'application/xml' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    const base = fileName ? fileName.replace(/\.(pxml|xml)$/i, '') : 'edited';
    a.download = `${base}_edited.pxml`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  /* -------------------- expand / resize the panel ------------------- */

  // Reset any custom width whenever we expand or restore.
  const toggleMaximize = () => {
    setPanelLeft(null);
    setIsMaximized((v) => !v);
  };

  // Drag the LEFT edge of the expanded panel to make it wider (drag left) or
  // narrower (drag right). No-op while docked.
  const handleResizeMouseDown = (e: React.MouseEvent) => {
    if (!isMaximized) return;
    e.preventDefault();
    const MIN_WIDTH = 320; // narrowest the editor may become
    const MIN_LEFT = 120;  // widest (closest to the left screen edge)
    const onMove = (ev: MouseEvent) => {
      const maxLeft = window.innerWidth - MIN_WIDTH;
      const left = Math.max(MIN_LEFT, Math.min(ev.clientX, maxLeft));
      setPanelLeft(left);
    };
    const onUp = () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      document.body.style.cursor = '';
    };
    document.body.style.cursor = 'col-resize';
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  };

  /* ------------------------------- view ------------------------------ */

  if (!fileName) {
    return (
      <div className={`pxml-editor ${isMaximized ? 'maximized' : ''}`}>
        <div className="panel-header">
          <h3>PXML Editor</h3>
        </div>
        <div
          className="editor-empty"
          onClick={() => inputRef.current?.click()}
          onDrop={handleDrop}
          onDragOver={handleDragOver}
        >
          <input
            ref={inputRef}
            type="file"
            accept=".pxml,.xml"
            onChange={handleInputChange}
            hidden
          />
          <div className="empty-icon">XML</div>
          <p>Click or drag and drop a .pxml / .xml file here</p>
          <p className="hint">This panel is for your final PXML file</p>
        </div>
      </div>
    );
  }

  return (
    <div
      className={`pxml-editor ${isMaximized ? 'maximized' : ''}`}
      style={isMaximized && panelLeft !== null ? { left: `${panelLeft}px` } : undefined}
    >
      {isMaximized && (
        <div
          className="pxml-resize-handle"
          onMouseDown={handleResizeMouseDown}
          title="Drag to resize"
        />
      )}
      <div className="panel-header">
        <h3>PXML Editor</h3>
        {fileName && <span className="pxml-filename">{fileName}</span>}
        <div className="editor-actions">
          <button className="btn-upload" onClick={() => inputRef.current?.click()}>
            Load
          </button>
          {!isEditing ? (
            <button className="btn-edit" onClick={enterEdit}>
              Edit
            </button>
          ) : (
            <>
              <button className="btn-save" onClick={handleSave}>
                Save
              </button>
              <button className="btn-cancel" onClick={handleCancel}>
                Cancel
              </button>
            </>
          )}
          <button
            className="btn-download"
            onClick={handleDownload}
            title="Download the current XML as a file"
          >
            ⤓ Download
          </button>
          <button
            className="btn-maximize"
            onClick={toggleMaximize}
            title={isMaximized ? 'Restore to panel' : 'Maximize for readability'}
          >
            {isMaximized ? '✕ Close' : '⛶ Expand'}
          </button>
        </div>
        <input
          ref={inputRef}
          type="file"
          accept=".pxml,.xml"
          onChange={handleInputChange}
          hidden
        />
      </div>

      {(matches.length > 0 || isSearching) && (
        <div className="matches-panel">
          {isSearching ? (
            <div className="matches-loading">Searching...</div>
          ) : (
            <>
              <div className="matches-header">
                <span>Top {matches.length} matches:</span>
                <button
                  className="matches-hide-btn"
                  onClick={() => setMatchesHidden((v) => !v)}
                  title={matchesHidden ? 'Show matches' : 'Hide matches'}
                >
                  {matchesHidden ? '▼' : '▲'}
                </button>
              </div>
              {!matchesHidden && (
                <div className="matches-list">
                  {matches.map((match, idx) => (
                    <div
                      key={idx}
                      className={`match-item ${activeMatch === match ? 'active' : ''}`}
                      onClick={() => handleMatchClick(match)}
                    >
                      <span className="match-score">{match.score}%</span>
                      <span className="match-preview">
                        {match.plain_text.substring(0, 80)}...
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      )}

      <div
        className={`pxml-editor-host ${isEditing ? 'editing' : 'viewing'}`}
        ref={hostRef}
        onDrop={handleDrop}
        onDragOver={handleDragOver}
        title={!isEditing ? 'Double-click to edit' : undefined}
      />
    </div>
  );
};

export default PxmlEditor;
 