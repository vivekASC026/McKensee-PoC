"""Reading a source into the analysis store.

Pattern analysis does not read a source database. It reads a denormalized
store, centralized across every source, in a database of its own. An extract is
what gets data from one into the other.

That indirection is the point rather than an inconvenience:

    the source is read once     clustering a book means loading and joining it
                                repeatedly, and doing that against a customer's
                                live database is a cost they did not agree to
    analysis survives the source a store that is already extracted can be
                                analyzed when the source is offline, moved, or
                                gone
    sources sit side by side    one store, one shape, a `source` column. Two
                                books can be compared because they are the same
                                table
    nothing of ours lives there the tool's own tables -- saved patterns,
                                baselines, rules, layouts -- moved out of the
                                customer's database along with the store

A refresh is complete: every row for that source is removed and the source is
read again. No incremental merge, no extract history. The consequence is worth
stating because it is felt rather than seen -- a pattern found before a refresh
may describe rows that no longer exist, and the pattern screens report that as
a stale pattern rather than quietly showing nothing.
"""

from sdg.extract.run import (
    ExtractResult,
    extract,
    keys_from_manifest,
    sources,
)

__all__ = ["ExtractResult", "extract", "keys_from_manifest", "sources"]
