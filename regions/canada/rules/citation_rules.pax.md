CASE CITATION RULES (MANDATORY):

3. Case Citations Rules:
3.1 Citation Classification:
   A. Examine each case reference and classify:
      - If citation contains year, court reference, reporter reference, or decision number → FULL CITATION
      - If citation contains only the case name → SHORT CITATION
   B. Process each citation independently based solely on its immediate context.
   C. Citation Recognition Requirements:
      - Case citations must be identified and properly formatted regardless of how they appear in the input
      - Search for and identify case citations whether they appear within <i>, <b>, <bi>, <us> tags or their expanded equivalents (<ital>, <bold>, etc.)
      - The citation classification rules take precedence over any existing formatting tags
3.2 Full Citation Format:
When a citation includes elements beyond just the case name:
   A. Citation Structure Requirements:
      - Always include p1, p2 (if applicable), pre.rptr, post.rptr, and title attributes in all full citations
   B. Extracting pre.rptr and post.rptr:
      - CONSISTENT PATTERN RULE: Always use the FIRST TWO numeric elements that appear in the citation:
         * pre.rptr = first numeric element (year, volume number, etc.)
         * post.rptr = second numeric element (reporter number, page number, etc.)
      - This pattern applies consistently regardless of citation format:
         * Standard Format (e.g., "2018 FC 648"): pre.rptr="2018" post.rptr="648"
         * Square Bracket Format (e.g., "[1987] 2 S.C.R. 99"): pre.rptr="[1987] 2" post.rptr="99"
         * Year-in-parentheses Format (e.g., "(1976), 73 D.L.R. (3d) 62"): pre.rptr="73" post.rptr="62"
      - CRITICAL PRE.RPTR AND POST.RPTR RULE:
         * ALWAYS extract from FIRST occurrence of year/numbers in citation
         * For "2025 FC 478, 2025 CarswellNat 843" → pre.rptr="2025" post.rptr="478"
         * NEVER use values from secondary citations (CarswellNat, etc.)
         * VERIFY both values come from same citation element
      - When multiple options exist (e.g., "2025 CarswellNS 16, 2025 NSCA 3"), always use the first occurrence
      - Process exactly as they appear, preserving all formatting including brackets, spaces, and punctuation
   C. Citation Examples:
      1. Standard Format:
      ```
      <cite type="case"><ref p1="R." p2="T.W.W." post.rptr="1463" pre.rptr="2024" title="R. v. T.W.W.">R. v. T.W.W., 2024 CarswellBC 1463, 2024 SCC 19, 2024 CarswellBC 1463 (S.C.C.)</ref></cite>
      ```
      2. Standard format (NO formatting tags inside cite):
      ```
      <cite type="case"><ref p1="Caplan" p2="Atas" post.rptr="670" pre.rptr="2021" title="Caplan v. Atas">Caplan v. Atas, 2021 ONSC 670 (Ont. S.C.J.)</ref></cite>
      ```
      ⚠️ NOTE: Even if the input has <ital> around the case name inside <cite>, the output must NOT include it.
      3. Square Bracket Format:
      ```
      <cite type="case"><ref p1="Frame" p2="Smith" post.rptr="99" pre.rptr="[1987] 2" title="Frame v. Smith">Frame v. Smith, [1987] 2 S.C.R. 99 (S.C.C.)</ref></cite>
      ```
      4.
      ```
      <cite type="case"><ref title="Motherwell v. Motherwell" p1="Motherwell" p2="Motherwell" pre.rptr="73" post.rptr="62" date="1976">Motherwell v. Motherwell (1976), 73 D.L.R. (3d) 62 (Alta. C.A.)</ref></cite>
      ```
   D. Two-party Citation:
      <cite type="case"><ref p1="Party1" p2="Party2" post.rptr="XXXX" pre.rptr="YYYY" title="Party1 v. Party2">complete citation details</ref></cite>
   E. Single-party Citation:
      <cite type="case"><ref p1="Party1" post.rptr="XXXX" pre.rptr="YYYY" title="Party1">complete citation details</ref></cite>
   F. Paragraph References:
      - Use <point> tags for paragraph references
     
   G. Continuing with Paragraph References:                            
      - POINT TAG USAGE RESTRICTIONS:
         * USE <point> tags ONLY for:
         - Page numbers WITHIN citation text:
            "2024 BCSC 480, <point>487</point>, 2024 CarswellBC..."
            "2024 BCSC 480, <point>481 - 516</point>, 2024 CarswellBC..."
            "2024 CarswellBC 815 (B.C. S.C.) <point>at paras. 139-141</point>, additional reasons..."
         - Paragraph references WITHIN citation BEFORE additional citation elements
         
         * DO NOT use point tags for:
         - "at para" phrases after the complete citation
         - Any text following the court reference in parentheses
         
         * Content after full citation belongs OUTSIDE ref/cite tags entirely
           — EXCEPTION: subsequent/prior history ("affirmed", "aff'd", "reversed",
             "rev'd", "varied", "additional reasons", "leave to appeal refused",
             etc.) is PART of the citation and stays INSIDE the <ref> (see §3.2.1).
         * PARAGRAPH REFERENCES MUST BE OUTSIDE REF/CITE TAGS:
            - CORRECT: <cite>...</cite> at para 97 and 98
            - INCORRECT: <cite><ref>... at para 97 and 98</ref></cite>
      - Example:
      ```
      <cite type="case"><ref p1="I.F." p2="Gilead Sciences, Inc." pre.rptr="2024" post.rptr="480" title="I.F. v. Gilead Sciences, Inc.">I.F. v. Gilead Sciences, Inc., 2024 BCSC 480, 2024 CarswellBC 815 (B.C. S.C.) <point>at paras. 139-141</point>, additional reasons 2024 BCSC 1479 (B.C. S.C.)</ref></cite>
      ```

