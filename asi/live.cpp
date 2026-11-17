// Decky OptiScaler Nightly live-control ASI plugin.
//
// OptiScaler reads OptiScaler.ini once, at startup. Its in-game overlay changes
// settings live because it runs inside the game and writes Config directly.
// This plugin does the same for the Decky panel, from inside the game process:
//
//   * Plain settings OptiScaler re-reads every frame (frame generation on/off,
//     HUD fix, sharpness, the FPS overlay...) are a Config write and nothing
//     else, exactly like the overlay.
//   * Settings read when the upscaler is created (the FFX upscaler version)
//     are a Config write followed by OptiPluginApi::ReInitUpscalers, which
//     OptiScaler applies on its next frame.
//
// OptiScaler 10 has a stable C plugin API (plugin_sdk/optiscaler_plugin.h): it
// calls OptiPluginInit in every ASI it loads and hands it a versioned table of
// functions. Everything that API offers is used through it. It does not expose
// OptiScaler's own settings, so Config is still found in memory, with the
// layout generated from the nightly's Config.h (generated/config_mirror.h),
// and validated before anything is written.
//
// Deliberately NOT done, unlike the 0.9.x plugin: nothing in OptiScaler's State
// object is read or written. Switching to another upscaler backend or FFX frame
// generator needs State internals that change between nightlies; those stay
// "restart to apply" in the panel, or are done in OptiScaler's own overlay.
//
// Protocol (files next to OptiScaler's dll, written by py_modules/optiscaler/live.py):
//   decky_optiscaler_live.cmd     seq <n>, then one command per line:
//                                   set <member> <bool|i32|u32|u8|f32> <value>
//                                   unset <member>  drop the value (the overlay's "Auto")
//                                   reinit      re-create the upscaler
//                                   dump        write every field to .dump
//   decky_optiscaler_live.status  rewritten every second (schema 8):
//                                 status loaded|searching|ready|failed
//   decky_optiscaler_live.dump    <member> <kind> <engaged> <value> <default> <ini> <volatile>
//   decky_optiscaler_live.values  the same, rewritten whenever a setting moves (read-only)
//   decky_optiscaler_live.loaded  the same, once, as OptiScaler loaded them
//   decky_optiscaler_live.log     what the plugin did

#define __USE_MINGW_ANSI_STDIO 1

#include <windows.h>
#include <psapi.h>

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#include "msvc_abi.h"
#include "generated/config_mirror.h"
#include "plugin_sdk/optiscaler_plugin.h"

using namespace optimirror;

#define STATUS_SCHEMA 8

// The OptiScaler commit whose Config.h this was compiled against (asi/build.sh
// passes asi/optiscaler_ref/SOURCE_COMMIT.txt). Reported so the panel can tell
// "built for another OptiScaler" apart from everything else that stops it.
#ifndef DECKY_BUILT_FOR
#define DECKY_BUILT_FOR "unknown"
#endif

// How many times (200 ms apart) Config is looked for before giving up.
#ifndef DECKY_SEARCH_ATTEMPTS
#define DECKY_SEARCH_ATTEMPTS 150
#endif

// Field kind tags used by the generated table.
enum FieldKind { OPTI_BOOL, OPTI_I32, OPTI_U32, OPTI_U8, OPTI_FLOAT, OPTI_STR, OPTI_WSTR };

struct FieldDesc {
    const char* name;
    unsigned offset;
    unsigned kind;
};

#define FIELD_ENTRY(NAME, TYPE, KIND) { #NAME, (unsigned) offsetof(Config, NAME), KIND },
static const FieldDesc kFields[] = { OPTI_CONFIG_FIELDS(FIELD_ENTRY) };
#undef FIELD_ENTRY
static const size_t kFieldCount = sizeof(kFields) / sizeof(kFields[0]);

// ------------------------------------------------------------------ state --

static const OptiPluginApi* volatile g_api = NULL;
static OptiPlugin* volatile g_plugin = NULL;
static HMODULE g_self = NULL;
static HMODULE g_opti = NULL;
static Config* g_config = NULL;

