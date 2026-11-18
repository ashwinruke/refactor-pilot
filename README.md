# refactor-pilot

An LSP-powered agent that modernizes legacy C++ (C++11/14 → C++20) in small,
verified, reversible change-sets, and delivers the result as a reviewable branch.

**Status:** Three recipes work end to end:

- **R1, mechanical** (no LLM): `nullptr`, `override`, range-for, `using`, `= delete`, C++ casts, from `clang-tidy` fix-its.
- **R2, ownership** (LLM): manual `lock`/`unlock` → `lock_guard`, `FILE*` → RAII, raw owning pointers and `delete` → `unique_ptr`.
- **R6, dead code** (no LLM): `#if 0` blocks, stale flag branches, constant-folded `if`s, unused includes and unused internal functions are removed; unreferenced public functions are reported.

Every edit, whoever wrote it, must pass the same gates before it is committed.
Macros, SFINAE and C++20 library recipes come next.

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

R2 finds its targets deterministically (regex plus `clangd`), asks the model for
one edit per target as SEARCH/REPLACE blocks, and checks the answer four ways:
the edit must apply exactly, the legacy pattern must be gone, public signatures
must be unchanged (unless `--allow-api-changes`), and the full gate including
sanitizers must pass. A rejected attempt is rolled back and its reason is sent
back to the model for the next attempt.

Target repos must be CMake projects, and `apply` and `run` need them to be in a git repository. Build trees go to the system temp directory unless `--work-dir` is given.

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

## LLM provider

Only recipe R2 uses a model. It talks to any **OpenAI-compatible chat-completions
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
  checkpoint.py    private git worktree: commit, rollback, cherry-pick
  gates.py         fast gate and full gate, with feedback text for retries
  session.py       executor loop: apply -> verify -> commit or roll back; culprit search
  recipes/         r1_mechanical.py (clang-tidy fix-its), r2_ownership.py (LLM, RAII),
                   r6_dead_code.py (tiered pruning)
  preproc.py       text-level helpers: #if groups, brace matching, constant folding
  llm/             chat client: budget, backoff, record, replay
  edits.py         SEARCH/REPLACE edit format: parse and apply exactly
  lsp/             JSON-RPC framing, position conversion, clangd client
  navigator.py     find / outline / references / signature / callers / live diagnostics
  evalkit/         manifests and scoring
  cli.py           baseline | check | score | symbols | refs | apply | run
examples/patches/            three good and three bad patches for the fixture
examples/replays/            recorded model answers for R2 on the fixture
fixtures/legacy_inventory/   benchmark target
tests/                       unit tests + end-to-end tests with injected bugs
```