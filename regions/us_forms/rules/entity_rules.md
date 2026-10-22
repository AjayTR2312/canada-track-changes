
XML ENTITY ENCODING RULES (MANDATORY for all inserted / modified text):
  - Double section sign (§§) MUST be encoded as &dblsect;
  - Section sign (§) MUST be encoded as &sect;
  - Pilcrow (¶) MUST be encoded as &para;
  - Square brackets ([ and ]) MUST be kept LITERAL — do NOT encode them as
    &lsqb;/&rsqb;. US Forms keeps brackets as-is (e.g. inside <inline.instr>).
  - Number sign (#) MUST be encoded as &num;
  - Dollar sign ($) MUST be encoded as &dollar;
  - Percent sign (%) MUST be encoded as &percnt;
  - Asterisk (*) MUST be encoded as &ast;
  - Left single quote (‘) MUST be encoded as &lsquo;
  - Right single quote (’) MUST be encoded as &rsquo;
  - Opening double quote (“) MUST be encoded as &ldquo;
  - Closing double quote (”) MUST be encoded as &rdquo;
  - Straight double quote (") — use &ldquo; for the opening and &rdquo; for the closing
  - Apostrophe / single quote (’ or ') MUST be encoded as &apos;
  - Ampersand (&) MUST be encoded as &amp;  (unless it is already part of an entity like &apos;)
  - Less-than (<) inside text MUST be encoded as &lt;  (do NOT confuse with XML tags)
  - Greater-than (>) inside text MUST be encoded as &gt;
  - Em-dash (—) should be encoded as &mdash;
  - En-dash (–) should be encoded as &ndash;
  - Plus sign (+) MUST be encoded as &plus;
  - West's key symbol MUST be encoded as &key;

  Apply these rules ONLY to the text content you are inserting or changing.
  Do NOT alter entity encoding in text you are not changing.

  ⚠ CRITICAL: In FIND, copy entities EXACTLY as they appear in the XML CHUNK.
  NEVER output an entity followed by its literal character (e.g. &ldquo;" or
  &percnt;% or &mdash;—). This is the #1 cause of replacement failures.
  The FIND string must be byte-for-byte identical to the source XML.
