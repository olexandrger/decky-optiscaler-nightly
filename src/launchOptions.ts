/**
 * The Steam launch options this plugin writes, and how to take them back out.
 *
 * Installing OptiScaler is two changes, not one: a folder full of files, and a
 * `WINEDLLOVERRIDES` entry in Steam. Removing it used to undo only the first,
 * so a game that had been "removed" still carried an override for a DLL that
 * was no longer there — harmless, but it is litter this plugin left behind and
 * nothing on screen admitted to it.
 *
 * Undoing the second half properly needs to know what was there first, which
 * only Steam can say and only while the plugin is running. `record_launch_options`
 * is therefore called at install time with whatever Steam currently passes, and
 * the answer is kept next to the install. Three things then become tellable
 * apart, which is the whole point:
 *
 * - **recorded and empty** — the game had no launch options at all, so removing
 *   OptiScaler can clear the field outright.
 * - **recorded and not empty** — there is something to put back, verbatim.
 * - **not recorded** — the install predates this, or Steam would not say. Only
 *   the override this plugin recognises is removed; anything else is left, on
 *   the grounds that it is not ours to guess about.
 */
import { getLaunchOptions, recordLaunchOptions } from "./api";
import { readLaunchOptions } from "./hooks/useRunningGame";
import type { LaunchRecord } from "./types";

/** What to do with the launch options when OptiScaler is removed. */
export type LaunchAction = "restore" | "clear" | "keep";

export interface LaunchState {
  /** Steam's current launch options, or null when the client will not say. */
  current: string | null;
  /** What was there before the install, or null when nothing was recorded. */
  recorded: string | null;
  /** The proxy filename OptiScaler is installed as, e.g. `dxgi.dll`. */
  filename: string;
}

export interface LaunchChoice {
  action: LaunchAction;
  label: string;
  description: string;
  /** What Steam would be set to, or null to leave the field untouched. */
  value: string | null;
}

/** The `WINEDLLOVERRIDES` entry an install under this filename adds. */
export function overrideStem(filename: string): string {
  return filename.replace(/\.(dll|asi)$/i, "");
}

export function launchOptionFor(filename: string): string {
  if (filename.toLowerCase().endsWith(".asi")) return "%command%";
  return `WINEDLLOVERRIDES="${overrideStem(filename)}=n,b" %command%`;
}

/** `WINEDLLOVERRIDES=...` assignments, unquoted or in either quote. */
const OVERRIDES_RE = /WINEDLLOVERRIDES=(?:"([^"]*)"|'([^']*)'|(\S+))/gi;

/**
 * Wine's override list: `;`-separated clauses, each `dll[,dll...]=mode`. The
 * mode can itself hold commas (`n,b`), so a clause splits at its first `=`.
 */
function clauses(value: string): { dlls: string[]; mode: string }[] {
  return value.split(";").filter((clause) => clause.trim() !== "").map((clause) => {
    const at = clause.indexOf("=");
    const names = at < 0 ? clause : clause.slice(0, at);
    return { dlls: names.split(",").map((dll) => dll.trim()).filter(Boolean), mode: at < 0 ? "" : clause.slice(at + 1) };
  });
}

export function hasOverride(options: string | null, filename: string): boolean {
  if (!options) return false;
  const stem = overrideStem(filename).toLowerCase();
  for (const match of options.matchAll(OVERRIDES_RE)) {
    const value = match[1] ?? match[2] ?? match[3] ?? "";
    if (clauses(value).some((clause) =>
      clause.mode.trim().toLowerCase() === "n,b" && clause.dlls.some((dll) => dll.toLowerCase() === stem))) {
      return true;
    }
  }
  return false;
}

/**
 * The same launch options with our override taken out and nothing else.
 *
 * Conservative on purpose: a user may have added `-dx12` or a gamescope
 * wrapper of their own alongside the override, and those are not this plugin's
 * to delete. Inside `WINEDLLOVERRIDES` only our proxy's name goes: another
 * DLL in the same clause (`dxgi,d3d11=n,b`) or another clause
 * (`dxgi=n,b;winmm=n,b`) stays, and the assignment goes only when nothing is
 * left in it (ported from upstream's audit, PD-07: the whole assignment used
 * to go). A leftover bare `%command%` is Steam's own default written out
 * longhand, so that collapses to an empty field.
 */
