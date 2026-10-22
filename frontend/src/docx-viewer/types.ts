export interface TrackChange {
  id: string;
  type: 'insertion' | 'deletion' | 'formatChange' | 'moveFrom' | 'moveTo';
  author: string;
  date: string;
  text: string;
  paragraphIndex: number;
  runIndex: number;
  /** Characters before the change within the paragraph */
  prevContext: string;
  /** Characters after the change within the paragraph */
  postContext: string;
  /** Full text of the paragraph this change belongs to */
  paragraphText: string;
  /** Closest non-empty previous paragraph text */
  previousPara: string;
  /** Closest non-empty next paragraph text */
  postPara: string;
}

export interface DocxParagraph {
  id: string;
  runs: DocxRun[];
  paragraphIndex: number;
}

export interface DocxRun {
  id: string;
  text: string;
  type: 'normal' | 'insertion' | 'deletion' | 'formatChange' | 'moveFrom' | 'moveTo';
  author?: string;
  date?: string;
  trackChangeId?: string;
}

export interface RevisionCounts {
  insertions: number;
  deletions: number;
  moves: number;
  formatChanges: number;
  total: number;
}

export interface RevisionStats {
  /** Individual Word edits (each w:ins / w:del with text) before any merging. */
  raw: RevisionCounts;
  /** Revisions after merging Word-split pieces — the units listed and validated. */
  grouped: RevisionCounts;
  /** Characters inserted / deleted across all revisions. */
  insertedChars: number;
  deletedChars: number;
}

/** A formatting-only revision (Word's "Formatting" count). Listed for reference; not validated. */
export interface FormatRevision {
  id: string;
  kind: 'character' | 'paragraph' | 'other';
  author: string;
  date: string;
  /** The text whose formatting changed (may be empty for section / table changes). */
  text: string;
  /** What changed, e.g. ["Bold added", "Size 12 → 14 pt"]. */
  changes: string[];
  paragraphIndex: number;
  prevContext: string;
  postContext: string;
  paragraphText: string;
}

export interface ParsedDocx {
  formatRevisions: FormatRevision[];
  paragraphs: DocxParagraph[];
  trackChanges: TrackChange[];
  rawXml: string;
  stats: RevisionStats;
}

export interface ChunkMatch {
  chunk_text: string;
  plain_text: string;
  score: number;
  start_index: number;
  end_index: number;
}

// Excel-related types
export interface ExcelRow {
  trackChangeText: string;   // "Track changes" column from Excel
  isValid: boolean;          // "is_valid" column from Excel
  reason: string;            // "reason" column from Excel
  suggestedFix: string;      // "suggested_fix" column from Excel
  previousContext: string;   // "previous_context" column from Excel
}

export interface ExcelValidationMap {
  /** Maps TrackChange.id -> is_valid boolean from the matched Excel row */
  [trackChangeId: string]: boolean;
}

export interface ExcelMetaMap {
  /** Maps TrackChange.id -> { reason, suggestedFix } from the matched Excel row */
  [trackChangeId: string]: { reason: string; suggestedFix: string };
}

export type ValidityFilter = 'all' | 'true' | 'false';
