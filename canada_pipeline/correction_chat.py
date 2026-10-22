"""Correction chat: a reviewer selects a block of the XML output that is tagged inaccurately (or
not tagged), says what it must become, and gets the retagged block back.

The LLM is only the proposer. Every proposal is checked against the DTD in force before the user
sees it (well-formed, known elements, allowed children, required / enumerated attributes, known
entities, text unchanged unless the instruction asked for a text change); if the check fails the
proposal goes back to the model once with the exact problems and a stronger model (propose ->
validate -> repair). Anything that still fails is returned WITH its issues listed, never silently.
"""
from __future__ import annotations

import re
import time
from functools import lru_cache

from .config import (
    build_anthropic_client, call_with_model_fallback, extract_text_from_message,
    get_escalation_model, get_model_chain,
)
from .dtd_schema import get_pxml_schema
from .prompt_loader import load_prompt
from .region import get_region
from .xml_entity_rules import XML_ENTITY_RULES, CASE_CITATION_RULES, URL_TAGGING_RULES, set_active_dtd_family

MAX_BLOCK_CHARS = 20000
MAX_CONTEXT_CHARS = 1500
MAX_HISTORY_TURNS = 6
_STD_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}
_TEXT_EDIT_WORDS = re.compile(r"\b(replace|change|reword|rewrite|add|insert|delete|remove|correct|fix|typo|spell|capitali[sz]e|text)\b", re.I)

_client = None
_client_at = 0.0


def _get_client():
    """Anthropic client, rebuilt every 10 minutes (the gateway token is short-lived)."""
    global _client, _client_at
    if _client is None or time.time() - _client_at > 600:
        _client = build_anthropic_client()
        _client_at = time.time()
    return _client


# ── DTD facts for the prompt ──────────────────────────────────────────────────

def _attr_sets(schema: dict) -> dict:
    return {int(k): v for k, v in (schema.get("attribute_sets") or {}).items()}


def _element_line(name: str, schema: dict, sets: dict) -> str:
    el = (schema.get("elements") or {}).get(name)
    if not el:
        return f"<{name}>: not declared in this DTD"
    kind = {"empty": "EMPTY (no content)", "mixed": "text and child elements", "element": "child elements only", "any": "anything"}.get(el["type"], el["type"])
    attrs = sets.get(el.get("attrs"), [])
    req = [a for a in attrs if a.get("required")]
    req_txt = "; ".join(
        f'{a["name"]}' + (f' (one of: {", ".join(a["values"])})' if a.get("values") else "") for a in req
    ) or "none"
    children = el["children"]
    ch_txt = ", ".join(children[:60]) + (" ..." if len(children) > 60 else "") if children else "none"
    return f"<{name}> — content: {kind}; allowed children: {ch_txt}; REQUIRED attributes: {req_txt}"


def _facts_for(block_xml: str, parent_tag: str, instruction: str, schema: dict) -> str:
    elements = schema.get("elements") or {}
    sets = _attr_sets(schema)
    names = []
    if parent_tag and parent_tag in elements:
        names.append(parent_tag)
    for n in re.findall(r"<([A-Za-z][\w.\-]*)", block_xml):
        if n in elements and n not in names:
            names.append(n)
    for word in re.findall(r"[A-Za-z][\w.\-]*", instruction or ""):
        if word in elements and word not in names:
            names.append(word)
    return "\n".join(_element_line(n, schema, sets) for n in names[:25]) or "(no elements resolved)"


# ── validation of a proposal ──────────────────────────────────────────────────

def _other_family_info(schema: dict):
    """(tags, {tag: attr names}) declared by the OTHER selectable DTD families but not by `schema`."""
    own = set((schema.get("elements") or {}))
    own_family = schema.get("active_family")
    tags: set = set()
    attrs: dict = {}
    for fam in get_region().selectable_families:
        if fam.id == own_family:
            continue
        other = get_pxml_schema("", fam.id)
        if not other.get("available"):
            continue
        sets = {int(k): v for k, v in (other.get("attribute_sets") or {}).items()}
        for name, el in (other.get("elements") or {}).items():
            attrs.setdefault(name, set()).update(a["name"] for a in sets.get(el.get("attrs"), []))
            if name not in own:
                tags.add(name)
    return tags, attrs


