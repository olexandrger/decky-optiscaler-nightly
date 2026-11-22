# Upstream review

What this fork has taken from upstream
([danielcamilo1/decky-optiscaler](https://github.com/danielcamilo1/decky-optiscaler),
`main`) and what is still to look at. The fork ports fixes and ideas one
commit at a time and never rebases (AGENTS.md, "Taking from upstream").
`scripts/upstream_review.py` reads and updates this file; keep its two
machine-read parts as they are: the `reviewed:` line and the `- [ ]` / `- [x]`
items.

reviewed: 6a243593df7986981ec714598d91540952c852e2

Every upstream commit up to and including `reviewed` has been read, and each
thing in it worth a port is in the queue below. The next review starts after it
(`python3 scripts/upstream_review.py new`).

## Queue

Open items, oldest first. One port a day at most (the daily task, AGENTS.md
"Automatic fixes"). Each line: id, upstream commit, what upstream found or did.
An item is checked against the fork's code before anything is written: many
were found in code the fork has since changed. One that needs a decision says
so, and once reported says "Reported <date>, awaiting a decision"; the task
skips it until the line changes. A port ticks its item in the port's own
commit as "Ported in this commit" (`git log -S "<id> ("` finds it).

- [ ] CD-02 (886d5f0): a reinstall rebuilds the manifest from this install only, so OptiPatcher and earlier imported files are no longer owned and uninstall leaves them.
- [ ] CD-04 (886d5f0): installing the live-control ASI writes over the file in place (write_bytes) even when a running game has it mapped.
- [ ] UI-07 (886d5f0): the full-screen Now Playing page drops write errors and the restart-needed notice the Quick Access panel shows.
- [ ] UI-09 (886d5f0): Now Playing does not pass the detected FidelityFX file version, so the FSR version names differ from the Quick Access panel's.
- [ ] UI-06 (886d5f0): adding live control with the game stopped does not refresh the status afterwards, and a failure is not shown.
- [ ] UI-01 (886d5f0): after automatic settings are written, an older local write record still overrides the new values on screen.
- [ ] UI-03 (886d5f0): the automatic toggle has no in-progress state; turned off while applying, it comes back on.
- [ ] UI-05 (886d5f0): re-picking the wiki entry by hand leaves the setup page on the old plan.
- [ ] UI-08 (886d5f0): a launch-option toggle snaps back because a stale read overwrites the optimistic state.
- [ ] UI-10 (886d5f0): a failed log read leaves the Logs tab on "Reading OptiScaler log" for good, or its buttons disabled.
- [ ] UI-11 (886d5f0): the game page's "Restart now" is a bare button the D-pad cannot reach.
- [ ] PD-06 (886d5f0): automatic mode refreshes the wiki plan but does not write the new settings to the installed game.
- [ ] TS-04 (886d5f0): the automatic-install orchestration has no integration test tying "done" to the written config.
- [ ] TS-01b (886d5f0): all-options.tsx computes ungrouped sections, empty enums and inverted ranges but exits on React errors only.
- [ ] IDEA running-exe-folder (886d5f0): install next to the executable the game actually runs (the process OptiScaler's log names, or the running game's), not the scored guess that favours Unreal's Binaries/Win64; the general form of upstream's Phantom Blade Zero folder rule. A feature: needs a decision. Reported 2026-11-18, awaiting a decision.

## Done

- [x] CD-03 (886d5f0): live commands lost between two pushes. Ported in c8515e3.
- [x] CD-07 (886d5f0): IPv4 fallback patching the resolver for every thread. Ported in 1f6c3c1.
- [x] PD-07 (886d5f0): removing our override deleted other DLLs in the same WINEDLLOVERRIDES. Ported in 6807916.
- [x] IDEA restore-point (886d5f0): keep the previous OptiScaler on update, with "Go back to OptiScaler <version>" on the game's page. Ported in this commit, the fork's way: on top of the install journal, one step back, only the files the update changed, ini untouched (not upstream's restorepoint.py).
- [x] CD-01 (886d5f0): half installs that cannot be undone. Already covered: transactional installs (installer `_Journal`).
- [x] TS-01 (886d5f0): run.tsx printing false without failing. Already covered: CI fails on a line ending ": false".
- [x] UI-02 (886d5f0): another game opened while the page shows one kept the previous game's detail and writable controls until the new folder was read, and an older app-id lookup answering last replaced the newer game. Ported in this commit: the game page is keyed by folder, and only the newest open request picks the game.
- [x] PD-02 (886d5f0): setup and "Set launch options for me" wrote Steam's launch options as only this plugin's override and the wiki's flags, dropping the player's own wrappers and arguments. Ported in this commit: `mergeOverride` adds our `n,b` clauses to the assignment the game gets (or in front) and appends missing flags; switching the checklist's launch options off takes out only ours.
- [x] CD-05 (886d5f0): the payload cache counted as extracted when OptiScaler.dll existed, so an extraction cut short was trusted and installs skipped the support files it lacked. Ported in this commit: the extracted folder is trusted only with a stamp written after a finished extraction (naming the archive hash) and every payload file and dir present; otherwise it is unpacked again, and an extraction that comes out short is an error.
- [x] CD-08 (886d5f0): FSR 4 file import, REFramework add/remove, OptiPatcher and live-control installs wrote the game folder outside the lock install, uninstall and settings writes hold. Ported in this commit: each takes the service's mutation lock for its folder writes (downloads stay outside), the foreign check runs inside it, and LoadAsiPlugins is written under the same lock as the ASI.
- [x] PD-01 (886d5f0): an existing OptiScaler.ini is not backed up; uninstall deletes it with the install's files. Ported in this commit: an ini the manifest does not list as ours gets a copy in the backup folder at install (kept in place, or replaced when the ini is not preserved), uninstall puts it back, and an uninstall that keeps the ini keeps the current one and drops the copy.

## Not taken

- 886d5f0: the OptiScaler 0.9.5 bundle and its live control calibrated against the 0.9.5 PDB (reads OptiScaler's internals; AGENTS.md "Live editing").
- 886d5f0, 2369d02: the community FSR 4.1.1b / 4.1.2 Lite Exact downloads (the fork uses the bundled upscaler's Lite / RDNA2 models).
- 886d5f0, 2369d02: archives committed to `bin/`.
- 405deac, 6a24359: upstream's own repository metadata, docs and release notes.
- IDEA game-profiles (886d5f0): upstream's hand-written per-game table (Phantom Blade Zero's folder, notes and 0.9.5 requirement). A table per game goes stale; the fork shows the wiki's own notes, ships 10.x, and the folder problem is queued in its general form (running-exe-folder). Decided 2026-11-18.
- CD-09, CD-10 (886d5f0): security findings whose details upstream keeps private; nothing to port from.
