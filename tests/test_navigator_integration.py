"""Navigator checks against a real clangd, on a copy of the fixture."""

import shutil
from pathlib import Path

import pytest

from refactor_pilot.build import BuildConfig
from refactor_pilot.navigator import SymbolNavigator

FIXTURE = Path(__file__).parent.parent / "fixtures" / "legacy_inventory"
TOOLS = ("cmake", "ninja", "g++", "clangd")

pytestmark = [
    pytest.mark.toolchain,
    pytest.mark.skipif(any(shutil.which(t) is None for t in TOOLS), reason="clangd or C++ toolchain not installed"),
]


@pytest.fixture(scope="module")
def nav(tmp_path_factory):
    root = tmp_path_factory.mktemp("nav")
    repo = root / "repo"
    shutil.copytree(FIXTURE, repo)
    with SymbolNavigator(BuildConfig(repo=repo, work_dir=root / "work", compiler="g++")) as navigator:
        yield navigator


def one(nav, name):
    matches = nav.find(name)
    assert len({m.name for m in matches}) == 1, matches
    return matches[0]


def files(locations):
    return sorted({loc.file for loc in locations})


def test_outline_lists_members_with_qualified_names(nav):
    outline = {s.name: s for s in nav.outline("include/inventory/warehouse.h")}
    assert outline["inventory::Warehouse"].kind == "class"
    assert outline["inventory::Warehouse::release"].kind == "method"
    assert outline["inventory::Warehouse::items_"].kind == "field"
    assert outline["inventory::Warehouse"].end_line > outline["inventory::Warehouse"].location.line


def test_find_accepts_partially_qualified_names(nav):
    assert one(nav, "Warehouse::add_item").name == "inventory::Warehouse::add_item"
    assert one(nav, "inventory::create_item").kind == "function"
    assert nav.find("no_such_symbol_anywhere") == []


def test_find_reports_ambiguity(nav):
    names = {m.name for m in nav.find("total_value")}
    assert names == {"inventory::Item::total_value", "inventory::PerishableItem::total_value",
                     "inventory::Warehouse::total_value"}


def test_references_cross_files_and_see_through_macros(nav):
    refs = nav.references(one(nav, "create_item").location)
    assert files(refs) == ["include/inventory/item.h", "src/item.cpp", "tests/test_main.cpp"]
    # One use sits inside the CHECK(...) macro; grep-by-AST tools often miss these.
    assert any("CHECK(warehouse.add_item(create_item(" in r.text for r in refs)
    assert len(refs) == 4


def test_references_point_at_the_identifier(nav):
    for ref in nav.references(one(nav, "Warehouse::release").location):
        line = (nav.repo / ref.file).read_text().splitlines()[ref.line - 1]
        assert line[ref.col - 1:].startswith("release"), (ref, line)


def test_references_distinguish_same_named_members(nav):
    """Text search cannot tell Item::total_value from Warehouse::total_value."""
    refs = nav.references(one(nav, "Warehouse::total_value").location)
    assert "src/item.cpp" not in files(refs)
    assert "src/warehouse.cpp" in files(refs)


def test_definition_jumps_from_use_to_definition(nav):
    use = next(r for r in nav.references(one(nav, "Warehouse::find").location) if r.file == "tests/test_main.cpp")
    targets = nav.definition(use)
    assert [(t.file, "Warehouse::find" in t.text) for t in targets] == [("src/warehouse.cpp", True)]


def test_signature_gives_types(nav):
    signature = nav.signature(one(nav, "Warehouse::add_item").location)
    assert "Item *" in signature and "add_item" in signature


def test_callers(nav):
    callers = nav.callers(one(nav, "Warehouse::release").location)
    assert {(c.location.file, c.name.split("::")[-1]) for c in callers} == {
        ("src/warehouse.cpp", "remove"), ("tests/test_main.cpp", "test_warehouse")}


def test_dead_function_has_no_uses(nav):
    """The signal the Phase 5 dead-code pruner is built on."""
    symbol = one(nav, "legacy_flat_tax")
    assert len(nav.references(symbol.location)) == 2  # declaration + definition only
    assert nav.callers(symbol.location) == []


def test_live_diagnostics_match_the_compiler(nav):
    diags = nav.diagnostics("src/warehouse.cpp")
    assert any("sign" in d.code or "sign" in d.message for d in diags), diags
    assert all(d.location.file == "src/warehouse.cpp" for d in diags)


def test_edit_on_disk_is_picked_up_after_invalidate(nav):
    path = nav.repo / "src/util.cpp"
    original = path.read_text()
    try:
        path.write_text(original.replace("int total = 0;", "int total = undeclared_name;", 1))
        nav.invalidate("src/util.cpp")
        errors = [d for d in nav.diagnostics("src/util.cpp") if d.severity == "error"]
        assert errors and "undeclared_name" in errors[0].message
    finally:
        path.write_text(original)
        nav.invalidate("src/util.cpp")
    assert [d for d in nav.diagnostics("src/util.cpp") if d.severity == "error"] == []


def test_clangd_crash_is_survived(nav):
    """Kill clangd mid-session; the next query restarts it and still answers."""
    before = nav.restarts
    nav._client.kill()
    outline = {s.name for s in nav.outline("include/inventory/warehouse.h")}
    assert "inventory::Warehouse::release" in outline
    assert nav.restarts == before + 1
    assert nav.diagnostics("src/util.cpp") is not None
