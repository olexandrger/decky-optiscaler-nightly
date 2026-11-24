/**
 * The Quick Access panel's live controls, against a backend that behaves like
 * the real one: read_config returns a *copy* of the ini, write_config applies
 * the changes to it the way the Python writer does (rejecting anything the
 * schema will not take), and the live status is re-polled with a fresh object
 * every time.
 *
 * The thing under test is that a control shows what was picked. A dropdown
 * whose value is derived from the ini has to agree with the choice the moment
 * it is made, keep agreeing across a live-status poll, and still agree after
 * the debounced write has come back.
 */
import React from "react";
import { createRoot } from "react-dom/client";
import { act } from "react";
import { fixtures } from "@decky/api";
import { Navigation } from "@decky/ui";
import { GENERATED_OPTIONS } from "../src/config/generatedSchema";
import { QuickPanel } from "../src/components/QuickPanel";
import { NowPlaying } from "../src/components/NowPlaying";
import { nextFpsOverlay } from "../src/components/FpsOverlayButton";
import { BasicPanel } from "../src/components/BasicPanel";
import { useConfig } from "../src/hooks/useConfig";
import { useLiveStatus } from "../src/hooks/useLiveStatus";
import { forgetWrites } from "../src/config/writeRecord";

// ---- a backend that keeps state -------------------------------------------
const ini: Record<string, Record<string, string>> = {};
for (const o of GENERATED_OPTIONS) {
  ini[o.section] = ini[o.section] ?? {};
  ini[o.section][o.key] = "auto";
}
ini.FrameGen.Enabled = "true";
ini.FrameGen.FGInput = "fsrfg";
ini.FrameGen.FGOutput = "fsrfg";
ini.Upscalers.Dx12Upscaler = "fsr31";
ini.FSR.Fsr4Update = "false";

const SCHEMA = new Map(GENERATED_OPTIONS.map((o) => [`${o.section}.${o.key}`, o]));

/** The same check py_modules/optiscaler/schema.py:valid() makes. */
function accepts(section: string, key: string, value: string) {
  const meta: any = SCHEMA.get(`${section}.${key}`);
  if (!meta || value === "auto") return true;
  // schema.INDEX_RANGES: the reference ini names the FFX generators and FSR
  // versions the reference build had, but both lists are per-game, so these are
  // indexes rather than the closed enums the ini makes them look like.
  if (section === "FSR" && (key === "FGIndex" || key === "UpscalerIndex")) {
    const index = Number(value);
    return Number.isInteger(index) && index >= 0 && index <= 15;
  }
  if (meta.type === "bool") return ["true", "false"].includes(value.toLowerCase());
  if (meta.type === "enum") return !meta.options?.length || meta.options.includes(value);
  if (meta.type === "int" || meta.type === "float") return Number.isFinite(Number(value));
  return true;
}

/** The two device stories this harness needs: the Deck the INT8 route is for,
 *  and a card OptiScaler's own GPU checks already cover. Neither is fixed
 *  inside a test — the FSR version section plays both in turn. */
const DECK_GPU = {
  name: "Van Gogh (Steam Deck)", vendor: "amd", generation: "RDNA2",
  fsr4: "experimental" as const,
};
const RDNA4_GPU = {
  name: "Radeon RX 9070 XT", vendor: "amd", generation: "RDNA4", fsr4: "native" as const,
};

const detail = {
  path: "/games/Cyberpunk 2077",
  name: "Cyberpunk 2077",
  target: "/games/Cyberpunk 2077/bin/x64",
  install: {
    path: "/games/Cyberpunk 2077/bin/x64",
    installed: true, filename: "dxgi.dll", managed: true, version: "0.9.4",
    ini_present: true, log_present: true, candidates: ["dxgi.dll"], extra_proxies: [],
    backed_up: [],
    // The library the shipped OptiScaler release actually carries, which is a
    // later version than the reference ini it documents itself with.
    fsr4: {
      files: {}, ready: false, required: [],
      ffx: { present: true, name: "amd_fidelityfx_upscaler_dx12.dll",
             version: "4.1.1.2740", fsr4_capable: true },
    },
  },
  gpu: DECK_GPU,
  wiki_entry: null,
};

const pressedKeys: string[] = [];
let installedFsr4Build = "bundled";
let fsr4SetupFails = true;
Object.assign(fixtures, {
  find_running_game: () => ({ found: true, appid: "1091500", detail }),
  get_auto_plan: { recommendation: { matched: false }, plan: { available: false } },
  get_pref: { key: "auto", value: null },
  // The Steam Deck presets: the bundled upscaler with one of its models
  // (installer.enable_fsr4_deck). The DLL stays the bundled one.
  set_fsr4_build: (_dir: string, model: string) => {
    if (fsr4SetupFails) return { ok: false, error: "Close the game before changing the FSR 4 build." };
    const [index, support] = model === "rdna2" ? ["2", "3"] : ["3", "4"];
    installedFsr4Build = "bundled";
    setIni({ Upscalers: { Dx12Upscaler: "ffx", Dx11Upscaler: "ffx_12", VulkanUpscaler: "ffx_12" },
      FSR: { Fsr4GpuSupport: support, Fsr4Model: index, UpscalerIndex: "0" } });
    return { ok: true };
  },
  read_config: () => ({
    fsr4_build: installedFsr4Build,
    ok: true, path: "/x/OptiScaler.ini",
    values: JSON.parse(JSON.stringify(ini)), modified: 1,
  }),
  write_config: (_dir: string, changes: any[]) => {
    const applied: any[] = [];
    const rejected: any[] = [];
    for (const c of changes) {
      const value = c.value === null || c.value === "" ? "auto" : String(c.value);
      if (!accepts(c.section, c.key, value)) {
        rejected.push({ ...c, value });
        continue;
      }
      ini[c.section] = ini[c.section] ?? {};
      ini[c.section][c.key] = value;
      applied.push({ section: c.section, key: c.key, value });
    }
    // As live.apply_changes: frame generation on is OptiScaler's FG key.
    const keys = applied.filter((c) => c.section === "FrameGen" && c.key === "Enabled" && c.value === "true")
      .map(() => ({ ini_key: "FrameGen.Enabled", shortcut: "FGShortcutKey" }));
    return { ok: true, applied, rejected, live: { sent: true, deferred: [], attached: true, keys } };
  },
  get_live_status: () => ({
    asi_installed: true, asi_available: true, asi_current: true,
    attached: true, ready: true, state: "ready",
    load_enabled: true, loaded_by_optiscaler: true, error: null, seq: "1",
    can_reinit: true, can_change_ffx_upscaler: true, api: 3,
    age: 1.0, dir: "/x", dir_matches: true,
    live_keys: ["FrameGen.Enabled", "FSR.UpscalerIndex", "FSR.Fsr4Model", "Menu.ShowFps"],
    schema: 8,
    // OptiScaler's own names and default (live_fields.json, from its menu and Config.h).
    fps_overlay_types: ["Just FPS", "Simple", "Detailed", "Detailed + Graph", "Full", "Full + Graph",
      "Low latency timings", "Custom"],
    fps_overlay_default: 5,
  }),
  press_live_keys: (_dir: string, actions: any[]) => {
    pressedKeys.push(...actions.map((a) => a.shortcut));
    return { ok: true, pressed: actions.map((a) => a.ini_key), already: [], deferred: [] };
  },
});

