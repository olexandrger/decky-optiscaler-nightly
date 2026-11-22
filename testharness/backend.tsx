/**
 * The Quick Access panel against the real backend and a simulated running game.
 *
 * live-fields.tsx fakes the backend by hand, and a fake only behaves the way
 * its author thought the real one does. None of its fakes served a running
 * game whose values differ from the ini, and that is where the panel broke:
 * the upscaler read as plain FFX once the game held another FSR 4 model, a
 * live switch that the ini already agreed with never reached the game, and
 * turning frame generation on rewrote the FG pair from what OptiScaler had
 * reset at runtime.
 *
 * Here every panel call goes to scripts/harness_backend.py: the real
 * OptiScalerService on a temporary game folder, and a game that loads that
 * folder's ini into its Config, applies the panel's commands as the in-game
 * plugin does, and changes values by itself (its menu, its keys, its own
 * adjustments). Each case sets an ini, starts the game, makes the game differ,
 * uses one control, waits past a refresh of the running values, and requires:
 *
 *   - the control shows what was picked, and still does after the refresh;
 *   - the ini holds it;
 *   - for a live setting, the running game holds it.
 */
import React from "react";
import { createRoot } from "react-dom/client";
import { act } from "react";
import { spawn } from "node:child_process";
import path from "node:path";
import { fixtures } from "@decky/api";
import { Navigation } from "@decky/ui";
import { QuickPanel } from "../src/components/QuickPanel";
import { forgetWrites } from "../src/config/writeRecord";

// ---- the backend ------------------------------------------------------------
const python = spawn("python3", [path.join(process.cwd(), "scripts", "harness_backend.py")],
  { stdio: ["pipe", "pipe", "inherit"] });
let nextId = 1;
const waiting = new Map<number, (reply: any) => void>();
let buffer = "";
python.stdout.on("data", (chunk: Buffer) => {
  buffer += chunk.toString();
  let at: number;
  while ((at = buffer.indexOf("\n")) >= 0) {
    const line = buffer.slice(0, at);
    buffer = buffer.slice(at + 1);
    if (!line.trim()) continue;
    const reply = JSON.parse(line);
    waiting.get(reply.id)?.(reply);
    waiting.delete(reply.id);
  }
});
const backendErrors: string[] = [];
function call(method: string, ...args: any[]): Promise<any> {
  const id = nextId++;
  return new Promise((resolve) => {
    waiting.set(id, (reply) => {
      if (reply.error) {
        backendErrors.push(`${method}: ${reply.error.split("\n")[0]}`);
        resolve({ ok: false, error: reply.error });
      } else resolve(reply.result);
    });
    python.stdin.write(JSON.stringify({ id, method, args }) + "\n");
  });
}

const DECK_GPU = { name: "Van Gogh (Steam Deck)", vendor: "amd", generation: "RDNA2", fsr4: "experimental" };
let gameDir = "";
// What the game page read from the last session's OptiScaler.log.
let detailXefgMax: number | null = null;
const detail = () => ({
  path: gameDir, name: "Stellar Blade", target: gameDir,
  install: {
    path: gameDir, installed: true, filename: "dxgi.dll", managed: true, version: undefined,
    ini_present: true, log_present: false, candidates: ["dxgi.dll"], extra_proxies: [], backed_up: [],
    fsr4: { files: {}, ready: false, required: [],
      ffx: { present: true, name: "amd_fidelityfx_upscaler_dx12.dll", version: "4.1.2.0", fsr4_capable: true } },
  },
  gpu: DECK_GPU,
  wiki_entry: null,
  xefg_max: detailXefgMax,
});
for (const name of ["read_config", "write_config", "get_live_status", "get_running_config", "press_live_keys",
  "keep_running_changes", "set_fsr4_build", "toggle_menu"]) {
  fixtures[name] = (...args: any[]) => call(name, ...args);
}
Object.assign(fixtures, {
  find_running_game: () => ({ found: true, appid: "3489700", detail: detail() }),
  get_auto_plan: { recommendation: { matched: false }, plan: { available: false } },
  get_pref: { key: "auto", value: null },
  check_update: { ok: true, available: false },
});

