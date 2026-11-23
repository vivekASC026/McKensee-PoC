"""Review decisions already made on this project, applied to a new installation.

Decisions live in `work/`, which is one installation's state and never shipped,
so a new machine starts with every question in Review open again -- including
ones that were answered long ago. These are those answers, recorded through the
same path the Review screen uses, followed by a plan rebuild so they take
effect. A column decided differently on the new machine since is left alone.

    python seed/decisions.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DECIDED = [
    # The claim line's procedure code. Its format matches PROCEDURE_CODE
    # exactly, but nothing in the name says so, so mapping asks. Without it no
    # rule produces a procedure code and files carry none.
    # The rule is left to the catalog -- for a code set, the store's codes --
    # rather than the pattern the proposal fitted, which invents codes.
    {"table": "dbo.CMC_CDML_CL_LINE", "column": "CDML_SVC_CD",
     "approved": True, "element": "PROCEDURE_CODE", "generator": None, "params": None},
]


def main() -> int:
    from sdg.api.app import service

    # These are decisions about the source's columns. Applied while another
    # database is worked on, they would land in that database's review.
    working = service.working_database()["database"]
    if working != service.settings.source_db:
        print(f"  working on {working}, not {service.settings.source_db}: left alone")
        return 0

    held = {
        (c.get("table"), c.get("column")): c
        for c in service.read_decisions().get("columns", [])
    }
    applied = 0
    for decision in DECIDED:
        existing = held.get((decision["table"], decision["column"]))
        if existing and existing.get("approved") and \
                existing.get("element") != decision["element"]:
            print(f"  {decision['column']}: decided otherwise here, left alone")
            continue
        service.record_decision(dict(decision))
        print(f"  {decision['column']} -> {decision['element']}")
        applied += 1
    if applied:
        service.build()
        print("  plan rebuilt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
