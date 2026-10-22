/**
 * Find a spot in the XML from plain text copied out of Word (a revision's context), ignoring tags and entities.
 *
 * The XML wraps words in <ital>, <cite ...>, <ref ...> and writes symbols as entities (&sect;, &hyphen;), so a
 * plain substring search for text from the Word document rarely matches. Both sides are reduced to lowercase
 * letters and digits; every kept character remembers its position in the raw XML, so a match in the reduced text
 * maps back to an exact place in the document.
 */
const ALNUM = /[A-Za-z0-9]/;

export interface AlnumMap {
  /** Lowercase letters and digits of the text content, in order. */
  s: string;
  /** For each character of `s`, its index in the raw XML. */
  raw: number[];
}

export function buildAlnumMap(doc: string): AlnumMap {
  const chars: string[] = [];
  const raw: number[] = [];
  const n = doc.length;
  for (let i = 0; i < n; i++) {
    const c = doc[i];
    if (c === '<') {                       // skip a whole tag (also comments / PIs)
      const end = doc.indexOf('>', i);
      if (end < 0) break;
      i = end;
    } else if (c === '&') {                // skip an entity such as &sect; or &#8212;
      const m = /^&[#\w.]+;/.exec(doc.slice(i, i + 16));
      if (m) { i += m[0].length - 1; } else { chars.push('&'.toLowerCase()); raw.push(i); chars.pop(); raw.pop(); }
    } else if (ALNUM.test(c)) {
      chars.push(c.toLowerCase());
      raw.push(i);
    }
  }
  return { s: chars.join(''), raw };
}

/** Letters and digits only, lowercase (how text from Word is compared with the XML). */
export function reduce(text: string): string {
  return (text || '').toLowerCase().replace(/[^a-z0-9]/g, '');
}

function nearest(positions: number[], map: AlnumMap, hint?: number): number {
  if (positions.length === 1 || hint === undefined) return positions[0];
  return positions.reduce((best, p) => (Math.abs(map.raw[p] - hint) < Math.abs(map.raw[best] - hint) ? p : best), positions[0]);
}

function allIndexes(hay: string, needle: string): number[] {
  const out: number[] = [];
  for (let at = hay.indexOf(needle); at >= 0 && out.length < 200; at = hay.indexOf(needle, at + 1)) out.push(at);
  return out;
}

const MIN_ANCHOR = 12;   // fewer reduced characters than this is too ambiguous to trust

/** Raw XML index just AFTER the end of `anchor` (e.g. the Word text that comes before an insertion). */
export function findAfterAnchor(doc: string, anchor: string, hint?: number, map: AlnumMap = buildAlnumMap(doc)): number | null {
  const tail = reduce(anchor).slice(-40);
  if (tail.length < MIN_ANCHOR) return null;
  const hits = allIndexes(map.s, tail);
  if (!hits.length) return null;
  const at = nearest(hits, map, hint);
  return skipClosingPunctuation(doc, map.raw[at + tail.length - 1] + 1);
}

/** Step over the punctuation that ends the anchor sentence ("mediation" -> after "mediation."), but never past a tag. */
export function skipClosingPunctuation(doc: string, pos: number): number {
  for (;;) {
    const c = doc[pos];
    if (c && '.,;:!?)]"\'\u201D\u2019'.includes(c)) { pos++; continue; }
    const m = c === '&' ? /^&(rdquo|rsquo|rsqb|hellip|quot|apos);/.exec(doc.slice(pos, pos + 10)) : null;
    if (m) { pos += m[0].length; continue; }
    return pos;
  }
}

/** Spaces to put around text inserted at `pos` so words do not run together (spaces go OUTSIDE the <trace>). */
export function spacingAround(doc: string, pos: number, text: string): { before: string; after: string } {
  const prev = doc[pos - 1] ?? '';
  const next = doc[pos] ?? '';
  const startsWithPunct = /^[\s.,;:!?)\]]/.test(text);
  const endsWithSpace = /\s$/.test(text);
  const before = prev && !/\s|>/.test(prev) && !startsWithPunct ? ' ' : '';
  const nextIsWord = /[A-Za-z0-9(\u201C\u2018]/.test(next) || next === '&';
  const after = nextIsWord && !endsWithSpace ? ' ' : '';
  return { before, after };
}

/** Raw XML range of `text` when it follows `before` (used to select text that Word deleted). */
export function findTextRange(doc: string, before: string, text: string, hint?: number,
                              map: AlnumMap = buildAlnumMap(doc)): { from: number; to: number } | null {
  const body = reduce(text);
  if (!body) return null;
  const lead = reduce(before).slice(-20);
  const hits = allIndexes(map.s, lead + body);
  const at = hits.length ? nearest(hits, map, hint) + lead.length : (() => {
    const bare = allIndexes(map.s, body);          // no usable context: fall back to the text alone
    return bare.length && body.length >= MIN_ANCHOR ? nearest(bare, map, hint) : -1;
  })();
  if (at < 0) return null;
  return { from: map.raw[at], to: map.raw[at + body.length - 1] + 1 };
}

/** True when every tag opened inside `fragment` is also closed inside it (so removing it cannot unbalance the XML). */
export function tagsBalanced(fragment: string): boolean {
  const stack: string[] = [];
  const re = /<(\/?)([A-Za-z][\w.\-]*)\b[^>]*?(\/?)>/g;
  for (let m = re.exec(fragment); m; m = re.exec(fragment)) {
    const [, closing, name, selfClosing] = m;
    if (selfClosing) continue;
    if (!closing) stack.push(name);
    else if (stack.pop() !== name) return false;
  }
  return stack.length === 0;
}
