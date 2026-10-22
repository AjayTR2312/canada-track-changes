"""Group a flat pile of files into per-chapter file sets.

A "file set" is one docx + one xml/pxml + one images zip belonging to the same
chapter. There is no separate pdf role: this pipeline reads the chapter PDF from
inside the images zip, so asking for a standalone pdf would request a file the
pipeline never opens.

DESIGN - why there is no list of known filename conventions here
----------------------------------------------------------------
The obvious implementation is a table of "role suffixes" to strip (``_images``,
``(revision copy)``, ``_clean``, a house prefix, ...). That only ever works for
the naming conventions its author happened to see; the next publication invents
new ones and the grouping silently degrades.

So nothing here knows any convention. Instead:

* **Boilerplate is measured, not listed.** A token appearing in nearly every
  file of a role says nothing about WHICH chapter a file belongs to - if every
  zip ends in ``_images`` then ``images`` distinguishes nothing. Such tokens are
  discovered per-role from the batch itself, which is what a hard-coded suffix
  list was only approximating.
* **Numbers are the strongest cross-role signal.** Chapter identity almost
  always survives as a number whatever the surrounding text, and ``Ch1``,
  ``_01`` and ``" 1"`` all reduce to the same one.
* **Pairing is an assignment problem.** Each set needs exactly one file per
  role, so partners are chosen globally by score with each file used once,
  rather than by hoping two names reduce to an identical key.

The result is still a *guess* - filenames are not a reliable source of truth -
so every group carries a confidence and callers must let the user review and
correct the plan before anything is processed.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Optional

_ROLE_BY_EXT = {
    ".docx": "docx",
    ".doc": "docx",
    ".xml": "xml",
    ".pxml": "xml",
    ".zip": "zip",
}

REQUIRED_ROLES = ("docx", "xml", "zip")

# A token shared by at least this fraction of a role's files carries no
# chapter-identifying information and is ignored when comparing across roles.
# Below 1.0 so one oddly-named file cannot keep genuine boilerplate in play.
_BOILERPLATE_SHARE = 0.8

# Groups scoring below this are surfaced for confirmation rather than being
# presented as settled.
_CONFIDENT_SCORE = 0.55


def classify_file(name: str) -> Optional[str]:
    """Return 'docx' | 'xml' | 'zip' from a filename's extension, else None."""
    return _ROLE_BY_EXT.get(PurePosixPath(name).suffix.lower())


def _tokenize(name: str) -> list:
    """Filename stem -> lowercase alphanumeric tokens.

    Splits on separators AND on letter/digit boundaries, so ``COCFCh1``,
    ``COCF 1`` and ``cocf_01`` yield comparable tokens. Leading zeros are
    dropped so ``07`` and ``7`` are the same chapter. No token is interpreted.
    """
    stem = PurePosixPath(name).stem.lower()
    out = []
    for part in re.findall(r"[a-z]+|\d+", stem):
        out.append((part.lstrip("0") or "0") if part.isdigit() else part)
    return out


def _numbers(tokens: list) -> set:
    return {t for t in tokens if t.isdigit()}


def _boilerplate_tokens(names_by_role: dict) -> set:
    """Tokens near-universal within their own role, and therefore useless for
    telling one chapter from another. This replaces a hard-coded suffix list:
    if every zip says ``images``, the batch itself reveals it."""
    boilerplate = set()
    for _role, names in names_by_role.items():
        if len(names) < 2:
            continue  # a single file cannot establish what is common
        counts = Counter()
        for name in names:
            counts.update(set(_tokenize(name)))
        cutoff = max(2, int(len(names) * _BOILERPLATE_SHARE))
        for token, seen in counts.items():
            if seen >= cutoff and not token.isdigit():
                boilerplate.add(token)
    return boilerplate


