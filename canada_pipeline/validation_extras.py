"""Extras for the side-by-side PAX validator: pretty-print, AI-fix prompt, and
deterministic batch fixers. Ported from the "XML Conversion Chapter" project so the
US Forms validator matches its Repair / Format / Fix / Fix-all behaviour.

Pure Python + lxml (already a dependency). The AI-fix prompt is consumed by the
/api/validation/fix endpoint, which calls Claude via usforms_pipeline.config.
"""
import re
from typing import Tuple


# ─── AI fix prompt (from Chapter src/processing_pipeline.py) ──────────────────
FIX_SYSTEM_PROMPT = (
    "You are a PAX XML repair assistant for Thomson Reuters legal publications. "
    "You will be given a snippet of PAX XML that contains a structural error, along with "
    "the validator error message. "
    "Your ENTIRE response must be the corrected XML snippet and NOTHING else. "
    "The first character of your response MUST be '<'. "
    "Do NOT restate or explain the problem, do NOT write any sentence, do NOT use markdown "
    "or code fences, do NOT add any text before or after the XML. "
    "Preserve all text content exactly as-is. Only fix the XML structure (tags, nesting, attributes). "
    "If you cannot fix it, return the snippet unchanged — never return prose."
)


def build_fix_prompt(error_msg: str, error_line: int, context_xml: str) -> str:
    return (
        f"The PAX XML validator reported the following error at line {error_line}:\n\n"
        f"  {error_msg}\n\n"
        f"Here is the XML snippet surrounding the error (fix only what is broken). "
        f"Respond with ONLY the corrected XML, starting with '<':\n\n"
        f"{context_xml}"
    )


# Prose markers that indicate the model preface leaked into the response.
_PROSE_MARKERS = re.compile(
    r"(?i)\b(?:I see|let me|here(?:'s| is)|the (?:issue|problem|structural)|"
    r"corrected xml|fixed xml|I(?:'ll| will)|to fix|the error)\b"
)


def extract_fix_xml(raw: str) -> str:
    """Extract ONLY the XML from an AI fix response, stripping any prose preamble/suffix
    or markdown fences the model may add despite instructions. Defensive: the /fix
    endpoint splices this straight into the document, so a leaked sentence would corrupt
    the XML (observed: "I see the structural issue ... Let me provide the corrected XML:").
    """
    s = (raw or "").strip()
    if not s:
        return s

    # 1. Prefer a fenced code block if the model wrapped the XML in ``` / ```xml.
    fences = re.findall(r"```(?:xml)?\s*\n?(.*?)```", s, flags=re.DOTALL)
    if fences:
        return max(fences, key=len).strip()

    # 2. Drop a leading prose preamble ending in a ':' line
    #    (e.g. "Let me provide the corrected XML:") — cut after the LAST such intro.
    intros = list(re.finditer(
        r"(?im)^.*?(?:corrected xml|fixed xml|here(?:'s| is) the [^:\n]*|let me [^:\n]*|"
        r"the (?:corrected|fixed)[^:\n]*)\s*:\s*$", s))
    if intros:
        s = s[intros[-1].end():].strip()

    # 3. Trim to the outermost XML span: from the first tag-open to the last '>'.
    first = re.search(r"<[A-Za-z/!?]", s)
    if first and first.start() > 0:
        # Only trim leading text if it looks like prose (no XML tag before it).
        if _PROSE_MARKERS.search(s[:first.start()]):
            s = s[first.start():]
    last = s.rfind(">")
    if last != -1 and last < len(s) - 1:
        # Only trim trailing text if what follows the last '>' looks like prose.
        if _PROSE_MARKERS.search(s[last + 1:]):
            s = s[:last + 1]

    return s.strip()


