"""Live, in-game control of OptiScaler through the bundled ASI plugin.

OptiScaler only reads OptiScaler.ini at startup, so writing the file changes
nothing until the game restarts. Its in-game overlay avoids that because it runs
*inside* the game and writes straight into the live config object.

This module drives a small ASI plugin (built from ``asi/live.cpp``) that
OptiScaler loads into the game process. OptiScaler 10 hands it its plugin API
(``asi/plugin_sdk/optiscaler_plugin.h``); the plugin writes Config fields at
offsets generated from the nightly's own Config.h, and asks OptiScaler to
re-create the upscaler through that API when a setting needs it. Nothing in
OptiScaler's State is touched, so switching to another upscaler backend or FFX
frame generator is not live: those keep "restart to apply".

The two sides talk through plain files next to OptiScaler.ini:

    decky_optiscaler_live.cmd     what to apply, written here
    decky_optiscaler_live.status  what the ASI found and did, written there
    decky_optiscaler_live.dump    every Config field, on request (tests)

Files rather than a socket because they need no ports, no permissions and no
knowledge of how Proton has wired the game's network up; the ASI polls the
command file ten times a second. scripts/test_live_asi.py runs this whole
channel against the real OptiScaler under Wine.
"""

import json
import os
import re
import threading
import time
from pathlib import Path

from . import layout
from .constants import BACKUP_DIR, OPTI_SUBDIR
from .inifile import IniFile

ASI_NAME = "decky_optiscaler_live.asi"
# The folder OptiScaler loads ASIs from, in its own folder (layout.plugin_dir):
# OptiScaler/plugins, or plugins/ in the game folder for an install from
# before OptiScaler/.
PLUGIN_SUBDIR = layout.PLUGINS
CMD_FILE = "decky_optiscaler_live.cmd"
STATUS_FILE = "decky_optiscaler_live.status"
LOG_FILE = "decky_optiscaler_live.log"
DUMP_FILE = "decky_optiscaler_live.dump"
# The status-file format this module reads (asi/live.cpp STATUS_SCHEMA).
STATUS_SCHEMA = 8

# A status file older than this means the ASI is not running - the game exited,
# or it never loaded. The ASI heartbeats every few seconds precisely so that
# this stays a statement about the game and not about whether anyone happened to
# change a setting recently.
STATUS_STALE_SECONDS = 20

# The FFX frame generators OptiScaler can be told to use, and the INI key that
# records the choice. The list itself is not fixed: the FFX SDK reports what it
# can offer to each game, so the running game is the authority and the ASI reads
# it out of State. These are only what the shipped reference INI documents, used
# when nothing is attached to ask.
FFX_FG_SECTION = "FSR"
FFX_FG_KEY = "FGIndex"
FFX_FG_ID = f"{FFX_FG_SECTION}.{FFX_FG_KEY}"
# An index has to be one the game actually reports; OptiScaler clamps anything
# else to 0, which would quietly select a generator nobody asked for.
FFX_FG_MAX = 16

# Which version of FSR the FidelityFX backend runs -- the overlay's "FFX
# Upscaler" combo, and a different question from which backend runs at all. The
# same caveats apply: the list comes from the running game, and an index it does
# not have is clamped rather than refused, so the ceiling here is a bound and
# not a menu.
FFX_UPSCALER_SECTION = "FSR"
FFX_UPSCALER_KEY = "UpscalerIndex"
FFX_UPSCALER_ID = f"{FFX_UPSCALER_SECTION}.{FFX_UPSCALER_KEY}"
FFX_UPSCALER_MAX = FFX_FG_MAX

class LiveField:
    """One INI key the ASI can apply without a restart."""

    def __init__(self, member, wire_type, encode, needs_reinit=False, resettable=False, on_by_key=None):
        self.member = member
        self.wire_type = wire_type
        self.encode = encode
        # Turning it on takes OptiScaler's own shortcut ([Menu] <on_by_key>):
        # its menu pairs the change with a State flag the plugin API does not
        # reach, and pressing the key runs exactly that code (service.py).
        self.on_by_key = on_by_key
        # "auto" in the INI is an unset value; for these the ASI unsets it
        # (the overlay's "Auto"), instead of leaving it for the next launch.
        self.resettable = resettable
        # Read when the upscaler is created rather than every frame: the write
        # is followed by OptiPluginApi::ReInitUpscalers, which makes OptiScaler
        # re-create the upscaler with the new value on its next frame.
        self.needs_reinit = needs_reinit


def _bool(value):
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return "1"
    if text in ("0", "false", "no", "off"):
        return "0"
    raise ValueError(f"not a bool: {value!r}")


def _int(value):
    return str(int(float(str(value).strip())))


def _float(value):
    return repr(float(str(value).strip()))


def _index(limit, what):
    def encode(value):
        index = int(float(str(value).strip()))
        if index < 0 or index >= limit:
            raise ValueError(f"not {what}: {value!r}")
        return str(index)
    return encode


