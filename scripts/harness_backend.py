#!/usr/bin/env python3
"""The real plugin backend for the UI harness, against a simulated running game.

testharness/backend.tsx starts this and talks to it over stdin/stdout, one
JSON object per line: {"id": n, "method": "...", "args": [...]} answered by
{"id": n, "result": ...} or {"id": n, "error": "..."}.

Panel methods (read_config, write_config, get_live_status, get_running_config,
press_live_keys, keep_running_changes, set_fsr4_build, toggle_menu) are served
by the real OptiScalerService against a temporary game folder, so the UI is
tested against what the Python side really answers rather than a hand-written
fake of it. The hand-written fakes are how a panel bug got through: none of
them served a running game whose values differ from the ini.

"game.*" methods play the running game: OptiScaler with the in-game plugin.
It loads the ini into its Config, writes the ASI's status, as-loaded and
current-values files exactly as asi/live.cpp does, applies the panel's
commands (set/unset/reinit), toggles frame generation on its FG key, and can
change any value by itself: what its menu, its own keys, or its own runtime
adjustments do. Values are kept as the ASI dumps them (member, kind, engaged,
value), from asi/generated/config_mirror.h and live_fields.json.
"""
import asyncio
import json
import logging
import re
import sys
import tempfile
import time
import traceback
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "py_modules"))

from optiscaler import installer, keys, live, running  # noqa: E402
from optiscaler.inifile import IniFile  # noqa: E402
from optiscaler.service import OptiScalerService  # noqa: E402

KIND = {"OPTI_BOOL": "bool", "OPTI_I32": "i32", "OPTI_U32": "u32", "OPTI_U8": "u8", "OPTI_FLOAT": "f32"}


def mirror_kinds():
    text = (ROOT / "asi" / "generated" / "config_mirror.h").read_text(encoding="utf-8")
    return {name: KIND[tag] for name, tag in re.findall(r"X\((\w+),\s*[\w:]+,\s*(OPTI_\w+)\)", text)
            if tag in KIND}


class Game:
    """OptiScaler running with the in-game plugin, as far as the panel can tell."""

    def __init__(self, folder):
        self.dir = Path(folder)
        self.kinds = mirror_kinds()
        self.config = {}      # member -> (engaged, value as dumped)
        self.volatile = set()
        self.seq = 0
        self.running = False

    # -- ini values <-> Config values ---------------------------------
    def _encode(self, ini_key, text):
        """An ini value as Config would hold it: (engaged, dumped value)."""
        info = running.FIELDS.get(ini_key)
        if info is None or info["member"] not in self.kinds:
            return None
        text = str(text).strip()
        if text.lower() in ("", "auto"):
            return (False, "-")
        kind = self.kinds[info["member"]]
        if kind == "bool":
            return (True, "1" if text.lower() in ("true", "1") else "0")
        if info["kind"].startswith("enum:") and info["read"] == "String":
            codes = running.ENUMS.get(info["kind"][5:], {}).get("codes", {})
            for number, code in codes.items():
                if code.lower() == text.lower():
                    return (True, number)
            return None
        try:
            return (True, repr(float(text)) if kind == "f32" else str(int(float(text))))
        except ValueError:
            return None

    def start(self):
        """Launch: Config loads the ini; the plugin writes as-loaded and status."""
        ini = IniFile(self.dir / "OptiScaler.ini").to_dict()
        self.config.clear()
        self.volatile.clear()
        for ini_key, info in running.FIELDS.items():
            section, key = ini_key.split(".", 1)
            encoded = self._encode(ini_key, ini.get(section, {}).get(key, "auto"))
            if encoded is not None:
                self.config[info["member"]] = encoded
        self.running = True
        # As asi/live.cpp: a command written before the game started is an
        # earlier session's and is not applied.
        cmd = self.dir / live.CMD_FILE
        if cmd.is_file():
            seq = re.search(r"^seq (\d+)", cmd.read_text(encoding="utf-8"), re.M)
            if seq:
                self.seq = int(seq.group(1))
        self._write(running.LOADED_FILE)
        self.tick()

    def _write(self, name):
        lines = [f"fields {len(self.config)}"]
        for member, (engaged, value) in sorted(self.config.items()):
            vol = "1" if member in self.volatile else "0"
            lines.append(f"{member} {self.kinds[member]} {1 if engaged else 0} {value if engaged else '-'} 0 - {vol}")
        (self.dir / name).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def tick(self):
        """The ASI's loop: apply a new command, rewrite values and status."""
        if not self.running:
            return
        cmd = self.dir / live.CMD_FILE
        applied, error = 0, ""
        if cmd.is_file():
            text = cmd.read_text(encoding="utf-8")
            seq = re.search(r"^seq (\d+)", text, re.M)
            if seq and int(seq.group(1)) != self.seq:
                self.seq = int(seq.group(1))
                for line in text.splitlines()[1:]:
                    parts = line.split()
                    if len(parts) == 4 and parts[0] == "set" and parts[1] in self.config:
                        self.config[parts[1]] = (True, parts[3])
                        self.volatile.discard(parts[1])
                        applied += 1
                    elif len(parts) == 2 and parts[0] == "unset" and parts[1] in self.config:
                        self.config[parts[1]] = (False, "-")
                        applied += 1
                    elif parts and parts[0] == "reinit":
                        applied += 1
                    elif parts:
                        error = f"unknown command: {line}"
        self._write(running.VALUES_FILE)
        (self.dir / live.STATUS_FILE).write_text(
            f"schema {live.STATUS_SCHEMA}\nbuilt_for test\nstatus ready\napi 3\nreinit_available 1\n"
            f"seq {self.seq}\napplied {applied}\nreinit -1\nconfig 0x1\nfields {len(self.config)}\n"
            f"pid 1\ndir {self.dir}\nheartbeat {int(time.time())}\nerror {error}\n", encoding="utf-8")

    # -- what the game does by itself ----------------------------------
    def set(self, ini_key, value, volatile=False):
        """Its menu, its keys, or OptiScaler adjusting a value itself."""
        encoded = self._encode(ini_key, value)
        if encoded is None:
            raise ValueError(f"cannot hold {ini_key}={value}")
        member = running.FIELDS[ini_key]["member"]
        self.config[member] = encoded
        (self.volatile.add if volatile else self.volatile.discard)(member)
        self.tick()

    def fg_key(self):
        """OptiScaler's FG key: FGEnabled = !IsFGEnabled()."""
        engaged, value = self.config.get("FGEnabled", (False, "-"))
        on = engaged and value == "1"
        self.config["FGEnabled"] = (True, "0" if on else "1")
        self.tick()

    def values(self):
        """{ini key: ini value} as the game holds them now."""
        now = running.read_fields(self.dir / running.VALUES_FILE) or {}
        return {key: running.to_ini(key, now.get(info["member"])) for key, info in running.FIELDS.items()
                if info["member"] in now}


