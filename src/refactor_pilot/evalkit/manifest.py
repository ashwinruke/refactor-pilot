"""Benchmark manifests: the known legacy constructs in a target repo.

Each item names one construct, the recipe expected to fix it, and a regex that
matches the legacy form. An item is "resolved" when the regex no longer matches
the file with comments stripped (or the file is gone).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_COMMENTS = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)


@dataclass(frozen=True)
class Item:
    id: str
    recipe: str
    file: str
    legacy_pattern: str
    description: str = ""


def load(path: Path) -> list[Item]:
    data = json.loads(Path(path).read_text())
    items = [Item(**raw) for raw in data["items"]]
    ids = [i.id for i in items]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate manifest ids: {sorted(dupes)}")
    for item in items:
        re.compile(item.legacy_pattern)
    return items


def strip_comments(source: str) -> str:
    """Rough comment removal. Good enough for pattern checks; it does not
    understand comment markers inside string literals."""
    return _COMMENTS.sub(" ", source)


def is_resolved(item: Item, repo: Path) -> bool:
    path = Path(repo) / item.file
    if not path.exists():
        return True
    text = strip_comments(path.read_text(errors="replace"))
    return re.search(item.legacy_pattern, text, re.MULTILINE) is None