def _fsr4_model(value):
    """[FSR] Fsr4Model: 0 FP8, 1 INT8, 2 INT8 RDNA2, 3 INT8 Lite."""
    model = int(float(str(value).strip()))
    if model < 0 or model > 3:
        raise ValueError(f"not an FSR 4 model: {value!r}")
    return str(model)


# OptiScaler's FPS overlay types ([Menu] FpsOverlayType), in its own order and
# with its own names (menu_common.cpp fpsType[]), from the generated map.
_GENERATED = json.loads(Path(__file__).with_name("live_fields.json").read_text(encoding="utf-8"))
FPS_OVERLAY_TYPES = _GENERATED.get("labels", {}).get("FpsOverlay", [])
# The type OptiScaler shows when the ini says "auto" (Config.h's default).
FPS_OVERLAY_DEFAULT = int(_GENERATED.get("defaults", {}).get("Menu.FpsOverlayType", "0"))


def _fps_overlay_type(value):
    index = int(float(str(value).strip()))
    if index < 0 or index >= len(FPS_OVERLAY_TYPES):
        raise ValueError(f"not an FPS overlay type: {value!r}")
    return str(index)


def _is_auto(value):
    return str(value).strip().lower() in ("auto", "")


def _enum_code(enum):
    """An ini token of one of OptiScaler's enums ("opti") as the number Config holds.

    The tokens are OptiScaler's own (EnumConfig<...> in OptiTypes.h, synced at
    the bundled dll's commit and generated into live_fields.json), so a token
    another nightly drops or adds follows it.
    """
    def encode(value):
        codes = _GENERATED.get("enums", {}).get(enum, {}).get("codes", {})
        text = str(value).strip().lower()
        for number, code in codes.items():
            if code.lower() == text:
                return str(int(number))
        raise ValueError(f"not a {enum} token: {value!r}")
    return encode


def _ffx_upscaler_index(value):
    """An index into the FFX upscaler versions OptiScaler offers."""
    index = int(float(str(value).strip()))
    if index < 0 or index >= FFX_UPSCALER_MAX:
        raise ValueError(f"not an FFX upscaler index: {value!r}")
    return str(index)


