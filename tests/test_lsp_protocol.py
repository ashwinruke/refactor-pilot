import io
import threading

from refactor_pilot.lsp import jsonrpc
from refactor_pilot.lsp.positions import from_lsp_character, path_to_uri, to_lsp_character, uri_to_path


def test_encode_then_read_roundtrip():
    message = {"jsonrpc": "2.0", "id": 1, "result": {"text": "naïve ☃"}}
    stream = io.BytesIO(jsonrpc.encode(message) + jsonrpc.encode({"method": "x"}))
    assert jsonrpc.read_message(stream) == message
    assert jsonrpc.read_message(stream) == {"method": "x"}
    assert jsonrpc.read_message(stream) is None


def test_content_length_counts_bytes_not_characters():
    assert jsonrpc.encode({"t": "☃"}).startswith(b"Content-Length: 11\r\n\r\n")


def test_truncated_body_is_end_of_stream():
    assert jsonrpc.read_message(io.BytesIO(b"Content-Length: 50\r\n\r\n{}")) is None


class FakeServer:
    """Answers each request with its params, over in-memory pipes."""

    def __init__(self):
        import os

        c2s_r, c2s_w = os.pipe()
        s2c_r, s2c_w = os.pipe()
        self.client_reader, self.client_writer = os.fdopen(s2c_r, "rb"), os.fdopen(c2s_w, "wb")
        self._reader, self._writer = os.fdopen(c2s_r, "rb"), os.fdopen(s2c_w, "wb")
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while (msg := jsonrpc.read_message(self._reader)) is not None:
            if msg.get("method") == "die":
                break
            if "id" in msg:
                if msg["method"] == "fail":
                    reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -1, "message": "nope"}}
                else:
                    self._writer.write(jsonrpc.encode({"jsonrpc": "2.0", "method": "note", "params": 7}))
                    reply = {"jsonrpc": "2.0", "id": msg["id"], "result": msg["params"]}
                self._writer.write(jsonrpc.encode(reply))
                self._writer.flush()
        self._writer.close()


def test_connection_request_notification_error_and_exit():
    server, notes = FakeServer(), []
    conn = jsonrpc.Connection(server.client_reader, server.client_writer, lambda m, p: notes.append((m, p)))
    assert conn.request("echo", {"a": 1}, timeout=5) == {"a": 1}
    assert notes == [("note", 7)]
    try:
        conn.request("fail", None, timeout=5)
        raise AssertionError("expected LspError")
    except jsonrpc.LspError as exc:
        assert "nope" in str(exc)
    conn.notify("die")
    try:
        conn.request("echo", 1, timeout=5)
        raise AssertionError("expected LspError")
    except jsonrpc.LspError:
        pass


def test_utf16_columns_with_wide_characters():
    line = 'x = "😀é"; y'
    col_of_y = line.index("y") + 1
    # The emoji is 2 UTF-16 units, so the LSP offset is one more than the index.
    assert to_lsp_character(line, col_of_y, "utf-16") == line.index("y") + 1
    assert from_lsp_character(line, line.index("y") + 1, "utf-16") == col_of_y
    assert to_lsp_character(line, col_of_y, "utf-8") == len(line[: col_of_y - 1].encode())
    assert from_lsp_character(line, to_lsp_character(line, col_of_y, "utf-8"), "utf-8") == col_of_y


def test_ascii_columns_are_offset_by_one():
    assert to_lsp_character("int x;", 5) == 4
    assert from_lsp_character("int x;", 4) == 5
    assert from_lsp_character("ab", 99) == 3


def test_uri_roundtrip(tmp_path):
    path = tmp_path / "dir with space" / "a.cpp"
    path.parent.mkdir()
    path.write_text("")
    assert uri_to_path(path_to_uri(path)) == path.resolve()


def test_diagnostics_for_an_older_version_of_a_file_are_ignored(tmp_path):
    """A late report about text that has since been replaced must not be taken as current."""
    from refactor_pilot.lsp.client import ClangdClient

    client = ClangdClient(tmp_path, tmp_path)
    uri = "file:///x/a.cpp"
    client._versions[uri] = 2

    def publish(version, line):
        params = {"uri": uri, "diagnostics": [{"range": {"start": {"line": line, "character": 0}}, "message": "m"}]}
        if version is not None:
            params["version"] = version
        client._on_notification("textDocument/publishDiagnostics", params)

    publish(1, 21)  # stale: describes version 1
    assert uri not in client._diagnostics
    publish(2, 17)  # current
    assert client._diagnostics[uri][0]["range"]["start"]["line"] == 17
    publish(1, 99)  # stale again, arriving late
    assert client._diagnostics[uri][0]["range"]["start"]["line"] == 17
    publish(None, 5)  # a server that sends no version is believed, as before
    assert client._diagnostics[uri][0]["range"]["start"]["line"] == 5
