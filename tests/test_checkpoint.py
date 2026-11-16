"""Checkpoint manager tests. These need only git."""

import shutil
import subprocess
from pathlib import Path

import pytest

from refactor_pilot.checkpoint import CheckpointError, Workspace

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def user_repo(tmp_path):
    """A git repo whose project lives in a subdirectory, like the fixture does."""
    top = tmp_path / "user"
    (top / "proj").mkdir(parents=True)
    (top / "proj" / "a.txt").write_text("one\ntwo\nthree\n")
    (top / "other.txt").write_text("outside the project\n")
    git(top, "init", "-q")
    git(top, "-c", "user.name=u", "-c", "user.email=u@u", "add", "-A")
    git(top, "-c", "user.name=u", "-c", "user.email=u@u", "commit", "-qm", "init")
    return top


@pytest.fixture()
def ws(user_repo, tmp_path):
    workspace = Workspace.create(user_repo / "proj", tmp_path / "work", branch="rp/test")
    yield workspace
    workspace.remove(delete_branch=True)


def test_worktree_is_separate_and_on_its_own_branch(ws, user_repo):
    assert ws.worktree != user_repo and ws.repo == ws.worktree / "proj"
    assert (ws.repo / "a.txt").read_text() == "one\ntwo\nthree\n"
    assert git(ws.worktree, "rev-parse", "--abbrev-ref", "HEAD") == "rp/test"
    assert git(user_repo, "rev-parse", "--abbrev-ref", "HEAD") != "rp/test"


def test_commit_and_rollback_never_touch_the_users_tree(ws, user_repo):
    head_before = git(user_repo, "rev-parse", "HEAD")
    (ws.repo / "a.txt").write_text("ONE\ntwo\nthree\n")
    (ws.repo / "new.txt").write_text("new\n")
    assert ws.changed_files() == ["a.txt", "new.txt"]
    sha = ws.commit("change one")
    assert sha and ws.commits() == [(sha, "change one")]

    (ws.repo / "a.txt").write_text("garbage")
    (ws.repo / "junk.txt").write_text("junk")
    ws.rollback()
    assert (ws.repo / "a.txt").read_text() == "ONE\ntwo\nthree\n"
    assert not (ws.repo / "junk.txt").exists() and ws.changed_files() == []

    assert (user_repo / "proj" / "a.txt").read_text() == "one\ntwo\nthree\n"
    assert not (user_repo / "proj" / "new.txt").exists()
    assert git(user_repo, "rev-parse", "HEAD") == head_before
    assert git(user_repo, "status", "--porcelain") == ""


def test_commit_with_no_changes_returns_none(ws):
    assert ws.commit("nothing") is None


def test_commit_needs_no_git_identity_configured(ws, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    (ws.repo / "a.txt").write_text("x\n")
    assert ws.commit("works without user.name")


def test_reset_and_cherry_pick_drop_a_middle_commit(ws):
    (ws.repo / "a.txt").write_text("ONE\ntwo\nthree\n")
    first = ws.commit("first")
    (ws.repo / "b.txt").write_text("bad\n")
    ws.commit("bad")
    (ws.repo / "a.txt").write_text("ONE\ntwo\nTHREE\n")
    third = ws.commit("third")

    ws.reset_to(first)
    assert ws.cherry_pick(third)
    assert [subject for _, subject in ws.commits()] == ["first", "third"]
    assert not (ws.repo / "b.txt").exists()
    assert (ws.repo / "a.txt").read_text() == "ONE\ntwo\nTHREE\n"


def test_cherry_pick_conflict_is_reported_and_cleaned_up(ws):
    base = ws.head()
    (ws.repo / "a.txt").write_text("one\nTWO\nthree\n")
    ws.commit("a")
    (ws.repo / "a.txt").write_text("one\nTWO-AGAIN\nthree\n")
    dependent = ws.commit("b depends on a")
    ws.reset_to(base)
    assert ws.cherry_pick(dependent) is None
    assert ws.changed_files() == [] and ws.head() == base


def test_apply_patch_uses_project_relative_paths(ws, tmp_path):
    patch = tmp_path / "p.patch"
    patch.write_text("Subject: x\n\n--- a/a.txt\n+++ b/a.txt\n@@ -1,3 +1,3 @@\n one\n-two\n+2\n three\n")
    ws.apply_patch(patch)
    assert (ws.repo / "a.txt").read_text() == "one\n2\nthree\n"
    with pytest.raises(CheckpointError):
        ws.apply_patch(patch)  # already applied: context no longer matches


def test_dirty_target_is_refused_unless_allowed(user_repo, tmp_path):
    (user_repo / "proj" / "a.txt").write_text("uncommitted\n")
    with pytest.raises(CheckpointError, match="uncommitted"):
        Workspace.create(user_repo / "proj", tmp_path / "w1", branch="rp/d1")
    allowed = Workspace.create(user_repo / "proj", tmp_path / "w2", branch="rp/d2", allow_dirty=True)
    assert (allowed.repo / "a.txt").read_text() == "one\ntwo\nthree\n"  # starts from the last commit
    allowed.remove(delete_branch=True)
    assert (user_repo / "proj" / "a.txt").read_text() == "uncommitted\n"


def test_dirt_outside_the_target_is_ignored(user_repo, tmp_path):
    (user_repo / "other.txt").write_text("changed\n")
    Workspace.create(user_repo / "proj", tmp_path / "w", branch="rp/o").remove(delete_branch=True)


def test_not_a_git_repo(tmp_path):
    (tmp_path / "plain").mkdir()
    with pytest.raises(CheckpointError, match="not inside a git repository"):
        Workspace.create(tmp_path / "plain", tmp_path / "w")


def test_remove_keeps_branch_by_default(user_repo, tmp_path):
    workspace = Workspace.create(user_repo / "proj", tmp_path / "w", branch="rp/keep")
    (workspace.repo / "a.txt").write_text("kept\n")
    sha = workspace.commit("kept")
    workspace.remove()
    assert not workspace.worktree.exists()
    assert git(user_repo, "rev-parse", "rp/keep") == sha
    assert "rp/keep" not in git(user_repo, "worktree", "list")