# Only settings OptiScaler genuinely re-reads while running belong here.
# Anything consumed once, when a feature or swapchain is created, would appear
# to work and silently do nothing, so it keeps its "restart to apply" treatment.
# Not here on purpose: the upscaler backends (Dx11/Dx12/VulkanUpscaler) and the
# FFX frame generator (FSR.FGIndex). Switching those in a running game takes
# OptiScaler's State internals, which change between nightlies; OptiScaler's own
# overlay does it, and the panel records them for the next launch.
#
# What OptiScaler's own menu does next to each write is the reference, and
# scripts/selftest.py (check_live_fields_match_optiscalers_menu) holds every
# entry to it in the synced menu_common.cpp:
LIVE_FIELDS = {
    # Frame generation. Off is the write alone, exactly as OptiScaler's FG key
    # does it: every FG path checks it each frame. On also needs State's
    # fgChanged, which (re)activates the generator for a game-fed input (FSR
    # FG, DLSSG, XeFG), so the panel presses OptiScaler's FG key for that.
    "FrameGen.Enabled": LiveField("FGEnabled", "bool", _bool, on_by_key="FGShortcutKey"),
    # Read per frame where the frame is generated (FSR FG and XeFG dispatch).
    "FrameGen.DebugView": LiveField("FGDebugView", "bool", _bool),
    "XeFG.DebugView": LiveField("FGXeFGDebugView", "bool", _bool),
    "OptiFG.MakeDepthCopy": LiveField("FGMakeDepthCopy", "bool", _bool),
    "OptiFG.HUDFixExtended": LiveField("FGHUDFixExtended", "bool", _bool),
    # XeFG applies it per frame (SetNumInterpolatedFrames); the menu's extra
    # fgChanged only pauses the generator for a few frames.
    "XeFG.InterpolationCount": LiveField("FGXeFGInterpolationCount", "i32", _int),
    # Dynamic MFG reads it every present (XeFG_DynamicMFG::Update) and the menu's
    # checkbox writes nothing else; off, the next present is back at the count.
    "XeFG.DynamicMFG": LiveField("FGXeFGDynamicMFG", "bool", _bool),
    # Not here: [OptiFG] HUDFix. On the FSR FG and XeFG outputs OptiScaler's
    # menu also clears the captured HUDless frames and rebuilds the generator
    # (State), so it waits for a restart or OptiScaler's menu.

    # Which FSR version the FidelityFX upscaler runs, read when it creates its
    # context: written, then the upscaler is re-created through the plugin API.
    FFX_UPSCALER_ID: LiveField("FfxUpscalerIndex", "i32", _ffx_upscaler_index,
                               needs_reinit=True),

    # Also read when the upscaler is created, and switched by OptiScaler's own
    # overlay exactly this way: the value written, then the upscaler rebuilt on
    # the same backend (menu_common.cpp: the "FSR4 Model" combo and the Init
    # Flags checkboxes call ReInitUpscaler, which is what ReInitUpscalers asks).
    "FSR.Fsr4Model": LiveField("Fsr4Model", "u8", _fsr4_model, needs_reinit=True, resettable=True),
    "InitFlags.AutoExposure": LiveField("AutoExposure", "bool", _bool, needs_reinit=True, resettable=True),
    "InitFlags.HDR": LiveField("HDR", "bool", _bool, needs_reinit=True, resettable=True),
    "InitFlags.DepthInverted": LiveField("DepthInverted", "bool", _bool, needs_reinit=True, resettable=True),
    "InitFlags.JitterCancellation": LiveField("JitterCancellation", "bool", _bool,
                                              needs_reinit=True, resettable=True),
    "InitFlags.DisplayResolution": LiveField("DisplayResolution", "bool", _bool,
                                             needs_reinit=True, resettable=True),
    "InitFlags.DynamicResolution": LiveField("DynamicResolution", "bool", _bool,
                                             needs_reinit=True, resettable=True),
    # The model each other backend runs, also switched by OptiScaler's menu as a
    # write and a rebuild on the same backend: XeSS's "Network Models" combo,
    # and DLSS's render preset override with its "Apply Changes" button.
    "XeSS.NetworkModel": LiveField("NetworkModel", "i32", _index(6, "an XeSS network model"),
                                   needs_reinit=True, resettable=True),
    "DLSS.RenderPresetOverride": LiveField("RenderPresetOverride", "bool", _bool,
                                           needs_reinit=True, resettable=True),
    "DLSS.RenderPresetForAll": LiveField("RenderPresetForAll", "u32", _index(16, "a DLSS render preset"),
                                         needs_reinit=True, resettable=True),

    # The override is read per frame, except by DLSS before 3, which OptiScaler's
    # menu rebuilds for it; rebuilding always is the same thing, one frame later.
    "Sharpness.OverrideSharpness": LiveField("OverrideSharpness", "bool", _bool, needs_reinit=True),
    "Sharpness.Sharpness": LiveField("Sharpness", "f32", _float),
    # OptiScaler's own sharpener, the menu's "Method" combo and the OptiSharpen
    # model under it, all plain writes there. The upscaler's Evaluate reads
    # RcasEnabled every frame (IFeature_Dx12/Dx11/Vk; the RCAS pass always
    # exists), and RCAS_Dx12/Dx11/Vk::Dispatch read the shader and the model at
    # every dispatch, creating a neural shader the first time it runs.
    "CAS.Enabled": LiveField("RcasEnabled", "bool", _bool),
    "Sharpness.Shader": LiveField("SharpnessShader", "i32", _enum_code("SharpenShader")),
    "CAS.OptiSharpenModel": LiveField("OptiSharpenModel", "i32", _index(4, "an OptiSharpen model"),
                                      resettable=True),
    # The FPS overlay: exactly what OptiScaler's FPS and FPS-cycle keys write.
    "Menu.ShowFps": LiveField("ShowFps", "bool", _bool),
    "Menu.FpsOverlayType": LiveField("FpsOverlayType", "u32", _fps_overlay_type, resettable=True),
}


def is_live_key(section, key):
    return f"{section}.{key}" in LIVE_FIELDS


def plugin_dir(target_dir):
    """Where OptiScaler loads ASIs from in this game (OptiScaler 10's rule)."""
    return layout.plugin_dir(target_dir)


def asi_path(target_dir):
    return plugin_dir(target_dir) / ASI_NAME


def _control_path(target_dir, name):
    return Path(target_dir) / name


def installed(target_dir):
    return asi_path(target_dir).is_file()


# (size, mtime) of the shipped plugin -> its bytes, so the comparison below does
# not re-read a 160 KB file on every poll of the live status.
_shipped_asi = {}


def asi_current(target_dir, source):
    """Whether the game's copy of the in-game plugin is the one this build ships.

    Updating the Decky plugin does not touch any game: the ASI was copied into
    the game's ``plugins`` folder at set-up time and stays at whatever version
    it was then. That is invisible from the panel -- an old plugin attaches,
    heartbeats and answers, it simply does not report the fields that did not
    exist yet -- so a feature added on this side appears to be missing entirely
    rather than out of date. Returns None when there is nothing to compare.
    """
    installed_path = asi_path(target_dir)
    if not source:
        return None
    try:
        source_path = Path(source)
        stat = source_path.stat()
        key = (str(source_path), stat.st_size, stat.st_mtime_ns)
        shipped = _shipped_asi.get(key)
        if shipped is None:
            shipped = source_path.read_bytes()
            _shipped_asi.clear()
            _shipped_asi[key] = shipped
        if installed_path.stat().st_size != len(shipped):
            return False
        return installed_path.read_bytes() == shipped
    except OSError:
        return None


