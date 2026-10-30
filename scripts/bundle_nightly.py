#!/usr/bin/env python3
"""Bundle an OptiScaler nightly build in place of the pinned release.

    python3 scripts/bundle_nightly.py path/to/OptiScaler_v10.0.0-pre1_20261030.7z
    python3 scripts/bundle_nightly.py --latest      # newest optiscaler/OptiScaler-nightly

What it does, in order:

1. Unpacks the nightly into a scratch folder.
2. Flattens it. Since 10.0, the archive keeps every dependency DLL in an
   ``OptiScaler/`` subfolder next to OptiScaler.dll. OptiScaler only uses that
   folder when it exists; otherwise it falls back to the game folder itself
   (dllmain.cpp, MainDllPath/PluginPath). Flattening keeps this plugin's
   install layout -- proxy, INI, DLLs, ``plugins/`` -- exactly as it was, so
   the FSR 4 build swap, verify, backup and uninstall paths keep working.
3. Repacks it as ``bin/OptiScaler-<version>.7z`` (plain LZMA2, which every
   archiver the plugin looks for can read) and removes the previous archive.
4. Rewrites the pinned values in ``py_modules/optiscaler/constants.py``:
   version, archive name, SHA-256, the payload file/dir lists (retiring
   anything a previous bundle shipped that this one does not), and the hash
   and size of the bundled FidelityFX upscaler DLL that FSR 4 restore uses.
5. Copies the nightly's OptiScaler.ini to ``defaults/OptiScaler.reference.ini``.
   Run ``scripts/generate_schema.py`` afterwards to refresh Advanced settings.
"""

import argparse
import ast
import hashlib
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
CONSTANTS = ROOT / "py_modules" / "optiscaler" / "constants.py"
FSR4BUILD = ROOT / "py_modules" / "optiscaler" / "fsr4build.py"
REFERENCE_INI = ROOT / "defaults" / "OptiScaler.reference.ini"
PACKAGE_JSON = ROOT / "package.json"

NIGHTLY_REPO = "optiscaler/OptiScaler-nightly"
FFX_UPSCALER_DLL = "amd_fidelityfx_upscaler_dx12.dll"

# Top-level files the installer handles itself, or that never go in a game folder.
NOT_PAYLOAD = {
    "OptiScaler.dll",
    "OptiScaler.ini",
    "setup_linux.sh",
    "setup_windows.bat",
    # Windows-only helper that downloads Streamline DLLs; nothing to run on a Deck.
    "Streamlined_fetcher_windows.bat",
}


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def pe_file_version(path):
    """(major, minor, hotfix, build) from a PE's VS_FIXEDFILEINFO, or None."""
    data = Path(path).read_bytes()
    marker = data.find(b"\xbd\x04\xef\xfe")
    if marker < 0:
        return None
    ms, ls = struct.unpack("<II", data[marker + 8: marker + 16])
    return (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)


def run(argv):
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"{' '.join(argv[:2])} failed:\n{result.stderr or result.stdout}")


def archiver():
    for name in ("7z", "7zz", "7za"):
        if shutil.which(name):
            return name
    sys.exit("need 7z, 7zz or 7za on PATH (p7zip / 7-Zip)")


def download_latest(dest_dir):
    api = f"https://api.github.com/repos/{NIGHTLY_REPO}/releases?per_page=1"
    with urllib.request.urlopen(api, timeout=60) as response:
        release = json.load(response)[0]
    asset = next((a for a in release["assets"] if a["name"].endswith(".7z")), None)
    if not asset:
        sys.exit(f"{release['tag_name']} has no .7z asset")
    target = Path(dest_dir) / asset["name"]
    print(f"downloading {release['tag_name']}: {asset['name']}")
    with urllib.request.urlopen(asset["browser_download_url"], timeout=600) as response, \
            open(target, "wb") as handle:
        shutil.copyfileobj(response, handle)
    return target


