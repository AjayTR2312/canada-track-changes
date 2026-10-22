# Visual Validator — "Pop out panel to second screen" feature

Lets testers detach any of the three Visual Validator panels (**Revisions**, **Document / PDF-DOCX**,
**PXML editor**) into a separate browser window and drag it onto a second monitor. The detached panel
stays **fully live** (same session, selected revision, editor/PDF state, scroll & undo) because it is
**relocated, not remounted**.

## Files changed (all under `frontend/src/docx-viewer/`)

| Status | File |
|--------|------|
| 🆕 New | `frontend/src/docx-viewer/components/PanelPortal.tsx` |
| ✏️ Modified | `frontend/src/docx-viewer/DocxViewerApp.tsx` |
| ✏️ Modified | `frontend/src/docx-viewer/DocxViewerApp.css` |
| ✏️ Modified | `frontend/src/docx-viewer/components/PdfViewer.tsx` |

No new dependencies (pure React portals + CSS). Frontend-only. `npm run build` passes.

---

## 1. NEW FILE — `frontend/src/docx-viewer/components/PanelPortal.tsx`

Core of the feature. Renders a panel into a **stable** DOM container that is physically moved between
its inline slot and a detached window, so React never remounts it. Copies stylesheets + theme into the
window, handles dock-back, window-close, popup-blocker, and rescues the live node before the window closes.

```tsx
import { useEffect, useRef, type ReactNode, type RefObject } from 'react';
import { createPortal } from 'react-dom';

interface PanelPortalProps {
  popped: boolean;
  title: string;
  theme: 'dark' | 'light';
  onClose: () => void;
  hostRef: RefObject<HTMLElement | null>;
  width?: number;
  height?: number;
  children: ReactNode;
}

export default function PanelPortal({
  popped, title, theme, onClose, hostRef, width = 1000, height = 850, children,
}: PanelPortalProps) {
  // Stable container — created once, never swapped. `display: contents` keeps the panel's
  // own children laid out as if they were direct children of host / window content.
  const elRef = useRef<HTMLDivElement | null>(null);
  if (!elRef.current) {
    const el = document.createElement('div');
    el.style.display = 'contents';
    elRef.current = el;
  }
  const winRef = useRef<Window | null>(null);

  // Keep the element attached to its inline host whenever docked (re-attaches if host remounts).
  useEffect(() => {
    if (popped) return;
    const el = elRef.current!;
    const host = hostRef.current;
    if (host && el.parentNode !== host) host.appendChild(el);
  });

  // Open/close the detached window and move the element into/out of it.
  useEffect(() => {
    if (!popped) return;
    const el = elRef.current!;

    const features = `popup=yes,width=${width},height=${height},menubar=no,toolbar=no,location=no,status=no`;
    const win = window.open('', `vv_popout_${Date.now()}`, features);
    if (!win) {
      window.alert(
        'The pop-out window was blocked by the browser.\n\n' +
          'Please allow pop-ups for this site, then try "Pop out" again.',
      );
      onClose();
      return;
    }
    winRef.current = win;
    const doc = win.document;
    doc.title = `${title} — Visual Validator`;

    const base = doc.createElement('base');
    base.href = window.location.href;
    doc.head.appendChild(base);

    document.querySelectorAll('style, link[rel="stylesheet"]').forEach((node) => {
      doc.head.appendChild(node.cloneNode(true));
    });

    doc.documentElement.setAttribute('data-theme', theme);
    doc.body.style.margin = '0';
    doc.body.style.background = theme === 'light' ? '#f5f5f5' : '#11111b';

    const body = doc.createElement('div');
    body.className = 'popout-body';

    const titlebar = doc.createElement('div');
    titlebar.className = 'popout-titlebar';
    const titleSpan = doc.createElement('span');
    titleSpan.className = 'popout-title';
    titleSpan.textContent = title;
    const dockBtn = doc.createElement('button');
    dockBtn.type = 'button';
    dockBtn.className = 'popout-dock-btn';
    dockBtn.textContent = '⤡ Dock back';
    dockBtn.title = 'Return this panel to the main window';
    dockBtn.addEventListener('click', () => onClose());
    titlebar.appendChild(titleSpan);
    titlebar.appendChild(dockBtn);

    const content = doc.createElement('div');
    content.className = 'popout-content';
    content.appendChild(el); // move the live panel into the window — no remount

    body.appendChild(titlebar);
    body.appendChild(content);
    doc.body.appendChild(body);

    // Rescue the live element back to the main document before the window goes away.
    const rescue = () => {
      const host = hostRef.current;
      try {
        if (host) host.appendChild(el);
        else document.body.appendChild(el);
      } catch { /* ignore */ }
    };
    const handleHide = () => { rescue(); onClose(); };
    win.addEventListener('pagehide', handleHide);
    win.addEventListener('beforeunload', handleHide);

    const closeOnMainUnload = () => { try { win.close(); } catch { /* ignore */ } };
    window.addEventListener('beforeunload', closeOnMainUnload);

    const poll = window.setInterval(() => {
      if (win.closed) { window.clearInterval(poll); rescue(); onClose(); }
    }, 500);

    return () => {
      window.clearInterval(poll);
      win.removeEventListener('pagehide', handleHide);
      win.removeEventListener('beforeunload', handleHide);
      window.removeEventListener('beforeunload', closeOnMainUnload);
      rescue();
      try { win.close(); } catch { /* ignore */ }
      winRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [popped]);

  // Keep the detached window's theme in sync with the main window.
  useEffect(() => {
    const win = winRef.current;
    if (popped && win && !win.closed) {
      win.document.documentElement.setAttribute('data-theme', theme);
      win.document.body.style.background = theme === 'light' ? '#f5f5f5' : '#11111b';
    }
  }, [theme, popped]);

  return createPortal(children, elRef.current);
}
```

