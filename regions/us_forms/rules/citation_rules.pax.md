
CASE CITATION RULES (US Forms — MANDATORY):

US Forms use <cite type="case"> for case citations — NOT a bare <cc> tag. <cc>/<st>/<pl>/<x>/
<tk> are NOT declared anywhere in this DTD (pax.dtd) and produce INVALID XML if used — this
DTD's only citation element is <cite>, distinguished entirely by its REQUIRED type attribute:
  - case citations                                        -> <cite type="case">
  - statutes / codes / CFR / constitutions / public laws / named acts / court rules
                                                            -> <cite type="lrre">
  - law-review & journal periodicals                       -> <cite type="periodical">
  - internal section cross-references (e.g. § 3:567, §§ 2:18, 3:13)
                                                            -> <cite type="secondaryxref">
  - West's Key Number Digest references                    -> <cite type="topic.key">
  - URLs / DOIs                                            -> <cite type="url"> (see URL rules)
<cite> takes NO attribute besides `type` (and, optionally, `finder="manual"` — matching how
this document's own existing, pre-existing citations are tagged; never invent a `ref.id` or
`normalpub` value, since those come from a citation-lookup database this pipeline doesn't have
access to — plain `<cite type="...">text</cite>` with no nested <ref> is valid DTD content on
its own). This section covers CASE citations specifically (`type="case"`); the other `type`
values above follow the same wrapping mechanics, just swap the type value.

1. FULL vs SHORT CITATION
   A. FULL citation = a case reference that includes ANY of: year, court, reporter, volume,
      page, docket number, or WL/LEXIS number. Wrap the ENTIRE citation in
      <cite type="case">...</cite>.
      Example: State v. Walker, 36 Kan. 297, 13 P. 279 (1887)
        -> <cite type="case">State v. Walker, 36 Kan. 297, 13 P. 279 (1887)</cite>
   B. SHORT citation = an adversarial case name (Party1 v. Party2) with NONE of the above.
      Do NOT wrap in <cite type="case">. Keep <ital> ONLY if the input already had italics;
      never add italics that were not there; never expand a short cite into a full cite even
      if the case was fully cited earlier.
      NOT short citations (leave as ordinary text): single-word mentions ("Emoral"), named
      tests/doctrines ("Emoral test", "Smith doctrine"), and id./supra/infra.

2. ONE <cite type="case"> PER CASE
   - Each "v." starts a new case. Run-together citations must be SPLIT into separate
     <cite type="case"> tags, preserving the separator punctuation (e.g. "; ") between them.
     Example: State v. Scerba, 2025-Ohio-2791, 271 N.E.3d 932 (Ohio Ct. App. 2025); State v.
     Chandler, 54 Ohio App. 3d 92, 560 N.E.2d 832 (1989), dismissed, 49 Ohio St. 3d 716 (1990)
        -> <cite type="case">State v. Scerba, 2025-Ohio-2791, 271 N.E.3d 932 (Ohio Ct. App. 2025)</cite>; <cite type="case">State v. Chandler, 54 Ohio App. 3d 92, 560 N.E.2d 832 (1989), dismissed, 49 Ohio St. 3d 716 (1990)</cite>
   - Keep ALL subsequent history (cert. denied, aff'd, overruled by, dismissed, etc.) INSIDE
     the same <cite type="case"> as the case it belongs to.
   - VALIDATION: the count of "v." in the inserted text equals the count of <cite type="case">
     pairs.

3. TRACK-CHANGE FORM (this pipeline)
   - A newly inserted case citation is coded INSIDE the trace / new-content wrapper; the
     <cite type="case"> itself never carries date.trace or any attribute besides type/finder:
        <trace>The court ruled in <cite type="case">Smith v. Jones, 123 F.3d 456 (9th Cir. 2000)</cite>, that...</trace>
   - <cite type="case"> is NEVER given date.trace="1", date.updated, or any attribute other
     than `type` (and optionally `finder="manual"`).

4. SIGNAL / INTRODUCTORY WORDS
   - Words such as See, But see, Compare, Accord, Cf., E.g., Contra, See also, See generally,
     Citing, Following, Discussed in, Applied in stay OUTSIDE <cite type="case"> but INSIDE
     the trace / new content. Preserve any italic/bold formatting on the signal word.
        RIGHT: <trace>See <cite type="case">Smith v. Jones, 123 F.3d 456 (9th Cir. 2000)</cite></trace>
        WRONG: <trace><cite type="case">See Smith v. Jones, 123 F.3d 456 (9th Cir. 2000)</cite></trace>
   - Prefatory text before a case name is never absorbed into or dropped from
     <cite type="case">.

5. SHORT-IN-TEXT + FULL-IN-FOOTNOTE
   - When the body text has only the short case name and a footnote holds the full citation:
     the body keeps <ital> ONLY (if the input italicized it); the FULL <cite type="case">
     lives ONLY inside the footnote.body. Never duplicate the full citation in both places.
     Example: In <ital>AFX Mixing & Pumping Technologies Inc. v. McKinon</ital>,<footnote><footnote.reference>1</footnote.reference><footnote.body><para><para.text><cite type="case">AFX Mixing & Pumping Technologies Inc. v. McKinon, 62 B.L.R. (6th) 204 (2025)</cite>.</para.text></para></footnote.body></footnote> the plaintiff sought...

6. PAGE / PARAGRAPH REFERENCES (<point>)
   - Use <point> ONLY for page/paragraph references that appear WITHIN the citation text
     before any further citation element (e.g. "..., <point>487</point>, ..."). An "at para
     97" that FOLLOWS a complete citation stays OUTSIDE <cite type="case"> entirely.

7. CLASSIFY BEFORE FORMATTING
   - Detect case citations even inside <i>/<b>/<bi>/<us> (or their expanded <ital>/<bold>/
     <boldital>/<underscore>) forms; citation classification takes precedence over existing
     formatting. If a full-sequence citation is underscored, wrap the WHOLE sequence in
     <cite type="case"> and keep <underscore> inside it.

8. CONSISTENCY
   - Apply the same full/short classification consistently for every occurrence of a given
     citation throughout the document.

NON-CASE citations use the SAME <cite> element with a DIFFERENT `type` value (see the table
at the top of this rule set) — never a bare <cc>/<st>/<pl>/<x>/<tk> tag.
