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
from optiscaler import installer, payload as payload_mod, steam  # noqa: E402
from optiscaler.constants import (  # noqa: E402
    BACKUP_DIR, FFX_UPSCALER_DLL, INI_NAME, MANIFEST_NAME, PAYLOAD_ARCHIVE, PAYLOAD_DIRS,
    PAYLOAD_FILES, PROXY_FILENAMES,
)

ROOT = Path(__file__).resolve().parents[1]
MB = 1024 * 1024

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

    def test_a_crash_while_committing_does_not_undo_the_install(self):
        # Killed after the commit point (journal gone) but before staging was
        # cleared: the next run must keep the finished install.
        real_rmtree = shutil.rmtree
        def leave_staging(path, *a, **k):
            if Path(path).name == installer.INSTALL_STAGING:
                raise KeyboardInterrupt("killed")
            return real_rmtree(path, *a, **k)
        with mock.patch.object(installer.shutil, "rmtree", side_effect=leave_staging), \
                self.assertRaises(KeyboardInterrupt):
            installer.install(self.game, self.payload, "dxgi.dll", logger=LOG)
        self.assertTrue((self.game / installer.INSTALL_STAGING).is_dir())
        installed = {k: v for k, v in snapshot(self.game).items()
                     if not k.startswith(installer.INSTALL_STAGING)}
        self.assertFalse(installer.recover_interrupted(self.game, LOG))
        self.assertEqual(snapshot(self.game), installed)
        self.assertEqual((self.game / BACKUP_DIR / "libxess.dll").read_bytes(), b"the game's own XeSS")

    def test_reinstall_merges_into_payload_folders(self):
        # Upstream copies Licenses/ and D3D12_OptiScaler/ over what is there
        # (copytree dirs_exist_ok); a file added inside one survives a reinstall.
        installer.install(self.game, self.payload, "dxgi.dll", logger=LOG)
        (self.game / "D3D12_OptiScaler" / "extra.txt").write_bytes(b"added later")
        installer.install(self.game, self.payload, "dxgi.dll", logger=LOG)
        self.assertEqual((self.game / "D3D12_OptiScaler" / "extra.txt").read_bytes(), b"added later")

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


class ProxyNameTests(unittest.TestCase):
    """Every filename the plugin offers installs, gets the right launch option,
    and uninstalls back to the game's own file."""

    def test_every_proxy_name(self):
        for name in PROXY_FILENAMES:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                payload = make_payload(root)
                game = root / "game"
                game.mkdir()
                (game / "Game.exe").write_bytes(b"MZ")
                (game / name).write_bytes(b"the game's own " + name.encode())
                before = snapshot(game)
                result = installer.install(game, payload, name, logger=LOG)
                stem = name[:-4]
                expected = ("%command%" if name.endswith(".asi")
                            else f'WINEDLLOVERRIDES="{stem}=n,b" %command%')
                self.assertEqual(result["launch_option"], expected)
                self.assertEqual(installer.detect(game)["filename"], name)
                self.assertEqual((game / name).read_bytes(),
                                 (payload / "OptiScaler.dll").read_bytes())
                installer.uninstall(game, logger=LOG, payload_root=payload)
                self.assertEqual(snapshot(game), before)

    def test_an_unknown_name_is_refused_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = make_payload(root)
            game = root / "game"
            game.mkdir()
            before = snapshot(game)
            with self.assertRaises(ValueError):
                installer.install(game, payload, "d3d11.dll", logger=LOG)
            self.assertEqual(snapshot(game), before)


