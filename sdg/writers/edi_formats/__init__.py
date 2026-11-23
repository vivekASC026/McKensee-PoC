"""EDI transaction sets, described rather than coded.

The first 834 and 837 writers built their segments in Python, with FACETS
column names written into the code. That works for exactly two transaction
sets and for exactly one source schema, and adding an 835 meant writing a third
one the same way.

A transaction set is a list of segments, each a list of elements, each either a
value from the record or a constant. That is a description, not a program, so
it lives in a JSON file beside the others and is picked by name:

    {
      "id": "837P",
      "subject": "claims",
      "groups": [
        {"opens_on": "CLCL_ID",
         "segments": [
           {"id": "CLM", "elements": [
              {"from": "CLCL_ID"},
              {"from": "CLCL_TOT_CHG", "default": "0"},
              {"skip": 2},
              {"composite": ["11", "B", "1"]},
              {"value": "Y"}
           ]}
         ]},
        ...
      ]
    }

The shape is borrowed from the masking tool this organization already runs,
which addresses segments and element positions the same way and binds each to
an algorithm. Here the binding is to a column instead of an algorithm, because
one tool changes a value that exists and the other produces one that does not.

A format a person adds is a file in this directory. Nothing is compiled in.
"""

from sdg.writers.edi_formats.registry import (
    EdiFormat,
    formats,
    load_format,
    stream_format,
)

__all__ = ["EdiFormat", "formats", "load_format", "stream_format"]
