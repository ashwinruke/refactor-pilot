# refactor-pilot — Phase 1 guide: clangd client and symbol navigator

Phase 1 adds the `lsp_symbol_navigator` tool: the project can now ask `clangd`
where a symbol is defined, what its type is, who calls it, and every place it is
used across files. No code is rewritten yet.

Tested with `clangd` 21.1.8 (your version) and `g++` 13 on Linux: 43 tests pass.

---

## Why this phase matters

Modernizing a symbol safely means changing its definition and **all** of its uses
together. Text search is not good enough for that:

- `total_value` exists on `Item`, `PerishableItem` and `Warehouse`. A text search returns all three mixed together; `clangd` separates them.
- One use of `create_item` sits inside the `CHECK(...)` macro. `clangd` sees through the macro.
- `legacy_flat_tax` has a declaration and a definition but no callers. That "zero uses" answer is what the Phase 5 dead-code pruner is built on.

The list `refs` prints (definition + every reference) is the unit that Phase 2
will turn into one atomic, verified change-set.

---

## Files in this zip

| File | Status | What changed |
|---|---|---|
| `src/refactor_pilot/lsp/__init__.py` | new | Package for the language-server client |
| `src/refactor_pilot/lsp/jsonrpc.py` | new | JSON-RPC over stdio with `Content-Length` framing; a reader thread routes responses, notifications and server requests |
| `src/refactor_pilot/lsp/positions.py` | new | Converts between compiler-style positions (1-based, characters) and LSP positions (0-based, UTF-16 units); file path ↔ URI |
| `src/refactor_pilot/lsp/client.py` | new | Starts and stops `clangd`, opens files, waits for the background index, collects diagnostics, sends queries |
| `src/refactor_pilot/navigator.py` | new | The friendly API: `find`, `outline`, `definition`, `references`, `signature`, `callers`, `diagnostics`, `invalidate` |
| `src/refactor_pilot/build.py` | changed | Split `configure()` out of `build()`. Configure alone produces `compile_commands.json`, which is all `clangd` needs, so navigation does not require a full compile |
| `src/refactor_pilot/cli.py` | changed | Two new commands: `symbols` and `refs`; LSP errors are reported cleanly |
| `src/refactor_pilot/__init__.py`, `pyproject.toml` | changed | Version 0.0.1 → 0.1.0 |
| `README.md` | changed | Status, the two new commands, layout, roadmap tick |
| `tests/test_lsp_protocol.py` | new | 7 unit tests: message framing, a fake server, UTF-16 column conversion, URI round-trip |
| `tests/test_navigator_integration.py` | new | 12 tests against a real `clangd` on a copy of the fixture |
| `docs/PHASE1_GUIDE.md` | new | This guide |

No fixture files changed. No new Python dependencies.

---

## New setup

None, if you completed the Phase 0 guide: `clangd` was installed in step 3.
Confirm with:

```bash
clangd --version | head -1
```

---

## Apply the update

```bash
cd ~/projects/refactor-pilot
git status --short                      # should print nothing
cp /mnt/c/Users/<YourWindowsUser>/Downloads/refactor-pilot-phase1.zip ..
unzip -o ../refactor-pilot-phase1.zip -d ..
git status --short                      # 5 modified (M) and 5 new (??) entries
git diff                                # line-by-line changes to the 5 modified files
source .venv/bin/activate
pip install -e ".[dev]"
```

## Verify

**1. Tests**

```bash
pytest
```

Expected: `43 passed`. (`31 passed, 12 skipped` means `clangd` is not on PATH.)

**2. Outline of a file**

```bash
refactor-pilot symbols fixtures/legacy_inventory include/inventory/warehouse.h
```

Expected: 17 lines, starting `10  namespace    inventory` and including
`31  method       inventory::Warehouse::release`.

**3. References to a symbol**

```bash
refactor-pilot refs fixtures/legacy_inventory Warehouse::release
```

Expected, after the signature block:

```
5 references in 3 files:
  include/inventory/warehouse.h:31:11      Item* release(ItemId id);
  src/warehouse.cpp:35:18                  Item* item = release(id);
  src/warehouse.cpp:43:18                  Item* Warehouse::release(ItemId id) {
  tests/test_main.cpp:83:32                Item* released = warehouse.release(2);
  tests/test_main.cpp:87:21                CHECK(warehouse.release(2) == NULL);

2 callers:
  src/warehouse.cpp:34:17                  remove
  tests/test_main.cpp:52:13                test_warehouse
```

**4. A dead function**

```bash
refactor-pilot refs fixtures/legacy_inventory legacy_flat_tax
```

Expected: `2 references in 2 files` (declaration and definition) and `0 callers`.

**5. An ambiguous name**

```bash
refactor-pilot refs fixtures/legacy_inventory total_value
```

Expected: `'total_value' is ambiguous; qualify it:` followed by three qualified names, exit code 1.

**6. Commit**

```bash
git add -A && git commit -m "Phase 1: clangd client and symbol navigator"
```

---

## How it works, briefly

1. `SymbolNavigator` runs cmake configure in an out-of-tree build directory to get `compile_commands.json`.
2. It starts `clangd` pointed at that directory and opens one source file, which triggers background indexing of the whole project.
3. It waits for the index to go idle. Cross-file answers are incomplete before that.
4. Each query converts your position to LSP form, asks `clangd`, and converts the answer back to repo-relative, 1-based locations with the source line attached.

`clangd` writes its log to `clangd.log` and its index to `.cache/` inside the build
directory (under the system temp folder by default), never inside the target repo.

## Known limits

- **One build configuration.** `clangd` sees the code as configured. Code inside an `#ifdef` branch that is not compiled is invisible to it, as is the body of an `#if 0` block.
- **Caller names are unqualified.** `callers` prints `remove`, not `inventory::Warehouse::remove`. The location is exact.
- **Virtual calls.** References to `Item::total_value` do not automatically include calls that dispatch to an override. Phase 2 handles overrides as one change-set explicitly.
- **Cold start cost.** The fixture indexes in about 2 seconds. A large repo will take minutes the first time; the index is cached on disk afterwards.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `error: 'clangd' was not found on PATH` | `sudo apt install -y clangd` |
| `error: ... timed out` | Look at `clangd.log` in the build directory; rerun with `--work-dir ./.refactor-pilot` to find it easily |
| `no symbol named ...` for a symbol that exists | It may be inside a branch that is not compiled, or in a file not listed in `CMakeLists.txt` |
| `refs` is slow the first time | That is the cold index; the second run is fast |

## What to send back to me

1. The last line of `pytest`.
2. The "references" and "callers" blocks from verify step 3.
3. Anything in the output that differs from this guide.

## Next: Phase 2

Git worktree checkpoints and the two-speed verification gate: a fast gate after
every change-set (recompile affected files, live diagnostics) and a full gate
(build, tests, sanitizers) every few change-sets, with automatic rollback.