static wchar_t g_dir[MAX_PATH];       // folder holding OptiScaler's dll
static wchar_t g_logPath[MAX_PATH];
static wchar_t g_cmdPath[MAX_PATH];
static wchar_t g_statusPath[MAX_PATH];
static wchar_t g_dumpPath[MAX_PATH];
static wchar_t g_valuesPath[MAX_PATH];
static wchar_t g_loadedPath[MAX_PATH];

static char g_status[32] = "loaded";
static char g_error[256] = "";
static long long g_seq = -1;
static int g_applied = 0;
static int g_reinit = -1;  // -1 never asked, 0 refused, 1 accepted

// ---------------------------------------------------------------- logging --

static void LogLine(const char* fmt, ...) {
    char buf[1024];
    va_list args;
    va_start(args, fmt);
    int n = vsnprintf(buf, sizeof(buf) - 2, fmt, args);
    va_end(args);
    if (n < 0) n = 0;
    if (n > (int) sizeof(buf) - 2) n = (int) sizeof(buf) - 2;
    buf[n] = 0;

    const OptiPluginApi* api = g_api;
    if (api && OPTI_API_HAS(api, Log) && g_plugin) api->Log(g_plugin, OptiLog_Info, buf);

    if (!g_logPath[0]) return;
    HANDLE h = CreateFileW(g_logPath, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                           OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE) return;
    buf[n++] = '\n';
    DWORD written = 0;
    WriteFile(h, buf, (DWORD) n, &written, NULL);
    CloseHandle(h);
}

static void SetError(const char* fmt, ...) {
    va_list args;
    va_start(args, fmt);
    vsnprintf(g_error, sizeof(g_error), fmt, args);
    va_end(args);
    LogLine("error: %s", g_error);
}

/** Best-effort ASCII copy of a wide string, for logging paths. */
static void NarrowCopy(char* out, size_t outLen, const wchar_t* in) {
    size_t i = 0;
    for (; in[i] && i + 1 < outLen; i++)
        out[i] = (in[i] < 0x80) ? (char) in[i] : '?';
    out[i] = 0;
}

// ------------------------------------------------------- memory safety net --

// Config is found by scanning, so every candidate dereference is checked
// against the page protection first. A one-entry region cache keeps the scan
// cheap; it is only enabled while scanning.
static bool g_cacheEnabled = false;
static const unsigned char* g_cacheBase = NULL;
static size_t g_cacheSize = 0;

static bool Readable(const void* p, size_t len) {
    if (!p || len == 0) return false;
    const unsigned char* cur = (const unsigned char*) p;
    const unsigned char* end = cur + len;
    if (g_cacheEnabled && g_cacheBase && cur >= g_cacheBase && end <= g_cacheBase + g_cacheSize)
        return true;
    while (cur < end) {
        MEMORY_BASIC_INFORMATION mbi;
        if (VirtualQuery(cur, &mbi, sizeof(mbi)) != sizeof(mbi)) return false;
        if (mbi.State != MEM_COMMIT) return false;
        DWORD ok = PAGE_READONLY | PAGE_READWRITE | PAGE_WRITECOPY | PAGE_EXECUTE_READ |
                   PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY;
        if (!(mbi.Protect & ok) || (mbi.Protect & PAGE_GUARD)) return false;
        if (g_cacheEnabled) {
            g_cacheBase = (const unsigned char*) mbi.BaseAddress;
            g_cacheSize = mbi.RegionSize;
        }
        cur = (const unsigned char*) mbi.BaseAddress + mbi.RegionSize;
    }
    return true;
}

static bool Writable(const void* p, size_t len) {
    if (!p || len == 0) return false;
    MEMORY_BASIC_INFORMATION mbi;
    if (VirtualQuery(p, &mbi, sizeof(mbi)) != sizeof(mbi)) return false;
    if (mbi.State != MEM_COMMIT) return false;
    DWORD ok = PAGE_READWRITE | PAGE_WRITECOPY | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY;
    return (mbi.Protect & ok) && !(mbi.Protect & PAGE_GUARD) &&
           (const unsigned char*) p + len <= (const unsigned char*) mbi.BaseAddress + mbi.RegionSize;
}

struct ScopedScanCache {
    ScopedScanCache() { g_cacheEnabled = true; g_cacheBase = NULL; }
    ~ScopedScanCache() { g_cacheEnabled = false; g_cacheBase = NULL; }
};

