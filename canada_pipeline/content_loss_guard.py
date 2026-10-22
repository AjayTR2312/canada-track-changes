"""
content_loss_guard.py
-----------------------------------------------------------------------------
Defensive, ADDITIVE safety checks that detect silent content loss around the
final post-processing step. Designed for legal documents where deleting real
text is never acceptable.

Design goals
------------
* PURELY ADDITIVE: this module changes no existing behaviour. It only READS the
  before/after XML strings, writes a human-readable report when it finds a
  problem, and prints warnings. It never mutates content.
* NEVER CRASHES THE PIPELINE: every public function is wrapped so that any
  internal error is swallowed and reported - a bug in the guard must not be able
  to stop a production run.
* ZERO third-party dependencies (stdlib `re`/`os` only).

What it detects
---------------
1. Section loss  - any <designator> section label present BEFORE post-processing
   that is missing AFTER. This is the highest-value check: it catches content
   loss regardless of the root cause.
2. Tag imbalance - unbalanced <trace.deleted> open/close markers and unbalanced
   structural tags (footnote, section, para, ...) in the pre-post-processing
   content. An imbalance here is what lets a single malformed tag "run away" and
   wipe downstream sections during the trace.deleted collapse.

On a detected problem it writes `CONTENT_LOSS_REPORT.txt` into the output
directory and prints a loud banner. It returns a small summary dict so callers
may optionally act on it (e.g. halt) - but by default it does not alter flow.
"""

import os
import re

# Tags whose open/close counts should stay balanced in well-formed content.
_STRUCTURAL_TAGS = (
    "footnote", "footnote.body", "footnote.reference",
    "section", "section.body", "section.block",
    "para", "para.text",
    "analytical.level", "analytical.level.body",
    "quote.block", "quote.para",
    "list", "list.item",
)

_DESIGNATOR_RE = re.compile(r"<designator[^>]*>([^<]+)</designator>")


def _extract_section_labels(xml):
    """Return the ordered list of <designator> label texts."""
    return [m.group(1).strip() for m in _DESIGNATOR_RE.finditer(xml or "")]


def _count_tag(xml, tag):
    """Return (open_count, close_count) for an exact tag name.

    Uses a lookahead so that e.g. 'para' does not also match 'para.text'.
    Self-closing occurrences are not counted as opens.
    """
    opens = len(re.findall(r"<" + re.escape(tag) + r"(?=[\s/>])(?:[^>]*?)(?<!/)>", xml))
    closes = len(re.findall(r"</" + re.escape(tag) + r"\s*>", xml))
    return opens, closes


def _trace_deleted_balance(xml):
    """Return (open_markers, close_markers) for the non-standard trace.deleted.

    <trace.deleted>  is treated as an OPEN marker.
    <trace.deleted/> is treated as a CLOSE marker.
    """
    closes = len(re.findall(r"<trace\.deleted\s*/>", xml))
    total = len(re.findall(r"<trace\.deleted\b", xml))
    opens = total - closes
    return opens, closes


def analyze(before_xml, after_xml):
    """Pure analysis (no I/O). Returns a summary dict describing any problems."""
    before_labels = _extract_section_labels(before_xml)
    after_labels = _extract_section_labels(after_xml)
    after_set = set(after_labels)

    # Preserve order, report each missing label once.
    seen = set()
    missing_sections = []
    for lbl in before_labels:
        if lbl not in after_set and lbl not in seen:
            missing_sections.append(lbl)
            seen.add(lbl)

    td_open, td_close = _trace_deleted_balance(before_xml)
    trace_deleted_imbalance = td_open - td_close

    structural_imbalances = {}
    for tag in _STRUCTURAL_TAGS:
        o, c = _count_tag(before_xml, tag)
        if o != c:
            structural_imbalances[tag] = {"open": o, "close": c, "diff": o - c}

    problems = bool(missing_sections) or trace_deleted_imbalance != 0 or bool(structural_imbalances)

    return {
        "ok": not problems,
        "section_count_before": len(before_labels),
        "section_count_after": len(after_labels),
        "missing_sections": missing_sections,
        "trace_deleted_open": td_open,
        "trace_deleted_close": td_close,
        "trace_deleted_imbalance": trace_deleted_imbalance,
        "structural_imbalances": structural_imbalances,
    }


