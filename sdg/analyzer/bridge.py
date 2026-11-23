"""Turn a pattern the analyzer found into rules the generator can run.

The analyzer describes a cluster statistically: age runs from 18 to 32, 54 per
cent of the members are female, two thirds are single, each subscriber carries
1.8 dependents. The generator wants rules -- a date window, a weighted list, a
children-per-parent ratio. This module is the translation between them, and it
is deliberately the only place that knows both vocabularies.

Three things it will not do, because each would quietly misrepresent the
finding:

    collapse a band to its mean   A cluster spanning 18 to 32 is not a
                                  population of 25-year-olds. Continuous
                                  features cross as bands.
    drop a distribution           A categorical split of 54/46 crosses as
                                  values with weights, not as the majority
                                  value.
    invent a prevalence           The share of the population a pattern covers
                                  is carried through as stated, never assumed.

What comes back is an `AnalyzerPattern` in the shape `sdg.plan.patterns`
already resolves, so everything downstream -- the element lookup, the subject
scoping, the bucket guards, the width checks -- applies unchanged.
"""

from __future__ import annotations

from typing import Any

from sdg.plan.patterns import AnalyzerPattern, PatternParameter
from sdg.packs import held

# What the analyzer calls a feature, and what a person calls it. The label is
# what the resolver matches against the catalog, so it is written the way the
# element is named rather than the way the dataframe column is.
FEATURE_LABELS: dict[str, tuple[str, str | None]] = {
    k: tuple(v) for k, v in held("features:analyzer.feature_labels").items()}

# Ranked code lists a claims pattern carries beside its clustered features.
# They are distributions in everything but name: a mapping of code to how often
# it appeared, which is exactly what a weighted lookup wants.
# The element each ranked list or share is about, so one a combination group
# already draws is not drawn a second time on its own.
FEATURE_ELEMENTS: dict[str, str] = held("features:bridge.feature_elements")

RANKED_FEATURES: dict[str, str] = held("features:bridge.ranked")

# Below this share a category is noise in the cluster rather than part of what
# the pattern is, and carrying it through would widen the generated population
# past the one that was found.
MINIMUM_SHARE = 0.02


def _band(stats: dict[str, Any]) -> tuple[float, float] | None:
    """The range a continuous feature covers, preferring what was measured.

    A cluster's minimum and maximum are the honest bounds. Where only a mean
    and a standard deviation survive, one deviation either side holds about two
    thirds of the cluster, which is a narrower claim than the full range and a
    truer one than the mean alone.
    """
    low, high = stats.get("min"), stats.get("max")
    if low is not None and high is not None and float(high) >= float(low):
        return float(low), float(high)
    mean, deviation = stats.get("mean"), stats.get("std")
    if mean is None:
        return None
    spread = float(deviation or 0)
    return float(mean) - spread, float(mean) + spread


def _slices(stats: dict[str, Any], low: float, high: float) -> list[list[float]] | None:
    """The pattern's deciles as ten slices of equal share, inside its band.

    Slices of no width (many members of one age) are kept: the value they
    hold is drawn as often as the members have it.
    """
    cuts = [float(c) for c in (stats.get("deciles") or []) if c is not None]
    if len(cuts) < 2:
        return None
    cuts = [min(max(c, low), high) for c in cuts]
    share = 1.0 / (len(cuts) - 1)
    return [[round(a, 4), round(b, 4), round(share, 6)] for a, b in zip(cuts, cuts[1:])]


def _distribution(dist: dict[str, Any]) -> tuple[list[str], list[float]] | None:
    """A categorical split as values and weights, smallest shares dropped.

    Three shapes arrive here and all mean the same thing: `{pct: {...}}` from the
    member analyzer, `{counts: {...}}` where percentages were not computed, and
    a flat mapping of value to number from the claims analyzer and its ranked
    code lists. Whether the numbers are shares or counts does not matter --
    they are normalized either way.
    """
    shares = dist.get("pct") or {}
    if not shares:
        counts = dist.get("counts") or {}
        total = sum(counts.values()) or 1
        shares = {key: value / total for key, value in counts.items()}
    if not shares and dist and all(
        isinstance(value, (int, float)) for value in dist.values()
    ):
        total = sum(dist.values()) or 1
        shares = {key: value / total for key, value in dist.items()}
    kept = {
        str(key): float(share)
        for key, share in shares.items()
        if share is not None and float(share) >= MINIMUM_SHARE and str(key).strip()
    }
    if not kept:
        return None
    # Percentages sometimes arrive as 0-100 and sometimes as 0-1. Normalizing
    # here means the weights always read as proportions of one.
    total = sum(kept.values())
    if total <= 0:
        return None
    ordered = sorted(kept.items(), key=lambda item: (-item[1], item[0]))
    return [key for key, _ in ordered], [round(share / total, 4) for _, share in ordered]


