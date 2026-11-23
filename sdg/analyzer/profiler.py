"""Per-cluster profile computation."""

from .config import (
    CATEGORICAL_FEATURES,
    CONTINUOUS_FEATURES,
    DESCRIPTION_COLUMNS,
    GEOGRAPHY_COLUMNS,
)
from sdg import packs
from sdg.packs import held, held_or


def profile_cluster(data, family_data, mask, cluster_id, total_n):
    """Compute demographic profile for a single cluster.

    Args:
        data: Deduplicated member DataFrame.
        family_data: Full (non-deduplicated) DataFrame for family analysis.
        mask: Boolean array selecting members in this cluster.
        cluster_id: Integer cluster label.
        total_n: Total members in subset (for pct calculation).
    """
    subset = data[mask]
    size = len(subset)

    profile = {
        'cluster_id': int(cluster_id),
        'size': size,
        'pct_of_subset': round(size / total_n, 4) if total_n > 0 else 0.0,
    }

    # The features the pattern was found on are described with it, beside the
    # ones every member pattern always carries. Not guarded: a store that
    # cannot be read falls back to the default features inside `chosen` (and
    # is logged there), and anything else is a fault to see, not a profile
    # quietly missing the features it was found on.
    from .services.discovery_features import columns as _chosen

    numbers, lists = _chosen(packs.active().primary)
    continuous_cols = list(dict.fromkeys([*CONTINUOUS_FEATURES, *numbers]))
    categorical_cols = [c for c in dict.fromkeys([*CATEGORICAL_FEATURES, *lists])
                        if c in subset.columns]

    # Continuous feature stats
    continuous = {}
    for col in continuous_cols:
        vals = subset[col].dropna()
        if len(vals) > 0:
            continuous[col] = {
                'mean': round(float(vals.mean()), 2),
                'std': round(float(vals.std()), 2),
                'min': round(float(vals.min()), 2),
                'max': round(float(vals.max()), 2),
                'median': round(float(vals.median()), 2),
                'deciles': deciles(vals),
            }
        else:
            continuous[col] = {'mean': 0, 'std': 0, 'min': 0, 'max': 0, 'median': 0}
    profile['continuous'] = continuous

    # Categorical distributions
    categorical = {}
    for col in categorical_cols:
        counts = subset[col].value_counts().to_dict()
        total = sum(counts.values())
        pct = {k: round(v / total, 4) for k, v in counts.items()} if total > 0 else {}
        categorical[col] = {'counts': counts, 'pct': pct}
    profile['categorical'] = categorical

    profile['geography'] = _geography(subset)
    profile['enrollment'] = _enrollment(subset)

    # Family structure, where the pack counts households
    # (primary_analysis.family: health's subscribers and their dependents).
    spec = held_or("features:primary_analysis.family", None)
    if spec and family_data is not None and spec["household"] in subset.columns:
        households = subset[spec["household"]].unique()
        family_subset = family_data[family_data[spec["household"]].isin(households)]
        profile['family'] = _compute_family_stats(family_subset, households, spec)
    else:
        profile['family'] = {}

    # Description columns
    descriptions = {}
    for col in DESCRIPTION_COLUMNS:
        if col in subset.columns:
            descriptions[col] = sorted(subset[col].dropna().unique().tolist())
        else:
            descriptions[col] = []
    profile['descriptions'] = descriptions

    return profile


def _compute_family_stats(family_subset, households, spec):
    """Average dependents and spouse rate per household (spec: the pack's
    household, member and relation columns, and its dependent and spouse codes)."""
    household, member, relation = spec["household"], spec["member"], spec["relation"]
    n_subscribers = len(households)
    if n_subscribers == 0:
        return {'avg_dependents': 0.0, 'spouse_rate': 0.0}

    if relation not in family_subset.columns:
        return {'avg_dependents': 0.0, 'spouse_rate': 0.0}

    # Count dependents per household
    dep_counts = (
        family_subset[family_subset[relation] == spec["dependent"]]
        .groupby(household)[member]
        .nunique()
    )
    avg_dependents = float(dep_counts.sum()) / n_subscribers

    # Spouse rate: the share of households with at least one spouse
    has_spouse = (
        family_subset[family_subset[relation] == spec["spouse"]][household].nunique()
    )
    spouse_rate = has_spouse / n_subscribers

    return {
        'avg_dependents': round(avg_dependents, 2),
        'max_dependents': int(dep_counts.max()) if len(dep_counts) > 0 else 0,
        'spouse_rate': round(spouse_rate, 4),
    }


def build_subset_summary(data, filters_used):
    """Build metadata summary for the filtered subset."""
    summary = {
        'total_members': len(data),
        'filters': filters_used,
    }

    # Add hierarchy descriptions from the data
    for col in DESCRIPTION_COLUMNS:
        if col in data.columns:
            summary[col] = sorted(data[col].dropna().unique().tolist())

    return summary


def deciles(vals) -> list[float]:
    """The eleven cut points that split values into ten equal shares: what a
    number is drawn from, so generated values spread as the pattern's do."""
    import numpy as np

    data = np.asarray(vals, dtype=float)
    data = data[~np.isnan(data)]
    if data.size == 0:
        return []
    return [round(float(q), 2) for q in np.quantile(data, [i / 10 for i in range(11)])]


# Where a member is enrolled, as the elements generation writes it to.
ENROLLMENT_COLUMNS = held("features:analyzer.enrollment_columns")


def _enrollment(subset):
    """The group, plan and product the cluster's members are enrolled in, as
    whole records with their shares: a plan is drawn with the group it belongs
    to, never apart from it."""
    present = {c: e for c, e in ENROLLMENT_COLUMNS.items() if c in subset.columns}
    if not present or len(subset) == 0:
        return []
    frame = subset[list(present)].dropna(how='all')
    if frame.empty:
        return []
    frame = frame.astype(str).apply(lambda col: col.str.strip())
    counts = frame.value_counts()
    total = int(counts.sum()) or 1
    # Ids (the loader's id filters: health's group) are whole numbers, though
    # a column with a blank in it reads as floats.
    ids = {f["column"] for f in
           (packs.active().features.get("loaders") or {}).get(packs.active().primary, {})
           .get("filters", []) if f["kind"] == "ids"}
    out = []
    for values, count in counts.items():
        values = values if isinstance(values, tuple) else (values,)
        record = {present[c]: (str(int(float(v))) if c in ids and v not in ('', 'nan')
                               else v)
                  for c, v in zip(present, values) if v and v != 'nan'}
        if record:
            out.append({**record, 'share': round(int(count) / total, 6)})
    return out


def _geography(subset):
    """Where the cluster's members live: whole address records, with shares.

    Kept as records rather than one distribution per column, because a ZIP
    and its city belong together and drawing them apart puts members in
    places that do not exist.
    """
    present = {c: e for c, e in GEOGRAPHY_COLUMNS.items() if c in subset.columns}
    if not present or len(subset) == 0:
        return []
    frame = subset[list(present)].dropna(how='all').astype(str)
    if frame.empty:
        return []
    counts = frame.value_counts()
    total = int(counts.sum()) or 1
    out = []
    for values, count in counts.items():
        values = values if isinstance(values, tuple) else (values,)
        record = {present[c]: str(v).strip() for c, v in zip(present, values)
                  if str(v).strip() and str(v) != 'nan'}
        if record:
            out.append({**record, 'share': round(int(count) / total, 6)})
    return out
