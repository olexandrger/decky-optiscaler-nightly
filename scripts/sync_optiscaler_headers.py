#!/usr/bin/env python3
"""Fetch the Config.h / Config.cpp the bundled OptiScaler was built from.

The live-control ASI (asi/live.cpp) writes OptiScaler's Config at offsets the
compiler computes from asi/generated/config_mirror.h, which
scripts/generate_asi_layout.py generates from Config.h. That header has to be
the one the shipped OptiScaler.dll was compiled from: nightlies add and move
Config members almost daily (the 30 Oct and 16 Nov builds differ by a dozen).

Every OptiScaler build names its source commit in a string it logs at startup,
"OptiScaler v10.0.0-dev (1e568bb3) (20261116_122736)", so the commit is read
straight out of the dll and the two files are fetched from
raw.githubusercontent.com at exactly that commit (no GitHub API involved).

    python3 scripts/sync_optiscaler_headers.py              # the archive in bin/
    python3 scripts/sync_optiscaler_headers.py --dll X.dll  # any OptiScaler.dll

Then regenerates the mirror (generate_asi_layout.py). bundle_nightly.py calls
sync() itself, so bundling a nightly keeps the ASI's layout in step.
"""
import argparse
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bundle_nightly  # noqa: E402  (archiver, retries)

ROOT = Path(__file__).resolve().parent.parent
REF = ROOT / "asi" / "optiscaler_ref"
SOURCE_REPO = "optiscaler/OptiScaler"
RAW = "https://raw.githubusercontent.com/" + SOURCE_REPO + "/{commit}/OptiScaler/{name}"
#: What the layout generator reads. Nothing else of OptiScaler's source is used.
HEADERS = ("Config.h", "Config.cpp")

BUILD_RE = re.compile(rb"OptiScaler v([0-9][\w.\-]*) \(([0-9a-f]{7,40})\) \((\d{8}_\d{6})\)")


def build_info(dll_bytes):
    """(version, commit, built) from the banner string inside OptiScaler.dll."""
    match = BUILD_RE.search(dll_bytes)
    if not match:
        raise ValueError("no 'OptiScaler v<version> (<commit>) (<date>)' banner in the dll")
    return tuple(part.decode("ascii") for part in match.groups())


def fetch(commit, name, opener=urllib.request.urlopen, sleep=None):
    url = RAW.format(commit=commit, name=name)

    def get():
        with opener(url, timeout=60) as response:
            return response.read()

    kwargs = {"sleep": sleep} if sleep else {}
    data = bundle_nightly.with_retries(get, f"fetching {name} at {commit}", **kwargs)
    if len(data) < 1000 or b"Config" not in data:
        raise ValueError(f"{url} does not look like OptiScaler's {name}")
    return data


def sync(dll_bytes, opener=urllib.request.urlopen, sleep=None, regenerate=True):
    """Fetch the headers for this dll's commit into asi/optiscaler_ref/."""
    version, commit, built = build_info(dll_bytes)
    current = (REF / "SOURCE_COMMIT.txt").read_text(encoding="utf-8") if (REF / "SOURCE_COMMIT.txt").exists() else ""
    if current.startswith(commit) and all((REF / name).is_file() for name in HEADERS):
        print(f"OptiScaler headers already at {commit} ({version}, built {built})")
    else:
        files = {name: fetch(commit, name, opener, sleep) for name in HEADERS}
        REF.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (REF / name).write_bytes(data)
        (REF / "SOURCE_COMMIT.txt").write_text(
            f"{commit}\n\n"
            f"Version:  v{version} (built {built})\n"
            f"Source:   https://github.com/{SOURCE_REPO}/tree/{commit}/OptiScaler\n\n"
            "Config.h and Config.cpp at the commit the bundled OptiScaler.dll names in\n"
            "its startup banner. scripts/generate_asi_layout.py turns them into\n"
            "asi/generated/config_mirror.h, the layout the live-control ASI writes\n"
            "Config with. Written by scripts/sync_optiscaler_headers.py (bundling a\n"
            "nightly runs it); do not edit by hand.\n",
            encoding="utf-8")
        print(f"OptiScaler headers synced to {commit} ({version}, built {built})")
    if regenerate:
        subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_asi_layout.py")],
                       check=True, stdout=subprocess.DEVNULL)
    return version, commit, built


def dll_from_archive(archive):
    seven = bundle_nightly.archiver()
    with tempfile.TemporaryDirectory() as scratch:
        subprocess.run([seven, "e", "-y", f"-o{scratch}", str(archive), "OptiScaler.dll", "-r"],
                       check=True, capture_output=True)
        found = list(Path(scratch).rglob("OptiScaler.dll"))
        if not found:
            sys.exit(f"no OptiScaler.dll in {archive}")
        return found[0].read_bytes()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dll", help="OptiScaler.dll to read the commit from (default: the archive in bin/)")
    args = parser.parse_args()
    if args.dll:
        data = Path(args.dll).read_bytes()
    else:
        archives = sorted((ROOT / "bin").glob("OptiScaler-*.7z"))
        if len(archives) != 1:
            sys.exit("bin/ must hold exactly one OptiScaler archive: run scripts/bundle_nightly.py first")
        data = dll_from_archive(archives[0])
    sync(data)


if __name__ == "__main__":
    main()
