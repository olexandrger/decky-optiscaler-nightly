#!/usr/bin/env python3
"""The in-plugin updater: reading the fork's latest release and deciding on an update.

The network is replaced by fixtures shaped like GitHub's releases API; the
install itself is Decky's (see src/components/UpdateBanner.tsx and the
live-fields harness, which checks the call made to it).
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "py_modules"))
from optiscaler import update  # noqa: E402

SHA = "a" * 64
ZIP_URL = ("https://github.com/olexandrger/decky-optiscaler-nightly/releases/download/"
           "v0.0.7-nightly.20261031/decky-optiscaler-nightly.zip")


def release(tag="v0.0.7-nightly.20261031", digest=f"sha256:{SHA}", sidecar=None, asset=True):
    assets = []
    if asset:
        entry = {"name": update.UPDATE_ASSET, "browser_download_url": ZIP_URL}
        if digest is not None:
            entry["digest"] = digest
        assets.append(entry)
    if sidecar is not None:
        assets.append({"name": update.UPDATE_ASSET + ".sha256",
                       "browser_download_url": "https://example/sidecar"})
    return {"tag_name": tag, "html_url": "https://example/release", "assets": assets,
            "published_at": "2026-10-31T05:50:00Z"}


class LatestReleaseTests(unittest.TestCase):
    def test_the_release_asset_and_githubs_digest_are_used(self):
        got = update.latest_release(fetch_json=lambda url: release(), fetch_text=None)
        self.assertEqual(got["version"], "0.0.7-nightly.20261031")
        self.assertEqual(got["url"], ZIP_URL)
        self.assertEqual(got["sha256"], SHA)

    def test_without_a_digest_the_sidecar_is_read(self):
        got = update.latest_release(
            fetch_json=lambda url: release(digest=None, sidecar=True),
            fetch_text=lambda url: f"{'b' * 64}  decky-optiscaler-nightly.zip\n")
        self.assertEqual(got["sha256"], "b" * 64)

    def test_a_malformed_sidecar_gives_no_hash_rather_than_a_wrong_one(self):
        got = update.latest_release(
            fetch_json=lambda url: release(digest=None, sidecar=True),
            fetch_text=lambda url: "not a hash")
        self.assertEqual(got["sha256"], "")

    def test_a_release_without_the_zip_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "no decky-optiscaler-nightly.zip"):
            update.latest_release(fetch_json=lambda url: release(asset=False))

    def test_it_asks_this_forks_releases(self):
        asked = []
        update.latest_release(fetch_json=lambda url: asked.append(url) or release())
        self.assertEqual(asked, ["https://api.github.com/repos/olexandrger/"
                                 "decky-optiscaler-nightly/releases/latest"])


class CheckerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.plugin = Path(self.temp.name)
        self.set_version("0.0.7-nightly.20261030")
        self.calls = 0
        self.now = 1000.0

    def set_version(self, version):
        (self.plugin / "package.json").write_text(json.dumps({"version": version}))

    def fetch(self):
        self.calls += 1
        return {"version": "0.0.7-nightly.20261031", "url": ZIP_URL, "sha256": SHA, "page": "p"}

    def checker(self):
        return update.UpdateChecker(self.plugin, fetch=self.fetch, clock=lambda: self.now)

    def test_a_newer_release_is_an_update(self):
        result = self.checker().check()
        self.assertTrue(result["available"])
        self.assertEqual((result["current"], result["latest"]),
                         ("0.0.7-nightly.20261030", "0.0.7-nightly.20261031"))
        self.assertEqual((result["url"], result["sha256"]), (ZIP_URL, SHA))

    def test_the_installed_release_is_not(self):
        self.set_version("0.0.7-nightly.20261031")
        self.assertFalse(self.checker().check()["available"])

    def test_the_answer_is_reused_until_it_is_old_or_forced(self):
        checker = self.checker()
        checker.check()
        checker.check()
        self.assertEqual(self.calls, 1)
        self.now += update.CHECK_TTL + 1
        checker.check()
        self.assertEqual(self.calls, 2)
        checker.check(force=True)
        self.assertEqual(self.calls, 3)

    def test_the_version_decky_reads_is_the_one_compared(self):
        self.assertEqual(update.installed_version(self.plugin), "0.0.7-nightly.20261030")
        (self.plugin / "package.json").unlink()
        self.assertEqual(update.installed_version(self.plugin), "")


if __name__ == "__main__":
    unittest.main()
