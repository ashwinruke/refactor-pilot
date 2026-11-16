"""Convert between editor-style positions and LSP positions.

refactor-pilot uses 1-based line and 1-based column counted in characters, the
same convention compilers print. LSP uses 0-based lines and a 0-based column
counted in UTF-16 code units (or UTF-8 bytes / code points if negotiated).
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse


def _units(char: str, encoding: str) -> int:
    if encoding == "utf-8":
        return len(char.encode("utf-8"))
    if encoding == "utf-16":
        return 2 if ord(char) > 0xFFFF else 1
    return 1  # utf-32: one unit per code point


def to_lsp_character(line_text: str, col: int, encoding: str = "utf-16") -> int:
    """1-based character column -> 0-based LSP character offset."""
    return sum(_units(ch, encoding) for ch in line_text[: max(col - 1, 0)])


def from_lsp_character(line_text: str, character: int, encoding: str = "utf-16") -> int:
    """0-based LSP character offset -> 1-based character column."""
    units = 0
    for index, ch in enumerate(line_text):
        if units >= character:
            return index + 1
        units += _units(ch, encoding)
    return len(line_text) + 1


def path_to_uri(path: Path) -> str:
    return Path(path).resolve().as_uri()


def uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    path = unquote(parsed.path)
    if len(path) >= 3 and path[0] == "/" and path[2] == ":":  # /C:/x on Windows
        path = path[1:]
    return Path(path)
