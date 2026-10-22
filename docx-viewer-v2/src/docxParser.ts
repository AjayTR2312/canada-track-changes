import JSZip from 'jszip';
import type { ParsedDocx, DocxParagraph, DocxRun, TrackChange, RunStyle, ParagraphStyle, FootnoteMap, FootnoteEntry } from './types';

let changeCounter = 0;

function generateId(prefix: string): string {
  changeCounter++;
  return `${prefix}-${changeCounter}`;
}

/** Get the local tag name without namespace prefix */
function localName(el: Element): string {
  return el.tagName.includes(':') ? el.tagName.split(':')[1] : el.tagName;
}

/** Extract text from w:t nodes that are direct children of a run */
function getRunText(runEl: Element): string {
  let text = '';
  for (let i = 0; i < runEl.childNodes.length; i++) {
    const child = runEl.childNodes[i] as Element;
    if (!child.tagName) continue;
    const name = localName(child);
    if (name === 't') {
      text += child.textContent || '';
    } else if (name === 'delText') {
      text += child.textContent || '';
    } else if (name === 'tab') {
      text += '\t';
    } else if (name === 'br') {
      text += '\n';
    }
    // footnoteReference and endnoteReference are skipped - 
    // they are just markers, Word renders the number automatically
  }
  return text;
}

/** Container tags that can wrap runs / track-change elements */
const CONTAINER_TAGS = new Set([
  'hyperlink', 'smartTag', 'fldSimple', 'sdtContent',
]);

/** Extract run-level formatting properties from w:rPr */
function getRunStyle(runEl: Element): RunStyle | undefined {
  const rPr = Array.from(runEl.childNodes).find(
    (n) => (n as Element).tagName && localName(n as Element) === 'rPr'
  ) as Element | undefined;

  if (!rPr) return undefined;

  const style: RunStyle = {};
  let hasStyle = false;

  for (let i = 0; i < rPr.childNodes.length; i++) {
    const child = rPr.childNodes[i] as Element;
    if (!child.tagName) continue;
    const name = localName(child);

    switch (name) {
      case 'b':
      case 'bCs': {
        const val = child.getAttribute('w:val');
        if (val !== '0' && val !== 'false') {
          style.bold = true;
          hasStyle = true;
        }
        break;
      }
      case 'i':
      case 'iCs': {
        const val = child.getAttribute('w:val');
        if (val !== '0' && val !== 'false') {
          style.italic = true;
          hasStyle = true;
        }
        break;
      }
      case 'u': {
        const val = child.getAttribute('w:val');
        if (val && val !== 'none') {
          style.underline = true;
          hasStyle = true;
        }
        break;
      }
      case 'strike':
      case 'dstrike': {
        const val = child.getAttribute('w:val');
        if (val !== '0' && val !== 'false') {
          style.strikethrough = true;
          hasStyle = true;
        }
        break;
      }
      case 'sz':
      case 'szCs': {
        const val = child.getAttribute('w:val');
        if (val) {
          style.fontSize = parseInt(val) / 2; // Half-points to points
          hasStyle = true;
        }
        break;
      }
      case 'rFonts': {
        const font = child.getAttribute('w:ascii') || child.getAttribute('w:hAnsi') || child.getAttribute('w:cs');
        if (font) {
          style.fontFamily = font;
          hasStyle = true;
        }
        break;
      }
      case 'color': {
        const val = child.getAttribute('w:val');
        if (val && val !== 'auto') {
          style.color = `#${val}`;
          hasStyle = true;
        }
        break;
      }
      case 'highlight': {
        const val = child.getAttribute('w:val');
        if (val && val !== 'none') {
          style.highlight = val;
          hasStyle = true;
        }
        break;
      }
      case 'vertAlign': {
        const val = child.getAttribute('w:val');
        if (val === 'superscript') {
          style.superscript = true;
          hasStyle = true;
        } else if (val === 'subscript') {
          style.subscript = true;
          hasStyle = true;
        }
        break;
      }
    }
  }

  return hasStyle ? style : undefined;
}

