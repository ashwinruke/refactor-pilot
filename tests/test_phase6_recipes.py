"""std, the C++20 clang-tidy checks, and the model-written recipes R3, R4, R5."""

import shutil
import subprocess
from pathlib import Path

import pytest

from refactor_pilot.cli import main
from refactor_pilot.recipes import llm_engine as engine
from refactor_pilot.recipes import std_raise

ROOT = Path(__file__).parent.parent
FIXTURE = ROOT / "fixtures" / "legacy_inventory"
REPLAYS = ROOT / "examples" / "replays"
TOOLS = ("cmake", "ninja", "ctest", "g++", "git", "clangd", "clang-tidy")
FLAG = "INVENTORY_ENABLE_LEGACY_PRICING"


# ---- unit ---------------------------------------------------------------------


@pytest.mark.parametrize("before, after", [
    ("set(CMAKE_CXX_STANDARD 11 CACHE STRING \"std\")", "set(CMAKE_CXX_STANDARD 20 CACHE STRING \"std\")"),
    ("set_target_properties(app PROPERTIES CXX_STANDARD 14)", "set_target_properties(app PROPERTIES CXX_STANDARD 20)"),
    ("target_compile_features(lib PUBLIC cxx_std_11)", "target_compile_features(lib PUBLIC cxx_std_20)"),
    ("set(CMAKE_CXX_FLAGS \"-std=gnu++0x -O2\")", "set(CMAKE_CXX_FLAGS \"-std=gnu++20 -O2\")"),
])
def test_raise_in_text_understands_the_usual_spellings(before, after):
    assert std_raise.raise_in_text(before, 20) == (after, 1)


def test_raise_in_text_never_lowers_a_standard():
    text = "set(CMAKE_CXX_STANDARD 23)\ntarget_compile_features(a PUBLIC cxx_std_20)\n"
    assert std_raise.raise_in_text(text, 20) == (text, 0)


def test_standard_number():
    assert [engine.standard_number(s) for s in ("C++11", "C++2a", "C++0x", "C++20", "")] == [11, 20, 11, 20, 17]


def test_only_value_like_macros_are_targets():
    source = (
        "#ifndef GUARD_H\n#define GUARD_H\n"
        "#define FEATURE_FLAG\n"                       # empty: a flag, not a value
        "#define STRINGIFY(x) #x\n"                    # needs the preprocessor
        "#define LOG(msg) do { log(msg); } while (0)\n"  # a statement
        "#define LIMIT 64\n"                           # tested in an #if below
        "#define SIZE 128\n"                           # a constant: target
        "#define SQUARE(x) ((x) * (x))\n"              # an expression: target
        "#if LIMIT > 32\n#endif\n#endif\n"
    )
    context = {"condition_macros": {"LIMIT", "GUARD_H"}}
    assert [text for _, text in engine._macro_hits(source, context)] == ["#define SIZE 128", "#define SQUARE(x) ((x) * (x))"]


def test_every_kind_belongs_to_a_recipe_and_formats_its_goal():
    assert set(engine.RECIPE_KINDS) == {"r2", "r3", "r4", "r5"}
    for kind in engine.KINDS.values():
        assert "`X`" in kind.goal.format(owner="`X`")


# ---- end to end ----------------------------------------------------------------

needs_toolchain = pytest.mark.skipif(any(shutil.which(t) is None for t in TOOLS), reason="clang tools or C++ toolchain not installed")


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


def run(repo, tmp_path, capsys, recipe, *extra):
    code = main(["run", str(repo), "--recipe", recipe, "--manifest", str(repo / "manifest.json"), "--compiler", "g++",
                 "--branch", "rp/p6", "--work-dir", str(tmp_path / "work"), *extra])
    return code, capsys.readouterr().out


def show(repo, file):
    return git(repo.parent, "show", f"rp/p6:legacy_inventory/{file}")


@needs_toolchain
@pytest.mark.toolchain
def test_std_then_clang_tidy_resolves_sfinae_without_a_model(repo, tmp_path, capsys):
    code, out = run(repo, tmp_path, capsys, "std,r1", "--checks", "modernize-use-constraints,modernize-type-traits")
    assert code == 0
    assert "std/raise: raise the language standard from C++11 to C++20" in out
    assert "now compiling as C++20" in out and "R4: 0/1 -> 1/1" in out
    util = show(repo, "include/inventory/util.h")
    assert "enable_if" not in util and "requires" in util
    assert "set(CMAKE_CXX_STANDARD 20 CACHE" in show(repo, "CMakeLists.txt")


@needs_toolchain
@pytest.mark.toolchain
def test_cxx20_recipes_are_skipped_until_the_standard_is_raised(repo, tmp_path, capsys):
    code, out = run(repo, tmp_path, capsys, "r4,r5", "--replay", str(REPLAYS / "r345-fixture-internal.jsonl"), "--no-sanitizers")
    assert code == 0 and "0 committed" in out
    assert out.count("needs C++20; the project compiles as C++11. Raise it first: --recipe std,...") >= 4
    assert "model usage: 0 requests" in out  # nothing was asked of the model


