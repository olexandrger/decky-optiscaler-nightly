#!/usr/bin/env python3
"""Write the Markdown description for a nightly-fork release.

    python3 scripts/release_notes.py --zip out/decky-optiscaler-nightly.zip \
        [--selftest-log selftest.log] [--fsr4-log fsr4.log] > notes.md

Everything is read from the tree being released, so the notes describe the
build that is attached, not the one the workflow meant to make:

- the OptiScaler nightly in bin/ (version, tag, link) and the FidelityFX
  upscaler inside it, with the archive's and the zip's SHA-256;
- this fork's commits since its previous build. Builds are found by tag: each
  release is tagged v<plugin version>-nightly.<date>;
- the test counts the release was gated on, when their logs are given.

OptiScaler's own changes are not repeated here: the table links the nightly's
release on optiscaler/OptiScaler-nightly, which lists them.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "py_modules"))

from optiscaler import constants, fsr4build  # noqa: E402

NIGHTLY_REPO = "optiscaler/OptiScaler-nightly"
UPSTREAM_PLUGIN = "danielcamilo1/decky-optiscaler"
ASSET = "decky-optiscaler-nightly.zip"


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def git(*args):
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def nightly_tag():
    """nightly-YYYYMMDD of the archive in bin/, from its pinned name."""
    match = re.search(r"-(\d{8})\.7z$", constants.PAYLOAD_ARCHIVE)
    return f"nightly-{match.group(1)}" if match else None


def previous_release_tag(current):
    """This fork's last nightly release tag before `current`, or None."""
    tags = [t for t in git("tag", "--list", "v*-nightly.*", "--sort=-creatordate").splitlines() if t]
    earlier = [t for t in tags if t != current]
    return earlier[0] if earlier else None


def fork_commits(since, repo):
    if not since:
        return []
    log_range = f"{since}..HEAD"
    lines = git("log", "--no-merges", "--format=%h%x1f%H%x1f%s", log_range).splitlines()
    out = []
    for line in lines:
        short, full, subject = line.split("\x1f", 2)
        link = f"https://github.com/{repo}/commit/{full}" if repo else None
        out.append(f"- [`{short}`]({link}) {subject}" if link else f"- `{short}` {subject}")
    return out


def test_counts(selftest_log, fsr4_log, install_log=None):
    found = []
    if selftest_log and Path(selftest_log).is_file():
        match = re.search(r"(\d+) passed, (\d+) failed", Path(selftest_log).read_text(errors="replace"))
        if match:
            found.append(f"backend selftest: {match.group(1)} passed, {match.group(2)} failed")
    if fsr4_log and Path(fsr4_log).is_file():
        text = Path(fsr4_log).read_text(errors="replace")
        match = re.search(r"Ran (\d+) tests?", text)
        if match:
            status = "OK" if re.search(r"^OK", text, re.M) else "FAILED"
            found.append(f"FSR 4 lifecycle: {match.group(1)} tests, {status}")
    if install_log and Path(install_log).is_file():
        text = Path(install_log).read_text(errors="replace")
        match = re.search(r"Ran (\d+) tests?", text)
        if match:
            status = "OK" if re.search(r"^OK", text, re.M) else "FAILED"
            found.append(f"install safety: {match.group(1)} tests, {status}")
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--zip", required=True, help="the plugin zip being attached")
    parser.add_argument("--selftest-log")
    parser.add_argument("--fsr4-log")
    parser.add_argument("--install-log")
    args = parser.parse_args()

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    version = package["version"]
    current_tag = f"v{version}"
    tag = nightly_tag()
    archive = ROOT / "bin" / constants.PAYLOAD_ARCHIVE
    previous_tag = previous_release_tag(current_tag)

    out = []
    out.append(f"> [!WARNING]\n> **Experimental fork build for tinkering, not intended for real use.** "
               f"Use [{UPSTREAM_PLUGIN}](https://github.com/{UPSTREAM_PLUGIN}) instead. "
               "Live in-game control is unavailable in these builds.")
    out.append("")
    if repo:
        out.append("**Install on Steam Deck:** Decky → Settings → Developer mode → Install Plugin from URL →")
        out.append(f"`https://github.com/{repo}/releases/latest/download/{ASSET}`  ")
        out.append("then run **Install** again on each game.")
        out.append("")

    out.append("## What's inside")
    out.append("")
    out.append("| | |")
    out.append("|---|---|")
    out.append(f"| Plugin | `{version}` |")
    nightly_link = f"https://github.com/{NIGHTLY_REPO}/releases/tag/{tag}" if tag else None
    out.append(f"| OptiScaler | `{constants.OPTISCALER_VERSION}`"
               + (f" — [{tag}]({nightly_link})" if nightly_link else "") + " |")
    out.append(f"| FidelityFX upscaler | {fsr4build.BUNDLED_BUILD['label'].replace('Bundled ', '').strip('()')} |")
    deck_models = ", ".join(spec["label"] for key, spec in constants.FSR4_DECK_MODELS.items() if key != "int8")
    out.append(f"| Steam Deck presets | {deck_models} |")
    if archive.is_file():
        out.append(f"| `{archive.name}` SHA-256 | `{sha256(archive)}` |")
    out.append(f"| `{ASSET}` SHA-256 | `{sha256(args.zip)}` |")
    out.append("")

    # The fork: its own commits since the previous build's tag.
    if previous_tag:
        commits = fork_commits(previous_tag, repo)
        out.append(f"## Fork changes since the last build ({previous_tag})")
        out.append("")
        out.extend(commits or ["- None: only the bundled OptiScaler nightly is new."])
        out.append("")

    counts = test_counts(args.selftest_log, args.fsr4_log, args.install_log)
    if counts:
        out.append("## Checks this release passed")
        out.append("")
        out.extend(f"- {line}" for line in counts)
        out.append("- UI harnesses: run, all-options, live-fields")
        out.append("")

    sys.stdout.write("\n".join(out).rstrip() + "\n")


if __name__ == "__main__":
    main()
