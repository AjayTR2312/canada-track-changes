# US Forms Others Prompt
> **Source**: XML Conversion Insert Instructions
>
> **Purpose**: Convert legal documents into rigorously structured XML output, preserving *all* legal meaning and formatting rules.

---

## 🔐 Model Contract (Follow These Always)

**You must:**
- Follow the *Tag Precedence Hierarchy* and *Tagging Conflict Resolution* rules exactly.
- Preserve every word of source content unless deletion is explicitly instructed.
- Use **structural tags** for new/whole elements; use **`<trace>`** for in-place text edits.
- Apply **`date.updated="1"` and `date.trace="1"`** to the HIGHEST/OUTERMOST structural tag only — never to nested children.
- Never wrap the final XML output in Markdown fences or backticks.

**You must not:**
- Mix structural content inside `<trace>`.
- Drop, re-order, or infer any content not present in the input.
- Apply date attributes to nested structural tags within an already-dated container.

---

## 🧭 Decision Framework (High-Level)
- **New/whole element?** → Structural tag with `date.updated="1" date.trace="1"` on the outermost wrapper only.
- **Edit to existing paragraph/sentence?** → `<trace date.trace="1">` (no `date.updated`).
- **`<readme>` instruction is a deletion?** → Pass through the `<readme>` and content unchanged. Do NOT add XML coding.
- **Case citation (contains "v.")?** → `<cite type="case">` tags.
- **Statute reference?** → `<cite type="lrre">` tags.
- **Square brackets in form content?** → `<inline.instr>` tags.
- **Square brackets in normal paragraph?** → Leave as-is.
- **Standalone short `<b>` on its own line?** → `<head>` element, NOT `<para>`.

---

## 📋 Output Contract
- **Output = Raw XML only** (no Markdown, no comments, no explanation).
- **Whitespace**: keep only what is semantically meaningful; remove accidental doubles.
- **Encoding**: preserve symbols and special characters as-is unless XML requires escaping.
- **Validation**: run the word-count and sequence-preservation checks before output.

---

## 🧱 Required Attributes

| Element type | Attribute required |
|---|---|
| New `<section>`, `<para>`, `<footnote>`, `<list>` | `date.updated="1" date.trace="1"` on outermost only |
| `<update>` tags | `date.trace="1"` only |
| `<trace>` for in-para edits | `date.trace="1"` only (NO `date.updated`) |
| Inside already-dated parent | NO attributes on children |
| `<note.block>` content | NO date attributes |

---

## 🏷️ Tag Precedence Hierarchy (CRITICAL — Apply in This Order)
1. **Specialized Content Tags** (HIGHEST) — Case citations `<cite type="case">`, statutes `<cite type="lrre">`, cross-references `<cite type="secondaryxref">`
2. **Structural Tags** (MEDIUM) — Paragraphs `<para>`, sections `<section>`, lists `<list>`
3. **Format Tags** (LOWEST) — Bold `<bold>`, italic `<ital>`, underline `<underscore>`

When tags overlap: apply specialized tags first, then structural, then formatting. **NEVER omit any layer.**

---

## 🧪 Validation Essentials
Before finalizing output, verify ALL of the following:
- **Citations**: count `v.` occurrences in input; ensure matching `<cite type="case">` pairs in output.
- **Headings** ⚠️: search output for `<bold>` inside `<para.text>` — if 1–3 word ALL CAPS/Title Case text not embedded in flowing sentence → must be a `<head>` element. **This is the #1 error.**
- **Introductory words**: preserve signal words ("See", "But see", "Compare", "Accord", "Cf.", "E.g.", "Contra", "See also", "See generally") **outside** citation tags but **inside** `<trace>` tags. VALIDATION: compare first words of input vs output — ensure none are missing.
- **Punctuation** (MANDATORY): every `:` present in input also appears in output; colons after labels (`Summary:`, `Conclusion:`, `Caution:`) must NOT be deleted.
- **Word Count** (MANDATORY): total words in input (tags stripped) MUST EQUAL total words in output (tags stripped).
- **Word Sequence**: words must appear in IDENTICAL order in output as in input.
- **Formatting tags** (ABSOLUTE): `<b>` → `<bold>`, `<i>` → `<ital>`, `<bi>` → `<boldital>`, `<us>` → `<underscore>`. ALL must be expanded.
- **Leaveout**: double underscore `__` → `<leaveout/>`; `<us>` tags → `<underscore>`.
- **Tags**: all opened tags are properly closed; nesting hierarchy is correct.
- **Square brackets in forms** must be inside `<inline.instr>` tags.

---

## ✅ Critical Do/Don't Recap

| DO | DON'T |
|---|---|
| Use `<trace>` for text changes within existing elements | Use `<para>` for in-paragraph edits |
| Apply date attributes to outermost container only | Apply date attributes to nested children |
| Pass deletion `<readme>` instructions through unchanged | Add XML coding to deletion instructions |
| Convert `<b>` → `<bold>`, `<i>` → `<ital>` always | Leave legacy formatting tags unexpanded |
| Count words before finalizing | Submit without word-count verification |

---

## 📚 Full Rules Reference (Verbatim)

Your task is to convert the content in the document into a structured XML format following these instructions:
XML Content Conversion Instructions
Key Processing Rules:

CRITICAL: TAG ACCURACY AND CLOSURE REQUIREMENTS
   •	Understand the content thoroughly and tag EVERY element accurately according to the syntax instructions
   •	EVERY opened tag MUST be closed properly - no unclosed tags allowed
   •	When content is chunked, maintain accurate continuation across chunks
   •	Verify tag hierarchy and proper nesting before completing the conversion

1. Required Structural Attributes
   Mandatory Attribute Requirements:
   •	ALL new or replacement content must include:
      o	date.updated="1"
      o	date.trace="1"
   •	All <update> tags require date.trace="1"
   
   CRITICAL ATTRIBUTE PLACEMENT RULE - APPLIES TO ALL SYNTAX CONVERSIONS:
   •	Apply BOTH date.updated="1" AND date.trace="1" to the HIGHEST/OUTERMOST structural tag in ANY conversion
   •	DO NOT apply these attributes to nested structural tags within the content (e.g., <head>, <para>, <footnote>, <cite type="case">, etc.)
   •	This rule applies to ALL syntax instructions in this prompt (sections, appendix sections, headings, paragraphs, lists, etc.)
   •	Examples:
      - Paragraph with footnotes: date.updated="1" date.trace="1" on <para> tag ONLY
        <para date.updated="1" date.trace="1"><para.text>Content<footnote>...</footnote></para.text></para>
        NOT on nested <footnote> or inner <para> tags inside footnote.body
      
      - Paragraph with case citations: date.updated="1" date.trace="1" on <para> tag ONLY
        <para date.updated="1" date.trace="1"><para.text>Content with <cite type="case">case citation</cite></para.text></para>
        NOT on nested <cite type="case"> tags
      
      - Standalone footnote: date.updated="1" date.trace="1" on <footnote> tag (outermost)
        <footnote date.updated="1" date.trace="1"><footnote.reference>1</footnote.reference><footnote.body><para>...</para></footnote.body></footnote>
        NOT on nested <para> tags inside footnote.body
      
      - Section with content: date.updated="1" date.trace="1" on <section> tag ONLY
        <section date.updated="1" date.trace="1"><section.front>...</section.front><section.body><para>...</para></section.body></section>
        NOT on nested <para> or inner tags within section.body

2. Citation and Reference Tagging
   Case Citation Requirements:
      •	ALL case citations must be wrapped in <cite type="case"> tags
      •	This includes citations within footnotes, quoted material, and update blocks
      •	Multi-case citations must properly tag each individual citation
      •	Pattern recognition: Look for "v." and reporter references
   Statute Reference Requirements:
      •	ALL statutory references must be wrapped in <cite type="lrre"> tags
      •	Include any KRS, U.S.C., or other code references
      •	Statute references take precedence over other formatting

3. Special Character and Complex Structure Handling
   Square Brackets Handling:
      •	In instructional context: Convert to <inline.instr> tags only in form sections , in normal paragraphs and sections leave them as it is.
      •	In quoted legal text: Preserve exactly as they appear
      •	When in doubt about context, prioritize preserving legal meaning
   Quote Block Structure:
      •	Maintain structural integrity of quote blocks
      •	Preserve footnotes and special formatting within quotes
      •	Never merge quote content into surrounding structural elements
   Ellipses and Special Characters:
      •	Preserve three dots (...) exactly as they appear
      •	In legal citations, maintain exact formatting including punctuation

4. Footnote Structure
   •	Footnotes must maintain complete structure regardless of context
   •	Nested footnotes (within quotes or other structures) need proper formatting
   •	All footnote references must be properly paired with footnote bodies
   •	Footnotes in section headings: When a [n] reference appears in a section title, embed the <footnote> inline inside the <name> tag of <outline.name.block>. See Instruction 29D for full example.

5. Pattern Recognition Examples
   Instruction Recognition:
      •	"Replace X in § 26:28" = Replacement instruction
      •	"Add after footnote in § 21:1" = Addition instruction
      •	"Add at end of Comment in § 26:20" = Addition to existing content
   Comment Replacements:
      •	"Replace the second paragraph of Comment in § 26:28 with:" = Replace instruction
      •	Following content needs proper <para> structure, not update tags
   Footnote Modifications:
      •	"Add at end of footnote 4 in § 21:1" = Addition to existing content
      •	"Replace footnote 1 with:" = Full replacement of footnote

6. Content Preservation Requirements
   •	VERIFY all words in input appear in output (100% preservation)
   •	ESPECIALLY preserve ALL explanatory text following citations
   •	CHECK citation block word count: input = output
   •	NO content dropping permitted under any circumstances
   •	NO HALLUCINATION: Output MUST contain ONLY content from the current input. NEVER copy or reproduce paragraph text from the surrounding XML document context (existing sections, paragraphs with ct.id attributes, or any XML not explicitly in the current instruction). If existing document XML appears in your context window, treat it as read-only reference — do NOT reproduce its paragraph content in your output.

7. Bullet and Formatting Unicode Preservation
   •	ALWAYS preserve bullet point and list marker Unicode values EXACTLY as they appear in input
   •	Common bullet Unicode characters to preserve include:
      * \u2022 (•) - Bullet
      * \u25CF (●) - Black circle
      * \u25CB (○) - White circle
      * \u25A0 (■) - Black square
      * \u25B6 (▶) - Triangle bullet
      * And ANY other bullet/list marker Unicode characters
   •	DO NOT convert these Unicode values to XML entities or alternative representations
   •	Example: If input contains "text \u2022 item" → output must retain "\u2022" exactly as is
   •	This rule applies to ALL bullet and list formatting characters

Cross-Chunk Consistency Requirements:

1. Structure Continuity Between Chunks:
   - When content is split across multiple chunks, maintain EXACT structural consistency
   - Begin each new chunk by examining how the previous chunk ended
   - If a previous chunk ended mid-section or mid-list, continue the SAME structure
   - Use IDENTICAL tag styles, attributes, and designator formats as the previous chunk

2. Tag Closure and Reopening:
   - When a chunk ends mid-structure (like inside a section or list):
      a. Properly close all open tags at the end of the chunk
      b. At the beginning of the next chunk, reopen all necessary tags to maintain structure
      c. Include a comment indicating structure continuation: <!-- Continuing from previous chunk -->
   
3. List Consistency Requirements:
   - CRITICAL: Use the SAME designator style throughout the entire document
   - If the first chunk used designators like "(1)", "(2)", etc., ALL subsequent chunks must use IDENTICAL formatting
   - If numbered lists span across chunks, maintain proper sequential numbering
   - Apply date.updated="1" date.trace="1" attributes consistently to ALL list elements in ALL chunks

4. Section Structure Preservation:
   - When a section spans multiple chunks, maintain the section.body until complete
   - NEVER introduce new section structures unless explicitly instructed
   - Maintain identical attribute patterns across section components in all chunks

5. Previous Chunk Examination:
   - Always review the ending structure of previous chunks before processing
   - If processing a continuation chunk, begin by rebuilding the exact structural context
   - For complex nested structures, maintain the complete hierarchy across chunk boundaries

6. Processing Steps for Chunk Handling:
   - Step 1: Identify if current chunk is continuing a structure from previous chunk
   - Step 2: Rebuild necessary opening tags to match previous chunk's structure
   - Step 3: Process current chunk content with consistent tagging
   - Step 4: Properly close all open tags if chunk ends mid-structure
   - Step 5: Note the ending structural state for next chunk processing

7. Designator Format Registry:
   - Maintain a consistent "designator style registry" across all chunks
   - For each list level, use exactly the same designator style throughout the document:
     * Level 1 lists: Consistent use of either "(1)" or "1" format
     * Level 2 lists: Consistent use of either "(i)" or "i" format
     * Level 3 lists: Consistent use of either "(A)" or "A" format
   - Never change designator styles mid-document or between chunks

8. Tag Precedence and Nesting Rules (HIGHEST PRIORITY):
   A. Specialized Content Processing:
      - ALWAYS apply appropriate specialized tags even when format tags (<bold>, <ital>, etc.) are present:
        * Case citations require <cite> structure
        * Law reviews require <cite type="periodical"> structure 
        * Key Number Digest references require <cite type="topic.key"> structure
        * Cross references require <cite type="secondaryxref"> tags
      - Format tags and specialized tags can be nested: <bold><cite>Smith v. Jones</cite></bold> or <cite>Smith v. <ital>Jones</ital></cite>
   B. Overlapping Tag Resolution:
      - When tags overlap, maintain proper XML hierarchy
      - Process content from inside out - first apply specialized content tags, then apply format tags
      - When multiple nested formats exist, preserve all formatting layers
      - Never omit any formatting layer when multiple formats are present
   C. This rule has highest priority and overrides any contradictory formatting guidelines

Instruction Tag Processing and Enforcement:

