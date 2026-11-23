"""SQL Server errors, said plainly.

A driver error reaches a person as "('42S02', '[42S02] [Microsoft][ODBC Driver
18 for SQL Server][SQL Server]Invalid object name ...')". What it means and
what to do about it is a sentence; the code SQL Server gave stays at the end
for whoever has to look further.
"""

from __future__ import annotations

import re

# (what the driver says, the sentence). First match wins; {1} is the first
# quoted name in the message, where there is one.
_KNOWN: list[tuple[str, str]] = [
    (r"Cannot open database",
     "The database{q} does not exist on this server, or this login cannot open it."),
    (r"Login failed for user",
     "SQL Server refused the login{q}. Check the user name and password in the "
     "connection, or use Windows login."),
    (r"certificate chain was issued by an authority that is not trusted|"
     r"certificate verify failed",
     "SQL Server's certificate is not trusted on this machine. Turn on Trust "
     "server certificate for this connection (SDG_MSSQL_TRUST_CERT=yes)."),
    (r"Data source name not found|Can't open lib|driver.*not found|IM002",
     "The ODBC driver named in the settings is not installed. Install "
     "\"ODBC Driver 18 for SQL Server\", or set SDG_MSSQL_DRIVER to one that is."),
    (r"server was not found|Named Pipes Provider|TCP Provider|"
     r"network-related|Login timeout expired|actively refused",
     "SQL Server could not be reached. Check the server name and port, that "
     "SQL Server is running, and that it accepts TCP connections."),
    (r"Invalid object name",
     "The table{q} is missing from this database. Run scripts\\upgrade.cmd "
     "(at a customer, the scripts in scripts\\customer) to create it."),
    (r"Invalid column name",
     "The column{q} is missing from a table this tool uses. Run "
     "scripts\\upgrade.cmd (at a customer, scripts\\customer\\01_sdm_upgrade.sql) "
     "to add it."),
    (r"permission was denied|The .* permission was denied|not have permission",
     "This login is not allowed to do that{q}. Ask for the permission, or "
     "connect with a login that has it."),
    (r"Cannot insert the value NULL into column",
     "A required column{q} was left empty. The table may need its id to be "
     "assigned, or a value this tool did not supply."),
    (r"Violation of (PRIMARY KEY|UNIQUE KEY) constraint|duplicate key",
     "That would repeat a value that must be unique{q}."),
    (r"conflicted with the (FOREIGN KEY|REFERENCE) constraint",
     "That row points at one that does not exist, or is still pointed at{q}."),
    (r"String or binary data would be truncated|right truncation",
     "A value is longer than its column allows{q}."),
    (r"Query timeout expired|HYT00|timeout",
     "SQL Server did not answer in time. It may be busy; try again."),
    (r"Communication link failure|connection is busy|TCP Provider: An existing "
     r"connection was forcibly closed",
     "The connection to SQL Server dropped. Try again; if it keeps happening, "
     "check the network to the server."),
]


def _code(text: str) -> str:
    found = re.search(r"\[(\w{5})\]", text) or re.search(r"'(\w{5})'", text)
    return found.group(1) if found else ""


def _named(text: str) -> str:
    found = re.search(r"'([^'\[\]]{1,128})'", text.split("]")[-1]) \
        or re.search(r"\"([^\"]{1,128})\"", text)
    return f" {found.group(1)}" if found else ""


def is_database_error(exc: BaseException) -> bool:
    try:
        import pyodbc

        if isinstance(exc, pyodbc.Error):
            return True
    except ImportError:  # pragma: no cover
        pass
    return "ODBC" in str(exc) or "SQL Server" in str(exc)


# Messages SQL Server sends beside an error that are not the error: an
# aggregate skipping NULLs, a value converted. Never what a person is told.
_INFORMATIONAL = re.compile(r"^\s*Warning:|Null value is eliminated|"
                            r"Changed (database|language) context", re.IGNORECASE)


# One driver message: "[42S02] [Microsoft][ODBC Driver 18 for SQL Server]
# [SQL Server]Invalid object name 'x'. (208) (SQLExecDirectW)", several of
# them joined by "; " in one error.
_MESSAGE = re.compile(r"\[(\w{5})\]\s*\[Microsoft\](?:\[[^\]]*\])+(.*?)(?=;\s*\[\w{5}\]\s*\[Microsoft\]|$)",
                      re.DOTALL)


def _messages(text: str) -> list[tuple[str, str]]:
    """(state, message) for each message the driver joined, the error first.

    A warning that rode along -- an aggregate skipping NULLs -- comes after
    every real error, and is kept only when nothing else was said.
    """
    found = []
    for state, message in _MESSAGE.findall(text):
        message = re.sub(r"\s*\(SQL\w+\)", "", message)
        # The message number at the end, not a value in parentheses inside it.
        message = re.sub(r"\s*\(\d+\)[\s'\");]*$", "", message).strip(" '\");")
        if message:
            found.append((state, message))
    real = [m for m in found if not _INFORMATIONAL.search(m[1])]
    return real + [m for m in found if m not in real]


def plain(exc: BaseException) -> str:
    """A sentence for an error, with SQL Server's own code kept at the end."""
    text = str(exc)
    if not is_database_error(exc):
        return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    messages = _messages(text) or [(_code(text), text)]
    real = [m for m in messages if not _INFORMATIONAL.search(m[1])] or messages
    for pattern, sentence in _KNOWN:
        for code, message in real:
            if re.search(pattern, message, re.IGNORECASE):
                said = sentence.replace("{q}", _named(message))
                return _with_code(said, code, message)
    code, message = real[0]
    return _with_code("SQL Server reported an error.", code, message)


def _with_code(said: str, code: str, message: str) -> str:
    # A warning's state (01xxx) can stand in for the error's; it is not shown.
    code = "" if not code or code.startswith("01") else code
    return f"{said} (SQL Server{' ' + code if code else ''}: {message[:300]})"
