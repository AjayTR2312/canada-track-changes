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
import { search, searchKeymap, openSearchPanel } from '@codemirror/search';
import { xml } from '@codemirror/lang-xml';
import {
  syntaxHighlighting,
  HighlightStyle,
  bracketMatching,
  indentOnInput,
  syntaxTree,
  matchBrackets,
} from '@codemirror/language';
import { tags as t } from '@lezer/highlight';
import type { SyntaxNode } from '@lezer/common';
import TagPalette, { type DtdSchema, type DtdFamily } from './TagPalette';
import CorrectionChat, { type CapturedBlock } from './CorrectionChat';
import { findAfterAnchor, findTextRange, spacingAround, tagsBalanced } from '../xmlLocate';
import { elementSpanHighlight } from './elementSpan';
import './PxmlEditor.css';

export type { DtdSchema, DtdSchemaElement, DtdFamily } from './TagPalette';

export interface DtdError {
  line: number;
  lines: number[];
  column?: number;
  severity: 'error' | 'warning';
  rule: string;
  title: string;
  message: string;
  fix: string;
  location: string;
  count: number;
  /** Which DTD reported it when validating against several (e.g. 'PAX', 'PIX'). */
  dtd?: string;
  dtd_family?: string;
}

export interface DtdResult {
  available: boolean;
  wellformed: boolean;
  validation_passed: boolean;
  dtd_label: string | null;
  error_count: number;
  warning_count: number;
  errors: DtdError[];
  /** Per-DTD summary when validated against more than one family. */
  by_family?: Record<string, { label: string; error_count: number; warning_count: number; passed: boolean; primary: boolean }>;
  primary_family?: string;
}

export interface CoverageResult {
  coverage_pct: number;
  input_words: number;
  missing_words: number;
  missing_segments: { text: string; input_line?: number; words: number }[];
  scattered_count: number;
}

// Rules that have a deterministic batch fixer on the backend.
const FIXABLE_RULES = ['FN-004', 'FORM-030', 'PRES-011', 'PRES-001'];

interface Props {
  onPxmlLoaded: (content: string) => void;
  onPxmlSaved?: (content: string) => void;
  matches: ChunkMatch[];
  isSearching: boolean;
  sessionId?: string | null;
  initialContent?: string;
  initialFileName?: string | null;
  // Validation + fix actions. Each receives the editor's current text and returns
  // a promise; the editor applies returned text and auto-revalidates.
  dtdResult?: DtdResult | null;
  dtdValidating?: boolean;
  onValidateDtd?: (content: string) => void;
  onRepair?: (content: string) => Promise<{ text: string; summary: string }>;
  onContentCheck?: (content: string) => Promise<CoverageResult>;
  onFix?: (content: string, errorLine: number, errorMsg: string) => Promise<{ suggestion: string; start_line: number; end_line: number }>;
  onFixAll?: (content: string, rule: string) => Promise<{ text: string; fixes_applied: number }>;
  onLocateInDoc?: (text: string) => void;
  onEnsureWidth?: () => void;   // ask the host to widen the right panel for the errors box
  onClearMatches?: () => void;  // dismiss the top-5 search matches panel
  onClearDtd?: () => void;      // dismiss the DTD/well-formedness panel (return to issue-fixing)
  // DTD-driven tag-insertion palette. `dtdSchema` is fetched by the host once
  // per load (see onPxmlLoaded) so the palette never needs its own network call.
  dtdSchema?: DtdSchema | null;
  /** Which DTD family the palette shows, and the host callback that refetches the
   * schema when the user switches PAX/PIX (the host owns the fetch). */
  dtdFamily?: DtdFamily;
  onDtdFamilyChange?: (f: DtdFamily) => void;
  /** Lets the host (resolver agent panel) read the live editor text and apply a verified
   * find→replace edit. applyEdit returns an error message, or null on success. */
  editorApiRef?: React.MutableRefObject<PxmlEditorApi | null>;
}

