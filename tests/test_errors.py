"""SQL Server errors reach a person as a sentence, and as the real error.

The driver joins every message a statement produced into one error. A warning
SQL Server sends beside a failure -- an aggregate skipping NULLs -- can come
first; it is never what the person is told.
"""

import pytest

from sdg.discovery.errors import plain


def _driver(*messages: tuple[str, str, int]) -> Exception:
    import pyodbc

    joined = "; ".join(
        f"[{state}] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]{text} ({number})"
        for state, text, number in messages) + " (SQLExecDirectW)"
    return pyodbc.ProgrammingError(messages[0][0], joined)


def test_a_warning_beside_an_error_is_not_the_error():
    said = plain(_driver(
        ("01003", "Warning: Null value is eliminated by an aggregate or other SET operation.", 8153),
        ("42S02", "Invalid object name 'sdm.provider_denorm'.", 208),
    ))
    assert "Null value" not in said
    assert said.startswith("The table sdm.provider_denorm is missing")
    assert "Invalid object name 'sdm.provider_denorm'" in said


def test_each_known_failure_says_what_to_do():
    assert "does not exist on this server" in plain(_driver(
        ("28000", "Login failed for user 'sa'.", 18456),
        ("42000", 'Cannot open database "NoSuch" requested by the login. The login failed.', 4060),
    ))
    assert "The column nope is missing" in plain(_driver(("42S22", "Invalid column name 'nope'.", 207)))
    duplicate = plain(_driver(("23000", "Violation of PRIMARY KEY constraint 'PK_x'. "
                                        "Cannot insert duplicate key in object 'dbo.t'. "
                                        "The duplicate key value is (1).", 2627)))
    assert "must be unique" in duplicate and "value is (1)" in duplicate


def test_an_error_nothing_recognizes_is_still_said_with_its_code():
    said = plain(_driver(("42000", "Incorrect syntax near 'SELEC'.", 102)))
    assert said.startswith("SQL Server reported an error.")
    assert "42000" in said and "Incorrect syntax near 'SELEC'" in said


@pytest.mark.parametrize("error", [ValueError("not a number"), KeyError("x")])
def test_an_error_that_is_not_the_databases_is_left_as_it_is(error):
    assert plain(error).startswith(type(error).__name__)
