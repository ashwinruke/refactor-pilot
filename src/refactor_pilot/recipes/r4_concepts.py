"""R4: SFINAE to C++20 concepts. Runs on the shared engine.

At C++20, clang-tidy's modernize-use-constraints (part of R1) rewrites most
enable_if constraints mechanically. This recipe is for what that leaves behind.
"""

from .llm_engine import RECIPE_KINDS, run  # noqa: F401

KINDS = RECIPE_KINDS["r4"]