3.2.1 ⚠️ SUBSEQUENT / PRIOR HISTORY MUST STAY INSIDE THE REF:
   Appellate / procedural history that is part of the SAME citation MUST remain
   INSIDE the <ref> text — it is NOT "content after the citation".
   This includes connectors such as: "affirmed", "aff'd", "reversed", "rev'd",
   "varied", "additional reasons", "supplementary reasons", "leave to appeal
   refused", "leave to appeal granted", "reconsideration refused", each followed
   by its own year/court/reporter (e.g. "affirmed 2024 BCCA 388").
   - Extract p1/p2/pre.rptr/post.rptr from the FIRST citation element only, but the
     visible <ref> text MUST run from the case name THROUGH the history clause.
   ✓ RIGHT:
   <cite type="case"><ref p1="C.G." p2="M.G." pre.rptr="2023" post.rptr="1355" title="C.G. v. M.G.">C.G. v. M.G., 2023 BCSC 1355, 2023 CarswellBC 2305, affirmed 2024 BCCA 388</ref></cite>
   ✗ WRONG (history pushed outside the cite):
   <cite type="case"><ref ...>C.G. v. M.G., 2023 BCSC 1355, 2023 CarswellBC 2305</ref></cite>, affirmed 2024 BCCA 388
   NOTE: The "Content after full citation belongs OUTSIDE" rule (§3.2 G) applies ONLY
   to pinpoint/paragraph references ("at para 97") and explanatory parentheticals —
   NOT to subsequent/prior history, which stays inside the <ref>.

3.2.2 ⚠️ REF TEXT MUST BEGIN WITH THE CASE NAME (NEVER SPLIT THE NAME OUT):
   For a FULL citation, the text inside <ref> MUST start with the full case name
   (Party1 v. Party2), even if that name was <ital> in the input. NEVER leave the
   case name as a standalone <ital> before the <cite> while starting the <ref> text
   at the reporter/year. Setting p1/p2/title on the ref does NOT substitute for the
   name appearing in the ref text — the name must be visible inside <ref>.
   ✗ WRONG (name split out, ref text starts at reporter):
   <ital>C.G. v. M.G.</ital> <cite type="case"><ref p1="C.G." p2="M.G." pre.rptr="2023" post.rptr="1355" title="C.G. v. M.G.">2023 BCSC 1355, 2023 CarswellBC 2305</ref></cite>
   ✓ RIGHT (name inside ref, no stray <ital>):
   <cite type="case"><ref p1="C.G." p2="M.G." pre.rptr="2023" post.rptr="1355" title="C.G. v. M.G.">C.G. v. M.G., 2023 BCSC 1355, 2023 CarswellBC 2305</ref></cite>
 
