"""Self-update for the nightly fork, through Decky's own installer.

Decky only updates plugins it installed from its store. This fork is installed
from a release URL, so the plugin checks the fork's latest GitHub release
itself and, when the user asks, hands the zip to Decky's installer: the frontend
calls the same `utilities/install_plugin` route Decky's store and "Install
Plugin from URL" use. Decky shows its own confirmation, checks the SHA-256
given here, replaces the plugin as root and reloads it, so this plugin never
writes to its own install folder.

A custom Decky store URL would also give update prompts, but Decky has one
store at a time, and pointing it here would cut every other plugin off from
the official store's updates.
"""

import json
import time
from pathlib import Path

from . import wiki

UPDATE_REPO = "olexandrger/decky-optiscaler-nightly"
UPDATE_ASSET = "decky-optiscaler-nightly.zip"
LATEST_URL = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"
#: How long one answer is reused. Short: this fork has one user, who wants a new
#: build offered as soon as it is released; the minute only stops tab switches
#: re-asking GitHub (60 unauthenticated requests an hour per address). The
#: Settings tab's "Check for updates" skips it entirely.
CHECK_TTL = 60


def installed_version(plugin_dir):
    """The version this plugin was built as (package.json, as Decky reads it)."""
    try:
        data = json.loads((Path(plugin_dir) / "package.json").read_text(encoding="utf-8"))
        return str(data.get("version") or "")
    except (OSError, ValueError):
        return ""


def _sha256_of(asset, release, fetch_text):
    """The zip's SHA-256: GitHub's own asset digest, else the .sha256 sidecar."""
    digest = str(asset.get("digest") or "")
    if digest.startswith("sha256:") and len(digest) == len("sha256:") + 64:
        return digest.split(":", 1)[1]
    sidecar = next((a for a in release.get("assets", [])
                    if a.get("name") == UPDATE_ASSET + ".sha256"), None)
    if sidecar:
        text = fetch_text(sidecar["browser_download_url"]).strip()
        candidate = text.split()[0] if text else ""
        if len(candidate) == 64 and all(c in "0123456789abcdef" for c in candidate.lower()):
            return candidate.lower()
    return ""


def latest_release(fetch_json=wiki.download_json, fetch_text=None):
    """The fork's newest release: version, zip URL, its SHA-256 and page."""
    if fetch_text is None:
        fetch_text = lambda url: wiki._http_get(url, timeout=20)  # noqa: E731
    release = fetch_json(LATEST_URL)
    asset = next((a for a in release.get("assets", []) if a.get("name") == UPDATE_ASSET), None)
    if asset is None:
        raise ValueError(f"the latest release has no {UPDATE_ASSET}")
    return {
        "version": str(release.get("tag_name") or "").lstrip("v"),
        "url": asset["browser_download_url"],
        "sha256": _sha256_of(asset, release, fetch_text),
        "page": release.get("html_url"),
        "published": release.get("published_at"),
    }


class UpdateChecker:
    """Caches the answer so opening the panel does not ask GitHub every time."""

    def __init__(self, plugin_dir, fetch=latest_release, clock=time.time):
        self.plugin_dir = plugin_dir
        self.fetch = fetch
        self.clock = clock
        self._cached = None
        self._at = 0.0

    def check(self, force=False):
        current = installed_version(self.plugin_dir)
        if force or self._cached is None or self.clock() - self._at > CHECK_TTL:
            self._cached = self.fetch()
            self._at = self.clock()
        latest = self._cached
        return {
            "current": current,
            "latest": latest["version"],
            # Any difference from the newest release is an update: releases
            # only move forward, and a locally built zip should be replaced too.
            "available": bool(latest["version"]) and latest["version"] != current,
            "url": latest["url"],
            "sha256": latest["sha256"],
            "page": latest["page"],
        }