---

## 2. `frontend/src/docx-viewer/DocxViewerApp.tsx`

### 2a. Import (top of file)
```tsx
import PanelPortal from './components/PanelPortal';
```

### 2b. State + handlers (next to the existing panel-collapse state)
```tsx
// Pop-out: detach a panel into a separate browser window (second-monitor support).
const [poppedOut, setPoppedOut] = useState<{ left: boolean; center: boolean; right: boolean }>({
  left: false, center: false, right: false,
});
// Inline slots a panel occupies while docked (PanelPortal relocates the live panel between these and the windows).
const leftHostRef = useRef<HTMLDivElement | null>(null);
const centerHostRef = useRef<HTMLDivElement | null>(null);
const rightHostRef = useRef<HTMLDivElement | null>(null);
const popOut = useCallback((panel: 'left' | 'center' | 'right') => {
  setPoppedOut(prev => ({ ...prev, [panel]: true }));
  try {
    if (!localStorage.getItem('vv-popout-tip-seen')) {
      localStorage.setItem('vv-popout-tip-seen', '1');
      setPopoutTip(true);
    }
  } catch { /* ignore */ }
}, []);
const dockBack = useCallback((panel: 'left' | 'center' | 'right') => {
  setPoppedOut(prev => ({ ...prev, [panel]: false }));
}, []);
const [popoutTip, setPopoutTip] = useState(false);
const popoutBrowser = useMemo(() => {
  const ua = navigator.userAgent;
  if (/Edg\//.test(ua)) return 'Microsoft Edge';
  if (/OPR\//.test(ua) || /Opera/.test(ua)) return 'Opera';
  if (/Firefox\//.test(ua)) return 'Firefox';
  if (/Chrome\//.test(ua)) return 'Google Chrome';
  if (/Safari\//.test(ua)) return 'Safari';
  return 'your current browser';
}, []);
```

### 2c. Panel content moved into helpers that take an `inline` flag
The existing panel JSX (TrackChangesPanel/DraftPanel, DocxViewer/PdfViewer, PxmlEditor) was moved into
three helpers: `renderLeftPanel(inline)`, `renderCenterPanel(inline)`, `renderRightPanel(inline)`.
`inline` toggles the in-app **⧉ Pop out** / **Hide** controls (hidden in the detached window, which has
its own Dock-back button). The panel contents themselves are unchanged. New button examples:

```tsx
// Left panel tab-bar (inline only):
<button className="left-tab-popout" onClick={() => popOut('left')}
  title="Open this panel in a separate window — drag it to your other screen">⧉ Pop out</button>

// Document & PXML panels (inline only), floating:
<button className="panel-popout-btn" onClick={() => popOut('center')} title="...">⧉ Pop out</button>
<button className="panel-popout-btn right" onClick={() => popOut('right')} title="...">⧉ Pop out</button>
```

### 2d. Layout bookkeeping (before the main `return`)
```tsx
const leftInline = !leftCollapsed && !poppedOut.left;
const centerInline = !centerCollapsed && !poppedOut.center;
const rightInline = !poppedOut.right;
const rightGrow = (centerCollapsed || poppedOut.center) && rightInline;
const showLeftCenterHandle = leftInline && centerInline;
const showCenterRightHandle = centerInline && rightInline && !rightGrow;
const needsFlexSpacer = !(centerInline || rightGrow);
```

