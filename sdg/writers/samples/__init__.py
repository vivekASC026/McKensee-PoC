"""Files modeled on a sample: everything copied, except what a config names.

One way of working for every file format: a sample file is the model, a
config names the places in it that take generated values (a path and an
algorithm), and the masking tool's config for that format is the master a
generation config starts from. What differs per format is how a place is
addressed and what repeats; that lives in the format's module, which the
screens and the API never look inside.

A format module provides:

    FORMAT, LABEL, MEDIA, EXTENSION, VALIDATION (a checker's name, or None)
    detect(text) -> bool                   the sample is this format
    read_sample(text) -> sample            with .kind and .version
    facts(sample) -> (counts, summary)     what the sample holds, for the screen
    subject(kind) -> "members" | "claims"  what records a file of that kind is written for
    read_config(body) -> entries           a generation config, or a masking config
    write_config(rows, **meta) -> dict     rows chosen on the screen, as a config
    positions(config) -> int               how many places a saved config generates
    used(entries) -> set[str]              the algorithm names entries refer to
    propose(sample, entries, algorithms)   every place of the sample, with a proposal
    stream(sample, entries, records, rules, seed=, as_of=) -> Iterator[str]

and may provide:

    check(text, profile) -> report         the written file checked as the format checks
    package(chunks, kind) -> Iterator[bytes], TEXT_MEDIA
                                           the download packed (CDA: a zip of documents);
                                           stream's text is what a preview shows
"""

from __future__ import annotations

from types import ModuleType

from sdg.writers.samples import cda, fhir, hl7v2, x12
from sdg.writers.samples.base import SampleError, algorithm_rules

FORMATS: dict[str, ModuleType] = {m.FORMAT: m for m in (x12, fhir, hl7v2, cda)}

# The business pack's modules that offer each format: every one is a health
# format, so another pack models no file on a sample unless it names one.
PACK_MODULES: dict[str, tuple[str, ...]] = {
    "x12": ("x12",), "fhir": ("fhir",), "hl7v2": ("hl7v2",), "cda": ("cda", "hl7v3"),
}

__all__ = ["FORMATS", "SampleError", "algorithm_rules", "detect", "module", "offered"]


def offered(format_: str) -> bool:
    """Whether the active business pack offers files of this format."""
    from sdg import packs

    return any(packs.offers(m) for m in PACK_MODULES.get(format_, ()))


def _require(fmt: ModuleType) -> ModuleType:
    from sdg import packs

    if not offered(fmt.FORMAT):
        packs.require(PACK_MODULES[fmt.FORMAT][0], fmt.LABEL)
    return fmt


def module(format_: str) -> ModuleType:
    try:
        fmt = FORMATS[format_]
    except KeyError:
        raise SampleError(f"No sample format {format_!r}.") from None
    return _require(fmt)


def detect(text: str) -> ModuleType:
    """The format a sample is written in, from its content (one the active
    pack does not offer is read, and refused by name)."""
    for fmt in FORMATS.values():
        if fmt.detect(text):
            return _require(fmt)
    known = ", ".join(m.LABEL for m in FORMATS.values() if offered(m.FORMAT)) or "none"
    raise SampleError(f"The sample is not a format a file can be modeled on ({known}).")
