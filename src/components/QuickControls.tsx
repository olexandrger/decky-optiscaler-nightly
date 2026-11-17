import { PanelSection, PanelSectionRow, ToggleField } from "@decky/ui";
import { useState } from "react";
import {
  activeFgPreset,
  activeUpscalerPreset,
  DECK_FSR4_PRESETS,
  disableFrameGenChanges,
  enableFrameGenChanges,
  enableFrameGenWith,
  ffxUpscalerChanges,
  FFX_UPSCALER_ID,
  frameGenEnabled,
  isDeckFsr4Preset,
  isFsr4Version,
  MULTIPLIER_KEY,
  MULTIPLIER_SECTION,
  needsForcedInt8,
  supportsInt8Override,
  supportsMultiplier,
  upscalerChoicesFor,
  UPSCALER_PRESETS,
} from "../config/basic";
import { curatedLabel, UPSCALER_LABELS } from "../config/labels";
import { optionById } from "../config/tabs";
import { AUTO, effectiveValue, labelFor } from "../config/values";
import type { AutoPlan, ConfigValues, FfxUpscalerInfo, GpuInfo, LiveStatus, OptionChange } from "../types";
import { type Choice, ffxVersionName, ffxVersionOptions, ValueDropdown } from "./BasicPanel";
import { Notice } from "./Common";
import { InfoRow } from "./Compact";

/**
 * Which upscaler, as the Quick Access panel asks it: a family, then the model
 * (or version) inside it. Each family is the existing presets grouped, so
 * nothing here writes anything a preset does not; the Steam Deck FSR 4 presets
 * are FSR 4's models on a Deck.
 */
export const UPSCALER_FAMILIES: { id: string; label: string; presets: string[] }[] = [
  { id: "auto", label: "Auto", presets: ["auto"] },
  { id: "fsr4", label: "FSR 4", presets: ["fsr4-lite", "fsr4-rdna2", "fsr4"] },
  { id: "fsr3", label: "FSR 3", presets: ["fsr31"] },
  { id: "fsr22", label: "FSR 2.2", presets: ["fsr22"] },
  { id: "xess", label: "XeSS", presets: ["xess"] },
  { id: "dlss", label: "DLSS", presets: ["dlss"] },
];

const DECK_MODEL_LABELS: Record<string, string> = { "fsr4-lite": "INT8 Lite", "fsr4-rdna2": "INT8 RDNA2" };

function familyOf(presetId: string | undefined) {
  return UPSCALER_FAMILIES.find((family) => presetId && family.presets.includes(presetId));
}

function change(section: string, key: string, value: string): OptionChange {
  return { section, key, value };
}

/** Choices for an ini key straight from the schema, "auto" first where it means something. */
function schemaChoices(id: string, withAuto = false): Choice[] {
  const option = optionById(id);
  const choices = (option?.options ?? []).map((value) => ({ data: value, label: labelFor(option!, value) }));
  return withAuto ? [{ data: AUTO, label: "Auto" }, ...choices] : choices;
}

/**
 * The condensed controls of the Quick Access panel: frame generation on/off and
 * its multiplier, the upscaler and the model under it. Everything else (the FG
 * input/output pair, the FFX FG version, every other option) is on the game's
 * page; each explanation is behind its section's ⓘ.
 */
