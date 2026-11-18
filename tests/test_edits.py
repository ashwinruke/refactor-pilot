import pytest

from refactor_pilot import edits
from refactor_pilot.edits import EditError


def block(path, search, replace):
    return f"<<<<<<< SEARCH {path}\n{search}=======\n{replace}>>>>>>> REPLACE\n"


@pytest.fixture()
def repo(tmp_path):
    (tmp_path / "a.cpp").write_text("int a = 1;\nint b = 2;\nint a2 = 1;\n")
    (tmp_path / "b.cpp").write_text("void f() {}\n")
    return tmp_path


def test_parse_ignores_surrounding_prose():
    text = "Here you go:\n\n" + block("a.cpp", "int b = 2;\n", "int b = 3;\n") + "\nDone."
    assert edits.parse(text) == [edits.Edit("a.cpp", "int b = 2;\n", "int b = 3;\n")]


def test_parse_without_blocks_explains_the_format():
    with pytest.raises(EditError, match="No edit blocks found"):
        edits.parse("I would change the mutex to a lock_guard.")


def test_apply_changes_only_the_matched_place(repo):
    changed = edits.apply(edits.parse(block("a.cpp", "int b = 2;\n", "int b = 3;\n")), repo, {"a.cpp"})
    assert changed == ["a.cpp"]
    assert (repo / "a.cpp").read_text() == "int a = 1;\nint b = 3;\nint a2 = 1;\n"


def test_search_not_found_tells_the_model_what_to_fix(repo):
    with pytest.raises(EditError, match="was not found in a.cpp"):
        edits.apply(edits.parse(block("a.cpp", "int c = 9;\n", "x\n")), repo, {"a.cpp"})


def test_ambiguous_search_is_refused(repo):
    with pytest.raises(EditError, match="occurs 2 times"):
        edits.apply(edits.parse(block("a.cpp", " = 1;\n", " = 5;\n")), repo, {"a.cpp"})


def test_file_outside_the_allowed_set_is_refused(repo):
    with pytest.raises(EditError, match="not one of the files you may change"):
        edits.apply(edits.parse(block("b.cpp", "void f() {}\n", "void g() {}\n")), repo, {"a.cpp"})
    with pytest.raises(EditError, match="not one of the files"):
        edits.apply(edits.parse(block("../outside.cpp", "x\n", "y\n")), repo, {"a.cpp"})


def test_nothing_is_written_unless_every_block_applies(repo):
    text = block("a.cpp", "int b = 2;\n", "int b = 3;\n") + block("b.cpp", "missing\n", "x\n")
    with pytest.raises(EditError):
        edits.apply(edits.parse(text), repo, {"a.cpp", "b.cpp"})
    assert "int b = 2;" in (repo / "a.cpp").read_text()


def test_later_block_sees_the_result_of_an_earlier_one(repo):
    text = block("a.cpp", "int b = 2;\n", "int b = 3;\n") + block("a.cpp", "int b = 3;\n", "int b = 4;\n")
    edits.apply(edits.parse(text), repo, {"a.cpp"})
    assert "int b = 4;" in (repo / "a.cpp").read_text()


def test_trailing_whitespace_differences_are_tolerated(repo):
    (repo / "a.cpp").write_text("int a = 1;   \nint b = 2;\n")
    edits.apply(edits.parse(block("a.cpp", "int a = 1;\nint b = 2;\n", "int z = 0;\n")), repo, {"a.cpp"})
    assert (repo / "a.cpp").read_text() == "int z = 0;\n"


def test_empty_replace_deletes_lines(repo):
    edits.apply(edits.parse(block("a.cpp", "int b = 2;\n", "")), repo, {"a.cpp"})
    assert (repo / "a.cpp").read_text() == "int a = 1;\nint a2 = 1;\n"