export interface PxmlEditorApi {
  getText: () => string;
  applyEdit: (find: string, replace: string) => string | null;
  /** Insert an XML fragment at the cursor (or over the selection). Returns an error message, or null on success. */
  insertAtCursor: (fragment: string) => string | null;
  /** Put the cursor right after the end of `anchor` (Word text that precedes an insertion). Null on success. */
  locateInsertion: (anchor: string, hint?: number) => string | null;
  /** Select `text` (Word text that was deleted) where it follows `before`. Null on success. */
  locateText: (before: string, text: string, hint?: number) => string | null;
  /** Insert already-entity-encoded text at the cursor as <trace>…</trace>, with spaces so words do not run together. */
  insertTracked: (encodedText: string) => string | null;
  /** Replace the current selection (the deleted text) with the empty marker <trace.deleted/>. */
  markSelectionDeleted: () => string | null;
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

// Light-mode syntax colors (high-contrast on white background).
const pxmlHighlightLight = HighlightStyle.define([
  { tag: t.tagName, color: '#1565c0', fontWeight: '500' },
  { tag: t.angleBracket, color: '#616161' },
  { tag: t.attributeName, color: '#b8860b' },
  { tag: [t.attributeValue, t.string], color: '#2e7d32' },
  { tag: t.comment, color: '#9e9e9e', fontStyle: 'italic' },
  { tag: t.processingInstruction, color: '#7b1fa2' },
  { tag: t.content, color: '#212121' },
  { tag: t.meta, color: '#7b1fa2' },
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
    '.cm-pxml-edited': {
      textDecoration: 'underline wavy #f9e2af',
      textUnderlineOffset: '3px',
      backgroundColor: '#f9e2af15',
      borderRadius: '2px',
    },
    '.cm-pxml-error-line': {
      backgroundColor: '#f38ba822',
      boxShadow: 'inset 3px 0 0 #f38ba8',
    },
    // bracketMatching()'s own decoration classes — undecorated by default in
    // CodeMirror's base theme (near-invisible), which is why a tag's matching
    // open/close partner was easy to miss, especially once it scrolls off
    // screen. Bold, high-contrast, and clickable-looking on purpose.
    '.cm-matchingBracket': {
      backgroundColor: '#f9e2af44',
      outline: '1.5px solid #f9e2af',
      borderRadius: '2px',
      fontWeight: '700',
    },
    '.cm-nonmatchingBracket': {
      backgroundColor: '#f38ba844',
      outline: '1.5px solid #f38ba8',
      borderRadius: '2px',
    },
  },
  { dark: true }
);

// Light theme for CodeMirror.
const pxmlThemeLight = EditorView.theme(
  {
    '&': {
      color: '#212121',
      backgroundColor: '#ffffff',
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
      caretColor: '#1565c0',
      padding: '8px 0',
    },
    '.cm-cursor, .cm-dropCursor': {
      borderLeftColor: '#1565c0',
      borderLeftWidth: '2px',
    },
    '&.cm-focused .cm-cursor': { borderLeftColor: '#1565c0' },
    '.cm-selectionBackground, .cm-content ::selection': {
      backgroundColor: '#bbdefb',
    },
    '&.cm-focused .cm-selectionBackground': { backgroundColor: '#90caf9' },
    '.cm-gutters': {
      backgroundColor: '#f5f5f5',
      color: '#9e9e9e',
      border: 'none',
      borderRight: '1px solid #e0e0e0',
    },
    '.cm-activeLineGutter': { backgroundColor: '#e3f2fd', color: '#1565c0' },
    '.cm-activeLine': { backgroundColor: '#f5f5f5' },
    '.cm-pxml-match': {
      backgroundColor: '#bbdefb',
      outline: '1px solid #1565c0',
      borderRadius: '2px',
    },
    '.cm-pxml-trace': {
      color: '#2e7d32',
      fontWeight: '700',
      backgroundColor: '#e8f5e9',
      borderRadius: '3px',
    },
    '.cm-pxml-trace-del': {
      color: '#c62828',
      fontWeight: '700',
      backgroundColor: '#fce4ec',
      borderRadius: '3px',
    },
    '.cm-pxml-edited': {
      textDecoration: 'underline wavy #e65100',
      textUnderlineOffset: '3px',
      backgroundColor: 'rgba(255, 152, 0, 0.08)',
      borderRadius: '2px',
    },
    '.cm-pxml-error-line': {
      backgroundColor: 'rgba(198, 40, 40, 0.10)',
      boxShadow: 'inset 3px 0 0 #c62828',
    },
    '.cm-matchingBracket': {
      backgroundColor: 'rgba(184, 134, 11, 0.22)',
      outline: '1.5px solid #b8860b',
      borderRadius: '2px',
      fontWeight: '700',
    },
    '.cm-nonmatchingBracket': {
      backgroundColor: 'rgba(198, 40, 40, 0.18)',
      outline: '1.5px solid #c62828',
      borderRadius: '2px',
    },
  },
  { dark: false }
);

// Helper: check if the page is currently in light mode.
const isDarkMode = () =>
  document.documentElement.getAttribute('data-theme') !== 'light';

// Return the CodeMirror extensions for the current theme.
const themeExtensions = (dark: boolean) => [
  dark ? pxmlTheme : pxmlThemeLight,
  syntaxHighlighting(dark ? pxmlHighlight : pxmlHighlightLight),
];

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

// --- DTD error line highlight (controlled by a StateEffect) ---
// Takes a list of 1-based line numbers and paints those lines as errors.
const setDtdErrors = StateEffect.define<number[]>();
const errorLineMark = Decoration.line({ class: 'cm-pxml-error-line' });

const dtdField = StateField.define<DecorationSet>({
  create() {
    return Decoration.none;
  },
  update(deco, tr) {
    deco = deco.map(tr.changes);
    for (const e of tr.effects) {
      if (e.is(setDtdErrors)) {
        const doc = tr.state.doc;
        const seen = new Set<number>();
        const ranges = [];
        for (const ln of [...e.value].sort((a, b) => a - b)) {
          if (ln >= 1 && ln <= doc.lines && !seen.has(ln)) {
            seen.add(ln);
            ranges.push(errorLineMark.range(doc.line(ln).from));
          }
        }
        deco = Decoration.set(ranges, true);
      }
    }
    return deco;
  },
  provide: (f) => EditorView.decorations.from(f),
});

// --- Edit-tracking: underline only the text the user actually types/changes ---
const setEditTracking = StateEffect.define<boolean>();
const clearEditRanges = StateEffect.define<null>();
const editedMark = Decoration.mark({ class: 'cm-pxml-edited' });

const editTrackingField = StateField.define<{ tracking: boolean; ranges: DecorationSet }>({
  create() {
    return { tracking: false, ranges: Decoration.none };
  },
  update(state, tr) {
    let { tracking, ranges } = state;

    // Check for control effects
    for (const e of tr.effects) {
      if (e.is(setEditTracking)) {
        tracking = e.value;
        if (!e.value) ranges = Decoration.none; // exiting edit mode clears
      }
      if (e.is(clearEditRanges)) {
        ranges = Decoration.none;
      }
    }

    if (!tracking) return { tracking, ranges };

    // Map existing decorations through changes
    ranges = ranges.map(tr.changes);

    // If the document changed, mark the inserted ranges
    if (tr.docChanged) {
      const newRanges: { from: number; to: number }[] = [];
      tr.changes.iterChanges((_fromA, _toA, fromB, toB) => {
        if (toB > fromB) {
          newRanges.push({ from: fromB, to: toB });
        }
      });
      if (newRanges.length > 0) {
        const marks = newRanges.map((r) => editedMark.range(r.from, r.to));
        ranges = ranges.update({ add: marks, sort: true });
      }
    }

    return { tracking, ranges };
  },
  provide: (f) => EditorView.decorations.from(f, (val) => val.ranges),
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

const PxmlEditor: React.FC<Props> = ({ onPxmlLoaded, onPxmlSaved, matches, isSearching, sessionId, initialContent, initialFileName, dtdResult, dtdValidating, onValidateDtd, onRepair, onContentCheck, onFix, onFixAll, onLocateInDoc, onEnsureWidth, onClearMatches, onClearDtd, dtdSchema, dtdFamily = 'auto', onDtdFamilyChange, editorApiRef }) => {
  const [fileName, setFileName] = useState<string | null>(null);
  const [isEditing, setIsEditing] = useState(false);
  const [showTagPalette, setShowTagPalette] = useState(false);
  const [paletteContextTag, setPaletteContextTag] = useState<string | null>(null);
  const insertTagBtnRef = useRef<HTMLButtonElement>(null);
  // Correction chat: fix a selected block from a plain-language instruction.
  const [correctionBlock, setCorrectionBlock] = useState<CapturedBlock | null>(null);
  const aiBtnRef = useRef<HTMLButtonElement>(null);
  // buildExtensions() runs once at mount (before jumpToMatchingTag below is
  // even defined in this render pass) and installs a keymap that must call
  // whatever the CURRENT jumpToMatchingTag is — this ref is the indirection
  // that lets a function defined later in the component stay reachable from
  // a keymap wired up earlier, kept fresh every render (see the useEffect
  // right after jumpToMatchingTag's own definition).
  const jumpToMatchingTagRef = useRef<() => void>(() => {});
  const [activeMatch, setActiveMatch] = useState<ChunkMatch | null>(null);
  // Validator action state
  const [busy, setBusy] = useState<string>('');            // '' | 'repair' | 'coverage' | 'fix'
  const [errIdx, setErrIdx] = useState(-1);                 // Next-error cursor
  const [coverage, setCoverage] = useState<CoverageResult | null>(null);
  // When a document is checked against several DTDs, the summary chips filter the list to one DTD.
  const [dtdFamFilter, setDtdFamFilter] = useState<string | null>(null);
  const [fixSuggestion, setFixSuggestion] = useState<
    { suggestion: string; start_line: number; end_line: number; title: string } | null
  >(null);
  const [asideWidth, setAsideWidth] = useState(340);   // resizable errors box width
  const editorBodyRef = useRef<HTMLDivElement>(null);

  const inputRef = useRef<HTMLInputElement>(null);
  const hostRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const editComp = useRef(new Compartment());
  const themeComp = useRef(new Compartment());
  const isEditingRef = useRef(false);
  const preEditRef = useRef<string>('');
  const pendingDocRef = useRef<string>('');
  const onPxmlLoadedRef = useRef(onPxmlLoaded);
  // Tracks whether initial content has been loaded for the current session;
  // resets when initial props become empty (e.g. navigating back to sessions).
  const initialLoadedRef = useRef(false);

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
    elementSpanHighlight(),
    xml(),
    search({ top: true }),
    themeComp.current.of(themeExtensions(isDarkMode())),
    EditorView.lineWrapping,
    traceField,
    matchField,
    dtdField,
    editTrackingField,
    keymap.of([
      { key: 'Mod-]', run: () => { jumpToMatchingTagRef.current(); return true; } },
      ...searchKeymap, ...defaultKeymap, ...historyKeymap,
    ]),
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

  // Watch for dark/light theme changes and reconfigure CodeMirror colors.
  useEffect(() => {
    const observer = new MutationObserver(() => {
      const view = viewRef.current;
      if (!view) return;
      view.dispatch({
        effects: themeComp.current.reconfigure(themeExtensions(isDarkMode())),
      });
    });
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-theme'],
    });
    return () => observer.disconnect();
  }, []);

  // Toggle read-only when switching between view and edit mode.
  useEffect(() => {
    isEditingRef.current = isEditing;
    const view = viewRef.current;
    if (!view) return;
    view.dispatch({
      effects: [
        editComp.current.reconfigure(EditorState.readOnly.of(!isEditing)),
        setEditTracking.of(isEditing),
        ...(isEditing ? [clearEditRanges.of(null)] : []),
      ],
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

  // When a fresh set of search matches arrives, auto-jump to the best (top-scored)
  // match so the editor points at the location immediately — no extra click needed.
  useEffect(() => {
    if (matches && matches.length > 0) {
      setActiveMatch(matches[0]);
    }
  }, [matches]);

  // Paint DTD error lines in the gutter/background whenever results change.
  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    const lines: number[] = [];
    for (const e of dtdResult?.errors ?? []) {
      for (const ln of e.lines && e.lines.length ? e.lines : [e.line]) {
        if (ln && ln > 0) lines.push(ln);
      }
    }
    view.dispatch({ effects: setDtdErrors.of(lines) });
  }, [dtdResult]);

  // When the errors/coverage box appears, ask the host to widen the right panel so
  // the editor keeps a usable width (avoids the editor being squished to a sliver).
  useEffect(() => {
    if (dtdResult || coverage) onEnsureWidth?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dtdResult, coverage]);

  // Scroll the editor to a specific 1-based line (used by the error list).
  const scrollToLine = (line: number) => {
    const view = viewRef.current;
    if (!view || !line || line < 1) return;
    const n = Math.min(line, view.state.doc.lines);
    const pos = view.state.doc.line(n).from;
    setActiveMatch(null);
    view.dispatch({
      selection: { anchor: pos },
      effects: EditorView.scrollIntoView(pos, { y: 'center' }),
    });
    view.focus();
  };

  // Current editor text (live doc if mounted, else pending).
  const currentText = () => viewRef.current?.state.doc.toString() ?? pendingDocRef.current;

  // Replace the whole editor doc (used by Repair / Format / Fix / Fix-all).
  const setDoc = (text: string) => {
    const view = viewRef.current;
    pendingDocRef.current = text;
    onPxmlLoadedRef.current(text);
    if (view) {
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text } });
    }
  };

