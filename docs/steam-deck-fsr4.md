# Steam Deck FSR 4 (nightly fork)

Close the game, install OptiScaler through the plugin if needed, and pick one of
these under Basic → Override upscaler with:

| Preset | Model (`Fsr4Model`) | `Fsr4GpuSupport` | Trade-off |
|---|---|---|---|
| **FSR 4 Lite — Steam Deck** | 3 — INT8 Lite | 4 | AMD's model for APUs, the one OptiScaler 10 detects for the Deck's Van Gogh. Lightest. |
| **FSR 4 RDNA2 — Steam Deck** | 2 — INT8 RDNA2 | 3 | AMD's INT8 model for RDNA 2. Better image, more GPU time. |

Both run the FidelityFX 4.1.2 upscaler bundled with OptiScaler 10
(`amd_fidelityfx_upscaler_dx12.dll`, which registers both providers:
`ffxProvider_FSR4Lite_Int8` and `ffxProvider_FSR4_Int8_Navi2`). Nothing is
downloaded. Each preset also sets the FidelityFX backend (`ffx` / `ffx_12`),
`UpscalerIndex=0` (FSR 4) and leaves frame generation alone. The DLL and the
settings are written as one step and rolled back together if anything fails.

`Fsr4GpuSupport` is written explicitly even though OptiScaler detects the Deck as
INT8 Lite on its own, so the preset does not depend on asic detection under
Proton.

## Checking it

OptiScaler's overlay (Insert) shows the active upscaler and lets you switch
`Fsr4Model` while playing, so Lite and RDNA2 can be compared in the same scene.
`OptiScaler.log` in the game folder names the model it created.

## Rolling back

Pick **FSR 3.X** (or any other upscaler) in Basic settings, or uninstall
OptiScaler from the game; uninstall restores the game's own files.

## The retired 4.1.1b build

Earlier versions of the plugin downloaded a community 4.1.1b build with an RDNA 2
ghosting fix. FidelityFX 4.1.2 ships AMD's own RDNA 2 model, so that download is
gone. A game folder that still has it shows it as retired; choosing either preset,
or reinstalling OptiScaler there, puts the bundled upscaler back.
