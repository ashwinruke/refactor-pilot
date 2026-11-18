"""Command line entry point.

Commands:
  baseline  build + test the repo as it stands and save a snapshot
  check     rebuild + retest and fail if anything regressed against a snapshot
  score     report how many known legacy constructs from a manifest are gone
  symbols   list the symbols declared in one file (via clangd)
  refs      show a symbol's signature, callers and every reference (via clangd)
  apply     push patches through the verification gates onto a new branch
  run       run a modernization recipe (r1: mechanical via clang-tidy; r2: ownership via an LLM)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, baseline
from . import build as buildmod
from .build import BuildConfig, BuildFailed
from .checkpoint import CheckpointError
from .evalkit import score as scoremod
from .lsp import LspError
from .navigator import SymbolNavigator
from .llm import Budget, LlmError, RecordingClient, ReplayClient, client_from_env
from .recipes import r1_mechanical, r2_ownership
from .session import ChangeSet, Session
from .toolchain import ToolMissing


def _config(args: argparse.Namespace) -> BuildConfig:
    return BuildConfig(repo=args.repo, work_dir=args.work_dir, compiler=args.compiler)


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
    with SymbolNavigator(BuildConfig(repo=args.repo, work_dir=args.work_dir, compiler=args.compiler)) as nav:
        for sym in nav.outline(args.file):
            print(f"{sym.location.line:5d}  {sym.kind:<12} {sym.name}")
    return 0


def cmd_refs(args: argparse.Namespace) -> int:
    with SymbolNavigator(BuildConfig(repo=args.repo, work_dir=args.work_dir, compiler=args.compiler)) as nav:
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


def cmd_run(args: argparse.Namespace) -> int:
    checks = [c.strip() for c in args.checks.split(",") if c.strip()] if args.checks else None
    client = _llm_client(args) if args.recipe == "r2" and not args.plan else None
    session = Session(
        args.repo, work_dir=args.work_dir, compiler=args.compiler, branch=args.branch,
        sanitizers=not args.no_sanitizers, full_every=args.full_every,
        allow_dirty=args.allow_dirty, keep_worktree=args.keep_worktree,
        log=lambda message: print(f"  {message}", flush=True),
    )
    stopped = ""
    with session:
        print(f"branch {session.workspace.branch} (private worktree: {session.workspace.worktree})")
        score_before = scoremod.score(session.workspace.repo, args.manifest) if args.manifest else None
        if args.recipe == "r2" and args.plan:
            return _print_r2_plan(session, args.allow_api_changes)
        if args.recipe == "r1":
            findings_before = len(buildmod.run_tidy(session.cfg, session.build_dir))
            r1_mechanical.run(session, checks)
        else:
            if isinstance(client, ReplayClient):
                print(f"  model: replaying {args.replay} (no network, no key)")
            else:
                inner = client.inner if isinstance(client, RecordingClient) else client
                print(f"  model: {inner.model} via {inner.base_url}; budget {inner.budget.max_requests} requests")
            stopped = r2_ownership.run(
                session, client, allow_api_changes=args.allow_api_changes, max_attempts=args.max_attempts,
                log=lambda message: print(f"  {message}", flush=True),
            ).stopped
        session.checkpoint()  # settle everything before measuring
        if args.recipe == "r1":
            findings_after = len(buildmod.run_tidy(session.cfg, session.build_dir))
        score_after = scoremod.score(session.workspace.repo, args.manifest) if args.manifest else None
    _print_report(session.report, show_unchanged=args.verbose or args.recipe == "r2")
    print()
    if args.recipe == "r1":
        print(f"clang-tidy modernize findings: {findings_before} -> {findings_after}")
    else:
        print(f"model usage: {client.budget.requests} requests, {client.budget.tokens} tokens")
        if stopped:
            print(f"stopped early: {stopped}")
    if score_after is not None:
        print(f"manifest score: {len(score_before.resolved)}/{score_before.total} -> "
              f"{len(score_after.resolved)}/{score_after.total} legacy constructs resolved")
        for recipe, (done, total) in score_after.by_recipe.items():
            was = score_before.by_recipe[recipe][0]
            print(f"  {recipe}: {was}/{total} -> {done}/{total}")
    return 0


def _print_r2_plan(session: Session, allow_api_changes: bool) -> int:
    """List what R2 would work on, and how big each prompt is. Calls no model."""
    cfg = session.cfg
    with SymbolNavigator(cfg) as nav:
        targets = r2_ownership.find_targets(nav, cfg.repo, buildmod.source_files(cfg, session.build_dir), allow_api_changes)
    standard = r2_ownership.language_standard(session.build_dir)
    print(f"\n{len(targets)} target(s); standard: {standard or 'compiler default'}; "
          f"API changes {'allowed' if allow_api_changes else 'not allowed'}\n")
    for target in targets:
        prompt, editable = r2_ownership.build_prompt(target, cfg.repo, standard, allow_api_changes)
        note = "  [skipped without --allow-api-changes]" if target.needs_api_change and not allow_api_changes else ""
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
    p.add_argument("--recipe", choices=["r1", "r2"], default="r1",
                   help="r1: mechanical fixes via clang-tidy; r2: ownership and RAII via an LLM")
    p.add_argument("--checks", default=None, help="r1: comma-separated clang-tidy checks (default: the R1 list)")
    p.add_argument("--allow-api-changes", action="store_true",
                   help="r2: allow public signatures to change (callers are updated too)")
    p.add_argument("--plan", action="store_true", help="r2: list the targets and prompt sizes, call no model")
    p.add_argument("--replay", type=Path, default=None, help="r2: answer from a recording instead of a live model")
    p.add_argument("--record", type=Path, default=None, help="r2: append every model answer to this file")
    p.add_argument("--max-requests", type=int, default=40, help="r2: hard cap on model requests for this run")
    p.add_argument("--max-attempts", type=int, default=3, help="r2: attempts per target before giving up")
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
