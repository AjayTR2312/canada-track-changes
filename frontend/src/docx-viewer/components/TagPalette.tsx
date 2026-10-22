import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import './TagPalette.css';

export interface DtdSchemaElement {
  type: 'empty' | 'mixed' | 'element' | 'any' | string;
  children: string[];
  /** Index into DtdSchema.attribute_sets (attribute lists are de-duplicated). */
  attrs?: number;
}

export interface DtdAttribute {
  name: string;
  type: string;
  required: boolean;
  values?: string[];
  fixed?: string;
  default?: string;
}

export interface DtdEntity {
  name: string;
  desc: string;
}

/** 'auto' (the document's own DOCTYPE) or a DTD family id defined by the active region. */
export type DtdFamily = string;

export interface DtdSchema {
  available: boolean;
  reason?: string;
  dtd_label?: string;
  active_family?: string;
  detected_family?: string;
  available_families?: string[];
  /** family id -> display label (from the region pack), e.g. { pax: 'PAX', pix: 'PIX' } */
  family_labels?: Record<string, string>;
  elements?: Record<string, DtdSchemaElement>;
  attribute_sets?: Record<string, DtdAttribute[]>;
  entities?: DtdEntity[];
}

interface Props {
  schema: DtdSchema | null;
  /** Enclosing element name at the cursor, or null if it couldn't be resolved
   * (e.g. cursor at document root, or inside a tag the parser didn't recognize). */
  contextTag: string | null;
  hasSelection: boolean;
  /** The trigger button, used to position the popover in fixed/viewport
   * coordinates via a portal — NOT CSS `position: absolute` inside the
   * toolbar. The PXML editor sits in a narrow right-hand panel; an
   * absolutely-positioned child there is clipped/squeezed by that panel's
   * own width, which is what made the palette render but its tags be
   * pushed out of the visible box. A portal to document.body sidesteps
   * every ancestor's overflow/width entirely. */
  anchorEl: HTMLElement | null;
  /** Which DTD family the palette shows ('auto' = whatever the document's DOCTYPE selects). */
  family: DtdFamily;
  onFamilyChange: (f: DtdFamily) => void;
  /** `attrs` is a ready-to-insert string of the tag's REQUIRED attributes, e.g. ` type=""`. */
  onSelect: (tagName: string, elType: string, attrs?: string) => void;
  /** Insert raw text (an entity) at the cursor. */
  onInsertText: (text: string) => void;
  /** Set an attribute on the open tag at the cursor: replaces its value if the
   * attribute is already there (never a duplicate), otherwise adds it. */
  onInsertAttribute: (name: string, value: string) => void;
  onClose: () => void;
}

type TabKey = 'tags' | 'attrs' | 'entities';

/**
 * Click-to-insert palette, driven entirely by the live DTD schema — never a
 * hard-coded list. Three tabs: Tags (scoped to the cursor's enclosing element
 * when it can be resolved, Arbortext-style), Attributes (per element, with
 * required flags and enumerated values) and Entities (character entities the
 * DTD declares). PAX and PIX are separate DTD families with different tag sets
 * (e.g. <cite> vs <cc>/<st>/<pl>), so the family is switchable.
 */
const PALETTE_WIDTH = 380;
const PALETTE_MAX_HEIGHT = 480;
const VIEWPORT_MARGIN = 8;
const ENTITY_RENDER_CAP = 300;

