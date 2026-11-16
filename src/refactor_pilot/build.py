"""Configure, build, test and lint a CMake project out-of-tree."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import diagnostics
from .diagnostics import Diagnostic
from .toolchain import CmdResult, require, run

SANITIZER_FLAGS = "-fsanitize=address,undefined -fno-omit-frame-pointer -fno-sanitize-recover=all"
DEFAULT_TIDY_CHECKS = "-*,modernize-*,-modernize-use-trailing-return-type"


class BuildFailed(RuntimeError):
    def __init__(self, stage: str, result: CmdResult, errors: list[Diagnostic]):
        self.stage, self.result, self.errors = stage, result, errors
        first = errors[0] if errors else None
        hint = f" First error: {first.file}:{first.line}: {first.message}" if first else ""
        super().__init__(f"{stage} failed with exit code {result.returncode}.{hint}")


@dataclass
class BuildConfig:
    repo: Path
    work_dir: Path | None = None  # build trees live here, never inside the repo
    compiler: str = "clang++"
    warning_flags: str = "-Wall -Wextra"
    cmake_args: list[str] = field(default_factory=list)
    jobs: int = field(default_factory=lambda: os.cpu_count() or 2)
    test_timeout: int = 120

    def __post_init__(self) -> None:
        self.repo = Path(self.repo).resolve()
        if self.work_dir is None:
            digest = hashlib.sha1(str(self.repo).encode()).hexdigest()[:12]
            self.work_dir = Path(tempfile.gettempdir()) / "refactor-pilot" / digest
        self.work_dir = Path(self.work_dir).resolve()

    def build_dir(self, kind: str) -> Path:
        return self.work_dir / f"build-{kind}"


@dataclass
class BuildResult:
    build_dir: Path
    warnings: list[Diagnostic]
    seconds: float


def configure(cfg: BuildConfig, kind: str = "plain", sanitize: bool = False) -> Path:
    """Run cmake configure and return the build directory.

    This alone produces compile_commands.json, which is all clangd needs.
    """
    for tool in ("cmake", "ninja", cfg.compiler):
        require(tool)
    bdir = cfg.build_dir(kind)
    bdir.mkdir(parents=True, exist_ok=True)
    flags = cfg.warning_flags + (" " + SANITIZER_FLAGS if sanitize else "")
    result = run(
        [
            "cmake", "-S", cfg.repo, "-B", bdir, "-G", "Ninja",
            f"-DCMAKE_CXX_COMPILER={cfg.compiler}",
            f"-DCMAKE_CXX_FLAGS={flags}",
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
            "-DCMAKE_BUILD_TYPE=Debug",
            *cfg.cmake_args,
        ]
    )
    if not result.ok:
        raise BuildFailed("cmake configure", result, [])
    return bdir


def build(cfg: BuildConfig, kind: str = "plain", sanitize: bool = False) -> BuildResult:
    """Configure and build. Raises BuildFailed if the tree does not compile."""
    start = time.monotonic()
    bdir = configure(cfg, kind, sanitize)
    # Always a clean build: an incremental build skips up-to-date files and so
    # prints none of their warnings, which would make a snapshot look cleaner
    # than the code is. (Phase 2 replaces this with a per-file warning cache.)
    compiled = run(["cmake", "--build", bdir, "--clean-first", "--parallel", str(cfg.jobs), "--", "-k", "0"])
    diags = diagnostics.parse(compiled.output, cfg.repo)
    if not compiled.ok:
        raise BuildFailed("build", compiled, [d for d in diags if d.severity == "error"])
    return BuildResult(bdir, [d for d in diags if d.severity == "warning"], time.monotonic() - start)


def run_tests(cfg: BuildConfig, build_dir: Path, sanitize: bool = False) -> dict[str, str]:
    """Run ctest and return {test name: "pass" | "fail"}."""
    require("ctest")
    junit = build_dir / "refactor-pilot-junit.xml"
    junit.unlink(missing_ok=True)
    env = dict(os.environ)
    if sanitize:
        env.setdefault("ASAN_OPTIONS", "detect_leaks=1:abort_on_error=0")
        env.setdefault("UBSAN_OPTIONS", "print_stacktrace=1")
    run(
        ["ctest", "--test-dir", build_dir, "--output-junit", junit,
         "--timeout", str(cfg.test_timeout), "--parallel", str(cfg.jobs)],
        env=env,
    )
    if not junit.exists():
        return {}
    results: dict[str, str] = {}
    for case in ET.parse(junit).getroot().iter("testcase"):
        passed = case.get("status") == "run" and case.find("failure") is None
        results[case.get("name", "?")] = "pass" if passed else "fail"
    return dict(sorted(results.items()))


def source_files(cfg: BuildConfig, build_dir: Path) -> list[Path]:
    """Translation units of the project itself, from compile_commands.json."""
    db = json.loads((build_dir / "compile_commands.json").read_text())
    files = []
    for entry in db:
        path = Path(entry["file"])
        if not path.is_absolute():
            path = Path(entry["directory"]) / path
        path = path.resolve()
        if cfg.repo in path.parents and build_dir not in path.parents:
            files.append(path)
    return sorted(set(files))


def run_tidy(cfg: BuildConfig, build_dir: Path, checks: str = DEFAULT_TIDY_CHECKS) -> list[Diagnostic]:
    """Run clang-tidy over every project translation unit (and its headers)."""
    require("clang-tidy")
    header_filter = "^" + str(cfg.repo).replace("\\", "/") + "/.*"

    def one(path: Path) -> str:
        return run(
            ["clang-tidy", "-p", build_dir, f"--checks={checks}",
             f"--header-filter={header_filter}", "--quiet", path]
        ).output

    with ThreadPoolExecutor(max_workers=cfg.jobs) as pool:
        output = "\n".join(pool.map(one, source_files(cfg, build_dir)))
    diags = diagnostics.parse(output, cfg.repo)
    # Compiler warnings also show up in clang-tidy output; keep tidy checks only.
    return [d for d in diags if d.flag and not d.flag.startswith("-W") and not d.flag.startswith("clang-diagnostic")]
