"""R2: ownership and RAII. The first recipe that needs a model's judgement.

What is deterministic and what is not:

  deterministic   finding the targets (manual lock/unlock, FILE*, new/delete),
                  working out which class or function owns each, collecting the
                  files and call sites involved (clangd), checking the result
                  (goal check, API check, test guard, full gate)
  model           writing the edit: unique_ptr or shared_ptr or a value, how
                  ownership moves through each function, what the callers become

One target becomes one change-set. The model answers with SEARCH/REPLACE blocks;
a rejected attempt is rolled back and its rejection reason is sent back for the
next attempt.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import edits as editmod
from ..llm import BudgetExceeded, Client, LlmError
from ..navigator import SymbolNavigator
from ..session import ChangeSet, Outcome, Session

KINDS = ("manual-lock", "file-handle", "raw-ownership")
HEADER_SUFFIXES = {".h", ".hh", ".hpp", ".hxx"}
MAX_PROMPT_CHARS = 60_000

_LOCK = re.compile(r"(?:\.|->)\s*(?:lock|unlock)\s*\(\s*\)")
_FILE_ANY = re.compile(r"\bfopen\s*\(|\bfclose\s*\(|\bFILE\s*\*")
_FILE_DECL = re.compile(r"\bFILE\s*\*\s*\w+\s*(?:;|=|\{)")  # a raw FILE* variable or member
_DELETE = re.compile(r"\bdelete\b(?:\s*\[\s*\])?\s*[\w(*:]")  # a delete-expression, not "= delete;"
_NEW = re.compile(r"\bnew\s+[A-Za-z_:]")
_OWN_ANY = re.compile(_DELETE.pattern + "|" + _NEW.pattern)
_SCAN = {"manual-lock": _LOCK, "file-handle": _FILE_ANY, "raw-ownership": _OWN_ANY}

_MASK = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'', re.DOTALL)


def mask(source: str) -> str:
    """Blank out comments and string literals, keeping every line and column in place."""
    return _MASK.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), source)


def hits(pattern: re.Pattern, source: str) -> list[tuple[int, str]]:
    """(line number, source line) for each match outside comments and strings."""
    lines = source.split("\n")
    found: dict[int, str] = {}
    for m in pattern.finditer(mask(source)):
        line = source.count("\n", 0, m.start()) + 1
        found.setdefault(line, lines[line - 1].strip())
    return sorted(found.items())


def is_test_file(rel: str) -> bool:
    parts = Path(rel).parts
    return any(p.lower() in ("test", "tests", "testing", "unittests") for p in parts[:-1]) or \
        Path(rel).name.lower().startswith(("test_", "tests_")) or Path(rel).stem.lower().endswith(("_test", "_tests"))


def public_signatures(header: str) -> list[str]:
    """Function declarations a caller can see: everything outside private/protected.

    A line-based reading of a conventional header. It is the guard that stops an
    'internal only' edit from quietly changing the API.
    """
    access, out = "public", []
    for raw in mask(header).split("\n"):
        line = " ".join(raw.split())
        if re.match(r"(?:template\s*<.*>\s*)?class\b[^;]*$", line):
            access = "private"
        elif re.match(r"(?:template\s*<.*>\s*)?struct\b[^;]*$", line):
            access = "public"
        elif line.startswith("public:"):
            access = "public"
        elif line.startswith(("private:", "protected:")):
            access = "hidden"
        elif line.startswith("};"):
            access = "public"
        elif access == "public" and "(" in line and not line.startswith("#"):
            out.append(line)
    return sorted(out)


@dataclass
class Target:
    kind: str
    owner: str  # qualified name of the class or function
    owner_kind: str  # "class" or "function"
    files: list[str]  # files that hold the owner; always editable
    api_symbols: list[str] = field(default_factory=list)  # short names whose signature may need to change
    callers: dict[str, list[str]] = field(default_factory=dict)  # other file -> api symbols it uses
    needs_api_change: bool = False  # cannot be done without changing a public signature

    @property
    def id(self) -> str:
        return f"r2/{self.kind}/{self.owner}"


@dataclass
class R2Result:
    targets: list[Target] = field(default_factory=list)
    outcomes: list[Outcome] = field(default_factory=list)
    stopped: str = ""  # why the run ended early, if it did


# ---- planning (deterministic, uses clangd) --------------------------------


def find_targets(nav: SymbolNavigator, repo: Path, sources: list[Path], allow_api_changes: bool) -> list[Target]:
    repo = Path(repo)
    files = {p.relative_to(repo).as_posix() for p in sources}
    files |= {p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.suffix in HEADER_SUFFIXES and p.is_file()}
    files = sorted(f for f in files if not is_test_file(f))  # tests are the oracle, never a target

    outlines = {f: nav.outline(f) for f in files}
    grouped: dict[tuple[str, str, str], set[str]] = {}
    factory_like: dict[tuple[str, str, str], bool] = {}
    for rel in files:
        text = (repo / rel).read_text(errors="replace")
        for kind in KINDS:
            for line, _ in hits(_SCAN[kind], text):
                owner = _enclosing_owner(outlines[rel], line)
                if owner is None:
                    continue
                key = (kind, *owner)
                grouped.setdefault(key, set()).add(rel)
                if kind == "raw-ownership":
                    is_delete = bool(_DELETE.search(mask(text).split("\n")[line - 1]))
                    factory_like[key] = factory_like.get(key, True) and not is_delete

    targets = []
    for (kind, owner, owner_kind), where in grouped.items():
        target_files = set(where)
        declared = [s for s in nav.find(owner) if not is_test_file(s.location.file)]
        target_files |= {s.location.file for s in declared}
        if owner_kind == "class":  # every file that defines one of its members
            target_files |= {f for f, syms in outlines.items() if any(s.name.startswith(owner + "::") for s in syms)}
        target = Target(kind, owner, owner_kind, sorted(target_files))
        if kind == "raw-ownership":
            _collect_api(nav, repo, outlines, target, declared)
            # new without delete in a free function: it hands a raw owning pointer out.
            target.needs_api_change = owner_kind == "function" and factory_like.get((kind, owner, owner_kind), False)
        targets.append(target)
    order = {kind: i for i, kind in enumerate(KINDS)}
    targets.sort(key=lambda t: (order[t.kind], t.owner_kind != "class", t.owner))
    return targets


def _enclosing_owner(outline: list, line: int) -> tuple[str, str] | None:
    inside = [s for s in outline if s.location.line <= line <= max(s.end_line, s.location.line)
              and s.kind in ("class", "struct", "function", "method", "constructor", "field")]
    if not inside:
        return None
    best = min(inside, key=lambda s: max(s.end_line, s.location.line) - s.location.line)
    if best.kind in ("class", "struct"):
        return best.name, "class"
    if best.kind == "function":
        return best.name, "function"
    return best.name.rsplit("::", 1)[0], "class"  # a member: the class owns the resource


def _collect_api(nav: SymbolNavigator, repo: Path, outlines: dict, target: Target, declared: list) -> None:
    """Which functions pass raw pointers across the boundary, and who calls them."""
    candidates = []
    if target.owner_kind == "function":
        candidates = declared[:1]
    else:
        for rel in target.files:
            if Path(rel).suffix not in HEADER_SUFFIXES:
                continue
            lines = (repo / rel).read_text(errors="replace").split("\n")
            for sym in outlines.get(rel, []):
                if sym.kind == "method" and sym.name.startswith(target.owner + "::") \
                        and "*" in lines[sym.location.line - 1]:
                    candidates.append(sym)
    for sym in candidates:
        short = sym.name.rsplit("::", 1)[-1]
        if short not in target.api_symbols:
            target.api_symbols.append(short)
        for ref in nav.references(sym.location):
            if ref.file not in target.files and not Path(ref.file).is_absolute():
                names = target.callers.setdefault(ref.file, [])
                if short not in names:
                    names.append(short)


def language_standard(build_dir: Path) -> str:
    try:
        db = json.loads((Path(build_dir) / "compile_commands.json").read_text())
    except (OSError, ValueError):
        return ""
    for entry in db:
        command = entry.get("command") or " ".join(entry.get("arguments", []))
        m = re.search(r"-std=(?:gnu|c)\+\+(\w+)", command)
        if m:
            return "C++" + m.group(1)
    return ""


# ---- prompts ---------------------------------------------------------------

SYSTEM = """You are a senior C++ engineer modernizing a legacy codebase one small, verified step at a time.