def _format_report(summary, context=""):
    lines = []
    lines.append("=" * 72)
    lines.append("CONTENT LOSS GUARD REPORT")
    if context:
        lines.append(f"Context: {context}")
    lines.append("=" * 72)
    lines.append("")
    lines.append(f"Sections before post-processing : {summary['section_count_before']}")
    lines.append(f"Sections after  post-processing : {summary['section_count_after']}")
    lines.append("")

    if summary["missing_sections"]:
        lines.append(f"!! MISSING SECTIONS ({len(summary['missing_sections'])}) - present BEFORE, gone AFTER:")
        lines.append("   " + ", ".join(summary["missing_sections"]))
        lines.append("")
    else:
        lines.append("No sections were lost. [OK]")
        lines.append("")

    if summary["trace_deleted_imbalance"] != 0:
        lines.append(
            "!! TRACE.DELETED IMBALANCE in pre-post-processing content: "
            f"{summary['trace_deleted_open']} open <trace.deleted> vs "
            f"{summary['trace_deleted_close']} close <trace.deleted/> "
            f"(diff={summary['trace_deleted_imbalance']})."
        )
        lines.append(
            "   An unmatched <trace.deleted> can 'run away' and cause downstream "
            "sections to be wiped during collapse."
        )
        lines.append("")

    if summary["structural_imbalances"]:
        lines.append("!! STRUCTURAL TAG IMBALANCE in pre-post-processing content:")
        for tag, info in summary["structural_imbalances"].items():
            lines.append(
                f"   <{tag}>: {info['open']} open vs {info['close']} close (diff={info['diff']})"
            )
        lines.append("")

    lines.append("=" * 72)
    return "\n".join(lines)


def verify_no_content_loss(before_xml, after_xml, output_dir=None,
                           context="", log_fn=None):
    """Compare pre/post post-processing content and report any content loss.

    This is SAFE TO CALL ANYWHERE: it never raises and never mutates content.

    Parameters
    ----------
    before_xml : str   content saved as save_before_postprocessing.pxml
    after_xml  : str   content after postprocessing_fortags()
    output_dir : str   where to write CONTENT_LOSS_REPORT.txt (optional)
    context    : str   free-text label for the report (e.g. file/job name)
    log_fn     : callable  defaults to print; receives a single string

    Returns
    -------
    dict  the analysis summary (see analyze()); always includes key 'ok'.
          On internal error returns {'ok': None, 'error': <str>} and never raises.
    """
    say = log_fn or print
    try:
        summary = analyze(before_xml, after_xml)

        if summary["ok"]:
            say("  [CONTENT-GUARD] OK - No content loss detected "
                f"({summary['section_count_before']} sections preserved).")
            return summary

        report = _format_report(summary, context=context)

        # Loud console banner so it cannot be missed in logs.
        say("\n" + "!" * 72)
        say("!! [CONTENT-GUARD] POSSIBLE CONTENT LOSS DETECTED - SEE REPORT BELOW")
        say("!" * 72)
        say(report)

        # Persist a report + a sentinel file for easy discovery.
        if output_dir:
            try:
                report_path = os.path.join(output_dir, "CONTENT_LOSS_REPORT.txt")
                with open(report_path, "w", encoding="utf-8") as f:
                    f.write(report + "\n")
                say(f"  [CONTENT-GUARD] Report written to: {report_path}")
            except Exception as werr:
                say(f"  [CONTENT-GUARD] Could not write report file: {werr}")

        return summary

    except Exception as e:
        # The guard must never break the pipeline.
        say(f"  [CONTENT-GUARD] Skipped (internal error, non-fatal): {e}")
        return {"ok": None, "error": str(e)}
