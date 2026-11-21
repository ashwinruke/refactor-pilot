"""Benchmark harness, flaky-test handling and pipeline robustness."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from refactor_pilot import bench, build as buildmod, pipeline
from refactor_pilot.build import BuildConfig
from refactor_pilot.cli import main
from refactor_pilot.recipes import r6_dead_code

ROOT = Path(__file__).parent.parent
FIXTURE = ROOT / "fixtures" / "legacy_inventory"


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit_all(repo):
    git(repo, "init", "-q")
    git(repo, "-c", "user.name=u", "-c", "user.email=u@u", "add", "-A")
    git(repo, "-c", "user.name=u", "-c", "user.email=u@u", "commit", "-qm", "init")


def write_config(path, targets):
    path.write_text(json.dumps({"work_dir": "bench-work", "targets": targets}))
    return path


# ---- config ---------------------------------------------------------------------


def test_shipped_target_list_is_valid_and_pinned():
    data, targets = bench.load_config(ROOT / "bench" / "targets.json")
    assert [t.name for t in targets][0] == "legacy_inventory"
    assert [t.status for t in targets].count("approved") == 1  # only the fixture until others are approved
    for target in targets:
        if target.git:
            assert len(target.commit) == 40 and target.ref


@pytest.mark.parametrize("target, message", [
    ({"name": "x"}, "exactly one of 'path' or 'git'"),
    ({"name": "x", "path": "a", "git": "b"}, "exactly one of 'path' or 'git'"),
    ({"name": "x", "git": "https://example.invalid/r.git", "ref": "v1"}, "needs both 'ref' and 'commit'"),
    ({"name": "x", "path": "a", "recipes": "r1,r9"}, "unknown recipe 'r9'"),
    ({"name": "x", "path": "a", "colour": "blue"}, "unknown field"),
])
def test_bad_targets_are_refused(tmp_path, target, message):
    with pytest.raises((bench.BenchError, ValueError), match=message):
        bench.load_config(write_config(tmp_path / "c.json", [target]))


def test_report_shows_failures_rejections_and_scores():
    ok = bench.TargetResult("alpha", "ok", recipes="r1,r6", ref="v1.0", tests=3, tests_passing=3, committed=7, rolled_back=1,
                            findings_before=90, findings_after=40, warnings_before=2, warnings_after=1, files_changed=4,
                            lines_added=10, lines_removed=12, seconds=61, score_before="0/5", score_after="4/5",
                            by_recipe={"r1": {"committed": 6, "rolled_back": 1}, "r6": {"committed": 1}},
                            rejections=[{"id": "r1/x/a.cpp", "status": "rolled_back", "reason": "tests: test now fails: t"}])
    bad = bench.TargetResult("beta", "failed", error="build failed with exit code 1", recipes="r1", ref="v2")
    text = bench.render_markdown([ok, bad], {"version": "0", "compiler": "g++", "model": "none", "date": "today"})
    assert "| alpha | v1.0 | r1,r6 | 3/3 | 7 | 1 | 90 → 40 | 2 → 1 | 4 files, +10 −12 | 0/5 → 4/5 | 61s |" in text
    assert "| beta | v2 | r1 |" in text and "failed: build failed" in text
    assert "| alpha | `r1/x/a.cpp` | rolled_back | tests: test now fails: t |" in text


# ---- flaky tests -----------------------------------------------------------------

needs_cmake = pytest.mark.skipif(any(shutil.which(t) is None for t in ("cmake", "ctest", "ninja", "g++")),
                                 reason="cmake toolchain not installed")


@needs_cmake
@pytest.mark.toolchain
def test_a_test_that_fails_once_is_rerun_before_it_counts(tmp_path):
    project = tmp_path / "p"
    project.mkdir()
    (project / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.16)\nproject(p NONE)\nenable_testing()\n"
        "add_test(NAME flaky COMMAND ${CMAKE_COMMAND} -DMARK=${CMAKE_BINARY_DIR}/seen -P ${CMAKE_SOURCE_DIR}/flaky.cmake)\n"
        "add_test(NAME broken COMMAND ${CMAKE_COMMAND} -E false)\n"
        "add_test(NAME fine COMMAND ${CMAKE_COMMAND} -E true)\n")
    (project / "flaky.cmake").write_text(
        'if(NOT EXISTS "${MARK}")\n  file(WRITE "${MARK}" x)\n  message(FATAL_ERROR "first run fails")\nendif()\n')
    cfg = BuildConfig(repo=project, work_dir=tmp_path / "w", compiler="g++")
    build_dir = buildmod.configure(cfg)
    buildmod.FLAKY.clear()
    output: dict[str, str] = {}
    results = buildmod.run_tests(cfg, build_dir, failure_output=output)
    assert results == {"broken": "fail", "fine": "pass", "flaky": "pass"}
    assert buildmod.FLAKY == {"flaky"}
    assert set(output) == {"broken"}  # only real failures are explained


# ---- end to end -------------------------------------------------------------------

TOOLS = ("cmake", "ninja", "ctest", "g++", "git", "clangd", "clang-tidy")
needs_toolchain = pytest.mark.skipif(any(shutil.which(t) is None for t in TOOLS), reason="clang tools or C++ toolchain not installed")


@pytest.fixture()
def project(tmp_path):
    repo = tmp_path / "proj" / "legacy_inventory"
    shutil.copytree(FIXTURE, repo)
    commit_all(repo.parent)
    return repo


@needs_toolchain
@pytest.mark.toolchain
def test_bench_runs_a_local_target_and_writes_both_reports(project, tmp_path, capsys):
    config = write_config(tmp_path / "proj" / "targets.json", [
        {"name": "fixture", "path": "legacy_inventory", "manifest": "legacy_inventory/manifest.json",
         "recipes": "r6", "sanitizers": False},
        {"name": "later", "path": "legacy_inventory", "recipes": "r1", "status": "proposed"},
    ])
    out = tmp_path / "out"
    assert main(["bench", str(config), "--out", str(out), "--compiler", "g++"]) == 0
    printed = capsys.readouterr().out
    assert "| fixture | local | r6 | 5/5 | 4 | 0 |" in printed and "0/32 → 4/32" in printed
    assert "| later |" not in printed  # proposed targets do not run by default
    data = json.loads((out / "results.json").read_text())
    result = data["results"][0]
    assert result["committed"] == 4 and result["skipped"] == 1 and result["warnings_after"] < result["warnings_before"]
    assert result["by_recipe"] == {"r6": {"committed": 4, "skipped": 1}}
    assert (out / "REPORT.md").read_text().startswith("# refactor-pilot benchmark")
    # Nothing is left behind in the benchmarked repository.
    assert git(project.parent, "branch", "--list", "refactor-pilot/*") == ""
    assert git(project.parent, "status", "--porcelain", "--", "legacy_inventory") == ""


@needs_toolchain
@pytest.mark.toolchain
def test_git_target_is_cloned_and_must_match_its_pin(project, tmp_path, capsys):
    upstream = project.parent
    git(upstream, "tag", "v1")
    head = git(upstream, "rev-parse", "HEAD")
    base = {"name": "cloned", "git": upstream.as_uri(), "ref": "v1", "subdir": "legacy_inventory",
            "recipes": "r6", "sanitizers": False}
    work = tmp_path / "cfg"
    work.mkdir()
    setup = [{"file": "legacy_inventory/CMakeLists.txt", "find": "# Deliberately old:", "replace": "# Setup edit:"}]
    good = write_config(work / "good.json", [{**base, "commit": head, "prepare": setup}])
    assert main(["bench", str(good), "--out", str(tmp_path / "o1"), "--compiler", "g++"]) == 0
    assert "| cloned | v1 | r6 | 5/5 | 4 |" in capsys.readouterr().out
    assert git(upstream, "branch", "--list", "refactor-pilot/*") == ""  # the upstream repo itself is never touched
    clone = work / "bench-work" / "repos" / "cloned"
    assert "build-system setup (not part of the results)" in git(clone, "log", "-1", "--format=%s")
    assert "# Setup edit:" in (clone / "legacy_inventory" / "CMakeLists.txt").read_text()
    assert "# Deliberately old:" in (upstream / "legacy_inventory" / "CMakeLists.txt").read_text()

    stale = write_config(work / "stale.json", [{**base, "name": "stale", "commit": head,
                                                "prepare": [{"file": "legacy_inventory/CMakeLists.txt", "find": "not there", "replace": "x"}]}])
    assert main(["bench", str(stale), "--out", str(tmp_path / "o3"), "--compiler", "g++"]) == 1
    assert "prepare step did not match" in capsys.readouterr().out

    bad = write_config(work / "bad.json", [{**base, "name": "wrongpin", "commit": "0" * 40}])
    assert main(["bench", str(bad), "--out", str(tmp_path / "o2"), "--compiler", "g++"]) == 1
    assert "but the config pins 000000000000" in capsys.readouterr().out


def test_bench_needs_a_model_only_for_model_recipes(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("REFACTOR_PILOT_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    config = write_config(tmp_path / "c.json", [{"name": "x", "path": ".", "recipes": "r1,r2"}])
    assert main(["bench", str(config), "--out", str(tmp_path / "o")]) == 2
    assert "no API key" in capsys.readouterr().err


@needs_toolchain
@pytest.mark.toolchain
def test_one_recipe_crashing_keeps_the_commits_of_the_others(project, tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("clangd fell over")

    monkeypatch.setattr(r6_dead_code, "run", boom)
    opts = pipeline.PipelineOptions(repo=project, recipes=["r1", "r6"], branch="rp/robust", work_dir=tmp_path / "w",
                                    compiler="g++", sanitizers=False, checks=["modernize-use-nullptr"], measure_tidy=False)
    result = pipeline.run_pipeline(opts)
    assert result.report.count("committed") == 4
    assert any("recipe r6 stopped with an error: RuntimeError: clangd fell over" in note for note in result.notes)
    assert "nullptr" in git(project.parent, "show", "rp/robust:legacy_inventory/src/logger.cpp")


@needs_toolchain
@pytest.mark.toolchain
def test_flag_hints_list_only_flags_the_tool_can_resolve(project, tmp_path):
    source = project / "src" / "util.cpp"
    source.write_text(source.read_text() + "\n#if defined(A_REAL_FLAG)\nint a_real = 1;\n#endif\n"
                      "#if __has_include(<version>) && SOME_NUMBER > 3\nint b = 2;\n#endif\n")
    cfg = BuildConfig(repo=project, work_dir=tmp_path / "w", compiler="g++")
    build_dir = buildmod.configure(cfg)
    hints = r6_dead_code.undefined_flag_candidates(cfg, build_dir, r6_dead_code.repo_files(cfg, build_dir))
    assert "A_REAL_FLAG" in hints and "INVENTORY_ENABLE_LEGACY_PRICING" in hints
    assert "SOME_NUMBER" not in hints and "__has_include" not in hints and "version" not in hints