def install_plugin_asi(target_dir, source, name, logger=None, folder=None):
    """Drop an ASI into the game's OptiScaler plugin folder (or `folder`).

    OptiScaler loads every .asi in this folder once ``LoadAsiPlugins`` is on,
    which is how both our live-control module and OptiPatcher get in.
    """
    source = Path(source)
    if not source.is_file():
        return {"ok": False, "error": f"{name} missing at {source}"}
    destination = Path(folder or plugin_dir(target_dir)) / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())
    if logger:
        logger.info("installed %s at %s", name, destination)
    return {"ok": True, "path": str(destination)}


def install_asi(target_dir, source, logger=None):
    """Drop the live-control ASI into the game's OptiScaler plugin folder."""
    return install_plugin_asi(target_dir, source, ASI_NAME, logger)


def remove_asi(target_dir, logger=None):
    """Remove the ASI and every file the live channel created."""
    removed = []
    # Also where an install from before OptiScaler/ had it.
    legacy = Path(target_dir) / PLUGIN_SUBDIR
    for path in (
        asi_path(target_dir),
        legacy / ASI_NAME,
        _control_path(target_dir, CMD_FILE),
        _control_path(target_dir, STATUS_FILE),
        _control_path(target_dir, LOG_FILE),
        _control_path(target_dir, DUMP_FILE),
    ):
        try:
            if path.is_file():
                path.unlink()
                removed.append(path.name)
        except OSError as exc:
            if logger:
                logger.warning("could not remove %s: %s", path, exc)

    # Only clean the plugins folder up if we are the ones who created it.
    for folder in {plugin_dir(target_dir), legacy}:
        try:
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
        except OSError:
            pass
    return {"ok": True, "removed": removed}


def ini_requirements():
    """INI settings OptiScaler needs before it will load the ASI at all.

    Only the switch. ``Plugins.Path`` is deliberately left alone: OptiScaler
    already defaults it to ``OptiScaler/plugins`` (its own folder's), where
    the installer puts the ASI.
    """
    return [
        {"section": "Plugins", "key": "LoadAsiPlugins", "value": "true"},
    ]


def load_enabled(target_dir):
    """Whether OptiScaler.ini actually switches ASI loading on.

    It defaults to *false*, so an install that predates this plugin - or one
    whose ini was replaced - loads nothing at all, silently.
    """
    ini = Path(target_dir) / "OptiScaler.ini"
    if not ini.is_file():
        return None
    try:
        text = ini.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    section = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section != "Plugins" or "=" not in line or line.startswith((";", "#")):
            continue
        key, _, value = line.partition("=")
        if key.strip().lower() == "loadasiplugins":
            return value.strip().lower() == "true"
    return False


def loaded_by_optiscaler(target_dir):
    """Whether OptiScaler's own log says it loaded our plugin this run.

    This is the one unambiguous answer to "is it even being loaded?" - the
    difference between a missing switch and a plugin that loaded but could not
    find what it needed in memory.
    """
    log = Path(target_dir) / "OptiScaler.log"
    if not log.is_file():
        return None
    try:
        # The line we want is written during startup, so the head is enough.
        with log.open("r", encoding="utf-8", errors="replace") as handle:
            head = handle.read(400_000)
    except OSError:
        return None
    if ASI_NAME.lower() in head.lower():
        return True
    # It logs "Checking <path> for *.asi" at debug level; seeing that without
    # our name means the folder was searched and we were not in it.
    return False if "for *.asi" in head else None


def _parse_status(text):
    out = {}
    for line in text.splitlines():
        parts = line.strip().split(" ", 1)
        if not parts[0]:
            continue
        out[parts[0]] = parts[1].strip() if len(parts) > 1 else ""
    return out


def _number(text, cast, unknown=None):
    """One numeric status line, or None when it is missing or unreadable.

    The plugin writes -1 for "could not read this", which is not the same
    statement as 0 and must not be shown as one.
    """
    if text in (None, ""):
        return None
    try:
        value = cast(text)
    except (TypeError, ValueError):
        return None
    return None if value == unknown else value


