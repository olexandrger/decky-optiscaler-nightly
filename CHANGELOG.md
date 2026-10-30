# Changelog

Newest first. Each release ships `Decky OptiScaler.zip` for Decky Loader's
*Install from URL* (Developer mode), and bundles **OptiScaler 0.9.4**
(the nightly fork bundles an OptiScaler nightly; see its entries).

## [Unreleased]

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
  runs it daily.

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

### Added

- **Update button.** The Quick Access panel checks this fork's latest release
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
