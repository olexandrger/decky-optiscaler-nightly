#!/usr/bin/env python3
"""bundle_nightly.py's download: retries on GitHub's transient failures, not on real errors.

A scheduled run failed on one 504 from GitHub's release-asset server. The
network is replaced by fakes; nothing is downloaded.
"""
import http.client
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bundle_nightly  # noqa: E402

ASSET = {"name": "OptiScaler_v10.0.0-pre1_20261108.7z",
         "browser_download_url": "https://example/asset.7z", "size": 8}
RELEASE = [{"tag_name": "nightly-20261108", "assets": [ASSET]}]


def http_error(code):
    return urllib.error.HTTPError("https://example", code, "x", {}, io.BytesIO())


class Network:
    """Answers each request in turn from a script of bodies and exceptions."""

    def __init__(self, release=(), asset=()):
        self.script = {"api": list(release), "asset": list(asset)}
        self.calls = {"api": 0, "asset": 0}

    def __call__(self, request, timeout=None):
        kind = "asset" if request == ASSET["browser_download_url"] else "api"
        self.calls[kind] += 1
        answer = self.script[kind].pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return io.BytesIO(answer)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.slept = []

    def download(self, network):
        return bundle_nightly.download_latest(self.temp.name, opener=network,
                                              sleep=self.slept.append)

    def test_a_gateway_timeout_on_the_asset_is_retried(self):
        net = Network(release=[json.dumps(RELEASE).encode()],
                      asset=[http_error(504), b"7zpayload"[:8]])
        target = self.download(net)
        self.assertEqual(target.read_bytes(), b"7zpayloa")
        self.assertEqual(net.calls["asset"], 2)
        self.assertEqual(self.slept, [bundle_nightly.RETRY_DELAYS[0]])

    def test_a_cut_transfer_starts_the_file_over(self):
        net = Network(release=[json.dumps(RELEASE).encode()],
                      asset=[b"7z", b"12345678"])
        self.assertEqual(self.download(net).read_bytes(), b"12345678")
        self.assertEqual(net.calls["asset"], 2)

    def test_the_api_is_retried_too(self):
        net = Network(release=[urllib.error.URLError("reset"), json.dumps(RELEASE).encode()],
                      asset=[b"12345678"])
        self.download(net)
        self.assertEqual(net.calls["api"], 2)

    def test_it_gives_up_after_the_last_delay(self):
        attempts = len(bundle_nightly.RETRY_DELAYS) + 1
        net = Network(release=[json.dumps(RELEASE).encode()],
                      asset=[http_error(502)] * attempts)
        with self.assertRaises(urllib.error.HTTPError):
            self.download(net)
        self.assertEqual(net.calls["asset"], attempts)
        self.assertEqual(self.slept, list(bundle_nightly.RETRY_DELAYS))

    def test_a_real_error_is_not_retried(self):
        net = Network(release=[json.dumps(RELEASE).encode()], asset=[http_error(404)])
        with self.assertRaises(urllib.error.HTTPError):
            self.download(net)
        self.assertEqual((net.calls["asset"], self.slept), (1, []))

    def test_transient_means_5xx_429_and_dropped_connections(self):
        transient = bundle_nightly._transient
        for error in (http_error(500), http_error(504), http_error(429), TimeoutError(),
                      ConnectionResetError(), http.client.IncompleteRead(b"", 1)):
            self.assertTrue(transient(error), error)
        for error in (http_error(403), http_error(404), ValueError("bad json")):
            self.assertFalse(transient(error), error)


if __name__ == "__main__":
    unittest.main()