/** Extract paragraph-level formatting properties from w:pPr */
function getParagraphStyle(pElement: Element): ParagraphStyle | undefined {
  const pPr = Array.from(pElement.childNodes).find(
    (n) => (n as Element).tagName && localName(n as Element) === 'pPr'
  ) as Element | undefined;

  if (!pPr) return undefined;

  const style: ParagraphStyle = {};
  let hasStyle = false;

  for (let i = 0; i < pPr.childNodes.length; i++) {
    const child = pPr.childNodes[i] as Element;
    if (!child.tagName) continue;
    const name = localName(child);

    switch (name) {
      case 'jc': {
        const val = child.getAttribute('w:val');
        if (val) {
          if (val === 'both') {
            style.alignment = 'justify';
          } else if (val === 'center' || val === 'left' || val === 'right') {
            style.alignment = val;
          }
          hasStyle = true;
        }
        break;
      }
      case 'pStyle': {
        const val = child.getAttribute('w:val');
        if (val) {
          // Detect heading level from style names like "Heading1", "heading 1", etc.
          const headingMatch = val.match(/[Hh]eading\s*(\d)/);
          if (headingMatch) {
            style.headingLevel = parseInt(headingMatch[1]);
            hasStyle = true;
          }
          // Title style
          if (val.toLowerCase() === 'title') {
            style.headingLevel = 1;
            hasStyle = true;
          }
          if (val.toLowerCase() === 'subtitle') {
            style.headingLevel = 2;
            hasStyle = true;
          }
          // List styles
          if (val.toLowerCase().includes('list')) {
            style.isListItem = true;
            hasStyle = true;
          }
        }
        break;
      }
      case 'ind': {
        const left = child.getAttribute('w:left') || child.getAttribute('w:start');
        const right = child.getAttribute('w:right') || child.getAttribute('w:end');
        const firstLine = child.getAttribute('w:firstLine');
        const hanging = child.getAttribute('w:hanging');
        if (left) {
          style.indentLeft = parseInt(left) / 20; // Twips to points
          hasStyle = true;
        }
        if (right) {
          style.indentRight = parseInt(right) / 20;
          hasStyle = true;
        }
        if (firstLine) {
          style.indentFirstLine = parseInt(firstLine) / 20;
          hasStyle = true;
        }
        if (hanging) {
          style.indentFirstLine = -(parseInt(hanging) / 20);
          hasStyle = true;
        }
        break;
      }
      case 'spacing': {
        const before = child.getAttribute('w:before');
        const after = child.getAttribute('w:after');
        const line = child.getAttribute('w:line');
        const lineRule = child.getAttribute('w:lineRule');
        if (before) {
          style.spacingBefore = parseInt(before) / 20;
          hasStyle = true;
        }
        if (after) {
          style.spacingAfter = parseInt(after) / 20;
          hasStyle = true;
        }
        if (line) {
          const lineVal = parseInt(line);
          if (lineRule === 'auto' || !lineRule) {
            style.lineSpacing = lineVal / 240; // 240 = single spacing
          } else {
            style.lineSpacing = lineVal / 240;
          }
          hasStyle = true;
        }
        break;
      }
      case 'numPr': {
        style.isListItem = true;
        hasStyle = true;
        const ilvl = Array.from(child.childNodes).find(
          (n) => (n as Element).tagName && localName(n as Element) === 'ilvl'
        ) as Element | undefined;
        if (ilvl) {
          const level = ilvl.getAttribute('w:val');
          if (level) {
            style.listLevel = parseInt(level);
          }
        }
        break;
      }
    }
  }

  return hasStyle ? style : undefined;
}

// ─── Context length (characters before/after change) ───────────────────────
const CONTEXT_CHARS = 20;

// ─── Types for internal processing ─────────────────────────────────────────

interface NodeInfo {
  type: 'normal' | 'insertion' | 'deletion' | 'moveTo' | 'moveFrom' | 'footnote-ref' | 'endnote-ref';
  text: string;
  author: string;
  date: string;
  /** Sequential position in the paragraph's node list */
  position: number;
  /** Run-level formatting */
  style?: RunStyle;
  /** For footnote/endnote refs: the reference ID */
  refId?: string;
  /** w:id from the WordProcessing XML ins/del element */
  xmlId?: string;
}

/** Check if a run element contains a footnoteReference or endnoteReference */
function getFootnoteRefFromRun(runEl: Element): { type: 'footnote' | 'endnote'; id: string } | null {
  for (let i = 0; i < runEl.childNodes.length; i++) {
    const child = runEl.childNodes[i] as Element;
    if (!child.tagName) continue;
    const name = localName(child);
    if (name === 'footnoteReference') {
      return { type: 'footnote', id: child.getAttribute('w:id') || '' };
    }
    if (name === 'endnoteReference') {
      return { type: 'endnote', id: child.getAttribute('w:id') || '' };
    }
  }
  return null;
}

/**
 * First-pass: Walk all children of a paragraph and collect node info in order.
 * This preserves the exact document order needed for replacement detection.
 */
