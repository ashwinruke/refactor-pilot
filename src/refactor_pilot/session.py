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
    why the previous attempt was rejected, as text (an edit that would not
    apply, compiler errors, failing tests, an unmet goal); on the first attempt
    it is None. Deterministic edits ignore it; the agent reads it.

    Optional:
      max_attempts  overrides the session's retry limit for this change-set
      gate          "fast" (default) or "full": which gate decides accept/retry.
                    Ownership edits use "full", because only the sanitizers can
                    see a leak or a double free, and the model should get that
                    feedback while it can still retry.
      accept        goal check run before the gate. Returns None when the edit
                    did what was asked, else a message explaining what is missing.
      rebaseline    for edits that legitimately change what the compiler reports
                    (raising the language standard). The tree must still build
                    and every test must still pass, sanitizers included, but the
                    set of warnings is allowed to change and becomes the new
                    baseline for everything after it.
    """

    id: str
    description: str
    apply: Callable[[Path, str | None], None]
    no_change_reason: str = "the edit changed nothing"
    max_attempts: int | None = None
    gate: str = "fast"
    accept: Callable[[Path], str | None] | None = None
    rebaseline: bool = False


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
    feedback: str = ""  # the full text of the last rejection: compiler errors, failing test output


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
        cmake_args: list[str] | None = None,
        test_timeout: int = 120,
    ):
        self._source_cfg = BuildConfig(repo=repo, work_dir=work_dir, compiler=compiler,
                                       cmake_args=list(cmake_args or []), test_timeout=test_timeout)
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
        self.warning_shift = 0  # warnings gained (+) or lost (-) at the last re-baseline

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
                cmake_args=list(self._source_cfg.cmake_args),
                test_timeout=self._source_cfg.test_timeout,
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
        attempts = change.max_attempts or self.max_attempts
        for attempt in range(1, attempts + 1):
            outcome.attempts = attempt
            try:
                change.apply(ws.repo, feedback)
            except Exception as exc:  # a broken edit must never take the session down
                ws.rollback()
                outcome.reason = f"could not apply: {exc}".splitlines()[0][:300]
                if getattr(exc, "fatal", False):  # e.g. budget spent: stop the whole recipe
                    outcome.seconds = time.monotonic() - start
                    self._log(f"{outcome.status:<11} {change.id}  ({outcome.reason})")
                    raise
                feedback = f"Your edit could not be applied.\n{exc}"
                self._attempt_failed(change, attempt, attempts, outcome.reason)
                continue
            outcome.files = ws.changed_files()
            if not outcome.files:
                outcome.status, outcome.reason = "no_change", change.no_change_reason
                break
            problem = change.accept(ws.repo) if change.accept else None
            if problem:
                ws.rollback()
                outcome.reason, feedback = " ".join(f"goal not met: {problem}".split())[:300], problem
                self._attempt_failed(change, attempt, attempts, outcome.reason)
                continue
            if change.rebaseline:
                problem = self._rebaseline()
                if problem:
                    ws.rollback()
                    outcome.reason, feedback = problem, problem
                    self._attempt_failed(change, attempt, attempts, outcome.reason)
                    continue
                gate = GateResult("full", ok=True)
            elif change.gate == "full":
                self._full_gates += 1
                gate = self.verifier.full()
            else:
                gate = self.verifier.fast()
            if gate.ok:
                outcome.sha = ws.commit(f"{change.id}: {change.description}")
                outcome.status, outcome.reason = "committed", ""
                if change.gate == "full" or change.rebaseline:
                    # A full pass at this commit also covers everything before it.
                    self._pending, self._last_green = [], outcome.sha
                else:
                    self._pending.append(outcome)
                break
            ws.rollback()
            outcome.reason, feedback = gate.summary(), gate.feedback()
            self._attempt_failed(change, attempt, attempts, outcome.reason)
        outcome.seconds = time.monotonic() - start
        outcome.feedback = "" if outcome.status == "committed" else (feedback or "")
        self._log(f"{outcome.status:<11} {change.id}" + (f"  ({outcome.reason})" if outcome.reason else ""))
        if len(self._pending) >= self.full_every:
            self.checkpoint()
        return outcome

    def _rebaseline(self) -> str | None:
        """Take a fresh snapshot and adopt it, provided the tests did not regress.

        Returns None on success, else why the tree was not acceptable.
        """
        from .baseline import compare
        from .build import BuildFailed

        old = self.verifier.baseline
        try:
            new = self.verifier.snapshot()
        except BuildFailed as exc:
            return f"compile: {exc}"
        self._full_gates += 1
        regressions = compare(old, new)
        regressions.new_warnings = {}  # allowed to change; that is the point
        if not regressions.ok:
            return "tests: " + "; ".join(regressions.describe()[:3])
        self.warning_shift = sum(new.warnings.values()) - sum(old.warnings.values())
        self.verifier.set_baseline(new)
        return None

    def _attempt_failed(self, change: ChangeSet, attempt: int, attempts: int, reason: str) -> None:
        if attempts > 1:  # only worth a line when there is a retry to explain
            self._log(f"  attempt {attempt}/{attempts} of {change.id} rejected: {reason}")

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
