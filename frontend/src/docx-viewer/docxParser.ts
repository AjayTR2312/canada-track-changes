import JSZip from 'jszip';
import type { ParsedDocx, DocxParagraph, DocxRun, TrackChange, RevisionCounts, RevisionStats, FormatRevision } from './types';

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
    } else if (name === 'footnoteReference') {
      const fnId = child.getAttribute('w:id') || '';
      text += `[footnote ${fnId}]`;
    } else if (name === 'endnoteReference') {
      const enId = child.getAttribute('w:id') || '';
      text += `[endnote ${enId}]`;
    }
  }
  return text;
}

/** Container tags that can wrap runs / track-change elements */
const CONTAINER_TAGS = new Set([
  'hyperlink', 'smartTag', 'fldSimple', 'sdtContent',
]);

/**
 * Recursively walk all children of a parent element inside a paragraph.
 * Handles: w:r, w:ins, w:del, w:moveTo, w:moveFrom, containers, and
 * nested w:rPrChange / w:pPrChange.
 */
function walkNodes(
  parent: Element,
  paragraphIndex: number,
  runs: DocxRun[],
  changes: TrackChange[],
): void {
  const children = parent.childNodes;

  for (let i = 0; i < children.length; i++) {
    const child = children[i] as Element;
    if (!child.tagName) continue;
    const name = localName(child);

    switch (name) {
      // ── normal run ──────────────────────────────────────────────
      case 'r': {
        const text = getRunText(child);
        if (text) {
          runs.push({ id: generateId('run'), text, type: 'normal' });
        }
        break;
      }

      // ── insertion ───────────────────────────────────────────────
      case 'ins': {
        handleTrackedChange(child, 'insertion', paragraphIndex, runs, changes);
        break;
      }

      // ── deletion ────────────────────────────────────────────────
      case 'del': {
        handleTrackedChange(child, 'deletion', paragraphIndex, runs, changes);
        break;
      }

      // ── move-to (destination of moved text, shown like insertion) ──
      case 'moveTo': {
        handleTrackedChange(child, 'moveTo', paragraphIndex, runs, changes);
        break;
      }

      // ── move-from (source of moved text, shown like deletion) ──
      case 'moveFrom': {
        handleTrackedChange(child, 'moveFrom', paragraphIndex, runs, changes);
        break;
      }

      // ── sdt wraps sdtContent which holds the real content ──────
      case 'sdt': {
        // Recurse into sdt – walkNodes will hit sdtContent below
        walkNodes(child, paragraphIndex, runs, changes);
        break;
      }

      default: {
        // Recurse into known container elements
        if (CONTAINER_TAGS.has(name)) {
          walkNodes(child, paragraphIndex, runs, changes);
        }
        break;
      }
    }
  }
}

/**
 * Handle a tracked-change wrapper (w:ins, w:del, w:moveTo, w:moveFrom).
 * Collects runs inside it, creates Run + TrackChange entries.
 */
function handleTrackedChange(
  el: Element,
  type: 'insertion' | 'deletion' | 'moveTo' | 'moveFrom',
  paragraphIndex: number,
  runs: DocxRun[],
  changes: TrackChange[],
): void {
  const author = el.getAttribute('w:author') || 'Unknown';
  const date = el.getAttribute('w:date') || '';
  const trackChangeId = generateId('tc');

  let fullText = '';

  // Walk all descendant runs (may be nested inside hyperlinks etc.)
  const descendantRuns = el.getElementsByTagName('w:r');
  for (let j = 0; j < descendantRuns.length; j++) {
    const text = getRunText(descendantRuns[j]);
    if (text) {
      fullText += text;
      runs.push({
        id: generateId('run'),
        text,
        type,
        author,
        date,
        trackChangeId,
      });
    }
  }

  if (fullText) {
    changes.push({
      id: trackChangeId,
      type,
      author,
      date,
      text: fullText,
      paragraphIndex,
      runIndex: runs.length - 1,
      prevContext: '',
      postContext: '',
      paragraphText: '',
      previousPara: '',
      postPara: '',
    });
  }
}

const MERGEABLE_CHANGE_TYPES = new Set(['insertion', 'deletion']);

/** Whitespace-only text (incl. tabs / newlines we synthesised for w:tab / w:br). */
function isWhitespaceOnly(text: string): boolean {
  return /^\s*$/.test(text);
}