  // API for the resolver agent panel: read the live text, apply one verified edit.
  if (editorApiRef) {
    editorApiRef.current = {
      getText: () => currentText(),
      applyEdit: (find, replace) => {
        const view = viewRef.current;
        if (!view) return 'The editor is not ready.';
        const text = view.state.doc.toString();
        const at = text.indexOf(find);
        if (at < 0) return 'The target block changed since the agent read it, so the edit was not applied. Run the resolver again.';
        if (text.indexOf(find, at + 1) >= 0) return 'The target block is no longer unique in the editor, so the edit was not applied.';
        view.dispatch({
          changes: { from: at, to: at + find.length, insert: replace },
          selection: { anchor: at },
          effects: EditorView.scrollIntoView(at, { y: 'center' }),
        });
        view.focus();
        return null;
      },
      locateInsertion: (anchor, hint) => {
        const view = viewRef.current;
        if (!view) return 'The XML editor is not ready.';
        const doc = view.state.doc.toString();
        const pos = findAfterAnchor(doc, anchor, hint);
        if (pos === null) return 'Could not find this spot in the XML automatically. Click where it belongs in the XML, then try again.';
        const from = Math.max(0, pos - 40);
        view.dispatch({
          selection: { anchor: pos },
          effects: [setMatch.of({ from, to: pos }), EditorView.scrollIntoView(pos, { y: 'center' })],
        });
        return null;
      },
      locateText: (before, text, hint) => {
        const view = viewRef.current;
        if (!view) return 'The XML editor is not ready.';
        const range = findTextRange(view.state.doc.toString(), before, text, hint);
        if (!range) return 'Could not find this text in the XML automatically. Select it in the XML yourself, then try again.';
        view.dispatch({
          selection: { anchor: range.from, head: range.to },
          effects: [setMatch.of(null), EditorView.scrollIntoView(range.from, { y: 'center' })],
        });
        return null;
      },
      insertTracked: (encodedText) => {
        const view = viewRef.current;
        if (!view) return 'The XML editor is not ready.';
        const sel = view.state.selection.main;
        if (sel.head === 0 && sel.empty) return 'Click in the XML where this text belongs, then try again.';
        const doc = view.state.doc.toString();
        const { before, after } = spacingAround(doc, sel.from, encodedText);
        const fragment = `${before}<trace>${encodedText}</trace>${after}`;
        view.dispatch({
          changes: { from: sel.from, to: sel.to, insert: fragment },
          selection: { anchor: sel.from + before.length, head: sel.from + fragment.length - after.length },
          effects: [setMatch.of(null), EditorView.scrollIntoView(sel.from, { y: 'center' })],
        });
        view.focus();
        return null;
      },
      markSelectionDeleted: () => {
        const view = viewRef.current;
        if (!view) return 'The XML editor is not ready.';
        const sel = view.state.selection.main;
        if (sel.empty) return 'Locate or select the deleted text in the XML first.';
        // The finished XML keeps only an empty marker where deleted text was (the pipeline's postprocessing collapses
        // <trace.deleted>text<trace.deleted/> into <trace.deleted/>), so the selected text is replaced by that marker.
        if (!tagsBalanced(view.state.sliceDoc(sel.from, sel.to))) {
          return 'The selection cuts through a tag. Select whole words (and whole tags), then try again.';
        }
        const fragment = '<trace.deleted/>';
        view.dispatch({
          changes: { from: sel.from, to: sel.to, insert: fragment },
          selection: { anchor: sel.from, head: sel.from + fragment.length },
        });
        view.focus();
        return null;
      },
      insertAtCursor: (fragment) => {
        const view = viewRef.current;
        if (!view) return 'The XML editor is not ready.';
        const sel = view.state.selection.main;
        if (sel.head === 0 && sel.empty) return 'Click in the XML where this text belongs, then try again.';
        view.dispatch({
          changes: { from: sel.from, to: sel.to, insert: fragment },
          selection: { anchor: sel.from, head: sel.from + fragment.length },
          effects: EditorView.scrollIntoView(sel.from, { y: 'center' }),
        });
        view.focus();
        return null;
      },
    };
  }

