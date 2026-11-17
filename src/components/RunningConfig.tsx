import { ButtonItem, PanelSection, PanelSectionRow } from "@decky/ui";
import { toaster } from "@decky/api";
import { useState } from "react";
import { keepRunningChanges, type RunningConfig as Running } from "../api";
import { curatedLabel, UPSCALER_LABELS } from "../config/labels";
import { optionById } from "../config/tabs";
import { KeyValue, Notice } from "./Common";

const FG_OUTPUT_LABELS: Record<string, string> = {
  nofg: "None", fsrfg: "FSR FG", dlssg: "DLSSG", xefg: "XeFG",
  reprojection: "Reprojection", flipmeter: "Flip Meter",
};

function label(key: string): string {
  return optionById(key)?.label ?? key;
}

function valueLabel(key: string, value: string): string {
  const meta = optionById(key);
  return curatedLabel(key, value) ?? meta?.optionLabels?.[value] ?? value;
}

/**
 * What the running game actually has: OptiScaler's settings as it holds them
 * now, read through the in-game plugin, and what it changed in its own menu
 * without saving them, with a button to keep those in the ini.
 *
 * Switching the upscaler backend or the frame generator is done in
 * OptiScaler's menu (the OptiScaler menu button); this is where the panel
 * shows the outcome.
 */
export function RunningConfig({
  targetDir,
  running,
  unsaved,
  onKept,
}: Readonly<{
  targetDir: string;
  running: Running | null;
  unsaved: string[];
  onKept: () => void;
}>) {
  const [keeping, setKeeping] = useState(false);
  if (!running) return null;
  const v = running.values;
  const backend = v["Upscalers.Dx12Upscaler"];
  const fgOn = v["FrameGen.Enabled"] === "true";
  const fgOutput = (v["FrameGen.FGOutput"] ?? "").toLowerCase();

  return (
    <PanelSection title="Running now">
      <PanelSectionRow>
        <div style={{ padding: "2px 0", fontSize: "12px" }}>
          <KeyValue
            label="Upscaler"
            value={
              running.created
                ? `${running.created.upscaler} (created ${running.created.at})`
                : backend && backend !== "auto"
                  ? `${UPSCALER_LABELS[backend] ?? backend} (selected)`
                  : "chosen by OptiScaler"
            }
          />
          {v["FSR.UpscalerIndex"] && v["FSR.UpscalerIndex"] !== "auto" ? (
            <KeyValue label="FSR version index" value={v["FSR.UpscalerIndex"]} />
          ) : null}
          {v["FSR.Fsr4Model"] ? (
            <KeyValue label="FSR 4 model" value={valueLabel("FSR.Fsr4Model", v["FSR.Fsr4Model"])} />
          ) : null}
          <KeyValue
            label="Frame generation"
            value={fgOn ? FG_OUTPUT_LABELS[fgOutput] ?? (fgOutput || "on") : "off"}
          />
        </div>
      </PanelSectionRow>

      {unsaved.length > 0 ? (
        <>
          <PanelSectionRow>
            <Notice tone="info" title="Changed in OptiScaler's menu, not saved">
              {unsaved.map((key) => `${label(key)}: ${valueLabel(key, v[key] ?? "?")}`).join(" · ")}
              {". The controls here show these; the game loses them on restart unless they are kept."}
            </Notice>
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              disabled={keeping}
              onClick={async () => {
                setKeeping(true);
                try {
                  const result = await keepRunningChanges(targetDir, unsaved);
                  toaster.toast({
                    title: result.ok ? "Kept in OptiScaler.ini" : "Could not keep them",
                    body: result.ok ? `${result.kept?.length ?? 0} setting(s)` : result.error ?? "",
                  });
                  onKept();
                } finally {
                  setKeeping(false);
                }
              }}
            >
              {keeping ? "Saving…" : "Keep them in OptiScaler.ini"}
            </ButtonItem>
          </PanelSectionRow>
        </>
      ) : null}
    </PanelSection>
  );
}
