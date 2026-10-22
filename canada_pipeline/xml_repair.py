"""Deterministic XML structure repair for PAX conversion output.

The LLM occasionally emits structurally broken XML — most commonly a closing
tag that was never opened (orphan), or an element left unclosed. This module
walks the markup with a tag stack and repairs those issues deterministically:

  * orphan </tag>  (no matching open on the stack)      -> dropped
  * </tag> that matches a tag deeper in the stack       -> auto-close the
                                                           intervening tags,
                                                           then close it
  * tags still open at end-of-document                  -> closed at EOF

It is intentionally conservative and reports every change it makes, so nothing
is altered silently. Entities (&dblsect; &key; &apos;), self-closing tags
(<sep/> <leaveout/> <name/>), comments and processing instructions are
preserved as-is.

No LLM, no network — runs in milliseconds and fixes ALL occurrences at once.
"""

import re
from typing import Dict, List, Tuple

# Matches one markup construct: comment, CDATA, processing instr, close, or open/self-close tag.
_CONSTRUCT = re.compile(
    r"<!--.*?-->"          # comment
    r"|<!\[CDATA\[.*?\]\]>" # cdata
    r"|<\?.*?\?>"          # processing instruction / decl
    r"|</[^>]+>"           # closing tag
    r"|<[^>]+>",           # opening or self-closing tag
    re.DOTALL,
)

_NAME = re.compile(r"^</?\s*([\w.:-]+)")


def _tag_name(raw: str) -> str:
    m = _NAME.match(raw)
    return m.group(1) if m else ""


def repair_xml_structure(xml: str) -> Tuple[str, Dict]:
    """Return (repaired_xml, report).

    report keys:
      orphans_dropped:   list of tag names dropped (closing tag with no open)
      auto_closed:       list of tag names auto-closed before a mismatched close
      closed_at_eof:     list of tag names closed at end of document
      changed:           bool — True if any repair was applied
    """
    out: List[str] = []
    stack: List[str] = []
    report = {"orphans_dropped": [], "auto_closed": [], "closed_at_eof": [], "changed": False}

    pos = 0
    for m in _CONSTRUCT.finditer(xml):
        # text/content before this construct — preserve verbatim
        if m.start() > pos:
            out.append(xml[pos:m.start()])
        pos = m.end()

        raw = m.group(0)

        # Non-element constructs: pass through untouched
        if raw.startswith("<!--") or raw.startswith("<![CDATA[") or raw.startswith("<?"):
            out.append(raw)
            continue

        is_close = raw.startswith("</")
        is_self = (not is_close) and raw.rstrip().endswith("/>")
        name = _tag_name(raw)

        if is_self or not name:
            out.append(raw)
            continue

        if not is_close:
            # opening tag
            stack.append(name)
            out.append(raw)
            continue

        # closing tag --------------------------------------------------------
        if stack and stack[-1] == name:
            stack.pop()
            out.append(raw)
        elif name in stack:
            # Mismatched but the tag IS open deeper — auto-close intervening tags
            while stack and stack[-1] != name:
                victim = stack.pop()
                out.append(f"</{victim}>")
                report["auto_closed"].append(victim)
            if stack:
                stack.pop()
            out.append(raw)
            report["changed"] = True
        else:
            # orphan closing tag — drop it
            report["orphans_dropped"].append(name)
            report["changed"] = True
            # (do not append raw)

    # trailing content
    if pos < len(xml):
        out.append(xml[pos:])

    # close anything left open at EOF
    while stack:
        victim = stack.pop()
        out.append(f"</{victim}>")
        report["closed_at_eof"].append(victim)
        report["changed"] = True

    return "".join(out), report


def repair_summary(report: Dict) -> str:
    """Human-readable one-line summary of a repair report."""
    parts = []
    if report["orphans_dropped"]:
        parts.append(f"{len(report['orphans_dropped'])} orphan close-tag(s) dropped")
    if report["auto_closed"]:
        parts.append(f"{len(report['auto_closed'])} tag(s) auto-closed")
    if report["closed_at_eof"]:
        parts.append(f"{len(report['closed_at_eof'])} tag(s) closed at EOF")
    return "; ".join(parts) if parts else "no structural changes"
