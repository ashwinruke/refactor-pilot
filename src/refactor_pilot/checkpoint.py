"""git_checkpoint_manager: an isolated git worktree to edit in.

The user's working tree is never touched. A session gets its own worktree on its
own branch; each verified change-set becomes one commit there, and a failed one
is discarded with `git reset --hard` inside that worktree only.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .toolchain import CmdResult, require, run

IDENTITY = ["-c", "user.name=refactor-pilot", "-c", "user.email=refactor-pilot@users.noreply.local"]


class CheckpointError(RuntimeError):
    pass


def _git(cwd: Path, *args: str, check: bool = True) -> CmdResult:
    result = run(["git", "-C", cwd, *args])
    if check and not result.ok:
        raise CheckpointError(f"git {' '.join(args)} failed: {result.output.strip()[-400:]}")
    return result


@dataclass
class Workspace:
    source_repo: Path  # the directory the user pointed at
    toplevel: Path  # root of the user's git repository
    prefix: Path  # source_repo relative to toplevel ("." when they are the same)
    worktree: Path  # root of our private checkout
    branch: str
    base_sha: str

    @property
    def repo(self) -> Path:
        """The target project inside the private checkout. All edits happen here."""
        return self.worktree / self.prefix

    @classmethod
    def create(cls, repo: Path, work_dir: Path, branch: str | None = None, allow_dirty: bool = False) -> "Workspace":
        require("git")
        source = Path(repo).resolve()
        top = run(["git", "-C", source, "rev-parse", "--show-toplevel"])
        if not top.ok:
            raise CheckpointError(f"{source} is not inside a git repository. Run 'git init' and commit first.")
        toplevel = Path(top.output.strip()).resolve()
        prefix = source.relative_to(toplevel)
        if not _git(toplevel, "rev-parse", "--verify", "HEAD", check=False).ok:
            raise CheckpointError("the repository has no commits yet; commit first")
        dirty = _git(toplevel, "status", "--porcelain", "--", str(prefix)).output.strip()
        if dirty and not allow_dirty:
            raise CheckpointError(
                "uncommitted changes in the target would be left out of the run "
                "(the worktree starts from the last commit). Commit or stash them, "
                "or pass --allow-dirty:\n" + dirty
            )
        branch = branch or "refactor-pilot/" + time.strftime("%Y%m%d-%H%M%S")
        worktree = Path(work_dir).resolve() / ("worktree-" + branch.replace("/", "-"))
        if worktree.exists():
            raise CheckpointError(f"{worktree} already exists; choose another --branch or delete it")
        worktree.parent.mkdir(parents=True, exist_ok=True)
        _git(toplevel, "worktree", "add", "-b", branch, str(worktree), "HEAD")
        base = _git(worktree, "rev-parse", "HEAD").output.strip()
        return cls(source, toplevel, prefix, worktree, branch, base)

    # ---- safety ----------------------------------------------------------

    def _guard(self) -> None:
        """Refuse destructive commands anywhere but our own worktree."""
        actual = Path(_git(self.worktree, "rev-parse", "--show-toplevel").output.strip()).resolve()
        if actual != self.worktree.resolve() or actual == self.toplevel:
            raise CheckpointError(f"refusing a destructive git command outside the private worktree ({actual})")

    # ---- state -----------------------------------------------------------

    def head(self) -> str:
        return _git(self.worktree, "rev-parse", "HEAD").output.strip()

    def changed_files(self) -> list[str]:
        """Files modified, added or deleted since the last commit, repo-relative."""
        out = _git(self.worktree, "status", "--porcelain", "--untracked-files=all", "--", str(self.prefix)).output
        files = []
        for line in out.splitlines():
            path = line[3:].split(" -> ")[-1].strip().strip('"')
            full = self.worktree / path
            try:
                files.append(full.relative_to(self.repo).as_posix())
            except ValueError:
                files.append(path)
        return sorted(files)

    def commits(self) -> list[tuple[str, str]]:
        """(sha, subject) for every commit made in this workspace, oldest first."""
        out = _git(self.worktree, "log", "--reverse", "--format=%H%x09%s", f"{self.base_sha}..HEAD").output
        return [tuple(line.split("\t", 1)) for line in out.splitlines() if line]

    def diffstat(self) -> tuple[int, int, int]:
        """(files changed, lines added, lines removed) in the target since the session began."""
        out = _git(self.worktree, "diff", "--numstat", self.base_sha, "HEAD", "--", str(self.prefix)).output
        files = added = removed = 0
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                files += 1
                added += int(parts[0]) if parts[0].isdigit() else 0
                removed += int(parts[1]) if parts[1].isdigit() else 0
        return files, added, removed

    # ---- checkpoints -----------------------------------------------------

    def commit(self, message: str) -> str | None:
        """Commit everything changed in the target. Returns the sha, or None if nothing changed."""
        _git(self.worktree, "add", "-A", "--", str(self.prefix))
        if _git(self.worktree, "diff", "--cached", "--quiet", check=False).ok:
            return None
        _git(self.worktree, *IDENTITY, "commit", "-q", "-m", message)
        return self.head()

    def rollback(self) -> None:
        """Throw away every uncommitted edit."""
        self._guard()
        _git(self.worktree, "reset", "-q", "--hard", "HEAD")
        _git(self.worktree, "clean", "-q", "-fd", "--", str(self.prefix))

    def reset_to(self, sha: str) -> None:
        """Move the branch to an earlier (or later) commit, discarding edits."""
        self._guard()
        _git(self.worktree, "reset", "-q", "--hard", sha)
        _git(self.worktree, "clean", "-q", "-fd", "--", str(self.prefix))

    def cherry_pick(self, sha: str) -> str | None:
        """Re-apply a commit on top of HEAD. Returns the new sha, or None on conflict."""
        self._guard()
        result = _git(self.worktree, *IDENTITY, "cherry-pick", "--allow-empty", sha, check=False)
        if result.ok:
            return self.head()
        _git(self.worktree, "cherry-pick", "--abort", check=False)
        self.rollback()
        return None

    def apply_patch(self, patch: Path) -> None:
        """Apply a unified diff whose paths are relative to the target project."""
        args = ["apply", "--whitespace=nowarn"]
        if str(self.prefix) != ".":
            args.append(f"--directory={self.prefix.as_posix()}")
        result = _git(self.worktree, *args, str(Path(patch).resolve()), check=False)
        if not result.ok:
            raise CheckpointError(f"patch does not apply: {result.output.strip()[-300:]}")

    def remove(self, delete_branch: bool = False) -> None:
        """Delete the private checkout. The branch and its commits are kept by default."""
        _git(self.toplevel, "worktree", "remove", "--force", str(self.worktree), check=False)
        shutil.rmtree(self.worktree, ignore_errors=True)
        _git(self.toplevel, "worktree", "prune", check=False)
        if delete_branch:
            _git(self.toplevel, "branch", "-D", self.branch, check=False)
