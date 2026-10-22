
CASE CITATION RULES (US Forms — MANDATORY):

This document is a PIX document (DOCTYPE pix.dtd). In pix.dtd the bare citation tags
<cc>, <st>, <pl>, <x>, <tk>, <rc>, <ntc>, <url> ARE declared and <cite> is NOT declared —
so use the bare tags below and NEVER <cite>.
US Forms use <cc> for case citations. NEVER use <cite type="case"> / <ref ...> for a case. <cc> takes NO attributes. Statutes use
<st>, periodicals <pl>, cross-references <x>, and non-case cites (URLs / CFR) use <cite> —
each has its own rule below / elsewhere.

1. FULL vs SHORT CITATION
   A. FULL citation = a case reference that includes ANY of: year, court, reporter, volume,
      page, docket number, or WL/LEXIS number. Wrap the ENTIRE citation in <cc>...</cc>
      (no attributes).
      Example: State v. Walker, 36 Kan. 297, 13 P. 279 (1887)
        -> <cc>State v. Walker, 36 Kan. 297, 13 P. 279 (1887)</cc>
   B. SHORT citation = an adversarial case name (Party1 v. Party2) with NONE of the above.
      Do NOT wrap in <cc>. Keep <ital> ONLY if the input already had italics; never add
      italics that were not there; never expand a short cite into a full cite even if the
      case was fully cited earlier.
      NOT short citations (leave as ordinary text): single-word mentions ("Emoral"), named
      tests/doctrines ("Emoral test", "Smith doctrine"), and id./supra/infra.

2. ONE <cc> PER CASE
   - Each "v." starts a new case. Run-together citations must be SPLIT into separate <cc>
     tags, preserving the separator punctuation (e.g. "; ") between them.
     Example: State v. Scerba, 2025-Ohio-2791, 271 N.E.3d 932 (Ohio Ct. App. 2025); State v.
     Chandler, 54 Ohio App. 3d 92, 560 N.E.2d 832 (1989), dismissed, 49 Ohio St. 3d 716 (1990)
        -> <cc>State v. Scerba, 2025-Ohio-2791, 271 N.E.3d 932 (Ohio Ct. App. 2025)</cc>; <cc>State v. Chandler, 54 Ohio App. 3d 92, 560 N.E.2d 832 (1989), dismissed, 49 Ohio St. 3d 716 (1990)</cc>
   - Keep ALL subsequent history (cert. denied, aff'd, overruled by, dismissed, etc.) INSIDE
     the same <cc> as the case it belongs to.
   - VALIDATION: the count of "v." in the inserted text equals the count of <cc> pairs.

3. TRACK-CHANGE FORM (this pipeline)
   - A newly inserted case citation is coded INSIDE the trace / new-content wrapper; the <cc>
     itself never carries date.trace or any attribute:
        <trace>The court ruled in <cc>Smith v. Jones, 123 F.3d 456 (9th Cir. 2000)</cc>, that...</trace>
   - <cc> is NEVER given date.trace="1", date.updated, or any attribute.

4. SIGNAL / INTRODUCTORY WORDS
   - Words such as See, But see, Compare, Accord, Cf., E.g., Contra, See also, See generally,
     Citing, Following, Discussed in, Applied in stay OUTSIDE <cc> but INSIDE the trace / new
     content. Preserve any italic/bold formatting on the signal word.
        RIGHT: <trace>See <cc>Smith v. Jones, 123 F.3d 456 (9th Cir. 2000)</cc></trace>
        WRONG: <trace><cc>See Smith v. Jones, 123 F.3d 456 (9th Cir. 2000)</cc></trace>
   - Prefatory text before a case name is never absorbed into or dropped from <cc>.

5. SHORT-IN-TEXT + FULL-IN-FOOTNOTE
   - When the body text has only the short case name and a footnote holds the full citation:
     the body keeps <ital> ONLY (if the input italicized it); the FULL <cc> lives ONLY inside
     the footnote.body. Never duplicate the full citation in both places.
     Example: In <ital>AFX Mixing & Pumping Technologies Inc. v. McKinon</ital>,<footnote><footnote.reference>1</footnote.reference><footnote.body><para><para.text><cc>AFX Mixing & Pumping Technologies Inc. v. McKinon, 62 B.L.R. (6th) 204 (2025)</cc>.</para.text></para></footnote.body></footnote> the plaintiff sought...

6. PAGE / PARAGRAPH REFERENCES (<point>)
   - Use <point> ONLY for page/paragraph references that appear WITHIN the citation text
     before any further citation element (e.g. "..., <point>487</point>, ..."). An "at para
     97" that FOLLOWS a complete citation stays OUTSIDE the <cc> entirely.

7. CLASSIFY BEFORE FORMATTING
   - Detect case citations even inside <i>/<b>/<bi>/<us> (or their expanded <ital>/<bold>/
     <boldital>/<underscore>) forms; citation classification takes precedence over existing
     formatting. If a full-sequence citation is underscored, wrap the WHOLE sequence in <cc>
     and keep <underscore> inside it.

8. CONSISTENCY
   - Apply the same full/short classification consistently for every occurrence of a given
     citation throughout the document.

NON-CASE citations are NOT <cc>:
  - statutes / codes / CFR / constitutions / public laws / named acts  -> <st>
  - law-review & journal periodicals                                   -> <pl>
  - internal section cross-references (e.g. § 3:567, §§ 2:18, 3:13)     -> <x>
  - West's Key Number Digest references                                -> <tk>
  - URLs / DOIs                                                        -> <url> (see URL rules)
  - <rc> and <ntc> are also valid in this DTD: use them ONLY where the input text
    already carries that kind of reference — never invent them.