class Backend:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="optiscaler-harness-"))
        self.game_dir = self.root / "game"
        self.game_dir.mkdir()
        self.game = Game(self.game_dir)
        self.pressed = []
        self.service = OptiScalerService(ROOT, self.root / "settings", self.root / "runtime", self.root / "home",
                                         logging.getLogger("harness"))
        # A plugins/ copy of the shipped in-game plugin, so status reads it as current.
        source = self.service.live_asi_path()
        (self.game_dir / live.PLUGIN_SUBDIR).mkdir()
        target = self.game_dir / live.PLUGIN_SUBDIR / live.ASI_NAME
        target.write_bytes(Path(source).read_bytes() if source else b"MZ")

    def reset(self, entries=None):
        """A fresh ini from OptiScaler's reference, with these entries, and no game."""
        reference = (ROOT / "defaults" / "OptiScaler.reference.ini").read_text(encoding="utf-8")
        (self.game_dir / "OptiScaler.ini").write_text(reference, encoding="utf-8")
        ini = IniFile(self.game_dir / "OptiScaler.ini")
        for section, values in (entries or {}).items():
            for key, value in values.items():
                ini.set(section, key, value)
        ini.save()
        for name in (live.CMD_FILE, live.STATUS_FILE, running.VALUES_FILE, running.LOADED_FILE):
            (self.game_dir / name).unlink(missing_ok=True)
        self.game.running = False
        self.game.seq = 0
        return {"dir": str(self.game_dir)}

    def ini(self):
        return IniFile(self.game_dir / "OptiScaler.ini").to_dict()

    def set_fsr4_build(self, target_dir, build_id):
        # The real one also checks and swaps the bundled DLL; its ini half is
        # installer.deck_preset_changes, written here the same way, and its
        # live half is the service's own.
        ini = IniFile(Path(target_dir) / "OptiScaler.ini")
        ini.update(installer.deck_preset_changes(build_id))
        ini.save()
        outcome = self.service._push_preset(target_dir, installer.deck_preset_changes(build_id))
        return {"ok": True, "model": build_id, "live": outcome}

    def leftover(self, lines):
        """An earlier session's last command, left in the folder before this game starts."""
        seq = time.time_ns() // 1_000_000 - 60_000
        (self.game_dir / live.CMD_FILE).write_text("\n".join([f"seq {seq}", *lines, ""]), encoding="utf-8")

    def call(self, method, args):
        if method.startswith("game."):
            name = method[5:]
            if name == "start":
                return self.game.start()
            if name == "set":
                return self.game.set(*args)
            if name == "values":
                return self.game.values()
            if name == "stop":
                self.game.running = False
                return None
            raise ValueError(f"no game method {name}")
        if method == "reset":
            return self.reset(*args)
        if method == "leftover":
            return self.leftover(*args)
        if method == "ini":
            return self.ini()
        if method == "pressed":
            return self.pressed
        self.game.tick()
        if method == "set_fsr4_build":
            result = self.set_fsr4_build(*args)
        else:
            handler = getattr(self.service, method)
            with mock.patch.object(keys, "press", side_effect=self._press):
                result = asyncio.run(handler(*args))
        self.game.tick()
        return result

    def _press(self, vk):
        self.pressed.append(vk)
        if vk == self.service._shortcut(str(self.game_dir), "FGShortcutKey"):
            self.game.fg_key()
        return {"vk": vk}


def main():
    logging.basicConfig(level=logging.ERROR)
    backend = Backend()
    for line in sys.stdin:
        if not line.strip():
            continue
        request = json.loads(line)
        try:
            reply = {"id": request["id"], "result": backend.call(request["method"], request.get("args", []))}
        except Exception as exc:  # reported to the harness, which fails the check
            reply = {"id": request["id"], "error": f"{exc}\n{traceback.format_exc()}"}
        sys.stdout.write(json.dumps(reply, default=str) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