1. Instruction Tag Processing:

   ## A. CRITICAL TAG DECISION RULES - ANALYZE EACH INSTRUCTION CAREFULLY:
   
      1. USE <update date.trace="1"> TAGS WHEN:
         • MODIFYING EXISTING CONTENT:
           - Adding text at end/beginning/before/after existing content
           - Adding sentences within paragraphs
           - Replacing sentences within paragraphs
           - Any change that does NOT create a complete new structural element
         
         • KEY INSTRUCTION INDICATORS:
           - "Add at end of section/notes/commentary"
           - "Add before/after [text reference]"
           - "Replace sentence/text"
           - "Insert at/in/within"
           - "Add new citation" / "Add new cites"
           - "Add new sentence"

         ⚠️ COMMON ERROR — "ADD NEW SENTENCE" ALWAYS USES <update>:
           RULE: "Add new sentence" ALWAYS uses <update date.trace="1"> tags, regardless of context (footnotes, citations, length)
           ✓ CORRECT: <update date.trace="1">Content with citation.</update>
           ✗ WRONG:   <para date.updated="1" date.trace="1"><para.text>...</para.text></para>
           DISTINCTION: "Add new sentence" → <update date.trace="1">  |  "Add new paragraph" → <para date.updated="1" date.trace="1">

      2. USE STRUCTURAL TAGS DIRECTLY WHEN:
         • CREATING NEW STRUCTURAL ELEMENTS:
           - Adding new paragraphs
           - Adding new footnotes
           - Adding new sections
           - Replacing entire paragraphs/footnotes/sections
         
         • KEY INSTRUCTION INDICATORS:
           - "Add new paragraph"
           - "Add as [ordinal] paragraph"
           - "Replace paragraph/footnote/section"
           - "Add new footnote"
   
   ## B. CRITICAL EXAMPLES - EXACT PATTERNS TO FOLLOW:
   
      1. REPLACING SENTENCES (Use <update> tags):
         <readme>Replace first sentence of Comment with:</readme>
         <update date.trace="1">A patient alleging medical malpractice must show that: (1) physician owed a duty to the patient; (2) the physician breached the duty by providing services that fell below the medical community's standard of care; (3) the patient was injured; and (4) a causal connection between the breach and the injury.</update>
      
      2. ADDING NEW PARAGRAPHS (Use structural tags directly):
         <readme>Add new paragraphs at end of footnote 1 in § 30:22.</readme>
         <para date.updated="1" date.trace="1"><para.text>The tort of invasion of privacy may be described as every citizen's right to be "let" alone. Private individuals have a right to live their lives without unjustifiable interference about matters with which the public is not necessarily concerned, though exceptions exist. See <cite type="case">McCall v. Courier-Journal and Louisville Times Co., 623 S.W.2d 882 (Ky. 1981)</cite>.</para.text></para>
         <para date.updated="1" date.trace="1"><para.text>Four theories for a plaintiff to seek relief for invasion of privacy: 1) intrusion upon seclusion, i.e., requiring physical intrusion into a person's private affairs; 2) appropriation of another's name or likeness for the defendant's benefit; 3) unreasonable publicity of the plaintiff's private life; and 4) publicity unreasonably placing another in a false light before the public. See <cite type="case">Barbo v. Kroger Co., 2007 WL 2350179 (W.D.Ky. 2007)</cite>. Private citizens retain a more than <ital>de minimis</ital> interest in the confidentiality of the personally identifiable information collected from them by the state, which may be protected from disclosure under the privacy exemption set forth by the <cite type="lrre">Kentucky Open Records Act, KRS 61.870 et seq.</cite></para.text></para>
      
      3. ADDING PARAGRAPH IN SPECIFIC POSITION (Use structural tags directly):
         <readme>Add as second paragraph in footnote 2 after See, e.g. signal in § 34:6.50:</readme>
         <para date.updated="1" date.trace="1"><para.text><cite type="case">Browne v. Poole, 680 S.W.3d 810 (Ky. 2023)</cite> (scoring state bar examination and reporting results are "judicial acts" to which absolute immunity attached); <cite type="case">Howard v. Big Sandy Area Development District, Inc., 626 S.W.3d 466 (Ky. 2020)</cite>; <cite type="case">Bryant v. Louisville Metro Housing Authority, 568 S.W.3d 839 (Ky. 2019)</cite>;</para.text></para>
      
      4. ADDING TEXT TO EXISTING CONTENT (Use <update> tags):
         <readme>Add at end of section § 12:9:</readme>
         <update date.trace="1"><cite type="case">Boston Firefighters Union, Local 718, International Association of Fire Fighters, AFL-CIO v. City of Boston, 491 Mass. 556 (2023)</cite></update>
      
      4a. CRITICAL - "ADD AFTER" OR "ADD BELOW" WITHOUT "NEW" KEYWORD (Use <update> tags):
         When readme says "Add after footnote X" or "Add below text after footnote X" WITHOUT the word "new", use <update> tags:
         
         Pattern Recognition:
         <readme>Add after footnote 47.50 in section 5:50</readme>
         Where a permit has been approved and the owner wants...<fn fnref="47.50.10"><p>E.g. see <IT>Case</IT>...</p></fn>.
         
         CORRECT - Use <update> tags for text with embedded footnote:
         <update date.trace="1">Where a permit has been approved and the owner wants to change the design, an application to amend the permit should be sufficient as opposed to having to seek a new development permit<footnote><footnote.reference>47.50.10</footnote.reference><footnote.body><para><para.text>E.g. see <cite type="case">...</cite></para.text></para></footnote.body></footnote>.</update>
         
         Key Rule: "Add after/below" WITHOUT "new" = adding TO existing content = Use <update> tags
         Only use structural tags directly when instruction explicitly says "Add NEW paragraph/footnote/section"
      
      5. ADDING MULTIPLE PARAGRAPHS TO EXISTING CONTENT:
         <readme>Add at end of section § 12:9:</readme>
         <update date.trace="1"><cite type="case">Boston Firefighters Union, Local 718, International Association of Fire Fighters, AFL-CIO v. City of Boston, 491 Mass. 556 (2023)</cite></update>
         <para date.updated="1" date.trace="1"><para.text>Party seeking preliminary injunction must show: (1) likelihood of success on merits; (2) irreparable harm will result from denial of injunction; and (3) in light of moving party's likelihood of success on merits, risk of irreparable harm to moving party outweighs potential harm to nonmoving party in granting injunction.</para.text></para>
         <para date.updated="1" date.trace="1"><para.text>Preliminary injunction will not be granted if the moving party cannot demonstrate a likelihood of success on the merits. Nonprofit corporation's allegation was insufficient to support associational standing to seek declaratory and injunctive relief.</para.text></para>

   ## C. UNIVERSAL TAG SELECTION RULE - MASTER FRAMEWORK:
      
      Ask yourself: "Is the instruction creating a COMPLETE NEW structural element, or modifying/adding to EXISTING content?"
      
      Category 1 - Use Structural Tags Directly (<para>, <section>, <footnote>, etc.):
      • Contains keyword "NEW" + element name:
        - "Add NEW paragraph"
        - "Add NEW section"
        - "Add NEW footnote"
      • "Replace [complete element]":
        - "Replace paragraph"
        - "Replace section"
        - "Replace footnote"
      • "Retitle" or "Retitled"
      • "Create [element]"
      • Creates standalone structural component
      
      Category 2 - Use <update> Tags:
      • "Add at/to end/start" (adding to existing)
      • "Add after/before/under" (adding to existing without "new")
      • "Replace sentence/text/wording" (replacing part of existing)
      • "Insert/Append/Modify/Update" (modifying existing)
      • Does NOT explicitly say "new"
      
      Decision Test:
      • NEW complete element? → Structural tags
      • Adding TO existing or replacing PART? → <update> tags
      
      VALIDATION: Re-read the instruction to confirm your tag choice matches intent

   ## D. CRITICAL DECISION PROCESS:
      
      1. IDENTIFY THE CORE INSTRUCTION TYPE:
         • Is it MODIFYING existing content? → Use <update> tags
         • Is it CREATING new structural elements? → Use structural tags directly
      
      2. ANALYZE THE SPECIFIC LANGUAGE:
         • "Replace sentence" = <update> tags (modifying within element)
         • "Add new paragraph" = structural tags (creating new element)
         • "Add as [ordinal] paragraph" = structural tags (creating new element)
         • "Add at end of section/notes/commentary" = <update> tags (modifying existing)
      
      3. MULTI-PARAGRAPH HANDLING:
         • First text addition to existing content: <trace> tags
         • Additional paragraphs after first addition: structural tags directly
         • All paragraphs in "Add new paragraphs": structural tags directly

2. CRITICAL ENFORCEMENT RULES - ALWAYS FOLLOW THESE:
   A. INSTRUCTION INTENT RECOGNITION:
      - FOCUS ON INTENT rather than exact wording:
        * Is it adding TO something existing? → <update> tags
        * Is it replacing PART OF something existing? → <update> tags
        * Is it creating or replacing a WHOLE element? → Structural tags directly
      - When in doubt about instruction intent:
        * Default to <update> tags for any addition to existing content
        * Default to structural tags for any complete new elements
      - VALIDATION: Re-read the instruction to confirm your tag choice matches intent

   B. CASE CITATION ENFORCEMENT:
      - EVERY individual case citation MUST be wrapped in its own <cite type="case"> tags
      - Multiple citations in sequence require separate <cite type="case"> tags for each case
      - Example: "<cite type="case">Case1 v. Party1</cite> <cite type="case">Case2 v. Party2</cite>"
      - VALIDATION: Count citations in input = Count <cite type="case"> tags in output

3. UNIVERSAL SUB-COMPONENT REPLACEMENT RULE (CRITICAL - APPLIES TO ALL XML TAGS):
   
   A. CORE PRINCIPLE - PROVIDE EXACTLY WHAT README ASKS FOR:
      
      When readme instruction mentions a SPECIFIC TAG NAME, provide ONLY that exact tag and its contents, NOT any parent or wrapper structures.
      
      GOLDEN RULE: If the readme names a specific XML tag, that's ALL you provide. Never wrap it in parent tags unless explicitly requested.
      
      CRITICAL EXAMPLES:
      
      1. "Replace section body" or "New section body":
         ✓ CORRECT: Provide ONLY the content between <section.body> tags
         ✗ WRONG: Do NOT provide entire <section><section.front>...</section.front><section.body>...</section.body></section>
         
         Example:
         <readme>Replace section body in § 10:5:</readme>
         CORRECT OUTPUT:
         <section.body><para date.updated="1" date.trace="1"><para.text>Content</para.text></para></section.body>
         
         WRONG OUTPUT (DO NOT DO THIS):
         <section><section.front>...</section.front><section.body>...</section.body></section>
      
      2. "Replace note group" or "New note group":
         ✓ CORRECT: Provide ONLY the content between <note.group> tags
         ✗ WRONG: Do NOT provide entire <note.block> structure
         
         Example:
         <readme>Replace note group in § 15:3:</readme>
         CORRECT OUTPUT:
         <note.group note.head="drnotes"><para date.updated="1" date.trace="1"><para.text>Content</para.text></para></note.group>
         
         WRONG OUTPUT (DO NOT DO THIS):
         <note.block><note.group>...</note.group></note.block>
      
      3. "Replace footnote body" or "New footnote body":
         ✓ CORRECT: Provide ONLY the content between <footnote.body> tags
         ✗ WRONG: Do NOT provide entire <footnote> structure with <footnote.reference>
         
         Example:
         <readme>Replace footnote body of footnote 2:</readme>
         CORRECT OUTPUT:
         <footnote.body><para date.updated="1" date.trace="1"><para.text>Content</para.text></para></footnote.body>
         
         WRONG OUTPUT (DO NOT DO THIS):
         <footnote><footnote.reference>2</footnote.reference><footnote.body>...</footnote.body></footnote>
      
      4. "Replace list" vs "Replace list.item":
         • "Replace list" → Provide entire <list>...</list> structure
         • "Replace list.item" → Provide ONLY <list.item>...</list.item>
      
      5. "Replace chapter.body" vs "Replace chapter":
         • "Replace chapter.body" → Provide ONLY <chapter.body>...</chapter.body>
         • "Replace chapter" → Provide entire <chapter> structure including <front> and <chapter.body>
   
   B. HOW TO IDENTIFY WHAT TO PROVIDE:
      
      STEP 1: Read the readme instruction carefully and identify the EXACT tag name mentioned
      STEP 2: Ask yourself: "Is this tag nested inside another tag?"
      STEP 3: Provide ONLY the tag that was named, regardless of its position in the hierarchy
      
      EXAMPLES ACROSS DIFFERENT PATTERNS:
      
      Pattern: "Replace [TAG_NAME]"
      • "Replace section body" → Output: <section.body>...</section.body> ONLY
      • "Replace note group" → Output: <note.group>...</note.group> ONLY
      • "Replace form.para" → Output: <form.para>...</form.para> ONLY
      • "Replace list.item" → Output: <list.item>...</list.item> ONLY
      
      Pattern: "New [TAG_NAME]"
      • "New section body" → Output: <section.body>...</section.body> ONLY
      • "New footnote body" → Output: <footnote.body>...</footnote.body> ONLY
      • "New table.body" → Output: <table.body>...</table.body> ONLY
      
      Pattern: Complete structure request
      • "Replace section" (not "section body") → Output: <section><section.front>...</section.front><section.body>...</section.body></section>
      • "Replace footnote" (not "footnote body") → Output: <footnote><footnote.reference>...</footnote.reference><footnote.body>...</footnote.body></footnote>
      • "Replace list" (not "list.item") → Output: <list>...</list> with all items
   
   C. UNIVERSAL VALIDATION CHECKLIST:
      
      Before providing output, ALWAYS ask yourself:
      1. What EXACT tag name did the readme specify?
      2. Am I providing ONLY that tag (with its contents)?
      3. Am I accidentally wrapping it in any parent/container tags?
      
      WRONG PATTERNS TO AVOID:
      • Readme says "Replace X" → You provide <parent><X>...</X></parent> ✗ WRONG
      • Readme says "New X" → You provide <wrapper><X>...</X></wrapper> ✗ WRONG
      
      CORRECT PATTERN:
      • Readme says "Replace X" → You provide <X>...</X> ✓ CORRECT
      • Readme says "New X" → You provide <X>...</X> ✓ CORRECT
   
   D. COMMON STRUCTURE PATTERNS (Examples Only - Principle Applies to ALL Tags):
      
      Understanding which tags are nested inside others helps you identify what to provide:
      • section contains → section.front, section.body
      • footnote contains → footnote.reference, footnote.body
      • note.block contains → note.group
      • chapter contains → front, chapter.body, chapter.back
      • list contains → list.item
      • form contains → form.para, form.line, form.unit
      • table contains → table.head, table.body
      • ANY parent tag contains → child tags
      
      If readme asks for the CHILD tag → provide only the child, not the parent.

4. INTRODUCTORY WORD PRESERVATION (CRITICAL):
   A. When content begins with introductory words like "See", "In", "But see", etc., followed by citations:
      • ALWAYS preserve these introductory words in the output
      • Place introductory words OUTSIDE citation tags but INSIDE update tags
      • CORRECT: <update date.trace="1">See <cite type="case">Case v. Party</cite></update>
      • INCORRECT: <update date.trace="1"><cite type="case">Case v. Party</cite></update> (missing "See")
   
   B. Common introductory words to preserve include but are not limited to:
      • See
      • In
      • But see
      • Compare
      • Accord
      • Cf.
      • E.g.,
      • Contra
      • See also
      • See generally
   
   C. VALIDATION CHECK:
      • Before finalizing output, compare beginning words of input text with output
      • Ensure no introductory words are missing in the XML conversion
      • This applies to all content types including citations, statutory references, and quotes

   D. FORMATTING PRESERVATION FOR INTRODUCTORY WORDS (CRITICAL):
      • When introductory words appear with <i>, <b>, or <bi> formatting in input, preserve as <ital>/<bold>
      • Do NOT strip formatting from introductory words even when they precede citations
      • CORRECT:   <update date.trace="1"><ital>See also</ital> <cite type="case">Deliz v. Julia, 218 A.D.2d 576</cite></update>
      • INCORRECT: <update date.trace="1">See also <cite type="case">Deliz v. Julia, 218 A.D.2d 576</cite></update>  ❌ (lost italic)

4. COMPLETE TEXT PRESERVATION (CRITICAL PRIORITY):
   A. PRESERVE EVERY WORD in the exact original sequence:
      • NEVER omit ANY words from the input text regardless of position
      • This includes but is not limited to:
         - Introductory words (See, In, But see, etc.)
         - Transitional phrases between citations
         - Explanatory text surrounding references
         - Concluding remarks after citations
         - ANY text regardless of its relationship to tagged elements
      
   B. Proper Citation Tagging While Preserving Context:
      • When tagging citations, statutory references, or other special elements:
         - ONLY the specific citation/reference should be within special tags
         - ALL surrounding text must remain in the output
         - Text before/between/after citations must be preserved in exact order
      • CORRECT: <update date.trace="1">See <cite type="case">Case v. Party</cite> which states that...</update>
      • INCORRECT: <update date.trace="1"><cite type="case">Case v. Party</cite> which states that...</update> (missing "See")
      • INCORRECT: <update date.trace="1">See <cite type="case">Case v. Party which states that...</cite></update> (over-tagging)
      
      • CORRECT EXAMPLE - Long Citation with Explanatory Text:
        Input: <readme>Add at end:</readme> See Martinez v. Cot'n Wash, Inc., 81 Cal. App. 5th 1026, 297 Cal. Rptr. 3d 712 (2022) (holding that purely digital retail websites are not "places of public accommodation").
        
        Output: 
        <readme>Add at end:</readme>
        <update date.trace="1">See <cite type="case">Martinez v. Cot'n Wash, Inc., 81 Cal. App. 5th 1026, 297 Cal. Rptr. 3d 712 (2022)</cite> (holding that purely digital retail websites are not "places of public accommodation").</update>
   
   C. MANDATORY VALIDATION CHECK:
      • WORD COUNT CHECK: Total words in input text MUST EQUAL total words in output text
      • SEQUENCE CHECK: Words must appear in IDENTICAL order in output as in input
      • Before finalizing, compare input and output text with all tags removed to verify no words are missing
      • This validation is REQUIRED for ALL content conversions

