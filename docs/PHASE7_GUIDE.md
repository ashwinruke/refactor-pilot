# refactor-pilot — Phase 7 guide: the benchmark, and a dry run on real code

Phase 7 adds a benchmark command and, more usefully, the first runs of the tool on
real open-source projects. Those runs found five bugs that the fixture never
could. All five are fixed in this zip.

**One thing needs your decision.** You asked to approve the open-source targets
before they are finalized. Three are proposed below with measured results. Until
you approve them, only the bundled fixture runs by default.

Tested on both toolchains (CMake 3.28 / clang-tidy 18 and CMake 4.4 / Ninja 1.13 /
clang-tidy 21): **164 tests pass** on each.

**Apply this after Phase 6.** Every command can be pasted as it is, except the
one `cp` line.

---

## The proposed targets, for your approval

Deterministic recipes only (`r1`, `r6`), `g++` 13, ASan + UBSan on, measured in my
workspace:

| Target | Pinned at | Tests | Commits | Rejected by a gate | clang-tidy modernize findings | Diff | Time |
|---|---|---|---|---|---|---|---|
| tinyxml2 | tag `6.2.0` (2018) | 1 | 15 | 0 | 454 → 102 | 3 files, +423 −626 | 30–40 s |
| jsoncpp | tag `1.9.3` (2020) | 3 | 21 | 1 | 93 → 80 | 15 files, +109 −110 | about 6 min |
| leveldb | tag `1.22` (2019) | 30 | not run | — | 736 at baseline | — | hours |

What I can and cannot say about each:

- **tinyxml2 6.2.0.** The best small target. Old style throughout (220 of the findings are `0`/`NULL` used as null pointers), CRLF line endings, one large test. Results were identical on both toolchains. Its CMake file sets a policy that CMake 4 refuses outright, so the target carries a one-line setup edit that disables that line.
- **jsoncpp 1.9.3.** Already partly modernized upstream, so the numbers are modest. It is valuable for a different reason: it is where the flaky-test and clangd-crash bugs showed up. The one rejected change-set is instructive: `clangd` called five includes in the umbrella header `json.h` unused, removing them broke 225 compile lines, and the gate rolled it back.
- **leveldb 1.22.** The natural target for R2: 30 tests and raw pointers everywhere. I only captured a baseline (builds, 736 findings, 114 warnings). One of its tests, `db_test`, failed at baseline in my workspace, which the tool tolerates by design. A full run is too slow for my session, because every gate runs all 30 tests. It is in the list for you to try overnight.

I also tried and dropped: jsoncpp 1.8.4 (more legacy, but it registers no tests
with ctest), yaml-cpp 0.6.3 and 0.7.0 (their bundled googletest does not build
with current compilers), pugixml 1.9 (no ctest tests).

**To approve a target**, change its `"status"` from `"proposed"` to `"approved"`
in `bench/targets.json`. To reject one, delete its entry. Tell me which you want
and I will make that the shipped default.

---

## Five bugs that only real code could find

| Bug | Where it showed | Fix |
|---|---|---|
| **Line endings rewritten.** A three-line dead-code removal in a CRLF file became a 2,663-line diff, because Python's text mode converts CRLF to LF. | tinyxml2 | New `textio` module: every tool-written edit reads bytes, works on LF text, and writes back the file's own line endings. Bytes that are not valid UTF-8 survive too |
| **Flaky tests caused false rollbacks.** Two jsoncpp tests share a scratch directory and collide when run in parallel. 15 of 22 correct change-sets were rolled back or reverted. | jsoncpp | A failing test is rerun once, alone and serially, before it counts. After the fix: 21 committed, 1 rejected |
| **clangd crashed** (a segfault inside clangd while re-parsing one file) and took the whole recipe with it. | jsoncpp | The navigator restarts clangd and retries once. A file clangd cannot handle is skipped |
| **One recipe failing lost the run.** Any exception inside a recipe aborted the pipeline. | jsoncpp | Each recipe is isolated: an error is recorded as a note, uncommitted edits are rolled back, and earlier commits are kept |
| **Misleading flag hints.** R6 suggested "flags" such as `finite` and `attribute_deprecated_with_message` that are not flags. | jsoncpp | Hints now list only macros used in a form `--flag` can resolve |

