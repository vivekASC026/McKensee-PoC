"""Constants and column definitions for the pattern analyzer.

The analyzer arrived with its own hard-coded server and database. It now reads
the same connection the rest of the tool uses, so there is one place to say
where the data is and one set of credentials to keep out of artifacts.

The analyzer's tables live in the `sdm` schema of that same database, beside
the `dbo` FACETS tables it describes. That is deliberate: a pattern found here
is about the rows the generator writes, not about a separate copy of them.
"""

from __future__ import annotations

import os

from sdg.config import get_settings
from sdg.packs import held

# Where the analyzer's own tables live: patterns, layouts, generation rules,
# the denormalized member and claims views.
SQL_SCHEMA = get_settings().denorm_schema

# Ages and tenures are measured from a stated date, never from the clock, so a
# pattern found today still means the same thing when it is replayed.
DEFAULT_REFERENCE_DATE = os.getenv("SDG_ANALYZER_AS_OF", "2026-01-01")


def get_connection_string() -> str:
    """ODBC string for the analysis store.

    Every query the analyzer makes resolves through here, which is why the
    store could be moved out of the customer's database in one change. It
    reads the denormalized extracts and writes the tool's own tables; it never
    touches a source. Extracts are what cross between the two, and they go
    through the connection registry rather than this.
    """
    from sdg.config import refuse_protected

    settings = get_settings()
    # The store is never production, however .env names it.
    refuse_protected(settings.denorm_db, "connect to")
    return settings.store.connection_string(settings.denorm_db)


def describe_connection() -> dict[str, str]:
    """Server and database, with no credentials in it. For the health endpoint."""
    settings = get_settings()
    return {
        "server": settings.store.server,
        "database": settings.denorm_db,
        "schema": SQL_SCHEMA,
        "driver": settings.store.driver,
    }


# ── Pattern analysis features ────────────────────────────────────────────────

CONTINUOUS_FEATURES = held("features:analyzer.continuous_features")
# Where a pattern's members live: the columns of the member extract holding an
# address, and the element each one is. Carried with a pattern as whole
# records, so a ZIP and its city are always drawn together.
GEOGRAPHY_COLUMNS = held("features:analyzer.geography_columns")
CATEGORICAL_FEATURES = held("features:analyzer.categorical_features")

DESCRIPTION_COLUMNS = held("features:analyzer.description_columns")

FILTER_COLUMNS = held("features:analyzer.filter_columns")

CONTINUOUS_WEIGHT = 2.0

LABEL_CLUSTER_COLUMNS = held("features:analyzer.label_cluster_columns")
