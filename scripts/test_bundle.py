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


class NightlyDateTests(unittest.TestCase):
    """The date every version, tag and "already released?" check hangs off.

    The 17 Nov nightly appended its commit to the archive name, the version
    lost its date and a release went out as v0.0.7-nightly.10.0.0-pre1.21.
    """

    def test_every_archive_name_shape(self):
        for name in ("OptiScaler_v10.0.0-pre1_20261030.7z",
                     "OptiScaler_v10.0.0-pre1_20261117_03b37db7.7z",
                     "OptiScaler-10.0.0-pre1-20261030.7z"):
            self.assertRegex(bundle_nightly.nightly_date(name), r"^2026\d{4}$", name)
        self.assertEqual(bundle_nightly.nightly_date("OptiScaler_v10.0.0-pre1_20261117_03b37db7.7z"),
                         "20261117")

    def test_the_tag_wins_over_the_name(self):
        self.assertEqual(bundle_nightly.nightly_date("OptiScaler_v10.0.0-pre1_20261116.7z",
                                                     tag="nightly-20261117"), "20261117")

    def test_the_dll_build_stamp_is_the_last_resort(self):
        self.assertEqual(bundle_nightly.nightly_date("OptiScaler.7z", built="20261117_124833"),
                         "20261117")
        self.assertIsNone(bundle_nightly.nightly_date("OptiScaler_v10.0.0-pre1.7z"))

    def test_a_commit_hash_is_not_a_date(self):
        self.assertIsNone(bundle_nightly.nightly_date("OptiScaler_v10.0.0-pre1_03b37db7.7z"))

    def test_the_plugin_version_is_the_nightlys_date(self):
        self.assertEqual(bundle_nightly.plugin_version("10.0.0-pre1-20261123"), "2026.11.23")
        # Plain numbers: semver allows no leading zeros.
        self.assertEqual(bundle_nightly.plugin_version("10.0.0-pre1-20270105"), "2027.1.5")
        with self.assertRaises(SystemExit):
            bundle_nightly.plugin_version("10.0.0-pre1")


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

    def test_a_pinned_nightly_is_fetched_by_its_tag(self):
        asked = []

        class Pinned(Network):
            def __call__(self, request, timeout=None):
                if request != ASSET["browser_download_url"]:
                    asked.append(request.full_url)
                return super().__call__(request, timeout)

        # /releases/tags/<tag> answers one release, not a list.
        net = Pinned(release=[json.dumps(RELEASE[0]).encode()], asset=[b"12345678"])
        target = bundle_nightly.download_latest(self.temp.name, opener=net, sleep=self.slept.append,
                                                tag="nightly-20261108")
        self.assertEqual(asked, ["https://api.github.com/repos/optiscaler/OptiScaler-nightly/"
                                 "releases/tags/nightly-20261108"])
        self.assertEqual(target.name, ASSET["name"])

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


class PayloadListTests(unittest.TestCase):
    """What a bundle installs: OptiScaler 10's layout, its DLLs and licences in OptiScaler/."""

    def nightly(self, root):
        """A nightly archive's layout (20261122: marker, scripts, Licenses/ at the top)."""
        for name in ("OptiScaler.dll", "setup_linux.sh", "setup_windows.bat",
                     "!! EXTRACT ALL FILES TO GAME FOLDER !!"):
            (root / name).write_bytes(b"x")
        (root / "OptiScaler.ini").write_text("[Menu]\n")
        (root / "Licenses").mkdir()
        (root / "Licenses" / "XeSS_LICENSE.txt").write_bytes(b"x")
        deps = root / "OptiScaler"
        (deps / "D3D12_OptiScaler").mkdir(parents=True)
        (deps / "D3D12_OptiScaler" / "D3D12Core.dll").write_bytes(b"x")
        (deps / "plugins").mkdir()
        (deps / "libxess.dll").write_bytes(b"x")
        (deps / "Streamlined_fetcher_windows.bat").write_bytes(b"x")
        (deps / "notes.txt").write_bytes(b"x")

    def test_the_archive_keeps_optiscalers_folder_and_drops_what_never_goes_in_a_game(self):
        with tempfile.TemporaryDirectory() as scratch:
            extracted = Path(scratch)
            self.nightly(extracted)
            (extracted / "WhatsNew.txt").write_bytes(b"x")
            unclassified = []
            root = bundle_nightly.arrange(extracted, unclassified)
            left = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
            files, dirs = bundle_nightly.payload_lists(root)
        # What it neither installs nor knows to leave out is reported (the
        # release run's OptiScaler review lists it), not silently dropped.
        self.assertEqual(unclassified, ["WhatsNew.txt", "OptiScaler/notes.txt"])
        self.assertEqual(left, ["OptiScaler", "OptiScaler.dll", "OptiScaler.ini", "OptiScaler/D3D12_OptiScaler",
                                "OptiScaler/D3D12_OptiScaler/D3D12Core.dll", "OptiScaler/Licenses",
                                "OptiScaler/Licenses/XeSS_LICENSE.txt", "OptiScaler/libxess.dll",
                                "OptiScaler/notes.txt"])
        # Only DLLs and licences: not the marker, the scripts, the empty
        # plugins/ (the game's own ASIs live there), nor whatever else it holds.
        self.assertEqual(files, ["OptiScaler/D3D12_OptiScaler/D3D12Core.dll",
                                 "OptiScaler/Licenses/XeSS_LICENSE.txt", "OptiScaler/libxess.dll"])
        self.assertEqual(dirs, ["OptiScaler", "OptiScaler/D3D12_OptiScaler", "OptiScaler/Licenses"])

    def test_an_archive_without_optiscalers_folder_is_refused(self):
        with tempfile.TemporaryDirectory() as scratch:
            extracted = Path(scratch)
            (extracted / "OptiScaler.dll").write_bytes(b"x")
            (extracted / "libxess.dll").write_bytes(b"x")
            with self.assertRaises(SystemExit):
                bundle_nightly.arrange(extracted)


