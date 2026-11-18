# refactor-pilot — Phase 5 guide: recipe R6, dead-code pruning

Phase 5 adds the dead-code pruner. It is deterministic: no LLM, no API key.

On the fixture: **R6 0/6 → 4/6** by default, **6/6** when you declare the stale
flag and allow API removal.

Tested on both toolchains (CMake 3.28 / clang-tidy 18 and CMake 4.4 / Ninja 1.13 /
clang-tidy 21), with `clangd` 21.1.8: **127 tests pass** on each.

**Apply this after Phase 4.** Every command can be pasted as it is, except the
one `cp` line.

---

## The tiers

Removing code is only safe when you can prove it is dead. R6 sorts what it finds
by how strong that proof is.

| Tier | What | Proof | Action |
|---|---|---|---|
| 1 | `#if 0` blocks (and the dead side of `#if 1`) | The preprocessor already drops them | Removed |
| 1 | Branches of a flag **you declare** stale | Your `--flag NAME=off`, then the tests | Removed |
| 1 | `if (kFlag) {…} else {…}` on a file-local `const bool` | The value is a compile-time constant no other file can see | Folded to the live branch; the constant is removed when nothing uses it |
| 1 | Unused `#include` lines | `clangd`'s include analysis, then the build | Removed |
| 1 | Unused internal-linkage functions | The compiler's own `-Wunused-function` | Removed, with the comment above them |
| 2 | Functions declared in a header that nothing in the repo references | `clangd` finds only the declaration and definition | **Reported.** Removed only with `--allow-api-changes` |

Why tier 2 is not automatic: "no references in this repository" cannot see
callers in other repositories, other build configurations, or code that finds
functions by name at run time. A person has to decide.

Why flags are never guessed: a macro that this build does not define may be a
platform macro, or set by a configuration you are not building. R6 lists such
macros as hints and acts only on the ones you name.

Every removal is one change-set through the same gates as any other edit. A
wrong removal breaks the build or a test and is rolled back.

---

## Files in this zip

| File | Status | What it is |
|---|---|---|
| `src/refactor_pilot/preproc.py` | new | Text-level helpers: find and resolve `#if` groups, brace matching, folding `if` on a constant, removing lines with their comment |
| `src/refactor_pilot/recipes/r6_dead_code.py` | new | The R6 recipe and its tiers |
| `src/refactor_pilot/navigator.py` | changed | `Symbol.start_line`: the first line of a whole declaration, needed to delete a function cleanly |
| `src/refactor_pilot/cli.py` | changed | `--recipe` now takes a comma-separated list (`r1,r6`); new `--flag NAME=on|off`; `--allow-api-changes` also applies to R6 |
| `src/refactor_pilot/__init__.py`, `pyproject.toml` | changed | Version 0.4.0 → 0.5.0 |
| `README.md` | changed | Status, R6 commands, layout, roadmap tick |
| `tests/test_preproc.py` | new | 14 unit tests of the text helpers |
| `tests/test_r6_integration.py` | new | 6 end-to-end tests |
| `docs/PHASE5_GUIDE.md` | new | This guide |

No fixture files changed. No new dependencies. No new setup.

---

## Apply the update

```bash
cd ~/projects/refactor-pilot
git status --short
```

This must print nothing. If it lists files: `git add -A && git commit -m "Phase 4"`.

Fill in your Windows user folder (`ls /mnt/c/Users` lists them):

```bash
cp /mnt/c/Users/YOUR_WINDOWS_USER/Downloads/refactor-pilot-phase5.zip ~/projects/
```

```bash
unzip -o ~/projects/refactor-pilot-phase5.zip -d ~/projects
git status --short
source .venv/bin/activate
pip install -e ".[dev]"
```

Expected from `git status --short`: 5 modified (`M`) and 5 new (`??`) entries.

## Verify

**1. Tests**

```bash
pytest
```

Expected: `127 passed`.

```bash
git add -A && git commit -m "Phase 5: recipe R6, dead-code pruning"
```

**2. Default run: tier 1 removed, tier 2 reported**

```bash
refactor-pilot run fixtures/legacy_inventory --recipe r6 --manifest fixtures/legacy_inventory/manifest.json --branch r6-demo
```

Expected ending:

```
committed   ........  r6/if-literal/src/pricing.cpp: tier 1: remove 1 `#if 0`/`#if 1` block(s) in src/pricing.cpp
committed   ........  r6/constant-flag/src/pricing.cpp:kUseTieredDiscounts: tier 1: fold 1 `if (kUseTieredDiscounts)` (always true) in src/pricing.cpp
committed   ........  r6/unused-include/src/pricing.cpp: tier 1: remove 1 unused #include(s) in src/pricing.cpp
committed   ........  r6/unused-function/src/pricing.cpp:inventory::legacy_round_cents: tier 1: remove unused internal function ...
skipped     --------  r6/unreferenced-public/inventory::legacy_flat_tax: tier 2: inventory::legacy_flat_tax is declared in a header but referenced nowhere in the repository
                      reason: may have callers outside this repository; remove with --allow-api-changes

