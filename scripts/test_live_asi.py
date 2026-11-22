#!/usr/bin/env python3
"""Live editing, end to end, inside the real bundled OptiScaler under Wine.

No game and no GPU: a stand-in host (asi/test_host.cpp) loads OptiScaler the
way a game does, as dxgi.dll in a folder this plugin installed, so OptiScaler
reads its ini and loads the live-control ASI from plugins/. The ASI is then
driven through the same command files, by the same code (optiscaler.live),
that the panel uses on the Deck.

What must hold, or the build fails:
  * OptiScaler loads the ASI, hands it the plugin API, and the ASI finds and
    validates Config with the layout generated from the bundled nightly's
    Config.h (scripts/sync_optiscaler_headers.py);
  * the layout is right: every one of the ~270 Config members with a literal
    default in Config.h reads back as that default, and the sentinel values
    written to OptiScaler.ini come back as what OptiScaler parsed;
  * every live setting (optiscaler.live.LIVE_FIELDS) is applied by the ASI and
    reads back with its new value, and the upscaler re-init is requested;
  * bad commands are refused and change nothing;
  * an ASI whose layout does not match ends in "failed" and writes nothing.

Not covered (needs a GPU and a game): that a changed value then shows up in a
rendered frame. OptiScaler.log on the Deck is the source of truth for that.

Needs: wine (wine64), x86_64-w64-mingw32-g++, the archive in bin/ and the ASI
built for it (bash asi/build.sh). CI installs wine and g++-mingw-w64-x86-64.
"""
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py_modules"))
from optiscaler import constants, installer, live, running  # noqa: E402
from optiscaler.inifile import IniFile  # noqa: E402
from optiscaler.payload import Payload  # noqa: E402

PASSED, FAILED = [], []
PROXY = "dxgi.dll"
MINGW = "x86_64-w64-mingw32-g++"


