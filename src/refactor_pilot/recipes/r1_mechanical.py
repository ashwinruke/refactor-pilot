"""R1: mechanical modernization, driven by clang-tidy fix-its. No LLM.

For each check, in order, and for each translation unit that still has findings:
one change-set = "apply this check's fix-its to this file and the headers it
includes". The fix is computed against the tree as it is at that moment, so
earlier commits (or rollbacks) never leave it working from stale positions.

clang-tidy decides *what* to change. The Session decides *whether it stays*:
every change-set must pass the gates like any other edit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import build as buildmod
from ..build import NO_HEADERS
from ..diagnostics import Diagnostic
from ..session import ChangeSet, Outcome, Session

# Ordered: earlier checks make later ones simpler (loops become range-for before
# the remaining iterator declarations become auto).
DEFAULT_CHECKS = [
    "modernize-use-nullptr",
    "modernize-use-override",
    "modernize-use-equals-default",
    "modernize-use-equals-delete",
    "modernize-use-using",
    "modernize-deprecated-headers",
    "modernize-redundant-void-arg",
    "modernize-use-bool-literals",
    "modernize-loop-convert",
    "modernize-use-auto",
    "modernize-use-emplace",
    "modernize-use-default-member-init",
    "modernize-return-braced-init-list",
    "modernize-use-noexcept",
    "google-readability-casting",
    # The checks below need a newer standard. clang-tidy stays silent about them
    # until the project compiles in that mode, so listing them costs nothing at
    # C++11 and takes effect once the `std` recipe has run.
    "modernize-make-unique",                 # C++14
    "modernize-make-shared",
    "modernize-use-constraints",             # C++20: enable_if -> requires
    "modernize-type-traits",                 # C++14/17: _t and _v forms
    "modernize-use-starts-ends-with",        # C++20
    "modernize-use-std-numbers",             # C++20
    "readability-container-contains",        # C++20
]
# Deliberately not here:
#   modernize-macro-to-enum, modernize-avoid-c-arrays  -> recipe R3/R5 (need judgement)
#   modernize-use-trailing-return-type, modernize-use-nodiscard -> style, very noisy
#   modernize-pass-by-value -> rewrites constructor signatures in public headers;
#       API changes are opt-in: pass it with --checks if you want it
#   modernize-make-unique and other C++14+ fixes -> need the standard raised first

NO_FIX = "clang-tidy offers no automatic fix"


@dataclass
class CheckStats:
    check: str
    committed: int = 0
    rolled_back: int = 0
    no_fix: int = 0


@dataclass
class R1Result:
    stats: list[CheckStats] = field(default_factory=list)
    outcomes: list[Outcome] = field(default_factory=list)


def _key(d: Diagnostic) -> str:
    return f"{d.file}|{d.message}"


def run(session: Session, checks: list[str] | None = None, log: Callable[[str], None] = lambda m: None) -> R1Result:
    cfg, build_dir = session.cfg, session.build_dir
    result = R1Result()

    def fixer(tu: Path, check: str, header_filter: str | None) -> Callable[[Path, str | None], None]:
        def apply(repo: Path, feedback: str | None) -> None:
            buildmod.tidy_file(cfg, build_dir, tu, f"-*,{check}", header_filter, fix=True)
        return apply

    for check in checks or DEFAULT_CHECKS:
        stats = CheckStats(check)
        short = check.split("-", 1)[1] if "-" in check else check
        settled: set[str] = set()  # findings already tried: no fix-it, or the gate said no
        for tu in buildmod.source_files(cfg, build_dir):
            rel = tu.relative_to(cfg.repo).as_posix()
            findings = [
                d for d in buildmod.tidy_file(cfg, build_dir, tu, f"-*,{check}")
                if d.flag == check and _key(d) not in settled and not Path(d.file).is_absolute()
            ]
            if not findings:
                continue
            files = sorted({d.file for d in findings})
            outcome = session.run(ChangeSet(
                id=f"r1/{short}/{rel}",
                description=f"{check}: {len(findings)} finding(s) in {', '.join(files)}",
                apply=fixer(tu, check, None),
                no_change_reason=NO_FIX,
            ))
            result.outcomes.append(outcome)
            if outcome.status == "rolled_back" and files != [rel]:
                # A header fix may be the problem. Retry with this file alone so its
                # own fixes are not lost along with the header's.
                own = [d for d in findings if d.file == rel]
                if own:
                    outcome = session.run(ChangeSet(
                        id=f"r1/{short}/{rel}#own",
                        description=f"{check}: {len(own)} finding(s) in {rel} (headers excluded)",
                        apply=fixer(tu, check, NO_HEADERS),
                    ))
                    result.outcomes.append(outcome)
            if outcome.status == "committed":
                stats.committed += 1
            elif outcome.status == "no_change":
                stats.no_fix += 1
                settled |= {_key(d) for d in findings}
            else:
                stats.rolled_back += 1
                settled |= {_key(d) for d in findings}
        if stats.committed or stats.rolled_back or stats.no_fix:
            result.stats.append(stats)
            log(f"{check}: {stats.committed} committed, {stats.rolled_back} rolled back, {stats.no_fix} without a fix")
    return result
