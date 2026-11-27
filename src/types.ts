export type OptionType = "bool" | "enum" | "int" | "float" | "string" | "keycode";

export interface OptionMeta {
  section: string;
  key: string;
  label: string;
  description: string;
  type: OptionType;
  default: string;
  options?: string[];
  optionLabels?: Record<string, string>;
  min?: number;
  max?: number;
  inheritedFrom?: string;
}

export interface PayloadStatus {
  version: string;
  archive_present: boolean;
  archive_path: string;
  extracted: boolean;
  extract_path: string;
  extractor: string | null;
  optiscaler_version: string;
  proxy_filenames: string[];
  default_proxy: string;
}

export interface Library {
  path: string;
  name: string;
  source: "steam" | "custom";
  game_count: number;
  available: boolean;
}

export interface Game {
  appid: string | null;
  name: string;
  path: string;
  source: "steam" | "custom";
  size_on_disk: number;
  installed?: boolean;
  filename?: string | null;
  install_path?: string | null;
  /** Which library it was found in; only set by the flat all-games list. */
  library?: string;
}

export interface ExeCandidate {
  path: string;
  relative: string;
  score: number;
  executables: string[];
}

export interface GpuInfo {
  names: string[];
  name: string | null;
  gfx: string | null;
  vendor: string | null;
  generation: string | null;
  fsr4: "native" | "int8" | "experimental" | "unsupported" | "unknown";
}

export interface FfxUpscalerInfo {
  present: boolean;
  name: string;
  /** The FidelityFX version OptiScaler's overlay prints in its FFX box. */
  version: string | null;
  fsr4_capable: boolean;
}

/** Which FSR 4 upscaler build is in the game folder.
 *
 * `known` is false for a file that hashes to nothing this plugin pins: it is
 * still reported — with its hash — rather than being passed off as the one the
 * release carries. Which build it is cannot be read from a version number,
 * because the retired community 4.1.1b reports exactly what the released SDK reports. */
export interface Fsr4Build {
  known: boolean;
  id: string | null;
  label: string;
  note: string;
  /** "int8" (the override) or "upgrade" (the FSR 3 → FSR 4 path). */
  reaches_fsr4_by: string | null;
  sha256: string | null;
  bytes: number;
}

export interface Fsr4Status {
  files: Record<string, boolean>;
  ready: boolean;
  required: string[];
  ffx?: FfxUpscalerInfo;
  build?: Fsr4Build | null;
}

export interface Fsr4Source {
  path: string;
  files: string[];
}

/** The Steam launch options a game had before OptiScaler was installed. */
export interface LaunchRecord {
  /** False when nothing was kept — an older install, or Steam would not say. */
  recorded: boolean;
  /** Recorded and empty means the game genuinely had none. */
  value: string;
}

export interface InstallInfo {
  path: string;
  installed: boolean;
  filename: string | null;
  managed: boolean;
  /**
   * OptiScaler here was not installed by this plugin: by upstream's Decky
   * OptiScaler (named in `owner`), another tool or by hand (`owner` null). This
   * plugin does not share games with them and leaves such folders alone.
   */
  foreign?: boolean;
  owner?: string | null;
  version: string | null;
  /** The OptiScaler this plugin build ships. */
  bundled_version?: string;
  /** This game holds another OptiScaler than the bundled one: reapply it. */
  outdated?: boolean;
  /**
   * Installed before OptiScaler 10's OptiScaler/ folder: its DLLs sit in the
   * game folder, over the game's own. Reapplying moves them.
   */
  flat_layout?: boolean;
  installed_at: number | null;
  ini_present: boolean;
  ini_path: string;
  log_present: boolean;
  log_path: string;
  candidates: string[];
  extra_proxies: string[];
  backup_dir: string;
  backed_up: string[];
  launch_record: LaunchRecord;
  fsr4: Fsr4Status;
  /**
   * Whether REFramework's dll is next to the executable, and whether this
   * plugin is the one that put it there. Read from the folder rather than the
   * manifest: the step is "is REF present", not "did we install REF", and
   * somebody's own working copy counts.
   */
  reframework: { installed: boolean; revision: string | null; managed: boolean };
  /** The OptiScaler this game had before its last update, to go back to. */
  previous?: PreviousInstall | null;
}

export interface PreviousInstall {
  version: string | null;
  /** The version the update installed: the restore point belongs to it. */
  replaced_by: string | null;
  taken_at: number;
  /** Only the files the update changed are kept. */
  files: string[];
}

export interface IniInfo {
  present: boolean;
  legacy: boolean;
  keys: number;
}

export interface GameDetail {
  path: string;
  name: string;
  ini_info: IniInfo;
  wiki_entry: string | null;
  fsr4_sources: Fsr4Source[];
  gpu: GpuInfo;
  target: string;
  target_is_saved: boolean;
  /** The folder the game was seen rendering from while it ran (null: never seen). */
  seen_exe_dir: string | null;
  /** Whether that was seen just now, with the game running. */
  seen_running: boolean;
  /** Installed beside another executable than the one seen rendering, and not loaded. */
  installed_elsewhere: boolean;
  candidates: ExeCandidate[];
  install: InstallInfo;
  launch_option: string;
  reframework: ReframeworkStatus;
  writable: boolean;
  /** Frames XeFG can generate per real one on this GPU, from the last session's log (null: not known). */
  xefg_max: number | null;
}

