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
- `.github/workflows/nightly.yml` builds, tests and releases daily.

## How settings change in this fork (do not "fix" this)

**Edits made in the plugin's panel require a game restart. Live, in-game edits
are made in OptiScaler's own overlay.** This is the intended design, not a bug
or a missing feature.

- The panel writes `OptiScaler.ini`, which OptiScaler reads once at startup. The
  Quick Access panel says "Changes made here apply when the game restarts" and
  offers **Restart game to apply**.
- For live changes, the **OptiScaler menu** button closes the Quick Access menu
  and presses the game's `[Menu] ShortcutKey` (Insert by default) on a uinput
  virtual keyboard (`py_modules/optiscaler/keys.py`). OptiScaler's overlay runs
  inside the game, so its changes apply immediately.

**Do not try to restore the panel's live editing.** Specifically, do not:

- port or rebuild `asi/live.cpp` for OptiScaler 10, or put
  `bin/decky_optiscaler_live.asi` back;
- un-skip the ASI suites in `scripts/selftest.py` (they run only when the ASI is
  in `bin/`);
- add another IPC or memory-writing mechanism into the game process.

Why: upstream's live control is an ASI that writes into OptiScaler's internal
C++ `Config`/`State` objects at byte offsets mirrored from one exact OptiScaler
build. OptiScaler 10 rewrote `State`: `newBackend` became an `Upscaler` enum,
the frame-generation flag run and the FFX version lists moved. A nightly can
reshuffle these on any day, so a port would break silently between builds, and
it cannot be verified without a game running under Proton. The native overlay
is maintained by OptiScaler itself and always matches the build.
`asi/optiscaler_ref/SOURCE_COMMIT.txt` records the details.

If a change would make the panel claim an edit took effect in a running game,
it is wrong for this fork.

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
| Generated: `src/config/generatedSchema.ts`, `py_modules/optiscaler/schema_generated.py`, `asi/generated/config_mirror.h`, `py_modules/optiscaler/live_fields.json` | Take either side, then regenerate: `python3 scripts/generate_schema.py`, `python3 scripts/generate_asi_layout.py`. Never merge them by hand. |
| `bin/OptiScaler-*.7z`, `defaults/OptiScaler.reference.ini`, and the pinned values in `py_modules/optiscaler/constants.py` (`OPTISCALER_VERSION`, `PAYLOAD_*`, `FSR4_BUNDLED_*`, payload/retired lists) | Keep the fork's side, then re-run `scripts/bundle_nightly.py` (on the bundled `.7z` or `--latest`) so they are consistent. Upstream will keep pinning 0.9.x; that is expected. |
| `asi/` (live-control ASI) | Accept upstream's source changes, but keep the ASI out of `bin/` (see above). |
| Steam Deck FSR 4 (`basic.ts` presets, `installer.py`, `fsr4build.py`, `test_fsr4.py`) | Keep the fork's Lite/RDNA2 presets. Do not bring back the 4.1.1b preset or download. |
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
is moved into `.decky-optiscaler-install/` with an undo record first, a failure
rolls everything back, and an install killed half way is rolled back by the next
install or uninstall (`recover_interrupted`). Any new write in `_install` must go
through the journal too; `scripts/test_install.py` fails every write in turn and
will catch one that does not. Uninstall without a manifest only removes files
byte-identical to the bundled payload.

## Bundling a newer nightly

```bash
python3 scripts/bundle_nightly.py --latest   # or a downloaded .7z path
python3 scripts/generate_schema.py
```

`bundle_nightly.py` flattens OptiScaler 10's `OptiScaler/` dependency folder into
the game-folder layout this plugin installs (OptiScaler 10 falls back to the exe
folder when that subfolder is missing), repacks the archive, and rewrites every
pinned value: version, archive name and SHA-256, payload file and dir lists
(retiring files a previous bundle shipped), the bundled FidelityFX upscaler hash,
and the plugin version (`<base>-nightly.<date>`). Do not hand-edit those values.

The nightly workflow does this daily and **does not commit the archive back**
(about 70 MB per day would bloat history); the release zip carries it. Commit a
new archive by hand only when the fork's own code needs a newer OptiScaler.

## Checks

Run all of these before pushing; CI runs the same set.

```bash
npx tsc --noEmit
pnpm build
python3 scripts/test_fsr4.py
python3 scripts/test_install.py   # a failed or interrupted install leaves the folder as it was
python3 scripts/selftest.py
HARNESS=run.tsx node testharness/build-and-run.mjs          # "React errors/warnings: 0", no line ending ": false"
HARNESS=all-options.tsx node testharness/build-and-run.mjs  # "real React errors: 0"
HARNESS=live-fields.tsx node testharness/build-and-run.mjs  # exits non-zero on failure
python3 scripts/package.py
```

None of these run OptiScaler in a game. Whether it loads under Proton, which FSR
4 model it creates, and whether the menu key reaches the game can only be
checked on a Deck: `OptiScaler.log` in the game folder is the source of truth.

## Releases

`nightly.yml` publishes a full release (not a prerelease) tagged
`v<plugin version>-nightly.<date>` with the asset `decky-optiscaler-nightly.zip`,
so the Deck installs from a stable URL:

```
https://github.com/olexandrger/decky-optiscaler-nightly/releases/latest/download/decky-optiscaler-nightly.zip
```

Release notes come from `scripts/release_notes.py`. Builds are compared by tag:
fork commits since the previous `v*-nightly.*` tag, and OptiScaler commits between
the nightly that tag bundled and this one. Run the workflow by hand with
"Release the current build" to publish without waiting for a newer nightly.
