"""Snapshots of a repo's health, and the 'nothing new broke' comparison.

Legacy repos are rarely clean, so the gate is never "zero warnings". It is:
no new warnings, no test that used to pass now failing, and no test missing.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import build as buildmod
from . import diagnostics
from .build import BuildConfig


@dataclass
class Snapshot:
    warnings: dict[str, int]  # Diagnostic.key -> count
    tests: dict[str, str]  # name -> pass | fail
    sanitizer_tests: dict[str, str] | None = None  # same tests under ASan+UBSan
    tidy: dict[str, int] | None = None  # clang-tidy check -> findings (informational)
    meta: dict = field(default_factory=dict)

    def save(self, path: Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2, sort_keys=True) + "\n")

    @classmethod
    def load(cls, path: Path) -> "Snapshot":
        return cls(**json.loads(Path(path).read_text()))


@dataclass
class Regressions:
    new_warnings: dict[str, int] = field(default_factory=dict)  # key -> extra count
    broken_tests: list[str] = field(default_factory=list)
    missing_tests: list[str] = field(default_factory=list)
    broken_sanitizer_tests: list[str] = field(default_factory=list)
    missing_sanitizer_tests: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(asdict(self).values())

    def describe(self) -> list[str]:
        lines = []
        for key, extra in self.new_warnings.items():
            file, flag, msg = key.split("|", 2)
            lines.append(f"new warning x{extra}: {file}: {msg} [{flag}]")
        lines += [f"test now fails: {t}" for t in self.broken_tests]
        lines += [f"test disappeared: {t}" for t in self.missing_tests]
        lines += [f"test now fails under sanitizers: {t}" for t in self.broken_sanitizer_tests]
        lines += [f"sanitizer test disappeared: {t}" for t in self.missing_sanitizer_tests]
        return lines


def _test_regressions(before: dict[str, str], after: dict[str, str]) -> tuple[list[str], list[str]]:
    broken = [t for t, r in before.items() if r == "pass" and after.get(t) == "fail"]
    # Deleting a test must never count as fixing it.
    missing = [t for t in before if t not in after]
    return sorted(broken), sorted(missing)


def compare(before: Snapshot, after: Snapshot) -> Regressions:
    reg = Regressions()
    for key, count in after.warnings.items():
        extra = count - before.warnings.get(key, 0)
        if extra > 0:
            reg.new_warnings[key] = extra
    reg.broken_tests, reg.missing_tests = _test_regressions(before.tests, after.tests)
    if before.sanitizer_tests is not None and after.sanitizer_tests is not None:
        reg.broken_sanitizer_tests, reg.missing_sanitizer_tests = _test_regressions(
            before.sanitizer_tests, after.sanitizer_tests
        )
    return reg


def capture(cfg: BuildConfig, sanitizers: bool = True, tidy: bool = True) -> Snapshot:
    """Build and test the repo as it stands and record the result."""
    plain = buildmod.build(cfg, "plain")
    snap = Snapshot(
        warnings=diagnostics.count_by_key(plain.warnings),
        tests=buildmod.run_tests(cfg, plain.build_dir),
        meta={"compiler": cfg.compiler, "warning_flags": cfg.warning_flags, "build_seconds": round(plain.seconds, 2)},
    )
    if tidy:
        snap.tidy = diagnostics.count_by_flag(buildmod.run_tidy(cfg, plain.build_dir))
    if sanitizers:
        san = buildmod.build(cfg, "sanitize", sanitize=True)
        snap.sanitizer_tests = buildmod.run_tests(cfg, san.build_dir, sanitize=True)
    return snap
