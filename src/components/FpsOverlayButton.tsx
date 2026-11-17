import type { ConfigValues, LiveStatus, OptionChange } from "../types";

const OFF = -1;

/** Where the overlay is now: OFF, or OptiScaler's overlay type index. */
export function currentFpsOverlay(values: ConfigValues, live: LiveStatus | null): number {
  const types = live?.fps_overlay_types ?? [];
  if (values.Menu?.ShowFps !== "true") return OFF;
  const raw = values.Menu?.FpsOverlayType;
  const index = raw && raw !== "auto" ? Number.parseInt(raw, 10) : live?.fps_overlay_default ?? 0;
  return Number.isFinite(index) && index >= 0 && index < types.length ? index : 0;
}

/**
 * Off, then every overlay type in OptiScaler's order, then off again.
 *
 * "Custom" shows the rows chosen in [Menu] FpsOverlayRows; with none chosen it
 * shows the same rows as the type before it, so it is only a step when rows are.
 */
export function fpsOverlaySteps(values: ConfigValues, live: LiveStatus | null): number[] {
  const types = live?.fps_overlay_types ?? [];
  const rows = (values.Menu?.FpsOverlayRows ?? "").trim();
  const hasRows = rows !== "" && rows.toLowerCase() !== "auto";
  return [OFF, ...types.map((_, i) => i).filter((i) => types[i] !== "Custom" || hasRows)];
}

/** The ini changes that move the overlay one step on. */
export function nextFpsOverlay(values: ConfigValues, live: LiveStatus | null): OptionChange[] {
  const steps = fpsOverlaySteps(values, live);
  const current = currentFpsOverlay(values, live);
  const at = steps.indexOf(current);
  // A type that is not a step (Custom without rows): on to the one after it.
  const next = at >= 0 ? steps[(at + 1) % steps.length] : steps.find((s) => s > current) ?? OFF;
  if (next === OFF) return [{ section: "Menu", key: "ShowFps", value: "false" }];
  return [
    { section: "Menu", key: "ShowFps", value: "true" },
    { section: "Menu", key: "FpsOverlayType", value: String(next) },
  ];
}

/** What the overlay button says: the overlay now, in OptiScaler's own words. */
export function fpsOverlayLabel(values: ConfigValues, live: LiveStatus | null): string | null {
  const types = live?.fps_overlay_types ?? [];
  if (types.length === 0) return null;
  const current = currentFpsOverlay(values, live);
  return current === OFF ? "Off" : types[current];
}