// ---- harness ------------------------------------------------------------------
const errors: string[] = [];
const originalError = console.error;
console.error = (...args: any[]) => {
  errors.push(args.map(String).join(" "));
  originalError(...args);
};
let failures = 0;
const failed: string[] = [];
let current = "";
function check(label: string, actual: unknown, expected: unknown) {
  const ok = String(actual) === String(expected);
  if (!ok) {
    failures += 1;
    failed.push(`${current}: ${label}`);
  }
  console.log(`  ${ok ? "[ok]" : "[FAIL]"} ${label}${ok ? "" : ` — got ${actual}, want ${expected}`}`);
}
async function wait(ms: number) {
  const until = Date.now() + ms;
  while (Date.now() < until) await act(async () => { await new Promise((r) => setTimeout(r, 25)); });
}
const all = (node: Element, selector: string) => Array.from(node.querySelectorAll(selector));
const control = (node: Element, label: string) => all(node, `[data-mock][data-label="${label}"]`)[0];
const selected = (node: Element, label: string) => control(node, label)?.getAttribute("data-selected");
const toggled = (node: Element, label: string) =>
  all(control(node, label) ?? node, "[data-toggle]")[0]?.getAttribute("data-checked");
const buttonText = (node: Element, id: string) => {
  const button = all(node, `[data-button="${id}"]`)[0];
  return button?.textContent ?? null;
};

type Ini = Record<string, Record<string, string>>;
interface Case {
  name: string;
  ini: Ini;
  /** What the running game holds other than the ini: [ini key, value, volatile?]. */
  game?: [string, string, boolean?][];
  act: (node: Element) => Promise<void>;
  /** [label, value] the control must show, after a refresh of the running values. */
  shows?: [string, string][];
  /** The overlay button's text. */
  overlay?: string;
  ini_has?: [string, string][];
  game_has?: [string, string][];
  /** An earlier session's last command, in the folder before the game starts. */
  leftover?: string[];
  /** [text, whether the panel shows it]. */
  text?: [string, boolean][];
  /** XeFG runs in this session and reports this maximum in OptiScaler.log. */
  xefg_max?: number;
  /** The maximum the game page read from the last session's log. */
  detail_xefg_max?: number;
  /** [control label, option value, whether the control offers it]. */
  offers?: [string, string, boolean][];
}

const iniValue = (ini: Ini, key: string) => {
  const [section, name] = key.split(".", 2);
  return ini[section]?.[name] ?? "auto";
};

async function run(c: Case) {
  console.log(`=== ${c.name} ===`);
  current = c.name;
  gameDir = (await call("reset", c.ini)).dir;
  forgetWrites(gameDir);
  detailXefgMax = c.detail_xefg_max ?? null;
  if (c.leftover) await call("leftover", c.leftover);
  await call("game.start", c.xefg_max ?? null);
  for (const [key, value, vol] of c.game ?? []) await call("game.set", key, value, Boolean(vol));
  const node = document.createElement("div");
  document.body.appendChild(node);
  const root = createRoot(node);
  await act(async () => {
    root.render(<QuickPanel runningGame={{ appid: 3489700, name: "Stellar Blade", gameid: "3489700" }}
      onOpenManager={() => {}} />);
  });
  await wait(2300); // the running values arrive
  await c.act(node);
  // Past the write's debounce, the game applying it, and one more refresh of
  // the running values: a control that flips back does so by then.
  await wait(2600);
  for (const [label, value] of c.shows ?? []) {
    const shown = label.startsWith("toggle:") ? toggled(node, label.slice(7)) : selected(node, label);
    check(`shows ${label} = ${value}`, shown, value);
  }
  if (c.overlay !== undefined) check("the overlay button says", buttonText(node, "fps-overlay"), c.overlay);
  for (const [label, value, present] of c.offers ?? []) {
    const ctl = control(node, label);
    check(`${label} offers ${value}: ${present}`,
      ctl ? all(ctl, `[data-opt-value="${value}"]`).length > 0 : "no such control", present);
  }
  const ini: Ini = await call("ini");
  for (const [key, value] of c.ini_has ?? []) check(`ini ${key} = ${value}`, iniValue(ini, key), value);
  const game = await call("game.values");
  for (const [key, value] of c.game_has ?? []) check(`game ${key} = ${value}`, game[key], value);
  for (const [text, present] of c.text ?? []) check(`says "${text}"`, (node.textContent ?? "").includes(text), present);
  await act(async () => root.unmount());
  await call("game.stop");
}