4 committed, 0 rolled back, 0 reverted, 0 unchanged, 1 skipped; ...

flag never defined in this build: INVENTORY_ENABLE_LEGACY_PRICING  (to remove its branches: --flag INVENTORY_ENABLE_LEGACY_PRICING=off)
manifest score: 0/32 -> 4/32 legacy constructs resolved
  ...
  R6: 0/6 -> 4/6
```

Read the result:

```bash
git diff HEAD r6-demo -- fixtures/legacy_inventory/src/pricing.cpp
```

`bulk_price` shrinks from 16 lines of preprocessor and constant branching to two.

**3. Declare the flag stale and allow API removal**

```bash
refactor-pilot run fixtures/legacy_inventory --recipe r6 --flag INVENTORY_ENABLE_LEGACY_PRICING=off --allow-api-changes --manifest fixtures/legacy_inventory/manifest.json --branch r6-full
```

Expected: `6 committed, 0 rolled back` and `R6: 0/6 -> 6/6`.

```bash
git show r6-full:fixtures/legacy_inventory/src/pricing.cpp
git show r6-full:fixtures/legacy_inventory/include/inventory/pricing.h
```

The `#ifdef` is gone, and so is `legacy_flat_tax` with its comment, from both the
header and the source file.

**4. Declare the flag wrongly and watch the gate refuse**

Saying the flag is ON would switch the old pricing code in, which changes behaviour:

```bash
refactor-pilot run fixtures/legacy_inventory --recipe r6 --flag INVENTORY_ENABLE_LEGACY_PRICING=on --branch r6-wrong
```

Expected: one line `rolled_back ... r6/flag/INVENTORY_ENABLE_LEGACY_PRICING/src/pricing.cpp`
with `reason: tests: test now fails: pricing`. A wrong declaration costs nothing.

**5. Two recipes on one branch**

```bash
refactor-pilot run fixtures/legacy_inventory --recipe r1,r6 --flag INVENTORY_ENABLE_LEGACY_PRICING=off --manifest fixtures/legacy_inventory/manifest.json --branch r1-r6
```

Expected: `R1: 0/11 -> 11/11` and `R6: 0/6 -> 5/6`, with the `r1/...` commits
first and the `r6/...` commits after them:

```bash
git log --oneline HEAD..r1-r6
```

**6. Clean up**

```bash
git branch -D r6-demo r6-full r6-wrong r1-r6
```

---

## Using it on your own code

```bash
refactor-pilot run path/to/project --recipe r6
```

Read the `flag never defined in this build:` lines it prints. For each one you
know to be dead, rerun with `--flag NAME=off` (or `=on` if the feature shipped
and the old path is the dead one). Repeat `--flag` for several flags.

## Known limits

- **Only plain preprocessor forms.** `#ifdef X`, `#ifndef X`, `#if defined(X)`, `#if !defined(X)`, `#if 0`, `#if 1`. Compound conditions (`#if defined(A) && B > 2`) and `#elif` chains are left alone.
- **Only plain constant `if`s.** The condition must be exactly the constant or its negation, with braced branches. `else if` chains and `kFlag && x` are left alone.
- **Constants in headers are not folded.** Other files can see them, so "this file only" cannot be shown.
- **Tier 2 covers free functions only.** Unused methods, classes and types are not detected yet; virtual dispatch and templates make "unreferenced" much weaker evidence for them.
- **One build configuration.** Code used only on another platform looks dead from here. Tier 1 items are still safe (the compiler said so for this build), but run R6 per configuration before trusting tier 2.
- **Test files are never touched.**

## Troubleshooting

| Symptom | Fix |
|---|---|
| `error: --flag expects NAME=on or NAME=off` | Use exactly that form, e.g. `--flag FOO=off` |
| `error: unknown recipe` | Recipes are `r1`, `r2`, `r6`, comma-separated without spaces |
| An unused-include removal is rolled back | Another file relied on that include indirectly. The rollback is the correct result |
| Nothing found on your project | Run `refactor-pilot baseline` first to confirm it builds, then check the warning flags include `-Wall` |

## What to send back to me

1. The last line of `pytest` (expected `127 passed`).
2. The `N committed, ...` line and the `R6:` line from verify steps 2 and 3.
3. Whether step 4 showed the `rolled_back` line.
