"""Producing a value from a rule, for a file.

A file, an EDI transaction or a layout is written from the rule repository.
The values are made by the engine's own generators -- the same algorithms a
database load runs -- read from each rule by `sdg.engine.rules`. There is no
second implementation here.

Deterministic, like everything else. The same rule, the same seed and the same
row index give the same value on any machine and any day.

Where a generated row already holds the value -- because a database load
produced it and the file should describe the same people -- that is used
instead. A rule is how a value is made when nobody has made one yet, not a
reason to invent a second answer.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from sdg.engine.generators import GenerationError
from sdg.engine.rules import SUM_PREFIX, RuleError, run, summed

__all__ = ["SUM_PREFIX", "produce", "summed"]

log = logging.getLogger(__name__)

# (field, error type) already logged, so a rule that fails on every row of a
# file says so once rather than once per row. Bounded: it is a log throttle.
_logged: set[tuple[str, str]] = set()
_LOGGED_MAX = 1000


def produce(
    rule: dict[str, Any],
    *,
    seed: str,
    row_index: int,
    record: dict[str, Any] | None = None,
    as_of: str = "2026-06-30",
    context: dict[str, Any] | None = None,
) -> str:
    """One value for one rule, for one row.

    `record` is a generated row, where there is one; a value it holds wins.
    `context` is what the record holds so far, which a combination agrees with
    and a template is filled from.
    """
    field = str(rule.get("element") or rule.get("field_name") or "").strip().upper()
    if record and field and record.get(field) not in (None, ""):
        return _as_text(record[field])
    try:
        return run({**rule, "as_of": as_of}, seed=seed, row_index=row_index,
                   field=field or "?", context={**(context or {}), **(record or {})})
    except (RuleError, GenerationError) as exc:
        # A rule that cannot make a value for this row -- a template naming a
        # field the row lacks, a list the store does not have -- leaves the
        # field blank; the Rules screen's sample says why. The log says so
        # too, so a blank column in a file is never a silent one. Not the
        # message: some quote the row value they could not use.
        key = (field or "?", type(exc).__name__)
        if key not in _logged and len(_logged) < _LOGGED_MAX:
            _logged.add(key)
            log.warning("The rule for %s could not make a value (%s), so the field "
                        "is left blank. Check the rule's sample on the Rules screen.",
                        key[0], key[1])
        return ""


def _as_text(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()