/**
 * Merge "nearby" track changes across the WHOLE document so the viewer's
 * revision list lines up 1:1 with the pipeline's validation report.
 *
 * Mirrors usforms_pipeline/exe_track_changes_1.py exactly:
 *   - TextProcessor.flatten_trace_tags() merges adjacent <trace> insertions that
 *     are separated only by whitespace, empty <para></para> blocks, or a single
 *     </para><para> boundary.
 *   - MapperGenerator.merge_consecutive_deletions() merges consecutive deletions
 *     that share the same "Previous Content" — i.e. deletions with nothing but
 *     other deletions (and whitespace / paragraph breaks) between them.
 *
 * Word routinely splits one logical revision into several w:ins / w:del runs
 * (around footnotes, formatting boundaries, language runs) and across paragraph
 * boundaries. The previous version merged only runs that were *directly*
 * adjacent inside a *single* paragraph, so it under-merged versus the pipeline
 * and the on-screen count drifted from the report.
 *
 * We now scan every run in document order (across paragraph boundaries) and fold
 * a change into the previous one when BOTH hold:
 *   - they are the same mergeable type (insertion↔insertion, deletion↔deletion), and
 *   - everything between them is only whitespace and/or paragraph boundaries.
 * Any real text, a move, or a format change between two changes ends the run and
 * starts a fresh revision — exactly as the pipeline's regexes do. Switching type
 * (e.g. a deletion between two insertions) likewise starts a new revision, which
 * reproduces "different Previous Content" without computing it explicitly.
 *
 * Runs are mutated so every piece of a merged revision carries the canonical
 * trackChangeId (the DocxViewer highlights by that id), and each surviving
 * change's text is recomputed from its pieces in order.
 */
function mergeNearbyChanges(
  paragraphs: DocxParagraph[],
  changes: TrackChange[],
): TrackChange[] {
  // Maps an original (folded) track-change id -> the canonical id it merged into.
  const remap = new Map<string, string>();

  let prevId: string | null = null;
  let prevType: string | null = null;

  for (const paragraph of paragraphs) {
    for (const run of paragraph.runs) {
      if (run.trackChangeId && MERGEABLE_CHANGE_TYPES.has(run.type)) {
        const canonical = remap.get(run.trackChangeId) ?? run.trackChangeId;
        if (prevId !== null && prevType === run.type && canonical !== prevId) {
          // Same-type change with only whitespace/paragraph breaks before it ->
          // fold it into the still-open revision.
          remap.set(run.trackChangeId, prevId);
          run.trackChangeId = prevId;
          // prevId / prevType stay the same: the merged revision is still "open".
        } else {
          remap.set(run.trackChangeId, canonical);
          run.trackChangeId = canonical;
          prevId = canonical;
          prevType = run.type;
        }
      } else if (run.type === 'normal' && isWhitespaceOnly(run.text)) {
        // A stray untracked space/tab/newline doesn't split a revision — the
        // pipeline collapses "</trace>\s*<trace>" and whitespace-only gaps too.
        // Keep the current revision open.
      } else {
        // Real text, a move, or a format change ends the current revision.
        prevId = null;
        prevType = null;
      }
    }
    // NOTE: a paragraph boundary on its own does NOT end a revision (the pipeline
    // merges insertions and deletions across "</para><para>" and empty <para>
    // blocks), so we deliberately do not reset prevId / prevType here.
  }

  // Recompute each surviving change's text from its (remapped) runs, in order.
  const textById = new Map<string, string>();
  for (const paragraph of paragraphs) {
    for (const run of paragraph.runs) {
      if (run.trackChangeId && MERGEABLE_CHANGE_TYPES.has(run.type)) {
        textById.set(
          run.trackChangeId,
          (textById.get(run.trackChangeId) ?? '') + run.text,
        );
      }
    }
  }

  const merged: TrackChange[] = [];
  for (const ch of changes) {
    const canonical = remap.get(ch.id) ?? ch.id;
    if (canonical !== ch.id) continue; // folded into an earlier change — drop it
    merged.push(
      MERGEABLE_CHANGE_TYPES.has(ch.type) ? { ...ch, text: textById.get(ch.id) ?? ch.text } : ch,
    );
  }
  return merged;
}

