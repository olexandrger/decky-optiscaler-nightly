import { ButtonItem, PanelSection, PanelSectionRow } from "@decky/ui";
import { toaster } from "@decky/api";
import { useCallback, useEffect, useState } from "react";
import { liveDiagnostics, saveDebugReport, setLogging, type LiveDiagnostics as Report } from "../api";
import { KeyValue, Notice } from "./Common";

const TONE_COLORS = { ok: "#7fd08a", warn: "#e6c36a", bad: "#ff8a80" } as const;

/**
 * Everything that decides whether live editing works in this game, on one
 * screen: which OptiScaler is installed and which one last ran, which one the
 * in-game plugin was built for, whether OptiScaler loaded it, what it reports
 * and the last command it applied. "Save debug report" writes the same facts
 * plus the raw status, command, plugin log and OptiScaler.log excerpts to
 * ~/Downloads, as one text file to send on.
 */
export function LiveDiagnostics({
  targetDir,
  embedded,
}: Readonly<{
  targetDir: string;
  /** Shown already open, under a button of the caller's (the Quick Access panel). */
  embedded?: boolean;
}>) {
  const [open, setOpen] = useState(Boolean(embedded));
  const [report, setReport] = useState<Report | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      setReport(await liveDiagnostics(targetDir));
    } catch (error) {
      setReport({ ok: false, facts: [], report: "", logging: false, error: String(error) });
    } finally {
      setBusy(false);
    }
  }, [targetDir]);

  useEffect(() => {
    if (embedded) void load();
  }, [embedded, load]);

  return (
    <PanelSection title={embedded ? undefined : "Live-control diagnostics"}>
      {embedded ? null : (
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={busy}
          onClick={() => {
            const next = !open;
            setOpen(next);
            if (next) void load();
          }}
        >
          {open ? "Hide diagnostics" : "Show diagnostics"}
        </ButtonItem>
      </PanelSectionRow>
      )}

      {open && report?.error ? (
        <PanelSectionRow>
          <Notice tone="error">{report.error}</Notice>
        </PanelSectionRow>
      ) : null}

      {open && report && !report.error ? (
        <>
          <PanelSectionRow>
            <div style={{ padding: "2px 0", fontSize: "12px" }}>
              {report.facts.map(([label, value, tone]) => (
                <KeyValue
                  key={label}
                  label={label}
                  value={<span style={{ color: TONE_COLORS[tone] ?? undefined }}>{value}</span>}
                />
              ))}
            </div>
          </PanelSectionRow>

          {!report.logging ? (
            <>
              <PanelSectionRow>
                <Notice tone="warn">
                  OptiScaler is not writing OptiScaler.log here, and that log is the only record of
                  whether it loaded the in-game plugin and which build ran.
                </Notice>
              </PanelSectionRow>
              <PanelSectionRow>
                <ButtonItem
                  layout="below"
                  onClick={async () => {
                    const result = await setLogging(targetDir, true);
                    toaster.toast({
                      title: result.ok ? "OptiScaler's log turned on" : "Could not turn the log on",
                      body: result.ok ? "It starts with the next game launch." : result.error ?? "",
                    });
                    void load();
                  }}
                >
                  Turn on OptiScaler&apos;s log
                </ButtonItem>
              </PanelSectionRow>
            </>
          ) : null}

          <PanelSectionRow>
            <ButtonItem layout="below" disabled={busy} onClick={() => void load()}>
              {busy ? "Reading…" : "Refresh"}
            </ButtonItem>
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              disabled={busy}
              description="One text file in ~/Downloads with all of the above and the raw logs."
              onClick={async () => {
                const result = await saveDebugReport(targetDir);
                toaster.toast({
                  title: result.ok ? "Debug report saved" : "Could not save the report",
                  body: result.ok ? result.path ?? "" : result.error ?? "",
                });
              }}
            >
              Save debug report
            </ButtonItem>
          </PanelSectionRow>
        </>
      ) : null}
    </PanelSection>
  );
}