### 2e. Inline panels become empty host slots, e.g.
```tsx
{leftInline && (
  <div ref={leftHostRef} className="panel panel-left" style={{ width: leftPanelWidth, minWidth: 220 }} />
)}
// ...plus a "popped" rail when detached:
{!leftCollapsed && poppedOut.left && (
  <div className="panel-rail panel-rail-popped" onClick={() => dockBack('left')}
    title="Revisions panel is open in a separate window — click to dock it back">
    <span>⧉ Revisions ↗</span>
  </div>
)}
```
(Same pattern for center `centerHostRef` / right `rightHostRef`.)

### 2f. Three always-mounted PanelPortals + the one-time tip (after `.panels-container`)
```tsx
<PanelPortal popped={poppedOut.left} hostRef={leftHostRef} title="Revisions"
  theme={theme} onClose={() => dockBack('left')} width={560} height={900}>
  {renderLeftPanel(!poppedOut.left)}
</PanelPortal>
<PanelPortal popped={poppedOut.center} hostRef={centerHostRef} title="Document"
  theme={theme} onClose={() => dockBack('center')} width={1100} height={900}>
  {renderCenterPanel(!poppedOut.center)}
</PanelPortal>
<PanelPortal popped={poppedOut.right} hostRef={rightHostRef} title="PXML Editor"
  theme={theme} onClose={() => dockBack('right')} width={900} height={900}>
  {renderRightPanel(!poppedOut.right)}
</PanelPortal>

{popoutTip && (
  <div className="popout-tip" role="status">
    <span className="popout-tip-icon">💡</span>
    <div className="popout-tip-body">
      <strong>Pop-outs open in {popoutBrowser}.</strong>
      <span>A detached panel always opens in the same browser as this app. To use a
        different browser, open the Visual Validator there before popping out.</span>
    </div>
    <button className="popout-tip-close" onClick={() => setPopoutTip(false)}
      title="Dismiss this tip">Got it</button>
  </div>
)}
```

---

## 3. `frontend/src/docx-viewer/DocxViewerApp.css`

- Added `position: relative;` to `.panel-right` (anchors the floating pop-out button).
- Appended styles for: `.left-tab-popout`, `.panel-popout-btn` (+ `.right`), `.panel-rail-popped`,
  `.panel-flex-spacer`, the detached-window layout (`.popout-body`, `.popout-panel`, `.popout-titlebar`,
  `.popout-title`, `.popout-dock-btn`, `.popout-content`) and the one-time tip
  (`.popout-tip`, `.popout-tip-icon`, `.popout-tip-body`, `.popout-tip-close`), each with
  `[data-theme="light"]` variants.

---

## 4. `frontend/src/docx-viewer/components/PdfViewer.tsx` — cross-window fix

When the PDF panel is popped to another screen, text selection happens in **that** window's document.
The code read it from the main `window`/`document`, so PDF→PXML navigation silently did nothing.
Fix: read selection & highlights from the PDF panel's **own** document / container.

```tsx
// Text-selection handler (mouseup):
- const sel = window.getSelection();
+ const sel = container.ownerDocument.getSelection();

// Clearing the selection from the "✕" button:
- window.getSelection()?.removeAllRanges();
+ containerRef.current?.ownerDocument.getSelection()?.removeAllRanges();

// Highlight clears scoped to the PDF container instead of the global document:
- document.querySelectorAll('.pdf-find-hit, .pdf-find-hit-current')      // markFindHits + find-effect
+ (containerRef.current ?? document).querySelectorAll('.pdf-find-hit, .pdf-find-hit-current')
- document.querySelectorAll('.pdf-text-highlight')                        // highlightInTextLayer
+ (containerRef.current ?? document).querySelectorAll('.pdf-text-highlight')
```

---

## How it works for the tester
1. Click **⧉ Pop out** on any panel → it opens in a separate window.
2. Drag the window to the second monitor.
3. The main screen reclaims the space; a slim rail (e.g. `⧉ Revisions ↗`) stays behind — click it, the
   window's **⤡ Dock back** button, or just close the window to re-dock. All state is preserved.

> Note: the pop-out always opens in the same browser as the app (browser security — it can't be forced to
> a different browser). A one-time tip reminds testers to open the app in their preferred browser first.
