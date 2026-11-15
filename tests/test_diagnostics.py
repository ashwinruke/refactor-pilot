from pathlib import Path

from refactor_pilot import diagnostics

GCC = """\
[3/8] Building CXX object CMakeFiles/inventory.dir/src/warehouse.cpp.o
/repo/src/warehouse.cpp:58:23: warning: comparison of integer expressions of different signedness [-Wsign-compare]
   58 |     for (int i = 0; i < items_.size(); ++i) {
/repo/include/inventory/util.h:12:1: warning: unused thing [-Wunused]
/repo/include/inventory/util.h:12:1: warning: unused thing [-Wunused]
/repo/src/item.cpp:9:5: error: use of undeclared identifier 'x'
"""


def test_parse_extracts_fields_and_relativizes():
    diags = diagnostics.parse(GCC, Path("/repo"))
    first = diags[0]
    assert (first.file, first.line, first.col) == ("src/warehouse.cpp", 58, 23)
    assert first.severity == "warning"
    assert first.flag == "-Wsign-compare"
    assert first.message == "comparison of integer expressions of different signedness"


def test_header_warning_repeated_per_translation_unit_counts_once():
    diags = diagnostics.parse(GCC, Path("/repo"))
    assert sum(d.file == "include/inventory/util.h" for d in diags) == 1


def test_error_without_flag():
    err = [d for d in diagnostics.parse(GCC, Path("/repo")) if d.severity == "error"]
    assert len(err) == 1 and err[0].flag == ""


def test_key_ignores_line_numbers():
    a = diagnostics.parse("/repo/a.cpp:1:1: warning: m [-Wx]", Path("/repo"))[0]
    b = diagnostics.parse("/repo/a.cpp:99:7: warning: m [-Wx]", Path("/repo"))[0]
    assert a.key == b.key


def test_clang_tidy_flag():
    out = "/repo/a.cpp:3:9: warning: use nullptr [modernize-use-nullptr]"
    assert diagnostics.count_by_flag(diagnostics.parse(out, Path("/repo"))) == {"modernize-use-nullptr": 1}