def _similarity(a_tokens: list, b_tokens: list, boilerplate: set) -> float:
    """How strongly two filenames from different roles look like the same
    chapter, 0..1 - numbers dominate, remaining words break ties."""
    a_nums, b_nums = _numbers(a_tokens), _numbers(b_tokens)
    a_words = [t for t in a_tokens if not t.isdigit() and t not in boilerplate]
    b_words = [t for t in b_tokens if not t.isdigit() and t not in boilerplate]

    if a_nums and b_nums:
        number_score = len(a_nums & b_nums) / len(a_nums | b_nums)
    elif not a_nums and not b_nums:
        number_score = 0.5  # neither is numbered: no evidence either way
    else:
        number_score = 0.0

    if a_words and b_words:
        try:
            from rapidfuzz import fuzz
            word_score = fuzz.token_sort_ratio(" ".join(a_words), " ".join(b_words)) / 100.0
        except ImportError:
            union = set(a_words) | set(b_words)
            word_score = len(set(a_words) & set(b_words)) / max(len(union), 1)
    else:
        word_score = 0.5  # nothing distinctive left once boilerplate is removed

    return (0.65 * number_score) + (0.35 * word_score)


@dataclass
class FileSetGroup:
    """One chapter's files. Role fields hold whatever the caller passed in
    (filenames here); this module never reads file contents."""
    task_name: str
    docx: object = None
    xml: object = None
    zip: object = None
    extras: list = field(default_factory=list)
    confidence: float = 1.0
    low_confidence: bool = False

    def role_map(self) -> dict:
        return {"docx": self.docx, "xml": self.xml, "zip": self.zip}


def missing_roles(group: FileSetGroup) -> list:
    return [role for role in REQUIRED_ROLES if group.role_map()[role] is None]


def group_is_complete(group: FileSetGroup) -> bool:
    return not missing_roles(group)


def group_files(names) -> list:
    """Bucket `names` (filename strings) into FileSetGroups.

    Anchors on whichever role has the most files - every set needs one of each,
    so any role serves - then assigns the other roles' files to anchors
    best-score-first, each file used at most once. Leftovers become incomplete
    groups; unrecognized extensions go to a trailing "(unclassified)" group so
    nothing is silently dropped.
    """
    by_role = {"docx": [], "xml": [], "zip": []}
    unclassified = []
    for name in names or []:
        role = classify_file(name)
        if role is None:
            unclassified.append(name)
        else:
            by_role[role].append(name)

    boilerplate = _boilerplate_tokens(by_role)
    tokens = {name: _tokenize(name) for role in by_role for name in by_role[role]}

    anchor_role = max(REQUIRED_ROLES, key=lambda r: len(by_role[r]))
    groups = [FileSetGroup(task_name=_display_name(n), **{anchor_role: n})
              for n in by_role[anchor_role]]
    scores_by_group = {id(g): [] for g in groups}

    for role in REQUIRED_ROLES:
        if role == anchor_role or not by_role[role]:
            continue
        # Score every (anchor, candidate) pair and take them best-first, so a
        # strong pairing is never blocked by a weaker one claiming a file.
        pairs = [
            (_similarity(tokens[getattr(g, anchor_role)], tokens[cand], boilerplate), g, cand)
            for g in groups for cand in by_role[role]
        ]
        pairs.sort(key=lambda t: t[0], reverse=True)
        used_files, filled = set(), set()
        for score, group, cand in pairs:
            if cand in used_files or id(group) in filled:
                continue
            setattr(group, role, cand)
            scores_by_group[id(group)].append(score)
            used_files.add(cand)
            filled.add(id(group))
        for leftover in (c for c in by_role[role] if c not in used_files):
            groups.append(FileSetGroup(task_name=_display_name(leftover),
                                       **{role: leftover}))

    for group in groups:
        scores = scores_by_group.get(id(group), [])
        group.confidence = round(min(scores), 3) if scores else 0.0
        group.low_confidence = bool(scores) and group.confidence < _CONFIDENT_SCORE

    if unclassified:
        groups.append(FileSetGroup(task_name="(unclassified)", extras=unclassified))
    return groups


def _display_name(sample_filename: str) -> str:
    """Label for the group: the anchor file's own stem, unchanged.

    Deliberately NOT boilerplate-stripped. Boilerplate is what to ignore when
    deciding which files belong together; stripping it from the label too would
    reduce "COCF 1" to "1" and "VOL3_CH07_redline" to "3 07", since a shared
    publication name is by definition common across the batch. The user's own
    filename is always the most recognizable label.
    """
    return PurePosixPath(sample_filename).stem.strip() or sample_filename



