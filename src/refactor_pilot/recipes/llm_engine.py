"""The engine behind every recipe that needs a model's judgement (R2, R3, R4, R5).

What is deterministic and what is not:

  deterministic   finding the targets, working out which class, function or file
                  owns each, collecting the files and uses involved (clangd),
                  and checking the result: the edit applies exactly, the legacy
                  pattern is gone, public signatures are intact, tests are not
                  thinned, and the gates pass
  model           writing the edit

A *kind* describes one legacy pattern: how to find it, what to ask for, what
must be gone afterwards, and which rules apply. One target (a kind plus its
owner) becomes one change-set. The model answers with SEARCH/REPLACE blocks; a
rejected attempt is rolled back and its rejection reason is sent back for the
next attempt.
"""

from __future__ import annotations

import difflib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import edits as editmod
from ..llm import BudgetExceeded, Client, LlmError
from ..navigator import SymbolNavigator
from ..preproc import mask
from ..session import ChangeSet, Outcome, Session

HEADER_SUFFIXES = {".h", ".hh", ".hpp", ".hxx"}
MAX_PROMPT_CHARS = 60_000

Hit = tuple[int, str]  # (1-based line, stripped source line)


def hits(pattern: re.Pattern, source: str) -> list[Hit]:
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


# ---- kinds -----------------------------------------------------------------

_LOCK = re.compile(r"(?:\.|->)\s*(?:lock|unlock)\s*\(\s*\)")
_FILE_ANY = re.compile(r"\bfopen\s*\(|\bfclose\s*\(|\bFILE\s*\*")
_FILE_DECL = re.compile(r"\bFILE\s*\*\s*\w+\s*(?:;|=|\{)")  # a raw FILE* variable or member
_DELETE = re.compile(r"\bdelete\b(?:\s*\[\s*\])?\s*[\w(*:]")  # a delete-expression, not "= delete;"
_NEW = re.compile(r"\bnew\s+[A-Za-z_:]")
_OWN_ANY = re.compile(_DELETE.pattern + "|" + _NEW.pattern)
_ENABLE_IF = re.compile(r"\benable_if(?:_t)?\b")
_PRINTF = re.compile(r"\bsn?printf\s*\(")
_PTR_LEN = re.compile(r"\bconst\s+[\w:]+\s*\*\s*\w+\s*,\s*(?:std::)?size_t\s+\w+")
_PREFIX = re.compile(r"\.\s*compare\s*\(\s*0\s*,")
_UNSCOPED_ENUM = re.compile(r"\benum\s+(?!class\b|struct\b)\w+\s*(?::\s*[\w: ]+)?\{")
_DEFINE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+(\w+)(\([^)\n]*\))?[ \t]*(.*)$", re.MULTILINE)
_NOT_A_VALUE = re.compile(r"#|__LINE__|__FILE__|__func__|__VA_ARGS__|\.\.\.|[{};]|\b(?:do|return|if|while|for)\b|\\\\$")


def _regex(pattern: re.Pattern) -> Callable[[str, dict], list[Hit]]:
    return lambda source, context: hits(pattern, source)


def _macro_hits(source: str, context: dict) -> list[Hit]:
    """#defines that are plain values or expressions, so a constexpr can replace them.

    Left alone: include guards, empty flag macros, macros tested in #if/#ifdef
    anywhere in the repository, and anything using #, ##, __LINE__, statements
    or line continuations. Those must stay macros.
    """
    masked = mask(source)
    guard = re.search(r"^\s*#\s*ifndef\s+(\w+)\s*\n\s*#\s*define\s+\1\b", masked, re.MULTILINE)
    skip = set(context.get("condition_macros", ())) | ({guard.group(1)} if guard else set())
    lines, out = source.split("\n"), []
    for m in _DEFINE.finditer(masked):
        name, body = m.group(1), m.group(3).strip()
        if name in skip or not body or _NOT_A_VALUE.search(body):
            continue
        line = source.count("\n", 0, m.start()) + 1
        out.append((line, lines[line - 1].strip()))
    return out