def validate_fragment(xml: str, schema: dict, parent_tag: str, original_xml: str, instruction: str) -> dict:
    """{issues: [{severity, message}], text_changed: bool}. Lightweight, fragment-level."""
    from lxml import etree

    issues: list = []
    elements = schema.get("elements") or {}
    sets = _attr_sets(schema)
    entities = {e["name"] for e in schema.get("entities") or []}

    def _mask_entities(s: str) -> str:
        return re.sub(r"&([A-Za-z][\w.]*);", lambda m: m.group(0) if m.group(1) in _STD_ENTITIES else f"⁣{m.group(1)}⁣", s)

    try:
        root = etree.fromstring(("<root>" + _mask_entities(xml) + "</root>").encode("utf-8"))
    except etree.XMLSyntaxError as exc:
        return {"issues": [{"severity": "error", "message": f"Not well-formed XML: {exc}"}], "text_changed": False}

    # Region-allowed tags (validation.allowed_tags) are valid wherever they appear: unwrap them so
    # they trigger neither "not declared" nor a bogus "not allowed inside" on their parents.
    allowed = set(get_region().allowed_tags)
    # Existing files get new revisions tagged in PAX or PIX style as needed, so a tag / attribute the
    # OTHER selectable DTD family declares is accepted too; only what neither family allows is flagged.
    foreign_tags, other_attrs = _other_family_info(schema)
    allowed |= foreign_tags
    if allowed:
        etree.strip_tags(root, *sorted(allowed))

    for ent in sorted(set(re.findall(r"&([A-Za-z][\w.]*);", xml)) - _STD_ENTITIES):
        if entities and ent not in entities:
            issues.append({"severity": "error", "message": f"Unknown entity &{ent}; (not declared in this DTD)"})

    def _check(el, parent_name):
        name = el.tag
        if not isinstance(name, str):
            return
        decl = elements.get(name)
        if not decl:
            issues.append({"severity": "error", "message": f"<{name}> is not declared in this DTD"})
        else:
            if parent_name and parent_name in elements:
                pdecl = elements[parent_name]
                if pdecl["type"] == "empty":
                    issues.append({"severity": "error", "message": f"<{parent_name}> is EMPTY and cannot contain <{name}>"})
                elif pdecl["type"] != "any" and pdecl["children"] and name not in pdecl["children"]:
                    issues.append({"severity": "error", "message": f"<{name}> is not allowed inside <{parent_name}>"})
            attrs = {a["name"]: a for a in sets.get(decl.get("attrs"), [])}
            for a in attrs.values():
                if a.get("required") and a["name"] not in el.attrib:
                    issues.append({"severity": "error", "message": f'<{name}> is missing required attribute "{a["name"]}"'})
            for k, v in el.attrib.items():
                spec = attrs.get(k)
                if spec is None and k in other_attrs.get(name, ()):
                    continue                      # declared by the other DTD family: accepted
                if spec is None:
                    issues.append({"severity": "error", "message": f'<{name}> has attribute "{k}", which the DTD does not declare for it'})
                elif spec.get("values") and v not in spec["values"]:
                    issues.append({"severity": "error", "message": f'<{name} {k}="{v}">: value must be one of {", ".join(spec["values"])}'})
        for child in el:
            _check(child, name)

    for top in root:
        _check(top, parent_tag)

    def _text(s: str) -> str:
        try:
            r = etree.fromstring(("<root>" + _mask_entities(s) + "</root>").encode("utf-8"))
            return re.sub(r"\s+", " ", "".join(r.itertext())).strip()
        except etree.XMLSyntaxError:
            return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s)).strip()

    text_changed = _text(original_xml) != _text(xml)
    if text_changed and not _TEXT_EDIT_WORDS.search(instruction or ""):
        issues.append({"severity": "warning", "message": "The visible text differs from the original — the instruction did not ask for a text change."})
    return {"issues": issues, "text_changed": text_changed}


# ── LLM plumbing ──────────────────────────────────────────────────────────────

_RESULT_RE = re.compile(r"<result>\s*(.*?)\s*</result>", re.S)
_EXPL_RE = re.compile(r"<explanation>\s*(.*?)\s*</explanation>", re.S)


def _parse_reply(text: str):
    m = _RESULT_RE.search(text or "")
    if not m:
        return None
    xml = m.group(1).strip()
    xml = re.sub(r"^```[a-z]*\n|\n```$", "", xml).strip()   # tolerate a stray fence
    e = _EXPL_RE.search(text)
    return {"xml": xml, "explanation": (e.group(1).strip() if e else "")}


def _conversation(history: list) -> str:
    if not history:
        return ""
    lines = ["CONVERSATION SO FAR (the reviewer is refining an earlier proposal):"]
    for turn in history[-MAX_HISTORY_TURNS:]:
        role = "Reviewer" if turn.get("role") == "user" else "You (previous proposal)"
        lines.append(f"{role}: {str(turn.get('content', ''))[:3000]}")
    return "\n".join(lines) + "\n\n"


