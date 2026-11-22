# AGENTS.md

Instructions for AI agents (and humans) working in this repository. Read this
before changing anything.

## What this repository is

An **experimental personal fork** of
[danielcamilo1/decky-optiscaler](https://github.com/danielcamilo1/decky-optiscaler),
used to tinker with **OptiScaler nightly builds** from
[optiscaler/OptiScaler-nightly](https://github.com/optiscaler/OptiScaler-nightly)
on a Steam Deck. It is **not intended for real use** and has no users to keep
compatible with. Nothing from it is reported upstream, either to the plugin
author or to the OptiScaler project.

What the fork adds on top of upstream:

- the bundled OptiScaler is a nightly (10.x), not the pinned 0.9.4 release, and
  `scripts/bundle_nightly.py` swaps in any nightly;
- the plugin speaks OptiScaler 10's INI (`ffx` backends, `Fsr4GpuSupport`,
  `Fsr4Model`);
- Steam Deck FSR 4 presets use models inside the bundled upscaler (**FSR 4
  Lite**, **FSR 4 RDNA2**); the community 4.1.1b download is retired;
- a Quick Access **OptiScaler menu** button opens OptiScaler's own overlay;
- live editing from the panel through a new ASI on OptiScaler 10's plugin API,
  built and tested against each bundled nightly (see below);
- the panel offers **Update OptiScaler in this game** when a game holds another
  OptiScaler than the bundled one;
- fixes and ideas ported from upstream one commit at a time (never rebased),
  tracked in `docs/upstream-review.md`;
- `.github/workflows/nightly.yml` builds, tests and releases, started daily by a
  Claude scheduled task that also fixes what a new nightly breaks; the 10 newest
  releases are kept.

## Live editing (keep it on the plugin API)

Some panel edits apply in the running game; the rest when it restarts. The
live ones go through `asi/live.cpp`, an ASI OptiScaler loads from
`OptiScaler/plugins/` in the game (`[Plugins] LoadAsiPlugins=true`, which the
installer sets).

- OptiScaler 10 calls `OptiPluginInit` in every ASI and hands it its plugin API
  (`asi/plugin_sdk/optiscaler_plugin.h`, append-only, version-checked with
  `OPTI_API_HAS`). The ASI finds OptiScaler's module through it and asks for
  upscaler re-creation with `ReInitUpscalers` (API 2+).
- The API does not expose OptiScaler's settings, so the ASI writes `Config`
  directly, at offsets from `asi/generated/config_mirror.h`, which
  `scripts/generate_asi_layout.py` generates from `asi/optiscaler_ref/Config.h`.
  That header must be the bundled dll's own: the dll names its commit in its
  startup banner (`OptiScaler v10.0.0-dev (1e568bb3) (...)`) and
  `scripts/sync_optiscaler_headers.py` fetches `Config.h`/`Config.cpp` (and the
  enum headers, below) at that commit. `bundle_nightly.py` runs it, so bundling a nightly keeps them in step.
- The ASI validates `Config` (file name, path, defaults) before writing anything
  and otherwise reports `failed` and stays inert.
- Live keys are `optiscaler.live.LIVE_FIELDS`: settings OptiScaler re-reads per
  frame, plus what OptiScaler's own overlay changes by writing the value and
  rebuilding the upscaler on the same backend (`ReInitUpscaler` in
  `menu_common.cpp`): the FFX upscaler version, the FSR 4 model and the init
  flags, and the other backends' models (XeSS's network model, DLSS's render
  preset) (write, then `ReInitUpscalers`). `ReInitUpscalers` keeps the current
  backend, so it cannot switch to another one. "auto" for those is `unset`
  (the overlay's "Auto").
  `py_modules/optiscaler/live.py` sends them as `set`/`unset`/`reinit` commands through
  `decky_optiscaler_live.cmd`/`.status`; the panel claims an edit took effect
  only when the ASI is `ready` and the key was sent. A command is for the
  session it was sent to: the ASI ignores one written before the game started
  (the file outlives the game, and replaying it overrode the ini OptiScaler had
  just loaded), and `live._send` carries an unapplied command into the next only
  while it is seconds old (`PENDING_MAX_AGE_MS`).
- A Steam Deck FSR 4 preset (`set_fsr4_build`) goes live the way OptiScaler's
  menu switches the model ("FSR4 Model": write `Fsr4Model`, rebuild on the same
  backend; OptiScaler's support check reads it at every rebuild,
  `fsr4/FSR4Models.cpp`): the model and FSR version are pushed like any live
  key, and what else differs from the running game is reported as waiting for
  the next launch (`OptiScalerService._push_preset`). `Fsr4GpuSupport` (read
  once, when OptiScaler identifies the GPU) only waits when it would matter:
  with a model named it decides nothing but whether that model may run
  (`FSR4Models::CanGpuRun`: INT8 Lite always, the RDNA2 model whenever the
  support is not None), so Lite (support 4) and RDNA2 (3) switch live.
- Another upscaler backend (XeSS, DLSS, FSR 2.2) is never live: a rebuild keeps
  the backend the running feature has (`MenuCommon::ReInitUpscaler` takes
  `feature->GetUpscalerType()`, not `Config`), and only OptiScaler's menu starts
  another one, through `State`. The Quick Access restart button names the
  upscaler that waits and points at the OptiScaler menu.
- OptiScaler's own menu (`asi/optiscaler_ref/menu_common.cpp`, synced at the
  dll's commit) is the reference for each live key: the panel does what the
  menu does when it changes that setting, or the key is not live.
  `scripts/selftest.py` (`check_live_fields_match_optiscalers_menu`) finds every
  place the menu writes each `LIVE_FIELDS` member and fails if the menu also
  does something the plugin cannot (a State flag, a backend switch). Its
  `benign` list holds only side effects shown harmless in OptiScaler's own
  consumer code, with the file named. Frame generation **on** is one the
  plugin cannot write: the menu pairs it with State's `fgChanged`, which
  (re)activates a generator fed by the game, so the Quick Access panel closes
  itself and presses OptiScaler's own FG key (`[Menu] FGShortcutKey`, the value
  the game holds) instead, only when the running game has FG off
  (`service.press_live_keys`). Off is the write alone, as OptiScaler's key does
  it. `[OptiFG] HUDFix` is not live for the same reason. The FPS overlay
  button writes `ShowFps`/`FpsOverlayType`, exactly what OptiScaler's FPS and
  FPS-cycle keys write, through OptiScaler's own type names (`fpsType[]`).
