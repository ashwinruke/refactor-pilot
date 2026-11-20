# refactor-pilot — Phase 6 guide: C++20, and recipes R3, R4, R5

Phase 6 completes the recipe set:

- **`std`** raises the project's language standard (C++11 → C++20 by default).
- **R1** gains the fixes that only exist at newer standards.
- **R3** (macros, enums), **R4** (SFINAE → concepts) and **R5** (C++20 library) are new, model-written recipes.

With everything together, the fixture goes from **0/32 to 32/32** planted
constructs resolved, in 43 commits that each passed the gates.

Tested on both toolchains (CMake 3.28 / clang-tidy 18 and CMake 4.4 / Ninja 1.13 /
clang-tidy 21): **143 tests pass** on each. As in Phase 4, all model behaviour in
the tests is scripted; no real model has run R3–R5.

**Apply this after Phase 5.** Every command can be pasted as it is, except the
one `cp` line.

---

## What changed in the design

### One engine for every model-written recipe

Phase 4's R2 code became a shared engine (`recipes/llm_engine.py`). A recipe is
now a list of *kinds*. A kind says how to find a legacy pattern, what to ask
for, what must be gone afterwards, the lowest standard the modern form needs,
and whether it can only be done by changing how other files use it.

| Recipe | Kind | Finds | Needs | Needs `--allow-api-changes` |
|---|---|---|---|---|
| r2 | `manual-lock` | `x.lock()` / `x.unlock()` | C++11 | no |
| r2 | `file-handle` | `fopen` / `fclose` / `FILE*` | C++11 | no |
| r2 | `raw-ownership` | `new` / `delete` | C++11 | only for factories |
| r3 | `macro` | `#define` of a value or expression | C++11 | no |
| r3 | `unscoped-enum` | `enum X {` | C++11 | yes |
| r4 | `sfinae` | `enable_if` | C++20 | no |
| r5 | `printf-buffer` | `snprintf` / `sprintf` | C++20 | no |
| r5 | `pointer-length` | `const T* p, size_t n` parameters | C++20 | yes |
| r5 | `prefix-compare` | `s.compare(0, n, p)` | C++20 | no |

The five checks from Phase 4 apply to every kind. A kind whose standard is not
met is skipped with the reason `needs C++20 ... Raise it first: --recipe std,...`.

### The `std` recipe and re-baselining

Raising the standard is a change-set like any other, with one difference. A
newer standard legitimately changes what the compiler warns about, so "no new
warnings" cannot apply. The rule for `std` is: the tree must build and every
test must pass, sanitizers included; the warnings are then recorded as the new
baseline for everything after it. If the code does not compile at the new
standard, `std` is rolled back and the later recipes run at the old one.

### Codemods before the model, again

At C++20, `clang-tidy` can do part of R4 and R5 by itself, so R1's check list
grew: `modernize-use-constraints` (`enable_if` → `requires`),
`modernize-use-starts-ends-with`, `modernize-type-traits`, `modernize-make-unique`
and a few more. They are silent at C++11 and take effect after `std`. The
model-written R4 and R5 handle what those leave behind. In the recommended order
(`all` = `std,r1,r6,r2,r3,r4,r5`) the cheap deterministic recipes run first, so
the model sees cleaner code and fewer targets.

### Two fixes found while building this

- **Several owners in one file.** The Phase 4 goal check looked at a whole file. With two classes in one file both using `delete`, fixing one was rejected because the other's `delete` was still there. The check now counts each target's own lines.
- **A rolled-back standard left the build tree behind.** CMake caches a `-D` value in the build directory, so after a failed `std` the tree kept compiling as C++20 and every later change-set failed. A test caught it; the override is now undone properly.

---

## Files in this zip

