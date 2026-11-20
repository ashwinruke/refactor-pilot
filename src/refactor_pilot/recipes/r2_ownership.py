"""R2: ownership and RAII (manual lock/unlock, FILE*, raw owning pointers).

The machinery is shared with the other model-written recipes and lives in
llm_engine; this module names R2's part of it.
"""

from .llm_engine import (  # noqa: F401  (re-exported)
    HEADER_SUFFIXES, KINDS as ALL_KINDS, RECIPE_KINDS, RecipeResult as R2Result, Target, _DELETE, _FILE_DECL, _LOCK,
    build_prompt, find_targets, hits, is_test_file, language_standard, make_accept, mask, public_signatures, run,
)

KINDS = RECIPE_KINDS["r2"]