/** Copy a MSVC std::wstring out as UTF-16. */
static bool ReadStdWString(const msvc_wstring* s, wchar16* out, size_t outLen) {
    if (!Readable(s, sizeof(*s))) return false;
    if (s->capacity < s->size || s->size > 0x8000) return false;
    const wchar16* data = s->capacity <= 7 ? s->bx.buf : s->bx.ptr;
    if (!Readable(data, (s->size + 1) * sizeof(wchar16))) return false;
    if (s->size >= outLen) return false;
    memcpy(out, data, s->size * sizeof(wchar16));
    out[s->size] = 0;
    return true;
}

static bool WideEqualsAscii(const wchar16* w, const char* ascii) {
    for (; *ascii; ++ascii, ++w)
        if (*w != (wchar16) (unsigned char) *ascii) return false;
    return *w == 0;
}

static bool WideEndsWithAsciiNoCase(const wchar16* w, const char* suffix) {
    size_t wl = 0, sl = strlen(suffix);
    while (w[wl]) wl++;
    if (wl < sl) return false;
    const wchar16* tail = w + (wl - sl);
    for (size_t i = 0; i < sl; i++) {
        int a = tail[i], b = (unsigned char) suffix[i];
        if (a >= 'A' && a <= 'Z') a += 32;
        if (b >= 'A' && b <= 'Z') b += 32;
        if (a != b) return false;
    }
    return true;
}

// ------------------------------------------------- OptiScaler module lookup --

/** The module a function pointer lives in. */
static HMODULE ModuleOf(const void* address) {
    HMODULE mod = NULL;
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                                GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                            (LPCWSTR) address, &mod))
        return NULL;
    return mod;
}

/**
 * OptiScaler's own module. With the plugin API it is simply where the API's
 * functions live; without it (an OptiScaler older than the API), the only
 * module exporting both an NVNGX entry point and the wininet AppCache surface.
 */
static HMODULE FindOptiScalerModule() {
    const OptiPluginApi* api = g_api;
    if (api && OPTI_API_HAS(api, Log)) {
        HMODULE mod = ModuleOf((const void*) api->Log);
        if (mod && mod != g_self) return mod;
    }
    HMODULE mods[1024];
    DWORD needed = 0;
    if (!EnumProcessModules(GetCurrentProcess(), mods, sizeof(mods), &needed)) return NULL;
    size_t count = needed / sizeof(HMODULE);
    if (count > 1024) count = 1024;
    for (size_t i = 0; i < count; i++) {
        if (GetProcAddress(mods[i], "NVSDK_NGX_D3D12_Init") &&
            GetProcAddress(mods[i], "AppCacheCheckManifest"))
            return mods[i];
    }
    return NULL;
}

struct Section {
    unsigned char* base;
    size_t size;
};

/** Writable data sections of a module -- where its statics live. */
static int ModuleDataSections(HMODULE mod, Section* out, int maxOut) {
    unsigned char* base = (unsigned char*) mod;
    IMAGE_DOS_HEADER* dos = (IMAGE_DOS_HEADER*) base;
    if (!Readable(dos, sizeof(*dos)) || dos->e_magic != IMAGE_DOS_SIGNATURE) return 0;
    IMAGE_NT_HEADERS* nt = (IMAGE_NT_HEADERS*) (base + dos->e_lfanew);
    if (!Readable(nt, sizeof(*nt)) || nt->Signature != IMAGE_NT_SIGNATURE) return 0;
    IMAGE_SECTION_HEADER* sec = IMAGE_FIRST_SECTION(nt);
    int found = 0;
    for (int i = 0; i < nt->FileHeader.NumberOfSections && found < maxOut; i++) {
        if (!(sec[i].Characteristics & IMAGE_SCN_MEM_WRITE)) continue;
        if (sec[i].Characteristics & IMAGE_SCN_MEM_EXECUTE) continue;
        size_t size = sec[i].Misc.VirtualSize ? sec[i].Misc.VirtualSize : sec[i].SizeOfRawData;
        if (!size) continue;
        out[found].base = base + sec[i].VirtualAddress;
        out[found].size = size;
        found++;
    }
    return found;
}

// ------------------------------------------------------------ Config lookup --

