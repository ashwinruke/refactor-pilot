"""End-to-end checks against the real toolchain, on copies of the fixture."""

import shutil
from pathlib import Path

import pytest

from refactor_pilot import baseline
from refactor_pilot.build import BuildConfig, BuildFailed

FIXTURE = Path(__file__).parent.parent / "fixtures" / "legacy_inventory"
TOOLS = ("cmake", "ninja", "ctest", "g++", "clang-tidy")

pytestmark = [
    pytest.mark.toolchain,
    pytest.mark.skipif(any(shutil.which(t) is None for t in TOOLS), reason="C++ toolchain not installed"),
]


@pytest.fixture()
def repo(tmp_path):
    dest = tmp_path / "repo"
    shutil.copytree(FIXTURE, dest)
    return dest


def capture(repo, tmp_path, name, **kw):
    # g++ here because its sanitizer runtime ships with the compiler.
    cfg = BuildConfig(repo=repo, work_dir=tmp_path / name, compiler="g++")
    return baseline.capture(cfg, **kw)


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"{old!r} not found in {path}"
    path.write_text(text.replace(old, new, 1))


@pytest.fixture()
def before(repo, tmp_path):
    return capture(repo, tmp_path, "before", tidy=False)


def test_fixture_baseline_is_green_with_known_warnings(repo, tmp_path):
    snap = capture(repo, tmp_path, "w")
    assert set(snap.tests.values()) == {"pass"} and len(snap.tests) == 5
    assert set(snap.sanitizer_tests.values()) == {"pass"}
    flags = {key.split("|")[1] for key in snap.warnings}
    assert flags == {"-Wsign-compare", "-Wunused-function"}
    assert snap.tidy["modernize-use-nullptr"] > 0
    assert snap.tidy["modernize-use-override"] > 0


def test_second_capture_still_sees_warnings(repo, tmp_path, before):
    """An incremental no-op build prints nothing; captures must not rely on it."""
    again = capture(repo, tmp_path, "before", tidy=False)
    assert again.warnings == before.warnings and before.warnings


def test_behaviour_change_is_caught(repo, tmp_path, before):
    edit(repo / "src/pricing.cpp", "return 0.10;", "return 0.15;")
    reg = baseline.compare(before, capture(repo, tmp_path, "after", tidy=False))
    assert reg.broken_tests == ["pricing"]


def test_new_warning_is_caught(repo, tmp_path, before):
    edit(repo / "src/util.cpp", "int total = 0;", "int total = 0; int unused_local = 1;")
    reg = baseline.compare(before, capture(repo, tmp_path, "after", tidy=False))
    assert any("-Wunused-variable" in key for key in reg.new_warnings)
    assert not reg.broken_tests


def test_leak_is_caught_only_by_sanitizers(repo, tmp_path, before):
    """Dropping a delete keeps every plain test green; only ASan notices."""
    edit(repo / "src/warehouse.cpp", "    delete item;\n    return true;", "    return true;")
    reg = baseline.compare(before, capture(repo, tmp_path, "after", tidy=False))
    assert reg.broken_tests == []
    assert reg.broken_sanitizer_tests == ["warehouse"]


def test_use_after_free_is_caught_by_sanitizers(repo, tmp_path, before):
    edit(repo / "src/warehouse.cpp", "        delete item;\n        return NULL;",
         "        delete item;\n        if (item->id() == 0) { return NULL; }\n        return NULL;")
    reg = baseline.compare(before, capture(repo, tmp_path, "after", tidy=False))
    assert "warehouse" in reg.broken_sanitizer_tests


def test_compile_error_raises_with_location(repo, tmp_path):
    edit(repo / "src/util.cpp", "int total = 0;", "int total = undeclared_name;")
    with pytest.raises(BuildFailed) as exc:
        capture(repo, tmp_path, "broken", sanitizers=False, tidy=False)
    assert exc.value.errors and exc.value.errors[0].file == "src/util.cpp"


def test_fixture_also_builds_as_cxx20(repo, tmp_path):
    cfg = BuildConfig(repo=repo, work_dir=tmp_path / "cxx20", compiler="g++", cmake_args=["-DCMAKE_CXX_STANDARD=20"])
    snap = baseline.capture(cfg, sanitizers=False, tidy=False)
    assert set(snap.tests.values()) == {"pass"}
