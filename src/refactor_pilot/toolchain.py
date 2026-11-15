"""Thin wrappers for running external tools (cmake, ninja, ctest, clang-tidy)."""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


class ToolMissing(RuntimeError):
    """A required external tool is not on PATH."""


@dataclass(frozen=True)
class CmdResult:
    cmd: tuple[str, ...]
    returncode: int
    output: str  # stdout and stderr interleaved, as a terminal would show them
    seconds: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def require(tool: str) -> str:
    path = shutil.which(tool)
    if path is None:
        raise ToolMissing(f"'{tool}' was not found on PATH. See README.md > Setup.")
    return path


def run(
    cmd: Sequence[str | Path],
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> CmdResult:
    """Run a command, never raising on a non-zero exit code."""
    argv = tuple(str(c) for c in cmd)
    start = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=timeout,
        )
        code, out = proc.returncode, proc.stdout
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode(errors="replace")
        code, out = 124, out + f"\n[refactor-pilot] timed out after {timeout}s"
    return CmdResult(argv, code, out, time.monotonic() - start)
