"""DTD validation for PXML — thin wrapper over the ported PAX validator.

Runs ONLY well-formedness + DTD conformance (auto-detected PAX DTD), NOT the
extra PAX business-rule checks in validate_pax.PAXValidator.validate_file(), and
normalizes the result for the side-by-side validator UI.

Reused engine: usforms_pipeline/validate_pax.py (ported from the "XML Conversion
Chapter" project). DTD asset files live in usforms_pipeline/validation_rule_files/.
"""
import os
import re
import tempfile
from pathlib import Path

# The ACTIVE REGION's DTD folder (regions/<REGION_ID>/dtd): pax.dtd / pix.dtd / ... and
# the shared entities file. PAXValidator._pick_dtd() looks here.
from .region import get_region

_RULE_DIR = get_region().dtd_dir


def rule_dir_available() -> bool:
    return _RULE_DIR.is_dir() and any(_RULE_DIR.glob("*.dtd"))


# Matches an element start/end tag: group(1)='/' for a close tag, group(2)=name,
# group(4)='/' for a self-closing tag. Not a full XML parser — a pragmatic tag
# walker used only to list unbalanced tags (see _scan_unbalanced_tags).
_SCAN_TAG_RE = re.compile(r"<(/?)([A-Za-z_][\w.\-]*)([^>]*?)(/?)>")


def _scan_unbalanced_tags(text: str) -> list:
    """Deterministically list EVERY unclosed opening tag and every stray closing
    tag in `text`, independent of the XML parser's stop-at-first-error behavior.

    A standard well-formedness parse reports only the FIRST break, so a later
    problem (e.g. an unclosed <footnote> deep in the document) stays hidden until
    the earlier ones are fixed. This secondary pass walks all tags with an
    error-RECOVERING stack: when a close tag matches an opening tag further down
    the stack, every tag opened after it is reported as unclosed and the stack is
    unwound to that point — so one real mistake does not cascade into dozens of
    bogus ones. Report-only and best-effort; it never raises.

    Returns UI-shaped error dicts, grouped by tag name (one row per distinct
    unclosed/stray tag, with a count and every affected line).
    """
    if not text:
        return []
    try:
        # Blank out non-element constructs so their '<' / '>' can't be read as tags,
        # replacing each with same-length spaces (newlines kept) so line numbers stay exact.
        def _blank(m):
            return re.sub(r"[^\n]", " ", m.group(0))

        s = text
        s = re.sub(r"<\?.*?\?>", _blank, s, flags=re.DOTALL)                        # processing instructions
        s = re.sub(r"<!--.*?-->", _blank, s, flags=re.DOTALL)                       # comments
        s = re.sub(r"<!\[CDATA\[.*?\]\]>", _blank, s, flags=re.DOTALL)              # CDATA sections
        s = re.sub(r"<!DOCTYPE[^>\[]*(?:\[[^\]]*\])?[^>]*>", _blank, s, flags=re.DOTALL)  # DOCTYPE

        def _line_of(pos: int) -> int:
            return text.count("\n", 0, pos) + 1

        stack = []     # (name, pos) of currently-open elements
        problems = []  # (pos, kind, name) where kind is "unclosed" | "extra_close"
        for m in _SCAN_TAG_RE.finditer(s):
            closing, name, _attrs, selfclose = m.group(1), m.group(2), m.group(3), m.group(4)
            pos = m.start()
            if closing:
                idx = next((i for i in range(len(stack) - 1, -1, -1) if stack[i][0] == name), None)
                if idx is None:
                    problems.append((pos, "extra_close", name))
                else:
                    for uname, upos in stack[idx + 1:]:   # opened after the match, never closed
                        problems.append((upos, "unclosed", uname))
                    del stack[idx:]
            elif selfclose:
                continue
            else:
                stack.append((name, pos))
        for uname, upos in stack:                         # still open at end of document
            problems.append((upos, "unclosed", uname))

        # Group by (kind, name) so a repeated mistake is one row with a count + all lines.
        from collections import defaultdict
        grouped = defaultdict(list)
        for pos, kind, name in problems:
            grouped[(kind, name)].append(_line_of(pos))

        errors = []
        for (kind, name), lines in grouped.items():
            lines = sorted(set(lines))
            first, n = lines[0], len(lines)
            plural = "s" if n > 1 else ""
            if kind == "unclosed":
                title = f"Unclosed tag <{name}>"
                message = (f"<{name}> is opened but never closed with a matching </{name}> "
                           f"({n} occurrence{plural}).")
                fix = (f"Add a </{name}> for each unclosed <{name}>, or write it self-closing "
                       f"(<{name}/>) if it is meant to be empty.")
            else:
                title = f"Unexpected closing tag </{name}>"
                message = (f"</{name}> has no matching open <{name}> ({n} occurrence{plural}) — "
                           f"it is extra, or its open tag was written self-closing (/>).")
                fix = f"Remove each stray </{name}>, or add the matching <{name}> before it."
            errors.append({
                "line": first,
                "lines": lines,
                "column": 0,
                "severity": "error",
                "rule": "SYNTAX",
                "title": title,
                "message": message,
                "fix": fix,
                "location": f"Line {first}" + (f" (+{n - 1} more)" if n > 1 else ""),
                "count": n,
            })
        errors.sort(key=lambda e: e["line"])
        return errors
    except Exception as exc:   # never let the supplementary scan break validation
        print(f"[DTD] unbalanced-tag scan skipped: {exc}")
        return []