function collectParagraphNodes(parent: Element): NodeInfo[] {
  const nodes: NodeInfo[] = [];
  let position = 0;

  function walkForNodes(el: Element): void {
    const children = el.childNodes;
    for (let i = 0; i < children.length; i++) {
      const child = children[i] as Element;
      if (!child.tagName) continue;
      const name = localName(child);

      switch (name) {
        case 'r': {
          // Check for footnote/endnote reference first
          const fnRef = getFootnoteRefFromRun(child);
          if (fnRef) {
            const refType = fnRef.type === 'footnote' ? 'footnote-ref' as const : 'endnote-ref' as const;
            nodes.push({
              type: refType,
              text: fnRef.id, // Will be replaced with display number later
              author: '',
              date: '',
              position: position++,
              style: { superscript: true },
              refId: fnRef.id,
            });
          }
          const text = getRunText(child);
          if (text) {
            const style = getRunStyle(child);
            nodes.push({ type: 'normal', text, author: '', date: '', position: position++, style });
          }
          break;
        }
        case 'ins': {
          const author = child.getAttribute('w:author') || 'Unknown';
          const date = child.getAttribute('w:date') || '';
          const xmlId = child.getAttribute('w:id') || '';
          const descendantRuns = child.getElementsByTagName('w:r');
          let fullText = '';
          let style: RunStyle | undefined;
          for (let j = 0; j < descendantRuns.length; j++) {
            fullText += getRunText(descendantRuns[j]);
            if (!style) style = getRunStyle(descendantRuns[j]);
          }
          if (fullText) {
            nodes.push({ type: 'insertion', text: fullText, author, date, position: position++, style, xmlId });
          }
          break;
        }
        case 'del': {
          const author = child.getAttribute('w:author') || 'Unknown';
          const date = child.getAttribute('w:date') || '';
          const xmlId = child.getAttribute('w:id') || '';
          const descendantRuns = child.getElementsByTagName('w:r');
          let fullText = '';
          let style: RunStyle | undefined;
          for (let j = 0; j < descendantRuns.length; j++) {
            fullText += getRunText(descendantRuns[j]);
            if (!style) style = getRunStyle(descendantRuns[j]);
          }
          if (fullText) {
            nodes.push({ type: 'deletion', text: fullText, author, date, position: position++, style, xmlId });
          }
          break;
        }
        case 'moveTo': {
          const author = child.getAttribute('w:author') || 'Unknown';
          const date = child.getAttribute('w:date') || '';
          const xmlId = child.getAttribute('w:id') || '';
          const descendantRuns = child.getElementsByTagName('w:r');
          let fullText = '';
          let style: RunStyle | undefined;
          for (let j = 0; j < descendantRuns.length; j++) {
            fullText += getRunText(descendantRuns[j]);
            if (!style) style = getRunStyle(descendantRuns[j]);
          }
          if (fullText) {
            nodes.push({ type: 'moveTo', text: fullText, author, date, position: position++, style, xmlId });
          }
          break;
        }
        case 'moveFrom': {
          const author = child.getAttribute('w:author') || 'Unknown';
          const date = child.getAttribute('w:date') || '';
          const xmlId = child.getAttribute('w:id') || '';
          const descendantRuns = child.getElementsByTagName('w:r');
          let fullText = '';
          let style: RunStyle | undefined;
          for (let j = 0; j < descendantRuns.length; j++) {
            fullText += getRunText(descendantRuns[j]);
            if (!style) style = getRunStyle(descendantRuns[j]);
          }
          if (fullText) {
            nodes.push({ type: 'moveFrom', text: fullText, author, date, position: position++, style, xmlId });
          }
          break;
        }
        case 'sdt': {
          walkForNodes(child);
          break;
        }
        default: {
          if (CONTAINER_TAGS.has(name)) {
            walkForNodes(child);
          }
          break;
        }
      }
    }
  }

  walkForNodes(parent);
  return nodes;
}

/**
 * Build paragraph full text (what the current state of the paragraph looks like):
 * Normal text + insertions (since they're accepted state), excluding deletions.
 */
function buildParagraphText(nodes: NodeInfo[]): string {
  return nodes
    .filter(n => n.type === 'normal' || n.type === 'insertion' || n.type === 'moveTo')
    .map(n => n.text)
    .join('');
}

/**
 * Calculate prev_context and post_context for a change node.
 * prev_context = last N chars of text before this change in the paragraph.
 * post_context = first N chars of text after this change in the paragraph.
 */
