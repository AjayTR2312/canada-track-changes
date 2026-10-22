
URL TAGGING RULES (MANDATORY for all inserted / modified text):

  Identify ALL strings that start with "http://", "https://", "www.", or contain "doi.org".
  Tag every URL regardless of its location (main text, footnotes, tables, etc.).
  This is a PIX document: use the bare <url> tag (NOT <cite type="url">, which is not
  declared in pix.dtd).

  FORMAT:   <url>URL_HERE</url>
  EXAMPLES: <url>https://www.example.com</url>
            <url>https://doi.org/10.1000/xyz123</url>

  RULES:
  - Tag ALL URLs — no exceptions, even when URL appears inside other tags or structures.
  - If URL is being inserted as a tracked change, keep the <url> inside <trace>:
      <trace><url>https://www.example.com</url></trace>
  - Do NOT leave raw URL text untagged in output XML.