  /* --------------------- DTD-driven tag palette --------------------- */

  // The element the cursor currently sits inside, e.g. editing inside
  // <para><para.text>…|…</para.text></para> resolves to "para.text". Climbs
  // past a SelfClosingTag (e.g. <trace.deleted/>) since it has no body to
  // insert into — the nearest REAL container is what a palette needs.
  const resolveEnclosingTag = (): string | null => {
    const view = viewRef.current;
    if (!view) return null;
    const tree = syntaxTree(view.state);
    let node: SyntaxNode | null = tree.resolveInner(view.state.selection.main.head, -1);
    for (; node; node = node.parent) {
      if (node.name !== 'Element') continue;
      if (node.getChild('SelfClosingTag')) continue;
      const tagName = node.getChild('OpenTag')?.getChild('TagName');
      if (tagName) return view.state.sliceDoc(tagName.from, tagName.to);
    }
    return null;
  };

  /* --------------------- correction chat (AI retag of a block) --------------------- */

  const tagNameOf = (state: EditorState, node: SyntaxNode): string => {
    const t = node.getChild('OpenTag')?.getChild('TagName') ?? node.getChild('SelfClosingTag')?.getChild('TagName');
    return t ? state.sliceDoc(t.from, t.to) : '';
  };

  // The block to correct: the selection if there is one (its parent is the smallest element
  // that strictly contains it), else the element the cursor sits in (its parent is the
  // element above it).
  const captureBlock = (): CapturedBlock | null => {
    const view = viewRef.current;
    if (!view) return null;
    const state = view.state;
    const { from, to, head } = state.selection.main;
    const tree = syntaxTree(state);
    const elementsAt = (pos: number, side: -1 | 1): SyntaxNode[] => {
      const out: SyntaxNode[] = [];
      for (let n: SyntaxNode | null = tree.resolveInner(pos, side); n; n = n.parent) {
        if (n.name === 'Element') out.push(n);
      }
      return out;
    };
    let blockFrom = from, blockTo = to, parentTag = '', kind: CapturedBlock['kind'] = 'selection', label = 'Selection';
    if (to > from) {
      const containing = elementsAt(from, 1).find((n) => n.from <= from && n.to >= to && (n.from < from || n.to > to));
      parentTag = containing ? tagNameOf(state, containing) : '';
    } else {
      const chain = elementsAt(head, -1).filter((n) => !n.getChild('SelfClosingTag'));
      const target = chain[0];
      if (!target) return null;
      blockFrom = target.from; blockTo = target.to; kind = 'element';
      label = `<${tagNameOf(state, target)}> at the cursor`;
      const parent = chain[1];
      parentTag = parent ? tagNameOf(state, parent) : '';
    }
    if (blockTo <= blockFrom) return null;
    return {
      text: state.sliceDoc(blockFrom, blockTo),
      from: blockFrom,
      to: blockTo,
      parentTag,
      before: state.sliceDoc(Math.max(0, blockFrom - 1500), blockFrom),
      after: state.sliceDoc(blockTo, Math.min(state.doc.length, blockTo + 1500)),
      kind,
      label,
    };
  };