def _image_content(images: list | None = None):
    blocks = []
    for image in images or []:
        if not isinstance(image, dict):
            continue
        data = image.get("data") or image.get("base64") or ""
        media_type = image.get("media_type") or image.get("mime_type") or "image/png"
        if not data:
            continue
        blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        })
    return blocks


def _ask(prompt: str, chain: list, parse_check=True, images: list | None = None) -> dict:
    client = _get_client()
    content = prompt
    image_blocks = _image_content(images)
    if image_blocks:
        content = [{"type": "text", "text": prompt}] + image_blocks
    kwargs = dict(max_tokens=8000, messages=[{"role": "user", "content": content}])
    msg = call_with_model_fallback(
        client, chain, kwargs,
        quality_check=(lambda m: _parse_reply(extract_text_from_message(m)) is not None) if parse_check else None,
    )
    parsed = _parse_reply(extract_text_from_message(msg))
    parsed["model"] = getattr(msg, "model", chain[0])
    return parsed


# ── public entry point ────────────────────────────────────────────────────────

def propose_correction(*, block_xml: str, instruction: str, context_before: str = "", context_after: str = "",
                       parent_tag: str = "", pxml_head: str = "", dtd_family: str = "auto", history: list | None = None,
                       images: list | None = None) -> dict:
    block_xml = (block_xml or "")[:MAX_BLOCK_CHARS]
    instruction = (instruction or "").strip()
    if not block_xml.strip():
        raise ValueError("Select a block of XML first.")
    if not instruction:
        raise ValueError("Tell me what the block should be changed to.")

    schema = get_pxml_schema(pxml_head or "", dtd_family)
    if not schema.get("available"):
        raise RuntimeError(f"DTD not available: {schema.get('reason')}")
    family = schema.get("active_family") or get_region().default_family
    if dtd_family == "auto":
        set_active_dtd_family(pxml_head or "")
    else:
        # The rule snippets follow the family the reviewer is working in.
        set_active_dtd_family(f'<!DOCTYPE x SYSTEM "{family}.dtd">')

    region = get_region()
    prompt = load_prompt(
        "correction_chat.txt",
        region_name=region.name,
        dtd_label=schema.get("dtd_label", family),
        parent_tag=parent_tag or "(unknown)",
        dtd_facts=_facts_for(block_xml, parent_tag, instruction, schema),
        XML_ENTITY_RULES=XML_ENTITY_RULES,
        CASE_CITATION_RULES=CASE_CITATION_RULES,
        URL_TAGGING_RULES=URL_TAGGING_RULES,
        context_before=(context_before or "")[-MAX_CONTEXT_CHARS:],
        context_after=(context_after or "")[:MAX_CONTEXT_CHARS],
        conversation=_conversation(history or []),
        block_xml=block_xml,
        instruction=instruction,
    )

    result = _ask(prompt, get_model_chain("chat"), images=images)
    check = validate_fragment(result["xml"], schema, parent_tag, block_xml, instruction)
    attempts = 1

    # propose -> validate -> repair: one retry with the exact problems and the stronger model.
    errors = [i for i in check["issues"] if i["severity"] == "error"]
    if errors:
        repair_prompt = (
            prompt
            + "\n\nYOUR PREVIOUS ANSWER WAS REJECTED by the DTD check:\n"
            + "\n".join(f"- {i['message']}" for i in errors)
            + "\n\nPrevious answer:\n<result>\n" + result["xml"][:6000] + "\n</result>\n\n"
            + "Fix every problem above and answer again in the same format."
        )
        try:
            fixed = _ask(repair_prompt, [get_escalation_model("chat")] + [m for m in get_model_chain("chat") if m != get_escalation_model("chat")], images=images)
            fixed_check = validate_fragment(fixed["xml"], schema, parent_tag, block_xml, instruction)
            if len([i for i in fixed_check["issues"] if i["severity"] == "error"]) <= len(errors):
                result, check = fixed, fixed_check
            attempts = 2
        except Exception as exc:   # keep the first proposal; the user still sees its issues
            print(f"[CORRECTION] repair attempt failed: {exc}")

    return {
        "xml": result["xml"],
        "explanation": result["explanation"],
        "issues": check["issues"],
        "valid": not any(i["severity"] == "error" for i in check["issues"]),
        "text_changed": check["text_changed"],
        "model": result.get("model", ""),
        "attempts": attempts,
        "dtd_label": schema.get("dtd_label", ""),
    }
