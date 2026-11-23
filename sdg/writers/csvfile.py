"""CSV writer: one file per table.

Values are written in a stable textual form so two runs with the same seed
produce byte-identical files, which is how reproducibility is tested.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sdg.engine.execute import RunResult
from sdg.plan.model import Plan

NULL = ""


def format_value(value: Any) -> str:
    """One canonical text form per value, so output is byte-stable."""
    if value is None:
        return NULL
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        return repr(value)
    return str(value)


def write_table(
    path: Path,
    columns: list[str],
    rows: list[dict[str, Any]],
) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" and \n keep the files identical on every platform.
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow([format_value(row.get(column)) for column in columns])
    return len(rows)


def write_run(result: RunResult, plan: Plan, out_dir: str | Path) -> dict[str, Path]:
    """Write every generated table to `out_dir`. Returns the paths written."""
    out_dir = Path(out_dir)
    written: dict[str, Path] = {}
    for table in plan.tables_in_order():
        table_result = result.tables.get(table.name)
        if table_result is None:
            continue
        columns = [f.name for f in table.fields if f.generator != "skip"]
        path = out_dir / f"{table.name}.csv"
        write_table(path, columns, table_result.rows)
        written[table.name] = path
    return written