  const openCorrectionChat = () => {
    if (correctionBlock) { setCorrectionBlock(null); return; }
    const block = captureBlock();
    if (!block) {
      alert('Select the block to fix (or put the cursor inside it) first.');
      return;
    }
    setShowTagPalette(false);
    setCorrectionBlock(block);
  };

  // Replace the block with the proposal. The block is re-found if the text moved; if it was
  // edited or is ambiguous we refuse rather than overwrite the wrong thing.
  const applyCorrection = (block: CapturedBlock, newXml: string): string => {
    const view = viewRef.current;
    if (!view) return 'The editor is not ready.';
    const doc = view.state.doc.toString();
    let from = block.from;
    if (doc.slice(block.from, block.to) !== block.text) {
      const first = doc.indexOf(block.text);
      if (first === -1) return 'The block changed in the editor since you opened the chat — select it again.';
      if (doc.indexOf(block.text, first + 1) !== -1) return 'That block now appears more than once — select it again to fix the right one.';
      from = first;
    }
    view.dispatch({
      changes: { from, to: from + block.text.length, insert: newXml },
      selection: { anchor: from },
      effects: EditorView.scrollIntoView(from, { y: 'center' }),
    });
    // Later refinements in the chat now target the NEW text at its new position.
    setCorrectionBlock({ ...block, text: newXml, from, to: from + newXml.length });
    view.focus();
    return '';
  };

  // Jump the cursor/view to the OTHER end of the tag pair at the cursor — the
  // part bracketMatching()'s highlight alone doesn't solve: a highlight is
  // only visible when both ends happen to be on screen together, which a
  // long <front>...</front> or a run of <trace>...</trace> pairs routinely
  // isn't. Tries both scan directions since the cursor may sit just before
  // or just after the tag boundary.
  const jumpToMatchingTag = () => {
    const view = viewRef.current;
    if (!view) return;
    const pos = view.state.selection.main.head;
    const match = matchBrackets(view.state, pos, 1) || matchBrackets(view.state, pos, -1);
    if (!match?.end) return;
    const target = match.end.from;
    view.dispatch({
      selection: { anchor: target },
      effects: EditorView.scrollIntoView(target, { y: 'center' }),
    });
    view.focus();
  };
  useEffect(() => {
    jumpToMatchingTagRef.current = jumpToMatchingTag;
  });

  const openTagPalette = () => {
    setPaletteContextTag(resolveEnclosingTag());
    setShowTagPalette(true);
  };