def status(target_dir, source=None):
    """What the in-game side is doing right now."""
    path = _control_path(target_dir, STATUS_FILE)
    report = {
        "asi_installed": installed(target_dir),
        # False when the game holds an older build of the in-game plugin than
        # this one ships, which is what an updated Decky plugin leaves behind.
        "asi_current": asi_current(target_dir, source),
        "load_enabled": load_enabled(target_dir),
        "loaded_by_optiscaler": loaded_by_optiscaler(target_dir),
        "attached": False,
        "ready": False,
        "state": "absent",
        "error": None,
        "seq": None,
        # Which status-file format the in-game plugin speaks; anything but
        # STATUS_SCHEMA is a plugin from another build of this one.
        "schema": None,
        # OptiScaler's plugin API version (0: it never called OptiPluginInit).
        "api": None,
        # The OptiScaler commit the in-game plugin was compiled for.
        "built_for": None,
        # Whether settings read at upscaler creation can be applied live: needs
        # OptiPluginApi::ReInitUpscalers (plugin API 2 and later).
        "can_reinit": False,
        "age": None,
        # Where the in-game side decided its control files live. It should be
        # target_dir; anything else means the plugin is writing where nothing is
        # reading, which is exactly how it used to fail.
        "dir": None,
        "dir_matches": None,
        # The FSR version is rebuilt through ReInitUpscalers, like can_reinit.
        "can_change_ffx_upscaler": False,
    }
    if not path.is_file():
        return report
    try:
        raw = _parse_status(path.read_text(encoding="utf-8", errors="replace"))
        age = time.time() - path.stat().st_mtime
    except OSError as exc:
        report["error"] = str(exc)
        return report

    report["age"] = round(age, 1)
    report["state"] = raw.get("status", "unknown")
    report["error"] = raw.get("error") or None
    report["seq"] = raw.get("seq")
    report["schema"] = _number(raw.get("schema"), int)
    report["api"] = _number(raw.get("api"), int)
    report["built_for"] = raw.get("built_for") or None
    fresh = age < STATUS_STALE_SECONDS
    # "loaded"/"searching" mean the plugin is running but has not found Config
    # yet; "failed" means it gave up (its layout does not match this build).
    report["attached"] = report["state"] in ("ready", "loaded", "searching") and fresh
    report["ready"] = report["state"] == "ready" and fresh \
        and report["schema"] == STATUS_SCHEMA
    report["can_reinit"] = report["ready"] and raw.get("reinit_available") == "1"
    report["can_change_ffx_upscaler"] = report["can_reinit"]
    reported_dir = raw.get("dir") or None
    report["dir"] = reported_dir
    if reported_dir:
        report["dir_matches"] = _same_dir(reported_dir, target_dir)
    return report


def _same_dir(windows_path, target_dir):
    """Whether a path the ASI reported is the folder we are managing.

    The ASI reports a Windows path, and Proton's drive letters are what make
    this more than a string compare. ``Z:`` is the filesystem root, so
    ``Z:\\home\\deck\\...`` shares a tail with the Linux path outright -- but a
    library on the SD card is mounted as its own letter, and
    ``S:\\steamapps\\common\\...`` shares no tail at all with
    ``/run/media/.../steamapps/common/...``. That reported a perfectly healthy
    install as "writing to a different folder", right next to the live readings
    it was writing correctly.

    So the letter is dropped and what is left is compared component by
    component, from the end. Whole components only: a shared suffix in the
    middle of a folder name is not a shared path.
    """
    def parts(text):
        text = str(text).replace("\\", "/").strip().rstrip("/")
        # "S:/steamapps/..." and bare "S:" alike lose the letter.
        text = re.sub(r"^[A-Za-z]:", "", text)
        return [part.lower() for part in text.split("/") if part]

    theirs, ours = parts(windows_path), parts(target_dir)
    if not theirs or not ours:
        return None
    depth = min(len(theirs), len(ours))
    # One shared folder name is a coincidence; "Win64" is every game's.
    if depth < 2:
        return None
    return theirs[-depth:] == ours[-depth:]


# A ready ASI applies a command within its 100 ms poll. One still unapplied
# after this long is not waiting for this game: it is an earlier session's, and
# carrying it into a new command would replay it over what the ini says now.
PENDING_MAX_AGE_MS = 5000

_SEND_LOCKS = {}
_SEND_LOCKS_GUARD = threading.Lock()


def _send_lock(path):
    with _SEND_LOCKS_GUARD:
        return _SEND_LOCKS.setdefault(str(path), threading.Lock())


