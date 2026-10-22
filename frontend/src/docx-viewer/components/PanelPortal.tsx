import { useEffect, useRef, type ReactNode, type RefObject } from 'react';
import { createPortal } from 'react-dom';

interface PanelPortalProps {
  /** When true, the panel lives in a detached window; otherwise it's docked inline. */
  popped: boolean;
  /** Window title + heading shown to the tester. */
  title: string;
  /** Current app theme, mirrored into the detached window. */
  theme: 'dark' | 'light';
  /** Called when the detached window closes (user, dock-back, or app shutdown). */
  onClose: () => void;
  /** The inline slot the panel occupies when docked. */
  hostRef: RefObject<HTMLElement | null>;
  width?: number;
  height?: number;
  children: ReactNode;
}

/**
 * Renders a panel into a *stable* DOM container that is physically relocated
 * between the main layout and a detached browser window. Because the container
 * node identity never changes, React never remounts the panel — so the PDF
 * viewer, the CodeMirror editor, scroll position, undo history and all loaded
 * content survive popping out and docking back. The detached window can be
 * dragged onto a second monitor.
 */
export default function PanelPortal({
  popped,
  title,
  theme,
  onClose,
  hostRef,
  width = 1000,
  height = 850,
  children,
}: PanelPortalProps) {
  // Stable container — created once, never swapped. `display: contents` keeps
  // the panel's own children laid out exactly as if they were direct children
  // of the host (inline) or the detached window content.
  const elRef = useRef<HTMLDivElement | null>(null);
  if (!elRef.current) {
    const el = document.createElement('div');
    el.style.display = 'contents';
    elRef.current = el;
  }
  const winRef = useRef<Window | null>(null);

  // Keep the element attached to its inline host whenever it is docked. Runs on
  // every commit (cheap parent check) so it re-attaches if the host remounts
  // (e.g. after collapse/expand).
  useEffect(() => {
    if (popped) return;
    const el = elRef.current!;
    const host = hostRef.current;
    if (host && el.parentNode !== host) {
      host.appendChild(el);
    }
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

    // Resolve relative asset URLs (fonts, pdf.js worker, etc.) against the app.
    const base = doc.createElement('base');
    base.href = window.location.href;
    doc.head.appendChild(base);

    // Clone every stylesheet so the panel looks identical (covers Vite dev
    // <style> injection and production <link rel=stylesheet>).
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

    // Rescue the live element back to the main document before the window goes
    // away, otherwise closing it would destroy the panel's DOM/state.
    const rescue = () => {
      const host = hostRef.current;
      try {
        if (host) host.appendChild(el);
        else document.body.appendChild(el);
      } catch {
        /* ignore */
      }
    };
    const handleHide = () => {
      rescue();
      onClose();
    };
    win.addEventListener('pagehide', handleHide);
    win.addEventListener('beforeunload', handleHide);

    const closeOnMainUnload = () => {
      try {
        win.close();
      } catch {
        /* ignore */
      }
    };
    window.addEventListener('beforeunload', closeOnMainUnload);

    // Fallback for browsers that don't fire pagehide/beforeunload on the child.
    const poll = window.setInterval(() => {
      if (win.closed) {
        window.clearInterval(poll);
        rescue();
        onClose();
      }
    }, 500);

    return () => {
      window.clearInterval(poll);
      win.removeEventListener('pagehide', handleHide);
      win.removeEventListener('beforeunload', handleHide);
      window.removeEventListener('beforeunload', closeOnMainUnload);
      rescue(); // move the panel out before we close the window
      try {
        win.close();
      } catch {
        /* ignore */
      }
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
