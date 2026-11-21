"""Read and write source files without disturbing what the tool did not mean to change.

Two properties of real repositories that a fixture never shows:

* Line endings. Many older projects use CRLF. Python's text mode silently turns
  CRLF into LF on read and writes LF back, which rewrites every line of the file
  and buries a three-line edit under a five-thousand-line diff.
* Encodings. Legacy sources are not always UTF-8. Decoding with replacement
  characters and writing the result back corrupts every byte that was not valid.

So: files are read as bytes, decoded so that invalid bytes survive a round trip,
handed to the caller with LF line endings, and written back in the style they
had.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable


def load(path: Path) -> tuple[str, str]:
    """Return (text with LF line endings, newline style: 'lf', 'crlf' or 'mixed')."""
    raw = Path(path).read_bytes().decode("utf-8", errors="surrogateescape")
    crlf, lf = raw.count("\r\n"), raw.count("\n")
    if crlf == 0:
        return raw, "lf"
    if crlf == lf:
        return raw.replace("\r\n", "\n"), "crlf"
    return raw, "mixed"  # inconsistent: leave every line ending exactly as it is


def save(path: Path, text: str, style: str) -> None:
    if style == "crlf":
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    Path(path).write_bytes(text.encode("utf-8", errors="surrogateescape"))


def rewrite(path: Path, transform: Callable[[str], str]) -> bool:
    """Apply `transform` to a file's text, preserving its line endings and bytes. True if it changed."""
    text, style = load(path)
    new = transform(text)
    if new == text:
        return False
    save(path, new, style)
    return True