const TagPalette: React.FC<Props> = ({
  schema, contextTag, hasSelection, anchorEl, family, onFamilyChange, onSelect, onInsertText, onInsertAttribute, onClose,
}) => {
  const [tab, setTab] = useState<TabKey>('tags');
  const [filter, setFilter] = useState('');
  const [attrTag, setAttrTag] = useState<string | null>(contextTag);
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);

  // Position from the trigger button's REAL screen location, clamped to the
  // viewport — never the toolbar's own (narrow) box. Recomputed on open and
  // on scroll/resize so it tracks the button if the panel scrolls.
  useLayoutEffect(() => {
    if (!anchorEl) return;
    const place = () => {
      const rect = anchorEl.getBoundingClientRect();
      let left = rect.right - PALETTE_WIDTH;
      left = Math.max(VIEWPORT_MARGIN, Math.min(left, window.innerWidth - PALETTE_WIDTH - VIEWPORT_MARGIN));
      let top = rect.bottom + 4;
      if (top + PALETTE_MAX_HEIGHT > window.innerHeight - VIEWPORT_MARGIN) {
        // Not enough room below — open upward instead of clipping off-screen.
        top = Math.max(VIEWPORT_MARGIN, rect.top - PALETTE_MAX_HEIGHT - 4);
      }
      setPos({ top, left });
    };
    place();
    window.addEventListener('scroll', place, true);
    window.addEventListener('resize', place);
    return () => {
      window.removeEventListener('scroll', place, true);
      window.removeEventListener('resize', place);
    };
  }, [anchorEl]);

  useEffect(() => {
    inputRef.current?.focus();
  }, [tab]);

  useEffect(() => {
    const onDocClick = (e: MouseEvent) => {
      const target = e.target as Node;
      if (rootRef.current?.contains(target)) return;
      if (anchorEl?.contains(target)) return; // let the toggle button handle its own click
      onClose();
    };
    const onEsc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('mousedown', onDocClick);
    document.addEventListener('keydown', onEsc);
    return () => {
      document.removeEventListener('mousedown', onDocClick);
      document.removeEventListener('keydown', onEsc);
    };
  }, [onClose]);

  const elements = schema?.elements || {};
  const attrsOf = (name: string | null): DtdAttribute[] => {
    const id = name ? elements[name]?.attrs : undefined;
    return id === undefined ? [] : schema?.attribute_sets?.[String(id)] || [];
  };

  const { tagNames, scoped, contextType } = useMemo(() => {
    if (contextTag && elements[contextTag]) {
      const el = elements[contextTag];
      if (el.children.length > 0) {
        return { tagNames: el.children, scoped: true, contextType: el.type };
      }
      // A resolved context with NO valid children (e.g. an EMPTY or pure-text
      // element) means "insert nothing here" is actually the correct answer —
      // still show the full list rather than an empty, confusing palette.
    }
    return { tagNames: Object.keys(elements).sort(), scoped: false, contextType: null };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [schema, contextTag]);

  const q = filter.trim().toLowerCase();
  const filteredTags = useMemo(() => (q ? tagNames.filter((t) => t.toLowerCase().includes(q)) : tagNames), [tagNames, q]);
  const filteredEntities = useMemo(() => {
    const all = schema?.entities || [];
    return q ? all.filter((e) => e.name.toLowerCase().includes(q) || e.desc.toLowerCase().includes(q)) : all;
  }, [schema, q]);

  if (!schema || !schema.available || !pos) return null;

  const activeFamily = schema.active_family || '';
  const detected = schema.detected_family || activeFamily;
  const mismatch = activeFamily !== detected;
  const famLabel = (id: string) => schema.family_labels?.[id] || id.toUpperCase();

  const requiredAttrString = (name: string) =>
    attrsOf(name).filter((a) => a.required).map((a) => ` ${a.name}=""`).join('');

  const showAttrsFor = (name: string) => {
    setAttrTag(name);
    setFilter('');
    setTab('attrs');
  };

  const switchTab = (t: TabKey) => {
    setTab(t);
    setFilter('');
    if (t === 'attrs' && !attrTag) setAttrTag(contextTag && elements[contextTag] ? contextTag : null);
  };

  const attrTagAttrs = attrsOf(attrTag);
  const attrRows = q ? attrTagAttrs.filter((a) => a.name.toLowerCase().includes(q)) : attrTagAttrs;
  const placeholder =
    tab === 'tags' ? `Filter ${tagNames.length} tag${tagNames.length === 1 ? '' : 's'}...`
    : tab === 'attrs' ? (attrTag ? `Filter ${attrTagAttrs.length} attributes of <${attrTag}>...` : 'Pick a tag (ⓘ in the Tags tab)...')
    : `Filter ${(schema.entities || []).length} entities...`;

  return createPortal(
    <div
      className="tag-palette"
      ref={rootRef}
      style={{ position: 'fixed', top: pos.top, left: pos.left, width: PALETTE_WIDTH, maxHeight: PALETTE_MAX_HEIGHT }}
    >
      <div className="tag-palette-header">
        <div className="tag-palette-family" role="group" aria-label="DTD family">
          {['auto', ...(schema.available_families || [])].map((f) => (
              <button
                key={f}
                className={`tag-palette-family-btn ${family === f ? 'active' : ''}`}
                onClick={() => onFamilyChange(f)}
                title={f === 'auto' ? `Use the document's own DOCTYPE (${famLabel(detected)})` : `Show ${famLabel(f)} tags`}
              >
                {f === 'auto' ? `Auto · ${famLabel(detected)}` : famLabel(f)}
              </button>
            ))}
        </div>
        <button className="tag-palette-close" onClick={onClose} title="Close">✕</button>
      </div>

      <div className="tag-palette-tabs" role="tablist">
        <button role="tab" aria-selected={tab === 'tags'} className={`tag-palette-tab ${tab === 'tags' ? 'active' : ''}`} onClick={() => switchTab('tags')}>
          Tags <span className="tag-palette-count">{Object.keys(elements).length}</span>
        </button>
        <button role="tab" aria-selected={tab === 'attrs'} className={`tag-palette-tab ${tab === 'attrs' ? 'active' : ''}`} onClick={() => switchTab('attrs')}>
          Attributes
        </button>
        <button role="tab" aria-selected={tab === 'entities'} className={`tag-palette-tab ${tab === 'entities' ? 'active' : ''}`} onClick={() => switchTab('entities')}>
          Entities <span className="tag-palette-count">{(schema.entities || []).length}</span>
        </button>
      </div>

      {tab === 'tags' && (
        <div className="tag-palette-context-row">
          {scoped ? (
            <span className="tag-palette-context">
              Valid inside <code>&lt;{contextTag}&gt;</code>
              {contextType === 'mixed' && <span className="tag-palette-hint"> (text allowed too)</span>}
            </span>
          ) : (
            <span className="tag-palette-context tag-palette-unscoped">
              {contextTag
                ? (elements[contextTag]
                    ? `No child elements known for <${contextTag}> — showing all tags`
                    : `<${contextTag}> isn't declared in ${famLabel(activeFamily)} — showing all ${famLabel(activeFamily)} tags`)
                : 'Cursor position unresolved — showing all tags'}
            </span>
          )}
        </div>
      )}
      {tab === 'attrs' && (
        <div className="tag-palette-context-row">
          <span className="tag-palette-context">
            {attrTag ? <>Attributes of <code>&lt;{attrTag}&gt;</code> — click to set on the open tag at the cursor (replaces an existing value)</> : 'No tag selected — click ⓘ on a tag in the Tags tab'}
          </span>
        </div>
      )}
      {tab === 'entities' && (
        <div className="tag-palette-context-row">
          <span className="tag-palette-context">Character entities declared by {schema.dtd_label} — click to insert <code>&amp;name;</code></span>
        </div>
      )}

      <input
        ref={inputRef}
        className="tag-palette-search"
        type="text"
        placeholder={placeholder}
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
      />

      <div className="tag-palette-list">
        {tab === 'tags' && (
          <>
            {filteredTags.length === 0 && <div className="tag-palette-empty">No matching tags</div>}
            {filteredTags.map((name) => {
              const el = elements[name];
              const elType = el?.type || 'element';
              const attrs = attrsOf(name);
              const required = attrs.filter((a) => a.required);
              return (
                <div key={name} className="tag-palette-row">
                  <button
                    className="tag-palette-item"
                    onClick={() => onSelect(name, elType, requiredAttrString(name))}
                    title={elType === 'empty' ? `<${name}/> (self-closing)` : `<${name}>...</${name}>`}
                  >
                    <span className="tag-palette-tagname">{name}</span>
                    {required.length > 0 && (
                      <span className="tag-palette-badge tag-palette-badge-req" title={`Required: ${required.map((a) => a.name).join(', ')}`}>
                        {required.map((a) => a.name).join(', ')}*
                      </span>
                    )}
                    {elType === 'empty' && <span className="tag-palette-badge">empty</span>}
                  </button>
                  <button
                    className="tag-palette-info"
                    onClick={() => showAttrsFor(name)}
                    title={`${attrs.length} attribute${attrs.length === 1 ? '' : 's'} — view`}
                  >
                    ⓘ<span className="tag-palette-info-n">{attrs.length}</span>
                  </button>
                </div>
              );
            })}
          </>
        )}

        {tab === 'attrs' && (
          <>
            {attrTag && attrRows.length === 0 && <div className="tag-palette-empty">{attrTagAttrs.length === 0 ? 'This element has no attributes' : 'No matching attributes'}</div>}
            {attrRows.map((a) => (
              <div key={a.name} className="tag-palette-attr">
                <div className="tag-palette-attr-head">
                  <button
                    className="tag-palette-item tag-palette-attr-name"
                    onClick={() => onInsertAttribute(a.name, a.fixed ?? a.default ?? '')}
                    title={`Insert ${a.name}="" at the cursor`}
                  >
                    <span className="tag-palette-tagname">{a.name}</span>
                  </button>
                  <span className="tag-palette-attr-type">{a.type === 'enumeration' ? 'enum' : a.type}</span>
                  {a.required && <span className="tag-palette-badge tag-palette-badge-req">required</span>}
                </div>
                {a.values && a.values.length > 0 && (
                  <div className="tag-palette-chips">
                    {a.values.map((v) => (
                      <button
                        key={v}
                        className="tag-palette-chip"
                        onClick={() => onInsertAttribute(a.name, v)}
                        title={`Insert ${a.name}="${v}"`}
                      >
                        {v}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </>
        )}

        {tab === 'entities' && (
          <>
            {filteredEntities.length === 0 && <div className="tag-palette-empty">No matching entities</div>}
            {filteredEntities.slice(0, ENTITY_RENDER_CAP).map((e) => (
              <button
                key={e.name}
                className="tag-palette-item tag-palette-entity"
                onClick={() => onInsertText(`&${e.name};`)}
                title={`&${e.name};${e.desc ? ' — ' + e.desc : ''}`}
              >
                <span className="tag-palette-tagname">&amp;{e.name};</span>
                <span className="tag-palette-entity-desc">{e.desc}</span>
              </button>
            ))}
            {filteredEntities.length > ENTITY_RENDER_CAP && (
              <div className="tag-palette-empty">Showing first {ENTITY_RENDER_CAP} of {filteredEntities.length} — type to narrow</div>
            )}
          </>
        )}
      </div>

      <div className="tag-palette-footer">
        {mismatch && (
          <div className="tag-palette-warn">
            ⚠ Showing {famLabel(activeFamily)} — this document's DOCTYPE is {famLabel(detected)}, so validation will flag tags that exist only in {famLabel(activeFamily)}.
          </div>
        )}
        {tab === 'tags' ? (hasSelection ? 'Wraps your current selection · * = required attribute' : 'Inserts at the cursor · * = required attribute') : 'Inserts at the cursor'}
      </div>
    </div>,
    document.body
  );
};

export default TagPalette;
