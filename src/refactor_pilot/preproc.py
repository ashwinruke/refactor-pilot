"""Text-level C/C++ helpers: preprocessor conditionals, brace matching, line removal.

These work on source text, not an AST. Each is deliberately narrow: when a
construct is not in the simple form it understands, it leaves the code alone.
Whatever they do produce still has to pass the gates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

_MASK = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'', re.DOTALL)
_DIRECTIVE = re.compile(r"^\s*#\s*(if|ifdef|ifndef|elif|elifdef|elifndef|else|endif)\b(.*)$")


def mask(source: str) -> str:
    """Blank out comments and string literals, keeping every line and column in place."""
    return _MASK.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), source)


@dataclass
class Conditional:
    kind: str  # if | ifdef | ifndef
    expr: str
    start: int  # line index of the opening directive
    else_line: int | None
    end: int  # line index of #endif
    has_elif: bool


def conditionals(lines: list[str]) -> list[Conditional]:
    """Every #if/#ifdef/#ifndef ... #endif group, innermost groups included."""
    masked = mask("\n".join(lines)).split("\n")
    found: list[Conditional] = []
    stack: list[Conditional] = []
    for index, line in enumerate(masked):
        m = _DIRECTIVE.match(line)
        if not m:
            continue
        word, rest = m.group(1), m.group(2).strip()
        if word in ("if", "ifdef", "ifndef"):
            stack.append(Conditional(word, rest, index, None, -1, False))
        elif not stack:
            continue  # unbalanced; leave the file alone
        elif word.startswith("elif"):
            stack[-1].has_elif = True
        elif word == "else":
            stack[-1].else_line = index
        elif word == "endif":
            group = stack.pop()
            group.end = index
            found.append(group)
    return sorted(found, key=lambda g: g.start)


Decide = Callable[[Conditional], "bool | None"]


def simplify_conditionals(source: str, decide: Decide) -> tuple[str, int]:
    """Resolve every conditional `decide` has an answer for.

    decide(group) returns True (the #if branch is live), False (the #else branch
    is live, or nothing if there is none) or None (leave it). Groups with #elif
    are never touched. Returns (new source, number of groups resolved).
    """
    lines = source.split("\n")
    resolved = 0
    while True:
        for group in conditionals(lines):
            if group.has_elif or group.end < 0:
                continue
            verdict = decide(group)
            if verdict is None:
                continue
            if verdict:
                keep = lines[group.start + 1:group.else_line if group.else_line is not None else group.end]
            else:
                keep = lines[group.else_line + 1:group.end] if group.else_line is not None else []
            lines[group.start:group.end + 1] = keep
            resolved += 1
            break  # line numbers moved; rescan
        else:
            break
    return collapse_blank_lines("\n".join(lines)), resolved


def is_literal(expr: str) -> bool | None:
    """True/False for `#if 1`, `#if 0`, `#if (0)`, `#if false`; None otherwise."""
    value = expr.strip()
    while value.startswith("(") and value.endswith(")"):
        value = value[1:-1].strip()
    return {"0": False, "false": False, "1": True, "true": True}.get(value)


_DEFINED = re.compile(r"^(!?)\s*defined\s*(?:\(\s*(\w+)\s*\)|\s+(\w+))$")


def flag_of(group: Conditional) -> tuple[str, bool] | None:
    """(macro name, True if the #if branch is taken when the macro IS defined).

    Understands #ifdef X, #ifndef X, #if defined(X), #if !defined(X) only.
    """
    if group.kind == "ifdef" and re.fullmatch(r"\w+", group.expr):
        return group.expr, True
    if group.kind == "ifndef" and re.fullmatch(r"\w+", group.expr):
        return group.expr, False
    if group.kind == "if":
        m = _DEFINED.match(group.expr)
        if m:
            return m.group(2) or m.group(3), m.group(1) != "!"
    return None


def collapse_blank_lines(source: str) -> str:
    """Never leave more than one blank line where code was removed."""
    return re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", source)


def remove_lines(source: str, first: int, last: int, with_comment_above: bool = True) -> str:
    """Remove 1-based lines first..last, plus the comment block directly above."""
    lines = source.split("\n")
    start = first - 1
    if with_comment_above:
        while start > 0 and lines[start - 1].lstrip().startswith("//"):
            start -= 1
    del lines[start:last]
    return collapse_blank_lines("\n".join(lines))


def matching_brace(masked: str, open_index: int) -> int:
    """Index of the `}` matching the `{` at open_index in masked text, or -1."""
    depth = 0
    for index in range(open_index, len(masked)):
        char = masked[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _dedent_block(body: str, indent: str) -> str:
    """Re-indent the inside of a `{ ... }` block to sit at `indent`."""
    lines = body.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    widths = [len(l) - len(l.lstrip()) for l in lines if l.strip()]
    cut = min(widths) if widths else 0
    return "\n".join((indent + l[cut:]) if l.strip() else "" for l in lines)


def fold_constant_ifs(source: str, name: str, value: bool) -> tuple[str, int]:
    """Fold `if (name) { A } else { B }` (and `if (!name)`) given the constant's value.

    Only the plain forms: the condition is exactly the name or its negation, and
    both branches are braced. `else if` chains are left alone.
    """
    pattern = re.compile(r"^([ \t]*)if\s*\(\s*(!?)\s*" + re.escape(name) + r"\s*\)\s*\{", re.MULTILINE)
    folded = 0
    while True:
        masked = mask(source)
        m = pattern.search(masked)
        if not m:
            break
        indent, negated = m.group(1), m.group(2) == "!"
        then_open = m.end() - 1
        then_close = matching_brace(masked, then_open)
        if then_close < 0:
            break
        end = then_close + 1
        else_body = None
        tail = re.match(r"\s*else\s*(\{|if\b)", masked[end:])
        if tail:
            if tail.group(1) != "{":
                break  # else-if chain: not a plain form
            else_open = end + tail.end() - 1
            else_close = matching_brace(masked, else_open)
            if else_close < 0:
                break
            else_body, end = source[else_open + 1:else_close], else_close + 1
        taken = value != negated
        body = source[then_open + 1:then_close] if taken else else_body
        replacement = _dedent_block(body, indent) if body is not None and body.strip() else ""
        # Swallow the rest of the line after the closing brace if it is empty.
        line_end = source.find("\n", end)
        line_end = len(source) if line_end < 0 else line_end
        if not source[end:line_end].strip():
            end = line_end
        source = source[:m.start()] + replacement + source[end:]
        folded += 1
    return collapse_blank_lines(source), folded
