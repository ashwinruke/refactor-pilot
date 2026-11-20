"""R3: macros to constexpr, unscoped enums to enum class. Runs on the shared engine."""

from .llm_engine import RECIPE_KINDS, run  # noqa: F401

KINDS = RECIPE_KINDS["r3"]