| File | Status | What it is |
|---|---|---|
| `src/refactor_pilot/recipes/llm_engine.py` | new | The shared engine: kinds, target finding, prompts, the checks, the loop. Most of it moved from `r2_ownership.py` |
| `src/refactor_pilot/recipes/r2_ownership.py` | changed | Now a thin module naming R2's kinds |
| `src/refactor_pilot/recipes/r3_macros.py`, `r4_concepts.py`, `r5_cxx20_library.py` | new | Thin modules naming each recipe's kinds |
| `src/refactor_pilot/recipes/std_raise.py` | new | The `std` recipe |
| `src/refactor_pilot/recipes/r1_mechanical.py` | changed | Seven more checks that need C++14–20 |
| `src/refactor_pilot/session.py` | changed | `ChangeSet.rebaseline` and the re-baselining path |
| `src/refactor_pilot/cli.py` | changed | Recipes `std`, `r3`, `r4`, `r5` and `all`; `--std`; `--plan` covers every model-written recipe |
| `src/refactor_pilot/__init__.py`, `pyproject.toml` | changed | Version 0.5.0 → 0.6.0; a `slow` test marker |
| `README.md` | changed | Recipe table, commands, layout, roadmap tick |
| `examples/replays/r345-fixture-internal.jsonl` | new | Scripted answers for R3–R5, default mode, with one deliberately wrong first attempt |
| `examples/replays/r345-fixture-api.jsonl` | new | The same with API changes |
| `examples/replays/full-fixture-api.jsonl` | new | Scripted answers for the whole pipeline |
| `tests/test_phase6_recipes.py` | new | 15 tests |
| `tests/test_r2_ownership.py` | changed | 2 tests updated or added for the several-owners fix |
| `docs/PHASE6_GUIDE.md` | new | This guide |

No fixture files changed. No new dependencies. No new setup.

---

# Part A — verify without an API key

## 1. Apply the update

```bash
cd ~/projects/refactor-pilot
git status --short
```

This must print nothing. If it lists files: `git add -A && git commit -m "Phase 5"`.

Fill in your Windows user folder (`ls /mnt/c/Users` lists them):

```bash
cp /mnt/c/Users/YOUR_WINDOWS_USER/Downloads/refactor-pilot-phase6.zip ~/projects/
```

```bash
unzip -o ~/projects/refactor-pilot-phase6.zip -d ~/projects
git status --short
source .venv/bin/activate
pip install -e ".[dev]"
```

Expected from `git status --short`: 8 modified (`M`) and 10 new (`??`) entries.

## 2. Tests

```bash
pytest
```

Expected: `143 passed`. The suite is long now (about 10 minutes here, more on
your machine). For a quicker run that skips the whole-pipeline test:

```bash
pytest -m "not slow"
```

Expected from that: `142 passed, 1 deselected`.

```bash
git add -A && git commit -m "Phase 6: std, R3, R4, R5"
```

## 3. Raise the standard and let clang-tidy do R4 (no model)

```bash
refactor-pilot run fixtures/legacy_inventory --recipe std,r1 --manifest fixtures/legacy_inventory/manifest.json --branch p6-std
```

Expected, among the output:

```
  committed   std/raise
  now compiling as C++20; no change in warnings; this is the new baseline
  ...
  committed   r1/use-constraints/src/item.cpp
  committed   r1/type-traits/src/item.cpp
...
  R1: 0/11 -> 11/11
  R4: 0/1 -> 1/1
```

```bash
git diff HEAD p6-std -- fixtures/legacy_inventory/include/inventory/util.h
```

The two `enable_if` overloads now use `requires`, with no model involved. (The
"clang-tidy modernize findings" line compares a C++11 count with a C++20 count,
so it can go up as well as down in this run.)

## 4. See what R3–R5 would work on

```bash
refactor-pilot run fixtures/legacy_inventory --recipe r3,r4,r5 --plan --allow-api-changes
```

Expected: `8 target(s); standard: C++11`, with the six C++20 targets marked
`[skipped: needs C++20; add the std recipe]`. The plan shows the tree as it is
now, before `std` has run.

## 5. Replay R3–R5, default mode

```bash
refactor-pilot run fixtures/legacy_inventory --recipe std,r3,r4,r5 --replay examples/replays/r345-fixture-internal.jsonl --manifest fixtures/legacy_inventory/manifest.json --branch p6-internal
```

Expected:

```
    attempt 1/3 of r5/printf-buffer/inventory::Item rejected: tests: test now fails: item
...
6 committed, 0 rolled back, 0 reverted, 0 unchanged, 3 skipped; ...
  R3: 0/3 -> 2/3
  R4: 0/1 -> 1/1
  R5: 0/3 -> 2/3
```

The rejected attempt is scripted: it turns `%.2f` into `{}` instead of `{:.2f}`,
so a price prints as `2` instead of `2.00`. The test catches it, the model is
sent the failing check, and the second attempt is right.

The three skipped targets need `--allow-api-changes`.

```bash
git diff HEAD p6-internal -- fixtures/legacy_inventory/include/inventory/util.h fixtures/legacy_inventory/src/item.cpp
```

## 6. Replay R3–R5 with API changes

```bash
refactor-pilot run fixtures/legacy_inventory --recipe std,r3,r4,r5 --allow-api-changes --replay examples/replays/r345-fixture-api.jsonl --manifest fixtures/legacy_inventory/manifest.json --branch p6-api
```

