/**
 * Clean text copied from the PDF text layer so it can go into the XML: PDF line breaks, end-of-line
 * hyphenation ("evalua-\ntion"), ligatures and soft hyphens are what make a raw copy unusable.
 */
const LIGATURES: Record<string, string> = { '\uFB00': 'ff', '\uFB01': 'fi', '\uFB02': 'fl', '\uFB03': 'ffi', '\uFB04': 'ffl' };
// A hyphen followed by one of these is a real compound ("pre- and post-"), not a split word.
const KEEP_AFTER_HYPHEN = new Set(['and', 'or', 'nor', 'to', 'of', 'the', 'a', 'an', 'in', 'on', 'by', 'for']);

export function cleanPdfText(raw: string): string {
  let t = (raw || '').replace(/[\uFB00-\uFB04]/g, (c) => LIGATURES[c] || c).replace(/\u00AD/g, '').replace(/\u00A0/g, ' ');
  // Split word at a line end: "evalua-\ntion" -> "evaluation" (only when the next piece starts lowercase)
  t = t.replace(/([A-Za-z])-[ \t]*\r?\n[ \t]*([a-z]+)/g, '$1$2');
  // Same split when the line break was exported as a space: "amend- ing" -> "amending"
  t = t.replace(/([A-Za-z]{2})- ([a-z]{2,})\b/g, (m, a, b) => (KEEP_AFTER_HYPHEN.has(b) ? m : a + b));
  return t.replace(/\s*\r?\n\s*/g, ' ').replace(/[ \t]{2,}/g, ' ').trim();
}