@dataclass(frozen=True)
class Kind:
    name: str
    recipe: str
    goal: str
    find: Callable[[str, dict], list[Hit]]  # where the legacy pattern is (shown to the model)
    must_go: Callable[[str, dict], list[Hit]] | None = None  # what has to be gone to accept; default: find
    owner: str = "symbol"  # "symbol": enclosing class/function; "file"; "enum"
    min_standard: int = 11  # the lowest C++ standard the modern form needs
    api_check: bool = True  # keep public signatures textually identical (unless API changes are allowed)
    always_api: bool = False  # cannot be done at all without changing a public interface
    uses: str = "none"  # how other files depend on it: "pointer-api" (clangd) or "names" (text search)

    def remaining(self, source: str, context: dict) -> list[Hit]:
        return (self.must_go or self.find)(source, context)


KINDS: dict[str, Kind] = {k.name: k for k in [
    Kind("manual-lock", "r2",
         "Replace every manual `lock()` / `unlock()` pair on a mutex in {owner} with a scoped RAII lock "
         "(`std::lock_guard`, or `std::unique_lock` only where the lock really must be released early). "
         "Every early return must stay correctly unlocked. No explicit lock()/unlock() call may remain.",
         _regex(_LOCK)),
    Kind("file-handle", "r2",
         "Make {owner} hold its `FILE*` through RAII so it can never leak or be closed twice: for example a "
         "`std::unique_ptr<FILE, int (*)(FILE*)>` with `&fclose` as the deleter. No raw `FILE*` member or "
         "variable may remain. Keep the exact file contents and open/closed behaviour.",
         _regex(_FILE_ANY), must_go=_regex(_FILE_DECL)),
    Kind("raw-ownership", "r2",
         "Make ownership explicit in {owner}: every object it owns through a raw pointer and frees with "
         "`delete` must be held by a smart pointer (prefer `std::unique_ptr`; use `std::shared_ptr` only if "
         "ownership is really shared) or by value. No `delete` expression may remain in the files you edit. "
         "Pointers that merely observe an object owned elsewhere stay raw.",
         _regex(_OWN_ANY), must_go=_regex(_DELETE), uses="pointer-api"),
    Kind("macro", "r3",
         "Replace the value-like and function-like macros defined in {owner} with real C++: `constexpr` "
         "variables for constants, and `constexpr` function templates for function-like macros (which also "
         "stops them evaluating an argument twice). KEEP EVERY NAME EXACTLY AS IT IS, so that all existing "
         "uses compile unchanged, and declare each replacement where its uses can see it (inside the file's "
         "namespace if every use is inside it or qualifies it; otherwise at global scope). Leave include "
         "guards and any other `#define` not listed below untouched.",
         _macro_hits, owner="file", api_check=False, uses="names"),
    Kind("unscoped-enum", "r3",
         "Turn the unscoped enum {owner} into an `enum class`. Keep the enumerator names; every use must "
         "then be qualified with the enum's name. Update every use listed below.",
         _regex(_UNSCOPED_ENUM), owner="enum", always_api=True, api_check=False, uses="names"),
    Kind("sfinae", "r4",
         "Replace every `std::enable_if` / SFINAE constraint in {owner} with C++20 constraints: prefer "
         "standard concepts (`std::integral`, `std::floating_point`, ...) in the template parameter list "
         "or a `requires` clause. Overload resolution must stay exactly the same for every caller.",
         _regex(_ENABLE_IF), owner="file", min_standard=20, api_check=False),
    Kind("printf-buffer", "r5",
         "In {owner}, replace each fixed-size character buffer filled by `snprintf`/`sprintf` with "
         "`std::format`. The resulting string must be byte-for-byte what it was (for example `%.2f` becomes "
         "`{{:.2f}}`, `%u` and `%d` become `{{}}`). Remove the buffer. Add `#include <format>`.",
         _regex(_PRINTF), min_standard=20),
    Kind("pointer-length", "r5",
         "Replace the (pointer, length) parameter pair of {owner} with a `std::span<const T>`. Update its "
         "declaration, its definition and every caller listed below. A caller passing an array and its "
         "length should pass `std::span<const T>(array, length)` so the same elements are used.",
         _regex(_PTR_LEN), min_standard=20, always_api=True, api_check=False, uses="names"),
    Kind("prefix-compare", "r5",
         "In {owner}, replace each hand-written prefix test of the form `s.compare(0, n, prefix) == 0` "
         "(with or without a preceding size check) by `s.starts_with(prefix)`. Behaviour must be identical, "
         "including for an empty prefix.",
         _regex(_PREFIX), min_standard=20),
]}
RECIPE_KINDS: dict[str, tuple[str, ...]] = {}
for _kind in KINDS.values():
    RECIPE_KINDS[_kind.recipe] = RECIPE_KINDS.get(_kind.recipe, ()) + (_kind.name,)
