"""OptiScaler's settings in the running game, as ini values.

The in-game plugin writes every Config field OptiScaler holds now
(``decky_optiscaler_live.values``, rewritten whenever one moves) and once as
loaded (``.loaded``). This turns them back into ini values, so the panel can
show a running game's real settings, including what was changed in
OptiScaler's own menu and not saved, and say which those are.

Read-only: nothing here writes to the game. Config only, like every other live
path (AGENTS.md, "Live editing"); OptiScaler's State is never read.
"""
import json
import math
import re
from pathlib import Path

from . import live
from .inifile import IniFile
from .schema import SCHEMA

VALUES_FILE = "decky_optiscaler_live.values"
LOADED_FILE = "decky_optiscaler_live.loaded"
INI_NAME = "OptiScaler.ini"

_MAP = json.loads((Path(__file__).with_name("live_fields.json")).read_text(encoding="utf-8"))
FIELDS = _MAP["fields"]
ENUMS = _MAP.get("enums", {})

# What OptiScaler logs when it creates the upscaler the game ends up with:
#   TryCreateOptiFeature Creating FSR 3.X upscaler feature
_CREATED_RE = re.compile(r"\[(\d\d:\d\d:\d\d)[.\d]*\].*?TryCreateOptiFeature Creating (.+?) feature")


def read_fields(path):
    """{member: (kind, engaged, value, volatile)} from a values/loaded/dump file."""
    fields = {}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) in (6, 7) and parts[0] != "fields":
            volatile = len(parts) == 7 and parts[6] == "1"
            fields[parts[0]] = (parts[1], parts[2] == "1", parts[3], volatile)
    return fields


def _canonical(meta, token):
    """The schema's own spelling of a token (the ini is case-insensitive)."""
    for option in (meta or {}).get("options") or []:
        if str(option).lower() == token.lower():
            return str(option)
    return token.lower()


def to_ini(ini_key, field):
    """An ini value for one dumped field, or None when there is no ini spelling."""
    info = FIELDS.get(ini_key)
    if not info or field is None:
        return None
    kind, engaged, value, _volatile = field
    if not engaged:
        return "auto"
    declared = info["kind"]
    meta = SCHEMA.get(tuple(ini_key.split(".", 1)))
    if kind == "bool":
        return "true" if value == "1" else "false"
    if kind == "f32":
        number = float(value)
        return f"{number:.6g}" if math.isfinite(number) else None
    if kind in ("i32", "u32", "u8"):
        if declared.startswith("enum:") and info["read"] == "String":
            code = ENUMS.get(declared[5:], {}).get("codes", {}).get(str(int(value)))
            return _canonical(meta, code) if code else None
        return str(int(value))
    return None  # strings: not shown from here


def same(a, b):
    """Whether two ini values mean the same thing."""
    if a is None or b is None:
        return a is b
    a, b = str(a).strip().lower(), str(b).strip().lower()
    if a in ("", "auto") or b in ("", "auto"):
        return a in ("", "auto") and b in ("", "auto")
    try:
        return math.isclose(float(a), float(b), rel_tol=1e-5, abs_tol=1e-6)
    except ValueError:
        return a == b


def created_upscaler(target_dir):
    """("FSR 3.X upscaler", "18:18:13") from the last creation OptiScaler logged."""
    path = Path(target_dir) / "OptiScaler.log"
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 4_000_000))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    last = None
    for match in _CREATED_RE.finditer(text):
        last = match
    return (last.group(2), last.group(1)) if last else None


def snapshot(target_dir):
    """The running game's settings, for the panel.

    ``values``: every ini key with an ini spelling, as OptiScaler holds it now.
    ``unsaved``: keys changed in the running game (OptiScaler's menu) that the
    ini does not hold: different from the ini, and from what OptiScaler loaded,
    so a value it resolved itself at startup ("auto" becoming the GPU's model)
    is not one. A volatile value (one OptiScaler sets for itself and never
    saves) is shown but never counted.
    """
    target = Path(target_dir)
    status = live.status(target_dir)
    empty = {"ok": True, "ready": False, "values": {}, "unsaved": [], "created": None}
    if not status.get("ready"):
        return empty
    now = read_fields(target / VALUES_FILE)
    loaded = read_fields(target / LOADED_FILE) or {}
    if not now:
        return empty
    try:
        ini = IniFile(target / INI_NAME).to_dict()
    except OSError:
        ini = {}

    values, unsaved = {}, []
    for ini_key, info in FIELDS.items():
        member = info["member"]
        current = to_ini(ini_key, now.get(member))
        if current is None:
            continue
        values[ini_key] = current
        if now[member][3]:  # volatile
            continue
        section, key = ini_key.split(".", 1)
        on_disk = ini.get(section, {}).get(key, "auto")
        at_load = to_ini(ini_key, loaded.get(member))
        if not same(current, on_disk) and (at_load is None or not same(current, at_load)):
            unsaved.append(ini_key)
    created = created_upscaler(target_dir)
    return {
        "ok": True,
        "ready": True,
        "values": values,
        "unsaved": sorted(unsaved),
        "created": {"upscaler": created[0], "at": created[1]} if created else None,
    }
