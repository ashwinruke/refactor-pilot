"""Benchmark: run a recipe pipeline over a list of repositories and report the results.

A target is either a path inside this repository or a git URL pinned to an exact
commit. Pinning matters: a benchmark that follows a moving branch measures the
upstream project's churn, not the tool.

Targets carry a status. "approved" targets run by default; "proposed" ones run
only when asked for, so a candidate can be tried without becoming part of the
published numbers.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from . import __version__
from .build import BuildFailed
from .checkpoint import CheckpointError
from .llm import Client
from .pipeline import LLM_RECIPES, PipelineOptions, PipelineResult, parse_recipes, run_pipeline
from .toolchain import require, run


class BenchError(RuntimeError):
    pass


@dataclass
class Target:
    name: str
    recipes: str = "r1,r6"
    path: str | None = None  # a directory, relative to the config file
    git: str | None = None  # or: a clone URL ...
    ref: str | None = None  # ... the tag or branch to fetch ...
    commit: str | None = None  # ... and the exact commit it must be
    subdir: str = "."
    manifest: str | None = None
    cmake_args: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    allow_api_changes: bool = False
    sanitizers: bool = True
    test_timeout: int = 300
    full_every: int = 5
    status: str = "approved"  # or "proposed"
    note: str = ""
    # Minimal build-system fixes needed before an old project configures at all,
    # as [{"file": ..., "find": ..., "replace": ...}]. Applied to the clone as one
    # setup commit; the run starts from there, so they are not counted as results.
    prepare: list[dict] = field(default_factory=list)


@dataclass
class TargetResult:
    name: str
    status: str  # ok | failed
    error: str = ""
    recipes: str = ""
    ref: str = ""
    branch: str = ""
    seconds: float = 0.0
    tests: int = 0
    tests_passing: int = 0
    sanitizer_tests_passing: int = 0
    warnings_before: int = 0
    warnings_after: int = 0
    findings_before: int = 0
    findings_after: int = 0
    committed: int = 0
    rolled_back: int = 0
    reverted: int = 0
    skipped: int = 0
    unchanged: int = 0
    by_recipe: dict = field(default_factory=dict)
    files_changed: int = 0
    lines_added: int = 0
    lines_removed: int = 0
    score_before: str = ""
    score_after: str = ""
    requests: int = 0
    tokens: int = 0
    notes: list[str] = field(default_factory=list)
    rejections: list[dict] = field(default_factory=list)  # what was rolled back or reverted, and why


def load_config(path: Path) -> tuple[dict, list[Target]]:
    data = json.loads(Path(path).read_text())
    targets = []
    for raw in data.get("targets", []):
        unknown = set(raw) - set(Target.__dataclass_fields__)
        if unknown:
            raise BenchError(f"target '{raw.get('name', '?')}': unknown field(s) {sorted(unknown)}")
        target = Target(**raw)
        if bool(target.path) == bool(target.git):
            raise BenchError(f"target '{target.name}': give exactly one of 'path' or 'git'")
        if target.git and not (target.ref and target.commit):
            raise BenchError(f"target '{target.name}': a git target needs both 'ref' and 'commit' (pin it)")
        parse_recipes(target.recipes)
        targets.append(target)
    names = [t.name for t in targets]
    if len(set(names)) != len(names):
        raise BenchError("target names must be unique")
    for target in targets:
        if target.prepare and not target.git:
            raise BenchError(f"target '{target.name}': 'prepare' is only for git targets (it edits the clone)")
        for step in target.prepare:
            if set(step) != {"file", "find", "replace"}:
                raise BenchError(f"target '{target.name}': each prepare step needs exactly file, find, replace")
    return data, targets


def checkout(target: Target, config_dir: Path, repos_dir: Path) -> Path:
    """Return the directory to modernize, cloning and pinning a git target if needed."""
    if target.path:
        repo = (config_dir / target.path).resolve()
        if not repo.is_dir():
            raise BenchError(f"target '{target.name}': {repo} does not exist")
        return repo
    require("git")
    clone = repos_dir / target.name
    if not (clone / ".git").exists():
        clone.parent.mkdir(parents=True, exist_ok=True)
        result = run(["git", "clone", "--quiet", "--depth", "1", "--branch", target.ref, target.git, clone])
        if not result.ok:
            raise BenchError(f"target '{target.name}': clone failed: {result.output.strip()[-300:]}")
    head = run(["git", "-C", clone, "rev-parse", "HEAD"]).output.strip()
    if head != target.commit:
        # Leave earlier bench branches alone; just make sure we start from the pinned commit.
        back = run(["git", "-C", clone, "checkout", "--quiet", "--detach", target.commit])
        head = run(["git", "-C", clone, "rev-parse", "HEAD"]).output.strip()
        if not back.ok or head != target.commit:
            raise BenchError(
                f"target '{target.name}': {target.ref} is at {head[:12]}, but the config pins {target.commit[:12]}. "
                "The upstream ref moved, or the pin is wrong."
            )
    run(["git", "-C", clone, "worktree", "prune"])
    if target.prepare:
        _prepare(target, clone)
    return (clone / target.subdir).resolve()


def _prepare(target: Target, clone: Path) -> None:
    """Apply the target's setup edits on a local branch that starts at the pinned commit."""
    from . import textio
    from .checkpoint import IDENTITY

    def git(*args: str) -> None:
        result = run(["git", "-C", clone, *args])
        if not result.ok:
            raise BenchError(f"target '{target.name}': git {' '.join(args[:2])} failed: {result.output.strip()[-200:]}")

    git("checkout", "--quiet", "-B", "refactor-pilot-bench-base", target.commit)
    for step in target.prepare:
        path = clone / step["file"]
        text, style = textio.load(path)
        if step["find"] not in text:
            raise BenchError(f"target '{target.name}': prepare step did not match in {step['file']}: {step['find']!r}")
        textio.save(path, text.replace(step["find"], step["replace"]), style)
    git("add", "-A")
    git(*IDENTITY, "commit", "--quiet", "-m", "refactor-pilot bench: build-system setup (not part of the results)")


