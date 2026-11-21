"""Edits must not disturb line endings or bytes they were not asked to change."""

from pathlib import Path

from refactor_pilot import edits, preproc, textio


def test_lf_file_roundtrip(tmp_path):
    path = tmp_path / "a.cpp"
    path.write_bytes(b"int a;\nint b;\n")
    assert textio.load(path) == ("int a;\nint b;\n", "lf")
    assert textio.rewrite(path, lambda t: t.replace("int b", "long b")) is True
    assert path.read_bytes() == b"int a;\nlong b;\n"


def test_crlf_file_keeps_crlf_on_every_line(tmp_path):
    path = tmp_path / "a.cpp"
    path.write_bytes(b"int a;\r\nint b;\r\nint c;\r\n")
    text, style = textio.load(path)
    assert (text, style) == ("int a;\nint b;\nint c;\n", "crlf")
    textio.rewrite(path, lambda t: t.replace("int b;\n", "long b;\nlong b2;\n"))
    assert path.read_bytes() == b"int a;\r\nlong b;\r\nlong b2;\r\nint c;\r\n"


def test_unchanged_text_is_not_rewritten(tmp_path):
    path = tmp_path / "a.cpp"
    path.write_bytes(b"x\r\n")
    before = path.stat().st_mtime_ns
    assert textio.rewrite(path, lambda t: t) is False
    assert path.stat().st_mtime_ns == before


def test_mixed_line_endings_are_left_exactly_as_found(tmp_path):
    path = tmp_path / "a.cpp"
    path.write_bytes(b"a\r\nb\nc\r\n")
    text, style = textio.load(path)
    assert style == "mixed" and text == "a\r\nb\nc\r\n"
    textio.save(path, text.replace("b", "B"), style)
    assert path.read_bytes() == b"a\r\nB\nc\r\n"


def test_bytes_that_are_not_utf8_survive(tmp_path):
    path = tmp_path / "latin1.cpp"
    original = "// café ©\nint a = 1;\n".encode("latin-1")
    path.write_bytes(original)
    textio.rewrite(path, lambda t: t.replace("a = 1", "a = 2"))
    assert path.read_bytes() == original.replace(b"a = 1", b"a = 2")


def test_search_replace_edit_on_a_crlf_file_changes_only_its_lines(tmp_path):
    path = tmp_path / "w.cpp"
    path.write_bytes(b"void f() {\r\n    m.lock();\r\n    x();\r\n    m.unlock();\r\n}\r\n")
    answer = ("<<<<<<< SEARCH w.cpp\n    m.lock();\n    x();\n    m.unlock();\n=======\n"
              "    std::lock_guard<std::mutex> g(m);\n    x();\n>>>>>>> REPLACE\n")
    assert edits.apply(edits.parse(answer), tmp_path, {"w.cpp"}) == ["w.cpp"]
    assert path.read_bytes() == b"void f() {\r\n    std::lock_guard<std::mutex> g(m);\r\n    x();\r\n}\r\n"


def test_dead_block_removal_on_a_crlf_file(tmp_path):
    path = tmp_path / "p.cpp"
    path.write_bytes(b"a();\r\n#if 0\r\ndead();\r\n#endif\r\nb();\r\n")
    textio.rewrite(path, lambda t: preproc.simplify_conditionals(
        t, lambda g: preproc.is_literal(g.expr) if g.kind == "if" else None)[0])
    assert path.read_bytes() == b"a();\r\nb();\r\n"
