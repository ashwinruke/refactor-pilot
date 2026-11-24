# refactor-pilot

An LSP-powered agent that modernizes legacy C++ (C++11/14 → C++20) in small,
verified, reversible change-sets, and delivers the result as a reviewable branch.

**Status:** Seven recipes, a benchmark, and an MCP server.
On the bundled fixture the full pipeline resolves **32 of 32** planted legacy constructs in 43 verified commits.
Read [What has been verified](#what-has-been-verified) before relying on any of it.

| Recipe | What it does | Engine |
|---|---|---|
| `std` | Raises the project's C++ standard (default C++20) | CMake edit, re-baselined |
| `r1` | `nullptr`, `override`, range-for, `using`, `= delete`, casts; at C++20 also `enable_if` → `requires`, `starts_with` | `clang-tidy` fix-its |
| `r2` | Manual `lock`/`unlock` → `lock_guard`, `FILE*` → RAII, raw owning pointers → `unique_ptr` | LLM |
| `r3` | Value and function macros → `constexpr`; unscoped enums → `enum class` | LLM |
| `r4` | Remaining SFINAE → concepts | LLM |
| `r5` | `snprintf` buffers → `std::format`, pointer+length → `std::span`, prefix tests → `starts_with` | LLM |
| `r6` | Dead code: `#if 0`, stale flags, constant `if`s, unused includes and functions | Text + `clangd` |

Every edit, whoever wrote it, must pass the same gates before it is committed.

## Results so far

Deterministic recipes only (`r1`, `r6`; plus `std` on the fixture), `g++` 13, ASan + UBSan on:

| Target | Commits | Rejected by a gate | clang-tidy modernize findings | Diff |
|---|---|---|---|---|
| Bundled fixture | 27 | 0 | 67 → 32 | 11 files, +67 −92 |
| tinyxml2 6.2.0 | 15 | 0 | 454 → 102 | 3 files, +423 −626 |
| jsoncpp 1.9.3 | 21 | 1 | 93 → 80 | 15 files, +109 −110 |

The open-source rows are a dry run of *proposed* benchmark targets, measured during
development. Reproduce them with `refactor-pilot bench bench/targets.json --only tinyxml2,jsoncpp`.

## What has been verified

| Part | Status |
|---|---|
| Gates, checkpoints, rollback, culprit search | Tested with injected bugs on two toolchains (CMake 3.28 / clang-tidy 18 and CMake 4.4 / clang-tidy 21) |
| R1 and R6 (deterministic) | Tested on the fixture and run on tinyxml2 and jsoncpp |
| R2–R5 (model-written) | The loop, checks and prompts are tested with **scripted** model answers only. They have not yet been run against a real model |
| Benchmark on leveldb | Baseline only; a full run has not been done |
| MCP server | Tested with a scripted client over stdio. Not yet tried with a real MCP client |
| GitHub Actions workflows | Written but never executed |

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

```bash
# Every symbol declared in a file
refactor-pilot symbols fixtures/legacy_inventory include/inventory/warehouse.h

# A symbol's signature, every reference across files, and its callers
refactor-pilot refs fixtures/legacy_inventory Warehouse::release
```

`refs` is semantic, not textual: it tells `Item::total_value` from
`Warehouse::total_value`, and finds uses inside macros. The definition plus all
references is the unit Phase 2 turns into one atomic change-set.

```bash
# Push patches through the gates onto a new branch (your working tree is untouched)
refactor-pilot apply fixtures/legacy_inventory --patch examples/patches/*.patch
```

Each patch is one change-set: it is applied in a private git worktree, checked by
the **fast gate** (incremental build, no new warnings, no test regressions) and
committed, or rolled back. Every few commits the **full gate** reruns the tests
under ASan + UBSan; if that fails, a binary search finds the commit responsible,
removes it and re-applies the later ones. Of the six example patches, three are
deliberately bad and all three are removed.

```bash
# Run recipe R1 on a new branch and report the benchmark score before and after
refactor-pilot run fixtures/legacy_inventory --manifest fixtures/legacy_inventory/manifest.json
```

`run` asks `clang-tidy` for fixes one check and one file at a time. Each becomes
a change-set that must pass the same gates as any patch, so a fix that breaks the
build, adds a warning or fails a test is rolled back and reported, not merged.
Public function signatures are left alone by default.

```bash
# Recipe R2 (ownership). --plan lists the targets and calls no model.
refactor-pilot run fixtures/legacy_inventory --recipe r2 --plan

# Run it from a recording: no network, no API key
refactor-pilot run fixtures/legacy_inventory --recipe r2 --replay examples/replays/r2-fixture-internal.jsonl

# Run it against a live model (needs REFACTOR_PILOT_API_KEY, see below)
refactor-pilot run fixtures/legacy_inventory --recipe r2 --record my-run.jsonl
```

```bash
# Recipe R6 (dead code). Declare a flag stale to have its branches removed.
refactor-pilot run fixtures/legacy_inventory --recipe r6 --flag INVENTORY_ENABLE_LEGACY_PRICING=off

# Several recipes in order on one branch
refactor-pilot run fixtures/legacy_inventory --recipe r1,r6
```

```bash
# Everything, in the recommended order (std,r1,r6,r2,r3,r4,r5); needs an API key for r2-r5
refactor-pilot run fixtures/legacy_inventory --recipe all

# The whole pipeline from a recording: no network, no key, 32/32
refactor-pilot run fixtures/legacy_inventory --recipe std,r2,r3,r4,r5,r1,r6 --allow-api-changes \
    --flag INVENTORY_ENABLE_LEGACY_PRICING=off --replay examples/replays/full-fixture-api.jsonl \
    --manifest fixtures/legacy_inventory/manifest.json
```

The model-written recipes (R2–R5) share one engine. It finds targets
deterministically (regex plus `clangd`), asks the model for
one edit per target as SEARCH/REPLACE blocks, and checks the answer four ways:
the edit must apply exactly, the legacy pattern must be gone, public signatures
must be unchanged (unless `--allow-api-changes`), and the full gate including
sanitizers must pass. A rejected attempt is rolled back and its reason is sent
back to the model for the next attempt.

```bash
# Benchmark: run pipelines over the repositories listed in a config and write a report
refactor-pilot bench bench/targets.json
```

```bash
# Serve the navigation and verified-change tools to an MCP client (stdio)
refactor-pilot mcp path/to/project
```

Target repos must be CMake projects, and `apply`, `run` and `mcp` need them to be in a git repository. Build trees go to the system temp directory unless `--work-dir` is given.

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

## MCP server

`refactor-pilot mcp <repo>` exposes seven tools to any agent that speaks the
Model Context Protocol:

| Tool | What it gives an agent |
|---|---|
| `find_symbol`, `references`, `outline`, `diagnostics` | Semantic navigation through `clangd` instead of text search |
| `check_patch` | Propose a diff; get back either the branch it was committed to, or the compiler errors and failing-test output. The agent's working tree is never modified |
| `run_recipes` | Run `std`, `r1`, `r6` on a new branch |
| `baseline` | Build and test the repository as it stands |

Register it with a client as a stdio server, for example:

```json
{"mcpServers": {"refactor-pilot": {"command": "refactor-pilot", "args": ["mcp", "/absolute/path/to/project"]}}}
```

It implements the handshake-based protocol revisions (2024-11-05 to 2025-11-25).

## LLM provider

Recipes R2–R5 use a model. It talks to any **OpenAI-compatible chat-completions
endpoint**, so the provider is configuration, not code:

```bash
export REFACTOR_PILOT_API_KEY="..."                       # required
export REFACTOR_PILOT_MODEL="gemini-3.8-flash"            # default
export REFACTOR_PILOT_BASE_URL="https://generativelanguage.googleapis.com/v1beta/openai/"   # default
export REFACTOR_PILOT_RPM="10"                            # requests per minute, default 10
```

The defaults target the **free tier of the Google Gemini API**. Groq, OpenRouter
or a local server work by changing the three variables. Free tiers are
rate-limited and change often, so the client throttles itself, backs off on HTTP
429, and stops at a hard request budget (`--max-requests`, default 40).

**A stronger model is expected to give better results.** The model writes only
the judgement-heavy edits, and every edit must pass the same checks and gates; a
rejected one is rolled back and reported. So model quality mainly changes *how
many targets are resolved and how many attempts they take*, not whether the
output is safe to merge. Pointing the variables at a stronger paid model (a
Claude, GPT or Gemini Pro-class model) should raise the share resolved,
especially when API changes are allowed. The Phase 7 benchmark reports the score
per model so that this is measured rather than assumed.

**Privacy:** some free tiers may use your prompts to improve their products. R2
sends the source of the files it is editing. Use free tiers on open-source code
only; use a paid tier for anything private.

## Layout

```
src/refactor_pilot/
  toolchain.py     run external tools
  diagnostics.py   parse compiler / clang-tidy output
  build.py         configure, build, ctest, clang-tidy (out-of-tree)
  baseline.py      snapshots and the regression comparison
  textio.py        file I/O that preserves line endings and non-UTF-8 bytes
  pipeline.py      run a list of recipes on one branch and measure the result
  bench.py         benchmark over several pinned repositories, Markdown + JSON report
  mcp_server.py    Model Context Protocol server over stdio
  checkpoint.py    private git worktree: commit, rollback, cherry-pick
  gates.py         fast gate and full gate, with feedback text for retries
  session.py       executor loop: apply -> verify -> commit or roll back; culprit search
  recipes/         std_raise.py, r1_mechanical.py (clang-tidy), r6_dead_code.py (tiers),
                   llm_engine.py (shared by r2-r5: kinds, planning, prompts, checks)
  preproc.py       text-level helpers: #if groups, brace matching, constant folding
  llm/             chat client: budget, backoff, record, replay
  edits.py         SEARCH/REPLACE edit format: parse and apply exactly
  lsp/             JSON-RPC framing, position conversion, clangd client
  navigator.py     find / outline / references / signature / callers / live diagnostics
  evalkit/         manifests and scoring
  cli.py           baseline | check | score | symbols | refs | apply | run | bench | mcp
examples/patches/            three good and three bad patches for the fixture
examples/replays/            recorded model answers for R2-R5 on the fixture
bench/targets.json           benchmark targets (approved and proposed)
.github/workflows/ci.yml     this project's own tests
examples/github/             a workflow other projects can copy to get modernization PRs
docs/                        one walkthrough guide per phase
fixtures/legacy_inventory/   benchmark target
tests/                       unit tests + end-to-end tests with injected bugs
```
