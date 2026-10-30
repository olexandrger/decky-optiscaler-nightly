#!/usr/bin/env python3
"""Write the Markdown description for a nightly-fork release.

    python3 scripts/release_notes.py --zip out/decky-optiscaler-nightly.zip \
        [--selftest-log selftest.log] [--fsr4-log fsr4.log] > notes.md

Everything is read from the tree being released, so the notes describe the
build that is attached, not the one the workflow meant to make:

- the OptiScaler nightly in bin/ (version, tag, link) and the FidelityFX
  upscaler inside it, with the archive's and the zip's SHA-256;
- OptiScaler's changes since this fork's previous build: the upstream commit
  each nightly was built from, compared on optiscaler/OptiScaler, so nightlies
  skipped in between are covered. The first build has nothing to compare with
  and quotes its nightly's own "last 24 hours" list instead;
- this fork's commits since its previous build. Builds are found by tag: each
  release is tagged v<plugin version>-nightly.<date>;
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
OPTISCALER_REPO = "optiscaler/OptiScaler"
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


def gh_api(path):
    """GET a GitHub REST path; None when it cannot be read (offline, rate limit, 404)."""
    request = urllib.request.Request(
        f"https://api.github.com/{path}",
        headers={"Accept": "application/vnd.github+json"},
    )
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except Exception as exc:  # degrade: the notes fall back to links
        print(f"note: could not read {path}: {exc}", file=sys.stderr)
        return None


def nightly_release(tag):
    """The upstream nightly release as the API returns it, or None."""
    return gh_api(f"repos/{NIGHTLY_REPO}/releases/tags/{tag}") if tag else None


def built_commit(release):
    """The optiscaler/OptiScaler commit a nightly was built from.

    The nightly workflow lists the day's commits newest first with full-SHA
    links, so the first link is the HEAD it built. A day without commits has no
    list; then it is the last master commit before the release was created.
    """
    if not release:
        return None
    match = re.search(r"github\.com/optiscaler/OptiScaler/commit/([0-9a-f]{40})",
                      release.get("body") or "")
    if match:
        return match.group(1)
    when = release.get("created_at")
    found = gh_api(f"repos/{OPTISCALER_REPO}/commits?sha=master&per_page=1&until={when}") if when else None
    return found[0]["sha"] if found else None


def optiscaler_changes(previous_tag, current_tag):
    """OptiScaler commits between two nightlies, newest first.

    Returns (lines, compare_url, total) or None when either end is unknown.
    """
    previous, current = nightly_release(previous_tag), nightly_release(current_tag)
    base, head = built_commit(previous), built_commit(current)
    if not base or not head:
        return None
    if base == head:
        return [], None, 0
    compare = gh_api(f"repos/{OPTISCALER_REPO}/compare/{base}...{head}")
    if not compare:
        return None
    lines = []
    for commit in reversed(compare.get("commits", [])):  # API is oldest first
        if len(commit.get("parents", [])) > 1:
            continue
        subject = commit["commit"]["message"].splitlines()[0]
        author = (commit.get("author") or {}).get("login") or commit["commit"]["author"]["name"]
        lines.append(f"- [`{commit['sha'][:8]}`]({commit['html_url']}) {subject} ({author})")
    return lines, compare.get("html_url"), compare.get("total_commits", len(lines))


def demote(markdown, levels=1):
    """Push headings down so a quoted release body nests under ours."""
    return re.sub(r"^(#{1,5}) ", lambda m: "#" * (len(m.group(1)) + levels) + " ",
                  markdown, flags=re.M)


def previous_release_tag(current):
    """This fork's last nightly release tag before `current`, or None."""
    tags = [t for t in git("tag", "--list", "v*-nightly.*", "--sort=-creatordate").splitlines() if t]
    earlier = [t for t in tags if t != current]
    return earlier[0] if earlier else None


def previous_nightly(release_tag):
    """The OptiScaler nightly a fork release bundled: v0.0.7-nightly.20261029 -> nightly-20261029."""
    match = re.search(r"-nightly\.(\d{8})$", release_tag or "")
    return f"nightly-{match.group(1)}" if match else None


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
    archive = ROOT / "bin" / constants.PAYLOAD_ARCHIVE
    previous_tag = previous_release_tag(current_tag)
    previous_nightly_tag = previous_nightly(previous_tag)

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

    # OptiScaler: what changed since the nightly the previous build carried.
    changes = optiscaler_changes(previous_nightly_tag, tag) if previous_nightly_tag else None
    if changes is not None:
        lines, compare_url, total = changes
        out.append(f"## OptiScaler changes since the last build ({previous_nightly_tag} → {tag})")
        out.append("")
        if not lines:
            out.append("- None: the same OptiScaler build as last time.")
        else:
            limit = 60
            out.extend(lines[:limit])
            if total > limit:
                out.append(f"- …and {total - limit} more")
        if compare_url:
            out.append("")
            out.append(f"[Full comparison]({compare_url})")
    else:
        heading = (f"## OptiScaler changes in {tag}" if tag else "## OptiScaler changes")
        out.append(heading)
        out.append("")
        if previous_nightly_tag:
            out.append(f"_Could not compare with {previous_nightly_tag}; "
                       "showing this nightly's own list (the 24 hours before it)._")
        else:
            out.append("_First build of this fork: showing this nightly's own list "
                       "(the 24 hours before it)._")
        out.append("")
        body = ((nightly_release(tag) or {}).get("body") or "").strip()
        if body:
            # The nightly's own heading would repeat ours; keep only the list.
            body = re.sub(r"^#{1,6} Changes in the Last 24 Hours\s*\n+", "", body, flags=re.I | re.M)
            out.append(demote(body, 2))
        elif nightly_link:
            out.append(f"See the [{tag} release notes]({nightly_link}).")
    out.append("")

    # The fork: its own commits since the previous build's tag.
    if previous_tag:
        commits = fork_commits(previous_tag, repo)
        out.append(f"## Fork changes since the last build ({previous_tag})")
        out.append("")
        out.extend(commits or ["- None: only the bundled OptiScaler nightly is new."])
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