// Config is heap-allocated (`_config = new Config()`), and `_config` is a static
// in OptiScaler's data section: walk the data sections for a pointer whose
// target validates as a Config. The checks are spread from the middle of the
// struct to its end (the last two members are the ini's path and name), so a
// layout that is off anywhere before them fails at least one.

static bool LooksLikeConfig(const Config* cfg) {
#ifdef DECKY_TEST_BREAK_LAYOUT
    // scripts/test_live_asi.py builds this variant to prove that a layout that
    // does not match OptiScaler ends in "failed" with nothing written.
    (void) cfg;
    return false;
#endif
    if (!Readable(cfg, sizeof(Config))) return false;

    wchar16 buf[MAX_PATH];
    if (!ReadStdWString(&cfg->fileName, buf, MAX_PATH)) return false;
    if (!WideEqualsAscii(buf, "OptiScaler.ini")) return false;
    if (!ReadStdWString(&cfg->absoluteFileName.text, buf, MAX_PATH)) return false;
    if (!WideEndsWithAsciiNoCase(buf, "OptiScaler.ini")) return false;

    // Compile-time defaults from Config.h, sampled across the struct.
    if (cfg->ShortcutKey.defaultValue != VK_INSERT) return false;
    if (cfg->FpsShortcutKey.defaultValue != VK_PRIOR) return false;
    if (cfg->FGShortcutKey.defaultValue != VK_END) return false;
    if (cfg->FontSize.defaultValue != 14.0f) return false;
    if (cfg->Sharpness.defaultValue != 0.4f) return false;
    if (cfg->DlssReactiveMaskBias.defaultValue != 0.45f) return false;
    return true;
}

static unsigned g_scanSections = 0;
static unsigned long long g_scanPointers = 0;

static Config* FindConfig(HMODULE opti) {
    ScopedScanCache cache;
    Section sections[16];
    int n = ModuleDataSections(opti, sections, 16);
    g_scanSections = (unsigned) n;
    g_scanPointers = 0;
    for (int s = 0; s < n; s++) {
        unsigned char* p = (unsigned char*) (((uintptr_t) sections[s].base + 7) & ~(uintptr_t) 7);
        unsigned char* end = sections[s].base + sections[s].size;
        for (; p + sizeof(void*) <= end; p += sizeof(void*)) {
            if (!Readable(p, sizeof(void*))) continue;
            Config* candidate = *(Config**) p;
            uintptr_t address = (uintptr_t) candidate;
            if (address < 0x10000 || address > 0x7FFFFFFFFFFFull) continue;
            if (address & 0xF) continue;  // operator new is 16-byte aligned
            g_scanPointers++;
            if (LooksLikeConfig(candidate)) return candidate;
        }
    }
    return NULL;
}

// ---------------------------------------------------------- field access --

static const FieldDesc* FindField(const char* name) {
    for (size_t i = 0; i < kFieldCount; i++)
        if (_stricmp(kFields[i].name, name) == 0) return &kFields[i];
    return NULL;
}

template <class T> static CustomOptional<T>* FieldAt(const FieldDesc* f) {
    return (CustomOptional<T>*) ((unsigned char*) g_config + f->offset);
}

/** Write one Config option, the way CustomOptional::operator= does. */
static bool SetField(const char* name, const char* type, const char* value) {
    const FieldDesc* f = FindField(name);
    if (!f) { SetError("no Config field %s", name); return false; }
    if (!g_config) { SetError("Config not found"); return false; }
    unsigned char* at = (unsigned char*) g_config + f->offset;
    if (!Writable(at, sizeof(CustomOptional<double>))) { SetError("%s is not writable", name); return false; }

    char* end = NULL;
    if (strcmp(type, "bool") == 0 && f->kind == OPTI_BOOL) {
        long v = strtol(value, &end, 10);
        if (*end || (v != 0 && v != 1)) { SetError("%s: not a bool: %s", name, value); return false; }
        FieldAt<bool>(f)->assign(v != 0);
    } else if (strcmp(type, "i32") == 0 && f->kind == OPTI_I32) {
        long v = strtol(value, &end, 10);
        if (*end) { SetError("%s: not an integer: %s", name, value); return false; }
        FieldAt<int32_t>(f)->assign((int32_t) v);
    } else if (strcmp(type, "u32") == 0 && f->kind == OPTI_U32) {
        unsigned long v = strtoul(value, &end, 10);
        if (*end) { SetError("%s: not an integer: %s", name, value); return false; }
        FieldAt<uint32_t>(f)->assign((uint32_t) v);
    } else if (strcmp(type, "u8") == 0 && f->kind == OPTI_U8) {
        unsigned long v = strtoul(value, &end, 10);
        if (*end || v > 255) { SetError("%s: not a byte: %s", name, value); return false; }
        FieldAt<uint8_t>(f)->assign((uint8_t) v);
    } else if (strcmp(type, "f32") == 0 && f->kind == OPTI_FLOAT) {
        float v = strtof(value, &end);
        if (*end) { SetError("%s: not a number: %s", name, value); return false; }
        FieldAt<float>(f)->assign(v);
    } else {
        SetError("%s is not a %s field", name, type);
        return false;
    }
    LogLine("set %s (%s) = %s", name, type, value);
    return true;
}

