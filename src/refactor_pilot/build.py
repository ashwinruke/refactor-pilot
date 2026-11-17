"""Configure, build, test and lint a CMake project out-of-tree."""

from __future__ import annotations

import hashlib
import json
import os
import re
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


def run_tests(
    cfg: BuildConfig, build_dir: Path, sanitize: bool = False, failure_output: dict[str, str] | None = None
) -> dict[str, str]:
    """Run ctest and return {test name: "pass" | "fail"}.

    If `failure_output` is given it is filled with {test name: captured output}
    for failing tests, so a gate can explain what went wrong.
    """
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
        name = case.get("name", "?")
        passed = case.get("status") == "run" and case.find("failure") is None
        results[name] = "pass" if passed else "fail"
        if not passed and failure_output is not None:
            out = case.find("system-out")
            failure_output[name] = (out.text or "").strip() if out is not None else ""
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


def repo_header_filter(cfg: BuildConfig) -> str:
    """clang-tidy --header-filter regex matching every header inside the repo."""
    return "^" + re.escape(str(cfg.repo).replace("\\", "/")) + "/.*"


NO_HEADERS = "^$"  # a --header-filter that matches no header


def tidy_file(
    cfg: BuildConfig, build_dir: Path, path: Path, checks: str, header_filter: str | None = None, fix: bool = False
) -> list[Diagnostic]:
    """Run clang-tidy on one translation unit. With fix=True, apply its fix-its in place.

    Returns the clang-tidy findings (compiler warnings are filtered out).
    """
    require("clang-tidy")
    command = [
        "clang-tidy", "-p", build_dir, f"--checks={checks}",
        f"--header-filter={header_filter or repo_header_filter(cfg)}", "--quiet",
    ]
    if fix:
        command.append("--fix")
        if (cfg.repo / ".clang-format").exists():
            # Re-format only the lines a fix touched, in the project's own style.
            command.append("--format-style=file")
    output = run([*command, path]).output
    return [d for d in diagnostics.parse(output, cfg.repo) if _is_tidy_check(d)]


def _is_tidy_check(d: Diagnostic) -> bool:
    return bool(d.flag) and not d.flag.startswith("-W") and not d.flag.startswith("clang-diagnostic")


def run_tidy(cfg: BuildConfig, build_dir: Path, checks: str = DEFAULT_TIDY_CHECKS) -> list[Diagnostic]:
    """Run clang-tidy over every project translation unit (and its headers)."""
    require("clang-tidy")
    with ThreadPoolExecutor(max_workers=cfg.jobs) as pool:
        per_file = pool.map(lambda path: tidy_file(cfg, build_dir, path, checks), source_files(cfg, build_dir))
    merged: dict[tuple, Diagnostic] = {}
    for diags in per_file:  # a header finding is reported by every file that includes it
        for d in diags:
            merged.setdefault((d.file, d.line, d.col, d.flag, d.message), d)
    return list(merged.values())


_EDGE = re.compile(r"^\[\d+/\d+\] Building C(?:XX)? object (\S+)")
_FAILED = re.compile(r"^FAILED: (\S+)")
_OTHER_EDGE = re.compile(r"^\[\d+/\d+\] ")


def split_by_object(output: str) -> tuple[dict[str, str], str]:
    """Split ninja output into {object file: its compiler output} plus the rest.

    Ninja prints each finished compile as a status line followed by everything
    that compile wrote, so the text between two status lines belongs to one object.
    """
    per_object: dict[str, list[str]] = {}
    rest: list[str] = []
    current: str | None = None
    for line in output.splitlines():
        edge, failed = _EDGE.match(line), _FAILED.match(line)
        if edge:
            current = edge.group(1)
            per_object.setdefault(current, [])
        elif failed:
            current = failed.group(1)
            per_object.setdefault(current, [])
        elif _OTHER_EDGE.match(line):
            current = None
        elif current is not None:
            per_object[current].append(line)
        else:
            rest.append(line)
    return {obj: "\n".join(lines) for obj, lines in per_object.items()}, "\n".join(rest)


