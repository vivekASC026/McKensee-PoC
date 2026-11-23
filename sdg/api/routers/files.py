"""Files as a source: a folder on this machine, and what each file in it is.

A database is connected, read and measured; so is a folder of files. Connect
names the folder. Discover & Profile reads every delimited or fixed-width file
in it -- its kind, its fields, their widths and types -- and measures what each
field holds. Map and Review reads any of them as a layout sample.

Only what the folder holds is read, by its own name: a name with a path in it
is refused, so nothing outside the folder can be asked for.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from sdg.analyzer.services import layout_service as ls
from sdg.discovery.errors import plain

router = APIRouter(prefix="/files", tags=["files"])

log = logging.getLogger(__name__)

EXTENSIONS = (".txt", ".csv", ".dat", ".tsv")
# A file is profiled from its first rows: enough to measure a field, without
# reading a gigabyte extract end to end to show a screen.
MAX_BYTES = 4_000_000
MAX_ROWS = 20_000


# Kept in work/ but not with any one database's files: it stays put when
# another database is chosen (Service.STAYS), and is not the settings file a
# plan is built from, so naming a folder never makes a plan stale.
SOURCE_FILE = "files-source.json"


def _setting() -> Path:
    """Where the folder is remembered."""
    return _service().work / SOURCE_FILE


def _service():
    from sdg.api.app import service

    return service


def _folder() -> Path | None:
    import json

    path = _setting()
    if not path.exists():
        return None
    named = json.loads(path.read_text(encoding="utf-8")).get("folder")
    return Path(named) if named else None


def _listing(folder: Path | None) -> dict[str, Any]:
    if folder is None:
        return {"folder": None, "files": []}
    if not folder.is_dir():
        return {"folder": str(folder), "files": [], "missing": True}
    files = []
    for path in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if path.is_file() and path.suffix.lower() in EXTENSIONS:
            stat = path.stat()
            files.append({
                "name": path.name,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            })
    return {"folder": str(folder), "files": files}


def _file(name: str) -> Path:
    """A file in the folder, by its own name and nothing else."""
    folder = _folder()
    if folder is None:
        raise HTTPException(status_code=409, detail="Name the folder under Layouts and samples › Folder first.")
    if not name or Path(name).name != name:
        raise HTTPException(status_code=422, detail="A file is named by its name alone.")
    path = folder / name
    if not path.is_file() or path.suffix.lower() not in EXTENSIONS:
        raise HTTPException(status_code=404, detail=f"No file {name} in {folder}.")
    return path


def _head(path: Path) -> bytes:
    with path.open("rb") as handle:
        content = handle.read(MAX_BYTES)
    # Cut at the last whole line, so a profile never measures half a row.
    if len(content) == MAX_BYTES and b"\n" in content:
        content = content[: content.rfind(b"\n") + 1]
    return content


class SourceRequest(BaseModel):
    folder: str


@router.get("/source")
def source() -> dict[str, Any]:
    return _listing(_folder())


@router.put("/source")
def set_source(request: SourceRequest) -> dict[str, Any]:
    # As pasted from Explorer: "C:\data\files", quotes and all.
    folder = Path(request.folder.strip().strip('"').strip("'").strip()).expanduser()
    if not folder.is_dir():
        raise HTTPException(status_code=422, detail=f"{folder} is not a folder on this machine.")
    import json

    _setting().write_text(json.dumps({"folder": str(folder.resolve())}), encoding="utf-8")
    return _listing(folder.resolve())


def _values(parsed: dict[str, Any], text: str) -> list[list[str]]:
    """Each field's values, column by column, from the file's first rows."""
    fields = parsed["fields"]
    if parsed["kind"] == "csv":
        rows = [r for r in csv.reader(io.StringIO(text), delimiter=parsed["delimiter"]) if r]
        rows = rows[1:MAX_ROWS + 1]
        return [[r[i].strip() if i < len(r) else "" for r in rows] for i in range(len(fields))]
    lines = [line.rstrip("\r\n") for line in text.split("\n") if line.strip()]
    if parsed.get("header_row"):
        lines = lines[1:]
    lines = lines[:MAX_ROWS]
    return [[line[f["start"]: f["start"] + f["length"]].strip() for line in lines]
            for f in fields]


def profile(path: Path) -> dict[str, Any]:
    """One file: its kind, its fields, and what each holds."""
    content = _head(path)
    try:
        parsed = ls.parse_sample(path.name, content)
    except ValueError as e:
        return {"name": path.name, "kind": "", "row_count": 0, "fields": [], "error": str(e)}
    text = content.decode("utf-8", errors="replace")
    columns = _values(parsed, text)
    rows = len(columns[0]) if columns else 0
    for field, values in zip(parsed["fields"], columns):
        filled = [v for v in values if v]
        field["empty_rate"] = round(1 - len(filled) / rows, 4) if rows else 0.0
        field["distinct"] = len(set(filled))
        field["samples"] = list(dict.fromkeys(filled))[:5]
        field["max_width"] = max([len(v) for v in values] + [field.get("max_width") or 0])
    return {
        "name": path.name,
        "kind": parsed["kind"],
        "delimiter": parsed.get("delimiter"),
        "row_count": rows,
        "header_row": parsed.get("header_row"),
        "fields": parsed["fields"],
        "truncated": rows >= MAX_ROWS or path.stat().st_size > MAX_BYTES,
    }


def _profile_or_say(path: Path) -> dict[str, Any]:
    """One file's profile; a file that cannot be read says why, and the rest
    of the folder is still read."""
    try:
        return profile(path)
    except Exception as e:  # one unreadable file is not the folder's failure
        from sdg.discovery.errors import plain

        return {"name": path.name, "kind": "", "row_count": 0, "fields": [],
                "error": f"Could not be read: {plain(e)}"}


@router.get("/profile")
def profiles() -> dict[str, Any]:
    folder = _folder()
    listing = _listing(folder)
    return {
        "folder": listing["folder"],
        "files": [_profile_or_say(folder / f["name"]) for f in listing["files"]] if folder else [],
    }


class ParseRequest(BaseModel):
    name: str


@router.post("/parse")
def parse(request: ParseRequest) -> dict[str, Any]:
    """A file in the folder read as a layout sample, the way an upload is."""
    path = _file(request.name)
    try:
        parsed = ls.parse_sample(path.name, _head(path))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    # Without the rules every field would get the blank default (as in
    # routers/layouts.parse): said, rather than shown as assigned.
    try:
        rules = ls.get_rules()
    except Exception as e:
        log.exception("Generation rules could not be read for a folder file")
        raise HTTPException(status_code=500, detail=(
            f"The generation rules could not be read, so no field was assigned. "
            f"Try again when they can be read: {plain(e)}"))
    parsed["fields"] = ls.auto_assign(parsed["fields"], rules)
    parsed["sample_name"] = path.name
    parsed["n_default"] = sum(1 for f in parsed["fields"] if f.get("is_default"))
    return parsed
