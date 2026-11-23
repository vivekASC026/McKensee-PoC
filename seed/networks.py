"""Provider networks, as FACETS holds them.

A plan names a network set (CMC_CSPI_CS_PLAN.NWST_PFX); the set lists its
networks (CMC_NWST_NET_SET); a provider participates in networks over dated
spans (CMC_NWPR_RELATION). A claim is in network when its provider
participates, on the service date, in a network of the member's plan -- and
then the claim names that network (CMC_CLCL_CLAIM.NWNW_ID). Otherwise it is
out of network and names none. A member's primary care provider
(CMC_MEPR_PRIM_PROV) is a PCP of one of the plan's networks.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

# (id, name, type, kind): kind M serves medical plans, D dental ones.
NETWORKS = [
    ("NW0000000001", "Heartland Medical PPO", "PPO", "M"),
    ("NW0000000002", "Ohio Valley HMO", "HMO", "M"),
    ("NW0000000003", "Bluegrass Care Network", "HMO", "M"),
    ("NW0000000004", "Great Lakes Select", "EPO", "M"),
    ("NW0000000005", "National Access PPO", "PPO", "M"),
    ("NW0000000006", "SmileCare Dental", "DENT", "D"),
    ("NW0000000007", "Dental Choice Plus", "DENT", "D"),
]
PRIMARY_CARE = {"FAM", "INT", "PED"}
# Entity of a provider record: P practitioner, F facility, G group.
FACILITY_SPECIALTIES = {"EMR", "RAD"}
NETWORK_SINCE = date(2010, 1, 1)
IN_NETWORK_SHARE = 0.86


def network_rows() -> list[tuple]:
    return [(nw, name, kind_type) for nw, name, kind_type, _k in NETWORKS]


def entity(specialty: str, index: int) -> str:
    if specialty in FACILITY_SPECIALTIES:
        return "F"
    return "G" if index % 25 == 0 else "P"


def network_sets(plans: list[tuple], product_type_by_id: dict[str, str],
                 rng: random.Random) -> tuple[dict[str, str], list[tuple]]:
    """Each plan's network set prefix, and the sets' rows.

    One set per plan: one or two networks of the plan's kind.
    Returns ({CSPI_ID: NWST_PFX}, NWST rows).
    """
    by_kind = {k: [n for n in NETWORKS if n[3] == k] for k in ("M", "D")}
    prefix_by_plan: dict[str, str] = {}
    rows: list[tuple] = []
    for index, (cspi_id, _cat, pdpd_id, eff, _term, _sel) in enumerate(plans):
        kind = product_type_by_id[pdpd_id]
        prefix = f"N{index + 1:03d}"
        prefix_by_plan[cspi_id] = prefix
        chosen = rng.sample(by_kind[kind], k=min(len(by_kind[kind]), rng.choice([1, 2])))
        for seq, network in enumerate(chosen, start=1):
            # NWST_EFF_DT, NWST_SEQ_NO, NWST_TERM_DT, status P(articipating),
            # NWNW_ID, NWPR_PFX, tier 1 for the first network, 2 after.
            rows.append((prefix, min(eff, NETWORK_SINCE), seq, None, "P",
                         network[0], "STD", seq))
    return prefix_by_plan, rows


def relations(providers: list[tuple], rng: random.Random,
              as_of: date) -> list[tuple]:
    """Each provider's network participation: one or two networks of its kind.

    Row: (PRPR_ID, NWNW_ID, NWPR_PFX, NWPR_PCP_IND, NWPR_EFF_DT, NWPR_TERM_DT,
    AGAG_ID). A few spans ended, so a provider can leave a network.
    """
    out = []
    for prpr_id, _npi, specialty, _state, *_rest in providers:
        kind = "D" if specialty == "DEN" else "M"
        pool = [n for n in NETWORKS if n[3] == kind]
        for network in rng.sample(pool, k=min(len(pool), rng.choice([1, 1, 2]))):
            eff = NETWORK_SINCE + timedelta(days=rng.randint(0, 900))
            term = (as_of - timedelta(days=rng.randint(30, 700))
                    if rng.random() < 0.08 else None)
            out.append((prpr_id, network[0], "STD",
                        "Y" if specialty in PRIMARY_CARE else "N",
                        eff, term, f"AG{network[0][-4:]}{'P' if specialty in PRIMARY_CARE else 'S'}"))
    return out


class Roster:
    """Who is in which network on a day, for drawing a claim's provider."""

    def __init__(self, providers: list[tuple], relation_rows: list[tuple],
                 set_rows: list[tuple], prefix_by_plan: dict[str, str]):
        self.kind = {p[0]: ("D" if p[2] == "DEN" else "M") for p in providers}
        self.spans: dict[str, list[tuple[str, date, date | None, str]]] = {}
        for prpr_id, nwnw_id, _pfx, pcp, eff, term, _ag in relation_rows:
            self.spans.setdefault(nwnw_id, []).append((prpr_id, eff, term, pcp))
        self.networks: dict[str, list[str]] = {}
        for prefix, _eff, _seq, _term, _sts, nwnw_id, _p, _tier in set_rows:
            self.networks.setdefault(prefix, []).append(nwnw_id)
        self.prefix_by_plan = prefix_by_plan
        self.by_kind = {k: [p for p, v in self.kind.items() if v == k] for k in ("M", "D")}

    def _active(self, nwnw_id: str, day: date, pcp_only: bool = False) -> list[str]:
        return [p for p, eff, term, pcp in self.spans.get(nwnw_id, [])
                if eff <= day and (term is None or term >= day)
                and (not pcp_only or pcp == "Y")]

    def claim_provider(self, cspi_id: str, kind: str, day: date,
                       rng: random.Random) -> tuple[str, str, str | None]:
        """(PRPR_ID, CLCL_NTWK_IND, NWNW_ID) for a claim of this plan on this day."""
        networks = self.networks.get(self.prefix_by_plan.get(cspi_id, ""), [])
        inside = {p for n in networks for p in self._active(n, day)}
        outside = [p for p in self.by_kind[kind] if p not in inside]
        # Out of network only with a provider who really is outside the plan's
        # networks that day; where everyone is inside, the claim is in network.
        if outside and rng.random() >= IN_NETWORK_SHARE:
            return rng.choice(outside), "O", None
        for nwnw_id in rng.sample(networks, k=len(networks)):
            active = self._active(nwnw_id, day)
            if active:
                return rng.choice(active), "I", nwnw_id
        return rng.choice(outside or self.by_kind[kind]), "O", None

    def pcp(self, cspi_id: str, day: date, rng: random.Random) -> str | None:
        networks = self.networks.get(self.prefix_by_plan.get(cspi_id, ""), [])
        choices = [p for n in networks for p in self._active(n, day, pcp_only=True)]
        return rng.choice(choices) if choices else None
