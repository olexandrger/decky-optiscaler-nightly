# Upstream review

What this fork takes from upstream
([danielcamilo1/decky-optiscaler](https://github.com/danielcamilo1/decky-optiscaler),
`main`). The fork ports fixes and ideas one commit each and never rebases
(AGENTS.md, "Taking from upstream"). `scripts/upstream_review.py` reads and
updates this file.

reviewed: 1a2b9f9a085a81c85a757a5cd2bf6327f9abe5bd

Every upstream commit up to and including `reviewed` has been read. A review
sorts each finding the day it is read: ported that day (one `Port:` commit
each), not applicable, or a decision for the owner. So this file holds only
what is still open, and the rules that keep things from being looked at twice:
one line each, at most 30 open items and 12 rules (`upstream_review.py check`,
run by selftest). Nothing done is kept here: a port deletes its item in its
own commit, whose message names the finding (`git log --grep "<id>"`), and the
review commit's message lists what was not applicable or not taken, and why.
The ports and reviews before this format are in the history
(`git log --grep "^Port:\|^Upstream review"`).

## Open

Oldest first. Each line: `- [ ] <id> (<upstream commit>): <what upstream found or did>`.
A decision for the owner says "A feature: needs a decision." (or why it needs
one) and, once reported, "Reported <date>, awaiting a decision."; a port that
failed says so with its date. Every other item is one the review could not
finish that day (the daily cap), and goes first the next day.

- [ ] UI-03 (886d5f0): the automatic toggle has no in-progress state; turned off while applying, it comes back on.
- [ ] UI-05 (886d5f0): re-picking the wiki entry by hand leaves the setup page on the old plan.
- [ ] UI-08 (886d5f0): a launch-option toggle snaps back because a stale read overwrites the optimistic state.
- [ ] UI-10 (886d5f0): a failed log read leaves the Logs tab on "Reading OptiScaler log" for good, or its buttons disabled.
- [ ] UI-11 (886d5f0): the game page's "Restart now" is a bare button the D-pad cannot reach.
- [ ] PD-06 (886d5f0): automatic mode refreshes the wiki plan but does not write the new settings to the installed game.
- [ ] TS-04 (886d5f0): the automatic-install orchestration has no integration test tying "done" to the written config.
- [ ] TS-01b (886d5f0): all-options.tsx computes ungrouped sections, empty enums and inverted ranges but exits on React errors only.
- [ ] running-exe-folder (886d5f0): install next to the executable the game actually runs (the process OptiScaler's log names), not the scored guess that favours Unreal's Binaries/Win64. A feature: needs a decision. Reported 2026-11-18, awaiting a decision.

## Rules

What is never taken (AGENTS.md "Taking from upstream" has why), so it is not queued:

- Anything reading or writing OptiScaler's State or internals outside the plugin API and the generated Config layout (upstream's 0.9.5 live control and its PDB layouts, backend or FFX FG switching through State).
- OptiScaler builds, archives committed to bin/, upstream's pinned OptiScaler version, payload lists and reference ini.
- Community FSR 4 downloads (4.1.1b, 4.1.2 Lite Exact): the fork's Deck presets use the bundled upscaler's Lite and RDNA2 models.
- Upstream's own repository metadata, docs and release notes; upstream's names (a port adds `-nightly` names).
- Hand-written per-game tables (the folder problem is the running-exe-folder item, in general form). Decided 2026-11-18.
- Findings whose details upstream keeps private (security audits without the fix described): nothing to port from.
