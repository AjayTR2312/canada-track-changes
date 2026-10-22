"""Triage agent: turns the false-positive log and the correction (chatbot) log into a ranked,
evidence-backed list of "what to fix first" for the region's developers.

SUGGEST ONLY. The agent reads data and the region's prompt / rule files through read-only tools
and writes a REPORT of proposed changes; it never edits a prompt, rule, DTD or code file. A
developer reviews each suggestion and applies it (or not) by hand.

How it works
  1. Deterministic grouping (no LLM): false positives are grouped by revision type + validator
     reason; corrections by what the reviewer actually changed (tags added / removed) and the
     parent element. Groups are ranked by severity and frequency.
  2. A bounded tool-using loop (<= MAX_STEPS model turns) investigates the top groups: it can
     list_clusters, get_examples for one group, read_region_file (prompts/ and rules/ only) and
     search_region_files, then must call submit_report with a structured diagnosis per group.
  3. The report is saved as JSON + Markdown (+ Excel) in S3 under feedback/<region>/triage/.
"""
from __future__ import annotations

import io
import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

from .config import build_anthropic_client, get_escalation_model
from .region import get_region

MAX_STEPS = 14
TOP_CLUSTERS = 12
MAX_TOOL_CHARS = 9000
_SEV_RANK = {"critical": 3, "medium": 2, "low": 1}
DIAGNOSES = ["prompt_gap", "rule_gap", "dtd_conflict", "validator_too_strict", "validator_bug", "data_quirk", "unclear"]


# ── 1. deterministic grouping ─────────────────────────────────────────────────

def _tag_signature(original: str, proposed: str) -> str:
    """What the correction changed, as tags: '+cite[type=case] -bold'."""
    def tags(s: str) -> Counter:
        c = Counter()
        for m in re.finditer(r"<([A-Za-z][\w.\-]*)([^>]*?)/?>", s):
            name, attrs = m.group(1), m.group(2)
            t = re.search(r'\btype="([^"]+)"', attrs)
            c[name + (f"[type={t.group(1)}]" if t else "")] += 1
        return c
    a, b = tags(original), tags(proposed)
    added = sorted(k for k in (b - a))
    removed = sorted(k for k in (a - b))
    return " ".join([f"+{k}" for k in added] + [f"-{k}" for k in removed]) or "(text/whitespace only)"


def build_clusters(fp_rows: list, corr_rows: list, since_days: int | None = None) -> list:
    """Ranked clusters from both logs. Each: {id, kind, key, count, ...stats, examples[]}."""
    cutoff = None
    if since_days:
        cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - since_days * 86400))
    keep = (lambda r: (r.get("timestamp", "") >= cutoff)) if cutoff else (lambda r: True)

    from src.false_positive_log import normalize_reason
    clusters = []

    groups = defaultdict(list)
    for r in filter(keep, fp_rows):
        groups[(r.get("revision_type", ""), normalize_reason(r.get("validator_reason", "")))].append(r)
    for (rtype, reason), items in groups.items():
        clusters.append({
            "kind": "false_positive",
            "key": f"{rtype} | {reason}",
            "count": len(items),
            "worst_severity": max((i.get("severity", "medium") for i in items), key=lambda s: _SEV_RANK.get(s, 0)),
            "documents": len({i.get("session_id") for i in items}),
            "reviewers": len({i.get("emp_id") for i in items}),
            "notes": [i["note"] for i in items if i.get("note")][:5],
            "examples": [{"revision_type": i.get("revision_type"), "text": (i.get("revision_text") or "")[:400],
                          "context": (i.get("previous_context") or "")[:200], "validator_reason": (i.get("validator_reason") or "")[:300],
                          "severity": i.get("severity"), "note": i.get("note", "")} for i in items[:6]],
        })

    cgroups = defaultdict(list)
    for r in filter(keep, corr_rows):
        sig = _tag_signature(r.get("original_xml", ""), r.get("proposed_xml", ""))
        cgroups[(r.get("parent_tag", ""), sig)].append(r)
    for (parent, sig), items in cgroups.items():
        applied = [i for i in items if i.get("applied") == "yes"]
        clusters.append({
            "kind": "correction",
            "key": f"in <{parent or '?'}>: {sig}",
            "count": len(items),
            "applied": len(applied),
            "worst_severity": "medium" if applied else "low",   # a correction a reviewer APPLIED = a confirmed miss
            "documents": len({i.get("session_id") for i in items}),
            "reviewers": len({i.get("emp_id") for i in items}),
            "notes": [],
            "examples": [{"instruction": (i.get("instruction") or "")[:300], "before": (i.get("original_xml") or "")[:700],
                          "after": (i.get("proposed_xml") or "")[:700], "applied": i.get("applied"), "valid": i.get("valid")} for i in (applied or items)[:5]],
        })

    clusters.sort(key=lambda c: (-_SEV_RANK.get(c["worst_severity"], 0), -c["count"], -c.get("applied", 0)))
    for n, c in enumerate(clusters[:TOP_CLUSTERS], 1):
        c["id"] = f"C{n}"
    return clusters[:TOP_CLUSTERS]


