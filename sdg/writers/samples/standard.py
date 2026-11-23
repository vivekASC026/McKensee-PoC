"""The standard file each format starts from: a made-up sample and the master
config whose places it proposes (defaults/index.json). They are offered on the
FHIR, HL7 v2 and CDA tabs as the EDI tab offers its transaction sets, so a file
can be written before the customer has given a sample of their own. A person's
changes are saved as a new config; nothing here is written to.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).with_name("defaults")


def listing() -> list[dict[str, Any]]:
    """Every standard file the active pack offers: id, format, name, and the
    names of its sample and master."""
    from sdg.writers.samples import offered

    return [d for d in _every() if offered(d["format"])]


def _every() -> list[dict[str, Any]]:
    return json.loads((HERE / "index.json").read_text(encoding="utf-8"))["defaults"]


def files(default_id: str) -> dict[str, str] | None:
    """The sample's and the master's names and text, or None for an unknown id.
    Every standard file is found, offered or not: what refuses a format the
    business pack does not offer is the format (samples.module), by name."""
    entry = next((d for d in _every() if d["id"] == default_id), None)
    if entry is None:
        return None
    return {
        "sample_name": entry["sample"],
        "sample": (HERE / entry["sample"]).read_text(encoding="utf-8"),
        "master_name": f"standard {entry.get('label') or entry['format'].upper()} ({entry['master']})",
        "master": (HERE / entry["master"]).read_text(encoding="utf-8"),
    }
