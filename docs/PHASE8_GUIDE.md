# refactor-pilot — Phase 8 guide: MCP server, GitHub Actions, and wrap-up

Phase 8 is the last planned phase. It makes the tool usable from outside its own
command line:

- **An MCP server**, so any agent that speaks the Model Context Protocol can use the semantic navigation and, more importantly, have its own edits verified.
- **Two GitHub Actions workflows**: one runs this project's tests; one is an example other projects can copy to receive modernization pull requests.
- **A license file** and a README that states what has and has not been verified.

Tested on both toolchains (CMake 3.28 / clang-tidy 18 and CMake 4.4 / Ninja 1.13 /
clang-tidy 21): **174 tests pass** on each.

**Apply this after Phase 7.** Every command can be pasted as it is, except the
`cp` line and the GitHub lines in step 6.

---

## The MCP server

```bash
refactor-pilot mcp path/to/project
```

starts a server bound to that one repository. It reads and writes one JSON
message per line on stdin and stdout, and logs to stderr.

| Tool | Arguments | What it returns |
|---|---|---|
| `find_symbol` | `name` | Kind, location and type signature of each match |
| `references` | `symbol` | Declaration, definition, every use across files, and the callers |
| `outline` | `file` | Every symbol declared in the file |
| `diagnostics` | `file` | Errors and warnings from `clangd`, without a build |
| `baseline` | — | Warning count and test results after a clean build |
| `check_patch` | `patch`, `description` | `ACCEPTED` with the branch name, or `REJECTED` with compiler errors and failing-test output |
| `run_recipes` | `recipes`, `flags`, `allow_api_changes` | What `std`, `r1`, `r6` committed and rejected |

### Why `check_patch` is the point

An agent that edits C++ usually cannot tell whether its edit is right. With
`check_patch` it proposes a diff and gets a verdict from the same gates this
project uses for its own recipes: the change must build, add no warning, and
keep every test passing, also under ASan and UBSan. A rejection comes back with
the reason in full, so the agent can fix its patch and try again. The agent's
working tree is never touched; an accepted change lands on a new branch.

That is the Phase 4 loop turned inside out: there, refactor-pilot called a model
and checked its answers; here, any model can call refactor-pilot to have its
answers checked.

### Protocol notes

- It implements the handshake-based MCP revisions, 2024-11-05 through 2025-11-25: `initialize`, `notifications/initialized`, `ping`, `tools/list`, `tools/call`.
- MCP's 2026-07-28 revision removed that handshake in favour of stateless requests. This server does not implement that revision. It answers methods it does not know with a standard "method not found" error, and it answers `tools/list` and `tools/call` without a prior `initialize`. Client SDKs that prefer the new revision document a fallback to the handshake for older servers; I have not tested that with a real client.
- The model-written recipes (R2–R5) are not offered through MCP. An agent that is itself a model should write the edit and use `check_patch`.

---

## Files in this zip

| File | Status | What it is |
|---|---|---|
| `src/refactor_pilot/mcp_server.py` | new | The server: protocol handling and the seven tools |
| `src/refactor_pilot/cli.py` | changed | New `mcp` command |
| `src/refactor_pilot/session.py` | changed | `Outcome.feedback` keeps the full text of a rejection, so `check_patch` can return it |
| `src/refactor_pilot/lsp/client.py`, `recipes/r6_dead_code.py` | changed | Fix for an intermittent wrong target in R6, see below |
| `tests/test_lsp_protocol.py`, `tests/test_preproc.py` | changed | One new test each for that fix |
| `src/refactor_pilot/__init__.py`, `pyproject.toml` | changed | Version 0.7.0 → 0.8.0 |
| `README.md` | changed | Final status, a "What has been verified" table, an MCP section |
| `LICENSE` | new | MIT, matching `pyproject.toml` |
| `.github/workflows/ci.yml` | new | Runs `pytest -m "not slow"` on push and pull request |
| `examples/github/modernize.yml` | new | Example workflow for *another* project: runs `r1,r6` and opens a pull request |
| `tests/test_mcp_server.py` | new | 8 tests: 5 of the protocol, 3 real sessions over stdio |
| `docs/PHASE8_GUIDE.md` | new | This guide |

No fixture files changed. No new dependencies.

## An intermittent bug caught by the final test run