export interface VerifyResult {
  ok: boolean;
  error?: string;
  path: string;
  files: {
    name: string;
    present: boolean;
    matches_payload: boolean;
    size: number;
    expected_size: number;
  }[];
  problems: string[];
  complete: boolean;
  ffx_upscaler: { present: boolean; version: string | null; fsr4_capable: boolean };
}

/** Why the compatibility list is or is not answering. */
export interface WikiStatus {
  url: string;
  entry_count: number;
  available: boolean;
  /** "cache" (last good fetch), "bundled" (shipped with the plugin), or "none". */
  source: string | null;
  fetched_at: number | null;
  /** Seconds since it was fetched, or null when there is no list at all. */
  age: number | null;
  stale: boolean;
  /** Content fingerprint; changes when a background refresh brought something new. */
  revision: string;
  /** True while a refresh is running behind the answer already given. */
  revalidating: boolean;
  last_attempt: number | null;
  /** The failure verbatim — the sentence that says which problem this is. */
  error: string | null;
  /** Which CA source the last successful fetch used. */
  tls: string | null;
  cache_path: string;
}

export interface WikiSearchResult {
  name: string;
  page: string | null;
  compatibility: string;
  inputs: string;
  score: number;
}

export interface Recommendation {
  matched: boolean;
  searched: string[];
  entry_count: number;
  list_available: boolean;
  near_misses: { name: string; page: string | null; score: number }[];
  manual?: boolean;
  game: string | null;
  /** The wiki detail page behind this answer, when there is one. */
  page?: string | null;
  /**
   * True when this game has a wiki page that is not cached yet. The answer is
   * the compatibility-list row; a fuller one is being fetched behind it.
   */
  detail_pending?: boolean;
  filename: string;
  filename_source: string;
  alternatives: string[];
  compatibility: string | null;
  inputs: string | null;
  notes: string | null;
  optipatcher: boolean;
  wiki_url: string | null;
  detail: Record<string, string>;
  match_score: number | null;
  list_meta: {
    source: string;
    fetched_at: number | null;
    error: string | null;
    /** Whether a background refresh is worth waiting a moment for. */
    stale?: boolean;
    /** Content fingerprint: when this changes, the list behind the answer did. */
    revision?: string;
  };
}

/** One INI key the wiki asked for, with where in the entry it was stated. */
export interface PlannedSetting {
  section: string;
  key: string;
  value: string;
  label: string;
  source: string;
}

/**
 * REFramework, for the RE Engine games that cannot host OptiScaler without it.
 *
 * `automatic` is the whole question: when it is true this plugin can fetch the
 * right build itself, and when it is false `reason` says why not and `page` is
 * where to get it by hand. `plugin` is the one file that is never automatic —
 * PureDark's UpscalerBasePlugin lives behind a Nexus Mods login — so the pd
 * entries name it and link it instead of pretending it has been handled.
 */
export interface PlannedReframework {
  required: boolean;
  /** "nightly" (praydog's unified build) or "pd-upscaler" (per game). */
  variant: string;
  dll: string;
  /** The WINEDLLOVERRIDES stem REFramework needs from Proton. */
  override: string;
  source: string;
  detail: string;
  asset: string | null;
  url: string | null;
  page: string;
  plugin: string | null;
  plugin_name: string | null;
  plugin_url: string | null;
  automatic: boolean;
  reason: string | null;
}

/** What is actually next to the executable, against what the entry asked for. */
export interface ReframeworkStatus {
  required: boolean;
  installed: boolean;
  dll: string;
  path: string;
  revision: string | null;
  plugin: string | null;
  plugin_installed: boolean;
  plugin_url: string | null;
  plugin_name: string | null;
  /**
   * Whether everything this game needs is actually present. REFramework in and
   * OptiScaler in is not the same as "this game will do anything": the
   * pd-upscaler games do not upscale at all until PDPerfPlugin.dll is there,
   * and without this the panel reports a live, connected, entirely inert
   * install as working.
   */
  complete: boolean;
}

/** The frame-generation pairing a wiki entry recommends. */
export interface PlannedFrameGen {
  input: string;
  output: string;
  input_label: string;
  output_label: string;
  source: string;
  detail: string;
}

/**
 * Everything the wiki says about setting one game up, as instructions.
 *
 * `available` is false when no compatibility entry matched, which is what the
 * whole automatic path is gated on: with no entry there is nothing to be
 * automatic about, and the UI offers manual setup alone.
 */