def run_target(
    target: Target, config_dir: Path, work_dir: Path, compiler: str, client: Client | None,
    log: Callable[[str], None], stamp: str, keep_branches: bool = False,
) -> TargetResult:
    result = TargetResult(target.name, "ok", recipes=target.recipes, ref=target.ref or "local")
    started = time.monotonic()
    try:
        repo = checkout(target, config_dir, work_dir / "repos")
        flags = {}
        for item in target.flags:
            name, _, state = item.partition("=")
            flags[name] = state.lower() == "on"
        opts = PipelineOptions(
            repo=repo, recipes=parse_recipes(target.recipes), branch=f"refactor-pilot/bench-{stamp}",
            work_dir=work_dir / "work" / target.name, compiler=compiler, sanitizers=target.sanitizers,
            full_every=target.full_every, flags=flags, allow_api_changes=target.allow_api_changes,
            manifest=(config_dir / target.manifest).resolve() if target.manifest else None,
            cmake_args=list(target.cmake_args), test_timeout=target.test_timeout,
            discard_branch=not keep_branches,
        )
        _fill(result, run_pipeline(opts, client, log=log))
    except (BenchError, BuildFailed, CheckpointError, ValueError) as exc:
        result.status, result.error = "failed", str(exc).splitlines()[0][:300]
    result.seconds = round(time.monotonic() - started, 1)
    return result


def _fill(result: TargetResult, run_result: PipelineResult) -> None:
    report, base = run_result.report, run_result.baseline
    result.branch = report.branch
    result.tests = len(base.tests)
    result.tests_passing = sum(v == "pass" for v in base.tests.values())
    result.sanitizer_tests_passing = sum(v == "pass" for v in (base.sanitizer_tests or {}).values())
    result.warnings_before, result.warnings_after = run_result.warnings_before, run_result.warnings_after
    result.findings_before = sum(run_result.tidy_before.values())
    result.findings_after = sum(run_result.tidy_after.values())
    for status in ("committed", "rolled_back", "reverted", "skipped"):
        setattr(result, status, report.count(status))
    result.unchanged = report.count("no_change")
    result.by_recipe = run_result.by_recipe()
    result.files_changed, result.lines_added, result.lines_removed = run_result.diffstat
    if run_result.score_after is not None:
        result.score_before = f"{len(run_result.score_before.resolved)}/{run_result.score_before.total}"
        result.score_after = f"{len(run_result.score_after.resolved)}/{run_result.score_after.total}"
    result.requests, result.tokens = run_result.requests, run_result.tokens
    result.notes = list(run_result.notes) + ([f"stopped early: {run_result.stopped}"] if run_result.stopped else [])
    result.rejections = [{"id": o.id, "status": o.status, "reason": o.reason}
                         for o in report.outcomes if o.status in ("rolled_back", "reverted")]


