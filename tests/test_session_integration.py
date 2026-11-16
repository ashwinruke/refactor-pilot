"""The executor loop against the real toolchain, on a git copy of the fixture."""

import shutil
import subprocess
from pathlib import Path

import pytest

from refactor_pilot.build import BuildConfig, IncrementalBuilder, known_objects, split_by_object
from refactor_pilot.cli import main
from refactor_pilot.session import ChangeSet, Session

ROOT = Path(__file__).parent.parent
FIXTURE = ROOT / "fixtures" / "legacy_inventory"
PATCHES = sorted((ROOT / "examples" / "patches").glob("*.patch"))
TOOLS = ("cmake", "ninja", "ctest", "g++", "git")


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def test_split_by_object_assigns_output_to_the_right_file():
    output = (
        "[1/3] Building CXX object CMakeFiles/x.dir/a.cpp.o\n"
        "/r/a.cpp:1:1: warning: w1 [-Wa]\n"
        "[2/3] Building CXX object CMakeFiles/x.dir/b.cpp.o\n"
        "FAILED: CMakeFiles/x.dir/c.cpp.o\n"
        "/usr/bin/g++ -c c.cpp\n"
        "/r/c.cpp:2:2: error: boom\n"
        "[3/3] Linking CXX executable x\n"
        "ld: note\n"
    )
    per_object, rest = split_by_object(output)
    assert "w1" in per_object["CMakeFiles/x.dir/a.cpp.o"]
    assert per_object["CMakeFiles/x.dir/b.cpp.o"] == ""
    assert "boom" in per_object["CMakeFiles/x.dir/c.cpp.o"]
    assert rest == "ld: note"


def test_known_objects_handles_relative_and_absolute_outputs(tmp_path):
    """CMake 3.x writes relative object paths, CMake 4.x absolute ones."""
    build = tmp_path / "build"
    build.mkdir()
    relative = [{"directory": str(build), "file": "/r/a.cpp", "output": "CMakeFiles/x.dir/a.cpp.o"}]
    absolute = [{"directory": str(build), "file": "/r/a.cpp", "output": str(build / "CMakeFiles/x.dir/a.cpp.o")}]
    assert known_objects(relative, build) == {"CMakeFiles/x.dir/a.cpp.o"}
    assert known_objects(absolute, build) == {"CMakeFiles/x.dir/a.cpp.o"}
    assert known_objects([{"directory": str(build), "file": "/r/a.cpp"}], build) == set()


needs_toolchain = pytest.mark.skipif(any(shutil.which(t) is None for t in TOOLS), reason="C++ toolchain not installed")
pytestmark = pytest.mark.toolchain


@pytest.fixture()
def repo(tmp_path):
    dest = tmp_path / "user" / "legacy_inventory"
    shutil.copytree(FIXTURE, dest)
    top = dest.parent
    git(top, "init", "-q")
    git(top, "-c", "user.name=u", "-c", "user.email=u@u", "add", "-A")
    git(top, "-c", "user.name=u", "-c", "user.email=u@u", "commit", "-qm", "init")
    return dest


def replace(file, old, new):
    def apply(repo: Path, feedback):
        path = repo / file
        text = path.read_text()
        assert old in text, f"{old!r} not in {file}"
        path.write_text(text.replace(old, new, 1))
    return apply


def session(repo, tmp_path, **kw):
    return Session(repo, work_dir=tmp_path / "work", compiler="g++", branch="rp/t", **kw)


@needs_toolchain
def test_incremental_build_keeps_warnings_of_untouched_files(repo, tmp_path):
    builder = IncrementalBuilder(BuildConfig(repo=repo, work_dir=tmp_path / "b", compiler="g++"))
    first = builder.build()
    assert first.ok and first.rebuilt == 6 and len(first.warnings) == 2
    second = builder.build()
    assert second.rebuilt == 0 and len(second.warnings) == 2  # nothing compiled, nothing forgotten
    path = repo / "src/warehouse.cpp"
    path.write_text(path.read_text().replace("for (int i = 0; i < items_.size(); ++i)",
                                             "for (size_t i = 0; i < items_.size(); ++i)"))
    third = builder.build()
    assert third.rebuilt == 1 and [w.flag for w in third.warnings] == ["-Wunused-function"]