3.3 Short Citation Format:
   A. Definition: A citation is considered "short" when ALL of these apply:
      - An adversarial case name in the form Party1 v. Party2 (or Party1 vs. Party2), AND
      - It contains NO year, court reference, reporter reference, volume number, page number, docket number, or WL/LEXIS number
   B. Non-examples (NOT short citations — do NOT apply short-citation rules):
      - Single-word mentions (e.g., "Emoral", "Musel")
      - Named tests/doctrines (e.g., "Emoral test", "Smith doctrine")
      - Id., supra, infra (unless explicitly defined elsewhere)
   C. Formatting Requirements:
      - Do NOT wrap short citations in <cite> tags
      - Do NOT add <ital> tags if the input has no italic formatting on the case name
      - ONLY preserve existing formatting from input; never add formatting that was not there
      - Example: "In <ital>Smith v. Jones</ital>, the court held..." (only if input had italic)
      - Example: "Following the <ital>Smith</ital> decision, courts have..." (only if input had italic)
      - NEVER use full <cite> structure for short citations even if the case was previously fully cited
      - ⚠️ ITALIC CASE NAME IMMEDIATELY FOLLOWED BY REPORTER/YEAR IN SAME SENTENCE → FULL CITATION:
        When the input has an italic case name directly followed by year/reporter in the same sentence,
        the italic formatting does NOT make it a short citation — use <cite> for the complete citation,
        do NOT add a separate <ital> tag before <cite> (this duplicates the case name):
        • CORRECT: In <cite type="case"><ref p1="R." p2="Zock" pre.rptr="2025" post.rptr="10877" title="R. v. Zock">R. v. Zock, 2025 ONCA 483 (Ont. C.A.)</ref></cite>, trial counsel...
        • WRONG:   In <ital>R. v. Zock</ital>, <cite type="case"><ref...>R. v. Zock, 2025 ONCA 483...</ref></cite>... ❌ (case name duplicated)
        • WRONG:   In <ital>R. v. Zock</ital>, <cite type="case"><ref...>2025 ONCA 483...</ref></cite>... ❌ (name split out — ref text must START with the case name; see §3.2.2)
 
3.3.1 CRITICAL - SHORT CITATION IN CONTENT WITH FULL CITATION IN FOOTNOTE:
   When you see a pattern where:
   • Text has a SHORT case citation (case name only in italics)
   • Followed immediately by a footnote marker (e.g., <fn fnref=0.1>)
   • And the footnote contains the FULL case citation
   
   Then:
   • Content paragraph: Use ONLY <ital> tags for the short case name
   • Do NOT add the full citation to the content paragraph
   • The full citation belongs ONLY inside the <footnote.body>
   
   Example Input:
   In <ital>AFX Mixing & Pumping Technologies Inc. v. McKinon</ital>,<fn fnref=0.1><p><ital>AFX Mixing & Pumping Technologies Inc. v. McKinon</ital>, 2025 CarswellBC 2606, 62 B.L.R. (6th) 204, 2025 BCSC 1717 (B.C.S.C.).</p></fn> the plaintiff, AFX, sought...
   
   CORRECT Output:
   <para><para.text>In <ital>AFX Mixing & Pumping Technologies Inc. v. McKinon</ital>,<footnote><footnote.reference>0.1</footnote.reference><footnote.body><para><para.text><cite type="case"><ref>...</ref></cite>.</para.text></para></footnote.body></footnote> the plaintiff, AFX, sought...</para.text></para>
   
   WRONG Output (DO NOT DO THIS):
   <para><para.text>In <cite type="case"><ref>...</ref></cite>,<footnote>...</footnote> the plaintiff...</para.text></para>
   
   Key Points:
   • Short citation in content = <ital> tags only
   • Full citation in footnote = <cite type="case"><ref> structure
   • Never duplicate the full citation in both locations