def render_markdown(results: list[TargetResult], meta: dict) -> str:
    lines = [
        "# refactor-pilot benchmark",
        "",
        f"refactor-pilot {meta['version']} · compiler `{meta['compiler']}` · model `{meta['model']}` · {meta['date']}",
        "",
        "Every commit counted below passed the gates: it builds, adds no compiler warning, and keeps every "
        "test passing, including under ASan + UBSan where the target enables them.",
        "",
        "| Target | Ref | Recipes | Tests | Commits | Rejected | clang-tidy findings | Warnings | Diff | Score | Time |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        if r.status != "ok":
            lines.append(f"| {r.name} | {r.ref} | {r.recipes} | — | — | — | — | — | — | — | failed: {r.error} |")
            continue
        score = f"{r.score_before} → {r.score_after}" if r.score_after else "—"
        lines.append(
            f"| {r.name} | {r.ref} | {r.recipes} | {r.tests_passing}/{r.tests} | {r.committed} | "
            f"{r.rolled_back + r.reverted} | {r.findings_before} → {r.findings_after} | "
            f"{r.warnings_before} → {r.warnings_after} | {r.files_changed} files, +{r.lines_added} −{r.lines_removed} | "
            f"{score} | {r.seconds:.0f}s |"
        )
    lines += ["", "## Per recipe", "", "| Target | Recipe | Committed | Rolled back | Reverted | Skipped | No fix |", "|---|---|---|---|---|---|---|"]
    for r in results:
        for recipe, row in r.by_recipe.items():
            lines.append(f"| {r.name} | {recipe} | {row.get('committed', 0)} | {row.get('rolled_back', 0)} | "
                         f"{row.get('reverted', 0)} | {row.get('skipped', 0)} | {row.get('no_change', 0)} |")
    rejected = [(r.name, x) for r in results for x in r.rejections]
    if rejected:
        lines += ["", "## Rejected change-sets", "", "These were proposed, failed a gate, and were not kept.", "",
                  "| Target | Change-set | Outcome | Why |", "|---|---|---|---|"]
        for name, x in rejected:
            lines.append(f"| {name} | `{x['id']}` | {x['status']} | {x['reason'].replace('|', '/')[:160]} |")
    used = [r for r in results if r.requests]
    if used:
        lines += ["", "## Model usage", "", "| Target | Requests | Tokens |", "|---|---|---|"]
        lines += [f"| {r.name} | {r.requests} | {r.tokens} |" for r in used]
    return "\n".join(lines) + "\n"


def run_bench(
    config: Path, out_dir: Path, compiler: str = "clang++", only: list[str] | None = None,
    include_proposed: bool = False, client: Client | None = None, model_name: str = "none",
    log: Callable[[str], None] = print, keep_branches: bool = False,
) -> list[TargetResult]:
    config = Path(config).resolve()
    data, targets = load_config(config)
    if only:
        missing = set(only) - {t.name for t in targets}
        if missing:
            raise BenchError(f"no such target(s): {', '.join(sorted(missing))}")
        targets = [t for t in targets if t.name in only]
    elif not include_proposed:
        targets = [t for t in targets if t.status == "approved"]
    needs_model = [t.name for t in targets if any(r in LLM_RECIPES for r in parse_recipes(t.recipes))]
    if needs_model and client is None:
        raise BenchError(f"target(s) {', '.join(needs_model)} use recipes r2-r5 and need a model (an API key or --replay)")
    work_dir = (config.parent / data.get("work_dir", ".refactor-pilot/bench")).resolve()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    results = []
    for target in targets:
        log(f"== {target.name} ({target.ref or target.path}): {target.recipes}")
        result = run_target(target, config.parent, work_dir, compiler, client, lambda m: log(f"   {m}"), stamp,
                            keep_branches)
        log(f"   -> {result.status}: {result.committed} committed, {result.rolled_back + result.reverted} rejected, {result.seconds:.0f}s"
            + (f"  [{result.error}]" if result.error else ""))
        results.append(result)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {"version": __version__, "compiler": compiler, "model": model_name, "date": time.strftime("%Y-%m-%d")}
    (out_dir / "results.json").write_text(json.dumps({"meta": meta, "results": [asdict(r) for r in results]}, indent=2) + "\n")
    (out_dir / "REPORT.md").write_text(render_markdown(results, meta))
    return results
