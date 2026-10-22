
URL TAGGING RULES (MANDATORY for all inserted / modified text):

  Identify ALL strings that start with "http://", "https://", "www.", or contain "doi.org".
  Tag every URL regardless of its location (main text, footnotes, tables, etc.).

  STANDARD URL FORMAT:
    <cite finder="legacy" type="url"><ref normalpub="webref">URL_HERE</ref></cite>

  DOI URL FORMAT:
    <cite finder="manual" type="url"><ref normalpub="webref">URL_HERE</ref></cite>

  EXAMPLES:
    <cite finder="legacy" type="url"><ref normalpub="webref">https://www.example.com</ref></cite>
    <cite finder="manual" type="url"><ref normalpub="webref">https://doi.org/10.1000/xyz123</ref></cite>

  RULES:
  - Tag ALL URLs — no exceptions, even when URL appears inside other tags or structures.
  - If URL is being inserted as a tracked change, keep the URL cite block inside <trace>:
      <trace><cite finder="legacy" type="url"><ref normalpub="webref">https://www.example.com</ref></cite></trace>
  - Do NOT leave raw URL text untagged in output XML.
