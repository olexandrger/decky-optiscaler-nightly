# OptiScaler review

What OptiScaler's own nightlies add that this plugin should follow or use, as
opposed to what breaks a check (that is the release step's autofix).
`scripts/optiscaler_review.py` reads and updates this file; AGENTS.md
"Following OptiScaler" says how a review goes.

This file holds only what is open and the rules that keep things from being
looked at twice. It stays small: one line per item, at most 12 open items and
12 rules (`optiscaler_review.py check`, run by selftest). A resolved item is
deleted in the commit that resolves it, and that commit's message is its record
(`git log --grep "OptiScaler review"`). The v10 changelog as last reviewed is
in `optiscaler-changelog.md`, replaced at each review.

reviewed: b7b07d2d nightly-20261123

## Open

Each line: `- [ ] <id> (<OptiScaler commit>): <what it is> -- <verdict and recommendation>.`
Once reported, " Reported <date>, awaiting a decision." is appended.

## Rules

- NVIDIA-only paths (DLSSG output, DLSSG via Nvngx, Streamline, Reflex and fakenvapi tuning): the Deck has none. Not queued.
- Windows-only things (setup .bat, Streamline fetcher, a custom amdxc64.dll for Windows RDNA2 drivers, DLSS Enabler mods): not queued.
- OptiScaler's own overlay tools (magnifier, GPU time breakdown, notifications, themes): the OptiScaler menu button reaches them. Not queued.
- A new ini key shows in All settings by itself (the schema is regenerated at every release). Queue it only when OptiScaler re-reads it while running (a live candidate: read its consumer and the menu's write) or it matters on a Deck (FSR 4, FSR FG, XeFG, the sharpener, the upscalers a Deck runs).
- Changes to the archive's layout, to where OptiScaler looks for its files (dllmain.cpp, layout.py) or to its plugin API are always queued.
- What upstream's plugin does is docs/upstream-review.md's, not this file's.
