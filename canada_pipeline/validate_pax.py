#!/usr/bin/env python3
"""
PAX XML Validation Script
Thomson Reuters - Phoenix Project
Version 1.0 - March 2026

Validates PAX DTD XML documents against:
- PAX DTD structural requirements
- Generic Spec business rules  
- AI conversion prompt requirements
- Content quality preservation
"""

import sys
import os
import re
import argparse
from pathlib import Path
from datetime import datetime
import subprocess
import xml.etree.ElementTree as ET
from typing import Dict, List, Tuple
import json

# Parsed-DTD cache (path -> lxml DTD). The PAX DTDs are large (~550 KB); parsing
# one on every validation is wasteful, so cache across calls.
_DTD_CACHE = {}


class PAXValidator:
    """Main validator class for PAX XML documents"""
    
    def __init__(self, schematron_dir: Path):
        self.schematron_dir = schematron_dir
        self.master_sch = schematron_dir / "pax-validation-master.sch"
        self.results = {
            'errors': [],
            'warnings': [],
            'info': [],
            'stats': {}
        }
        
    # Known HTML/named entities that are valid in PAX output but not built-in XML
    _HTML_ENTITIES = {
        'sect': '§', 'para': '¶', 'copy': '©', 'reg': '®', 'trade': '™',
        'mdash': '—', 'ndash': '–', 'ldquo': '\u201c', 'rdquo': '\u201d',
        'lsquo': '\u2018', 'rsquo': '\u2019', 'nbsp': '\u00a0',
        'hellip': '…', 'bull': '•', 'middot': '·', 'laquo': '«', 'raquo': '»',
        'times': '×', 'divide': '÷', 'plusmn': '±', 'deg': '°',
        'micro': 'µ', 'pound': '£', 'euro': '€', 'yen': '¥', 'cent': '¢',
        'dagger': '†', 'Dagger': '‡', 'permil': '‰', 'szlig': 'ß',
        'agrave': 'à', 'Agrave': 'À', 'aacute': 'á', 'Aacute': 'Á',
        'acirc': 'â', 'Acirc': 'Â', 'atilde': 'ã', 'Atilde': 'Ã',
        'auml': 'ä', 'Auml': 'Ä', 'aring': 'å', 'Aring': 'Å',
        'aelig': 'æ', 'AElig': 'Æ', 'ccedil': 'ç', 'Ccedil': 'Ç',
        'egrave': 'è', 'Egrave': 'È', 'eacute': 'é', 'Eacute': 'É',
        'ecirc': 'ê', 'Ecirc': 'Ê', 'euml': 'ë', 'Euml': 'Ë',
        'igrave': 'ì', 'Igrave': 'Ì', 'iacute': 'í', 'Iacute': 'Í',
        'icirc': 'î', 'Icirc': 'Î', 'iuml': 'ï', 'Iuml': 'Ï',
        'ograve': 'ò', 'Ograve': 'Ò', 'oacute': 'ó', 'Oacute': 'Ó',
        'ocirc': 'ô', 'Ocirc': 'Ô', 'otilde': 'õ', 'Otilde': 'Õ',
        'ouml': 'ö', 'Ouml': 'Ö', 'oslash': 'ø', 'Oslash': 'Ø',
        'ugrave': 'ù', 'Ugrave': 'Ù', 'uacute': 'ú', 'Uacute': 'Ú',
        'ucirc': 'û', 'Ucirc': 'Û', 'uuml': 'ü', 'Uuml': 'Ü',
        'yacute': 'ý', 'Yacute': 'Ý', 'thorn': 'þ', 'Thorn': 'Þ',
        'ntilde': 'ñ', 'Ntilde': 'Ñ', 'eth': 'ð', 'ETH': 'Ð',
        'alpha': 'α', 'beta': 'β', 'gamma': 'γ', 'delta': 'δ',
        'epsilon': 'ε', 'zeta': 'ζ', 'eta': 'η', 'theta': 'θ',
        'iota': 'ι', 'kappa': 'κ', 'lambda': 'λ', 'mu': 'μ',
        'nu': 'ν', 'xi': 'ξ', 'pi': 'π', 'rho': 'ρ', 'sigma': 'σ',
        'tau': 'τ', 'upsilon': 'υ', 'phi': 'φ', 'chi': 'χ',
        'psi': 'ψ', 'omega': 'ω',
        'frac12': '½', 'frac14': '¼', 'frac34': '¾',
        'sup2': '²', 'sup3': '³',
        'acute': '´', 'cedil': '¸', 'uml': '¨', 'macr': '¯',
        'shy': '\u00ad', 'ordf': 'ª', 'ordm': 'º',
    }
    # The 5 entities XML parsers handle natively — never substitute these
    _XML_BUILTINS = {'amp', 'lt', 'gt', 'apos', 'quot'}

    def _safe_parse(self, xml_file: Path) -> ET.ElementTree:
        """Parse XML, substituting known HTML entities so the parser doesn't
        choke on intentional business entities like &sect;.
        The original file is never modified."""
        import re
        content = xml_file.read_text(encoding='utf-8', errors='replace')

        def _sub(m):
            name = m.group(1)
            if name in self._XML_BUILTINS:
                return m.group(0)  # leave &amp; &lt; etc. for the parser
            return self._HTML_ENTITIES.get(name, f'\u25a0{name}\u25a0')
            # unknown entities become ■name■ so they don't break parsing
            # but can still be spotted if needed

        content = re.sub(r'&([a-zA-Z][a-zA-Z0-9]*);', _sub, content)
        return ET.ElementTree(ET.fromstring(content.encode('utf-8')))

    # Plain-English translations for XML parser error codes
    _PARSE_ERROR_EXPLANATIONS = {
        'no element found': (
            'The file appears to be cut off or incomplete.',
            'A tag was opened but never closed. Scroll to the end of the file and check that all opened tags have a matching closing tag.'
        ),
        'mismatched tag': (
            'A closing tag was found that does not match any open tag — it is either extra, or the matching opening tag was accidentally written as self-closing (ending with /> instead of >).',
            'See the fix advice below for the specific tag and line.'
        ),
        'not well-formed': (
            'The file contains an invalid character or syntax that XML does not allow.',
            'Check the location above for stray characters, unescaped & < > symbols, or broken tag syntax.'
        ),
        'undefined entity': (
            'An unrecognised special character shortcut (entity) was found.',
            'Replace the entity with its actual character, or escape it as &amp; if it is a literal & symbol.'
        ),
        'junk after document element': (
            'There is extra content after the final closing tag of the document.',
            'Delete any text, tags, or whitespace that appears after the last closing tag at the very end of the file.'
        ),
        'syntax error': (
            'The XML syntax is broken at this location.',
            'Check for a missing quote in an attribute, a < or > inside text, or a malformed tag.'
        ),
        'duplicate attribute': (
            'The same attribute appears twice on the same tag.',
            'Remove the duplicate attribute from the tag shown above.'
        ),
    }

    def _error_context(self, xml_file: Path, error_line: int, window: int = 5) -> List[Dict]:
        """Return lines around the error, dedented so indentation doesn't obscure content."""
        try:
            all_lines = xml_file.read_text(encoding='utf-8', errors='replace').splitlines()
        except Exception:
            return []
        start = max(0, error_line - window - 1)
        end = min(len(all_lines), error_line + window)
        snippet = all_lines[start:end]

        # Strip common leading whitespace so deeply-indented XML is readable
        non_empty = [l for l in snippet if l.strip()]
        if non_empty:
            common = min(len(l) - len(l.lstrip()) for l in non_empty)
        else:
            common = 0

        context = []
        for i, text in enumerate(snippet, start=start + 1):
            context.append({
                'line_num': i,
                'content': text[common:],   # remove common indent
                'is_error': i == error_line,
            })
        return context

    def _find_unclosed_tag(self, xml_file: Path) -> str:
        """Scan for the innermost unclosed tag — used when file ends prematurely."""
        import re
        try:
            content = xml_file.read_text(encoding='utf-8', errors='replace')
        except Exception:
            return ''
        stack = []
        # Match opening tags (not self-closing, not closing, not comments/PI)
        for m in re.finditer(r'<(/?)([a-zA-Z_][\w.\-]*)[^>]*?(/?)>', content):
            is_close = m.group(1) == '/'
            tag = m.group(2)
            is_self = m.group(3) == '/'
            if is_self:
                continue
            if is_close:
                if stack and stack[-1] == tag:
                    stack.pop()
            else:
                stack.append(tag)
        return stack[-1] if stack else ''

    def validate_xml_wellformed(self, xml_file: Path) -> Tuple[bool, str, Dict]:
        """Check if XML is well-formed. Returns (ok, short_message, detail_dict)."""
        try:
            self._safe_parse(xml_file)
            return True, "XML is well-formed", {}
        except ET.ParseError as e:
            err_str = str(e)
            # Extract line / column from the error message
            import re
            line_num, col_num = 0, 0
            m = re.search(r'line (\d+), column (\d+)', err_str)
            if m:
                line_num, col_num = int(m.group(1)), int(m.group(2))

            # Pick the best plain-English explanation
            plain_english, fix_advice = (
                'The XML file has a structural problem that prevents it from being read.',
                'Open the file at the line shown and correct the syntax error.'
            )
            for key, (expl, fix) in self._PARSE_ERROR_EXPLANATIONS.items():
                if key in err_str.lower():
                    plain_english, fix_advice = expl, fix
                    break

            # For EOF errors, try to name the unclosed tag
            # For mismatched tag errors, extract the bad closing tag from the error line
            unclosed = ''
            if 'mismatched tag' in err_str.lower():
                # Read the actual line that caused the mismatch
                try:
                    file_lines = xml_file.read_text(encoding='utf-8', errors='replace').splitlines()
                    if 0 < line_num <= len(file_lines):
                        bad_line = file_lines[line_num - 1].strip()
                        # Extract the closing tag name from that line e.g. </designator>
                        m2 = re.search(r'</([a-zA-Z_][\w.\-]*)', bad_line)
                        if m2:
                            bad_tag = m2.group(1)
                            unclosed = bad_tag
                            fix_advice = (
                                f'Line {line_num} has </{bad_tag}> but the tag it is trying to close was '
                                f'never opened (or was already closed earlier). '
                                f'Remove the extra </{bad_tag}> on line {line_num}, or find the matching '
                                f'<{bad_tag}> opening tag and make sure it is not accidentally self-closed '
                                f'(i.e. ends with /> instead of >).'
                            )
                except Exception:
                    pass
            elif 'no element found' in err_str.lower():
                unclosed = self._find_unclosed_tag(xml_file)
                if unclosed:
                    fix_advice = (
                        f'The tag <{unclosed}> was opened but never closed. '
                        f'Find the last <{unclosed}> in the file and add a matching </{unclosed}> after it.'
                    )

            context = self._error_context(xml_file, line_num) if line_num else []

            detail = {
                'raw': f"XML Parse Error: {err_str}",
                'line': line_num,
                'column': col_num,
                'plain_english': plain_english,
                'fix': fix_advice,
                'unclosed_tag': unclosed,
                'context': context,
            }
            return False, f"XML Parse Error: {err_str}", detail
    
    def run_schematron_validation(self, xml_file: Path) -> bool:
        """Run Schematron validation using Saxon or other XSLT processor"""
        
        # Check for Saxon
        saxon_jar = self._find_saxon()
        
        if not saxon_jar:
            print("WARN  WARNING: Saxon XSLT processor not found")
            print("   Install Saxon-HE for full Schematron validation:")
            print("   https://sourceforge.net/projects/saxon/files/Saxon-HE/")
            return False
        
        # ISO Schematron requires 3-step process:
        # 1. Include phase
        # 2. Abstract pattern expansion  
        # 3. SVRL generation
        # For now, we'll do basic XML validation
        
        return True
    
    def _find_saxon(self) -> Path:
        """Locate Saxon JAR file"""
        possible_locations = [
            Path("C:/Program Files/Saxon/saxon-he.jar"),
            Path.home() / "saxon" / "saxon-he.jar",
            Path("saxon-he.jar")
        ]
        
        for location in possible_locations:
            if location.exists():
                return location
        return None
    
    def validate_citations(self, xml_file: Path) -> Dict:
        """Validate citation accuracy - custom checks"""
        tree = self._safe_parse(xml_file)
        root = tree.getroot()
        
        results = {
            'case_citations': 0,
            'statute_citations': 0,
            'periodical_citations': 0,
            'citation_issues': []
        }
        
        # Count citations
        results['case_citations'] = len(root.findall('.//cc'))
        results['statute_citations'] = len(root.findall('.//st'))
        results['periodical_citations'] = len(root.findall('.//pl'))
        
        # Check for " v. " in text OUTSIDE <cc> tags — possible untagged case citations.
        # We collect text only from elements that are NOT inside a <cc> tag.
        import re as _re
        _v_pat = _re.compile(r'\b[A-Z][A-Za-z\s,\.\']+\s+v\.?\s+[A-Z][A-Za-z\s,\.\']+')
        untagged_matches = []
        cc_set = set(root.findall('.//cc'))

        def _outside_cc(el):
            """True if el has no <cc> ancestor."""
            parent = el.getparent() if hasattr(el, 'getparent') else None
            while parent is not None:
                if parent.tag == 'cc':
                    return False
                parent = parent.getparent() if hasattr(parent, 'getparent') else None
            return True

        try:
            from lxml import etree as _letree
            _ltree = _letree.parse(str(xml_file))
            _lroot = _ltree.getroot()
            for el in _lroot.iter():
                if el.tag == 'cc':
                    continue  # skip already-tagged citations
                for text_part in (el.text or '', el.tail or ''):
                    for m in _v_pat.finditer(text_part):
                        line = getattr(el, 'sourceline', 0) or 0
                        untagged_matches.append((line, m.group(0).strip()))
        except Exception:
            pass  # fall back silently; lxml may not be available

        if untagged_matches:
            sample_lines = sorted(set(ln for ln, _ in untagged_matches if ln))[:8]
            sample_texts = [txt for _, txt in untagged_matches[:3]]
            line_str = ', '.join(str(l) for l in sample_lines) if sample_lines else 'unknown'
            if len(untagged_matches) > len(sample_lines):
                line_str += f' … ({len(untagged_matches)} total)'
            first_line = sample_lines[0] if sample_lines else 0
            results['citation_issues'].append({
                'rule':     'CC-003',
                'message':  f'{len(untagged_matches)} possible untagged case citation(s) in plain text (outside <cc> tags)',
                'detail':   f'Examples: {"; ".join(sample_texts[:3])}.\nFound near lines: {line_str}.',
                'fix':      'Wrap each case name in <cc finder="manual" type="case">Smith v. Jones</cc>. If the phrase is not a case name (e.g. volume abbreviation), no action needed.',
                'severity': 'warning',
                'line':     first_line,
            })
        
        # Check for statute citations in <st> tags containing " v. "
        for st_elem in root.findall('.//st'):
            text = ''.join(st_elem.itertext())
            if ' v. ' in text:
                results['citation_issues'].append({
                    'rule': 'ST-002',
                    'message': f'CRITICAL: Statute tag contains " v. " - should be <cc>: "{text[:50]}..."',
                    'severity': 'error'
                })
        
        # Check for periodical citations containing " v. "
        for pl_elem in root.findall('.//pl'):
            text = ''.join(pl_elem.itertext())
            if ' v. ' in text:
                results['citation_issues'].append({
                    'rule': 'PL-002',
                    'message': f'CRITICAL: Periodical tag contains " v. " - should be <cc>: "{text[:50]}..."',
                    'severity': 'error'
                })
        
        return results
    
    def validate_headings(self, xml_file: Path) -> List[Dict]:
        """Critical check for #1 error: bold text that should be headings"""
        tree = self._safe_parse(xml_file)
        root = tree.getroot()
        
        issues = []
        
        for para_text in root.findall('.//para.text'):
            for bold in para_text.findall('.//bold'):
                text = ''.join(bold.itertext()).strip()
                words = text.split()
                
                # Check if this is short, ALL CAPS, and only content in para
                if len(words) <= 3 and text.isupper():
                    para_all_text = ''.join(para_text.itertext()).strip()
                    if text == para_all_text:
                        issues.append({
                            'rule': 'HEAD-001',
                            'message': f'CRITICAL: Short ALL CAPS bold should be <head>: "{text}"',
                            'severity': 'error',
                            'location': 'para.text'
                        })
        
        return issues
    
    def validate_forms(self, xml_file: Path) -> Dict:
        """Validate form-specific requirements"""
        tree = self._safe_parse(xml_file)
        root = tree.getroot()
        
        results = {
            'forms_found': 0,
            'inline_instr_count': 0,
            'form_issues': []
        }
        
        forms = root.findall('.//form')
        results['forms_found'] = len(forms)
        
        for form in forms:
            # Check for square brackets without inline.instr
            for form_text in form.findall('.//form.text'):
                text = ''.join(form_text.itertext())
                if '[' in text and ']' in text:
                    # Check if wrapped in inline.instr
                    has_inline = form_text.find('.//inline.instr') is not None
                    if not has_inline:
                        bracket_content = text[text.find('[')+1:text.find(']')]
                        results['form_issues'].append({
                            'rule': 'INLINE-001',
                            'message': f'CRITICAL: Square brackets not in <inline.instr>: "[{bracket_content}]"',
                            'severity': 'error'
                        })
            
            # Count inline.instr elements
            results['inline_instr_count'] += len(form.findall('.//inline.instr'))
        
        return results
    
    def validate_footnotes(self, xml_file: Path) -> Dict:
        """Validate footnote structure"""
        tree = self._safe_parse(xml_file)
        root = tree.getroot()
        
        results = {
            'footnotes_found': 0,
            'footnote_issues': []
        }
        
        footnotes = root.findall('.//footnote')
        results['footnotes_found'] = len(footnotes)
        
        for fn in footnotes:
            # Check for required structure
            fn_ref = fn.find('footnote.reference')
            fn_body = fn.find('footnote.body')
            
            if fn_ref is None:
                results['footnote_issues'].append({
                    'rule': 'FN-001',
                    'message': 'CRITICAL: footnote missing footnote.reference',
                    'severity': 'error'
                })
            
            if fn_body is None:
                results['footnote_issues'].append({
                    'rule': 'FN-001',
                    'message': 'CRITICAL: footnote missing footnote.body',
                    'severity': 'error'
                })
            elif fn_body.find('para') is None:
                results['footnote_issues'].append({
                    'rule': 'FN-004',
                    'message': 'CRITICAL: footnote.body missing para element',
                    'severity': 'error'
                })
        
        return results
    
    def validate_attributes(self, xml_file: Path) -> List[Dict]:
        """Validate required attributes, grouping duplicates with per-occurrence line numbers."""
        # Use lxml for source-line tracking; fall back to ElementTree if unavailable
        try:
            from lxml import etree as letree
            raw = xml_file.read_text(encoding='utf-8', errors='replace')
            raw = re.sub(r'<!DOCTYPE\s[^[>]*(?:\[[^\]]*\])?\s*>', '', raw, flags=re.DOTALL)
            def _sub2(m):
                n = m.group(1)
                return m.group(0) if n in self._XML_BUILTINS else self._HTML_ENTITIES.get(n, f'\u25a0{n}\u25a0')
            raw = re.sub(r'&([a-zA-Z][a-zA-Z0-9]*);', _sub2, raw)
            parser = letree.XMLParser(load_dtd=False, no_network=True, resolve_entities=False)
            root = letree.fromstring(raw.encode('utf-8'), parser)
            def _line(el): return getattr(el, 'sourceline', 0) or 0
        except Exception:
            tree = self._safe_parse(xml_file)
            root = tree.getroot()
            def _line(el): return 0

        # lines_by_rule: rule → list of line numbers
        lines_by_rule: Dict[str, list] = {}

        # Check <para date.updated="1"> must also have date.trace="1"
        for el in root.findall('.//para[@date.updated="1"]'):
            if el.get('date.trace') != '1':
                lines_by_rule.setdefault('ATTR-001', []).append(_line(el))

        # Check <update> must have date.trace="1"
        for el in root.findall('.//update'):
            if el.get('date.trace') != '1':
                lines_by_rule.setdefault('ATTR-002', []).append(_line(el))
            if 'date.updated' in el.attrib:
                lines_by_rule.setdefault('ATTR-003', []).append(_line(el))

        # <trace> has date.trace CDATA '1' as DTD default — no check needed for missing attribute.
        # Only flag if date.updated is wrongly placed on <trace>
        for el in root.findall('.//trace'):
            if 'date.updated' in el.attrib:
                lines_by_rule.setdefault('ATTR-005', []).append(_line(el))

        # Human-friendly templates: rule → (message, fix)
        _templates = {
            'ATTR-001': (
                lambda n: f"{n} paragraph(s) are marked as updated but are missing the required date.trace=\"1\" attribute",
                "Every <para> with date.updated=\"1\" must also have date.trace=\"1\". "
                "Add the missing attribute to each flagged paragraph."
            ),
            'ATTR-002': (
                lambda n: f"{n} <update> element(s) are missing the required date.trace=\"1\" attribute",
                "Every <update> element must have date.trace=\"1\". "
                "Add date.trace=\"1\" to each <update> that is missing it."
            ),
            'ATTR-003': (
                lambda n: f"{n} <update> element(s) incorrectly have a date.updated attribute",
                "The date.updated attribute belongs on <para>, not on <update>. "
                "Remove date.updated from every <update> element."
            ),
            'ATTR-005': (
                lambda n: f"{n} <trace> element(s) incorrectly have a date.updated attribute",
                "The date.updated attribute is not allowed on <trace> elements. "
                "Remove date.updated from every <trace> element."
            ),
        }

        issues = []
        for rule, lines in lines_by_rule.items():
            msg_fn, fix = _templates[rule]
            count = len(lines)
            known_lines = sorted(set(ln for ln in lines if ln > 0))
            if known_lines:
                preview = ', '.join(str(l) for l in known_lines[:20])
                if len(known_lines) > 20:
                    preview += f' … ({len(known_lines)} total)'
                detail = f"Found at line(s): {preview}"
            else:
                detail = f"{count} occurrence(s) — line numbers not available"
            issues.append({
                'rule':     rule,
                'message':  msg_fn(count),
                'detail':   detail,
                'fix':      fix,
                'severity': 'error',
                'count':    count,
                'line':     known_lines[0] if known_lines else 0,
            })

        return issues
    
    # ─────────────────────────────────────────────────────────────────────
    # A) Tag Whitelist — built from the DTD files in schematron_dir
    # ─────────────────────────────────────────────────────────────────────

    def _build_tag_whitelist(self, dtd_paths=None) -> set:
        """Extract every element name declared in the given DTD file(s). Defaults to
        every DTD file in schematron_dir when dtd_paths is None — kept as a fallback
        for a caller that can't determine which single DTD governs a document, but
        callers that CAN (via _pick_dtd) should always pass that one specific path:
        scanning every file indiscriminately mixes unrelated DTD families together
        (e.g. pix.dtd's bare <cc>/<st> elements would wrongly whitelist those same
        names for a pax.dtd document, which doesn't declare them at all)."""
        whitelist: set = set()
        paths = dtd_paths if dtd_paths is not None else list(self.schematron_dir.iterdir())
        for f in paths:
            if f is None:
                continue
            if f.suffix.lower() == '.dtd' or 'dtd' in f.name.lower():
                try:
                    text = f.read_text(encoding='utf-8', errors='ignore')
                    for m in re.finditer(r'<!ELEMENT\s+(\S+)', text):
                        whitelist.add(m.group(1))
                except Exception:
                    pass
        return whitelist

    # ─────────────────────────────────────────────────────────────────────
    # Accepting the OTHER selectable DTD family's tags / attributes (PAX <-> PIX)
    # ─────────────────────────────────────────────────────────────────────
    # Existing files get new revisions tagged in PAX or PIX style as needed, so a tag or attribute
    # that the other family declares must not be flagged; only what NEITHER allows is an error.
    # (Enabled by `accept_other_families`, set by dtd_validation for family="both".)

    def _other_family_paths(self, primary_path: Path) -> List[Path]:
        from .region import get_region
        out = []
        for fam in get_region().selectable_families:
            p = self.schematron_dir / fam.file
            if p.exists() and p.name != primary_path.name:
                out.append(p)
        return out

    def _foreign_tags(self, primary_path: Path) -> set:
        """Elements declared by another selectable family but NOT by the document's own DTD."""
        foreign: set = set()
        for p in self._other_family_paths(primary_path):
            foreign |= self._build_tag_whitelist([p])
        return foreign - self._build_tag_whitelist([primary_path])

    def _attr_names_by_tag(self, dtd_path: Path, dtd_obj=None) -> Dict[str, set]:
        key = ('attrs', str(dtd_path))
        cached = _DTD_CACHE.get(key)
        if cached is None:
            from lxml import etree as _le
            obj = dtd_obj if dtd_obj is not None else _le.DTD(str(dtd_path))
            cached = {el.name: {a.name for a in el.iterattributes()} for el in obj.iterelements()}
            _DTD_CACHE[key] = cached
        return cached

    def validate_tag_whitelist(self, xml_file: Path) -> List[Dict]:
        """Flag any element in the XML that is not declared in the DTD governing
        THIS document (auto-detected from its DOCTYPE via _pick_dtd — the same
        mechanism validate_dtd uses), not "declared in SOME DTD file anywhere in
        the folder." Falls back to scanning every DTD file only if the document's
        DOCTYPE can't be matched to one (the previous, more permissive behavior),
        rather than reporting nothing."""
        content = xml_file.read_text(encoding='utf-8', errors='ignore')
        dtd_path, _ = self._pick_dtd(content)
        whitelist = self._build_tag_whitelist([dtd_path] if dtd_path else None)
        if not whitelist:
            return []

        tree = self._safe_parse(xml_file)
        root = tree.getroot()

        from .region import get_region
        allowed = set(get_region().allowed_tags)      # region.yaml validation.allowed_tags
        if getattr(self, 'accept_other_families', False) and dtd_path:
            allowed |= self._foreign_tags(dtd_path)   # tags the other DTD family declares

        unknown: Dict[str, int] = {}
        for elem in root.iter():
            tag = elem.tag
            if '{' in tag:          # skip namespace-prefixed tags
                continue
            if tag in allowed:
                continue
            if tag not in whitelist:
                unknown[tag] = unknown.get(tag, 0) + 1

        issues = []
        for tag, count in sorted(unknown.items()):
            issues.append({
                'rule': 'ELEM-001',
                'message': (
                    f'Unknown element <{tag}> ({count} occurrence(s)) '
                    f'— not declared in any PAX DTD file'
                ),
                'severity': 'error',
                'count': count,
            })
        return issues

    # ─────────────────────────────────────────────────────────────────────
    # B) Reference Rules — loaded from sharepoint-rules.md
    # ─────────────────────────────────────────────────────────────────────

    def load_reference_rules(self) -> List[Dict]:
        """Parse sharepoint-rules.md for machine-readable validation rules.

        Each rule block must start with a line like:
            ## RULE: PRES-001
        followed by key: value lines (severity, check, element, …, message).
        Only rules that have both a 'check' and a 'message' key are used.
        """
        rules_file = self.schematron_dir / 'sharepoint-rules.md'
        if not rules_file.exists():
            return []

        text = rules_file.read_text(encoding='utf-8', errors='ignore')
        # Strip HTML comment blocks first — rules inside <!-- ... --> are EXAMPLE
        # templates ("uncomment and customise"), NOT active rules. Without this,
        # the example rules fire as phantom validation errors.
        text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)
        block_pattern = re.compile(r'##\s+RULE:\s+(\S+)\n((?:(?!##\s+RULE:).)*)', re.DOTALL)
        rules = []
        for bm in block_pattern.finditer(text):
            rule_id = bm.group(1)
            body    = bm.group(2)
            rule: Dict = {'id': rule_id}
            for line in body.splitlines():
                line = line.strip()
                if ':' in line and not line.startswith(('-', '#', '*', '>')):
                    key, _, val = line.partition(':')
                    rule[key.strip().lower()] = val.strip()
            if 'check' in rule and 'message' in rule:
                rules.append(rule)
        return rules

    def validate_reference_rules(self, xml_file: Path) -> List[Dict]:
        """Apply rules loaded from sharepoint-rules.md against the XML file.

        Supported check types (set via 'check: <type>' in the rule block):
          element-exists   — element must appear at least once
          element-has-attr — element must carry a given attribute
          attr-value       — attribute must be one of a comma-separated list
          text-pattern     — element text must match a regex
          text-forbidden   — element text must NOT match a regex
          child-required   — element must contain a specific child element
        """
        rules = self.load_reference_rules()
        if not rules:
            return []

        tree = self._safe_parse(xml_file)
        root = tree.getroot()
        issues: List[Dict] = []

        for rule in rules:
            rule_id  = rule.get('id', 'REF-000')
            check    = rule.get('check', '')
            element  = rule.get('element', '')
            message  = rule.get('message', f'Rule {rule_id} failed')
            severity = rule.get('severity', 'warning')

            try:
                if check == 'element-exists':
                    if element and root.find(f'.//{element}') is None:
                        issues.append({'rule': rule_id, 'message': message, 'severity': severity})

                elif check == 'element-has-attr':
                    attr = rule.get('attr', '')
                    for elem in root.findall(f'.//{element}'):
                        if attr and elem.get(attr) is None:
                            issues.append({'rule': rule_id, 'message': message, 'severity': severity})
                            break   # one report per rule

                elif check == 'attr-value':
                    attr   = rule.get('attr', '')
                    values = [v.strip() for v in rule.get('values', '').split(',') if v.strip()]
                    for elem in root.findall(f'.//{element}'):
                        val = elem.get(attr)
                        if val is not None and values and val not in values:
                            issues.append({
                                'rule': rule_id,
                                'message': f"{message} (found: '{val}')",
                                'severity': severity,
                            })
                            break

                elif check == 'text-pattern':
                    pattern = rule.get('pattern', '')
                    if pattern:
                        rx = re.compile(pattern)
                        for elem in root.findall(f'.//{element}'):
                            if not rx.search(''.join(elem.itertext())):
                                issues.append({'rule': rule_id, 'message': message, 'severity': severity})
                                break

                elif check == 'text-forbidden':
                    pattern = rule.get('pattern', '')
                    if pattern:
                        rx = re.compile(pattern)
                        for elem in root.findall(f'.//{element}'):
                            if rx.search(''.join(elem.itertext())):
                                issues.append({'rule': rule_id, 'message': message, 'severity': severity})
                                break

                elif check == 'child-required':
                    child = rule.get('child', '')
                    if child:
                        for elem in root.findall(f'.//{element}'):
                            if elem.find(child) is None:
                                issues.append({'rule': rule_id, 'message': message, 'severity': severity})
                                break

            except Exception:
                pass    # malformed rule — skip silently

        return issues

    # ── PAX-specific knowledge used by _translate_dtd_error ───────────────
    _TRACK_CHANGE_TAGS = {'trace', 'trace.deleted', 'trace.insert', 'trace.delete'}
    _TRACK_CHANGE_ATTRS = {'date.trace', 'rev.id', 'trace.id', 'trace.type'}

    # Wrong tag → (correct tag, plain-English explanation)
    # NOTE: <st> is intentionally excluded — it is a valid PAX shortcut for
    # statute citations that gets expanded in downstream workflows.
    _TAG_SUGGESTIONS = {
        'bold':   ('b',          '<b> is the correct PAX tag for bold text'),
        'italic': ('ital',       '<ital> is the correct PAX tag for italic text'),
        'i':      ('ital',       '<ital> is the correct PAX tag for italic text'),
        'em':     ('ital',       '<ital> is the correct PAX tag for emphasis/italic'),
        'strong': ('b',          '<b> is the correct PAX tag for bold/strong text'),
        'u':      ('unders',     '<unders> is the correct PAX tag for underline'),
        'sup':    ('superscript','<superscript> is the correct PAX tag for superscript'),
        'sub':    ('subscript',  '<subscript> is the correct PAX tag for subscript'),
        'p':      ('para.text',  '<para.text> is the correct PAX tag for paragraph text'),
        'span':   (None,         'PAX does not use <span> — use the appropriate inline tag'),
        'div':    (None,         'PAX does not use <div> — use the appropriate block-level tag'),
    }

    def _translate_dtd_error(self, msg: str) -> Tuple[str, str]:
        """Convert a raw lxml DTD error message to a specific, actionable plain-English message."""

        # ── Pattern 1: tag not allowed inside parent ───────────────────────
        m = re.match(r"Element (\S+) is not declared in (\S+) list of possible children", msg)
        if m:
            child, parent = m.group(1), m.group(2)
            title = f"Tag <{child}> is not allowed inside <{parent}>"
            if child in self._TRACK_CHANGE_TAGS:
                fix = (f"<{child}> is a track-changes marker and cannot appear inside <{parent}>. "
                       f"Remove it — track-changes markup must not be left inside content elements.")
            elif child in self._TAG_SUGGESTIONS:
                alt, desc = self._TAG_SUGGESTIONS[child]
                if alt:
                    fix = f"Did you mean <{alt}>? {desc}. Replace <{child}> with <{alt}> inside <{parent}>."
                else:
                    fix = f"{desc}. Remove <{child}> and use the correct PAX tag inside <{parent}>."
            else:
                fix = (f"<{child}> cannot appear directly inside <{parent}>. "
                       f"Either remove <{child}> or move it to the correct parent element.")
            return title, fix

        # ── Pattern 2: completely unknown tag ─────────────────────────────
        m = re.match(r"No declaration for element (\S+)", msg)
        if m:
            tag = m.group(1)
            if tag in self._TRACK_CHANGE_TAGS:
                return (
                    f"Tag <{tag}> is not defined in the PAX DTD",
                    f"<{tag}> is a track-changes element that was left in the output. Remove it entirely."
                )
            if tag in self._TAG_SUGGESTIONS:
                alt, desc = self._TAG_SUGGESTIONS[tag]
                if alt:
                    return (
                        f"Unknown tag <{tag}> — did you mean <{alt}>?",
                        f"{desc}. Replace every <{tag}> with <{alt}>."
                    )
            return (
                f"Unknown tag <{tag}> — not in the PAX DTD",
                f"<{tag}> is not a valid PAX tag. Check for a typo or look up the correct PAX element name."
            )

        # ── Pattern 3: content model mismatch — identify the bad element ──
        m = re.search(
            r"Element (\S+) content does not follow the DTD, expecting (.+?), got \((.+?)\)\s*$",
            msg, re.DOTALL
        )
        if m:
            parent_tag  = m.group(1)
            expected_raw = m.group(2)
            got_raw      = m.group(3).strip()

            # Pull out all element names from the allowed model
            allowed = set(re.findall(r'[a-zA-Z_][\w.\-]*', expected_raw))
            got_elements = got_raw.split()

            # Find elements present in "got" that are NOT in the allowed set
            wrong = list(dict.fromkeys(           # deduplicate, preserve order
                e for e in got_elements
                if re.match(r'[a-zA-Z]', e) and e not in allowed
            ))

            if wrong:
                wrong_str = ', '.join(f'<{e}>' for e in wrong)
                title = f"Unexpected element(s) {wrong_str} inside <{parent_tag}>"
                fixes = []
                for e in wrong:
                    if e in self._TRACK_CHANGE_TAGS:
                        fixes.append(f"<{e}> is a track-changes marker — remove it from inside <{parent_tag}>")
                    elif e in self._TAG_SUGGESTIONS:
                        alt, desc = self._TAG_SUGGESTIONS[e]
                        fixes.append(f"<{e}>: did you mean <{alt}>? {desc}")
                    else:
                        fixes.append(f"<{e}> is not a valid child of <{parent_tag}> — remove or relocate it")
                fix = ". ".join(fixes) + "."
            else:
                # Elements are valid but in the wrong order, or a required one is missing
                got_short = got_raw[:120] + ('…' if len(got_raw) > 120 else '')
                title = f"Child elements inside <{parent_tag}> are in the wrong order or incomplete"
                fix = (f"Found: {got_short}. "
                       f"Check that all required child elements are present and appear in the order "
                       f"the DTD requires for <{parent_tag}>.")
            return title, fix

        # ── Pattern 4a: undeclared attribute (lxml format A) ──────────────
        m = re.match(r"Attribute (\S+) of (\S+) is not declared", msg)
        if not m:
            m = re.match(r"No declaration for attribute (\S+) of element (\S+)", msg)
        if m:
            attr, tag = m.group(1), m.group(2)
            if attr in self._TRACK_CHANGE_ATTRS:
                return (
                    f"Attribute '{attr}' is not valid on <{tag}>",
                    f"'{attr}' is a track-changes attribute that should not appear in the output. "
                    f"Remove this attribute from every <{tag}> element."
                )
            return (
                f"Attribute '{attr}' is not valid on <{tag}>",
                f"<{tag}> does not support the '{attr}' attribute in the PAX DTD. "
                f"Remove it or check for a typo in the attribute name."
            )

        # ── Pattern 5: invalid attribute value ────────────────────────────
        m = re.match(r'Value "?(.+?)"? for attribute (\S+) of (\S+) is not among the enumerated set', msg)
        if m:
            val, attr, tag = m.group(1), m.group(2), m.group(3)
            return (
                f"Invalid value '{val}' for attribute '{attr}' on <{tag}>",
                f"'{val}' is not one of the allowed values for the '{attr}' attribute on <{tag}>. "
                f"Check the PAX tag reference for the correct allowed values."
            )

        # ── Pattern 6: element is empty but must have content ─────────────
        m = re.match(r"Element (\S+) content is empty", msg)
        if m:
            tag = m.group(1)
            return (
                f"<{tag}> is empty but must contain something",
                f"The <{tag}> element requires child elements or text. "
                f"Either add the required content or remove the empty <{tag}> tags entirely."
            )

        # ── Pattern 7: required attribute missing ─────────────────────────
        m = re.match(r"Specification mandates value for attribute (\S+)", msg)
        if m:
            attr = m.group(1)
            return (
                f"Required attribute '{attr}' is missing",
                f"Every element that uses '{attr}' must have a value for it. Add '{attr}=\"...\"'."
            )

        # Fallback
        return msg, "Check the DTD for the correct structure."

    def _pick_dtd(self, content: str) -> Tuple[Path, str]:
        """
        Choose the DTD from the ACTIVE REGION's families (regions/<id>/region.yaml): the
        document's DOCTYPE path is matched against each family's doctype_keywords in manifest
        order. Returns (dtd_path, label); falls back to the region's default family.
        """
        from .region import get_region
        region = get_region()

        # An explicit family override ("pax" / "pix" / any family id) beats the DOCTYPE —
        # used by the standalone DTD validator so a file can be checked against a DTD it
        # doesn't (yet) declare, or has no DOCTYPE at all.
        forced = getattr(self, 'forced_family', None)
        if forced:
            fam = region.family(forced)
            if fam and (self.schematron_dir / fam.file).exists():
                return self.schematron_dir / fam.file, fam.label

        doctype_m = re.search(r'<!DOCTYPE\s+\S+\s+SYSTEM\s+"([^"]+)"', content)
        fam = region.detect_family(doctype_m.group(1)) if doctype_m else None
        if fam and (self.schematron_dir / fam.file).exists():
            return self.schematron_dir / fam.file, fam.label

        default = region.default_dtd_family()
        if (self.schematron_dir / default.file).exists():
            return self.schematron_dir / default.file, default.label
        return None, None

    def validate_dtd(self, xml_file: Path) -> Tuple[List[Dict], str]:
        """Validate XML against the correct PAX DTD (auto-detected). Returns (issues, dtd_label)."""
        try:
            from lxml import etree as letree
        except ImportError:
            print("  WARN  lxml not installed — skipping DTD validation (pip install lxml)")
            return [], ''

        # Parse XML with entity substitution (same approach as _safe_parse)
        content = xml_file.read_text(encoding='utf-8', errors='replace')

        # Auto-detect which DTD to use from the DOCTYPE declaration
        dtd_path, dtd_label = self._pick_dtd(content)
        if not dtd_path:
            print("  WARN  No PAX DTD found — skipping DTD validation")
            return [], ''
        print(f"  Using DTD: {dtd_label} ({dtd_path.name})")

        # Load DTD via file path so lxml can resolve external entity references
        # (e.g. PAX DTD Entities.dtd) relative to the DTD file location.
        # Cache the parsed DTD (the ~550 KB file is expensive to re-parse every call).
        try:
            global _DTD_CACHE
            key = str(dtd_path)
            dtd = _DTD_CACHE.get(key)
            if dtd is None:
                dtd = letree.DTD(key)
                _DTD_CACHE[key] = dtd
        except Exception as e:
            print(f"  WARN  Could not load DTD: {e}")
            return [], dtd_label

        # Strip DOCTYPE declaration so lxml does NOT try to fetch the
        # external DTD URL (e.g. http://phoenix.int.thomsonreuters.com/dtd/pax.dtd).
        # Error line numbers would otherwise be offset into the external file.
        content = re.sub(
            r'<!DOCTYPE\s[^[>]*(?:\[[^\]]*\])?\s*>',
            '',
            content,
            flags=re.DOTALL,
        )

        def _sub(m):
            name = m.group(1)
            if name in self._XML_BUILTINS:
                return m.group(0)
            return self._HTML_ENTITIES.get(name, f'\u25a0{name}\u25a0')

        content = re.sub(r'&([a-zA-Z][a-zA-Z0-9]*);', _sub, content)

        # Use a parser that blocks all network access and external entity loading
        parser = letree.XMLParser(load_dtd=False, no_network=True, resolve_entities=False)
        try:
            tree = letree.ElementTree(letree.fromstring(content.encode('utf-8'), parser))
        except letree.XMLSyntaxError:
            # The document has a well-formedness problem — validate_xml_wellformed()
            # (Step 1) already reports that separately. Returning [] here unconditionally
            # would silently claim "0 DTD issues" even when the rest of the document has
            # genuine DTD content-model violations (e.g. an undeclared element) that have
            # nothing to do with the syntax slip — confirmed on a real document that had
            # both a mismatched-tag typo AND 15 invalid <cc>/<st> tags, which reported
            # zero DTD issues before this fix purely because the strict parse bailed out
            # first. Retry with a RECOVERING parser so DTD validation can still run
            # against lxml's best-effort reconstruction of the rest of the document.
            recovering_parser = letree.XMLParser(
                load_dtd=False, no_network=True, resolve_entities=False, recover=True
            )
            try:
                tree = letree.ElementTree(letree.fromstring(content.encode('utf-8'), recovering_parser))
            except Exception:
                return [], dtd_label  # truly unparseable even with recovery

        # Tags the region lists under validation.allowed_tags (new structural tags being rolled
        # out) are UNWRAPPED first -- their text/children stay -- so they raise no "no declaration"
        # error and don't cascade into "content does not follow the DTD" errors on their parents.
        from .region import get_region
        allowed = set(get_region().allowed_tags)
        if getattr(self, 'accept_other_families', False):
            # Accept the other DTD family's tags (unwrapped) and attributes (dropped from the copy
            # being validated) so only what NEITHER family allows is reported.
            allowed |= self._foreign_tags(dtd_path)
            own_attrs = self._attr_names_by_tag(dtd_path, dtd)
            other_attrs: Dict[str, set] = {}
            for p in self._other_family_paths(dtd_path):
                for tag, names in self._attr_names_by_tag(p).items():
                    other_attrs.setdefault(tag, set()).update(names)
            for el in tree.iter():
                if not isinstance(el.tag, str) or el.tag in allowed:
                    continue
                extra = other_attrs.get(el.tag, set()) - own_attrs.get(el.tag, set())
                for a in list(el.attrib):
                    if a in extra:
                        del el.attrib[a]
        if allowed:
            letree.strip_tags(tree, *sorted(allowed))

        dtd.validate(tree)

        # Actual line count of the original file — used to discard bogus line
        # numbers that lxml sometimes reports (e.g. lines from an external DTD).
        orig_lines = xml_file.read_text(encoding='utf-8', errors='replace').count('\n') + 1

        # Group identical error messages, collecting all line numbers
        groups: Dict[str, Dict] = {}
        for err in dtd.error_log:
            severity = 'error' if err.level_name == 'ERROR' else 'warning'
            title, fix = self._translate_dtd_error(err.message)
            key = title  # group by plain-English title
            # Discard line numbers that fall outside the actual file
            line = err.line if 0 < err.line <= orig_lines else 0
            if key not in groups:
                groups[key] = {
                    'rule': 'DTD',
                    'severity': severity,
                    'title': title,
                    'fix': fix,
                    'lines': [],
                    'raw': err.message,
                    'message': err.message,
                }
            groups[key]['lines'].append(line)

        issues = []
        for g in groups.values():
            all_lines  = sorted(set(g['lines']))
            known      = [l for l in all_lines if l > 0]
            unknown_ct = all_lines.count(0)

            if known:
                lines_str = ', '.join(str(l) for l in known[:10])
                if len(known) > 10:
                    lines_str += f' … ({len(known)} total)'
                if unknown_ct:
                    lines_str += f' (+{unknown_ct} occurrence(s) with unknown line)'
            else:
                lines_str = f'{unknown_ct} occurrence(s) — line number could not be determined'

            g['location'] = f"Line(s): {lines_str}"
            g['count']    = len(all_lines)
            g['line']     = known[0] if known else 0   # first navigable line
            issues.append(g)

        # Sort: errors first, then by occurrence count descending
        issues.sort(key=lambda x: (0 if x['severity'] == 'error' else 1, -x['count']))
        return issues, dtd_label

    def validate_file(self, xml_file: Path) -> Dict:
        """Main validation orchestrator"""
        print(f"\n{'='*70}")
        print(f"Validating: {xml_file.name}")
        print(f"{'='*70}\n")
        
        results = {
            'file': str(xml_file),
            'timestamp': datetime.now().isoformat(),
            'wellformed': False,
            'validation_passed': True,
            'error_count': 0,
            'warning_count': 0,
            'info_count': 0,
            'details': {}
        }
        
        # Step 1: Check well-formedness
        print("Step 1: Checking XML well-formedness...")
        wellformed, message, wf_detail = self.validate_xml_wellformed(xml_file)
        results['wellformed'] = wellformed

        if not wellformed:
            results['validation_passed'] = False
            results['error_count'] += 1
            results['details']['wellformed_error'] = message
            results['details']['wellformed_detail'] = wf_detail
            print(f"  FAIL {message}")
            if wf_detail.get('unclosed_tag'):
                print(f"     Likely unclosed tag: <{wf_detail['unclosed_tag']}>")
            if wf_detail.get('line'):
                print(f"     Location: line {wf_detail['line']}, column {wf_detail['column']}")
            return results
        
        print(f"  OK {message}")

        # Step 2: DTD structural validation
        print("\nStep 2: Validating against PAX DTD...")
        dtd_issues, dtd_label = self.validate_dtd(xml_file)
        results['details']['dtd_issues'] = dtd_issues
        results['details']['dtd_label']  = dtd_label   # e.g. "PAX", "Front Matter", etc.
        dtd_errors   = [i for i in dtd_issues if i['severity'] == 'error']
        dtd_warnings = [i for i in dtd_issues if i['severity'] == 'warning']
        results['error_count']   += len(dtd_errors)
        results['warning_count'] += len(dtd_warnings)
        if dtd_errors:
            results['validation_passed'] = False
        if dtd_issues:
            for issue in dtd_issues:
                icon = 'FAIL' if issue['severity'] == 'error' else 'WARN '
                print(f"  {icon} [DTD] {issue['title']} ({issue['count']} occurrence(s))")
        else:
            print("  OK DTD validation passed")

        # Step 3: Citation validation
        print("\nStep 3: Validating citations...")
        citation_results = self.validate_citations(xml_file)
        results['details']['citations'] = citation_results
        
        print(f"  📊 Found:")
        print(f"     - Case citations (<cc>): {citation_results['case_citations']}")
        print(f"     - Statute citations (<st>): {citation_results['statute_citations']}")
        print(f"     - Periodical citations (<pl>): {citation_results['periodical_citations']}")
        
        for issue in citation_results['citation_issues']:
            if issue['severity'] == 'error':
                print(f"  FAIL [{issue['rule']}] {issue['message']}")
                results['error_count'] += 1
                results['validation_passed'] = False
            else:
                print(f"  WARN  [{issue['rule']}] {issue['message']}")
                results['warning_count'] += 1
        
        # Step 4: Heading detection (#1 error)
        print("\nStep 4: Detecting heading misclassification (#1 ERROR)...")
        heading_issues = self.validate_headings(xml_file)
        results['details']['heading_issues'] = heading_issues
        results['error_count'] += len(heading_issues)
        
        if heading_issues:
            results['validation_passed'] = False
            for issue in heading_issues:
                print(f"  FAIL [{issue['rule']}] {issue['message']}")
        else:
            print("  OK No heading misclassification detected")
        
        # Step 5: Form validation
        print("\nStep 5: Validating forms...")
        form_results = self.validate_forms(xml_file)
        results['details']['forms'] = form_results
        
        print(f"  📊 Found {form_results['forms_found']} forms with {form_results['inline_instr_count']} inline instructions")
        
        for issue in form_results['form_issues']:
            if issue['severity'] == 'error':
                print(f"  FAIL [{issue['rule']}] {issue['message']}")
                results['error_count'] += 1
                results['validation_passed'] = False
            else:
                print(f"  WARN  [{issue['rule']}] {issue['message']}")
                results['warning_count'] += 1
        
        if not form_results['form_issues']:
            print("  OK No form issues detected")
        
        # Step 6: Footnote validation
        print("\nStep 6: Validating footnotes...")
        footnote_results = self.validate_footnotes(xml_file)
        results['details']['footnotes'] = footnote_results
        
        print(f"  📊 Found {footnote_results['footnotes_found']} footnotes")
        
        for issue in footnote_results['footnote_issues']:
            if issue['severity'] == 'error':
                print(f"  FAIL [{issue['rule']}] {issue['message']}")
                results['error_count'] += 1
                results['validation_passed'] = False
            else:
                print(f"  WARN  [{issue['rule']}] {issue['message']}")
                results['warning_count'] += 1
        
        if not footnote_results['footnote_issues']:
            print("  OK No footnote issues detected")
        
        # Step 7: Attribute validation
        print("\nStep 7: Validating attributes...")
        attribute_issues = self.validate_attributes(xml_file)
        results['details']['attribute_issues'] = attribute_issues
        
        for issue in attribute_issues:
            if issue['severity'] == 'error':
                print(f"  FAIL [{issue['rule']}] {issue['message']}")
                results['error_count'] += 1
                results['validation_passed'] = False
            else:
                print(f"  WARN  [{issue['rule']}] {issue['message']}")
                results['warning_count'] += 1
        
        if not attribute_issues:
            print("  OK No attribute issues detected")

        # Step 8: Tag whitelist (DTD-derived)
        print("\nStep 8: Checking for unknown/invalid tags (DTD whitelist)...")
        whitelist_issues = self.validate_tag_whitelist(xml_file)
        results['details']['whitelist_issues'] = whitelist_issues
        wl_errors   = [i for i in whitelist_issues if i['severity'] == 'error']
        wl_warnings = [i for i in whitelist_issues if i['severity'] == 'warning']
        results['error_count']   += len(wl_errors)
        results['warning_count'] += len(wl_warnings)
        if wl_errors:
            results['validation_passed'] = False
        if whitelist_issues:
            for issue in whitelist_issues:
                icon = 'FAIL' if issue['severity'] == 'error' else 'WARN '
                print(f"  {icon} [{issue['rule']}] {issue['message']}")
        else:
            print("  OK All elements are valid PAX tags")

        # Step 9: Reference rules from sharepoint-rules.md (if present)
        ref_rules_file = self.schematron_dir / 'sharepoint-rules.md'
        if ref_rules_file.exists():
            print("\nStep 9: Applying reference rules (sharepoint-rules.md)...")
            ref_issues = self.validate_reference_rules(xml_file)
            results['details']['reference_issues'] = ref_issues
            ref_errors   = [i for i in ref_issues if i['severity'] == 'error']
            ref_warnings = [i for i in ref_issues if i['severity'] == 'warning']
            results['error_count']   += len(ref_errors)
            results['warning_count'] += len(ref_warnings)
            if ref_errors:
                results['validation_passed'] = False
            if ref_issues:
                for issue in ref_issues:
                    icon = 'FAIL' if issue['severity'] == 'error' else 'WARN '
                    print(f"  {icon} [{issue['rule']}] {issue['message']}")
            else:
                print("  OK All reference rules passed")
        else:
            results['details']['reference_issues'] = []
            print("\nStep 9: sharepoint-rules.md not found — paste your SharePoint rules there to enable this step")

        # Summary
        print(f"\n{'='*70}")
        if results['validation_passed']:
            print("OK VALIDATION PASSED")
        else:
            print("FAIL VALIDATION FAILED")
        
        print(f"\nSummary:")
        print(f"  Errors: {results['error_count']}")
        print(f"  Warnings: {results['warning_count']}")
        print(f"{'='*70}\n")
        
        return results
    
    # Plain-English descriptions for each rule code
    RULE_EXPLANATIONS = {
        'CC-003': {
            'title': 'Possible untagged case citations',
            'what': 'Text that looks like a court case name (e.g. "Smith v. Jones") was found outside a <cc> tag. '
                    'This means the case name is in plain text and has not been marked up as a case citation. '
                    'This is a warning — review each location to decide if it needs a <cc> tag.',
            'fix': 'Wrap the case name in <cc finder="manual" type="case">Smith v. Jones</cc>. '
                   'If the phrase is not actually a case name (e.g. a volume abbreviation), no action needed.'
        },
        'ST-002': {
            'title': 'Wrong tag used for a case citation',
            'what': 'A statute/law citation tag is being used around text that looks like a court case (contains " v. "). Case citations need a different tag.',
            'fix': 'Change the <st> tag to a <cc> tag around the case name shown above.'
        },
        'PL-002': {
            'title': 'Wrong tag used for a case citation',
            'what': 'A periodical/journal citation tag is being used around text that looks like a court case (contains " v. ").',
            'fix': 'Change the <pl> tag to a <cc> tag around the case name shown above.'
        },
        'HEAD-001': {
            'title': 'Heading formatted as bold text instead of a heading tag',
            'what': 'Short ALL-CAPS bold text found inside a paragraph — this is almost certainly a section heading that was not converted correctly.',
            'fix': 'Replace the bold paragraph containing this text with a proper heading element (<head>).'
        },
        'INLINE-001': {
            'title': 'Square bracket instructions not tagged correctly',
            'what': 'Text inside square brackets [ ] in a form should be marked as an "inline instruction" (fill-in placeholder for the user). These brackets were found without that tag.',
            'fix': 'Wrap the bracketed text in an <inline.instr> tag.'
        },
        'FN-001': {
            'title': 'Footnote is missing a required part',
            'what': 'A footnote must have both a reference marker and a body. One of these is missing.',
            'fix': 'Check the footnote structure and add the missing footnote.reference or footnote.body element.'
        },
        'FN-004': {
            'title': 'Footnote body has no paragraph',
            'what': 'The body of a footnote must contain at least one paragraph element.',
            'fix': 'Wrap the footnote body content in a <para> element.'
        },
        'ATTR-001': {
            'title': 'Paragraph missing a required tracking attribute',
            'what': 'A paragraph marked as "date updated" must also have a "date trace" attribute.',
            'fix': 'Add date.trace="1" to the paragraph that has date.updated="1".'
        },
        'ATTR-002': {
            'title': '<update> element missing required attribute',
            'what': 'Every <update> element must include date.trace="1".',
            'fix': 'Add date.trace="1" to the <update> element.'
        },
        'ATTR-003': {
            'title': '<update> element has a forbidden attribute',
            'what': '<update> elements must not have a date.updated attribute — that attribute belongs on <para> only.',
            'fix': 'Remove the date.updated attribute from the <update> element.'
        },
        'ATTR-004': {
            'title': '<trace> element missing required attribute',
            'what': 'Every <trace> element must include date.trace="1".',
            'fix': 'Add date.trace="1" to the <trace> element.'
        },
        'ATTR-005': {
            'title': '<trace> element has a forbidden attribute',
            'what': '<trace> elements must not have a date.updated attribute.',
            'fix': 'Remove the date.updated attribute from the <trace> element.'
        },
    }

    def _rule_html(self, rule: str, raw_message: str, severity: str) -> str:
        """Render a single issue card with plain-English explanation."""
        exp = self.RULE_EXPLANATIONS.get(rule, {})
        title = exp.get('title', raw_message)
        what = exp.get('what', '')
        fix = exp.get('fix', '')
        severity_label = {'error': '🔴 Error', 'warning': '🟡 Warning', 'info': 'ℹ️ Info'}.get(severity, severity.title())

        # Extract the quoted content from the raw message (text inside quotes)
        import re
        found_text = ''
        quoted = re.findall(r'"([^"]+)"', raw_message)
        if quoted:
            found_text = f'<div class="found-text">Found: <em>"{quoted[-1]}"</em></div>'

        return f"""
        <div class="issue {severity}">
            <div class="issue-header">
                <span class="severity-label">{severity_label}</span>
                <span class="rule-id">{rule}</span>
            </div>
            <div class="issue-title">{title}</div>
            {found_text}
            {'<div class="issue-what"><strong>What this means:</strong> ' + what + '</div>' if what else ''}
            {'<div class="issue-fix"><strong>How to fix it:</strong> ' + fix + '</div>' if fix else ''}
        </div>
"""

    def generate_html_report(self, results: Dict, output_file: Path):
        """Generate HTML validation report"""
        # Collect all issues to build the "What's wrong" summary at the top
        wellformed_error = results['details'].get('wellformed_error', '')
        all_issues = []
        if wellformed_error:
            all_issues.append(('XML structure is broken — file cannot be read', 'error'))

        citation_issues = results['details'].get('citations', {}).get('citation_issues', [])
        for i in citation_issues:
            exp = self.RULE_EXPLANATIONS.get(i['rule'], {})
            all_issues.append((exp.get('title', i['message']), i['severity']))

        heading_issues = results['details'].get('heading_issues', [])
        for i in heading_issues:
            exp = self.RULE_EXPLANATIONS.get(i['rule'], {})
            all_issues.append((exp.get('title', i['message']), 'error'))

        form_issues = results['details'].get('forms', {}).get('form_issues', [])
        for i in form_issues:
            exp = self.RULE_EXPLANATIONS.get(i['rule'], {})
            all_issues.append((exp.get('title', i['message']), i['severity']))

        footnote_issues = results['details'].get('footnotes', {}).get('footnote_issues', [])
        for i in footnote_issues:
            exp = self.RULE_EXPLANATIONS.get(i['rule'], {})
            all_issues.append((exp.get('title', i['message']), 'error'))

        attribute_issues = results['details'].get('attribute_issues', [])
        for i in attribute_issues:
            exp = self.RULE_EXPLANATIONS.get(i['rule'], {})
            all_issues.append((exp.get('title', i['message']), i['severity']))

        # Quick-summary list for the top of the page
        issues_summary_html = ''
        if all_issues:
            issues_summary_html = '<ul class="issues-summary-list">'
            for title, sev in all_issues:
                icon = '🔴' if sev == 'error' else '🟡'
                issues_summary_html += f'<li>{icon} {title}</li>'
            issues_summary_html += '</ul>'

        file_name = Path(results['file']).name
        validated_time = datetime.fromisoformat(results['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
        pass_fail_class = 'pass' if results['validation_passed'] else 'fail'
        pass_fail_text = 'OK File passed — no issues found' if results['validation_passed'] else f'FAIL File failed — {results["error_count"]} error(s), {results["warning_count"]} warning(s)'

        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>XML Validation Report — {file_name}</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 0; background: #f0f2f5; color: #222; }}
        .container {{ max-width: 960px; margin: 30px auto; background: white; padding: 36px 40px; border-radius: 8px; box-shadow: 0 2px 12px rgba(0,0,0,0.10); }}
        h1 {{ color: #1a2a3a; font-size: 22px; border-bottom: 3px solid #2980b9; padding-bottom: 10px; margin-bottom: 6px; }}
        h2 {{ color: #2c3e50; font-size: 16px; margin-top: 32px; margin-bottom: 10px; border-left: 4px solid #2980b9; padding-left: 10px; }}
        .file-info {{ color: #555; font-size: 13px; margin-bottom: 18px; }}
        .file-info .fname {{ font-weight: bold; color: #1a2a3a; font-size: 15px; word-break: break-all; }}

        /* Pass / Fail banner */
        .status {{ font-size: 18px; font-weight: bold; padding: 16px 20px; border-radius: 6px; margin: 18px 0; }}
        .status.pass {{ background: #eafaf1; color: #1e8449; border: 2px solid #27ae60; }}
        .status.fail {{ background: #fdf2f2; color: #c0392b; border: 2px solid #e74c3c; }}

        /* Quick-summary box */
        .issues-summary {{ background: #fdf6ec; border: 1px solid #f0a500; border-radius: 6px; padding: 14px 18px; margin: 10px 0 24px 0; }}
        .issues-summary h3 {{ margin: 0 0 8px 0; color: #7d5a00; font-size: 14px; text-transform: uppercase; letter-spacing: 0.5px; }}
        .issues-summary-list {{ margin: 0; padding-left: 18px; font-size: 14px; line-height: 1.9; }}

        /* Counters */
        .counters {{ display: flex; gap: 16px; margin: 18px 0; }}
        .counter {{ flex: 1; padding: 14px 10px; border-radius: 6px; text-align: center; }}
        .counter .num {{ font-size: 32px; font-weight: bold; }}
        .counter .label {{ font-size: 12px; margin-top: 4px; color: #555; }}
        .counter.errors {{ background: #fdf2f2; border: 2px solid #e74c3c; }}
        .counter.errors .num {{ color: #c0392b; }}
        .counter.warnings {{ background: #fefce8; border: 2px solid #f0a500; }}
        .counter.warnings .num {{ color: #b7770d; }}
        .counter.wellformed {{ background: #eafaf1; border: 2px solid #27ae60; }}
        .counter.wellformed-fail {{ background: #fdf2f2; border: 2px solid #e74c3c; }}
        .counter.wellformed .num, .counter.wellformed-fail .num {{ font-size: 24px; }}

        /* Issue cards */
        .issue {{ margin: 10px 0; padding: 14px 16px; border-left: 5px solid #ccc; border-radius: 0 5px 5px 0; background: #f9f9f9; }}
        .issue.error {{ border-left-color: #e74c3c; background: #fdf2f2; }}
        .issue.warning {{ border-left-color: #f0a500; background: #fefce8; }}
        .issue.info {{ border-left-color: #2980b9; background: #eaf4fb; }}
        .issue-header {{ display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }}
        .severity-label {{ font-size: 12px; font-weight: bold; }}
        .rule-id {{ font-family: monospace; font-size: 11px; background: #e8e8e8; padding: 2px 7px; border-radius: 3px; color: #555; }}
        .issue-title {{ font-weight: bold; font-size: 15px; color: #1a2a3a; margin-bottom: 6px; }}
        .found-text {{ font-size: 13px; color: #444; background: #fff3cd; border-left: 3px solid #f0a500; padding: 5px 10px; margin: 6px 0; border-radius: 0 3px 3px 0; }}
        .issue-what {{ font-size: 13px; color: #444; margin: 5px 0 4px 0; }}
        .issue-fix {{ font-size: 13px; color: #1a5276; margin-top: 5px; }}
        .issue-fix strong, .issue-what strong {{ color: #333; }}

        /* Well-formedness error box */
        .wf-error {{ background: #fdf2f2; border: 2px solid #e74c3c; border-radius: 6px; padding: 16px 20px; margin: 14px 0; }}
        .wf-header {{ display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }}
        .wf-title {{ font-weight: bold; font-size: 16px; color: #c0392b; }}
        .loc-badge {{ background: #c0392b; color: white; font-size: 12px; font-weight: bold; padding: 3px 10px; border-radius: 12px; white-space: nowrap; }}
        .wf-detail {{ font-family: monospace; font-size: 13px; background: #fff; border: 1px solid #f5c6cb; padding: 8px 12px; border-radius: 4px; margin: 8px 0; word-break: break-all; }}
        .wf-what {{ font-size: 13px; color: #444; margin: 8px 0 4px 0; }}
        .wf-fix {{ font-size: 13px; color: #1a5276; margin-top: 6px; }}
        .unclosed-tag {{ font-size: 13px; background: #fff3cd; border-left: 4px solid #f0a500; padding: 7px 12px; margin: 8px 0; border-radius: 0 4px 4px 0; }}
        .ctx-block {{ margin: 10px 0; }}
        .ctx-title {{ font-size: 12px; color: #888; margin-bottom: 4px; }}
        .ctx-table {{ width: 100%; border-collapse: collapse; font-family: monospace; font-size: 12px; }}
        .ctx-table tr {{ border-bottom: 1px solid #f0d0d0; }}
        .ctx-table tr.ctx-error {{ background: #ffeaea; font-weight: bold; color: #c0392b; }}
        .ctx-lnum {{ color: #999; padding: 3px 10px 3px 4px; text-align: right; min-width: 36px; user-select: none; border-right: 2px solid #f5c6cb; }}
        .ctx-table tr.ctx-error .ctx-lnum {{ color: #c0392b; border-right-color: #c0392b; }}
        .ctx-code {{ padding: 3px 8px; white-space: pre-wrap; word-break: break-all; }}

        /* Stats */
        .stats {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin: 10px 0; }}
        .stat-item {{ background: #f4f6f8; padding: 12px 14px; border-radius: 5px; font-size: 13px; }}
        .stat-item .stat-label {{ font-weight: bold; color: #2c3e50; margin-bottom: 4px; display: block; }}
        .stat-item .stat-value {{ font-size: 22px; font-weight: bold; color: #2980b9; }}
        .stat-item .stat-note {{ font-size: 11px; color: #888; margin-top: 2px; }}

        .all-good {{ background: #eafaf1; border: 1px solid #27ae60; border-radius: 5px; padding: 10px 14px; color: #1e8449; font-size: 13px; margin: 8px 0; }}
    </style>
</head>
<body>
<div class="container">
    <h1>XML Validation Report</h1>
    <div class="file-info">
        <div class="fname">{file_name}</div>
        <div>Full path: {results['file']}</div>
        <div>Checked on: {validated_time}</div>
    </div>

    <div class="status {pass_fail_class}">{pass_fail_text}</div>

    {'<div class="issues-summary"><h3>Problems found</h3>' + issues_summary_html + '</div>' if all_issues else ''}

    <div class="counters">
        <div class="counter errors">
            <div class="num">{results['error_count']}</div>
            <div class="label">Error(s)<br><small>Must be fixed</small></div>
        </div>
        <div class="counter warnings">
            <div class="num">{results['warning_count']}</div>
            <div class="label">Warning(s)<br><small>Review recommended</small></div>
        </div>
        <div class="counter {'wellformed' if results['wellformed'] else 'wellformed-fail'}">
            <div class="num">{'Valid' if results['wellformed'] else 'Invalid'}</div>
            <div class="label">XML structure<br><small>{'File can be read' if results['wellformed'] else 'File cannot be parsed'}</small></div>
        </div>
    </div>
"""

        # --- Well-formedness error (THE most critical — shown prominently) ---
        if wellformed_error:
            wf = results['details'].get('wellformed_detail', {})
            line_num = wf.get('line', 0)
            col_num  = wf.get('column', 0)
            plain_en = wf.get('plain_english', '')
            fix_adv  = wf.get('fix', '')
            unclosed = wf.get('unclosed_tag', '')
            context  = wf.get('context', [])

            location_badge = ''
            if line_num:
                location_badge = f'<span class="loc-badge">Line {line_num}, column {col_num}</span>'

            unclosed_html = ''
            is_mismatch = 'mismatched tag' in wellformed_error.lower()
            if unclosed:
                if is_mismatch:
                    unclosed_html = (
                        f'<div class="unclosed-tag">Problem tag on line {line_num}: '
                        f'<code>&lt;/{unclosed}&gt;</code> — this closing tag has no matching open tag. '
                        f'Either remove it, or check that <code>&lt;{unclosed}&gt;</code> was not accidentally written as self-closing '
                        f'(<code>&lt;{unclosed} .../&gt;</code>).</div>'
                    )
                else:
                    unclosed_html = f'<div class="unclosed-tag">Likely unclosed tag: <code>&lt;{unclosed}&gt;</code> — add <code>&lt;/{unclosed}&gt;</code> after the last occurrence.</div>'

            # Build context table
            context_html = ''
            if context:
                context_html = '<div class="ctx-block"><div class="ctx-title">File content around the error:</div><table class="ctx-table">'
                for row in context:
                    cls = 'ctx-error' if row['is_error'] else ''
                    arrow = ' ◀ ERROR' if row['is_error'] else ''
                    content_escaped = (row['content']
                        .replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))
                    context_html += f'<tr class="{cls}"><td class="ctx-lnum">{row["line_num"]}</td><td class="ctx-code">{content_escaped}{arrow}</td></tr>'
                context_html += '</table></div>'

            html += f"""
    <h2>🔴 XML Structure Error</h2>
    <div class="wf-error">
        <div class="wf-header">
            <span class="wf-title">The XML file cannot be read</span>
            {location_badge}
        </div>
        <div class="wf-detail">{wellformed_error}</div>
        {'<div class="wf-what">' + plain_en + '</div>' if plain_en else ''}
        {unclosed_html}
        {context_html}
        {'<div class="wf-fix"><strong>How to fix:</strong> ' + fix_adv + '</div>' if fix_adv else ''}
        <div class="wf-fix" style="margin-top:8px;"><strong>Next step:</strong> Fix this error, then re-run the validator to check everything else.</div>
    </div>
"""

        # --- DTD issues ---
        dtd_issues = results['details'].get('dtd_issues', [])
        if dtd_issues:
            html += "<h2>🏗️ DTD Structure Issues</h2>\n"
            html += '<p style="font-size:13px;color:#555;margin:0 0 10px 0;">These are structural errors found by validating against the official PAX DTD — wrong tags, missing required elements, or incorrect nesting.</p>\n'
            for issue in dtd_issues:
                exp = self.RULE_EXPLANATIONS.get('DTD', {})
                sev = issue['severity']
                severity_label = '🔴 Error' if sev == 'error' else '🟡 Warning'
                html += f"""
        <div class="issue {sev}">
            <div class="issue-header">
                <span class="severity-label">{severity_label}</span>
                <span class="rule-id">DTD</span>
                <span style="font-size:12px;color:#888;">{issue.get('location','')}</span>
            </div>
            <div class="issue-title">{issue['title']}</div>
            {'<div class="issue-fix"><strong>How to fix:</strong> ' + issue['fix'] + '</div>' if issue.get('fix') else ''}
        </div>
"""
        elif results['wellformed']:
            html += '<div class="all-good">OK DTD validation passed — all tags and structure are correct.</div>'

        # --- Citation issues ---
        if citation_issues:
            html += "<h2>📚 Citation Issues</h2>\n"
            for issue in citation_issues:
                html += self._rule_html(issue['rule'], issue['message'], issue['severity'])
        elif results['wellformed']:
            html += '<div class="all-good">OK No citation issues found.</div>'

        # --- Heading issues ---
        if heading_issues:
            html += "<h2>📌 Heading Issues</h2>\n"
            for issue in heading_issues:
                html += self._rule_html(issue['rule'], issue['message'], 'error')
        elif results['wellformed']:
            html += '<div class="all-good">OK No heading issues found.</div>'

        # --- Form issues ---
        if form_issues:
            html += "<h2>📋 Form Issues</h2>\n"
            for issue in form_issues:
                html += self._rule_html(issue['rule'], issue['message'], issue['severity'])
        elif results['wellformed']:
            html += '<div class="all-good">OK No form issues found.</div>'

        # --- Footnote issues ---
        if footnote_issues:
            html += "<h2>🔖 Footnote Issues</h2>\n"
            for issue in footnote_issues:
                html += self._rule_html(issue['rule'], issue['message'], issue['severity'])
        elif results['wellformed']:
            html += '<div class="all-good">OK No footnote issues found.</div>'

        # --- Attribute issues ---
        if attribute_issues:
            html += "<h2>🏷️ Attribute Issues</h2>\n"
            for issue in attribute_issues:
                html += self._rule_html(issue['rule'], issue['message'], issue['severity'])
        elif results['wellformed']:
            html += '<div class="all-good">OK No attribute issues found.</div>'

        # --- Tag whitelist issues ---
        whitelist_issues = results['details'].get('whitelist_issues', [])
        if whitelist_issues:
            html += "<h2>🚫 Unknown/Invalid Tags</h2>\n"
            for issue in whitelist_issues:
                severity_label = '🔴 Error'
                html += f"""
        <div class="issue error">
            <div class="issue-header">
                <span class="severity-label">{severity_label}</span>
                <span class="rule-id">{issue['rule']}</span>
            </div>
            <div class="issue-title">{issue['message']}</div>
            <div class="issue-fix"><strong>How to fix:</strong> Replace this tag with the correct PAX element name. Refer to the PAX DTD or the tag reference in the DAC tag directory.</div>
        </div>
"""
        elif results['wellformed']:
            html += '<div class="all-good">OK All elements are valid PAX tags.</div>'

        # --- Reference rules issues (from sharepoint-rules.md) ---
        reference_issues = results['details'].get('reference_issues', [])
        ref_rules_present = (self.schematron_dir / 'sharepoint-rules.md').exists()
        if reference_issues:
            html += "<h2>📐 Presentation & Reference Rules</h2>\n"
            for issue in reference_issues:
                html += self._rule_html(issue['rule'], issue['message'], issue['severity'])
        elif ref_rules_present and results['wellformed']:
            html += '<div class="all-good">OK All presentation/reference rules passed.</div>'
        elif not ref_rules_present:
            html += """<div class="all-good" style="background:#f0f4ff;border-color:#2980b9;color:#1a4a7a;">
                ℹ️ <strong>sharepoint-rules.md</strong> not found — paste your SharePoint presentation rules
                into that file to enable automated checking. See the template already in this folder.
            </div>"""

        # --- Statistics (only shown when file parsed successfully) ---
        if results['wellformed']:
            cit = results['details'].get('citations', {})
            forms = results['details'].get('forms', {})
            fns = results['details'].get('footnotes', {})
            html += f"""
    <h2>📊 Document Statistics</h2>
    <div class="stats">
        <div class="stat-item">
            <span class="stat-label">Case Citations</span>
            <div class="stat-value">{cit.get('case_citations', 0)}</div>
            <div class="stat-note">e.g. "Smith v. Jones"</div>
        </div>
        <div class="stat-item">
            <span class="stat-label">Statute / Law Citations</span>
            <div class="stat-value">{cit.get('statute_citations', 0)}</div>
            <div class="stat-note">References to acts, codes, regulations</div>
        </div>
        <div class="stat-item">
            <span class="stat-label">Journal / Periodical Citations</span>
            <div class="stat-value">{cit.get('periodical_citations', 0)}</div>
            <div class="stat-note">Law review articles, journals</div>
        </div>
        <div class="stat-item">
            <span class="stat-label">Forms</span>
            <div class="stat-value">{forms.get('forms_found', 0)}</div>
            <div class="stat-note">Form elements in document</div>
        </div>
        <div class="stat-item">
            <span class="stat-label">Inline Instructions</span>
            <div class="stat-value">{forms.get('inline_instr_count', 0)}</div>
            <div class="stat-note">Fill-in placeholders [ ] in forms</div>
        </div>
        <div class="stat-item">
            <span class="stat-label">Footnotes</span>
            <div class="stat-value">{fns.get('footnotes_found', 0)}</div>
            <div class="stat-note">Footnote elements in document</div>
        </div>
    </div>
"""

        html += """
</div>
</body>
</html>"""

        output_file.write_text(html, encoding='utf-8')
        print(f"\n📄 HTML report generated: {output_file}")

    def _collect_issues(self, results: Dict) -> List[Dict]:
        """Flatten all issues from results into a uniform list of dicts."""
        issues = []

        wellformed_error = results['details'].get('wellformed_error', '')
        wf = results['details'].get('wellformed_detail', {})
        if wellformed_error:
            location = f"Line {wf['line']}, column {wf['column']}" if wf.get('line') else ''
            unclosed = f"Likely unclosed tag: <{wf['unclosed_tag']}>" if wf.get('unclosed_tag') else ''
            detail_parts = [p for p in [location, unclosed, wellformed_error] if p]
            issues.append({
                'severity': 'ERROR',
                'category': 'XML Structure',
                'rule': '—',
                'title': wf.get('plain_english', 'XML file cannot be read — structural/formatting problem'),
                'detail': ' | '.join(detail_parts),
                'fix': wf.get('fix', 'Fix the structural error and re-run the validator.'),
            })

        for i in results['details'].get('dtd_issues', []):
            issues.append({
                'severity': i['severity'].upper(),
                'category': 'DTD Structure',
                'rule': 'DTD',
                'title': i['title'],
                'detail': i.get('location', ''),
                'fix': i.get('fix', ''),
            })

        def _add(raw_issues, category):
            for i in raw_issues:
                exp = self.RULE_EXPLANATIONS.get(i['rule'], {})
                import re
                found = ''
                quoted = re.findall(r'"([^"]+)"', i['message'])
                if quoted:
                    found = quoted[-1]
                issues.append({
                    'severity': i['severity'].upper(),
                    'category': category,
                    'rule': i['rule'],
                    'title': exp.get('title', i['message']),
                    'detail': found,
                    'fix': exp.get('fix', ''),
                })

        _add(results['details'].get('citations', {}).get('citation_issues', []), 'Citations')
        _add(results['details'].get('heading_issues', []), 'Headings')
        _add(results['details'].get('forms', {}).get('form_issues', []), 'Forms')
        _add(results['details'].get('footnotes', {}).get('footnote_issues', []), 'Footnotes')
        _add(results['details'].get('attribute_issues', []), 'Attributes')

        return issues

    def generate_txt_report(self, results: Dict, output_file: Path):
        """Generate a plain-text report readable in Word or any text editor."""
        W = 70  # line width
        file_name = Path(results['file']).name
        validated_time = datetime.fromisoformat(results['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
        status = 'PASSED' if results['validation_passed'] else 'FAILED'
        issues = self._collect_issues(results)

        lines = []
        lines.append('=' * W)
        lines.append('XML VALIDATION REPORT')
        lines.append('=' * W)
        lines.append(f"File   : {file_name}")
        lines.append(f"Path   : {results['file']}")
        lines.append(f"Checked: {validated_time}")
        lines.append(f"Result : {status}  ({results['error_count']} error(s), {results['warning_count']} warning(s))")
        lines.append(f"XML OK : {'Yes — file can be parsed' if results['wellformed'] else 'No — file has a structural problem'}")
        lines.append('')

        if not issues:
            lines.append('No issues found. The file passed all checks.')
        else:
            lines.append(f"PROBLEMS FOUND ({len(issues)} total)")
            lines.append('-' * W)
            for idx, issue in enumerate(issues, 1):
                lines.append(f"\n[{idx}]  {issue['severity']}  |  {issue['category']}  |  Rule: {issue['rule']}")
                lines.append(f"     {issue['title']}")
                if issue['detail']:
                    lines.append(f"     Detail  : {issue['detail']}")
                if issue['fix']:
                    lines.append(f"     Fix     : {issue['fix']}")

            # Show file context for well-formedness errors
            wf = results['details'].get('wellformed_detail', {})
            ctx = wf.get('context', [])
            if ctx:
                lines.append('')
                lines.append('  FILE CONTENT AROUND THE ERROR:')
                lines.append('  ' + '-' * 60)
                for row in ctx:
                    marker = ' <-- ERROR' if row['is_error'] else ''
                    lines.append(f"  {row['line_num']:>5} | {row['content']}{marker}")
                lines.append('  ' + '-' * 60)
            lines.append('')
            lines.append('-' * W)

        # Statistics (only if file parsed)
        if results['wellformed']:
            cit = results['details'].get('citations', {})
            forms = results['details'].get('forms', {})
            fns = results['details'].get('footnotes', {})
            lines.append('')
            lines.append('DOCUMENT STATISTICS')
            lines.append('-' * W)
            lines.append(f"  Case citations (e.g. Smith v. Jones) : {cit.get('case_citations', 0)}")
            lines.append(f"  Statute / law citations               : {cit.get('statute_citations', 0)}")
            lines.append(f"  Journal / periodical citations        : {cit.get('periodical_citations', 0)}")
            lines.append(f"  Forms                                 : {forms.get('forms_found', 0)}")
            lines.append(f"  Inline instructions (fill-in [ ])     : {forms.get('inline_instr_count', 0)}")
            lines.append(f"  Footnotes                             : {fns.get('footnotes_found', 0)}")
        else:
            lines.append('')
            lines.append('DOCUMENT STATISTICS')
            lines.append('-' * W)
            lines.append('  Not available — file could not be parsed.')

        lines.append('')
        lines.append('=' * W)

        output_file.write_text('\n'.join(lines), encoding='utf-8')
        print(f"\n📄 Text report generated: {output_file}")

    def generate_csv_report(self, results: Dict, output_file: Path):
        """Generate a CSV report — opens in Excel with one row per issue."""
        import csv
        issues = self._collect_issues(results)
        file_name = Path(results['file']).name
        validated_time = datetime.fromisoformat(results['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
        status = 'PASSED' if results['validation_passed'] else 'FAILED'

        with open(output_file, 'w', newline='', encoding='utf-8-sig') as f:
            # utf-8-sig writes the BOM so Excel opens it correctly without garbled characters
            writer = csv.writer(f)

            # Header rows with file info
            writer.writerow(['XML Validation Report'])
            writer.writerow(['File', file_name])
            writer.writerow(['Full path', results['file']])
            writer.writerow(['Checked', validated_time])
            writer.writerow(['Result', status,
                             f"{results['error_count']} error(s)",
                             f"{results['warning_count']} warning(s)"])
            writer.writerow([])

            # Column headers for issues table
            writer.writerow(['#', 'Severity', 'Category', 'Rule', 'Issue', 'Found text', 'How to fix'])

            if not issues:
                writer.writerow(['', '', '', '', 'No issues found — file passed all checks.', '', ''])
            else:
                for idx, issue in enumerate(issues, 1):
                    writer.writerow([
                        idx,
                        issue['severity'],
                        issue['category'],
                        issue['rule'],
                        issue['title'],
                        issue['detail'],
                        issue['fix'],
                    ])

            # Statistics section
            writer.writerow([])
            writer.writerow(['Document Statistics'])
            if results['wellformed']:
                cit = results['details'].get('citations', {})
                forms = results['details'].get('forms', {})
                fns = results['details'].get('footnotes', {})
                writer.writerow(['Case citations (e.g. Smith v. Jones)', cit.get('case_citations', 0)])
                writer.writerow(['Statute / law citations', cit.get('statute_citations', 0)])
                writer.writerow(['Journal / periodical citations', cit.get('periodical_citations', 0)])
                writer.writerow(['Forms', forms.get('forms_found', 0)])
                writer.writerow(['Inline instructions (fill-in [ ])', forms.get('inline_instr_count', 0)])
                writer.writerow(['Footnotes', fns.get('footnotes_found', 0)])
            else:
                writer.writerow(['Not available', 'File could not be parsed'])

        print(f"\n📄 CSV report generated: {output_file}")


def main():
    parser = argparse.ArgumentParser(
        description='PAX XML Validation Tool - Thomson Reuters Phoenix Project',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python validate-pax.py myfile.xml
  python validate-pax.py myfile.xml --report report.html
  python validate-pax.py myfile.xml --txt report.txt
  python validate-pax.py myfile.xml --csv report.csv
  python validate-pax.py myfile.xml --json results.json
        """
    )

    parser.add_argument('xml_file', type=Path, help='XML file to validate')
    parser.add_argument('--schematron-dir', type=Path,
                       default=Path(__file__).parent,
                       help='Directory containing Schematron files')
    parser.add_argument('--report', type=Path, help='Output HTML report file')
    parser.add_argument('--txt', type=Path, help='Output plain-text report (opens in Word / Notepad)')
    parser.add_argument('--csv', type=Path, help='Output CSV report (opens in Excel)')
    parser.add_argument('--json', type=Path, help='Output JSON results file')

    args = parser.parse_args()

    # Validate inputs
    if not args.xml_file.exists():
        print(f"FAIL Error: XML file not found: {args.xml_file}")
        sys.exit(1)

    if not args.schematron_dir.exists():
        print(f"FAIL Error: Schematron directory not found: {args.schematron_dir}")
        sys.exit(1)

    # Run validation
    validator = PAXValidator(args.schematron_dir)
    results = validator.validate_file(args.xml_file)

    # Generate HTML report (always produced by default)
    if args.report:
        validator.generate_html_report(results, args.report)
    else:
        default_report = args.xml_file.with_suffix('.validation.html')
        validator.generate_html_report(results, default_report)

    # Plain-text report
    if args.txt:
        validator.generate_txt_report(results, args.txt)
    else:
        default_txt = args.xml_file.with_suffix('.validation.txt')
        validator.generate_txt_report(results, default_txt)

    # CSV report
    if args.csv:
        validator.generate_csv_report(results, args.csv)
    else:
        default_csv = args.xml_file.with_suffix('.validation.csv')
        validator.generate_csv_report(results, default_csv)

    # JSON output
    if args.json:
        args.json.write_text(json.dumps(results, indent=2), encoding='utf-8')
        print(f"📄 JSON results saved: {args.json}")
    
    # Exit with appropriate code
    sys.exit(0 if results['validation_passed'] else 1)


if __name__ == '__main__':
    main()
