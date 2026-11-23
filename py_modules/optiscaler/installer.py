"""Install, detect and remove OptiScaler inside a game directory.

Mirrors what the official setup_linux.sh does — copy the release next to the
game executable, its DLLs in OptiScaler 10's own ``OptiScaler/`` folder, and
rename OptiScaler.dll to a proxy filename the game already loads — but records
a manifest and stashes every displaced file in a visible backup folder, so an
uninstall can put the game back exactly as it was. Installs from before the
``OptiScaler/`` folder (DLLs in the game folder) move into it on update.
"""

import json
import os
import shutil
import time
import tempfile
from pathlib import Path

from . import fsr4build, layout, live, schema
from .inifile import IniFile
from .constants import (
    OPTIPATCHER_NAME,
    BACKUP_DIR,
    FFX_UPSCALER_DLL,
    FSR4_DECK_MODELS,
    FSR4_MIN_SDK_VERSION,
    DEFAULT_PROXY,
    FSR4_SOURCE_HINTS,
    FSR4_SUPPORT_FILES,
    INI_NAME,
    LAUNCH_RECORD_NAME,
    LOG_NAME,
    MANIFEST_NAME,
    FOREIGN_MANIFESTS,
    PLUGIN_ID,
    PLUGIN_NAME,
    OPTISCALER_VERSION,
    OPTI_SUBDIR,
    PAYLOAD_DIRS,
    PAYLOAD_FILES,
    PROXY_FILENAMES,
    REFRAMEWORK_DLL,
    RETIRED_PAYLOAD_DIRS,
    RETIRED_PAYLOAD_FILES,
    RUNTIME_ARTIFACTS,
)

# Suffix used by v0.1 of this plugin, still restored on uninstall.
LEGACY_BACKUP_SUFFIX = ".decky-optiscaler.bak"
DLL_MARKER = b"OptiScaler"
MIN_OPTISCALER_SIZE = 4 * 1024 * 1024
#: The manifest's "layout" for an install as OptiScaler 10 ships it, its DLLs
#: in OptiScaler/. Manifests without it are from installs that put them in the
#: game folder; the next install moves them.
LAYOUT = "optiscaler-folder"


def _looks_like_optiscaler(path):
    """True if a proxy-named file is really an OptiScaler build."""
    try:
        if path.stat().st_size < MIN_OPTISCALER_SIZE:
            return False
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(1 << 20)
                if not chunk:
                    return False
                if DLL_MARKER in chunk:
                    return True
    except OSError:
        return False


def read_manifest(target_dir):
    path = Path(target_dir) / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def backup_dir(target_dir):
    return Path(target_dir) / BACKUP_DIR


def launch_record_path(target_dir):
    return Path(target_dir) / LAUNCH_RECORD_NAME


def read_launch_record(target_dir):
    """The Steam launch options this game had before OptiScaler went in.

    ``recorded`` is what separates "there was nothing there" from "nobody
    looked": an empty string is a real answer and the reason uninstalling can
    offer to clear the launch options outright, while a missing file means the
    install predates this record or Steam would not say, and only the override
    this plugin added can safely be taken back out.
    """
    path = launch_record_path(target_dir)
    if not path.is_file():
        return {"recorded": False, "value": ""}
    try:
        return {"recorded": True, "value": path.read_text(encoding="utf-8")}
    except OSError:
        return {"recorded": False, "value": ""}


def record_launch_options(target_dir, options, logger=None):
    """Keep the pre-install launch options, once.

    Write-once for the same reason ``_stash`` keeps the oldest backup: the value
    worth putting back is the one from before this plugin first touched the
    game, and a reinstall — or turning the launch-options step off and on again
    — must not overwrite it with the override we ourselves wrote.
    """
    path = launch_record_path(target_dir)
    if path.exists():
        return {"recorded": True, "value": read_launch_record(target_dir)["value"],
                "written": False}
    path.write_text(options or "", encoding="utf-8")
    if logger:
        logger.info("recorded previous launch options for %s", target_dir)
    return {"recorded": True, "value": options or "", "written": True}


def forget_launch_options(target_dir):
    """Drop the record. Called by uninstall: it belongs to one install."""
    path = launch_record_path(target_dir)
    existed = path.is_file()
    path.unlink(missing_ok=True)
    return existed


def detect(target_dir):
    """Report whether (and how) OptiScaler is installed in a directory."""
    target = Path(target_dir)
    result = {
        "path": str(target),
        "installed": False,
        "filename": None,
        "managed": False,
        # OptiScaler that this plugin did not install: another plugin's (named
        # in "owner") or put there by hand / another tool (owner None).
        "foreign": False,
        "owner": None,
        "version": None,
        # The OptiScaler this plugin build ships, and whether this game holds
        # another one: an install made by an earlier build of the plugin keeps
        # its OptiScaler (and live-control ASI, compiled for it) until reapplied.
        "bundled_version": OPTISCALER_VERSION,
        "outdated": False,
        # An install of this plugin's from before OptiScaler 10's OptiScaler/
        # folder: its DLLs sit in the game folder, over the game's own.
        "flat_layout": False,
        "installed_at": None,
        "ini_present": False,
        "ini_path": None,
        "log_present": False,
        "log_path": None,
        "candidates": [],
        "extra_proxies": [],
        "backup_dir": str(backup_dir(target)),
        "backed_up": [],
        "launch_record": {"recorded": False, "value": ""},
        "fsr4": {},
        # REFramework, for the RE Engine games that need it. Reported from the
        # folder rather than from the manifest so a copy the user put there
        # themselves counts — the step is "is REF present", not "did we install
        # REF", and re-installing over somebody's own working REF would be a
        # worse answer than leaving it alone.
        "reframework": {"installed": False, "revision": None, "managed": False},
        # The OptiScaler this game had before its last update, to go back to.
        "previous": None,
    }
    if not target.is_dir():
        return result

    result["launch_record"] = read_launch_record(target)

    ini = target / INI_NAME
    result["ini_present"] = ini.is_file()
    result["ini_path"] = str(ini)
    log = target / LOG_NAME
    result["log_present"] = log.is_file()
    result["log_path"] = str(log)
    result["fsr4"] = fsr4_status(target)

    result["reframework"] = {
        "installed": (target / REFRAMEWORK_DLL).is_file(),
        # Which build went in is only knowable from what we recorded: the
        # stamp file is deliberately not installed next to the game.
        "revision": None,
        "managed": False,
    }

    stash = backup_dir(target)
    if stash.is_dir():
        result["backed_up"] = sorted(p.name for p in stash.iterdir())

    present = [name for name in PROXY_FILENAMES if (target / name).is_file()]
    result["candidates"] = present

    manifest = read_manifest(target)
    if manifest and manifest.get("filename"):
        name = manifest["filename"]
        if (target / name).is_file():
            result.update(
                installed=True,
                filename=name,
                managed=True,
                version=manifest.get("optiscaler_version"),
                outdated=manifest.get("optiscaler_version") != OPTISCALER_VERSION,
                flat_layout=manifest.get("layout") != LAYOUT,
                installed_at=manifest.get("installed_at"),
            )
            result["reframework"]["managed"] = bool(manifest.get("reframework"))
            result["reframework"]["revision"] = manifest.get("reframework_revision")
            result["extra_proxies"] = [p for p in present if p != name
                                       and _looks_like_optiscaler(target / p)]
            result["previous"] = read_previous(target)
            return result

    # Installed by another plugin, by hand or by another tool.
    owner = next((who for name, who in FOREIGN_MANIFESTS.items()
                  if (target / name).is_file()), None)
    for name in present:
        if _looks_like_optiscaler(target / name):
            result.update(installed=True, filename=name, managed=False,
                          foreign=True, owner=owner)
            result["extra_proxies"] = [
                p for p in present if p != name and _looks_like_optiscaler(target / p)
            ]
            break
    else:
        if owner:
            # The other plugin's record without its proxy: still its folder.
            result.update(foreign=True, owner=owner)
    return result


