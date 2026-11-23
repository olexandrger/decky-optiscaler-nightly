<!-- OptiScaler's v10 changelog (the 'nightly' release of optiscaler/OptiScaler) as last reviewed; scripts/optiscaler_review.py replaces it. sha256 8450c64e705f -->

# Actual Nightly build downloads can now be found [HERE](https://github.com/optiscaler/OptiScaler-nightly/releases)

---
---
---

# WIP Opti v10 changelog - will be updated through time
* Last updated 9 Sept 2026

## MAJOR features with v10

### **All bundled files** have been moved to the `OptiScaler` **subfolder** for a cleaner layout (@cdozdil, @FakeMichau)
  - Upscaler, FG, OptiPatcher and other binaries are now in the subfolder - _meaning OptiScaler no longer overwrites game files_
    - **_Only Opti itself, its config and the setup scripts remain in the root as before_**
- Updated the Setup BAT for the new folder layout changes (@TheRazerMD)
- Updated the config for all new changes (@cdozdil, @FakeMichau, @TheRazerMD)
- Added **Streamlined Fetcher BAT** located in the Opti subfolder (@TheRazerMD)
  - _Currently used for autodownloading latest Streamline files from the Streamline SDK repo_

---

### **Integrated Fakenvapi** into OptiScaler (@FakeMichau)
  - No longer needs `fakenvapi.dll` bundled with Opti

---

### **New UI Input system** (@cdozdil)
  - Should help fix uninteractable Opti overlay in some games, as well as mouse inputs not registering
  - _Some games might still act up where the overlay keyboard won’t work, but the mouse will – luckily there’s the Close button in the meantime_

---

### Added **DX11 FG support** (@cdozdil)
  - Only OptiFG/Upscaler inputs are supported, and performance is limited due to the DX12 interop overhead
  - _No HUDFix available and probably never will, XeFG should be useful in that regard_

### Added **Streamline 1 (SL1) DLSSG inputs** support (@cdozdil)
  - Experimental as every SL1 version is different, but should let Opti properly hook old SL1 DLSS-FG games when using DLSSG via SL (e.g. The Witcher 3, A Plague Tale Requiem)

---

