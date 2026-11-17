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
- `.github/workflows/nightly.yml` builds, tests and releases, started daily by a
  Claude scheduled task that also fixes what a new nightly breaks; the 10 newest
  releases are kept.

## Live editing (keep it on the plugin API)

Some panel edits apply in the running game; the rest when it restarts. The
live ones go through `asi/live.cpp`, an ASI OptiScaler loads from the game's
`plugins/` folder (`[Plugins] LoadAsiPlugins=true`, which the installer sets).

- OptiScaler 10 calls `OptiPluginInit` in every ASI and hands it its plugin API
  (`asi/plugin_sdk/optiscaler_plugin.h`, append-only, version-checked with
  `OPTI_API_HAS`). The ASI finds OptiScaler's module through it and asks for
  upscaler re-creation with `ReInitUpscalers` (API 2+).
- The API does not expose OptiScaler's settings, so the ASI writes `Config`
  directly, at offsets from `asi/generated/config_mirror.h`, which
  `scripts/generate_asi_layout.py` generates from `asi/optiscaler_ref/Config.h`.
  That header must be the bundled dll's own: the dll names its commit in its
  startup banner (`OptiScaler v10.0.0-dev (1e568bb3) (...)`) and
  `scripts/sync_optiscaler_headers.py` fetches `Config.h`/`Config.cpp` at that
  commit. `bundle_nightly.py` runs it, so bundling a nightly keeps them in step.
- The ASI validates `Config` (file name, path, defaults) before writing anything
  and otherwise reports `failed` and stays inert.