function parseRuns(paragraph: Element, paragraphIndex: number): { runs: DocxRun[]; changes: TrackChange[] } {
  const runs: DocxRun[] = [];
  const changes: TrackChange[] = [];
  walkNodes(paragraph, paragraphIndex, runs, changes);
  // Merging is done globally per section in parseDocxFile so that Word-split
  // revisions spanning paragraph boundaries are collapsed too.
  return { runs, changes };
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

  const paragraphs: DocxParagraph[] = [];
  const trackChanges: TrackChange[] = [];
  // Every individual Word edit, before mergeNearbyChanges folds them together.
  const rawChanges: TrackChange[] = [];

  // --- Main document body: merged as one section ---
  const pElements = xmlDoc.getElementsByTagName('w:p');
  const bodyStart = paragraphs.length;
  const bodyChanges: TrackChange[] = [];

  for (let i = 0; i < pElements.length; i++) {
    const pElement = pElements[i];
    const { runs, changes } = parseRuns(pElement, i);

    paragraphs.push({
      id: generateId('para'),
      runs,
      paragraphIndex: i,
    });

    bodyChanges.push(...changes);
  }

  // Collapse Word-split revisions across paragraph boundaries so the viewer's
  // revision list matches the pipeline's validation report.
  rawChanges.push(...bodyChanges);
  trackChanges.push(...mergeNearbyChanges(paragraphs.slice(bodyStart), bodyChanges));

  // Also parse footnotes and endnotes for track changes. Each file is its own
  // merge section so a body revision can't bleed into a note (or vice versa).
  const footnoteFiles = ['word/footnotes.xml', 'word/endnotes.xml'];
  for (const fn of footnoteFiles) {
    const xml = await zip.file(fn)?.async('string');
    if (!xml) continue;

    const doc = parser.parseFromString(xml, 'text/xml');
    const fnParagraphs = doc.getElementsByTagName('w:p');

    const sectionStart = paragraphs.length;
    const sectionChanges: TrackChange[] = [];

    for (let i = 0; i < fnParagraphs.length; i++) {
      const pElement = fnParagraphs[i];
      // Use a paragraph index that continues from the main document
      const paraIdx = paragraphs.length;
      const { runs, changes } = parseRuns(pElement, paraIdx);

      if (runs.length > 0 || changes.length > 0) {
        paragraphs.push({
          id: generateId('para'),
          runs,
          paragraphIndex: paraIdx,
        });
        sectionChanges.push(...changes);
      }
    }

    rawChanges.push(...sectionChanges);
    trackChanges.push(...mergeNearbyChanges(paragraphs.slice(sectionStart), sectionChanges));
  }

  const rawXml = formatXml(documentXml);

  // Formatting revisions (w:rPrChange / w:pPrChange ...) are not listed as cards (they never reach
  // the validation report) but Word counts them, so count them the way Word's Revisions pane does.
  const formatRevisions = collectFormatRevisions(xmlDoc, 0);
  for (const fn of footnoteFiles) {
    const xml = await zip.file(fn)?.async('string');
    if (xml) formatRevisions.push(...collectFormatRevisions(parser.parseFromString(xml, 'text/xml'), 1_000_000));
  }
  const formatCount = formatRevisions.length;

  // Enrich track changes with surrounding context (needed for PDF fuzzy matching)
  enrichTrackChangesContext(paragraphs, trackChanges);

  return {
    paragraphs,
    trackChanges,
    rawXml,
    stats: computeRevisionStats(rawChanges, trackChanges, formatCount),
    formatRevisions,
  };
}

const TOGGLES: Record<string, string> = { b: 'Bold', i: 'Italic', strike: 'Strikethrough', caps: 'All caps', smallCaps: 'Small caps', dstrike: 'Double strikethrough' };

/** Property element -> a comparable string ("" when absent). Toggles are on unless w:val is 0/false. */
function propMap(props: Element | null): Record<string, string> {
  const out: Record<string, string> = {};
  if (!props) return out;
  for (let i = 0; i < props.childNodes.length; i++) {
    const el = props.childNodes[i] as Element;
    if (!el.tagName) continue;
    const key = localName(el);
    if (key === 'rPrChange' || key === 'pPrChange' || key === 'rPr') continue;
    if (key in TOGGLES) {
      const v = (el.getAttribute('w:val') || '').toLowerCase();
      out[key] = v === '0' || v === 'false' ? '' : 'on';
    } else {
      const attrs = Array.from(el.attributes).map((a) => `${a.value}`).join('/');
      out[key] = attrs || 'on';
    }
  }
  return out;
}

