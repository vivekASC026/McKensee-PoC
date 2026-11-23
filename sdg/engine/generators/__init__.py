"""Generator library.

Importing this package registers every generator. The engine only ever reaches
generators through `get_generator`.
"""

from sdg.engine.generators import (  # noqa: F401  (imported for the side effect)
    constants,
    dates,
    expression,
    lookup,
    number,
    pattern,
    reference,
    sequence,
    text,
)
from sdg.engine.generators.base import (  # noqa: F401
    DeferredValue,
    GenerationError,
    RowContext,
    algorithm,
    algorithm_versions,
    get_generator,
    pinned,
    register_algorithm,
    registered_names,
)
from sdg.engine.generators.constants import SKIPPED, Skipped  # noqa: F401

__all__ = [
    "DeferredValue",
    "GenerationError",
    "RowContext",
    "SKIPPED",
    "Skipped",
    "algorithm",
    "algorithm_versions",
    "pinned",
    "register_algorithm",
    "get_generator",
    "registered_names",
]
