"""Expose the PAX/PIX DTDs' own content models, attributes and character
entities for editor tooling (tag-insertion palette): "what elements are valid
inside <para>?", "is <trace.deleted> a self-closing EMPTY element?", "what
attributes does <cite> take, and which are required?", "what entities exist?" —
all read directly from the DTD, never hard-coded.

Reuses the same DTD auto-detection PAXValidator already does for validation
(usforms_pipeline/validate_pax.py, usforms_pipeline/dtd_validation.py), so a
document's tag palette matches the DTD its own DOCTYPE selects by default. The
caller can also force the PAX or PIX family explicitly (a PIX tag palette for a
document that still declares pax.dtd, e.g. while authoring new structural tags).

DTDs are static files under validation_rule_files/, so the walk is cached per
resolved DTD path.
"""
import re
from functools import lru_cache
from pathlib import Path

def _selectable_families() -> dict:
    """{family id: (dtd file, label)} offered in the tag palette — from the active region."""
    from .region import get_region
    return {f.id: (f.file, f.label) for f in get_region().selectable_families}

# <!ENTITY name "...">   <!-- description -->   (general entities only: no `%`)
_ENTITY_RE = re.compile(
    r'<!ENTITY\s+(?!%)([A-Za-z][\w.\-]*)\s+(?:"[^"]*"|\'[^\']*\'|SYSTEM\s+"[^"]*")\s*>\s*(?:<!--\s*(.*?)\s*-->)?',
    re.S,
)


def _flatten_element_names(node, out: set) -> None:
    """Collect every element name reachable in a content-model node, ignoring
    sequencing/choice/occurrence — a tag palette needs the SET of what can
    appear, not the grammar's exact ordering rules (the DTD validator already
    enforces the strict grammar; this is for offering choices, not gatekeeping)."""
    if node is None:
        return
    if node.type == "element" and node.name:
        out.add(node.name)
    _flatten_element_names(node.left, out)
    _flatten_element_names(node.right, out)


def _attribute_record(a) -> dict:
    rec = {
        "name": a.name,
        "type": a.type,  # cdata | id | idref | enumeration | nmtoken | ...
        "required": a.default == "required",
    }
    if a.type == "enumeration":
        rec["values"] = list(a.values())
    if a.default == "fixed" and a.default_value is not None:
        rec["fixed"] = a.default_value
    elif a.default_value not in (None, ""):
        rec["default"] = a.default_value
    return rec


def _parse_entities(dtd_path: Path) -> list:
    text = dtd_path.read_text(encoding="utf-8", errors="replace")
    seen, out = set(), []
    for m in _ENTITY_RE.finditer(text):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "desc": re.sub(r"\s+", " ", m.group(2) or "").strip()})
    out.sort(key=lambda e: e["name"].lower())
    return out


def _build_schema(dtd_path: Path, dtd_label: str) -> dict:
    from lxml import etree

    dtd = etree.DTD(str(dtd_path))
    elements = {}
    # Most elements share the same big "system attributes" bundle, so attribute
    # lists are de-duplicated into numbered sets instead of repeated per element.
    attr_sets: dict = {}
    set_ids: dict = {}
    for el in dtd.iterelements():
        children: set = set()
        if el.type == "mixed" and el.content is not None:
            # MIXED content nests its element choices one level down, inside an
            # (#PCDATA | a | b | ...)* node — el.content itself is that node.
            _flatten_element_names(el.content, children)
        elif el.type == "element" and el.content is not None:
            _flatten_element_names(el.content, children)

        attrs = [_attribute_record(a) for a in el.iterattributes()]
        key = repr(attrs)
        if key not in set_ids:
            set_ids[key] = len(attr_sets)
            attr_sets[set_ids[key]] = attrs
        elements[el.name] = {
            "type": el.type,  # 'empty' | 'mixed' | 'element' | 'any'
            "children": sorted(children),
            "attrs": set_ids[key],
        }
    return {
        "dtd_label": dtd_label,
        "dtd_file": dtd_path.name,
        "elements": elements,
        "attribute_sets": attr_sets,
        "entities": _parse_entities(dtd_path),
    }


@lru_cache(maxsize=8)
def _build_schema_cached(dtd_path_str: str, dtd_label: str) -> dict:
    return _build_schema(Path(dtd_path_str), dtd_label)


def _family_id_for_file(dtd_path) -> str:
    """Family id (from the active region's manifest) for a DTD file path."""
    from .region import get_region
    name = Path(dtd_path).name if dtd_path else ""
    fam = next((f for f in get_region().dtd_families if f.file == name), None)
    return fam.id if fam else get_region().default_family


def get_pxml_schema(pxml_content: str, family: str = "auto") -> dict:
    """Return the element/attribute/entity map for `pxml_content`'s DOCTYPE-selected
    DTD (family="auto", same logic the validator uses) or for an explicitly chosen
    family (any selectable family id of the active region, e.g. "pax" / "pix").

    Returns {"available": False, "reason": "..."} if lxml or the DTD files
    aren't available, or the document's DOCTYPE doesn't match a known DTD —
    callers should fall back to a plain text editor with no tag palette rather
    than fail the whole editor.
    """
    try:
        from lxml import etree  # noqa: F401 — import-availability check
    except ImportError:
        return {"available": False, "reason": "lxml not installed"}

    from .validate_pax import PAXValidator
    from .dtd_validation import _RULE_DIR

    validator = PAXValidator(_RULE_DIR)
    detected_path, detected_label = validator._pick_dtd(pxml_content or "")
    detected_family = _family_id_for_file(detected_path)

    selectable = _selectable_families()
    family = (family or "auto").lower()
    if family in selectable:
        fname, label = selectable[family]
        dtd_path = Path(_RULE_DIR) / fname
        dtd_label = label
        if not dtd_path.exists():
            return {"available": False, "reason": f"{fname} not found"}
    else:
        dtd_path, dtd_label = detected_path, detected_label
    if not dtd_path:
        return {"available": False, "reason": "No matching DTD found for this document"}

    try:
        schema = _build_schema_cached(str(dtd_path), dtd_label)
    except Exception as e:
        return {"available": False, "reason": f"Could not parse DTD: {e}"}
    return {
        "available": True,
        "active_family": _family_id_for_file(dtd_path),
        "detected_family": detected_family,
        "available_families": [f for f, (fn, _) in selectable.items() if (Path(_RULE_DIR) / fn).exists()],
        "family_labels": {f: lbl for f, (fn, lbl) in selectable.items()},
        **schema,
    }
