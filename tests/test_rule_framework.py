"""Every generation algorithm, through the one framework.

A rule in the repository is read as one of the engine's generators
(sdg.engine.rules), so a file, an EDI transaction, a layout and a database
load all make values with the same code. These check each method's reading
and its values, that nothing else makes them, and -- against the store --
that every active rule in the repository makes a value.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import pytest

from sdg.engine import rules as framework
from sdg.engine.generators import RowContext, get_generator, registered_names
from sdg.writers import elements, rule_values


def values(rule: dict, n: int = 50, field: str = "X", context: dict | None = None) -> list[str]:
    return [framework.run(rule, seed="t", row_index=i, field=field, context=context)
            for i in range(n)]


# ---------------------------------------------------------------------------
# One framework
# ---------------------------------------------------------------------------

EVERY_METHOD = {
    "constant": {"gen_method": "constant", "domain": "Y"},
    "sequential": {"gen_method": "sequential", "start_value": 1},
    "random": {"gen_method": "random", "length": 6},
    "lookup": {"gen_method": "lookup", "domain": "A,B"},
    "range": {"gen_method": "range", "min_value": 1, "max_value": 2},
    "pattern": {"gen_method": "pattern", "format_pattern": "[A-Z]{2}"},
    "date": {"gen_method": "date", "format_pattern": "2026-01-01", "start_value": 10},
    "formatted": {"gen_method": "formatted", "format_pattern": "{A}-{B}"},
}


def test_every_method_on_the_rules_screen_is_read_as_an_engine_generator():
    assert set(EVERY_METHOD) | {"default"} == set(framework.METHODS)
    for method, rule in EVERY_METHOD.items():
        name, _params = framework.generator_for(rule, "X")
        assert name in registered_names(), method
    assert framework.generator_for({"gen_method": "default"}, "X") is None


def test_a_file_value_is_made_by_the_engine_and_nothing_else(monkeypatch):
    """The same generator a database load runs, with the same draw."""
    rule = {"gen_method": "pattern", "format_pattern": r"[A-Z]{3}\d{6}"}
    for row in range(20):
        ctx = RowContext(seed="s", scenario="file", table="rule", field_name="SUBSCRIBER_ID",
                         params={"regex": r"[A-Z]{3}\d{6}"}, row_index=row)
        assert rule_values.produce({**rule, "element": "SUBSCRIBER_ID"}, seed="s",
                                   row_index=row) == get_generator("pattern")(ctx)

    calls = []
    import sdg.engine.rules as module

    real = module.get_generator
    monkeypatch.setattr(module, "get_generator", lambda name: calls.append(name) or real(name))
    for rule in EVERY_METHOD.values():
        rule_values.produce(rule, seed="s", row_index=1, context={"A": "a", "B": "b"})
    assert sorted(calls) == sorted(framework.generator_for(r, "X")[0]
                                   for r in EVERY_METHOD.values())


def test_a_method_nobody_knows_is_said_not_guessed():
    with pytest.raises(framework.RuleError, match="no such method"):
        framework.generator_for({"gen_method": "magic"}, "X")
    # In a file it leaves the field blank rather than failing the file.
    assert rule_values.produce({"gen_method": "magic"}, seed="s", row_index=1) == ""


# ---------------------------------------------------------------------------
# Each method
# ---------------------------------------------------------------------------

def test_constant():
    assert set(values({"gen_method": "constant", "domain": "HLT"})) == {"HLT"}


def test_a_counter_counts_with_its_prefix_and_width():
    made = values({"gen_method": "sequential", "start_value": 7, "prefix": "EMP", "length": 10}, 3)
    assert made == ["EMP0000007", "EMP0000008", "EMP0000009"]
    assert values({"gen_method": "sequential", "start_value": 100}, 2) == ["100", "101"]


def test_a_random_number_has_its_width_inside_its_prefix():
    made = values({"gen_method": "random", "prefix": "R", "length": 7})
    assert all(re.fullmatch(r"R\d{6}", v) for v in made), made
    assert len(set(made)) > 40


def test_a_list_is_drawn_from_with_its_weights():
    made = values({"gen_method": "lookup", "domain": "F,M", "weights": "0.9,0.1"}, 400)
    assert set(made) == {"F", "M"}
    assert 0.8 < made.count("F") / 400 < 0.97


def test_a_range_stays_inside_its_bounds_and_places():
    money = values({"gen_method": "range", "min_value": 5, "max_value": 25000, "decimals": 2})
    assert all(re.fullmatch(r"\d+\.\d{2}", v) and 5 <= float(v) <= 25000 for v in money)
    whole = values({"gen_method": "range", "min_value": 1, "max_value": 10, "data_type": "int"})
    assert all(v.isdigit() and 1 <= int(v) <= 10 for v in whole), whole
    shaped = values({"gen_method": "range", "min_value": 5, "max_value": 500, "decimals": 2,
                     "distribution": "lognormal", "mean_value": 4.0, "stddev_value": 1.0}, 200)
    assert all(5 <= float(v) <= 500 for v in shaped)


@pytest.mark.parametrize("regex", [
    r"\d{12}", r"[A-Z]{3}\d{8}", "[MDSP]", r"EMP\d{7}", r"\d{3}-\d{2}-\d{4}",
    r"(AB|CD)\d{2}", r"[A-Z]{1,3}\d?", r"[A-Za-z ]{5}", r"1[0-9]{9}",
])
def test_a_format_is_made_to_its_whole_shape(regex):
    made = values({"gen_method": "pattern", "format_pattern": regex})
    assert all(re.fullmatch(regex, v) for v in made), (regex, made[:5])


def test_a_date_falls_in_its_window_either_side_of_its_day():
    forward = values({"gen_method": "date", "format_pattern": "2020-01-01",
                      "min_value": 0, "max_value": 2000}, 200)
    days = [date.fromisoformat(v) for v in forward]
    assert min(days) >= date(2020, 1, 1) and max(days) <= date(2020, 1, 1) + timedelta(2000)
    assert max(days) > date(2023, 1, 1), "a forward window was drawn backwards"

    back = values({"gen_method": "date", "format_pattern": "2026-06-30", "start_value": 30}, 200)
    days = [date.fromisoformat(v) for v in back]
    assert date(2026, 6, 1) <= min(days) and max(days) <= date(2026, 6, 30)

    assert set(values({"gen_method": "date", "format_pattern": "2018-01-01",
                       "start_value": 0}, 5)) == {"2018-01-01"}


def test_a_template_fills_from_the_row_and_an_email_from_the_name():
    rule = {"gen_method": "formatted", "format_pattern": "{LAST_NAME}, {FIRST_NAME}"}
    assert values(rule, 1, context={"FIRST_NAME": "Ana", "LAST_NAME": "Diaz"}) == ["Diaz, Ana"]
    # A field the row lacks leaves it empty rather than half-written.
    assert values(rule, 1, context={"FIRST_NAME": "Ana"}) == [""]
    email = {"gen_method": "formatted", "domain": "example.org"}
    assert values(email, 1, context={"FIRST_NAME": "Ana", "LAST_NAME": "Diaz"}) == \
        ["ana.diaz@example.org"]
    with pytest.raises(framework.RuleError, match="template"):
        framework.generator_for({"gen_method": "formatted"}, "X")


def test_a_claims_total_is_the_sum_of_its_lines():
    rules = {
        "CHARGE_AMOUNT": {"gen_method": "range", "min_value": 1, "max_value": 100, "decimals": 2},
        "TOTAL_CHARGE": {"gen_method": "formatted", "format_pattern": "sum:CHARGE_AMOUNT"},
    }
    lines = list(elements.claims_for([{"MEMBER_ID": "M1"}], rules, claim_level=["TOTAL_CHARGE"],
                                     line_level=["CHARGE_AMOUNT"], claims_per_member=2,
                                     lines_per_claim=3, seed="t"))
    for claim in {line["CLAIM_ID"] for line in lines}:
        mine = [line for line in lines if line["CLAIM_ID"] == claim]
        assert len(mine) == 3
        total = sum(float(line["CHARGE_AMOUNT"]) for line in mine)
        assert all(abs(float(line["TOTAL_CHARGE"]) - total) < 0.005 for line in mine)


def test_a_combination_agrees_with_what_the_row_holds():
    records = [{"DIAGNOSIS_CODE": "E11", "PROCEDURE_CODE": "P1"},
               {"DIAGNOSIS_CODE": "E11", "PROCEDURE_CODE": "P2"},
               {"DIAGNOSIS_CODE": "J45", "PROCEDURE_CODE": "P9"}]
    rule = {"gen_method": "lookup", "values": records, "field": "PROCEDURE_CODE",
            "given": {"DIAGNOSIS_CODE": "DIAGNOSIS_CODE"}}
    assert set(values(rule, 60, context={"DIAGNOSIS_CODE": "J45"})) == {"P9"}
    assert set(values(rule, 60, context={"DIAGNOSIS_CODE": "E11"})) == {"P1", "P2"}


def test_the_new_algorithms_are_part_of_the_plan_vocabulary():
    from sdg.plan.editing import generator_catalog

    names = {g["name"] for g in generator_catalog()}
    assert {"street_address", "template"} <= names <= set(registered_names())


# ---------------------------------------------------------------------------
# Against the store: reference lists, combinations, every rule
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_names_streets_and_addresses_come_from_the_reference_lists():
    from sdg.analyzer import generation_rules as tables

    firsts = values({"gen_method": "lookup", "lookup_source": "first_names"}, 30)
    assert all(firsts) and set(firsts) <= set(tables.name_list("first_names"))
    women = values({"gen_method": "lookup", "lookup_source": "first_names"}, 30,
                   context={"SEX": "F"})
    assert set(women) <= set(tables.name_list("first_names", "F"))

    streets = values({"gen_method": "lookup", "lookup_source": "street_names"}, 30, "STREET_1")
    assert all(re.fullmatch(r"\d{3,4} [A-Za-z' .]+", s) for s in streets), streets

    places = {(r["ADDRESS_ZIP"], r["ADDRESS_CITY"], r["ADDRESS_STATE"])
              for r in tables.address_records()}
    for row in range(30):
        drawn = tuple(framework.run({"gen_method": "lookup", "lookup_source": "zip_city_state"},
                                    seed="t", row_index=row, field=f)
                      for f in ("ZIP_CODE", "ADDRESS_CITY", "ADDRESS_STATE"))
        assert drawn in places, "a ZIP was drawn apart from its city and state"


@pytest.mark.db
def test_a_clinical_combination_is_one_the_store_holds():
    from sdg.plan.combinations import for_patterns

    group = next((g for g in for_patterns()
                  if {"DIAGNOSIS_CODE", "PROCEDURE_CODE"} <= set(g.elements)), None)
    if group is None:
        pytest.skip("the store holds no diagnosis and procedure combinations")
    key = "combo:" + "+".join(sorted(group.elements))
    held = {tuple(dict(zip(group.elements, v))[e] for e in ("DIAGNOSIS_CODE", "PROCEDURE_CODE"))
            for v, _ in group.combinations}
    for row in range(40):
        context: dict = {}
        for element in group.elements:
            context[element] = framework.run({"gen_method": "lookup", "lookup_source": key},
                                             seed="t", row_index=row, field=element,
                                             context=context)
        assert (context["DIAGNOSIS_CODE"], context["PROCEDURE_CODE"]) in held


@pytest.mark.db
def test_every_active_rule_in_the_repository_makes_a_value():
    """What the Rules screen's Try it shows, for every rule there is."""
    from fastapi.testclient import TestClient

    from sdg.analyzer.services import layout_service as ls
    from sdg.api.app import app

    client = TestClient(app)
    blank = []
    for rule in ls.get_rules():
        if str(rule.get("gen_method") or "default") == "default":
            continue
        body = {k: v for k, v in rule.items()
                if k in ("field_name", "element", "gen_method", "data_type", "length", "prefix",
                         "postfix", "start_value", "domain", "format_pattern", "lookup_source",
                         "min_value", "max_value", "decimals", "weights", "distribution",
                         "mean_value", "stddev_value") and v is not None}
        response = client.post("/api/rules/sample", json=body)
        made = response.json().get("values") if response.status_code == 200 else None
        # A network is empty for an out-of-network claim: a value, not a blank.
        empty_is_a_value = str(rule.get("element") or rule["field_name"]).upper() == "NETWORK_ID"
        if made is None or not (any(made) if empty_is_a_value else all(made)):
            blank.append((rule["field_name"], rule["gen_method"], response.text[:120]))
    assert not blank, blank
