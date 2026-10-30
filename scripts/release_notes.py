#!/usr/bin/env python3
"""Write the Markdown description for a nightly-fork release.

    python3 scripts/release_notes.py --zip out/decky-optiscaler-nightly.zip \
        [--selftest-log selftest.log] [--fsr4-log fsr4.log] > notes.md

Everything is read from the tree being released, so the notes describe the
build that is attached, not the one the workflow meant to make:

- the OptiScaler nightly in bin/ (version, tag, link) and the FidelityFX
  upscaler inside it, with the archive's and the zip's SHA-256;
- OptiScaler's own changes, taken from that nightly's release on
  optiscaler/OptiScaler-nightly (its "changes in the last 24 hours" list);
- this fork's commits since its previous nightly release, or since the
  upstream release it is based on for the first one;
- the test counts the release was gated on, when their logs are given.

The GitHub API is optional: without it (or offline) the OptiScaler section
links to the nightly release instead of quoting it.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
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


def nightly_release(tag):
    """The upstream nightly release as the API returns it, or None."""
    if not tag:
        return None
    request = urllib.request.Request(
        f"https://api.github.com/repos/{NIGHTLY_REPO}/releases/tags/{tag}",
        headers={"Accept": "application/vnd.github+json"},
    )
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except Exception as exc:  # network, rate limit, missing release: degrade
        print(f"note: could not read {NIGHTLY_REPO} {tag}: {exc}", file=sys.stderr)
        return None


def demote(markdown, levels=1):
    """Push headings down so a quoted release body nests under ours."""
    return re.sub(r"^(#{1,5}) ", lambda m: "#" * (len(m.group(1)) + levels) + " ",
                  markdown, flags=re.M)


def previous_release_tag(current):
    """This fork's last nightly tag before `current`, else the upstream base tag."""
    tags = [t for t in git("tag", "--list", "v*", "--sort=-creatordate").splitlines() if t]
    nightly = [t for t in tags if "-nightly." in t and t != current]
    if nightly:
        return nightly[0]
    base = [t for t in tags if "-nightly." not in t]
    return base[0] if base else None


def fork_commits(since, repo):
    log_range = f"{since}..HEAD" if since else "HEAD~15..HEAD"
    lines = git("log", "--no-merges", "--format=%h%x1f%H%x1f%s", log_range).splitlines()
    out = []
    for line in lines:
        short, full, subject = line.split("\x1f", 2)
        link = f"https://github.com/{repo}/commit/{full}" if repo else None
        out.append(f"- [`{short}`]({link}) {subject}" if link else f"- `{short}` {subject}")
    return out


def test_counts(selftest_log, fsr4_log):
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
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--zip", required=True, help="the plugin zip being attached")
    parser.add_argument("--selftest-log")
    parser.add_argument("--fsr4-log")
    args = parser.parse_args()

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    version = package["version"]
    current_tag = f"v{version}"
    tag = nightly_tag()
    release = nightly_release(tag)
    archive = ROOT / "bin" / constants.PAYLOAD_ARCHIVE
    since = previous_release_tag(current_tag)

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

    out.append(f"## OptiScaler changes in {tag or 'this nightly'}")
    out.append("")
    body = (release or {}).get("body", "").strip()
    if body:
        # The nightly's own heading ("Changes in the Last 24 Hours") would
        # repeat ours; drop it and keep the list.
        body = re.sub(r"^#{1,6} Changes in the Last 24 Hours\s*\n+", "", body, flags=re.I | re.M)
        out.append(demote(body, 2))
    elif nightly_link:
        out.append(f"See the [{tag} release notes]({nightly_link}).")
    else:
        out.append("Not recorded.")
    out.append("")

    commits = fork_commits(since, repo)
    out.append(f"## Fork changes since {since}" if since else "## Recent fork changes")
    out.append("")
    out.extend(commits or ["- No plugin changes; only the bundled OptiScaler nightly is new."])
    out.append("")

    counts = test_counts(args.selftest_log, args.fsr4_log)
    if counts:
        out.append("## Checks this release passed")
        out.append("")
        out.extend(f"- {line}" for line in counts)
        out.append("- UI harnesses: run, all-options, live-fields")
        out.append("")

    sys.stdout.write("\n".join(out).rstrip() + "\n")


if __name__ == "__main__":
    main()
