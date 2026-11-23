"""The analyzer's filters write their values into SQL; a value can never become SQL.

A filter value is text a request sent (a plan, a product, a diagnosis). A
quote in it is doubled, so it stays inside its literal: a value that would
have ended the literal and run a statement of its own is looked for as the
odd value it is, and found nowhere.
"""

from __future__ import annotations

import pytest

from sdg.analyzer import entity_loader
from sdg.analyzer.sql import literal

ATTACK = "X') ; DROP TABLE sdm.patterns; --"


def test_a_value_is_one_literal_whatever_it_holds():
    assert literal("PLN001 ") == "'PLN001'"
    assert literal("O'BRIEN") == "'O''BRIEN'"
    assert literal(ATTACK) == "'X'') ; DROP TABLE sdm.patterns; --'"


def test_an_id_that_is_no_number_is_dropped_not_a_crash():
    # "inf" became float('inf'), and int() of it raised OverflowError mid-query.
    assert entity_loader.int_list(["inf", "-inf", "1e999", "12", "x"]) == [12]
    with pytest.raises(ValueError, match="age_min must be a number"):
        entity_loader.as_int("inf", "age_min")


def test_every_filter_keeps_its_values_inside_their_literals():
    where = entity_loader.where("members", {"grgr_ck": ["1", "x"], "cspd_cat": [ATTACK],
                                            "cspi_id": ["A'"], "pdpd_id": ["P"]})
    assert where == ("WHERE GRGR_CK IN (1) AND CSPD_CAT IN ('X'') ; DROP TABLE sdm.patterns; --') "
                     "AND CSPI_ID IN ('A''') AND PDPD_ID IN ('P')")
    where = entity_loader.where("claims", {"cspi_id": [ATTACK], "pdpd_id": ["P'"],
                                           "claim_type": ["M"], "gender": "F'",
                                           "diagnosis": ["E11'"], "procedure": ["9'"]})
    for held in ("'X'') ; DROP", "'P'''", "'F'''", "'E11'''", "'9'''"):
        assert held in where, (held, where)


@pytest.mark.db
def test_a_value_meant_to_end_the_literal_is_looked_for_and_found_nowhere():
    from sdg.analyzer import db

    before = len(db.get_all_patterns())
    assert entity_loader.options("members", "plans", entity_loader.given(cspd_cat=[ATTACK])) == []
    assert entity_loader.options("members", "products",
                                 entity_loader.given(cspi_id=["O'BRIEN"])) == []
    assert len(db.get_all_patterns()) == before
