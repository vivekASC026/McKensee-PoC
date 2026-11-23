"""Error detail that names its own origin.

A message like `invalid literal for int() with base 10: ''` says what happened
but not where, and the same text can come from any of a dozen int() calls across
the loaders. Attaching the raising frame turns one round trip of guesswork into
a location.

Used on the routes that surface ValueError as a 422, since those are the ones a
user actually reads.
"""

from __future__ import annotations

import traceback
from pathlib import Path


_LIBRARY_MARKERS = ("site-packages", "dist-packages", "lib/python", "lib\\python")


def _is_ours(filename: str) -> bool:
    """Whether a frame belongs to this project rather than an installed library."""
    lowered = filename.replace("\\", "/").lower()
    return not any(m.replace("\\", "/") in lowered for m in _LIBRARY_MARKERS)


def _frames(exc: BaseException) -> list:
    out = []
    tb = exc.__traceback__
    while tb is not None:
        out.append(tb)
        tb = tb.tb_next
    return out


def _describe(tb) -> str:
    frame = tb.tb_frame
    return (f"{Path(frame.f_code.co_filename).name}:{tb.tb_lineno} "
            f"in {frame.f_code.co_name}()")


def origin(exc: BaseException) -> str:
    """Where an exception came from, in terms of code we can actually change.

    Reporting the innermost frame alone sent a real investigation to
    `array.py:1729 in to_numpy()` — pandas internals, true but useless, when the
    actionable line was the `.astype(int)` in our own module one frame up. This
    reports our deepest frame, and names the library frame after it only when
    the two differ.
    """
    frames = _frames(exc)
    if not frames:
        return "origin unknown"

    innermost = frames[-1]
    ours = next((tb for tb in reversed(frames)
                 if _is_ours(tb.tb_frame.f_code.co_filename)), None)

    if ours is None:
        return _describe(innermost)
    if ours is innermost:
        return _describe(ours)
    return f"{_describe(ours)} → {_describe(innermost)}"


def _build() -> str:
    """The running build stamp, for the error message itself.

    Included because a fixed bug and an undeployed fix produce identical
    symptoms, and two rounds were spent on that ambiguity. An error that names
    its own build settles it without anyone having to check.
    """
    try:
        from sdg import __version__

        return __version__
    except Exception:
        return "unknown"


def detail(exc: BaseException, context: str = "") -> str:
    """The message a user sees: what failed, where in our code, and which build.

    Deliberately not a full traceback — that goes to the console. This is what
    fits in a toast and still identifies the call.
    """
    parts = [str(exc) or type(exc).__name__]
    if context:
        parts.append(f"[{context}]")
    parts.append(f"({type(exc).__name__} at {origin(exc)} · build {_build()})")
    return " ".join(parts)


def log(exc: BaseException, context: str = "") -> None:
    """Full traceback to the server console, for anything worth investigating."""
    print(f"\n[error] {context or 'unhandled'}: {type(exc).__name__}: {exc}")
    traceback.print_exception(type(exc), exc, exc.__traceback__)
