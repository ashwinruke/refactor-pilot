"""std: raise the project's C++ language standard.

Recipes that need newer language or library features (concepts, std::format,
std::span, starts_with) can only run once the project compiles in that mode, and
clang-tidy only offers a fix when the project's standard allows it. So raising
the standard is a recipe of its own, and usually the first one in a pipeline.

It edits the CMake files where the standard is set. If no file sets one, it adds
`set(CMAKE_CXX_STANDARD N)` after the top-level project() call. The change must
still build and pass every test, sanitizers included. Warnings are allowed to
change, because a newer standard changes what the compiler diagnoses; the new
set becomes the baseline for the recipes that follow.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from ..session import ChangeSet, Outcome, Session
from .llm_engine import language_standard, standard_number

_PATTERNS = [
    (re.compile(r"(\bCMAKE_CXX_STANDARD\s+)(\d+)"), r"\g<1>{std}"),
    (re.compile(r"(\bCXX_STANDARD\s+)(\d+)"), r"\g<1>{std}"),
    (re.compile(r"(\bcxx_std_)(\d+)"), r"\g<1>{std}"),
    (re.compile(r"(-std=(?:c|gnu)\+\+)(\w+)"), r"\g<1>{std}"),
]
_PROJECT = re.compile(r"^[ \t]*project\s*\([^)]*\)[ \t]*\n", re.IGNORECASE | re.MULTILINE)


def raise_in_text(text: str, std: int) -> tuple[str, int]:
    """Rewrite every place a CMake file names a lower standard. Returns (text, edits)."""
    count = 0
    for pattern, template in _PATTERNS:
        def bump(m: re.Match) -> str:
            nonlocal count
            current = standard_number("C++" + m.group(2))
            if current >= std:
                return m.group(0)
            count += 1
            return m.expand(template.format(std=std))
        text = pattern.sub(bump, text)
    return text, count


def cmake_files(repo: Path) -> list[Path]:
    return sorted(p for p in list(repo.rglob("CMakeLists.txt")) + list(repo.rglob("*.cmake")) if p.is_file())


def run(session: Session, std: int = 20, log: Callable[[str], None] = lambda m: None) -> Outcome:
    cfg, repo = session.cfg, session.cfg.repo
    current = language_standard(session.build_dir)
    if current and standard_number(current) >= std:
        outcome = Outcome("std/raise", f"raise the language standard to C++{std}", "skipped",
                          reason=f"already compiles as {current}")
        session.outcomes.append(outcome)
        return outcome
    override = f"-DCMAKE_CXX_STANDARD={std}"

    def apply(repo_dir: Path, feedback: str | None) -> None:
        edited = 0
        for path in cmake_files(repo_dir):
            text, count = raise_in_text(path.read_text(errors="replace"), std)
            if count:
                path.write_text(text)
                edited += count
        if not edited:  # nothing sets a standard: say so explicitly, once, at the top
            top = repo_dir / "CMakeLists.txt"
            text = top.read_text(errors="replace")
            m = _PROJECT.search(text)
            if not m:
                raise RuntimeError("no project() call found in the top-level CMakeLists.txt")
            line = f"set(CMAKE_CXX_STANDARD {std})\nset(CMAKE_CXX_STANDARD_REQUIRED ON)\n"
            top.write_text(text[:m.end()] + line + text[m.end():])
        # A standard stored as a CMake cache variable keeps its old value in an
        # existing build tree, so tell this session's builds explicitly as well.
        if "-UCMAKE_CXX_STANDARD" in cfg.cmake_args:
            cfg.cmake_args.remove("-UCMAKE_CXX_STANDARD")
        if override not in cfg.cmake_args:
            cfg.cmake_args.append(override)

    was = current or "the compiler default"
    outcome = session.run(ChangeSet(
        "std/raise", f"raise the language standard from {was} to C++{std}", apply, rebaseline=True,
    ))
    if outcome.status != "committed":
        # Undo the override too. Dropping the argument is not enough: CMake
        # remembers a -D value in the build tree's cache. -U removes the cached
        # entry, so the restored CMake files decide the standard again.
        if override in cfg.cmake_args:
            cfg.cmake_args.remove(override)
        if "-UCMAKE_CXX_STANDARD" not in cfg.cmake_args:
            cfg.cmake_args.append("-UCMAKE_CXX_STANDARD")
    else:
        shift = session.warning_shift
        note = "no change in warnings" if shift == 0 else f"{abs(shift)} warning(s) {'more' if shift > 0 else 'fewer'}"
        log(f"now compiling as C++{std}; {note}; this is the new baseline")
    return outcome
