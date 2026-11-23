#!/usr/bin/env python3
"""Where the fork's review of upstream stands, and what is new since.

The fork ports fixes from upstream one commit at a time and never rebases
(AGENTS.md, "Taking from upstream"). docs/upstream-review.md records the last
upstream commit read (its ``reviewed:`` line) and the queue of things to port.

    python3 scripts/upstream_review.py new      # upstream commits after the marker, oldest first
    python3 scripts/upstream_review.py queue    # open items in the queue
    python3 scripts/upstream_review.py mark SHA # every commit up to SHA has been read
    python3 scripts/upstream_review.py check    # the record parses and keeps to its limits

The record holds only open items and rules (AGENTS.md "Taking from upstream"):
a port deletes its item in its own commit, and a review commit's message
lists what was not applicable or not taken. ``check`` holds it to that.

``new`` adds the ``upstream`` remote when it is missing and fetches it. ``mark``
only moves forward along upstream/main, so a review can never skip back or
land on a commit upstream does not have.
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REVIEW = ROOT / "docs" / "upstream-review.md"
UPSTREAM_URL = "https://github.com/danielcamilo1/decky-optiscaler"
BRANCH = "upstream/main"
MARKER_RE = re.compile(r"^reviewed: ([0-9a-f]{40})$", re.M)
OPEN_RE = re.compile(r"^- \[ \] (.+)$", re.M)
ITEM_RE = re.compile(r"^[\w-]+ \([0-9a-f]{7,40}\): \S")
# An audit can leave many findings for the next day; decisions should be few.
MAX_OPEN = 30
MAX_RULES = 12
MAX_LINE = 400
MAX_BYTES = 12000


def git(*args, check=True):
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if check and result.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def marker(text=None):
    text = REVIEW.read_text(encoding="utf-8") if text is None else text
    found = MARKER_RE.search(text)
    if not found:
        sys.exit(f"{REVIEW.relative_to(ROOT)} has no 'reviewed: <40-hex sha>' line")
    return found.group(1)


def open_items(text=None):
    text = REVIEW.read_text(encoding="utf-8") if text is None else text
    return OPEN_RE.findall(section(text, "Open"))


def section(text, name):
    found = re.search(r"^## " + name + r"\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return found.group(1) if found else ""


def problems(text=None):
    """What keeps the record from being read, or from staying small."""
    text = REVIEW.read_text(encoding="utf-8") if text is None else text
    found = []
    if len(MARKER_RE.findall(text)) != 1:
        found.append("no single 'reviewed: <40-hex sha>' line")
    items = OPEN_RE.findall(section(text, "Open"))
    rules = re.findall(r"^- (?!\[[ x]\] )(.+)$", section(text, "Rules"), re.M)
    found += [f"open item not '<id> (<commit>): <what>': {item[:80]}" for item in items if not ITEM_RE.match(item)]
    if re.search(r"^- \[x\] ", text, re.M):
        found.append("a done item is kept: a port deletes its item, and a review commit's message records the rest")
    if len(OPEN_RE.findall(text)) != len(items):
        found.append("an open item outside '## Open'")
    if len(items) > MAX_OPEN:
        found.append(f"{len(items)} open items (at most {MAX_OPEN}): triage the queue with the owner")
    if len(rules) > MAX_RULES:
        found.append(f"{len(rules)} rules (at most {MAX_RULES}): fold them into fewer, broader ones")
    found += [f"line over {MAX_LINE} characters: {line[:60]}..." for line in text.splitlines() if len(line) > MAX_LINE]
    if len(text.encode("utf-8")) > MAX_BYTES:
        found.append(f"{len(text.encode('utf-8'))} bytes (at most {MAX_BYTES})")
    return found


def fetch():
    if git("remote", "get-url", "upstream", check=False) == "":
        git("remote", "add", "upstream", UPSTREAM_URL)
    git("fetch", "--quiet", "upstream", "main")


def new():
    fetch()
    since = marker()
    ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", since, BRANCH], cwd=ROOT).returncode
    if ancestor != 0:
        sys.exit(f"the marker {since[:7]} is not on {BRANCH}: upstream rewrote its history. "
                 "Review from the merge base by hand and set the marker again.")
    log = git("log", "--reverse", "--format=%H %ad %an: %s", "--date=short", f"{since}..{BRANCH}")
    if not log:
        print(f"up to date: nothing after {since[:7]} on {BRANCH}")
        return
    commits = log.splitlines()
    print(f"{len(commits)} upstream commit(s) after {since[:7]}, oldest first:")
    for line in commits:
        sha = line.split(" ", 1)[0]
        print(f"\n{line}")
        print(git("show", "--stat=120", "--format=", sha))
    print(f"\nRead each one (git show <sha>), queue what is worth a port in "
          f"{REVIEW.relative_to(ROOT)}, then: python3 scripts/upstream_review.py mark "
          f"{commits[-1].split(' ', 1)[0]}")


def mark(sha):
    fetch()
    full = git("rev-parse", "--verify", f"{sha}^{{commit}}")
    since = marker()
    on_upstream = subprocess.run(["git", "merge-base", "--is-ancestor", full, BRANCH], cwd=ROOT).returncode
    if on_upstream != 0:
        sys.exit(f"{full[:7]} is not on {BRANCH}")
    forward = subprocess.run(["git", "merge-base", "--is-ancestor", since, full], cwd=ROOT).returncode
    if forward != 0:
        sys.exit(f"{full[:7]} is not after the current marker {since[:7]}")
    text = REVIEW.read_text(encoding="utf-8")
    REVIEW.write_text(MARKER_RE.sub(f"reviewed: {full}", text, count=1), encoding="utf-8")
    print(f"reviewed: {since[:7]} -> {full[:7]}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("new", help="upstream commits after the marker")
    sub.add_parser("queue", help="open items")
    marking = sub.add_parser("mark", help="move the marker forward")
    marking.add_argument("sha")
    sub.add_parser("check", help="the record parses and keeps to its limits")
    args = parser.parse_args()
    if args.command == "new":
        new()
    elif args.command == "queue":
        items = open_items()
        print(f"{len(items)} open item(s):")
        for item in items:
            print(f"- {item}")
    elif args.command == "check":
        left = problems()
        print("\n".join(left) or "ok")
        sys.exit(1 if left else 0)
    else:
        mark(args.sha)


if __name__ == "__main__":
    main()