  // Wraps the current selection in the chosen tag, or inserts an empty
  // open/close pair (self-closing for EMPTY elements) with the cursor left
  // ready to type — matching the "select tag, structure appears" workflow
  // this palette exists to replace hand-typing for.
  // `attrs` is the tag's REQUIRED attributes pre-rendered by the palette (e.g.
  // ` type=""`), so a tag that the DTD demands attributes on is never inserted bare.
  const handleTagInsert = (tagName: string, elType: string, attrs = '') => {
    const view = viewRef.current;
    setShowTagPalette(false);
    if (!view) return;

    const { from, to } = view.state.selection.main;
    const open = `<${tagName}${attrs}>`;
    if (elType === 'empty') {
      const insert = `<${tagName}${attrs}/>`;
      view.dispatch({ changes: { from, to, insert }, selection: { anchor: from + insert.length } });
    } else if (to > from) {
      const selected = view.state.sliceDoc(from, to);
      const insert = `${open}${selected}</${tagName}>`;
      view.dispatch({
        changes: { from, to, insert },
        selection: { anchor: from + open.length, head: from + open.length + selected.length },
      });
    } else {
      const insert = `${open}</${tagName}>`;
      view.dispatch({ changes: { from, to, insert }, selection: { anchor: from + open.length } });
    }
    view.focus();
  };

  // Set an attribute on the OPEN tag at the cursor: either the cursor is inside
  // the tag (`<cite |type="">`) or right after its `>` (where handleTagInsert
  // leaves it). An attribute that's already there gets its value replaced — never
  // duplicated (`<cite type="" type="lrre">` is not well-formed). Falls back to a
  // plain insert at the cursor if no open tag is found.
  const handleInsertAttribute = (name: string, value: string) => {
    const view = viewRef.current;
    if (!view) return;
    const doc = view.state.doc.toString();
    const head = view.state.selection.main.head;

    let tagStart = -1;
    let tagEnd = -1; // index of the closing '>'
    const lt = doc.lastIndexOf('<', head - 1);
    const gtBefore = doc.lastIndexOf('>', head - 1);
    if (lt > gtBefore) {
      tagStart = lt;                       // cursor is inside a tag
      tagEnd = doc.indexOf('>', head);
    } else if (head > 0 && doc[head - 1] === '>' && lt >= 0) {
      tagStart = doc.lastIndexOf('<', head - 2); // cursor sits right after a tag's '>'
      tagEnd = head - 1;
    }
    const tagText = tagStart >= 0 && tagEnd > tagStart ? doc.slice(tagStart, tagEnd + 1) : '';
    if (!tagText || /^<[/!?]/.test(tagText)) {
      handleInsertText(` ${name}="${value}"`);
      return;
    }

    const esc = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const existing = new RegExp(`(\\s${esc}\\s*=\\s*)(["'])(.*?)\\2`).exec(tagText);
    if (existing) {
      const valFrom = tagStart + existing.index + existing[1].length + 1; // inside the quotes
      const valTo = valFrom + existing[3].length;
      if (value === '') {
        // Plain attribute-name click on one that's already there: don't wipe its
        // value, just put the cursor inside it.
        view.dispatch({ selection: { anchor: valFrom, head: valTo }, effects: EditorView.scrollIntoView(valFrom) });
      } else {
        view.dispatch({
          changes: { from: valFrom, to: valTo, insert: value },
          selection: { anchor: valFrom + value.length },
        });
      }
    } else {
      const selfClose = tagText.endsWith('/>');
      const at = tagEnd - (selfClose ? 1 : 0);
      const insert = ` ${name}="${value}"`;
      view.dispatch({
        changes: { from: at, insert },
        selection: { anchor: at + insert.length - 1 }, // just before the closing quote
      });
    }
    view.focus();
  };

  // Entity insertion: the palette stays open so several can be added in a row;
  // the text goes in at the cursor (replacing any selection).
  const handleInsertText = (text: string) => {
    const view = viewRef.current;
    if (!view) return;
    const { from, to } = view.state.selection.main;
    view.dispatch({ changes: { from, to, insert: text }, selection: { anchor: from + text.length } });
    view.focus();
  };

  /* --------------------- validator actions --------------------- */
  const doValidate = () => onValidateDtd?.(currentText());

  const doFormat = () => {
    try { setDoc(formatXmlString(currentText())); } catch { /* leave as-is */ }
  };

  const doRepair = async () => {
    if (!onRepair) return;
    setBusy('repair');
    try {
      const r = await onRepair(currentText());
      setDoc(r.text);
      onValidateDtd?.(r.text);            // auto-revalidate
    } catch (e) { console.error('Repair failed:', e); }
    finally { setBusy(''); }
  };

  const doContentCheck = async () => {
    if (!onContentCheck) return;
    setBusy('coverage');
    try { setCoverage(await onContentCheck(currentText())); }
    catch (e) { console.error('Content check failed:', e); }
    finally { setBusy(''); }
  };

  const doFix = async (err: DtdError) => {
    if (!onFix || !err.line) return;
    setBusy('fix');
    try {
      const r = await onFix(currentText(), err.line, err.title || err.message);
      setFixSuggestion({ ...r, title: err.title || err.message });
    } catch (e) { console.error('Fix failed:', e); }
    finally { setBusy(''); }
  };

  const applyFix = () => {
    if (!fixSuggestion) return;
    const lines = currentText().split('\n');
    const before = lines.slice(0, fixSuggestion.start_line - 1);
    const after = lines.slice(fixSuggestion.end_line);
    const next = [...before, fixSuggestion.suggestion.replace(/\n$/, ''), ...after].join('\n');
    setDoc(next);
    setFixSuggestion(null);
    onValidateDtd?.(next);                // auto-revalidate
  };

  const doFixAll = async (rule: string) => {
    if (!onFixAll) return;
    setBusy('fix');
    try {
      const r = await onFixAll(currentText(), rule);
      setDoc(r.text);
      onValidateDtd?.(r.text);            // auto-revalidate
    } catch (e) { console.error('Fix-all failed:', e); }
    finally { setBusy(''); }
  };