async function pick(node: Element, label: string, value: string) {
  const option = all(control(node, label) ?? node, `[data-opt-value="${value}"]`)[0] as HTMLElement | undefined;
  if (!option) {
    check(`${label} offers ${value}`, "missing", "present");
    return;
  }
  await act(async () => { option.click(); });
}
async function toggle(node: Element, label: string) {
  const knob = all(control(node, label) ?? node, "[data-toggle]")[0] as HTMLElement | undefined;
  if (!knob) {
    check(`${label} is there`, "missing", "present");
    return;
  }
  await act(async () => { knob.click(); });
}
async function press(node: Element, id: string) {
  await act(async () => { (all(node, `[data-button="${id}"]`)[0] as HTMLElement | undefined)?.click(); });
}

// ---- the cases ------------------------------------------------------------------
const UPSCALERS: Ini = { Upscalers: { Dx12Upscaler: "ffx", Dx11Upscaler: "ffx_12", VulkanUpscaler: "ffx_12" } };
const DECK_LITE: Ini = { ...UPSCALERS, FSR: { Fsr4GpuSupport: "4", Fsr4Model: "3", UpscalerIndex: "0", Fsr4Preset: "auto" } };
const FSR3: Ini = { Upscalers: { Dx12Upscaler: "ffx", Dx11Upscaler: "fsr31", VulkanUpscaler: "ffx" },
  FSR: { UpscalerIndex: "1", Fsr4GpuSupport: "auto", Fsr4Model: "auto" } };
const XEFG_ON: Ini = { FrameGen: { Enabled: "true", FGInput: "fsrfg", FGOutput: "xefg" } };
const XEFG_OFF: Ini = { FrameGen: { Enabled: "false", FGInput: "fsrfg", FGOutput: "xefg" } };

/** Ways the running game can hold an upscaler setting other than the ini. */
const DRIFTS: [string, [string, string, boolean?][]][] = [
  ["as loaded", []],
  ["model at Auto in the game", [["FSR.Fsr4Model", "auto"]]],
  ["another model in the game", [["FSR.Fsr4Model", "1"]]],
  ["another FSR version in the game", [["FSR.UpscalerIndex", "2"]]],
  ["XeSS switched in OptiScaler's menu", [["Upscalers.Dx12Upscaler", "xess"]]],
  ["FG output reset by OptiScaler", [["FrameGen.FGOutput", "nofg"]]],
];

