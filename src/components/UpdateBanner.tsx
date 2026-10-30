import { ButtonItem, PanelSection, PanelSectionRow } from "@decky/ui";
import { toaster } from "@decky/api";
import { useEffect, useState } from "react";
import { checkUpdate, type UpdateInfo } from "../api";

/** Decky's own plugin name, as plugin.json declares it. */
const PLUGIN_NAME = "Decky OptiScaler Nightly";
/** Decky's PluginInstallType.UPDATE. */
const INSTALL_TYPE_UPDATE = 2;

/**
 * Offers the fork's newest release when it differs from this build.
 *
 * Decky only updates store plugins, so the update goes through Decky's own
 * installer: the same `utilities/install_plugin` route its store and "Install
 * Plugin from URL" use. Decky shows its confirmation, verifies the SHA-256,
 * replaces the plugin and reloads it. Version "dev" is what "Install from URL"
 * sends: Decky then takes the name from the zip's plugin.json.
 */
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

  const install = () => {
    const backend = (window as any).DeckyBackend;
    if (!backend?.call) {
      toaster.toast({ title: "Could not start the update", body: "Decky's installer is not reachable." });
      return;
    }
    void backend
      .call("utilities/install_plugin", info.url, PLUGIN_NAME, "dev", info.sha256 ?? "", INSTALL_TYPE_UPDATE)
      .catch((error: unknown) =>
        toaster.toast({ title: "Could not start the update", body: String(error) }));
  };

  return (
    <PanelSection>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          description={`Installed: ${info.current || "unknown"}. Decky asks before replacing it.`}
          onClick={install}
        >
          Update to {info.latest}
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
}
