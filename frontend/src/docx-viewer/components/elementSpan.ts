import { Decoration, ViewPlugin, type DecorationSet, type EditorView, type ViewUpdate } from '@codemirror/view';
import { syntaxTree } from '@codemirror/language';
import type { SyntaxNode } from '@lezer/common';

/**
 * Colours the WHOLE element the cursor is on — from its start tag to its end tag — so the extent of a
 * structure is visible at a glance (a long <para>…</para> or <footnote>…</footnote> otherwise looks
 * like undifferentiated text). The element is the one whose start tag, end tag or content holds the
 * cursor (self-closing tags are skipped: they have no body). The start and end tags get a stronger
 * mark than the content between them. Styling lives in PxmlEditor.css (.cm-el-body / .cm-el-tag).
 */
function elementAtCursor(view: EditorView): SyntaxNode | null {
  const head = view.state.selection.main.head;
  const tree = syntaxTree(view.state);
  for (let n: SyntaxNode | null = tree.resolveInner(head, -1); n; n = n.parent) {
    if (n.name === 'Element' && !n.getChild('SelfClosingTag')) return n;
  }
  return null;
}

function build(view: EditorView): DecorationSet {
  const el = elementAtCursor(view);
  if (!el) return Decoration.none;

  const open = el.getChild('OpenTag');
  const close = el.getChild('CloseTag');
  // Everything between the end of the start tag and the start of the end tag.
  const bodyFrom = open ? open.to : el.from;
  const bodyTo = close ? close.from : el.to;

  const ranges: ReturnType<Decoration['range']>[] = [];
  if (open) ranges.push(Decoration.mark({ class: 'cm-el-tag' }).range(open.from, open.to));
  if (bodyTo > bodyFrom) ranges.push(Decoration.mark({ class: 'cm-el-body' }).range(bodyFrom, bodyTo));
  if (close) ranges.push(Decoration.mark({ class: 'cm-el-tag' }).range(close.from, close.to));

  // A left rule down every line of the element (only the lines on screen, so a huge element such as
  // the whole chapter stays cheap) — it marks the extent even through blank / indented space.
  const doc = view.state.doc;
  for (const vr of view.visibleRanges) {
    const from = Math.max(vr.from, el.from);
    const to = Math.min(vr.to, el.to);
    if (to < from) continue;
    for (let line = doc.lineAt(from); ; line = doc.lineAt(line.to + 1)) {
      ranges.push(Decoration.line({ class: 'cm-el-line' }).range(line.from));
      if (line.to >= to || line.to + 1 > doc.length) break;
    }
  }
  return Decoration.set(ranges, true);
}
export function elementSpanHighlight() {
  return ViewPlugin.fromClass(
    class {
      decorations: DecorationSet;
      constructor(view: EditorView) {
        this.decorations = build(view);
      }
      update(u: ViewUpdate) {
        if (u.selectionSet || u.docChanged || u.viewportChanged) this.decorations = build(u.view);
      }
    },
    { decorations: (v) => v.decorations },
  );
}
