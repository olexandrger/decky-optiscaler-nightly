import { ButtonItem, Navigation, PanelSection, PanelSectionRow } from "@decky/ui";
import { toaster } from "@decky/api";
import { useCallback, useEffect, useState } from "react";
import { resolveGame } from "../shortcuts";
import { useAutoPlan } from "../hooks/useAutoPlan";
import { useConfig } from "../hooks/useConfig";
import { useLiveStatus } from "../hooks/useLiveStatus";
import { LiveDiagnostics } from "./LiveDiagnostics";
import { fpsOverlayLabel, nextFpsOverlay } from "./FpsOverlayButton";
import { ButtonRow, InfoRow } from "./Compact";
import { QuickControls } from "./QuickControls";
import { useRunningConfig } from "../hooks/useRunningConfig";
import { OutdatedInstall } from "./OutdatedInstall";
import { restartGame } from "../hooks/useRunningGame";
import { keepRunningChanges, toggleMenu, type RunningConfig } from "../api";
import { optionById } from "../config/tabs";
import type { Game, GameDetail, LiveStatus, RunningGame, OptionChange } from "../types";
import { GamesList } from "./GamesList";
import { TabStrip } from "./TabStrip";
import { UpdateBanner } from "./UpdateBanner";
import { Centered, Notice, Pill } from "./Common";
import { UPSCALER_LABELS, curatedLabel } from "../config/labels";
import { sameBackend } from "../config/basic";

/**
 * The Quick Access panel: the plugin's front door, opened over a running game.
 *
 * Two tabs, because those are the two things worth doing without leaving the
 * game — change what the running game is doing, or pick a different game to set
 * up. Everything else (all the INI options, monitoring, library management)
 * lives on the full page, which this hands off to.
 *
 * It is deliberately terse. This column is ~310px wide and is read with a game
 * running behind it, so the explanations live on the full page and what is here
 * is the controls, their current state, and nothing else.
 */
export function QuickPanel({
  runningGame,
  onOpenManager,
}: Readonly<{
  runningGame: RunningGame | null;
  onOpenManager: (gamePath?: string, gameName?: string, appid?: string) => void;
}>) {
  // Land on the running game when there is one; otherwise the list is the only
  // thing this panel can usefully show.
  const [tab, setTab] = useState(runningGame ? "now" : "games");

  return (
    <>
      <UpdateBanner />
      <TabStrip
        tabs={[
          { id: "now", title: "Now Playing" },
          { id: "games", title: "Games" },
        ]}
        active={tab}
        onChange={setTab}
      />

      {tab === "now" ? (
        <NowPlayingTab
          runningGame={runningGame}
          onOpenManager={onOpenManager}
          onBrowse={() => setTab("games")}
        />
      ) : (
        <>
          <GamesList
            onOpenGame={(game: Game) =>
              onOpenManager(game.path, game.name, game.appid ?? undefined)
            }
          />
          <PanelSection>
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={() => onOpenManager()}>
                Libraries & all settings
              </ButtonItem>
            </PanelSectionRow>
          </PanelSection>
        </>
      )}
    </>
  );
}