  const nextError = () => {
    const errs = dtdResult?.errors ?? [];
    const withLine = errs.filter((e) => e.line > 0).sort((a, b) => a.line - b.line);
    if (!withLine.length) return;
    const ni = (errIdx + 1) % withLine.length;
    setErrIdx(ni);
    scrollToLine(withLine[ni].line);
  };

  // Drag the divider between the XML editor and the errors box.
  const startAsideResize = (e: React.MouseEvent) => {
    e.preventDefault();
    const body = editorBodyRef.current;
    if (!body) return;
    const onMove = (ev: MouseEvent) => {
      const rect = body.getBoundingClientRect();
      const w = Math.min(Math.max(rect.right - ev.clientX, 240), rect.width - 220);
      setAsideWidth(w);
    };
    const onUp = () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  };

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

  // Load initial content when resuming a session (placed after loadContent definition)
  useEffect(() => {
    if (!initialContent || !initialFileName) {
      // Props are empty (session not loaded yet, or navigated away) — reset the flag
      // so we load content when the new session's PXML arrives.
      initialLoadedRef.current = false;
      return;
    }
    // Already loaded for this resume cycle — skip the formatted echo that comes
    // back through handlePxmlLoaded → setPxmlContent.
    if (initialLoadedRef.current) return;
    initialLoadedRef.current = true;
    loadContent(initialContent, initialFileName);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialContent, initialFileName]);

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
    if (onPxmlSaved) onPxmlSaved(content);
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

  /* ------------------------------- view ------------------------------ */

