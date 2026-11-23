#!/usr/bin/env python3
"""What OptiScaler's own nightlies add, since the last one reviewed.

The daily task fixes what a new nightly breaks, but a nightly can also add
something the plugin should follow (OptiScaler 10's own folder layout) or use
(Dynamic MFG, OptiSharpen, XeFG's real multiplier limit) without breaking a
single check. docs/optiscaler-review.md records the last OptiScaler commit
reviewed (its ``reviewed:`` line), the open items and the standing rules
(AGENTS.md, "Following OptiScaler").

    optiscaler_review.py delta [--to COMMIT | --nightly TAG]  # what changed since the marker
    optiscaler_review.py queue                                # open items
    optiscaler_review.py mark --from-log LOG                  # the review of a delta is done
    optiscaler_review.py check                                # the record keeps to its limits

``delta`` is mechanical, so nothing in its scope can slip past: INI keys added,
removed or changed (OptiScaler.ini at both commits), Config members added or
removed (Config.h), Config members OptiScaler's menu starts or stops writing
(menu/menu_common.cpp), what the bundled archive holds that bundle_nightly.py
does not know, OptiScaler's commits in between with the areas of this plugin
they touch, and changes to the v10 changelog (the ``nightly`` release of
optiscaler/OptiScaler). The files come from raw.githubusercontent.com; the
commits and the changelog need GitHub's API, which the release workflow has
(GITHUB_TOKEN) and the Claude sessions do not, so nightly.yml prints the delta
in its log between DELTA_BEGIN and DELTA_END, and ``mark --from-log`` reads the
saved log.

The record stays small: open items and rules only, each one line, under the
limits below. A resolved item is deleted in the commit that resolves it, and
that commit's message is its record (``git log --grep "OptiScaler review"``).
"""
import argparse
import difflib
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REVIEW = ROOT / "docs" / "optiscaler-review.md"
CHANGELOG = ROOT / "docs" / "optiscaler-changelog.md"
SOURCE_COMMIT = ROOT / "asi" / "optiscaler_ref" / "SOURCE_COMMIT.txt"
ARCHIVE_REPORT = ROOT / "tmp" / "archive-report.json"
REPO = "optiscaler/OptiScaler"
NIGHTLY_REPO = "optiscaler/OptiScaler-nightly"
RAW = "https://raw.githubusercontent.com/" + REPO + "/{commit}/{path}"
API = "https://api.github.com/repos/"

DELTA_BEGIN = "=== OptiScaler review delta ==="
DELTA_END = "=== OptiScaler review delta end ==="
CHANGELOG_BEGIN = "--- changelog (stored by mark) ---"
CHANGELOG_END = "--- changelog end ---"

# The record's limits. A full queue is triaged with the user, not grown.
MAX_OPEN = 12
MAX_RULES = 12
MAX_LINE = 320
MAX_BYTES = 7000
MAX_COMMITS = 80

MARKER_RE = re.compile(r"^reviewed: ([0-9a-f]{7,40}) (nightly-\d{8})$", re.M)
OPEN_RE = re.compile(r"^- \[ \] (.+)$", re.M)
ITEM_RE = re.compile(r"^[\w-]+ \([0-9a-f]{7,40}\): \S")
# The changelog was read by hand on 2026-11-23, when it said this.
BASELINE_UPDATED = "9 Sept 2026"
UPDATED_RE = re.compile(r"Last updated\s+([0-9]{1,2} \w+ [0-9]{4})")