/** Drop a field's value, as the overlay's "Auto" does: OptiScaler then uses its default. */
static bool UnsetField(const char* name) {
    const FieldDesc* f = FindField(name);
    if (!f) { SetError("no Config field %s", name); return false; }
    if (!g_config) { SetError("Config not found"); return false; }
    unsigned char* at = (unsigned char*) g_config + f->offset;
    if (!Writable(at, sizeof(CustomOptional<double>))) { SetError("%s is not writable", name); return false; }
    switch (f->kind) {
    case OPTI_BOOL: FieldAt<bool>(f)->reset(); break;
    case OPTI_I32: FieldAt<int32_t>(f)->reset(); break;
    case OPTI_U32: FieldAt<uint32_t>(f)->reset(); break;
    case OPTI_U8: FieldAt<uint8_t>(f)->reset(); break;
    case OPTI_FLOAT: FieldAt<float>(f)->reset(); break;
    default: SetError("%s cannot be unset", name); return false;
    }
    LogLine("unset %s", name);
    return true;
}

/** "<engaged> <value> <default> <ini>" for a scalar field, "-" where unset. */
template <class T> static void FormatOptional(const CustomOptional<T>* o, const char* fmt, char* out,
                                              size_t outLen) {
    char value[48] = "-", def[48], ini[48] = "-";
    if (o->base.engaged) snprintf(value, sizeof(value), fmt, o->base.value);
    snprintf(def, sizeof(def), fmt, o->defaultValue);
    if (o->configIni.engaged) snprintf(ini, sizeof(ini), fmt, o->configIni.value);
    snprintf(out, outLen, "%d %s %s %s %d", o->base.engaged ? 1 : 0, value, def, ini, o->isVolatile ? 1 : 0);
}

static void FormatField(const FieldDesc* f, char* out, size_t outLen) {
    const void* at = (unsigned char*) g_config + f->offset;
    if (!Readable(at, sizeof(CustomOptional<double>))) { snprintf(out, outLen, "unreadable"); return; }
    switch (f->kind) {
    case OPTI_BOOL: {
        const CustomOptional<bool>* o = (const CustomOptional<bool>*) at;
        char v[4] = "-", d[4], i[4] = "-";
        if (o->base.engaged) snprintf(v, sizeof(v), "%d", o->base.value ? 1 : 0);
        snprintf(d, sizeof(d), "%d", o->defaultValue ? 1 : 0);
        if (o->configIni.engaged) snprintf(i, sizeof(i), "%d", o->configIni.value ? 1 : 0);
        snprintf(out, outLen, "bool %d %s %s %s %d", o->base.engaged ? 1 : 0, v, d, i, o->isVolatile ? 1 : 0);
        return;
    }
    case OPTI_I32: {
        char tail[200];
        FormatOptional((const CustomOptional<int32_t>*) at, "%d", tail, sizeof(tail));
        snprintf(out, outLen, "i32 %s", tail);
        return;
    }
    case OPTI_U32: {
        char tail[200];
        FormatOptional((const CustomOptional<uint32_t>*) at, "%u", tail, sizeof(tail));
        snprintf(out, outLen, "u32 %s", tail);
        return;
    }
    case OPTI_U8: {
        const CustomOptional<uint8_t>* o = (const CustomOptional<uint8_t>*) at;
        char v[8] = "-", d[8], i[8] = "-";
        if (o->base.engaged) snprintf(v, sizeof(v), "%u", (unsigned) o->base.value);
        snprintf(d, sizeof(d), "%u", (unsigned) o->defaultValue);
        if (o->configIni.engaged) snprintf(i, sizeof(i), "%u", (unsigned) o->configIni.value);
        snprintf(out, outLen, "u8 %d %s %s %s %d", o->base.engaged ? 1 : 0, v, d, i, o->isVolatile ? 1 : 0);
        return;
    }
    case OPTI_FLOAT: {
        const CustomOptional<float>* o = (const CustomOptional<float>*) at;
        char v[48] = "-", d[48], i[48] = "-";
        if (o->base.engaged) snprintf(v, sizeof(v), "%.9g", (double) o->base.value);
        snprintf(d, sizeof(d), "%.9g", (double) o->defaultValue);
        if (o->configIni.engaged) snprintf(i, sizeof(i), "%.9g", (double) o->configIni.value);
        snprintf(out, outLen, "f32 %d %s %s %s %d", o->base.engaged ? 1 : 0, v, d, i, o->isVolatile ? 1 : 0);
        return;
    }
    default:
        snprintf(out, outLen, "%s", f->kind == OPTI_STR ? "str" : "wstr");
        return;
    }
}

