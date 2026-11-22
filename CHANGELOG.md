# Changelog

Newest first. Each release ships `Decky OptiScaler.zip` for Decky Loader's
*Install from URL* (Developer mode), and bundles **OptiScaler 0.9.4**
(the nightly fork bundles an OptiScaler nightly; see its entries).

## [Unreleased]

### Nightly fork

- OptiScaler goes into games as OptiScaler 10 lays itself out: OptiScaler.dll
  (as the proxy) and OptiScaler.ini in the game folder, every other file in an
  `OptiScaler/` folder next to them, the in-game plugin and OptiPatcher in
  `OptiScaler/plugins/`. A game's own `libxess.dll`, FidelityFX DLLs or
  `Licenses/` are no longer set aside and replaced: OptiScaler reads its own
  from `OptiScaler/`. Games set up before move on **Update OptiScaler in this
  game** (the panel says so): the game's own DLLs come back, and **Go back**
  returns to the old layout. ASIs of the player's in `plugins/` stay where they
  are, and the diagnostics say OptiScaler 10 no longer loads them from there, or
  when OptiScaler.ini points its DLLs (`[Libraries] OptiDllPath`) or ASIs
  (`[Plugins] Path`) elsewhere. The bundle no longer flattens the nightly; it
  leaves out the setup scripts, the Streamline fetcher and the "extract here"
  marker.
