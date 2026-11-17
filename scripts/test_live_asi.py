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
from optiscaler import constants, installer, live  # noqa: E402
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
    "OptiFG.HUDFix": "true",
    "FrameGen.DebugView": "true",
    "OptiFG.MakeDepthCopy": "true",
    "OptiFG.HUDFixExtended": "true",
    "XeFG.InterpolationCount": "2",
    "FSR.UpscalerIndex": "1",
    "Sharpness.OverrideSharpness": "true",
    "Sharpness.Sharpness": "0.55",
    "Menu.ShowFps": "true",
}
# What the panel then changes live. Different from every sentinel.
LIVE_VALUES = {
    "FrameGen.Enabled": "false",
    "OptiFG.HUDFix": "false",
    "FrameGen.DebugView": "false",
    "OptiFG.MakeDepthCopy": "false",
    "OptiFG.HUDFixExtended": "false",
    "XeFG.InterpolationCount": "3",
    "FSR.UpscalerIndex": "0",
    "Sharpness.OverrideSharpness": "false",
    "Sharpness.Sharpness": "0.8125",
    "Menu.ShowFps": "false",
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
    for key, value in SENTINELS.items():
        section, name = key.split(".", 1)
        ini.set(section, name, value)
    ini.save()
    return game


def run(wine, scratch):
    host_exe, broken = build(scratch)
    asi = ROOT / "bin" / live.ASI_NAME
    game = install(scratch, asi)
    prefix = scratch / "prefix"

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
        check("OptiScaler's log names the ASI as loaded", live.loaded_by_optiscaler(str(game)) is True)
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

        print("\nLive changes, through the panel's own code")
        changes = [{"section": k.split(".", 1)[0], "key": k.split(".", 1)[1], "value": v}
                   for k, v in LIVE_VALUES.items()]
        sent = live.apply_changes(str(game), changes, can_reinit=st["can_reinit"])
        expected_deferred = [] if st["can_reinit"] else [live.FFX_UPSCALER_ID]
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
            check(f"{key} now holds the new value in OptiScaler's Config",
                  got is not None and got[1] and same(field.wire_type, got[2], field.encode(value)), got)
        if st["can_reinit"]:
            check("the upscaler re-init was requested through the plugin API",
                  _status_line(game, "reinit") in ("0", "1")
                  and "ReInitUpscalers" in "\n".join(live.log_tail(str(game), 20)))

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
        wineserver = shutil.which("wineserver") or str(Path(wine).with_name("wineserver"))
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