class ForeignInstallError(RuntimeError):
    """OptiScaler in this folder belongs to something other than this plugin."""


def foreign_install_message(info):
    owner = info.get("owner")
    if owner:
        return (f"OptiScaler in this folder was installed by {owner}. {PLUGIN_NAME} does not "
                f"share games with it: remove OptiScaler with {owner} first.")
    return ("OptiScaler is already in this folder, installed by another tool or by hand. "
            f"{PLUGIN_NAME} only manages its own installs: remove that one first.")


def require_not_foreign(target_dir):
    """Refuse to touch a folder whose OptiScaler is not this plugin's."""
    info = detect(target_dir)
    if info.get("foreign"):
        raise ForeignInstallError(foreign_install_message(info))
    return info


def ffx_upscaler_info(target_dir):
    """The FidelityFX upscaler library OptiScaler loads (its own, in OptiScaler/).

    This is the version OptiScaler's overlay prints in its FFX Settings box, and
    it is what decides whether FSR 4 is even on offer - so it belongs in front
    of the user rather than behind a verify step. A copy the game ships in its
    own folder is the game's; OptiScaler only hooks it for its inputs.
    """
    dll = layout.ffx_upscaler(target_dir)
    version = pe_file_version(dll) if dll.is_file() else None
    return {
        "present": dll.is_file(),
        "name": FFX_UPSCALER_DLL,
        "version": ".".join(str(n) for n in version) if version else None,
        # The tuple as well, because callers compare it against the floor FSR 4
        # starts at and against the 4.1.1 the INT8 override needs — and parsing
        # the string back into numbers in each of them is a chance for them to
        # disagree.
        "version_tuple": list(version) if version else None,
        # OptiScaler treats an SDK at or above 4.0.2 as providing FSR 4.
        "fsr4_capable": bool(version and version[:3] >= FSR4_MIN_SDK_VERSION[:3]),
    }


def fsr4_status(target_dir):
    """Which FSR4 support DLLs are present next to the game executable."""
    target = Path(target_dir)
    present = {name: (target / name).is_file() for name in FSR4_SUPPORT_FILES}
    ffx = ffx_upscaler_info(target_dir)
    return {
        "files": present,
        "ready": present.get("amdxcffx64.dll", False),
        "required": FSR4_SUPPORT_FILES,
        "ffx": ffx,
        # Which upscaler build this actually is. Read out of the bytes rather
        # than the manifest, so a game that had a build dropped into it by hand
        # — or by another tool — is reported the same way one installed from
        # here is, and reported as *unknown* rather than as the released one when
        # the hash matches nothing (see fsr4build.identify).
        "build": fsr4build.identify(layout.ffx_upscaler(target)),
    }


def find_fsr4_sources(home):
    """Folders on this machine that already hold FSR4 support DLLs."""
    found = []
    for hint in FSR4_SOURCE_HINTS:
        candidate = Path(home) / hint
        if not candidate.is_dir():
            continue
        names = [n for n in FSR4_SUPPORT_FILES if (candidate / n).is_file()]
        if names:
            found.append({"path": str(candidate), "files": names})
    return found


