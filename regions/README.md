# Region packs

A **region pack** holds everything that is specific to one region / product line, so a team can
run the same tool for its own content without touching code.

```
regions/<id>/
  region.yaml            name, products, DTD families, prompt + rule wiring, log names
  dtd/                   the region's DTD file(s) (and an optional shared entities .dtd)
  prompts/               the LLM prompt templates the pipeline loads (tagging prompts, ...)
  validation_prompts/    prompt.txt + prompt_validation.txt for the revision validator
  rules/                 snippets injected into prompts: entity, citation and URL rules
```

The app runs **one** region per deployment, chosen with the `REGION_ID` environment variable
(default `us_forms`). The UI reads the region from `GET /api/region`, so the name, product list
and DTD choices follow the pack.

## Add a region

1. `python tools/new_region.py my_region "My Region Name"` - copies `_template` and the prompts/rules of
   `us_forms` as a working starting point.
2. Put your DTD file(s) in `regions/my_region/dtd/` and describe them under `dtd.families` in `region.yaml`
   (`doctype_keywords` decide which DTD a document uses; list specific names before generic ones).
3. Adapt `prompts/` and `rules/` to your region's tagging conventions. If your DTD has no `<cite>` but bare
   citation tags, set `citation_style: bare` and fill `citation_tag_map`.
4. `python tools/check_region.py my_region` - must say `ready`.
5. Run with `REGION_ID=my_region` (add it to `.env`).

## What each part controls

| Piece | Where it is used |
|---|---|
| `dtd/` + `dtd.families` | DOCTYPE detection, DTD validation, the tag palette (tags, attributes, entities) and the standalone DTD validator |
| `prompts/` | every `load_prompt("...")` in the pipeline, plus the per-product tagging prompt |
| `rules/` | `{{XML_ENTITY_RULES}}`, `{{CASE_CITATION_RULES}}`, `{{URL_TAGGING_RULES}}` in prompts |
| `products` | the Product dropdown (Processing and Batch) |
| `logs` | the region's own false-positive and correction logs |

Tip: keep prompt wording that is the same across regions identical, and only change what your DTD or
house style needs - that keeps later improvements easy to share.