XML Syntax and Tag Instructions:

1. Tag Precedence and Nesting Rules (HIGHEST PRIORITY):
   A. Format Tag Expansion:
      - ALWAYS expand basic format tags (<b>, <i>, <bi>, <us>) regardless of context
      - When these tags surround specialized content, expand them BEFORE applying specialized tags
      - Basic format tags must be properly expanded even when they contain citations, footnotes, or other special content
   B. Specialized Content Processing:
      - After expanding format tags, ALWAYS apply appropriate specialized tags:
        * Case citations require <cite> structure
        * Law reviews require <cite type="periodical"> structure 
        * Key Number Digest references require <cite type="topic.key"> structure
        * Cross references require <cite type="secondaryxref"> tags
      - Never skip specialized tag application because of surrounding format tags
   C.  Overlapping Tag Resolution:
      - When tags overlap, maintain proper XML hierarchy
      - Process content from inside out - first apply specialized content tags, then apply format tags
      - When multiple nested formats exist, preserve all formatting layers
      - Never omit any formatting layer when multiple formats are present
   D. This rule has highest priority and overrides any contradictory formatting guidelines

2. URL TAGGING (HIGH PRIORITY):
   * IDENTIFY: All strings starting with "http://", "https://", "www." or containing "doi.org"
   * TAG ALL URLs regardless of location (main text, footnotes, tables, etc.)
   * FORMAT:
     - Standard URLs: <cite finder="legacy" type="url"><ref normalpub="webref">URL_HERE</ref></cite>
     - DOI URLs: <cite finder="manual" type="url"><ref normalpub="webref">URL_HERE</ref></cite>
   * VERIFY: Check all content for untagged URLs before completion
   * NO EXCEPTIONS: This rule applies even within other tags or formatting

3. Case Citations Rules:
3.1 Citation Classification:
   A. Examine each case reference and classify: 
      - If citation contains year, court reference, reporter reference, or decision number → FULL CITATION 
      - If citation contains only the case name → SHORT CITATION
   B. Process each citation independently based solely on its immediate context.
   C. Citation Recognition Requirements:
      - Case citations must be identified and properly formatted regardless of how they appear in the input
      - Search for and identify case citations whether they appear within <i>, <b>, <bi>, <us> tags or their expanded equivalents (<ital>, <bold>, <underscore> etc.)
      - The citation classification rules take precedence over any existing formatting tags
3.2 Full Citation Format:
   When a citation includes elements beyond just the case name:
   Add <cite type="case"> for case citations
   A. <cite type="case">case citation details</cite>
   B. ALL case citations must be wrapped in <cite type="case"> tags regardless of location
      - This includes citations within update blocks, footnotes, and quoted material
   C. Citation Recognition and Formatting Rules:
      - ALL case citations must be wrapped in <cite type="case"> tags, one citation per tag set
      - Citation format typically follows pattern: "Name v. Name, Volume Reporter Page (Court Year)"
      - IMPORTANT: When multiple citations appear in sequence:
         * CAREFULLY identify where one citation ends and another begins
         * Each case MUST have its own separate <cite type="case"> tags even when poorly formatted
         * Look for the pattern "v." to help identify the start of a new citation
      - CRITICAL: Fix common citation formatting issues:
         * If parentheses are unbalanced (missing closing parenthesis), treat as if properly formatted
         * When two citations run together without punctuation, separate them into distinct <cite type="case"> tags
         * Example of citations needing separation:
            Input: "Smith v. Jones, 123 F.3d 456 (2000 Brown v. Green, 789 F.3d 012 (2001)"
            Output: "<cite type="case">Smith v. Jones, 123 F.3d 456 (2000</cite> <cite type="case">Brown v. Green, 789 F.3d 012 (2001)</cite>"   
      - Include ALL subsequent history terms (cert. denied, overruled by, etc.) within the SAME <cite type="case"> tag
      - When multiple cases are cited together, each should have its own <cite type="case"> tags
      - VALIDATION CHECK: For any input containing "v.", verify it's properly tagged within <cite type="case"> tags
   D. Applied Examples in Update Blocks:
      - Input: <readme>Add after Smith cite:</readme> Thompson v. Roberts, 123 F.3d 456, cert. denied, 567 U.S. 890;
      - Output: 
      <readme>Add after Smith cite:</readme>
      <update date.trace="1"><cite type="case">Thompson v. Roberts, 123 F.3d 456, cert. denied, 567 U.S. 890</cite>;</update>
	E. You might find underscored citations like <us>Smith v. Jones</us> or <underscore>Smith v. Jones</underscore>. Treat these as normal citations and wrap the ENTIRE CITATION SEQUENCE in <cite type="case"> tags.
      ⚠️  CRITICAL CITATION RULE: When you see underscored citations like <us>United States ex rel.</us> Trans Coastal Roofing Co. v. David Boland, Inc., 922 F. Supp. 597, 598 (S.D. Fla. 1996), <us>aff'd</us>, 226 F.3d 646 (11th Cir. 2000), you MUST wrap the ENTIRE citation sequence in <cite type="case"> tags, not just the case name. The <cite type="case"> tags must include all parts: case name, reporter citations, page numbers, court references, and any subsequent citations like aff'd citations.

      CRITICAL: The <cite type="case"> tags must wrap the COMPLETE citation including all parts: case name, reporter, page numbers, court/year, and any subsequent citations (like aff'd).
      Examples:
         Input: <us>United States ex rel.</us> Horizon Masonry Corp. v. Greenfield Builders, Inc., 935 F. Supp. 712, 713 (N.D. Tex. 1998), <us>aff’d</us>, 234 F.3d 659 (5th Cir. 2001)
         Correct Output: <cite type="case"><underscore>United States ex rel.</underscore> Horizon Masonry Corp. v. Greenfield Builders, Inc., 935 F. Supp. 712, 713 (N.D. Tex. 1998), <underscore>aff’d</underscore>, 234 F.3d 659 (5th Cir. 2001)</cite>
         Incorrect Output: <cite type="case"><underscore>United States ex rel.</underscore> Horizon Masonry Corp. v. Greenfield Builders, Inc.</cite>, 935 F. Supp. 712, 713 (N.D. Tex. 1998), <underscore>aff’d</underscore>, 234 F.3d 659 (5th Cir. 2001)
   F. Paragraph References: 
      - Use <point> tags for paragraph reference.
      
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
         * PARAGRAPH REFERENCES MUST BE OUTSIDE REF/CITE TAGS:
            - CORRECT: <cite>...</cite> at para 97 and 98
            - INCORRECT: <cite><ref>... at para 97 and 98</ref></cite>
      - Example: 
      ```
      <cite type="case">I.F. v. Gilead Sciences, Inc., 2024 BCSC 480, 2024 CarswellBC 815 (B.C. S.C.) <point>at paras. 139-141</point>, additional reasons 2024 BCSC 1479 (B.C. S.C.)</cite>
      ```
3.3 Short Citation Format:
   A. Definition: A citation is considered "short" when ALL of these apply:
      - An adversarial case name in the form Party1 v. Party2 (or Party1 vs. Party2), AND
      - It contains NO year, court reference, reporter reference, volume number, page number, docket number, or WL/LEXIS number
   B. Non-examples (NOT short citations — do NOT apply short-citation rules):
      - Single-word mentions (e.g., "Emoral", "Musel")
      - Named tests/doctrines (e.g., "Emoral test", "Smith doctrine")
      - Id., supra, infra (unless explicitly defined elsewhere)
   C. Formatting Requirements:
      - Do NOT wrap short citations in <cite type="case"> tags
      - Do NOT add <ital> tags if the input has no italic formatting on the case name
      - ONLY preserve existing formatting from input; never add formatting that was not there
      - Example: "In <ital>Smith v. Jones</ital>, the court held..." (only if input had italic)
      - Example: "Following the <ital>Smith</ital> decision, courts have..." (only if input had italic)
      - NEVER use full <cite type="case"> structure for short citations even if the case was previously fully cited

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
   In <ital>Smith v. Jones</ital>,<fn fnref=1><p><ital>Smith v. Jones</ital>, 123 F.3d 456 (5th Cir. 2020).</p></fn> the court held...
   
   CORRECT Output:
   <para><para.text>In <ital>Smith v. Jones</ital>,<footnote><footnote.reference>1</footnote.reference><footnote.body><para><para.text><cite type="case">Smith v. Jones, 123 F.3d 456 (5th Cir. 2020)</cite>.</para.text></para></footnote.body></footnote> the court held...</para.text></para>
   
   WRONG Output (DO NOT DO THIS):
   <para><para.text>In <cite type="case">Smith v. Jones, 123 F.3d 456 (5th Cir. 2020)</cite>,<footnote>...</footnote> the court held...</para.text></para>
   
   Key Points:
   • Short citation in content = <ital> tags only
   • Full citation in footnote = <cite type="case"> structure
   • Never duplicate the full citation in both locations
 
3.4 Cross-Format Consistency:
   A. Apply the same citation classification logic consistently throughout the document
   B. Ensure that all instances of the same citation type (full vs. short) receive identical formatting regardless of their original tag structure
   C. When processing case names, prioritize citation rules over preserving original tag structure
   D. Maintain consistent formatting for identical citations throughout the document
 
3.5 Multiple Citation Processing
   * Series of citations (A; B; C) = process each citation individually
   * Maintain exact separator punctuation (;) between citations if provided/as provided
   * Preserve any explanatory text after EACH citation
   * For series format, use: <cite type="case">A</cite>; <cite type="case">B</cite>; <cite type="case">C</cite>

3.6 CRITICAL PARAGRAPH VS. SECTION DISTINCTION:
   A. When instructions mention "Add new paragraphs/paragraph" or "Insert new paragraphs/paragraph":
      - ONLY use <para><para.text>Content</para.text></para> structure
      - NEVER include section tags, section.front, section.body, or outline.name.block
      - Do NOT wrap paragraphs in any section structure even if the target location is within a section
      - This rule has NO exceptions when "new paragraphs" is explicitly mentioned

   B. Example of CORRECT paragraph addition:
      <readme>Add/Insert new paragraphs before/after/at [location]:</readme>
      <para><para.text>Content</para.text></para>
      <para><para.text>More content</para.text></para>

   C. Example of INCORRECT paragraph addition (NEVER DO THIS):
      <readme>Add new paragraphs before/after/at [location]:</readme>
      <section>...<section.body><para>...</para></section.body></section>

4.	Add <cite type="topic.key"> for west's reference
   A.	Usually they start with West's
   B.	Examples:
      o	 West's Key Number Digest, Brokers &key;1 to 106
      o	 West's Key Number Digest, Principal and Agent &key;1 to 199

5. Add <inline.instr> to square bracketed text and any placeholders
   A. CRITICAL RULE: Square brackets handling depends on context:
      • In FORM SECTIONS: Wrap with <inline.instr> tags
      • In NORMAL PARAGRAPHS and SECTIONS: Leave square brackets as they appear (do NOT wrap)
      • In QUOTED LEGAL TEXT: Preserve exactly as they appear
   B. The square brackets themselves must be preserved inside the <inline.instr> tags (when used in form sections)
   C. Examples (for FORM SECTIONS only):
      Input: [Name of party]
      Output: <inline.instr>[Name of party]</inline.instr>
      Input: Contract dated [insert date]
      Output: Contract dated <inline.instr>[insert date]</inline.instr>
   D. Apply this rule to all placeholder text, form fields, and instructional content in square brackets IN FORM SECTIONS
   E. When square brackets contain formatted text:
      - The <inline.instr> tag must ALWAYS start before the opening '[' and end after the closing ']'
      - Any formatting tags must be properly nested INSIDE the <inline.instr> tags
      - Example:
        Input: [number of months or</ital> <ital>years]
        Output: <inline.instr>[number of months or</ital> <ital>years]</inline.instr>
        
        INCORRECT (never do this): <inline.instr>[number of months or</inline.instr> <ital>years]</ital>
   F. Bracketed Text Validation Check (CRITICAL):
      • FINAL VALIDATION CHECK: Search output for "[" and "]" characters.
      • ALL square brackets in "Form sections" MUST be inside <inline.instr> tags.
      • This applies even when brackets are partially formatted with italic/bold.
      • EXAMPLE: "<ital>day of month</ital>]" is WRONG
      • CORRECT: "<inline.instr><ital>day of month</ital>]</inline.instr>"

6.	Add <cite type="secondaryxref"> for cross references
   Input:
   § 3:567
   Output:
   <cite type="secondaryxref">§ 3:567</cite>
   Input:
   §§ 2:18, 3:13, 3:474
   Output:
   <cite type="secondaryxref">§§ 2:18, 3:13, 3:474</cite>
7.	<sep/> Tags
   A. Use <sep/> for hyphens, emdashes and punctuation only in section titles.
   B. Use <sep/> to replace dashes in section names.
   C. Retain hyphens, replace dashes and punctuation with <sep/> in section numbers.

8.	Leaveout Placeholder Rules:
   Detect underscore placeholders (variable length) and encode with <leaveout/> according to context.

   A. Detection: Any contiguous run of underscores (`__`, `______`, `_______`, etc.) represents a leaveout. There is no fixed length.

   B. Tagging Rules:
      1) Standalone line (no text before or after on that line) → treat as a heading:
         Input:  ___________
         Output: <head align="l"><name.block><name><leaveout/></name></name.block></head>

      2) Inline within running text → insert self-closing <leaveout/> at that position:
         Input:  A full summary of the ________ Advisory Opinion appears in the document.
         Output: A full summary of the <leaveout/> Advisory Opinion appears in the document.

      3) Replace all blanks (________________) with <leaveout/>.

   C. Validation:
      - Use <leaveout/> (self-closing). Do NOT output </leaveout>.
      - Each underscore run converts to exactly one <leaveout/>.
      - No surrounding text is lost or re-ordered.
      - In the standalone case, the line contains ONLY the placeholder; otherwise use the inline rule.

9. Omit Para Tag
   If you find three astricks "*      *      *" in between any type of paragraphs, add them inside <omit.para> tags.
      Example: <omit.para align="c">*      *      *</omit.para>
      Note: There is no need to add any kind of paragraph tags around <omit.para> tags. They should be alone in a line.
      Incorrect tagging Example: 
      <para><para.text><omit.para align="c">*      *      *</omit.para></para.text></para>
      <quote.para pstart="n"><para.text><omit.para align="c">*      *      *</omit.para></para.text>/quote.para>

10.	Avoid using emdash or dash between <designator> elements.

11.	Ellipsis Handling:
   A.	Preserve three consecutive dots (...) exactly as they appear in the input
   B.	Do not convert to entities or modify spacing
      Example:
      Input: The court stated... and continued
      Output: The court stated... and continued

12. Paragraph Block: <para><para.text>Content including citations/footnotes</para.text></para>

12.1 Add content with Multiple Paragraphs:
   A.	Each paragraph must be individually wrapped in <para><para.text> tags
   B.	Blank lines between paragraphs in input indicate paragraph breaks

