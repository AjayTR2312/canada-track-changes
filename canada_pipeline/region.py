"""Region packs: every region-specific asset (DTDs, prompts, rule snippets, product list,
log names) lives in `regions/<id>/` described by a `region.yaml`. The app loads ONE pack,
chosen by the REGION_ID environment variable (default `canada`).

Nothing else in the code base should hard-code a region name, a DTD file or a prompt path --
ask this module. See regions/_template/ for the skeleton to copy for a new region, and
`python tools/check_region.py <id>` to validate a pack.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
REGIONS_DIR = Path(os.getenv("REGIONS_DIR") or (REPO_ROOT / "regions"))
DEFAULT_REGION_ID = "canada"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]*$")


class RegionError(RuntimeError):
    """The region pack is missing or malformed (the message says exactly what to fix)."""


@dataclass(frozen=True)
class DtdFamily:
    id: str
    file: str
    label: str
    doctype_keywords: tuple
    rules_family: str
    citation_style: str          # "cite" (<cite type=..>) or "bare" (<cc>, <st>, ...)
    selectable: bool             # offered in the tag palette / DTD validator dropdown


@dataclass(frozen=True)
class Region:
    id: str
    name: str
    description: str
    root: Path
    products: tuple              # ({"id":..., "label":...}, ...)
    dtd_families: tuple          # tuple[DtdFamily] in DOCTYPE-match priority order
    default_family: str
    entities_file: str
    tagging_prompts: dict        # product id -> file ; "fallbacks" -> list
    rules: dict
    citation_tag_map: dict
    logs: dict
    raw: dict = field(default_factory=dict, compare=False, hash=False)

    # ---- folders -------------------------------------------------------------
    @property
    def prompts_dir(self) -> Path: return self.root / "prompts"
    @property
    def validation_prompts_dir(self) -> Path: return self.root / "validation_prompts"
    @property
    def dtd_dir(self) -> Path: return self.root / "dtd"
    @property
    def rules_dir(self) -> Path: return self.root / "rules"

    # ---- validation ----------------------------------------------------------
    @property
    def allowed_tags(self) -> frozenset:
        """Tags that never raise DTD / unknown-element errors (new structural tags being rolled
        out): region.yaml `validation.allowed_tags` plus the optional ALLOWED_TAGS env var
        (comma separated). They are unwrapped before DTD validation."""
        cfg = (self.raw.get("validation") or {}).get("allowed_tags") or []
        env = re.split(r"[,\s;]+", os.getenv("ALLOWED_TAGS", ""))
        return frozenset(str(t).strip() for t in [*cfg, *env] if str(t).strip())

    # ---- DTD helpers ---------------------------------------------------------
    def family(self, family_id: str) -> DtdFamily | None:
        return next((f for f in self.dtd_families if f.id == family_id), None)

    @property
    def selectable_families(self) -> list:
        """Families offered in the palette / DTD validator dropdown: the default family
        first, then the rest in manifest order."""
        sel = [f for f in self.dtd_families if f.selectable]
        return sorted(sel, key=lambda f: f.id != self.default_family)

    def detect_family(self, doctype_path: str) -> DtdFamily | None:
        """First family (manifest order) whose keyword appears in the DOCTYPE path and
        whose DTD file exists."""
        declared = (doctype_path or "").lower()
        for fam in self.dtd_families:
            if (self.dtd_dir / fam.file).exists() and any(k.lower() in declared for k in fam.doctype_keywords):
                return fam
        return None

    def default_dtd_family(self) -> DtdFamily:
        return self.family(self.default_family) or self.dtd_families[0]

    # ---- products ------------------------------------------------------------
    @property
    def default_product(self) -> str:
        """The first product listed in region.yaml (what the UI preselects)."""
        return self.products[0]["id"]

    def product_setting(self, product: str, key: str, default=None):
        """A per-product setting from region.yaml `products:` (e.g. date_updated, folder_keywords)."""
        for p in self.raw.get("products") or []:
            if str(p.get("id", "")).lower() == (product or "").lower():
                return p.get(key, default)
        return default

    def product_for_folder(self, folder_name: str) -> str:
        """Product for a batch folder name: the first product whose `folder_keywords` appear in it,
        else the default product."""
        name = (folder_name or "").lower()
        for p in self.raw.get("products") or []:
            if any(str(k).lower() in name for k in (p.get("folder_keywords") or [])):
                return str(p["id"])
        return self.default_product

    # ---- prompts / rules -----------------------------------------------------
    def tagging_prompt_candidates(self, product: str) -> list:
        tp = self.tagging_prompts
        names = [tp.get((product or "").lower())] + list(tp.get("fallbacks") or [])
        return [n for n in names if n]

    def rule_text(self, kind: str, family_id: str | None = None) -> str:
        """Contents of a rule snippet ('entity_rules' | 'citation_rules' | 'url_rules')."""
        tmpl = self.rules.get(kind)
        if not tmpl:
            return ""
        fam = self.family(family_id or self.default_family)
        name = tmpl.replace("{family}", fam.rules_family if fam else self.default_family)
        path = self.rules_dir / name
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise RegionError(f"Region '{self.id}': rule file not found: {path}") from None

    # ---- UI payload ----------------------------------------------------------
    def public_info(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "products": list(self.products),
            # default family first (dropdown order), then manifest order
            "dtd_families": [
                {"id": f.id, "label": f.label, "selectable": f.selectable}
                for f in sorted(self.dtd_families, key=lambda f: f.id != self.default_family)
            ],
            "default_family": self.default_family,
        }


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise RegionError(msg)


def _load(region_id: str) -> Region:
    _require(bool(_ID_RE.match(region_id or "")), f"Invalid REGION_ID '{region_id}' (use lowercase letters, digits, _ or -)")
    root = REGIONS_DIR / region_id
    manifest = root / "region.yaml"
    _require(manifest.is_file(), f"Region '{region_id}' not found: expected {manifest}")
    try:
        data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RegionError(f"{manifest}: invalid YAML: {exc}") from exc
    _require(isinstance(data, dict), f"{manifest}: top level must be a mapping")
    _require(data.get("id") == region_id, f"{manifest}: `id` must equal the folder name '{region_id}' (got {data.get('id')!r})")
    _require(bool(data.get("name")), f"{manifest}: `name` is required")

    products = tuple(
        {"id": str(p["id"]), "label": str(p.get("label") or p["id"])} for p in (data.get("products") or [])
    )
    _require(len(products) > 0, f"{manifest}: at least one entry under `products` is required")

    dtd = data.get("dtd") or {}
    fams = []
    for f in dtd.get("families") or []:
        _require(bool(f.get("id") and f.get("file")), f"{manifest}: every dtd family needs `id` and `file`")
        fams.append(DtdFamily(
            id=str(f["id"]), file=str(f["file"]), label=str(f.get("label") or f["id"]),
            doctype_keywords=tuple(str(k) for k in (f.get("doctype_keywords") or [f["id"]])),
            rules_family=str(f.get("rules_family") or f["id"]),
            citation_style=str(f.get("citation_style") or "cite"),
            selectable=bool(f.get("selectable", False)),
        ))
    _require(len(fams) > 0, f"{manifest}: at least one `dtd.families` entry is required")
    default_family = str(dtd.get("default_family") or fams[0].id)
    _require(any(f.id == default_family for f in fams), f"{manifest}: dtd.default_family '{default_family}' is not in dtd.families")

    return Region(
        id=region_id,
        name=str(data["name"]),
        description=str(data.get("description") or ""),
        root=root,
        products=products,
        dtd_families=tuple(fams),
        default_family=default_family,
        entities_file=str(dtd.get("entities_file") or ""),
        tagging_prompts=dict(data.get("tagging_prompts") or {}),
        rules=dict(data.get("rules") or {}),
        citation_tag_map=dict(data.get("citation_tag_map") or {}),
        logs=dict(data.get("logs") or {}),
        raw=data,
    )


@lru_cache(maxsize=8)
def load_region(region_id: str) -> Region:
    return _load(region_id)


def active_region_id() -> str:
    return (os.getenv("REGION_ID") or DEFAULT_REGION_ID).strip()


def get_region() -> Region:
    """The region this deployment runs (REGION_ID, default canada)."""
    return load_region(active_region_id())


def list_regions() -> list:
    """Ids of every pack under regions/ (folders with a region.yaml; `_template` excluded)."""
    if not REGIONS_DIR.is_dir():
        return []
    return sorted(p.name for p in REGIONS_DIR.iterdir() if p.is_dir() and not p.name.startswith("_") and (p / "region.yaml").is_file())