_ORDER = {name: index for index, name in enumerate(KINDS)}


@dataclass
class Target:
    kind: str
    owner: str  # qualified name of the class, function or enum; or the file path
    owner_kind: str  # "class", "function", "enum" or "file"
    files: list[str]  # files that hold the owner; always editable
    api_symbols: list[str] = field(default_factory=list)  # names other files use to reach it
    callers: dict[str, list[str]] = field(default_factory=dict)  # other file -> the names it uses
    needs_api_change: bool = False  # cannot be done without changing a public interface
    shown: dict[str, Counter] = field(default_factory=dict)  # file -> owner's lines to show the model
    must_go: dict[str, Counter] = field(default_factory=dict)  # file -> owner's lines that must be gone

    @property
    def id(self) -> str:
        return f"{KINDS[self.kind].recipe}/{self.kind}/{self.owner}"


@dataclass
class RecipeResult:
    targets: list[Target] = field(default_factory=list)
    outcomes: list[Outcome] = field(default_factory=list)
    stopped: str = ""  # why the run ended early, if it did


# ---- planning (deterministic, uses clangd) --------------------------------


def repo_context(repo: Path, files: list[str]) -> dict:
    """Facts about the whole repository that a kind's detector may need."""
    tested: set[str] = set()
    for rel in files:
        text = mask((Path(repo) / rel).read_text(errors="replace"))
        for m in re.finditer(r"^\s*#\s*(?:ifdef|ifndef|if|elif)\b(.*)$", text, re.MULTILINE):
            tested |= set(re.findall(r"\b[A-Za-z_]\w*\b", m.group(1)))
    return {"condition_macros": tested}


def find_targets(
    nav: SymbolNavigator, repo: Path, sources: list[Path], allow_api_changes: bool,
    kinds: tuple[str, ...] = RECIPE_KINDS["r2"],
) -> list[Target]:
    repo = Path(repo)
    every = {p.relative_to(repo).as_posix() for p in sources}
    every |= {p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.suffix in HEADER_SUFFIXES and p.is_file()}
    every = sorted(every)
    files = [f for f in every if not is_test_file(f)]  # tests are the oracle, never a target
    context = repo_context(repo, every)
    texts = {f: (repo / f).read_text(errors="replace") for f in every}
    outlines = {f: nav.outline(f) for f in files}

    found: dict[tuple[str, str, str], Target] = {}
    factory_like: dict[tuple[str, str, str], bool] = {}
    for rel in files:
        for name in kinds:
            kind = KINDS[name]
            shown, gone = kind.find(texts[rel], context), kind.remaining(texts[rel], context)
            for line, text in sorted(set(shown) | set(gone)):
                owner = (rel, "file") if kind.owner == "file" else _enclosing_owner(outlines[rel], line, kind.owner)
                if owner is None:
                    continue
                key = (name, *owner)
                target = found.setdefault(key, Target(name, owner[0], owner[1], []))
                if rel not in target.files:
                    target.files.append(rel)
                if (line, text) in shown:
                    target.shown.setdefault(rel, Counter())[text] += 1
                if (line, text) in gone:
                    target.must_go.setdefault(rel, Counter())[text] += 1
                if name == "raw-ownership":
                    is_delete = bool(_DELETE.search(mask(texts[rel]).split("\n")[line - 1]))
                    factory_like[key] = factory_like.get(key, True) and not is_delete

    for key, target in found.items():
        kind = KINDS[target.kind]
        declared = []
        if target.owner_kind in ("class", "function"):
            declared = [s for s in nav.find(target.owner) if not is_test_file(s.location.file)]
            target.files = sorted(set(target.files) | {s.location.file for s in declared})
        if target.owner_kind == "class":  # every file that defines one of its members
            members = {f for f, syms in outlines.items() if any(s.name.startswith(target.owner + "::") for s in syms)}
            target.files = sorted(set(target.files) | members)
        if kind.uses == "pointer-api":
            _collect_pointer_api(nav, repo, outlines, target, declared)
            # new without delete in a free function: it hands a raw owning pointer out.
            target.needs_api_change = target.owner_kind == "function" and factory_like.get(key, False)
        elif kind.uses == "names":
            _collect_names(texts, target)
        target.needs_api_change = target.needs_api_change or kind.always_api
    return sorted(found.values(), key=lambda t: (_ORDER[t.kind], t.owner_kind not in ("class", "file", "enum"), t.owner))