Expected: `9 committed, 0 rolled back`, `R3: 0/3 -> 3/3`, `R5: 0/3 -> 3/3`.
`Category` is now an `enum class`, and `sum` and `variance` take a `std::span`.

## 7. The whole pipeline: 32 of 32

```bash
refactor-pilot run fixtures/legacy_inventory --recipe std,r2,r3,r4,r5,r1,r6 --allow-api-changes --flag INVENTORY_ENABLE_LEGACY_PRICING=off --replay examples/replays/full-fixture-api.jsonl --manifest fixtures/legacy_inventory/manifest.json --branch p6-full
```

This takes several minutes. Expected ending:

```
43 committed, 0 rolled back, 0 reverted, 1 unchanged; 19 full-gate runs; ...
...
manifest score: 0/32 -> 32/32 legacy constructs resolved
  R1: 0/11 -> 11/11
  R2: 0/8 -> 8/8
  R3: 0/3 -> 3/3
  R4: 0/1 -> 1/1
  R5: 0/3 -> 3/3
  R6: 0/6 -> 6/6
```

Why this order and not `all`: a recording is tied to the exact text it was made
against. These answers were written against the original files, so the model
recipes run first here and the deterministic ones clean up after them. With a
live model, use `all`.

Read the before and after of one file:

```bash
git show HEAD:fixtures/legacy_inventory/src/warehouse.cpp
git show p6-full:fixtures/legacy_inventory/src/warehouse.cpp
```

And the history that got there:

```bash
git log --oneline HEAD..p6-full
```

## 8. Clean up

```bash
git branch -D p6-std p6-internal p6-api p6-full
```

---

# Part B — live model

This needs the API key from the Phase 4 guide (`source ~/.refactor-pilot.env`).

## 9. Everything, live

```bash
refactor-pilot run fixtures/legacy_inventory --recipe all --flag INVENTORY_ENABLE_LEGACY_PRICING=off --manifest fixtures/legacy_inventory/manifest.json --branch p6-live --record ~/p6-live.jsonl
```

- About 6–8 model targets × up to 3 attempts, so at most 24 requests, paced at 10 per minute. The default budget of 40 covers it.
- Expect a score between 20/32 and 28/32: R1 and most of R6 are deterministic (16–17 constructs), and the rest depends on the model.
- Add `--allow-api-changes` for the harder version, where 32/32 is possible.

```bash
git log --oneline HEAD..p6-live
git branch -D p6-live
```

---

## Known limits

- **R3–R5 are untested against a real model**, like R2. Send me recordings.
- **`std::format` does not truncate.** The old code cut text off at the buffer size; `std::format` returns the whole string. That is usually the fix you want, but it is a behaviour change for over-long input, and the model is told to keep output identical only for input that fitted.
- **Macro replacement keeps the macro's name.** `INVENTORY_MAX_ORDER` becomes a `constexpr` variable with the same shouty name, so that no caller has to change. Renaming is a separate decision.
- **Macros that must stay macros are not targets**: include guards, empty flags, anything tested in `#if`, anything using `#`, `##`, `__LINE__`, statements or line continuations.
- **`std` edits CMake files with patterns.** It handles `CMAKE_CXX_STANDARD`, `CXX_STANDARD`, `cxx_std_NN` and `-std=` flags. A standard set some other way is not found; `std` then adds `set(CMAKE_CXX_STANDARD 20)` after `project()`.
- **Existing build directories keep the old standard** if the project stores it as a CMake cache variable (the fixture does). After merging a branch from `std`, configure a fresh build directory.
- **Raising the standard can surface new warnings** in real code. `std` accepts them and reports how many; later recipes must not add more.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `needs C++20; the project compiles as C++11` | Put `std` before that recipe: `--recipe std,r4,r5` |
| `rolled_back std/raise ... compile:` | The code is not valid at the new standard (the reason names the first error). Fix that by hand, or try `--std 17` |
| `no recorded response for '...'` | The recording does not match this recipe order or these files. Use the exact command from the guide |
| `<format>` not found | Your standard library predates C++20 `<format>` (GCC before 13). R5's `printf-buffer` targets will be rolled back; everything else still works |

## What to send back to me

1. The last line of `pytest` (expected `143 passed`).
2. The score blocks from steps 5, 6 and 7.
3. From Part B: the full output of step 9, and the recording:

```bash
cat ~/p6-live.jsonl
```