13.	Syntax for footnotes:
   A.	<footnote><footnote.reference>Footnote number</footnote.reference><footnote.body><para><para.text>Content</para.text></para><para><para.text>Content</para.text></para></footnote.body></footnote>
   
      **Syntax for footnotes if found in new paragraph:
      <para><para.text>Content<footnote><footnote.reference>Footnote number</footnote.reference><footnote.body><para><para.text>Content</para.text></para><para><para.text>Content</para.text></para></footnote.body></footnote></para.text></para>

      **Do not add the footnote number at the end of a paragraph in the coding.
   
      Example:
      Input: to one the insurer has a duty to defend.17.50
      17.50<cite type="case">Federal Insurance Company v. 3M Company, 2022 WL 17176889 (D. Minn. 2022)</cite>.
      Output: to one the insurer has a duty to defend.<footnote><footnote.reference>17.50</footnote.reference><footnote.body><para><para.text><cite type="case">Federal Insurance Company v. 3M Company, 2022 WL 17176889 (D. Minn. 2022)</cite>.</para.text></para></footnote.body></footnote>

13.1. FOOTNOTE PARAGRAPH STRUCTURE (HIGH PRIORITY):
   * ANY blank line in footnote content = new paragraph
   * Each paragraph REQUIRES separate <para><para.text> structure WITHIN footnote.body
   * COUNT paragraphs in footnote input = COUNT paragraphs in output
   * NEVER combine multiple paragraphs into single <para.text>
   * Example:
     <footnote.body>
       <para><para.text>First paragraph content.</para.text></para>
       <para><para.text>Second paragraph content.</para.text></para>
     </footnote.body>
   * Apply URL tagging within each footnote paragraph

13.2. PARAGRAPH INTEGRITY WITH FOOTNOTES (HIGH PRIORITY):
   * Text with embedded footnotes = ONE paragraph
   * NEVER split paragraphs at footnote boundaries
   * TEST: Remove footnotes - if text flows continuously, keep as ONE paragraph
   * Footnotes NEVER create paragraph breaks
   * ONLY create new paragraphs when:
     - Original text has blank line separations
     - Readme explicitly instructs multiple paragraphs

13.3. TRACE TAG VS PARA TAG USAGE FOR FOOTNOTE MODIFICATIONS:
   When the readme instructs adding/modifying content within a footnote, choice between <update date.trace="1"> and <para date.updated="1" date.trace="1"> depends on the specific instruction:

   * Use <update date.trace="1"> WHEN:
     - Readme explicitly mentions modifying a specific "sentence" or "paragraph" within the footnote
     - You are replacing or adding a sentence within an existing footnote paragraph
     - Example: <readme>Replace first sentence of footnote 5 with:</readme>
       → <update date.trace="1">New sentence text here.</update>

   * Use <para date.updated="1" date.trace="1"> FOR ALL OTHER FOOTNOTE SCENARIOS:
     - Adding content at the beginning or end of a footnote
     - Adding content before or after subheadings within a footnote
     - Adding a new paragraph within a footnote
     - Any footnote modification NOT explicitly targeting a sentence within an existing paragraph
     - Example: <readme>Add at end of footnote 3:</readme>
       → <para date.updated="1" date.trace="1"><para.text>New content here. <cite type="case">Case v. Party, 123 F.3d 456</cite>.</para.text></para>

   ⚠️ COMMON ERROR: Using <update date.trace="1"> when readme says "Add at end of footnote" or "Add before [subheading]"
   These ALWAYS require <para date.updated="1" date.trace="1"> — NOT <update date.trace="1">

14. List Structure Guidelines:
   A. Designator Formats - ALL designator types must be wrapped in <designator> tags:
      - Numbers: <designator>1</designator>, <designator>2</designator>
      - Letters: <designator>a</designator>, <designator>A</designator>
      - Roman numerals: <designator>i</designator>, <designator>I</designator>
      - Symbols: <designator>•</designator>, <designator>-</designator>, <designator>—</designator>
      - Any other format: Use exactly as it appears in the input

   B. Basic List Syntax:
      <list>
        <list.item>
          <name.block>
            <designator>X</designator>  <!-- X represents any designator format -->
            <name>Optional name</name>  <!-- Include only if present -->
          </name.block>
          <para><para.text>Content</para.text></para>
        </list.item>
        <!-- Additional list items follow same structure -->
      </list>

   C. Multiple Paragraphs Within List Items:
      <list.item>
        <name.block><designator>X</designator></name.block>
        <para><para.text>First paragraph</para.text></para>
        <para><para.text>Second paragraph</para.text></para>
      </list.item>

   D. Nested List Structure:
      <list>
        <list.item>
          <name.block>
            <designator>I</designator>
            <name>Main item</name>
          </name.block>
          <para><para.text>Content</para.text></para>
          <list>
            <list.item>
              <name.block><designator>A</designator></name.block>
              <para><para.text>Sub-item</para.text></para>
            </list.item>
          </list>
        </list.item>
      </list>

   E. List Processing Rules:
      • When input contains <li> tags, convert to <list.item> structure
      • For nested list structure, start with <list> as container
      • Nested list.items are NOT allowed
      • Use list.items in list sections (never in form sections)

15.	Syntax of image coding:
   A.	<image.block><image ident="pdf id.pdf" format="pdf"/><image ident="Tiff id.t01" suppress.online="yes"/></image.block>

16.	Syntax for attachment block:
   A.	<attachment.block><attachment><form.para><form.text>Content</form.text></form.para></attachment></attachment.block>

17.	Syntax for quote block:
   A.	<quote.block><quote.para pstart="n"><para.text>Content</para.text></quote.para><quote.para pstart="n"><para.text>para1</para.text></quote.para></quote.block>
   B. Mandatory validation check for quote.para:
      • pstart attribute in the quote.para tag should be equal to "n" only.
   C. We might get nested quote blocks where there would be quote blocks inside another quote block. When you see a nested quote block after a quote para, it means that the nested quote block is part of the previous quote para. So, you need to put the nested quote block inside the previous quote para.
   D. NEVER create a new quote para for nested quote blocks.
      Example:
         <quote.block>
            <quote.para>
               <para.text>secure payment for their services. Specifically,</para.text>
               <quote.block>
                  <quote.para>
                     <para.text>lease required that the defendant approve.</para.text>
                  </quote.para>
               </quote.block>
            </quote.para>
            <quote.para>
               <para.text>A Laborer may not enforce against an owner</para.text>
               <quote.block>
                  <quote.para>
                     <para.text>Before closing the PSA, passing title to the property.</para.text>
                  </quote.para>
               </quote.block>
            </quote.para>
            <quote.para>
               <para.text>Any person furnishing labor, professional services.</para.text>
            </quote.para>
         </quote.block>

18. Syntax for table:
      <tbl><table><tgroup cols="n"><thead><row><entry>Content</entry></row></thead><tbody><row><entry>content</entry></row><row><entry>content</entry></row></tbody></tgroup></table></tbl>
   A. Single row:
      <row><entry>Content</entry></row>
      Input:
         <row>2023 CIRB 1094	<ital>Seaspan ULC</ital>	§ 3:567</row>
      Output:
         <row><entry>2023 CIRB 1094</entry><entry><ital>Seaspan ULC</ital></entry><entry><cite type="secondaryxref">§ 3:567</cite></entry></row>
      Input:
         <row>2023 CIRB 1095	<ital>Dexterra</ital>§§ 2:18, 3:13, 3:474</row>
      Output:
         <row><entry>2023 CIRB 1095</entry><entry><ital>Dexterra</ital></entry><entry><cite type="secondaryxref">§§ 2:18, 3:13, 3:474</cite></entry></row>
   B. Table Header Pattern Recognition (CRITICAL):
      • PATTERN RECOGNITION: Analyze the first 1-2 rows of ANY table to identify header rows
      • Header Row Identification Rules:
         - First row with bold formatting typically indicates a header
         - Rows containing title-like text (ALL CAPS, centered, bold)
         - Text that serves as column identifiers or table titles
         - Text that categorically differs from subsequent content rows
         - Rows with separator lines (---, ===, etc.) following the row
      • ALL identified header rows MUST be placed in <thead> tags, NOT <tbody>
      • This recognition pattern applies to ANY table content regardless of specific text
      • EXAMPLE (structure applies to ALL tables, not just this specific content):
         <tbl>
            <table>
               <tgroup cols="1">
               <thead>
                  <row><entry><bold>ANY TABLE TITLE OR HEADER TEXT</bold></entry></row>
               </thead>
               <tbody>
                  <row><entry>Subsequent content...</entry></row>
               </tbody>
               </tgroup>
            </table>
         </tbl>
      • VALIDATION CHECK: Review ALL tables to ensure header content is properly identified and placed in thead tags.
   
   C. Table Detection Patterns:
      • Tables with explicit <tbl>, <row>, <entry> markers
      • Tables with | or + separators between cells
      • Structured data with aligned columns using spaces
      • Tables marked with grid lines (-, +, |)
      • When encountering any of these patterns, convert to proper <tbl> structure
   
   D. Cell Content Processing:
      • Preserve all formatting tags inside cells (<bold>, <ital>, <underscore>, etc.)
      • Process citations within cells following citation rules (wrap in <cite type="case">, <cite type="lrre">, etc.)
      • Process cross-references within cells (wrap in <cite type="secondaryxref"> tags)
      • Preserve line breaks in cells when present
      • Remove excess whitespace at start/end of cells
      • Apply URL tagging within cells following URL rules
   
   E. Column Count Determination:
      • Set the cols attribute in <tgroup> to the MAXIMUM number of cells found in any row
      • Count all cells including header rows when determining maximum
      • If rows have different cell counts, use the highest count
      • Example: If one row has 3 cells and another has 4 cells, set cols="4"

18.1 Syntax for Correlation and Cross-Reference Tables:
   A. <correlation.tbl> - Correlation table structure
      • Purpose: Show relationships between different versions, statutes, or references
      • Components:
        - <correlation.tbl.front> - Header/introductory material for correlation table
        - <correlation.tbl.block> - Main content block of correlation table
        - <correlation.tbl.group> - Group related correlation entries
        - <correlation.entry> - Individual correlation mapping entry
      • Structure:
        <correlation.tbl>
          <correlation.tbl.front>
            table title and headers
          </correlation.tbl.front>
          <correlation.tbl.block>
            <correlation.tbl.group>
              <correlation.entry>old reference → new reference</correlation.entry>
              <correlation.entry>old reference → new reference</correlation.entry>
            </correlation.tbl.group>
          </correlation.tbl.block>
        </correlation.tbl>

19.	Syntax for feature para or <tip> or Practice tip or Caution:
      Sample readme Instructions:
      <readme>Add as a new Caution after the existing Caution in the section 1:137</readme>
      <readme>Add as a new Practice Tip after the existing Practice Tip in the section 1:137</readme>
      <readme>Add as a new Tip after the existing Tip in the section 1:137</readme>
   A.	<feature.para.block><name.block><name>Heading</name></name.block><para><para.text>Content</para.text></para></feature.para.block>

19.1 Syntax for Inline Formatting Elements:
   A. <super> - Superscript text
      • Usage: For superscript numbers (footnote references, exponents, ordinals)
      • Example: 1<super>st</super>, x<super>2</super>
   
   B. <sub> - Subscript text
      • Usage: For subscript characters (chemical formulas, mathematical notation)
      • Example: H<sub>2</sub>O, x<sub>n</sub>
   
   C. <former> - Former designation or obsolete reference
      • Purpose: Mark content that references old/superseded designations
      • Usage: <former>old citation or reference</former>
   
   D. <revised> - Revised content marker
      • Purpose: Indicate content that has been revised
      • Usage: <revised>updated content</revised>
   
   E. <hist> - Historical note or reference
      • Purpose: Mark historical context, amendments, or legislative history
      • Usage: <hist>historical information</hist>

19.2 Syntax for Special Content Markers:
   A. <online.bluechip.rank> - Online ranking metadata
      • Purpose: Store ranking or priority information for online presentation
      • Usage: <online.bluechip.rank>ranking value</online.bluechip.rank>
   
   B. <online.sortkeys> - Online sorting metadata
      • Purpose: Define sort keys for online database indexing and organization
      • Usage: <online.sortkeys>sort key values</online.sortkeys>

19.3 Author's Note — Placement Rules (CRITICAL):
   "Author's Note" placement in the XML depends entirely on WHERE it appears relative to the form content in the input.

   RULE 1 — Author's Note at START of section (appears BEFORE the form/image):
      Use <feature.para.block>. This is the correct structure when "Author's Note" is the FIRST element in the section body, before any Insert/image or form content.
      Structure:
      <feature.para.block><name.block><name>Author's Note:</name></name.block><para><para.text>Content</para.text></para></feature.para.block>
      Example input:
         Author's Note:
         The form says: "Complete this form if you have completed the Small Claims..."
         Insert FM29.pdf.
      Example output:
         <section.body><feature.para.block><name.block><name>Author's Note:</name></name.block><para><para.text>The form says: &ldquo;Complete this form if you have completed the Small Claims...&rdquo;</para.text></para></feature.para.block><image.block><image ident="FM29.pdf" format="pdf"/></image.block></section.body>

   RULE 2 — Author's Note at END of section (appears AFTER the form/image, under "Notes to Form" or similar):
      Use <note.block><note.group note.head="none"> with an explicit <head> tag containing the Author's Note label in <ital>.
      Structure:
      <note.block><note.group note.head="none"><head align="l"><name.block><name><ital>Author's Note</ital></name></name.block></head><para><para.text>Content</para.text></para></note.group></note.block>
      Example input:
         Insert FM29.pdf.
         Author's Note
         This form says: "This form is only to be used to respond to a Request for Earlier Hearing on Motion(s)..."
      Example output:
         <section.body><image.block><image ident="FM29.pdf" format="pdf"/></image.block><note.block><note.group note.head="none"><head align="l"><name.block><name><ital>Author's Note</ital></name></name.block></head><para><para.text>This form says: &ldquo;This form is only to be used to respond to a Request for Earlier Hearing on Motion(s)...&rdquo;</para.text></para></note.group></note.block></section.body>

   DECISION RULE — How to determine position:
      • If Author's Note text comes BEFORE "Insert [filename]" or before any image/form content → RULE 1 (feature.para.block)
      • If Author's Note text comes AFTER "Insert [filename]" or after image/form content → RULE 2 (note.block)
      • NEVER use note.head="drnotes" for Author's Note — that is only for explicitly labelled "Drafter's Note"
      • NEVER create two separate blocks (one for the heading, one for content) — always keep heading and content together in the same block

20.	Syntax for item references:
   A.	<item.reference><name.block><label>Heading</label><designator>number</designator></name.block><note.group note.head="none"><feature.para.block><name.block><name>Heading</name></name.block><para><para.text>Content</para.text></para></feature.para.block></note.group></item.reference>

20.1	Syntax for Item Reference Block:
   A.	<item.reference.block> - Item reference container  
       Purpose: Contains references to specific items or citations
       Structure: <item.reference.block><item.reference><name.block><label>[label]</label><designator>[number]</designator></name.block><note.group note.head="none"><para><para.text>[content]</para.text></para></note.group></item.reference></item.reference.block>
       Usage: For formal item citations and references

21.	Syntax for Item Selection:
   A.	<item.selection.group><instruction><item.selection><instruction align="c">CONTENT</instruction></item.selection></item.selection.group>