def import_fsr4_files(target_dir, source_dir, logger=None):
    """Copy user-supplied FSR4 DLLs in and register them for later removal."""
    target = Path(target_dir)
    source = Path(source_dir)
    if not target.is_dir():
        raise NotADirectoryError(f"install target does not exist: {target}")
    if not source.is_dir():
        raise NotADirectoryError(f"source folder does not exist: {source}")

    names = [n for n in FSR4_SUPPORT_FILES if (source / n).is_file()]
    if not names:
        raise FileNotFoundError(
            f"no FSR4 support files ({', '.join(FSR4_SUPPORT_FILES)}) in {source}"
        )

    manifest = read_manifest(target) or {}
    backups = dict(manifest.get("backups") or {})
    files = list(manifest.get("files") or [])

    for name in names:
        destination = target / name
        if destination.exists() and name not in backups and name not in files:
            backups[name] = _stash(target, destination, logger)
        shutil.copy2(source / name, destination)
        if name not in files:
            files.append(name)
        if logger:
            logger.info("installed FSR4 support file %s", name)

    if manifest:
        manifest["files"] = files
        manifest["backups"] = backups
        (target / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return {"path": str(target), "imported": names, "source": str(source)}


def require_stopped_install(target_dir, action="changing the FSR 4 build"):
    """Require a managed install and reject DLL replacement while it is in use."""
    target = Path(target_dir).resolve()
    manifest = read_manifest(target)
    if not manifest or manifest.get("plugin") != PLUGIN_ID:
        raise ValueError("Install OptiScaler with this plugin first.")
    if live.status(target).get("attached"):
        raise RuntimeError(f"Close the game before {action}.")
    # A game can be running without our ASI. Wine maps the DLL/executable into
    # its process; check mappings too, including paths containing spaces.
    for maps in Path("/proc").glob("[0-9]*/maps"):
        try:
            for line in maps.read_text(errors="replace").splitlines():
                fields = line.split(maxsplit=5)
                if len(fields) == 6 and fields[5].startswith(str(target) + "/"):
                    raise RuntimeError(f"Close the game before {action}.")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return manifest


def atomic_copy(source, destination):
    """Stage beside the destination so replacement is atomic on its filesystem."""
    destination = Path(destination)
    fd, name = tempfile.mkstemp(prefix=".decky-fsr4-", dir=destination.parent)
    os.close(fd)
    try:
        shutil.copy2(source, name)
        os.replace(name, destination)
    finally:
        Path(name).unlink(missing_ok=True)


def deck_preset_changes(model):
    """The ini a Steam Deck FSR 4 preset writes: FidelityFX backend, FSR 4, a model."""
    spec = FSR4_DECK_MODELS[model]
    return {
        # OptiScaler 10 names the FidelityFX backend "ffx" (fsr31 before).
        "Upscalers": {"Dx12Upscaler": "ffx", "Dx11Upscaler": "ffx_12",
                      "VulkanUpscaler": "ffx_12"},
        "FSR": {"Fsr4GpuSupport": spec["Fsr4GpuSupport"], "Fsr4Model": spec["Fsr4Model"],
                "UpscalerIndex": "0", "Fsr4Preset": "auto"},
    }


def require_managed_install(target_dir):
    """The manifest of an install this plugin made, or an error."""
    manifest = read_manifest(Path(target_dir).resolve())
    if not manifest or manifest.get("plugin") != PLUGIN_ID:
        raise ValueError("Install OptiScaler with this plugin first.")
    return manifest


def _replace_fsr4(target, source, recorded, deck_model=None, replace_dll=True):
    # Swapping the DLL needs the game closed; writing only the ini does not
    # (OptiScaler reads it at the next launch).
    manifest = require_stopped_install(target) if replace_dll else require_managed_install(target)
    target = Path(target)
    # The upscaler OptiScaler loads: in OptiScaler/, or the game folder for an
    # install from before it.
    upscaler = layout.relative(target, layout.ffx_upscaler(target))
    if upscaler not in manifest.get("files", []):
        raise ValueError("Reinstall OptiScaler before changing its upscaler build.")
    # Roll back both settings and DLL if any part of the operation fails.
    names = [upscaler, MANIFEST_NAME, INI_NAME]
    with tempfile.TemporaryDirectory(prefix=".decky-fsr4-", dir=target) as staging:
        staging = Path(staging)
        existed = {name: (target / name).is_file() for name in names}
        saved = {name: staging / f"{index}.saved" for index, name in enumerate(names)}
        for name in names:
            if existed[name]:
                shutil.copy2(target / name, saved[name])
        try:
            if replace_dll:
                atomic_copy(source, target / upscaler)
            if deck_model:
                if not existed[INI_NAME]:
                    raise FileNotFoundError("OptiScaler.ini not found")
                ini = IniFile(saved[INI_NAME])
                changes = deck_preset_changes(deck_model)
                for section, values in changes.items():
                    for key, value in values.items():
                        meta = schema.option(section, key)
                        if meta is None or not schema.valid(meta, value):
                            raise ValueError(f"Invalid FSR 4 preset setting: {section}.{key}={value}")
                ini.update(changes)
                # Keep the rollback copy separate from the edited settings.
                ini.path = staging / "new.ini"
                ini.save()
                atomic_copy(ini.path, target / INI_NAME)
            manifest["fsr4_build"] = recorded
            staged_manifest = staging / "new.json"
            staged_manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            atomic_copy(staged_manifest, target / MANIFEST_NAME)
        except Exception:
            for name in names:
                if existed[name]:
                    atomic_copy(saved[name], target / name)
                else:
                    (target / name).unlink(missing_ok=True)
            raise


def install_fsr4_build(target_dir, dll_path, build, logger=None, enable_int8=False):
    """Install a verified community DLL, optionally with its INT8 settings."""
    if fsr4build.digest(dll_path) != build["file_sha256"]:
        raise ValueError("FSR 4 DLL checksum mismatch")
    recorded = {"id": build["id"], "label": build["label"],
                "file": FFX_UPSCALER_DLL, "sha256": build["file_sha256"],
                "reaches_fsr4_by": build["reaches_fsr4_by"]}
    _replace_fsr4(target_dir, dll_path, recorded, "int8" if enable_int8 else None)
    return {"path": str(target_dir), "file": layout.relative(target_dir, layout.ffx_upscaler(target_dir)),
            "build": recorded}


def enable_fsr4_deck(target_dir, payload_root, model, logger=None):
    """A Steam Deck FSR 4 preset: the bundled upscaler plus one of its models.

    Puts the bundled DLL back first if something else is there (a community
    build), then writes the preset's settings, as one rollback-safe step. When
    the bundled DLL is already in place, which is the usual case, only the
    settings change, so it works while the game runs and applies at its next
    launch; swapping a DLL still needs the game closed.
    """
    if model not in FSR4_DECK_MODELS:
        raise ValueError(f"Unknown Steam Deck FSR 4 model: {model}")
    source = Path(payload_root) / layout.payload_name(FFX_UPSCALER_DLL)
    bundled = fsr4build.BUNDLED_BUILD["file_sha256"]
    if fsr4build.digest(source) != bundled:
        raise ValueError("Bundled FSR 4 DLL checksum mismatch")
    current = layout.ffx_upscaler(target_dir)
    in_place = current.is_file() and fsr4build.digest(current) == bundled
    _replace_fsr4(target_dir, source, {}, model, replace_dll=not in_place)
    return {"path": str(target_dir), "model": model,
            "label": FSR4_DECK_MODELS[model]["label"],
            "build": fsr4build.identify(layout.ffx_upscaler(target_dir))}


def restore_fsr4_build(target_dir, payload_root, logger=None):
    """Restore the bundled DLL; upscaler and frame-generation settings stay intact."""
    source = Path(payload_root) / layout.payload_name(FFX_UPSCALER_DLL)
    if fsr4build.digest(source) != fsr4build.BUNDLED_BUILD["file_sha256"]:
        raise ValueError("Bundled FSR 4 DLL checksum mismatch")
    _replace_fsr4(target_dir, source, {})
    return {"path": str(target_dir), "restored": FFX_UPSCALER_DLL,
            "build": fsr4build.identify(layout.ffx_upscaler(target_dir))}


def add_files(target_dir, sources, logger=None, reframework_revision=None):
    """Copy extra files into an existing install and register them for removal.

    The same contract as everything else this plugin puts in a game folder:
    anything displaced is stashed, everything added is written into the
    manifest, and uninstall reverses both. Used to add REFramework to a game
    that is already set up, or to retry a download that failed the first time.

    A game with no manifest — OptiScaler installed by hand, or by another tool —
    still gets the files. It is the tracking that is unavailable there, not the
    install, and refusing would be refusing the only thing the user asked for.
    """
    target = Path(target_dir)
    if not target.is_dir():
        raise NotADirectoryError(f"install target does not exist: {target}")

    manifest = read_manifest(target) or {}
    backups = dict(manifest.get("backups") or {})
    files = list(manifest.get("files") or [])
    added = []

    for source in sources:
        source = Path(source)
        if not source.is_file():
            raise FileNotFoundError(f"missing {source}")
        name = source.name
        destination = target / name
        if destination.exists() and name not in backups and name not in files:
            backups[name] = _stash(target, destination, logger)
        shutil.copy2(source, destination)
        if name not in files:
            files.append(name)
        added.append(name)
        if logger:
            logger.info("installed %s", name)

    if manifest:
        manifest["files"] = files
        manifest["backups"] = backups
        if REFRAMEWORK_DLL in added:
            manifest["reframework"] = True
            manifest["reframework_revision"] = reframework_revision
        (target / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return {"ok": True, "path": str(target), "files": added, "tracked": bool(manifest)}


def remove_files(target_dir, names, logger=None):
    """Take specific files back out, restoring anything they displaced.

    The counterpart to `add_files`, and the reason it exists is Monster Hunter
    Wilds: REFramework is a 23 MB third-party DLL that hooks the engine, and
    when a game stops booting after an install there is no way to find out
    whether that was OptiScaler or REF unless one of them can be taken out on
    its own. Bundling them into a single all-or-nothing install left the user
    with a game that would not start and no way to bisect it.
    """
    target = Path(target_dir)
    if not target.is_dir():
        raise NotADirectoryError(f"path does not exist: {target}")

    manifest = read_manifest(target) or {}
    files = list(manifest.get("files") or [])
    backups = dict(manifest.get("backups") or {})
    removed = []
    restored = []

    for name in names:
        path = target / name
        if path.is_file():
            path.unlink()
            removed.append(name)
        if name in files:
            files.remove(name)
        # Whatever this displaced goes back, exactly as a full uninstall does.
        if backups.pop(name, None):
            stashed = backup_dir(target) / name
            if stashed.is_file():
                shutil.move(str(stashed), str(path))
                restored.append(name)
                if logger:
                    logger.info("restored original %s", name)

    if manifest:
        manifest["files"] = files
        manifest["backups"] = backups
        if REFRAMEWORK_DLL in removed:
            manifest["reframework"] = False
            manifest["reframework_revision"] = None
        (target / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return {"ok": True, "removed": removed, "restored": restored}


def record_optipatcher(target_dir, installed):
    """Record OptiPatcher, added or taken out from its own toggle, in the manifest.

    Without this, an OptiPatcher added after the install was nobody's: uninstall
    left it in OptiScaler's plugin folder. Only this plugin's own installs have
    a manifest to record it in; returns whether it was recorded.
    """
    target = Path(target_dir)
    manifest = read_manifest(target)
    if not manifest or manifest.get("plugin") != PLUGIN_ID:
        return False
    name = layout.relative(target, live.plugin_dir(target) / OPTIPATCHER_NAME)
    files = [n for n in manifest.get("files") or [] if n != name]
    if installed:
        files.append(name)
    manifest["files"] = files
    manifest["optipatcher"] = bool(installed)
    (target / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return True


def _stash(target, path, logger=None):
    """Move a file or directory into the backup folder; returns its name."""
    stash = backup_dir(target)
    stash.mkdir(parents=True, exist_ok=True)
    destination = stash / path.name
    if destination.exists():
        # An earlier install already preserved the original; keep the oldest.
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        return path.name
    shutil.move(str(path), str(destination))
    if logger:
        logger.info("backed up %s to %s/", path.name, BACKUP_DIR)
    return path.name


#: Where an install keeps what it replaced until it has finished. Inside the
#: game folder so every move is a rename on the same filesystem.
INSTALL_STAGING = ".decky-optiscaler-nightly-install"
_JOURNAL = "journal.json"


class _Journal:
    """Undo log for one install: everything it writes can be put back.

    Every path the install is about to create, overwrite or remove goes through
    ``prepare`` first. If something is there it is moved into the staging folder
    (a rename, so instant) and a "restore" is recorded; otherwise a "remove" is.
    The log is saved after each entry, so a plugin that dies half way leaves
    enough behind for ``recover_interrupted`` to finish the rollback later.
    """

    def __init__(self, target):
        self.target = Path(target)
        self.dir = self.target / INSTALL_STAGING
        self.dir.mkdir()
        self.ops = []
        self._count = 0
        self._save()

    def _save(self):
        # Written to a temp file and renamed over, so a crash never leaves a
        # half-written journal for recover_interrupted to misread.
        temp = self.dir / (_JOURNAL + ".tmp")
        temp.write_text(json.dumps(self.ops), encoding="utf-8")
        os.replace(temp, self.dir / _JOURNAL)

    def _rel(self, path):
        return str(Path(path).relative_to(self.target))

    def prepare(self, path):
        """Call before writing to, replacing or removing `path`."""
        path = Path(path)
        if path.exists() or path.is_symlink():
            self._count += 1
            saved = f"{self._count:04d}"
            shutil.move(str(path), str(self.dir / saved))
            self.ops.append({"op": "restore", "path": self._rel(path), "saved": saved})
        else:
            self.ops.append({"op": "remove", "path": self._rel(path)})
        self._save()

    def ensure_dir(self, path):
        """Create a directory, recording it so an undo removes it if it is left empty."""
        path = Path(path)
        if path.is_dir():
            return
        self.ops.append({"op": "rmdir", "path": self._rel(path)})
        self._save()
        path.mkdir(parents=True)

    def saved_path(self, path):
        """Where `prepare` moved `path`, or None."""
        rel = self._rel(path)
        for op in reversed(self.ops):
            if op["op"] == "restore" and op["path"] == rel:
                return self.dir / op["saved"]
        return None

    def write_file(self, source, path):
        self.prepare(path)
        shutil.copy2(source, path)

    def write_tree(self, source, path):
        """Copy `source` over `path`, merging into what is there as copytree's
        dirs_exist_ok does, with the previous contents kept for an undo."""
        self.prepare(path)
        previous = self.saved_path(path)
        if previous is not None and previous.is_dir():
            shutil.copytree(previous, path)
            shutil.copytree(source, path, dirs_exist_ok=True)
        else:
            shutil.copytree(source, path)

    def write_text(self, path, text):
        self.prepare(path)
        Path(path).write_text(text, encoding="utf-8")

    def remove(self, path):
        self.prepare(path)

    def stash(self, name):
        """Set the game's own `name` aside in the backup folder, as _stash does.

        A name in a subfolder (OptiScaler/libxess.dll) keeps its place under
        the backup folder.
        """
        path = self.target / name
        self.prepare(path)
        original = self.saved_path(path)
        stash_dir = backup_dir(self.target)
        destination = stash_dir / name
        self.ensure_dir(stash_dir)
        for parent in reversed(destination.relative_to(stash_dir).parents[:-1]):
            self.ensure_dir(stash_dir / parent)
        # An earlier install already preserved the original; keep the oldest.
        if not destination.exists():
            self.prepare(destination)
            (shutil.copytree if original.is_dir() else shutil.copy2)(original, destination)
        return name

    def unstash(self, name):
        """Put the game's own `name` back from the backup folder."""
        stashed = backup_dir(self.target) / name
        if not stashed.exists():
            return False
        self.prepare(stashed)
        original = self.saved_path(stashed)
        # Folders it was kept in under the backup folder (OptiScaler/...) go
        # once empty, as they came.
        folder = stashed.parent
        while folder != backup_dir(self.target) and self.remove_if_empty(folder):
            folder = folder.parent
        path = self.target / name
        self.ensure_dirs(path.parent)
        self.prepare(path)
        (shutil.copytree if original.is_dir() else shutil.copy2)(original, path)
        return True

    def ensure_dirs(self, path):
        """ensure_dir for `path` and each missing folder above it in the game folder."""
        path = Path(path)
        missing = []
        while path != self.target and not path.is_dir():
            missing.append(path)
            path = path.parent
        for folder in reversed(missing):
            self.ensure_dir(folder)

    def remove_if_empty(self, path):
        """Remove a folder only when nothing is left in it; True when it went."""
        path = Path(path)
        if not path.is_dir() or path.is_symlink() or any(path.iterdir()):
            return False
        self.prepare(path)
        return True

    def commit(self, keep=None):
        # Removing the journal is the commit point: from here on a crash leaves
        # staging without a journal, which recover_interrupted only cleans up.
        # Deleting staging first could leave a journal pointing at saved files
        # that are already gone, and "undoing" that would lose the originals.
        (self.dir / _JOURNAL).unlink()
        # What the install replaced is still in staging: `keep` may move some of
        # it out (the previous OptiScaler, for going back) before it goes.
        if keep is not None:
            keep(self.dir, self.ops)
        shutil.rmtree(self.dir, ignore_errors=True)

    def rollback(self):
        _undo(self.target, self.dir, self.ops)


def _undo(target, staging, ops):
    for op in reversed(ops):
        path = target / op["path"]
        if op["op"] == "rmdir":
            try:
                path.rmdir()
            except OSError:
                pass
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists() or path.is_symlink():
            path.unlink()
        if op["op"] == "restore":
            saved = staging / op["saved"]
            if saved.exists() or saved.is_symlink():
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(saved), str(path))
    shutil.rmtree(staging, ignore_errors=True)


def recover_interrupted(target_dir, logger=None):
    """Finish rolling back an install that stopped half way (plugin killed, power lost).

    Returns True when there was one to roll back.
    """
    target = Path(target_dir)
    # A restore point that was being put together when the plugin stopped.
    shutil.rmtree(target / (PREVIOUS_DIR + ".new"), ignore_errors=True)
    staging = target / INSTALL_STAGING
    if not staging.is_dir():
        return False
    journal = staging / _JOURNAL
    if not journal.is_file():
        # The install had committed and was only cleaning up.
        shutil.rmtree(staging, ignore_errors=True)
        return False
    ops = json.loads(journal.read_text(encoding="utf-8"))  # written atomically
    if logger:
        logger.warning("rolling back an interrupted install in %s", target)
    _undo(target, staging, ops)
    return True


def install(target_dir, payload_root, filename=DEFAULT_PROXY, preserve_ini=True,
            logger=None, live_asi=None, optipatcher=None, reframework_files=None,
            reframework_revision=None):
    """Copy OptiScaler into target_dir under the given proxy filename.

    ``live_asi`` is the path to the compiled live-control plugin; when given it
    is installed alongside OptiScaler so settings can be changed while playing.

    ``optipatcher`` is the path to OptiPatcher.asi. It goes in the same plugin
    folder and lets OptiScaler expose DLSS/DLSS-FG inputs in the games it has
    patterns for, without DXGI spoofing.

    ``reframework_files`` are the already-downloaded REFramework files for the
    RE Engine games that cannot host OptiScaler without it. They go in through
    the same ``place`` as everything else, on purpose: that is what puts them in
    the manifest, stashes anything they displace, and takes them back out on
    uninstall. A mod installed off to one side of that machinery is a mod this
    plugin cannot promise to remove.
    """
    if filename not in PROXY_FILENAMES:
        raise ValueError(f"unsupported filename: {filename}")

    target = Path(target_dir)
    payload = Path(payload_root)
    if not target.is_dir():
        raise NotADirectoryError(f"install target does not exist: {target}")
    if not os.access(target, os.W_OK):
        raise PermissionError(f"no write permission for {target}")
    source_dll = payload / "OptiScaler.dll"
    if not source_dll.is_file():
        raise FileNotFoundError(f"payload incomplete, missing {source_dll}")

    recover_interrupted(target, logger)
    require_not_foreign(target)
    old_manifest = read_manifest(target) or {}
    journal = _Journal(target)
    try:
        result = _install(target, payload, filename, preserve_ini, logger, live_asi,
                          optipatcher, reframework_files, reframework_revision, journal)
    except BaseException:
        journal.rollback()
        raise

    def keep(staging, ops):
        try:
            _keep_previous(target, staging, ops, old_manifest, logger)
        except Exception:  # a convenience: the install itself has committed
            shutil.rmtree(target / (PREVIOUS_DIR + ".new"), ignore_errors=True)
            if logger:
                logger.exception("could not keep the previous OptiScaler in %s", target)

    journal.commit(keep=keep)
    result["fsr4"] = fsr4_status(target)
    result["previous"] = read_previous(target)
    return result


#: The OptiScaler this game had before its last update, for going back to it.
#: One step: each update that changes anything replaces it, uninstall removes it.
PREVIOUS_DIR = ".decky-optiscaler-nightly-previous"
_PREVIOUS_RECORD = "previous.json"


def _same_file(a, b):
    a, b = Path(a), Path(b)
    if a.is_dir() or b.is_dir():
        if not (a.is_dir() and b.is_dir()):
            return False
        left = sorted(p.relative_to(a).as_posix() for p in a.rglob("*"))
        right = sorted(p.relative_to(b).as_posix() for p in b.rglob("*"))
        return left == right and all(_same_file(a / name, b / name) for name in left
                                     if (a / name).is_file())
    if not (a.is_file() and b.is_file()) or a.stat().st_size != b.stat().st_size:
        return False
    with a.open("rb") as left, b.open("rb") as right:
        while True:
            chunk = left.read(1 << 20)
            if chunk != right.read(1 << 20):
                return False
            if not chunk:
                return True


def _keep_previous(target, staging, ops, old_manifest, logger=None):
    """After an update: keep the previous OptiScaler's files the update replaced.

    Only this plugin's own earlier install counts (its manifest), never the
    ini (it is the user's settings, kept by the update anyway) and never a
    file the update left byte-identical: most of a nightly's DLLs do not change
    from one to the next, and a whole copy is about 300 MB. An update that
    changed nothing keeps the restore point it already had.
    """
    if old_manifest.get("plugin") != PLUGIN_ID:
        return
    new_manifest = read_manifest(target) or {}
    first = {}
    for op in ops:
        if op["op"] == "restore":
            first.setdefault(Path(op["path"]).as_posix(), op["saved"])
    old_names = [name for name in (old_manifest.get("files") or []) + (old_manifest.get("dirs") or [])
                 if name != INI_NAME]
    new_names = set((new_manifest.get("files") or []) + (new_manifest.get("dirs") or []))
    kept = {name: first[name] for name in old_names
            if name in first and not _same_file(staging / first[name], target / name)}
    added = sorted(name for name in new_names - set(old_names) if name != INI_NAME)
    if not kept and not added:
        return
    building = target / (PREVIOUS_DIR + ".new")
    shutil.rmtree(building, ignore_errors=True)
    (building / "files").mkdir(parents=True)
    for name, saved in kept.items():
        destination = building / "files" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging / saved), str(destination))
    record = {
        "version": old_manifest.get("optiscaler_version"),
        "replaced_by": new_manifest.get("optiscaler_version"),
        "taken_at": time.time(),
        "files": sorted(kept),
        "manifest": old_manifest,
    }
    (building / _PREVIOUS_RECORD).write_text(json.dumps(record, indent=2), encoding="utf-8")
    shutil.rmtree(target / PREVIOUS_DIR, ignore_errors=True)
    os.replace(building, target / PREVIOUS_DIR)
    if logger:
        logger.info("kept OptiScaler %s (%d changed files) to go back to", record["version"], len(kept))


def read_previous(target_dir):
    """{version, replaced_by, taken_at, files} of the restore point, or None.

    Only one that belongs to the install in the folder now: a later update or
    a reinstall under another version replaced or invalidated it.
    """
    target = Path(target_dir)
    try:
        record = json.loads((target / PREVIOUS_DIR / _PREVIOUS_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    manifest = read_manifest(target) or {}
    if manifest.get("plugin") != PLUGIN_ID or manifest.get("optiscaler_version") != record.get("replaced_by"):
        return None
    return {key: record.get(key) for key in ("version", "replaced_by", "taken_at", "files")}


def restore_previous(target_dir, logger=None):
    """Put back the OptiScaler this game had before its last update.

    The game must be closed. Goes through the same journal as an install, so a
    failure part way leaves the folder as it was. OptiScaler.ini is not touched:
    it is the user's settings, and the update kept it. The restore point is
    used up; "Update OptiScaler in this game" takes the bundled one again.
    """
    target = Path(target_dir).resolve()
    recover_interrupted(target, logger)
    require_not_foreign(target)
    manifest = require_stopped_install(target, "going back to the previous OptiScaler")
    try:
        record = json.loads((target / PREVIOUS_DIR / _PREVIOUS_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("There is no earlier OptiScaler to go back to in this game.")
    if read_previous(target) is None:
        raise ValueError("OptiScaler in this game has changed since its last update; "
                         "there is nothing to go back to.")
    old = record["manifest"]
    kept = target / PREVIOUS_DIR / "files"
    old_names = set((old.get("files") or []) + (old.get("dirs") or []))
    old_containers = set(old.get("containers") or [])
    old_backups = dict(old.get("backups") or {})
    backups = dict(manifest.get("backups") or {})
    journal = _Journal(target)
    try:
        # What the update added: ours (the current manifest says so), so it
        # goes, and a game file it had set aside comes back.
        for name in (manifest.get("files") or []) + (manifest.get("dirs") or []):
            if name == INI_NAME or name in old_names:
                continue
            path = target / name
            if path.exists() or path.is_symlink():
                journal.remove(path)
            if name in backups and name not in old_backups:
                backups.pop(name)
                journal.unstash(name)
        # Its folders the previous install did not have, once empty. Back to an
        # install from before OptiScaler/, that folder must go: OptiScaler
        # would look for its DLLs in it.
        for name in sorted(set(manifest.get("containers") or []) - old_containers,
                           key=lambda n: n.count("/"), reverse=True):
            path = target / name
            if not journal.remove_if_empty(path) and name == OPTI_SUBDIR and path.is_dir():
                left = sorted(layout.relative(target, p) for p in path.rglob("*")
                              if not p.is_dir() or not any(p.iterdir()))
                raise ValueError(
                    f"{OPTI_SUBDIR}/ holds files this plugin did not put there ({', '.join(left[:3])}"
                    f"{', ...' if len(left) > 3 else ''}). The OptiScaler to go back to reads its "
                    f"DLLs from the game folder, and would look in {OPTI_SUBDIR}/ instead: move "
                    "them out first.")
        # What it replaced comes back. A file the update retired had the
        # game's own original put back in its place: that goes aside again.
        for name in record.get("files") or []:
            source = kept / name
            destination = target / name
            if name in old_backups and name not in backups and destination.exists():
                backups[name] = journal.stash(name)
            journal.ensure_dirs(destination.parent)
            if source.is_dir():
                journal.prepare(destination)
                shutil.copytree(source, destination)
            else:
                journal.write_file(source, destination)
        restored = dict(old)
        # In the old manifest's order: the record it was is the record it is.
        restored["backups"] = {**{name: backups[name] for name in old_backups if name in backups},
                               **{name: value for name, value in backups.items() if name not in old_backups}}
        journal.write_text(target / MANIFEST_NAME, json.dumps(restored, indent=2))
    except BaseException:
        journal.rollback()
        raise
    journal.commit()
    shutil.rmtree(target / PREVIOUS_DIR, ignore_errors=True)
    if logger:
        logger.info("went back to OptiScaler %s in %s", old.get("optiscaler_version"), target)
    return {"path": str(target), "version": old.get("optiscaler_version"),
            "restored": record.get("files") or []}


def _merge_stashed_plugins(target, own, journal=None, logger=None):
    """Put back the player's files from a plugins/ an earlier bundle set aside.

    The bundles of mid-November 2026 nightlies listed the archive's empty
    plugins/ folder as payload. Installing one then treated the game's plugins/
    as the game's own: it copied it, with this plugin's ASIs and any the player
    had added, into the backup folder, and uninstall deleted plugins/ (with
    anything added since) before moving that old copy back. The player's files go back into plugins/ (never over
    one that is there now); `own` names this plugin's files, which an install
    writes again and an uninstall removes.
    """
    target = Path(target)
    stashed = backup_dir(target) / live.PLUGIN_SUBDIR
    if not stashed.is_dir():
        return []
    folder = target / live.PLUGIN_SUBDIR
    put_back = []
    for item in sorted(stashed.iterdir()):
        destination = folder / item.name
        if item.name in own or destination.exists():
            continue
        if journal is not None:
            journal.ensure_dir(folder)
            (journal.write_tree if item.is_dir() else journal.write_file)(item, destination)
        else:
            folder.mkdir(exist_ok=True)
            (shutil.copytree if item.is_dir() else shutil.copy2)(item, destination)
        put_back.append(item.name)
    if journal is not None:
        journal.remove(stashed)
    else:
        shutil.rmtree(stashed, ignore_errors=True)
    if logger:
        logger.info("put back %d file(s) of the player's plugins/ an earlier install had set aside",
                    len(put_back))
    return put_back


def _own_plugin_files(manifest):
    """This plugin's files in plugins/: the in-game plugin, and OptiPatcher when it put it there."""
    prefixes = (f"{live.PLUGIN_SUBDIR}/", f"{OPTI_SUBDIR}/{live.PLUGIN_SUBDIR}/")
    return {live.ASI_NAME} | {Path(name).name for name in (manifest.get("files") or [])
                              if name.startswith(prefixes)}


def _install(target, payload, filename, preserve_ini, logger, live_asi, optipatcher,
             reframework_files, reframework_revision, journal):
    """install()'s steps. Every write goes through `journal`, so a failure anywhere
    leaves the game folder exactly as it was."""
    source_dll = payload / "OptiScaler.dll"
    previous = detect(target)
    old_manifest = read_manifest(target) or {}
    selected_build = old_manifest.get("fsr4_build") or {}
    if selected_build and fsr4build.retired(selected_build.get("id")):
        # A build this plugin no longer installs (4.1.1b): reinstalling is the
        # way back to the bundled upscaler, so let it through and forget it.
        if logger:
            logger.info("dropping retired FSR 4 build %s", selected_build.get("id"))
        selected_build = {}
    upscaler = layout.payload_name(FFX_UPSCALER_DLL)
    if selected_build and layout.relative(target, layout.ffx_upscaler(target)) != upscaler:
        # An install from before OptiScaler/: its selected build is in the game
        # folder, which the move takes out. The bundled upscaler goes in.
        if logger:
            logger.info("FSR 4 build %s stays behind with the old layout", selected_build.get("id"))
        selected_build = {}
    if selected_build:
        build = fsr4build.find(selected_build.get("id"))
        current = target / upscaler
        if not build or not current.is_file() or fsr4build.digest(current) != build["file_sha256"]:
            raise ValueError("Selected FSR 4 build is missing or changed. Enable it again or restore the bundled build first.")
    old_files = set(old_manifest.get("files") or [])
    old_dirs = set(old_manifest.get("dirs") or [])
    old_containers = list(old_manifest.get("containers") or [])
    # This plugin's own install from before OptiScaler/: it moves.
    moving = old_manifest.get("plugin") == PLUGIN_ID and old_manifest.get("layout") != LAYOUT
    backups = dict(old_manifest.get("backups") or {})
    created = []
    containers = []

    # Remove a previous install under a different filename so the game does not
    # end up loading OptiScaler twice.
    for stale in previous.get("candidates", []):
        if stale == filename:
            continue
        stale_path = target / stale
        if not _looks_like_optiscaler(stale_path):
            continue
        journal.remove(stale_path)
        # If we displaced the game's own dll to install there, put it back now
        # rather than leaving the game without it until uninstall.
        if backups.pop(stale, None):
            if journal.unstash(stale) and logger:
                logger.info("restored original %s while moving proxy", stale)
        elif logger:
            logger.info("removed previous OptiScaler proxy %s", stale)

    def place(src, dest_name):
        dest = target / dest_name
        if dest.exists() and dest_name not in backups and dest_name not in created:
            # Only stash files that belong to the game, not our own leftovers.
            if dest_name not in old_files and not _looks_like_optiscaler(dest):
                backups[dest_name] = journal.stash(dest_name)
        journal.write_file(src, dest)
        if dest_name not in created:
            created.append(dest_name)

    place(source_dll, filename)

    if live.PLUGIN_SUBDIR in old_dirs or live.PLUGIN_SUBDIR in backups:
        backups.pop(live.PLUGIN_SUBDIR, None)
        _merge_stashed_plugins(target, _own_plugin_files(old_manifest), journal, logger)

    # Files and folders an earlier bundle installed that this one no longer
    # ships (OptiScaler 10 folded fakenvapi in and replaced amd_fidelityfx_dx12/
    # _vk with the loader DLLs). They are ours only if the previous manifest
    # says so; anything they displaced from the game goes back.
    for name in RETIRED_PAYLOAD_FILES:
        if name not in old_files:
            continue
        journal.remove(target / name)
        if backups.pop(name, None):
            journal.unstash(name)
        if logger:
            logger.info("removed retired payload file %s", name)
    for name in RETIRED_PAYLOAD_DIRS:
        if name not in old_dirs:
            continue
        journal.remove(target / name)
        if backups.pop(name, None):
            journal.unstash(name)
        if logger:
            logger.info("removed retired payload dir %s/", name)

    # This plugin's ASIs in plugins/ in the game folder, where installs from
    # before OptiScaler/ put them: OptiScaler loads them from OptiScaler/plugins
    # now, where this install puts its copies. The player's own stay where they
    # are (another ASI loader may read that folder too).
    legacy_plugins = target / live.PLUGIN_SUBDIR
    if moving:
        replaced = {live.ASI_NAME: bool(live_asi), OPTIPATCHER_NAME: bool(optipatcher)}
        for name in sorted({*(n for n in old_files if n.startswith(f"{live.PLUGIN_SUBDIR}/")),
                            *(f"{live.PLUGIN_SUBDIR}/{n}" for n, new in replaced.items() if new)}):
            path = target / name
            if path.is_file() or path.is_symlink():
                journal.remove(path)
                if logger:
                    logger.info("moved %s into %s/%s", name, OPTI_SUBDIR, live.PLUGIN_SUBDIR)
            if backups.pop(name, None):
                journal.unstash(name)
        journal.remove_if_empty(legacy_plugins)

    ini_path = target / INI_NAME
    if ini_path.is_file() and INI_NAME not in old_files and INI_NAME not in backups:
        # An OptiScaler.ini this plugin did not put there (left by an earlier
        # OptiScaler, or the player's own): a copy goes in the backup folder,
        # so uninstall puts it back as it was instead of deleting it with
        # the install's files.
        backups[INI_NAME] = journal.stash(INI_NAME)
        if preserve_ini:
            journal.write_file(backup_dir(target) / INI_NAME, ini_path)
    if not (preserve_ini and ini_path.is_file()):
        journal.write_file(payload / INI_NAME, ini_path)
    if INI_NAME not in created:
        created.append(INI_NAME)

    # OptiScaler's folders, parents first. Only ever removed once empty: the
    # player's own ASIs go in OptiScaler/plugins too.
    for name in PAYLOAD_DIRS:
        if (target / name).exists() and not (target / name).is_dir():
            backups[name] = journal.stash(name)
        journal.ensure_dirs(target / name)
        containers.append(name)

    for name in PAYLOAD_FILES:
        if name == upscaler and selected_build:
            created.append(name)
            continue
        src = payload / name
        if src.is_file():
            place(src, name)

    # REFramework, for the games whose wiki entry says OptiScaler does nothing
    # without it. One file (plus the revision stamp REF itself writes), placed
    # next to the executable exactly as REF's own instructions describe.
    ref_result = {"installed": False, "files": [], "error": None,
                  "revision": reframework_revision}
    for source in reframework_files or []:
        source = Path(source)
        if not source.is_file():
            ref_result["error"] = f"missing {source.name}"
            continue
        place(source, source.name)
        ref_result["files"].append(source.name)
    ref_result["installed"] = REFRAMEWORK_DLL in ref_result["files"]

    # The live-control plugin: an .asi OptiScaler loads into the game process so
    # frame generation and the upscaler can be changed without a restart.
    plugins = f"{OPTI_SUBDIR}/{live.PLUGIN_SUBDIR}"
    live_result = {"installed": False, "error": None}
    if live_asi:
        journal.ensure_dirs(target / plugins)
        journal.prepare(target / plugins / live.ASI_NAME)
        outcome = live.install_plugin_asi(target, live_asi, live.ASI_NAME, logger, folder=target / plugins)
        if outcome.get("ok"):
            live_result["installed"] = True
            created.append(f"{plugins}/{live.ASI_NAME}")
        else:
            live_result["error"] = outcome.get("error")
            if logger:
                logger.warning("live control unavailable: %s", outcome.get("error"))

    # OptiPatcher: an optional ASI from the OptiScaler project. Some games
    # (Red Dead Redemption 2 among them) need it to expose their DLSS inputs.
    patcher_result = {"installed": False, "error": None}
    if optipatcher:
        journal.ensure_dirs(target / plugins)
        journal.prepare(target / plugins / OPTIPATCHER_NAME)
        outcome = live.install_plugin_asi(target, optipatcher, OPTIPATCHER_NAME, logger, folder=target / plugins)
        if outcome.get("ok"):
            patcher_result["installed"] = True
            created.append(f"{plugins}/{OPTIPATCHER_NAME}")
        else:
            patcher_result["error"] = outcome.get("error")
            if logger:
                logger.warning("optipatcher unavailable: %s", outcome.get("error"))
    if (target / plugins).is_dir():
        containers.append(plugins)

    # What an earlier install of this plugin owned and this one left untouched
    # (OptiPatcher left unticked, REFramework or FSR 4 files added later) is
    # still this plugin's and still on disk: it stays in the manifest, so
    # uninstall removes it and puts back what it displaced. A name this install
    # wrote, removed or gave back to the game (retired payload, the old layout's
    # files) is decided by this install.
    touched = {Path(op["path"]).as_posix() for op in journal.ops}
    carried = [name for name in old_manifest.get("files") or []
               if old_manifest.get("plugin") == PLUGIN_ID and name not in created
               and name not in touched
               and ((target / name).is_file() or (target / name).is_symlink())]
    if carried and logger:
        logger.info("still owned from the previous install: %s", ", ".join(carried))
    patcher_owned = patcher_result["installed"] or f"{plugins}/{OPTIPATCHER_NAME}" in carried
    ref_owned = ref_result["installed"] or REFRAMEWORK_DLL in carried
    ref_revision = ref_result["revision"]
    if not ref_result["installed"] and ref_owned:
        ref_revision = old_manifest.get("reframework_revision")

    # Folders an earlier install created that this one does not use, once empty.
    for name in sorted(set(old_containers) - set(containers), key=lambda n: n.count("/"), reverse=True):
        journal.remove_if_empty(target / name)

    manifest = {
        "plugin": PLUGIN_ID,
        "live_asi": live_result["installed"],
        "optipatcher": patcher_owned,
        "reframework": ref_owned,
        "reframework_revision": ref_revision,
        "fsr4_build": selected_build,
        "optiscaler_version": OPTISCALER_VERSION,
        "filename": filename,
        "installed_at": time.time(),
        "layout": LAYOUT,
        "files": created + carried,
        # Folders copied whole; installs from before OptiScaler/ had some.
        "dirs": [],
        "containers": containers,
        "backups": backups,
        "backup_dir": BACKUP_DIR,
    }
    journal.write_text(target / MANIFEST_NAME, json.dumps(manifest, indent=2))

    return {
        "path": str(target),
        "filename": filename,
        "version": OPTISCALER_VERSION,
        "ini_preserved": preserve_ini and ini_path.is_file(),
        "optipatcher": patcher_result,
        "reframework": ref_result,
        "launch_option": launch_option(filename),
        "files": created,
        "dirs": containers,
        "backups": backups,
        "backup_dir": str(backup_dir(target)),
        "live": live_result,
    }


def launch_option(filename):
    """Steam launch options needed for Wine to load the proxy dll."""
    if filename.lower().endswith(".asi"):
        return "%command%"  # .asi is loaded by an ASI loader, no override needed
    stem = filename[:-4] if filename.lower().endswith(".dll") else filename
    return f'WINEDLLOVERRIDES="{stem}=n,b" %command%'


def uninstall(target_dir, remove_ini=True, logger=None):
    """Remove this plugin's OptiScaler, restoring everything it displaced.

    Only installs this plugin made (its manifest says which files are its own).
    OptiScaler put there by another plugin, another tool or by hand is refused:
    it is removed with whatever put it there.
    """
    target = Path(target_dir)
    if not target.is_dir():
        raise NotADirectoryError(f"path does not exist: {target}")

    recover_interrupted(target, logger)
    manifest = read_manifest(target)
    if not manifest:
        require_not_foreign(target)
    removed = []
    restored = []

    if manifest:
        for name in manifest.get("files", []):
            if name == INI_NAME and not remove_ini:
                continue
            path = target / name
            if path.is_file():
                path.unlink()
                removed.append(name)
        for name in manifest.get("dirs", []):
            # plugins/ holds the player's ASIs too: only this plugin's own go
            # (live.remove_asi, below), never the folder (an earlier bundle
            # recorded it as an install folder).
            if name == live.PLUGIN_SUBDIR:
                continue
            path = target / name
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
                removed.append(name + "/")
        restored.extend(_merge_stashed_plugins(target, _own_plugin_files(manifest), None, logger))

    for name in RUNTIME_ARTIFACTS + ["remove_optiscaler.sh"]:
        path = target / name
        if path.is_file():
            path.unlink()
            removed.append(name)

    # The record of what Steam was passing before is ours and belongs to this
    # install; whoever asked for the uninstall has already been shown it.
    if forget_launch_options(target):
        removed.append(LAUNCH_RECORD_NAME)

    if not remove_ini:
        # Keeping the ini keeps the settings it holds now, not the copy an
        # install set aside from before it.
        kept = backup_dir(target) / INI_NAME
        if kept.is_file() and (target / INI_NAME).is_file():
            kept.unlink()

    # The live-control plugin and its command/status/log files are ours, so they
    # go too -- uninstall should leave nothing of this plugin behind.
    for name in live.remove_asi(target, logger).get("removed", []):
        if name not in removed:
            removed.append(name)

    # OptiScaler's folders, once empty: the player's own ASIs in
    # OptiScaler/plugins (and anything else put there) keep them.
    for name in sorted((manifest or {}).get("containers") or [], key=lambda n: n.count("/"), reverse=True):
        path = target / name
        try:
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
                removed.append(name + "/")
        except OSError:
            pass

    restored.extend(restore_backups(target, logger))

    manifest_path = target / MANIFEST_NAME
    if manifest_path.is_file():
        manifest_path.unlink()
    for name in (PREVIOUS_DIR, PREVIOUS_DIR + ".new"):
        if (target / name).is_dir():
            shutil.rmtree(target / name, ignore_errors=True)
            removed.append(name + "/")

    if logger:
        logger.info(
            "uninstalled OptiScaler from %s (%d removed, %d restored)",
            target, len(removed), len(restored),
        )
    return {
        "path": str(target),
        "removed": removed,
        "restored": restored,
        "backup_dir": str(backup_dir(target)),
    }


def _put_back(item, destination):
    """Move a backed-up file or folder to `destination`, into a folder that is there."""
    if destination.is_dir() and not destination.is_symlink() and item.is_dir():
        # A folder an install keeps using (OptiScaler/): the game's files go
        # back into it one by one, past anything of the player's in it.
        for child in sorted(item.iterdir()):
            _put_back(child, destination / child.name)
        item.rmdir()
        return
    if destination.is_dir() and not destination.is_symlink():
        shutil.rmtree(destination, ignore_errors=True)
    elif destination.exists() or destination.is_symlink():
        destination.unlink()
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(item), str(destination))


def restore_backups(target_dir, logger=None):
    """Move everything in the backup folder back, then drop the folder."""
    target = Path(target_dir)
    restored = []

    stash = backup_dir(target)
    if stash.is_dir():
        for item in sorted(stash.iterdir()):
            _put_back(item, target / item.name)
            restored.append(item.name)
            if logger:
                logger.info("restored %s from %s/", item.name, BACKUP_DIR)
        try:
            stash.rmdir()
        except OSError:
            if logger:
                logger.warning("%s/ not empty, leaving it in place", BACKUP_DIR)

    # Installs made by v0.1 of this plugin used a suffix instead of a folder.
    for path in target.glob("*" + LEGACY_BACKUP_SUFFIX):
        original = target / path.name[: -len(LEGACY_BACKUP_SUFFIX)]
        if original.exists():
            original.unlink()
        shutil.move(str(path), str(original))
        restored.append(original.name)

    return restored


def pe_file_version(path):
    """Read a PE file's VS_FIXEDFILEINFO version, or None.

    Used to report which FidelityFX SDK — and therefore which FSR version — is
    actually sitting in the game folder.
    """
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    marker = data.find(b"\xbd\x04\xef\xfe")
    if marker < 0 or marker + 16 > len(data):
        return None
    import struct

    try:
        minor, major, build, patch = struct.unpack("<HHHH", data[marker + 8 : marker + 16])
    except struct.error:
        return None
    return (major, minor, patch, build)


def verify_install(target_dir, payload_root):
    """Check that every bundled file really landed in the game folder.

    Answers the practical question "did the install actually override what the
    game shipped?" by comparing size and content against the payload.
    """
    target = Path(target_dir)
    payload = Path(payload_root)
    manifest = read_manifest(target)
    entries = []

    expected = list(PAYLOAD_FILES)
    if manifest and manifest.get("filename"):
        expected.insert(0, manifest["filename"])

    for name in expected:
        source = payload / ("OptiScaler.dll" if manifest and name == manifest.get("filename")
                            else name)
        destination = target / name
        record = {
            "name": name,
            "present": destination.is_file(),
            "matches_payload": False,
            "size": destination.stat().st_size if destination.is_file() else 0,
            "expected_size": source.stat().st_size if source.is_file() else 0,
        }
        if record["present"] and source.is_file():
            record["matches_payload"] = record["size"] == record["expected_size"]
        if name == layout.payload_name(FFX_UPSCALER_DLL):
            selected = (manifest or {}).get("fsr4_build") or {}
            build = fsr4build.find(selected.get("id", "bundled"))
            record["matches_payload"] = bool(
                record["present"] and build
                and fsr4build.digest(destination) == build["file_sha256"])
            if build and build.get("file_bytes"):
                record["expected_size"] = build["file_bytes"]
        entries.append(record)

    for name in PAYLOAD_DIRS:
        destination = target / name
        entries.append({
            "name": name + "/",
            "present": destination.is_dir(),
            "matches_payload": destination.is_dir(),
            "size": 0,
            "expected_size": 0,
        })

    ffx = layout.ffx_upscaler(target)
    version = pe_file_version(ffx) if ffx.is_file() else None
    fsr4_capable = bool(version and version[:3] >= FSR4_MIN_SDK_VERSION[:3])

    problems = [e["name"] for e in entries if not e["present"] or not e["matches_payload"]]
    return {
        "path": str(target),
        "files": entries,
        "problems": problems,
        "complete": not problems,
        "ffx_upscaler": {
            "present": ffx.is_file(),
            "version": ".".join(str(n) for n in version) if version else None,
            "fsr4_capable": fsr4_capable,
        },
    }
