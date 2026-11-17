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

import os
import re
import time
from pathlib import Path

from .constants import BACKUP_DIR

ASI_NAME = "decky_optiscaler_live.asi"
PLUGIN_SUBDIR = "plugins"
CMD_FILE = "decky_optiscaler_live.cmd"
STATUS_FILE = "decky_optiscaler_live.status"
LOG_FILE = "decky_optiscaler_live.log"
DUMP_FILE = "decky_optiscaler_live.dump"
# The status-file format this module reads (asi/live.cpp STATUS_SCHEMA).
STATUS_SCHEMA = 6

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

    def __init__(self, member, wire_type, encode, needs_reinit=False):
        self.member = member
        self.wire_type = wire_type
        self.encode = encode
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
LIVE_FIELDS = {
    # Frame generation: FGEnabled is checked per frame by the swapchain and by
    # every FG dispatch path, which is exactly why the overlay's hotkey works.
    "FrameGen.Enabled": LiveField("FGEnabled", "bool", _bool),
    "OptiFG.HUDFix": LiveField("FGHUDFix", "bool", _bool),
    "FrameGen.DebugView": LiveField("FGDebugView", "bool", _bool),
    "OptiFG.MakeDepthCopy": LiveField("FGMakeDepthCopy", "bool", _bool),
    "OptiFG.HUDFixExtended": LiveField("FGHUDFixExtended", "bool", _bool),
    "XeFG.InterpolationCount": LiveField("FGXeFGInterpolationCount", "i32", _int),

    # Which FSR version the FidelityFX upscaler runs, read when it creates its
    # context: written, then the upscaler is re-created through the plugin API.
    FFX_UPSCALER_ID: LiveField("FfxUpscalerIndex", "i32", _ffx_upscaler_index,
                               needs_reinit=True),

    # Cheap per-frame knobs, safe to move while playing.
    "Sharpness.OverrideSharpness": LiveField("OverrideSharpness", "bool", _bool),
    "Sharpness.Sharpness": LiveField("Sharpness", "f32", _float),
    "Menu.ShowFps": LiveField("ShowFps", "bool", _bool),
}


def is_live_key(section, key):
    return f"{section}.{key}" in LIVE_FIELDS


def asi_path(target_dir):
    return Path(target_dir) / PLUGIN_SUBDIR / ASI_NAME


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