export interface AutoPlan {
  available: boolean;
  /** Whether this game's settings are currently driven by the wiki. */
  enabled?: boolean;
  game: string | null;
  /** "wiki entry" (a page written for this game) or "compatibility list". */
  source: string | null;
  wiki_url: string | null;
  filename: string;
  filename_source: string;
  optipatcher: boolean;
  /** Game arguments to append after %command%, e.g. -dx12. */
  launch_flags: string[];
  /** The complete Steam launch options string, override and arguments. */
  launch_options: string;
  settings: PlannedSetting[];
  framegen: PlannedFrameGen | null;
  /**
   * The overlay shortcut this plan changes, when an entry asks for one. Several
   * REFramework games do, because REF's overlay uses Insert too — and applying
   * that without saying so is how "Insert does nothing" gets reported as the
   * overlay being broken, when it had simply moved.
   */
  hotkey: { value: string; name: string | null; source: string } | null;
  /** Set only for the games whose entry says OptiScaler needs REFramework. */
  reframework: PlannedReframework | null;
  /** Things the wiki named that could not be turned into a setting. */
  unresolved: { text: string; source: string }[];
  warnings: string[];
}

export interface AutoPlanResult {
  recommendation: Recommendation;
  plan: AutoPlan;
}

export type ConfigValues = Record<string, Record<string, string>>;

export interface ConfigResult {
  fsr4_build?: string | null;
  ok: boolean;
  error?: string;
  path?: string;
  values: ConfigValues;
  modified?: number;
}

/** Result of writing config keys, including what the running game adopted. */
export interface WriteConfigResult {
  ok: boolean;
  error?: string;
  applied?: OptionChange[];
  rejected?: OptionChange[];
  written_at?: number;
  live?: LiveApplyResult;
}

export interface LogEntry {
  time: string | null;
  level: string;
  message: string;
}

export interface MonitorReport {
  path: string;
  log_path: string;
  log_present: boolean;
  log_size: number;
  log_modified: number | null;
  logging_enabled: string | null;
  state: Record<string, string | null>;
  counts: Record<string, number>;
  frame_generation: string[];
  problems: LogEntry[];
  recent: LogEntry[];
  hints: string[];
  configured: Record<string, string>;
  total_lines?: number;
  error?: string;
}

export interface OptionChange {
  section: string;
  key: string;
  value: string;
}

export interface RunningGame {
  appid: number;
  name: string;
  gameid: string;
}

/** Whether OptiPatcher is bundled here and installed for one game. */
export interface OptipatcherStatus {
  available: boolean;
  installed: boolean;
  version: string;
}

/** What the in-game live-control plugin reports about itself. */
export interface LiveStatus {
  asi_installed: boolean;
  asi_available: boolean;
  /** OptiScaler's FPS overlay type names, in its order (menu_common.cpp fpsType[]). */
  fps_overlay_types?: string[];
  /** The type OptiScaler shows for "auto" (Config.h's default). */
  fps_overlay_default?: number;
  /**
   * Whether the game's copy of the in-game plugin is the one this build ships.
   * Updating the Decky plugin leaves every game on the ASI it was set up with,
   * and an old one attaches and answers normally — it just has nothing to say
   * about anything added since.
   */
  asi_current: boolean | null;
  /** Whether OptiScaler.ini switches ASI loading on at all (defaults to off). */
  load_enabled: boolean | null;
  /** Whether OptiScaler's own log says it loaded the plugin. null = unknown. */
  loaded_by_optiscaler: boolean | null;
  attached: boolean;
  /** Attached *and* finished finding what it needs. */
  ready?: boolean;
  /** The running OptiScaler can re-create its upscaler (plugin API 2+): the FSR version is live. */
  can_reinit?: boolean;
  /** OptiScaler's plugin API version, 0 when it never called OptiPluginInit. */
  api?: number | null;
  state: string;
  error: string | null;
  seq: string | null;
  age: number | null;
  live_keys: string[];
  /**
   * Whether the FSR version can be changed without a restart. It needs no flags
   * of its own — the same rebuild that switches upscaler re-reads it — so this
   * is really "there is a list to pick from and an upscaler to rebuild".
   */
  can_change_ffx_upscaler?: boolean;
  /** Which status-file format the installed plugin speaks; older ones say less. */
  schema?: number | null;
  /** Where the in-game plugin decided to put its control files. */
  dir?: string | null;
  /** Whether that is the folder being managed. false means it is writing where nobody reads. */
  dir_matches?: boolean | null;
}

/** Outcome of pushing a config write into the running game. */
export interface LiveKeyAction {
  ini_key: string;
  shortcut: string;
}

export interface LiveApplyResult {
  ok?: boolean;
  sent: boolean;
  attached?: boolean;
  reason?: string;
  applied?: string[];
  deferred?: string[];
  /** The upscaler is re-created on the next frame to pick the change up. */
  reinit?: boolean;
  /** The write left every value as it was: nothing to apply or restart for. */
  unchanged?: boolean;
  /** Settings turned on by OptiScaler's own shortcut (FG on): press_live_keys. */
  keys?: LiveKeyAction[];
  error?: string;
}
