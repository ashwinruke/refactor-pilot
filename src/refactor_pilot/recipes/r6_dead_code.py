"""R6: dead-code pruning, in confidence tiers. Deterministic; no LLM.

Tier 1 - removed automatically (the code provably cannot run or be reached):
    #if 0 blocks                          the preprocessor already drops them
    branches of flags you declare stale   --flag NAME=off|on
    if/else on a file-local bool constant folded to the live branch
    unused #includes                      clangd's include analysis
    unused internal-linkage functions     the compiler's own -Wunused-function

Tier 2 - reported, removed only with --allow-api-changes:
    free functions declared in a header that nothing in the repository
    references. Static analysis cannot see callers outside the repository,
    callers through function pointers assembled from strings, or other
    build configurations, so a person has to make that call.

Every removal is one change-set and passes the gates like any other edit.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import build as buildmod
from .. import preproc
from ..navigator import SymbolNavigator
from ..session import ChangeSet, Outcome, Session
from .r2_ownership import HEADER_SUFFIXES, is_test_file

_BOOL_CONST = re.compile(
    r"^[ \t]*(?:static\s+)?(?:const|constexpr)\s+(?:static\s+)?bool\s+(\w+)\s*(?:=\s*(true|false)|\{\s*(true|false)\s*\})\s*;",
    re.MULTILINE,
)
_CONDITION_MACROS = re.compile(r"^\s*#\s*(?:ifdef|ifndef|if|elif)\b(.*)$", re.MULTILINE)
_WELL_KNOWN = re.compile(r"^(?:_|NDEBUG$|DEBUG$|WIN32$|WIN64$|UNIX$|APPLE$|ANDROID$|linux$|unix$|defined$|true$|false$)")


@dataclass
class R6Result:
    outcomes: list[Outcome] = field(default_factory=list)
    flag_candidates: list[str] = field(default_factory=list)  # tested but never defined in this build
    unreferenced_public: list[str] = field(default_factory=list)


def repo_files(cfg, build_dir: Path) -> list[str]:
    """Project sources and headers, repo-relative, tests excluded."""
    repo = cfg.repo
    files = {p.relative_to(repo).as_posix() for p in buildmod.source_files(cfg, build_dir)}
    files |= {p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.suffix in HEADER_SUFFIXES and p.is_file()}
    return sorted(f for f in files if not is_test_file(f))


def undefined_flag_candidates(cfg, build_dir: Path, files: list[str]) -> list[str]:
    """Macros tested in #if/#ifdef that nothing in this build defines.

    A hint only: such a macro may be a platform macro, or defined by a build
    configuration that is not the one in front of us.
    """
    repo = cfg.repo
    tested: set[str] = set()
    defined: set[str] = set()
    guards: set[str] = set()
    for rel in files:
        text = preproc.mask((repo / rel).read_text(errors="replace"))
        for m in _CONDITION_MACROS.finditer(text):
            tested |= set(re.findall(r"\b[A-Za-z_]\w*\b", m.group(1)))
        defined |= set(re.findall(r"^\s*#\s*define\s+(\w+)", text, re.MULTILINE))
        guard = re.search(r"^\s*#\s*ifndef\s+(\w+)\s*\n\s*#\s*define\s+\1\b", text, re.MULTILINE)
        if guard:
            guards.add(guard.group(1))
    build_text = ""
    for cmake in list(repo.rglob("CMakeLists.txt")) + list(repo.rglob("*.cmake")):
        build_text += cmake.read_text(errors="replace")
    try:
        for entry in json.loads((Path(build_dir) / "compile_commands.json").read_text()):
            build_text += " " + (entry.get("command") or " ".join(entry.get("arguments", [])))
    except (OSError, ValueError):
        pass
    return sorted(
        name for name in tested - defined - guards
        if not _WELL_KNOWN.match(name) and not re.search(r"\b" + re.escape(name) + r"\b", build_text)
    )


def run(
    session: Session,
    flags: dict[str, bool] | None = None,
    allow_api_changes: bool = False,
    log: Callable[[str], None] = lambda m: None,
) -> R6Result:
    cfg, repo, build_dir = session.cfg, session.cfg.repo, session.build_dir
    flags = flags or {}
    result = R6Result()
    files = repo_files(cfg, build_dir)

    def read(rel: str) -> str:
        return (repo / rel).read_text(errors="replace")

    def rewrite(rel: str, transform: Callable[[str], str]) -> Callable[[Path, str | None], None]:
        def apply(repo_dir: Path, feedback: str | None) -> None:
            path = repo_dir / rel
            path.write_text(transform(path.read_text(errors="replace")))
        return apply

    with SymbolNavigator(cfg) as nav:
        def submit(change: ChangeSet) -> Outcome:
            outcome = session.run(change)
            result.outcomes.append(outcome)
            for rel in outcome.files:  # keep clangd in step with the tree, kept or rolled back
                nav.invalidate(rel)
            return outcome

        # Tier 2 is planned first: it needs the cross-file index of the untouched tree.
        public_dead = _unreferenced_public_functions(nav, repo, files)
        result.unreferenced_public = [name for name, _ in public_dead]
        result.flag_candidates = [f for f in undefined_flag_candidates(cfg, build_dir, files) if f not in flags]

        # ---- #if 0 / #if 1
        for rel in files:
            _, count = preproc.simplify_conditionals(read(rel), lambda g: preproc.is_literal(g.expr) if g.kind == "if" else None)
            if count:
                submit(ChangeSet(
                    f"r6/if-literal/{rel}", f"tier 1: remove {count} `#if 0`/`#if 1` block(s) in {rel}",
                    rewrite(rel, lambda text: preproc.simplify_conditionals(
                        text, lambda g: preproc.is_literal(g.expr) if g.kind == "if" else None)[0]),
                ))

        # ---- branches of flags declared stale
        for name, is_on in flags.items():
            def decide(group, name=name, is_on=is_on):
                flag = preproc.flag_of(group)
                return None if flag is None or flag[0] != name else (flag[1] == is_on)
            for rel in files:
                _, count = preproc.simplify_conditionals(read(rel), decide)
                if count:
                    state = "on" if is_on else "off"
                    submit(ChangeSet(
                        f"r6/flag/{name}/{rel}", f"tier 1: resolve {count} branch(es) of {name} (declared {state}) in {rel}",
                        rewrite(rel, lambda text, decide=decide: preproc.simplify_conditionals(text, decide)[0]),
                    ))

        # ---- if/else on a constant that only this file can see
        for rel in files:
            if Path(rel).suffix in HEADER_SUFFIXES:
                continue  # a header constant is visible to every includer
            for m in _BOOL_CONST.finditer(preproc.mask(read(rel))):
                name, value = m.group(1), (m.group(2) or m.group(3)) == "true"
                _, count = preproc.fold_constant_ifs(read(rel), name, value)
                if not count:
                    continue

                def fold(text: str, name=name, value=value) -> str:
                    text, _ = preproc.fold_constant_ifs(text, name, value)
                    uses = re.findall(r"\b" + re.escape(name) + r"\b", preproc.mask(text))
                    if len(uses) == 1:  # only its own declaration is left
                        decl = _BOOL_CONST.search(preproc.mask(text))
                        while decl and decl.group(1) != name:
                            decl = _BOOL_CONST.search(preproc.mask(text), decl.end())
                        if decl:
                            line = text.count("\n", 0, decl.start()) + 1
                            text = preproc.remove_lines(text, line, line)
                    return text

                submit(ChangeSet(
                    f"r6/constant-flag/{rel}:{name}",
                    f"tier 1: fold {count} `if ({name})` (always {str(value).lower()}) in {rel}",
                    rewrite(rel, fold),
                ))

        # ---- unused #includes (clangd include analysis)
        for rel in files:
            unused = sorted({d.location.line for d in nav.diagnostics(rel) if d.code == "unused-includes"}, reverse=True)
            if unused:
                def drop(text: str, unused=unused) -> str:
                    lines = text.split("\n")
                    for line in unused:
                        if re.match(r"\s*#\s*include\b", lines[line - 1]):
                            del lines[line - 1]
                    return preproc.collapse_blank_lines("\n".join(lines))
                submit(ChangeSet(f"r6/unused-include/{rel}", f"tier 1: remove {len(unused)} unused #include(s) in {rel}",
                                 rewrite(rel, drop)))

        # ---- unused internal-linkage functions; removing one can orphan another
        for rel in files:
            tried: set[str] = set()
            for _ in range(20):
                target = _next_unused_internal(nav, rel, tried)
                if target is None:
                    break
                name, first, last = target
                tried.add(name)
                submit(ChangeSet(
                    f"r6/unused-function/{rel}:{name}", f"tier 1: remove unused internal function {name} in {rel}",
                    rewrite(rel, lambda text, first=first, last=last: preproc.remove_lines(text, first, last)),
                ))

        # ---- tier 2: unreferenced functions in public headers
        for name, sites in public_dead:
            description = f"tier 2: {name} is declared in a header but referenced nowhere in the repository"
            if not allow_api_changes:
                outcome = Outcome(f"r6/unreferenced-public/{name}", description, "skipped",
                                  reason="may have callers outside this repository; remove with --allow-api-changes")
                session.outcomes.append(outcome)
                result.outcomes.append(outcome)
                continue
            touched = sorted({file for file, _ in sites})

            def remove_everywhere(repo_dir: Path, feedback: str | None, name=name, touched=touched) -> None:
                short = name.rsplit("::", 1)[-1]
                for rel in touched:
                    nav.invalidate(rel)
                    for sym in sorted(nav.outline(rel), key=lambda s: -s.location.line):
                        if sym.kind == "function" and sym.name.rsplit("::", 1)[-1] == short and sym.name.endswith(name.split("::", 1)[-1]):
                            path = repo_dir / rel
                            path.write_text(preproc.remove_lines(path.read_text(errors="replace"),
                                                                 sym.start_line or sym.location.line, sym.end_line))

            submit(ChangeSet(f"r6/unreferenced-public/{name}",
                             f"tier 2: remove unreferenced public function {name} ({', '.join(touched)})",
                             remove_everywhere))
    committed = sum(o.status == "committed" for o in result.outcomes)
    log(f"dead code: {committed} removal(s) committed, {len(result.unreferenced_public)} unreferenced public function(s)")
    return result


def _next_unused_internal(nav: SymbolNavigator, rel: str, tried: set[str]) -> tuple[str, int, int] | None:
    flagged = [d for d in nav.diagnostics(rel) if d.code in ("-Wunused-function", "-Wunused-const-variable")]
    if not flagged:
        return None
    outline = nav.outline(rel)
    for diag in flagged:
        for sym in outline:
            if sym.location.line == diag.location.line and sym.kind in ("function", "variable", "constant") \
                    and sym.name not in tried:
                return sym.name, sym.start_line or sym.location.line, max(sym.end_line, sym.location.line)
    return None


def _unreferenced_public_functions(nav: SymbolNavigator, repo: Path, files: list[str]) -> list[tuple[str, set]]:
    """Free functions declared in a header whose only references are their own declarations."""
    outlines = {rel: nav.outline(rel) for rel in files}
    sites: dict[str, set[tuple[str, int]]] = {}
    for rel, outline in outlines.items():
        for sym in outline:
            if sym.kind == "function":
                sites.setdefault(sym.name, set()).add((rel, sym.location.line))
    dead = []
    for rel, outline in outlines.items():
        if Path(rel).suffix not in HEADER_SUFFIXES:
            continue
        for sym in outline:
            short = sym.name.rsplit("::", 1)[-1]
            if sym.kind != "function" or short == "main" or short.startswith("operator"):
                continue
            line_text = (repo / rel).read_text(errors="replace").split("\n")[sym.location.line - 1]
            if "template" in line_text or "inline" in line_text:
                continue  # header-only code: absence of calls in this repo proves little
            references = {(r.file, r.line) for r in nav.references(sym.location)}
            if references and references <= sites.get(sym.name, set()):
                dead.append((sym.name, sites[sym.name]))
    return sorted(dead, key=lambda item: item[0])