# ── 2. the tool-using agent ───────────────────────────────────────────────────

_TOOLS = [
    {"name": "list_clusters", "description": "List the ranked clusters (id, kind, key, counts, worst severity).", "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_examples", "description": "Full examples (text, context, before/after XML, notes) for one cluster.",
     "input_schema": {"type": "object", "properties": {"cluster_id": {"type": "string"}}, "required": ["cluster_id"]}},
    {"name": "list_region_files", "description": "List the region's prompt and rule files that a change could target.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "read_region_file", "description": "Read one prompt or rule file by its path relative to the region folder, e.g. prompts/paragraph_trackchanges_find_replace.txt or rules/citation_rules.pax.md.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "search_region_files", "description": "Case-insensitive text search across the region's prompts and rules; returns matching lines with file and line number.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "submit_report", "description": "Submit the final triage report. Call exactly once, when you have diagnosed the clusters worth acting on.",
     "input_schema": {"type": "object", "properties": {
         "summary": {"type": "string", "description": "3-5 sentences: the headline patterns and what to do first."},
         "findings": {"type": "array", "items": {"type": "object", "properties": {
             "cluster_id": {"type": "string"},
             "diagnosis": {"type": "string", "enum": DIAGNOSES},
             "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
             "explanation": {"type": "string", "description": "Why this keeps happening, citing the evidence."},
             "target_file": {"type": "string", "description": "Region-relative file to change (prompts/... or rules/...), or '' if none."},
             "suggested_change": {"type": "string", "description": "The concrete wording to add or modify, as a developer would paste it."},
             "risk": {"type": "string", "enum": ["low", "medium", "high"]},
             "how_to_verify": {"type": "string"},
         }, "required": ["cluster_id", "diagnosis", "confidence", "explanation", "suggested_change", "risk"]}},
     }, "required": ["summary", "findings"]}},
]

_SYSTEM = """You are a triage analyst for an XML tagging pipeline ({region}). Reviewers log FALSE POSITIVES (the
validator wrongly called a revision invalid) and CORRECTIONS (they asked an assistant to retag a block the pipeline
had tagged wrongly or not at all). Your job: find the few root causes behind the most frequent / most severe
clusters and propose precise fixes for a developer to review.

Rules:
- You only SUGGEST. You cannot change anything. Never claim a change was made.
- Ground every claim in the examples; quote short evidence. If the evidence is thin, say so and lower the confidence.
- Decide whether a cluster is a prompt gap, a rule gap, a DTD conflict, an over-strict validator, a validator bug, a
  data quirk, or unclear. For prompt/rule gaps, read the relevant file first and propose exact wording that fits it.
- Prefer one change that fixes several clusters over many tiny ones. Mention risk: a prompt change affects every job.
- Work efficiently: list clusters, inspect the top ones, read at most a few files, then call submit_report once."""


def _confine(path: str) -> Path | None:
    """Resolve a region-relative path, allowing only prompts/ and rules/ inside the region."""
    region = get_region()
    p = (region.root / path).resolve()
    allowed = [(region.root / "prompts").resolve(), (region.root / "rules").resolve()]
    if p.is_file() and any(a in p.parents for a in allowed):
        return p
    return None