def to_pattern(
    pattern: dict[str, Any],
    *,
    kind: str = "member",
    as_of: str | None = None,
    pattern_id: str | None = None,
) -> AnalyzerPattern:
    """One analyzer pattern, in the shape the plan resolver reads.

    `pattern` is a row of an analyze response: the model the API returns, or the
    same thing loaded back from `sdm.patterns`.
    """
    parameters: list[PatternParameter] = []

    for feature, stats in (pattern.get("continuous") or {}).items():
        label, unit = FEATURE_LABELS.get(feature, (feature, None))
        band = _band(stats or {})
        if band is None:
            continue
        low, high = band
        if unit in {"years", "months"} or label in {"Age", "Tenure"}:
            # A cluster centered near zero can have a band that runs below it.
            # An age of minus seven is not a narrower claim, it is a wrong one.
            low = max(0.0, low)
            high = max(low, high)
        parameters.append(
            PatternParameter(label=label, unit=unit, min=round(low), max=round(high),
                             bands=_slices(stats or {}, low, high))
        )

    groups = [g for g in (pattern.get("combinations") or [])
              if isinstance(g, dict) and g.get("records")]
    clinical = [r for r in (pattern.get("clinical") or []) if isinstance(r, dict)]
    if clinical and not groups:
        # Saved before every group was kept: its diagnosis and procedure pairs.
        groups = [{"elements": sorted({k for r in clinical for k in r if k != "share"}),
                   "records": clinical}]
    covered = {e for g in groups for e in g.get("elements") or []}
    for feature, ranked in RANKED_FEATURES.items():
        if FEATURE_ELEMENTS.get(feature) in covered:
            # Drawn as whole combinations below, never as a list apart.
            continue
        values = pattern.get(feature)
        if not isinstance(values, dict) or not values:
            continue
        split = _distribution(values)
        if split is None:
            continue
        codes, weights = split
        parameters.append(
            PatternParameter(label=ranked, values=codes, weights=weights)
        )

    # Group, plan and product drawn together, in the shares the members have them.
    enrollment = [r for r in (pattern.get("enrollment") or []) if isinstance(r, dict)]
    if enrollment:
        records = [{k: v for k, v in r.items() if k != "share"} for r in enrollment]
        shares = [float(r.get("share") or 0) for r in enrollment]
        total = sum(shares) or 1.0
        weights = [round(s / total, 8) for s in shares]
        for element in sorted({k for r in records for k in r}):
            parameters.append(PatternParameter(
                label=element.replace("_", " ").title(),
                values=records, weights=weights, record_field=element,
                record_group="enrollment",
            ))

    geography = [r for r in (pattern.get("geography") or []) if isinstance(r, dict)]
    if geography:
        records = [{k: v for k, v in r.items() if k != "share"} for r in geography]
        shares = [float(r.get("share") or 0) for r in geography]
        total = sum(shares) or 1.0
        weights = [round(s / total, 8) for s in shares]
        for element in sorted({k for r in records for k in r}):
            parameters.append(PatternParameter(
                label=element.replace("_", " ").title(),
                values=records, weights=weights, record_field=element,
            ))

    for group in groups:
        rows = [r for r in group["records"] if isinstance(r, dict)]
        records = [{k: v for k, v in r.items() if k != "share"} for r in rows]
        shares = [float(r.get("share") or 0) for r in rows]
        total = sum(shares) or 1.0
        weights = [round(s / total, 8) for s in shares]
        elements = sorted({k for r in records for k in r})
        for element in elements:
            parameters.append(PatternParameter(
                label=element.replace("_", " ").title(),
                values=records, weights=weights, record_field=element,
                record_group="combo:" + "+".join(elements),
            ))

    for feature, dist in (pattern.get("categorical") or {}).items():
        if FEATURE_ELEMENTS.get(feature) in covered:
            continue
        label, unit = FEATURE_LABELS.get(feature, (feature, None))
        split = _distribution(dist or {})
        if split is None:
            continue
        values, weights = split
        if len(values) == 1:
            parameters.append(PatternParameter(label=label, value=values[0], unit=unit))
        else:
            parameters.append(
                PatternParameter(label=label, values=values, weights=weights, unit=unit)
            )

    support = pattern.get("pct_of_total")
    if support is None:
        support = pattern.get("pct_of_lines")
    if support is None:
        support = pattern.get("pct_of_subset")
    if support is not None and float(support) > 1:
        support = float(support) / 100

    return AnalyzerPattern(
        id=str(pattern_id or pattern.get("pattern_key") or pattern.get("id")
                or f"cluster-{pattern.get('cluster_id')}"),
        kind=kind,
        label=str(pattern.get("name") or pattern.get("contextual_name") or ""),
        support=float(support) if support is not None else None,
        as_of=as_of,
        parameters=parameters,
    )


def ratio_overrides(pattern: dict[str, Any], *, child: str, parent: str) -> dict[str, Any]:
    """The children-per-parent shape a pattern implies, if it carries one.

    `avg_dependents` is the one family statistic that is really a cascade rule:
    it says how many members a subscriber in this pattern has. It is returned
    separately from the column targets because the plan holds it in a different
    place -- on the table, not on a field.
    """
    family = pattern.get("family") or {}
    average = family.get("avg_dependents")
    if average is None:
        return {}
    # A dependent count excludes the subscriber themself, who is also a member.
    members_per_subscriber = float(average) + 1
    return {
        child: {
            "kind": "fixed",
            "value": round(members_per_subscriber, 4),
            "parent": parent,
            "_why": (
                f"the pattern carries {float(average):.2f} dependents per subscriber, "
                f"which is {members_per_subscriber:.2f} members including the subscriber"
            ),
        }
    }


def describe(pattern: AnalyzerPattern) -> str:
    """One line naming what the pattern asks for. For a report or a log."""
    if not pattern.parameters:
        return f"{pattern.id}: nothing to state"
    return f"{pattern.id}: " + "; ".join(p.stated() for p in pattern.parameters)