// ---- harness ---------------------------------------------------------------
const errors: string[] = [];
const originalError = console.error;
console.error = (...args: any[]) => {
  errors.push(args.map(String).join(" "));
  originalError(...args);
};

let failures = 0;
function check(label: string, actual: unknown, expected: unknown) {
  const ok = String(actual) === String(expected);
  if (!ok) failures += 1;
  console.log(`  ${ok ? "[ok]" : "[FAIL]"} ${label}${ok ? "" : ` — got ${actual}, want ${expected}`}`);
}

async function settle(ms = 5) {
  for (let i = 0; i < 8; i++) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, ms));
    });
  }
}

/**
 * Rewrite the ini behind the plugin's back, as a fresh session would find it.
 *
 * The plugin remembers what it wrote so that reopening the panel does not read
 * a file the write has not reached yet, and that record outlives a render root.
 * Editing the fixture directly is the harness playing a different session, so
 * it has to drop the record too — otherwise the previous case's writes are
 * still, correctly, winning.
 */
function setIni(entries: Record<string, Record<string, string>>) {
  for (const [section, keys] of Object.entries(entries)) {
    ini[section] = { ...(ini[section] ?? {}), ...keys };
  }
  forgetWrites(detail.install.path);
}

/** The same trick for the device: which GPU is under the game decides whether
 *  an FSR 4 version is reachable through OptiScaler's own checks or only
 *  through the INT8 override, and the fixture is one object either way. */
function setGpu(gpu: typeof detail.gpu) {
  detail.gpu = gpu;
  forgetWrites(detail.install.path);
}

const all = (node: Element, selector: string) => Array.from(node.querySelectorAll(selector));
const control = (host: Element, label: string) =>
  all(host, `[data-mock][data-label="${label}"]`)[0];
const selected = (host: Element, label: string) =>
  control(host, label)?.getAttribute("data-selected");

/**
 * The game page's settings (BasicPanel, wired as NowPlaying and GameDetail wire
 * it). The Quick Access panel no longer shows the FG pair, the FFX FG version or
 * the full upscaler preset list; this is where those are, and tested.
 */
function FullPage() {
  const config = useConfig(detail.install.path, true, null);
  const { status: live } = useLiveStatus(detail.install.path, true);
  if (config.loading) return <div>loading</div>;
  return (
    <BasicPanel values={config.values} gpu={detail.gpu} live={live} ffx={detail.install.fsr4?.ffx}
      targetDir={detail.install.path} fsr4Build={config.fsr4Build}
      onEnableFsr4={config.enableFsr4} onApply={config.setOptions} />
  );
}

async function mount(kind: "quick" | "page") {
  const node = document.createElement("div");
  document.body.appendChild(node);
  const r = createRoot(node);
  await act(async () => {
    r.render(kind === "page" ? <FullPage /> : (
      <QuickPanel runningGame={{ appid: 1091500, name: "Cyberpunk 2077", gameid: "1091500" }}
        onOpenManager={() => {}} />
    ));
  });
  await settle();
  return { node, root: r };
}