function calculateContext(nodes: NodeInfo[], changeIndex: number): { prevContext: string; postContext: string } {
  let textBefore = '';
  let textAfter = '';

  // Text before: accumulate normal/insertion text from nodes before this one
  for (let i = 0; i < changeIndex; i++) {
    const n = nodes[i];
    if (n.type === 'normal' || n.type === 'insertion' || n.type === 'moveTo') {
      textBefore += n.text;
    }
  }

  // Text after: accumulate normal/insertion text from nodes after this one
  for (let i = changeIndex + 1; i < nodes.length; i++) {
    const n = nodes[i];
    if (n.type === 'normal' || n.type === 'insertion' || n.type === 'moveTo') {
      textAfter += n.text;
    }
  }

  return {
    prevContext: textBefore.slice(-CONTEXT_CHARS),
    postContext: textAfter.slice(0, CONTEXT_CHARS),
  };
}

/**
 * Determine if this is a paragraph-level change:
 * - paragraph_deletion: ALL content in the paragraph is deleted (no normal/insertion text)
 * - paragraph_addition: ALL content in the paragraph is inserted (no normal/deletion text)
 */
function classifyParagraphLevel(nodes: NodeInfo[]): 'paragraph_deletion' | 'paragraph_addition' | null {
  if (nodes.length === 0) return null;

  const hasNormal = nodes.some(n => n.type === 'normal');
  const hasInsertion = nodes.some(n => n.type === 'insertion' || n.type === 'moveTo');
  const hasDeletion = nodes.some(n => n.type === 'deletion' || n.type === 'moveFrom');

  // Entire paragraph is deleted: only deletion nodes, no normal or insertion
  if (hasDeletion && !hasNormal && !hasInsertion) {
    return 'paragraph_deletion';
  }

  // Entire paragraph is added: only insertion nodes, no normal or deletion
  if (hasInsertion && !hasNormal && !hasDeletion) {
    return 'paragraph_addition';
  }

  return null;
}

/**
 * Detect inline replacements: a deletion immediately followed by an insertion
 * at the same position (adjacent nodes in the paragraph).
 * Returns grouped changes matching the backend's logic.
 */
interface ClassifiedChange {
  type: TrackChange['type'];
  deletedText: string;
  insertedText: string;
  text: string;
  author: string;
  date: string;
  nodeIndex: number;
  xmlIds: string[];
}

function classifyChanges(nodes: NodeInfo[]): ClassifiedChange[] {
  const changes: ClassifiedChange[] = [];

  // First check paragraph-level
  const paraLevel = classifyParagraphLevel(nodes);

  if (paraLevel === 'paragraph_deletion') {
    const delNodes = nodes.filter(n => n.type === 'deletion' || n.type === 'moveFrom');
    const fullText = delNodes.map(n => n.text).join('');
    const author = delNodes[0]?.author || 'Unknown';
    const date = delNodes[0]?.date || '';
    const xmlIds = delNodes.map(n => n.xmlId).filter(Boolean) as string[];
    changes.push({
      type: 'paragraph_deletion',
      deletedText: fullText,
      insertedText: '',
      text: fullText,
      author,
      date,
      nodeIndex: 0,
      xmlIds,
    });
    return changes;
  }

  if (paraLevel === 'paragraph_addition') {
    const insNodes = nodes.filter(n => n.type === 'insertion' || n.type === 'moveTo');
    const fullText = insNodes.map(n => n.text).join('');
    const author = insNodes[0]?.author || 'Unknown';
    const date = insNodes[0]?.date || '';
    const xmlIds = insNodes.map(n => n.xmlId).filter(Boolean) as string[];
    changes.push({
      type: 'paragraph_addition',
      deletedText: '',
      insertedText: fullText,
      text: fullText,
      author,
      date,
      nodeIndex: 0,
      xmlIds,
    });
    return changes;
  }

  // Inline changes: detect replacements (adjacent del + ins)
  let i = 0;
  while (i < nodes.length) {
    const node = nodes[i];

    if (node.type === 'normal') {
      i++;
      continue;
    }

    if (node.type === 'deletion' || node.type === 'moveFrom') {
      // Check if next non-normal node is an insertion (= replacement)
      let nextIdx = i + 1;
      while (nextIdx < nodes.length && nodes[nextIdx].type === 'normal' && nodes[nextIdx].text.trim() === '') {
        nextIdx++;
      }

      if (nextIdx < nodes.length && (nodes[nextIdx].type === 'insertion' || nodes[nextIdx].type === 'moveTo')) {
        // This is an inline_replacement — backend splits it into 2 rows
        const delText = node.text;
        const insText = nodes[nextIdx].text;

        // Row 1: the deletion part
        changes.push({
          type: 'inline_deletion',
          deletedText: delText,
          insertedText: '',
          text: delText,
          author: node.author,
          date: node.date,
          nodeIndex: i,
          xmlIds: node.xmlId ? [node.xmlId] : [],
        });

        // Row 2: the insertion part
        changes.push({
          type: 'inline_insertion',
          deletedText: '',
          insertedText: insText,
          text: insText,
          author: nodes[nextIdx].author,
          date: nodes[nextIdx].date,
          nodeIndex: nextIdx,
          xmlIds: nodes[nextIdx].xmlId ? [nodes[nextIdx].xmlId] : [],
        });

        i = nextIdx + 1;
        continue;
      }

      // Standalone deletion
      changes.push({
        type: node.type === 'moveFrom' ? 'moveFrom' : 'inline_deletion',
        deletedText: node.text,
        insertedText: '',
        text: node.text,
        author: node.author,
        date: node.date,
        nodeIndex: i,
        xmlIds: node.xmlId ? [node.xmlId] : [],
      });
      i++;
      continue;
    }

    if (node.type === 'insertion' || node.type === 'moveTo') {
      // Standalone insertion (not paired with preceding deletion)
      changes.push({
        type: node.type === 'moveTo' ? 'moveTo' : 'inline_insertion',
        deletedText: '',
        insertedText: node.text,
        text: node.text,
        author: node.author,
        date: node.date,
        nodeIndex: i,
        xmlIds: node.xmlId ? [node.xmlId] : [],
      });
      i++;
      continue;
    }

    i++;
  }

  return changes;
}