def known_objects(compile_db: list[dict], build_dir: Path) -> set[str]:
    """Object files the build produces, as ninja names them (build-dir relative).

    CMake 3.x writes the "output" of each compile_commands.json entry relative
    to the build directory; CMake 4.x writes it as an absolute path. Ninja's
    status lines are always relative, so both forms are normalized to that.
    """
    build_dir = Path(build_dir).resolve()
    known: set[str] = set()
    for entry in compile_db:
        output = entry.get("output")
        if not output:
            continue
        path = Path(output)
        if not path.is_absolute():
            path = Path(entry.get("directory", build_dir)) / path
        try:
            known.add(Path(os.path.normpath(path)).relative_to(build_dir).as_posix())
        except ValueError:
            known.add(Path(output).as_posix())
    return known


@dataclass
class IncrementalResult:
    ok: bool
    warnings: list[Diagnostic]  # for the whole tree, including files not rebuilt
    errors: list[Diagnostic]
    rebuilt: int  # number of objects compiled this time
    seconds: float
    log: str


class IncrementalBuilder:
    """Builds only what changed, yet still reports every warning in the tree.

    An incremental build prints nothing for up-to-date files. To keep the
    warning count honest, the warnings of each object file are cached; a rebuilt
    object replaces its entry and an untouched object keeps the cached one.
    """

    def __init__(self, cfg: BuildConfig, kind: str = "plain", sanitize: bool = False):
        self.cfg, self.kind, self.sanitize = cfg, kind, sanitize
        self.build_dir = cfg.build_dir(kind)
        self._cache_path = self.build_dir / "refactor-pilot-warnings.json"

    def _load(self) -> dict[str, list[dict]] | None:
        try:
            return json.loads(self._cache_path.read_text())
        except (OSError, ValueError):
            return None

    def build(self) -> IncrementalResult:
        start = time.monotonic()
        configure(self.cfg, self.kind, self.sanitize)
        cache = self._load()
        command = ["cmake", "--build", self.build_dir, "--parallel", str(self.cfg.jobs)]
        if cache is None:  # no trustworthy cache yet: compile everything once
            cache = {}
            command.append("--clean-first")
        result = run([*command, "--", "-k", "0"])
        per_object, rest = split_by_object(result.output)
        errors: list[Diagnostic] = []
        for obj, text in per_object.items():
            diags = diagnostics.parse(text, self.cfg.repo)
            errors += [d for d in diags if d.severity == "error"]
            cache[obj] = [d.__dict__ for d in diags if d.severity == "warning"]
        errors += [d for d in diagnostics.parse(rest, self.cfg.repo) if d.severity == "error"]
        # Forget objects that no longer exist in the build (deleted source files).
        try:
            db = json.loads((self.build_dir / "compile_commands.json").read_text())
            known = known_objects(db, self.build_dir)
            if known:
                cache = {obj: w for obj, w in cache.items() if obj in known}
        except (OSError, ValueError):
            pass
        if result.ok:
            self._cache_path.write_text(json.dumps(cache))
        else:
            # A failed build leaves the cache unsure; drop the failed objects only.
            for obj, text in per_object.items():
                if any(d.severity == "error" for d in diagnostics.parse(text, self.cfg.repo)):
                    cache.pop(obj, None)
            self._cache_path.write_text(json.dumps(cache))
        merged: dict[tuple, Diagnostic] = {}
        for entries in cache.values():
            for raw in entries:
                d = Diagnostic(**raw)
                merged.setdefault((d.file, d.line, d.col, d.flag, d.message), d)
        return IncrementalResult(
            ok=result.ok,
            warnings=list(merged.values()),
            errors=errors,
            rebuilt=len(per_object),
            seconds=time.monotonic() - start,
            log=result.output,
        )
