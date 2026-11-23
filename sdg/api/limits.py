"""How large an uploaded sample may be: one rule for every way a sample comes in.

A sample is a few records to model a file on. The limit is 20 MB of UTF-8
bytes, measured on what was sent -- a sample of multibyte characters reaches it
with fewer characters than a plain one. The body has been received by the time
it is checked, so it spares the parsing and modeling of an over-sized sample,
not the receiving of it.
"""

from __future__ import annotations

from fastapi import HTTPException

MAX_SAMPLE_MB = 20
MAX_SAMPLE_BYTES = MAX_SAMPLE_MB * 1024 * 1024

# UTF-8 spends at most four bytes on a character.
_MOST_BYTES_PER_CHAR = 4


def sample_size(sample: str | bytes) -> int:
    """The sample's size in UTF-8 bytes."""
    if isinstance(sample, bytes):
        return len(sample)
    return len(sample.encode("utf-8", errors="surrogatepass"))


def sample_fits(sample: str | bytes | None) -> None:
    """Refused with 413 and a sentence when the sample is over the limit."""
    if not sample:
        return
    if isinstance(sample, str):
        # Decided without encoding where the length alone settles it.
        if len(sample) > MAX_SAMPLE_BYTES:
            _refuse()
        if len(sample) * _MOST_BYTES_PER_CHAR <= MAX_SAMPLE_BYTES:
            return
    if sample_size(sample) > MAX_SAMPLE_BYTES:
        _refuse()


def _refuse() -> None:
    raise HTTPException(status_code=413, detail=(
        f"The sample is larger than {MAX_SAMPLE_MB} MB. Use a smaller sample: "
        "a few records are enough to model a file on."))