function NowPlayingTab({
  runningGame,
  onOpenManager,
  onBrowse,
}: Readonly<{
  runningGame: RunningGame | null;
  onOpenManager: (gamePath?: string, gameName?: string, appid?: string) => void;
  onBrowse: () => void;
}>) {
  const [detail, setDetail] = useState<GameDetail | null>(null);
  const [resolving, setResolving] = useState(false);
  const [notFound, setNotFound] = useState(false);
  const [diagnostics, setDiagnostics] = useState(false);

  const resolve = useCallback(async () => {
    if (!runningGame) {
      setDetail(null);
      setNotFound(false);
      return;
    }
    setResolving(true);
    setNotFound(false);
    try {
      const result = await resolveGame(runningGame.appid);
      if (result.found && result.detail) {
        setDetail(result.detail);
      } else {
        setDetail(null);
        setNotFound(true);
      }
    } catch {
      setNotFound(true);
    } finally {
      setResolving(false);
    }
  }, [runningGame]);

  useEffect(() => {
    void resolve();
  }, [resolve]);

  // Another plugin's (or tool's) OptiScaler is not ours to configure.
  const foreign = Boolean(detail?.install.foreign);
  const installed = Boolean(detail?.install.installed) && !foreign;
  const targetDir = installed && detail ? detail.install.path : null;
  // Re-read after the game exits so overlay-side Save is not overwritten.
  const config = useConfig(targetDir, installed, runningGame?.appid ?? null, { pressKeys: true });
  // This tab is only ever shown over a running game, so the live channel is
  // always worth polling here.
  const { status: live, refresh: refreshLive } = useLiveStatus(targetDir, Boolean(runningGame));
  // What the running game actually has, including changes made in OptiScaler's
  // own menu; laid over the ini for the keys changed in-game.
  const runningCfg = useRunningConfig(targetDir, Boolean(runningGame && live?.ready));
  const values = runningCfg.merge(config.values);
  const applyOptions = (changes: OptionChange[]) => {
    runningCfg.markEdited(changes);
    config.setOptions(changes);
  };
  // Read-only over a running game: the mode is chosen on the full page, where
  // the plan it comes from can actually be read.
  const { plan, auto } = useAutoPlan(detail?.path ?? null, detail?.name ?? null, installed);

  if (!runningGame) {
    return (
      <PanelSection title="Nothing running">
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={onBrowse}>
            Browse games
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    );
  }

  const overlay = fpsOverlayLabel(values, live);
  // An upscaler the running game cannot switch to: a rebuild keeps the backend
  // it runs, and only OptiScaler's own menu starts another one. The presets
  // keep the three backend keys in one family, so the DX12 key says which.
  const ranUpscaler = runningCfg.running?.values?.["Upscalers.Dx12Upscaler"];
  const pickedUpscaler = config.values?.Upscalers?.Dx12Upscaler;
  const upscalerWaits = Boolean(config.dirty && ranUpscaler && pickedUpscaler &&
    pickedUpscaler.toLowerCase() !== "auto" && !sameBackend(ranUpscaler, pickedUpscaler));

  const openOptiScalerMenu = () => {
    if (!detail) return;
    const target = detail.install.path;
    // OptiScaler's menu opens on its shortcut: close Quick Access first, or
    // the key goes to Steam's overlay, then give the game a moment for focus.
    Navigation.CloseSideMenus();
    window.setTimeout(() => {
      void toggleMenu(target).then((result) => {
        if (!result.ok) {
          toaster.toast({
            title: "Could not open the OptiScaler menu",
            body: result.error ?? "Press its shortcut key (Insert) instead.",
          });
        }
      });
    }, 450);
  };

  return (
    <>
      <PanelSection title={runningGame.name}>
        <InfoRow id="status" info={installed ? <StatusInfo live={live} /> : undefined}>
          <InstallPill filename={detail?.install.filename} resolving={resolving} />
          {installed ? <LivePill live={live} /> : null}
        </InfoRow>
        {installed && runningCfg.running ? (
          <RunningLine
            running={runningCfg.running}
            unsaved={runningCfg.unsaved}
            targetDir={targetDir}
            onKept={() => {
              void config.reload();
              void runningCfg.refresh();
            }}
          />
        ) : null}
        {installed && detail ? (
          <ButtonRow
            buttons={[
              { id: "optiscaler-menu", label: "OptiScaler menu", onClick: openOptiScalerMenu },
              ...(overlay !== null
                ? [{
                    id: "fps-overlay",
                    label: `Overlay: ${overlay}`,
                    disabled: config.loading,
                    onClick: () => applyOptions(nextFpsOverlay(values, live)),
                  }]
                : []),
            ]}
          />
        ) : null}
      </PanelSection>

      {/* A game set up by an earlier build keeps that build's OptiScaler and
          live-control ASI until it is reapplied. */}
      {installed && detail ? (
        <OutdatedInstall
          install={detail.install}
          live={live}
          runningGame={runningGame}
          onUpdated={() => {
            void resolve();
            void refreshLive();
          }}
        />
      ) : null}

      {resolving ? <Centered>Looking up this game…</Centered> : null}

      {!resolving && notFound ? (
        <PanelSection>
          <PanelSectionRow>
            <Notice tone="warn">Not an installed Steam game — add its folder as a library.</Notice>
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={() => onOpenManager()}>
              Manage games
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      ) : null}

      {!resolving && !notFound && foreign ? (
        <PanelSection>
          <PanelSectionRow>
            <Notice tone="warn"
              title={detail?.install.owner ? `Installed by ${detail.install.owner}` : "Installed by another tool"}>
              {detail?.install.owner
                ? `Manage this game in ${detail.install.owner}. To use the nightly here, remove OptiScaler from it there first.`
                : "Remove the OptiScaler already in this game's folder before setting it up here."}
            </Notice>
          </PanelSectionRow>
        </PanelSection>
      ) : null}

      {!resolving && !notFound && !foreign && !installed ? (
        <PanelSection>
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              onClick={() =>
                onOpenManager(detail?.path, detail?.name, String(runningGame.appid))
              }
            >
              Set up OptiScaler
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      ) : null}

      {installed && detail ? (
        <>
          {/* A write the backend would not take reaches neither the file nor
              the running game: said, rather than left looking as if it worked. */}
          {config.error ? (
            <PanelSection>
              <PanelSectionRow>
                <Notice tone="error">{config.error}</Notice>
              </PanelSectionRow>
            </PanelSection>
          ) : null}

          {config.loading ? (
            <Centered>Reading config…</Centered>
          ) : (
            <QuickControls
              values={values}
              configured={config.values}
              live={live}
              gpu={detail.gpu}
              ffx={detail.install.fsr4?.ffx}
              plan={plan}
              auto={auto}
              onApply={applyOptions}
              onEnableFsr4={config.enableFsr4}
            />
          )}

          {config.dirty ? (
            <PanelSection>
              <PanelSectionRow>
                <ButtonItem
                  layout="below"
                  description={upscalerWaits
                    ? `${UPSCALER_LABELS[pickedUpscaler!] ?? pickedUpscaler} starts at the next launch. ` +
                      "OptiScaler menu (top) switches it now."
                    : undefined}
                  onClick={() => {
                    if (restartGame(runningGame)) {
                      config.clearDirty();
                      toaster.toast({ title: "Restarting", body: runningGame.name });
                    } else {
                      toaster.toast({
                        title: "Could not restart",
                        body: "Relaunch the game manually.",
                      });
                    }
                  }}
                >
                  Restart game to apply
                </ButtonItem>
              </PanelSectionRow>
            </PanelSection>
          ) : null}

          <PanelSection>
            <ButtonRow
              buttons={[
                {
                  id: "diagnostics",
                  label: diagnostics ? "Hide diagnostics" : "Diagnostics",
                  onClick: () => setDiagnostics((open) => !open),
                },
                {
                  id: "all-settings",
                  label: "All settings",
                  onClick: () => onOpenManager(detail.path, detail.name, String(runningGame.appid)),
                },
              ]}
            />
          </PanelSection>
          {diagnostics ? <LiveDiagnostics targetDir={detail.install.path} embedded /> : null}
        </>
      ) : null}
    </>
  );
}

