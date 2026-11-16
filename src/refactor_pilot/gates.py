"""build_and_verify: the two gates every change-set must pass.

fast gate  - incremental build, no new warnings, no test regressions.
             Runs after every change-set.
full gate  - the fast gate plus the same tests under ASan + UBSan.
             Runs every few change-sets and at the end, because sanitizer
             builds are slow and most edits do not need them immediately.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import build as buildmod
from . import diagnostics
from .baseline import Regressions, Snapshot, compare
from .build import BuildConfig, IncrementalBuilder
from .diagnostics import Diagnostic


@dataclass
class GateResult:
    gate: str  # "fast" or "full"
    ok: bool
    stage: str = ""  # where it failed: compile | warnings | tests | sanitizer-compile | sanitizers
    errors: list[Diagnostic] = field(default_factory=list)
    regressions: Regressions | None = None
    test_output: dict[str, str] = field(default_factory=dict)
    seconds: float = 0.0
    rebuilt: int = 0

    def summary(self) -> str:
        if self.ok:
            return f"{self.gate} gate passed"
        if self.errors:
            first = self.errors[0]
            more = f" (+{len(self.errors) - 1} more)" if len(self.errors) > 1 else ""
            return f"{self.stage}: {first.file}:{first.line}: {first.message}{more}"
        if self.regressions is not None:
            lines = self.regressions.describe()
            return f"{self.stage}: " + "; ".join(lines[:3]) + (" ..." if len(lines) > 3 else "")
        return f"{self.stage} failed"

    def feedback(self, max_chars: int = 4000) -> str:
        """Everything a person or a model needs to fix the change, as text."""
        if self.ok:
            return ""
        parts = [f"The {self.gate} gate failed at stage '{self.stage}'."]
        for err in self.errors[:20]:
            parts.append(f"{err.file}:{err.line}:{err.col}: error: {err.message}")
        if self.regressions is not None:
            parts += self.regressions.describe()
        for name, output in self.test_output.items():
            parts.append(f"--- output of failing test '{name}' ---\n{output[-1500:]}")
        return "\n".join(parts)[:max_chars]


class Verifier:
    def __init__(self, cfg: BuildConfig, sanitizers: bool = True):
        self.cfg = cfg
        self.sanitizers = sanitizers
        self.baseline: Snapshot | None = None
        self._plain = IncrementalBuilder(cfg, "plain")
        self._san = IncrementalBuilder(cfg, "sanitize", sanitize=True)

    def snapshot(self) -> Snapshot:
        """Record the tree as it stands. Raises BuildFailed if it does not compile."""
        plain = self._plain.build()
        if not plain.ok:
            raise buildmod.BuildFailed("build", _as_cmd(plain), plain.errors)
        snap = Snapshot(
            warnings=diagnostics.count_by_key(plain.warnings),
            tests=buildmod.run_tests(self.cfg, self._plain.build_dir),
            meta={"compiler": self.cfg.compiler},
        )
        if self.sanitizers:
            san = self._san.build()
            if not san.ok:
                raise buildmod.BuildFailed("sanitizer build", _as_cmd(san), san.errors)
            snap.sanitizer_tests = buildmod.run_tests(self.cfg, self._san.build_dir, sanitize=True)
        return snap

    def set_baseline(self, snap: Snapshot | None = None) -> Snapshot:
        self.baseline = snap or self.snapshot()
        return self.baseline

    def _require_baseline(self) -> Snapshot:
        if self.baseline is None:
            raise RuntimeError("call set_baseline() before running a gate")
        return self.baseline

    def fast(self, gate: str = "fast") -> GateResult:
        base, start = self._require_baseline(), time.monotonic()
        built = self._plain.build()
        result = GateResult(gate, ok=False, rebuilt=built.rebuilt)
        if not built.ok:
            result.stage, result.errors = "compile", built.errors
        else:
            output: dict[str, str] = {}
            now = Snapshot(
                warnings=diagnostics.count_by_key(built.warnings),
                tests=buildmod.run_tests(self.cfg, self._plain.build_dir, failure_output=output),
            )
            reg = compare(base, now)
            if reg.new_warnings:
                result.stage, result.regressions = "warnings", reg
            elif not reg.ok:
                result.stage, result.regressions = "tests", reg
                result.test_output = {t: output.get(t, "") for t in reg.broken_tests}
            else:
                result.ok = True
        result.seconds = time.monotonic() - start
        return result

    def full(self) -> GateResult:
        base, start = self._require_baseline(), time.monotonic()
        result = self.fast(gate="full")
        if result.ok and self.sanitizers and base.sanitizer_tests is not None:
            built = self._san.build()
            if not built.ok:
                result.ok, result.stage, result.errors = False, "sanitizer-compile", built.errors
            else:
                output: dict[str, str] = {}
                after = buildmod.run_tests(self.cfg, self._san.build_dir, sanitize=True, failure_output=output)
                now = Snapshot(warnings=dict(base.warnings), tests=dict(base.tests), sanitizer_tests=after)
                reg = compare(base, now)
                if not reg.ok:
                    result.ok, result.stage, result.regressions = False, "sanitizers", reg
                    result.test_output = {t: output.get(t, "") for t in reg.broken_sanitizer_tests}
        result.seconds = time.monotonic() - start
        return result


def _as_cmd(built) -> "buildmod.CmdResult":
    from .toolchain import CmdResult

    return CmdResult(("cmake", "--build"), 1, built.log, built.seconds)
