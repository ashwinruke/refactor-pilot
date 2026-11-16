# refactor-pilot

An LSP-powered agent that modernizes legacy C++ (C++11/14 → C++20) in small,
verified, reversible change-sets, and delivers the result as a reviewable branch.

**Status:** The verification gates, git checkpoints, the executor
loop, the symbol navigator and the benchmark fixture exist. The tool can push
patches you give it through the gates; it does not yet write its own.

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

Target repos must be CMake projects, and `apply` needs them to be in a git repository. Build trees go to the system temp directory unless `--work-dir` is given.

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

## LLM provider (used from Phase 4)

Phases 0–3 use no LLM. From Phase 4, the agent talks to any **OpenAI-compatible
chat-completions endpoint**, so the provider is a config choice, not a code change:

```bash
export REFACTOR_PILOT_BASE_URL="https://generativelanguage.googleapis.com/v1beta/openai/"
export REFACTOR_PILOT_MODEL="<a model on your provider's free tier>"
export REFACTOR_PILOT_API_KEY="..."
```

The project is developed against **free API tiers** (Google Gemini via AI Studio as
the primary, Groq as the fallback; OpenRouter's free models also work). Free tiers
change often and are rate-limited per minute and per day, so the agent throttles,
backs off on HTTP 429, and stops at a configurable request/token budget. Check your
provider's own rate-limit page for current numbers.

**A stronger model is expected to give better results.** The model only handles the
judgement-heavy recipes (ownership, macros, SFINAE). Every edit it proposes must pass
the same verification gate, and an edit that fails is rolled back and reported as
skipped. So model quality mainly changes *how much gets migrated and how many retries
it takes*, not whether the output is safe to merge. Pointing the three variables above
at a stronger paid model (for example a Claude, GPT or Gemini Pro-class model) should
raise the share of constructs resolved, especially for R2. The Phase 7 benchmark
reports the score per model so this is measured rather than assumed.

**Privacy:** some free tiers may use your prompts to improve their products. Use free
tiers on open-source code only; use a paid tier for anything private.

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
  lsp/             JSON-RPC framing, position conversion, clangd client
  navigator.py     find / outline / references / signature / callers / live diagnostics
  evalkit/         manifests and scoring
  cli.py           baseline | check | score | symbols | refs | apply
examples/patches/            three good and three bad patches for the fixture
fixtures/legacy_inventory/   benchmark target
tests/                       unit tests + end-to-end tests with injected bugs
```