/**
 * Get closest non-empty previous paragraph text (matching backend _get_previous_para_text).
 */
function getPreviousPara(paragraphIndex: number, allParagraphTexts: Map<number, string>): string {
  for (let idx = paragraphIndex - 1; idx >= 0; idx--) {
    const text = allParagraphTexts.get(idx);
    if (text && text.trim().length > 3) {
      return text;
    }
  }
  return '';
}

/**
 * Get closest non-empty next paragraph text (matching backend _get_post_para_text).
 */
function getPostPara(paragraphIndex: number, allParagraphTexts: Map<number, string>, maxIndex: number): string {
  for (let idx = paragraphIndex + 1; idx <= maxIndex; idx++) {
    const text = allParagraphTexts.get(idx);
    if (text && text.trim().length > 3) {
      return text;
    }
  }
  return '';
}

/** Build DocxRun entries from nodes (for the DocxViewer display) */
function buildRuns(nodes: NodeInfo[]): DocxRun[] {
  return nodes
    .filter(n => n.text)
    .map((n, idx) => ({
      id: generateId('run'),
      text: n.text,
      type: n.type === 'insertion' ? 'insertion' as const
        : n.type === 'deletion' ? 'deletion' as const
        : n.type === 'moveTo' ? 'moveTo' as const
        : n.type === 'moveFrom' ? 'moveFrom' as const
        : n.type === 'footnote-ref' ? 'footnote-ref' as const
        : n.type === 'endnote-ref' ? 'endnote-ref' as const
        : 'normal' as const,
      author: n.author || undefined,
      date: n.date || undefined,
      style: n.style,
      refId: n.refId,
      // nodePosition stored so we can link trackChangeId later
      _nodePosition: n.position,
    }));
}

// ─── Merge nearby track changes ─────────────────────────────────────────────

/** Whitespace-only text (incl. tabs/newlines we synthesised for w:tab/w:br). */
function isWhitespaceOnly(text: string): boolean {
  return /^\s*$/.test(text);
}

/**
 * Merge "nearby" track changes across the WHOLE document so the viewer's
 * revision list lines up 1:1 with the pipeline's validation report.
 *
 * Mirrors canada_pipeline/exe_track_changes_1.py exactly:
 *   - TextProcessor.flatten_trace_tags() merges adjacent <trace> insertions that
 *     are separated only by whitespace, empty <para></para> blocks, or a single
 *     </para><para> boundary.
 *   - MapperGenerator.merge_consecutive_deletions() merges consecutive deletions
 *     that share the same "Previous Content" — i.e. deletions with nothing but
 *     other deletions (and whitespace / paragraph breaks) between them.
 *
 * Word routinely splits one logical revision into several w:ins / w:del runs
 * (around footnotes, formatting boundaries, language runs) and across paragraph
 * boundaries. We scan every run in document order (across paragraph boundaries)
 * and fold a change into the previous one when BOTH hold:
 *   - they are the same mergeable type (insertion↔insertion, deletion↔deletion)
 *   - everything between them is only whitespace and/or paragraph boundaries.
 * Any real text, a move, or a format change between two changes ends the run and
 * starts a fresh revision — exactly as the pipeline's regexes do.
 */
