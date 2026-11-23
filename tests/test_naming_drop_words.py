"""A scope level's short name leaves out the installation's own words.

A customer's group names carry the company's name ("Acme Ohio Medicaid"):
no pattern name needs it. The pack builds none in; SDG_NAMING_DROP_WORDS in
.env names them, separated by commas.
"""

from __future__ import annotations

from sdg.analyzer.naming import shorten

SPEC = {"fallback": "first_word", "length": 10, "empty_length": 8}


def test_the_installations_words_are_left_out_of_a_level_name(monkeypatch):
    monkeypatch.delenv("SDG_NAMING_DROP_WORDS", raising=False)
    assert shorten("Acme Ohio Medicaid", SPEC) == "Acme"
    monkeypatch.setenv("SDG_NAMING_DROP_WORDS", "Acme, Beta Health")
    assert shorten("Acme Ohio Medicaid", SPEC) == "Ohio"
    assert shorten("Beta Health Dental", SPEC) == "Dental"
    # The pack's own words are dropped too, beside the installation's.
    assert shorten("Gamma Acme Vision", {**SPEC, "drop": ["Gamma"]}) == "Vision"
    # A name that is nothing but dropped words keeps its first characters.
    assert shorten("Acme", SPEC) == "Acme"
