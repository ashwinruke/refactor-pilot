"""Command line entry point.

Phase 0 commands:
  baseline  build + test the repo as it stands and save a snapshot
  check     rebuild + retest and fail if anything regressed against a snapshot
  score     report how many known legacy constructs from a manifest are gone
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, baseline
from .build import BuildConfig, BuildFailed
from .evalkit import score as scoremod
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
    except ToolMissing as exc:
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
