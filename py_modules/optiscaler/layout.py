"""Where OptiScaler 10 looks for its own files in a game folder.

OptiScaler.dll (under a proxy name) and OptiScaler.ini sit in the game folder.
Everything else, its DLLs, D3D12_OptiScaler/ and its ASI plugins/, is in an
``OptiScaler/`` folder next to them: dllmain.cpp sets MainDllPath to
"OptiScaler" (relative to its own dll), falls back to the dll's folder when
that is missing, and loads plugins from MainDllPath/plugins. So a game's own
libxess.dll or FidelityFX DLLs stay untouched, and OptiScaler uses its own.

Installs this plugin made before it followed that layout put the DLLs in the
game folder; with no OptiScaler/ folder there OptiScaler reads them from it,
which is the same rule. [Libraries] OptiDllPath and [Plugins] Path can point
OptiScaler elsewhere; this plugin never writes them, and its diagnostics say
when an ini does (live.diagnostics).
"""
from pathlib import Path

from .constants import FFX_UPSCALER_DLL, OPTI_SUBDIR, PAYLOAD_FILES

PLUGINS = "plugins"


def opti_dir(target_dir):
    """The folder OptiScaler reads its DLLs from, by its own rule."""
    target = Path(target_dir)
    sub = target / OPTI_SUBDIR
    return sub if sub.is_dir() else target


def plugin_dir(target_dir):
    """Where OptiScaler loads ASI plugins from."""
    return opti_dir(target_dir) / PLUGINS


def ffx_upscaler(target_dir):
    """The FidelityFX upscaler OptiScaler itself loads (not a copy the game ships)."""
    return opti_dir(target_dir) / FFX_UPSCALER_DLL


def payload_name(basename):
    """Where the payload, and so an install, has the file of this name."""
    return next((name for name in PAYLOAD_FILES if Path(name).name == basename), basename)


def relative(target_dir, path):
    """`path` as a name in the game folder, as manifests record them."""
    return Path(path).relative_to(Path(target_dir)).as_posix()
