# Hide WIP controls in the Revisions panel ("Locate" + "Fix with agent")

Temporarily hides two work-in-progress controls from testers in the **Revisions** panel of the Visual
Validator. The code is kept intact behind a single feature flag so it can be re-enabled later without
re-implementing anything.

**Hidden controls**
- 📍 **Locate** button (on insertion/deletion revisions)
- 🤖 **Fix with agent** section (the `ResolverAgent` panel)

**Not affected:** 📋 Copy, ➕ Insert, ➖ Mark deleted still work as before.

## File changed

| Status | File |
|--------|------|
| ✏️ Modified | `frontend/src/docx-viewer/components/TrackChangesPanel.tsx` |

Frontend-only. No new dependencies. `npm run build` passes.

---

## Change detail — `frontend/src/docx-viewer/components/TrackChangesPanel.tsx`

### 1. Feature flag (added near the top, after the imports)
```tsx
import './TrackChangesPanel.css';

// WIP: hide the "Locate" and "Fix with agent" controls from testers for now.
// Flip to true to re-enable them when the work resumes.
const SHOW_WIP_FEATURES = false;
```

### 2. Gate the "Locate" button
```tsx
- {onLocateInXml && (currentChange.type === 'insertion' || currentChange.type === 'deletion') && (
+ {SHOW_WIP_FEATURES && onLocateInXml && (currentChange.type === 'insertion' || currentChange.type === 'deletion') && (
```

### 3. Gate the "Fix with agent" section
```tsx
- {editorApiRef && (currentChange.type === 'insertion' || currentChange.type === 'deletion') && (
+ {SHOW_WIP_FEATURES && editorApiRef && (currentChange.type === 'insertion' || currentChange.type === 'deletion') && (
```

---

## How to re-enable later
Set the flag to `true` in `TrackChangesPanel.tsx`:
```tsx
const SHOW_WIP_FEATURES = true;
```
Both controls reappear immediately — no other changes required.