def _enclosing_owner(outline: list, line: int, mode: str = "symbol") -> tuple[str, str] | None:
    wanted = ("enum",) if mode == "enum" else ("class", "struct", "function", "method", "constructor", "field")
    inside = [s for s in outline if (s.start_line or s.location.line) <= line <= max(s.end_line, s.location.line)
              and s.kind in wanted]
    if not inside:
        return None
    best = min(inside, key=lambda s: max(s.end_line, s.location.line) - (s.start_line or s.location.line))
    if best.kind == "enum":
        return best.name, "enum"
    if best.kind in ("class", "struct"):
        return best.name, "class"
    if best.kind == "function":
        return best.name, "function"
    return best.name.rsplit("::", 1)[0], "class"  # a member: the class owns the resource


def _collect_pointer_api(nav: SymbolNavigator, repo: Path, outlines: dict, target: Target, declared: list) -> None:
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


def _collect_names(texts: dict[str, str], target: Target) -> None:
    """Names the target introduces (macros, enumerators, a function), and the files that use them."""
    names: list[str] = []
    if target.kind == "macro":
        for counter in target.shown.values():
            for text in counter:
                m = _DEFINE.match(text)
                if m:
                    names.append(m.group(1))
    elif target.kind == "unscoped-enum":
        short = target.owner.rsplit("::", 1)[-1]
        names.append(short)
        for rel in target.files:
            body = re.search(r"\benum\s+" + re.escape(short) + r"\b[^{]*\{([^}]*)\}", mask(texts[rel]))
            if body:
                names += [part.split("=")[0].strip() for part in body.group(1).split(",") if part.split("=")[0].strip()]
    else:
        names.append(target.owner.rsplit("::", 1)[-1])
    target.api_symbols = list(dict.fromkeys(names))
    if not target.api_symbols:
        return
    pattern = re.compile(r"\b(?:" + "|".join(map(re.escape, target.api_symbols)) + r")\b")
    for rel, text in texts.items():
        if rel in target.files:
            continue
        used = list(dict.fromkeys(pattern.findall(mask(text))))
        if used:
            target.callers[rel] = used


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


def standard_number(standard: str) -> int:
    """'C++20' -> 20, 'C++2a' -> 20, '' (compiler default) -> 17."""
    aliases = {"0x": 11, "1y": 14, "1z": 17, "2a": 20, "2b": 23, "2c": 26, "98": 3, "03": 3}
    value = standard.replace("C++", "")
    if value in aliases:
        return aliases[value]
    return int(value) if value.isdigit() else 17


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
CALLERS_FROZEN = "Every other file must keep compiling exactly as it is; you cannot edit them."
CALLERS_OPEN = (
    "You MUST update every use listed below so the whole project still compiles. In test files, change only "
    "the lines that have to change; never remove or weaken a check."
)


def _current(target_lines: dict[str, Counter], rel: str, found: list[Hit]) -> list[Hit]:
    """Of the hits now in a file, the ones that belong to this target (matched by text)."""
    wanted = target_lines.get(rel, Counter())
    return [(line, text) for line, text in found if text in wanted]


def build_prompt(target: Target, repo: Path, standard: str, allow_api_changes: bool,
                 context: dict | None = None) -> tuple[str, set[str]]:
    kind, context = KINDS[target.kind], context or {}
    editable = list(target.files)
    if allow_api_changes:
        editable += [f for f in target.callers if f not in editable]
    parts = ["## Goal", kind.goal.format(owner=f"`{target.owner}`"), "", "## Rules"]
    if standard:
        parts.append(f"- The project compiles as {standard}. Use nothing newer"
                     + (" (so no `std::make_unique`, which is C++14)." if standard == "C++11" else "."))
    if kind.api_check:
        parts.append("- " + (API_OPEN if allow_api_changes else API_FROZEN))
    else:
        parts.append("- " + (CALLERS_OPEN if allow_api_changes and target.callers else CALLERS_FROZEN))
    parts.append("- Add any #include your change needs. Introduce no new compiler warnings.")
    parts.append("- Change only what the goal requires. Do not reformat, rename or tidy unrelated code. "
                 "If your change makes a comment wrong, correct the comment.")
    parts.append("- You may edit only these files: " + ", ".join(f"`{f}`" for f in editable))

    texts = {f: (repo / f).read_text(errors="replace") for f in dict.fromkeys(editable + list(target.callers))
             if (repo / f).exists()}
    parts += ["", "## Where the legacy pattern occurs"]
    for f in target.files:
        for line, text in _current(target.shown, f, kind.find(texts[f], context)):
            parts.append(f"- {f}:{line}: `{text}`")
    if target.callers:
        parts += ["", "## Callers you must keep compiling" if allow_api_changes
                  else "## Uses in other files (for reference; you cannot edit these files)"]
        for f, names in target.callers.items():
            if f not in texts:
                continue
            pattern = re.compile(r"\b(?:" + "|".join(map(re.escape, names)) + r")\b")
            for line, text in hits(pattern, texts[f])[:40]:
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


