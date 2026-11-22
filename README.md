# Decky OptiScaler Nightly

> [!WARNING]
> **Experimental personal fork, for tinkering only. Not intended for real use.**
> Want the plugin? Use the original,
> [danielcamilo1/decky-optiscaler](https://github.com/danielcamilo1/decky-optiscaler).
> Report nothing from this fork to its author or to the OptiScaler project.

[![Build](https://github.com/olexandrger/decky-optiscaler-nightly/actions/workflows/build.yml/badge.svg)](https://github.com/olexandrger/decky-optiscaler-nightly/actions/workflows/build.yml)
[![Nightly bundle](https://github.com/olexandrger/decky-optiscaler-nightly/actions/workflows/nightly.yml/badge.svg)](https://github.com/olexandrger/decky-optiscaler-nightly/actions/workflows/nightly.yml)

A [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) plugin that installs
[OptiScaler](https://github.com/optiscaler/OptiScaler) into Steam games and drives it from
Game Mode, built around OptiScaler's **nightly** builds instead of its releases.

It started as a fork of
[danielcamilo1/decky-optiscaler](https://github.com/danielcamilo1/decky-optiscaler) and has
since gone its own way: it no longer follows the original's code, and takes fixes and ideas
from it one at a time ([docs/upstream-review.md](docs/upstream-review.md)).

## What it does differently

- **OptiScaler nightlies.** Each release bundles the newest
  [OptiScaler nightly](https://github.com/optiscaler/OptiScaler-nightly) (10.x), built and
  tested daily. The plugin updates itself (**Update to …**); a game gets the new OptiScaler
  with **Update OptiScaler in this game**.
- **Live editing through OptiScaler 10's plugin API.** Frame generation and its multiplier
  (Dynamic MFG too, only the steps XeFG can do on the GPU), the FSR version, the FSR 4, XeSS
  and DLSS models, sharpness, OptiScaler's sharpener (OptiSharpen and its model) and the FPS
  overlay apply in the running game. Another upscaler backend applies at the next launch, or live from OptiScaler's
  own menu (a button on the panel).
- **The running game's real settings.** The panel shows what OptiScaler holds now, including
  changes made in its own menu, and can keep them in `OptiScaler.ini`.
- **FSR 4 on a Steam Deck** with the INT8 Lite and INT8 RDNA2 models of the bundled
  FidelityFX upscaler, no extra downloads.
- **Safe installs.** OptiScaler goes in as OptiScaler 10 lays itself out, its DLLs in an
  `OptiScaler` folder, so the game's own files stay as they are. An install or update that
  fails part way leaves the game folder as it was, and each update keeps the previous
  OptiScaler, so one game can go back to it.
- **A separate plugin.** It can sit next to the original, but the two never manage the same
  game.

## Install

Decky Loader → Settings → Developer mode → **Install Plugin from URL**:

```
https://github.com/olexandrger/decky-optiscaler-nightly/releases/latest/download/decky-optiscaler-nightly.zip
```

Then open a game in the plugin and install OptiScaler into it. Only tested on a Steam Deck.

## Development

[AGENTS.md](AGENTS.md) holds the design rules, the build and the full list of checks;
[CHANGELOG.md](CHANGELOG.md) what changed.

## License and credits

- The plugin's code is under the BSD 3-Clause license in [LICENSE](LICENSE), inherited from
  the original plugin and the Decky plugin template it was built on, with their copyright
  notices kept.
- Releases bundle OptiScaler, which is GPL-3.0. Each release names the OptiScaler nightly it
  carries and links OptiScaler's source at the commit that build came from; the licenses of
  the components OptiScaler ships are in its `OptiScaler/Licenses` folder.
- The original plugin is [danielcamilo1](https://github.com/danielcamilo1)'s work (you can
  [support them on Ko-fi](https://ko-fi.com/danielcamilo)). OptiScaler is the
  [OptiScaler project](https://github.com/optiscaler/OptiScaler)'s, with its wiki
  contributors. Neither is affiliated with this fork or endorses it.
- AI disclosure: this fork is developed largely by Claude (Anthropic), through Claude Code,
  directed and tested on a Steam Deck by [olexandrger](https://github.com/olexandrger).