(async () => {
  const page = (await mount("page")).node;
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => {
    root.render(
      <QuickPanel
        runningGame={{ appid: 1091500, name: "Cyberpunk 2077", gameid: "1091500" }}
        onOpenManager={() => {}}
      />
    );
  });
  await settle();

  console.log("=== the upscaler dropdown ===");
  check("opens on what the ini says", selected(page, "Override upscaler with"), "fsr31");
  const upscaler = control(page, "Override upscaler with");
  await act(async () => {
    (all(upscaler, '[data-opt-value="xess"]')[0] as HTMLElement).click();
  });
  await settle();
  check("shows the pick straight away", selected(page, "Override upscaler with"), "xess");
  // Past the debounce, so the write has been made and answered.
  await settle(200);
  check("still shows it after the write", selected(page, "Override upscaler with"), "xess");
  check("the ini has it", ini.Upscalers.Dx12Upscaler, "xess");

  console.log("=== the FFX FG version dropdown ===");
  const ffx = control(page, "FFX FG version");
  console.log(`  options: ${all(ffx, "[data-opt]").map((n) => n.textContent).join(", ")}`);
  check("the generators are the reference ini's", all(ffx, "[data-opt]").length,
    (SCHEMA.get("FSR.FGIndex")?.options ?? []).length);
  await act(async () => {
    (all(ffx, '[data-opt-value="1"]')[0] as HTMLElement).click();
  });
  await settle(200);
  check("shows the second generator", selected(page, "FFX FG version"), "1");
  check("and the ini records it", ini.FSR.FGIndex, "1");

  console.log("=== the FSR version dropdown ===");
  // The second half of the upscaler question: the FidelityFX backend runs
  // every FSR from 2.3 to 4, so which one is a separate choice. The list is
  // the reference ini's, with the newest named after the library in the folder.
  // Only the FidelityFX backend has versions, so back to it first.
  check("XeSS has no FSR version", Boolean(control(page, "FSR version")), false);
  await act(async () => {
    (all(control(page, "Override upscaler with"), '[data-opt-value="fsr31"]')[0] as HTMLElement).click();
  });
  await settle(200);
  const fsrVersion = control(page, "FSR version");
  check("the versions are the reference ini's, the newest named after the library",
    all(fsrVersion, "[data-opt]").map((n) => n.getAttribute("data-opt-value")).join(",")
      + " " + all(fsrVersion, "[data-opt]")[0]?.textContent,
    "0,1,2 FSR 4.1.1");
  // Start from the FSR 3.X preset's position, where the FSR 4 upgrade path is
  // off -- asking for FSR 4 from here has to switch it back on, or OptiScaler
  // falls back to FSR 3 without saying so.
  setIni({ FSR: { Fsr4Update: "false" } });
  await act(async () => {
    (all(fsrVersion, '[data-opt-value="2"]')[0] as HTMLElement).click();
  });
  await settle(200);
  check("shows the version picked", selected(page, "FSR version"), "2");
  check("and the ini records it", ini.FSR.UpscalerIndex, "2");
  check("an older version leaves the FSR 4 upgrade path alone",
    ini.FSR.Fsr4Update, "false");

  const toFsr4 = control(page, "FSR version");
  await act(async () => {
    (all(toFsr4, '[data-opt-value="0"]')[0] as HTMLElement).click();
  });
  await settle(200);
  check("asking for FSR 4 records the version", ini.FSR.UpscalerIndex, "0");
  // But not the upgrade path, on the device this plugin exists for: v0.9.4's
  // notes say setting it on an unsupported GPU forces FP8 and OptiScaler
  // answers with its internal FSR 3 fallback, and the maintainer's answer to
  // exactly this device on the tracker was that the INT8 override is the only
  // option it needs. Writing it here would be the bug, not the fix.
  check("and leaves the upgrade path alone where INT8 is the route",
    ini.FSR.Fsr4Update, "false");

  // On a device OptiScaler's own GPU checks cover, the same click has to write
  // it — that is what this section has always covered, and it still holds.
  setGpu(RDNA4_GPU);
  const onAmd = control(page, "FSR version");
  await act(async () => {
    (all(onAmd, '[data-opt-value="2"]')[0] as HTMLElement).click();
  });
  await settle(200);
  await act(async () => {
    (all(control(page, "FSR version"), '[data-opt-value="0"]')[0] as HTMLElement).click();
  });
  await settle(200);
  // OptiScaler 10 removed Fsr4Update: FSR 4 runs wherever the GPU supports
  // it, so the version pick is the whole write and no upgrade flag goes in.
  check("asking for FSR 4 on a supported GPU picks the FSR 4 index",
    ini.FSR.UpscalerIndex, "0");
  check("and writes no upgrade flag OptiScaler 10 no longer has",
    ini.FSR.Fsr4Update === "true", false);
  setGpu(DECK_GPU);

  console.log("=== the Steam Deck presets on that same Deck ===");
  // The other half of the device story: the presets the INT8 route needs,
  // recognised from the ini like any other, and the one control they must not
  // offer. The backend switch is live and the model is not — it is read while
  // the upscaler is created — so a switch under these presets would move the
  // game now and leave the thing that was asked for waiting for a restart.
  setIni({
    Upscalers: { Dx12Upscaler: "ffx" },
    FSR: { Fsr4GpuSupport: "4", Fsr4Model: "3", UpscalerIndex: "0" },
  });
  const host5 = document.createElement("div");
  document.body.appendChild(host5);
  const root5 = createRoot(host5);
  await act(async () => {
    root5.render(<FullPage />);
  });
  await settle();
  check("the Lite preset is the one recognised",
    selected(host5, "Override upscaler with"), "fsr4-lite");
  check("both Deck presets are offered",
    all(control(host5, "Override upscaler with"), '[data-opt-value="fsr4-rdna2"]').length, 1);
  check("and the panel says the model waits for a launch",
    host5.textContent!.includes("FSR 4 Lite ready for the next launch"), true);
  await act(async () => {
    (all(control(host5, "Override upscaler with"),
      '[data-opt-value="fsr4-rdna2"]')[0] as HTMLElement).click();
  });
  await settle(200);
  check("picking one offers no switch that cannot apply it",
    host5.textContent!.includes("Switch now"), false);
  check("a rejected setup reports the reason", host5.textContent!.includes("Close the game"), true);
  check("a rejected setup does not claim the new model", selected(host5, "Override upscaler with"), "fsr4-lite");
  const fgBeforeSetup = JSON.stringify(ini.FrameGen);
  fsr4SetupFails = false;
  await act(async () => {
    (all(control(host5, "Override upscaler with"), '[data-opt-value="fsr4-rdna2"]')[0] as HTMLElement).click();
  });
  await settle(200);
  check("successful setup selects the RDNA2 preset", selected(host5, "Override upscaler with"), "fsr4-rdna2");
  check("and writes its model", ini.FSR.Fsr4Model, "2");
  check("setup leaves frame generation alone", JSON.stringify(ini.FrameGen), fgBeforeSetup);
  check("setup says next launch", host5.textContent!.includes("FSR 4 RDNA2 ready for the next launch"), true);
  check("no community DLL warning is left", host5.textContent!.includes("anti-cheat"), false);

  // Leaving a Deck preset for the plain FSR 4 one must take its model with it,
  // or the ini still matches the Deck preset and OptiScaler keeps running it.
  setIni({ Upscalers: { Dx12Upscaler: "ffx" }, FSR: { Fsr4GpuSupport: "4", Fsr4Model: "3", UpscalerIndex: "0" } });
  await settle(200);
  await act(async () => {
    (all(control(host5, "Override upscaler with"), '[data-opt-value="fsr4"]')[0] as HTMLElement)?.click();
  });
  await settle(300);
  check("leaving Lite for FSR 3.X/4 clears the model", ini.FSR.Fsr4Model, "auto");
  check("and the panel shows what was picked", selected(host5, "Override upscaler with"), "fsr4");

  await act(async () => root5.unmount());
  setIni({ FSR: { Fsr4GpuSupport: "auto", Fsr4Model: "auto" } });
  await settle(200);

  console.log("=== a 4.0.2 upscaler in the folder ===");
  // The other half of the version question, and the reason the INT8 preset is
  // not offered blindly. A 4.0.2 build is below the 4.1.1 OptiScaler asks for
  // before it will offer FSR 4 off the override, so on the same Deck the upgrade
  // path is what reaches it — the opposite of what the same click does with the
  // released 4.1.1 SDK in the folder.
  installedFsr4Build = "bundled";
  detail.install.fsr4.ffx.version = "4.0.2.0";
  setIni({
    Upscalers: { Dx12Upscaler: "ffx" },
    FSR: { Fsr4GpuSupport: "auto", Fsr4Model: "auto", UpscalerIndex: "1" },
  });
  const host6 = document.createElement("div");
  document.body.appendChild(host6);
  const root6 = createRoot(host6);
  await act(async () => {
    root6.render(<FullPage />);
  });
  await settle();
  check("the Deck preset can replace an older DLL",
    all(control(host6, "Override upscaler with"), '[data-opt-value="fsr4-lite"]').length, 1);
  await act(async () => {
    (all(control(host6, "FSR version"), '[data-opt-value="0"]')[0] as HTMLElement).click();
  });
  await settle(200);
  check("and asking for FSR 4 picks the FSR 4 index",
    ini.FSR.UpscalerIndex, "0");
  check("without an upgrade flag OptiScaler 10 no longer has",
    ini.FSR.Fsr4Update === "true", false);
  await act(async () => root6.unmount());
  detail.install.fsr4.ffx.version = "4.1.1.2740";
  setIni({ FSR: { Fsr4Update: "false", UpscalerIndex: "0" } });

  console.log("=== the Quick Access upscaler: family, then the model under it ===");
  const opts = (node: Element, label: string) =>
    all(control(node, label) as Element, "[data-opt]").map((n) => n.getAttribute("data-opt-value")).join(",");
  const pick = async (node: Element, label: string, value: string) => {
    await act(async () => {
      (all(control(node, label) as Element, `[data-opt-value="${value}"]`)[0] as HTMLElement)?.click();
    });
    await settle(200);
  };
  setIni({ Upscalers: { Dx12Upscaler: "ffx", Dx11Upscaler: "ffx_12", VulkanUpscaler: "ffx_12" },
    FSR: { Fsr4GpuSupport: "4", Fsr4Model: "3", UpscalerIndex: "0" } });
  let quick = await mount("quick");
  check("a Deck on FSR 4 Lite reads as FSR 4", selected(quick.node, "Upscaler"), "fsr4");
  check("no DLSS on a Deck", opts(quick.node, "Upscaler"), "auto,fsr4,fsr3,fsr22,xess");
  check("its model is the Deck preset", selected(quick.node, "FSR 4 model"), "fsr4-lite");
  check("and both Deck models are offered", opts(quick.node, "FSR 4 model"), "fsr4-lite,fsr4-rdna2");
  await pick(quick.node, "FSR 4 model", "fsr4-rdna2");
  check("picking RDNA2 sets that model up", `${ini.FSR.Fsr4Model}/${ini.FSR.Fsr4GpuSupport}`, "2/3");
  check("and says the game needs a restart for it",
    (quick.node.textContent ?? "").includes("Restart game to apply"), true);
  await pick(quick.node, "Upscaler", "fsr3");
  // The reference ini's versions are FSR 4, 3.1 and 2.3: the first FSR 3 is index 1.
  check("FSR 3 is the FidelityFX backend on the first FSR 3 version",
    `${ini.Upscalers.Dx12Upscaler}/${ini.FSR.UpscalerIndex}`, "ffx/1");
  check("its versions are FSR 3 and older ones", opts(quick.node, "FSR version"), "1,2");
  await pick(quick.node, "FSR version", "2");
  check("a version pick is the FidelityFX index", ini.FSR.UpscalerIndex, "2");
  check("and stays FSR 3", selected(quick.node, "Upscaler"), "fsr3");
  await pick(quick.node, "Upscaler", "xess");
  check("XeSS is XeSS", ini.Upscalers.Dx12Upscaler, "xess");
  check("with OptiScaler's XeSS models", opts(quick.node, "XeSS model"), "0,1,2,3,4,5");
  await pick(quick.node, "XeSS model", "1");
  check("a model pick writes XeSS's network model", ini.XeSS.NetworkModel, "1");
  await pick(quick.node, "Upscaler", "fsr4");
  check("back to FSR 4 on a Deck goes through the Deck setup (Lite)",
    `${ini.Upscalers.Dx12Upscaler}/${ini.FSR.Fsr4Model}`, "ffx/3");
  await act(async () => quick.root.unmount());

  setGpu({ ...RDNA4_GPU });
  quick = await mount("quick");
  await pick(quick.node, "Upscaler", "fsr3");
  await pick(quick.node, "Upscaler", "fsr4");
  check("FSR 4 elsewhere is the plain preset", `${ini.Upscalers.Dx12Upscaler}/${ini.FSR.UpscalerIndex}`, "ffx/0");
  check("with OptiScaler's FSR 4 models", opts(quick.node, "FSR 4 model"), "auto,0,1,2,3");
  await pick(quick.node, "FSR 4 model", "0");
  check("a model pick writes the FSR 4 model", ini.FSR.Fsr4Model, "0");
  await act(async () => quick.root.unmount());

  setGpu({ ...RDNA4_GPU, vendor: "nvidia" } as any);
  quick = await mount("quick");
  await pick(quick.node, "Upscaler", "dlss");
  check("DLSS on an Nvidia GPU", ini.Upscalers.Dx12Upscaler, "dlss");
  check("starts on the game's own preset", selected(quick.node, "DLSS preset"), "0");
  await pick(quick.node, "DLSS preset", "11");
  check("a preset turns the override on and picks it",
    `${ini.DLSS.RenderPresetOverride}/${ini.DLSS.RenderPresetForAll}`, "true/11");
  await pick(quick.node, "DLSS preset", "0");
  check("Default turns the override off", ini.DLSS.RenderPresetOverride, "false");
  await act(async () => quick.root.unmount());
  setGpu(DECK_GPU);
  setIni({ Upscalers: { Dx12Upscaler: "ffx", Dx11Upscaler: "ffx_12", VulkanUpscaler: "ffx_12" },
    FSR: { Fsr4GpuSupport: "auto", Fsr4Model: "auto", UpscalerIndex: "0" }, DLSS: { RenderPresetOverride: "auto" } });

  console.log("=== a running game whose FSR 4 model differs from the ini ===");
  // The ini holds the Deck's Lite preset; the running game has the model at
  // Auto (OptiScaler adjusts it itself, or it was changed in its menu). The
  // panel used to lay that over the ini, Lite stopped matching, and the
  // upscaler read as plain "FSR 3.X/4" with no model to pick.
  setIni({ Upscalers: { Dx12Upscaler: "ffx", Dx11Upscaler: "ffx_12", VulkanUpscaler: "ffx_12" },
    FSR: { Fsr4GpuSupport: "4", Fsr4Model: "3", UpscalerIndex: "0" } });
  const runningSnapshot = { ok: true, ready: true, created: null, unsaved: ["FSR.Fsr4Model"],
    values: { "Upscalers.Dx12Upscaler": "ffx", "FSR.Fsr4Model": "auto", "FSR.Fsr4GpuSupport": "4",
      "FSR.UpscalerIndex": "0", "FrameGen.Enabled": "true" } };
  fixtures.get_running_config = () => runningSnapshot;
  quick = await mount("quick");
  await settle(100);
  check("the upscaler still reads as the configured FSR 4", selected(quick.node, "Upscaler"), "fsr4");
  check("with its Deck model", selected(quick.node, "FSR 4 model"), "fsr4-lite");
  await pick(quick.node, "Upscaler", "fsr3");
  check("FSR 3 can be picked", selected(quick.node, "Upscaler"), "fsr3");
  await pick(quick.node, "Upscaler", "fsr4");
  check("and FSR 4 again comes back as FSR 4 with its model, not as plain FFX",
    `${selected(quick.node, "Upscaler")}/${selected(quick.node, "FSR 4 model")}`, "fsr4/fsr4-lite");
  await act(async () => quick.root.unmount());
  delete (fixtures as any).get_running_config;
  setIni({ FSR: { Fsr4GpuSupport: "auto", Fsr4Model: "auto", UpscalerIndex: "0" } });

  console.log("=== explanations live behind the ⓘ ===");
  quick = await mount("quick");
  check("no control carries a description", all(quick.node, "[data-desc]").length, 0);
  check("the panel itself reads no paragraph",
    (quick.node.textContent ?? "").includes("presses OptiScaler's FG key"), false);
  check("each section has an ⓘ", all(quick.node, "[data-info]").map((n) => n.getAttribute("data-info")).join(","),
    "status,framegen,upscaler");
  await act(async () => { (all(quick.node, '[data-info="framegen"]')[0] as HTMLElement).click(); });
  check("pressing it shows the explanation",
    (quick.node.textContent ?? "").includes("presses OptiScaler's FG key"), true);
  check("FG input/output are not on the panel, only named", Boolean(control(quick.node, "Method")), false);
  await act(async () => { (all(quick.node, '[data-info="framegen"]')[0] as HTMLElement).click(); });
  check("and pressing it again hides it",
    (quick.node.textContent ?? "").includes("presses OptiScaler's FG key"), false);
  check("the OptiScaler menu and the overlay are the first row",
    all(quick.node, "[data-button]").slice(0, 2).map((n) => n.getAttribute("data-button")).join(","),
    "optiscaler-menu,fps-overlay");
  await act(async () => quick.root.unmount());

  console.log("=== the frame multiplier ===");
  // Only the XeSS FG output can do more than 2X; the output is chosen on the
  // game's page, so it is set in the ini and the panel reopened.
  check("the multiplier is there but off without the XeFG output",
    control(host, "Frame multiplier")?.getAttribute("data-disabled"), "true");
  setIni({ FrameGen: { FGInput: "fsrfg", FGOutput: "xefg" } });
  await act(async () => root.render(<div />));
  await act(async () => {
    root.render(<QuickPanel runningGame={{ appid: 1091500, name: "Cyberpunk 2077", gameid: "1091500" }}
      onOpenManager={() => {}} />);
  });
  await settle();
  const mult = control(host, "Frame multiplier");
  check("the multiplier is usable on XeFG", mult?.getAttribute("data-disabled"), "null");
  await act(async () => {
    (all(mult, '[data-opt-value="3"]')[0] as HTMLElement).click();
  });
  await settle(200);
  check("shows 3X", selected(host, "Frame multiplier"), "3");

  console.log("=== the frame generation toggle ===");
  const fg = all(host, '[data-mock="ToggleField"][data-label="Frame generation"]')[0];
  await act(async () => {
    (all(fg, "[data-toggle]")[0] as HTMLElement).click();
  });
  await settle(200);
  const fgNow = all(host, '[data-mock="ToggleField"][data-label="Frame generation"]')[0];
  check("turns off", all(fgNow, "[data-toggle]")[0].getAttribute("data-checked"), "false");
  check("and the ini agrees", ini.FrameGen.Enabled, "false");
  check("turning it off presses no key (OptiScaler checks the setting every frame)",
    pressedKeys.length, 0);
  let closedForKey = 0;
  const realCloseForKey = Navigation.CloseSideMenus;
  Navigation.CloseSideMenus = () => { closedForKey += 1; };
  await act(async () => {
    (all(all(host, '[data-mock="ToggleField"][data-label="Frame generation"]')[0], "[data-toggle]")[0] as
      HTMLElement).click();
  });
  await settle(200);
  check("turning it on closes Quick Access so the key reaches the game", closedForKey, 1);
  check("and presses OptiScaler's own FG key", pressedKeys.join(","), "FGShortcutKey");
  check("the ini agrees", ini.FrameGen.Enabled, "true");
  Navigation.CloseSideMenus = realCloseForKey;

  console.log("=== the FPS overlay button ===");
  // The button's own text, without its description.
  const ownText = (b: Element) =>
    (b.textContent ?? "").replace(b.querySelector("[data-desc]")?.textContent ?? "", "");
  const overlayFound = () => all(host, '[data-button="fps-overlay"]')[0] as HTMLElement | undefined;
  const overlayButton = () => {
    const found = overlayFound();
    return found ? { click: () => found.click(), textContent: ownText(found) } : undefined;
  };
  ini.Menu.ShowFps = "false";
  ini.Menu.FpsOverlayType = "auto";
  await act(async () => { overlayButton()?.click(); });
  await settle(200);
  // Off -> the first type.
  check("from off it shows OptiScaler's first type", overlayButton()?.textContent, "Overlay: Just FPS");
  check("and writes what OptiScaler's FPS keys write", `${ini.Menu.ShowFps}/${ini.Menu.FpsOverlayType}`, "true/0");
  const seen: string[] = [];
  for (let i = 0; i < 7; i++) {
    await act(async () => { overlayButton()?.click(); });
    await settle(200);
    seen.push((overlayButton()?.textContent ?? "").replace("Overlay: ", ""));
  }
  check("then every type in OptiScaler's order, Custom skipped without rows, then off",
    seen.join(" | "), "Simple | Detailed | Detailed + Graph | Full | Full + Graph | Low latency timings | Off");
  check("off keeps the type for next time", `${ini.Menu.ShowFps}/${ini.Menu.FpsOverlayType}`, "false/6");
  const liveNow = (fixtures.get_live_status as any)();
  const after6 = (rows: string) => nextFpsOverlay(
    { Menu: { ShowFps: "true", FpsOverlayType: "6", FpsOverlayRows: rows } }, liveNow);
  check("with rows chosen, Custom is a step",
    JSON.stringify(after6("fps,frametime")), JSON.stringify([
      { section: "Menu", key: "ShowFps", value: "true" },
      { section: "Menu", key: "FpsOverlayType", value: "7" }]));
  check("without, the step after Low latency timings is off",
    JSON.stringify(after6("auto")), JSON.stringify([{ section: "Menu", key: "ShowFps", value: "false" }]));
  check("an \"auto\" type is OptiScaler's default (Full + Graph)",
    JSON.stringify(nextFpsOverlay({ Menu: { ShowFps: "true", FpsOverlayType: "auto" } }, liveNow)),
    JSON.stringify([{ section: "Menu", key: "ShowFps", value: "true" },
      { section: "Menu", key: "FpsOverlayType", value: "6" }]));

  console.log("=== an ini the presets have no name for ===");
  // The Advanced page and the wiki plan write the same file. A value with no
  // preset used to be displayed as "Auto", which is the opposite of the truth.
  setIni({
    Upscalers: { Dx12Upscaler: "fsr21" },
    FSR: { Fsr4Update: "auto" },
    FrameGen: { Enabled: "true", FGInput: "dlssg", FGOutput: "xefg" },
  });
  const host3 = document.createElement("div");
  document.body.appendChild(host3);
  const root3 = createRoot(host3);
  await act(async () => {
    root3.render(<FullPage />);
  });
  await settle();
  check("the upscaler is not passed off as Auto",
    selected(host3, "Override upscaler with"), "fsr21");
  const shown = (host: Element, label: string) => {
    const node = control(host, label);
    const value = node?.getAttribute("data-selected");
    return all(node!, `[data-opt-value="${value}"]`)[0]?.textContent;
  };
  console.log(`  and is named: ${shown(host3, "Override upscaler with")}`);
  check("the FG pair is not passed off as FSR FG",
    selected(host3, "Method"), "dlssg \u2192 xefg");
  console.log(`  and is named: ${shown(host3, "Method")}`);
  await act(async () => root3.unmount());
  const quick3 = await mount("quick");
  check("the Quick Access panel does not pass it off as a family either",
    selected(quick3.node, "Upscaler"), "fsr21");
  check("and names it", shown(quick3.node, "Upscaler"), "FSR 2.1.2");
  await act(async () => quick3.root.unmount());

  console.log("=== a write the backend will not take ===");
  // A rejected write reaches neither the file nor the running game. The control
  // has to go back to what the file says and the panel has to say why — and the
  // re-read that does it must not blank the controls on the way past.
  setIni({
    Upscalers: { Dx12Upscaler: "fsr31" },
    FSR: { Fsr4Update: "false" },
    FrameGen: { Enabled: "true", FGInput: "fsrfg", FGOutput: "fsrfg" },
    XeFG: { InterpolationCount: "auto" },
  });
  const host4 = document.createElement("div");
  document.body.appendChild(host4);
  const root4 = createRoot(host4);
  await act(async () => {
    root4.render(
      <QuickPanel
        runningGame={{ appid: 1091500, name: "Cyberpunk 2077", gameid: "1091500" }}
        onOpenManager={() => {}}
      />
    );
  });
  await settle();
  // Nothing in the panel offers an unacceptable value, so one is forced in the
  // one place a real ini can also disagree with the schema: an out-of-range
  // FFX FG index, which is what the game reporting a shorter list looks like.
  let refused = true;
  fixtures.write_config = (_dir: string, changes: any[]) => {
    if (!refused) return { ok: true, applied: changes, rejected: [], live: { sent: true, deferred: [] } };
    return { ok: true, applied: [], rejected: changes, live: { sent: false, deferred: [] } };
  };
  const before = selected(host4, "Upscaler");
  const target = control(host4, "Upscaler");
  await act(async () => {
    (all(target, '[data-opt-value="xess"]')[0] as HTMLElement).click();
  });
  await settle(200);
  check("the panel stays up through the re-read",
    Boolean(control(host4, "Upscaler")), true);
  check("the control goes back to the file",
    selected(host4, "Upscaler"), before);
  check("and the refusal is reported",
    /would not accept/.test(host4.textContent ?? ""), true);

  // And once the backend accepts again, an edit sticks.
  refused = false;
  const retry = control(host4, "Upscaler");
  await act(async () => {
    (all(retry, '[data-opt-value="xess"]')[0] as HTMLElement).click();
  });
  await settle(200);
  check("a later edit sticks", selected(host4, "Upscaler"), "xess");
  await act(async () => root4.unmount());

  console.log("=== closing the panel while the write is in flight ===");
  // Closing the Quick Access panel unmounts everything, which is what you do to
  // look at the game after changing something. Reopening starts a read that can
  // be issued before the write has landed, and the panel then comes up showing
  // the value from before the change — one step behind, for ever.
  setIni({
    Upscalers: { Dx12Upscaler: "xess" },
    FSR: { Fsr4Update: "false" },
    FrameGen: { Enabled: "true", FGInput: "fsrfg", FGOutput: "fsrfg" },
  });
  let releaseWrite: () => void = () => {};
  fixtures.write_config = async (_dir: string, changes: any[]) => {
    await new Promise<void>((r) => {
      releaseWrite = r;
    });
    for (const c of changes) {
      ini[c.section] = ini[c.section] ?? {};
      ini[c.section][c.key] = String(c.value);
    }
    return { ok: true, applied: changes, rejected: [], live: { sent: true, deferred: [] } };
  };

  const open = async () => {
    const node = document.createElement("div");
    document.body.appendChild(node);
    const r = createRoot(node);
    await act(async () => {
      r.render(
        <QuickPanel
          runningGame={{ appid: 1091500, name: "Cyberpunk 2077", gameid: "1091500" }}
          onOpenManager={() => {}}
        />
      );
    });
    await settle();
    return { node, root: r };
  };

  let panel = await open();
  check("opens on XeSS", selected(panel.node, "Upscaler"), "xess");
  await act(async () => {
    (all(control(panel.node, "Upscaler"), '[data-opt-value="fsr3"]')[0] as HTMLElement)
      .click();
  });
  await settle();
  // Close it before the write has been answered, exactly as the panel closing
  // over a running game does.
  await act(async () => panel.root.unmount());
  panel = await open();
  check("reopening does not show the value from before the change",
    selected(panel.node, "Upscaler"), "fsr3");
  // Now let the write through and reopen once more: the file agrees, and the
  // record retires itself.
  await act(async () => {
    releaseWrite();
    await new Promise((r) => setTimeout(r, 5));
  });
  await settle();
  await act(async () => panel.root.unmount());
  panel = await open();
  check("and still agrees once the write lands",
    selected(panel.node, "Upscaler"), "fsr3");
  // OptiScaler 10's name for the FidelityFX backend.
  check("the file has it", ini.Upscalers.Dx12Upscaler, "ffx");

  console.log("=== the FSR version before the game answers ===");
  // The full page mounts its settings before the first live poll lands. The
  // newest FSR is named after the library sitting next to the game, which is
  // readable without the game running at all, not the reference ini's "4.0.2".
  setIni({ FSR: { UpscalerIndex: "0" } });
  const promptly = fixtures.get_live_status;
  fixtures.get_live_status = async (...args: any[]) => {
    await new Promise((r) => setTimeout(r, 60));
    return (promptly as any)(...args);
  };
  panel = await mount("page");
  const versionControl = control(panel.node, "FSR version");
  // Not the reference ini's "FSR 4.0.2": the library sitting next to the game
  // is 4.1.1, and that is readable without the game running at all.
  check("the installed library names the newest one before the game answers",
        versionControl?.getAttribute("data-shown"), "FSR 4.1.1");
  await settle(30);
  check("and keeps it once it does",
        control(panel.node, "FSR version")?.getAttribute("data-shown"), "FSR 4.1.1");
  fixtures.get_live_status = promptly;
  await act(async () => panel.root.unmount());

  console.log("=== no live-control plugin: restart to apply, and the OptiScaler menu ===");
  // The nightly build ships without the live-control ASI, so the panel's edits
  // wait for a restart and OptiScaler's own menu is the live route.
  const withAsi = fixtures.get_live_status;
  fixtures.get_live_status = () => ({
    asi_installed: false, asi_available: false, asi_current: null,
    attached: false, ready: false, state: "not_installed",
    load_enabled: false, loaded_by_optiscaler: false, error: null, seq: null,
    can_reinit: false, can_change_ffx_upscaler: false, api: null,
    age: null, live_keys: [], schema: null, dir: null, dir_matches: null,
  });
  const menuCalls: string[] = [];
  fixtures.toggle_menu = (dir: string) => {
    menuCalls.push(dir);
    return { ok: true, vk: 45 };
  };
  let closedSideMenus = 0;
  const realClose = Navigation.CloseSideMenus;
  Navigation.CloseSideMenus = () => { closedSideMenus += 1; };
  panel = await open();
  check("the explanation is behind the status ⓘ, not on the panel",
    (panel.node.textContent ?? "").includes("Changes made here apply when the game restarts"), false);
  await act(async () => { (all(panel.node, '[data-info="status"]')[0] as HTMLElement)?.click(); });
  const text = panel.node.textContent ?? "";
  check("the panel says edits apply on restart",
    text.includes("Changes made here apply when the game restarts"), true);
  check("the status pill says restart, not that something is off",
    text.includes("restart to apply") && !text.includes("live control off"), true);
  check("nothing is set apart as restart-only, since everything is",
    text.includes("Needs a restart"), false);
  const menuButton = all(panel.node, '[data-button="optiscaler-menu"]')[0] as HTMLElement | undefined;
  check("there is an OptiScaler menu button", Boolean(menuButton), true);
  await act(async () => { menuButton?.click(); });
  check("pressing it closes the Quick Access menu first", closedSideMenus, 1);
  check("and does not press the key while the menu is still open", menuCalls.length, 0);
  await settle(600);
  check("then presses the game's menu key", menuCalls.length, 1);
  check("for this game's folder", menuCalls[0], detail.install.path);
  Navigation.CloseSideMenus = realClose;
  fixtures.get_live_status = withAsi;
  await act(async () => panel.root.unmount());

  console.log("=== a game set up by upstream's Decky OptiScaler ===");
  // The two plugins never share a game: this one shows who owns it and offers
  // no controls for it.
  const ownGame = fixtures.find_running_game;
  fixtures.find_running_game = () => {
    const mine = (ownGame as any)();
    return { ...mine, detail: { ...mine.detail, install: {
      ...mine.detail.install, installed: true, managed: false,
      foreign: true, owner: "Decky OptiScaler" } } };
  };
  panel = await open();
  const foreignText = panel.node.textContent ?? "";
  check("it names the plugin that owns the game", foreignText.includes("Installed by Decky OptiScaler"), true);
  check("and says to remove it there first", foreignText.includes("remove OptiScaler from it there first"), true);
  check("no settings are offered for it",
    Boolean(control(panel.node, "Upscaler")), false);
  check("nor a set-up button", foreignText.includes("Set up OptiScaler"), false);
  fixtures.find_running_game = ownGame;
  await act(async () => panel.root.unmount());

  console.log("=== updating the plugin through Decky's installer ===");
  // No update: nothing is shown.
  fixtures.check_update = { ok: true, current: "2026.10.31-12",
    latest: "2026.10.31-12", available: false };
  panel = await open();
  check("no update, no update button", (panel.node.textContent ?? "").includes("Update to"), false);
  await act(async () => panel.root.unmount());
  // A failed check is silent too: it is not the user's problem.
  fixtures.check_update = { ok: false, error: "offline", current: "2026.10.30-11" };
  panel = await open();
  check("a failed check shows nothing", (panel.node.textContent ?? "").includes("Update to"), false);
  await act(async () => panel.root.unmount());
  // An update: the button, and the exact call to Decky's own installer.
  fixtures.check_update = { ok: true, current: "2026.10.30-11",
    latest: "2026.10.31-12", available: true,
    url: "https://github.com/x/releases/download/v1/decky-optiscaler-nightly.zip",
    sha256: "a".repeat(64), page: "https://github.com/x/releases/tag/v1" };
  const deckyCalls: any[][] = [];
  (globalThis as any).window.DeckyBackend = {
    call: async (...args: any[]) => { deckyCalls.push(args); },
  };
  panel = await open();
  const updateButton = all(panel.node, '[data-mock="ButtonItem"]')
    .find((b) => (b.textContent ?? "").includes("Update to 2026.10.31-12")) as HTMLElement | undefined;
  check("an update shows the version it updates to", Boolean(updateButton), true);
  await act(async () => { updateButton?.click(); });
  await settle(20);
  check("pressing it asks Decky's installer, with the zip, its hash and UPDATE",
    JSON.stringify(deckyCalls[0]), JSON.stringify([
      "utilities/install_plugin",
      "https://github.com/x/releases/download/v1/decky-optiscaler-nightly.zip",
      "Decky OptiScaler Nightly", "dev", "a".repeat(64), 2,
    ]));
  delete (globalThis as any).window.DeckyBackend;
  delete fixtures.check_update;
  await act(async () => panel.root.unmount());

  console.log("=== the full page's Now Playing tab ===");
  // The real component, not FullPage's wiring: it has to say what the Quick
  // Access panel says when a write waits for the next launch or is refused
  // (upstream 886d5f0, audit UI-07).
  setGpu(DECK_GPU);
  setIni({ Upscalers: { Dx12Upscaler: "fsr31", Dx11Upscaler: "auto", VulkanUpscaler: "auto" } });
  const mountNowPlaying = async () => {
    const node = document.createElement("div");
    document.body.appendChild(node);
    const r = createRoot(node);
    await act(async () => {
      r.render(<NowPlaying runningGame={{ appid: 1091500, name: "Cyberpunk 2077", gameid: "1091500" }}
        onOpenGame={() => {}} onBrowse={() => {}} />);
    });
    await settle(20);
    return { node, root: r };
  };
  const realWrite = fixtures.write_config;
  let nowPlaying = await mountNowPlaying();
  check("Now Playing shows the game's settings", Boolean(control(nowPlaying.node, "Override upscaler with")), true);
  // The newest FSR is named after the library in the folder, as on the Quick
  // Access panel (upstream 886d5f0, audit UI-09).
  check("its FSR versions are named as the Quick Access panel names them",
    all(control(nowPlaying.node, "FSR version") as Element, "[data-opt]")[0]?.textContent, "FSR 4.1.1");
  check("and no restart before anything waits",
    (nowPlaying.node.textContent ?? "").includes("Restart game to apply"), false);
  fixtures.write_config = (dir: string, changes: any[]) => {
    const result = realWrite(dir, changes);
    return { ...result, live: { ...result.live, deferred: changes.map((c: any) => `${c.section}.${c.key}`) } };
  };
  await act(async () => {
    (all(control(nowPlaying.node, "Override upscaler with"), '[data-opt-value="xess"]')[0] as HTMLElement).click();
  });
  await settle(200);
  check("a change that waits for the next launch offers the restart there too",
    (nowPlaying.node.textContent ?? "").includes("Restart game to apply"), true);
  await act(async () => nowPlaying.root.unmount());
  fixtures.write_config = (_dir: string, changes: any[]) => ({
    ok: true, applied: [], rejected: changes.map((c: any) => ({ ...c, value: String(c.value) })),
    live: { sent: false, deferred: [], attached: true, keys: [] },
  });
  nowPlaying = await mountNowPlaying();
  await act(async () => {
    (all(control(nowPlaying.node, "Override upscaler with"), '[data-opt-value="fsr31"]')[0] as HTMLElement).click();
  });
  await settle(200);
  check("a write OptiScaler would not take is said there too",
    (nowPlaying.node.textContent ?? "").includes("OptiScaler would not accept"), true);
  fixtures.write_config = realWrite;
  await act(async () => nowPlaying.root.unmount());

  const real = errors.filter((e) => !e.includes("not wrapped in act"));
  console.log(`\nreal React errors: ${real.length}`);
  for (const e of real) console.log(`  ${e}`);
  console.log(failures === 0 ? "\nall checks passed" : `\n${failures} check(s) failed`);
  // The panel polls; without this the intervals keep node alive for ever.
  await act(async () => root.unmount());
  process.exit(failures > 0 || real.length > 0 ? 1 : 0);
})();