// ------------------------------------------------------------- IPC files --

static void WriteFileAtomically(const wchar_t* path, const char* body, size_t len) {
    wchar_t temp[MAX_PATH];
    if (_snwprintf(temp, MAX_PATH, L"%ls.tmp", path) < 0) return;
    HANDLE h = CreateFileW(temp, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE) return;
    DWORD written = 0;
    BOOL ok = WriteFile(h, body, (DWORD) len, &written, NULL);
    CloseHandle(h);
    if (ok && written == len) MoveFileExW(temp, path, MOVEFILE_REPLACE_EXISTING);
    else DeleteFileW(temp);
}

static char* ReadWholeFile(const wchar_t* path) {
    HANDLE h = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                           NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE) return NULL;
    DWORD size = GetFileSize(h, NULL);
    if (size == INVALID_FILE_SIZE || size > (1 << 20)) { CloseHandle(h); return NULL; }
    char* buf = (char*) HeapAlloc(GetProcessHeap(), 0, size + 1);
    DWORD got = 0;
    if (!buf || !ReadFile(h, buf, size, &got, NULL)) {
        if (buf) HeapFree(GetProcessHeap(), 0, buf);
        CloseHandle(h);
        return NULL;
    }
    buf[got] = 0;
    CloseHandle(h);
    return buf;
}

/** Every field, one line each, into a heap buffer the caller frees. */
static char* FormatAllFields(size_t* lenOut) {
    size_t cap = kFieldCount * 160 + 256, used = 0;
    char* body = (char*) HeapAlloc(GetProcessHeap(), 0, cap);
    if (!body) return NULL;
    used += (size_t) snprintf(body, cap, "fields %u\n", (unsigned) kFieldCount);
    for (size_t i = 0; i < kFieldCount && g_config; i++) {
        char line[200];
        FormatField(&kFields[i], line, sizeof(line));
        int n = snprintf(body + used, cap - used, "%s %s\n", kFields[i].name, line);
        if (n < 0 || (size_t) n >= cap - used) break;
        used += (size_t) n;
    }
    *lenOut = used;
    return body;
}

static void WriteDump() {
    size_t used = 0;
    char* body = FormatAllFields(&used);
    if (!body) return;
    WriteFileAtomically(g_dumpPath, body, used);
    HeapFree(GetProcessHeap(), 0, body);
}

/** FNV-1a, to see whether the settings moved since the last snapshot. */
static unsigned long long Fnv1a(const char* data, size_t len) {
    unsigned long long hash = 1469598103934665603ULL;
    for (size_t i = 0; i < len; i++) { hash ^= (unsigned char) data[i]; hash *= 1099511628211ULL; }
    return hash;
}

static unsigned long long g_valuesHash = 0;

/**
 * OptiScaler's settings as they are now, read-only: what the panel shows for a
 * running game, including what was changed in OptiScaler's own menu and not
 * saved. Rewritten only when something moved. `first` also writes the
 * snapshot as loaded, which tells the panel those changes apart from values
 * OptiScaler resolved itself at startup.
 */