- OptiScaler's own sharpener is live from All settings: `[CAS] Enabled`,
  `[Sharpness] Shader` (RCAS, the depth-aware ones, BaBa and OptiSharpen, named
  as in OptiScaler's menu, now a list instead of a text field) and `[CAS]
  OptiSharpenModel`, which OptiScaler reads at every sharpening pass. Value
  lists written "a, b or c" in OptiScaler's ini (the shader, the BaBa and
  OptiSharpen debug views) are lists in All settings. The snapshot of
  OptiScaler's headers and ini moves to nightly 20261122 (ab1f3d49), which has
  OptiSharpen.
- Dynamic MFG from the panel, live: the frame multiplier lists each step once
  more as "Dynamic: up to ..." (on a Steam Deck the one "Dynamic: FG on/off"),
  OptiScaler's checkbox next to its Frame Gen slider. It writes `[XeFG]
  DynamicMFG` with the step, which XeFG reads every present; a fixed step ends
  it, also in the ini when only the game had it off.
- The frame multiplier offers only what XeFG can do on this GPU: XeFG reports
  its maximum in `OptiScaler.log` (a Steam Deck: 2X), OptiScaler's menu stops
  there and runs a higher count as that maximum, and so does the panel (Quick
  Access and the game page), showing such a count as the one in effect. Until
  XeFG has run in a game, every step is offered.
- Fixed: releases through `v0.0.7-nightly.20261122.38` installed the nightly's
  empty "!! EXTRACT ALL FILES TO GAME FOLDER !!" marker into games and treated
  `plugins/` as part of the payload, so uninstalling deleted ASIs added to
  `plugins/` since the install and put back an older copy. The next install or
  uninstall removes the marker and puts the player's own ASIs back.
- Live in-game control is back, rebuilt: a new ASI on OptiScaler 10's plugin API
  (`OptiPluginInit`, `ReInitUpscalers`) writes only Config, with the layout
  generated from the bundled nightly's own `Config.h` (fetched at the commit the
  dll names). Live: frame generation on/off, HUD fix, debug view, depth copy,
  XeFG multiplier, sharpness, the FPS overlay and the FSR version. Not live:
  switching the upscaler backend or the FFX frame generator.
- Fixed, found by the new real-backend harness: a live control the ini
  already agreed with (FG off after OptiScaler's End key, the overlay after
  Page Up, the multiplier, FSR version or XeSS model changed in OptiScaler's
  menu) never reached the game and flipped back; turning FG on rewrote the FG
  pair from an output OptiScaler had reset at runtime; the upscaler read as
  plain FFX when the game held another FSR 4 model than the ini.
- README rewritten for the fork: what it is and does differently, how to
  install, license and credits, with links to the original plugin. The
  original's own README sections are gone (they described 0.9.x and its
  plugin). Release notes now link the OptiScaler source commit next to the
  binaries (GPL-3.0).
- Steam Deck FSR 4 INT8 Lite ↔ INT8 RDNA2 switch live again: the panel waited
  for a restart because the two presets set different `Fsr4GpuSupport`, which
  OptiScaler only uses, with a model named, to decide whether that model may
  run (Lite always, RDNA2 whenever support is not None). A game started on
  FSR 3 now gets the FSR 4 model live as well; only the keys that really wait
  (another API's backend, GPU support that would matter) ask for a restart.
- Picking XeSS (or another backend) over a running game: the restart button
  now says the upscaler starts at the next launch and that OptiScaler's own
  menu switches it now. Live switching from the panel stays impossible: a
  rebuild keeps the running backend, and starting another needs OptiScaler's
  State.
- Release notes no longer claim live in-game control is unavailable.
- **Go back to OptiScaler <version>** (game page → Manual setup →
  Installation, game closed): an update now keeps the previous OptiScaler's
  files it changed, so a nightly that breaks one game can be undone there
  without downgrading the plugin. One step back, ini untouched, only changed
  files kept (most DLLs do not change between nightlies); uninstall removes it.
  The idea is upstream's (restore points); the implementation is the fork's,
  on the install journal.
- Fixed, from two Deck debug reports: the in-game plugin applied the last
  command of the previous session at every launch, overriding the ini
  OptiScaler had just loaded (an FSR 4 Lite pick came back as the earlier
  FSR 3; FG turned off at launch). Commands written before the game started
  are now ignored, and an unapplied one is carried into the next only while
  seconds old.
- Steam Deck FSR 4 Lite / RDNA2 apply live when the running game already has
  the preset's backend and GPU support (the model and FSR version, rebuilt as
  OptiScaler's menu does); otherwise at the next launch, as before.
- The debug report names the plugin build that wrote it.
- Ported from upstream's audit (upstream is no longer rebased on; fixes are
  ported, see AGENTS.md): two live edits inside one 100 ms poll no longer lose
  the first (an unapplied command is carried into the next, seqs strictly
  increase, concurrent pushes no longer share a temp file); overlapping wiki
  downloads no longer leave the resolver forced to IPv4 for the whole plugin;
  removing OptiScaler's override keeps other DLLs in the same
  `WINEDLLOVERRIDES`.
- Removed the frame-rate tiles, the "in game" backend line, the game-reported
  FSR/FFX FG version lists and the panel's upscaler switch: all read
  OptiScaler's State, so the backend always sent them empty and only harness
  fixtures ever filled them. Version lists come from the reference INI. A
  selftest now fails when the frontend's `LiveStatus`/`RunningConfig` types
  declare a field the backend does not send.
- `testharness/backend.tsx` + `scripts/harness_backend.py`: the panel against
  the real backend and a simulated running game, 43 cases, in CI.
- Condensed Quick Access panel (QuickControls): OptiScaler menu and overlay
  buttons on top, frame generation on/off and multiplier, the upscaler as a
  family plus the model under it, explanations behind ⓘ buttons, diagnostics
  and all settings as one row at the bottom. The FG method and FFX FG version
  moved to the game's page only. XeSS's network model and DLSS's render preset
  are live (OptiScaler's menu switches them with a rebuild on the same
  backend). Picking FSR 3 uses the runtime's first FSR 3 version.
- **FPS overlay** button in the Quick Access panel, cycling off and
  OptiScaler's overlay types in its own order and names (`FpsOverlayType` is
  live).
- Live settings checked against OptiScaler's own menu at the bundled commit
  (`menu_common.cpp` is synced too): frame generation **on** now presses
  OptiScaler's FG key (its menu also sets State's `fgChanged`, without which a
  game-fed generator stayed off); `[OptiFG] HUDFix` is restart-only for the
  same reason; the sharpness override rebuilds the upscaler (DLSS before 3
  needs it); XeFG's own debug view is live. The menu and FG keys pressed are
  the ones the game holds now, not only the ini's.
- The generator maps the overlay settings OptiScaler reads through
  `ReadOverlaySettings` (29 keys), and emits Config.h's defaults; the schema
  parser reads value lists that go on over several lines or miss an "=" (FPS
  overlay type and position, DLSS presets past G).
- **Running now** in the Quick Access panel and on the game's page: OptiScaler's
  settings as the running game holds them (the in-game plugin writes a
  read-only snapshot whenever one moves). What was changed in OptiScaler's own
  menu shows in the controls, marked as not saved, with **Keep them in
  OptiScaler.ini**. The generated ini-key map no longer maps `Dx12Upscaler`,
  `VulkanUpscaler`, `Fsr4GpuSupport`, `Downscaler` and two fakenvapi keys to the
  wrong member.
- Live: the FSR 4 model and the init flags, the way OptiScaler's overlay
  changes them (write, then rebuild the upscaler on the same backend); "auto"
  unsets them. The FSR version control is shown while the game runs (it was
  hidden), a preset that only moves the FSR version no longer asks for a
  restart, and a backend switch says it needs one or OptiScaler's own menu.
- Live-control diagnostics in the Quick Access panel and on the game's page,
  with **Save debug report** (a text file in `~/Downloads`). The ASI now names
  the OptiScaler commit it was built for (`built_for` in its status) and, when it
  gives up, how much it searched.
- `scripts/test_live_asi.py`: the ASI inside the real bundled OptiScaler under
  Wine, run by CI on every push and before every release.
- **Update OptiScaler in this game**, in the Quick Access panel and on the game's
  page, for a game that holds another OptiScaler than the bundled one.
- Releases are started by the daily Claude task instead of a GitHub schedule; a
  nightly that breaks the checks is fixed and then released pinned to that same
  nightly (`nightly.yml` input `nightly`, `bundle_nightly.py --tag`).

## [0.0.7-nightly.20261030] - 2026-10-30 (nightly fork)

Bundles the **OptiScaler 10.0.0-pre1 nightly** (`nightly-20261030`, master
`e7e3872d`) instead of 0.9.4. Unofficial build; see "Nightly fork" in the README.

### Changed

- Payload is OptiScaler 10's dependency set, flattened next to the proxy:
  `amd_fidelityfx_loader_{dx12,vk}`, the `_vk` upscaler and frame generation
  DLLs, and `D3D12_OptiScaler/`. OptiScaler 10 falls back to the game folder when
  there is no `OptiScaler/` subfolder, so install, verify, FSR 4 build swaps and
  uninstall keep working as before.
- Upgrading a game from a 0.9.x install removes what 10.0 no longer ships
  (`fakenvapi.dll/.ini` — now built into OptiScaler — `amd_fidelityfx_dx12.dll`,
  `amd_fidelityfx_vk.dll`, `D3D12_Optiscaler/`) and puts back anything of the
  game's those had displaced.
- Bundled FidelityFX upscaler is FSR 4.1.2, which carries AMD's INT8 RDNA 2 and
  INT8 Lite models.
- **Steam Deck presets: FSR 4 Lite and FSR 4 RDNA2** replace "FSR 4.1.1b — Steam
  Deck". Both keep the bundled upscaler and select a model (`Fsr4Model` 3 / 2,
  `Fsr4GpuSupport` 4 / 3); Lite is the one AMD assigns to the Deck's Van Gogh APU.
  Picking either puts the bundled DLL back over a community build.
- FSR presets write OptiScaler 10's keys: backend `ffx` / `ffx_12` (was `fsr31` /
  `fsr31_12`, still recognised when reading), `Fsr4GpuSupport=2` for the Steam
  Deck INT8 route (was `Fsr4ForceEnableInt8=true`). `Fsr4Update` no longer exists
  and is no longer written.
- Advanced settings are regenerated from 10.0's `OptiScaler.ini` (352 options,
  37 sections). The schema generator now reads 10.0's comment shapes: a note
  after the "Default (auto)" line, and upscalers that may also name a custom folder.
- `scripts/bundle_nightly.py` bundles any nightly (`--latest` downloads the
  newest) and rewrites every pinned value; `.github/workflows/nightly.yml`
  runs it daily and keeps the 10 newest releases.

### Fixed

- **A failed or interrupted install no longer damages the game folder.** Install
  is journaled: any error rolls every change back, and an install killed half way
  is rolled back by the next install or uninstall. Before, uninstalling after a
  failed install could delete game files sharing OptiScaler's names, and a retry
  could leave stray FidelityFX DLLs behind (inherited from upstream).
- Uninstall without a manifest removes only files identical to the bundled ones
  and reports the rest as left in place.
- Steam Deck presets work while the game runs when the bundled upscaler is
  already in place (settings only, applied at the next launch).
- Leaving a Steam Deck preset for another FSR preset clears its FSR 4 model.

### Changed

- The plugin is named **Decky OptiScaler Nightly** in Decky, a separate plugin
  from the original that can be installed beside it but never manages the same
  game: everything it writes has its own name (manifest, backups, launch-options
  record, route, storage, context-menu entry), and a game set up by the original,
  another tool or by hand is shown with its owner and left alone. Remove
  OptiScaler with the original before setting a game up here.

### Added

- **Update button.** The Quick Access panel checks this fork's latest release
  (at most every six hours; Settings → **Check for updates** checks at once)
  and offers **Update to …**, which goes through Decky's own installer (its
  confirmation, SHA-256 check and reload). Releases carry a `.sha256` sidecar.
- **OptiScaler menu** button in the Quick Access panel while a game runs. It
  closes the Quick Access menu and presses the game's `[Menu] ShortcutKey`
  (Insert by default) on a uinput virtual keyboard, opening or closing
  OptiScaler's own overlay, where changes apply immediately.
- With no live-control plugin, the panel says edits apply when the game restarts
  ("restart to apply") instead of "live control off", and no longer sets some
  controls apart under "Needs a restart" when all of them are.

### Removed

- **The community FSR 4.1.1b download.** Legacy now that 4.1.2 ships AMD's own
  RDNA 2 model. A folder that still has it is recognised as "4.1.1b (retired)",
  and reinstalling OptiScaler there puts the bundled upscaler back. The verified
  download machinery remains for any future registered build.
- **Live in-game control is unavailable in this build.** The ASI mirrors
  OptiScaler's `State`, which 10.0 rewrote (the upscaler switch is now an enum,
  the FG flags and FSR version lists moved). The ASI is not shipped, so the panel
  reports live control unavailable; settings apply on the next launch, and the
  OptiScaler overlay (Insert) still changes them in game.

## [0.0.7] - 2026-09-18

FSR 4 on the Steam Deck, and the compatibility list is whole again.

### Added

- **FSR 4.1.1b — Steam Deck**, in the Basic upscaler dropdown. One choice
  downloads the community RDNA 2 build of the FidelityFX upscaler, checks it
  against a hash pinned in the plugin, puts it next to the game and switches on
  the INT8 override that reaches FSR 4 on RDNA 2. Close the game first; it
  applies on the next launch. Frame generation is left exactly as it was.
  Thanks to [@drewboardman](https://github.com/drewboardman), who built this and
  tested it on a Deck.
- Setup names which upscaler build is in the game folder, identified by its bytes
  rather than its version — the community build reports the same 4.1.1 the
  released SDK does — and will put the bundled one back.

### Fixed

- **297 of the 695 games on the compatibility list were answering "no wiki entry
  matched".** One row on the wiki was saved without its leading `|`, which the
  parser read as the end of the table, so everything after Metro Exodus was
  invisible: Monster Hunter Wilds, the Resident Evil entries, Stalker 2, The
  Witcher 3 and 293 others. A row that lost its first character is now read as
  the row it is. Bundled list regenerated — 698 entries.
- Games listed under *Upscaler mods support* and *Luma Unreal Engine* no longer
  claim OptiPatcher support they do not have, and their notes are their notes
  rather than the images column. Those two tables have one column fewer than the
  main one, and were being read as though they did not.
- The refreshed list adds Onimusha: Way of the Sword to the games that need
  REFramework in place before OptiScaler can do anything.
- All six FSR 4 quality presets are offered. The reference INI writes them as one
  comma-separated line, of which two were being read, so Quality, Performance,
  DRS and Ultra Performance were rejected by the settings writer and never
  reached the file.
- Choosing an FSR version on RDNA 2 no longer turns on the FSR 4 upgrade path.
  That path forces the FP8 model, which such a GPU does not have, and OptiScaler
  answers with a silent FSR 3 fallback.
- Verification recognises a community upscaler by hash, and reinstalling keeps it
  instead of overwriting it with the bundled build.
- Replacing the upscaler is atomic and rolls back the DLL, the INI and the
  manifest together if any part of it fails. It refuses while the game is
  running, and on an install this plugin did not make.

[0.0.7]: https://github.com/danielcamilo1/decky-optiscaler/releases/tag/v0.0.7

## [0.0.6] - 2026-08-28

Everything from the `0.0.5.x-testing` prereleases, as one release.

### Added

- Non-Steam games can be set up. Their folder is taken from the shortcut's
  target, since Steam reports no install folder for them.
- Games whose wiki entry requires
  [REFramework](https://github.com/praydog/REFramework) say so on the setup
  checklist and link the right build. Installing it for you is disabled for
  now, until I can gather more information to get it fully working.

### Fixed

- A non-Steam game's launch options are read from `shortcuts.vdf`, so removing
  OptiScaler no longer wipes them.
- Settings are read only from the install method this plugin actually performs.
  Monster Hunter Wilds would not boot because of this.
- Settings hedged by an "if", a "try", an "e.g." or a "desired" are reported
  rather than applied. Resident Evil 2 was getting 2× output scaling it never
  asked for.
- Setup names the OptiScaler overlay's new shortcut key when an entry moves it
  off Insert.
- Wiki pages with brackets in the name download again — Resident Evil, Dead
  Space (2023) and Elden Ring were affected. Bundled list regenerated.

[0.0.6]: https://github.com/danielcamilo1/decky-optiscaler/releases/tag/v0.0.6

## [0.0.5.3-testing] - 2026-08-27

Prerelease on the `testing` branch.

### Added

- Non-Steam games can be set up. Their folder is taken from the shortcut's
  target, since Steam reports no install folder for them.

### Changed

- Automatic REFramework setup is disabled for now, until I can gather more
  information to get it fully working. The games that need it still say so and
  link the right build, and REFramework installed by 0.0.5.1/0.0.5.2-testing can
  still be removed.

### Fixed

- A non-Steam game's launch options are read from `shortcuts.vdf`, so removing
  OptiScaler no longer wipes them.

## [0.0.5.2-testing] - 2026-08-24

Prerelease.

### Fixed

- Monster Hunter Wilds would not boot: settings were being read from install
  methods other than the one performed.
- Resident Evil 2 got 2× output scaling it never asked for. Settings hedged by
  an "if", a "try", an "e.g." or a "desired" are reported, not applied.
- Setup names the OptiScaler overlay's new shortcut key when an entry moves it
  off Insert.

## [0.0.5.1-testing] - 2026-08-24

Prerelease. REFramework auto-install — **disabled in 0.0.5.3-testing**.

### Added

- The nine compatibility-list games that need REFramework are detected, and the
  build, the `dinput8` override and `PDPerfPlugin.dll` are stated on the
  checklist.

### Fixed

- Wiki pages with brackets in the name download again — Resident Evil, Dead
  Space (2023) and Elden Ring were affected. Bundled list regenerated.

## [0.0.5] - 2026-08-23

### Added

- Removing OptiScaler asks what to do with the Steam launch options: restore
  what was there, remove only the OptiScaler override, or change nothing. The
  answer can be remembered.
- A Settings tab on the main page: remembered answers, the switch that stops
  answers being kept, and the state of the compatibility list.
- A copy of the compatibility list ships with the plugin, so an offline Deck
  still matches games.

### Changed

- The compatibility list is read from cache and refreshed in the background,
  instead of a network timeout in front of every question.

### Fixed

- Launch options are read from three sources, not just
  `SteamClient.Apps.GetAppLaunchOptions`, which some client builds do not have.
- A wiki that will not download is no longer reported as "your game is not on
  the list", and there is a **Try again** button.
- A stalled connection is retried over IPv4.
- The last-resort unverified TLS context no longer reads the system trust store.

[0.0.5]: https://github.com/danielcamilo1/decky-optiscaler/releases/tag/v0.0.5

## [0.0.4] - 2026-08-21

### Added

- The exact FSR version the running game built, rather than just the backend id.
- A second dropdown for which FSR version to run. Applies without a restart.
- Both frame rates while frame generation is on: what the game renders and what
  reaches the screen.
- A live-control readout that says why a number is missing, and a warning when a
  game's in-game plugin is out of date.

### Fixed

- A dropdown could keep a name its option list no longer used.
- Asking for FSR 4 could silently give you FSR 3.
- A game on the SD card reported itself as "writing to a different folder".
- The newest FSR version was named after the reference INI's build, not the
  bundled one.

[0.0.4]: https://github.com/danielcamilo1/decky-optiscaler/releases/tag/v0.0.4

## [0.0.3] - 2026-08-20

### Fixed

- The upscaler dropdown lagged one change behind after reopening the panel.
- The FidelityFX FG version control disappeared on games reporting one
  generator.

[0.0.3]: https://github.com/danielcamilo1/decky-optiscaler/releases/tag/v0.0.3

## [0.0.2] - 2026-08-20

### Fixed

- A control could display a value it was not set to.
- Picking the third FidelityFX frame generator did nothing.
- Settings the config writer refused are reported instead of left on screen.
- Re-reading the config could undo an edit or blank the panel.
- The live upscaler switch works for DX11 games.

[0.0.2]: https://github.com/danielcamilo1/decky-optiscaler/releases/tag/v0.0.2

## [0.0.1] - 2026-08-20

First public release.

### Added

- Per-game install, into a folder scored automatically and overridable by hand.
  Steam libraries including the SD card, plus any folder as a custom library.
- Automatic setup from the OptiScaler
  [Compatibility List](https://github.com/optiscaler/OptiScaler/wiki/Compatibility-List),
  as a checklist that names the wiki field behind every line before writing it.
- Basic settings (four controls) and Advanced (all 288 options across 34
  sections), generated from the shipped `OptiScaler.ini` and preserving its
  comments.
- Live in-game control of frame generation, the FidelityFX FG version and the
  upscaler, with a frame rate readout, via a bundled ASI plugin.
- Quick Access panel: the running game's live controls and a filtered game list.
- Files an install would overwrite are backed up and restored on uninstall.
- `OptiScaler.log` parsing, bundled
  [OptiPatcher](https://github.com/optiscaler/OptiPatcher), and an **OptiScaler
  Settings** entry in the Steam library context menu.

[0.0.1]: https://github.com/danielcamilo1/decky-optiscaler/releases/tag/v0.0.1