- Not live, on purpose: switching to another upscaler backend or FFX frame
  generator. Those need OptiScaler's `State` internals. The panel records them
  for the next launch; OptiScaler's own overlay (the **OptiScaler menu** button)
  switches them live.
- The other direction is read-only: the ASI writes every Config field as
  OptiScaler loaded it (`decky_optiscaler_live.loaded`) and as it is now
  (`.values`, rewritten when one moves). `py_modules/optiscaler/running.py`
  turns them back into ini values, so the panel shows a running game's real
  settings, including what was changed in OptiScaler's menu and not saved
  (marked, with **Keep them in OptiScaler.ini**). Backend and FG switching
  stay in OptiScaler's menu; the panel shows the outcome. Enum tokens ("ffx",
  "XeFG") come from `OptiTypes.h/.cpp` and `State.h`, synced at the dll's
  commit and parsed as text for their enum tables only.

**Do not** read or write OptiScaler's `State` in memory, or anything else not
reachable through the plugin API or the generated `Config` layout (`State.h`
is synced only for its enum tables, as text). Upstream's 0.9.x ASI
did (hash-table internals, flag runs found by memory pattern) and broke on the
first nightly that moved them. A new live setting goes into `LIVE_FIELDS` only
if OptiScaler genuinely re-reads it while running, or if `ReInitUpscalers`
makes it re-read; anything else would look applied and do nothing.