const cases: Case[] = [];
// Seen on a Deck: the last panel command of one session (FSR 3) replayed at the
// next launch over the ini's FSR 4 Lite, and again inside the next live push.
const FSR3_COMMAND = ["unset Fsr4Model", "set FfxUpscalerIndex i32 1", "reinit"];
cases.push(
  {
    name: "Deck FSR 4 Lite, an earlier session's FSR 3 command left over: the game runs the ini",
    ini: { ...DECK_LITE, ...XEFG_ON }, leftover: FSR3_COMMAND,
    act: (node) => press(node, "fps-overlay"),
    shows: [["Upscaler", "fsr4"], ["FSR 4 model", "fsr4-lite"]],
    game_has: [["FSR.Fsr4Model", "3"], ["FSR.UpscalerIndex", "0"]],
  },
  // OptiScaler's menu switches the FSR 4 model live, and its support check
  // lets the RDNA2 model run whenever the GPU support is not None: Lite's
  // support (4) runs it. Seen on a Deck: INT8 <-> INT8 Lite never applied.
  {
    name: "Deck FSR 4 Lite → RDNA2: applies live under Lite's GPU support, no restart",
    ini: { ...DECK_LITE, ...XEFG_ON },
    act: (node) => pick(node, "FSR 4 model", "fsr4-rdna2"),
    shows: [["FSR 4 model", "fsr4-rdna2"]],
    ini_has: [["FSR.Fsr4Model", "2"], ["FSR.Fsr4GpuSupport", "3"]],
    game_has: [["FSR.Fsr4Model", "2"], ["FSR.UpscalerIndex", "0"]],
    text: [["Restart game to apply", false]],
  },
  {
    name: "Deck FSR 4 RDNA2 → Lite: applies live, no restart",
    ini: { ...DECK_LITE, FSR: { ...DECK_LITE.FSR, Fsr4GpuSupport: "3", Fsr4Model: "2" }, ...XEFG_ON },
    act: (node) => pick(node, "FSR 4 model", "fsr4-lite"),
    shows: [["FSR 4 model", "fsr4-lite"]],
    ini_has: [["FSR.Fsr4Model", "3"], ["FSR.Fsr4GpuSupport", "4"]],
    game_has: [["FSR.Fsr4Model", "3"], ["FSR.UpscalerIndex", "0"]],
    text: [["Restart game to apply", false]],
  },
  {
    name: "Deck FSR 4 Lite, FSR 3 from OptiScaler's menu → RDNA2: the model is back live",
    ini: { ...DECK_LITE, ...XEFG_ON }, game: [["FSR.Fsr4Model", "auto"], ["FSR.UpscalerIndex", "1"]],
    act: (node) => pick(node, "FSR 4 model", "fsr4-rdna2"),
    shows: [["FSR 4 model", "fsr4-rdna2"]],
    ini_has: [["FSR.Fsr4Model", "2"], ["FSR.Fsr4GpuSupport", "3"]],
    game_has: [["FSR.Fsr4Model", "2"], ["FSR.UpscalerIndex", "0"]],
    text: [["Restart game to apply", false]],
  },
  {
    name: "Deck, a game started without GPU support → RDNA2: the model goes in, the support waits",
    ini: { ...DECK_LITE, ...XEFG_ON }, game: [["FSR.Fsr4GpuSupport", "auto"]],
    act: (node) => pick(node, "FSR 4 model", "fsr4-rdna2"),
    ini_has: [["FSR.Fsr4Model", "2"], ["FSR.Fsr4GpuSupport", "3"]],
    game_has: [["FSR.Fsr4Model", "2"]],
    text: [["Restart game to apply", true]],
  },
  {
    name: "Deck, a game started on FSR 3 → FSR 4: Lite's model goes in live",
    ini: { ...FSR3, ...XEFG_ON },
    act: (node) => pick(node, "Upscaler", "fsr4"),
    shows: [["Upscaler", "fsr4"], ["FSR 4 model", "fsr4-lite"]],
    ini_has: [["FSR.Fsr4Model", "3"], ["FSR.UpscalerIndex", "0"]],
    game_has: [["FSR.Fsr4Model", "3"], ["FSR.UpscalerIndex", "0"]],
  },
  // A rebuild keeps the backend the game runs; only OptiScaler's menu starts
  // another. The panel says which upscaler waits and where to switch it now.
  {
    name: "XeSS over a running FSR game waits for the next launch, and says so",
    ini: { ...DECK_LITE, ...XEFG_ON },
    act: (node) => pick(node, "Upscaler", "xess"),
    shows: [["Upscaler", "xess"]],
    ini_has: [["Upscalers.Dx12Upscaler", "xess"]],
    game_has: [["Upscalers.Dx12Upscaler", "ffx"]],
    text: [["Restart game to apply", true], ["XeSS starts at the next launch", true],
      ["OptiScaler menu (top) switches it now", true]],
  },
  {
    name: "a model change inside the running backend does not claim an upscaler waits",
    ini: { ...DECK_LITE, ...XEFG_ON }, game: [["FSR.Fsr4GpuSupport", "auto"]],
    act: (node) => pick(node, "FSR 4 model", "fsr4-rdna2"),
    text: [["starts at the next launch", false]],
  },
  {
    name: "Deck: back to the model whose GPU support the game runs: applies live",
    ini: { ...DECK_LITE, ...XEFG_ON },
    game: [["FSR.Fsr4GpuSupport", "3"], ["FSR.Fsr4Model", "auto"], ["FSR.UpscalerIndex", "1"]],
    act: (node) => pick(node, "FSR 4 model", "fsr4-rdna2"),
    shows: [["FSR 4 model", "fsr4-rdna2"]],
    ini_has: [["FSR.Fsr4Model", "2"], ["FSR.Fsr4GpuSupport", "3"], ["FSR.UpscalerIndex", "0"]],
    game_has: [["FSR.Fsr4Model", "2"], ["FSR.UpscalerIndex", "0"]],
    text: [["Restart game to apply", false]],
  },
);
for (const [drift, game] of DRIFTS) {
  for (const [start, ini] of [["Deck FSR 4 Lite", DECK_LITE], ["FSR 3", FSR3]] as [string, Ini][]) {
    cases.push({
      name: `${start}, ${drift}: FSR 4 Lite → RDNA2`.replace("FSR 3, ", "FSR 3 → FSR 4, "),
      ini: { ...ini, ...XEFG_ON }, game,
      act: async (node) => {
        if (selected(node, "Upscaler") !== "fsr4") {
          await pick(node, "Upscaler", "fsr4");
          await wait(1500);
        }
        await pick(node, "FSR 4 model", "fsr4-rdna2");
      },
      shows: [["Upscaler", "fsr4"], ["FSR 4 model", "fsr4-rdna2"]],
      ini_has: [["FSR.Fsr4Model", "2"], ["FSR.Fsr4GpuSupport", "3"], ["FSR.UpscalerIndex", "0"]],
    });
    cases.push({
      name: `${start}, ${drift}: → XeSS`,
      ini: { ...ini, ...XEFG_ON }, game,
      act: (node) => pick(node, "Upscaler", "xess"),
      shows: [["Upscaler", "xess"]],
      ini_has: [["Upscalers.Dx12Upscaler", "xess"]],
    });
  }
  cases.push({
    name: `FSR 3, ${drift}: version 2.3.4`,
    ini: { ...FSR3, ...XEFG_ON }, game,
    act: (node) => pick(node, "FSR version", "2"),
    shows: [["Upscaler", "fsr3"], ["FSR version", "2"]],
    ini_has: [["FSR.UpscalerIndex", "2"]],
    game_has: [["FSR.UpscalerIndex", "2"]],
  });
  cases.push({
    name: `FSR 3, ${drift}: back to version 3.1.5`,
    ini: { ...FSR3, ...XEFG_ON }, game,
    act: (node) => pick(node, "FSR version", "1"),
    shows: [["Upscaler", "fsr3"], ["FSR version", "1"]],
    ini_has: [["FSR.UpscalerIndex", "1"]],
    game_has: [["FSR.UpscalerIndex", "1"]],
  });
}

