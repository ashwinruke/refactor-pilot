"""Parse compiler and clang-tidy diagnostics into comparable records."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

_DIAG = re.compile(
    r"^(?P<file>(?:[A-Za-z]:)?[^:\n]+):(?P<line>\d+):(?P<col>\d+): "
    r"(?P<severity>warning|error): (?P<message>.*?)(?: \[(?P<flag>[^\]\s]+)\])?$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class Diagnostic:
    file: str  # repo-relative with forward slashes when inside the repo
    line: int
    col: int
    severity: str
    message: str
    flag: str  # e.g. -Wsign-compare or modernize-use-nullptr; "" when absent

    @property
    def key(self) -> str:
        """Identity that survives line shifts: edits move code, they should not
        make an old warning look new."""
        return f"{self.file}|{self.flag}|{self.message}"


def _relativize(path: str, repo: Path | None) -> str:
    p = Path(path)
    if repo is not None:
        try:
            return p.resolve().relative_to(repo.resolve()).as_posix()
        except (ValueError, OSError):
            pass
    return p.as_posix()


def parse(output: str, repo: Path | None = None) -> list[Diagnostic]:
    """Extract diagnostics, de-duplicated by exact location.

    A warning in a header is printed once per translation unit that includes
    it; it is still one warning.
    """
    seen: dict[tuple, Diagnostic] = {}
    for m in _DIAG.finditer(output):
        d = Diagnostic(
            file=_relativize(m["file"].strip(), repo),
            line=int(m["line"]),
            col=int(m["col"]),
            severity=m["severity"],
            message=m["message"].strip(),
            flag=m["flag"] or "",
        )
        seen.setdefault((d.file, d.line, d.col, d.severity, d.flag, d.message), d)
    return list(seen.values())


def count_by_key(diags: list[Diagnostic]) -> dict[str, int]:
    return dict(sorted(Counter(d.key for d in diags).items()))


def count_by_flag(diags: list[Diagnostic]) -> dict[str, int]:
    return dict(sorted(Counter(d.flag for d in diags).items()))
