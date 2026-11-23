"""Build the offline packages for scripts\\setup_offline.cmd, in parts small
enough to send by mail.

For each Python version, downloads the Windows wheels of every package in
requirements-runtime.txt (and of requirements-optional.txt, where one is
built for that machine), zips them under `wheels/`, and cuts the zip into
numbered parts under the size limit:

    dist/sdm-wheels-cp314.001, .002, ...          64-bit Intel/AMD (x64)
    dist/sdm-wheels-cp314-arm64.001, .002, ...    ARM64
    dist/sdm-wheels-cp314t.001, .002, ...         free-threaded Python (python3.14t)

The parts are pieces of one zip, not zips of their own. Copied into the SDM
folder, setup_offline.cmd joins them, checks and unpacks them. Beside them,
dist/sdm-wheels-cp314.sha256 holds the joined zip's SHA-256: send it with the
parts, and the hash itself in the message, so a changed part is caught. There is no
32-bit set: pandas publishes no 32-bit build.

    python scripts/package_wheels.py                          3.12-3.14, x64
    python scripts/package_wheels.py --python 3.14 --arm64    one version, ARM64
    python scripts/package_wheels.py --python 3.14 --free-threaded
    python scripts/package_wheels.py --python 3.15 --extra work/wheels-cp315
    python scripts/package_wheels.py --python 3.15 --free-threaded --extra work/wheels-cp315t

Python 3.15: pyodbc and PyYAML publish no Windows wheels for it yet. The
workflow .github/workflows/wheels-from-source.yml builds them on GitHub;
download them (gh run download --name wheels-cp315 --dir work/wheels-cp315)
and pass the folder with --extra.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Mail encodes an attachment at about 4/3 its size: 15 MB goes out as about
# 20 MB, under the usual 25 MB limit.
PART_MB = 15


def part_name(version: str, arm64: bool, free_threaded: bool = False) -> str:
    """The parts' name for a Python version and machine; unpack_wheels.py
    looks for the same name."""
    return ("sdm-wheels-cp" + version.replace(".", "") + ("t" if free_threaded else "")
            + ("-arm64" if arm64 else ""))


def _for_version(requirements: Path, version: str, dest: Path) -> Path:
    """The requirements as the target Python reads them. pip download judges a
    line's marker (`; python_version >= "3.15"`) by the Python running it, not
    the one named by --python-version, so the lines are chosen here."""
    from packaging.markers import Marker

    kept = []
    for line in requirements.read_text().splitlines():
        spec, _, marker = line.partition(";")
        if marker.strip() and not Marker(marker.strip()).evaluate(
                {"python_version": version, "python_full_version": version + ".0"}):
            continue
        kept.append(spec.strip() if marker.strip() else line)
    dest.write_text("\n".join(kept) + "\n")
    return dest


def _download(requirements: str, dest: Path, version: str, platform: str,
              check: bool, free_threaded: bool = False,
              extra: Path | None = None) -> bool:
    tag = "cp" + version.replace(".", "") + "t"
    abi = ["--abi", tag, "--abi", "abi3", "--abi", "none"] if free_threaded else []
    chosen = _for_version(ROOT / requirements, version,
                          dest.parent / f"chosen-{Path(requirements).name}")
    links = ["--find-links", str(extra)] if extra else []
    done = subprocess.run(
        [sys.executable, "-m", "pip", "download", "-q",
         "-r", str(chosen), "-d", str(dest), *links,
         "--only-binary=:all:", "--platform", platform,
         "--python-version", version, "--implementation", "cp", *abi],
        check=check,
    )
    return done.returncode == 0


def build(version: str, arm64: bool, out_dir: Path, free_threaded: bool = False,
          extra: Path | None = None) -> list[Path]:
    name = part_name(version, arm64, free_threaded)
    platform = "win_arm64" if arm64 else "win_amd64"
    with tempfile.TemporaryDirectory() as tmp:
        wheels = Path(tmp) / "wheels"
        wheels.mkdir()
        _download("requirements-runtime.txt", wheels, version, platform, check=True,
                  free_threaded=free_threaded, extra=extra)
        for line in (ROOT / "requirements-optional.txt").read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                one = Path(tmp) / "one.txt"
                one.write_text(line)
                if not _download(str(one), wheels, version, platform, check=False,
                                 free_threaded=free_threaded):
                    print(f"  {line}: no build for {platform}, left out (optional)")
        joined = Path(tmp) / f"{name}.zip"
        with zipfile.ZipFile(joined, "w", zipfile.ZIP_STORED) as zf:
            for path in sorted(wheels.glob("*.whl")):
                zf.write(path, f"wheels/{path.name}")

        for old in out_dir.glob(f"{name}.[0-9][0-9][0-9]"):
            old.unlink()
        parts = []
        size = PART_MB * 1_048_576
        digest = hashlib.sha256()
        with joined.open("rb") as src:
            while chunk := src.read(size):
                digest.update(chunk)
                part = out_dir / f"{name}.{len(parts) + 1:03d}"
                part.write_bytes(chunk)
                parts.append(part)
        # unpack_wheels.py checks the joined parts against this before it
        # unpacks anything, and prints the same hash to compare by hand.
        (out_dir / f"{name}.sha256").write_text(
            f"{digest.hexdigest()}  {name}\n", encoding="ascii")
    return parts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", action="append", dest="versions")
    parser.add_argument("--arm64", action="store_true", help="ARM64 Windows")
    parser.add_argument("--free-threaded", action="store_true",
                        help="free-threaded Python (python3.14t)")
    parser.add_argument("--extra", type=Path,
                        help="a folder of wheels built elsewhere (pyodbc and PyYAML "
                             "for 3.15, from .github/workflows/wheels-from-source.yml)")
    args = parser.parse_args(argv)
    out_dir = ROOT / "dist"
    out_dir.mkdir(exist_ok=True)
    for version in args.versions or ["3.12", "3.13", "3.14"]:
        parts = build(version, args.arm64, out_dir, args.free_threaded, args.extra)
        total = sum(p.stat().st_size for p in parts) / 1_048_576
        print(f"Python {version}{'t' if args.free_threaded else ''}{' ARM64' if args.arm64 else ''}: "
              f"{len(parts)} parts, {total:.0f} MB")
        for part in parts:
            print(f"  {part.name}  {part.stat().st_size / 1_048_576:.1f} MB")
        checksum = parts[0].with_suffix(".sha256") if parts else None
        if checksum and checksum.exists():
            print(f"  {checksum.name}  SHA-256 {checksum.read_text(encoding='ascii').split()[0]}"
                  " (send it with the parts, and the hash itself in the message)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