// Live controls the ini already agrees with, while the game holds another value.
cases.push(
  {
    name: "FG turned on with OptiScaler's key, ini off: the panel turns it off",
    ini: { ...DECK_LITE, ...XEFG_OFF }, game: [["FrameGen.Enabled", "true"]],
    act: (node) => toggle(node, "Frame generation"),
    shows: [["toggle:Frame generation", "false"]],
    ini_has: [["FrameGen.Enabled", "false"]],
    game_has: [["FrameGen.Enabled", "false"]],
  },
  {
    name: "FG turned off with OptiScaler's key, ini on: the panel turns it on (its key)",
    ini: { ...DECK_LITE, ...XEFG_ON }, game: [["FrameGen.Enabled", "false"]],
    act: (node) => toggle(node, "Frame generation"),
    shows: [["toggle:Frame generation", "true"]],
    ini_has: [["FrameGen.Enabled", "true"]],
    game_has: [["FrameGen.Enabled", "true"]],
  },
  {
    name: "FG output reset to none by OptiScaler: turning FG on keeps the ini's pair",
    ini: { ...DECK_LITE, ...XEFG_OFF }, game: [["FrameGen.FGOutput", "nofg"]],
    act: (node) => toggle(node, "Frame generation"),
    ini_has: [["FrameGen.FGInput", "fsrfg"], ["FrameGen.FGOutput", "xefg"], ["FrameGen.Enabled", "true"]],
  },
  {
    name: "multiplier changed in OptiScaler's menu, ini 2X: the panel sets 2X again",
    ini: { ...DECK_LITE, ...XEFG_ON, XeFG: { InterpolationCount: "1" } }, game: [["XeFG.InterpolationCount", "3"]],
    act: (node) => pick(node, "Frame multiplier", "1"),
    shows: [["Frame multiplier", "1"]],
    game_has: [["XeFG.InterpolationCount", "1"]],
  },
  // XeFG reports how many frames it can generate (a Steam Deck: 1, so 2X);
  // OptiScaler's menu stops there, and runs a higher count as that maximum.
  {
    name: "Deck, XeFG reports 2X at most: the multiplier offers 2X only",
    ini: { ...DECK_LITE, ...XEFG_ON, XeFG: { InterpolationCount: "1" } }, xefg_max: 1,
    act: (node) => pick(node, "Frame multiplier", "1"),
    shows: [["Frame multiplier", "1"]],
    offers: [["Frame multiplier", "1", true], ["Frame multiplier", "2", false], ["Frame multiplier", "3", false]],
    game_has: [["XeFG.InterpolationCount", "1"]],
  },
  {
    name: "Deck, ini at 4X from elsewhere, XeFG runs 2X: shows the 2X it runs",
    ini: { ...DECK_LITE, ...XEFG_ON, XeFG: { InterpolationCount: "3" } }, xefg_max: 1,
    act: async () => {},
    shows: [["Frame multiplier", "1"]],
    offers: [["Frame multiplier", "3", false]],
  },
  {
    name: "XeFG reports 4X: every step is offered, and 3X goes in live",
    ini: { ...DECK_LITE, ...XEFG_ON, XeFG: { InterpolationCount: "1" } }, xefg_max: 3,
    act: (node) => pick(node, "Frame multiplier", "2"),
    shows: [["Frame multiplier", "2"]],
    offers: [["Frame multiplier", "3", true]],
    ini_has: [["XeFG.InterpolationCount", "2"]],
    game_has: [["XeFG.InterpolationCount", "2"]],
  },
  {
    name: "XeFG has not reported yet: every step is offered",
    ini: { ...DECK_LITE, ...XEFG_ON, XeFG: { InterpolationCount: "1" } },
    act: async () => {},
    offers: [["Frame multiplier", "1", true], ["Frame multiplier", "3", true]],
  },
  {
    name: "not reported this session, the last one's log said 2X: 2X only",
    ini: { ...DECK_LITE, ...XEFG_ON, XeFG: { InterpolationCount: "1" } }, detail_xefg_max: 1,
    act: async () => {},
    offers: [["Frame multiplier", "1", true], ["Frame multiplier", "2", false]],
  },
  // Dynamic MFG: OptiScaler's checkbox next to its Frame Gen slider, read
  // every present. Each step comes again as "Dynamic: up to ...".
  {
    name: "Deck: Dynamic (FG on/off) goes in live",
    ini: { ...DECK_LITE, ...XEFG_ON, XeFG: { InterpolationCount: "1" } }, xefg_max: 1,
    act: (node) => pick(node, "Frame multiplier", "dynamic:1"),
    shows: [["Frame multiplier", "dynamic:1"]],
    offers: [["Frame multiplier", "dynamic:2", false]],
    ini_has: [["XeFG.DynamicMFG", "true"], ["XeFG.InterpolationCount", "1"]],
    game_has: [["XeFG.DynamicMFG", "true"], ["XeFG.InterpolationCount", "1"]],
  },
  {
    name: "Dynamic in the ini and the game → fixed 2X: Dynamic ends live",
    ini: { ...DECK_LITE, ...XEFG_ON, XeFG: { InterpolationCount: "1", DynamicMFG: "true" } }, xefg_max: 1,
    act: (node) => pick(node, "Frame multiplier", "1"),
    shows: [["Frame multiplier", "1"]],
    ini_has: [["XeFG.DynamicMFG", "false"]],
    game_has: [["XeFG.DynamicMFG", "false"]],
  },
  {
    name: "Dynamic turned on in OptiScaler's menu, ini off: shown, and fixed 2X ends it",
    ini: { ...DECK_LITE, ...XEFG_ON, XeFG: { InterpolationCount: "1" } }, xefg_max: 1,
    game: [["XeFG.DynamicMFG", "true"]],
    act: async (node) => {
      check("shows the game's Dynamic MFG first", selected(node, "Frame multiplier"), "dynamic:1");
      await pick(node, "Frame multiplier", "1");
    },
    shows: [["Frame multiplier", "1"]],
    ini_has: [["XeFG.DynamicMFG", "false"]],
    game_has: [["XeFG.DynamicMFG", "false"]],
  },
  {
    name: "Dynamic turned off in OptiScaler's menu, ini on → fixed 3X: the ini ends it too",
    ini: { ...DECK_LITE, ...XEFG_ON, XeFG: { InterpolationCount: "1", DynamicMFG: "true" } }, xefg_max: 3,
    game: [["XeFG.DynamicMFG", "false"]],
    act: async (node) => {
      check("shows the game's fixed 2X first", selected(node, "Frame multiplier"), "1");
      await pick(node, "Frame multiplier", "2");
    },
    shows: [["Frame multiplier", "2"]],
    ini_has: [["XeFG.DynamicMFG", "false"], ["XeFG.InterpolationCount", "2"]],
    game_has: [["XeFG.DynamicMFG", "false"], ["XeFG.InterpolationCount", "2"]],
  },
  {
    name: "XeFG reports 4X: Dynamic up to 4X sets both live",
    ini: { ...DECK_LITE, ...XEFG_ON, XeFG: { InterpolationCount: "1" } }, xefg_max: 3,
    act: (node) => pick(node, "Frame multiplier", "dynamic:3"),
    shows: [["Frame multiplier", "dynamic:3"]],
    ini_has: [["XeFG.DynamicMFG", "true"], ["XeFG.InterpolationCount", "3"]],
    game_has: [["XeFG.DynamicMFG", "true"], ["XeFG.InterpolationCount", "3"]],
  },
  {
    name: "XeFG reports 0: no Dynamic MFG, as in OptiScaler's menu",
    ini: { ...DECK_LITE, ...XEFG_ON, XeFG: { InterpolationCount: "1" } }, xefg_max: 0,
    act: async () => {},
    offers: [["Frame multiplier", "1", true], ["Frame multiplier", "dynamic:1", false]],
  },
  {
    name: "XeSS model changed in OptiScaler's menu, ini KPSS: the panel sets KPSS again",
    ini: { Upscalers: { Dx12Upscaler: "xess", Dx11Upscaler: "xess_12", VulkanUpscaler: "xess" },
      XeSS: { NetworkModel: "0" }, ...XEFG_ON }, game: [["XeSS.NetworkModel", "3"]],
    act: (node) => pick(node, "XeSS model", "0"),
    shows: [["XeSS model", "0"]],
    game_has: [["XeSS.NetworkModel", "0"]],
  },
  {
    name: "overlay turned on with OptiScaler's key, ini off: the button moves it on",
    ini: { ...DECK_LITE, ...XEFG_ON, Menu: { ShowFps: "false", FpsOverlayType: "2" } },
    game: [["Menu.ShowFps", "true"]],
    act: (node) => press(node, "fps-overlay"),
    overlay: "Overlay: Detailed + Graph",
    game_has: [["Menu.ShowFps", "true"], ["Menu.FpsOverlayType", "3"]],
    ini_has: [["Menu.ShowFps", "true"], ["Menu.FpsOverlayType", "3"]],
  },
  {
    name: "overlay on in the game at its last type, ini off: the button turns it off",
    ini: { ...DECK_LITE, ...XEFG_ON, Menu: { ShowFps: "false", FpsOverlayType: "6" } },
    game: [["Menu.ShowFps", "true"]],
    act: (node) => press(node, "fps-overlay"),
    overlay: "Overlay: Off",
    game_has: [["Menu.ShowFps", "false"]],
  },
);

(async () => {
  for (const c of process.env.CASE ? cases.filter((x) => x.name.includes(process.env.CASE!)) : cases) {
    await run(c);
  }
  void Navigation;
  console.log(`\nbackend errors: ${backendErrors.length}`);
  for (const e of backendErrors) console.log(`  ${e}`);
  const real = errors.filter((e) => !e.includes("not wrapped in act"));
  console.log(`real React errors: ${real.length}`);
  for (const e of real.slice(0, 5)) console.log(`  ${e.slice(0, 200)}`);
  console.log(failures === 0 ? `\nall ${cases.length} cases passed` : `\n${failures} check(s) failed:`);
  for (const f of failed) console.log(`  ${f}`);
  python.stdin.end();
  process.exit(failures > 0 || real.length > 0 || backendErrors.length > 0 ? 1 : 0);
})();
