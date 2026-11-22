import { ButtonItem, PanelSection, PanelSectionRow } from "@decky/ui";
import { toaster } from "@decky/api";
import { useState } from "react";
import { updateInstall } from "../api";
import { restartGame } from "../hooks/useRunningGame";
import type { InstallInfo, LiveStatus, RunningGame } from "../types";
import { Notice } from "./Common";

/**
 * Why this game's OptiScaler needs reapplying, or null when it does not.
 *
 * Updating the plugin does not touch any game: a game set up by an earlier
 * build keeps that build's OptiScaler, and the live-control ASI compiled for
 * it, which no longer matches what the panel expects (the ASI then refuses to
 * attach rather than write to the wrong place).
 */
export function outdatedReason(install: InstallInfo | null | undefined,
                               live: LiveStatus | null | undefined): string | null {
  if (!install?.installed || !install.managed || install.foreign) return null;
  if (install.outdated) {
    return `This game has OptiScaler ${install.version ?? "from an older build"}; ` +
      `this plugin ships ${install.bundled_version ?? "a newer one"}.` +
      (install.flat_layout ? " Updating also moves it into an OptiScaler folder and puts the game's own DLLs back." : "");
  }
  if (install.flat_layout) {
    return "OptiScaler's DLLs sit in this game's folder, over the game's own. Updating moves them " +
      "into an OptiScaler folder, as OptiScaler 10 lays itself out, and puts the game's own back.";
  }
  if (live?.asi_current === false) {
    return "This game's live-control plugin is from an older build of this plugin.";
  }
  // Set up by a build that shipped no live-control plugin (every fork release
  // before live editing came back), on the same OptiScaler as this one.
  if (live && live.asi_available && !live.asi_installed) {
    return "This game was set up without live control.";
  }
  return null;
}

/** Offers to reapply the bundled OptiScaler to a game that holds another one. */
export function OutdatedInstall({
  install,
  live,
  runningGame,
  onUpdated,
}: Readonly<{
  install: InstallInfo | null | undefined;
  live?: LiveStatus | null;
  /** The game, when it is running: the update then loads on its next start. */
  runningGame?: RunningGame | null;
  onUpdated?: () => void;
}>) {
  const [busy, setBusy] = useState(false);
  const [updated, setUpdated] = useState(false);
  const reason = outdatedReason(install, live);

  if (updated && runningGame) {
    return (
      <PanelSection>
        <PanelSectionRow>
          <Notice tone="success">Updated. The game loads the new OptiScaler when it restarts.</Notice>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => {
              if (restartGame(runningGame)) {
                setUpdated(false);
                toaster.toast({ title: "Restarting", body: runningGame.name });
              } else {
                toaster.toast({ title: "Could not restart", body: "Relaunch the game manually." });
              }
            }}
          >
            Restart game to load it
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    );
  }
  if (!reason || !install) return null;

  return (
    <PanelSection>
      <PanelSectionRow>
        <Notice tone="warn">
          {reason} Update it so the panel's settings and live control match. OptiScaler.ini is kept.
        </Notice>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            try {
              const result = await updateInstall(install.path);
              if (result.ok) {
                setUpdated(true);
                toaster.toast({
                  title: "OptiScaler updated",
                  body: runningGame ? "Restart the game to load it." : `Now ${install.bundled_version ?? "the bundled build"}.`,
                });
                onUpdated?.();
              } else {
                toaster.toast({ title: "Could not update OptiScaler", body: result.error ?? "Unknown error" });
              }
            } finally {
              setBusy(false);
            }
          }}
        >
          {busy ? "Updating…" : "Update OptiScaler in this game"}
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
}
