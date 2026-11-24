"""A Model Context Protocol server, so any MCP-capable agent can use refactor-pilot's tools.

It speaks MCP over stdio: one JSON-RPC 2.0 message per line. It implements the
handshake-based protocol (revisions 2024-11-05 through 2025-11-25): `initialize`,
`notifications/initialized`, `ping`, `tools/list`, `tools/call`. Requests are
also answered without a prior `initialize`, and unknown methods get a proper
"method not found" error, which is what clients that prefer the newer stateless
revision use to fall back.

The server is bound to one repository, given when it starts. Two kinds of tools:

  read-only navigation   find_symbol, references, outline, diagnostics
  verified change        check_patch, run_recipes, baseline

`check_patch` is the important one. An agent proposes a unified diff; the server
applies it in a private worktree, builds, runs the tests (with sanitizers), and
answers with either the branch the change now lives on or the exact reason it
was rejected. The agent's own working tree is never modified.

Nothing but protocol messages is ever written to stdout; logs go to stderr.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, TextIO

from . import __version__, baseline as baselinemod
from .build import BuildConfig, BuildFailed
from .checkpoint import CheckpointError
from .lsp import LspError
from .navigator import SymbolNavigator
from .pipeline import LLM_RECIPES, PipelineOptions, parse_recipes, run_pipeline
from .session import ChangeSet, Session
from .toolchain import ToolMissing

SUPPORTED_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]


class ToolError(Exception):
    """A problem to report to the model as a failed tool call, not a protocol error."""


def _schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


class Server:
    def __init__(self, repo: Path, compiler: str = "clang++", cmake_args: list[str] | None = None,
                 work_dir: Path | None = None, sanitizers: bool = True, log: TextIO = sys.stderr):
        self.cfg = BuildConfig(repo=repo, work_dir=work_dir, compiler=compiler, cmake_args=list(cmake_args or []))
        self.sanitizers = sanitizers
        self._log_stream = log
        self._nav: SymbolNavigator | None = None
        text = {"type": "string"}
        self.tools: dict[str, tuple[str, dict, Callable[[dict], str]]] = {
            "find_symbol": (
                "Find a C++ symbol by name (optionally qualified, e.g. Warehouse::add_item). Returns its kind, "
                "location and type signature. Semantic, via clangd: it tells same-named members of different "
                "classes apart.",
                _schema({"name": text}, ["name"]), self._find_symbol),
            "references": (
                "List the declaration, definition and every use of a symbol across the repository, plus the "
                "functions that call it. Sees uses inside macros. Use this before changing a signature.",
                _schema({"symbol": text}, ["symbol"]), self._references),
            "outline": (
                "List every symbol declared in one file, with line numbers.",
                _schema({"file": {**text, "description": "path relative to the repository"}}, ["file"]), self._outline),
            "diagnostics": (
                "Compiler errors and warnings for one file as clangd sees it now. Much faster than a build.",
                _schema({"file": {**text, "description": "path relative to the repository"}}, ["file"]), self._diagnostics),
            "baseline": (
                "Build the repository and run its tests as they are now. Returns the warning count and test "
                "results. Slow: a full clean build.",
                _schema({}, []), self._baseline),
            "check_patch": (
                "Apply a unified diff in a private git worktree and verify it: it must build, add no compiler "
                "warning, and keep every test passing (also under ASan and UBSan). On success the change is "
                "committed to a new branch, which is returned. On failure nothing is kept and the reason is "
                "returned, including compiler errors and the output of failing tests. The working tree is never "
                "modified. Patch paths are relative to the repository (a/src/x.cpp).",
                _schema({"patch": text, "description": {**text, "description": "one line, used as the commit subject"}},
                        ["patch"]), self._check_patch),
            "run_recipes": (
                "Run deterministic modernization recipes on a new branch and return what was committed or "
                "rejected. Recipes: std (raise to C++20), r1 (mechanical fixes from clang-tidy), r6 (dead-code "
                "removal). Comma-separated, run in order.",
                _schema({"recipes": text,
                         "flags": {"type": "array", "items": text, "description": "r6: NAME=on or NAME=off"},
                         "allow_api_changes": {"type": "boolean"}}, ["recipes"]), self._run_recipes),
        }

    # ---- protocol ---------------------------------------------------------

    def log(self, message: str) -> None:
        print(f"[refactor-pilot mcp] {message}", file=self._log_stream, flush=True)

    def handle(self, message: Any) -> dict | None:
        """Answer one decoded JSON-RPC message. Returns None for notifications."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
            return _error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid Request")
        method, params, request_id = message["method"], message.get("params") or {}, message.get("id")
        if "id" not in message:
            return None  # a notification (notifications/initialized, cancelled, ...): nothing to answer
        if method == "initialize":
            wanted = params.get("protocolVersion")
            version = wanted if wanted in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
            return _result(request_id, {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "refactor-pilot", "version": __version__},
                "instructions": f"Tools operate on the C++ repository at {self.cfg.repo}. Use references before "
                                "changing a signature, and check_patch to have any change built and tested.",
            })
        if method == "ping":
            return _result(request_id, {})
        if method == "tools/list":
            return _result(request_id, {"tools": [
                {"name": name, "description": description, "inputSchema": schema}
                for name, (description, schema, _) in self.tools.items()
            ]})
        if method == "tools/call":
            return _result(request_id, self._call(params.get("name"), params.get("arguments") or {}))
        return _error(request_id, -32601, f"Method not found: {method}")

    def _call(self, name: Any, arguments: Any) -> dict:
        if name not in self.tools:
            return _tool_result(f"Unknown tool: {name}. Available: {', '.join(self.tools)}", error=True)
        _, schema, handler = self.tools[name]
        if not isinstance(arguments, dict):
            return _tool_result("Arguments must be an object.", error=True)
        missing = [key for key in schema["required"] if key not in arguments]
        unknown = [key for key in arguments if key not in schema["properties"]]
        if missing or unknown:
            problem = (f"missing: {', '.join(missing)}. " if missing else "") + (f"unknown: {', '.join(unknown)}." if unknown else "")
            return _tool_result(f"Bad arguments for {name}: {problem}".strip(), error=True)
        started = time.monotonic()
        try:
            text = handler(arguments)
            self.log(f"{name} ok in {time.monotonic() - started:.1f}s")
            return _tool_result(text)
        except (ToolError, LspError, CheckpointError, ToolMissing, BuildFailed, ValueError, OSError) as exc:
            self.log(f"{name} failed: {exc}")
            return _tool_result(str(exc), error=True)

    def serve(self, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
        self.log(f"serving {self.cfg.repo} over stdio")
        try:
            for line in stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    response = self.handle(json.loads(line))
                except json.JSONDecodeError:
                    response = _error(None, -32700, "Parse error")
                if response is not None:
                    stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
                    stdout.flush()
        finally:
            if self._nav is not None:
                self._nav.stop()

    # ---- navigation -------------------------------------------------------

    def _navigator(self) -> SymbolNavigator:
        if self._nav is None:
            self.log("starting clangd and indexing (first navigation call)")
            self._nav = SymbolNavigator(self.cfg).start()
        return self._nav

    def _file(self, rel: str) -> str:
        path = (self.cfg.repo / rel).resolve()
        if self.cfg.repo not in path.parents or not path.is_file():
            raise ToolError(f"No such file in the repository: {rel}")
        self._navigator().invalidate(path)  # the agent may have edited it since the last call
        return path.relative_to(self.cfg.repo).as_posix()

    def _one_symbol(self, name: str):
        matches = self._navigator().find(name)
        if not matches:
            raise ToolError(f"No symbol named '{name}'. It may be in code that is not compiled in this configuration.")
        names = sorted({m.name for m in matches})
        if len(names) > 1:
            raise ToolError(f"'{name}' is ambiguous. Qualify it as one of: {', '.join(names)}")
        return matches[0]

    def _find_symbol(self, args: dict) -> str:
        nav = self._navigator()
        matches = nav.find(args["name"])
        if not matches:
            raise ToolError(f"No symbol named '{args['name']}'.")
        lines = []
        for sym in matches:
            lines.append(f"{sym.name}  ({sym.kind})  {sym.location}")
            signature = nav.signature(sym.location)
            lines += ["    " + part for part in signature.splitlines() if part.strip()][:8]
        return "\n".join(lines)

    def _references(self, args: dict) -> str:
        nav, sym = self._navigator(), self._one_symbol(args["symbol"])
        refs = nav.references(sym.location)
        lines = [f"{sym.name} ({sym.kind}): {len(refs)} references in {len({r.file for r in refs})} files"]
        lines += [f"  {r}  {r.text}" for r in refs]
        if sym.kind in ("function", "method", "constructor"):
            callers = nav.callers(sym.location)
            lines.append(f"{len(callers)} callers")
            lines += [f"  {c.location}  {c.name}" for c in callers]
        return "\n".join(lines)

    def _outline(self, args: dict) -> str:
        rel = self._file(args["file"])
        return "\n".join(f"{s.location.line:5d}  {s.kind:<12} {s.name}" for s in self._navigator().outline(rel)) or "(no symbols)"

    def _diagnostics(self, args: dict) -> str:
        rel = self._file(args["file"])
        found = self._navigator().diagnostics(rel)
        return "\n".join(f"{d.location}: {d.severity}: {d.message} [{d.code}]" for d in found) or "no diagnostics"

    # ---- verified change ----------------------------------------------------

    def _baseline(self, args: dict) -> str:
        snap = baselinemod.capture(self.cfg, sanitizers=self.sanitizers, tidy=False)
        failing = [name for name, state in snap.tests.items() if state != "pass"]
        lines = [f"{sum(snap.warnings.values())} compiler warnings",
                 f"{len(snap.tests) - len(failing)}/{len(snap.tests)} tests passing"]
        if failing:
            lines.append("failing: " + ", ".join(failing))
        if snap.sanitizer_tests is not None:
            ok = sum(state == "pass" for state in snap.sanitizer_tests.values())
            lines.append(f"{ok}/{len(snap.sanitizer_tests)} passing under ASan+UBSan")
        return "\n".join(lines)

    def _check_patch(self, args: dict) -> str:
        description = " ".join((args.get("description") or "patch from MCP client").split())[:120]
        with tempfile.TemporaryDirectory() as scratch:
            patch = Path(scratch) / "change.patch"
            patch.write_text(args["patch"] if args["patch"].endswith("\n") else args["patch"] + "\n")
            session = Session(self.cfg.repo, work_dir=self.cfg.work_dir, compiler=self.cfg.compiler,
                              sanitizers=self.sanitizers, cmake_args=self.cfg.cmake_args, log=self.log)
            with session:
                outcome = session.run(ChangeSet(
                    "mcp/patch", description, lambda repo, feedback: session.workspace.apply_patch(patch),
                    gate="full" if self.sanitizers else "fast",
                ))
            report = session.report
        if outcome.status == "committed":
            return (f"ACCEPTED. The change builds, adds no warning and keeps all tests passing.\n"
                    f"It is committed as {outcome.sha[:12]} on branch '{report.branch}' "
                    f"({', '.join(outcome.files)}). The working tree was not modified.\n"
                    f"To adopt it: git merge {report.branch}")
        if outcome.status == "no_change":
            return "NO CHANGE. The patch applied but altered nothing."
        detail = outcome.feedback or outcome.reason
        return f"REJECTED ({outcome.reason}). Nothing was kept.\n\n{detail}"

    def _run_recipes(self, args: dict) -> str:
        recipes = parse_recipes(args["recipes"])
        model = [r for r in recipes if r in LLM_RECIPES]
        if model:
            raise ToolError(f"Recipes {', '.join(model)} are written by a model and are run from the command line, "
                            "not through this tool. Available here: std, r1, r6.")
        flags = {}
        for item in args.get("flags") or []:
            name, _, state = str(item).partition("=")
            if not name or state.lower() not in ("on", "off"):
                raise ToolError(f"A flag must be NAME=on or NAME=off, got '{item}'.")
            flags[name] = state.lower() == "on"
        opts = PipelineOptions(repo=self.cfg.repo, recipes=recipes, work_dir=self.cfg.work_dir, compiler=self.cfg.compiler,
                               sanitizers=self.sanitizers, flags=flags, cmake_args=self.cfg.cmake_args,
                               allow_api_changes=bool(args.get("allow_api_changes")), measure_tidy=False)
        result = run_pipeline(opts, log=self.log)
        report = result.report
        lines = [f"{report.count('committed')} committed, {report.count('rolled_back')} rolled back, "
                 f"{report.count('reverted')} reverted, {report.count('skipped')} skipped."]
        if report.count("committed"):
            files, added, removed = result.diffstat
            lines.append(f"Branch '{report.branch}': {files} files, +{added} -{removed}. The working tree was not modified.")
        for outcome in report.outcomes:
            if outcome.status != "no_change":
                lines.append(f"  {outcome.status:<11} {outcome.id}" + (f"  ({outcome.reason})" if outcome.reason else ""))
        return "\n".join(lines + result.notes)


def _result(request_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_result(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}
