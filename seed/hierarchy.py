"""The enrollment hierarchy, and the demographics that differ across it.

The pattern analyzer selects a group, a plan category, a plan or a product, and
then clusters the members underneath on age, tenure, sex and marital status. If
every group held the same population there would be nothing to find: the
clusters would be arbitrary cuts of one undifferentiated cloud, and a pattern
named after them would mean nothing.

So each group here is a different kind of employer with the workforce that
implies -- a university is young and single, a transit authority is older and
married, a senior care provider is older still and largely widowed. Those are
the patterns the analyzer is meant to discover, and `seed/verify.py` checks they
are present.

Nothing here is drawn from real data. The names are invented.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from typing import Any

# Each group carries the shape of its own workforce. `share` is its slice of
# the subscriber population, so the discovered patterns differ in size as well
# as in character and ranking them by population means something.
GROUPS: list[dict[str, Any]] = [
    {
        "id": "GRP00001", "name": "Riverside University", "state": "MA",
        "county": "Hampden", "type": "EDU", "share": 0.16,
        "age": (18, 32), "sex": (0.46, 0.54), "marital": (0.18, 0.74, 0.06, 0.02),
        "tenure_years": (0, 6), "products": "both",
    },
    {
        "id": "GRP00002", "name": "Metro Transit Authority", "state": "NY",
        "county": "Erie", "type": "MUNI", "share": 0.14,
        "age": (42, 64), "sex": (0.71, 0.29), "marital": (0.72, 0.12, 0.13, 0.03),
        "tenure_years": (3, 22), "products": "medical",
    },
    {
        "id": "GRP00003", "name": "Northside Health System", "state": "OH",
        "county": "Franklin", "type": "HLTH", "share": 0.17,
        "age": (28, 52), "sex": (0.24, 0.76), "marital": (0.58, 0.30, 0.10, 0.02),
        "tenure_years": (1, 15), "products": "both",
    },
    {
        "id": "GRP00004", "name": "Lakeshore Manufacturing", "state": "MI",
        "county": "Kent", "type": "MFG", "share": 0.13,
        "age": (38, 61), "sex": (0.78, 0.22), "marital": (0.70, 0.16, 0.12, 0.02),
        "tenure_years": (2, 24), "products": "medical",
    },
    {
        "id": "GRP00005", "name": "Summit Retail Group", "state": "TX",
        "county": "Travis", "type": "RTL", "share": 0.12,
        "age": (20, 37), "sex": (0.38, 0.62), "marital": (0.26, 0.66, 0.07, 0.01),
        "tenure_years": (0, 5), "products": "dental",
    },
    {
        "id": "GRP00006", "name": "Cedar Valley School District", "state": "PA",
        "county": "Lancaster", "type": "EDU", "share": 0.11,
        "age": (33, 57), "sex": (0.29, 0.71), "marital": (0.66, 0.21, 0.11, 0.02),
        "tenure_years": (2, 20), "products": "both",
    },
    {
        "id": "GRP00007", "name": "Harbor Logistics", "state": "NJ",
        "county": "Hudson", "type": "TRAN", "share": 0.10,
        "age": (26, 46), "sex": (0.69, 0.31), "marital": (0.45, 0.44, 0.10, 0.01),
        "tenure_years": (0, 11), "products": "medical",
    },
    {
        "id": "GRP00008", "name": "Pine Ridge Senior Care", "state": "FL",
        "county": "Pinellas", "type": "HLTH", "share": 0.07,
        "age": (52, 74), "sex": (0.31, 0.69), "marital": (0.44, 0.09, 0.17, 0.30),
        "tenure_years": (1, 17), "products": "medical",
    },
]

MARITAL_CODES = ["M", "S", "D", "W"]

# Plan categories. The category a plan sits in follows its product type, so a
# dental plan is never filed under a medical category.
PLAN_CATEGORIES: list[tuple[str, str, str]] = [
    # One character, as FACETS holds a plan category.
    ("M", "Medical and hospital", "MEDICAL"),
    ("D", "Dental", "DENTAL"),
]

SUBGROUP_SUFFIXES = ["Main", "Field", "Clerical", "Salaried", "Hourly", "Retiree"]

MEMBER_STATUS_CODES = ["AC", "AC", "AC", "AC", "TM"]


def group_rows(rng: random.Random, as_of: date) -> list[tuple]:
    """One row per group, oldest effective dates for the longest-running ones."""
    rows = []
    for index, group in enumerate(GROUPS, start=1):
        start = as_of - timedelta(days=rng.randint(8 * 365, 30 * 365))
        rows.append(
            (
                index,
                group["id"],
                group["name"],
                group["state"],
                group["county"],
                "AC",
                group["type"],
                start,
                None,
            )
        )
    return rows


def subgroup_rows(rng: random.Random, as_of: date) -> list[tuple]:
    """Two or three divisions inside each group."""
    rows = []
    sgsg_ck = 0
    for group_ck, group in enumerate(GROUPS, start=1):
        for slot in range(rng.randint(2, 3)):
            sgsg_ck += 1
            suffix = SUBGROUP_SUFFIXES[(group_ck + slot) % len(SUBGROUP_SUFFIXES)]
            rows.append(
                (
                    sgsg_ck,
                    group_ck,
                    f"SG{sgsg_ck:06d}",
                    f"{group['name']} - {suffix}",
                    f"CS{sgsg_ck:06d}",
                    group["state"],
                    "AC",
                    as_of - timedelta(days=rng.randint(5 * 365, 20 * 365)),
                    None,
                )
            )
    return rows


def plan_category_rows() -> list[tuple]:
    return [(code, description, kind) for code, description, kind in PLAN_CATEGORIES]


def plan_rows(
    products: list[tuple], rng: random.Random, as_of: date,
    prefixes: list[str], suffixes: list[str],
) -> tuple[list[tuple], list[tuple], list[tuple]]:
    """One coverage plan per product, with its description and the product's.

    Returns (plans, plan descriptions, product descriptions).
    """
    plans, plan_descriptions, product_descriptions = [], [], []
    for index, (product_id, product_type) in enumerate(products):
        category = "M" if product_type == "M" else "D"
        plan_id = f"CSPI{index + 1:04d}"
        eff = as_of - timedelta(days=rng.randint(3 * 365, 12 * 365))
        plans.append((plan_id, category, product_id, eff, None, "Y"))

        prefix = prefixes[index % len(prefixes)]
        suffix = suffixes[index % len(suffixes)]
        kind = "Medical" if product_type == "M" else "Dental"
        plan_descriptions.append((plan_id, f"{prefix} {kind} {suffix}"))
        product_descriptions.append(
            (product_id, f"{prefix} {kind} {suffix} product")
        )
    return plans, plan_descriptions, product_descriptions


def assign_group(rng: random.Random) -> tuple[int, dict[str, Any]]:
    """Pick a group for one subscriber, in proportion to its share."""
    index = rng.choices(range(len(GROUPS)), [g["share"] for g in GROUPS])[0]
    return index + 1, GROUPS[index]


def demographics(
    group: dict[str, Any], relationship: str, rng: random.Random, as_of: date
) -> tuple[str, str, date]:
    """Sex, marital status and birth date for one member of this group.

    A dependent child is drawn young and single whatever the group looks like;
    everyone else follows the group's own profile. That is what makes the
    clusters inside a group separate cleanly rather than smearing together.
    """
    if relationship == "D":  # a dependent child
        age = rng.randint(0, 25)
        sex = rng.choice(["M", "F"])
        marital = "S"
    else:
        low, high = group["age"]
        age = rng.randint(low, high)
        male, female = group["sex"]
        sex = rng.choices(["M", "F"], [male, female])[0]
        marital = rng.choices(MARITAL_CODES, group["marital"])[0]
        if relationship == "S" and marital == "S":
            marital = "M"  # a spouse is not single
    birth = as_of - timedelta(days=age * 365 + rng.randint(0, 364))
    return sex, marital, birth


def tenure_start(group: dict[str, Any], rng: random.Random, as_of: date) -> date:
    """Where a subscriber's coverage began, from the group's tenure profile."""
    low, high = group["tenure_years"]
    years = rng.randint(low, high)
    return as_of - timedelta(days=years * 365 + rng.randint(0, 364))


def ssn(rng: random.Random) -> str:
    """A meaningless identifier in the shape FACETS holds one: nine digits."""
    return f"{rng.randint(100, 899):03d}{rng.randint(10, 99):02d}{rng.randint(1000, 9999):04d}"