static void WriteValues(bool first) {
    size_t used = 0;
    char* body = FormatAllFields(&used);
    if (!body) return;
    unsigned long long hash = Fnv1a(body, used);
    if (first) WriteFileAtomically(g_loadedPath, body, used);
    if (first || hash != g_valuesHash) {
        WriteFileAtomically(g_valuesPath, body, used);
        g_valuesHash = hash;
    }
    HeapFree(GetProcessHeap(), 0, body);
}

static void WriteStatus() {
    const OptiPluginApi* api = g_api;
    char dir[MAX_PATH];
    NarrowCopy(dir, sizeof(dir), g_dir);
    char body[2048];
    int n = snprintf(body, sizeof(body),
                     "schema %d\n"
                     "built_for %s\n"
                     "status %s\n"
                     "api %u\n"
                     "reinit_available %d\n"
                     "seq %lld\n"
                     "applied %d\n"
                     "reinit %d\n"
                     "config %p\n"
                     "fields %u\n"
                     "pid %lu\n"
                     "dir %s\n"
                     "heartbeat %llu\n"
                     "error %s\n",
                     STATUS_SCHEMA, DECKY_BUILT_FOR, g_status, api ? api->version : 0u,
                     (api && OPTI_API_HAS(api, ReInitUpscalers)) ? 1 : 0, g_seq, g_applied, g_reinit,
                     (void*) g_config, (unsigned) kFieldCount, (unsigned long) GetCurrentProcessId(),
                     dir, (unsigned long long) GetTickCount64(), g_error);
    if (n > 0) WriteFileAtomically(g_statusPath, body, (size_t) n);
}

/** Ask OptiScaler to re-create its upscalers, through the plugin API. */
static bool RequestReInit() {
    const OptiPluginApi* api = g_api;
    if (!api || !OPTI_API_HAS(api, ReInitUpscalers) || !g_plugin) {
        SetError("this OptiScaler has no ReInitUpscalers (plugin API %u)", api ? api->version : 0u);
        g_reinit = 0;
        return false;
    }
    // False while OptiScaler's menu is not initialised yet (no frame drawn); the
    // request is kept and applied once it is, so it still counts as accepted.
    bool accepted = api->ReInitUpscalers(g_plugin);
    g_reinit = accepted ? 1 : 0;
    LogLine("ReInitUpscalers -> %s", accepted ? "accepted" : "deferred until the first frame");
    return true;
}

/** Apply one command file; returns how many commands took effect. */
static int ApplyCommands(char* text, long long* seqOut) {
    int applied = 0;
    bool wantReInit = false, wantDump = false;
    g_error[0] = 0;
    char* cursor = text;
    while (*cursor) {
        char* line = cursor;
        while (*cursor && *cursor != '\n' && *cursor != '\r') cursor++;
        if (*cursor) *cursor++ = 0;
        while (*cursor == '\n' || *cursor == '\r') cursor++;
        while (*line == ' ' || *line == '\t') line++;
        if (!*line || *line == '#') continue;

        char verb[16] = "", a[96] = "", b[16] = "", c[96] = "";
        int parts = sscanf(line, "%15s %95s %15s %95s", verb, a, b, c);
        if (parts >= 2 && strcmp(verb, "seq") == 0) {
            *seqOut = atoll(a);
        } else if (parts >= 4 && strcmp(verb, "set") == 0) {
            if (SetField(a, b, c)) applied++;
        } else if (parts >= 2 && strcmp(verb, "unset") == 0) {
            if (UnsetField(a)) applied++;
        } else if (strcmp(verb, "reinit") == 0) {
            wantReInit = true;
        } else if (strcmp(verb, "dump") == 0) {
            wantDump = true;
        } else {
            SetError("unknown command: %.60s", line);
        }
    }
    // After every write, so the re-created upscaler reads the new values.
    if (wantReInit && RequestReInit()) applied++;
    if (wantDump) WriteDump();
    return applied;
}

/** Read the command file if it carries a sequence number not seen yet. */
static void PollCommands() {
    char* text = ReadWholeFile(g_cmdPath);
    if (!text) return;
    long long seq = -1;
    // The seq line comes first; peek at it before applying anything.
    sscanf(text, "seq %lld", &seq);
    if (seq >= 0 && seq != g_seq) {
        g_applied = ApplyCommands(text, &seq);
        g_seq = seq;
        LogLine("command %lld: %d applied%s%s", seq, g_applied, g_error[0] ? ", " : "", g_error);
        WriteStatus();
    }
    HeapFree(GetProcessHeap(), 0, text);
}