Two smaller ones: clangd waited 60 seconds for diagnostics that never come when a
file's text had not changed, and overloaded functions were listed twice in the
tier-2 report.

---

## Files in this zip

| File | Status | What it is |
|---|---|---|
| `src/refactor_pilot/pipeline.py` | new | `run_pipeline()`: runs a list of recipes on one branch and measures the result. Both `run` and `bench` use it |
| `src/refactor_pilot/bench.py` | new | The benchmark: target list, cloning and pinning, per-target setup edits, JSON and Markdown reports |
| `src/refactor_pilot/textio.py` | new | File I/O that preserves line endings and non-UTF-8 bytes |
| `bench/targets.json` | new | The target list: the fixture (approved) and three proposed open-source targets |
| `src/refactor_pilot/build.py` | changed | Failing tests are rerun serially before they count; clearer CMake configure errors |
| `src/refactor_pilot/lsp/client.py`, `navigator.py` | changed | clangd runs in its own process group, is restarted after a crash, and is not asked to re-check unchanged text |
| `src/refactor_pilot/edits.py`, `recipes/r6_dead_code.py`, `recipes/std_raise.py` | changed | All edits go through `textio`; R6 tolerates files clangd cannot diagnose; flag hints and tier-2 names cleaned up |
| `src/refactor_pilot/session.py`, `gates.py`, `checkpoint.py` | changed | Extra CMake arguments and test timeout per session; warning count and diff size for reports |
| `src/refactor_pilot/cli.py` | changed | New `bench` command; `--cmake-arg` on every command that builds |
| `src/refactor_pilot/__init__.py`, `pyproject.toml`, `.gitignore` | changed | Version 0.6.0 → 0.7.0; `bench-results/` ignored |
| `README.md` | changed | A results section, the `bench` command, layout, roadmap tick |
| `tests/test_textio.py`, `tests/test_bench.py` | new | 7 and 13 tests |
| `tests/test_navigator_integration.py`, `tests/test_phase6_recipes.py` | changed | A clangd crash-survival test; one assertion tightened |
| `docs/PHASE7_GUIDE.md` | new | This guide |

No fixture files changed. No new Python dependencies.

---

## Apply the update

```bash
cd ~/projects/refactor-pilot
git status --short
```

This must print nothing. If it lists files: `git add -A && git commit -m "Phase 6"`.

Fill in your Windows user folder (`ls /mnt/c/Users` lists them):

```bash
cp /mnt/c/Users/YOUR_WINDOWS_USER/Downloads/refactor-pilot-phase7.zip ~/projects/
```

```bash
unzip -o ~/projects/refactor-pilot-phase7.zip -d ~/projects
git status --short
source .venv/bin/activate
pip install -e ".[dev]"
```

Expected from `git status --short`: 16 modified (`M`) and 7 new (`??`) entries.

## Verify

**1. Tests**

```bash
pytest -m "not slow"
```

Expected: `163 passed, 1 deselected`. (Plain `pytest` also runs the
whole-pipeline test: `164 passed`.)

```bash
git add -A && git commit -m "Phase 7: benchmark harness and real-code fixes"
```

**2. Benchmark the fixture (approved targets only)**

```bash
refactor-pilot bench bench/targets.json
```

Takes a few minutes. Expected table row:

```
| legacy_inventory | local | std,r1,r6 | 5/5 | 27 | 0 | 67 → 32 | 2 → 0 | 11 files, +67 −92 | 0/32 → 17/32 | ...s |
```

The report is also written to `bench-results/REPORT.md` and `bench-results/results.json`.

**3. Try a proposed target: tinyxml2**

This clones tinyxml2 from GitHub into `.refactor-pilot/bench/repos/` (ignored by git).

```bash
refactor-pilot bench bench/targets.json --only tinyxml2 --keep-branches
```

Expected row (your compiler may differ slightly from mine on the last columns):