class HeaderSyncTests(unittest.TestCase):
    """Syncing OptiScaler's headers at the dll's commit, and reading their enum tokens.

    The 21 Nov nightly (1c4e4848) moved UpscalerToCode out of OptiTypes.cpp
    into an EnumConfig<Upscaler> table in OptiTypes.h that lists aliases
    after each value's own token; the sync refused the file and bundling
    stopped.
    """

    OPTITYPES_CPP = b'#include "pch.h"\n\n#include "OptiTypes.h"\n' + b"// body\n" * 200
    UPSCALER = """
    enum class Upscaler { XeSS, XeSS_on12, FSR21, FSR21_on12, FSR22, FSR22_on12, FFX, FFX_on12,
                          DLSS, DLSS_on12, FSR31, DLSSD, Reset };
    template <> struct EnumConfig<Upscaler>
    {
        static constexpr std::pair<Upscaler, std::string_view> mapping[] = {
            { Upscaler::XeSS, "xess" },         { Upscaler::FFX, "ffx" },
            { Upscaler::FFX_on12, "ffx_12" },   { Upscaler::DLSS, "dlss" },
            { Upscaler::FSR31, "fsr31" },       // DX11 only
            { Upscaler::DLSS, "dlssd" },        // Ray reconstruction is picked by RRUpscaler
            { Upscaler::FFX_on12, "fsr31_12" }, // From before FSR 3.1 got renamed to FFX
            { Upscaler::DLSSD, "dlssd" },
        };
    };
    """

    def test_an_optitypes_cpp_without_upscalertocode_is_accepted(self):
        import sync_optiscaler_headers as sync
        data = sync.fetch("1c4e4848", "OptiTypes.cpp", lambda url, timeout=None: io.BytesIO(self.OPTITYPES_CPP))
        self.assertEqual(data, self.OPTITYPES_CPP)

    def test_an_error_page_is_still_refused(self):
        import sync_optiscaler_headers as sync
        page = b"<html>404: Not Found</html>" + b" " * 2000
        with self.assertRaises(ValueError):
            sync.fetch("1c4e4848", "OptiTypes.cpp", lambda url, timeout=None: io.BytesIO(page))

    def test_each_value_gets_the_token_enumtocode_writes(self):
        import generate_asi_layout
        enum = generate_asi_layout.parse_enums(self.UPSCALER)["Upscaler"]
        token = {name: enum["codes"].get(str(value)) for name, value in enum["values"].items()}
        self.assertEqual((token["DLSS"], token["FFX_on12"], token["DLSSD"], token["FFX"], token["FSR31"]),
                         ("dlss", "ffx_12", "dlssd", "ffx", "fsr31"))

    def test_the_older_upscalertocode_still_reads(self):
        import generate_asi_layout
        text = ('enum class Upscaler { XeSS, FFX, Reset };\n'
                'std::string UpscalerToCode(Upscaler upscaler)\n{\n    switch (upscaler)\n    {\n'
                '    case Upscaler::XeSS: return "xess";\n    case Upscaler::FFX: return "ffx";\n    }\n}\n')
        enum = generate_asi_layout.parse_enums(text)["Upscaler"]
        self.assertEqual(enum["codes"], {"0": "xess", "1": "ffx"})


if __name__ == "__main__":
    unittest.main()