21.1. When the choices between list items are EXPLICITLY marked with [EITHER:]/[OR:]/[END OF CHOICES] pattern:
   A. <item.selection.group type="choice"><instruction>[EITHER:]</instruction><item.selection><form.para><form.text>content</form.text></form.para></item.selection><instruction>[OR:]</instruction><item.selection><form.para><form.text>content</form.text></form.para></item.selection><instruction>[END OF CHOICES]</instruction></item.selection.group>
   B. IMPORTANT: All other conditional content including [OPTIONAL:] content must be wrapped in <inline.instr> tags, NOT selection groups
   C. Example of optional content (correct): <form.text>Content <inline.instr>[OPTIONAL: optional text]</inline.instr> more content</form.text>

21.2. OPTIONAL SECTION GROUP HANDLING ([OPTIONAL:]):
   A. When an entire block of content is marked with "[OPTIONAL:", treat it as a distinct optional selection.
   B. Use <item.selection.group type="optional"> for this pattern.
   C. CRITICAL: The "[OPTIONAL:" and closing "]" text should be converted to <instruction> tags and placed OUTSIDE the <item.selection> tag.
   D. EXAMPLE:
      Input:
         [OPTIONAL:
         7.11 Membership Interests as Joint Tenancy...
         7.12 Life Insurance on Members]
      
      Correct Output:
         <item.selection.group type="optional">
           <instruction>[OPTIONAL:]</instruction>
           <item.selection>
             <form.unit>
               <form.item>
                 <name.block><designator>7.11</designator>...</name.block>
                 ...
               </form.item>
               <form.item>
                 <name.block><designator>7.12</designator>...</name.block>
                 ...
               </form.item>
             </form.unit>
           </item.selection>
         </item.selection.group>

22.	Syntax for Research References:
   A.	<research.reference.block><reference.entry render="all"><ref.text> west's / cross reference </ref.text></reference.entry></research.reference.block>
   B. Research Reference Nesting Rules (context-dependent):
      SCENARIO 1 - When note content AND research references both exist:
      • Place research.reference.block AFTER note.group within the note.block
      • Example structure:
      <note.block><note.group note.head="none"><para><para.text>Content</para.text></para></note.group><research.reference.block><reference.entry render="all"><ref.text> west's / cross reference </ref.text></reference.entry></research.reference.block></note.block>
      
      SCENARIO 2 - When ONLY research references exist (NO other note content):
      • Place research.reference.block DIRECTLY inside the note.block
      • DO NOT include empty note.group elements
      • Example structure:
      <note.block><research.reference.block><reference.entry render="all"><ref.text>West's Key Number Digest, Evidence &key;596</ref.text></reference.entry></research.reference.block></note.block>
   C. General Nesting Requirements:
      • Never place research reference blocks directly in section bodies or other structures without proper note block nesting
      • This nesting requirement applies to all research references regardless of context

22.1	Syntax for Scope Statement Block:
   A.	<scope.statement.block> - Scope statement container
       Purpose: Contains statements about the scope or applicability of content
       Structure: <scope.statement.block><para><para.text>[scope text]</para.text></para></scope.statement.block>
       Usage: Typically appears at beginning of sections to define coverage

22.2	Syntax for Treated Elsewhere Block:
   A.	<treated.elsewhere.block> - Cross-reference to other treatments
       Purpose: Indicates where related topics are covered elsewhere
       Structure: <treated.elsewhere.block><para><para.text>[reference text]</para.text></para></treated.elsewhere.block>
       Usage: Guides readers to related content in other locations

23.	Syntax for Note Block & Note Group:
   A.	<note.block><note.group note.head="none"><para><para.text>Content</para.text></para></note.group></note.block>
   B. Note blocks typically appear after signature blocks
   C. Content appearing after signature blocks must be properly categorized:
      1. Use <attachment.block> for formal supplementary documents such as:
         - Exhibits
         - Schedules
         - Addenda
         - Appendices that are part of the form structure
      2. Use <note.block> for explanatory or reference content such as:
         - Citations
         - Explanatory notes
         - Research references
         - Practice guidance

   Examples:
   - Attachment: <signature.block>...</signature.block><attachment.block><attachment><form.para><form.text>EXHIBIT A: PROPERTY DESCRIPTION...</form.text></form.para></attachment></attachment.block>
   - Note: <signature.block>...</signature.block><note.block><note.group note.head="none"><para><para.text>See <cite type="lrre">26 U.S.C.A. §2010</cite>; Rev. Proc. 2023-34, 2023-48 I.R.B. 1287 (2023).</para.text></para></note.group></note.block>
   D. Headings Within Note Blocks:
   • When headings appear within note blocks, use the following structure:
     <note.block>
       <note.group note.head="none">
         <head><name.block><name>Heading Text</name></name.block></head>
         <para><para.text>Content text</para.text></para>
       </note.group>
     </note.block>
   • NEVER omit the head structure for headings within notes.
   • Use <head> tags with proper <name.block> structure for ALL headings within note blocks.
   • This ensures proper visual distinction and semantic meaning is preserved.
   E. Conditional Note Block After Signature:
   • A <note.block> should only be created after a <signature.block> if there is corresponding note-related content in the input file.
   • Content that triggers the creation of a <note.block> includes, but is not limited to:
      - Text from a <notes> tag
      - Text identified as a "Drafter's Note" or "Practice Note"
      - Text identified as a "Research Reference"
   • If and only if one of these content types is present after a <signature.block>, create the appropriate <note.block> structure around it.
   • If no such note-related content exists following the <signature.block>, you **must not** create an empty <note.block>.
   • VALIDATION CHECK: Verify that any <note.block> following a <signature.block> contains actual content and was not generated from empty space.

23.1. Syntax for Headnote:
   It will explicitly be mentioned in the instruction to add headnote. Sample instruction below.
   Input: <readme>Add new headnote at beginning of the section 2:15</readme> Content
   
   A. Basic syntax for headnote:
      <headnote><note.text>Content</note.text></headnote>
   
   B. Syntax when having multiple paragraphs in headnote:
      When you see multiple paragraphs in headnote instruction, follow the below guidelines:
      1. If each paragraph starts with a bold letter representing a state, then that para should be coded as a separate headnote.
         Example:
         If there are two paragraphs starting with bold letters Wyoming and Fifth Circuit, code them as two separate headnotes.
         <headnote><note.text><bold>Wyoming:</bold> Content for Wyoming</note.text></headnote>
         <headnote><note.text><bold>Fifth Circuit:</bold> Content for Fifth Circuit</note.text></headnote>
      
      2. If paragraphs do NOT start with state identifiers, keep them within ONE headnote:
         <headnote>
         <note.text>First paragraph content.</note.text>
         <note.text>Second paragraph content.</note.text>
         </headnote>

24.	Syntax for Drafters note:
   A.	<note.group note.head="drnotes"><para><para.text>Content</para.text></para></note.group>
   B.  Triggers for note.head="drnotes": Labels explicitly marked as "Drafter's Note" or "Drafters Note" only.
       "Author's Note" is NOT the same as a Drafter's Note — see Instruction 19.3 for Author's Note placement rules.
   C.  CONTENT INTEGRITY (CRITICAL): ONLY include paragraph content that is explicitly present in the current input. NEVER copy, borrow, or carry over paragraph text from the surrounding document XML context. If you see existing document content (recognizable by ct.id attributes or other XML not in the input), ignore it entirely — do not reproduce it in the output.

   	Syntax for practice note:
   A.	<note.group note.head="prnotes"><para><para.text>Content</para.text></para></note.group>

25. Syntax for comment:
   <commentary.note>
   <head><name.block><name>[COMMENTARY TYPE]</name></name.block></head>
   <para><para.text>[Main text content]</para.text></para>
   </commentary.note>

26. Syntax for Heading:
   Heading can be generally identified by the presence of <hac> or <hal> or <har> or <head> tags in the input file. If any of these tags are present, it is a heading and should be coded as such.

26.1 Basic Heading Structure: 
   • <head align="l/c/r"><name.block><name>Heading</name></name.block></head>
   • The align attribute specifies alignment: "l" (left), "c" (center), or "r" (right)
   
   Example:
      Input: <hal><b>FILE NAME</b>
      Output: <head align="l"><name.block><name><bold>FILE NAME</bold></name></name.block></head>

26.2 Common Heading Structure Variations:
   A. Heading with designator:
      Designator Pattern Recognition:
      Step 1: Check if heading text matches designator patterns:
         • Pattern: [Single Letter][.][Space(s)][Remaining Text]
         • Pattern: [Roman Numeral][.][Space(s)][Remaining Text]
         • Pattern: [Number][.][Space(s)][Remaining Text]
      
      Step 2: If pattern matches, SPLIT the text:
         • Everything before and including the period = designator
         • Everything after the period (trimmed) = name
      
      Step 3: Remove the period from designator, keep only the letter/number
      
      Examples of splitting:
         • "B.  Jury Instructions Given" → designator: "B" + name: "Jury Instructions Given"
         • "III. Analysis of Evidence" → designator: "III" + name: "Analysis of Evidence"
         • "1. Introduction" → designator: "1" + name: "Introduction"
      
      Example: <hac>I.  Defendant Green</hac>
      Correct Output: <head align="c"><name.block><designator>I</designator><name>Defendant Green</name></name.block></head>
      Incorrect Output: <head align="c"><name.block><name>I.  Defendant Green</name></name.block></head>
   
   B. Heading with label and designator:
      <head><name.block><label>LABEL</label><designator>DESIGNATOR</designator><name>TITLE</name></name.block></head>
   
   C. Heading with label, designator and endline in name:
      <head><name.block><label>LABEL</label><designator>DESIGNATOR</designator><name><endline/>TITLE</name></name.block></head>
   
   D. Multi-line heading (using endline tags):
      <head><name.block><name>TITLE LINE 1<endline/>TITLE LINE 2<endline/>TITLE LINE 3</name></name.block></head>
   
   E. Container Selection Rule:
      • Use <form.name.block> for document titles in forms
      • Use <head> for section headings in all document types
      • The internal patterns (A-D above) remain identical regardless of container
      • Context determines which container to use, not the internal structure

26.3 Pattern Recognition for Heading Identification:
   A. Visual Indicators:
      • Text is typically formatted distinctly (bold, larger font, ALL CAPS, centered)
      • Often appears at the beginning of sections or document
      • Frequently shorter than surrounding content
      • May contain numbering (I, II, 1., A., etc.) or labels (SECTION, ARTICLE)
   
   B. Contextual Clues:
      • Precedes substantive content blocks
      • Often followed by paragraphs or subheadings
      • May appear after section breaks
      • Document structure suggests hierarchical importance
   
   C. Structural Recognition:
      • Multiple heading components (label + designator + name) indicate heading structure
      • Endline tags may appear anywhere within name element
      • Top-level document titles typically use form.name.block
      • Section headings typically use head tags within appropriate containers
      • Articles and sections in content should be treated as headings

26.4 Multi-Line Heading Rules (applies to ALL variations):
   • Use <endline/> tags for line breaks ANYWHERE within the <name> element
   • When headings span multiple lines, ALWAYS insert <endline/> tags at each line break
      - CORRECT: <head><name.block><name><bold>ARTICLE I.</bold><endline/><bold>DEFINITIONS</bold></name></name.block></head>
      - INCORRECT: <head><name.block><name><bold>ARTICLE I.</bold> <bold>DEFINITIONS</bold></name></name.block></head>
   • NEVER split multi-line headings into separate elements
   • Keep all components within ONE <name> element regardless of structure
   • Maintain formatting tags (bold, italic) properly around each component

26.5 Very Important Validation Check for Headings:
   • Do not omit any <hac>, <hal>, <har>, or <head> tags from the input
   • Each identified heading in the input MUST be represented in the output
   • Do not change the position of headings relative to surrounding content, it should follow the same order as in the input

26.6 Standalone Bold Text Processing — Decision Tree:

   ⚠️ STEP 1: SAME-LINE EXCLUSION TEST (HIGHEST PRIORITY — evaluate this first):
      ❌ NOT A HEADING if the <b> or <bold> tag has ANY text after it on the same line
      Examples that FAIL Step 1 (NOT headings):
         - <b>National Name Protection</b>: As discussed... → NOT a heading (text after on same line)
         - The court in <b>Smith</b> held that... → NOT a heading (embedded in sentence)
         - <b>See</b> also the following cases... → NOT a heading (text follows on same line)
      Only proceed to Step 2 if NO text follows the bold tag on the same line.

   ✅ STEP 2: STANDALONE TEXT LENGTH TEST:
      - Bold text is short (typically 1-5 words)
      - Bold text appears between paragraphs (not embedded in flowing text)
      - Bold text is ALL CAPS or Title Case (e.g., "DISCUSSION", "Analysis of Evidence")
      If all conditions are met → convert to heading

   B. Conversion Rules (when Steps 1 & 2 are both passed):
      Input: <b>Heading Text</b> or <bold>Heading Text</bold>
      Output: <head align="l"><name.block><name><bold>Heading Text</bold></name></name.block></head>

   C. Exceptions (even if Steps 1 & 2 pass, do NOT convert if):
      - Bold text is part of a citation
      - Bold text is part of other structural elements
      - Bold text is followed by a colon → see section 26.7 (HIGHEST PRIORITY override)

26.7 Bold Text with Colon Pattern (HIGHEST PRIORITY):
   A. Recognition Pattern:
      - <b>Text</b>: followed by paragraph content
      - <bold>Text</bold>: followed by paragraph content
      
   B. Required Processing:
      - NEVER convert to separate heading elements
      - ALWAYS keep as part of the paragraph
      - Preserve exactly as: <para><para.text><bold>Text</bold>: content...</para.text></para>
      
   C. This rule OVERRIDES section 26.6 when colon is present
   
   D. Examples:
      Input: <b>National Name Protection</b>: As discussed...
      CORRECT: <para><para.text><bold>National Name Protection</bold>: As discussed...</para.text></para>
      INCORRECT: <head>...</head><para>As discussed...</para>

27.	Syntax for Checklist Section:
   A.	<section><section.front><outline.name.block change.type="new"><label>&sect;</label><designator>section number</designator><name>Section title</name></outline.name.block></section.front><section.body>
      <check.list><head align="l/c/r"><name.block><name>Heading</name></name.block></head>   <!-- Include only if present -->
      <list>
      <list.item><name.block><designator>Number</designator></name.block><para><para.text>Content</para.text></para></list.item>
      </list>
      </check.list></section.body></section>
   B. Note: For instructions mentioning "section body" for checklist sections, refer to Instruction #30 (Section Body Hierarchy Rule) for proper tagging guidance.

28.	Syntax of appendix section:
   A.	<appendix.section><appendix.section.front><outline.name.block toc.include="y"><label>&sect;</label><designator>Number</designator><name><ital>Title</ital></name></outline.name.block></appendix.section.front><appendix.section.body>...</appendix.section.body></appendix.section>
   
   B. Appendix Section Body Content Structure:
      • For headings within <appendix.section.body>, follow Instruction #26 (Syntax for Heading)
      • Headings identified by <hal>, <hac>, <har>, or <head> tags should be converted to <head> tags
      • Content following a heading must be in SEPARATE <para> tags
      • NEVER merge heading text with the following content - they must be separate elements
      
      Example:
         Input:
            <hal><b>FILE NAME</b>: PRO2016-TT-001
         
         Correct Output:
            <head align="l"><name.block><name><bold>FILE NAME</bold></name></name.block></head>
            <para><para.text>PRO2016-TT-001</para.text></para>
         
         Incorrect Output (DO NOT DO THIS):
            <head align="l"><name.block><name><bold>FILE NAME</bold>: PRO2016-TT-001</name></name.block></head>
   
   C. Note: For instructions mentioning "section body" for appendix sections, refer to Instruction #30 (Section Body Hierarchy Rule) for proper tagging guidance - appendix sections use <appendix.section.body> tags.