`scripts/test_live_asi.py` holds this, in the real bundled OptiScaler under Wine
(no game, no GPU): the installer's own install, the ASI loading and attaching,
every Config member with a literal default in `Config.h` read back at its
offset, INI sentinel values read back, the running-settings snapshot matching
the ini for every key as loaded (nothing marked as changed in-game) and
following every live change, every live setting applied through
`live.apply_changes` and read back, bad commands refused, and a deliberately
broken layout ending in `failed` with nothing written. CI runs it on every push
and before every release. What it cannot check is that a change shows in a
rendered frame: `OptiScaler.log` on the Deck is the source of truth for that.

## Exclusive with upstream's plugin (do not "fix" this either)

Decky OptiScaler Nightly and upstream's Decky OptiScaler can both be installed,
but **never manage the same game**, like Decky Framegen and Decky OptiScaler.
Moving a game from one to the other means removing OptiScaler with the first.

- Everything this fork writes has its own name: plugin name, manifest
  `.decky-optiscaler-nightly.json` with id `decky-optiscaler-nightly`, backup
  folder, launch-options record, install staging folder, frontend route,
  storage keys and library context-menu entry (`constants.py`, `navigation.ts`,
  `GameDetail.tsx`, `libraryContextMenu.tsx`).
- `installer.detect` marks OptiScaler it did not install as `foreign` (upstream's
  manifest names the owner; a proxy with no manifest of ours is "another tool or
  by hand"). Install, uninstall and every service call that writes to a game
  folder refuse such folders (`require_not_foreign`, `OptiScalerService._foreign`),
  and the UI shows the owner instead of controls.
- No adoption, no migration of upstream's settings or installs. Do not add any.
  `scripts/test_install.py` (ExclusiveWithOtherInstallsTests) holds this.

## Taking from upstream (port, never rebase)

The fork **no longer rebases on or merges upstream** `main`. Upstream moved to
0.9.5 with a live control that reads OptiScaler's internals through a
PDB-calibrated layout, and rewrote most of the files the fork changes; replaying
the fork on that would resolve nearly every conflict to "keep the fork's side".
Upstream is a source of ideas and fixes. Each useful one is **ported as its own
fork commit**, written against the fork's code and under the rules of this file:

- Port the fix or the idea, not the diff. Check the problem exists here first
  (reproduce it, or read the fork's code path), then fix it the fork's way and
  add a test that fails without the fix.
- The commit message starts `Port: ` and names the upstream commit, plus the
  finding id when there is one (`upstream 886d5f0, audit CD-03`).
- Never take: anything reading or writing OptiScaler's `State` or internals
  outside the plugin API and the generated `Config` layout (upstream's ASI, its
  PDB layouts, backend or FFX FG switching through State); committed archives in
  `bin/`; community FSR 4 downloads or the 4.1.1b preset (the fork's Steam Deck
  presets are FSR 4 Lite / RDNA2 from the bundled upscaler); upstream's pinned
  OptiScaler version, payload lists and reference INI; upstream's names (a file,
  folder, route or key a port adds gets a `-nightly` name, see "Exclusive with
  upstream's plugin").
- A port that writes INI keys uses keys that exist in OptiScaler 10 (the schema
  generated from the bundled nightly's INI is the reference) and the `ffx` /
  `ffx_12` backend ids.
- History is never rewritten for this: plain commits, plain `git push`.

**Where the review stands: `docs/upstream-review.md`.** Its `reviewed:` line is
the last upstream commit read; everything up to it has been looked at, and each
thing worth a port is in its queue (`- [ ]`), with what was done (`- [x]`,
naming the fork commit) and what was turned down, with the reason. So a review
never starts from the beginning again:

```bash
python3 scripts/upstream_review.py new      # fetches upstream; commits after the marker, oldest first
python3 scripts/upstream_review.py queue    # open items
python3 scripts/upstream_review.py mark SHA # after reading up to SHA and queueing what it holds
```

`mark` only moves forward along `upstream/main`. If upstream ever rewrites its
history so the marker is no longer on it, `new` stops and says so: review from
the merge base by hand, then set the marker again.

Small steps: a port is one queue item, one commit, its own test. The daily task
(below) reviews new upstream commits and ports at most one item a day.

`CLAUDE.md` only imports this file. Upstream's `.gitignore` excludes `CLAUDE.md`,
so it is tracked here with `git add -f`.

## Installing is transactional

`installer.install` routes every write through `_Journal`: whatever a path held
is moved into `.decky-optiscaler-nightly-install/` with an undo record first, a
failure rolls everything back, and an install killed half way is rolled back by
the next install or uninstall (`recover_interrupted`). Any new write in
`_install` must go through the journal too; `scripts/test_install.py` fails
every write in turn and will catch one that does not. Uninstall only removes
this plugin's own installs (its manifest lists them); anything else is foreign
and refused (see "Exclusive with upstream's plugin").

An update keeps one step back: when it replaces this plugin's own earlier
install, the files the journal set aside that the update actually changed
(never the ini, never a byte-identical DLL) move into
`.decky-optiscaler-nightly-previous/` at commit (`_keep_previous`). An update
that changed nothing keeps the restore point it had. `restore_previous` (the
game page's **Go back to OptiScaler <version>**, game closed) puts them back
through a journal of its own, removes what the update added, puts a game file
the update had uncovered aside again, and uses the restore point up; uninstall
removes it. `scripts/test_install.py` (PreviousInstallTests) fails every write
of the way back in turn.

Installs follow OptiScaler 10's own layout: the proxy and `OptiScaler.ini` in
the game folder, everything else in `OptiScaler/` (dllmain.cpp: MainDllPath is
`OptiScaler` next to its dll, the dll's folder when that is missing; ASIs load
from its `plugins/`). So a game's own `libxess.dll` or FidelityFX DLLs stay
untouched, and only the proxy displaces anything. `py_modules/optiscaler/layout.py`
is OptiScaler's rule, and every path to OptiScaler's files goes through it (the
upscaler the FSR 4 presets check, the in-game plugin, OptiPatcher). The
folders in `OptiScaler/` are the manifest's `containers`: created by the
install, removed only once empty (the player's own ASIs go in
`OptiScaler/plugins` too). An install from before that layout (manifest
without `layout`, DLLs and `D3D12_OptiScaler/`, `Licenses/` in the game folder,
the ASIs in `plugins/`) moves on update: its files are retired payload, the
game's own originals come back from the backup, this plugin's ASIs move, the
player's in `plugins/` stay (another ASI loader may read that folder; the
diagnostics list them). Going back across the move needs `OptiScaler/` empty
of anything this plugin did not put there, or OptiScaler would look in it.
`scripts/test_install.py` (OptiScalerFolderTests, `flat_install` rebuilds such
an install) fails every write of the move and of the way back.

## The OptiScaler archive is not committed

`bin/` holds no OptiScaler archive and no built ASI in git (`bin/.gitignore`);
about 70 MB per nightly would only grow history. Both workflows download the newest nightly and
bundle it before testing: `build.yml` on every push, so a nightly that breaks
the plugin fails CI, and `nightly.yml` before it packages a release. Locally,
bundle one before running the checks:

```bash
python3 scripts/bundle_nightly.py --latest   # or a downloaded .7z path (keep its dated name)
python3 scripts/generate_schema.py
bash asi/build.sh                            # zig, or x86_64-w64-mingw32-g++
```

`bundle_nightly.py` keeps OptiScaler 10's layout (`arrange`: its dll and ini at
the top, the DLLs in `OptiScaler/`, the licences moved in there, the setup
scripts, Streamline fetcher, "extract here" marker and empty folders left out),
repacks the archive into `bin/`, and rewrites every pinned value: version,
archive name and SHA-256, payload file and folder lists as paths in the game
folder (retiring what a previous bundle shipped, or shipped elsewhere), the bundled FidelityFX
upscaler hash, and the plugin version (`<base>-nightly.<date>`); and it syncs
`asi/optiscaler_ref/` (`Config.{h,cpp}`, `OptiTypes.{h,cpp}`, `State.h`) to the
commit the dll was built from. Do not
hand-edit those values.

The committed constants, reference INI, generated schema, OptiScaler headers and
ASI layout are a snapshot of the nightly the code was last written against. CI rewrites them in its working
tree and never commits them. Commit them yourself only when the fork's code
depends on a newer nightly's INI (new keys in the panel, say). `package.py`
refuses to build without exactly the pinned archive and the built ASI in `bin/`.

## Checks

Run all of these before pushing; CI runs the same set, against the newest
nightly. Bundle one and build the ASI first (see above); selftest fails without
the archive. `test_live_asi.py` needs Wine (`apt install wine`) and mingw-w64
(`apt install g++-mingw-w64-x86-64`).

```bash
npx tsc --noEmit
pnpm build
python3 scripts/test_fsr4.py
python3 scripts/test_install.py   # a failed or interrupted install leaves the folder as it was
python3 scripts/test_update.py    # the in-plugin updater
python3 scripts/test_bundle.py    # the nightly download retries GitHub's transient failures
python3 scripts/selftest.py
python3 scripts/test_live_asi.py  # live editing inside the bundled OptiScaler, under Wine
HARNESS=run.tsx node testharness/build-and-run.mjs          # "React errors/warnings: 0", no line ending ": false"
HARNESS=all-options.tsx node testharness/build-and-run.mjs  # "real React errors: 0"
HARNESS=live-fields.tsx node testharness/build-and-run.mjs  # exits non-zero on failure
HARNESS=backend.tsx node testharness/build-and-run.mjs      # the panel against the real backend; exits non-zero
python3 scripts/package.py
```

`backend.tsx` is the one to extend for anything the Quick Access panel shows
or changes. It runs the real `OptiScalerService` (`scripts/harness_backend.py`)
on a temporary game folder, with a simulated running game that loads the ini,
applies the panel's commands as the in-game plugin does, and changes values by
itself, the way OptiScaler's menu, its keys and its own runtime adjustments do.
Every case requires the control to show the pick after a refresh of the
running values, the ini to hold it, and the game to hold it if it is live.
Hand-written fakes (`live-fields.tsx`, `run.tsx`) only behave as their author
assumed the backend does; three panel bugs got past them because none served a
running game that differs from the ini. A new control gets cases there under
each such drift, and is checked to fail with the bug put back.

None of these run OptiScaler in a game (`test_live_asi.py` runs it in a stand-in
host). Whether it renders under Proton, which FSR 4 model it creates, whether a
live change shows on screen and whether the menu key reaches the game can only
be checked on a Deck: `OptiScaler.log` in the game folder is the source of truth.

## Automatic fixes (and the daily release)

GitHub has no schedule for this repo. A daily Claude scheduled task (07:23
Vilnius, after OptiScaler-nightly's 04:00 UTC build) is what releases:

1. It starts "Bundle OptiScaler nightly" (`nightly.yml`, newest nightly) through
   the API and waits for it. Released, or nothing new: done, silently.
2. If the run failed for a non-transient reason, it reproduces the failure
   against the nightly that run bundled (its log and summary name it), fixes
   the root cause under the rules of this file, runs every check above, and
   pushes the fix to `main` as a commit starting `autofix: `.
3. It starts `nightly.yml` again **pinned to the same nightly** (`nightly`
   input). That run re-runs every check on exactly the build the fix was made
   for and releases it; a newer nightly published meanwhile waits for the next
   day. If that run fails too, it reverts its commit and reports.
4. Only once the release is settled (released, nothing new, or the fix and its
   pinned run done; not after a failure it reported), the upstream step
   ("Taking from upstream"): `upstream_review.py new`; each new upstream
   commit is read and its port candidates queued, then `mark` moves the marker
   and that goes in as one commit `Upstream review: <old>..<new>`.
5. Then at most **one port** from the queue, oldest first. The item is checked
   against the fork's code first; one that does not apply is ticked with the
   reason and the next is checked (three checked a day at most). One that does
   is fixed the fork's way with a test that fails without it, every check
   passes, and it is pushed as `Port: ...` with the queue item ticked in the
   same commit. If `build.yml` fails on that push, the task reverts it and
   reports. An item marked as needing a decision (a feature, a UI change, or
   anything near "Live editing" or "Exclusive with upstream's plugin") is not
   ported: the task reports it once, with a recommendation, and leaves it. The
   port is released by the next day's run (`main` has commits since the last
   release).

A failure of `test_live_asi.py` is maintained like any other (usually
`generate_asi_layout.py` meeting a new kind of Config member or a new way
`Config.cpp` reads a key, which the generator then has to learn), under the
"Live editing" rules: never by reaching into `State`, never by weakening the
layout checks. A failure of the check against OptiScaler's own menu is not
fixed automatically: whether that setting stays live is a decision, so the
task reports it. It never force-pushes or touches tags and releases itself, makes at most
one autofix and one port a day, and stops with a report instead of pushing when an autofix
did not help or the fix needs a decision. A transient failure (GitHub 5xx, DNS,
apt mirrors) gets one retry of the same run; it only reports when that keeps
happening. It messages only when a human is needed.

## Releases

`nightly.yml` runs only when started: by the Claude task (above) or by hand
from the Actions tab. It bundles the newest nightly, or the one in its
`nightly` input (`nightly-YYYYMMDD`, `bundle_nightly.py --tag`), and publishes
when that nightly has no release yet (no `v*-nightly.<its date>*` tag), when
`main` has commits since the last release, or with "Release the current build".
A run with nothing to release downloads nothing; a failed run creates no tag.
A run that finds `main` moved on before it releases (a push mid-run) does not
release the outdated commit: it starts a fresh run on `main`, pinned to the same
nightly, and stops. GitHub
would refuse the tag anyway when the push changed a workflow file, since that
needs the `workflows` permission `GITHUB_TOKEN` cannot have.
After each publish, releases beyond the newest 10 (`KEEP_RELEASES`) are deleted
with their tags; upstream's own `v0.0.x` tags are not touched.
Each published build is its own full release (not a prerelease) tagged
`v<base>-nightly.<date>.<run number>` (the run number makes fork-only changes on
the same nightly a new version, which the updater needs), with the asset
`decky-optiscaler-nightly.zip`,
so the Deck installs from a stable URL:

```
https://github.com/olexandrger/decky-optiscaler-nightly/releases/latest/download/decky-optiscaler-nightly.zip
```

Installed plugins update themselves: `py_modules/optiscaler/update.py` reads
the latest release (cached six hours; Settings → **Check for updates** skips
the cache), and the **Update to …** button (Quick Access panel and Settings) hands
the zip and its SHA-256 (GitHub's asset digest, or the `.sha256` sidecar the
workflow uploads) to Decky's own installer (`utilities/install_plugin`), which
asks, verifies and replaces the plugin. Keep the asset name, the plugin name
in `plugin.json` ("Decky OptiScaler Nightly", which is also `PLUGIN_NAME` in
`UpdateBanner.tsx`; a test holds them together) and `UPDATE_REPO` in step. The
name differs from upstream's on purpose so the fork is a separate Decky plugin
(see "Exclusive with upstream's plugin"). Do not switch to a custom Decky
store: Decky has one store at a time, and that would cut every other plugin
off from official updates.

Release notes come from `scripts/release_notes.py`: what is inside (with a link
to the OptiScaler nightly's own release, which lists its changes), this fork's
commits since the previous `v*-nightly.*` tag, and the test counts. OptiScaler's
commit list is deliberately not repeated in the notes. Run the workflow by hand with
"Release the current build" to publish without waiting for a newer nightly.
