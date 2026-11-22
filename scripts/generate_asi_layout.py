#!/usr/bin/env python3
"""Generate the MSVC-ABI mirror of OptiScaler's Config object for the ASI.

The ASI has to read and write fields inside OptiScaler's live `Config` object,
which means it needs that object's exact memory layout. OptiScaler ships as an
MSVC build, so the layout is fixed by the MSVC C++ ABI and its standard library.

Rather than hand-copy offsets (one missed member silently shifts everything
after it), this parses the real `Config.h` member list and emits a C++ struct
built from MSVC-layout-compatible stand-ins. The *compiler* then computes the
offsets, and the ASI validates them at runtime before writing anything.

The INI key <-> member mapping is parsed out of `Config.cpp`'s Reload(), so the
plugin can address fields by the same (section, key) pairs it already uses.
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REF = ROOT / "asi" / "optiscaler_ref"
OUT_HEADER = ROOT / "asi" / "generated" / "config_mirror.h"
OUT_JSON = ROOT / "py_modules" / "optiscaler" / "live_fields.json"

# C++ types we know how to mirror, with the spelling used in the mirror header.
SCALARS = {
    "bool": "bool",
    "float": "float",
    "int": "int32_t",
    "int32_t": "int32_t",
    "uint32_t": "uint32_t",
    "UINT": "uint32_t",
    "std::string": "msvc_string",
    "std::wstring": "msvc_wstring",
}

# Enum members: mirrored as their underlying type.
ENUM_UNDERLYING = {
    "Upscaler": "int32_t",
    "SharpenShader": "int32_t",
    "FSR4Support": "uint8_t",
    "Scaler": "uint32_t",
    "ForceReflex": "uint32_t",
    "LFXMode": "uint32_t",
    "LowLatencyInput": "uint32_t",
    "LowLatencyMode": "uint32_t",
    "FrameTimeSource": "uint32_t",
    "FGInput": "uint32_t",
    "FGOutput": "uint32_t",
    "FGNvngxReplacement": "uint32_t",
    "FpsOverlay": "uint32_t",
    "FpsOverlayPos": "uint32_t",
    # Added in OptiScaler 10.0.0 (Config.h / OptiTypes.h).
    "FpsOverlayFpsRow": "uint32_t",
    "FpsOverlayShadow": "uint32_t",
    "FSR4Model": "uint8_t",
    "ReprojectionFill": "uint32_t",
}

# Non-CustomOptional members that still take up space in the object.
PLAIN_TYPES = {
    "std::filesystem::path": "msvc_path",
    "std::wstring": "msvc_wstring",
    "std::string": "msvc_string",
}


def strip_comments(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def class_body(text, name):
    match = re.search(r"\bclass\s+%s\b[^{]*\{" % name, text)
    if not match:
        raise SystemExit(f"class {name} not found")
    start = match.end()
    depth = 1
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i]
    raise SystemExit(f"class {name} body not closed")


def split_declarations(body):
    """Split a class body into statements, ignoring ; inside braces/parens/strings."""
    out, buf, depth, i = [], [], 0, 0
    fn_body = False
    while i < len(body):
        ch = body[i]
        if ch in "\"'":
            quote, buf_start = ch, i
            i += 1
            while i < len(body) and body[i] != quote:
                i += 2 if body[i] == "\\" else 1
            buf.append(body[buf_start : i + 1])
            i += 1
            continue
        if ch in "{([<":
            if ch == "{" and depth == 0:
                # Remember what precedes a top-level brace: a ")" (optionally
                # followed by const/noexcept/override) means an inline function
                # body, which has no terminating ";" of its own.
                head = "".join(buf).rstrip()
                head = re.sub(r"(\s+(const|noexcept|override|final))+$", "", head)
                fn_body = head.endswith(")")
            if ch != "<":
                depth += 1
        elif ch in "})]":
            depth -= 1
            if ch == "}" and depth == 0 and fn_body:
                # OptiScaler 10 defines small accessors inside class Config,
                # e.g. FfxUpscalerIndexFor(). They hold no data; end the
                # statement here so the next member is parsed on its own.
                buf.append(ch)
                out.append("".join(buf).strip())
                buf = []
                fn_body = False
                i += 1
                continue
        if ch == ";" and depth == 0:
            out.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if "".join(buf).strip():
        out.append("".join(buf).strip())
    return out


OPTIONAL_RE = re.compile(
    r"^CustomOptional\s*<\s*(?P<inner>[^,>]+?)\s*(?:,\s*(?P<mode>\w+)\s*)?>\s+"
    r"(?P<name>[A-Za-z_]\w*)\s*(?P<init>[={].*)?$",
    re.S,
)


def parse_members(header_text):
    """Ordered non-static data members of class Config."""
    body = strip_comments(class_body(header_text, "Config"))
    members, skipped = [], []
    for decl in split_declarations(body):
        decl = " ".join(decl.split())
        if not decl or decl.endswith(":"):
            continue
        if decl.startswith(("public", "private", "protected", "template", "static ")):
            continue
        if "inline static" in decl or decl.startswith("using "):
            skipped.append(decl)
            continue
        # Function declarations: a ( before any = initializer.
        paren, equals = decl.find("("), decl.find("=")
        if paren != -1 and (equals == -1 or paren < equals) and not decl.startswith("CustomOptional"):
            skipped.append(decl)
            continue
        # Inline member functions that return a CustomOptional, e.g.
        #   CustomOptional<int>& FfxUpscalerIndexFor(const std::string& custom) { ... }
        if re.match(r"^CustomOptional\s*<[^>]*>\s*[&*]?\s*[A-Za-z_]\w*\s*\(", decl):
            skipped.append(decl)
            continue

        match = OPTIONAL_RE.match(decl)
        if match:
            inner = match.group("inner").strip()
            if inner in SCALARS:
                mirror, kind = SCALARS[inner], inner
            elif inner in ENUM_UNDERLYING:
                mirror, kind = ENUM_UNDERLYING[inner], "enum:" + inner
            else:
                raise SystemExit(f"unknown CustomOptional inner type {inner!r} in: {decl}")
            members.append(
                {"name": match.group("name"), "kind": kind, "mirror": mirror,
                 "optional": True, "inner": inner}
            )
            continue

        # OptiScaler 10's per-game FFX upscaler index table and its lock, at
        # the end of the class. Templates nest too deeply for the plain regex.
        special = re.match(r"^std::(?P<kind>map|mutex)\b.*?\s(?P<name>[A-Za-z_]\w*)$", decl)
        if special:
            mirror = "msvc_map_raw" if special.group("kind") == "map" else "msvc_mutex_raw"
            members.append(
                {"name": special.group("name"), "kind": "std::" + special.group("kind"),
                 "mirror": mirror, "optional": False, "inner": "std::" + special.group("kind")}
            )
            continue

        # Plain (non-optional) data member.
        plain = re.match(r"^(?P<type>[\w:]+(?:\s*<[^>]*>)?)\s+(?P<name>[A-Za-z_]\w*)\s*(=.*)?$", decl)
        if plain and plain.group("type") in PLAIN_TYPES:
            members.append(
                {"name": plain.group("name"), "kind": plain.group("type"),
                 "mirror": PLAIN_TYPES[plain.group("type")], "optional": False,
                 "inner": plain.group("type")}
            )
            continue

        raise SystemExit(f"unrecognised declaration in class Config: {decl[:120]!r}")
    return members, skipped


READ_RE = re.compile(
    r"(?P<member>[A-Za-z_]\w*)\s*\.set_from_config\(\s*"
    r"read(?P<how>Bool|Int|Float|UInt|String|WString|Enum\s*<[^>]*>)\s*\(\s*"
    r"\"(?P<section>[^\"]+)\"\s*,\s*\"(?P<key>[^\"]+)\"",
    re.S,
)


# Some options are read into a local first so the value can be clamped or
# converted before being stored, e.g.
#   if (auto setting = readFloat("Sharpness", "Sharpness"); setting.has_value())
#       Sharpness.set_from_config(std::clamp(setting.value(), 0.0f, 1.3f));
DEFERRED_RE = re.compile(
    r"read(?P<how>Bool|Int|Float|UInt|String|WString|Enum\s*<[^>]*>)\s*\(\s*"
    # The tail is a lookahead so overlapping reads (a readBool nested inside one
    # set_from_config, immediately followed by another) both get seen.
    r"\"(?P<section>[^\"]+)\"\s*,\s*\"(?P<key>[^\"]+)\""
    r"(?=[^;]*;.{0,400}?(?P<member>[A-Za-z_]\w*)\s*\.set_from_config\()",
    re.S,
)


# Local helpers Config.cpp reads through (ReadOverlaySettings):
#   auto readIndex = [&](const char* key, uint32_t count) { return readUInt("Menu", key)...; };
#   auto apply = [assign](auto& setting, const auto& value) { ... setting.set_from_config(value); };
#   apply(FpsOverlayType, readIndex("FpsOverlayType", FpsOverlay_COUNT)...);
# are expanded in place to the plain spellings the patterns below know:
#   FpsOverlayType.set_from_config(readUInt("Menu", "FpsOverlayType", FpsOverlay_COUNT)...);
LAMBDA_RE = re.compile(
    r"auto\s+(?P<name>\w+)\s*=\s*\[[^\]]*\]\s*\((?P<params>[^)]*)\)\s*\{(?P<body>.*?)\}\s*;", re.S)


def _param_names(params):
    return [p.strip().split()[-1].lstrip("&*") for p in params.split(",") if p.strip()]


def expand_local_helpers(text):
    for helper in list(LAMBDA_RE.finditer(text)):
        name, params, body = helper.group("name"), _param_names(helper.group("params")), helper.group("body")
        if not params:
            continue
        reader = re.search(r"return\s+read(?P<how>Bool|Int|Float|UInt|String|WString)\s*\(\s*"
                           r"\"(?P<section>[^\"]+)\"\s*,\s*" + re.escape(params[0]) + r"\b", body)
        if reader:
            text = re.sub(r"(?<![\w.])" + re.escape(name) + r"\s*\(\s*(?=\")",
                          f'read{reader.group("how")}("{reader.group("section")}", ', text)
            continue
        if len(params) == 2 and re.search(re.escape(params[0]) + r"\s*\.set_from_config\(\s*"
                                          + re.escape(params[1]) + r"\s*\)", body):
            text = re.sub(r"(?<![\w.])" + re.escape(name) + r"\s*\(\s*(?P<member>[A-Za-z_]\w*)\s*,",
                          r"\g<member>.set_from_config(", text)
    return text


def parse_ini_map(source_text):
    """(section, key) -> member, as spelled in Config::Reload."""
    text = expand_local_helpers(strip_comments(source_text))
    mapping = {}
    for match in READ_RE.finditer(text):
        how = match.group("how")
        how = "Enum" if how.startswith("Enum") else how
        mapping[f"{match.group('section')}.{match.group('key')}"] = {
            "member": match.group("member"),
            "read": how,
        }
    for match in DEFERRED_RE.finditer(text):
        ini_key = f"{match.group('section')}.{match.group('key')}"
        if ini_key in mapping:
            continue
        how = match.group("how")
        mapping[ini_key] = {
            "member": _member_fed_by_local(text, match) or match.group("member"),
            "read": "Enum" if how.startswith("Enum") else how,
        }
    _map_helper_calls(text, mapping)
    return mapping


# A local helper that reads a key it is handed into a member it is handed:
#   auto readUpscaler = [&](const char* key, auto codeToUpscaler, auto& upscaler, auto& custom)
#   { auto value = readString("Upscalers", key, true)...; upscaler.set_from_config(value); };
#   readUpscaler("Dx12Upscaler", CodeToUpscalerFfx, Dx12Upscaler, CustomUpscaler);
# Each call maps Section.Key to the argument in the member parameter's place.
HELPER_RE = re.compile(
    r"auto\s+(?P<name>\w+)\s*=\s*\[[^\]]*\]\s*\((?P<params>[^)]*)\)\s*\{(?P<body>.*?)\n\s*\};", re.S)


def _map_helper_calls(text, mapping):
    for helper in HELPER_RE.finditer(text):
        params = [p.strip().split()[-1].lstrip("&*") for p in helper.group("params").split(",") if p.strip()]
        body = helper.group("body")
        read = re.search(r"read(?P<how>Bool|Int|Float|UInt|String|WString|Enum\s*<[^>]*>)\s*\(\s*"
                         r"\"(?P<section>[^\"]+)\"\s*,\s*(?P<key>\w+)\s*[,)]", body)
        if not read or read.group("key") not in params:
            continue
        fed = [i for i, name in enumerate(params)
               if re.search(r"\b" + re.escape(name) + r"\s*\.set_from_config\(\s*(?!std::string)", body)]
        if not fed:
            continue
        key_at, member_at = params.index(read.group("key")), fed[0]
        how = read.group("how")
        call_re = re.compile(re.escape(helper.group("name")) + r"\s*\((?P<args>[^;]*?)\)\s*;")
        for call in call_re.finditer(text, helper.end()):
            args = [a.strip() for a in call.group("args").split(",")]
            if len(args) != len(params) or not args[key_at].startswith('"'):
                continue
            ini_key = f"{read.group('section')}.{args[key_at].strip(chr(34))}"
            mapping[ini_key] = {"member": args[member_at],
                                "read": "Enum" if how.startswith("Enum") else how}


# `auto v = readX(...)` / `if (auto v = readX(...))`: the member is the one
# whose set_from_config is handed that local, not merely the next one. Taking
# the next one mapped Fsr4GpuSupport to Fsr4EnableWatermark and Dx12Upscaler
# to CustomUpscaler.
LOCAL_RE = re.compile(r"(?:auto|std::optional<[^>]*>)\s+(?P<local>[A-Za-z_]\w*)\s*=\s*$")


def _member_fed_by_local(text, match):
    before = text[max(0, match.start() - 120):match.start()]
    local = LOCAL_RE.search(before)
    if not local:
        return None
    name = re.escape(local.group("local"))
    feed = re.compile(r"(?P<member>[A-Za-z_]\w*)\s*\.set_from_config\((?P<arg>[^;]*?\b" + name + r"\b[^;]*)\)\s*;")
    found = feed.search(text, match.end(), match.end() + 1200)
    return found.group("member") if found else None


def emit_header(members):
    lines = [
        "// GENERATED by scripts/generate_asi_layout.py -- do not edit.",
        "// Mirror of OptiScaler's Config object using MSVC-ABI-compatible stand-ins.",
        "#pragma once",
        "#include \"msvc_abi.h\"",
        "",
        "namespace optimirror {",
        "",
        "struct Config",
        "{",
    ]
    for m in members:
        if m["optional"]:
            lines.append(f"    CustomOptional<{m['mirror']}> {m['name']};")
        else:
            lines.append(f"    {m['mirror']} {m['name']};")
    lines += ["};", ""]

    # X-macro list so the ASI builds its field table with real offsetof(), never
    # a number this script guessed at.
    kind_of = {
        "bool": "OPTI_BOOL", "float": "OPTI_FLOAT",
        "int": "OPTI_I32", "int32_t": "OPTI_I32",
        "uint32_t": "OPTI_U32", "UINT": "OPTI_U32",
        "std::string": "OPTI_STR", "std::wstring": "OPTI_WSTR",
    }
    lines.append("// name, mirror C type, kind tag")
    lines.append("#define OPTI_CONFIG_FIELDS(X) \\")
    for m in members:
        if not m["optional"]:
            continue
        kind = kind_of.get(m["kind"]) or (
            "OPTI_U32" if m["mirror"] in ("uint32_t", "uint8_t") else "OPTI_I32"
        )
        if m["mirror"] == "uint8_t":
            kind = "OPTI_U8"
        lines.append(f"    X({m['name']}, {m['mirror']}, {kind}) \\")
    lines.append("    /* end */")
    lines += ["", "} // namespace optimirror", ""]
    return "\n".join(lines)


# `enum class Upscaler {...}` and the older `enum FpsOverlay : uint32_t {...}`.
ENUM_RE = re.compile(r"enum\s+(?:class\s+)?(?P<name>\w+)\s*(?::\s*[\w:]+)?\s*\{(?P<body>[^{}]*)\}")
ENUM_CONFIG_RE = re.compile(r"struct\s+EnumConfig\s*<\s*(?P<name>\w+)\s*>\s*\{(?P<body>.*?)\};", re.S)
PAIR_RE = re.compile(r"\{\s*(?P<enum>\w+)::(?P<value>\w+)\s*,\s*\"(?P<code>[^\"]*)\"\s*\}")
UPSCALER_CASE_RE = re.compile(r"case\s+Upscaler::(?P<value>\w+)\s*:\s*return\s+\"(?P<code>[^\"]*)\"\s*;")


def parse_enums(*texts):
    """{enum: {"values": {name: int}, "codes": {int: ini token}}} from the headers.

    The tokens are what the ini holds ("ffx", "XeFG"): EnumConfig<T>::mapping,
    and UpscalerToCode for Upscaler in nightlies before EnumConfig<Upscaler>.
    The panel turns OptiScaler's live values back into those, so it can show
    them as ini values. A mapping may list a value more than once (aliases
    only read from older configs); EnumToCode writes its first match, so the
    first one is the token.
    """
    text = "\n".join(strip_comments(t) for t in texts)
    enums = {}
    for match in ENUM_RE.finditer(text):
        values, next_value = {}, 0
        for entry in (e.strip() for e in match.group("body").split(",")):
            if not entry:
                continue
            name, _, explicit = (part.strip() for part in entry.partition("="))
            if explicit:
                try:
                    next_value = int(explicit, 0)
                except ValueError:
                    raise SystemExit(f"enum {match.group('name')}: cannot evaluate {entry!r}")
            values[name] = next_value
            next_value += 1
        enums[match.group("name")] = {"values": values, "codes": {}}
    for match in ENUM_CONFIG_RE.finditer(text):
        enum = enums.get(match.group("name"))
        if enum is None:
            continue
        for pair in PAIR_RE.finditer(match.group("body")):
            if pair.group("value") in enum["values"]:
                enum["codes"].setdefault(str(enum["values"][pair.group("value")]), pair.group("code"))
    upscaler = enums.get("Upscaler")
    to_code = re.search(r"UpscalerToCode\s*\(\s*Upscaler\s+\w+\s*\)\s*\{(?P<body>.*?)\n\}", text, re.S)
    if upscaler and to_code:
        for case in UPSCALER_CASE_RE.finditer(to_code.group("body")):
            if case.group("value") in upscaler["values"] and case.group("code"):
                upscaler["codes"].setdefault(str(upscaler["values"][case.group("value")]), case.group("code"))
    # Hand-rolled reads in Config.cpp:  lstrcmpiA(x.value().c_str(), "nukems") == 0)
    #                                       FGNvngxReplacement.set_from_config(FGNvngxReplacement::Nukems);
    for case in STRCMP_CASE_RE.finditer(text):
        enum = enums.get(case.group("enum"))
        if enum and case.group("value") in enum["values"]:
            enum["codes"].setdefault(str(enum["values"][case.group("value")]), case.group("code"))
    return enums


STRCMP_CASE_RE = re.compile(
    r"lstrcmpiA\([^,]+,\s*\"(?P<code>[^\"]+)\"\s*\)\s*==\s*0\s*\)\s*\{?\s*"
    r"\w+\.set_from_config\(\s*(?P<enum>\w+)::(?P<value>\w+)\s*\)")


DEFAULT_DECL_RE = re.compile(
    r"CustomOptional<\s*(?P<type>[\w:]+)\s*(?:,\s*\w+)?\s*>\s+(?P<name>\w+)\s*\{\s*(?P<value>[^{};]+?)\s*\}\s*;")


def literal_defaults(header, fields, enums):
    """{ini key: ini value} for the members whose Config.h default is a literal.

    What OptiScaler runs with when the ini says "auto": true/false, numbers, and
    enum constants (FpsOverlay_FullGraph -> 5).
    """
    constants = {}
    for enum in enums.values():
        for name, value in enum["values"].items():
            constants[name] = value
    by_member = {}
    for match in DEFAULT_DECL_RE.finditer(strip_comments(header)):
        value = match.group("value").strip()
        if value in ("true", "false"):
            by_member[match.group("name")] = value
        elif re.fullmatch(r"-?(?:0x[0-9a-fA-F]+|\d+)", value):
            by_member[match.group("name")] = str(int(value, 0))
        elif re.fullmatch(r"-?\d+\.\d*f?", value):
            by_member[match.group("name")] = f"{float(value.rstrip('f')):.6g}"
        else:
            constant = value.split("::")[-1]
            if constant in constants:
                by_member[match.group("name")] = str(constants[constant])
    result = {}
    for key, info in fields.items():
        value = by_member.get(info["member"])
        if value is None:
            continue
        if info["kind"].startswith("enum:") and info["read"] == "String":
            value = enums.get(info["kind"][5:], {}).get("codes", {}).get(value)  # its ini word
            if value is None:
                continue
        result[key] = value
    return result


def overlay_type_labels(enums):
    """{"FpsOverlay": [names]}: OptiScaler's own "Overlay Type" names, in enum order.

    Read from its menu (menu_common.cpp, `fpsType[]`, which it static_asserts
    against FpsOverlay_COUNT), so the panel's overlay button says what
    OptiScaler's menu says. A list that does not match the enum fails here.
    """
    menu = REF / "menu_common.cpp"
    values = enums.get("FpsOverlay", {}).get("values", {})
    if not menu.is_file() or not values:
        return {}
    found = re.search(r"const\s+char\s*\*\s*fpsType\s*\[\s*\]\s*=\s*\{(?P<body>.*?)\}\s*;",
                      strip_comments(menu.read_text(encoding="utf-8", errors="replace")), re.S)
    if not found:
        raise SystemExit("menu_common.cpp has no fpsType[] (OptiScaler's overlay type names)")
    names = re.findall(r"\"([^\"]*)\"", found.group("body"))
    count = values.get("FpsOverlay_COUNT")
    if count is None or len(names) != count:
        raise SystemExit(f"fpsType[] has {len(names)} names, FpsOverlay has {count} types")
    return {"FpsOverlay": names}


def main():
    header = (REF / "Config.h").read_text(encoding="utf-8", errors="replace")
    source = (REF / "Config.cpp").read_text(encoding="utf-8", errors="replace")

    members, skipped = parse_members(header)
    ini_map = parse_ini_map(source)

    by_name = {m["name"]: m for m in members}
    unresolved = sorted({v["member"] for v in ini_map.values()} - set(by_name))
    if unresolved:
        raise SystemExit(f"Config.cpp reads members absent from Config.h: {unresolved}")

    OUT_HEADER.parent.mkdir(parents=True, exist_ok=True)
    OUT_HEADER.write_text(emit_header(members), encoding="utf-8")

    fields = {}
    for ini_key, info in sorted(ini_map.items()):
        member = by_name[info["member"]]
        fields[ini_key] = {
            "member": member["name"],
            "kind": member["kind"],
            "read": info["read"],
        }
    # What the panel shows from a running game and sends live: a nightly that
    # reads one of these some new way must fail here, not show an empty field.
    required = ("Upscalers.Dx11Upscaler", "Upscalers.Dx12Upscaler", "Upscalers.VulkanUpscaler",
                "FrameGen.Enabled", "FrameGen.FGInput", "FrameGen.FGOutput", "FSR.UpscalerIndex",
                "FSR.Fsr4Model", "FSR.Fsr4GpuSupport", "Menu.ShowFps", "Sharpness.Sharpness")
    unmapped = [key for key in required if key not in fields]
    if unmapped:
        raise SystemExit(f"Config.cpp reads these ini keys in a way this generator does not know: {unmapped}")
    enum_sources = [header, source] + [
        (REF / name).read_text(encoding="utf-8", errors="replace")
        for name in ("OptiTypes.h", "OptiTypes.cpp", "State.h") if (REF / name).is_file()]
    all_enums = parse_enums(*enum_sources)
    used = sorted({f["kind"][5:] for f in fields.values() if f["kind"].startswith("enum:")})
    enums = {name: all_enums[name] for name in used if name in all_enums}
    # An enum the ini spells as a word, with no token table found: the panel
    # could not show it, so fail here (the daily fix job then sees why).
    wordless = sorted(name for name in used
                      if any(f["kind"] == "enum:" + name and f["read"] == "String" for f in fields.values())
                      and not enums.get(name, {}).get("codes"))
    if wordless:
        raise SystemExit(f"no ini tokens found for enums read as strings: {wordless}")
    labels = overlay_type_labels(enums)
    defaults = literal_defaults(header, fields, all_enums)
    OUT_JSON.write_text(json.dumps({"fields": fields, "enums": enums, "labels": labels,
                                    "defaults": defaults}, indent=1, sort_keys=True), encoding="utf-8")

    print(f"members: {len(members)} (skipped {len(skipped)} non-data declarations)")
    print(f"ini keys mapped: {len(fields)}, enums with tokens: {len(enums)}")
    print(f"wrote {OUT_HEADER.relative_to(ROOT)} and {OUT_JSON.relative_to(ROOT)}")
    for probe in ("FrameGen.Enabled", "Upscalers.Dx12Upscaler", "XeFG.InterpolationCount"):
        print(f"  {probe:28} -> {fields.get(probe)}")


if __name__ == "__main__":
    sys.exit(main())