@needs_toolchain
def test_six_patches_end_to_end(repo, tmp_path, capsys):
    """The demo from the guide: 3 good patches survive, 3 bad ones are removed."""
    head = git(repo.parent, "rev-parse", "HEAD")
    code = main(["apply", str(repo), "--patch", *map(str, PATCHES), "--compiler", "g++",
                 "--branch", "rp/demo", "--work-dir", str(tmp_path / "work")])
    out = capsys.readouterr().out
    assert code == 1  # not every patch was accepted
    assert "3 committed, 2 rolled back, 1 reverted" in out

    subjects = git(repo.parent, "log", "--reverse", "--format=%s", f"{head}..rp/demo").splitlines()
    assert [s.split(":")[0] for s in subjects] == ["01-nullptr-logger", "03-range-for-total", "06-override-item"]
    # The user's checkout is exactly as it was, and no worktree is left registered.
    assert git(repo.parent, "rev-parse", "HEAD") == head
    assert git(repo.parent, "status", "--porcelain") == ""
    assert len(git(repo.parent, "worktree", "list").splitlines()) == 1
    assert "NULL" in (repo / "src/logger.cpp").read_text()
    assert "NULL" not in git(repo.parent, "show", "rp/demo:legacy_inventory/src/logger.cpp")
    assert "delete item;\n    return true;" in git(repo.parent, "show", "rp/demo:legacy_inventory/src/warehouse.cpp")


@needs_toolchain
def test_fast_gate_misses_a_leak_and_full_gate_reverts_it(repo, tmp_path):
    with session(repo, tmp_path, full_every=1) as s:
        outcome = s.run(ChangeSet("leak", "drop a delete", replace("src/warehouse.cpp", "    delete item;\n    return true;", "    return true;")))
    assert outcome.status == "reverted" and "sanitizers" in outcome.reason
    assert s.report.head_sha == s.report.base_sha
    assert "rp/t" not in git(repo.parent, "branch", "--list", "rp/t")  # nothing kept, no branch left


@needs_toolchain
def test_new_warning_is_rejected_and_fixing_one_is_accepted(repo, tmp_path):
    with session(repo, tmp_path, sanitizers=False) as s:
        bad = s.run(ChangeSet("warn", "adds an unused variable", replace("src/util.cpp", "int total = 0;", "int total = 0; int unused = 1;")))
        good = s.run(ChangeSet("fix", "fix sign-compare", replace("src/warehouse.cpp", "for (int i = 0;", "for (size_t i = 0;")))
        noop = s.run(ChangeSet("noop", "changes nothing", lambda repo, feedback: None))
        broken = s.run(ChangeSet("raise", "edit blows up", replace("src/util.cpp", "text that is not there", "x")))
    assert bad.status == "rolled_back" and bad.reason.startswith("warnings:")
    assert good.status == "committed" and good.files == ["src/warehouse.cpp"]
    assert noop.status == "no_change"
    assert broken.status == "rolled_back" and "could not apply" in broken.reason


@needs_toolchain
def test_retry_receives_the_gate_feedback(repo, tmp_path):
    seen = []

    def apply(repo: Path, feedback):
        seen.append(feedback)
        path = repo / "src/pricing.cpp"
        text = path.read_text()
        # First attempt changes behaviour; the retry makes a harmless edit instead.
        path.write_text(text.replace("return 0.10;", "return 0.15;") if feedback is None
                        else text.replace("return 0.10;", "return 0.1;"))

    with session(repo, tmp_path, sanitizers=False, max_attempts=3) as s:
        outcome = s.run(ChangeSet("retry", "second try works", apply))
    assert outcome.status == "committed" and outcome.attempts == 2
    assert seen[0] is None
    assert "test now fails: pricing" in seen[1] and "CHECK failed" in seen[1]  # includes the test's own output


@needs_toolchain
def test_later_commit_that_depends_on_a_culprit_is_reverted_too(repo, tmp_path):
    leak = replace("src/warehouse.cpp", "    delete item;\n    return true;", "    return true;")
    dependent = replace("src/warehouse.cpp", "        return false;\n    }\n    return true;", "        return false;\n    }\n    return true;  // released")
    independent = replace("src/logger.cpp", "file_(NULL)", "file_(nullptr)")
    with session(repo, tmp_path, full_every=10) as s:
        a = s.run(ChangeSet("leak", "drop a delete", leak))
        b = s.run(ChangeSet("dependent", "touches the same lines", dependent))
        c = s.run(ChangeSet("independent", "other file", independent))
    assert (a.status, b.status, c.status) == ("reverted", "reverted", "committed")
    assert "conflicts once 'leak' is removed" in b.reason
    log = git(repo.parent, "log", "--format=%s", f"{s.report.base_sha}..rp/t").splitlines()
    assert log == ["independent: other file"]


@needs_toolchain
def test_uncommitted_changes_are_refused(repo, tmp_path):
    (repo / "src/util.cpp").write_text((repo / "src/util.cpp").read_text() + "\n")
    assert main(["apply", str(repo), "--patch", str(PATCHES[0]), "--compiler", "g++", "--work-dir", str(tmp_path / "w")]) == 2