### Added **DLSSG** as an **FG Output** (@cdozdil)
  - **_Real DLSS-FG still requires an RTX 4000+ series GPU_**
  - If you plan on using DLSSG outputs (including DLSSG with NvngxFG) – either use the **Streamlined Fetcher BAT** in Opti subfolder to download latest Streamline files
    -  OR manually download latest Streamline SDK, create a `streamline` folder in `OptiScaler` subfolder and **copy all .dll files** from `bin\x64\` into the newly created `streamline` subfolder _(so it ends up in `\OptiScaler\streamline`)_
- Added an in-game toggle for Dynamic MFG (@FakeMichau)

---

### **Refactored FG options**, now with **DLSSG via Nvngx** option (@FakeMichau)
- While DLSSG via SL tapped only into the Streamline resources and used the native vendor swapchains (for FSR-FG/XeFG), the **new DLSSG via Nvngx** option now fully taps into Nvidia NGX and **_uses the Streamline swapchain for frame pacing_** etc.
  - _Also the reason why XeFG isn't supported with "via Nvngx" option, tightly integrated with its own swapchain and XeLL_
- **DLSSG via Nvngx** is recommended for using Nukems, Enabler's FSR3 MFG mod and 2 new options - FSR4-FG and FFX + Enabler
  - Added **FSR4-FG using FFX upgrade**, inspired by #1087, partially based on Nukem's work where FSR3-FG gets upgraded to FSR4-FG on RDNA4 _(otherwise falls back to FSR3-FG, no different from standard Nukems)_
    -  _Might have better performance in some games, as well as frame pacing_
- **_Bit more technical explanation_**
  - Normal flow on a 4090 - Game -> Streamline -> Nvngx -> DLSSG
  - DLSSG Via SL - _Opti pretends to be Streamline, closer to the game, so there's more control_
  - DLSSG Via Nvngx - _Opti pretends to be Nvngx, simpler, acts more like native DLSSG, but is also more restrictive_

### **Added support for Artur’s FSR 3 MFG mod** (@FakeMichau)
  - Dispatch flags used for modifying mod options
  - Also displays Enabler version in the menu
  - Requires renaming DLSS Enabler binary to `dlss-enabler-headless.dll` and putting it **_inside OptiScaler subfolder_**
    - _Older games may have too old Streamline for MFG, in that case follow the DLSSG output steps above as well_

### Added **Nvngx FG Combo - FFX + Enabler combo** mode (@FakeMichau)
  - **Mainly for use with FSR4-FG** as otherwise it doesn't make sense
  - FFX for the middle fake frame, Enabler for the rest
    - 2x – FFX, 3x – Enabler, 4x - FFX + Enabler, 5x – Enabler, 6x - FFX + Enabler
    - Due to pacing, only odd number of fake frames are able to use FFX

---

#### More intricate FSR4 INT8 forcing system (@FakeMichau)
  - Expected to be more futureproof compared to the 0.9 one
  - If you want to use FSR4 INT8 on unsupported GPUs (RDNA3/3.5 mobile iGPUs/APUs, Nvidia, Intel), then set `Fsr4ForceModel=2` in **OptiScaler.ini**
  - Added option for loading custom amdxc64.dll (@FakeMichau)
    - Used by RDNA2 Windows users that want to try FSR 4.1.1 without needing to downgrade to the old driver to avoid ghosting issues
    - Requires downloading https://download.amd.com/dir/bin/amdxc64.dll/64DE3678449f000, putting the amdxc64.dll inside the OptiScaler subfolder and also setting the option `LoadCustomAmdxc64OnRdna2=true` in **OptiScaler.ini**

---

#### Added ability to **use XeLL on non-Intel GPUs** without using XeFG (@FakeMichau)
  - _Requires using the XeFG swapchain, but FG itself is not active._
  - **_Force XeLL_** in the overlay under Fakenvapi, or `ForceXeLL=` option in **OptiScaler.ini**

---

#### Added a **basic notifications system** (@FakeMichau)
  - Notify about some upscaler issues, late Streamline hooking, when RTSS Reflex injection is enabled with XeFG etc.

#### **Massive code revamp, refactoring, cleaning** that will make it easier to maintain the code and add features in the future (@FakeMichau, @cdozdil)
  - _Macro additions, added IdentifyGpu, factored out DX12 shaders/downscalers into common code, refactored DX11 shaders, refactored VK shaders, refactored Vk w/Dx12 upscalers, better HDR management, more Dxgi exports enabled, a bit better resource ownership for Dx12 Imgui menu etc._

---

## Minor features with v10

- Added a **magnifier** for maximum pixel peeping (@FakeMichau)
  - _Supports DX12 upscalers, native DX11 upscalers, Vulkan for now_
  - Can be enabled from the Magnifier section in Opti Overlay
- **Overlay adjustments, additions and unifying** (@FakeMichau, @cdozdil)
  - Added radio buttons, merged some dropdown options, new tooltips
- Fixed GPU detection and Reflex timings overlay on native Vulkan games (@Astyyyyy)
- Hash game exe when making a report (@FakeMichau)
- Use DXVK interop for GPU info detection (@FakeMichau, @Astyyyyy)
- Added option for **Overlays using the theme** (@cdozdil)
  - _Set if you want the FPS overlay to also use current accent colour theme_
- Disable HUDFix in games that are known to have issues (@FakeMichau)
- **Faster upscaler context switching** (@FakeMichau)
- Send notification if trace logging is enabled for an extended period of time (@FakeMichau)
- Added **HDR16 check** to the menu for **DLSSG** (@FakeMichau)
- **Reworked spoof skipping** in order to avoid some race condition issues (@FakeMichau)
- Kill native AL2 if SL Reflex is hooked (@FakeMichau)
- Per shader GPU time breakdown for DX11 and DX12 for now (@FakeMichau)
  - _Also show OS shader's name in GPU time breakdown_
- Fixed GPU probe recursion with dxvk-nvapi (@levzzz5154)
  - _Fixes adapter identification on Linux_
- Fixed some Luma mod related issues (@cdozdil)
