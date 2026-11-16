"""lsp_symbol_navigator: semantic lookups over a C++ repo, via clangd.

Everything here is repo-relative and 1-based, so results line up with compiler
diagnostics and can be fed straight to an editor or an LLM.

The key product is `references()`: the definition plus every use of a symbol,
across files. That set is the unit Phase 2 turns into one atomic change-set.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import build as buildmod
from .build import BuildConfig
from .lsp import ClangdClient, LspError
from .lsp.positions import from_lsp_character, to_lsp_character, uri_to_path

SYMBOL_KINDS = {
    1: "file", 2: "module", 3: "namespace", 4: "package", 5: "class", 6: "method", 7: "property",
    8: "field", 9: "constructor", 10: "enum", 11: "interface", 12: "function", 13: "variable",
    14: "constant", 15: "string", 16: "number", 17: "boolean", 18: "array", 19: "object", 20: "key",
    21: "null", 22: "enum-member", 23: "struct", 24: "event", 25: "operator", 26: "type-parameter",
}


@dataclass(frozen=True)
class Location:
    file: str  # repo-relative when inside the repo, else absolute
    line: int  # 1-based
    col: int  # 1-based, in characters
    text: str = ""  # the source line, stripped

    def __str__(self) -> str:
        return f"{self.file}:{self.line}:{self.col}"


@dataclass(frozen=True)
class Symbol:
    name: str  # qualified, e.g. inventory::Warehouse::add_item
    kind: str
    location: Location  # where the name is written
    end_line: int = 0  # last line of the symbol's body, when known (outline only)


@dataclass(frozen=True)
class Diagnostic:
    location: Location
    severity: str
    message: str
    code: str


class SymbolNavigator:
    def __init__(self, cfg: BuildConfig, clangd: str = "clangd", index_timeout: float = 300.0):
        self.cfg = cfg
        self.repo = cfg.repo
        self._build_dir = buildmod.configure(cfg, "plain")
        self._client = ClangdClient(self.repo, self._build_dir, clangd=clangd, jobs=cfg.jobs)
        self._index_timeout = index_timeout
        self._lines: dict[Path, list[str]] = {}

    def start(self) -> "SymbolNavigator":
        self._client.start()
        sources = buildmod.source_files(self.cfg, self._build_dir)
        if not sources:
            raise LspError("compile_commands.json lists no source files inside the repo")
        self._client.open(sources[0])  # opening any file kicks off background indexing
        self._client.wait_for_index(self._index_timeout)
        return self

    def stop(self) -> None:
        self._client.stop()

    def __enter__(self) -> "SymbolNavigator":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # ---- position plumbing ----------------------------------------------

    def _abs(self, file: str | Path) -> Path:
        path = Path(file)
        return path if path.is_absolute() else self.repo / path

    def _rel(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.repo).as_posix()
        except ValueError:
            return path.as_posix()

    def _line_text(self, path: Path, line: int) -> str:
        if path not in self._lines:
            try:
                self._lines[path] = path.read_text(errors="replace").splitlines()
            except OSError:
                self._lines[path] = []
        lines = self._lines[path]
        return lines[line - 1] if 0 < line <= len(lines) else ""

    def invalidate(self, file: str | Path) -> None:
        """Forget cached text for a file and tell clangd it changed on disk."""
        path = self._abs(file)
        self._lines.pop(path, None)
        self._client.refresh(path)

    def _to_lsp(self, loc: Location) -> tuple[Path, int, int]:
        path = self._abs(loc.file)
        text = self._line_text(path, loc.line)
        return path, loc.line - 1, to_lsp_character(text, loc.col, self._client.encoding)

    def _from_lsp(self, uri: str, position: dict) -> Location:
        path = uri_to_path(uri)
        line = position["line"] + 1
        text = self._line_text(path, line)
        col = from_lsp_character(text, position["character"], self._client.encoding)
        return Location(self._rel(path), line, col, text.strip())

    def _locations(self, raw: list[dict]) -> list[Location]:
        out = []
        for item in raw:
            uri = item.get("uri") or item.get("targetUri")
            rng = item.get("range") or item.get("targetSelectionRange")
            out.append(self._from_lsp(uri, rng["start"]))
        return sorted(set(out), key=lambda l: (l.file, l.line, l.col))

    def at(self, file: str | Path, line: int, col: int) -> Location:
        path = self._abs(file)
        return Location(self._rel(path), line, col, self._line_text(path, line).strip())

    # ---- queries ---------------------------------------------------------

    def outline(self, file: str | Path) -> list[Symbol]:
        """Every symbol declared in a file, flattened, with qualified names."""
        path = self._abs(file)
        uri = path.resolve().as_uri()
        out: list[Symbol] = []

        def walk(nodes: list[dict], prefix: str) -> None:
            for node in nodes:
                name = f"{prefix}::{node['name']}" if prefix else node["name"]
                if "selectionRange" in node:  # hierarchical DocumentSymbol
                    start, end = node["selectionRange"]["start"], node["range"]["end"]
                    out.append(Symbol(name, SYMBOL_KINDS.get(node["kind"], "?"), self._from_lsp(uri, start), end["line"] + 1))
                    walk(node.get("children", []), name)
                else:  # flat SymbolInformation
                    container = node.get("containerName", "")
                    full = f"{container}::{node['name']}" if container else node["name"]
                    rng = node["location"]["range"]
                    out.append(Symbol(full, SYMBOL_KINDS.get(node["kind"], "?"), self._from_lsp(uri, rng["start"]), rng["end"]["line"] + 1))

        walk(self._client.document_symbols(path), "")
        return out

    def find(self, name: str, kind: str | None = None) -> list[Symbol]:
        """Look a symbol up by (optionally qualified) name anywhere in the repo.

        `Warehouse::add_item` matches `inventory::Warehouse::add_item`.
        """
        wanted = name.lstrip(":")
        short = wanted.rsplit("::", 1)[-1]
        found: dict[tuple, Symbol] = {}
        for raw in self._client.workspace_symbols(short):
            if raw["name"] != short:
                continue
            container = raw.get("containerName", "").rstrip(":")
            qualified = f"{container}::{short}" if container else short
            if not (qualified == wanted or qualified.endswith("::" + wanted)):
                continue
            symbol_kind = SYMBOL_KINDS.get(raw["kind"], "?")
            if kind is not None and symbol_kind != kind:
                continue
            loc = self._from_lsp(raw["location"]["uri"], raw["location"]["range"]["start"])
            if Path(loc.file).is_absolute():
                continue  # system or third-party header, not ours to edit
            found[(qualified, symbol_kind, loc)] = Symbol(qualified, symbol_kind, loc)
        return sorted(found.values(), key=lambda s: (s.name, s.location.file, s.location.line))

    def definition(self, loc: Location) -> list[Location]:
        return self._locations(self._client.definition(*self._to_lsp(loc)))

    def references(self, loc: Location, include_declaration: bool = True) -> list[Location]:
        """The declaration, definition and every use of the symbol at `loc`."""
        return self._locations(self._client.references(*self._to_lsp(loc), include_declaration))

    def signature(self, loc: Location) -> str:
        """Type information for the symbol at `loc` (clangd's hover text)."""
        return self._client.hover(*self._to_lsp(loc)).strip()

    def callers(self, loc: Location) -> list[Symbol]:
        """Functions that call the function at `loc`."""
        out = []
        for call in self._client.incoming_calls(*self._to_lsp(loc)):
            item = call["from"]
            where = self._from_lsp(item["uri"], item["selectionRange"]["start"])
            out.append(Symbol(item["name"], SYMBOL_KINDS.get(item["kind"], "?"), where))
        return sorted(set(out), key=lambda s: (s.location.file, s.location.line))

    def diagnostics(self, file: str | Path) -> list[Diagnostic]:
        """clangd's live diagnostics for one file: much faster than a compile."""
        path = self._abs(file)
        uri = path.resolve().as_uri()
        severities = {1: "error", 2: "warning", 3: "info", 4: "hint"}
        out = []
        for raw in self._client.diagnostics(path):
            out.append(
                Diagnostic(
                    self._from_lsp(uri, raw["range"]["start"]),
                    severities.get(raw.get("severity", 1), "error"),
                    raw.get("message", "").splitlines()[0] if raw.get("message") else "",
                    str(raw.get("code", "")),
                )
            )
        return out