/** Human description of what a formatting revision changed, e.g. ["Bold added", "Size 12→14 pt"]. */
function describeFormatDiff(before: Record<string, string>, after: Record<string, string>): string[] {
  const out: string[] = [];
  for (const key of new Set([...Object.keys(before), ...Object.keys(after)])) {
    const a = before[key] || '';
    const b = after[key] || '';
    if (a === b) continue;
    if (key in TOGGLES) out.push(`${TOGGLES[key]} ${b ? 'added' : 'removed'}`);
    else if (key === 'u') out.push(b ? (a ? 'Underline style changed' : 'Underline added') : 'Underline removed');
    else if (key === 'vertAlign') out.push(b ? `${b === 'superscript' ? 'Superscript' : b === 'subscript' ? 'Subscript' : 'Vertical align'} added` : 'Superscript/subscript removed');
    else if (key === 'sz' || key === 'szCs') { if (key === 'sz') out.push(`Size ${a ? Number(a) / 2 : '–'} → ${b ? Number(b) / 2 : '–'} pt`); }
    else if (key === 'color') out.push(`Colour ${a || 'auto'} → ${b || 'auto'}`);
    else if (key === 'rFonts') out.push(`Font ${a.split('/')[0] || 'default'} → ${b.split('/')[0] || 'default'}`);
    else if (key === 'highlight') out.push(b ? `Highlight ${b}` : 'Highlight removed');
    else if (key === 'jc') out.push(`Alignment ${a || 'left'} → ${b || 'left'}`);
    else if (key === 'pStyle') out.push(`Style ${a || 'Normal'} → ${b || 'Normal'}`);
    else if (key === 'ind') out.push('Indentation changed');
    else if (key === 'spacing') out.push('Spacing changed');
    else if (key === 'numPr') out.push(b ? 'List / numbering applied' : 'List / numbering removed');
    else if (key === 'lang' || key === 'noProof' || key === 'rStyle') { /* not a visible edit */ }
    else out.push(`${key} ${b ? 'changed' : 'removed'}`);
  }
  return out;
}

/** Formatting revisions as Word counts them: neighbouring runs carrying a character-format change
 *  are one revision; every paragraph / section / table property change is one. */
function collectFormatRevisions(doc: Document, offset: number): FormatRevision[] {
  const found: FormatRevision[] = [];
  const paras = doc.getElementsByTagName('w:p');
  for (let i = 0; i < paras.length; i++) {
    const para = paras[i];
    const paraText = Array.from(para.getElementsByTagName('w:r')).map((r) => getRunText(r)).join('');
    const runs = para.getElementsByTagName('w:r');
    let group: { text: string; author: string; date: string; before: Record<string, string>; after: Record<string, string>; start: number } | null = null;
    let cursor = 0;
    const flush = () => {
      if (!group) return;
      const changes = describeFormatDiff(group.before, group.after);
      found.push({
        id: generateId('fmt'), kind: 'character', author: group.author, date: group.date,
        text: group.text, changes: changes.length ? changes : ['Formatting changed'],
        paragraphIndex: offset + i,
        prevContext: paraText.slice(Math.max(0, group.start - 80), group.start),
        postContext: paraText.slice(group.start + group.text.length, group.start + group.text.length + 80),
        paragraphText: paraText,
      });
      group = null;
    };
    for (let j = 0; j < runs.length; j++) {
      const run = runs[j];
      const text = getRunText(run);
      const chg = run.getElementsByTagName('w:rPrChange')[0];
      if (chg && run.parentNode && localName(run.parentNode as Element) !== 'del') {
        let rPr: Element | null = null;
        for (let k = 0; k < run.childNodes.length; k++) {
          const c = run.childNodes[k] as Element;
          if (c.tagName && localName(c) === 'rPr') rPr = c;
        }
        let oldRPr: Element | null = null;
        for (let k = 0; k < chg.childNodes.length; k++) {
          const c = chg.childNodes[k] as Element;
          if (c.tagName && localName(c) === 'rPr') oldRPr = c;
        }
        if (!group) {
          group = { text: '', author: chg.getAttribute('w:author') || 'Unknown', date: chg.getAttribute('w:date') || '', before: propMap(oldRPr), after: propMap(rPr), start: cursor };
        } else {
          // merge: keep the union of differences
          Object.assign(group.before, propMap(oldRPr));
          Object.assign(group.after, propMap(rPr));
        }
        group.text += text;
      } else {
        flush();
      }
      cursor += text.length;
    }
    flush();

    const pChg = para.getElementsByTagName('w:pPrChange');
    for (let k = 0; k < pChg.length; k++) {
      const chg = pChg[k];
      const pPr = chg.parentNode as Element;
      let oldPPr: Element | null = null;
      for (let m = 0; m < chg.childNodes.length; m++) {
        const c = chg.childNodes[m] as Element;
        if (c.tagName && localName(c) === 'pPr') oldPPr = c;
      }
      const changes = describeFormatDiff(propMap(oldPPr), propMap(pPr));
      found.push({
        id: generateId('fmt'), kind: 'paragraph', author: chg.getAttribute('w:author') || 'Unknown', date: chg.getAttribute('w:date') || '',
        text: paraText.slice(0, 120), changes: changes.length ? changes : ['Paragraph formatting changed'],
        paragraphIndex: offset + i, prevContext: '', postContext: paraText.slice(120, 200), paragraphText: paraText,
      });
    }
  }
  for (const [tag, label] of [['w:sectPrChange', 'Section layout changed'], ['w:tblPrChange', 'Table formatting changed'], ['w:trPrChange', 'Table row formatting changed'], ['w:tcPrChange', 'Table cell formatting changed']] as const) {
    const els = doc.getElementsByTagName(tag);
    for (let k = 0; k < els.length; k++) {
      found.push({ id: generateId('fmt'), kind: 'other', author: els[k].getAttribute('w:author') || 'Unknown', date: els[k].getAttribute('w:date') || '',
        text: '', changes: [label], paragraphIndex: -1, prevContext: '', postContext: '', paragraphText: '' });
    }
  }
  return found;
}

