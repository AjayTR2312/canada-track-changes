"""Revision resolver agent: takes ONE revision the automatic pipeline failed to place (a row of
Failed_revisions.xlsx) and searches the PXML for where it belongs, then PROPOSES the exact edit.

PROPOSE ONLY. The agent works on an in-memory copy of the PXML text through read-only tools and
returns a proposal (find / replace). Nothing is written anywhere: the reviewer sees the activity
log and the proposed diff in the Failed Revisions panel and presses Accept or Reject; Accept
applies the edit in the editor, where the normal Save / Validate flow takes over.

Guardrails (code, not prompt): at most MAX_STEPS model turns; every proposal is machine-checked
(well-formed, DTD, existing text preserved, the change is actually present) and a failing proposal
goes back to the agent with the exact problems; the reviewer is the only one who can apply it.

`run_resolver(...)` is a generator of events so the UI can show the agent working live:
  {"type": "step",  "n", "kind": "think"|"tool"|"result", "text", ...}
  {"type": "final", "status": "proposed"|"gave_up"|"exhausted", "proposal"?, "reason"?, "model", "steps"}
"""
from __future__ import annotations

import difflib
import html
import json
import re
import time
from typing import Iterator

from .config import build_anthropic_client, call_with_model_fallback, get_model_chain
from .correction_chat import validate_fragment
from .dtd_schema import get_pxml_schema
from .prompt_loader import load_prompt
from .region import get_region
from .xml_entity_rules import XML_ENTITY_RULES, set_active_dtd_family

import os

MAX_STEPS = int(os.getenv("RESOLVER_MAX_STEPS") or 10)
MAX_PXML_CHARS = int(os.getenv("RESOLVER_MAX_PXML_CHARS") or 6_000_000)
MAX_TOOL_CHARS = 6000
# Elements the pipeline edits at paragraph scale. A region can override: resolver.block_tags in region.yaml.
_DEFAULT_BLOCK_TAGS = ["para.text", "form.line", "form.text", "entry", "name", "caption.text"]


def _block_tags() -> list:
    cfg = (get_region().raw.get("resolver") or {}).get("block_tags")
    return list(cfg) if cfg else list(_DEFAULT_BLOCK_TAGS)


# ── text helpers ──────────────────────────────────────────────────────────────

def _normalize_trace(xml: str) -> str:
    """The pipeline writes a deletion as <trace.deleted>x<trace.deleted/>; make that well-formed
    (and turn the [[SP]] spacing token into a space) so it can be parsed and checked."""
    xml = re.sub(r"<trace\.deleted>(.*?)<trace\.deleted\s*/>", r"<trace.deleted>\1</trace.deleted>", xml, flags=re.S)
    return xml.replace("[[SP]]", " ")


_DELETED_SPAN = re.compile(r"<trace\.deleted>((?:(?!<trace\.deleted)[\s\S])*?)<trace\.deleted\s*/>")


def collapse_deletions(xml: str) -> str:
    """The pipeline writes a deletion as <trace.deleted>text<trace.deleted/> while working, and its postprocessing
    (Step 1) reduces that to the empty marker <trace.deleted/> in the finished XML. The resolver edits the finished
    XML, so a proposal gets the same reduction once it has passed its checks."""
    prev = None
    while prev != xml:
        prev, xml = xml, _DELETED_SPAN.sub("<trace.deleted/>", xml)
    return xml


def _plain(xml: str, drop_trace_inserted: bool = False, drop_trace_deleted: bool = False) -> str:
    s = _normalize_trace(xml)
    if drop_trace_inserted:
        s = re.sub(r"<trace>.*?</trace>", "", s, flags=re.S)
    if drop_trace_deleted:
        s = re.sub(r"<trace\.deleted>.*?</trace\.deleted>", "", s, flags=re.S)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"').replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip()