```
| tinyxml2 | 6.2.0 | r1,r6 | 1/1 | 15 | 0 | 454 → 102 | 0 → 0 | 3 files, +423 −626 | — | ...s |
```

Because of `--keep-branches`, you can read what it did to a real project:

```bash
git -C .refactor-pilot/bench/repos/tinyxml2 branch --list 'refactor-pilot/*'
git -C .refactor-pilot/bench/repos/tinyxml2 log --oneline -17
git -C .refactor-pilot/bench/repos/tinyxml2 log -1 --stat
```

The second command shows 15 `r1/...` and `r6/...` commits on top of one
`refactor-pilot bench: build-system setup` commit and tinyxml2's own history.

**4. Try jsoncpp (about 6–10 minutes)**

```bash
refactor-pilot bench bench/targets.json --only jsoncpp
```

Expected: about 21 commits and 1 rejected. The exact count can vary by one or two
with the `clangd` and compiler versions. The report's "Rejected change-sets"
section should name `r6/unused-include/include/json/json.h`.

**5. Everything proposed, in one report (optional, long)**

Leave leveldb out unless you have hours:

```bash
refactor-pilot bench bench/targets.json --only legacy_inventory,tinyxml2,jsoncpp --out bench-results/dry-run
```

---

## Using the benchmark with a model

A target's `"recipes"` can be any pipeline. To benchmark the model-written
recipes, set for example `"recipes": "all"` on a target, load your key
(`source ~/.refactor-pilot.env`) and run `bench`. Add `--record FILE` to keep
every answer. The report gains a "Model usage" table, and its header names the
model, so results from different models can be compared side by side. That
comparison is the measurement behind the README's claim that a stronger model
should resolve more.

## Adding your own repository

Add an entry to `bench/targets.json`:

```json
{
  "name": "myproject",
  "git": "https://github.com/OWNER/REPO.git",
  "ref": "v1.2.3",
  "commit": "the full 40-character commit id of that tag",
  "recipes": "r1,r6",
  "cmake_args": ["-DBUILD_TESTING=ON"],
  "status": "proposed"
}
```

Or use `"path"` instead of `git`/`ref`/`commit` for a directory on disk (it must
be inside a git repository with no uncommitted changes). Other fields: `subdir`,
`manifest`, `flags`, `allow_api_changes`, `sanitizers`, `test_timeout`,
`full_every`, `prepare`, `note`.

A project is a usable target only if it is a CMake project, registers tests with
`add_test`, and builds without downloading anything at configure time.

## Known limits

- **Every gate runs every test.** Fine for tinyxml2, slow for jsoncpp, impractical for leveldb. Test selection (run only tests that depend on the changed files) is the most valuable missing feature for large repositories.
- **A flaky test that fails twice in a row still causes a false rollback.** One serial rerun removes most flakiness, not all.
- **Test files are recognised by name.** tinyxml2's `xmltest.cpp` does not match, so the tool treated it as ordinary source. That is harmless for R1 and R6, but the model-written recipes would treat it as a target.
- **Old projects need build-system help under CMake 4.** `-DCMAKE_POLICY_VERSION_MINIMUM=3.5` covers most. Some, like tinyxml2, need a `prepare` edit.
- **Open-source numbers are from my workspace.** Expect small differences on your machine.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `clone failed` | No network, or GitHub is unreachable from WSL. Check with `git ls-remote https://github.com/leethomason/tinyxml2.git` |
| `... but the config pins ...` | The upstream tag moved, or the pinned commit is wrong. Do not edit the pin without checking why |
| `failed: cmake configure failed ... CMake Error ...` | The message now includes what CMake said. Usually a missing `cmake_args` entry |
| `uncommitted changes in the target` on the fixture | Commit your work first; `bench` runs on the last commit |
| A long run was interrupted | `git worktree prune`, then rerun. Clones are reused |

## What to send back to me

1. The last line of `pytest -m "not slow"`.
2. The report table rows from verify steps 2, 3 and 4.
3. **Your decision on the three proposed targets**: approve, reject, or replace each.