def _unconsumed(path, target_dir):
    """(seq, command lines) of a command file the ASI has not applied yet."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, []
    head = re.match(r"seq (\d+)", text)
    if not head:
        return None, []
    seq = int(head.group(1))
    applied = _read_status_seq(target_dir)
    if applied is not None and applied == seq:
        return seq, []
    if time.time_ns() // 1_000_000 - seq > PENDING_MAX_AGE_MS:
        return seq, []
    return seq, [line.strip() for line in text.splitlines()[1:] if line.strip()]


def _read_status_seq(target_dir):
    try:
        raw = _parse_status(_control_path(target_dir, STATUS_FILE).read_text(
            encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return _number(raw.get("seq"), int)


def _merge(pending, lines):
    """One command holding both: a later set/unset of a member replaces an earlier one.

    The ASI applies reinit and dump after every write in the file, whatever
    their position, so each is kept once, at the end.
    """
    by_member, order, flags = {}, [], []
    for line in [*pending, *lines]:
        parts = line.split()
        if parts and parts[0] in ("set", "unset") and len(parts) >= 2:
            if parts[1] not in by_member:
                order.append(parts[1])
            by_member[parts[1]] = line
        elif parts and line not in flags:
            flags.append(line)
    return [by_member[member] for member in order] + flags


def _send(target_dir, lines):
    """Write a command, atomically, under a new sequence number.

    The ASI polls the command file every 100 ms and applies only the newest one,
    so a command it has not applied yet (its status still names an older seq)
    is carried into this one rather than overwritten: two edits in quick
    succession used to leave only the second, with both reported as sent.
    """
    path = _control_path(target_dir, CMD_FILE)
    with _send_lock(path):
        previous, pending = _unconsumed(path, target_dir)
        # Strictly increasing: the ASI skips a file whose seq it already applied.
        seq = max(time.time_ns() // 1_000_000, (previous or 0) + 1)
        body = "\n".join([f"seq {seq}", *_merge(pending, lines), ""])
        # Write-then-replace so the ASI never reads a half-written command file;
        # a name of its own, so two writers never replace each other's.
        temp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            temp.write_text(body, encoding="utf-8")
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    return seq


def apply_changes(target_dir, changes, can_reinit=True):
    """Ask the running game to adopt these INI changes immediately.

    ``changes`` are the same {section, key, value} dicts the config writer uses.
    ``can_reinit`` is whether the running OptiScaler can re-create its upscaler
    (status()["can_reinit"]); without it, settings that need that are deferred.
    Returns which ones were sent and which need a restart regardless.
    """
    live, deferred, keys = [], [], []
    reinit = False
    for change in changes:
        ini_key = f"{change['section']}.{change['key']}"
        field = LIVE_FIELDS.get(ini_key)
        if field is None or (field.needs_reinit and not can_reinit):
            deferred.append(ini_key)
            continue
        if field.on_by_key and str(change["value"]).strip().lower() in ("true", "1", "yes", "on"):
            # Turned on by OptiScaler's own key; the caller presses it.
            keys.append({"ini_key": ini_key, "shortcut": field.on_by_key})
            continue
        if field.resettable and _is_auto(change["value"]):
            live.append(f"unset {field.member}")
            reinit = reinit or field.needs_reinit
            continue
        try:
            encoded = field.encode(change["value"])
        except (TypeError, ValueError):
            # "auto" and the like: OptiScaler resolves those at startup.
            deferred.append(ini_key)
            continue
        live.append(f"set {field.member} {field.wire_type} {encoded}")
        reinit = reinit or field.needs_reinit

    if not live:
        return {"ok": True, "applied": [], "deferred": deferred, "sent": False, "keys": keys}
    if reinit:
        # After the writes: the re-created upscaler reads the new values.
        live.append("reinit")
    try:
        seq = _send(target_dir, live)
    except OSError as exc:
        return {"ok": False, "error": str(exc), "applied": [], "deferred": deferred, "sent": False,
                "keys": keys}
    return {
        "ok": True,
        "sent": True,
        "seq": seq,
        "applied": [line.split(" ")[1] for line in live if line.startswith(("set ", "unset "))],
        "deferred": deferred,
        "reinit": reinit,
        "keys": keys,
    }


def request_dump(target_dir):
    """Ask the ASI to write every Config field it can see to DUMP_FILE."""
    return _send(target_dir, ["dump"])


def read_dump(target_dir):
    """{member: (kind, engaged, value, default, ini)} from the last dump."""
    path = _control_path(target_dir, DUMP_FILE)
    fields = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) in (6, 7):  # the 7th is the volatile flag (schema 8)
            name, kind, engaged, value, default, ini = parts[:6]
            fields[name] = (kind, engaged == "1", value, default, ini)
        elif len(parts) == 2 and parts[0] != "fields":
            fields[parts[0]] = (parts[1], None, None, None, None)
    return fields


def log_tail(target_dir, lines=40):
    path = _control_path(target_dir, LOG_FILE)
    if not path.is_file():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except OSError:
        return []


# ------------------------------------------------------------ diagnostics --

_BANNER_RE = re.compile(r"OptiScaler v([0-9][\w.\-]*) \(([0-9a-f]{7,40})\)(?: \((\d{8}_\d{6})\))?")
# Frame generation and upscaler (re)creation: whether a live change took
# effect shows here, not in the plugin's own lines.
_EFFECT_RE = re.compile(r"FrameGen|FGEnabled|[^a-z]FG[A-Z_ :]|XeFG|xefg|FSRFG|fsrfg|FfxFG|"
                        r"SwapChain|swapchain|TryCreateOptiFeature|Creating .* feature|ReInit|"
                        r"ReleaseFeature|changeBackend|Interpolat")
# Streamline logs its own "plugins" by the hundred; they are not ours.
_STREAMLINE_RE = re.compile(r"streamlineLogCallback")
_RELEVANT_RE = re.compile(r"plugin|\.asi|LoadAsiPlugins|decky_optiscaler|OptiPluginInit|\[E\]|error",
                          re.IGNORECASE)


def _read_text(path, limit=2_000_000):
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return None


def _age(path):
    try:
        seconds = time.time() - Path(path).stat().st_mtime
    except OSError:
        return None
    if seconds < 90:
        return f"{seconds:.0f} s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min ago"
    return f"{seconds / 3600:.1f} h ago"


def running_build(target_dir):
    """(version, commit, built) from the banner in the game's OptiScaler.log."""
    text = _read_text(Path(target_dir) / "OptiScaler.log", 400_000)
    match = _BANNER_RE.search(text or "")
    return match.groups() if match else None