def _norm_for_match(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", _plain(s).lower())


class _Doc:
    """Paragraph-level blocks of the PXML, addressable by id (B1, B2 ...) in document order."""

    def __init__(self, pxml: str):
        self.pxml = pxml
        tags = "|".join(re.escape(t) for t in _block_tags())
        pat = re.compile(rf"<({tags})\b[^>]*?(?<!/)>.*?</\1>", re.S)
        self.blocks = []
        for m in pat.finditer(pxml):
            raw = m.group(0)
            text = _plain(raw)
            if text:
                self.blocks.append({"id": f"B{len(self.blocks) + 1}", "tag": m.group(1), "raw": raw, "text": text,
                                    "norm": _norm_for_match(raw), "start": m.start(), "end": m.end()})
        self.by_id = {b["id"]: b for b in self.blocks}

    def search(self, query: str, limit: int = 8) -> list:
        q = _norm_for_match(query)
        if len(q) < 4:
            return []
        scored = []
        qwords = set(q.split())
        for b in self.blocks:
            if q in b["norm"]:
                score = 1.0
            else:
                bw = set(b["norm"].split())
                overlap = len(qwords & bw) / max(1, len(qwords))
                if overlap < 0.5:
                    continue
                score = overlap * 0.6 + difflib.SequenceMatcher(None, q[:300], b["norm"][:600]).quick_ratio() * 0.4
            scored.append((score, b))
        scored.sort(key=lambda x: -x[0])
        return [{"block_id": b["id"], "score": round(s, 2), "tag": b["tag"], "text": b["text"][:260]} for s, b in scored[:limit]]

    def neighbours(self, block_id: str, n: int = 2) -> dict:
        i = next((k for k, b in enumerate(self.blocks) if b["id"] == block_id), None)
        if i is None:
            return {}
        return {"before": [{"block_id": b["id"], "text": b["text"][:220]} for b in self.blocks[max(0, i - n):i]],
                "after": [{"block_id": b["id"], "text": b["text"][:220]} for b in self.blocks[i + 1:i + 1 + n]]}


# ── proposal check ────────────────────────────────────────────────────────────

def check_proposal(doc: _Doc, row: dict, kind: str, block_id: str, replace_xml: str, schema: dict) -> dict:
    """{ok, problems[], warnings[]}: the deterministic gate every proposal must pass."""
    problems, warnings = [], []
    block = doc.by_id.get(block_id)
    if not block:
        return {"ok": False, "problems": [f"Unknown block_id {block_id}. Use an id returned by search_blocks."], "warnings": []}
    if kind not in ("replace", "insert_after"):
        return {"ok": False, "problems": ["kind must be 'replace' or 'insert_after'."], "warnings": []}
    new_xml = replace_xml.strip()
    if not new_xml:
        return {"ok": False, "problems": ["replace_xml is empty."], "warnings": []}

    frag = _normalize_trace(new_xml)
    original = block["raw"]
    check = validate_fragment(frag, schema, "", _normalize_trace(original) if kind == "replace" else "", "add insert delete text")
    for i in check["issues"]:
        (problems if i["severity"] == "error" else warnings).append(i["message"])

    inserted = str(row.get("Inserted Text") or "").strip()
    deleted = str(row.get("Deleted Text") or "").strip()
    ctype = str(row.get("Change Type") or "").lower()

    if kind == "replace":
        # Existing text must survive: with the insertions removed, the old text must be unchanged.
        if _plain(new_xml, drop_trace_inserted=True) != _plain(original, drop_trace_inserted=True):
            problems.append("Text outside your trace tags differs from the original block (with <trace> insertions removed). "
                            "Only add trace tags; do not change other words.")
        if re.search(r'="[^"]*<trace', new_xml):
            problems.append("A trace tag sits inside an attribute value.")
        if new_xml == original.strip():
            problems.append("replace_xml is identical to the original block: no change was made.")
    if inserted and not any(k in ctype for k in ("delet",)):
        want = _norm_for_match(inserted)[:80]
        got = _norm_for_match(" ".join(re.findall(r"<trace>(.*?)</trace>", frag, flags=re.S)))
        if want and want not in got:
            problems.append("The inserted text does not appear inside <trace>...</trace> in your edit.")
    if deleted and kind == "replace" and "insert" not in ctype:
        want = _norm_for_match(deleted)[:80]
        got = _norm_for_match(" ".join(re.findall(r"<trace\.deleted>(.*?)</trace\.deleted>", frag, flags=re.S)))
        if want and want not in got:
            problems.append("The deleted text is not wrapped in <trace.deleted>...<trace.deleted/> in your edit.")
    return {"ok": not problems, "problems": problems, "warnings": warnings}


# ── tools ─────────────────────────────────────────────────────────────────────

_TOOLS = [
    {"name": "search_blocks", "description": "Find paragraph-level XML blocks whose text matches the query (words from the Word paragraph or its contexts). Returns block ids with a text preview. Try different phrasings if nothing matches.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "get_block", "description": "Exact XML of one block plus the text of the blocks before and after it.",
     "input_schema": {"type": "object", "properties": {"block_id": {"type": "string"}}, "required": ["block_id"]}},
    {"name": "propose_edit", "description": "Submit the edit for a block. It is checked at once; if problems come back, fix them and call again.",
     "input_schema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": ["replace", "insert_after"]},
         "block_id": {"type": "string"},
         "replace_xml": {"type": "string", "description": "replace: the whole block with trace tags added. insert_after: only the new element(s)."},
         "explanation": {"type": "string", "description": "One or two sentences: why this block, and what the edit does."},
         "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
     }, "required": ["kind", "block_id", "replace_xml", "explanation", "confidence"]}},
    {"name": "give_up", "description": "Stop when no safe placement exists. State what you tried and why it failed.",
     "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
]


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)[:MAX_TOOL_CHARS]


