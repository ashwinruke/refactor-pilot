"""The executor loop: apply a change-set, verify it, commit or roll back.

    with Session(repo) as session:
        session.run(change_set)      # fast gate; commit on pass, roll back on fail
        ...
    report = session.report          # full gate has run; bad commits are gone

Every few commits (and at the end) the full gate runs. If it fails, a binary
search over the commits since the last full pass finds the one that introduced
the failure; that commit is removed and the later ones are re-applied on top.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .baseline import Snapshot
from .build import BuildConfig
from .checkpoint import Workspace
from .gates import GateResult, Verifier


@dataclass
class ChangeSet:
    """One atomic edit: a definition and all its references, or one codemod.

    `apply(repo, feedback)` edits files under `repo`. On a retry, `feedback` is
    the previous gate failure as text (compiler errors, failing tests); on the
    first attempt it is None. Deterministic edits can ignore it; the Phase 4
    agent will use it.
    """

    id: str
    description: str
    apply: Callable[[Path, str | None], None]
    no_change_reason: str = "the edit changed nothing"


@dataclass
class Outcome:
    id: str
    description: str
    status: str  # committed | rolled_back | reverted | no_change
    sha: str | None = None
    attempts: int = 0
    reason: str = ""
    files: list[str] = field(default_factory=list)
    seconds: float = 0.0


@dataclass
class Report:
    branch: str
    base_sha: str
    head_sha: str
    outcomes: list[Outcome]
    baseline: Snapshot
    full_gates: int
    seconds: float

    def count(self, status: str) -> int:
        return sum(o.status == status for o in self.outcomes)


class Session:
    def __init__(
        self,
        repo: Path,
        work_dir: Path | None = None,
        compiler: str = "clang++",
        branch: str | None = None,
        sanitizers: bool = True,
        full_every: int = 5,
        max_attempts: int = 1,
        allow_dirty: bool = False,
        keep_worktree: bool = False,
        log: Callable[[str], None] = lambda message: None,
    ):
        self._source_cfg = BuildConfig(repo=repo, work_dir=work_dir, compiler=compiler)
        self._branch, self._sanitizers = branch, sanitizers
        self.full_every, self.max_attempts = max(1, full_every), max(1, max_attempts)
        self._allow_dirty, self._keep_worktree, self._log = allow_dirty, keep_worktree, log
        self.workspace: Workspace | None = None
        self.verifier: Verifier | None = None
        self.outcomes: list[Outcome] = []
        self.report: Report | None = None
        self._pending: list[Outcome] = []  # committed since the last full-gate pass
        self._last_green = ""
        self._full_gates = 0
        self._started = 0.0

    @property
    def cfg(self) -> BuildConfig:
        """Build configuration of the private checkout (recipes edit and lint here)."""
        return self.verifier.cfg

    @property
    def build_dir(self) -> Path:
        """Build tree of the private checkout; holds its compile_commands.json."""
        return self.cfg.build_dir("plain")

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> "Session":
        self._started = time.monotonic()
        work = self._source_cfg.work_dir
        self.workspace = Workspace.create(self._source_cfg.repo, work, self._branch, self._allow_dirty)
        try:
            cfg = BuildConfig(
                repo=self.workspace.repo,
                work_dir=work / ("build-" + self.workspace.branch.replace("/", "-")),
                compiler=self._source_cfg.compiler,
            )
            self.verifier = Verifier(cfg, sanitizers=self._sanitizers)
            self._log("recording baseline (full build, first time only)")
            self.verifier.set_baseline()
        except BaseException:
            self.workspace.remove(delete_branch=True)
            raise
        self._last_green = self.workspace.base_sha
        return self

    def finish(self) -> Report:
        if self.report is None:
            self.checkpoint()
            ws = self.workspace
            self.report = Report(
                branch=ws.branch, base_sha=ws.base_sha, head_sha=ws.head(), outcomes=self.outcomes,
                baseline=self.verifier.baseline, full_gates=self._full_gates,
                seconds=time.monotonic() - self._started,
            )
            if not self._keep_worktree:
                ws.remove(delete_branch=ws.head() == ws.base_sha)  # no commits: leave nothing behind
        return self.report

    def __enter__(self) -> "Session":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.finish()
        elif self.workspace is not None and not self._keep_worktree:
            self.workspace.remove(delete_branch=self.workspace.head() == self.workspace.base_sha)

    # ---- the loop --------------------------------------------------------

    def run(self, change: ChangeSet) -> Outcome:
        ws, start = self.workspace, time.monotonic()
        outcome = Outcome(change.id, change.description, "rolled_back")
        self.outcomes.append(outcome)
        feedback: str | None = None
        for attempt in range(1, self.max_attempts + 1):
            outcome.attempts = attempt
            try:
                change.apply(ws.repo, feedback)
            except Exception as exc:  # a broken edit must never take the session down
                ws.rollback()
                outcome.reason = f"could not apply: {exc}"
                break
            outcome.files = ws.changed_files()
            if not outcome.files:
                outcome.status, outcome.reason = "no_change", change.no_change_reason
                break
            gate = self.verifier.fast()
            if gate.ok:
                outcome.sha = ws.commit(f"{change.id}: {change.description}")
                outcome.status, outcome.reason = "committed", ""
                self._pending.append(outcome)
                break
            ws.rollback()
            outcome.reason, feedback = gate.summary(), gate.feedback()
        outcome.seconds = time.monotonic() - start
        self._log(f"{outcome.status:<11} {change.id}" + (f"  ({outcome.reason})" if outcome.reason else ""))
        if len(self._pending) >= self.full_every:
            self.checkpoint()
        return outcome

    def _full(self) -> GateResult:
        self._full_gates += 1
        return self.verifier.full()

    def checkpoint(self) -> None:
        """Run the full gate over everything committed since the last pass."""
        ws = self.workspace
        while self._pending:
            gate = self._full()
            if gate.ok:
                break
            self._log(f"full gate failed ({gate.summary()}); searching {len(self._pending)} commits")
            index, culprit_gate = self._first_bad(gate)
            culprit, later = self._pending[index], self._pending[index + 1:]
            ws.reset_to(self._pending[index - 1].sha if index > 0 else self._last_green)
            culprit.status, culprit.reason, culprit.sha = "reverted", culprit_gate.summary(), None
            self._log(f"reverted    {culprit.id}  ({culprit.reason})")
            survivors = self._pending[:index]
            for outcome in later:  # put the innocent later commits back
                new_sha = ws.cherry_pick(outcome.sha)
                if new_sha is not None:
                    outcome.sha = new_sha
                    survivors.append(outcome)
                else:
                    outcome.status, outcome.sha = "reverted", None
                    outcome.reason = f"conflicts once '{culprit.id}' is removed"
                    self._log(f"reverted    {outcome.id}  ({outcome.reason})")
            self._pending = survivors
            if not later:
                break  # everything left was already shown good during the search
        self._last_green = ws.head()
        self._pending = []

    def _first_bad(self, tip_gate: GateResult) -> tuple[int, GateResult]:
        """Binary search for the first pending commit at which the full gate fails.

        Known: the commit before pending[0] passes, pending[-1] fails.
        """
        ws = self.workspace
        low, high, high_gate = -1, len(self._pending) - 1, tip_gate
        while high - low > 1:
            mid = (low + high) // 2
            ws.reset_to(self._pending[mid].sha)
            gate = self._full()
            if gate.ok:
                low = mid
            else:
                high, high_gate = mid, gate
        return high, high_gate