export function QuickControls({
  values,
  live,
  gpu,
  ffx,
  plan,
  auto,
  busy,
  onApply,
  onEnableFsr4,
}: Readonly<{
  values: ConfigValues;
  live: LiveStatus | null;
  gpu: GpuInfo | null | undefined;
  ffx?: FfxUpscalerInfo | null;
  plan?: AutoPlan | null;
  auto?: boolean;
  busy?: boolean;
  onApply: (changes: OptionChange[]) => void;
  onEnableFsr4?: (model: string) => Promise<void>;
}>) {
  const [installing, setInstalling] = useState(false);
  const [fsr4Error, setFsr4Error] = useState<string | null>(null);
  const disabled = Boolean(busy) || installing;
  const liveReady = Boolean(live?.ready);

  // -- frame generation --------------------------------------------------
  const fgOn = frameGenEnabled(values);
  const planned = plan?.available ? plan : null;
  const plannedFg = planned && auto ? planned.framegen ?? null : null;
  const fgUnavailable = plannedFg?.input === "nofg";
  const multiplierOption = optionById(`${MULTIPLIER_SECTION}.${MULTIPLIER_KEY}`);
  const multiplierUsable = supportsMultiplier(values);
  const rawFgInput = (values.FrameGen?.FGInput ?? AUTO).toLowerCase();
  const rawFgOutput = (values.FrameGen?.FGOutput ?? AUTO).toLowerCase();
  const fgPair = activeFgPreset(values)?.label ??
    `${curatedLabel("FrameGen.FGInput", rawFgInput) ?? rawFgInput} → ${curatedLabel("FrameGen.FGOutput", rawFgOutput) ?? rawFgOutput}`;

  const turnFrameGenOn = () =>
    onApply(plannedFg && plannedFg.input !== "nofg"
      ? enableFrameGenWith(plannedFg.input, plannedFg.output)
      : enableFrameGenChanges(values));

  // -- upscaler ----------------------------------------------------------
  const preset = activeUpscalerPreset(values);
  const choices = upscalerChoicesFor(gpu, preset);
  const available = new Set(choices.map((choice) => choice.id));
  const families = UPSCALER_FAMILIES.filter((family) => family.presets.some((id) => available.has(id)));
  const family = familyOf(preset?.id);
  const rawUpscaler = (values.Upscalers?.Dx12Upscaler ?? AUTO).toLowerCase();
  const deckRoute = needsForcedInt8(gpu) && supportsInt8Override(ffx);

  const applyPreset = (id: string) => {
    const picked = UPSCALER_PRESETS.find((candidate) => candidate.id === id);
    if (!picked) return;
    setFsr4Error(null);
    if (isDeckFsr4Preset(picked.id)) {
      if (!onEnableFsr4) return;
      setInstalling(true);
      void onEnableFsr4(DECK_FSR4_PRESETS[picked.id])
        .catch((error) => setFsr4Error(String(error)))
        .finally(() => setInstalling(false));
      return;
    }
    onApply(picked.changes);
  };

  // The FidelityFX versions this runtime offers (the game's list, else the ini's).
  const ffxVersions = ffxVersionOptions(live?.ffx_upscaler_versions, optionById(FFX_UPSCALER_ID),
    ffxVersionName(ffx)).options;

  const pickFamily = (id: string) => {
    const next = UPSCALER_FAMILIES.find((candidate) => candidate.id === id);
    if (!next || next.id === family?.id) return;
    if (next.id === "fsr3") {
      // The FSR 3.X preset's index is the reference list's FSR 3.1.5; a runtime
      // that reports another list has its first FSR 3 version elsewhere.
      const fsr31 = UPSCALER_PRESETS.find((candidate) => candidate.id === "fsr31");
      const first = ffxVersions.find((choice) => !isFsr4Version(choice.version));
      if (fsr31 && first) {
        onApply([...fsr31.changes.filter((item) => item.key !== "UpscalerIndex"), ...ffxUpscalerChanges(first.data)]);
        return;
      }
    }
    // On a Deck FSR 4 is one of its models; elsewhere the plain FSR 4 preset.
    const ids = next.presets.filter((presetId) => available.has(presetId));
    const target = next.id === "fsr4" && deckRoute ? ids.find(isDeckFsr4Preset) ?? ids[0] : ids.find((presetId) =>
      !isDeckFsr4Preset(presetId)) ?? ids[0];
    if (target) applyPreset(target);
  };

  // The model, or version, inside the family.
  let model: {
    label: string;
    options: Choice[];
    selected: string;
    onPick: (value: string) => void;
    /** Names a value the list does not offer (e.g. FSR 4's index under FSR 3). */
    name?: (value: string) => string | undefined;
  } | null = null;
  if (family?.id === "fsr4" && choices.some((choice) => isDeckFsr4Preset(choice.id))) {
    const deck = choices.filter((choice) => isDeckFsr4Preset(choice.id));
    model = {
      label: "FSR 4 model",
      options: deck.map((choice) => ({ data: choice.id, label: DECK_MODEL_LABELS[choice.id] ?? choice.label })),
      selected: preset?.id ?? "",
      onPick: applyPreset,
    };
  } else if (family?.id === "fsr4") {
    const option = optionById("FSR.Fsr4Model");
    model = {
      label: "FSR 4 model",
      options: schemaChoices("FSR.Fsr4Model", true),
      selected: option ? (values.FSR?.Fsr4Model ?? AUTO) : AUTO,
      onPick: (value) => onApply([change("FSR", "Fsr4Model", value)]),
    };
  } else if (family?.id === "fsr3") {
    const option = optionById(FFX_UPSCALER_ID);
    const all = ffxVersions;
    model = {
      label: "FSR version",
      options: all.filter((choice) => !isFsr4Version(choice.version)),
      selected: option ? effectiveValue(values, option) : "1",
      onPick: (value) => onApply(ffxUpscalerChanges(value)),
      name: (value) => all.find((choice) => choice.data === value)?.label,
    };
  } else if (family?.id === "xess") {
    const option = optionById("XeSS.NetworkModel");
    model = {
      label: "XeSS model",
      options: schemaChoices("XeSS.NetworkModel"),
      selected: option ? effectiveValue(values, option) : "0",
      onPick: (value) => onApply([change("XeSS", "NetworkModel", value)]),
    };
  } else if (family?.id === "dlss") {
    const override = values.DLSS?.RenderPresetOverride === "true";
    const presetValue = values.DLSS?.RenderPresetForAll ?? "0";
    model = {
      label: "DLSS preset",
      options: schemaChoices("DLSS.RenderPresetForAll"),
      selected: override ? presetValue : "0",
      onPick: (value) => onApply(value === "0"
        ? [change("DLSS", "RenderPresetOverride", "false")]
        : [change("DLSS", "RenderPresetOverride", "true"), change("DLSS", "RenderPresetForAll", value)]),
    };
  }
  const deckModel = Boolean(model && family?.id === "fsr4" && isDeckFsr4Preset(preset?.id));

  return (
    <>
      <PanelSection>
        <InfoRow
          id="framegen"
          title="Frame generation"
          info={
            <>
              Off applies at once. Turning it on closes this menu and presses OptiScaler&apos;s FG key
              (End), the way OptiScaler re-activates a generator the game feeds.
              {" "}Runs as {fgPair}; that pair, and the FFX FG version, are on the game&apos;s page
              and apply at the next launch.
              {plannedFg ? ` The OptiScaler wiki recommends ${plannedFg.input === "nofg" ? "no frame generation" :
                `${plannedFg.input_label} → ${plannedFg.output_label}`} for this game.` : ""}
              {" "}More than 2X only with the XeFG output.
            </>
          }
        />
        <PanelSectionRow>
          <ToggleField
            label="Frame generation"
            checked={fgOn}
            disabled={disabled || fgUnavailable}
            onChange={(checked) => (checked ? turnFrameGenOn() : onApply(disableFrameGenChanges()))}
          />
        </PanelSectionRow>
        {multiplierOption ? (
          <PanelSectionRow>
            <ValueDropdown
              label="Frame multiplier"
              disabled={disabled || !fgOn || !multiplierUsable}
              options={(multiplierOption.options ?? []).map((value) => ({
                data: value,
                label: labelFor(multiplierOption, value),
              }))}
              // Not XeFG: OptiScaler generates one frame, so that is what it shows.
              selected={multiplierUsable ? effectiveValue(values, multiplierOption) : (multiplierOption.options ?? ["1"])[0]}
              describe={(value) => labelFor(multiplierOption, value)}
              onPick={(value) => onApply([change(MULTIPLIER_SECTION, MULTIPLIER_KEY, value)])}
            />
          </PanelSectionRow>
        ) : null}
      </PanelSection>

      <PanelSection>
        <InfoRow
          id="upscaler"
          title="Upscaler"
          info={
            <>
              {preset?.description ? `${preset.description} ` : ""}
              A different upscaler applies at the next launch, or at once from the OptiScaler menu.
              {" "}{deckModel
                ? "The Steam Deck FSR 4 model applies at the next launch; the bundled FidelityFX upscaler runs it."
                : liveReady
                  ? "The model or version under it applies at once."
                  : "The model or version under it applies at the next launch."}
            </>
          }
        />
        <PanelSectionRow>
          <ValueDropdown
            label="Upscaler"
            disabled={disabled}
            options={families.map((candidate) => ({ data: candidate.id, label: candidate.label }))}
            selected={family?.id ?? rawUpscaler}
            describe={(code) => UPSCALER_LABELS[code] ?? curatedLabel("Upscalers.Dx12Upscaler", code) ?? code}
            onPick={pickFamily}
          />
        </PanelSectionRow>
        {model ? (
          <PanelSectionRow>
            <ValueDropdown
              label={model.label}
              disabled={disabled || model.options.length < 2}
              options={model.options}
              selected={model.selected}
              describe={(value) =>
                model.name?.(value) ?? (value === AUTO ? "Auto" : `${value} (not offered here)`)}
              onPick={model.onPick}
            />
          </PanelSectionRow>
        ) : null}
        {installing || fsr4Error ? (
          <PanelSectionRow>
            <Notice tone={fsr4Error ? "error" : "info"}>
              {fsr4Error ?? "Setting up FSR 4…"}
            </Notice>
          </PanelSectionRow>
        ) : null}
      </PanelSection>
    </>
  );
}
