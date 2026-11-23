"""Build a zip somebody can set this up from.

What goes in is exactly what git tracks, the built interface included. Using
git's own list rather than a set of exclude patterns means the zip can never
carry the things `.gitignore` exists to keep out -- the password in `.env`
above all, but also the 400MB virtual environment, the query cache, and one
installation's working state.

Commit first: a change that is not committed is not in the zip.

    python scripts/package.py            dist/sdm-<commit>.zip
    python scripts/package.py --out X    somewhere else

For a customer other than the one the repository's notes describe:

    python scripts/package.py --customer-neutral --forbid WORD [--forbid WORD ...]

leaves out the notes on one installation's history (STATUS.md,
ENVIRONMENTS.md, AUDIT.md) and the development plans, ships
scripts/customer/new-customer.env.example as customer.env.example, and reads
every file written for each WORD (the other customer's names, servers and
databases, given here so they are written nowhere), refusing to keep the zip
when one is found.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Tracked, and checked for: without it the far machine needs Node.
REQUIRED = ["ui/dist/index.html"]

# A sanity check rather than a filter. If any of these ever appear in the list,
# something has gone wrong with .gitignore and the zip should not be written.
NEVER = (".env", ".venv/", "node_modules/", "sdg/analyzer/cache/", "work/")

# Tracked for the team, never handed to a customer: the original build brief
# and design notes, and the instructions for Claude Code.
INTERNAL = (
    "CLAUDE.md",
    "docs/PoC-Build-Instructions-Claude-Code.docx",
    "docs/Synthetic-Data-Generation-Design.docx",
)

LIMIT_MB = 25

# Left out of a customer-neutral zip: one installation's history, and the
# plans and designs the work was built from.
NEUTRAL_LEFT_OUT = ("STATUS.md", "ENVIRONMENTS.md", "AUDIT.md",
                    "docs/source-accelerator-design.md", "docs/x12-single-engine-plan.md")
NEUTRAL_LEFT_OUT_PATTERNS = ("docs/day0-accelerator-phase",)
# Shipped under another name in a customer-neutral zip: the placeholders only.
NEUTRAL_ENV = ("scripts/customer/new-customer.env.example", "scripts/customer/customer.env.example")


def neutral(files: list[str]) -> list[str]:
    """The files a customer-neutral zip carries, by their names in the repository."""
    return [f for f in files if f not in NEUTRAL_LEFT_OUT and f != NEUTRAL_ENV[1]
            and not f.startswith(NEUTRAL_LEFT_OUT_PATTERNS)]


# Files that are zips themselves (a Word document is): read inside too.
NESTED = (".docx", ".xlsx", ".pptx", ".zip")


def _entries(zf: zipfile.ZipFile, prefix: str = ""):
    """Each file of the zip, and of every zip inside it, as (name, bytes)."""
    import io

    for name in zf.namelist():
        data = zf.read(name)
        yield prefix + name, data
        if name.lower().endswith(NESTED):
            try:
                inner = zipfile.ZipFile(io.BytesIO(data))
            except zipfile.BadZipFile:
                continue
            with inner:
                yield from _entries(inner, f"{prefix}{name}!")


def found_words(out: Path, words: list[str]) -> dict[str, list[str]]:
    """Each forbidden word (case aside), with the files of the zip that hold
    it: in a file's name or its text (UTF-8 or UTF-16), inside documents too."""
    hits: dict[str, list[str]] = {}
    with zipfile.ZipFile(out) as zf:
        for name, data in _entries(zf):
            low = data.lower()
            for word in words:
                w = word.lower()
                if (w in name.lower() or w.encode("utf-8") in low
                        or w.encode("utf-16-le") in low):
                    hits.setdefault(word, []).append(name)
    return hits


def tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return [line for line in out.stdout.splitlines()
            if line.strip() and line not in INTERNAL]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--customer-neutral", action="store_true",
                        help="for another customer: no installation notes, placeholder settings")
    parser.add_argument("--forbid", action="append", default=[], metavar="WORD",
                        help="a word no file of the zip may hold (repeat for each)")
    args = parser.parse_args(argv)
    if args.forbid and not args.customer_neutral:
        parser.error("--forbid checks a --customer-neutral zip")
    if args.customer_neutral and not args.forbid:
        parser.error("--customer-neutral needs at least one --forbid WORD to check for")

    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.strip()

    files = tracked()
    missing = [name for name in REQUIRED if name not in files]
    if missing:
        print(f"REFUSING: not committed: {', '.join(missing)}")
        print("  build the interface (scripts\\build-ui.cmd) and commit ui/dist")
        return 1

    # Exact names or directory prefixes, never a bare startswith: `.env.example`
    # is the template a person copies and must ship, and it starts with `.env`.
    forbidden = [
        f for f in files
        if any(f == n or (n.endswith("/") and f.startswith(n)) for n in NEVER)
    ]
    if forbidden:
        print("REFUSING: these should never be packaged:")
        for name in forbidden:
            print(f"  {name}")
        return 1

    renamed: dict[str, str] = {}
    if args.customer_neutral:
        files = neutral(files)
        renamed = {NEUTRAL_ENV[0]: NEUTRAL_ENV[1]}
    suffix = "-neutral" if args.customer_neutral else ""
    out = args.out or (ROOT / "dist" / f"sdm-{commit}{suffix}.zip")
    out.parent.mkdir(parents=True, exist_ok=True)

    # Which build a machine runs, readable there without git: the customer and
    # the demo laptop have no .git (ENVIRONMENTS.md, "Which build each runs").
    when = subprocess.run(
        ["git", "show", "-s", "--format=%ci", "HEAD"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.strip()
    version = (f"SDM build {commit}{' (customer-neutral)' if args.customer_neutral else ''}\n"
               f"committed {when}\n")

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr("SDM/VERSION.txt", version)
        for name in files:
            path = ROOT / name
            if path.is_file():
                zf.write(path, f"SDM/{renamed.get(name, name)}")

    if args.forbid:
        hits = found_words(out, args.forbid)
        if hits:
            out.unlink()
            print("REFUSING: the zip held forbidden words, so it was removed:")
            for word, names in hits.items():
                print(f"  {word!r}: {', '.join(names[:10])}"
                      f"{f' and {len(names) - 10} more' if len(names) > 10 else ''}")
            return 1

    size_mb = out.stat().st_size / 1_048_576
    print(f"{out}")
    print(f"  {len(files):,} files, {size_mb:.1f} MB")
    if size_mb > LIMIT_MB:
        print(f"  OVER the {LIMIT_MB} MB limit")
        return 1
    print(f"  under the {LIMIT_MB} MB limit")
    print("  no .env, no .venv, no node_modules, no cache, no work/")
    if args.forbid:
        print(f"  customer-neutral: none of the {len(args.forbid)} forbidden words in any file")
    return 0


if __name__ == "__main__":
    sys.exit(main())