Your edit is applied automatically, compiled with -Wall -Wextra, and run through the project's tests, \
including under AddressSanitizer and UndefinedBehaviorSanitizer. If anything fails, your edit is discarded \
and you are told why. So: preserve behaviour exactly, and make the smallest change that fully achieves the goal.

Answer ONLY with edit blocks in exactly this form, one per change:

<<<<<<< SEARCH path/to/file
<lines copied exactly from the file, including indentation>
=======
<the lines that replace them>
>>>>>>> REPLACE

Rules for edit blocks:
- SEARCH must match the file exactly and must match exactly one place. Include enough lines to be unique.
- Keep blocks small: one function or one declaration per block. Do not send a whole file.
- To add an #include, SEARCH an existing #include line and REPLACE it with that line plus the new one.
- No prose, no explanations, no markdown code fences around the blocks."""

GOALS = {
    "manual-lock": (
        "Replace every manual `lock()` / `unlock()` pair on a mutex in {owner} with a scoped RAII lock "
        "(`std::lock_guard`, or `std::unique_lock` only where the lock really must be released early). "
        "Every early return must stay correctly unlocked. No explicit lock()/unlock() call may remain."
    ),
    "file-handle": (
        "Make {owner} hold its `FILE*` through RAII so it can never leak or be closed twice: for example a "
        "`std::unique_ptr<FILE, int (*)(FILE*)>` with `&fclose` as the deleter. No raw `FILE*` member or "
        "variable may remain. Keep the exact file contents and open/closed behaviour."
    ),
    "raw-ownership": (
        "Make ownership explicit in {owner}: every object it owns through a raw pointer and frees with "
        "`delete` must be held by a smart pointer (prefer `std::unique_ptr`; use `std::shared_ptr` only if "
        "ownership is really shared) or by value. No `delete` expression may remain in the files you edit. "
        "Pointers that merely observe an object owned elsewhere stay raw."
    ),
}

API_FROZEN = (
    "Do NOT change the signature of any function declared outside `private:`/`protected:` in a header. "
    "Callers must keep compiling unchanged. Where a public function takes or returns an owning raw pointer, "
    "keep that signature and convert at the boundary (wrap an incoming pointer in a smart pointer at once; "
    "hand one out with `.release()` or `.get()` as the existing contract requires)."
)
API_OPEN = (
    "You MAY change public signatures so that ownership transfer is visible in the types "
    "(for example return `std::unique_ptr<T>` from a factory, take `std::unique_ptr<T>` where ownership is "
    "handed in). Functions that only observe keep raw pointers or references. You MUST then update every "
    "caller listed below so the whole project still compiles. In test files, change only the lines that have "
    "to change for the new signatures; never remove or weaken a check."
)


def build_prompt(target: Target, repo: Path, standard: str, allow_api_changes: bool) -> tuple[str, set[str]]:
    editable = list(target.files)
    if allow_api_changes:
        editable += [f for f in target.callers if f not in editable]
    parts = ["## Goal", GOALS[target.kind].format(owner=f"`{target.owner}`"), "", "## Rules"]
    if standard:
        parts.append(f"- The project compiles as {standard}. Use nothing newer"
                     + (" (so no `std::make_unique`, which is C++14)." if standard == "C++11" else "."))
    parts.append("- " + (API_OPEN if allow_api_changes else API_FROZEN))
    parts.append("- Add any #include your change needs. Introduce no new compiler warnings.")
    parts.append("- Change only what the goal requires. Do not reformat, rename or tidy unrelated code. "
                 "If your change makes a comment wrong, correct the comment.")
    parts.append("- You may edit only these files: " + ", ".join(f"`{f}`" for f in editable))

    texts = {f: (repo / f).read_text(errors="replace") for f in editable}
    parts += ["", "## Where the legacy pattern occurs"]
    for f in target.files:
        for line, text in hits(_SCAN[target.kind], texts[f]):
            parts.append(f"- {f}:{line}: `{text}`")
    if allow_api_changes and target.callers:
        parts += ["", "## Callers you must keep compiling"]
        for f, names in target.callers.items():
            pattern = re.compile(r"\b(?:" + "|".join(map(re.escape, names)) + r")\b")
            for line, text in hits(pattern, texts[f]):
                parts.append(f"- {f}:{line}: `{text}`")
    parts += ["", "## Files"]
    for f in editable:
        parts += [f"### {f}", "```cpp", texts[f].rstrip("\n"), "```", ""]
    return "\n".join(parts), set(editable)


RETRY = (
    "Your previous answer was rejected and rolled back. Every file is again exactly as shown in my first "
    "message.\n\nReason:\n{feedback}\n\nSend the complete, corrected set of edit blocks."
)


# ---- checks (deterministic) -------------------------------------------------


def make_accept(target: Target, before: dict[str, str], allow_api_changes: bool) -> Callable[[Path], str | None]:
    def accept(repo: Path) -> str | None:
        now = {f: (repo / f).read_text(errors="replace") for f in before}
        remaining = []
        for f in target.files:
            if target.kind == "manual-lock":
                pattern = _LOCK
            elif target.kind == "file-handle":
                pattern = _FILE_DECL
            elif target.needs_api_change:
                short = re.escape(target.owner.rsplit("::", 1)[-1])
                pattern = re.compile(r"\*\s*" + short + r"\s*\(")  # still returns a raw pointer
            else:
                pattern = _DELETE
            remaining += [f"{f}:{line}: {text}" for line, text in hits(pattern, now[f])]
        if remaining:
            return "The goal is not fully met. The legacy pattern is still present at:\n" + "\n".join(remaining[:10])
        if not allow_api_changes:
            for f in target.files:
                if Path(f).suffix in HEADER_SUFFIXES and public_signatures(before[f]) != public_signatures(now[f]):
                    lost = sorted(set(public_signatures(before[f])) - set(public_signatures(now[f])))
                    return (f"You changed a public signature in {f}, which is not allowed in this run. "
                            "These declarations must stay exactly as they were:\n" + "\n".join(lost[:10]))
        for f in before:
            if is_test_file(f) and before[f] != now[f]:
                problem = _weakened_test(f, before[f], now[f], target.api_symbols)
                if problem:
                    return problem
        return None
    return accept


_ASSERTION = re.compile(r"\b(?:CHECK|ASSERT|EXPECT|REQUIRE|VERIFY|TEST|assert)\w*\s*\(")


def _weakened_test(rel: str, before: str, after: str, api_symbols: list[str]) -> str | None:
    """Tests are the oracle. Following an API change, they may be adapted, not thinned.

    Allowed: rewriting a line that uses a changed function, and dropping a
    `delete` that a smart pointer made unnecessary. Refused: removing any other
    line, rewriting an unrelated line, or ending up with fewer assertions.
    """
    related = re.compile(r"\b(?:" + "|".join(map(re.escape, api_symbols or ["\0"])) + r")\b")
    old, new = before.split("\n"), after.split("\n")
    bad = []
    for op, i1, i2, _, _ in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        for line in (l.strip() for l in old[i1:i2]):
            if not line:
                continue
            if op == "delete" and not _DELETE.search(line + " "):
                bad.append(line)  # removed outright, and it was not a delete statement
            elif op == "replace" and not (related.search(line) or _DELETE.search(line + " ")):
                bad.append(line)  # rewritten although it does not use a changed function
    if bad:
        return (f"In the test file {rel} you removed or rewrote lines that have nothing to do with the changed "
                "signatures. Tests may only be adapted, never weakened. Leave these exactly as they were:\n"
                + "\n".join(bad[:10]))
    lost = len(_ASSERTION.findall(mask(before))) - len(_ASSERTION.findall(mask(after)))
    if lost > 0:
        return (f"The test file {rel} has {lost} fewer assertion(s) than before. Tests may only be adapted, "
                "never weakened. Keep every check.")
    return None


# ---- the recipe -------------------------------------------------------------


def run(
    session: Session,
    client: Client,
    allow_api_changes: bool = False,
    max_attempts: int = 3,
    kinds: tuple[str, ...] = KINDS,
    log: Callable[[str], None] = lambda m: None,
) -> R2Result:
    from .. import build as buildmod

    cfg, repo = session.cfg, session.cfg.repo
    result = R2Result()
    with SymbolNavigator(cfg) as nav:  # plan everything first, on the untouched tree
        sources = buildmod.source_files(cfg, session.build_dir)
        result.targets = [t for t in find_targets(nav, repo, sources, allow_api_changes) if t.kind in kinds]
    standard = language_standard(session.build_dir)
    log(f"{len(result.targets)} ownership target(s) found; standard: {standard or 'compiler default'}")

    for target in result.targets:
        description = f"{target.kind} in {target.owner}"
        if target.needs_api_change and not allow_api_changes:
            result.outcomes.append(_skip(session, target, description,
                                         "returns an owning raw pointer; fixing it changes a public "
                                         "signature. Rerun with --allow-api-changes"))
            continue
        prompt, editable = build_prompt(target, repo, standard, allow_api_changes)
        if len(prompt) > MAX_PROMPT_CHARS:
            result.outcomes.append(_skip(session, target, description,
                                         f"too large for one prompt ({len(prompt)} characters)"))
            continue
        before = {f: (repo / f).read_text(errors="replace") for f in editable}
        conversation = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]

        def apply(repo_dir: Path, feedback: str | None, conversation=conversation, editable=editable, tag=target.id):
            if feedback is not None:
                conversation.append({"role": "user", "content": RETRY.format(feedback=feedback)})
            completion = client.complete(conversation, tag=tag)
            conversation.append({"role": "assistant", "content": completion.text})
            editmod.apply(editmod.parse(completion.text), repo_dir, editable)

        change = ChangeSet(
            id=target.id, description=description, apply=apply, max_attempts=max_attempts,
            gate="full" if session.verifier.sanitizers else "fast",
            accept=make_accept(target, before, allow_api_changes),
            no_change_reason="the model's edit changed nothing",
        )
        try:
            result.outcomes.append(session.run(change))
        except BudgetExceeded as exc:
            result.outcomes.append(session.outcomes[-1])
            result.stopped = str(exc)
            log(f"stopping: {exc}")
            break
        except LlmError as exc:  # network down, bad key, quota: no point trying the next target
            result.outcomes.append(session.outcomes[-1])
            result.stopped = str(exc)
            log(f"stopping: {exc}")
            break
    return result


def _skip(session: Session, target: Target, description: str, reason: str) -> Outcome:
    outcome = Outcome(target.id, description, "skipped", reason=reason)
    session.outcomes.append(outcome)
    return outcome