3.3.2 ⚠️ ABSOLUTE RULE — CASE NAME ONLY = SHORT CITATION, NEVER <cite>:
   If the body text contains ONLY the case name (e.g., "Remington Development Corp. v.
   Canadian Pacific Railway Company") with NO year, reporter, page number, or court
   reference IMMEDIATELY following it in the SAME sentence — it is a SHORT CITATION.
   
   This is true EVEN IF:
   • A footnote immediately follows that contains the full citation
   • The case name is long or looks "official"
   • You know the full citation from the footnote
   
   TEST: Look ONLY at the body text. Does the text after the case name contain
   a year (e.g., 2025), reporter (e.g., ABCA), or page number? 
   • YES → Full citation, use <cite>
   • NO → Short citation, use <ital> ONLY
   
   ✗ WRONG (case name only in body, full cite only in footnote):
   in <cite type="case"><ref p1="Remington Development Corp." p2="Canadian Pacific Railway Company" pre.rptr="2025" post.rptr="" title="Remington Development Corp. v. Canadian Pacific Railway Company">Remington Development Corp. v. Canadian Pacific Railway Company</ref></cite>,<footnote>...(full cite here)...</footnote> the Alberta Court...
   
   ✓ RIGHT:
   in <ital>Remington Development Corp. v. Canadian Pacific Railway Company</ital>,<footnote>...<cite type="case"><ref p1="Remington Development Corp." p2="Canadian Pacific Railway Company" pre.rptr="2025" post.rptr="244" title="Remington Development Corp. v. Canadian Pacific Railway Company">Remington Development Corp. v. Canadian Pacific Railway Company, 2025 ABCA 244</ref></cite>...</footnote> the Alberta Court...
   
   DETECTION HINT: If you find yourself setting post.rptr="" (empty), that means
   there is NO reporter info in the body text → it is a SHORT citation → use <ital>.
 
3.3.3 ⚠️ ABSOLUTE RULE — STATUTE / LEGISLATION / REGULATION REFERENCES ARE **NOT** CITATIONS:
   A reference to a STATUTE, ACT, or REGULATION is NOT a case citation and must
   NEVER be wrapped in <cite> or <ref> tags. It has no p1/p2 parties and no
   pre.rptr/post.rptr reporter numbers, so the citation rules DO NOT apply to it.

   HOW TO RECOGNISE A STATUTE / LEGISLATION REFERENCE (NOT a case):
   • It is an Act / Statute / Regulation TITLE, e.g. ends with "Act", "Code",
     "Regulation(s)", "Rules", "Law", or has a year in the title
     (e.g. "Intestate Succession Act, 2019").
   • It is followed by a statutory citation form such as:
       R.S.O. 1990, c. S.26, s. 43.1;   S.S. 2019, c. I-13.2, s. 15;
       R.S.N.S. 1989, c. 236, s. 17;    S.M. 2014, c. 27
     i.e. jurisdiction abbreviation + year + "c." (chapter) + optional "s." (section).
   • It does NOT contain "Party1 v. Party2" and has NO court reporter
     (no CarswellXxx, no SCC/ONCA/BCSC, no [year] volume reporter page).

   CORRECT HANDLING:
   • Keep the Act/Statute/Regulation NAME wrapped in <ital> ONLY (if it was
     italic in the input) and leave the statutory reference text as plain text
     after it — exactly matching the sibling statutes around it.
   • If this reference is a tracked insertion, wrap the whole thing in <trace>
     but DO NOT add <cite>/<ref>.

   ✗ WRONG (statute wrongly cite-tagged — never do this):
   <trace><cite finder="manual" type="secondaryxref"><ref normalpub="legref">Succession Law Reform Act , R.S.O. 1990, c. S.26, s. 43.1;</ref></cite></trace>

   ✓ RIGHT (statute kept as <ital> name + plain reference, matching siblings):
   <trace><ital>Succession Law Reform Act</ital>, R.S.O. 1990, c. S.26, s. 43.1;</trace>

   NEVER invent attributes like type="secondaryxref" or normalpub="legref".
   The <cite>/<ref> structure is ONLY for case citations (Section 3.2 full or
   Section 3.3 short) and URLs (see URL rules) — never for statutes/legislation.

3.4 Cross-Format Consistency:
   A. Apply the same citation classification logic consistently throughout the document
   B. Ensure that all instances of the same citation type (full vs. short) receive identical formatting regardless of their original tag structure
   C. When processing case names, prioritize citation rules over preserving original tag structure
   D. Maintain consistent formatting for identical citations throughout the document
 
3.5 Multiple Citation Processing
   * Series of citations (A; B; C) = process each citation individually
   * Maintain exact separator punctuation (;) between citations if provided/as provided
   * Preserve any explanatory text after EACH citation
   * For series format, use: <cite>A</cite>; <cite>B</cite>; <cite>C</cite>

3.6 ⚠️ CRITICAL — PRESERVE PREFATORY TEXT BEFORE CITATIONS:
   Text that appears BEFORE a case name (e.g., "Citing:", "See", "See also",
   "But see", "Compare", "Discussed in", "Following", "Applied in", etc.)
   MUST be preserved OUTSIDE the <cite> tags. NEVER absorb prefatory text
   into the <cite> element or drop it.
   
   ✗ WRONG (prefix "Citing:" lost):
   <cite type="case"><ref ...>Fraser v. Imperial Bank of Canada, 1912 CarswellMan 306...</ref></cite>
   
   ✓ RIGHT (prefix preserved before <cite>):
   Citing: <cite type="case"><ref ...>Fraser v. Imperial Bank of Canada, 1912 CarswellMan 306...</ref></cite>
   
   This applies to ALL prefatory words/phrases. The <cite> tag wraps ONLY the
   citation itself (case name + reporter info). Everything before it stays outside.