function mergeNearbyChanges(
  paragraphs: DocxParagraph[],
  changes: TrackChange[],
): TrackChange[] {
  if (changes.length <= 1) return changes;

  // Maps an original (folded) track-change id -> the canonical id it merged into.
  const remap = new Map<string, string>();

  // Determine the "merge-family" of a TrackChange type
  const getMergeFamily = (type: string): string | null => {
    if (type === 'inline_insertion' || type === 'paragraph_addition') return 'insertion';
    if (type === 'inline_deletion' || type === 'paragraph_deletion') return 'deletion';
    return null;
  };

  let prevId: string | null = null;
  let prevFamily: string | null = null;

  for (const paragraph of paragraphs) {
    for (const run of paragraph.runs) {
      if (run.trackChangeId && (run.type === 'insertion' || run.type === 'deletion')) {
        const tcId = run.trackChangeId;
        // Find the family of this track change
        const tc = changes.find(c => c.id === tcId);
        const family = tc ? getMergeFamily(tc.type) : null;

        if (family) {
          const canonical = remap.get(tcId) ?? tcId;

          if (prevId !== null && prevFamily === family && canonical !== prevId) {
            // Same-family change with only whitespace/paragraph breaks before it ->
            // fold it into the still-open revision.
            remap.set(tcId, prevId);
            run.trackChangeId = prevId;
            // prevId/prevFamily stay the same: the merged revision is still "open".
          } else {
            remap.set(tcId, canonical);
            run.trackChangeId = canonical;
            prevId = canonical;
            prevFamily = family;
          }
        } else {
          prevId = null;
          prevFamily = null;
        }
      } else if (run.type === 'normal' && isWhitespaceOnly(run.text)) {
        // A stray untracked space/tab/newline doesn't split a revision — the
        // pipeline collapses "</trace>\s*<trace>" and whitespace-only gaps too.
        // Keep the current revision open.
      } else if (run.type === 'footnote-ref' || run.type === 'endnote-ref') {
        // Footnote/endnote references don't break a revision either.
      } else {
        // Real text, a move, or a format change ends the current revision.
        prevId = null;
        prevFamily = null;
      }
    }
    // NOTE: a paragraph boundary on its own does NOT end a revision (the pipeline
    // merges insertions and deletions across "</para><para>" and empty <para>
    // blocks), so we deliberately do not reset prevId/prevFamily here.
  }

  // Recompute each surviving change's text from its (remapped) runs, in order.
  const textById = new Map<string, string>();
  for (const paragraph of paragraphs) {
    for (const run of paragraph.runs) {
      if (run.trackChangeId && (run.type === 'insertion' || run.type === 'deletion')) {
        textById.set(
          run.trackChangeId,
          (textById.get(run.trackChangeId) ?? '') + run.text,
        );
      }
    }
  }

  // Build merged list: drop folded entries, update text on survivors
  const merged: TrackChange[] = [];
  const seen = new Set<string>();

  for (const ch of changes) {
    const canonical = remap.get(ch.id) ?? ch.id;
    if (canonical !== ch.id) continue; // folded into an earlier change — drop it
    if (seen.has(ch.id)) continue; // avoid duplicates
    seen.add(ch.id);

    const mergedText = textById.get(ch.id);
    merged.push(
      mergedText ? { ...ch, text: mergedText } : ch,
    );
  }

  return merged;
}

/**
 * After classifying changes and building runs, link each run's trackChangeId
 * to the corresponding TrackChange entry.
 */
function linkRunsToChanges(
  runs: DocxRun[],
  nodes: NodeInfo[],
  classifiedChanges: ClassifiedChange[],
  trackChangeIds: string[],
): void {
  // Build a map: nodePosition → trackChangeId
  const positionToTcId = new Map<number, string>();

  for (let i = 0; i < classifiedChanges.length; i++) {
    const change = classifiedChanges[i];
    const tcId = trackChangeIds[i];
    const nodeIdx = change.nodeIndex;

    // For paragraph-level changes, all change nodes get linked
    if (change.type === 'paragraph_deletion' || change.type === 'paragraph_addition') {
      for (const node of nodes) {
        if (node.type !== 'normal') {
          positionToTcId.set(node.position, tcId);
        }
      }
    } else {
      // Single node
      if (nodeIdx >= 0 && nodeIdx < nodes.length) {
        positionToTcId.set(nodes[nodeIdx].position, tcId);
      }
    }
  }

  // Now set trackChangeId on matching runs
  for (const run of runs) {
    const pos = (run as any)._nodePosition;
    if (pos !== undefined && positionToTcId.has(pos)) {
      run.trackChangeId = positionToTcId.get(pos);
    }
    delete (run as any)._nodePosition;
  }
}