export function stripOverride(options: string, filename: string): string {
  const stem = overrideStem(filename).toLowerCase();
  const withoutOurs = options.replace(OVERRIDES_RE, (match, double, single, bare) => {
    const value: string = double ?? single ?? bare ?? "";
    const kept = clauses(value)
      .map((clause) => ({ ...clause, dlls: clause.dlls.filter((dll) => dll.toLowerCase() !== stem) }))
      .filter((clause) => clause.dlls.length > 0);
    if (kept.length === 0) return "";
    const rebuilt = kept.map((clause) => `${clause.dlls.join(",")}=${clause.mode}`).join(";");
    if (rebuilt === value) return match;
    const quote = double !== undefined ? '"' : single !== undefined ? "'" : "";
    return `WINEDLLOVERRIDES=${quote}${rebuilt}${quote}`;
  });
  const collapsed = withoutOurs.replace(/\s+/g, " ").trim();
  return collapsed === "%command%" ? "" : collapsed;
}

/**
 * Steam's launch options with ours added, keeping everything already there.
 *
 * `wanted` is what this plugin would write on its own (`launchOptionFor`, or
 * the set-up plan's string): its `n,b` overrides and the game flags after its
 * `%command%`. Writing that over the field used to drop the player's own
 * wrappers (gamemoderun, gamescope, mangohud), environment and game arguments
 * (ported from upstream's audit, PD-02). Instead:
 *
 * - our DLLs join the last `WINEDLLOVERRIDES` in front of `%command%`, the one
 *   the game actually gets, as their own `dll=n,b` clauses (a DLL of ours
 *   already listed with another mode is moved to `n,b`); with no assignment
 *   there, one is put at the very front, which reaches the game through any
 *   wrapper;
 * - options with no `%command%` are game arguments to Steam, so they move
 *   behind one;
 * - each wanted flag not already after `%command%` is appended.
 *
 * With nothing to merge into (empty, or Steam would not say) it is `wanted`.
 */
export function mergeOverride(current: string | null, wanted: string): string {
  if (current === null || current.trim() === "") return wanted;
  const wantedAt = wanted.indexOf("%command%");
  const stems: string[] = [];
  for (const match of wanted.slice(0, Math.max(wantedAt, 0)).matchAll(OVERRIDES_RE)) {
    for (const clause of clauses(match[1] ?? match[2] ?? match[3] ?? "")) {
      if (clause.mode.trim().toLowerCase() === "n,b") stems.push(...clause.dlls);
    }
  }
  const flags = wantedAt < 0 ? [] : wanted.slice(wantedAt + "%command%".length).split(/\s+/).filter(Boolean);

  let merged = current.trim().replace(/\s+/g, " ");
  if (!merged.includes("%command%")) merged = `%command% ${merged}`;
  const commandAt = merged.indexOf("%command%");

  if (stems.length > 0) {
    const effective = [...merged.matchAll(OVERRIDES_RE)].filter((m) => (m.index ?? 0) < commandAt).pop();
    if (effective) {
      const [whole, double, single, bare] = effective;
      const value = double ?? single ?? bare ?? "";
      let parsed = clauses(value);
      for (const stem of stems) {
        const lower = stem.toLowerCase();
        const ok = parsed.some((clause) => clause.mode.trim().toLowerCase() === "n,b"
          && clause.dlls.some((dll) => dll.toLowerCase() === lower));
        if (ok) continue;
        parsed = parsed
          .map((clause) => ({ ...clause, dlls: clause.dlls.filter((dll) => dll.toLowerCase() !== lower) }))
          .filter((clause) => clause.dlls.length > 0);
        parsed.push({ dlls: [stem], mode: "n,b" });
      }
      const rebuilt = parsed.map((clause) => `${clause.dlls.join(",")}=${clause.mode}`).join(";");
      if (rebuilt !== value) {
        // A bare value cannot hold `;` (the shell would end the command there).
        const quote = single !== undefined ? "'" : '"';
        const start = effective.index ?? 0;
        merged = merged.slice(0, start) + `WINEDLLOVERRIDES=${quote}${rebuilt}${quote}`
          + merged.slice(start + whole.length);
      }
    } else {
      merged = `WINEDLLOVERRIDES="${stems.map((stem) => `${stem}=n,b`).join(";")}" ${merged}`;
    }
  }

  const after = merged.slice(merged.indexOf("%command%") + "%command%".length).split(/\s+/).filter(Boolean);
  const missing = flags.filter((flag) => !after.includes(flag));
  return missing.length ? `${merged} ${missing.join(" ")}` : merged;
}