  if (!fileName) {
    return (
      <div className="pxml-editor">
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
    <div className="pxml-editor">
      <div className="panel-header">
        <h3>PXML Editor</h3>
        {isEditing && <span className="editing-badge">✏️ EDITING</span>}
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
              {dtdSchema?.available && (
                <>
                  <button
                    ref={insertTagBtnRef}
                    className="btn-insert-tag"
                    onClick={() => (showTagPalette ? setShowTagPalette(false) : openTagPalette())}
                    title="Insert tags, attributes and entities from the document's DTD — no typing needed"
                  >
                    🏷️ Insert Tag
                  </button>
                  <button
                    ref={aiBtnRef}
                    className="btn-insert-tag btn-ai-fix"
                    onClick={openCorrectionChat}
                    title="Select a wrongly tagged or untagged block, say what it should be, and get it retagged"
                  >
                    🤖 Fix with AI
                  </button>
                  {correctionBlock && (
                    <CorrectionChat
                      block={correctionBlock}
                      anchorEl={aiBtnRef.current}
                      pxmlHead={currentText().slice(0, 800)}
                      dtdFamily={dtdFamily}
                      sessionId={sessionId}
                      docName={fileName}
                      onApply={applyCorrection}
                      onApplied={() => viewRef.current?.focus()}
                      onClose={() => setCorrectionBlock(null)}
                    />
                  )}
                  {showTagPalette && (
                    <TagPalette
                      schema={dtdSchema}
                      contextTag={paletteContextTag}
                      hasSelection={(viewRef.current?.state.selection.main.to ?? 0) > (viewRef.current?.state.selection.main.from ?? 0)}
                      anchorEl={insertTagBtnRef.current}
                      family={dtdFamily}
                      onFamilyChange={(f) => onDtdFamilyChange?.(f)}
                      onSelect={handleTagInsert}
                      onInsertText={handleInsertText}
                      onInsertAttribute={handleInsertAttribute}
                      onClose={() => setShowTagPalette(false)}
                    />
                  )}
                </>
              )}
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
            className="btn-find"
            onClick={() => { const v = viewRef.current; if (v) { openSearchPanel(v); v.focus(); } }}
            title="Find & Replace in the XML (Ctrl+F)"
          >
            🔍 Find/Replace
          </button>
          <button
            className="btn-jump-match"
            onClick={jumpToMatchingTag}
            title="Jump to this tag's matching open/close partner (Ctrl+]) — works even when it's scrolled far away"
          >
            ⇅ Jump to Match
          </button>
          {onValidateDtd && (
            <button className="btn-validate-dtd" disabled={dtdValidating || !!busy}
              onClick={doValidate} title="Validate against the DTD + rules">
              {dtdValidating ? '⏳ Validating…' : '✓ Validate'}
            </button>
          )}
          {dtdResult && dtdResult.errors.some((e) => e.line > 0) && (
            <button className="btn-next-error" onClick={nextError}
              title="Jump to the next error line">
              ↓ Next error
            </button>
          )}
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
                {onClearMatches && (
                  <button className="matches-close" onClick={onClearMatches} title="Dismiss matches">✕</button>
                )}
              </div>
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
            </>
          )}
        </div>
      )}

      <div className="editor-body" ref={editorBodyRef}>
        <div
          className={`pxml-editor-host ${isEditing ? 'editing' : 'viewing'}`}
          ref={hostRef}
          onDrop={handleDrop}
          onDragOver={handleDragOver}
          title={!isEditing ? 'Double-click to edit' : undefined}
        />
        {(dtdResult || coverage) && (
          <div className="aside-resize" onMouseDown={startAsideResize} title="Drag to resize the errors panel" />
        )}
        {(dtdResult || coverage) && (
        <aside className="validation-aside" style={{ width: asideWidth }}>
      {dtdResult && (
        <div className="dtd-panel">
          <div className={`dtd-header ${dtdResult.validation_passed ? 'ok' : 'fail'}`}>
            {dtdResult.available === false ? (
              <span>⚠ Validation service unavailable — is the backend running? Try again.</span>
            ) : dtdResult.validation_passed ? (
              <span>✓ Valid against DTD{dtdResult.dtd_label ? ` (${dtdResult.dtd_label})` : ''}</span>
            ) : !dtdResult.wellformed ? (
              <span>✗ Not well-formed — fix XML syntax first (or dismiss to keep fixing revisions)</span>
            ) : (
              <span>
                ✗ {dtdResult.error_count} error{dtdResult.error_count === 1 ? '' : 's'}
                {dtdResult.warning_count ? `, ${dtdResult.warning_count} warning(s)` : ''}
                {dtdResult.dtd_label ? ` · ${dtdResult.dtd_label}` : ''}
              </span>
            )}
            {onClearDtd && (
              <button className="dtd-close" onClick={onClearDtd}
                title="Dismiss DTD results and return to revision fixing">✕</button>
            )}
          </div>
          {dtdResult.by_family && dtdResult.wellformed && (
            <div className="dtd-families" title="The document's own DTD decides pass / fail; findings from the other DTD are shown as warnings">
              {Object.entries(dtdResult.by_family).map(([fam, f]) => (
                <button
                  key={fam}
                  className={`dtd-fam ${f.passed ? 'ok' : f.primary ? 'fail' : 'warn'} ${dtdFamFilter === fam ? 'active' : ''}`}
                  onClick={() => setDtdFamFilter((cur) => (cur === fam ? null : fam))}
                  title={dtdFamFilter === fam ? 'Show all DTDs again' : `Show only ${f.label} findings`}
                >
                  {f.label}{f.primary ? ' (declared)' : ''}: {f.passed ? '✓ valid' : `${f.error_count} issue${f.error_count === 1 ? '' : 's'}`}
                </button>
              ))}
            </div>
          )}
          {dtdResult.errors.length > 0 && (
            <div className="dtd-list">
              {[...dtdResult.errors]
                .filter((e) => !dtdFamFilter || e.dtd_family === dtdFamFilter)
                // the document's own DTD (errors) first, the other DTD's warnings after, then by line
                .sort((a, b) => (a.severity === b.severity ? 0 : a.severity === 'error' ? -1 : 1)
                  || (a.line > 0 ? a.line : 1e9) - (b.line > 0 ? b.line : 1e9))
                .map((err, idx) => (
                <div key={idx} className={`dtd-item ${err.severity}`}>
                  <div className="dtd-item-top" onClick={() => scrollToLine(err.line)}>
                    <span className="dtd-idx">{idx + 1}</span>
                    <span className={`dtd-badge ${err.severity}`}>{err.rule}</span>
                    {err.dtd && <span className="dtd-which" title={`Reported by the ${err.dtd} DTD`}>{err.dtd}</span>}
                    <span className="dtd-loc">{err.line ? `Line ${err.line}` : 'no line'}</span>
                    {err.count > 1 && <span className="dtd-count">×{err.count}</span>}
                    <span className="dtd-actions">
                      {onFix && err.line > 0 && (
                        <button className="dtd-fix-btn" disabled={!!busy}
                          onClick={(e) => { e.stopPropagation(); doFix(err); }}
                          title="Ask AI to fix this">
                          {busy === 'fix' ? '…' : '✨ Fix'}
                        </button>
                      )}
                      {onFixAll && FIXABLE_RULES.includes(err.rule) && (
                        <button className="dtd-fixall-btn" disabled={!!busy}
                          onClick={(e) => { e.stopPropagation(); doFixAll(err.rule); }}
                          title={`Auto-fix all ${err.rule} occurrences`}>
                          Fix all
                        </button>
                      )}
                    </span>
                  </div>
                  <div className="dtd-msg" onClick={() => scrollToLine(err.line)}>
                    {err.title || err.message}
                  </div>
                  {err.fix && <div className="dtd-fixhint">💡 {err.fix}</div>}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {coverage && (
        <div className="dtd-panel coverage-panel">
          <div className={`dtd-header ${coverage.coverage_pct >= 99.5 ? 'ok' : 'fail'}`}>
            <span>📊 Content coverage: {coverage.coverage_pct.toFixed(1)}%
              {coverage.missing_words ? ` · ${coverage.missing_words} word(s) missing` : ''}</span>
            <button className="dtd-close" onClick={() => setCoverage(null)} title="Dismiss">✕</button>
          </div>
          {coverage.missing_segments.length > 0 && (
            <div className="dtd-list">
              {coverage.missing_segments.map((seg, i) => (
                <div key={i} className="dtd-item warning"
                  onClick={() => onLocateInDoc?.(seg.text)}
                  title={onLocateInDoc ? 'Show this missing text in the document' : undefined}>
                  <div className="dtd-item-top">
                    <span className="dtd-loc">{seg.input_line ? `src ${seg.input_line}` : `${seg.words}w`}</span>
                    {onLocateInDoc && <span className="dtd-actions"><span className="dtd-goto">↞ show in doc</span></span>}
                  </div>
                  <div className="dtd-msg">{seg.text}</div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
        </aside>
        )}
      </div>

      {fixSuggestion && (
        <div className="fix-modal-overlay" onClick={() => setFixSuggestion(null)}>
          <div className="fix-modal" onClick={(e) => e.stopPropagation()}>
            <div className="fix-modal-header">
              🛠 Suggested fix — lines {fixSuggestion.start_line}–{fixSuggestion.end_line}
            </div>
            <div className="fix-modal-sub">{fixSuggestion.title}</div>
            <pre className="fix-modal-body">{fixSuggestion.suggestion}</pre>
            <div className="fix-modal-actions">
              <button className="btn-save" onClick={applyFix}>Apply</button>
              <button className="btn-cancel" onClick={() => setFixSuggestion(null)}>Cancel</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default PxmlEditor;
 