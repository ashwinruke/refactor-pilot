"""Command line entry point.

Commands:
  baseline  build + test the repo as it stands and save a snapshot
  check     rebuild + retest and fail if anything regressed against a snapshot
  score     report how many known legacy constructs from a manifest are gone
  symbols   list the symbols declared in one file (via clangd)
  refs      show a symbol's signature, callers and every reference (via clangd)
  apply     push patches through the verification gates onto a new branch
  run       run recipes on a new branch: std, r1..r6, or all
  bench     run recipe pipelines over a list of repositories and write a report
  mcp       serve the navigation and verified-change tools to an MCP client over stdio
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, baseline, bench, mcp_server, pipeline
from . import build as buildmod
from .build import BuildConfig, BuildFailed
from .checkpoint import CheckpointError
from .evalkit import score as scoremod
from .lsp import LspError
from .navigator import SymbolNavigator
from .llm import Budget, LlmError, RecordingClient, ReplayClient, client_from_env
from .recipes import llm_engine
from .session import ChangeSet, Session
from .toolchain import ToolMissing


def _config(args: argparse.Namespace) -> BuildConfig:
    return BuildConfig(repo=args.repo, work_dir=args.work_dir, compiler=args.compiler,
                       cmake_args=list(getattr(args, "cmake_arg", []) or []))


def _capture(args: argparse.Namespace) -> baseline.Snapshot:
    return baseline.capture(_config(args), sanitizers=not args.no_sanitizers, tidy=not args.no_tidy)


def _summary(snap: baseline.Snapshot) -> str:
    passed = sum(r == "pass" for r in snap.tests.values())
    parts = [f"{sum(snap.warnings.values())} warnings", f"{passed}/{len(snap.tests)} tests passing"]
    if snap.sanitizer_tests is not None:
        ok = sum(r == "pass" for r in snap.sanitizer_tests.values())
        parts.append(f"{ok}/{len(snap.sanitizer_tests)} passing under ASan+UBSan")
    if snap.tidy is not None:
        parts.append(f"{sum(snap.tidy.values())} clang-tidy modernize findings")
    return ", ".join(parts)


def cmd_baseline(args: argparse.Namespace) -> int:
    snap = _capture(args)
    snap.save(args.output)
    print(f"baseline: {_summary(snap)}")
    if snap.tidy:
        for check, n in sorted(snap.tidy.items(), key=lambda kv: -kv[1]):
            print(f"  {n:4d}  {check}")
    print(f"saved to {args.output}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    before = baseline.Snapshot.load(args.baseline)
    after = _capture(args)
    print(f"before: {_summary(before)}")
    print(f"after:  {_summary(after)}")
    reg = baseline.compare(before, after)
    if reg.ok:
        print("OK: nothing regressed")
        return 0
    print("REGRESSIONS:")
    for line in reg.describe():
        print(f"  {line}")
    return 1


def cmd_score(args: argparse.Namespace) -> int:
    result = scoremod.score(args.repo, args.manifest)
    print(f"resolved {len(result.resolved)}/{result.total} legacy constructs ({result.fraction:.0%})")
    for recipe, (done, total) in result.by_recipe.items():
        print(f"  {recipe}: {done}/{total}")
    if args.verbose and result.remaining:
        print("remaining:")
        for item_id in result.remaining:
            print(f"  {item_id}")
    return 0


def cmd_symbols(args: argparse.Namespace) -> int:
    with SymbolNavigator(_config(args)) as nav:
        for sym in nav.outline(args.file):
            print(f"{sym.location.line:5d}  {sym.kind:<12} {sym.name}")
    return 0


def cmd_refs(args: argparse.Namespace) -> int:
    with SymbolNavigator(_config(args)) as nav:
        matches = nav.find(args.symbol)
        if not matches:
            print(f"no symbol named '{args.symbol}' in {nav.repo}", file=sys.stderr)
            return 1
        names = sorted({m.name for m in matches})
        if len(names) > 1:
            print(f"'{args.symbol}' is ambiguous; qualify it:", file=sys.stderr)
            for name in names:
                print(f"  {name}", file=sys.stderr)
            return 1
        symbol = matches[0]
        print(f"{symbol.name}  ({symbol.kind})")
        signature = nav.signature(symbol.location)
        if signature:
            print("  " + signature.replace("\n", "\n  "))
        refs = nav.references(symbol.location)
        files = sorted({r.file for r in refs})
        print(f"\n{len(refs)} references in {len(files)} files:")
        for ref in refs:
            print(f"  {str(ref):<40} {ref.text}")
        if symbol.kind in ("function", "method", "constructor"):
            callers = nav.callers(symbol.location)
            print(f"\n{len(callers)} callers:")
            for caller in callers:
                print(f"  {str(caller.location):<40} {caller.name}")
    return 0


def _patch_change_set(session: Session, patch: Path) -> ChangeSet:
    description = patch.stem
    for line in patch.read_text(errors="replace").splitlines()[:5]:
        if line.lower().startswith("subject:"):
            description = line.split(":", 1)[1].strip()
            break
    return ChangeSet(patch.stem, description, lambda repo, feedback: session.workspace.apply_patch(patch))


def cmd_apply(args: argparse.Namespace) -> int:
    missing = [p for p in args.patch if not p.is_file()]
    if missing:
        print(f"error: no such patch file: {missing[0]}", file=sys.stderr)
        return 2
    session = Session(
        args.repo, work_dir=args.work_dir, compiler=args.compiler, branch=args.branch,
        sanitizers=not args.no_sanitizers, full_every=args.full_every,
        allow_dirty=args.allow_dirty, keep_worktree=args.keep_worktree,
        log=lambda message: print(f"  {message}", flush=True),
    )
    with session:
        print(f"branch {session.workspace.branch} (private worktree: {session.workspace.worktree})")
        for patch in args.patch:
            session.run(_patch_change_set(session, patch))
    return 0 if _print_report(session.report) == len(session.report.outcomes) else 1


def _print_report(report, show_unchanged: bool = True) -> int:
    """Print one line per change-set and the summary. Returns the number committed."""
    print()
    for outcome in report.outcomes:
        if outcome.status == "no_change" and not show_unchanged:
            continue
        sha = outcome.sha[:8] if outcome.sha else "-" * 8
        print(f"{outcome.status:<11} {sha}  {outcome.id}: {outcome.description}")
        if outcome.reason:
            print(f"{'':<21} reason: {outcome.reason}")
    kept = report.count("committed")
    skipped = f", {report.count('skipped')} skipped" if report.count("skipped") else ""
    print(
        f"\n{kept} committed, {report.count('rolled_back')} rolled back, {report.count('reverted')} reverted, "
        f"{report.count('no_change')} unchanged{skipped}; {report.full_gates} full-gate runs; {report.seconds:.0f}s"
    )
    if kept:
        print(f"result is on branch '{report.branch}'. Your working tree was not touched.")
        print(f"  review:  git log --oneline {report.base_sha[:8]}..{report.branch}")
        print(f"  diff:    git diff {report.base_sha[:8]} {report.branch}")
        print(f"  accept:  git merge {report.branch}")
    else:
        print("nothing was committed; no branch was left behind.")
    return kept


def _llm_client(args: argparse.Namespace):
    if args.replay:
        return ReplayClient.from_file(args.replay)
    client = client_from_env(Budget(max_requests=args.max_requests))
    return RecordingClient(client, args.record) if args.record else client


def _parse_flags(items: list[str]) -> dict[str, bool]:
    flags = {}
    for item in items or []:
        name, _, state = item.partition("=")
        if not name or state.lower() not in ("on", "off"):
            raise SystemExit(f"error: --flag expects NAME=on or NAME=off, got '{item}'")
        flags[name] = state.lower() == "on"
    return flags


def _parse_recipes(text: str) -> list[str] | None:
    try:
        return pipeline.parse_recipes(text)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def cmd_run(args: argparse.Namespace) -> int:
    recipes = _parse_recipes(args.recipe)
    if recipes is None:
        return 2
    model_recipes = [r for r in recipes if r in pipeline.LLM_RECIPES]
    log = lambda message: print(f"  {message}", flush=True)  # noqa: E731
    if args.plan:
        session = Session(args.repo, work_dir=args.work_dir, compiler=args.compiler, branch=args.branch,
                          sanitizers=False, allow_dirty=args.allow_dirty, log=log, cmake_args=args.cmake_arg)
        with session:
            kinds = tuple(k for r in (model_recipes or pipeline.LLM_RECIPES) for k in llm_engine.RECIPE_KINDS[r])
            return _print_plan(session, args.allow_api_changes, kinds)
    client = _llm_client(args) if model_recipes else None
    opts = pipeline.PipelineOptions(
        repo=args.repo, recipes=recipes, branch=args.branch, work_dir=args.work_dir, compiler=args.compiler,
        sanitizers=not args.no_sanitizers, full_every=args.full_every, allow_dirty=args.allow_dirty,
        keep_worktree=args.keep_worktree,
        checks=[c.strip() for c in args.checks.split(",") if c.strip()] if args.checks else None,
        flags=_parse_flags(args.flag), allow_api_changes=args.allow_api_changes, std=args.std,
        max_attempts=args.max_attempts, manifest=args.manifest, cmake_args=args.cmake_arg,
        measure_tidy="r1" in recipes,
    )

    def on_start(session: Session) -> None:
        print(f"branch {session.workspace.branch} (private worktree: {session.workspace.worktree})")
        if isinstance(client, ReplayClient):
            print(f"  model: replaying {args.replay} (no network, no key)")
        elif client is not None:
            inner = client.inner if isinstance(client, RecordingClient) else client
            print(f"  model: {inner.model} via {inner.base_url}; budget {inner.budget.max_requests} requests")

    result = pipeline.run_pipeline(opts, client, log=log, on_start=on_start)
    _print_report(result.report, show_unchanged=args.verbose or recipes != ["r1"])
    print()
    if "r1" in recipes:
        print(f"clang-tidy modernize findings: {sum(result.tidy_before.values())} -> {sum(result.tidy_after.values())}")
    if client is not None:
        print(f"model usage: {result.requests} requests, {result.tokens} tokens")
    if result.stopped:
        print(f"stopped early: {result.stopped}")
    for note in result.notes:
        print(note)
    if result.score_after is not None:
        before, after = result.score_before, result.score_after
        print(f"manifest score: {len(before.resolved)}/{before.total} -> "
              f"{len(after.resolved)}/{after.total} legacy constructs resolved")
        for recipe, (done, total) in after.by_recipe.items():
            print(f"  {recipe}: {before.by_recipe[recipe][0]}/{total} -> {done}/{total}")
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    if not repo.is_dir():
        print(f"error: {repo} is not a directory", file=sys.stderr)
        return 2
    mcp_server.Server(repo, compiler=args.compiler, cmake_args=args.cmake_arg, work_dir=args.work_dir,
                      sanitizers=not args.no_sanitizers).serve()
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    only = [n.strip() for n in args.only.split(",") if n.strip()] if args.only else None
    client, model = None, "none"
    try:
        _, targets = bench.load_config(args.config)
        selected = [t for t in targets if (t.name in only if only else (args.include_proposed or t.status == "approved"))]
        if any(r in pipeline.LLM_RECIPES for t in selected for r in pipeline.parse_recipes(t.recipes)):
            client = _llm_client(args)
            inner = client.inner if isinstance(client, RecordingClient) else client
            model = f"replay of {args.replay.name}" if isinstance(client, ReplayClient) else inner.model
        results = bench.run_bench(args.config, args.out, compiler=args.compiler, only=only,
                                  include_proposed=args.include_proposed, client=client, model_name=model,
                                  keep_branches=args.keep_branches)
    except (bench.BenchError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print()
    print((args.out / "REPORT.md").read_text())
    print(f"written: {args.out / 'REPORT.md'} and {args.out / 'results.json'}")
    return 0 if all(r.status == "ok" for r in results) else 1


def _print_plan(session: Session, allow_api_changes: bool, kinds: tuple[str, ...]) -> int:
    """List what the model-written recipes would work on, and how big each prompt is. Calls no model."""
    cfg = session.cfg
    sources = buildmod.source_files(cfg, session.build_dir)
    with SymbolNavigator(cfg) as nav:
        targets = llm_engine.find_targets(nav, cfg.repo, sources, allow_api_changes, kinds)
    every = sorted({p.relative_to(cfg.repo).as_posix() for p in sources}
                   | {p.relative_to(cfg.repo).as_posix() for p in cfg.repo.rglob("*") if p.suffix in llm_engine.HEADER_SUFFIXES})
    context = llm_engine.repo_context(cfg.repo, every)
    standard = llm_engine.language_standard(session.build_dir)
    level = llm_engine.standard_number(standard)
    print(f"\n{len(targets)} target(s); standard: {standard or 'compiler default'}; "
          f"API changes {'allowed' if allow_api_changes else 'not allowed'}\n")
    for target in targets:
        kind = llm_engine.KINDS[target.kind]
        prompt, editable = llm_engine.build_prompt(target, cfg.repo, standard, allow_api_changes, context)
        note = ""
        if level < kind.min_standard:
            note = f"  [skipped: needs C++{kind.min_standard}; add the std recipe]"
        elif target.needs_api_change and not allow_api_changes:
            note = "  [skipped without --allow-api-changes]"
        print(f"{target.id}{note}")
        print(f"    may edit: {', '.join(sorted(editable))}")
        print(f"    prompt:   {len(prompt)} characters (about {len(prompt) // 4} tokens)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="refactor-pilot", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_build_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("repo", type=Path, help="path to a CMake project")
        p.add_argument("--work-dir", type=Path, default=None, help="where build trees go (default: system temp)")
        p.add_argument("--compiler", default="clang++")
        p.add_argument("--no-sanitizers", action="store_true", help="skip the ASan+UBSan build and test run")
        p.add_argument("--no-tidy", action="store_true", help="skip clang-tidy")
        p.add_argument("--cmake-arg", action="append", default=[], metavar="ARG",
                       help="extra argument for cmake configure, e.g. --cmake-arg=-DBUILD_TESTS=ON (repeatable)")

    p = sub.add_parser("baseline", help="record the repo's current warnings and test results")
    add_build_args(p)
    p.add_argument("-o", "--output", type=Path, default=Path("baseline.json"))
    p.set_defaults(func=cmd_baseline)

    p = sub.add_parser("check", help="fail if the repo regressed against a baseline")
    add_build_args(p)
    p.add_argument("--baseline", type=Path, required=True)
    p.set_defaults(func=cmd_check)

    def add_lsp_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("repo", type=Path, help="path to a CMake project")
        p.add_argument("--work-dir", type=Path, default=None)
        p.add_argument("--compiler", default="clang++")
        p.add_argument("--cmake-arg", action="append", default=[], metavar="ARG")

    p = sub.add_parser("symbols", help="list the symbols declared in a file")
    add_lsp_args(p)
    p.add_argument("file", help="file path, relative to the repo")
    p.set_defaults(func=cmd_symbols)

    p = sub.add_parser("refs", help="show every reference to a symbol")
    add_lsp_args(p)
    p.add_argument("symbol", help="name, optionally qualified: Warehouse::add_item")
    p.set_defaults(func=cmd_refs)

    p = sub.add_parser("apply", help="apply patches as verified change-sets on a new branch")
    p.add_argument("repo", type=Path, help="path to a CMake project inside a git repository")
    p.add_argument("--patch", type=Path, nargs="+", required=True, help="unified diffs, paths relative to the repo")
    p.add_argument("--branch", default=None, help="branch to create (default: refactor-pilot/<timestamp>)")
    p.add_argument("--full-every", type=int, default=5, help="run the full gate after this many commits")
    p.add_argument("--work-dir", type=Path, default=None)
    p.add_argument("--compiler", default="clang++")
    p.add_argument("--no-sanitizers", action="store_true")
    p.add_argument("--allow-dirty", action="store_true", help="proceed even if the target has uncommitted changes")
    p.add_argument("--keep-worktree", action="store_true", help="leave the private worktree on disk afterwards")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("run", help="run a modernization recipe on a new branch")
    p.add_argument("repo", type=Path, help="path to a CMake project inside a git repository")
    p.add_argument("--recipe", default="r1",
                   help="std, r1, r2, r3, r4, r5, r6 or all; comma-separated to run several in order on one "
                        "branch, e.g. std,r1,r6. 'all' is std,r1,r6,r2,r3,r4,r5")
    p.add_argument("--cmake-arg", action="append", default=[], metavar="ARG",
                   help="extra argument for cmake configure, e.g. --cmake-arg=-DBUILD_TESTS=ON (repeatable)")
    p.add_argument("--std", type=int, default=20, choices=[14, 17, 20, 23],
                   help="std: the C++ standard to raise the project to (default 20)")
    p.add_argument("--flag", action="append", default=[], metavar="NAME=on|off",
                   help="r6: declare a compile-time flag permanently on or off; its dead branches are removed")
    p.add_argument("--checks", default=None, help="r1: comma-separated clang-tidy checks (default: the R1 list)")
    p.add_argument("--allow-api-changes", action="store_true",
                   help="r2-r5: allow public interfaces to change (callers are updated too); "
                        "r6: also remove unreferenced functions from public headers")
    p.add_argument("--plan", action="store_true", help="r2-r5: list the targets and prompt sizes, call no model")
    p.add_argument("--replay", type=Path, default=None, help="r2-r5: answer from a recording instead of a live model")
    p.add_argument("--record", type=Path, default=None, help="r2-r5: append every model answer to this file")
    p.add_argument("--max-requests", type=int, default=40, help="r2-r5: hard cap on model requests for this run")
    p.add_argument("--max-attempts", type=int, default=3, help="r2-r5: attempts per target before giving up")
    p.add_argument("--manifest", type=Path, default=None, help="benchmark manifest; prints the score before and after")
    p.add_argument("--branch", default=None)
    p.add_argument("--full-every", type=int, default=5)
    p.add_argument("--work-dir", type=Path, default=None)
    p.add_argument("--compiler", default="clang++")
    p.add_argument("--no-sanitizers", action="store_true")
    p.add_argument("--allow-dirty", action="store_true")
    p.add_argument("--keep-worktree", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true", help="also list findings that have no automatic fix")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("mcp", help="run as an MCP server (stdio) for one repository")
    p.add_argument("repo", type=Path, help="path to a CMake project inside a git repository")
    p.add_argument("--work-dir", type=Path, default=None)
    p.add_argument("--compiler", default="clang++")
    p.add_argument("--no-sanitizers", action="store_true")
    p.add_argument("--cmake-arg", action="append", default=[], metavar="ARG")
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("bench", help="benchmark recipe pipelines over several repositories")
    p.add_argument("config", type=Path, help="JSON file listing the targets (see bench/targets.json)")
    p.add_argument("--out", type=Path, default=Path("bench-results"), help="where REPORT.md and results.json go")
    p.add_argument("--only", default=None, help="comma-separated target names to run, whatever their status")
    p.add_argument("--include-proposed", action="store_true", help="also run targets whose status is 'proposed'")
    p.add_argument("--keep-branches", action="store_true",
                   help="keep each target's result branch (refactor-pilot/bench-<time>) for inspection")
    p.add_argument("--compiler", default="clang++")
    p.add_argument("--replay", type=Path, default=None)
    p.add_argument("--record", type=Path, default=None)
    p.add_argument("--max-requests", type=int, default=200)
    p.set_defaults(func=cmd_bench)

    p = sub.add_parser("score", help="count resolved legacy constructs from a manifest")
    p.add_argument("repo", type=Path)
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_score)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ToolMissing, LspError, CheckpointError, LlmError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except BuildFailed as exc:
        print(f"error: {exc}", file=sys.stderr)
        for err in exc.errors[:20]:
            print(f"  {err.file}:{err.line}:{err.col}: {err.message}", file=sys.stderr)
        if "libclang_rt" in exc.result.output:
            print(
                "  The clang sanitizer runtime is not installed. Install it (Ubuntu: "
                "libclang-rt-dev), or pass --compiler g++, or pass --no-sanitizers.",
                file=sys.stderr,
            )
        elif not exc.errors:
            print(exc.result.output[-2000:], file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
