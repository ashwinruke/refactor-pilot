"""Run a list of recipes on one branch and measure what happened.

This is what both `refactor-pilot run` and `refactor-pilot bench` call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import build as buildmod
from . import diagnostics
from .baseline import Snapshot
from .evalkit import score as scoremod
from .llm import Client
from .recipes import llm_engine, r1_mechanical, r6_dead_code, std_raise
from .session import Report, Session

RECIPES = {
    "std": "raise the C++ standard (default C++20)",
    "r1": "mechanical fixes from clang-tidy",
    "r2": "ownership and RAII (LLM)",
    "r3": "macros to constexpr, enum class (LLM)",
    "r4": "SFINAE to concepts (LLM; clang-tidy in r1 does most of it at C++20)",
    "r5": "C++20 library: std::format, std::span, starts_with (LLM)",
    "r6": "dead-code pruning",
}
ALL = ["std", "r1", "r6", "r2", "r3", "r4", "r5"]
LLM_RECIPES = ("r2", "r3", "r4", "r5")


def parse_recipes(text: str) -> list[str]:
    """'std, r1,all' -> ['std', 'r1', 'std', 'r1', 'r6', ...]. Raises ValueError on an unknown name."""
    recipes: list[str] = []
    for name in (r.strip().lower() for r in text.split(",") if r.strip()):
        if name == "all":
            recipes += ALL
        elif name in RECIPES:
            recipes.append(name)
        else:
            raise ValueError(f"unknown recipe '{name}'. Choose from: {', '.join(RECIPES)}, all")
    if not recipes:
        raise ValueError(f"no recipe given. Choose from: {', '.join(RECIPES)}, all")
    return recipes


@dataclass
class PipelineOptions:
    repo: Path
    recipes: list[str]
    branch: str | None = None
    work_dir: Path | None = None
    compiler: str = "clang++"
    sanitizers: bool = True
    full_every: int = 5
    allow_dirty: bool = False
    keep_worktree: bool = False
    checks: list[str] | None = None  # r1
    flags: dict[str, bool] = field(default_factory=dict)  # r6
    allow_api_changes: bool = False
    std: int = 20
    max_attempts: int = 3
    manifest: Path | None = None
    cmake_args: list[str] = field(default_factory=list)
    test_timeout: int = 120
    measure_tidy: bool = True
    discard_branch: bool = False  # delete the result branch after measuring (benchmarks)
    continue_branch: str | None = None  # add to this existing branch instead of making a new one


@dataclass
class PipelineResult:
    report: Report
    baseline: Snapshot
    worktree: Path
    warnings_before: int = 0
    warnings_after: int = 0
    tidy_before: dict[str, int] = field(default_factory=dict)
    tidy_after: dict[str, int] = field(default_factory=dict)
    score_before: scoremod.Score | None = None
    score_after: scoremod.Score | None = None
    diffstat: tuple[int, int, int] = (0, 0, 0)
    notes: list[str] = field(default_factory=list)
    stopped: str = ""
    requests: int = 0
    tokens: int = 0

    def by_recipe(self) -> dict[str, dict[str, int]]:
        """{'r1': {'committed': 19, 'no_change': 2}, ...} in pipeline order."""
        table: dict[str, dict[str, int]] = {}
        for outcome in self.report.outcomes:
            row = table.setdefault(outcome.id.split("/", 1)[0], {})
            row[outcome.status] = row.get(outcome.status, 0) + 1
        return table


def run_pipeline(
    opts: PipelineOptions,
    client: Client | None = None,
    log: Callable[[str], None] = lambda message: None,
    on_start: Callable[[Session], None] = lambda session: None,
) -> PipelineResult:
    if any(r in LLM_RECIPES for r in opts.recipes) and client is None:
        raise ValueError("recipes r2-r5 need a model client")
    session = Session(
        opts.repo, work_dir=opts.work_dir, compiler=opts.compiler, branch=opts.branch,
        sanitizers=opts.sanitizers, full_every=opts.full_every, allow_dirty=opts.allow_dirty,
        continue_branch=opts.continue_branch,
        keep_worktree=opts.keep_worktree, log=log, cmake_args=opts.cmake_args, test_timeout=opts.test_timeout,
    )
    notes: list[str] = []
    stopped = ""
    with session:
        on_start(session)
        baseline = session.verifier.baseline
        warnings_before = sum(baseline.warnings.values())
        score_before = scoremod.score(session.workspace.repo, opts.manifest) if opts.manifest else None
        tidy_before = _tidy_counts(session) if opts.measure_tidy else {}
        for recipe in opts.recipes:
            log(f"-- recipe {recipe}: {RECIPES[recipe]}")
            try:
                if recipe == "std":
                    std_raise.run(session, opts.std, log=log)
                elif recipe == "r1":
                    r1_mechanical.run(session, opts.checks)
                elif recipe == "r6":
                    dead = r6_dead_code.run(session, opts.flags, allow_api_changes=opts.allow_api_changes, log=log)
                    for name in dead.flag_candidates:
                        notes.append(f"flag never defined in this build: {name}  (to remove its branches: --flag {name}=off)")
                elif stopped:
                    log(f"skipped: the model is unavailable ({stopped})")
                else:
                    stopped = llm_engine.run(
                        session, client, allow_api_changes=opts.allow_api_changes, max_attempts=opts.max_attempts,
                        kinds=llm_engine.RECIPE_KINDS[recipe], log=log,
                    ).stopped
            except Exception as exc:  # one recipe failing must not cost the commits already made
                session.workspace.rollback()
                notes.append(f"recipe {recipe} stopped with an error: {type(exc).__name__}: {exc}")
                log(f"recipe {recipe} stopped with an error: {exc}")
        session.checkpoint()  # settle everything before measuring
        result = PipelineResult(
            report=None, baseline=baseline, worktree=session.workspace.worktree,
            warnings_before=warnings_before, warnings_after=session.verifier.warning_count(),
            tidy_before=tidy_before, tidy_after=_tidy_counts(session) if opts.measure_tidy else {},
            score_before=score_before,
            score_after=scoremod.score(session.workspace.repo, opts.manifest) if opts.manifest else None,
            diffstat=session.workspace.diffstat(), notes=notes, stopped=stopped,
            requests=client.budget.requests if client else 0, tokens=client.budget.tokens if client else 0,
        )
    result.report = session.report
    if opts.discard_branch and session.workspace is not None:
        session.workspace.remove(delete_branch=True)
    return result


def _tidy_counts(session: Session) -> dict[str, int]:
    return diagnostics.count_by_flag(buildmod.run_tidy(session.cfg, session.build_dir))
