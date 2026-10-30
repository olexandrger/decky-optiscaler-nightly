import { ButtonItem, PanelSection, PanelSectionRow } from "@decky/ui";
import { toaster } from "@decky/api";
import { useCallback, useEffect, useState } from "react";
import { checkUpdate, type UpdateInfo } from "../api";
import { KeyValue } from "./Common";

/** Decky's own plugin name, as plugin.json declares it. */
const PLUGIN_NAME = "Decky OptiScaler Nightly";
/** Decky's PluginInstallType.UPDATE. */
const INSTALL_TYPE_UPDATE = 2;

/**
 * Hand a release to Decky's own installer.
 *
 * Decky only updates store plugins, so the update goes through the same
 * `utilities/install_plugin` route its store and "Install Plugin from URL" use.
 * Decky shows its confirmation, verifies the SHA-256, replaces the plugin and
 * reloads it. Version "dev" is what "Install from URL" sends: Decky then takes
 * the name from the zip's plugin.json.
 */
export function installUpdate(info: UpdateInfo) {
  const backend = (window as any).DeckyBackend;
  if (!backend?.call || !info.url) {
    toaster.toast({ title: "Could not start the update", body: "Decky's installer is not reachable." });
    return;
  }
  void backend
    .call("utilities/install_plugin", info.url, PLUGIN_NAME, "dev", info.sha256 ?? "", INSTALL_TYPE_UPDATE)
    .catch((error: unknown) =>
      toaster.toast({ title: "Could not start the update", body: String(error) }));
}

/** Offers the fork's newest release in the Quick Access panel when it differs from this build. */
export function UpdateBanner() {
  const [info, setInfo] = useState<UpdateInfo | null>(null);

  useEffect(() => {
    let alive = true;
    checkUpdate(false)
      .then((result) => alive && setInfo(result))
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);

  if (!info?.ok || !info.available || !info.url) return null;

  return (
    <PanelSection>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          description={`Installed: ${info.current || "unknown"}. Decky asks before replacing it.`}
          onClick={() => installUpdate(info)}
        >
          Update to {info.latest}
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
}

/**
 * The Settings tab's view: installed and latest version, and a check that does
 * not wait for the cached answer.
 */
export function UpdateSection() {
  const [info, setInfo] = useState<UpdateInfo | null>(null);
  const [checking, setChecking] = useState(false);

  const check = useCallback(async (force: boolean) => {
    setChecking(true);
    try {
      setInfo(await checkUpdate(force));
    } catch (error) {
      setInfo({ ok: false, error: String(error), current: info?.current ?? "" });
    } finally {
      setChecking(false);
    }
  }, [info?.current]);

  useEffect(() => {
    void check(false);
    // Only on opening; the button asks again.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const status = checking
    ? "checking…"
    : !info
      ? ""
      : !info.ok
        ? `could not check: ${info.error ?? "unknown error"}`
        : info.available
          ? `${info.latest} available`
          : "up to date";

  return (
    <PanelSection title="Plugin updates">
      <PanelSectionRow>
        <KeyValue label="Installed" value={info?.current || "unknown"} />
      </PanelSectionRow>
      <PanelSectionRow>
        <KeyValue label="Latest release" value={status} />
      </PanelSectionRow>
      {info?.ok && info.available && info.url ? (
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => installUpdate(info)}>
            Update to {info.latest}
          </ButtonItem>
        </PanelSectionRow>
      ) : null}
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={checking} onClick={() => void check(true)}>
          Check for updates
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
}