/**
 * What the user can be offered when OptiScaler is removed, best answer first.
 *
 * "Put back what was there" is only offered when there was something — which is
 * exactly the condition the question is worth asking under. Clearing is offered
 * whenever the result is known: the record says the game had no launch options
 * at all, or the override we installed is visibly in what Steam reports.
 *
 * When neither is true, nothing here is known to be ours — and the honest
 * answer used to be to offer nothing, which is how a client build with no way
 * to read launch options turned this whole feature into a dialog that did
 * nothing. Clearing is offered even then, last and never as the default, and
 * says plainly that it empties the field rather than pruning it. Doing nothing
 * has to be a choice the user makes, not one made for them by a missing API.
 *
 * Two choices that would produce the same launch options are collapsed into
 * one, because a dialog offering the same outcome twice is one nobody can
 * answer.
 */
export function launchChoices(state: LaunchState): LaunchChoice[] {
  const { current, recorded, filename } = state;
  const restore: LaunchChoice[] =
    recorded !== null && recorded.trim() !== "" && recorded !== current
      ? [{
          action: "restore",
          label: "Put back what was there before",
          description: recorded,
          value: recorded,
        }]
      : [];

  const keep: LaunchChoice = {
    action: "keep",
    label: "Leave them as they are",
    description: current
      ? `Steam keeps passing: ${current}`
      : "Nothing in Steam is changed.",
    value: null,
  };

  // What removing our half would leave behind, when that is knowable.
  const known =
    recorded === ""
      ? ""
      : current !== null && hasOverride(current, filename)
        ? stripOverride(current, filename)
        : null;
  const blind = known === null && current === null;

  if (known === null && !blind) return [...restore, keep];

  const cleared = known ?? "";
  const clear: LaunchChoice = {
    action: "clear",
    label:
      blind
        ? "Clear the launch options"
        : cleared === ""
          ? "Clear the launch options"
          : "Remove only the OptiScaler override",
    description: blind
      ? `Steam would not report this game's launch options, so this empties the field
         completely — including anything you added yourself.`
      : cleared === ""
        ? recorded === ""
          ? "This game had none before OptiScaler was installed."
          : "Leaves the launch options empty."
        : `Leaves: ${cleared}`,
    value: cleared,
  };

  // A choice that would write back what Steam already has, or that lands on
  // the same string as one already on offer, is not a choice.
  const duplicate =
    cleared === current || restore.some((choice) => choice.value === cleared);
  if (duplicate) return [...restore, keep];
  // Blind clearing is a guess, so it never leads — the safe answer does.
  return blind ? [...restore, keep, clear] : [...restore, clear, keep];
}

/**
 * The choice to act on, honouring a remembered answer only if it still applies.
 *
 * Undefined when there is nothing to choose between — a game Steam does not
 * own, which has no launch options to decide about at all.
 */
export function resolveChoice(
  choices: LaunchChoice[],
  remembered: LaunchAction | null,
): LaunchChoice | undefined {
  const match = remembered && choices.find((choice) => choice.action === remembered);
  return match || choices[0];
}

/** Whether the removal dialog has anything to ask about at all. */
export function hasLaunchQuestion(appid: string | null, state: LaunchState): boolean {
  if (!appid) return false;
  return launchChoices(state).some((choice) => choice.action !== "keep");
}

/** The record as the backend reports it, as the nullable string the UI reasons in. */
export function recordedValue(record: LaunchRecord | undefined): string | null {
  return record?.recorded ? record.value : null;
}

/**
 * What Steam passes this game right now, from whichever source can say.
 *
 * The client is asked first because it is live; Steam only flushes its config
 * file periodically, so a value written seconds ago may not be in it yet. But
 * the client getter is missing from some builds entirely, and on those this
 * was the single point of failure that made the whole feature inert — the
 * plugin could see neither its own override nor anything it had replaced.
 */
export async function currentLaunchOptions(appid: string | null): Promise<string | null> {
  if (!appid) return null;
  const fromClient = await readLaunchOptions(Number(appid));
  if (fromClient !== null) return fromClient;
  try {
    const found = await getLaunchOptions(appid);
    return found.found ? found.value : null;
  } catch {
    return null;
  }
}

/**
 * Keep this game's launch options as they were, before we write our own.
 *
 * Called after a successful install and before the override is set, which is
 * the only moment Steam still holds the answer. Write-once in the backend, so
 * calling it again — a reinstall, or the checklist's launch-options toggle
 * being switched back on — cannot overwrite the original with our own override.
 *
 * What the client reads is passed along rather than looked up again in the
 * backend, because it is the fresher of the two; `null` tells the backend to
 * fall back to Steam's config file instead of recording a value nobody read.
 */
export async function recordPreviousLaunchOptions(
  appid: string | null,
  targetDir: string,
): Promise<void> {
  if (!appid) return;
  const current = await readLaunchOptions(Number(appid));
  try {
    await recordLaunchOptions(targetDir, current, appid);
  } catch {
    /* the record is a convenience; failing to write it must not fail an install */
  }
}
