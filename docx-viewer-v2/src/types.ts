export interface TrackChange {
  id: string;
  type: 'inline_insertion' | 'inline_deletion' | 'inline_replacement' | 'paragraph_addition' | 'paragraph_deletion' | 'moveFrom' | 'moveTo';
  author: string;
  date: string;
  text: string;
  paragraphIndex: number;
  runIndex: number;
  revisionOrder: number;
  /** For replacements split into two rows: the deleted text */
  deletedText?: string;
  /** For replacements split into two rows: the inserted text */
  insertedText?: string;
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
  /** w:id attribute(s) from WordProcessing XML for cross-referencing with validation */
  xmlElementIds?: string[];
}

export interface RunStyle {
  bold?: boolean;
  italic?: boolean;
  underline?: boolean;
  strikethrough?: boolean;
  fontSize?: number; // in points
  fontFamily?: string;
  color?: string;
  highlight?: string;
  superscript?: boolean;
  subscript?: boolean;
}

export interface ParagraphStyle {
  alignment?: 'left' | 'center' | 'right' | 'justify';
  headingLevel?: number; // 1-6
  indentLeft?: number; // in points
  indentRight?: number; // in points
  indentFirstLine?: number; // in points
  spacingBefore?: number; // in points
  spacingAfter?: number; // in points
  lineSpacing?: number; // multiplier (e.g. 1.5)
  isListItem?: boolean;
  listLevel?: number;
  numberingFormat?: string;
}

export interface DocxParagraph {
  id: string;
  runs: DocxRun[];
  paragraphIndex: number;
  style?: ParagraphStyle;
  /** Footnote IDs referenced in this paragraph (in order) */
  footnoteRefs?: string[];
  /** Endnote IDs referenced in this paragraph (in order) */
  endnoteRefs?: string[];
}

export interface DocxRun {
  id: string;
  text: string;
  type: 'normal' | 'insertion' | 'deletion' | 'formatChange' | 'moveFrom' | 'moveTo' | 'footnote-ref' | 'endnote-ref';
  author?: string;
  date?: string;
  trackChangeId?: string;
  style?: RunStyle;
  /** For footnote-ref / endnote-ref runs, the footnote/endnote ID */
  refId?: string;
}

export interface ParsedDocx {
  paragraphs: DocxParagraph[];
  trackChanges: TrackChange[];
  rawXml: string;
  footnotes: FootnoteMap;
  endnotes: FootnoteMap;
}

/** Map from footnote/endnote ID to its content */
export interface FootnoteMap {
  [id: string]: FootnoteEntry;
}

export interface FootnoteEntry {
  id: string;
  text: string; // Plain text content of the footnote
  runs: DocxRun[]; // Formatted runs for rendering
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
  xmlElementIds: string[];   // "xml_element_ids" column from Excel/mapper
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
