"""A small clangd client: lifecycle, document sync, index readiness, queries.

Methods here speak raw LSP (0-based positions, URIs, dict results). The
friendlier, repo-relative API lives in refactor_pilot.navigator.
"""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from typing import Any

from ..toolchain import require
from .jsonrpc import Connection, LspError
from .positions import path_to_uri

INDEX_TOKEN = "backgroundIndexProgress"

__all__ = ["ClangdClient", "LspError"]


class ClangdClient:
    def __init__(
        self,
        repo: Path,
        compile_commands_dir: Path,
        clangd: str = "clangd",
        jobs: int | None = None,
        log_path: Path | None = None,
    ):
        self.repo = Path(repo).resolve()
        self.compile_commands_dir = Path(compile_commands_dir).resolve()
        self.clangd = clangd
        self.jobs = jobs or os.cpu_count() or 2
        self.log_path = log_path or self.compile_commands_dir / "clangd.log"
        self.encoding = "utf-16"
        self._proc: subprocess.Popen | None = None
        self._conn: Connection | None = None
        self._log = None
        self._versions: dict[str, int] = {}  # uri -> document version
        self._cond = threading.Condition()
        self._index_begun = False
        self._index_active = False
        self._diagnostics: dict[str, list[dict]] = {}

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> "ClangdClient":
        require(self.clangd)
        if not (self.compile_commands_dir / "compile_commands.json").exists():
            raise LspError(f"no compile_commands.json in {self.compile_commands_dir}; configure the project first")
        self._log = open(self.log_path, "w")
        self._proc = subprocess.Popen(
            [
                self.clangd,
                f"--compile-commands-dir={self.compile_commands_dir}",
                "--background-index",
                "--header-insertion=never",
                "--log=error",
                f"-j={self.jobs}",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._log,
            cwd=self.repo,
        )
        self._conn = Connection(self._proc.stdout, self._proc.stdin, self._on_notification, self._on_request)
        result = self._conn.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": path_to_uri(self.repo),
                "capabilities": {
                    "general": {"positionEncodings": ["utf-16"]},
                    "window": {"workDoneProgress": True},
                    "textDocument": {
                        "publishDiagnostics": {"relatedInformation": False},
                        "hover": {"contentFormat": ["plaintext"]},
                        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                        "callHierarchy": {"dynamicRegistration": False},
                    },
                },
            },
        )
        self.encoding = (result or {}).get("capabilities", {}).get("positionEncoding", "utf-16")
        self._conn.notify("initialized", {})
        return self

    def stop(self) -> None:
        if self._conn is not None and self._proc is not None and self._proc.poll() is None:
            try:
                self._conn.request("shutdown", timeout=5)
                self._conn.notify("exit")
            except LspError:
                pass
        if self._proc is not None:
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        if self._log is not None:
            self._log.close()
        self._proc = self._conn = self._log = None

    def __enter__(self) -> "ClangdClient":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # ---- messages from the server ---------------------------------------

    def _on_request(self, method: str, params: Any) -> Any:
        # window/workDoneProgress/create, client/registerCapability, ...: accept.
        return None

    def _on_notification(self, method: str, params: Any) -> None:
        if method == "$/progress" and params.get("token") == INDEX_TOKEN:
            kind = params.get("value", {}).get("kind")
            with self._cond:
                if kind == "begin":
                    self._index_begun = self._index_active = True
                elif kind == "end":
                    self._index_active = False
                self._cond.notify_all()
        elif method == "textDocument/publishDiagnostics":
            with self._cond:
                self._diagnostics[params["uri"]] = params.get("diagnostics", [])
                self._cond.notify_all()

    # ---- documents -------------------------------------------------------

    def open(self, path: Path) -> str:
        """Open a file (or re-send its text if it changed on disk). Returns its URI."""
        path = Path(path).resolve()
        uri = path_to_uri(path)
        text = path.read_text(errors="replace")
        if uri not in self._versions:
            self._versions[uri] = 1
            language = "cpp" if path.suffix.lower() != ".c" else "c"
            self._conn.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": uri, "languageId": language, "version": 1, "text": text}},
            )
        return uri

    def refresh(self, path: Path) -> str:
        """Tell clangd a file was edited on disk. Phase 2 calls this after each edit."""
        path = Path(path).resolve()
        uri = path_to_uri(path)
        if uri not in self._versions:
            return self.open(path)
        self._versions[uri] += 1
        with self._cond:
            self._diagnostics.pop(uri, None)
        self._conn.notify(
            "textDocument/didChange",
            {
                "textDocument": {"uri": uri, "version": self._versions[uri]},
                "contentChanges": [{"text": path.read_text(errors="replace")}],
            },
        )
        return uri

    def wait_for_index(self, timeout: float = 300.0, start_grace: float = 5.0) -> bool:
        """Block until clangd's background index is idle.

        Cross-file queries (references, workspace symbols) are incomplete until
        then. Indexing only starts once a file has been opened. Returns False if
        indexing never started within `start_grace` (index already on disk) and
        raises LspError if it is still running after `timeout`.
        """
        with self._cond:
            if not self._cond.wait_for(lambda: self._index_begun, start_grace):
                return False
            while True:
                if not self._cond.wait_for(lambda: not self._index_active, timeout):
                    raise LspError(f"clangd was still indexing after {timeout:.0f}s")
                # Indexing can pause between batches; make sure it stays idle.
                if not self._cond.wait_for(lambda: self._index_active, 0.5):
                    return True

    def diagnostics(self, path: Path, timeout: float = 60.0) -> list[dict]:
        uri = self.open(path)
        with self._cond:
            if not self._cond.wait_for(lambda: uri in self._diagnostics, timeout):
                raise LspError(f"no diagnostics for {path} after {timeout:.0f}s")
            return list(self._diagnostics[uri])

    # ---- queries (raw LSP) ----------------------------------------------

    def _position(self, path: Path, line: int, character: int) -> dict:
        return {"textDocument": {"uri": self.open(path)}, "position": {"line": line, "character": character}}

    def definition(self, path: Path, line: int, character: int) -> list[dict]:
        result = self._conn.request("textDocument/definition", self._position(path, line, character))
        return _as_list(result)

    def references(self, path: Path, line: int, character: int, include_declaration: bool = True) -> list[dict]:
        params = self._position(path, line, character)
        params["context"] = {"includeDeclaration": include_declaration}
        return _as_list(self._conn.request("textDocument/references", params))

    def hover(self, path: Path, line: int, character: int) -> str:
        result = self._conn.request("textDocument/hover", self._position(path, line, character))
        contents = (result or {}).get("contents", "")
        return contents.get("value", "") if isinstance(contents, dict) else str(contents)

    def document_symbols(self, path: Path) -> list[dict]:
        return _as_list(self._conn.request("textDocument/documentSymbol", {"textDocument": {"uri": self.open(path)}}))

    def workspace_symbols(self, query: str) -> list[dict]:
        return _as_list(self._conn.request("workspace/symbol", {"query": query}))

    def incoming_calls(self, path: Path, line: int, character: int) -> list[dict]:
        items = _as_list(self._conn.request("textDocument/prepareCallHierarchy", self._position(path, line, character)))
        calls: list[dict] = []
        for item in items:
            calls += _as_list(self._conn.request("callHierarchy/incomingCalls", {"item": item}))
        return calls


def _as_list(result: Any) -> list:
    if result is None:
        return []
    return result if isinstance(result, list) else [result]
