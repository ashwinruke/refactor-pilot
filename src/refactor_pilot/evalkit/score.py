"""Score a (possibly migrated) repo against its manifest."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import manifest as manifestmod


@dataclass
class Score:
    resolved: list[str] = field(default_factory=list)
    remaining: list[str] = field(default_factory=list)
    by_recipe: dict[str, tuple[int, int]] = field(default_factory=dict)  # recipe -> (resolved, total)

    @property
    def total(self) -> int:
        return len(self.resolved) + len(self.remaining)

    @property
    def fraction(self) -> float:
        return len(self.resolved) / self.total if self.total else 0.0


def score(repo: Path, manifest_path: Path) -> Score:
    result = Score()
    tally: dict[str, list[int]] = {}
    for item in manifestmod.load(manifest_path):
        done = manifestmod.is_resolved(item, repo)
        (result.resolved if done else result.remaining).append(item.id)
        counts = tally.setdefault(item.recipe, [0, 0])
        counts[0] += int(done)
        counts[1] += 1
    result.by_recipe = {r: (c[0], c[1]) for r, c in sorted(tally.items())}
    return result
