"""Rules the generated SQL keeps, found by the review of the whole accelerator:
joins between columns of different type families are refused and text joins
across collations are compared in one; types the extract's driver cannot
read are refused at build; a pick never orders by a type SQL Server cannot
order; a criterion's share is counted over the whole sample (no TOP); a
protected database is read only when named a source in .env.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sdg.accelerator import source_read, sources, sqlgen
from sdg.discovery.model import Column, Table


def _column(name, type_name, collation=None, **kw):
    return Column(name=name, ordinal=1, type_name=type_name, collation=collation, **kw)


class _Graph:
    def __init__(self, tables):
        self.tables = {t.name: t for t in tables}

    def table(self, name):
        return self.tables.get(name)


def _table(name, *columns):
    schema, short = name.split(".")
    return Table(name=name, schema_name=schema, table_name=short, columns=list(columns))


def test_a_text_column_joined_to_a_number_is_refused_when_built():
    graph = _Graph([_table("dbo.A", _column("K", "varchar", max_length=10)),
                    _table("dbo.B", _column("K", "int"))])
    with pytest.raises(sqlgen.SqlRefused, match="different types"):
        sqlgen._collated(graph, "dbo.A", "K", "dbo.B", "K")


def test_text_columns_of_two_collations_are_compared_in_the_joined_columns():
    graph = _Graph([
        _table("dbo.A", _column("K", "varchar", "Latin1_General_CI_AS", max_length=10)),
        _table("dbo.B", _column("K", "nvarchar", "SQL_Latin1_General_CP1_CI_AS",
                                max_length=20))])
    assert sqlgen._collated(graph, "dbo.A", "K", "dbo.B", "K") == " COLLATE Latin1_General_CI_AS"
    same = _Graph([_table("dbo.A", _column("K", "varchar", "X_CI", max_length=1)),
                   _table("dbo.B", _column("K", "varchar", "X_CI", max_length=1))])
    assert sqlgen._collated(same, "dbo.A", "K", "dbo.B", "K") == ""
    assert sqlgen._collated(same, "dbo.A", "K", None, "K") == ""
    odd = _Graph([_table("dbo.A", _column("K", "varchar", "Bad Name;", max_length=1)),
                  _table("dbo.B", _column("K", "varchar", "Y_CI", max_length=1))])
    with pytest.raises(sqlgen.SqlRefused, match="cannot name"):
        sqlgen._collated(odd, "dbo.A", "K", "dbo.B", "K")


@pytest.mark.parametrize("type_name", ["sql_variant", "hierarchyid", "geography", "geometry",
                                       "datetimeoffset"])
def test_a_type_the_extract_cannot_read_is_refused_when_built(type_name):
    with pytest.raises(sqlgen.SqlRefused, match="cannot read"):
        sqlgen.store_type(_column("C", type_name, scale=7))


def test_types_sql_server_cannot_order_by_are_known():
    assert {"text", "ntext", "image", "xml"} <= sqlgen._UNORDERABLE
    assert "int" not in sqlgen._UNORDERABLE


def test_a_share_is_counted_over_the_whole_sample():
    big = SimpleNamespace(schema_name="dbo", table_name="T", row_count=10_000_000, name="dbo.T")
    said = source_read.count_source(big, "m")
    assert "TOP" not in said and "TABLESAMPLE SYSTEM" in said and said.startswith("[dbo].[T] AS m")
    small = SimpleNamespace(schema_name="dbo", table_name="S", row_count=10, name="dbo.S")
    assert source_read.count_source(small, "m") == "[dbo].[S] AS m"


def test_a_protected_database_is_read_only_when_named_a_source(monkeypatch):
    monkeypatch.setenv("SDG_PROTECTED_DATABASES", "SDMT_GUARDED")
    monkeypatch.delenv("SDG_SOURCE_SDMT_GUARDED_DATABASE", raising=False)
    with pytest.raises(sources.SourceError, match="protected database"):
        sources.source_settings("SDMT_GUARDED")
    monkeypatch.setenv("SDG_SOURCE_SDMT_GUARDED_DATABASE", "SDMT_GUARDED")
    assert sources.source_settings("SDMT_GUARDED")[1] == "SDMT_GUARDED"


@pytest.mark.parametrize("collation, told", [
    ("Latin1_General_CS_AS", True), ("SQL_Latin1_General_CP1_CS_AS", True),
    ("Latin1_General_BIN2", True), ("Latin1_General_BIN", True),
    ("Latin1_General_100_CS_AS_SC_UTF8", True), ("SQL_Latin1_General_CP1_CI_AS", False),
    ("Latin1_General_CI_AS_KS_WS", False), (None, False)])
def test_a_build_names_the_surrogates_whose_source_tells_case_apart(collation, told):
    from sdg.accelerator import builder

    graph = _Graph([_table("dbo.A", _column("K", "varchar", collation, max_length=10),
                           _column("N", "varchar", "Latin1_General_CS_AS", max_length=10))])
    column = SimpleNamespace(name="A_KEY", privacy="surrogate", source_table="dbo.A",
                             source_column="K")
    # A kept column of a case-sensitive collation is no surrogate: not named.
    kept = SimpleNamespace(name="A_NOTE", privacy="kept", source_table="dbo.A",
                           source_column="N")
    held = SimpleNamespace(structure=[SimpleNamespace(columns=[column, kept])])
    assert builder._case_sensitive_surrogates(held, graph) == (["A_KEY"] if told else [])
    assert builder._case_sensitive_surrogates(held, None) == []


def test_one_surrogate_name_read_with_two_case_rules_is_refused_when_built():
    """The tables join on a surrogate's name: read from a column that tells
    case apart in one table and from one that does not in the other, one
    surrogate cannot stand for both, so the build refuses it, naming both."""
    from sdg.accelerator import builder

    graph = _Graph([_table("dbo.A", _column("K", "varchar", "Latin1_General_CS_AS", max_length=9)),
                    _table("dbo.B", _column("K", "varchar", "Latin1_General_CI_AS", max_length=9)),
                    _table("dbo.C", _column("K", "varchar", "Latin1_General_BIN2", max_length=9))])

    def surrogate(table):
        return SimpleNamespace(name="KEY", privacy="surrogate", source_table=table,
                               source_column="K")

    mixed = SimpleNamespace(structure=[SimpleNamespace(columns=[surrogate("dbo.A")]),
                                       SimpleNamespace(columns=[surrogate("dbo.B")])])
    with pytest.raises(builder.BuildRefused, match=r"dbo\.A\.K.*dbo\.B\.K"):
        builder._case_sensitive_surrogates(mixed, graph)
    # Both tell case apart: one surrogate, case kept in both tables.
    alike = SimpleNamespace(structure=[SimpleNamespace(columns=[surrogate("dbo.A")]),
                                       SimpleNamespace(columns=[surrogate("dbo.C")])])
    assert builder._case_sensitive_surrogates(alike, graph) == ["KEY"]