export async function parseDocxFile(file: File): Promise<ParsedDocx> {
  changeCounter = 0;

  const arrayBuffer = await file.arrayBuffer();
  const zip = await JSZip.loadAsync(arrayBuffer);

  const documentXml = await zip.file('word/document.xml')?.async('string');
  if (!documentXml) {
    throw new Error('Invalid DOCX file: no word/document.xml found');
  }

  const parser = new DOMParser();
  const xmlDoc = parser.parseFromString(documentXml, 'text/xml');

  // ─── Parse footnotes and endnotes ─────────────────────────────────────────
  const footnotes: FootnoteMap = {};
  const endnotes: FootnoteMap = {};

  const footnotesXml = await zip.file('word/footnotes.xml')?.async('string');
  if (footnotesXml) {
    const fnDoc = parser.parseFromString(footnotesXml, 'text/xml');
    const fnElements = fnDoc.getElementsByTagName('w:footnote');
    for (let i = 0; i < fnElements.length; i++) {
      const fnEl = fnElements[i];
      const fnId = fnEl.getAttribute('w:id') || '';
      const fnType = fnEl.getAttribute('w:type') || '';
      // Skip separator/continuation footnotes (ids 0, -1)
      if (fnType === 'separator' || fnType === 'continuationSeparator' || fnId === '0' || fnId === '-1') continue;

      // Collect all paragraphs inside this footnote
      const fnParas = fnEl.getElementsByTagName('w:p');
      let fullText = '';
      const allRuns: DocxRun[] = [];

      for (let j = 0; j < fnParas.length; j++) {
        const nodes = collectParagraphNodes(fnParas[j]);
        const text = buildParagraphText(nodes);
        const runs = buildRuns(nodes);
        fullText += (j > 0 ? ' ' : '') + text;
        allRuns.push(...runs);
      }

      // Clean up _nodePosition from runs
      for (const run of allRuns) {
        delete (run as any)._nodePosition;
      }

      if (fullText.trim()) {
        footnotes[fnId] = { id: fnId, text: fullText.trim(), runs: allRuns };
      }
    }
  }

  const endnotesXml = await zip.file('word/endnotes.xml')?.async('string');
  if (endnotesXml) {
    const enDoc = parser.parseFromString(endnotesXml, 'text/xml');
    const enElements = enDoc.getElementsByTagName('w:endnote');
    for (let i = 0; i < enElements.length; i++) {
      const enEl = enElements[i];
      const enId = enEl.getAttribute('w:id') || '';
      const enType = enEl.getAttribute('w:type') || '';
      if (enType === 'separator' || enType === 'continuationSeparator' || enId === '0' || enId === '-1') continue;

      const enParas = enEl.getElementsByTagName('w:p');
      let fullText = '';
      const allRuns: DocxRun[] = [];

      for (let j = 0; j < enParas.length; j++) {
        const nodes = collectParagraphNodes(enParas[j]);
        const text = buildParagraphText(nodes);
        const runs = buildRuns(nodes);
        fullText += (j > 0 ? ' ' : '') + text;
        allRuns.push(...runs);
      }

      for (const run of allRuns) {
        delete (run as any)._nodePosition;
      }

      if (fullText.trim()) {
        endnotes[enId] = { id: enId, text: fullText.trim(), runs: allRuns };
      }
    }
  }

  const paragraphs: DocxParagraph[] = [];
  const allTrackChanges: TrackChange[] = [];

  const pElements = xmlDoc.getElementsByTagName('w:p');

  // ─── Pass 1: Collect all paragraph nodes and their text ─────────────────
  interface ParagraphData {
    nodes: NodeInfo[];
    paragraphText: string;
    pStyle?: ParagraphStyle;
  }

  const paragraphDataList: ParagraphData[] = [];
  const allParagraphTexts = new Map<number, string>();

  for (let i = 0; i < pElements.length; i++) {
    const pElement = pElements[i];
    const nodes = collectParagraphNodes(pElement);
    const paragraphText = buildParagraphText(nodes);
    const pStyle = getParagraphStyle(pElement);

    paragraphDataList.push({ nodes, paragraphText, pStyle });
    allParagraphTexts.set(i, paragraphText);
  }

  // ─── Pass 2: Classify changes and build enriched TrackChange entries ────
  let revisionOrder = 1;

  for (let i = 0; i < paragraphDataList.length; i++) {
    const { nodes, paragraphText, pStyle } = paragraphDataList[i];
    const runs = buildRuns(nodes);

    // Classify changes in this paragraph (matching backend logic)
    const classifiedChanges = classifyChanges(nodes);

    // Build enriched TrackChange entries and collect their IDs for linking
    const tcIds: string[] = [];
    for (const change of classifiedChanges) {
      const { prevContext, postContext } = calculateContext(nodes, change.nodeIndex);
      const previousPara = getPreviousPara(i, allParagraphTexts);
      const postPara = getPostPara(i, allParagraphTexts, paragraphDataList.length - 1);

      const tcId = generateId('tc');
      tcIds.push(tcId);

      allTrackChanges.push({
        id: tcId,
        type: change.type,
        author: change.author,
        date: change.date,
        text: change.text,
        paragraphIndex: i,
        runIndex: change.nodeIndex,
        revisionOrder: revisionOrder++,
        deletedText: change.deletedText || undefined,
        insertedText: change.insertedText || undefined,
        prevContext,
        postContext,
        paragraphText,
        previousPara,
        postPara,
        xmlElementIds: change.xmlIds.length > 0 ? change.xmlIds : undefined,
      });
    }

    // Link runs to their corresponding TrackChange IDs for highlighting
    linkRunsToChanges(runs, nodes, classifiedChanges, tcIds);

    // Collect footnote/endnote references from this paragraph's nodes
    const footnoteRefs: string[] = [];
    const endnoteRefs: string[] = [];
    for (const node of nodes) {
      if (node.type === 'footnote-ref' && node.refId) {
        footnoteRefs.push(node.refId);
      } else if (node.type === 'endnote-ref' && node.refId) {
        endnoteRefs.push(node.refId);
      }
    }

    paragraphs.push({
      id: generateId('para'),
      runs,
      paragraphIndex: i,
      style: pStyle,
      footnoteRefs: footnoteRefs.length > 0 ? footnoteRefs : undefined,
      endnoteRefs: endnoteRefs.length > 0 ? endnoteRefs : undefined,
    });
  }

  // ─── Also parse footnotes and endnotes for track changes ────────────────
  const footnoteFiles = ['word/footnotes.xml', 'word/endnotes.xml'];
  for (const fn of footnoteFiles) {
    const xml = await zip.file(fn)?.async('string');
    if (!xml) continue;

    const doc = parser.parseFromString(xml, 'text/xml');
    const fnParagraphs = doc.getElementsByTagName('w:p');

    for (let i = 0; i < fnParagraphs.length; i++) {
      const pElement = fnParagraphs[i];
      const paraIdx = paragraphs.length;
      const nodes = collectParagraphNodes(pElement);
      const paragraphText = buildParagraphText(nodes);
      const runs = buildRuns(nodes);

      if (runs.length > 0) {
        allParagraphTexts.set(paraIdx, paragraphText);

        const classifiedChanges = classifyChanges(nodes);
        const tcIds: string[] = [];
        for (const change of classifiedChanges) {
          const { prevContext, postContext } = calculateContext(nodes, change.nodeIndex);
          const previousPara = getPreviousPara(paraIdx, allParagraphTexts);
          const postPara = getPostPara(paraIdx, allParagraphTexts, paraIdx);

          const tcId = generateId('tc');
          tcIds.push(tcId);

          allTrackChanges.push({
            id: tcId,
            type: change.type,
            author: change.author,
            date: change.date,
            text: change.text,
            paragraphIndex: paraIdx,
            runIndex: change.nodeIndex,
            revisionOrder: revisionOrder++,
            deletedText: change.deletedText || undefined,
            insertedText: change.insertedText || undefined,
            prevContext,
            postContext,
            paragraphText,
            previousPara,
            postPara,
            xmlElementIds: change.xmlIds.length > 0 ? change.xmlIds : undefined,
          });
        }

        linkRunsToChanges(runs, nodes, classifiedChanges, tcIds);

        paragraphs.push({
          id: generateId('para'),
          runs,
          paragraphIndex: paraIdx,
        });
      }
    }
  }

  const rawXml = formatXml(documentXml);

  // ─── Merge Word-split revisions across paragraph boundaries ─────────────
  // Word routinely splits one logical revision into several w:ins/w:del runs
  // (around footnotes, formatting boundaries, language runs) and across
  // paragraph boundaries. This merges them so the viewer's revision list
  // matches the pipeline's validation report (canada_pipeline/exe_track_changes_1.py).
  const mergedTrackChanges = mergeNearbyChanges(paragraphs, allTrackChanges);

  return {
    paragraphs,
    trackChanges: mergedTrackChanges,
    rawXml,
    footnotes,
    endnotes,
  };
}

function formatXml(xml: string): string {
  let formatted = '';
  let indent = '';
  const tab = '  ';

  xml.split(/>\s*</).forEach((node) => {
    if (node.match(/^\/\w/)) {
      indent = indent.substring(tab.length);
    }
    formatted += indent + '<' + node + '>\r\n';
    if (node.match(/^<?\w[^>]*[^/]$/) && !node.startsWith('?')) {
      indent += tab;
    }
  });

  return formatted.substring(1, formatted.length - 3);
}