@needs_toolchain
@pytest.mark.toolchain
def test_std_is_rolled_back_when_the_code_is_not_valid_at_the_new_standard(repo, tmp_path, capsys):
    source = repo / "src/util.cpp"
    source.write_text(source.read_text().replace("int total = 0;", "int total = 0; int requires = 0; (void)requires;", 1))
    git(repo.parent, "-c", "user.name=u", "-c", "user.email=u@u", "commit", "-qam", "uses a C++20 keyword as a name")
    code, out = run(repo, tmp_path, capsys, "std,r1", "--checks", "modernize-use-nullptr", "--no-sanitizers")
    assert code == 0
    assert "rolled_back --------  std/raise" in out and "reason: compile:" in out
    assert "r1/use-nullptr/src/logger.cpp" in out  # later recipes still run, at the old standard
    assert "set(CMAKE_CXX_STANDARD 11 CACHE" in show(repo, "CMakeLists.txt")


@needs_toolchain
@pytest.mark.toolchain
def test_r345_internal_replay(repo, tmp_path, capsys):
    code, out = run(repo, tmp_path, capsys, "std,r3,r4,r5", "--replay", str(REPLAYS / "r345-fixture-internal.jsonl"))
    assert code == 0
    assert "6 committed, 0 rolled back, 0 reverted, 0 unchanged, 3 skipped" in out
    assert "attempt 1/3 of r5/printf-buffer/inventory::Item rejected: tests: test now fails: item" in out
    assert "R3: 0/3 -> 2/3" in out and "R4: 0/1 -> 1/1" in out and "R5: 0/3 -> 2/3" in out
    util = show(repo, "include/inventory/util.h")
    assert "#define INVENTORY_MAX" not in util and "#define INVENTORY_SQUARE" not in util
    assert "constexpr int INVENTORY_MAX_ORDER = 10000;" in util
    assert "#ifndef INVENTORY_UTIL_H\n#define INVENTORY_UTIL_H" in util  # the include guard is not a target
    assert "template <std::integral T>" in util
    item = show(repo, "src/item.cpp")
    assert 'std::format("#{} {} x{} @ {:.2f}"' in item and "snprintf" not in item
    assert "text.starts_with(prefix)" in show(repo, "src/util.cpp")
    assert show(repo, "tests/test_main.cpp") == (FIXTURE / "tests/test_main.cpp").read_text().strip()


@needs_toolchain
@pytest.mark.toolchain
def test_r345_api_replay(repo, tmp_path, capsys):
    code, out = run(repo, tmp_path, capsys, "std,r3,r4,r5", "--allow-api-changes",
                    "--replay", str(REPLAYS / "r345-fixture-api.jsonl"))
    assert code == 0 and "9 committed, 0 rolled back" in out
    assert "R3: 0/3 -> 3/3" in out and "R5: 0/3 -> 3/3" in out
    assert "enum class Category {" in show(repo, "include/inventory/item.h")
    assert "int sum(std::span<const int> data);" in show(repo, "include/inventory/util.h")
    tests = show(repo, "tests/test_main.cpp")
    assert "Category::CATEGORY_PERISHABLE" in tests and "sum(std::span<const int>(values, 4))" in tests
    assert tests.count("CHECK(") == (FIXTURE / "tests/test_main.cpp").read_text().count("CHECK(")


@needs_toolchain
@pytest.mark.toolchain
@pytest.mark.slow
def test_full_pipeline_resolves_every_planted_construct(repo, tmp_path, capsys):
    """Every recipe on one branch: 32 of 32, each commit having passed the gates."""
    code, out = run(repo, tmp_path, capsys, "std,r2,r3,r4,r5,r1,r6", "--allow-api-changes", "--flag", f"{FLAG}=off",
                    "--replay", str(REPLAYS / "full-fixture-api.jsonl"))
    assert code == 0
    assert "manifest score: 0/32 -> 32/32 legacy constructs resolved" in out
    assert "0 rolled back, 0 reverted" in out
    assert git(repo.parent, "status", "--porcelain") == ""
    commits = git(repo.parent, "log", "--format=%s", "HEAD..rp/p6").splitlines()
    assert len(commits) >= 35
    prefixes = {c.split("/")[0] for c in commits}
    assert prefixes == {"std", "r1", "r2", "r3", "r4", "r5", "r6"}


def test_all_expands_to_the_recommended_order(capsys):
    from refactor_pilot.cli import _parse_recipes

    assert _parse_recipes("all") == ["std", "r1", "r6", "r2", "r3", "r4", "r5"]
    assert _parse_recipes("STD, r1") == ["std", "r1"]
    assert _parse_recipes("r1,nope") is None
    assert "unknown recipe 'nope'" in capsys.readouterr().err