29. Syntax for Section Front:
   A. <section.front><outline.name.block change.type="new"><label>&sect;</label><designator>section number</designator><name>Section title</name></outline.name.block></section.front>
   B. When instructions specifically request to replace only a section title:
      - Use section.front with appropriate attributes (date.updated="1", date.trace="1")
      - Set change.type="retitled" in the outline.name.block
      - Do not include section.body elements
   C. Example for section title replacement:
      Input: <readme>Replace title of § 26:28 with:</readme> New Title for Section 26:28
      Output:
      <readme>Replace title of § 26:28 with:</readme>
      <section.front><outline.name.block change.type="retitled"><label>&sect;</label><designator>26:28</designator><name>New Title for Section 26:28</name></outline.name.block></section.front>
   D. Footnotes in Section Titles (CRITICAL):
      - When a section heading contains a footnote reference such as [1], [2], etc., the footnote MUST be embedded inline inside the <name> tag.
      - The corresponding footnote body from the bottom of the input is placed as a <footnote> element directly inside <name>, immediately after the text that carries the reference.
      - NEVER drop or omit footnotes that appear in section headings.
      - Structure:
        <name>Section title text<footnote><footnote.reference>1</footnote.reference><footnote.body><para><para.text>Footnote body text</para.text></para></footnote.body></footnote></name>
      - Example:
        Input heading: 10:29.  Response to request for earlier hearing on motion(s)[1]
        Footnote at bottom: [1] JD-FM-289 Rev. 1-26
        Output:
        <section.front><outline.name.block change.type="new"><label>&sect;</label><designator>10:29</designator><name>Response to request for earlier hearing on motion(s)<footnote><footnote.reference>1</footnote.reference><footnote.body><para><para.text>JD-FM-289 Rev. 1-26</para.text></para></footnote.body></footnote></name></outline.name.block>...</section.front>

29.1	Syntax for Changed Hierarchy or Section Title:
   A.	Include date.updated="1" and change.type="retitled" to the <outline.name.block change.type="new"> element

30.	Syntax for Section Body:
   A.	<section.body><head align="l"><name.block><name><bold>section heading</bold></name></name.block></head><para><para.text>Content</para.text></para></section.body>
   B. Section Body Hierarchy Rule (CRITICAL):
      • When a <readme> instruction mentions "section body" (e.g., "Replace section body", "New section body", "Add to section body"), ONLY body hierarchy tags must be applied to the content.
      • This rule applies to ALL section types with the following body tag usage:
         - Normal sections: use <section.body>
         - Checklist sections: use <section.body>
         - Form sections: use <section.body>
         - Appendix sections: use <appendix.section.body> (ONLY appendix uses different tag)
      • Body hierarchy tags include: <para>, <list>, <head>, <note.block>, <quote.block>, <table>, <form.para>, <form.line>, <check.list>, etc.
      • DO NOT apply section-level tags such as <section.front>, <outline.name.block>, <appendix.section.front> when the instruction specifically mentions "section body"
      • The content should be structured using ONLY the elements that belong inside the respective body container
   C. Section Body Wrapping Rule (CRITICAL):
      • When <readme> instruction says "Replace section body" or "New section body", the content MUST be wrapped in opening and closing section body tags
      • Wrapping format depends on section type:
         - Normal/Checklist/Form sections: <section.body>...content...</section.body>
         - Appendix sections: <appendix.section.body>...content...</appendix.section.body>
      • Examples:
         <readme>Replace section body in § 10:5:</readme>
         <section.body>
            <para><para.text>Content here</para.text></para>
            <list>...</list>
         </section.body>
         
         <readme>New appendix section body:</readme>
         <appendix.section.body>
            <para><para.text>Content here</para.text></para>
            <head><name.block><name>Heading</name></name.block></head>
         </appendix.section.body>
         
         <readme>Replace form section body:</readme>
         <section.body>
            <form.para><form.text>Form content here</form.text></form.para>
         </section.body>

31.	Section Format Conversion Guidelines:
   A. Basic section syntax:
      <section><section.front><outline.name.block change.type="new"><label>&sect;</label><designator>section number</designator><name>Section title</name></outline.name.block></section.front><section.body>...</section.body></section>
   B. When you encounter Input Format Type 1 (Standard section with [New] designation):
      Input example: § 2B:15 Merger of limited liability company into S corporation-Letter of intent and admission of new shareholder [New]   
      Convert to: <section><section.front><outline.name.block change.type="new"><label>&sect;</label><designator>2B:15</designator><name>Merger of limited liability company into S corporation-Letter of intent and admission of new shareholder</name></outline.name.block></section.front><section.body>...</section.body></section>
   C. When you encounter Input Format Type 2 (Form section with separator and [NEW] designation):
      Input example: § 670.101 Form 3.6 Bank-Customer EFT Agreement-Suspension of EFT services and access [NEW]
      Convert to: <section><section.front><outline.name.block change.type="new"><label>&sect;</label><designator>670.101 Form 3.6</designator><name>Bank-Customer EFT Agreement<sep/>Suspension of EFT services and access</name></outline.name.block></section.front><section.body>...</section.body></section>
   D. When you encounter Input Format Type 3 (Specialized label section with form number):
      Input example: VAMS § 82.210 Form 1. Notice to constitutional charter city of a population of one hundred thousand or more of injury caused by defect or unsafe condition of or on any bridge, boulevard, street, sidewalk or thoroughfare
      Convert to: <section><section.front><outline.name.block toc.include="y"><label>VAMS &sect;</label><designator>82.210 Form 1</designator><name>Notice to constitutional charter city of a population of one hundred thousand or more of injury caused by defect or unsafe condition of or on any bridge, boulevard, street, sidewalk or thoroughfare</name></outline.name.block></section.front><section.body>...</section.body></section>

32.	Syntax for section with heading:
   A.	<section><section.front><outline.name.block change.type=“new”><label>&sect;</label><designator>section number</designator><name>Section title</name></outline.name.block></section.front><section.body><head align="l"><name.block><name><bold>section heading</bold></name></name.block></head><para><para.text>Content</para.text></para><para><para.text>Content</para.text></para><para><para.text>Content</para.text></para><para><para.text>Content</para.text></para></section.body></section>

   B. Example with heading and citation:
   Input:
   <readme>Add as new section FCR52.5:1:</readme>
   <bi>Add the heading:</bi><b> Expert Reports /Objections</b>
   <i>McCain Foods Limited v. J.R. Simplot Company</i>, 2023 FC 1480 (F.C.)--Rule 52.5 requires that objections to expert reports be made as early as possible. The Rule does not require that a motion to determine the admissibility of an expert report also be brought as early as possible. While case management judges have the ability to strike expert evidence on a preliminary motion, that discretion should be exercised with great restraint. If it is debatable whether the test in <i>R. v. Mohan</i>, 1994 SCC 80, [1994] 2 S.C.R. 9 (S.C.C.) test has been met, that debate should be resolved by the trial judge.
   
   Output:
   <readme>Add as new section FCR52.5:1:</readme>
   <section><section.front><outline.name.block><label>&sect;</label><designator>FCR52.5:1</designator><name> Expert Reports /Objections</name></outline.name.block></section.front><section.body><head align="l"><name.block><name><bold>Expert Reports /Objections</bold></name></name.block></head><para><para.text><cite type="case">McCain Foods Limited v. J.R. Simplot Company, 2023 FC 1480 (F.C.)</cite>--Rule 52.5 requires that objections to expert reports be made as early as possible. The Rule does not require that a motion to determine the admissibility of an expert report also be brought as early as possible. While case management judges have the ability to strike expert evidence on a preliminary motion, that discretion should be exercised with great restraint. If it is debatable whether the test in <cite type="case">R. v. Mohan, 1994 SCC 80, [1994] 2 S.C.R. 9 (S.C.C.)</cite> test has been met, that debate should be resolved by the trial judge.</para.text></para></section.body></section>

32.1 Section Structure for Paragraph Additions (CRITICAL):
   A. When adding paragraphs to an existing section:
      - Use ONLY <para><para.text>Content</para.text></para>
      - NEVER include any section tags (<section>, <section.body>, etc.)
      - Do NOT wrap paragraphs in any section structure
      
   B. Include section tags ONLY when:
      - Explicitly creating a new section
      - The instruction specifically mentions "Insert new section" or "add new section" or "Replace section"
   
   C. Example of paragraph addition to existing section:
      Input:
      <readme>Add as a new paragraph at end of § 21:1:</readme>
      The tort of invasion of privacy may be described as every citizen's right to be "let" alone.
      
      Correct Output:
      <readme>Add as a new paragraph at end of § 21:1:</readme>
      <para date.updated="1" date.trace="1"><para.text>The tort of invasion of privacy may be described as every citizen's right to be "let" alone.</para.text></para>
      
      Incorrect Output (DO NOT DO THIS):
      <section><section.body><para><para.text>The tort of invasion of privacy may be described as every citizen's right to be "let" alone.</para.text></para></section.body></section>

   D. Added content that ITSELF starts with a bolded, titled line (CRITICAL):
      - A bolded sub-topic line at the start of new content being added to an EXISTING
        section (e.g. "The Bankruptcy Administration Improvement Act of 2025 (BAIA)")
        is NOT, by itself, grounds for a new <section> — the same rule in 32.1.B still
        applies: only create a <section> when the instruction explicitly says so.
      - A bolded line with NO numeric § designator is a sub-heading, per Instruction 26
        (Syntax for Heading): <head align="l"><name.block><name><bold>Title</bold></name></name.block></head>
      - NEVER invent a <designator> for it, and NEVER reuse/duplicate the designator of
        the section it is being added to — a sub-heading has no designator at all.
      - Example:
        Input:
        <readme>Add as a new paragraph at end of § 1:39:</readme>
        The Bankruptcy Administration Improvement Act of 2025 (BAIA)
        This act was passed by Congress and signed into law by President Trump...
        Correct Output:
        <readme>Add as a new paragraph at end of § 1:39:</readme>
        <head align="l" date.updated="1" date.trace="1"><name.block><name><bold>The Bankruptcy Administration Improvement Act of 2025 (BAIA)</bold></name></name.block></head><para date.updated="1" date.trace="1"><para.text>This act was passed by Congress and signed into law by President Trump...</para.text></para>
        Incorrect Output (DO NOT DO THIS):
        <section><section.front><outline.name.block><designator>1:39</designator><name>The Bankruptcy Administration Improvement Act of 2025 (BAIA)</name></outline.name.block></section.front><section.body><para><para.text>This act was passed by Congress and signed into law by President Trump...</para.text></para></section.body></section>

32.2 Reserved Sections:
   When instructions specify reserving a range of sections, use the reserved section structure.

   Recognition Pattern:
   - Instructions contain "reserve" keyword with section range
   - Pattern: "reserve the sections from X to Y" or similar variations

   Syntax:
   <section.reserved range="y">
   <outline.name.block>
   <label>&dblsect;</label>
   <designator>SECTION_RANGE</designator>
   </outline.name.block>
   </section.reserved>

   Example:
   Input: <readme>reserve the sections from 1:34 to 1:39</readme>
   Output:
   <section.reserved range="y">
   <outline.name.block>
   <label>&dblsect;</label>
   <designator>1:34 to 1:39</designator>
   </outline.name.block>
   </section.reserved>

   Key Points:
   - The structure is static except for the designator content
   - Extract the section range from the instruction (e.g., "1:34 to 1:39")
   - Use &dblsect; entity for the label (double section symbol)
   - Always include range="y" attribute
   - Do NOT add date.updated or date.trace attributes to reserved sections

33.    Syntax for Section Block:
   A.	<section.block><section><section.front><outline.name.block change.type="new"><label>&sect;</label><designator>section number</designator><name>Section title</name></outline.name.block></section.front><section.body><head align="l"><name.block><name><bold>section heading</bold></name></name.block></head><para><para.text>Content</para.text></para><para><para.text>Content</para.text></para><para><para.text>Content</para.text></para><para><para.text>Content</para.text></para></section.body></section></section.block>
   B. Section Block Usage Requirements (CRITICAL):
      • <section.block> is MANDATORY when multiple sections exist within a logical container such as:
         - Chapter
         - Analytical section
         - Topic
         - Part
         - Any other hierarchical container
      • The <section.block> wraps ALL multiple sections together as a group within that container
      • This ensures proper structural hierarchy and organization in the XML output
   C. Note: For instructions mentioning "section body" replacement or new section body, refer to Instruction #30 (Section Body Hierarchy Rule) for proper wrapping and tagging guidance.
34.	Syntax for caption block:
   A.	<caption.block ref="manual"><form.line align="c">Heading</form.line><tbl><table tabstyle="formcol"><tgroup cols="3"><colspec colnum="1" align="center" colname="1" colwidth="1*"></colspec><colspec colnum="2" align="center" colname="2" colwidth="*"></colspec><colspec colnum="3" align="left" colname="3" colwidth="1*"></colspec><tbody><row><entry>Content</entry><entry>Content</entry></row><row><entry>content</entry><entry>content</entry></row></tbody></tgroup></table></tbl></caption.block>

35.	Syntax for Online View Tag Instructions:
   A.	<online.view><online.view.item>FMS</online.view.item><online.view.item>REL</online.view.item>...</online.view>

36.	Syntax for Form Name:
   A.	<form.name.block><name>CONTENT</name></form.name.block>
   B. Multi-Line Document Titles:
      • For multi-line document titles (especially in operating agreements, contracts, etc.), use <endline/> tags between title components within the <name> element
      • Example:
         <form.name.block><name>OPERATING AGREEMENT<endline/>OF<endline/><inline.instr>[NAME]</inline.instr>, LLC</name></form.name.block>
      • NEVER split multi-line titles into separate form.name.block or form.line elements
      • All title components stay within a SINGLE <name> tag regardless of formatting or line breaks
      • When bold formatting appears across multiple lines, maintain it properly around each component
   C. Document Title Completeness Requirements (HIGH PRIORITY):
      • ALL components that form part of a document title MUST be included within a SINGLE <form.name.block> element
      • Document titles often include multiple parts with different formatting across multiple lines
      • Text appearing at the document top such as "[Manager-Managed]" is PART OF THE TITLE
      • ALWAYS use <endline/> tags between title components
      • NEVER separate document title components into separate form.line elements
      • EXAMPLE (CORRECT):
         <form.name.block><name>OPERATING AGREEMENT<endline/>OF<endline/><inline.instr>[NAME]</inline.instr>, LLC<endline/><bold>[Manager-Managed]</bold></name></form.name.block>

37.	Syntax for Form Paragraph:
   A.	<form.para><form.text>Content</form.text></form.para>

38. Syntax for Address Block:
   A. Basic Structure:
      <address.block position="l">
      <address.item><item.text>Line 1 content</item.text></address.item>
      <address.item><item.text>Line 2 content</item.text></address.item>
      <address.item><item.text>Line 3 content</item.text></address.item>
      </address.block>
   
   B. Required Elements:
      • Each address line must be wrapped in <address.item><item.text> tags
      • Default position is "l" (left-aligned)
      • Each line of the address gets its own address.item wrapper
   
   C. Example:
      Input:
         ABC Corporation
         123 Main Street
         New York, NY 10001
         Phone: 212-555-0100
      
      Output:
         <address.block position="l">
         <address.item><item.text>ABC Corporation</item.text></address.item>
         <address.item><item.text>123 Main Street</item.text></address.item>
         <address.item><item.text>New York, NY 10001</item.text></address.item>
         <address.item><item.text>Phone: 212-555-0100</item.text></address.item>
         </address.block>

39. Address Block vs Form.line vs Form.para Decision Guide:

39.1 When to Use <address.block>:
   A. Identifying Address Content:
      • Use when content represents a complete physical or contact address
      • Typical components: Organization name, street address, city/state/postal code, phone, fax, email, URL
      • Multi-line format where each line is a distinct address component
      • Often appears at document beginning or in letterhead-style sections
   
   B. Structure Requirements:
      • Wrap entire address in <address.block position="l"> tags
      • Each line gets its own <address.item><item.text> wrapper
      • Include all contact information lines (even multiple emails or phone numbers)
      
   C. What to Include:
      • Organization/company names
      • Street addresses (can be multiple lines)
      • City, state, postal code
      • Phone/fax numbers (include labels like "Phone:", "Fax:")
      • Email addresses (multiple allowed)
      • Websites/social media handles
   
   D. What to Exclude (stop immediately when you see):
      • Document labels ("Press Release", "Memorandum", "Notice")
      • Status indicators ("Unofficial", "Confidential", "Draft")
      • Reference numbers ("No. 2025/36", "Ref: ABC-123")
      • Dates that are part of document metadata
   
   E. Placement Rules:
      • In sections: Place inside <section.front> but before <section.body>
      • In forms: Can appear after <form.name.block> or within form body
      • NEVER break up address components with other tags

   F. Example:
      Input:
         Ministry of Trade & Commerce
         Tower Building, 5th Floor
         Wellington Street, Auckland 1010
         Contact: trade.enquiries@govt.nz
         Help Line: +64-9-555-7890
      
      Output:
         <address.block position="l">
         <address.item><item.text>Ministry of Trade &amp; Commerce</item.text></address.item>
         <address.item><item.text>Tower Building, 5th Floor</item.text></address.item>
         <address.item><item.text>Wellington Street, Auckland 1010</item.text></address.item>
         <address.item><item.text>Contact: trade.enquiries@govt.nz</item.text></address.item>
         <address.item><item.text>Help Line: &plus;64-9-555-7890</item.text></address.item>
         </address.block>

39.2 When to Use <form.line>:
   A. Use for standalone single lines in forms:
      • Addressing labels in forms (To:, From:, RE:, Date:)
      • Signature lines
      • Form headers with specific alignment
      • Labels that appear on their own lines
      • Fill-in-the-blank lines with underscores
   
   B. Key Difference from address.block:
      • form.line = Single labeled fields or placeholders in FORMS
      • address.block = Complete structured contact information
   
   C. When NOT to use:
      • Do NOT use for complete addresses (use address.block)
      • Do NOT use for form titles (use form.name.block)
      • Do NOT use for multi-line substantive content (use form.para)

   D. Example:
      <form.line align="l">To: <inline.instr>[Name of recipient]</inline.instr></form.line>
      <form.line align="l">Date: <leaveout/></form.line>

39.3 When to Use <form.para>:
   A. Use for substantive content paragraphs:
      • Multi-line text blocks
      • Body text that forms main content
      • Content within hierarchical structures (<form.item>)
      • Any text that continues flow of thought
   
   B. Key Difference:
      • form.para = Narrative/substantive content
      • form.line = Labels, fields, single-line elements
      • address.block = Structured contact addresses
   
   C. Attributes:
      • Default has indented first line
      • Use first.line="none" for non-indented paragraphs

39.4 Decision Tree (in order of priority):
   1. Is it a COMPLETE ADDRESS with organization/street/contact info? → <address.block>
   2. Is it in a FORM and is a label/field/single line? → <form.line>
   3. Is it SUBSTANTIVE PARAGRAPH content? → <form.para>
   4. EXCEPTION: When external container tags (<tbl>, <fc>, <ab>) are present, container structure takes precedence

39.5 Critical Examples:
   ✓ CORRECT - Complete address:
   <address.block position="l">
   <address.item><item.text>ABC Corporation</item.text></address.item>
   <address.item><item.text>123 Main Street</item.text></address.item>
   <address.item><item.text>New York, NY 10001</item.text></address.item>
   </address.block>
   
   ✓ CORRECT - Form addressing labels:
   <form.line align="l">To: <inline.instr>[Recipient Name]</inline.instr></form.line>
   <form.line align="l">From: <inline.instr>[Sender Name]</inline.instr></form.line>
   
   ✓ CORRECT - Paragraph content:
   <form.para><form.text>This agreement is entered into on this date between the parties as described below.</form.text></form.para>
   
   ✗ INCORRECT - Don't use form.line for complete addresses:
   <form.line>ABC Corporation</form.line>
   <form.line>123 Main Street</form.line>

39.6 Special Case - Container Precedence:
   When address content appears inside <tbl>, <fc>, or <ab> tags in the input, maintain the container structure and use table/entry format rather than address.block:
   
   Input: <tbl>To: [Name], [Address]</tbl>
   Output: <tbl><table><tgroup cols="1"><tbody><row><entry>To: <inline.instr>[Name]</inline.instr></entry></row><row><entry><inline.instr>[Address]</inline.instr></entry></row></tbody></tgroup></table></tbl>

39.7 Form Element Type Decision Rules:
   A. Form Title/Heading Decision:
      • For titles at the top of forms: ALWAYS use <form.name.block><name>TITLE</name></form.name.block>
      • NEVER use <form.line align="c"> for form titles - this is a critical error
      • Default alignment is center; use align="l" or align="r" for different alignments
   
   B. Major Section Headings Within Forms:
      • Use <head> with <name.block> structure:
        <form.item>
          <head><name.block><designator>I</designator><name>HEADING</name></name.block></head>
          <!-- content -->
        </form.item>
      
   C. Standard Item Headings (Numbered/Lettered Items):
      • Use direct <name.block> within <form.item>:
        <form.item>
          <name.block><designator>1</designator><name>Item Title</name></name.block>
          <form.para><form.text>Content...</form.text></form.para>
        </form.item>

40.	Syntax for Form Selection:
   A.	<form.selection.group><instruction><form.selection><instruction align="l">CONTENT</instruction></form.selection></form.selection.group>

40.1. Form Selection Group Structure:
   A. Proper Nesting Structure:
      <form.selection.group type="choice">
        <instruction align="c">[EITHER:]</instruction> <!-- OUTSIDE <form.selection> -->
        <form.selection>
          <form.para><form.text>Content option 1</form.text></form.para>
        </form.selection>
        <instruction align="c">[OR:]</instruction>
        <form.selection>
          <form.para><form.text>Content option 2</form.text></form.para>
        </form.selection>
        <instruction align="c">[END OF CHOICES]</instruction>
      </form.selection.group>
      
   B. CRITICAL ERRORS TO AVOID:
      • NEVER place <instruction> tags inside <form.selection> tags
      • NEVER use multiple <form.selection.group> tags for one selection
      • Instructions MUST go between group and selection tags
      
   C. Valid type= values: "alternate", "optional", "example", "choice"

41. Signature Block Enhanced Structure:
   A. Standard Pattern:
      <signature.block ref="manual">
        <signature.group position="r"> <!-- Default position is right -->
          <form.vertical.space amt="1"/> <!-- Always include before signature line -->
          <form.line align="l" lineover="y">Signature line content</form.line>
        </signature.group>
      </signature.block>
      
   B. Critical Requirements:
      • ALWAYS include <form.vertical.space amt="1"/> before the signature <form.line> inside signature block.
      • Use lineover="y" for lines requiring a physical signature.
      • Use size="large" for any <leaveout/> tags meant for signature fields.
      • Standard alignment is position="r" for the group and align="l" for the line.

   C. Critical Validation (To Prevent Errors):
      • **NON-NEGOTIABLE RULE: DO NOT GENERATE EMPTY SIGNATURE BLOCKS.**
      • A `<signature.block>` is considered "empty" and is **invalid** if its `<form.line>` tag would contain no text content.
      • Before finalizing your output, you **must** verify that any `<signature.block>` you created is justified by content in the source document and will NOT result in an empty `<form.line>`.
      • If boilerplate text like "IN WITNESS WHEREOF..." exists without any actual signature line content (e.g., underscores, a name, a title), **you must not** create a signature block.

42. Syntax for Form Caption:
   A.	<form><form.line align="c">Heading</form.line><caption.block><form.line align="l" lineover="n"><inline.instr>Content</inline.instr></form.line>...</caption.block></form>

43.	Syntax for Form Sections with Online Views:
   A.	<section.front><outline.name.block change.type="new"><label>&sect;</label><designator>Section number</designator><name>Section name</name></outline.name.block><online.view.item>FMS</online.view.item></section.front>

44. Syntax for Form unit:
   A. <form.unit><form.item><name.block><designator></designator><name>Section Title/Heading</name></name.block><form.para><form.text>Content text (not section titles)</form.text></form.para></form.item></form.unit>
   B. Form Item Usage Rules (CRITICAL):
      • Nested form.items ARE allowed (unlike list.items)
      • FORMS sections should NOT contain list.items - use form.items instead
      • NEVER use <list> or <list.item> tags within form sections
      • Use form.items in form sections exclusively
   C. IMPORTANT: Section Title Structure Guidelines:
      - Form document HIERARCHY determines proper heading structure:
      * Document titles: Use <form.name.block> at document beginning
      * Major section headings (with labels like "SECTION ONE"): Use <form.item> with <head> and appropriate <name.block>
      * Subsection headings and simple list items: Use <form.item> with direct <name.block>
      - The ENTIRE section heading must stay in ONE name element regardless of structure used
      - When multi-line section headings require line breaks, use <endline/> tags within the <name> element
   C. Heading Structure Decision Rules:
      - Use <head> elements for major section headings that require specific formatting or alignment
      - Use direct <name.block> for simple subsection headings and list items
      - When external container tags (<tbl>, <fc>, <ab>) are present, those container structures take precedence
      - For address-like content (recipient info, date lines, "RE:" lines), use <form.line> rather than heading structures or form.para
   D. Name Block Content Limits:
      • <name.block><name> elements must ONLY contain titles/headings
      • NEVER include paragraph-length content in name elements
      • Long text belongs in <para><para.text> or <form.para><form.text> elements

44.1 Form.unit Hierarchy Guidelines:
   A. When to Use <form.unit>:
      • ONLY use when grouping multiple related items at the same level
      • DO NOT use when there is only one item at that level
      • Required for proper nesting of hierarchical content
      
   B. Proper Nesting Pattern:
      <form.unit>
        <form.item> <!-- First-level item -->
          <name.block><designator>1</designator><name>Main Item</name></name.block>
          <form.para><form.text>Content...</form.text></form.para>
          <form.unit> <!-- Contains sub-items -->
            <form.item> <!-- Second-level item -->
              <name.block><designator>(a)</designator><name>Sub-item</name></name.block>
              <form.para><form.text>Sub-content...</form.text></form.para>
            </form.item>
          </form.unit>
        </form.item>
      </form.unit>
      
   C. Same-line Attribute Usage:
      • When a lower-level item begins on same line as higher-level item:
        <form.item>
          <name.block><designator>(b)</designator></name.block>
          <form.unit>
            <form.item sameline="yes"> <!-- Critical attribute! -->
              <name.block><designator>(i)</designator></name.block>
              <form.para><form.text>Content...</form.text></form.para>
            </form.item>
          </form.unit>
        </form.item>
44.2 CRITICAL FORM.UNIT STRUCTURE RULES:

1. NEVER close a form.unit tag until ALL nested form.item elements within that unit are complete
2. When processing chunks, if a form.unit spans multiple chunks:
   - Close form.unit at end of chunk ONLY if no more items follow
   - Reopen form.unit at start of next chunk if structure continues
3. Each form.unit must contain at least one form.item
4. Form.items cannot exist outside of form.unit containers

VALIDATION CHECKS:
- Before closing any form.unit, verify no more form.items follow at that nesting level
- Maintain consistent hierarchy: form.unit > form.item > form.para
- Never interrupt form.unit structure with other elements unless explicitly required

CHUNK BOUNDARY HANDLING:
- At chunk end: Only close form.unit if no continuation expected
- At chunk start: Rebuild exact structural context from previous chunk
- Use structural comments to track nesting state across chunks

44.3 CRITICAL FORM.UNIT PROCESSING RULES:

1. FORM.UNIT STRUCTURE PRESERVATION:
   - When processing chunks, NEVER add continuation comments in the output
   - Maintain exact form.unit nesting without structural modifications
   - If input starts with <form.unit>, preserve it exactly as the top-level container

2. FORM.UNIT CLOSURE VALIDATION:
   - Before writing </form.unit>, verify:
     * All form.item elements in this unit are complete
     * No more form.items follow at the same nesting level
     * The unit contains at least one form.item
   - NEVER close form.unit in the middle of a series of form.items
   - When in doubt, keep form.unit open until all related items are processed

3. CHUNK BOUNDARY HANDLING:
   - At chunk boundaries, seamlessly continue the form structure
   - Do not add HTML comments about chunk continuation
   - Maintain proper tag nesting across chunk boundaries

4. FORM.UNIT NESTING VALIDATION:
   - Ensure form.unit always contains form.item elements
   - Maintain consistent designator hierarchy (1, 1.1, 1.1(a), etc.)
   - Preserve the exact structural relationships from input

5. HIERARCHICAL STRUCTURE REQUIREMENTS:
   - When a form.item contains nested items, those nested items MUST be in their own form.unit
   - Example: <form.item>...<form.unit><form.item>...</form.item></form.unit></form.item>
   - Any time you have multiple form.items at the same level, they MUST be inside a form.unit

45.	Syntax for Form section:
   A.	<section><section.front><outline.name.block change.type="new"><label>&sect;</label><designator>Section number</designator><name>Section name</name></outline.name.block><online.view.item>FMS</online.view.item></section.front><section.body><form><form.para><form.text>Content</form.text></form.para><form.para><form.text>Content</form.text></form.para><signature.block><signature.group position="r"><form.vertical.space amt="1"/><form.line align="l" lineover="y">Content</form.line></signature.group></signature.block><note.block><note.group note.head="drnotes"><para><para.text>Content</para.text></para></note.group><research.reference.block><reference.entry><ref.text> west's / cross reference </ref.text></reference.entry><reference.entry><ref.text> west's / cross reference </ref.text></reference.entry></research.reference.block></note.block></form></section.body></section>

46.	Syntax for Form section with form.units:
   A.	<section><section.front><outline.name.block change.type=“new”><label>&sect;</label><designator>Section number</designator><name>Section name</name></outline.name.block><online.view><online.view.item>FMS</online.view.item><online.view.item>REL</online.view.item><online.view.item>RPS</online.view.item><online.view.item>FCV</online.view.item><online.view.item>FTR</online.view.item></online.view></section.front><section.body><form><form.para><form.text>Content</form.text></form.para><form.para><form.text>Content</form.text></form.para><form.unit><form.item><name.block><designator></designator><name>Content</name></name.block><form.para><form.text>Content</form.text></form.para><form.para><form.text></form.text></form.para></form.item><form.item><name.block><designator></designator><name>Content</name></name.block><form.para><form.text>Content</form.text></form.para><form.unit><form.item><name.block><designator></designator></name.block><form.para><form.text>Content</form.text></form.para><form.para><form.text></form.text></form.para></form.item><form.item><name.block><designator></designator></name.block><form.para><form.text>Content</form.text></form.para><form.para><form.text></form.text></form.para></form.item><form.item><name.block><designator></designator></name.block><form.para><form.text>Content</form.text></form.para><form.para><form.text></form.text></form.para></form.item><form.item><name.block><designator></designator></name.block><form.para><form.text>Content</form.text></form.para><form.para><form.text></form.text></form.para></form.item><form.item><name.block><designator></designator></name.block><form.para><form.text>Content</form.text></form.para><form.para><form.text></form.text></form.para></form.item></form.unit></form.item></form.unit></form></section.body></section>
   B. CRITICAL NESTING REQUIREMENTS:

      1. FORM.UNIT MUST ALWAYS CONTAIN FORM.ITEMS:
         - Any time you have multiple form.items at the same level, they MUST be inside a form.unit
         - Example: <form.unit><form.item>...</form.item><form.item>...</form.item></form.unit>

      2. HIERARCHICAL STRUCTURE:
         - When a form.item contains nested items, those nested items MUST be in their own form.unit
         - Example: <form.item>...<form.unit><form.item>...</form.item></form.unit></form.item>

      3. MAINTAIN CONSISTENT DESIGNATOR HIERARCHY:
         - Main items: <designator>1</designator>, <designator>2</designator>
         - First sub-level: <designator>1.1</designator>, <designator>1.2</designator>
         - Second sub-level: <designator>1.1(a)</designator>, <designator>1.1(b)</designator>

      4. CHUNK BOUNDARY HANDLING:
         - At chunk end: Properly close ALL open tags
         - At chunk start: Reopen ALL previously open tags in correct order