def _ini_setting(target_dir, section, key):
    """A value OptiScaler.ini sets for itself (not "auto"), or None."""
    try:
        values = IniFile(Path(target_dir) / "OptiScaler.ini").to_dict()
    except OSError:
        return None
    value = str(values.get(section, {}).get(key) or "").strip()
    return None if value.lower() in ("", "auto") else value


def _ini_folder(target_dir, value):
    """A folder an ini path names, as OptiScaler resolves it (relative: to its dll's folder)."""
    text = value.strip().strip('"').replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", text) or text.startswith("/"):
        return None  # a Windows path from inside the prefix: not checked from here
    return Path(target_dir) / text


def folder_facts(target_dir):
    """[label, value, tone]: where OptiScaler looks for its files here, if not where they are.

    OptiScaler reads its DLLs from OptiScaler/ (the game folder for an install
    from before it) and ASIs from its plugins/, unless [Libraries] OptiDllPath
    or [Plugins] Path in the ini say otherwise; this plugin never writes those.
    """
    target = Path(target_dir)
    facts = []
    opti = layout.opti_dir(target)
    if opti == target:
        facts.append(["OptiScaler's files", "in the game folder (an install from before "
                      f"{OPTI_SUBDIR}/): Update OptiScaler in this game moves them", "warn"])
    else:
        facts.append(["OptiScaler's files", f"{OPTI_SUBDIR}/", "ok"])
    dll_path = _ini_setting(target, "Libraries", "OptiDllPath")
    if dll_path:
        facts.append(["[Libraries] OptiDllPath", f"{dll_path}: OptiScaler reads its DLLs there, not from "
                      f"{layout.relative(target, opti) + '/' if opti != target else 'the game folder'}", "bad"])
    plugin_path = _ini_setting(target, "Plugins", "Path")
    named = _ini_folder(target, plugin_path) if plugin_path else None
    if plugin_path:
        elsewhere = named is None or named.is_dir()
        facts.append(["[Plugins] Path", f"{plugin_path}: " + (
            f"OptiScaler loads ASIs from there, not from {layout.relative(target, plugin_dir(target))}/"
            if elsewhere else f"no such folder here, so OptiScaler uses {layout.relative(target, plugin_dir(target))}/"),
            "bad" if elsewhere else "warn"])
    legacy = target / PLUGIN_SUBDIR
    if opti != target and legacy.is_dir() and (named is None or named.resolve() != legacy.resolve()):
        strays = sorted(p.name for p in legacy.iterdir() if p.is_file() and p.suffix.lower() == ".asi")
        if strays:
            facts.append([f"{PLUGIN_SUBDIR}/ in the game folder", f"{', '.join(strays)}: OptiScaler 10 loads ASIs "
                          f"from {OPTI_SUBDIR}/{PLUGIN_SUBDIR}/, not from here (move them if they are "
                          "OptiScaler's; another ASI loader may use this folder)", "warn"])
    return facts