// ---------------------------------------------------------------- worker --

static void SetPaths(HMODULE opti) {
    wchar_t path[MAX_PATH];
    DWORD len = GetModuleFileNameW(opti, path, MAX_PATH);
    if (!len || len >= MAX_PATH) return;
    wchar_t* slash = wcsrchr(path, L'\\');
    if (!slash) return;
    *slash = 0;
    wcsncpy(g_dir, path, MAX_PATH - 1);
    _snwprintf(g_logPath, MAX_PATH, L"%ls\\decky_optiscaler_live.log", g_dir);
    _snwprintf(g_cmdPath, MAX_PATH, L"%ls\\decky_optiscaler_live.cmd", g_dir);
    _snwprintf(g_statusPath, MAX_PATH, L"%ls\\decky_optiscaler_live.status", g_dir);
    _snwprintf(g_dumpPath, MAX_PATH, L"%ls\\decky_optiscaler_live.dump", g_dir);
    _snwprintf(g_valuesPath, MAX_PATH, L"%ls\\decky_optiscaler_live.values", g_dir);
    _snwprintf(g_loadedPath, MAX_PATH, L"%ls\\decky_optiscaler_live.loaded", g_dir);
}

static DWORD WINAPI Worker(LPVOID) {
    // OptiScaler calls OptiPluginInit right after loading this module; give it
    // a moment so the module is found through the API rather than by exports.
    for (int i = 0; i < 50 && !g_api; i++) Sleep(100);

    for (int i = 0; i < 300 && !g_opti; i++) {
        g_opti = FindOptiScalerModule();
        if (!g_opti) Sleep(200);
    }
    if (!g_opti) return 0;  // no OptiScaler in this process: nothing to report to
    SetPaths(g_opti);
    {
        const OptiPluginApi* api = g_api;
        char module[MAX_PATH];
        NarrowCopy(module, sizeof(module), g_dir);
        LogLine("attached to OptiScaler in %s, plugin API %u (%s); built for OptiScaler %s", module,
                api ? api->version : 0u, api ? "OptiPluginInit called" : "no OptiPluginInit",
                DECKY_BUILT_FOR);
    }

    strcpy(g_status, "searching");
    DWORD lastStatus = 0;
    for (int attempt = 0; !g_config; attempt++) {
        g_config = FindConfig(g_opti);
        if (g_config) break;
        if (attempt == 0 || attempt % 30 == 0) WriteStatus();
        if (attempt >= DECKY_SEARCH_ATTEMPTS) {  // ~30 s: the layout does not match this build
            strcpy(g_status, "failed");
            SetError("OptiScaler's Config was not found: the ASI's layout does not match this build "
                     "(built for OptiScaler %s; %u data sections, %llu candidate pointers checked)",
                     DECKY_BUILT_FOR, g_scanSections, g_scanPointers);
            WriteStatus();
            return 0;
        }
        Sleep(200);
    }
    strcpy(g_status, "ready");
    LogLine("Config found at %p (%u fields)", (void*) g_config, (unsigned) kFieldCount);
    WriteValues(true);
    WriteStatus();

    for (;;) {
        PollCommands();
        DWORD now = GetTickCount();
        if (now - lastStatus >= 1000) {
            WriteValues(false);
            WriteStatus();
            lastStatus = now;
        }
        Sleep(100);
    }
}

// ------------------------------------------------------------- exports --

extern "C" __declspec(dllexport) bool OptiPluginInit(const OptiPluginApi* api, OptiPlugin* plugin) {
    if (!api) return false;
    g_plugin = plugin;
    g_api = api;
    return true;
}

// OptiScaler calls this in every ASI it loads (before OptiPluginInit).
extern "C" __declspec(dllexport) void InitializeASI() {}

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        g_self = instance;
        DisableThreadLibraryCalls(instance);
        HANDLE thread = CreateThread(NULL, 0, Worker, NULL, 0, NULL);
        if (thread) CloseHandle(thread);
    }
    return TRUE;
}
