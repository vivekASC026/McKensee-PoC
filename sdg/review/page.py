"""Build the static review page.

One HTML file, no server and no build step, so it can be opened from a
directory or emailed to whoever is approving the rules. Everything the page
shows comes from the artifacts the earlier steps wrote.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from sdg.catalog.model import Catalog
from sdg.discovery.model import PhysicalModel
from sdg.mapping.scorer import MappingStore
from sdg.plan.proposer import Proposal
from sdg.profiling.model import ColumnProfile, ProfileReport
from sdg.review.queue import QueueItem

TEMPLATE_DIR = Path(__file__).parent
TEMPLATE_NAME = "page.html.j2"

# A params block with six hundred postal codes in it helps nobody.
PARAM_VALUE_PREVIEW = 8
PARAM_TEXT_LIMIT = 400


def summarize_params(params: dict[str, Any]) -> str:
    """One line per parameter, with long value lists cut down to a sample.

    Lists stay on their own line rather than expanding to one entry per row: a
    lookup over twenty states should read as a rule, not as a column of text.
    """
    if not params:
        return ""
    lines: list[str] = []
    for key, value in params.items():
        if isinstance(value, list):
            shown = value[:PARAM_VALUE_PREVIEW]
            rendered = ", ".join(_short(v) for v in shown)
            if len(value) > PARAM_VALUE_PREVIEW:
                rendered += f", ... {len(value) - PARAM_VALUE_PREVIEW:,} more"
            lines.append(f"{key}: [{rendered}]")
        else:
            lines.append(f"{key}: {_short(value)}")
    text = "\n".join(lines)
    if len(text) > PARAM_TEXT_LIMIT:
        text = text[:PARAM_TEXT_LIMIT].rstrip() + " ..."
    return text


def _short(value: Any) -> str:
    """A compact rendering of one parameter value."""
    if isinstance(value, dict):
        inner = ", ".join(f"{k}={v}" for k, v in list(value.items())[:3])
        return "{" + inner + ("..." if len(value) > 3 else "") + "}"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, float):
        return f"{value:g}"
    return json.dumps(value, default=str)


def _format_measure(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.4g}"
    if isinstance(value, int):
        return f"{value:,}"
    text = str(value)
    return text if len(text) <= 40 else text[:37] + "..."


def column_measures(profile: ColumnProfile | None) -> list[tuple[str, str]]:
    if profile is None:
        return [("profiled", "no")]
    measures: list[tuple[str, str]] = [
        ("rows", _format_measure(profile.row_count)),
        ("nulls", f"{profile.null_rate:.1%}"),
        ("distinct", f"{profile.distinct_count:,}"
                     + (" (approx)" if profile.approximate_distinct else "")),
        ("cardinality", profile.cardinality),
    ]
    if profile.minimum is not None or profile.maximum is not None:
        measures.append(
            ("range", f"{_format_measure(profile.minimum)} to {_format_measure(profile.maximum)}")
        )
    if profile.mean is not None:
        measures.append(("mean", _format_measure(profile.mean)))
        if profile.stddev is not None:
            measures.append(("std dev", _format_measure(profile.stddev)))
    if profile.min_length is not None:
        measures.append(("length", f"{profile.min_length} to {profile.max_length}"))
    if profile.fitted_format:
        measures.append(
            ("format", f"{profile.fitted_format['regex']} "
                       f"({profile.fitted_format['coverage']:.0%})")
        )
    if profile.check_digit:
        measures.append(
            ("check digit", f"{profile.check_digit['scheme']} "
                            f"({profile.check_digit['share']:.0%})")
        )
    if profile.sequence and profile.sequence.get("is_sequence"):
        measures.append(
            ("sequence", f"step {profile.sequence['step']}, max {profile.sequence['max']:,}")
        )
    if profile.distribution:
        measures.append(("distribution", profile.distribution["kind"]))
    return measures


def build_context(
    model: PhysicalModel,
    report: ProfileReport | None,
    mappings: MappingStore,
    proposals: list[Proposal],
    queue: list[QueueItem],
    catalog: Catalog,
    *,
    decisions_path: str = "work/review-decisions.json",
) -> dict[str, Any]:
    by_column = {p.qualified: p for p in proposals}
    stats = mappings.statistics()

    tables = []
    for table in model.tables:
        columns = []
        for column in table.columns:
            qualified = f"{table.name}.{column.name}"
            profile = report.column(table.name, column.name) if report else None
            mapping = mappings.for_column(table.name, column.name)
            proposal = by_column.get(qualified)
            columns.append(
                {
                    "name": column.name,
                    "type": column.type_description(),
                    "nullable": column.nullable,
                    "bucket": column.bucket or "free",
                    "measures": column_measures(profile),
                    "top_values": [
                        (str(t.value).rstrip() or "(blank)", f"{t.share:.0%}")
                        for t in (profile.top_values or [])[:10]
                    ] if profile else [],
                    "element": mapping.element if mapping else None,
                    "mapping_confidence": mapping.confidence if mapping else None,
                    "generator": proposal.generator if proposal else "-",
                    "proposal_confidence": proposal.confidence if proposal else None,
                    "coverage": f"{proposal.coverage:.0%}"
                    if proposal and proposal.coverage is not None else None,
                    "params_preview": summarize_params(proposal.params) if proposal else "",
                    "evidence": (proposal.evidence if proposal else [])
                    + (mapping.evidence if mapping else []),
                    "note": proposal.note if proposal else "",
                }
            )
        tables.append(
            {
                "name": table.name,
                "row_count": table.row_count,
                "triggers": table.triggers,
                "columns": columns,
            }
        )

    used_counts: dict[str, int] = {}
    for mapping in mappings.mappings:
        if mapping.element:
            used_counts[mapping.element] = used_counts.get(mapping.element, 0) + 1

    elements = [
        {
            "name": element.name,
            "domain": element.domain,
            "rule": element.rule,
            "format": element.format,
            "pii": element.pii,
            "aliases": ", ".join(element.aliases[:5]),
            "used_by": used_counts.get(element.name, 0),
        }
        for element in catalog.elements
    ]

    for item in queue:
        item.params_preview = summarize_params(item.params)  # type: ignore[attr-defined]

    return {
        "title": "Synthetic data generation — review",
        "subtitle": (
            f"{model.connection.get('database', 'source')} on "
            f"{model.connection.get('server', 'this server')} · "
            f"{catalog.organization or 'catalog'} v{catalog.version}"
        ),
        "stats": [
            {"value": f"{stats['auto_map_rate']:.0%}", "label": "auto-mapped"},
            {"value": str(len(queue)), "label": "in the review queue"},
            {"value": str(len(model.tables)), "label": "tables"},
            {"value": str(sum(len(t.columns) for t in model.tables)), "label": "columns"},
            {"value": str(len(catalog.elements)), "label": "catalog elements"},
            {
                "value": str(sum(1 for p in proposals if p.confidence == "high")),
                "label": "rules proposed at high confidence",
            },
        ],
        "queue": queue,
        "ratios": report.ratios if report else [],
        "tables": tables,
        "elements": elements,
        "decisions_path": decisions_path,
    }


def render(context: dict[str, Any]) -> str:
    environment = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        # The template is page.html.j2: "html" alone left it unescaped, and
        # column names, values and evidence come from the database.
        autoescape=select_autoescape(["html", "html.j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return environment.get_template(TEMPLATE_NAME).render(**context)


def write_page(context: dict[str, Any], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(context), encoding="utf-8")
    return path
