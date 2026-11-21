"""The edit format a model answers in, and how it is applied.

    <<<<<<< SEARCH src/logger.cpp
    exact existing lines
    =======
    replacement lines
    >>>>>>> REPLACE

A block replaces one exact, unique occurrence of the SEARCH text in the named
file. Exactness is the point: the model must quote real code, so an edit can
never land somewhere it did not look. Every failure is reported in words the
model can act on, because the error text becomes the feedback for its retry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import textio

_BLOCK = re.compile(
    r"^<{5,} SEARCH[ \t]+(?P<path>\S+)[ \t]*\n"
    r"(?P<search>.*?)"
    r"^={5,}[ \t]*\n"
    r"(?P<replace>.*?)"
    r"^>{5,} REPLACE[ \t]*$",
    re.DOTALL | re.MULTILINE,
)


class EditError(ValueError):
    """The response could not be turned into edits. Safe to show to the model."""


@dataclass(frozen=True)
class Edit:
    path: str
    search: str
    replace: str


def parse(response: str) -> list[Edit]:
    text = response.replace("\r\n", "\n")
    edits = [Edit(m["path"].strip("`'\""), m["search"], m["replace"]) for m in _BLOCK.finditer(text)]
    if not edits:
        raise EditError(
            "No edit blocks found. Answer with one or more blocks in exactly this form:\n"
            "<<<<<<< SEARCH path/to/file\n<existing lines>\n=======\n<new lines>\n>>>>>>> REPLACE"
        )
    return edits


def _rstrip_lines(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.split("\n"))


def apply(edits: list[Edit], repo: Path, allowed: set[str]) -> list[str]:
    """Apply edits under `repo`. Only files in `allowed` (repo-relative) may change.

    All edits are checked against the files in memory first; nothing is written
    unless every block applies. Returns the list of files changed.
    """
    repo = Path(repo).resolve()
    contents: dict[str, str] = {}
    originals: dict[str, tuple[str, str]] = {}  # rel -> (text as loaded, newline style)
    for number, edit in enumerate(edits, 1):
        rel = Path(edit.path).as_posix().lstrip("./")
        if rel not in allowed:
            raise EditError(
                f"Block {number} edits '{edit.path}', which is not one of the files you may change: "
                + ", ".join(sorted(allowed))
            )
        if rel not in contents:
            originals[rel] = textio.load(repo / rel)
            contents[rel] = originals[rel][0]
        text = contents[rel]
        if edit.search == "":
            raise EditError(f"Block {number} ({rel}) has an empty SEARCH section.")
        if originals[rel][1] == "mixed":  # match against LF text; the file itself is written back untouched elsewhere
            text = text.replace("\r\n", "\n")
        count = text.count(edit.search)
        search = edit.search
        if count == 0:  # tolerate trailing-whitespace differences only
            stripped_text, stripped_search = _rstrip_lines(text), _rstrip_lines(edit.search)
            if stripped_text.count(stripped_search) == 1:
                text, search, count = stripped_text, stripped_search, 1
        if count == 0:
            first = edit.search.strip().split("\n")[0][:80]
            raise EditError(
                f"Block {number}: the SEARCH text was not found in {rel} (it starts with: {first!r}). "
                "SEARCH must copy existing lines exactly, including indentation. "
                "Remember the files are back in their original state."
            )
        if count > 1:
            raise EditError(
                f"Block {number}: the SEARCH text occurs {count} times in {rel}. "
                "Include more surrounding lines so it matches exactly once."
            )
        contents[rel] = text.replace(search, edit.replace, 1)
    changed = []
    for rel, text in contents.items():
        original, style = originals[rel]
        if text != original:
            textio.save(repo / rel, text, "lf" if style == "mixed" else style)
            changed.append(rel)
    return sorted(changed)