class InstallFolderTests(unittest.TestCase):
    """OptiScaler has to sit next to the executable that renders the game. A wrong
    pick installs fine and then does nothing, so the common layouts are pinned.
    Files are sparse: real sizes (the ranking weighs them), no disk used."""

    LAYOUTS = {
        "Unity, exe at root": ("Hollow Knight", {
            "Hollow Knight.exe": 650_000, "UnityCrashHandler64.exe": MB, "UnityPlayer.dll": 30 * MB,
            "Hollow Knight_Data/Managed/Assembly-CSharp.dll": 2 * MB}, "."),
        "Unity IL2CPP": ("Cult Of The Lamb", {
            "Cult Of The Lamb.exe": 650_000, "GameAssembly.dll": 90 * MB,
            "UnityCrashHandler64.exe": MB}, "."),
        "Unreal with a bootstrap exe at root": ("Stray", {
            "Stray.exe": 300_000, "Hk_project/Binaries/Win64/Stray-Win64-Shipping.exe": 120 * MB,
            "Engine/Binaries/Win64/CrashReportClient.exe": 20 * MB,
            "Engine/Extras/Redist/en-us/UEPrereqSetup_x64.exe": 40 * MB}, "Hk_project/Binaries/Win64"),
        "Unreal, project folder named unlike the exe": ("Hogwarts Legacy", {
            "Hogwarts Legacy.exe": 300_000, "Phoenix/Binaries/Win64/HogwartsLegacy.exe": 150 * MB,
            "Engine/Binaries/ThirdParty/x.exe": MB}, "Phoenix/Binaries/Win64"),
        "x64 and x86 builds side by side": ("Hades", {
            "x64/Hades.exe": 15 * MB, "x86/Hades.exe": 13 * MB}, "x64"),
        "launcher at root, game in a subfolder": ("Launcher", {
            "Launcher.exe": 5 * MB, "Game/bin/Game.exe": 200 * MB}, "Game/bin"),
        "RE Engine, one big exe at root": ("RESIDENT EVIL 4  BIOHAZARD RE4", {
            "re4.exe": 500 * MB, "dinput8.dll": MB}, "."),
        "redistributables beside the game": ("Game", {
            "Game.exe": 80 * MB, "_CommonRedist/vcredist/2019/VC_redist.x64.exe": 25 * MB,
            "_CommonRedist/DirectX/Jun2010/DXSETUP.exe": MB}, "."),
        "editor and benchmark beside the game": ("TheGame", {
            "bin/x64/TheGame.exe": 150 * MB, "bin/x64/TheGameBenchmark.exe": 150 * MB,
            "tools/Editor.exe": 300 * MB}, "bin/x64"),
        "game exe under a retail folder": ("Game", {
            "retail/Game.exe": 60 * MB, "Support/EA Help/Help.exe": 2 * MB}, "retail"),
    }

    def test_the_render_executable_folder_is_picked(self):
        for title, (folder, files, want) in self.LAYOUTS.items():
            with self.subTest(layout=title), tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / folder
                for rel, size in files.items():
                    path = root / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with open(path, "wb") as handle:
                        handle.truncate(size)
                candidates = steam.find_exe_dirs(root)
                self.assertTrue(candidates)
                self.assertEqual(candidates[0]["relative"], want,
                                 [(c["relative"], round(c["score"])) for c in candidates[:3]])


class PayloadArchiveTests(unittest.TestCase):
    """The bundled archive is checked before use and unpacks without 7-Zip."""

    archive = ROOT / "bin" / PAYLOAD_ARCHIVE

    def setUp(self):
        if not self.archive.is_file():
            self.skipTest("no bundled archive in bin/")

    def test_a_tampered_archive_is_refused_before_anything_is_unpacked(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = Path(temp) / PAYLOAD_ARCHIVE
            fake.write_bytes(b"not the pinned release")
            payload = payload_mod.Payload(fake, Path(temp) / "out")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                payload.ensure()
            self.assertFalse(payload.ready)

    def test_bsdtar_unpacks_it_when_7zip_is_missing(self):
        if not shutil.which("bsdtar"):
            self.skipTest("bsdtar not installed")
        real_which = shutil.which
        hide_7z = lambda cmd, *a, **k: None if cmd.startswith("7z") else real_which(cmd, *a, **k)
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(payload_mod.shutil, "which", side_effect=hide_7z):
            payload = payload_mod.Payload(self.archive, Path(temp) / "out")
            self.assertEqual(payload_mod.available_extractor(), "bsdtar")
            root = payload.ensure()
            self.assertTrue(payload.ready)
            self.assertTrue(all((root / name).is_file() for name in PAYLOAD_FILES))


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()