def _short(s: str, n: int = 140) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[:n - 1] + "…"


# ── the agent ─────────────────────────────────────────────────────────────────

def run_resolver(*, row: dict, pxml: str, dtd_family: str = "auto") -> Iterator[dict]:
    if not (pxml or "").strip():
        raise ValueError("The XML editor is empty: load the PXML first.")
    if len(pxml) > MAX_PXML_CHARS:
        raise ValueError("The PXML is too large for the resolver.")
    head = pxml[:4000]
    schema = get_pxml_schema(head, dtd_family)
    if not schema.get("available"):
        raise RuntimeError(f"DTD not available: {schema.get('reason')}")
    family = schema.get("active_family") or get_region().default_family
    set_active_dtd_family(head if dtd_family == "auto" else f'<!DOCTYPE x SYSTEM "{family}.dtd">')

    doc = _Doc(pxml)
    region = get_region()
    prompt = load_prompt(
        "revision_resolver.txt",
        region_name=region.name, dtd_label=schema.get("dtd_label", family),
        change_type=row.get("Change Type") or "unknown", section=_short(str(row.get("Section") or ""), 300) or "(none)",
        designator=row.get("Designator") or "(none)", reason=_short(str(row.get("Reason") or ""), 600) or "(not recorded)",
        deleted_text=_short(str(row.get("Deleted Text") or ""), 800) or "(none)",
        inserted_text=_short(str(row.get("Inserted Text") or ""), 1500) or "(none)",
        docx_paragraph=_short(str(row.get("Docx_Paragraph") or ""), 1500) or "(none)",
        previous_context=_short(str(row.get("Previous Context") or ""), 500) or "(none)",
        post_context=_short(str(row.get("Post Context") or ""), 500) or "(none)",
        XML_ENTITY_RULES=XML_ENTITY_RULES,
    )
    yield {"type": "step", "n": 0, "kind": "think", "text": f"Indexed {len(doc.blocks):,} paragraph blocks in the XML ({schema.get('dtd_label', family)})."}

    client = build_anthropic_client()
    chain = get_model_chain("chat")
    messages = [{"role": "user", "content": "Resolve this failed revision. Start by searching for the paragraph."}]
    model_used = chain[0]
    failed_checks = 0

    for step in range(1, MAX_STEPS + 1):
        msg = call_with_model_fallback(client, chain, dict(max_tokens=6000, system=prompt, tools=_TOOLS, messages=messages))
        model_used = getattr(msg, "model", model_used)
        messages.append({"role": "assistant", "content": msg.content})
        for b in msg.content:
            if getattr(b, "type", "") == "text" and b.text.strip():
                yield {"type": "step", "n": step, "kind": "think", "text": _short(b.text, 300)}
        calls = [b for b in msg.content if getattr(b, "type", "") == "tool_use"]
        if not calls:
            yield {"type": "final", "status": "exhausted", "reason": "The agent stopped without proposing an edit.", "model": model_used, "steps": step}
            return

        results = []
        for call in calls:
            a = dict(call.input)
            if call.name == "search_blocks":
                hits = doc.search(a.get("query", ""))
                yield {"type": "step", "n": step, "kind": "tool", "tool": "search_blocks", "text": f"Searching the XML for “{_short(a.get('query', ''), 80)}”"}
                yield {"type": "step", "n": step, "kind": "result", "text": f"{len(hits)} candidate block(s)" + (f", best: {hits[0]['block_id']} (score {hits[0]['score']})" if hits else ""),
                       "data": [{"block_id": h["block_id"], "score": h["score"], "text": _short(h["text"], 110)} for h in hits[:4]]}
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": _json(hits) if hits else "No matching blocks. Try other words from the paragraph or contexts."})
            elif call.name == "get_block":
                b = doc.by_id.get(a.get("block_id", ""))
                yield {"type": "step", "n": step, "kind": "tool", "tool": "get_block", "text": f"Reading block {a.get('block_id', '?')}"}
                if not b:
                    yield {"type": "step", "n": step, "kind": "result", "text": "Unknown block id."}
                    results.append({"type": "tool_result", "tool_use_id": call.id, "content": "Unknown block id."})
                else:
                    yield {"type": "step", "n": step, "kind": "result", "text": _short(b["text"], 160), "block_id": b["id"]}
                    results.append({"type": "tool_result", "tool_use_id": call.id, "content": _json({"block_id": b["id"], "tag": b["tag"], "xml": b["raw"], **doc.neighbours(b["id"])})})
            elif call.name == "propose_edit":
                kind, bid, new_xml = a.get("kind", ""), a.get("block_id", ""), a.get("replace_xml", "")
                yield {"type": "step", "n": step, "kind": "tool", "tool": "propose_edit", "text": f"Checking a proposed {kind} on {bid}"}
                chk = check_proposal(doc, row, kind, bid, new_xml, schema)
                if chk["ok"]:
                    block = doc.by_id[bid]
                    find = block["raw"]
                    replace = (find + new_xml.strip()) if kind == "insert_after" else new_xml.strip()
                    if pxml.count(find) != 1:
                        chk = {"ok": False, "problems": ["That block's XML is not unique in the document, so it cannot be targeted safely. Pick a longer anchor or a neighbouring block."], "warnings": []}
                if chk["ok"]:
                    yield {"type": "step", "n": step, "kind": "result", "text": "All checks passed (well-formed, DTD, text preserved, change present)."
                           + (f" Warnings: {'; '.join(chk['warnings'])}" if chk["warnings"] else "")}
                    yield {"type": "final", "status": "proposed", "model": model_used, "steps": step,
                           "proposal": {"kind": kind, "block_id": bid, "find": find, "replace": collapse_deletions(_finalize_spacing(replace)),
                                        "explanation": a.get("explanation", ""), "confidence": a.get("confidence", "medium"),
                                        "warnings": chk["warnings"], "block_text": block["text"]}}
                    return
                failed_checks += 1
                yield {"type": "step", "n": step, "kind": "result", "text": "Rejected by the checks: " + "; ".join(chk["problems"]), "ok": False}
                results.append({"type": "tool_result", "tool_use_id": call.id, "is_error": True,
                                "content": "Your proposal failed these checks:\n- " + "\n- ".join(chk["problems"]) + "\nFix them and call propose_edit again, or give_up."})
            elif call.name == "give_up":
                yield {"type": "final", "status": "gave_up", "reason": a.get("reason", ""), "model": model_used, "steps": step}
                return
            else:
                results.append({"type": "tool_result", "tool_use_id": call.id, "is_error": True, "content": f"Unknown tool {call.name}."})
        messages.append({"role": "user", "content": results})

    yield {"type": "final", "status": "exhausted", "reason": f"No acceptable proposal within {MAX_STEPS} steps.", "model": model_used, "steps": MAX_STEPS}


def _finalize_spacing(xml: str) -> str:
    """[[SP]] is the model's spacing token; the pipeline turns it into a plain space."""
    return xml.replace("[[SP]]", " ")