function countByType(changes: TrackChange[]): RevisionCounts {
  const c: RevisionCounts = { insertions: 0, deletions: 0, moves: 0, formatChanges: 0, total: 0 };
  for (const ch of changes) {
    if (ch.type === 'insertion') c.insertions++;
    else if (ch.type === 'deletion') c.deletions++;
    else if (ch.type === 'moveFrom' || ch.type === 'moveTo') c.moves++;
    else if (ch.type === 'formatChange') c.formatChanges++;
    c.total++;
  }
  return c;
}

function computeRevisionStats(raw: TrackChange[], grouped: TrackChange[], formatCount = 0): RevisionStats {
  let insertedChars = 0;
  let deletedChars = 0;
  for (const ch of grouped) {
    if (ch.type === 'insertion') insertedChars += ch.text.length;
    else if (ch.type === 'deletion') deletedChars += ch.text.length;
  }
  const rawCounts = countByType(raw);
  const groupedCounts = countByType(grouped);
  rawCounts.formatChanges = groupedCounts.formatChanges = formatCount;
  return { raw: rawCounts, grouped: groupedCounts, insertedChars, deletedChars };
}

/**
 * Enrich each TrackChange with contextual information from its paragraph:
 * prevContext, postContext, paragraphText, previousPara, postPara.
 * This enables the PDF viewer's fuzzy page-matching logic.
 */
function enrichTrackChangesContext(paragraphs: DocxParagraph[], changes: TrackChange[]): void {
  // Build a lookup: paragraphIndex -> full text of that paragraph
  const paraTextMap = new Map<number, string>();
  for (const p of paragraphs) {
    paraTextMap.set(p.paragraphIndex, p.runs.map(r => r.text).join(''));
  }

  for (const change of changes) {
    const paraIdx = change.paragraphIndex;
    const paraRuns = paragraphs.find(p => p.paragraphIndex === paraIdx)?.runs ?? [];
    const fullParaText = paraTextMap.get(paraIdx) ?? '';
    change.paragraphText = fullParaText;

    // Compute prevContext and postContext within the paragraph
    let before = '';
    let after = '';
    let foundChange = false;
    for (const run of paraRuns) {
      if (run.trackChangeId === change.id) {
        foundChange = true;
      } else if (!foundChange) {
        before += run.text;
      } else {
        after += run.text;
      }
    }
    change.prevContext = before.slice(-100); // last 100 chars before
    change.postContext = after.slice(0, 100); // first 100 chars after

    // Find closest non-empty previous paragraph
    change.previousPara = '';
    for (let i = paraIdx - 1; i >= 0; i--) {
      const t = paraTextMap.get(i) ?? '';
      if (t.trim()) { change.previousPara = t; break; }
    }

    // Find closest non-empty next paragraph
    change.postPara = '';
    for (let i = paraIdx + 1; i <= paragraphs.length; i++) {
      const t = paraTextMap.get(i) ?? '';
      if (t.trim()) { change.postPara = t; break; }
    }
  }
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
