"""Caches that make a file from a sample fast, and never change what is written.

The lookups keep what they worked out for a whole streamed file (not only
while a plan runs), set around each piece so a piece drawn from another
thread's copy of the context still sees them. A CDA document's places are
walked once and walked again whenever the document changes shape.
"""

from __future__ import annotations

import contextvars
import copy
from pathlib import Path

from sdg.engine.generators import lookup
from sdg.writers.samples import cda

HERE = Path(__file__).resolve().parent / "fixtures" / "cda"
FAMILY = "recordTarget/patientRole/patient/name/family"


def test_lookups_cache_for_each_piece_of_a_stream_and_let_go_after():
    seen: list[bool] = []

    def pieces():
        for n in range(3):
            seen.append(lookup._CACHING.get())
            lookup._AGREEING[("piece", n)] = ((), n)
            yield n

    assert lookup._CACHING.get() is False
    stream = lookup.caching_each(pieces())
    # Each piece drawn in a fresh copy of the context, as a streamed response does.
    drawn = [contextvars.copy_context().run(next, stream) for _ in range(3)]
    assert drawn == [0, 1, 2] and seen == [True, True, True]
    assert lookup._CACHING.get() is False, "never left switched on between pieces"
    assert len(lookup._AGREEING) == 3, "kept from piece to piece"
    assert list(stream) == []
    assert lookup._AGREEING == {}, "let go when the stream ends"


def test_a_stream_closed_early_lets_its_caches_go():
    lookup._AGREEING[("early",)] = ((), 1)
    stream = lookup.caching_each(iter(range(5)))
    assert next(stream) == 0
    stream.close()
    assert lookup._AGREEING == {}


def _document():
    sample = cda.read_sample((HERE / "ccd.xml").read_text(encoding="utf-8"))
    tpl = cda.Tpl(sample.root, sample.prefixes, [], [])
    return cda.Reader(sample), cda.Item(copy.deepcopy(sample.root), tpl)


def test_a_documents_places_are_walked_again_when_it_changes_shape():
    reader, item = _document()
    was = reader.get(item, FAMILY)
    assert was and reader.original(item, FAMILY) == was
    # A value written is read back (the places are kept, the values are live).
    reader.set(item, FAMILY, "Written")
    assert reader.get(item, FAMILY) == "Written"
    assert reader.original(item, FAMILY) == was, "the sample's own is never changed"
    # An attribute added is a place from then on.
    flavor = FAMILY + "/@nullFlavor"
    assert not reader.matches(item, flavor)
    reader.set(item, flavor, "UNK")
    assert reader.matches(item, flavor) and reader.get(item, flavor) == "UNK"
    # A place dropped is no place any more.
    reader.drop(item, flavor)
    assert not reader.matches(item, flavor)
    reader.drop(item, FAMILY)
    assert not reader.matches(item, FAMILY) and reader.get(item, FAMILY) is None

