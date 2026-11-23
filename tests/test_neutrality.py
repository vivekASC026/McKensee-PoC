"""Health-insurance words outside the health pack: fewer, never more.

SDM is becoming domain neutral: a business pack names its people and
records, and the code reads them from it. Until it is done, the lines that
still say member, claim, FACETS and the like are counted here, and a count
may only fall. The health file formats (X12, SNIP, HL7, FHIR, CDA and the
835's payments) are health by nature and are not counted.

Since phase 6a what a person sees is held outright: under the banking pack no
screen says a health word (tests/test_bank_browser.py), and the bank is read,
planned, generated and written again with no health word in what it makes
(tests/test_bank_e2e.py); since phase 6b the bank's patterns are found, kept,
reported and read from a story, on screen and by the server, in the bank's
words too. The lines counted here are what remains in the code: the health
pack keeps its keys (members, claims) and the old routes stay as aliases, so
no count need reach zero -- each may only fall.

When a count falls, keep the new one:
    set SDM_RECORD_BASELINES=1 && pytest tests/test_neutrality.py
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
KEPT = Path(__file__).parent / "fixtures" / "neutrality-counts.json"
RECORDING = os.getenv("SDM_RECORD_BASELINES") == "1"

TERMS = {
    "member": re.compile(r"member", re.I),
    "claim": re.compile(r"claim", re.I),
    "subscriber": re.compile(r"subscriber", re.I),
    "diagnosis": re.compile(r"diagnos", re.I),
    "provider": re.compile(r"provider", re.I),
    "FACETS": re.compile(r"FACETS|CMC_|MEME_|CLCL_|SBSB_|GRGR_|CSPI_|PDPD_"),
}

WHERE = {"server": ("sdg", ("*.py",)), "screens": ("ui/src", ("*.ts", "*.tsx"))}

# Health by nature: offered only by the health pack.
HEALTH_FORMATS = (
    "sdg/writers/edi_formats/", "sdg/writers/snip/", "sdg/writers/samples/",
    "sdg/writers/adjudication.py", "sdg/packs/health/",
)


def _counts() -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for area, (folder, globs) in WHERE.items():
        held = {term: 0 for term in TERMS}
        for glob in globs:
            for path in sorted((ROOT / folder).rglob(glob)):
                relative = path.relative_to(ROOT).as_posix()
                if relative.startswith(HEALTH_FORMATS) or "__pycache__" in relative:
                    continue
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    for term, pattern in TERMS.items():
                        if pattern.search(line):
                            held[term] += 1
        counts[area] = held
    return counts


def test_health_words_outside_the_pack_never_grow():
    now = _counts()
    kept = json.loads(KEPT.read_text(encoding="utf-8"))
    grown = {f"{area} {term}": f"{kept[area][term]} -> {n}"
             for area, terms in now.items() for term, n in terms.items()
             if n > kept[area][term]}
    # Checked before anything is kept: recording keeps a fall, never a rise.
    assert not grown, (f"lines naming health terms outside the health pack grew: {grown}. "
                       "Read the term from the business pack instead.")
    if RECORDING:
        KEPT.write_text(json.dumps(now, indent=1, sort_keys=True) + "\n", encoding="utf-8")