/** What applies now and what at the next launch, behind the status ⓘ. */
function StatusInfo({ live }: Readonly<{ live: LiveStatus | null }>) {
  if (!live?.ready) {
    return (
      <>
        Changes made here apply when the game restarts
        {live?.state === "failed" ? ": the in-game plugin does not match this OptiScaler build (see Diagnostics)." :
          !live?.asi_installed ? ": this game has no in-game plugin (Update OptiScaler in this game adds it)." : "."}
      </>
    );
  }
  return (
    <>
      Live: frame generation and its multiplier, the upscaler&apos;s model or version, sharpness and the FPS
      overlay apply at once. Turning frame generation on closes this menu to press OptiScaler&apos;s FG key.
      A different upscaler or frame generator applies at the next launch, or at once from the OptiScaler
      menu.
    </>
  );
}

/** One line on what the game is running, and what was changed in OptiScaler's menu. */
function RunningLine({
  running,
  unsaved,
  targetDir,
  onKept,
}: Readonly<{ running: RunningConfig; unsaved: string[]; targetDir: string | null; onKept: () => void }>) {
  const v = running.values;
  const fgOutput = (v["FrameGen.FGOutput"] ?? "").toLowerCase();
  const fg = v["FrameGen.Enabled"] === "true" ? curatedLabel("FrameGen.FGOutput", fgOutput) ?? fgOutput : "FG off";
  const upscaler = running.created?.upscaler ?? null;
  const parts = [upscaler, fg].filter(Boolean);
  return (
    <>
      {parts.length > 0 ? (
        <PanelSectionRow>
          <div data-running style={{ fontSize: "12px", opacity: 0.75, padding: "0 0 4px" }}>
            Running: {parts.join(" · ")}
          </div>
        </PanelSectionRow>
      ) : null}
      {unsaved.length > 0 && targetDir ? (
        <>
          <InfoRow
            id="unsaved"
            info={unsaved.map((key) => `${optionById(key)?.label ?? key}: ${v[key] ?? "?"}`).join(" · ")}
          >
            <span style={{ fontSize: "12px" }}>
              {unsaved.length} changed in OptiScaler&apos;s menu, not saved
            </span>
          </InfoRow>
          <ButtonRow
            buttons={[{
              id: "keep-running",
              label: "Keep them in OptiScaler.ini",
              onClick: () => {
                void keepRunningChanges(targetDir, unsaved).then((result) => {
                  toaster.toast({
                    title: result.ok ? "Kept in OptiScaler.ini" : "Could not keep them",
                    body: result.ok ? `${result.kept?.length ?? 0} setting(s)` : result.error ?? "",
                  });
                  onKept();
                });
              },
            }]}
          />
        </>
      ) : null}
    </>
  );
}