# OptiScaler's source paths, and the part of this plugin that follows each.
AREAS = [
    ("OptiScaler.ini", "settings (schema, All settings)"),
    ("OptiScaler/Config.", "settings and the live layout (Config)"),
    ("OptiScaler/menu/", "OptiScaler's menu (the reference for live keys)"),
    ("OptiScaler/dllmain.cpp", "loading and folders (layout.py)"),
    ("OptiScaler/framegen/", "frame generation"),
    ("OptiScaler/fsr4/", "FSR 4 models (Deck presets)"),
    ("OptiScaler/upscalers/", "upscalers"),
    ("OptiScaler/shaders/rcas/", "sharpener"),
    ("OptiScaler/plugins/", "plugin API (the in-game ASI)"),
    ("OptiScaler/State.h", "enum tables"),
    ("OptiScaler/OptiTypes.", "enum tables"),
    (".github/", "packaging (archive layout)"),
    ("setup_", "packaging (archive layout)"),
]

MENU_WRITE_RE = re.compile(
    r"(?:config->|Config::Instance\(\)->)(\w+)\s*(?:=(?!=)|\.(?:reset|set_volatile_value)\()")
MEMBER_RE = re.compile(r"^\s*CustomOptional<(.+?)(?:,\s*\w+)?>\s+(\w+)\s*(\{[^;]*\})?\s*;", re.M)


# -- the record ---------------------------------------------------------------

def read(text=None):
    """(marker commit, nightly tag, open items, rules) of the record."""
    text = REVIEW.read_text(encoding="utf-8") if text is None else text
    markers = MARKER_RE.findall(text)
    if len(markers) != 1:
        sys.exit(f"{REVIEW.relative_to(ROOT)} needs exactly one 'reviewed: <commit> nightly-YYYYMMDD' line")
    rules = re.findall(r"^- (?!\[[ x]\] )(.+)$", section(text, "Rules"), re.M)
    return markers[0][0], markers[0][1], OPEN_RE.findall(section(text, "Open")), rules