The whole-pipeline test failed once in the last verification run and passed when
rerun. Running the pipeline repeatedly showed the cause, about one run in two:

- After R6 removed a function, it asked `clangd` for the file's diagnostics again.
- `clangd` sometimes delivered the diagnostics of the *previous* version of the file late, after the new text had been sent.
- R6 matched "unused function at line 21" against the new file, where line 21 was now `bulk_price`, a public function, and tried to delete it.
- The gate rejected that (it does not compile), so nothing wrong was ever committed and the score stayed 32/32. But a change-set was wasted, and on other code the wrong target might have compiled.

Two fixes: the client now accepts diagnostics only for the version of the file it
last sent, and R6 removes a symbol only if the compiler's message names that
symbol. With both in place the pipeline ran four times in a row with no rollback.

This bug existed since Phase 5. If you saw an unexpected
`rolled_back r6/unused-function/...` line in an earlier phase, this was it.

---

## Apply the update

```bash
cd ~/projects/refactor-pilot
git status --short
```

This must print nothing. If it lists files: `git add -A && git commit -m "Phase 7"`.

Fill in your Windows user folder (`ls /mnt/c/Users` lists them):

```bash
cp /mnt/c/Users/YOUR_WINDOWS_USER/Downloads/refactor-pilot-phase8.zip ~/projects/
```

```bash
unzip -o ~/projects/refactor-pilot-phase8.zip -d ~/projects
git status --short
source .venv/bin/activate
pip install -e ".[dev]"
```

Expected from `git status --short`: 9 modified (`M`) and 6 new (`??`) entries.
One of the new entries is `.github/`; `ls -a` shows it.

## Verify

**1. Tests**

```bash
pytest -m "not slow"
```

Expected: `173 passed, 1 deselected`. (Plain `pytest`: `174 passed`.)

```bash
git add -A && git commit -m "Phase 8: MCP server, workflows, license"
```

**2. Talk to the MCP server by hand**

This sends four messages and pretty-prints the replies. No client is needed.

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"me","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"references","arguments":{"symbol":"Warehouse::release"}}}' \
  | refactor-pilot mcp fixtures/legacy_inventory 2>/dev/null \
  | python3 -c 'import sys,json
for line in sys.stdin:
    m=json.loads(line); r=m["result"]
    if "tools" in r: print(m["id"], [t["name"] for t in r["tools"]])
    elif "content" in r: print(m["id"], r["content"][0]["text"])
    else: print(m["id"], r["serverInfo"], r["protocolVersion"])'
```

Expected: three replies (the notification gets none).

```
1 {'name': 'refactor-pilot', 'version': '0.8.0'} 2025-11-25
2 ['find_symbol', 'references', 'outline', 'diagnostics', 'baseline', 'check_patch', 'run_recipes']
3 inventory::Warehouse::release (method): 5 references in 3 files
  include/inventory/warehouse.h:31:11  Item* release(ItemId id);
  ...
2 callers
  ...
```

**3. Have a patch checked through MCP**

This builds one request from an example patch and sends it. The patch is the
"drops a `delete`" one from Phase 2: ordinary tests pass, only the sanitizers
catch the leak.

```bash
python3 -c 'import json,pathlib
patch = pathlib.Path("examples/patches/04-leak-in-remove.patch").read_text()
print(json.dumps({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"check_patch","arguments":{"patch":patch}}}))' \
  | refactor-pilot mcp fixtures/legacy_inventory 2>/dev/null \
  | python3 -c 'import sys,json; print(json.loads(sys.stdin.readline())["result"]["content"][0]["text"][:900])'
```

Expected: the text starts with `REJECTED (sanitizers: test now fails under sanitizers: warehouse). Nothing was kept.`
and goes on to include the LeakSanitizer report. If you get the clang
sanitizer-runtime error instead, add `--compiler g++` after `fixtures/legacy_inventory`.

Now a good patch:

```bash
python3 -c 'import json,pathlib
patch = pathlib.Path("examples/patches/01-nullptr-logger.patch").read_text()
print(json.dumps({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"check_patch","arguments":{"patch":patch,"description":"use nullptr in logger"}}}))' \
  | refactor-pilot mcp fixtures/legacy_inventory 2>/dev/null \
  | python3 -c 'import sys,json; print(json.loads(sys.stdin.readline())["result"]["content"][0]["text"])'