46.1. CRITICAL RULE FOR MAJOR STRUCTURAL HEADINGS:
   A. When encountering major headings (ARTICLE, SECTION, PART) within form.unit:
      - Close the current form.unit BEFORE the major heading
      - Create the major heading as a form.item with <head>
      - IMMEDIATELY open a new form.unit after the heading
      - Continue with nested form.item elements in the new form.unit
   
   B. CHUNK PROCESSING REQUIREMENTS:
      - Never include continuation comments in the final output
      - Maintain structural integrity across chunk boundaries
      - Preserve exact form.unit hierarchy from input

47.	Syntax for Chapter:
   A.	<chapter><metadata.block><metadata field="right.running.head"><value>[chapter title]</value></metadata></metadata.block><front><outline.name.block change.type=“new”><label>Chapter</label><designator>[chapter number]</designator><name>[chapter title]</name></outline.name.block></front><chapter.body><section.block><section><section.front><outline.name.block change.type=“new”><label>§</label><designator>[section number]</designator><name>[section title]</name></outline.name.block></section.front><section.body><para><para.text>[content]</para.text></para><para><para.text>[content]</para.text></para></section.body></section><section><section.front><outline.name.block change.type=“new”><label>§</label><designator>[section number]</designator><name>[section title]</name></outline.name.block></section.front><section.body><para><para.text>[content]</para.text></para><para><para.text>[content]</para.text></para></section.body></section><section><section.front><outline.name.block change.type=“new”><label>§</label><designator>[section number]</designator><name>[section title]</name></outline.name.block></section.front><section.body><para><para.text>[content]</para.text></para><para><para.text>[content]</para.text></para></section.body></section></section.block></chapter.body></chapter>
   B.	<chapter.back> - Back matter section for supplementary chapter content
       Purpose: Contains supplementary or concluding content for a chapter (similar to appendices)
       Structure: <chapter.back>[content]</chapter.back>
       Placement: After </chapter.body> and before </chapter>

48.	Syntax for Analytical Level:
   A.	<analytical.level> and <analytical.level.body> - Analytical content containers
       Purpose: Organize analytical or commentary content within documents
       Structure: 
       <analytical.level>
         <front><outline.name.block><name>[title]</name></outline.name.block></front>
         <analytical.level.body>[content]</analytical.level.body>
       </analytical.level>
       Notes: Can contain sections, paragraphs, or other structural elements

49.	Syntax for Part:
   A. <part> and <part.body> - Part-level structural elements
       Purpose: Organize content into major divisions (Parts) containing multiple chapters or sections
       Structure:
        <part>
          <front>
            <outline.name.block change.type="new">
              <label>Part</label>
              <designator>[part number]</designator>
              <name>[part title]</name>
            </outline.name.block>
          </front>
          <part.body>
            [chapters or sections go here]
          </part.body>
        </part>
   B. Part Structure Requirements:
       When multiple chapters exist in a part, wrap them in <part.body>
       Each chapter within a part should use standard chapter syntax (see Instruction #47)
       Parts can also contain sections directly if no chapters are present

General Processing Guidelines:

1.	Symbol Preservation:
   •	Retain all symbols exactly as they appear in the original content
   •	Do not replace or delete any symbols including:
      o	Emdashes (—)
      o	Endashes (–)
      o	Hyphens (-)
      o	Bullets (•, *, -)
      o	Mathematical symbols (±, ×, ÷, =, ≠, ≤, ≥)
      o	Currency symbols ($, €, £, ¥)
      o	Special characters (©, ®, ™, §, ¶)
      o	Arrows (→, ←, ↑, ↓)
      o	Quotation marks (" ", ' ', « », „ ")
   •	Exception: Only replace dashes with <sep/> in section names and section numbers as specified in instruction #6
   •	When symbols appear within XML content, ensure they are properly encoded as entities only if required for XML compliance
   •	Examples:
      o	Input: The cost—approximately $500—is reasonable
      o	Output: The cost—approximately $500—is reasonable
      o	Input: Items 1-10 are excluded
      o	Output: Items 1-10 are excluded

2.	Error Reporting Requirements:
   A.	Flag any unprocessable content with <!-- ERROR: description -->
   B.	Report any ambiguous tag structures
   C.	Note any lost formatting or content

3.	Content Validation Rules:
   A.	Preserve all numerical values exactly
   B.	Maintain case sensitivity
   C.	Keep special characters intact
   D.	Preserve whitespace in critical areas

4.	Processing Priority:
   A.	Critical Elements (highest priority)
      1.	Special tags (<readme>, <to be deleted>)
      2.	Section numbers and structure
      3.	Legal citations
   B.	Secondary Elements
      1.	Formatting tags
      2.	Cross-references
      3.	Footnotes
   C.	Tertiary Elements
      1.	Whitespace optimization
      2.	Basic formatting cleanup

5.	Procedural Summary Processing Rules:
   A.	Content Structure:
      o	 All procedural summaries must be processed as paragraph blocks
      o	 Never use form-related tags for procedural summaries
      o	 Each component must be wrapped in <para><para.text>...</para.text></para>
   B.	Standard Components:
      o	 Headings: Wrap in <bold> tags
      o	 Subject/Who statements: Wrap in <bold> tags
      o	 When/What sections: 
         a.	Label in <ital> tags
         b.	Content in regular text
         c.	Legal references: <bold><ital>LPA</ital></bold><bold>, s. X</bold>
   C.	Pattern Recognition:
      o	 Content following pattern "When:" and "What to do:" indicates procedural summary
      o	 Such content must never be treated as forms regardless of formatting

6.	Content Type Priority Rules:
   A.	When content matches procedural summary pattern:
      o	Override any form-like formatting
      o	Process as paragraphs
      o	Maintain consistent paragraph structure
   B.	Indicators of procedural summary:
      o	When/What to do structure
      o	Bold headings followed by timing/action items
      o	References to LPA sections
      o	Step-by-step instructions

7. Tagging Conflict Resolution Priority:
   A. When content could potentially be tagged with multiple tag types, apply these priority rules:
      1. Legal citation tags (<cite type="case">, <cite type="lrre">, <cite type="periodical">) take highest precedence
      2. Structural tags (<para>, <section>, <form.item>) take second precedence
      3. Formatting tags (<bold>, <ital>) take lowest precedence
   B. When in doubt about whether to apply a tag, err on the side of applying the tag rather than omitting it
   C. For statutory references specifically, always apply <cite type="lrre"> tags even when they appear within other complex structures

8. Special Form Document Patterns:
   A. Headings with Numbered Items Structure:
      - Recognize center-aligned headings followed by numbered lists
      - Each heading becomes <form.item> with <head><name.block><name>Heading</name></name.block></head>
      - Each numbered group under a heading needs its own <form.unit> container
      - Maintain continuous numbering across different heading sections
      - For COUNT sections, use <head><name.block><label>Label</label><designator>X</designator><name>Title</name></name.block></head>
   B. Nested Numbering Pattern:
      - Main items can use any designator format (numbers, letters, roman numerals, bullets, etc.)
      - Each level of nesting requires its own <form.unit> container regardless of designator style
      - Preserve the exact designator format as it appears in the input
      - Example of nested structure:
      <form.item><name.block><designator>[any designator]</designator></name.block>
      <form.para><form.text>content</form.text></form.para>
      <form.unit>
         <form.item><name.block><designator>[any nested designator]</designator></name.block>...</form.item>
         <form.item><name.block><designator>[any nested designator]</designator></name.block>...</form.item>
      </form.unit></form.item>
      - Focus on identifying hierarchical relationships rather than specific designator formats
   C. Paragraph-Item Mixed Pattern:
      - Regular paragraphs use <para><para.text>content</para.text></para>
      - Lettered/numbered items between paragraphs need <form.unit> structure
      - Closing paragraphs after item lists use <form.para><form.text>content</form.text></form.para>
      - Never convert regular paragraphs to form items unless they have clear designators
   D. Section-Based Document Pattern:
      - Top-level document name uses <form.name.block><name>TITLE</name></form.name.block>
      - Section headings use <head><name.block><label>SECTION</label><designator>ONE</designator><name><endline/>SUBTITLE</name></name.block></head>
      - For section titles with line breaks, use <endline/> between parts
      - Nested items under sections require nested <form.unit> structures
      - Choice selections within sections use <item.selection.group> with proper instruction tags

9. Attribute Application Rules for Updated Content:
   A. Parent/Starting Tags Only:
      - Apply `date.updated="1" date.trace="1"` attributes ONLY to the starting/parent tag in a conversion section
      - Examples of starting tags that need these attributes:
      * The initial <section> tag in a section block
      * The opening <list> tag in a list block
      * The main <para> tag in a paragraph block
      * The primary <footnote> tag in a footnote block   
   B. "In Between" and Child Elements:
      - Do NOT apply these attributes to child or "in-between" elements within a structure
      - Child elements (like nested list items, paragraphs within sections, etc.) inherit tracking information from their parent
      - Example of correct attribute application:
      <list>
         <list.item>  <!-- No attributes needed here -->
            <name.block><designator>1</designator></name.block>
            <para><para.text>Content</para.text></para>
         </list.item>
      </list>
   C. Exceptions:
      - Always apply `date.trace="1"` to ANY <update> tag regardless of nesting level
      - When explicitly instructed to replace specific child elements (like individual paragraphs within sections), apply attributes to those specific elements

10. Form Section Structure Consistency Guidelines:
   A. Placeholder and Inline Instruction Consistency:
      •	ALL placeholder text must be wrapped in <inline.instr> tags regardless of other formatting
      •	When placeholders appear within other formatting (<bold>, <ital>), the <inline.instr> tag must be the innermost wrapper
      •	Example: <bold><ital><inline.instr>[text]</inline.instr></ital></bold>
      •	Never leave placeholders unwrapped even when inside formatting tags
   
   B. Reference to Addressing Rules:
      •	For complete guidance on address.block vs form.line vs form.para, see Instruction 38
      •	Container precedence rules apply: <tbl>, <fc>, <ab> tags override standard address formatting

11. West's Key Number Digest Reference Replacement Rule:
   A. When replacing ONLY a West's Key Number Digest reference:
      • Do NOT wrap in a full note.block structure
      • Use ONLY the direct replacement tags:
        <reference.entry render="all">
        <ref.text><cite type="topic.key">West's Key Number Digest, [content]</cite></ref.text>
        </reference.entry>
      • This applies specifically when the instruction contains phrases like:
        - "Replace West's Key Digest Number reference with:"
        - "Replace West's Key Number Digest with:"   
   B. Important distinction:
      • When replacing an ENTIRE note block: Use full note.block structure
      • When replacing ONLY the West's reference within a note: Use only reference.entry

Processing Guidelines
When processing the document:
1.	Do not add any tags to <readme> or <to be deleted> content and maintain the same placement order as present in the input.
2.	Do not merge them in the paragraph tagging.
3.	Process all content sequentially and completely, ensuring no content is missed.
4.	Preserve all special formatting and placement.
5.	Remove any extra spaces that may appear in the output, including within tags or between tags
6.	Always code the content below <readme> or <to be deleted> tags, regardless of whether both tags are present or not.
7.	Ensure that all content, including footnotes and their references, are properly coded and included in the output.
8.	Do not perform prompt echoing and do not mention task completion statements in the output.
9.	Treat <p> tags in the input as indicators for paragraphs, but replace them with the appropriate <para> tags as per the XML structure.
10.	For content within specific tags, use the appropriate syntax as specified in the instructions.
11. Do not add any coding for <url>content</url> and maintain the same placement order as present in the input.
12. Expand all the <us> tags(not symbols) in the input as <underscore> tags in the output, double underscore(__) should be coded as <leaveout/>.															   
* <ff>content</ff> - ONLY content within these specific tags must use form section structure as specified in the general instructions -FORMS
* <appx>content</appx> - use the appropriate appendix section syntax – New appendix section
* <tbl>content</tbl> - use the table syntax
* <ck>content</ck> - use the checklist section syntax – New checklist
* <qb>content</qb>, <qp> - use the quote block syntax
* <fc>content</fc> - use the forms caption syntax --FORMS
* <ab>content</ab> - use the attachment block syntax ---FORMS
* <l>content</l> - use the list syntax
* <li>content</li> - use the list item syntax
* <hal>content</hal> - use left-aligned heading syntax
* <hac>content</hac> - use center-aligned heading syntax  
* <har>content</har> - use right-aligned heading syntax
A. Instruction Processing Priority:
• When <readme> tags contain instructions that imply structural elements:
  - Always separate the instruction from the content in the output
  - The actual content should follow with appropriate structural tags
  - Never nest structural tags (<para>, <footnote>, <section>) inside <update> tags
B. Complex Structure Priority Rule:
•	Always prioritize maintaining proper structural relationships over update wrapping
•	For complex elements like quotes, footnotes, and citations, ensure proper nesting
•	When processing update instructions that affect complex structures:
o	Maintain structural integrity first
o	Apply update attributes appropriately
Never sacrifice legal meaning for simpler XML structure
•	Process content from inside out - first apply specialized content tags, then apply format tags
•	When multiple nested formats exist, preserve all formatting layers
•	Never omit any formatting layer when multiple formats are present
C. This rule has highest priority and overrides any contradictory formatting guidelines
13.	For content following <readme> tags, apply appropriate section structure based on instructions
- If the readme contains instructions about adding/inserting sections:
      * Apply full section structure to all subsequent content
      * Include proper section.front with outline.name.block
      * Organize content within section.body
      * Continue section structure until next special tag or clear content break
    - Parse section numbers and titles from readme instructions
    - Apply appropriate section-level attributes (date.updated, date.trace)
    - Maintain proper heading hierarchy within the section structure
    - IMPORTANT: If readme mentions "section body" (e.g., "Replace section body", "New section body"), refer to Instruction #30 for proper body hierarchy tagging and wrapping rules
14. OUTPUT CLEANLINESS REQUIREMENTS(EXTREMELY IMPORTANT):
   A. PROHIBITED CONTENT - NEVER include any of the following:
      - HTML comments about chunk processing
      - Continuation markers or structural notes
      - Explanatory statements or preambles (e.g., "I'll convert this document...", "Here's the output...", "The XML format is...")
      - Processing notes or descriptions about what you're doing
      - Markdown formatting of any kind
      - Code block delimiters (xml, , or any backticks)
      - Any text before or after the XML output
      - Any commentary or explanation about the conversion
   
   B. REQUIRED OUTPUT FORMAT:
      - Output must contain ONLY the properly formatted XML tags and content
      - No introduction or conclusion statements allowed.
      - Raw XML markup only - treat as if you are directly writing to an XML file
      - No markdown, no code blocks, no explanations
Ensure strict adherence to these rules, particularly regarding spacing and tag conversions as specified in the general instructions.'''