def _run_tool(name: str, args: dict, clusters: list) -> str:
    region = get_region()
    if name == "list_clusters":
        return json.dumps([{k: c[k] for k in ("id", "kind", "key", "count", "applied", "worst_severity", "documents", "reviewers") if k in c} for c in clusters])
    if name == "get_examples":
        c = next((c for c in clusters if c["id"] == args.get("cluster_id")), None)
        return json.dumps(c, ensure_ascii=False)[:MAX_TOOL_CHARS] if c else "Unknown cluster id."
    if name == "list_region_files":
        files = sorted(str(f.relative_to(region.root)).replace("\\", "/") for sub in ("prompts", "rules") for f in (region.root / sub).glob("*") if f.is_file())
        return "\n".join(files)
    if name == "read_region_file":
        p = _confine(args.get("path", ""))
        if not p:
            return "Not found, or outside prompts/ and rules/."
        text = p.read_text(encoding="utf-8", errors="replace")
        return text[:MAX_TOOL_CHARS] + ("\n...[truncated]" if len(text) > MAX_TOOL_CHARS else "")
    if name == "search_region_files":
        q = (args.get("query") or "").lower().strip()
        if len(q) < 3:
            return "Query too short."
        hits = []
        for sub in ("prompts", "rules"):
            for f in sorted((region.root / sub).glob("*")):
                if not f.is_file():
                    continue
                for n, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if q in line.lower():
                        hits.append(f"{sub}/{f.name}:{n}: {line.strip()[:160]}")
        return "\n".join(hits[:40]) or "No matches."
    return f"Unknown tool {name}."