def section(text, name):
    found = re.search(r"^## " + name + r"\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return found.group(1) if found else ""


def problems(text=None):
    """What keeps the record from being read, or from staying small."""
    text = REVIEW.read_text(encoding="utf-8") if text is None else text
    found = []
    if len(MARKER_RE.findall(text)) != 1:
        found.append("no single 'reviewed: <commit> nightly-YYYYMMDD' line")
        return found
    _, _, items, rules = read(text)
    found += [f"open item not '<id> (<commit>): <what>': {item[:80]}" for item in items if not ITEM_RE.match(item)]
    if re.search(r"^- \[x\] ", text, re.M):
        found.append("a done item is kept: delete it in the commit that resolves it")
    if len(items) > MAX_OPEN:
        found.append(f"{len(items)} open items (at most {MAX_OPEN}): triage the queue with the user")
    if len(rules) > MAX_RULES:
        found.append(f"{len(rules)} rules (at most {MAX_RULES}): fold them into fewer, broader ones")
    found += [f"line over {MAX_LINE} characters: {line[:60]}..." for line in text.splitlines() if len(line) > MAX_LINE]
    if len(text.encode("utf-8")) > MAX_BYTES:
        found.append(f"{len(text.encode('utf-8'))} bytes (at most {MAX_BYTES})")
    return found


# -- fetching -----------------------------------------------------------------

def _get(url, token=None, accept=None, tries=3):
    headers = {"User-Agent": "decky-optiscaler-nightly"}
    if accept:
        headers["Accept"] = accept
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code < 500 and error.code != 429 or attempt == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == tries - 1:
                raise
        time.sleep(5 * (attempt + 1))


def raw(commit, path):
    """A file of OptiScaler's at a commit, or None when it does not exist there."""
    try:
        return _get(RAW.format(commit=commit, path=path)).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def api(path, token):
    return json.loads(_get(API + path, token, "application/vnd.github+json"))


def token():
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


# -- what changed -------------------------------------------------------------

def ini_options(text):
    sys.path.insert(0, str(ROOT / "scripts"))
    import generate_schema
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "OptiScaler.ini"
        path.write_text(text, encoding="utf-8")
        return {(o["section"], o["key"]): o for o in generate_schema.parse_ini(path)}


def ini_changes(old, new):
    if old is None or new is None:
        return ["OptiScaler.ini missing at one of the commits"]
    a, b = ini_options(old), ini_options(new)
    lines = []
    for key in sorted(b.keys() - a.keys()):
        o = b[key]
        lines.append(f"added [{key[0]}] {key[1]}: {o.get('type')}, default {o.get('default')}"
                     + (f", options {o['options']}" if o.get("options") else "")
                     + (f" -- {o['description'][:160]}" if o.get("description") else ""))
    lines += [f"removed [{s}] {k}" for s, k in sorted(a.keys() - b.keys())]
    for key in sorted(a.keys() & b.keys()):
        for field in ("type", "default", "options", "min", "max"):
            if a[key].get(field) != b[key].get(field):
                lines.append(f"changed [{key[0]}] {key[1]} {field}: {a[key].get(field)} -> {b[key].get(field)}")
    return lines


def members(text):
    """{name: (type, default)} of Config.h's settings, defaults with their layout evened out."""
    return {m.group(2): (m.group(1).strip(), " ".join((m.group(3) or "").split()))
            for m in MEMBER_RE.finditer(text or "")}


def config_changes(old, new):
    a, b = members(old), members(new)
    lines = [f"added {name}: CustomOptional<{b[name][0]}> {b[name][1]}".rstrip() for name in sorted(b.keys() - a.keys())]
    lines += [f"removed {name}" for name in sorted(a.keys() - b.keys())]
    lines += [f"changed {name}: {a[name]} -> {b[name]}" for name in sorted(a.keys() & b.keys()) if a[name] != b[name]]
    return lines


def menu_writes(text):
    first = {}
    for number, line in enumerate((text or "").splitlines(), 1):
        for name in MENU_WRITE_RE.findall(line):
            first.setdefault(name, number)
    return first


def menu_changes(old, new, live_members):
    a, b = menu_writes(old), menu_writes(new)
    lines = []
    for name in sorted(b.keys() - a.keys()):
        lines.append(f"now writes {name} (line {b[name]})" + (" -- live in the panel" if name in live_members else ""))
    lines += [f"no longer writes {name}" + (" -- live in the panel" if name in live_members else "")
              for name in sorted(a.keys() - b.keys())]
    return lines


def areas_of(files):
    found = []
    for path in files:
        for prefix, area in AREAS:
            if path.startswith(prefix) and area not in found:
                found.append(area)
    return found


def commit_list(old, new, auth):
    if not auth:
        return None, "no GitHub token here (the release workflow's log has it)"
    try:
        compare = api(f"{REPO}/compare/{old}...{new}", auth)
    except (urllib.error.URLError, OSError, ValueError) as error:
        return None, f"GitHub's compare API failed: {error}"
    lines = []
    commits = compare.get("commits") or []
    for entry in commits[-MAX_COMMITS:]:
        sha = entry["sha"][:8]
        message = entry["commit"]["message"].splitlines()[0][:150]
        date = entry["commit"]["committer"]["date"][:10]
        try:
            files = [f["filename"] for f in api(f"{REPO}/commits/{entry['sha']}", auth).get("files") or []]
        except (urllib.error.URLError, OSError, ValueError):
            files = []
        areas = areas_of(files)
        lines.append(f"{sha} {date} {message}" + (f" [{'; '.join(areas)}]" if areas else ""))
    if len(commits) > MAX_COMMITS:
        lines.insert(0, f"(the newest {MAX_COMMITS} of {len(commits)})")
    if compare.get("status") == "diverged":
        lines.insert(0, f"(diverged: {compare.get('behind_by')} commit(s) of the marker are not on the new commit)")
    return lines, None


def stored_changelog():
    if not CHANGELOG.is_file():
        return None
    text = CHANGELOG.read_text(encoding="utf-8")
    return text.split("\n\n", 1)[1] if "\n\n" in text else ""


def changelog(auth):
    """(lines to print, body to store or None)."""
    if not auth:
        return ["not available here (the release workflow's log has it)"], None
    try:
        body = api(f"{REPO}/releases/tags/nightly", auth).get("body") or ""
    except (urllib.error.URLError, OSError, ValueError) as error:
        return [f"GitHub's release API failed: {error}"], None
    body = body.replace("\r\n", "\n").strip() + "\n"
    before = stored_changelog()
    if before is None:
        updated = UPDATED_RE.search(body)
        if updated and updated.group(1) == BASELINE_UPDATED:
            return [f"baseline: last updated {BASELINE_UPDATED}, as when it was read by hand (2026-11-23); "
                    "mark stores it, nothing to queue"], body
        return ["no snapshot yet, and it changed since it was read by hand: all of it is new"], body
    if before == body:
        return ["unchanged"], None
    diff = list(difflib.unified_diff(before.splitlines(), body.splitlines(), "before", "now", lineterm="", n=1))
    return diff[2:200] + (["(diff cut at 200 lines)"] if len(diff) > 202 else []), body


def resolve(nightly, auth):
    """The OptiScaler commit a nightly was built from (its asset names it)."""
    release = api(f"{NIGHTLY_REPO}/releases/tags/{nightly}", auth)
    for asset in release.get("assets") or []:
        found = re.search(r"_([0-9a-f]{7,40})\.7z$", asset["name"])
        if found:
            return found.group(1)
    sys.exit(f"{nightly}: no asset names its commit")


def target(to, nightly, bundled, auth):
    """The commit to review up to: given, the bundled dll's, or the nightly's (its asset names it).

    Not the bundled one unless this run bundled: SOURCE_COMMIT.txt is also in
    the committed snapshot, which is usually older.
    """
    if to:
        return to
    if bundled or not nightly:
        if not SOURCE_COMMIT.is_file():
            sys.exit("no commit to compare to: bundle a nightly first, or give --to")
        return SOURCE_COMMIT.read_text(encoding="utf-8").split()[0]
    if not auth:
        sys.exit(f"finding {nightly}'s commit needs GitHub's API (no token here): give --to")
    return resolve(nightly, auth)


def delta(to=None, nightly=None, bundled=False, out=sys.stdout):
    marker, marker_nightly, items, _ = read()
    auth = token()
    to = target(to, nightly, bundled, auth)
    nightly = nightly or "nightly-?"
    print(DELTA_BEGIN, file=out)
    print(f"from: {marker} ({marker_nightly})", file=out)
    print(f"to: {to} ({nightly})", file=out)
    print(f"open items: {len(items)} of {MAX_OPEN}", file=out)
    same = marker.startswith(to) or to.startswith(marker)

    def block(title, lines):
        print(f"\n## {title}", file=out)
        for line in lines or ["nothing"]:
            print(f"  {line}", file=out)

    if same:
        print("\nsame commit as the marker: nothing new in OptiScaler's code", file=out)
    else:
        commits, why = commit_list(marker, to, auth)
        block("Commits, oldest first (the areas of this plugin they touch)", commits if commits is not None else [why])
        block("OptiScaler.ini", ini_changes(raw(marker, "OptiScaler.ini"), raw(to, "OptiScaler.ini")))
        block("Config members (Config.h)", config_changes(raw(marker, "OptiScaler/Config.h"),
                                                          raw(to, "OptiScaler/Config.h")))
        sys.path.insert(0, str(ROOT / "py_modules"))
        from optiscaler import live
        block("What OptiScaler's menu writes (menu_common.cpp)",
              menu_changes(raw(marker, "OptiScaler/menu/menu_common.cpp"),
                           raw(to, "OptiScaler/menu/menu_common.cpp"),
                           {field.member for field in live.LIVE_FIELDS.values()}))
    if ARCHIVE_REPORT.is_file():
        report = json.loads(ARCHIVE_REPORT.read_text(encoding="utf-8"))
        block(f"In the archive and new to bundle_nightly.py ({report.get('archive')})",
              [f"{name} (not installed)" for name in report.get("unclassified") or []])
    lines, body = changelog(auth)
    block("The v10 changelog (optiscaler/OptiScaler, release 'nightly')", lines)
    if body is not None:
        print(f"\n{CHANGELOG_BEGIN}", file=out)
        print(body, end="", file=out)
        print(CHANGELOG_END, file=out)
    print(DELTA_END, file=out)


# -- marking ------------------------------------------------------------------

LOG_PREFIX_RE = re.compile(r"^(?:[^\t\n]*\t){0,2}\d{4}-\d\d-\d\dT[\d:.]+Z ?")


def from_log(text):
    """(from commit, to commit, nightly, changelog body or None) of the delta in a saved log."""
    lines = [LOG_PREFIX_RE.sub("", line) for line in text.splitlines()]
    try:
        start = max(i for i, line in enumerate(lines) if line.strip() == DELTA_BEGIN)
    except ValueError:
        sys.exit("no OptiScaler review delta in that log")
    end = next((i for i in range(start, len(lines)) if lines[i].strip() == DELTA_END), None)
    if end is None:
        sys.exit("the delta in that log is cut short")
    block = lines[start:end]
    since = re.match(r"from: ([0-9a-f]+) ", next(line for line in block if line.startswith("from: ")))
    to = re.match(r"to: ([0-9a-f]+) \((nightly-[\d?]+)\)", next(line for line in block if line.startswith("to: ")))
    body = None
    if CHANGELOG_BEGIN in block:
        first = block.index(CHANGELOG_BEGIN) + 1
        last = block.index(CHANGELOG_END)
        body = "\n".join(block[first:last]) + "\n"
    return since.group(1), to.group(1), to.group(2), body


def mark(log_path):
    text = REVIEW.read_text(encoding="utf-8")
    marker, _, _, _ = read(text)
    since, to, nightly, body = from_log(Path(log_path).read_text(encoding="utf-8", errors="replace"))
    if not (marker.startswith(since) or since.startswith(marker)):
        sys.exit(f"that delta starts at {since}, the record at {marker}: use the newest release run's log")
    if not re.match(r"nightly-\d{8}$", nightly):
        sys.exit(f"the delta does not name its nightly ({nightly})")
    REVIEW.write_text(MARKER_RE.sub(f"reviewed: {to} {nightly}", text), encoding="utf-8")
    if body is not None:
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
        CHANGELOG.write_text(f"<!-- OptiScaler's v10 changelog (the 'nightly' release of {REPO}) as last reviewed; "
                             f"scripts/optiscaler_review.py replaces it. sha256 {digest} -->\n\n{body}",
                             encoding="utf-8")
    print(f"reviewed up to {to} ({nightly})" + ("; changelog stored" if body is not None else ""))
    left = problems()
    if left:
        sys.exit("the record is over its limits: " + "; ".join(left))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("delta", help="what changed in OptiScaler since the marker")
    d.add_argument("--to", help="OptiScaler commit (default: the bundled dll's)")
    d.add_argument("--nightly", help="the nightly's tag (nightly-YYYYMMDD): names it, and gives its commit")
    d.add_argument("--bundled", action="store_true", help="this run bundled that nightly (its commit is the dll's)")
    sub.add_parser("queue", help="open items")
    m = sub.add_parser("mark", help="the delta in a saved release log is reviewed")
    m.add_argument("--from-log", required=True)
    sub.add_parser("check", help="the record parses and keeps to its limits")
    args = parser.parse_args()
    if args.command == "delta":
        delta(args.to, args.nightly, args.bundled)
    elif args.command == "queue":
        _, _, items, _ = read()
        print("\n".join(f"- [ ] {item}" for item in items) or "the queue is empty")
    elif args.command == "mark":
        mark(args.from_log)
    else:
        left = problems()
        print("\n".join(left) or "ok")
        sys.exit(1 if left else 0)


if __name__ == "__main__":
    main()
