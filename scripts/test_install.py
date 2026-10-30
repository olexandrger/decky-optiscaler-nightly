#!/usr/bin/env python3
"""Install safety: a failed or interrupted install leaves the game folder as it was.

The install copies a few dozen files and sets the game's own aside. Before it
was journaled, a failure part way (disk full, I/O error, plugin killed) left a
half-installed folder with no manifest, and uninstalling that deleted game
files that had the same names as OptiScaler's. These tests inject a failure at
every write the install makes and check the folder byte for byte.

A small synthetic payload is used so every step can be failed in turn without
copying the real 200 MB release each time; the real payload's layout is
covered by selftest's check_bundled_payload_upgrade.
"""
import json
import logging
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "py_modules"))
from optiscaler import installer  # noqa: E402
from optiscaler.constants import (  # noqa: E402
    BACKUP_DIR, FFX_UPSCALER_DLL, INI_NAME, MANIFEST_NAME, PAYLOAD_DIRS, PAYLOAD_FILES,
)

LOG = logging.getLogger("test")


def make_payload(root):
    """A payload with the real file names and tiny contents (the proxy has to
    look like OptiScaler: over 4 MB with the marker in it)."""
    payload = root / "payload"
    payload.mkdir()
    (payload / "OptiScaler.dll").write_bytes(b"OptiScaler" + b"\0" * (5 * 1024 * 1024))
    (payload / INI_NAME).write_text("[Menu]\nShortcutKey=auto\n", encoding="utf-8")
    for name in PAYLOAD_FILES:
        (payload / name).write_bytes(f"payload {name}".encode())
    for name in PAYLOAD_DIRS:
        (payload / name).mkdir()
        (payload / name / "inside.txt").write_bytes(f"payload {name}".encode())
    return payload


def snapshot(folder):
    """Every file under `folder` with its bytes, plus every directory."""
    return {
        str(p.relative_to(folder)): (p.read_bytes() if p.is_file() else None)
        for p in sorted(folder.rglob("*"))
    }


class FailingCopies:
    """Make the n-th file write of an install raise, like a full disk would."""

    def __init__(self, fail_at):
        self.fail_at = fail_at
        self.count = 0
        self.real_copy2 = shutil.copy2
        self.real_copytree = shutil.copytree

    def _tick(self):
        self.count += 1
        if self.count == self.fail_at:
            raise OSError(28, "No space left on device")

    def copy2(self, *args, **kwargs):
        self._tick()
        return self.real_copy2(*args, **kwargs)

    def copytree(self, *args, **kwargs):
        self._tick()
        return self.real_copytree(*args, **kwargs)

    def __enter__(self):
        self._patches = [mock.patch.object(installer.shutil, "copy2", self.copy2),
                         mock.patch.object(installer.shutil, "copytree", self.copytree)]
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in self._patches:
            patch.stop()


class InstallSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.payload = make_payload(self.root)
        self.game = self.root / "game"
        self.game.mkdir()
        (self.game / "Game.exe").write_bytes(b"MZ game")
        # Files the game ships under OptiScaler's names: these are what a
        # careless uninstall deletes.
        (self.game / "dxgi.dll").write_bytes(b"the game's own dxgi")
        (self.game / "libxess.dll").write_bytes(b"the game's own XeSS")
        (self.game / "Licenses").mkdir()
        (self.game / "Licenses" / "game.txt").write_bytes(b"the game's licence")

    def count_writes(self):
        """How many file writes a clean install makes (in a throwaway copy)."""
        clone = self.root / "count"
        shutil.copytree(self.game, clone)
        with FailingCopies(fail_at=10**9) as counter:
            installer.install(clone, self.payload, "dxgi.dll", logger=LOG)
        shutil.rmtree(clone)
        return counter.count

    def test_a_failure_at_any_write_leaves_the_folder_as_it_was(self):
        before = snapshot(self.game)
        writes = self.count_writes()
        self.assertGreater(writes, 10)
        for fail_at in range(1, writes + 1):
            with self.subTest(fail_at=fail_at):
                with FailingCopies(fail_at), self.assertRaises(OSError):
                    installer.install(self.game, self.payload, "dxgi.dll", logger=LOG)
                self.assertEqual(snapshot(self.game), before)

    def test_a_failed_upgrade_keeps_the_previous_install_working(self):
        installer.install(self.game, self.payload, "dxgi.dll", logger=LOG)
        installed = snapshot(self.game)
        (self.payload / "libxess.dll").write_bytes(b"a newer XeSS")
        writes = self.count_writes()
        for fail_at in range(1, writes + 1):
            with self.subTest(fail_at=fail_at):
                with FailingCopies(fail_at), self.assertRaises(OSError):
                    installer.install(self.game, self.payload, "dxgi.dll", logger=LOG)
                self.assertEqual(snapshot(self.game), installed)

    def test_failure_then_uninstall_restores_the_game(self):
        before = snapshot(self.game)
        with FailingCopies(5), self.assertRaises(OSError):
            installer.install(self.game, self.payload, "dxgi.dll", logger=LOG)
        installer.uninstall(self.game, logger=LOG, payload_root=self.payload)
        self.assertEqual(snapshot(self.game), before)

    def test_failure_then_retry_then_uninstall_restores_the_game(self):
        before = snapshot(self.game)
        with FailingCopies(5), self.assertRaises(OSError):
            installer.install(self.game, self.payload, "dxgi.dll", logger=LOG)
        installer.install(self.game, self.payload, "dxgi.dll", logger=LOG)
        # Every file landed; the upscaler is only "wrong" because verify checks
        # it against the real bundled hash, which a synthetic payload lacks.
        problems = installer.verify_install(self.game, self.payload)["problems"]
        self.assertEqual(set(problems) - {FFX_UPSCALER_DLL}, set())
        installer.uninstall(self.game, logger=LOG, payload_root=self.payload)
        self.assertEqual(snapshot(self.game), before)

    def test_an_install_killed_half_way_is_rolled_back_next_time(self):
        before = snapshot(self.game)
        # The plugin dies: no exception handler runs, the journal stays behind.
        with FailingCopies(7), mock.patch.object(installer._Journal, "rollback"), \
                self.assertRaises(OSError):
            installer.install(self.game, self.payload, "dxgi.dll", logger=LOG)
        self.assertTrue((self.game / installer.INSTALL_STAGING).is_dir())
        self.assertTrue(installer.recover_interrupted(self.game, LOG))
        self.assertEqual(snapshot(self.game), before)

    def test_the_next_install_or_uninstall_finishes_an_interrupted_one_first(self):
        before = snapshot(self.game)
        for action in ("install", "uninstall"):
            with self.subTest(action=action):
                with FailingCopies(7), mock.patch.object(installer._Journal, "rollback"), \
                        self.assertRaises(OSError):
                    installer.install(self.game, self.payload, "dxgi.dll", logger=LOG)
                if action == "install":
                    installer.install(self.game, self.payload, "dxgi.dll", logger=LOG)
                installer.uninstall(self.game, logger=LOG, payload_root=self.payload)
                self.assertEqual(snapshot(self.game), before)

    def test_a_successful_install_leaves_no_staging_behind(self):
        installer.install(self.game, self.payload, "dxgi.dll", logger=LOG)
        self.assertFalse((self.game / installer.INSTALL_STAGING).exists())
        manifest = json.loads((self.game / MANIFEST_NAME).read_text())
        self.assertEqual(sorted(manifest["backups"]), ["Licenses", "dxgi.dll", "libxess.dll"])
        self.assertEqual((self.game / BACKUP_DIR / "libxess.dll").read_bytes(), b"the game's own XeSS")

    def test_a_folder_that_cannot_be_written_is_refused_untouched(self):
        before = snapshot(self.game)
        with mock.patch.object(installer.os, "access", return_value=False), \
                self.assertRaises(PermissionError):
            installer.install(self.game, self.payload, "dxgi.dll", logger=LOG)
        self.assertEqual(snapshot(self.game), before)

    def test_without_a_manifest_only_files_identical_to_ours_are_removed(self):
        # OptiScaler placed by hand: proxy, two payload files, a payload dir;
        # the game's own libxess.dll and Licenses/ sit beside them.
        shutil.copy2(self.payload / "OptiScaler.dll", self.game / "winmm.dll")
        for name in ("libxell.dll", "libxess_fg.dll"):
            shutil.copy2(self.payload / name, self.game / name)
        shutil.copytree(self.payload / "D3D12_OptiScaler", self.game / "D3D12_OptiScaler")
        result = installer.uninstall(self.game, logger=LOG, payload_root=self.payload)
        self.assertIn("winmm.dll", result["removed"])
        self.assertIn("libxell.dll", result["removed"])
        self.assertIn("D3D12_OptiScaler/", result["removed"])
        self.assertEqual((self.game / "libxess.dll").read_bytes(), b"the game's own XeSS")
        self.assertEqual((self.game / "Licenses" / "game.txt").read_bytes(), b"the game's licence")
        self.assertIn("libxess.dll", result["left"])
        self.assertIn("Licenses/", result["left"])

    def test_without_the_payload_nothing_unprovable_is_removed(self):
        shutil.copy2(self.payload / "libxell.dll", self.game / "libxell.dll")
        shutil.copy2(self.payload / "OptiScaler.dll", self.game / "winmm.dll")
        result = installer.uninstall(self.game, logger=LOG)
        self.assertIn("winmm.dll", result["removed"])  # the proxy is recognisable on its own
        self.assertTrue((self.game / "libxell.dll").exists())
        self.assertTrue((self.game / "libxess.dll").exists())


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()