def flatten(extracted):
    """Return a folder laid out the way the 0.9.x releases were."""
    root = next((p.parent for p in extracted.rglob("OptiScaler.dll")), None)
    if root is None:
        sys.exit("no OptiScaler.dll in the archive")
    deps = root / "OptiScaler"
    if deps.is_dir():
        for item in deps.iterdir():
            destination = root / item.name
            if destination.exists():
                sys.exit(f"{item.name} exists both at the top level and in OptiScaler/")
            shutil.move(str(item), str(destination))
        deps.rmdir()
    for name in NOT_PAYLOAD - {"OptiScaler.dll", "OptiScaler.ini"}:
        (root / name).unlink(missing_ok=True)
    return root


def version_label(dll, archive_name):
    major, minor, hotfix, build = pe_file_version(dll) or (0, 0, 0, 0)
    label = f"{major}.{minor}.{hotfix}-pre{build}"
    # Upstream names it OptiScaler_v10.0.0-pre1_20261030.7z; this repo stores
    # OptiScaler-10.0.0-pre1-20261030.7z, which re-bundling after a rebase reads.
    stamp = re.search(r"[_-](\d{8})\.7z$", archive_name)
    return f"{label}-{stamp.group(1)}" if stamp else label


def read_list(source, name):
    match = re.search(rf"^{name}\s*=\s*(\[.*?\])", source, re.S | re.M)
    return ast.literal_eval(match.group(1)) if match else []


def write_list(source, name, values, comment=None):
    body = "[\n" + "".join(f'    "{v}",\n' for v in values) + "]"
    pattern = re.compile(rf"^{name}\s*=\s*\[.*?\]", re.S | re.M)
    if pattern.search(source):
        return pattern.sub(lambda _: f"{name} = {body}", source, count=1)
    anchor = re.search(r"^PAYLOAD_DIRS\s*=\s*\[.*?\]", source, re.S | re.M)
    block = (f"\n\n# {comment}\n" if comment else "\n\n") + f"{name} = {body}"
    return source[: anchor.end()] + block + source[anchor.end():]