# ─── Format: lxml pretty-print with well-formed fallback ──────────────────────
def format_xml(text: str, indent: int = 2) -> dict:
    """Pretty-print XML with 2-space indentation and no blank lines.

    Returns {text, method, warning?}. If the XML is not well-formed, only blank
    lines are stripped (indentation preserved) and a warning is returned.
    """
    from lxml import etree

    text = (text or "").strip()
    try:
        root = etree.fromstring(text.encode("utf-8"))
        etree.indent(root, space=" " * indent)
        pretty = etree.tostring(root, pretty_print=True, xml_declaration=False, encoding="unicode")
        clean = "\n".join(line for line in pretty.splitlines() if line.strip())
        return {"text": clean, "method": "pretty-print"}
    except etree.XMLSyntaxError:
        clean = "\n".join(line for line in text.splitlines() if line.strip())
        return {
            "text": clean,
            "method": "compact-only",
            "warning": "XML has errors — blank lines removed but indentation unchanged. "
                       "Repair the structure first for full pretty-print.",
        }


# ─── Batch fixers for known PAX rules (deterministic regex, no LLM) ───────────
def _fix_fn004(xml: str):
    count = [0]

    def fix_block(m):
        inner = m.group(1)
        fixed = re.sub(r'<p>(.*?)</p>', lambda x: f'<para><para.text>{x.group(1)}</para.text></para>', inner, flags=re.DOTALL)
        if fixed != inner:
            count[0] += 1
        if not fixed.strip().startswith('<para'):
            stripped = fixed.strip()
            if stripped:
                count[0] += 1
                return f'<footnote.body><para><para.text>{stripped}</para.text></para></footnote.body>'
        return f'<footnote.body>{fixed}</footnote.body>'
    result = re.sub(r'<footnote\.body>(.*?)</footnote\.body>', fix_block, xml, flags=re.DOTALL)
    return result, count[0]


def _fix_form030(xml: str):
    count = [0]
    xml = re.sub(
        r'<inline\.instr>\s*<inline\.instr>(.*?)</inline\.instr>\s*</inline\.instr>',
        r'<inline.instr>\1</inline.instr>',
        xml, flags=re.DOTALL
    )

    def fix_block(m):
        inner = m.group(1)

        def wrap_bracket(bm):
            before = inner[:bm.start()]
            opens = before.count('<inline.instr>')
            closes = before.count('</inline.instr>')
            if opens > closes:
                return bm.group(0)
            count[0] += 1
            return f'<inline.instr>{bm.group(0)}</inline.instr>'
        return f'<form.text>{re.sub(r"\[([^\]]+)\]", wrap_bracket, inner)}</form.text>'

    result = re.sub(r'<form\.text>(.*?)</form\.text>', fix_block, xml, flags=re.DOTALL)
    return result, count[0]


def _fix_pres011(xml: str):
    count = [0]

    def fix_block(m):
        content = m.group(1)
        if '<name>' in content or '<name/>' in content:
            return m.group(0)
        count[0] += 1
        return f'<name.block>{content}<name/></name.block>'
    result = re.sub(r'<name\.block>(.*?)</name\.block>', fix_block, xml, flags=re.DOTALL)
    return result, count[0]


def _fix_pres001(xml: str):
    _type_map = {"CHK": "checklist", "FMS": "form", "CLS": "clause",
                 "LCV": "looseleaf", "LSC": "looseleaf", "LS2": "looseleaf",
                 "FCV": "form", "FLG": "form", "PTN": "practitioner", "REL": "related"}
    count = [0]

    def fix_block(m):
        if 'type=' in m.group(0):
            return m.group(0)
        item_m = re.search(r'<online\.view\.item>(\w+)</online\.view\.item>', m.group(0))
        view_type = _type_map.get(item_m.group(1), "reference") if item_m else "reference"
        count[0] += 1
        return f'<online.view type="{view_type}">{m.group(1)}</online.view>'
    result = re.sub(r'<online\.view>(.*?)</online\.view>', fix_block, xml, flags=re.DOTALL)
    return result, count[0]


FIX_ALL_DISPATCH = {
    "FN-004": _fix_fn004,
    "FORM-030": _fix_form030,
    "PRES-011": _fix_pres011,
    "PRES-001": _fix_pres001,
}


def fix_all(text: str, rule: str) -> Tuple[str, int]:
    """Apply the batch fixer for `rule`. Raises KeyError if no fixer exists."""
    fn = FIX_ALL_DISPATCH[rule.upper().strip()]
    return fn(text)