def run_triage(progress=None, since_days: int | None = None) -> dict:
    """Run the agent. `progress(msg)` is called with short status strings. Returns the report dict
    (also saved to disk): {summary, findings[], clusters[], generated_at, model, steps, stats}."""
    from src import correction_log, false_positive_log
    say = progress or (lambda m: None)
    fp_rows, corr_rows = false_positive_log.read_all(), correction_log.read_all()
    say(f"Read {len(fp_rows)} false positives and {len(corr_rows)} corrections")
    clusters = build_clusters(fp_rows, corr_rows, since_days)
    stats = {"false_positives": len(fp_rows), "corrections": len(corr_rows), "clusters": len(clusters)}
    if not clusters:
        return {"summary": "Not enough data yet: no false positives or corrections have been logged for this region.",
                "findings": [], "clusters": [], "stats": stats, "steps": 0, "model": "", "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}

    client = build_anthropic_client()
    model = get_escalation_model("chat")
    region = get_region()
    messages = [{"role": "user", "content": f"Region: {region.name}. {len(clusters)} clusters are ready ({stats['false_positives']} false-positive flags, {stats['corrections']} corrections). Investigate and submit your report."}]
    report = None
    steps = 0
    for steps in range(1, MAX_STEPS + 1):
        say(f"Agent step {steps}/{MAX_STEPS}")
        msg = client.messages.create(model=model, max_tokens=6000, system=_SYSTEM.format(region=region.name), tools=_TOOLS, messages=messages)
        messages.append({"role": "assistant", "content": msg.content})
        calls = [b for b in msg.content if getattr(b, "type", "") == "tool_use"]
        if not calls:
            break
        results = []
        for call in calls:
            if call.name == "submit_report":
                report = dict(call.input)
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": "Report received."})
            else:
                say(f"  tool: {call.name}")
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": _run_tool(call.name, dict(call.input), clusters)})
        messages.append({"role": "user", "content": results})
        if report is not None:
            break
    if report is None:
        report = {"summary": "The agent did not finish within its step budget; the grouped clusters below are still valid evidence.", "findings": []}
    report.update({"clusters": clusters, "stats": stats, "steps": steps, "model": model, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    _save(report)
    say("Report ready")
    return report


# ── 3. outputs ────────────────────────────────────────────────────────────────

def report_markdown(report: dict) -> str:
    region = get_region()
    by_id = {c["id"]: c for c in report.get("clusters", [])}
    lines = [f"# Triage report — {region.name}", "",
             f"Generated {report.get('generated_at', '')} · model {report.get('model', '')} · "
             f"{report['stats']['false_positives']} false positives, {report['stats']['corrections']} corrections, {report['stats']['clusters']} clusters", "",
             "> Suggestions only — nothing has been changed. Review each one before applying.", "",
             "## Summary", "", report.get("summary", ""), ""]
    for n, f in enumerate(report.get("findings", []), 1):
        c = by_id.get(f.get("cluster_id"), {})
        lines += [f"## {n}. {c.get('key', f.get('cluster_id'))}", "",
                  f"- **Cluster:** {f.get('cluster_id')} · {c.get('kind', '')} · {c.get('count', '?')} occurrence(s) · worst severity {c.get('worst_severity', '?')}"
                  f" · {c.get('documents', '?')} document(s) · {c.get('reviewers', '?')} reviewer(s)",
                  f"- **Diagnosis:** {f.get('diagnosis')} (confidence {f.get('confidence')}) · risk {f.get('risk')}",
                  f"- **Why:** {f.get('explanation', '')}",
                  f"- **Target file:** `{f.get('target_file') or 'none'}`", "",
                  "**Suggested change**", "", "```", f.get("suggested_change", ""), "```", ""]
        if f.get("how_to_verify"):
            lines += [f"**How to verify:** {f['how_to_verify']}", ""]
    return "\n".join(lines)


def report_xlsx(report: dict) -> bytes:
    from openpyxl import Workbook
    by_id = {c["id"]: c for c in report.get("clusters", [])}
    wb = Workbook()
    ws = wb.active
    ws.title = "Findings"
    ws.append(["Rank", "Cluster", "Kind", "Pattern", "Count", "Worst severity", "Diagnosis", "Confidence", "Risk", "Target file", "Suggested change", "Why", "How to verify"])
    for n, f in enumerate(report.get("findings", []), 1):
        c = by_id.get(f.get("cluster_id"), {})
        ws.append([n, f.get("cluster_id"), c.get("kind"), c.get("key"), c.get("count"), c.get("worst_severity"), f.get("diagnosis"),
                   f.get("confidence"), f.get("risk"), f.get("target_file"), f.get("suggested_change"), f.get("explanation"), f.get("how_to_verify")])
    cs = wb.create_sheet("Clusters")
    cs.append(["Cluster", "Kind", "Pattern", "Count", "Applied", "Worst severity", "Documents", "Reviewers"])
    for c in report.get("clusters", []):
        cs.append([c.get("id"), c.get("kind"), c.get("key"), c.get("count"), c.get("applied", ""), c.get("worst_severity"), c.get("documents"), c.get("reviewers")])
    from src.false_positive_log import _style_header
    _style_header(ws)
    _style_header(cs)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _triage_prefix() -> str:
    import s3_utils
    return f"{s3_utils.S3_PREFIX_FEEDBACK}/{s3_utils.safe_id(get_region().id)}/triage/"


def _save(report: dict) -> str:
    """Store the report (JSON + Markdown + Excel) in S3 under feedback/<region>/triage/.
    Returns the key of the Markdown copy."""
    import s3_utils
    prefix = _triage_prefix()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    s3_utils.put_bytes(f"{prefix}triage_{stamp}.json", json.dumps(report, indent=2, ensure_ascii=False).encode("utf-8"), "application/json")
    s3_utils.put_bytes(f"{prefix}triage_{stamp}.md", report_markdown(report).encode("utf-8"), "text/markdown")
    s3_utils.put_bytes(f"{prefix}triage_{stamp}.xlsx", report_xlsx(report),
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    return f"{prefix}triage_{stamp}.md"


def latest_report() -> dict | None:
    """The most recent saved triage report (as a dict), or None if there is none yet."""
    import s3_utils
    keys = sorted(o["key"] for o in s3_utils.list_objects(_triage_prefix()) if o["key"].endswith(".json"))
    return s3_utils.get_json(keys[-1]) if keys else None