/** Whether OptiScaler is set up for this game, and under which name. */
function InstallPill({
  filename,
  resolving,
}: Readonly<{ filename?: string | null; resolving: boolean }>) {
  if (filename) return <Pill color="#2f6b3f">{filename}</Pill>;
  return <Pill>{resolving ? "checking…" : "not installed"}</Pill>;
}

/** Whether the in-game plugin is answering, shown wherever the game is. */
function LivePill({ live }: Readonly<{ live: LiveStatus | null }>) {
  if (!live) return null;
  if (live.ready) {
    // An in-game plugin older than the one this build ships attaches and
    // answers normally, so the only sign of it is what it leaves out. Say so
    // here; the Setup tab's Logs page explains it and offers the reinstall.
    if (live.asi_current === false) return <Pill color="#5a4a20">live · outdated</Pill>;
    return <Pill color="#2f6b3f">live</Pill>;
  }
  // Loaded, still looking for OptiScaler's Config.
  if (live.attached) return <Pill>connecting…</Pill>;
  // It looked and gave up: its layout does not match this OptiScaler build.
  // Nothing was written; panel edits wait for a restart.
  if (live.state === "failed") return <Pill color="#5a4a20">restart to apply</Pill>;
  // No live-control plugin in this game: panel edits wait for a restart.
  if (!live.asi_installed) return <Pill>restart to apply</Pill>;
  return <Pill color="#5a4a20">not connected</Pill>;
}
