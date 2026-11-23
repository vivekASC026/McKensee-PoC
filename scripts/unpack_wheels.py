"""Join the parts sdm-wheels-<tag>.001, .002, ... in the SDM folder and unpack
them into wheels\\. Run by setup_offline.cmd with the new .venv's Python, so
it uses the standard library only.

The joined parts must match a SHA-256: the sdm-wheels-<tag>.sha256 file sent
with them, or the hash given as `--sha256 <hash>` (from the message the parts
came with). With neither, nothing is unpacked.
"""

from __future__ import annotations

import hashlib
import io
import shutil
import sys
import sysconfig
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    given = None
    if args:
        if len(args) != 2 or args[0] != "--sha256":
            print("\nUsage: unpack_wheels.py [--sha256 <hash>]")
            return 2
        given = args[1].strip().lower()
    machine = sysconfig.get_platform()
    if machine == "win32":
        print("\nThis Python is the 32-bit build; the tool needs 64-bit Python "
              "(pandas has no 32-bit build). Install the 64-bit Python from "
              "python.org, delete the .venv folder, and run this again.")
        return 1
    tag = f"cp{sys.version_info[0]}{sys.version_info[1]}"
    if sysconfig.get_config_var("Py_GIL_DISABLED"):
        tag += "t"  # the free-threaded build (python3.14t) has its own set
    if machine == "win-arm64":
        tag += "-arm64"
    parts = sorted(ROOT.glob(f"sdm-wheels-{tag}.[0-9][0-9][0-9]"))
    if not parts:
        others = sorted(p.name for p in ROOT.glob("sdm-wheels-*.[0-9][0-9][0-9]"))
        print(f"\nNo sdm-wheels-{tag}.001 in {ROOT}. This Python is "
              f"{sys.version_info[0]}.{sys.version_info[1]} ({machine}); copy the "
              "parts for it (sdm-wheels-" + tag + ".001, .002, ...) into this folder.")
        if others:
            print("Found parts for another Python: " + ", ".join(others))
        return 1
    numbers = [int(p.suffix[1:]) for p in parts]
    missing = sorted(set(range(1, max(numbers) + 1)) - set(numbers))
    if missing:
        print("\nMissing part(s): " + ", ".join(f"sdm-wheels-{tag}.{n:03d}" for n in missing))
        return 1

    data = b"".join(p.read_bytes() for p in parts)
    digest = hashlib.sha256(data).hexdigest()
    checksum = ROOT / f"sdm-wheels-{tag}.sha256"
    if given is not None:
        if digest != given:
            print("\nThe parts do not match the SHA-256 given: they were changed or "
                  "damaged on the way. Copy the parts again. Nothing was unpacked.")
            return 1
    elif checksum.exists():
        words = checksum.read_text(encoding="ascii", errors="replace").split()
        if not words or digest != words[0].lower():
            print(f"\nThe parts do not match {checksum.name}: they were changed or damaged "
                  "on the way. Copy the parts and the .sha256 file again. Nothing was "
                  "unpacked.")
            return 1
    else:
        print(f"\nNo {checksum.name} in {ROOT}, so the parts cannot be checked. Copy "
              "the .sha256 file sent with the parts into this folder, or run "
              "scripts\\setup_offline.cmd --sha256 <hash> with the hash from the "
              "message the parts came with. Nothing was unpacked.")
        return 1
    # Compare this with the SHA-256 the sender gave apart from the parts (in the
    # message, say): a file sent beside the parts can be changed with them.
    print(f"SHA-256 of the parts: {digest}")

    staging = ROOT / "wheels.unpacking"
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            bad = zf.testzip()
            if bad:
                print(f"\n{bad} is damaged. Copy the parts again.")
                return 1
            names = zf.namelist()
            if not names:
                print("\nThe parts make an empty zip. Copy the parts again.")
                return 1
            stray = [n for n in names if not _is_wheel_member(n)]
            if stray:
                print(f"\nThe parts hold {len(stray)} file(s) outside wheels\\ "
                      f"(the first is {stray[0]!r}). They are not a wheels set made by "
                      "package_wheels.py. Nothing was unpacked.")
                return 1
            # Unpacked beside the old folder first, so a failure partway leaves
            # the wheels there were; then swapped in. An earlier folder may hold
            # another machine's set, so it is replaced, not added to.
            shutil.rmtree(staging, ignore_errors=True)
            zf.extractall(staging)
    except zipfile.BadZipFile:
        print(f"\nThe parts do not make a whole zip: one is missing after "
              f"sdm-wheels-{tag}.{max(numbers):03d}, or one is damaged. Copy them again.")
        return 1
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        print(f"\nThe wheels could not be unpacked into {staging}: {exc}. Check the free "
              "disk space and that no other program has the folder open, then run this "
              "again. The wheels folder was left as it was.")
        return 1
    try:
        if (ROOT / "wheels").exists():
            shutil.rmtree(ROOT / "wheels")
        (staging / "wheels").replace(ROOT / "wheels")
        staging.rmdir()
    except OSError as exc:
        print(f"\nThe unpacked wheels in {staging / 'wheels'} could not be moved into "
              f"place: {exc}. Close any program using the wheels folder, then run this "
              "again.")
        return 1
    print(f"Unpacked {len(parts)} parts into wheels")
    return 0


def _is_wheel_member(name: str) -> bool:
    """A zip member package_wheels.py writes: wheels/<file>.whl and nothing else,
    so no member can land elsewhere in the SDM folder (.env, sdg\\)."""
    parts = name.split("/")
    return (len(parts) == 2 and parts[0] == "wheels" and parts[1].endswith(".whl")
            and "\\" not in name and ":" not in name and parts[1] not in ("", ".", ".."))


if __name__ == "__main__":
    sys.exit(main())
