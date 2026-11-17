import { useCallback, useEffect, useRef, useState } from "react";
import { getRunningConfig, type RunningConfig } from "../api";
import type { ConfigValues, OptionChange } from "../types";

const POLL_MS = 2000;

/**
 * OptiScaler's settings in the running game, read through the in-game plugin.
 *
 * `merge` lays the values changed in the running game (OptiScaler's own menu)
 * over the ini's, so the panel's controls show what is actually in effect.
 * Only those: everything else stays the ini's, which is also what OptiScaler
 * runs unless it resolved an "auto" for itself.
 *
 * An edit made here wins over a snapshot taken before it: the snapshot still
 * holds the old value for up to one poll, and laying it over the edit would
 * flick the control back.
 */
export function useRunningConfig(targetDir: string | null, active: boolean) {
  const [running, setRunning] = useState<RunningConfig | null>(null);
  const requestedAt = useRef(0);
  const shownAt = useRef(0);
  const editedAt = useRef<Map<string, number>>(new Map());

  const refresh = useCallback(async () => {
    if (!targetDir || !active) {
      setRunning(null);
      return;
    }
    const asked = Date.now();
    requestedAt.current = asked;
    try {
      const next = await getRunningConfig(targetDir);
      if (requestedAt.current !== asked) return; // a newer request is under way
      shownAt.current = asked;
      setRunning(next.ready ? next : null);
    } catch {
      /* the panel shows the ini, as for a stopped game */
    }
  }, [targetDir, active]);

  useEffect(() => {
    void refresh();
    if (!active || !targetDir) return;
    const handle = window.setInterval(() => void refresh(), POLL_MS);
    return () => window.clearInterval(handle);
  }, [refresh, active, targetDir]);

  useEffect(() => {
    editedAt.current.clear();
  }, [targetDir]);

  const markEdited = useCallback((changes: OptionChange[]) => {
    const now = Date.now();
    for (const change of changes) editedAt.current.set(`${change.section}.${change.key}`, now);
  }, []);

  /** Keys changed in the running game, minus any edited here since the snapshot. */
  const unsaved = (running?.unsaved ?? []).filter(
    (key) => (editedAt.current.get(key) ?? 0) < shownAt.current
  );

  const merge = useCallback(
    (values: ConfigValues): ConfigValues => {
      if (!running || unsaved.length === 0) return values;
      const next: ConfigValues = {};
      for (const [section, keys] of Object.entries(values)) next[section] = { ...keys };
      for (const key of unsaved) {
        const value = running.values[key];
        if (value === undefined) continue;
        const [section, name] = key.split(".", 2);
        next[section] = { ...(next[section] ?? {}), [name]: value };
      }
      return next;
    },
    // `unsaved` is derived from these on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [running, unsaved.join("|")]
  );

  return { running, unsaved, refresh, merge, markEdited };
}