def check(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {name}{f' — {detail}' if detail != '' else ''}")
    return ok


def find_wine():
    for candidate in (os.environ.get("WINE"), "wine64", "wine", "/usr/lib/wine/wine64"):
        if candidate and (shutil.which(candidate) or Path(candidate).is_file()):
            return shutil.which(candidate) or candidate
    return None


def wait_for(predicate, timeout, step=0.25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return predicate()


def f32(value):
    """A float as OptiScaler stores it."""
    return struct.unpack("f", struct.pack("f", float(value)))[0]


def same(kind, got, want):
    if got in (None, "-"):
        return False
    if kind == "f32":
        return abs(float(got) - f32(want)) < 1e-6
    return str(int(float(got))) == str(int(float(want)))


# INI sentinels: every live setting gets a value OptiScaler would never pick on
# its own, so reading it back proves both that OptiScaler parsed it and that the
# ASI reads the right offset.
SENTINELS = {
    "FrameGen.Enabled": "true",
    "FrameGen.DebugView": "true",
    "XeFG.DebugView": "true",
    "OptiFG.MakeDepthCopy": "true",
    "OptiFG.HUDFixExtended": "true",
    "XeFG.InterpolationCount": "2",
    "XeFG.DynamicMFG": "true",
    "FSR.UpscalerIndex": "1",
    "Sharpness.OverrideSharpness": "true",
    "Sharpness.Sharpness": "0.55",
    "CAS.Enabled": "true",
    "Sharpness.Shader": "das",
    "CAS.OptiSharpenModel": "2",
    "Menu.ShowFps": "true",
    "Menu.FpsOverlayType": "2",
    "FSR.Fsr4Model": "2",
    "InitFlags.AutoExposure": "true",
    "InitFlags.HDR": "true",
    "InitFlags.DepthInverted": "true",
    "InitFlags.JitterCancellation": "true",
    "InitFlags.DisplayResolution": "true",
    "InitFlags.DynamicResolution": "true",
    "XeSS.NetworkModel": "2",
    "DLSS.RenderPresetOverride": "true",
    "DLSS.RenderPresetForAll": "3",
}
# Shown from the running game, not set live: enums the ini spells as words,
# read back through the token tables generated from OptiScaler's headers.
DISPLAY_SENTINELS = {
    "OptiFG.HUDFix": "true",  # not live (OptiScaler's menu also rebuilds the generator)
    "Upscalers.Dx12Upscaler": "xess",
    "FrameGen.FGInput": "fsrfg",
    "FrameGen.FGOutput": "xefg",
}
# What the panel then changes live. Different from every sentinel.
LIVE_VALUES = {
    "FrameGen.Enabled": "false",
    "FrameGen.DebugView": "false",
    "XeFG.DebugView": "false",
    "OptiFG.MakeDepthCopy": "false",
    "OptiFG.HUDFixExtended": "false",
    "XeFG.InterpolationCount": "3",
    "XeFG.DynamicMFG": "false",
    "FSR.UpscalerIndex": "0",
    "Sharpness.OverrideSharpness": "false",
    "Sharpness.Sharpness": "0.8125",
    "CAS.Enabled": "false",
    "Sharpness.Shader": "opti",
    "CAS.OptiSharpenModel": "3",
    "Menu.ShowFps": "false",
    "Menu.FpsOverlayType": "6",
    # "auto" for a resettable field: the ASI unsets it, as the overlay's "Auto".
    "FSR.Fsr4Model": "auto",
    "InitFlags.AutoExposure": "false",
    "InitFlags.HDR": "auto",
    "InitFlags.DepthInverted": "false",
    "InitFlags.JitterCancellation": "false",
    "InitFlags.DisplayResolution": "false",
    "InitFlags.DynamicResolution": "false",
    "XeSS.NetworkModel": "4",
    "DLSS.RenderPresetOverride": "false",
    "DLSS.RenderPresetForAll": "11",
}

DEFAULT_RE = re.compile(
    r"CustomOptional<(bool|int|int32_t|uint32_t|uint8_t|float)(?:,\s*\w+)?>\s+(\w+)\s*"
    r"\{\s*(true|false|-?0x[0-9a-fA-F]+|-?[0-9]+(?:\.[0-9]*)?f?)\s*\}\s*;")


def literal_defaults():
    """{member: (kind, value)} for Config members with a literal default."""
    text = (ROOT / "asi" / "optiscaler_ref" / "Config.h").read_text(encoding="utf-8", errors="replace")
    out = {}
    for ctype, name, raw in DEFAULT_RE.findall(text):
        if raw in ("true", "false"):
            value = "1" if raw == "true" else "0"
        elif raw.lower().startswith(("0x", "-0x")):
            value = str(int(raw, 16))
        else:
            value = raw.rstrip("f")
        out[name] = ("f32" if ctype == "float" else "int", value)
    return out


def mirror_members():
    text = (ROOT / "asi" / "generated" / "config_mirror.h").read_text(encoding="utf-8")
    return set(re.findall(r"^    X\((\w+),", text, re.M))


def build(scratch):
    """The host, and an ASI whose Config check never passes."""
    host = scratch / "host.exe"
    subprocess.run([MINGW, "-O2", "-municode", "-static", "-o", str(host),
                    str(ROOT / "asi" / "test_host.cpp")], check=True)
    broken = scratch / "broken.asi"
    subprocess.run([MINGW, "-shared", "-O2", "-std=c++17", "-static", "-s", f"-I{ROOT / 'asi'}",
                    "-DDECKY_TEST_BREAK_LAYOUT", "-DDECKY_SEARCH_ATTEMPTS=5",
                    "-o", str(broken), str(ROOT / "asi" / "live.cpp"), "-lpsapi"], check=True)
    return host, broken


class Host:
    """OptiScaler running in the stand-in host, under Wine."""

    def __init__(self, wine, game, prefix, host_exe):
        self.game = game
        shutil.copy2(host_exe, game / "host.exe")
        (game / "host.quit").unlink(missing_ok=True)
        env = dict(os.environ, WINEPREFIX=str(prefix), WINEDEBUG="-all",
                   WINEDLLOVERRIDES=f"{Path(PROXY).stem}=n,b", DISPLAY="")
        self.process = subprocess.Popen([wine, "host.exe", PROXY, "180"], cwd=game, env=env,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def stop(self):
        (self.game / "host.quit").touch()
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        (self.game / "host.quit").unlink(missing_ok=True)
        return self.process.stdout.read().decode("utf-8", "replace")


def status(game):
    return live.status(str(game))


def wait_seq(game, seq, timeout=15):
    return wait_for(lambda: status(game)["seq"] == str(seq), timeout)


def dump(game):
    path = game / live.DUMP_FILE
    path.unlink(missing_ok=True)
    seq = live.request_dump(str(game))
    wait_seq(game, seq)
    if not wait_for(path.is_file, 10):
        return {}
    return live.read_dump(str(game))


def install(scratch, asi):
    game = scratch / "game"
    game.mkdir()
    root = Payload(ROOT / "bin" / constants.PAYLOAD_ARCHIVE, scratch / "payload").ensure()
    result = installer.install(str(game), str(root), PROXY, False, None, str(asi))
    check("the plugin's installer puts the ASI where OptiScaler looks",
          result.get("live", {}).get("installed") and live.installed(str(game)),
          result.get("live"))
    check("and creates no OptiScaler/ folder (it would move OptiScaler's own lookups)",
          not (game / "OptiScaler").exists())
    ini = IniFile(game / constants.INI_NAME)
    for change in live.ini_requirements():
        ini.set(change["section"], change["key"], change["value"])
    ini.set("Log", "LogToFile", "true")
    for key, value in {**SENTINELS, **DISPLAY_SENTINELS}.items():
        section, name = key.split(".", 1)
        ini.set(section, name, value)
    ini.save()
    return game


def run(wine, scratch):
    host_exe, broken = build(scratch)
    asi = ROOT / "bin" / live.ASI_NAME
    game = install(scratch, asi)
    prefix = scratch / "prefix"

    # The last command of an earlier session, left in the folder: the panel's
    # FSR version from before the game was closed. Seen on a Deck: applied at
    # every launch, it overrode the ini OptiScaler had just loaded.
    stale_seq = time.time_ns() // 1_000_000 - 60_000
    (game / live.CMD_FILE).write_text(
        f"seq {stale_seq}\nset FfxUpscalerIndex i32 {LIVE_VALUES['FSR.UpscalerIndex']}\n"
        f"set Sharpness f32 {LIVE_VALUES['Sharpness.Sharpness']}\nreinit\n", encoding="utf-8")

    print("\nThe ASI inside OptiScaler")
    host = Host(wine, game, prefix, host_exe)
    try:
        wait_for(lambda: status(game)["state"] in ("ready", "failed"), 90)
        st = status(game)
        check("OptiScaler loads the ASI and it finds Config", st["ready"],
              f"state {st['state']}, error {st['error']}")
        if not st["ready"]:
            return
        check("it speaks this plugin's status format", st["schema"] == live.STATUS_SCHEMA, st["schema"])
        check("it reports the folder the panel manages", st["dir_matches"] is True, st["dir"])
        synced = (ROOT / "asi" / "optiscaler_ref" / "SOURCE_COMMIT.txt").read_text().split()[0]
        check("it names the OptiScaler commit it was built for",
              st.get("built_for") and synced.startswith(st["built_for"][:7]), st.get("built_for"))
        ran = live.running_build(str(game))
        if ran:
            check("that is the commit of the OptiScaler that loaded it",
                  ran[1][:7] == st["built_for"][:7], ran)
        facts = {label: tone for label, _, tone in live.diagnostics(str(game), str(asi))["facts"]}
        check("the panel's diagnostics rate the running plugin as fine",
              facts.get("In-game plugin status") == "ok"
              and facts.get("In-game plugin built for", "ok") == "ok", facts)
        check("OptiScaler's log names the ASI as loaded", live.loaded_by_optiscaler(str(game)) is True)
        check("a command left from before the game started is not applied",
              status(game)["seq"] == str(stale_seq)
              and not any(line.startswith(("set FfxUpscalerIndex", "set Sharpness"))
                          for line in live.log_tail(str(game), 50))
              and any("ignored" in line for line in live.log_tail(str(game), 50)),
              live.log_tail(str(game), 6))
        api = st["api"] or 0
        print(f"  (OptiScaler plugin API {api}, upscaler re-init {'available' if st['can_reinit'] else 'not available'})")
        check("the plugin API offers ReInitUpscalers from version 2", st["can_reinit"] == (api >= 2), api)

        print("\nConfig layout")
        fields = dump(game)
        members = mirror_members()
        check("the ASI sees every Config member of the generated mirror",
              set(fields) == members, sorted(members ^ set(fields))[:8])
        defaults = literal_defaults()
        compared = [name for name in defaults if name in fields and fields[name][3] not in (None, "-")]
        wrong = [f"{name}={fields[name][3]} (Config.h {defaults[name][1]})" for name in compared
                 if not same(defaults[name][0], fields[name][3], defaults[name][1])]
        check(f"all {len(compared)} literal defaults from Config.h read back at their offsets",
              len(compared) >= 100 and not wrong, wrong[:6] or len(compared))
        for key, value in SENTINELS.items():
            field = live.LIVE_FIELDS[key]
            got = fields.get(field.member)
            want = live.LIVE_FIELDS[key].encode(value)
            check(f"{key} reads back as OptiScaler parsed it from the ini",
                  got is not None and same(field.wire_type, got[4], want), got)

        print("\nThe running game's settings, as the panel shows them")
        check("the plugin writes OptiScaler's settings as loaded and as they are now",
              wait_for(lambda: (game / running.LOADED_FILE).is_file()
                       and (game / running.VALUES_FILE).is_file(), 10))
        snap = running.snapshot(str(game))
        check(f"as loaded, none of {len(snap['values'])} settings is marked as changed in-game",
              snap["ready"] and len(snap["values"]) >= 200 and snap["unsaved"] == [],
              snap["unsaved"][:8] or len(snap["values"]))
        for key, value in {**SENTINELS, **DISPLAY_SENTINELS}.items():
            check(f"{key} shows as the ini value OptiScaler holds",
                  running.same(snap["values"].get(key), value), snap["values"].get(key))

        print("\nLive changes, through the panel's own code")
        check("every live setting has a sentinel and a live value here",
              set(SENTINELS) == set(LIVE_VALUES) == set(live.LIVE_FIELDS),
              sorted(set(live.LIVE_FIELDS) ^ set(SENTINELS)) or sorted(set(SENTINELS) ^ set(LIVE_VALUES)))
        changes = [{"section": k.split(".", 1)[0], "key": k.split(".", 1)[1], "value": v}
                   for k, v in LIVE_VALUES.items()]
        sent = live.apply_changes(str(game), changes, can_reinit=st["can_reinit"])
        expected_deferred = [] if st["can_reinit"] else sorted(
            key for key in LIVE_VALUES if live.LIVE_FIELDS[key].needs_reinit)
        check("every live setting is sent", sent["sent"] and sorted(sent["deferred"]) == expected_deferred,
              sent)
        check("the ASI applies the command", wait_seq(game, sent["seq"]) is True)
        st = status(game)
        want_applied = len(sent["applied"]) + (1 if sent.get("reinit") else 0)
        check("and reports every write as applied", st["error"] is None
              and _status_line(game, "applied") == str(want_applied),
              f"applied {_status_line(game, 'applied')}, error {st['error']}")
        fields = dump(game)
        for key, value in LIVE_VALUES.items():
            if key in expected_deferred:
                continue
            field = live.LIVE_FIELDS[key]
            got = fields.get(field.member)
            if value == "auto":
                check(f"{key} is unset in OptiScaler's Config (its default applies)",
                      got is not None and not got[1], got)
                continue
            check(f"{key} now holds the new value in OptiScaler's Config",
                  got is not None and got[1] and same(field.wire_type, got[2], field.encode(value)), got)
        if st["can_reinit"]:
            check("the upscaler re-init was requested through the plugin API",
                  _status_line(game, "reinit") in ("0", "1")
                  and "ReInitUpscalers" in "\n".join(live.log_tail(str(game), 20)))

        # What the panel did live is not in the ini (the harness bypasses it),
        # exactly like a change made in OptiScaler's own menu.
        changed = sorted(key for key in LIVE_VALUES if key not in expected_deferred
                         and not running.same(LIVE_VALUES[key], SENTINELS[key]))
        check("the live snapshot follows within a couple of seconds",
              wait_for(lambda: running.snapshot(str(game))["unsaved"] == changed, 10),
              (running.snapshot(str(game))["unsaved"], changed))
        snap = running.snapshot(str(game))
        check("and shows each new value",
              all(running.same(snap["values"].get(key), LIVE_VALUES[key]) for key in changed),
              {key: snap["values"].get(key) for key in changed if
               not running.same(snap["values"].get(key), LIVE_VALUES[key])})

        print("\nBad commands")
        before = dump(game).get("Sharpness")
        seq = live._send(str(game), ["set NoSuchField bool 1", "set Sharpness bool 1"])
        wait_seq(game, seq)
        st = status(game)
        check("are refused with an error", st["error"] is not None and _status_line(game, "applied") == "0",
              st["error"])
        check("and change nothing", dump(game).get("Sharpness") == before)
    finally:
        output = host.stop()
        if FAILED:
            print("  host output:", output.strip()[-400:])

    print("\nA layout that does not match")
    shutil.copy2(broken, game / live.PLUGIN_SUBDIR / live.ASI_NAME)
    for name in (live.STATUS_FILE, live.CMD_FILE, live.LOG_FILE, live.DUMP_FILE):
        (game / name).unlink(missing_ok=True)
    host = Host(wine, game, prefix, host_exe)
    try:
        wait_for(lambda: status(game)["state"] == "failed", 60)
        st = status(game)
        check("ends in 'failed' instead of writing anywhere", st["state"] == "failed" and not st["ready"],
              f"state {st['state']}")
        check("and says why", "layout" in (st["error"] or ""), st["error"])
        check("nothing was written", not any(line.startswith("set ") for line in live.log_tail(str(game), 50)))
    finally:
        host.stop()


def _status_line(game, name):
    path = game / live.STATUS_FILE
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        key, _, value = line.partition(" ")
        if key == name:
            return value.strip()
    return None


def main():
    wine = find_wine()
    missing = [what for what, ok in (
        ("wine (apt install wine)", wine),
        (f"{MINGW} (apt install g++-mingw-w64-x86-64)", shutil.which(MINGW)),
        (f"bin/{constants.PAYLOAD_ARCHIVE} (scripts/bundle_nightly.py)",
         (ROOT / "bin" / constants.PAYLOAD_ARCHIVE).is_file()),
        (f"bin/{live.ASI_NAME} (bash asi/build.sh)", (ROOT / "bin" / live.ASI_NAME).is_file()),
    ) if not ok]
    if missing:
        sys.exit("test_live_asi.py needs: " + "; ".join(missing))

    scratch = Path(tempfile.mkdtemp(prefix="optiscaler-live-asi-"))
    try:
        run(wine, scratch)
    finally:
        # Stop Wine's server for the test prefix. Distributions keep it in
        # different places (Ubuntu's wine package: /usr/lib/wine/), and a
        # cleanup that cannot find it must not fail the checks above.
        wineserver = next((str(c) for c in (
            shutil.which("wineserver"), Path(wine).with_name("wineserver"),
            "/usr/lib/wine/wineserver", "/usr/lib/wine/wineserver64",
        ) if c and Path(c).is_file()), None)
        if wineserver:
            subprocess.run([wineserver, "-k"], env=dict(os.environ, WINEPREFIX=str(scratch / "prefix")),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        shutil.rmtree(scratch, ignore_errors=True)
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  FAILED: {name}")
        sys.exit(1)


if __name__ == "__main__":
    main()
