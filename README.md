# refactor-pilot

An LSP-powered agent that modernizes legacy C++ (C++11/14 → C++20) in small,
verified, reversible change-sets, and delivers the result as a reviewable branch.

**Status: Phase 0 of 8.** The verification baseline, the benchmark fixture and the
scoring harness exist. Nothing rewrites code yet.

## Why it is built this way

- **Change-sets, not files.** A definition and all its references change in one commit, so every commit builds.
- **Baseline diff, not "zero warnings".** Legacy code is never clean. The gate is: no new warnings, no test that passed now failing, no test deleted.
- **Sanitizers are part of the gate.** In C++ a leak or use-after-free leaves ordinary tests green. The same tests are rerun under ASan + UBSan.
- **Codemods first, LLM second.** Mechanical rewrites go through `clang-tidy` fixes. The model is only used where judgement is needed (ownership, macros, SFINAE).
- **Never touches your working tree.** Builds are out-of-tree; edits (from Phase 2) happen in a separate git worktree.

## Setup (Windows via WSL2, or any Ubuntu)

```bash
sudo apt update
sudo apt install -y build-essential cmake ninja-build clang clang-tidy clangd libclang-rt-dev python3-venv git
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

If the clang sanitizer runtime is unavailable, use `--compiler g++` (its runtime ships with the compiler).

## Commands

```bash
# Record how the repo builds and tests today
refactor-pilot baseline fixtures/legacy_inventory -o fixture.baseline.json

# After any change: rebuild, retest, fail on regressions (exit code 1)
refactor-pilot check fixtures/legacy_inventory --baseline fixture.baseline.json

# How many known legacy constructs are gone
refactor-pilot score fixtures/legacy_inventory --manifest fixtures/legacy_inventory/manifest.json -v
```

Target repos must be CMake projects. Build trees go to the system temp directory unless `--work-dir` is given.

## The fixture

`fixtures/legacy_inventory` is a small C++11 library with 32 planted legacy
constructs, listed in its `manifest.json` and grouped by the recipe meant to fix them:

| Recipe | What | Count |
|---|---|---|
| R1 | Mechanical: `NULL`, missing `override`, `typedef`, iterator loops, undefined private copy constructors, C headers and casts | 11 |
| R2 | Ownership: raw owning pointers, manual `delete`, `FILE*`, manual `lock`/`unlock` | 8 |
| R3 | Macros and unscoped enums | 3 |
| R4 | `enable_if` SFINAE → concepts | 1 |
| R5 | C++20 library: `std::format`, `std::span`, `starts_with` | 3 |
| R6 | Dead code: stale `#ifdef`, constant flag, `#if 0`, unused function and include | 6 |

It builds with two known warnings and five passing tests, on purpose: that is the baseline the tool must not make worse.

## Layout

```
src/refactor_pilot/
  toolchain.py     run external tools
  diagnostics.py   parse compiler / clang-tidy output
  build.py         configure, build, ctest, clang-tidy (out-of-tree)
  baseline.py      snapshots and the regression comparison
  evalkit/         manifests and scoring
  cli.py           baseline | check | score
fixtures/legacy_inventory/   benchmark target
tests/                       unit tests + end-to-end tests with injected bugs
```

## Roadmap

| Phase | Deliverable |
|---|---|
| 0 | Fixture, baseline, scoring harness ✅ |
| 1 | `clangd` client and symbol navigator |
| 2 | Git worktree checkpoints, fast/full verification gates |
| 3 | R1 end to end (no LLM) |
| 4 | Agent loop and R2 (ownership) |
| 5 | R6 dead-code pruner with confidence tiers |
| 6 | R3–R5 |
| 7 | Benchmark on real open-source repos |
| 8 | MCP server, GitHub Action |
