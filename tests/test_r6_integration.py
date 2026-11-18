"""R6 (dead code) end to end on a git copy of the fixture."""

import shutil
import subprocess
from pathlib import Path

import pytest

from refactor_pilot.cli import main

ROOT = Path(__file__).parent.parent
FIXTURE = ROOT / "fixtures" / "legacy_inventory"
TOOLS = ("cmake", "ninja", "ctest", "g++", "git", "clangd")
FLAG = "INVENTORY_ENABLE_LEGACY_PRICING"

pytestmark = [
    pytest.mark.toolchain,
    pytest.mark.skipif(any(shutil.which(t) is None for t in TOOLS), reason="clangd or C++ toolchain not installed"),
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


def run(repo, tmp_path, capsys, *extra, recipe="r6"):
    code = main(["run", str(repo), "--recipe", recipe, "--manifest", str(repo / "manifest.json"), "--compiler", "g++",
                 "--branch", "rp/r6", "--work-dir", str(tmp_path / "work"), *extra])
    return code, capsys.readouterr().out


def show(repo, file):
    return git(repo.parent, "show", f"rp/r6:legacy_inventory/{file}")


def test_default_run_removes_tier_one_and_only_reports_tier_two(repo, tmp_path, capsys):
    code, out = run(repo, tmp_path, capsys)
    assert code == 0
    assert "4 committed, 0 rolled back, 0 reverted, 0 unchanged, 1 skipped" in out
    assert "R6: 0/6 -> 4/6" in out
    assert f"flag never defined in this build: {FLAG}" in out  # a hint, not an action
    assert "may have callers outside this repository" in out

    pricing = show(repo, "src/pricing.cpp")
    assert "#if 0" not in pricing and "experimental_dynamic_price" not in pricing
    assert "kUseTieredDiscounts" not in pricing and "legacy_round_cents" not in pricing
    assert "#include <map>" not in pricing
    assert FLAG in pricing  # nobody declared it stale, so it stays
    assert "legacy_flat_tax" in show(repo, "include/inventory/pricing.h")  # tier 2 stays
    assert "return gross * (1.0 - tier_discount(quantity));" in pricing
    assert git(repo.parent, "status", "--porcelain") == ""


def test_declared_flag_and_api_removal_reach_full_score(repo, tmp_path, capsys):
    code, out = run(repo, tmp_path, capsys, "--flag", f"{FLAG}=off", "--allow-api-changes")
    assert code == 0 and "6 committed, 0 rolled back" in out and "R6: 0/6 -> 6/6" in out
    pricing = show(repo, "src/pricing.cpp")
    assert FLAG not in pricing and "#ifdef" not in pricing and "legacy_flat_tax" not in pricing
    header = show(repo, "include/inventory/pricing.h")
    assert "legacy_flat_tax" not in header and "double bulk_price(double unit_price, int quantity);" in header
    assert "Flat tax used before" not in header  # its comment went with it


def test_wrongly_declared_flag_is_caught_by_the_tests(repo, tmp_path, capsys):
    """Declaring the flag ON would switch the legacy pricing branch in. The gate refuses."""
    code, out = run(repo, tmp_path, capsys, "--flag", f"{FLAG}=on")
    assert code == 0
    assert f"rolled_back --------  r6/flag/{FLAG}/src/pricing.cpp" in out
    assert "test now fails: pricing" in out
    assert FLAG in show(repo, "src/pricing.cpp")


def test_bad_flag_syntax_is_rejected(repo, tmp_path):
    with pytest.raises(SystemExit, match="NAME=on or NAME=off"):
        main(["run", str(repo), "--recipe", "r6", "--flag", "FOO=maybe", "--work-dir", str(tmp_path / "w")])


def test_recipes_chain_on_one_branch(repo, tmp_path, capsys):
    code, out = run(repo, tmp_path, capsys, "--flag", f"{FLAG}=off", recipe="r1,r6")
    assert code == 0
    assert "R1: 0/11 -> 11/11" in out and "R6: 0/6 -> 5/6" in out
    subjects = git(repo.parent, "log", "--reverse", "--format=%s", "HEAD..rp/r6").splitlines()
    first_r6 = next(i for i, s in enumerate(subjects) if s.startswith("r6/"))
    assert all(s.startswith("r1/") for s in subjects[:first_r6]) and all(s.startswith("r6/") for s in subjects[first_r6:])


def test_unknown_recipe(repo, tmp_path, capsys):
    assert main(["run", str(repo), "--recipe", "r9", "--work-dir", str(tmp_path / "w")]) == 2