def diagnostics(target_dir, source=None, install=None):
    """Everything that decides whether live editing works in this game.

    ``install`` is installer.detect() for the folder (version, filename). Returns
    {"facts": [[label, value, tone]], "report": text}; tone is "ok", "warn" or
    "bad" for the panel to colour, the report carries the raw files as well.
    """
    target = Path(target_dir)
    install = install or {}
    st = status(target_dir, source)
    facts = []

    def fact(label, value, tone="ok"):
        facts.append([label, str(value), tone])

    bundled = install.get("bundled_version")
    version = install.get("version")
    fact("OptiScaler installed here", f"{version or 'unknown'}"
         + ("" if not bundled or version == bundled else f" (this plugin ships {bundled})"),
         "ok" if version and version == bundled else "warn")

    log_path = target / "OptiScaler.log"
    running = running_build(target_dir)
    if running:
        fact("OptiScaler that last ran", f"v{running[0]} ({running[1]})"
             + (f", built {running[2]}" if running[2] else "") + f", log {_age(log_path)}")
    else:
        fact("OptiScaler that last ran", "no OptiScaler.log here (it has not run since install, "
             "or LogToFile is off)" if not log_path.is_file() else "no version banner in OptiScaler.log",
             "warn")

    built_for = st.get("built_for")
    if built_for and running:
        same = running[1].startswith(built_for[:7]) or built_for.startswith(running[1][:7])
        fact("In-game plugin built for", built_for + ("" if same else
             f" — but the game ran {running[1]}: restart the game, or Update OptiScaler in this game"),
             "ok" if same else "bad")
    elif built_for:
        fact("In-game plugin built for", built_for)

    asi = asi_path(target)
    shown = layout.relative(target, asi)
    if asi.is_file():
        current = st.get("asi_current")
        fact("In-game plugin file", f"{shown}, {asi.stat().st_size} bytes"
             + ("" if current is not False else " — older than this plugin's: Update OptiScaler in this game"),
             "ok" if current is not False else "bad")
    else:
        fact("In-game plugin file", f"missing ({shown}): Update OptiScaler in this game", "bad")

    for label, value, tone in folder_facts(target):
        fact(label, value, tone)

    enabled = st.get("load_enabled")
    fact("LoadAsiPlugins in OptiScaler.ini", {True: "true", False: "off: OptiScaler skips plugins",
                                              None: "not set (OptiScaler's default: off)"}[enabled],
         "ok" if enabled else "bad")
    loaded = st.get("loaded_by_optiscaler")
    fact("OptiScaler loaded it", {True: "yes (OptiScaler.log)", False: "no: searched plugins/ without it",
                                  None: "unknown (nothing in OptiScaler.log)"}[loaded],
         "ok" if loaded else ("bad" if loaded is False else "warn"))

    state = st.get("state")
    tone = "ok" if st.get("ready") else ("warn" if state in ("loaded", "searching", "absent") else "bad")
    status_bits = [state or "absent"]
    if st.get("age") is not None:
        status_bits.append(f"written {st['age']} s ago" + (" (stale: game not running?)"
                                                           if st["age"] >= STATUS_STALE_SECONDS else ""))
    if st.get("api") is not None:
        status_bits.append(f"plugin API {st['api']}")
    if st.get("schema") not in (None, STATUS_SCHEMA):
        status_bits.append(f"status format {st['schema']} (another build)")
    fact("In-game plugin status", ", ".join(status_bits), tone)
    if st.get("error"):
        fact("Its last error", st["error"], "bad")
    if st.get("dir_matches") is False:
        fact("Reports from", f"{st['dir']} (not this folder)", "bad")

    cmd = target / CMD_FILE
    if cmd.is_file():
        sent = ((_read_text(cmd) or "").split() + ["", "?"])[1]
        applied = st.get("seq")
        fact("Last command from the panel", f"seq {sent}, sent {_age(cmd)}; the in-game plugin "
             f"last applied seq {applied or 'none yet'}", "ok" if applied == sent else "warn")

    def section(title, body):
        return f"\n===== {title} =====\n{(body or '(none)').rstrip()}\n"

    ini_text = _read_text(target / "OptiScaler.ini") or ""
    plugins_ini = "\n".join(line for line in ini_text.splitlines()
                            if re.match(r"\s*(\[Plugins\]|\[Log\]|LoadAsiPlugins|Path|LogToFile|LogLevel)", line))
    log_text = _read_text(log_path) or ""
    log_lines = log_text.splitlines()
    relevant = [line for line in log_lines
                if _RELEVANT_RE.search(line) and not _STREAMLINE_RE.search(line)][:120]
    effects = [line for line in log_lines
               if _EFFECT_RE.search(line) and not _STREAMLINE_RE.search(line)]
    effects = effects[:60] + (["..."] if len(effects) > 260 else []) + effects[60:][-200:]
    plugins_dir = plugin_dir(target)
    listing = "\n".join(f"{p.name}  {p.stat().st_size} bytes" for p in sorted(plugins_dir.iterdir())) \
        if plugins_dir.is_dir() else f"(no {layout.relative(target, plugins_dir)}/ folder)"

    report = [f"Decky OptiScaler Nightly live-control report, {time.strftime('%Y-%m-%d %H:%M:%S')}",
              f"Game folder: {target}", ""]
    report += [f"[{tone:>4}] {label}: {value}" for label, value, tone in facts]
    report.append(section(STATUS_FILE, _read_text(target / STATUS_FILE)))
    report.append(section(CMD_FILE, _read_text(cmd)))
    report.append(section(LOG_FILE + " (last 80 lines)", "\n".join(log_tail(target_dir, 80))))
    report.append(section(f"{layout.relative(target, plugins_dir)}/", listing))
    report.append(section("OptiScaler.ini [Plugins]/[Log]", plugins_ini))
    report.append(section(f"OptiScaler.log: plugin lines ({len(relevant)})", "\n".join(relevant)))
    report.append(section("OptiScaler.log: frame generation and upscaler lines", "\n".join(effects)))
    report.append(section("OptiScaler.log: first 40 lines", "\n".join(log_lines[:40])))
    report.append(section("OptiScaler.log: last 80 lines", "\n".join(log_lines[-80:])))
    return {"facts": facts, "report": "\n".join(report), "status": st}
