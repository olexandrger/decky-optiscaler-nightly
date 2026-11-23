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
from optiscaler import installer, layout, payload as payload_mod, steam  # noqa: E402
from optiscaler.constants import (  # noqa: E402
    BACKUP_DIR, FFX_UPSCALER_DLL, INI_NAME, MANIFEST_NAME, OPTI_SUBDIR, PAYLOAD_ARCHIVE, PAYLOAD_DIRS,
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
    for name in PAYLOAD_DIRS:
        (payload / name).mkdir(parents=True, exist_ok=True)
    for name in PAYLOAD_FILES:
        (payload / name).parent.mkdir(parents=True, exist_ok=True)
        (payload / name).write_bytes(f"payload {name}".encode())
    return payload


def P(basename):
    """Where the payload, and an install, has OptiScaler's file of this name."""
    return layout.payload_name(basename)


# What installs from before OptiScaler/ put in the game folder itself (up to
# v0.0.7-nightly.20261122.38): OptiScaler's DLLs, D3D12_OptiScaler/ and
# Licenses/ copied whole, the ASIs in plugins/.
FLAT_FILES = sorted(Path(n).name for n in PAYLOAD_FILES if n.count("/") == 1 and n.endswith(".dll"))
FLAT_DIRS = ["D3D12_OptiScaler", "Licenses"]


def flat_install(game, version="flat", live_asi=b"MZ live, flat", extra_files=(), extra_dirs=()):
    """A game folder as this plugin's installs from before OptiScaler/ left it.

    The steps of that installer (_install up to 0156da5), in the same order and
    with the same records: the game's own file of each name set aside in the
    backup folder (an earlier install's own left alone), folders copied over
    what is there (the game's set aside first), the manifest as it wrote it.
    `extra_files` / `extra_dirs` are what a bundle listed besides (the .38
    bundle: the empty marker and plugins/).
    """
    game = Path(game)
    old = installer.read_manifest(game) or {}
    old_files, old_dirs = set(old.get("files") or []), set(old.get("dirs") or [])
    backups = dict(old.get("backups") or {})
    stash = game / BACKUP_DIR
    files, dirs = [], []

    def put(name, data):
        path = game / name
        if path.exists() and name not in backups and name not in old_files \
                and not installer._looks_like_optiscaler(path):
            stash.mkdir(exist_ok=True)
            shutil.move(str(path), str(stash / name))
            backups[name] = name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        files.append(name)

    put("dxgi.dll", b"OptiScaler, " + version.encode() + b"\0" * (5 * 1024 * 1024))
    if not (game / INI_NAME).is_file():
        put(INI_NAME, b"[Menu]\nShortcutKey=auto\n")
    else:
        files.append(INI_NAME)
    for name in [*FLAT_FILES, *extra_files]:
        put(name, b"" if name in extra_files else f"{version} {name}".encode())
    for name in [*FLAT_DIRS, *extra_dirs]:
        path = game / name
        if path.exists() and name not in backups and name not in old_dirs:
            stash.mkdir(exist_ok=True)
            shutil.copytree(path, stash / name)
            backups[name] = name
        path.mkdir(exist_ok=True)
        if name not in extra_dirs:
            (path / "inside.txt").write_bytes(f"{version} {name}".encode())
        dirs.append(name)
    if live_asi is not None:
        (game / "plugins").mkdir(exist_ok=True)
        (game / "plugins" / installer.live.ASI_NAME).write_bytes(live_asi)
        files.append(f"plugins/{installer.live.ASI_NAME}")
    (game / MANIFEST_NAME).write_text(json.dumps({
        "plugin": installer.PLUGIN_ID, "live_asi": live_asi is not None, "optipatcher": False,
        "reframework": False, "reframework_revision": None, "fsr4_build": {},
        "optiscaler_version": version, "filename": "dxgi.dll", "installed_at": 0,
        "files": files, "dirs": dirs, "backups": backups, "backup_dir": BACKUP_DIR,
    }, indent=2), encoding="utf-8")


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
        (self.payload / P("libxess.dll")).write_bytes(b"a newer XeSS")
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
        installer.uninstall(self.game, logger=LOG)
        self.assertEqual(snapshot(self.game), before)

    def test_failure_then_retry_then_uninstall_restores_the_game(self):
        before = snapshot(self.game)
        with FailingCopies(5), self.assertRaises(OSError):
            installer.install(self.game, self.payload, "dxgi.dll", logger=LOG)
        installer.install(self.game, self.payload, "dxgi.dll", logger=LOG)
        # Every file landed; the upscaler is only "wrong" because verify checks
        # it against the real bundled hash, which a synthetic payload lacks.
        problems = installer.verify_install(self.game, self.payload)["problems"]
        self.assertEqual(set(problems) - {P(FFX_UPSCALER_DLL)}, set())
        installer.uninstall(self.game, logger=LOG)
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
                installer.uninstall(self.game, logger=LOG)
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
        self.assertEqual((self.game / BACKUP_DIR / "dxgi.dll").read_bytes(), b"the game's own dxgi")

    def test_a_file_put_in_optiscalers_folders_survives_a_reinstall_and_an_uninstall(self):
        # The folders are OptiScaler's, not this plugin's alone: the player's
        # ASIs go in OptiScaler/plugins, and anything else put there stays.
        installer.install(self.game, self.payload, "dxgi.dll", logger=LOG)
        extra = self.game / OPTI_SUBDIR / "D3D12_OptiScaler" / "extra.txt"
        extra.write_bytes(b"added later")
        installer.install(self.game, self.payload, "dxgi.dll", logger=LOG)
        self.assertEqual(extra.read_bytes(), b"added later")
        installer.uninstall(self.game, logger=LOG)
        self.assertEqual(extra.read_bytes(), b"added later")
        self.assertEqual(sorted(p.relative_to(self.game).as_posix() for p in (self.game / OPTI_SUBDIR).rglob("*")),
                         ["OptiScaler/D3D12_OptiScaler", "OptiScaler/D3D12_OptiScaler/extra.txt"])

    def test_a_successful_install_leaves_no_staging_behind(self):
        installer.install(self.game, self.payload, "dxgi.dll", logger=LOG)
        self.assertFalse((self.game / installer.INSTALL_STAGING).exists())
        manifest = json.loads((self.game / MANIFEST_NAME).read_text())
        self.assertEqual(manifest["layout"], installer.LAYOUT)
        # OptiScaler's DLLs go in OptiScaler/: the game's own of the same
        # names, and its Licenses/, are not touched. Only the proxy displaces.
        self.assertEqual(sorted(manifest["backups"]), ["dxgi.dll"])
        self.assertEqual((self.game / "libxess.dll").read_bytes(), b"the game's own XeSS")
        self.assertEqual((self.game / "Licenses" / "game.txt").read_bytes(), b"the game's licence")
        self.assertEqual((self.game / P("libxess.dll")).read_bytes(), b"payload " + P("libxess.dll").encode())

    def test_a_folder_that_cannot_be_written_is_refused_untouched(self):
        before = snapshot(self.game)
        with mock.patch.object(installer.os, "access", return_value=False), \
                self.assertRaises(PermissionError):
            installer.install(self.game, self.payload, "dxgi.dll", logger=LOG)
        self.assertEqual(snapshot(self.game), before)



class ExistingIniTests(unittest.TestCase):
    """An OptiScaler.ini this plugin did not put there is the player's: an
    uninstall puts it back as it was (upstream 886d5f0, audit PD-01). Before,
    the install kept it in place but listed it as its own, and uninstalling
    deleted it."""

    ORIGINAL = "[Upscalers]\nDx12Upscaler=xess\n; the player's own\n"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.payload = make_payload(self.root)
        self.game = self.root / "game"
        self.game.mkdir()
        (self.game / "Game.exe").write_bytes(b"MZ game")
        (self.game / INI_NAME).write_text(self.ORIGINAL, encoding="utf-8")

    def ini(self):
        return (self.game / INI_NAME).read_text(encoding="utf-8")

    def test_the_install_keeps_it_and_uninstall_puts_it_back(self):
        before = snapshot(self.game)
        installer.install(self.game, self.payload, "dxgi.dll", logger=LOG)
        self.assertEqual(self.ini(), self.ORIGINAL)
        (self.game / INI_NAME).write_text("[Upscalers]\nDx12Upscaler=ffx\n", encoding="utf-8")
        installer.install(self.game, self.payload, "dxgi.dll", logger=LOG)  # an update
        installer.uninstall(self.game, logger=LOG)
        self.assertEqual(snapshot(self.game), before)

    def test_replaced_by_the_bundled_ini_it_still_comes_back(self):
        before = snapshot(self.game)
        installer.install(self.game, self.payload, "dxgi.dll", preserve_ini=False, logger=LOG)
        self.assertEqual(self.ini(), (self.payload / INI_NAME).read_text(encoding="utf-8"))
        installer.uninstall(self.game, logger=LOG)
        self.assertEqual(snapshot(self.game), before)

    def test_uninstall_keeping_the_ini_keeps_the_current_settings(self):
        installer.install(self.game, self.payload, "dxgi.dll", logger=LOG)
        (self.game / INI_NAME).write_text("[Upscalers]\nDx12Upscaler=ffx\n", encoding="utf-8")
        installer.uninstall(self.game, remove_ini=False, logger=LOG)
        self.assertEqual(self.ini(), "[Upscalers]\nDx12Upscaler=ffx\n")
        self.assertFalse((self.game / BACKUP_DIR).exists())

    def test_an_ini_this_plugin_installed_is_not_backed_up(self):
        (self.game / INI_NAME).unlink()
        installer.install(self.game, self.payload, "dxgi.dll", logger=LOG)
        installer.install(self.game, self.payload, "dxgi.dll", logger=LOG)
        self.assertNotIn(INI_NAME, installer.read_manifest(self.game)["backups"])
        installer.uninstall(self.game, logger=LOG)
        self.assertFalse((self.game / INI_NAME).exists())

    def test_a_failure_at_any_write_leaves_it_as_it_was(self):
        before = snapshot(self.game)
        clone = self.root / "count"
        shutil.copytree(self.game, clone)
        with FailingCopies(fail_at=10**9) as counter:
            installer.install(clone, self.payload, "dxgi.dll", logger=LOG)
        for fail_at in range(1, counter.count + 1):
            with self.subTest(fail_at=fail_at):
                with FailingCopies(fail_at), self.assertRaises(OSError):
                    installer.install(self.game, self.payload, "dxgi.dll", logger=LOG)
                self.assertEqual(snapshot(self.game), before)


class PreviousInstallTests(unittest.TestCase):
    """Going back to the OptiScaler a game had before its last update.

    Nightlies regress, one game at a time: the update keeps the previous
    OptiScaler's files it replaced (only those that changed) and "Go back"
    puts them back through the same journal, so it is as safe as an install.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.payload = make_payload(self.root)
        self.game = self.root / "game"
        self.game.mkdir()
        (self.game / "Game.exe").write_bytes(b"MZ game")
        (self.game / "dxgi.dll").write_bytes(b"the game's own dxgi")
        (self.game / "libxess.dll").write_bytes(b"the game's own XeSS")
        self.original = snapshot(self.game)

    def install(self, version):
        with mock.patch.object(installer, "OPTISCALER_VERSION", version):
            return installer.install(self.game, self.payload, "dxgi.dll", logger=LOG)

    def newer_nightly(self):
        (self.payload / "OptiScaler.dll").write_bytes(b"OptiScaler B" + b"\0" * (5 * 1024 * 1024))
        (self.payload / P("libxess.dll")).write_bytes(b"XeSS B")

    def updated(self):
        """Nightly A installed, then nightly B over it; returns the folder under A."""
        self.install("A")
        under_a = snapshot(self.game)
        self.newer_nightly()
        self.install("B")
        return under_a

    def without(self, folder_snapshot, *prefixes):
        return {k: v for k, v in folder_snapshot.items() if not k.startswith(prefixes)}

    def test_a_first_install_has_nothing_to_go_back_to(self):
        self.install("A")
        self.assertIsNone(installer.read_previous(self.game))
        self.assertIsNone(installer.detect(self.game)["previous"])
        with self.assertRaises(ValueError):
            installer.restore_previous(self.game, LOG)

    def test_an_update_keeps_only_the_files_it_changed(self):
        self.updated()
        previous = installer.read_previous(self.game)
        self.assertEqual((previous["version"], previous["replaced_by"]), ("A", "B"))
        self.assertEqual(previous["files"], sorted(["dxgi.dll", P("libxess.dll")]))
        kept = self.game / installer.PREVIOUS_DIR / "files"
        self.assertEqual(sorted(p.relative_to(kept).as_posix() for p in kept.rglob("*") if p.is_file()),
                         sorted(["dxgi.dll", P("libxess.dll")]))
        self.assertEqual((kept / P("libxess.dll")).read_bytes(), b"payload " + P("libxess.dll").encode())
        self.assertEqual(installer.detect(self.game)["previous"]["version"], "A")

    def test_going_back_restores_the_previous_install_byte_for_byte(self):
        under_a = self.updated()
        # Settings changed since the update are the user's: they stay.
        (self.game / INI_NAME).write_text("[Menu]\nShortcutKey=0x24\n", encoding="utf-8")
        result = installer.restore_previous(self.game, LOG)
        self.assertEqual(result["version"], "A")
        self.assertEqual(self.without(snapshot(self.game), INI_NAME), self.without(under_a, INI_NAME))
        self.assertEqual((self.game / INI_NAME).read_text(), "[Menu]\nShortcutKey=0x24\n")
        self.assertIsNone(installer.read_previous(self.game))
        self.assertEqual(installer.detect(self.game)["version"], "A")
        installer.uninstall(self.game, logger=LOG)
        self.assertEqual(snapshot(self.game), self.original)

    def test_a_failure_at_any_write_while_going_back_leaves_the_folder_as_it_was(self):
        self.updated()
        before = snapshot(self.game)
        clone = self.root / "count"
        shutil.copytree(self.game, clone)
        with FailingCopies(fail_at=10**9) as counter:
            installer.restore_previous(clone, LOG)
        shutil.rmtree(clone)
        self.assertGreater(counter.count, 1)
        for fail_at in range(1, counter.count + 1):
            with self.subTest(fail_at=fail_at):
                with FailingCopies(fail_at), self.assertRaises(OSError):
                    installer.restore_previous(self.game, LOG)
                self.assertEqual(snapshot(self.game), before)

    def test_an_update_that_changes_nothing_keeps_the_restore_point(self):
        self.updated()
        self.install("B")
        self.assertEqual(installer.read_previous(self.game)["version"], "A")

    def test_the_next_update_replaces_it(self):
        self.updated()
        (self.payload / P("libxess.dll")).write_bytes(b"XeSS C")
        self.install("C")
        previous = installer.read_previous(self.game)
        self.assertEqual((previous["version"], previous["replaced_by"], previous["files"]),
                         ("B", "C", [P("libxess.dll")]))

    def test_a_file_the_update_added_goes_and_the_game_file_it_displaced_comes_back(self):
        # A game file where OptiScaler's goes (a folder named OptiScaler/ the
        # game already had): set aside in its place under the backup folder.
        added = PAYLOAD_FILES[0]
        (self.game / added).parent.mkdir(parents=True)
        (self.game / added).write_bytes(b"the game's own copy")
        self.original = snapshot(self.game)
        shipped = (self.payload / added).read_bytes()
        (self.payload / added).unlink()
        under_a = self.install("A") and snapshot(self.game)
        (self.payload / added).write_bytes(shipped)
        self.install("B")
        self.assertEqual((self.game / added).read_bytes(), shipped)
        installer.restore_previous(self.game, LOG)
        self.assertEqual((self.game / added).read_bytes(), b"the game's own copy")
        self.assertEqual(snapshot(self.game), under_a)
        installer.uninstall(self.game, logger=LOG)
        self.assertEqual(snapshot(self.game), self.original)

    def test_a_file_the_update_retired_comes_back_and_the_game_file_goes_aside_again(self):
        retired = "nvngx-retired.dll"
        (self.game / retired).write_bytes(b"the game's own retired-name dll")
        self.original = snapshot(self.game)
        with mock.patch.object(installer, "PAYLOAD_FILES", [*PAYLOAD_FILES, retired]):
            (self.payload / retired).write_bytes(b"ours, from nightly A")
            under_a = self.install("A") and snapshot(self.game)
        (self.payload / retired).unlink()
        with mock.patch.object(installer, "RETIRED_PAYLOAD_FILES", [retired]):
            self.install("B")
        self.assertEqual((self.game / retired).read_bytes(), b"the game's own retired-name dll")
        installer.restore_previous(self.game, LOG)
        self.assertEqual(snapshot(self.game), under_a)
        installer.uninstall(self.game, logger=LOG)
        self.assertEqual(snapshot(self.game), self.original)

    def test_a_running_game_is_refused_untouched(self):
        self.updated()
        before = snapshot(self.game)
        with mock.patch.object(installer.live, "status", return_value={"attached": True}), \
                self.assertRaises(RuntimeError) as refused:
            installer.restore_previous(self.game, LOG)
        self.assertIn("Close the game", str(refused.exception))
        self.assertEqual(snapshot(self.game), before)

    def test_uninstall_takes_the_restore_point_too(self):
        self.updated()
        installer.uninstall(self.game, logger=LOG)
        self.assertEqual(snapshot(self.game), self.original)

    def test_a_restore_point_left_half_built_is_cleared(self):
        self.updated()
        (self.game / (installer.PREVIOUS_DIR + ".new")).mkdir()
        installer.recover_interrupted(self.game, LOG)
        self.assertFalse((self.game / (installer.PREVIOUS_DIR + ".new")).exists())
        self.assertEqual(installer.read_previous(self.game)["version"], "A")

    def test_it_is_named_as_this_fork_names_everything(self):
        self.assertIn("nightly", installer.PREVIOUS_DIR)


class PlaceholderBundleTests(unittest.TestCase):
    """The bundles of mid-November 2026 nightlies (up to v0.0.7-nightly.20261122.38).

    They listed the archive's empty "!! EXTRACT ALL FILES TO GAME FOLDER !!"
    marker as a payload file and its empty plugins/ folder as a payload folder.
    Installing one wrote the marker into the game and copied the game's
    plugins/ (this plugin's ASI and the player's own) into the backup folder as
    the game's; uninstall deleted plugins/, with anything added since, before
    moving that old copy back. Those installs were laid out before OptiScaler/
    (flat_install); the next install also moves them.
    """

    MARKER = "!! EXTRACT ALL FILES TO GAME FOLDER !!"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.payload = make_payload(self.root)
        self.asi = self.root / "live.asi"
        self.asi.write_bytes(b"MZ live")
        self.game = self.root / "game"
        self.game.mkdir()
        (self.game / "Game.exe").write_bytes(b"MZ game")
        (self.game / "plugins").mkdir()
        (self.game / "plugins" / "players_mod.asi").write_bytes(b"the player's own ASI")
        self.original = snapshot(self.game)

    def install(self, **kwargs):
        return installer.install(self.game, self.payload, "dxgi.dll", logger=LOG,
                                 live_asi=self.asi, **kwargs)

    def install_38(self):
        """An install as the .38 bundle made it, over an earlier install of ours."""
        flat_install(self.game, "A")
        flat_install(self.game, "B", extra_files=[self.MARKER], extra_dirs=["plugins"])

    def test_the_38_state_is_what_the_reports_describe(self):
        self.install_38()
        manifest = installer.read_manifest(self.game)
        self.assertIn(self.MARKER, manifest["files"])
        self.assertIn("plugins", manifest["dirs"])
        self.assertIn("plugins", manifest["backups"])
        # The install merged into plugins/, so the player's ASI still loads,
        # but a copy of the folder sits in the backup as if it were the game's.
        self.assertTrue((self.game / "plugins" / "players_mod.asi").exists())
        self.assertTrue((installer.backup_dir(self.game) / "plugins" / "players_mod.asi").exists())

    def test_the_next_install_puts_the_players_plugins_back_and_drops_the_marker(self):
        self.install_38()
        self.install()
        manifest = installer.read_manifest(self.game)
        self.assertEqual((self.game / "plugins" / "players_mod.asi").read_bytes(), b"the player's own ASI")
        self.assertTrue((self.game / OPTI_SUBDIR / "plugins" / "decky_optiscaler_live.asi").is_file())
        self.assertFalse((self.game / "plugins" / "decky_optiscaler_live.asi").exists())
        self.assertFalse((self.game / self.MARKER).exists())
        self.assertNotIn(self.MARKER, manifest["files"])
        self.assertNotIn("plugins", manifest["dirs"])
        self.assertNotIn("plugins", manifest["backups"])
        self.assertFalse((installer.backup_dir(self.game) / "plugins").exists())
        installer.uninstall(self.game, logger=LOG)
        self.assertEqual(snapshot(self.game), self.original)

    def test_a_failure_at_any_write_of_that_repair_leaves_the_folder_as_it_was(self):
        self.install_38()
        before = snapshot(self.game)
        clone = self.root / "count"
        shutil.copytree(self.game, clone)
        with FailingCopies(fail_at=10**9) as counter:
            installer.install(clone, self.payload, "dxgi.dll", logger=LOG, live_asi=self.asi)
        shutil.rmtree(clone)
        for fail_at in range(1, counter.count + 1):
            with self.subTest(fail_at=fail_at):
                with FailingCopies(fail_at), self.assertRaises(OSError):
                    self.install()
                self.assertEqual(snapshot(self.game), before)

    def test_uninstalling_a_38_install_keeps_what_the_player_added_since(self):
        self.install_38()
        (self.game / "plugins" / "added_later.asi").write_bytes(b"added while installed")
        installer.uninstall(self.game, logger=LOG)
        self.assertEqual((self.game / "plugins" / "added_later.asi").read_bytes(), b"added while installed")
        self.assertEqual((self.game / "plugins" / "players_mod.asi").read_bytes(), b"the player's own ASI")
        self.assertFalse((self.game / "plugins" / "decky_optiscaler_live.asi").exists())
        self.assertFalse((self.game / self.MARKER).exists())
        self.assertFalse(installer.backup_dir(self.game).exists())


class OptiScalerFolderTests(unittest.TestCase):
    """OptiScaler 10's own layout: its DLLs in OptiScaler/, the game's own kept.

    OptiScaler reads its DLLs from an OptiScaler/ folder next to its dll, and
    from the dll's folder when there is none (dllmain.cpp, MainDllPath); it
    loads ASIs from that folder's plugins/. Installs from before OptiScaler/
    (flat_install) put the DLLs in the game folder over the game's own; the
    next install moves them, puts the game's back, and can go back.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.payload = make_payload(self.root)
        self.asi = self.root / "live.asi"
        self.asi.write_bytes(b"MZ live")
        self.patcher = self.root / "OptiPatcher.asi"
        self.patcher.write_bytes(b"MZ patcher")
        self.game = self.root / "game"
        self.game.mkdir()
        (self.game / "Game.exe").write_bytes(b"MZ game")
        (self.game / "dxgi.dll").write_bytes(b"the game's own dxgi")
        (self.game / "libxess.dll").write_bytes(b"the game's own XeSS")
        (self.game / FFX_UPSCALER_DLL).write_bytes(b"the game's own FSR 3.1")
        (self.game / "Licenses").mkdir()
        (self.game / "Licenses" / "game.txt").write_bytes(b"the game's licence")
        self.original = snapshot(self.game)

    def install(self, **kwargs):
        return installer.install(self.game, self.payload, "dxgi.dll", logger=LOG, live_asi=self.asi, **kwargs)

    def assert_in_optiscalers_folder(self):
        for name in PAYLOAD_FILES:
            self.assertEqual((self.game / name).read_bytes(), f"payload {name}".encode(), name)
        self.assertEqual(installer.live.asi_path(self.game), self.game / OPTI_SUBDIR / "plugins" / installer.live.ASI_NAME)
        self.assertEqual(installer.live.asi_path(self.game).read_bytes(), b"MZ live")
        self.assertEqual(installer.ffx_upscaler_info(self.game)["present"], True)
        self.assertEqual(installer.layout.ffx_upscaler(self.game), self.game / P(FFX_UPSCALER_DLL))

    def test_an_install_leaves_the_games_own_dlls_alone(self):
        self.install()
        self.assert_in_optiscalers_folder()
        for name in ("libxess.dll", FFX_UPSCALER_DLL, "Licenses/game.txt"):
            self.assertEqual((self.game / name).read_bytes(), self.original[name], name)
        self.assertFalse((self.game / "plugins").exists())
        self.assertFalse(installer.detect(self.game)["flat_layout"])
        installer.uninstall(self.game, logger=LOG)
        self.assertEqual(snapshot(self.game), self.original)

    def test_an_install_from_before_the_folder_moves_into_it(self):
        flat_install(self.game, "A")
        self.assertTrue(installer.detect(self.game)["flat_layout"])
        self.assertEqual(installer.live.asi_path(self.game), self.game / "plugins" / installer.live.ASI_NAME)
        self.install()
        self.assert_in_optiscalers_folder()
        # The game's own files of OptiScaler's names come back from the backup.
        for name in ("libxess.dll", FFX_UPSCALER_DLL, "Licenses/game.txt"):
            self.assertEqual((self.game / name).read_bytes(), self.original[name], name)
        for name in [*FLAT_FILES]:
            self.assertEqual((self.game / name).exists(), name in ("libxess.dll", FFX_UPSCALER_DLL), name)
        self.assertFalse((self.game / "D3D12_OptiScaler").exists())
        self.assertEqual(sorted(p.name for p in (self.game / "Licenses").iterdir()), ["game.txt"])
        self.assertFalse((self.game / "plugins").exists())
        manifest = installer.read_manifest(self.game)
        self.assertEqual((manifest["layout"], sorted(manifest["backups"])), (installer.LAYOUT, ["dxgi.dll"]))
        self.assertFalse(installer.detect(self.game)["flat_layout"])
        self.assertEqual(installer.read_previous(self.game)["version"], "A")
        installer.uninstall(self.game, logger=LOG)
        self.assertEqual(snapshot(self.game), self.original)

    def test_a_failure_at_any_write_of_the_move_leaves_the_old_install_as_it_was(self):
        flat_install(self.game, "A")
        before = snapshot(self.game)
        clone = self.root / "count"
        shutil.copytree(self.game, clone)
        with FailingCopies(fail_at=10**9) as counter:
            installer.install(clone, self.payload, "dxgi.dll", logger=LOG, live_asi=self.asi)
        shutil.rmtree(clone)
        self.assertGreater(counter.count, len(PAYLOAD_FILES))
        for fail_at in range(1, counter.count + 1):
            with self.subTest(fail_at=fail_at):
                with FailingCopies(fail_at), self.assertRaises(OSError):
                    self.install()
                self.assertEqual(snapshot(self.game), before)

    def test_going_back_after_the_move_restores_the_old_install(self):
        flat_install(self.game, "A")
        under_a = snapshot(self.game)
        self.install()
        installer.restore_previous(self.game, LOG)
        self.assertEqual(snapshot(self.game), under_a)
        self.assertTrue(installer.detect(self.game)["flat_layout"])
        installer.uninstall(self.game, logger=LOG)
        self.assertEqual(snapshot(self.game), self.original)

    def test_a_failure_at_any_write_of_going_back_leaves_the_folder_as_it_was(self):
        flat_install(self.game, "A")
        self.install()
        before = snapshot(self.game)
        clone = self.root / "count"
        shutil.copytree(self.game, clone)
        with FailingCopies(fail_at=10**9) as counter:
            installer.restore_previous(clone, LOG)
        shutil.rmtree(clone)
        for fail_at in range(1, counter.count + 1):
            with self.subTest(fail_at=fail_at):
                with FailingCopies(fail_at), self.assertRaises(OSError):
                    installer.restore_previous(self.game, LOG)
                self.assertEqual(snapshot(self.game), before)

    def test_going_back_waits_while_the_folder_holds_the_players_files(self):
        # The OptiScaler to go back to reads its DLLs from the game folder; an
        # OptiScaler/ left behind would send it looking there instead.
        flat_install(self.game, "A")
        self.install()
        (self.game / OPTI_SUBDIR / "plugins" / "players.asi").write_bytes(b"the player's own ASI")
        before = snapshot(self.game)
        with self.assertRaises(ValueError) as refused:
            installer.restore_previous(self.game, LOG)
        self.assertIn("OptiScaler/plugins/players.asi", str(refused.exception))
        self.assertEqual(snapshot(self.game), before)

    def test_uninstall_keeps_the_players_asis_in_the_folder(self):
        self.install()
        (self.game / OPTI_SUBDIR / "plugins" / "players.asi").write_bytes(b"the player's own ASI")
        installer.uninstall(self.game, logger=LOG)
        self.assertEqual(sorted(p.relative_to(self.game).as_posix() for p in (self.game / OPTI_SUBDIR).rglob("*")),
                         ["OptiScaler/plugins", "OptiScaler/plugins/players.asi"])
        rest = {k: v for k, v in snapshot(self.game).items() if not k.startswith(OPTI_SUBDIR)}
        self.assertEqual(rest, self.original)

    def test_the_players_asis_in_plugins_stay_where_they_are_and_are_reported(self):
        (self.game / "plugins").mkdir()
        (self.game / "plugins" / "players_mod.asi").write_bytes(b"the player's own ASI")
        self.original = snapshot(self.game)
        flat_install(self.game, "A")
        self.install()
        self.assertEqual(sorted(p.name for p in (self.game / "plugins").iterdir()), ["players_mod.asi"])
        facts = {label: (value, tone) for label, value, tone in installer.live.folder_facts(self.game)}
        self.assertIn("players_mod.asi", facts["plugins/ in the game folder"][0])
        self.assertEqual(facts["OptiScaler's files"], ("OptiScaler/", "ok"))
        installer.uninstall(self.game, logger=LOG)
        self.assertEqual(snapshot(self.game), self.original)

    def test_optipatcher_moves_too_also_when_its_toggle_put_it_there(self):
        flat_install(self.game, "A")
        (self.game / "plugins" / "OptiPatcher.asi").write_bytes(b"MZ patcher, flat")
        from optiscaler.service import OptiScalerService
        service = OptiScalerService(ROOT, self.root / "settings", self.root / "runtime", self.root / "home", LOG)
        service.install = mock.AsyncMock(return_value={"ok": True})
        import asyncio
        asyncio.run(service.update_install(str(self.game)))
        self.assertTrue(service.install.call_args.kwargs["optipatcher"])
        self.install(optipatcher=self.patcher)
        self.assertFalse((self.game / "plugins").exists())
        self.assertEqual((self.game / OPTI_SUBDIR / "plugins" / "OptiPatcher.asi").read_bytes(), b"MZ patcher")

    def test_a_game_file_where_optiscalers_goes_is_set_aside_and_comes_back(self):
        (self.game / OPTI_SUBDIR).mkdir()
        (self.game / P("libxess.dll")).write_bytes(b"left in OptiScaler/ by something else")
        self.original = snapshot(self.game)
        clone = self.root / "count"
        shutil.copytree(self.game, clone)
        with FailingCopies(fail_at=10**9) as counter:
            installer.install(clone, self.payload, "dxgi.dll", logger=LOG, live_asi=self.asi)
        shutil.rmtree(clone)
        for fail_at in range(1, counter.count + 1):
            with self.subTest(fail_at=fail_at):
                with FailingCopies(fail_at), self.assertRaises(OSError):
                    self.install()
                self.assertEqual(snapshot(self.game), self.original)
        self.install()
        self.assertEqual((installer.backup_dir(self.game) / P("libxess.dll")).read_bytes(),
                         b"left in OptiScaler/ by something else")
        self.assert_in_optiscalers_folder()
        installer.uninstall(self.game, logger=LOG)
        self.assertEqual(snapshot(self.game), self.original)

    def test_uninstall_puts_a_game_file_back_into_the_folder_past_the_players_asis(self):
        (self.game / OPTI_SUBDIR).mkdir()
        (self.game / P("libxess.dll")).write_bytes(b"left in OptiScaler/ by something else")
        self.install()
        (self.game / OPTI_SUBDIR / "plugins" / "players.asi").write_bytes(b"the player's own ASI")
        installer.uninstall(self.game, logger=LOG)
        self.assertEqual((self.game / P("libxess.dll")).read_bytes(), b"left in OptiScaler/ by something else")
        self.assertEqual((self.game / OPTI_SUBDIR / "plugins" / "players.asi").read_bytes(), b"the player's own ASI")

    def test_a_folder_an_earlier_bundle_had_goes_once_empty(self):
        extra = f"{OPTI_SUBDIR}/streamline"
        with mock.patch.object(installer, "PAYLOAD_DIRS", [*PAYLOAD_DIRS, extra]):
            self.install()
        self.assertTrue((self.game / extra).is_dir())
        self.install()
        self.assertFalse((self.game / extra).exists())
        self.assertNotIn(extra, installer.read_manifest(self.game)["containers"])
        installer.uninstall(self.game, logger=LOG)
        self.assertEqual(snapshot(self.game), self.original)

    def test_an_old_install_still_uninstalls_as_it_was_made(self):
        flat_install(self.game, "A")
        installer.uninstall(self.game, logger=LOG)
        self.assertEqual(snapshot(self.game), self.original)


class ExclusiveWithOtherInstallsTests(unittest.TestCase):
    """Decky OptiScaler Nightly and upstream's Decky OptiScaler never share a game.

    OptiScaler that this plugin did not install (upstream's plugin, another
    tool, by hand) is refused by every operation that would write to the
    folder, so the other tool's record of what it changed stays true.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.payload = make_payload(self.root)
        self.game = self.root / "game"
        self.game.mkdir()
        (self.game / "Game.exe").write_bytes(b"MZ game")
        (self.game / "libxess.dll").write_bytes(b"the game's own XeSS")

    def install_by_upstream(self):
        """What upstream's plugin leaves: its proxy, its ini, its manifest."""
        shutil.copy2(self.payload / "OptiScaler.dll", self.game / "dxgi.dll")
        (self.game / INI_NAME).write_text("[Menu]\nShortcutKey=auto\n")
        (self.game / ".decky-optiscaler.json").write_text(json.dumps(
            {"plugin": "decky-optiscaler", "filename": "dxgi.dll",
             "files": ["dxgi.dll", INI_NAME], "backups": {}}))

    def assert_refused_untouched(self, owner):
        before = snapshot(self.game)
        info = installer.detect(self.game)
        self.assertTrue(info["foreign"])
        self.assertEqual(info["owner"], owner)
        self.assertFalse(info["managed"])
        for action in (lambda: installer.install(self.game, self.payload, "dxgi.dll", logger=LOG),
                       lambda: installer.install(self.game, self.payload, "winmm.dll", logger=LOG),
                       lambda: installer.uninstall(self.game, logger=LOG)):
            with self.assertRaises(installer.ForeignInstallError) as raised:
                action()
            self.assertIn(owner or "another tool", str(raised.exception))
            self.assertEqual(snapshot(self.game), before)

    def test_upstreams_install_is_left_to_upstream(self):
        self.install_by_upstream()
        self.assert_refused_untouched("Decky OptiScaler")

    def test_an_install_by_another_tool_or_by_hand_is_left_alone(self):
        shutil.copy2(self.payload / "OptiScaler.dll", self.game / "winmm.dll")
        self.assert_refused_untouched(None)

    def test_upstreams_record_alone_still_marks_the_folder_as_its(self):
        (self.game / ".decky-optiscaler.json").write_text('{"plugin": "decky-optiscaler"}')
        self.assert_refused_untouched("Decky OptiScaler")

    def test_settings_writes_are_refused_too(self):
        import asyncio
        from optiscaler.service import OptiScalerService
        self.install_by_upstream()
        before = snapshot(self.game)
        service = OptiScalerService(ROOT, self.root / "s", self.root / "r", self.root / "h", LOG)
        result = asyncio.run(service.write_config(
            str(self.game), [{"section": "Menu", "key": "ShortcutKey", "value": "0x24"}]))
        self.assertFalse(result["ok"])
        self.assertEqual(result["owner"], "Decky OptiScaler")
        self.assertEqual(snapshot(self.game), before)

    def test_once_upstream_removed_it_this_plugin_installs(self):
        self.install_by_upstream()
        for name in ("dxgi.dll", INI_NAME, ".decky-optiscaler.json"):
            (self.game / name).unlink()
        installer.install(self.game, self.payload, "dxgi.dll", logger=LOG)
        manifest = json.loads((self.game / MANIFEST_NAME).read_text())
        self.assertEqual(manifest["plugin"], "decky-optiscaler-nightly")
        self.assertEqual(MANIFEST_NAME, ".decky-optiscaler-nightly.json")
        self.assertFalse(installer.detect(self.game)["foreign"])

    def test_the_two_plugins_keep_separate_records(self):
        (self.game / "dxgi.dll").write_bytes(b"the game's own dxgi")
        installer.install(self.game, self.payload, "dxgi.dll", logger=LOG)
        names = {p.name for p in self.game.iterdir()}
        self.assertNotIn(".decky-optiscaler.json", names)
        self.assertNotIn("decky_optiscaler_backup_files", names)
        self.assertIn("decky_optiscaler_nightly_backup_files", names)


class GameFolderWritesAreSerializedTests(unittest.TestCase):
    """Every service call that writes a game folder waits for the others.

    Upstream's audit (886d5f0, CD-08) found that importing FSR 4 files,
    adding or removing REFramework or OptiPatcher and adding live control
    wrote the folder outside the lock install, uninstall and settings writes
    hold. One of them reading the manifest while an uninstall removed it
    could write the manifest back, reviving an install that is gone.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.payload = make_payload(self.root)
        self.game = self.root / "game"
        self.game.mkdir()
        (self.game / "Game.exe").write_bytes(b"MZ game")
        installer.install(self.game, self.payload, "dxgi.dll", logger=LOG)
        self.asi = self.root / "plugin.asi"
        self.asi.write_bytes(b"asi")

    def calls(self, service):
        """(name, coroutine factory, the function that writes the folder)."""
        from optiscaler import live
        game = str(self.game)
        plan = {"plan": {"reframework": {"required": True, "automatic": True}}}
        service.get_auto_plan = mock.AsyncMock(return_value=plan)
        service.live_asi_path = lambda: str(self.asi)
        service.optipatcher_path = lambda: str(self.asi)
        return [
            ("import_fsr4_files", lambda: service.import_fsr4_files(game, str(self.root)),
             (installer, "import_fsr4_files", {"files": []})),
            ("install_reframework", lambda: service.install_reframework(game),
             (installer, "add_files", {"ok": False, "error": "stub"})),
            ("remove_reframework", lambda: service.remove_reframework(game),
             (installer, "remove_files", {"removed": []})),
            ("install_optipatcher", lambda: service.install_optipatcher(game, True),
             (live, "install_plugin_asi", {"ok": False, "error": "stub"})),
            ("install_live", lambda: service.install_live(game),
             (live, "install_asi", {"ok": False, "error": "stub"})),
        ]

    def test_each_write_waits_for_a_running_uninstall(self):
        import asyncio
        import threading
        from optiscaler import reframework
        from optiscaler.service import OptiScalerService

        def fresh():
            # One per event loop: an asyncio.Lock belongs to the loop that used it.
            return OptiScalerService(ROOT, self.root / "s", self.root / "r", self.root / "h", LOG)

        for index, (name, _, (module, attr, answer)) in enumerate(self.calls(fresh())):
            service = fresh()
            call = self.calls(service)[index][1]
            with self.subTest(name):
                entered, release = threading.Event(), threading.Event()
                order = []

                def slow_uninstall(*args, **kwargs):
                    entered.set()
                    release.wait(5)
                    order.append("uninstall")
                    return {"removed": []}

                def write(*args, **kwargs):
                    order.append(name)
                    return answer

                async def race():
                    uninstall = asyncio.ensure_future(service.uninstall(str(self.game)))
                    while not entered.is_set():
                        await asyncio.sleep(0.01)
                    other = asyncio.ensure_future(call())
                    await asyncio.sleep(0.3)
                    release.set()
                    await uninstall
                    return await other

                with mock.patch.object(installer, "uninstall", slow_uninstall), \
                        mock.patch.object(module, attr, write), \
                        mock.patch.object(reframework, "fetch", lambda *a, **k: []), \
                        mock.patch.object(reframework, "revision_of", lambda *a, **k: None):
                    outcome = asyncio.run(race())
                self.assertEqual(order, ["uninstall", name], outcome)


class ReinstallKeepsWhatItOwnedTests(unittest.TestCase):
    """What this plugin added to an install stays its own across a reinstall.

    Upstream's audit (886d5f0, CD-02) found that a reinstall rebuilt the
    manifest from that install's files only, and that OptiPatcher added from
    its toggle was never recorded: OptiPatcher, REFramework and imported FSR 4
    files stayed on disk, nobody's, and uninstall left them in the game.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.payload = make_payload(self.root)
        self.asi = self.root / "live.asi"
        self.asi.write_bytes(b"MZ live")
        self.patcher = self.root / "OptiPatcher.asi"
        self.patcher.write_bytes(b"MZ patcher")
        self.game = self.root / "game"
        self.game.mkdir()
        (self.game / "Game.exe").write_bytes(b"MZ game")
        (self.game / "dinput8.dll").write_bytes(b"the game's own dinput8")
        self.original = snapshot(self.game)

    def install(self, **kwargs):
        return installer.install(self.game, self.payload, "dxgi.dll", logger=LOG, live_asi=self.asi, **kwargs)

    def service(self):
        from optiscaler.service import OptiScalerService
        service = OptiScalerService(ROOT, self.root / "s", self.root / "r", self.root / "h", LOG)
        service.optipatcher_path = lambda: str(self.patcher)
        return service

    def test_optipatcher_from_the_install_is_still_removed_after_a_reinstall_without_it(self):
        self.install(optipatcher=self.patcher)
        self.install()
        self.assertTrue(installer.read_manifest(self.game)["optipatcher"])
        installer.uninstall(self.game, logger=LOG)
        self.assertEqual(snapshot(self.game), self.original)

    def test_optipatcher_from_its_toggle_is_removed_by_uninstall(self):
        import asyncio
        self.install()
        service = self.service()
        self.assertTrue(asyncio.run(service.install_optipatcher(str(self.game), True))["ok"])
        self.assertTrue(installer.read_manifest(self.game)["optipatcher"])
        self.install()
        self.assertTrue((installer.live.plugin_dir(self.game) / "OptiPatcher.asi").is_file())
        installer.uninstall(self.game, logger=LOG)
        self.assertEqual(snapshot(self.game), self.original)

    def test_optipatcher_taken_out_by_its_toggle_is_no_longer_recorded(self):
        import asyncio
        self.install(optipatcher=self.patcher)
        self.assertTrue(asyncio.run(self.service().install_optipatcher(str(self.game), False))["ok"])
        manifest = installer.read_manifest(self.game)
        self.assertFalse(manifest["optipatcher"])
        self.assertFalse([n for n in manifest["files"] if n.endswith("OptiPatcher.asi")])
        installer.uninstall(self.game, logger=LOG)
        self.assertEqual(snapshot(self.game), self.original)

    def test_files_added_later_stay_owned_and_their_originals_come_back(self):
        self.install()
        reframework = self.root / "ref" / "dinput8.dll"
        reframework.parent.mkdir()
        reframework.write_bytes(b"REFramework")
        installer.add_files(self.game, [reframework], LOG, reframework_revision="r1")
        fsr4 = self.root / "fsr4"
        fsr4.mkdir()
        (fsr4 / "amdxcffx64.dll").write_bytes(b"imported FSR 4 support")
        installer.import_fsr4_files(self.game, fsr4, LOG)
        self.install()
        manifest = installer.read_manifest(self.game)
        self.assertIn("dinput8.dll", manifest["files"])
        self.assertIn("amdxcffx64.dll", manifest["files"])
        self.assertTrue(manifest["reframework"])
        self.assertEqual(manifest["reframework_revision"], "r1")
        installer.uninstall(self.game, logger=LOG)
        self.assertEqual(snapshot(self.game), self.original)

    def test_a_file_the_player_removed_is_not_carried(self):
        self.install(optipatcher=self.patcher)
        (installer.live.plugin_dir(self.game) / "OptiPatcher.asi").unlink()
        self.install()
        manifest = installer.read_manifest(self.game)
        self.assertFalse(manifest["optipatcher"])
        self.assertFalse([n for n in manifest["files"] if n.endswith("OptiPatcher.asi")])


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
                installer.uninstall(game, logger=LOG)
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


class PayloadCompletenessTests(unittest.TestCase):
    """A payload folder counts as extracted only once a whole extraction
    finished (upstream 886d5f0, audit CD-05): one cut short keeps the dll but
    not the support files, and installs from it skipped them silently."""

    def _fill(self, root, skip=()):
        root.mkdir(parents=True, exist_ok=True)
        for name in payload_mod.PAYLOAD_DIRS:
            if name not in skip:
                (root / name).mkdir(parents=True, exist_ok=True)
        for name in [payload_mod.MARKER, INI_NAME, *PAYLOAD_FILES]:
            if name not in skip:
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_bytes(b"x")

    def _payload(self, temp):
        return payload_mod.Payload(Path(temp) / PAYLOAD_ARCHIVE, Path(temp) / "out")

    def test_a_folder_left_by_an_unfinished_extraction_is_not_ready(self):
        with tempfile.TemporaryDirectory() as temp:
            payload = self._payload(temp)
            self._fill(payload.root)  # every file, but no finished stamp
            self.assertFalse(payload.ready)
            (payload.root / payload_mod.STAMP).write_text(payload_mod.PAYLOAD_SHA256)
            self.assertTrue(payload.ready)
            (payload.root / PAYLOAD_FILES[0]).unlink()
            self.assertFalse(payload.ready)

    def test_a_stamp_from_another_archive_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as temp:
            payload = self._payload(temp)
            self._fill(payload.root)
            (payload.root / payload_mod.STAMP).write_text("0" * 64)
            self.assertFalse(payload.ready)

    def test_ensure_unpacks_again_over_a_partial_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            payload = self._payload(temp)
            self._fill(payload.root, skip=PAYLOAD_FILES[1:])  # dll, ini, one dll
            calls = []

            def extract(argv, logger=None):
                calls.append(argv)
                self._fill(payload.root)
                return True, ""

            with mock.patch.object(payload, "verify_archive"), \
                    mock.patch.object(payload_mod, "_run", side_effect=extract), \
                    mock.patch.object(payload_mod.shutil, "which", return_value="/usr/bin/7z"):
                root = payload.ensure()
            self.assertEqual(len(calls), 1)
            self.assertTrue(payload.ready)
            self.assertTrue(all((root / name).is_file() for name in PAYLOAD_FILES))

    def test_an_extraction_that_comes_out_short_is_an_error(self):
        with tempfile.TemporaryDirectory() as temp:
            payload = self._payload(temp)

            def extract(argv, logger=None):
                self._fill(payload.root, skip=(PAYLOAD_FILES[-1],))
                return True, ""

            with mock.patch.object(payload, "verify_archive"), \
                    mock.patch.object(payload_mod, "_run", side_effect=extract), \
                    mock.patch.object(payload_mod.shutil, "which", return_value="/usr/bin/7z"):
                with self.assertRaisesRegex(RuntimeError, "payload incomplete"):
                    payload.ensure()
            self.assertFalse(payload.ready)


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
