"""Database error text kept out of the log.

A SQL Server message can quote a row's values: "The duplicate key value is
(M1234567)", "Conversion failed when converting the varchar value 'Smith'".
Logged as it is, a member id or a name lands in a log file that is not
protected like the data. Every log record is therefore made with the driver's
message withheld: the error's type and SQLSTATE stay, and so does the
traceback, so the log still says what failed and where.

Installed once, when the sdg package is imported, for every logger.
"""

from __future__ import annotations

import logging
import re
import traceback
from types import TracebackType

# The driver's own wording: "[42S02] [Microsoft][ODBC Driver 18 for SQL Server]...".
_DRIVER_TEXT = re.compile(r"\[Microsoft\]|\[ODBC Driver|\[SQL Server\]")
_STATE = re.compile(r"\[([0-9A-Z]{5})\]")
_WITHHELD = "the driver's message is withheld from the log, as it can quote row values"

_installed = False

try:
    from pyodbc import Error as _DriverError
except ImportError:  # pragma: no cover
    _DriverError = None


def _is_database_error(exc: BaseException) -> bool:
    if _DriverError is not None and isinstance(exc, _DriverError):
        return True
    return bool(_DRIVER_TEXT.search(str(exc)))


def _state(exc: BaseException) -> str:
    """The SQLSTATE: the driver's first argument, or a bracketed [42S02]. Only
    a well-formed state is taken, never a quoted word that could be a value."""
    first = exc.args[0] if exc.args else ""
    if isinstance(first, str) and re.fullmatch(r"[0-9A-Z]{5}", first):
        return first
    found = _STATE.search(str(exc))
    return found.group(1) if found else ""


def summary(exc: BaseException) -> str:
    """What a log may say about an error: a database error by its type and
    SQLSTATE only, anything else as it is."""
    if not _is_database_error(exc):
        return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    state = _state(exc)
    return f"{type(exc).__name__} (SQL Server{' ' + state if state else ''}; {_WITHHELD})"


def _format(exc_type: type[BaseException] | None, exc: BaseException | None,
            tb: TracebackType | None) -> str:
    """A traceback as logging writes one, each database error in the chain said
    by its summary instead of its message."""
    if exc is None:
        return "".join(traceback.format_exception(exc_type, exc, tb))
    chain: list[tuple[BaseException, str]] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    link = ""
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append((current, link))
        if current.__cause__ is not None:
            current, link = current.__cause__, (
                "\nThe above exception was the direct cause of the following exception:\n\n")
        elif current.__context__ is not None and not current.__suppress_context__:
            current, link = current.__context__, (
                "\nDuring handling of the above exception, another exception occurred:\n\n")
        else:
            current = None
    lines: list[str] = []
    for error, after in reversed(chain):
        frames = traceback.format_tb(error.__traceback__)
        if frames:
            lines.append("Traceback (most recent call last):\n")
            lines.extend(frames)
        lines.append(summary(error) + "\n")
        if after:
            lines.append(after)
    return "".join(lines).rstrip("\n")


def _holds_database_error(exc: BaseException | None) -> bool:
    """Whether the error, or one it was raised from or during, is a database error."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        if _is_database_error(exc):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    return False


def _withheld_arg(value: object) -> object:
    if isinstance(value, BaseException) and _is_database_error(value):
        return summary(value)
    if isinstance(value, str) and _DRIVER_TEXT.search(value):
        return _DRIVER_TEXT.split(value, 1)[0].rstrip(" ([:") + f" ({_WITHHELD})"
    return value


def install() -> None:
    """Make every log record with database error text withheld. Idempotent."""
    global _installed
    if _installed:
        return
    make = logging.getLogRecordFactory()

    def safe_record(*args, **kwargs) -> logging.LogRecord:
        record = make(*args, **kwargs)
        try:
            if isinstance(record.args, tuple) and record.args:
                record.args = tuple(_withheld_arg(a) for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: _withheld_arg(v) for k, v in record.args.items()}
            if isinstance(record.msg, (BaseException, str)):
                record.msg = _withheld_arg(record.msg)
            if record.exc_info and _holds_database_error(record.exc_info[1]):
                # Formatted here and the original dropped, so no handler or
                # formatter can format the driver's message itself; the
                # standard Formatter appends exc_text as it is. Any other
                # error keeps its record whole.
                record.exc_text = _format(*record.exc_info)
                record.exc_info = None
        except Exception:  # noqa: BLE001 -- a log line must never raise
            record.msg = "(log text withheld: it could not be checked)"
            record.args = ()
            if record.exc_info:
                record.exc_text = "(the error could not be summarized)"
                record.exc_info = None
        return record

    logging.setLogRecordFactory(safe_record)
    _installed = True