def make_accept(target: Target, before: dict[str, str], allow_api_changes: bool,
                context: dict | None = None) -> Callable[[Path], str | None]:
    kind, context = KINDS[target.kind], context or {}

    def accept(repo: Path) -> str | None:
        now = {f: (repo / f).read_text(errors="replace") for f in before}
        remaining = []
        for f in target.files:
            if target.kind == "raw-ownership" and target.needs_api_change:
                short = re.escape(target.owner.rsplit("::", 1)[-1])
                still = hits(re.compile(r"\*\s*" + short + r"\s*\("), now[f])  # still returns a raw pointer
                remaining += [f"{f}:{line}: {text}" for line, text in still]
                continue
            # Count by line text: of this target's own lines, as many must be gone
            # as it had. Identical lines belonging to other code in the file stay.
            owned = target.must_go.get(f, Counter())
            was, is_now = kind.remaining(before[f], context), kind.remaining(now[f], context)
            had, has = Counter(text for _, text in was), Counter(text for _, text in is_now)
            for line, text in is_now:
                if text in owned and has[text] > had[text] - owned[text]:
                    remaining.append(f"{f}:{line}: {text}")
            # Rewording a legacy line without removing the pattern does not count either.
            if not remaining and len(is_now) > len(was) - sum(owned.values()):
                remaining += [f"{f}:{line}: {text}" for line, text in is_now if text not in had]
        if remaining:
            listed = "\n".join(dict.fromkeys(remaining[:10]))
            return "The goal is not fully met. The legacy pattern is still present at:\n" + listed
        if kind.api_check and not allow_api_changes:
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
    kinds: tuple[str, ...] = RECIPE_KINDS["r2"],
    log: Callable[[str], None] = lambda m: None,
) -> RecipeResult:
    from .. import build as buildmod

    cfg, repo = session.cfg, session.cfg.repo
    result = RecipeResult()
    with SymbolNavigator(cfg) as nav:  # plan everything first, on the tree as it stands
        sources = buildmod.source_files(cfg, session.build_dir)
        result.targets = find_targets(nav, repo, sources, allow_api_changes, kinds)
        every = sorted({p.relative_to(repo).as_posix() for p in sources}
                       | {p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.suffix in HEADER_SUFFIXES})
    context = repo_context(repo, every)
    standard = language_standard(session.build_dir)
    level = standard_number(standard)
    log(f"{len(result.targets)} target(s) found; standard: {standard or 'compiler default'}")

    for target in result.targets:
        kind = KINDS[target.kind]
        description = f"{target.kind} in {target.owner}"
        if level < kind.min_standard:
            result.outcomes.append(_skip(session, target, description,
                                         f"needs C++{kind.min_standard}; the project compiles as {standard or 'the default'}. "
                                         "Raise it first: --recipe std,..."))
            continue
        if target.needs_api_change and not allow_api_changes:
            why = ("returns an owning raw pointer; fixing it changes a public signature"
                   if target.kind == "raw-ownership" else "changes how other files use it")
            result.outcomes.append(_skip(session, target, description, f"{why}. Rerun with --allow-api-changes"))
            continue
        prompt, editable = build_prompt(target, repo, standard, allow_api_changes, context)
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
            accept=make_accept(target, before, allow_api_changes, context),
            no_change_reason="the model's edit changed nothing",
        )
        try:
            result.outcomes.append(session.run(change))
        except (BudgetExceeded, LlmError) as exc:  # budget, network, bad key: the next target would fail too
            result.outcomes.append(session.outcomes[-1])
            result.stopped = str(exc)
            log(f"stopping: {exc}")
            break
    return result


def _skip(session: Session, target: Target, description: str, reason: str) -> Outcome:
    outcome = Outcome(target.id, description, "skipped", reason=reason)
    session.outcomes.append(outcome)
    return outcome