- Live keys are `optiscaler.live.LIVE_FIELDS`: settings OptiScaler re-reads per
  frame, plus what OptiScaler's own overlay changes by writing the value and
  rebuilding the upscaler on the same backend (`ReInitUpscaler` in
  `menu_common.cpp`): the FFX upscaler version, the FSR 4 model and the init
  flags (write, then `ReInitUpscalers`). `ReInitUpscalers` keeps the current
  backend, so it cannot switch to another one. "auto" for those is `unset`
  (the overlay's "Auto").
  `py_modules/optiscaler/live.py` sends them as `set`/`unset`/`reinit` commands through
  `decky_optiscaler_live.cmd`/`.status`; the panel claims an edit took effect
  only when the ASI is `ready` and the key was sent.
- Not live, on purpose: switching to another upscaler backend or FFX frame
  generator. Those need OptiScaler's `State` internals. The panel records them
  for the next launch; OptiScaler's own overlay (the **OptiScaler menu** button)
  switches them live.

**Do not** read or write OptiScaler's `State`, or anything else not reachable
through the plugin API or the generated `Config` layout. Upstream's 0.9.x ASI
did (hash-table internals, flag runs found by memory pattern) and broke on the
first nightly that moved them. A new live setting goes into `LIVE_FIELDS` only
if OptiScaler genuinely re-reads it while running, or if `ReInitUpscalers`
makes it re-read; anything else would look applied and do nothing.

`scripts/test_live_asi.py` holds this, in the real bundled OptiScaler under Wine
(no game, no GPU): the installer's own install, the ASI loading and attaching,
every Config member with a literal default in `Config.h` read back at its
offset, INI sentinel values read back, every live setting applied through
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

## Rebasing on upstream

The fork is kept as **a stack of commits on top of upstream `main`**. Sync by
rebasing, not merging, so the fork's changes stay a readable patch series.

```bash
git remote add upstream https://github.com/danielcamilo1/decky-optiscaler   # once
git fetch upstream
git rebase upstream/main          # replay the fork's commits on upstream
```

Resolving conflicts, by file kind:

| Files | Resolution |
|---|---|
| Generated: `src/config/generatedSchema.ts`, `py_modules/optiscaler/schema_generated.py`, `asi/generated/config_mirror.h`, `py_modules/optiscaler/live_fields.json` | Take either side, then regenerate: `python3 scripts/sync_optiscaler_headers.py`, `python3 scripts/generate_schema.py`. Never merge them by hand. |
| `bin/OptiScaler-*.7z` | Keep it deleted: the fork commits no OptiScaler archive (`bin/.gitignore`). An upstream bump shows as modify/delete; resolve with `git rm`. |
| `defaults/OptiScaler.reference.ini`, and the pinned values in `py_modules/optiscaler/constants.py` (`OPTISCALER_VERSION`, `PAYLOAD_*`, `FSR4_BUNDLED_*`, payload/retired lists) | Keep the fork's side, then re-run `scripts/bundle_nightly.py --latest` so they are consistent. Upstream will keep pinning 0.9.x; that is expected. |
| `asi/` (live-control ASI) | Keep the fork's `asi/live.cpp` (plugin API, Config only; see "Live editing"), `asi/plugin_sdk/` and `asi/optiscaler_ref/` (synced to the bundled nightly). Upstream's ASI targets 0.9.x's `State`: do not take its changes; port an upstream live feature only if it fits the plugin-API design. The built ASI is not committed. |
| Steam Deck FSR 4 (`basic.ts` presets, `installer.py`, `fsr4build.py`, `test_fsr4.py`) | Keep the fork's Lite/RDNA2 presets. Do not bring back the 4.1.1b preset or download. |
| Identity names (`MANIFEST_NAME`, `BACKUP_DIR`, `LAUNCH_RECORD_NAME`, `PLUGIN_ID`, `ROUTE`, storage keys, context-menu marker) | Keep the fork's `-nightly` names. A new file, folder, route or key upstream adds gets its own `-nightly` name too. |
| Upstream features unrelated to the above | Take them. If they write INI keys, check the keys exist in OptiScaler 10 (the schema generated from the nightly's INI is the reference) and use `ffx`/`ffx_12` backend ids. |

After the rebase, run the full checks (below), then push with
`git push --force-with-lease origin main`. Release tags keep pointing at their
original commits, so rewriting `main` does not break existing releases. The
first release after a rebase lists the whole replayed stack plus the upstream
commits pulled in under "Fork changes", because the previous tag is no longer an
ancestor of `main`; that is expected.

`CLAUDE.md` only imports this file. Upstream's `.gitignore` excludes `CLAUDE.md`,
so it is tracked here with `git add -f`; leave upstream's `.gitignore` alone.

## Installing is transactional

`installer.install` routes every write through `_Journal`: whatever a path held
is moved into `.decky-optiscaler-nightly-install/` with an undo record first, a
failure rolls everything back, and an install killed half way is rolled back by
the next install or uninstall (`recover_interrupted`). Any new write in
`_install` must go through the journal too; `scripts/test_install.py` fails
every write in turn and will catch one that does not. Uninstall only removes
this plugin's own installs (its manifest lists them); anything else is foreign
and refused (see "Exclusive with upstream's plugin").

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

`bundle_nightly.py` flattens OptiScaler 10's `OptiScaler/` dependency folder into
the game-folder layout this plugin installs (OptiScaler 10 falls back to the exe
folder when that subfolder is missing), repacks the archive into `bin/`, and
rewrites every pinned value: version, archive name and SHA-256, payload file and
dir lists (retiring files a previous bundle shipped), the bundled FidelityFX
upscaler hash, and the plugin version (`<base>-nightly.<date>`); and it syncs
`asi/optiscaler_ref/Config.{h,cpp}` to the commit the dll was built from. Do not
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
python3 scripts/package.py
```

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

A failure of `test_live_asi.py` is maintained like any other (usually
`generate_asi_layout.py` meeting a new kind of Config member), under the "Live
editing" rules: never by reaching into `State`, never by weakening the layout
checks. It never force-pushes or touches tags and releases itself, makes at most
one autofix a day, and stops with a report instead of pushing when an autofix
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