```

Expected: `ACCEPTED. ...` naming a branch `refactor-pilot/<timestamp>`.

```bash
git status --short
git branch --list 'refactor-pilot/*'
git branch --list 'refactor-pilot/*' | xargs git branch -D
```

The first prints nothing (your tree was not touched), the second shows the one
branch the accepted patch created, the third removes it.

**4. Connect a real MCP client (optional)**

Clients differ in where their configuration lives, so check your client's own
documentation. The entry they need is a stdio server with this command and
arguments (use absolute paths):

```json
{
  "mcpServers": {
    "refactor-pilot": {
      "command": "/home/YOUR_LINUX_USER/projects/refactor-pilot/.venv/bin/refactor-pilot",
      "args": ["mcp", "/home/YOUR_LINUX_USER/projects/refactor-pilot/fixtures/legacy_inventory"]
    }
  }
}
```

If the client runs on Windows and the tool lives in WSL, the command is `wsl`
and the arguments start with that path: `["/home/.../.venv/bin/refactor-pilot", "mcp", "/home/.../legacy_inventory"]`.
I have not tried this with any client. If it fails, the server's stderr output is
the first thing to look at.

**5. Read the workflows**

```bash
cat .github/workflows/ci.yml
cat examples/github/modernize.yml
```

Neither has ever been executed. Treat the first run of each as a test.

**6. Publish (when you are ready)**

Create an empty repository named `refactor-pilot` on GitHub, without a README or
license (the project has both). Then, filling in your user name:

```bash
git branch -M main
git remote add origin https://github.com/YOUR_GITHUB_USER/refactor-pilot.git
git push -u origin main
```

Before pushing, check that nothing private is tracked:

```bash
git ls-files | grep -iE "env|key|secret|jsonl$" 
```

That should list only the five example recordings under `examples/replays/`.
Your API key file lives in your home directory, outside the repository, and
`.gitignore` already excludes `*.baseline.json`, `.refactor-pilot/` and
`bench-results/`.

The CI workflow will start on that first push. GitHub's runner has an older
`clangd` (18) than the one the navigation tests were developed against (21), so
some tests may behave differently there. If CI fails, send me the log.

---

## Where the project stands

Built and tested:

| | |
|---|---|
| Phases | 0 to 8, each with a guide in `docs/` |
| Recipes | `std`, `r1`, `r2`, `r3`, `r4`, `r5`, `r6` |
| Commands | `baseline`, `check`, `score`, `symbols`, `refs`, `apply`, `run`, `bench`, `mcp` |
| Tests | 174, on two toolchains |
| Fixture | 32 of 32 planted constructs resolved by the full pipeline (scripted model) |
| Real code | `r1,r6` run on tinyxml2 6.2.0 and jsoncpp 1.9.3 |

Not yet done, in the order I would do it:

1. **Run R2–R5 against a real model** (Phase 4 guide part B, Phase 6 guide part B) and send me the recordings. This is the largest open risk: the prompts have never been read by a real model. Expect to tune them.
2. **Decide the benchmark targets** (Phase 7 guide). Then run the benchmark with `"recipes": "all"` on at least one free model and, if you can, one stronger model. That table is the headline result for a portfolio.
3. **Try the MCP server with a real client**, and run both workflows once.
4. **Test selection**, so a gate runs only the tests affected by a change. Without it, large repositories such as leveldb are impractically slow.
5. **Better test-file detection** (a config option instead of naming conventions) and support for build systems other than CMake.

## Troubleshooting

| Symptom | Fix |
|---|---|
| The `printf ... | refactor-pilot mcp` pipeline prints nothing | Remove `2>/dev/null` to see the server's log |
| `Method not found` from a client | The client is using an MCP method this server does not implement. Send me the method name |
| `check_patch` says `could not apply` | Patch paths must be relative to the repository, with `a/` and `b/` prefixes, against the last commit |
| `uncommitted changes in the target` from `check_patch` or `run_recipes` | These work from the last commit. Commit or stash first |
| A long tool call seems to hang | `check_patch` and `run_recipes` build and test; the first call in a session also does a full baseline build |

## What to send back to me

1. The last line of `pytest -m "not slow"` (expected `173 passed, 1 deselected`).
2. The output of verify steps 2 and 3.
3. If you tried a real MCP client or pushed to GitHub: what happened.
