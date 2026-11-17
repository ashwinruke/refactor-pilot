"""R1 (mechanical modernization) end to end on a git copy of the fixture."""

import shutil
import subprocess
from pathlib import Path

import pytest

from refactor_pilot import build as buildmod
from refactor_pilot.cli import main
from refactor_pilot.diagnostics import Diagnostic
from refactor_pilot.evalkit import score as scoremod
from refactor_pilot.recipes import r1_mechanical
from refactor_pilot.session import Session

ROOT = Path(__file__).parent.parent
FIXTURE = ROOT / "fixtures" / "legacy_inventory"
TOOLS = ("cmake", "ninja", "ctest", "g++", "git", "clang-tidy")

pytestmark = [
    pytest.mark.toolchain,
    pytest.mark.skipif(any(shutil.which(t) is None for t in TOOLS), reason="C++ toolchain not installed"),
]


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def repo(tmp_path):
    dest = tmp_path / "user" / "legacy_inventory"
    shutil.copytree(FIXTURE, dest)
    git(dest.parent, "init", "-q")
    git(dest.parent, "-c", "user.name=u", "-c", "user.email=u@u", "add", "-A")
    git(dest.parent, "-c", "user.name=u", "-c", "user.email=u@u", "commit", "-qm", "init")
    return dest


def show(repo, branch, file):
    return git(repo.parent, "show", f"{branch}:legacy_inventory/{file}")


def test_r1_modernizes_the_fixture_and_every_commit_passed_the_gates(repo, tmp_path, capsys):
    head = git(repo.parent, "rev-parse", "HEAD")
    code = main(["run", str(repo), "--manifest", str(repo / "manifest.json"), "--compiler", "g++",
                 "--branch", "rp/r1", "--work-dir", str(tmp_path / "work")])
    out = capsys.readouterr().out
    assert code == 0
    assert "manifest score: 0/32 ->" in out

    # The user's checkout is untouched.
    assert git(repo.parent, "rev-parse", "HEAD") == head
    assert git(repo.parent, "status", "--porcelain") == ""
    assert "NULL" in (repo / "src/warehouse.cpp").read_text()

    # The branch holds one commit per change-set, each named after its check.
    subjects = git(repo.parent, "log", "--format=%s", f"{head}..rp/r1").splitlines()
    assert len(subjects) >= 10 and all(s.startswith("r1/") for s in subjects)

    # Spot-check the code itself.
    warehouse = show(repo, "rp/r1", "src/warehouse.cpp")
    assert "NULL" not in warehouse and "nullptr" in warehouse
    assert "const_iterator" not in warehouse and "for (auto" in warehouse
    item_h = show(repo, "rp/r1", "include/inventory/item.h")
    assert item_h.count("override") == 4 and "typedef" not in item_h
    assert "= delete" in show(repo, "rp/r1", "include/inventory/warehouse.h")
    # Public constructor signatures are left alone (pass-by-value is opt-in).
    assert "Item(ItemId id, const std::string& name, double unit_price, int quantity);" in item_h

    # Score the branch: check it out into a scratch directory and count.
    scratch = tmp_path / "scratch"
    git(repo.parent, "worktree", "add", "-q", str(scratch), "rp/r1")
    result = scoremod.score(scratch / "legacy_inventory", repo / "manifest.json")
    done, total = result.by_recipe["R1"]
    assert total == 11 and done >= 9, result.remaining
    assert result.by_recipe["R2"][0] == 0  # R1 must not claim work it did not do

    # And the modernized tree is at least as clean as the original.
    cfg = buildmod.BuildConfig(repo=scratch / "legacy_inventory", work_dir=tmp_path / "after", compiler="g++")
    built = buildmod.build(cfg)
    assert len(built.warnings) <= 2
    assert set(buildmod.run_tests(cfg, built.build_dir).values()) == {"pass"}


def test_checks_option_limits_the_run(repo, tmp_path, capsys):
    main(["run", str(repo), "--checks", "modernize-use-nullptr", "--compiler", "g++", "--no-sanitizers",
          "--branch", "rp/only", "--work-dir", str(tmp_path / "work")])
    capsys.readouterr()
    assert "NULL" not in show(repo, "rp/only", "src/logger.cpp")
    assert "typedef" in show(repo, "rp/only", "include/inventory/item.h")  # other checks did not run
    subjects = git(repo.parent, "log", "--format=%s", "HEAD..rp/only").splitlines()
    assert subjects and all(s.startswith("r1/use-nullptr/") for s in subjects)


def test_bad_header_fix_is_rolled_back_and_the_files_own_fix_is_kept(repo, tmp_path, monkeypatch):
    """A fix-it is a proposal. If the gate rejects it, it does not land; and a
    file's own good fixes are retried without the header that sank them."""
    real_tidy_file = buildmod.tidy_file

    def fake_tidy_file(cfg, build_dir, path, checks, header_filter=None, fix=False):
        if "fake-check" not in checks:
            return real_tidy_file(cfg, build_dir, path, checks, header_filter, fix)
        if path.name != "logger.cpp":
            return []
        source, header = cfg.repo / "src/logger.cpp", cfg.repo / "include/inventory/logger.h"
        if not fix:
            if "file_(NULL)" not in source.read_text():
                return []
            return [Diagnostic("src/logger.cpp", 5, 1, "warning", "own finding", "fake-check"),
                    Diagnostic("include/inventory/logger.h", 20, 1, "warning", "header finding", "fake-check")]
        source.write_text(source.read_text().replace("file_(NULL)", "file_(nullptr)"))  # a good fix
        if header_filter != buildmod.NO_HEADERS:
            header.write_text(header.read_text().replace("int lines_;", "int lines_"))  # a fix that breaks the build
        return []

    monkeypatch.setattr(buildmod, "tidy_file", fake_tidy_file)
    with Session(repo, work_dir=tmp_path / "work", compiler="g++", branch="rp/fake", sanitizers=False) as session:
        result = r1_mechanical.run(session, ["fake-check"])
    first, retry = result.outcomes
    assert first.status == "rolled_back" and first.reason.startswith("compile:")
    assert retry.status == "committed" and retry.id.endswith("#own") and retry.files == ["src/logger.cpp"]
    assert "file_(nullptr)" in show(repo, "rp/fake", "src/logger.cpp")
    assert "int lines_;" in show(repo, "rp/fake", "include/inventory/logger.h")


def test_finding_without_a_fix_is_reported_once_not_per_file(repo, tmp_path):
    with Session(repo, work_dir=tmp_path / "work", compiler="g++", branch="rp/nofix", sanitizers=False) as session:
        result = r1_mechanical.run(session, ["modernize-avoid-c-arrays"])
    statuses = [o.status for o in result.outcomes]
    assert statuses and set(statuses) == {"no_change"}
    assert all(o.reason == r1_mechanical.NO_FIX for o in result.outcomes)
    files = [o.id for o in result.outcomes]
    assert len(files) == len(set(files))