def _wellformed_error_list(parser_error: dict, text: str) -> list:
    """The parser's own first-error row, followed by every OTHER unbalanced tag the
    full-document scan found (deduplicated against the parser's line)."""
    extra = [e for e in _scan_unbalanced_tags(text) if e["line"] != parser_error.get("line")]
    return [parser_error] + extra


def _make_validator(family: str = "auto", accept_other_families: bool = False):
    from .validate_pax import PAXValidator
    validator = PAXValidator(_RULE_DIR)
    fam = (family or "auto").lower()
    validator.forced_family = None if fam == "auto" else fam
    # family='both': the document's own DTD decides, but tags/attributes the OTHER selectable family
    # (PAX <-> PIX) declares are accepted; only what neither allows is flagged.
    validator.accept_other_families = accept_other_families
    return validator


def _validate_pxml_dtd_single(text: str, family: str = "auto", accept_other_families: bool = False) -> dict:
    """Validate PXML `text` against the auto-detected PAX DTD (or the forced
    `family` — "pax" / "pix" — when given).

    Returns a JSON-serializable dict:
      {
        available, wellformed, validation_passed, dtd_label,
        error_count, warning_count,
        errors: [{line, lines, column?, severity, rule, title, message, fix,
                  location, count}, ...]
      }
    Errors are grouped by distinct message (one row per issue type, listing all
    affected lines) — matching the engine's own grouping.
    """
    tmp = tempfile.NamedTemporaryFile("w", suffix=".pxml", delete=False, encoding="utf-8")
    try:
        tmp.write(text or "")
        tmp.close()
        path = Path(tmp.name)
        validator = _make_validator(family, accept_other_families)  # lazy import inside: keeps cost off app startup

        # Step 1 — well-formedness. If this fails, DTD validation is meaningless.
        ok, short_msg, detail = validator.validate_xml_wellformed(path)
        if not ok:
            parser_error = {
                "line": detail.get("line", 0),
                "lines": [detail.get("line", 0)] if detail.get("line") else [],
                "column": detail.get("column", 0),
                "severity": "error",
                "rule": "SYNTAX",
                "title": "XML is not well-formed",
                "message": detail.get("plain_english") or short_msg,
                "fix": detail.get("fix", ""),
                "location": f"Line {detail.get('line', 0)}" if detail.get("line") else "",
                "count": 1,
            }
            # The parser stops at the first break; add every OTHER unbalanced tag so
            # all unclosed/stray tags (e.g. a later unclosed <footnote>) show in one pass.
            all_errors = _wellformed_error_list(parser_error, text)
            return {
                "available": True,
                "wellformed": False,
                "validation_passed": False,
                "dtd_label": None,
                "error_count": len(all_errors),
                "warning_count": 0,
                "errors": all_errors,
            }

        # Step 2 — DTD conformance (auto-detects DTD, defaults to pax.dtd).
        issues, dtd_label = validator.validate_dtd(path)

        errors = []
        error_count = warning_count = 0
        for g in issues:
            severity = g.get("severity", "error")
            if severity == "error":
                error_count += 1
            else:
                warning_count += 1
            errors.append({
                "line": g.get("line", 0),
                "lines": g.get("lines", []),
                "severity": severity,
                "rule": g.get("rule", "DTD"),
                "title": g.get("title", ""),
                "message": g.get("message", g.get("title", "")),
                "fix": g.get("fix", ""),
                "location": g.get("location", ""),
                "count": g.get("count", 1),
            })

        return {
            "available": True,
            "wellformed": True,
            "validation_passed": error_count == 0,
            "dtd_label": dtd_label or None,
            "error_count": error_count,
            "warning_count": warning_count,
            "errors": errors,
        }
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _norm_issue(issue: dict, default_rule: str) -> dict:
    """Normalize any engine issue dict into the unified UI error shape."""
    lines = issue.get("lines") or ([issue["line"]] if issue.get("line") else [])
    return {
        "line": issue.get("line", 0) or 0,
        "lines": [l for l in lines if l],
        "severity": issue.get("severity", "error"),
        "rule": issue.get("rule", default_rule),
        "title": issue.get("title") or issue.get("message", ""),
        "message": issue.get("message") or issue.get("title", ""),
        "fix": issue.get("fix", "") or issue.get("detail", ""),
        "location": issue.get("location", ""),
        "count": issue.get("count", 1) or 1,
    }