def install_plugin_asi(target_dir, source, name, logger=None):
    """Drop an ASI into the game's OptiScaler plugin folder.

    OptiScaler loads every .asi in this folder once ``LoadAsiPlugins`` is on,
    which is how both our live-control module and OptiPatcher get in.
    """
    source = Path(source)
    if not source.is_file():
        return {"ok": False, "error": f"{name} missing at {source}"}
    destination = Path(target_dir) / PLUGIN_SUBDIR / name
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
    for path in (
        asi_path(target_dir),
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
    plugin_dir = Path(target_dir) / PLUGIN_SUBDIR
    try:
        if plugin_dir.is_dir() and not any(plugin_dir.iterdir()):
            plugin_dir.rmdir()
    except OSError:
        pass
    return {"ok": True, "removed": removed}


def ini_requirements():
    """INI settings OptiScaler needs before it will load the ASI at all.

    Only the switch. ``Plugins.Path`` is deliberately left alone: OptiScaler
    already defaults it to ``<dll folder>/plugins``, and writing a *relative*
    path here would be resolved against the game's working directory instead,
    which is not reliably the folder OptiScaler was loaded from.
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
        # Not available from the ASI (they lived in OptiScaler's State, which it
        # no longer reads). Kept so the panel can treat them as "unknown".
        "can_switch_upscaler": False,
        "can_change_fg": False,
        "can_change_ffx_upscaler": False,
        "fps": None, "base_fps": None, "total_fps": None, "frames": None,
        "rendered_ms": None, "presented_ms": None,
        "backend_entries": None, "fg_enabled": None, "upscaler": None,
        "pending_backend": None, "fg_index": None, "ffx_fg_versions": [],
        "ffx_upscaler_index": None, "ffx_upscaler_versions": [],
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


def _send(target_dir, lines):
    """Write one command, atomically, under a fresh sequence number."""
    seq = time.time_ns() // 1_000_000
    body = "\n".join([f"seq {seq}", *lines, ""])
    path = _control_path(target_dir, CMD_FILE)
    # Write-then-replace so the ASI never reads a half-written command file.
    temp = path.with_suffix(".cmd.tmp")
    temp.write_text(body, encoding="utf-8")
    os.replace(temp, path)
    return seq


def apply_changes(target_dir, changes, can_reinit=True):
    """Ask the running game to adopt these INI changes immediately.

    ``changes`` are the same {section, key, value} dicts the config writer uses.
    ``can_reinit`` is whether the running OptiScaler can re-create its upscaler
    (status()["can_reinit"]); without it, settings that need that are deferred.
    Returns which ones were sent and which need a restart regardless.
    """
    live, deferred = [], []
    reinit = False
    for change in changes:
        ini_key = f"{change['section']}.{change['key']}"
        field = LIVE_FIELDS.get(ini_key)
        if field is None or (field.needs_reinit and not can_reinit):
            deferred.append(ini_key)
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
        return {"ok": True, "applied": [], "deferred": deferred, "sent": False}
    if reinit:
        # After the writes: the re-created upscaler reads the new values.
        live.append("reinit")
    try:
        seq = _send(target_dir, live)
    except OSError as exc:
        return {"ok": False, "error": str(exc), "applied": [], "deferred": deferred, "sent": False}
    return {
        "ok": True,
        "sent": True,
        "seq": seq,
        "applied": [line.split(" ")[1] for line in live if line.startswith("set ")],
        "deferred": deferred,
        "reinit": reinit,
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
        if len(parts) == 6:
            name, kind, engaged, value, default, ini = parts
            fields[name] = (kind, engaged == "1", value, default, ini)
        elif len(parts) == 2 and parts[0] != "fields":
            fields[parts[0]] = (parts[1], None, None, None, None)
    return fields


def switch_backend(target_dir, code):
    """Switching to another upscaler backend live is not supported.

    It takes OptiScaler's State internals, which the ASI no longer touches; the
    choice is recorded in the INI for the next launch, and OptiScaler's own
    overlay (the Quick Access "OptiScaler menu" button) switches it live.
    """
    return {"ok": False, "error": "switching the upscaler live is done in OptiScaler's own "
                                  "overlay; the panel applies it at the next launch"}


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

    asi = target / PLUGIN_SUBDIR / ASI_NAME
    if asi.is_file():
        current = st.get("asi_current")
        fact("In-game plugin file", f"{PLUGIN_SUBDIR}/{ASI_NAME}, {asi.stat().st_size} bytes"
             + ("" if current is not False else " — older than this plugin's: Update OptiScaler in this game"),
             "ok" if current is not False else "bad")
    else:
        fact("In-game plugin file", f"missing ({PLUGIN_SUBDIR}/{ASI_NAME}): Update OptiScaler in this game", "bad")

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
    relevant = [line for line in log_lines if _RELEVANT_RE.search(line)][:120]
    plugins_dir = target / PLUGIN_SUBDIR
    listing = "\n".join(f"{p.name}  {p.stat().st_size} bytes" for p in sorted(plugins_dir.iterdir())) \
        if plugins_dir.is_dir() else "(no plugins/ folder)"

    report = [f"Decky OptiScaler Nightly live-control report, {time.strftime('%Y-%m-%d %H:%M:%S')}",
              f"Game folder: {target}", ""]
    report += [f"[{tone:>4}] {label}: {value}" for label, value, tone in facts]
    report.append(section(STATUS_FILE, _read_text(target / STATUS_FILE)))
    report.append(section(CMD_FILE, _read_text(cmd)))
    report.append(section(LOG_FILE + " (last 80 lines)", "\n".join(log_tail(target_dir, 80))))
    report.append(section("plugins/", listing))
    report.append(section("OptiScaler.ini [Plugins]/[Log]", plugins_ini))
    report.append(section(f"OptiScaler.log: plugin lines ({len(relevant)})", "\n".join(relevant)))
    report.append(section("OptiScaler.log: first 40 lines", "\n".join(log_lines[:40])))
    report.append(section("OptiScaler.log: last 80 lines", "\n".join(log_lines[-80:])))
    return {"facts": facts, "report": "\n".join(report), "status": st}
