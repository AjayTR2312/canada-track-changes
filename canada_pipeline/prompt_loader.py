"""
Centralized prompt loading for the Canada track-changes pipeline.

Instead of hard-coding LLM prompts as f-strings inside Python modules, prompts
live as plain-text / Markdown files under the repo-root ``prompts/`` directory.
This keeps the wording editable (and reviewable) without touching code, and
versions the prompt copy independently from the logic that uses it.

Usage
-----
    from canada_pipeline.prompt_loader import load_prompt

    prompt = load_prompt(
        "track_changes_audit.txt",
        designator=designator,
        section_normalized=section_normalized,
    )

Placeholders inside a prompt file use double-brace syntax, e.g.
``{{designator}}``. Single braces (``{`` / ``}``) are left untouched, so XML and
JSON examples in the prompt body are safe to include verbatim.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

# Repo-root /prompts directory. This file lives in <root>/canada_pipeline/, so
# the prompts folder is one level up. This matches the convention already used
# by canada_pipeline/section_insertion.py.
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Matches {{ name }} / {{name}} placeholders (allows optional surrounding spaces).
_PLACEHOLDER_RE = re.compile(r"{{\s*(\w+)\s*}}")


@lru_cache(maxsize=None)
def _read_template(name: str) -> str:
    """Read and cache the raw prompt template by file name."""
    path = PROMPTS_DIR / name
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Prompt file not found: {path}") from exc


def render_prompt(template: str, **values: object) -> str:
    """Substitute ``{{name}}`` placeholders in a template string.

    Raises ``KeyError`` if the template references a placeholder for which no
    value was supplied, so missing substitutions fail loudly instead of leaking
    a literal ``{{name}}`` into the prompt sent to the model.
    """

    def _sub(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key not in values:
            raise KeyError(f"Missing value for prompt placeholder '{{{{{key}}}}}'")
        return str(values[key])

    return _PLACEHOLDER_RE.sub(_sub, template)


def load_prompt(name: str, **values: object) -> str:
    """Load a prompt file by name and substitute any ``{{placeholders}}``.

    Parameters
    ----------
    name:
        File name relative to ``prompts/``, e.g. ``"track_changes_audit.txt"``.
    **values:
        Values for the ``{{placeholder}}`` tokens inside the prompt file.

    Returns
    -------
    str
        The fully rendered prompt text.
    """
    return render_prompt(_read_template(name), **values)


def clear_cache() -> None:
    """Clear the in-memory template cache.

    Useful when editing prompt files in a long-running process (e.g. a worker
    or notebook) and you want the next ``load_prompt`` call to re-read from disk.
    """
    _read_template.cache_clear()