def set_value(source, name, literal):
    pattern = re.compile(rf"^{name}\s*=\s*(\([^)]*\)|[^\n]+)", re.S | re.M)
    if not pattern.search(source):
        sys.exit(f"{name} not found in constants.py")
    return pattern.sub(lambda _: f"{name} = {literal}", source, count=1)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("archive", nargs="?", help="nightly .7z to bundle")
    parser.add_argument("--latest", action="store_true", help=f"download the newest {NIGHTLY_REPO} build")
    parser.add_argument("--if-newer", action="store_true",
                        help="with --latest: exit 3 without changing anything if that build is already bundled")
    args = parser.parse_args()
    if bool(args.archive) == args.latest:
        parser.error("give an archive path or --latest, not both")

    seven = archiver()
    with tempfile.TemporaryDirectory() as scratch:
        scratch = Path(scratch)
        if args.latest and args.if_newer:
            current = re.search(r'^PAYLOAD_ARCHIVE\s*=\s*"OptiScaler-.*?-(\d{8})\.7z"',
                                CONSTANTS.read_text(encoding="utf-8"), re.M)
            api = f"https://api.github.com/repos/{NIGHTLY_REPO}/releases?per_page=1"
            with urllib.request.urlopen(api, timeout=60) as response:
                tag = json.load(response)[0]["tag_name"]
            if current and tag.endswith(current.group(1)):
                print(f"{tag} is already bundled")
                sys.exit(3)
            # CI never commits the archive back, so main keeps an older one; a
            # release tag for this nightly's date means it was already built.
            date = tag.rsplit("-", 1)[-1]
            released = subprocess.run(["git", "tag", "--list", f"v*-nightly.{date}"], cwd=ROOT,
                                      capture_output=True, text=True).stdout.strip()
            if released:
                print(f"{tag} is already released as {released.splitlines()[0]}")
                sys.exit(3)
        archive = download_latest(scratch) if args.latest else Path(args.archive).resolve()
        extracted = scratch / "x"
        run([seven, "x", "-y", f"-o{extracted}", str(archive)])
        root = flatten(extracted)

        version = version_label(root / "OptiScaler.dll", archive.name)
        files = sorted(p.name for p in root.iterdir() if p.is_file() and p.name not in NOT_PAYLOAD)
        dirs = sorted(p.name for p in root.iterdir() if p.is_dir())
        ffx = root / FFX_UPSCALER_DLL
        if not ffx.is_file():
            sys.exit(f"{FFX_UPSCALER_DLL} missing -- FSR 4 restore needs it")
        ffx_version = ".".join(map(str, (pe_file_version(ffx) or (0, 0, 0))[:3]))

        out_name = f"OptiScaler-{version}.7z"
        out = BIN / out_name
        # Packed under a temporary name first: a failed repack must not leave
        # bin/ without an archive. -mf=off: no BCJ2/BCJ filters (plain LZMA2,
        # which bsdtar and every 7-Zip read). Members relative to root.
        packed = scratch / out_name
        subprocess.run([seven, "a", "-t7z", "-mx=7", "-mf=off", "-mmt=on", str(packed), "."],
                       cwd=root, check=True, capture_output=True)
        for old in BIN.glob("OptiScaler-*.7z"):
            old.unlink()
        shutil.move(str(packed), str(out))
        shutil.copy2(root / "OptiScaler.ini", REFERENCE_INI)

        source = CONSTANTS.read_text(encoding="utf-8")
        old_files = read_list(source, "PAYLOAD_FILES")
        old_dirs = read_list(source, "PAYLOAD_DIRS")
        retired_files = sorted((set(old_files) | set(read_list(source, "RETIRED_PAYLOAD_FILES"))) - set(files))
        retired_dirs = sorted((set(old_dirs) | set(read_list(source, "RETIRED_PAYLOAD_DIRS"))) - set(dirs))

        source = set_value(source, "OPTISCALER_VERSION", f'"{version}"')
        source = set_value(source, "PAYLOAD_ARCHIVE", f'"{out_name}"')
        source = set_value(source, "PAYLOAD_SHA256", f'"{sha256(out)}"')
        source = write_list(source, "PAYLOAD_FILES", files)
        source = write_list(source, "PAYLOAD_DIRS", dirs)
        source = write_list(source, "RETIRED_PAYLOAD_FILES", retired_files,
                            "Shipped by an earlier bundle, not by this one. An install that owns them "
                            "removes them on upgrade.")
        source = write_list(source, "RETIRED_PAYLOAD_DIRS", retired_dirs,
                            "Directories an earlier bundle shipped (0.9.x spelled D3D12_Optiscaler).")
        source = set_value(source, "FSR4_BUNDLED_FILE_SHA256", f'(\n    "{sha256(ffx)}"\n)')
        source = set_value(source, "FSR4_BUNDLED_FILE_BYTES", str(ffx.stat().st_size))
        CONSTANTS.write_text(source, encoding="utf-8")

        # The plugin's own version: the upstream base plus the nightly date, so
        # Decky shows which OptiScaler build is inside.
        package = PACKAGE_JSON.read_text(encoding="utf-8")
        base = re.search(r'"version"\s*:\s*"([0-9.]+)', package).group(1)
        stamp = re.search(r"(\d{8})$", version)
        plugin_version = f"{base}-nightly.{stamp.group(1) if stamp else version}"
        package = re.sub(r'("version"\s*:\s*")[^"]+(")', rf"\g<1>{plugin_version}\g<2>", package, count=1)
        PACKAGE_JSON.write_text(package, encoding="utf-8")

        label = FSR4BUILD.read_text(encoding="utf-8")
        label = re.sub(r'"Bundled \(FidelityFX SDK [^)]*\)"', f'"Bundled (FidelityFX SDK {ffx_version})"', label)
        FSR4BUILD.write_text(label, encoding="utf-8")

    print(f"bundled OptiScaler {version} (plugin version {plugin_version})")
    print(f"  bin/{out_name}  ({out.stat().st_size / 1e6:.1f} MB)")
    print(f"  payload files: {', '.join(files)}")
    print(f"  payload dirs:  {', '.join(dirs)}")
    if retired_files or retired_dirs:
        print(f"  retired:       {', '.join(retired_files + [d + '/' for d in retired_dirs])}")
    print(f"  bundled FSR upscaler: {ffx_version}")
    print("next: python3 scripts/generate_schema.py && pnpm build && python3 scripts/package.py")


if __name__ == "__main__":
    main()