def _validate_pxml_full_single(text: str, family: str = "auto", accept_other_families: bool = False) -> dict:
    """Full PAX validation: well-formedness + DTD + citation/heading/form/footnote/
    attribute/whitelist/reference checks. Flattens every issue category into the same
    UI error shape as validate_pxml_dtd().
    """
    tmp =tempfile.NamedTemporaryFile("w", suffix=".pxml", delete=False, encoding="utf-8")
    try:
        tmp.write(text or "")
        tmp.close()
        path = Path(tmp.name)
        try:
            res = _make_validator(family, accept_other_families).validate_file(path)
        except Exception as e:
            # A rule check choked on this document — fall back to DTD-only rather than 503.
            print(f"[DTD] Full PAX validation errored ({e}); returning DTD-only result")
            return _validate_pxml_dtd_single(text, family, accept_other_families)
        details = res.get("details", {}) or {}

        errors = []
        # Not well-formed → syntax errors only, nothing else runs.
        if not res.get("wellformed", False):
            wf = details.get("wellformed_detail", {}) or {}
            parser_error = {
                "line": wf.get("line", 0), "lines": [wf.get("line")] if wf.get("line") else [],
                "column": wf.get("column", 0), "severity": "error", "rule": "SYNTAX",
                "title": "XML is not well-formed",
                "message": wf.get("plain_english") or details.get("wellformed_error", "Not well-formed"),
                "fix": wf.get("fix", ""),
                "location": f"Line {wf.get('line', 0)}" if wf.get("line") else "", "count": 1,
            }
            # The parser stops at the first break; add every OTHER unbalanced tag so
            # all unclosed/stray tags (e.g. a later unclosed <footnote>) show in one pass.
            errors = _wellformed_error_list(parser_error, text)
        else:
            # Each category: (details path, default rule label)
            for issue in details.get("dtd_issues", []) or []:
                errors.append(_norm_issue(issue, "DTD"))
            for issue in (details.get("citations", {}) or {}).get("citation_issues", []) or []:
                errors.append(_norm_issue(issue, "CITATION"))
            for issue in details.get("heading_issues", []) or []:
                errors.append(_norm_issue(issue, "HEADING"))
            for issue in (details.get("forms", {}) or {}).get("form_issues", []) or []:
                errors.append(_norm_issue(issue, "FORM"))
            for issue in (details.get("footnotes", {}) or {}).get("footnote_issues", []) or []:
                errors.append(_norm_issue(issue, "FOOTNOTE"))
            for issue in details.get("attribute_issues", []) or []:
                errors.append(_norm_issue(issue, "ATTRIBUTE"))
            for issue in details.get("whitelist_issues", []) or []:
                errors.append(_norm_issue(issue, "TAG"))
            for issue in details.get("reference_issues", []) or []:
                errors.append(_norm_issue(issue, "REFERENCE"))
            # Errors first, then by descending occurrence count.
            errors.sort(key=lambda e: (0 if e["severity"] == "error" else 1, -e["count"]))

        return {
            "available": True,
            "wellformed": res.get("wellformed", False),
            "validation_passed": res.get("validation_passed", False),
            "dtd_label": details.get("dtd_label") or None,
            # When not well-formed, the error list is the syntax rows built above
            # (parser + full unbalanced-tag scan), so count those rather than the
            # engine's single-syntax-error count.
            "error_count": (len([e for e in errors if e.get("severity") == "error"])
                            if not res.get("wellformed", False) else res.get("error_count", 0)),
            "warning_count": res.get("warning_count", 0),
            "errors": errors,
        }
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


# ── validate against BOTH DTD families ────────────────────────────────────────

def _both_label(res: dict) -> dict:
    """Name the result for the UI: the document's own DTD, with the other family's tags accepted."""
    from .region import get_region
    region = get_region()
    others = [f.label for f in region.selectable_families if f.label != res.get("dtd_label")]
    if res.get("dtd_label") and others:
        res["dtd_label"] = f"{res['dtd_label']} (+ {' / '.join(others)} tags accepted)"
    return res


def validate_pxml_dtd(text: str, family: str = "auto") -> dict:
    """DTD validation.

    family: "auto" (only the DOCTYPE's DTD), a family id ("pax" / "pix" ...), or "both":
    the document's own DTD decides, but any tag or attribute that the OTHER selectable family
    declares is accepted -- existing files get new revisions tagged in PAX or PIX style as needed,
    so only what NEITHER family allows is flagged (see PAXValidator.accept_other_families).
    """
    if (family or "auto").lower() == "both":
        return _both_label(_validate_pxml_dtd_single(text, "auto", accept_other_families=True))
    return _validate_pxml_dtd_single(text, family)


def validate_pxml_full(text: str, family: str = "auto") -> dict:
    """Full validation (DTD + business rules); same family choices as validate_pxml_dtd."""
    if (family or "auto").lower() == "both":
        return _both_label(_validate_pxml_full_single(text, "auto", accept_other_families=True))
    return _validate_pxml_full_single(text, family)