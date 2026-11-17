#!/usr/bin/env python3
"""End-to-end backend test against a synthetic Steam library in a temp dir.

Exercises library discovery, executable-folder scoring, the wiki lookup (needs
network; skipped gracefully offline), install/detect/uninstall round trips and
comment-preserving INI edits.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "py_modules"))

from optiscaler.service import OptiScalerService  # noqa: E402
from optiscaler import installer  # noqa: E402
from optiscaler.inifile import IniFile  # noqa: E402
from optiscaler.constants import COMPAT_CACHE_TTL, INI_NAME  # noqa: E402

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {name}{(' — ' + str(detail)) if detail else ''}")


def build_fixture(root):
    """A Steam install with two libraries and two differently-shaped games."""
    home = root / "home"
    steam = home / ".local" / "share" / "Steam"
    lib2 = root / "sdcard"
    (steam / "steamapps" / "common").mkdir(parents=True)
    (lib2 / "steamapps" / "common").mkdir(parents=True)
    (home / ".steam").mkdir(parents=True, exist_ok=True)
    os.symlink(steam, home / ".steam" / "steam")

    (steam / "steamapps" / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n'
        '\t"1"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n}\n' % (steam, lib2)
    )
    (steam / "steamapps" / "appmanifest_1091500.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"1091500"\n\t"name"\t\t"Cyberpunk 2077"\n'
        '\t"installdir"\t\t"Cyberpunk 2077"\n\t"SizeOnDisk"\t\t"73819938816"\n}\n'
    )
    (steam / "steamapps" / "appmanifest_228980.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"228980"\n\t"name"\t\t"Steamworks Common Redistributables"\n'
        '\t"installdir"\t\t"Steamworks Shared"\n}\n'
    )
    (lib2 / "steamapps" / "appmanifest_2358720.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"2358720"\n\t"name"\t\t"Black Myth Wukong"\n'
        '\t"installdir"\t\t"BlackMythWukong"\n\t"SizeOnDisk"\t\t"1"\n}\n'
    )

    # Cyberpunk: launcher at the root, real binary under bin/x64.
    cp = steam / "steamapps" / "common" / "Cyberpunk 2077"
    (cp / "bin" / "x64").mkdir(parents=True)
    (cp / "REDprelauncher.exe").write_bytes(b"MZ")
    (cp / "bin" / "x64" / "Cyberpunk2077.exe").write_bytes(b"MZ" + b"\0" * (60 << 20))
    (steam / "steamapps" / "common" / "Steamworks Shared").mkdir(parents=True)

    # Unreal: real binary under <Project>/Binaries/Win64, Engine/ must be ignored.
    bmw = lib2 / "steamapps" / "common" / "BlackMythWukong"
    (bmw / "Engine" / "Binaries" / "Win64").mkdir(parents=True)
    (bmw / "b1" / "Binaries" / "Win64").mkdir(parents=True)
    (bmw / "Engine" / "Binaries" / "Win64" / "UnrealEditor.exe").write_bytes(b"MZ")
    (bmw / "b1" / "Binaries" / "Win64" / "b1-Win64-Shipping.exe").write_bytes(b"MZ" + b"\0" * (80 << 20))
    (bmw / "b1" / "Binaries" / "Win64" / "CrashReportClient.exe").write_bytes(b"MZ")
    return home


async def run():
    root = Path(tempfile.mkdtemp(prefix="decky-optiscaler-selftest-"))
    try:
        home = build_fixture(root)
        service = OptiScalerService(
            ROOT, root / "settings", root / "runtime", home, logging.getLogger("selftest")
        )

        print("\nPayload")
        status = await service.get_status()
        check("bundled archive present", status["archive_present"], status["archive_path"])
        if not status["archive_present"]:
            # Nightly fork: no archive is committed. Everything after this
            # installs it, so stop here rather than fail a hundred times.
            sys.exit("no OptiScaler archive in bin/: run `python3 scripts/bundle_nightly.py --latest` "
                     "(or give it a downloaded .7z), then re-run the selftest")
        check("an extractor is available", bool(status["extractor"]), status["extractor"])
        if not status["extractor"]:
            print("  (no 7z/bsdtar — skipping install tests)")
            return

        print("\nLibraries and games")
        libraries = await service.list_libraries()
        check("two steam libraries found", len(libraries) == 2, [l["name"] for l in libraries])
        added = await service.add_custom_library(str(root / "sdcard" / "steamapps" / "common"))
        check("custom library added", added["ok"])
        check("duplicate library rejected",
              not (await service.add_custom_library(str(root / "sdcard" / "steamapps" / "common")))["ok"])
        libraries = await service.list_libraries()
        check("custom library listed", any(l["source"] == "custom" for l in libraries))

        games = await service.list_games(libraries[0]["path"])
        names = [g["name"] for g in games]
        check("game listed", "Cyberpunk 2077" in names, names)
        check("redistributables filtered out",
              not any("Steamworks" in n for n in names), names)

        print("\nInstall target detection")
        cyberpunk = next(g for g in games if g["name"] == "Cyberpunk 2077")
        detail = await service.get_game(cyberpunk["path"], cyberpunk["name"])
        check("launcher folder rejected in favour of bin/x64",
              detail["target"].endswith("bin/x64"), detail["target"])
        wukong = await service.get_game(
            str(root / "sdcard" / "steamapps" / "common" / "BlackMythWukong"), "Black Myth Wukong"
        )
        check("unreal project binaries chosen over Engine/",
              wukong["target"].endswith("b1/Binaries/Win64"), wukong["target"])

        print("\nWiki name matching")
        from optiscaler.wiki import WikiClient  # noqa: E402
        # Against the bundled seed rather than whatever the wiki serves today.
        # With no seed this fell through to a synchronous download, so the
        # matcher was being graded on a page a few hundred people can edit:
        # when one row lost its leading pipe upstream, two of these probes
        # started failing and the build went red on a commit that had not
        # touched the matcher. What the live page does to the *parser* is
        # check_compat_list_shapes()'s business, from a fixture.
        wiki = WikiClient(root / "runtime" / "wiki-cache",
                          ROOT / "defaults" / "compat-list.json")
        wiki_entries, wiki_meta = wiki.load_entries()
        if not wiki_entries:
            print("  (offline — skipped)")
        else:
            expected = {
                "Expedition 33": "Clair Obscur: Expedition 33",
                "Cyberpunk 2077": "Cyberpunk 2077",
                "BlackMythWukong": "Black Myth: Wukong",
                "Talos Principle 2": "The Talos Principle 2",
                "Oblivion Remastered": "The Elder Scrolls IV: Oblivion Remastered",
            }
            for probe, want in expected.items():
                hit = wiki.match(probe, wiki_entries)
                check(f"matches {probe!r}", hit is not None and hit["name"] == want,
                      hit["name"] if hit else "no match")
            for probe in ("Balatro", "Stardew Valley", "Totally Made Up Game 9000"):
                check(f"no false positive for {probe!r}",
                      wiki.match(probe, wiki_entries) is None)

        print("\nWiki lookup")
        recommendation = await service.get_recommendation("Cyberpunk 2077")
        if recommendation["list_meta"]["error"] and not recommendation["matched"]:
            print("  (offline — skipped)")
        else:
            check("matched the compatibility list", recommendation["matched"], recommendation["game"])
            # The answer is two-phase now. The compatibility-list row answers
            # at once, and this game's own wiki page — which is what states the
            # filename outright — arrives behind it. Fetching it in front of
            # the answer is what made the setup tab hang for ever on a Deck
            # whose route to the wiki stalls.
            check("the first answer says its wiki page is still coming",
                  recommendation["detail_pending"] is True
                  or recommendation["filename_source"] == "wiki entry",
                  f"pending={recommendation['detail_pending']} "
                  f"via {recommendation['filename_source']}")
            for _ in range(200):
                await asyncio.sleep(0.05)
                if not service._wiki_jobs:
                    break
            settled = await service.get_recommendation("Cyberpunk 2077")
            check("and once it lands the filename comes from that entry",
                  settled["filename_source"] == "wiki entry"
                  and not settled["detail_pending"],
                  f"{settled['filename']} via {settled['filename_source']}")

        print("\nSchema generation")
        from optiscaler.schema_generated import OPTIONS  # noqa: E402
        by_id = {(o["section"], o["key"]): o for o in OPTIONS}
        multiplier = by_id[("XeFG", "InterpolationCount")]
        check("frame multiplier is a 2X/3X/4X choice, not a toggle",
              multiplier["type"] == "enum" and multiplier["options"] == ["1", "2", "3"],
              f"{multiplier['type']} {multiplier.get('options')}")
        check("multiplier labelled 2X/3X/4X",
              multiplier.get("optionLabels") == {"1": "2X", "2": "3X", "3": "4X"})
        # The upscaler pickers: a nightly rewording the "or a custom upscaler"
        # note once turned Dx12Upscaler into a free-text field (2026-11-13).
        for key in ("Dx11Upscaler", "Dx12Upscaler", "VulkanUpscaler"):
            picker = by_id[("Upscalers", key)]
            check(f"{key} is a choice of backends, not free text",
                  picker["type"] == "enum" and {"dlss", "xess"} <= set(picker.get("options") or []),
                  f"{picker['type']} {picker.get('options')}")
        dx12 = by_id[("Upscalers", "Dx12Upscaler")]
        check("ffx documented as also serving FSR 4",
              "4.x" in (dx12.get("optionLabels") or {}).get("ffx", ""),
              (dx12.get("optionLabels") or {}).get("ffx"))

        print("\nInstall / uninstall round trip")
        target = Path(detail["target"])
        original = b"THE GAME'S OWN DXGI" * 8
        (target / "dxgi.dll").write_bytes(original)
        # Games that ship their own FidelityFX DLLs must get them back intact.
        game_upscaler = b"GAME'S OWN FSR31 UPSCALER" * 8
        (target / "amd_fidelityfx_upscaler_dx12.dll").write_bytes(game_upscaler)
        (target / "Licenses").mkdir(exist_ok=True)
        (target / "Licenses" / "game.txt").write_text("GAME LICENSE")

        result = await service.install(str(target), "dxgi.dll")
        check("install succeeded", result["ok"], result.get("error"))
        check("launch option built",
              result.get("launch_option") == 'WINEDLLOVERRIDES="dxgi=n,b" %command%',
              result.get("launch_option"))
        check("original dll backed up", "dxgi.dll" in (result.get("backups") or {}))
        from optiscaler.constants import BACKUP_DIR
        stash = target / BACKUP_DIR
        check("backup folder created", stash.is_dir(), stash.name)
        check("game's own FidelityFX dll set aside",
              (stash / "amd_fidelityfx_upscaler_dx12.dll").is_file())
        check("game's own Licenses folder set aside", (stash / "Licenses").is_dir())
        check("OptiScaler's FidelityFX dll actually replaced it",
              (target / "amd_fidelityfx_upscaler_dx12.dll").read_bytes() != game_upscaler)

        info = installer.detect(str(target))
        check("install detected", info["installed"] and info["filename"] == "dxgi.dll")
        from optiscaler import constants as _constants, live as _live
        check("a fresh install is not outdated",
              info["outdated"] is False and info["bundled_version"] == _constants.OPTISCALER_VERSION)

        # A game set up by an earlier build of the plugin keeps that build's
        # OptiScaler (and the live-control ASI compiled for it) until reapplied.
        manifest_path = target / _constants.MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["optiscaler_version"] = "10.0.0-pre1-20260901"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        ini_path = target / _constants.INI_NAME
        ini_path.write_text(ini_path.read_text(encoding="utf-8").replace(
            "Sharpness=auto", "Sharpness=0.33", 1), encoding="utf-8")
        stale = installer.detect(str(target))
        check("an install from an earlier bundle is outdated",
              stale["outdated"] is True and stale["version"] == "10.0.0-pre1-20260901")
        updated = await service.update_install(str(target))
        after = installer.detect(str(target))
        check("updating reapplies the bundled OptiScaler", updated["ok"] and after["outdated"] is False
              and after["version"] == _constants.OPTISCALER_VERSION, updated.get("error"))
        check("and keeps the proxy filename and OptiScaler.ini",
              after["filename"] == "dxgi.dll" and "Sharpness=0.33" in ini_path.read_text(encoding="utf-8"))
        check("and puts this build's live-control ASI in place",
              _live.asi_current(str(target), service.live_asi_path()) is True)
        check("updating a folder this plugin did not set up is refused",
              not (await service.update_install(str(root / "nowhere")))["ok"])

        # The launch options are Steam's, so only the frontend can read them --
        # but they are the other half of an install and have to be recoverable.
        recorded = await service.record_launch_options(str(target), "gamemoderun %command%")
        check("previous launch options recorded", recorded["ok"] and recorded["written"])
        # The client getter is absent from some builds; the frontend then hands
        # over None and Steam's own config has to answer instead. Recording
        # nothing is what made the removal dialog inert on those Decks.
        blind = await service.record_launch_options(str(root / "nowhere"), None, "1091500")
        check("a frontend that could not read is refused, not recorded as empty",
              not blind["ok"] and not blind["recorded"], blind.get("error"))
        again = await service.record_launch_options(
            str(target), 'WINEDLLOVERRIDES="dxgi=n,b" %command%')
        check("recording again keeps the original, not our own override",
              not again["written"] and again["value"] == "gamemoderun %command%",
              again["value"])
        check("the record travels with the install detail",
              installer.detect(str(target))["launch_record"]
              == {"recorded": True, "value": "gamemoderun %command%"})

        # Live control has to arrive with the install, and OptiScaler has to be
        # told to load it -- an .asi it never looks for would do nothing.
        from optiscaler import live as live_mod
        asi_shipped = (ROOT / "bin" / live_mod.ASI_NAME).is_file()
        if asi_shipped:
            check("live-control asi installed alongside OptiScaler",
                  (target / "plugins" / live_mod.ASI_NAME).is_file())
            ini_after = IniFile(target / "OptiScaler.ini").to_dict()
            check("OptiScaler told to load asi plugins",
                  ini_after.get("Plugins", {}).get("LoadAsiPlugins") == "true",
                  ini_after.get("Plugins"))
            check("install reports whether asi loading was switched on",
                  result.get("asi_loading_enabled") is True,
                  result.get("asi_loading_error"))
            # Path is deliberately left at auto: OptiScaler resolves that to
            # <dll folder>/plugins, whereas a relative "plugins" would be read
            # against the game's working directory instead.
            check("asi plugin path is left for OptiScaler to resolve",
                  ini_after.get("Plugins", {}).get("Path") in (None, "auto"),
                  ini_after.get("Plugins", {}).get("Path"))
        else:
            print("  [skip] live-control asi not built (run asi/build.sh)")
        check("recognised as plugin-managed", info["managed"])
        check("OptiScaler.ini written", info["ini_present"])

        print("\nSwitching filename")
        switched = await service.install(str(target), "winmm.dll")
        check("switch succeeded", switched["ok"])
        check("now installed as winmm.dll",
              installer.detect(str(target))["filename"] == "winmm.dll")
        check("original dxgi.dll restored on switch",
              (target / "dxgi.dll").read_bytes() == original)

        print("\nInstall verification / FSR4")
        verified = await service.verify_install(str(target))
        check("every bundled file landed in the game folder",
              verified["ok"] and verified["complete"], verified.get("problems"))
        ffx = verified["ffx_upscaler"]
        check("FidelityFX upscaler present with a readable version",
              ffx["present"] and ffx["version"], ffx)
        check("bundled FidelityFX provides FSR4 (>= 4.1.1)",
              ffx["fsr4_capable"], ffx["version"])

        source = root / "fsr4src"
        source.mkdir()
        (source / "amdxcffx64.dll").write_bytes(b"AMDXCFFX64" * 64)
        (source / "amdxc64.dll").write_bytes(b"AMDXC64" * 64)
        imported = await service.import_fsr4_files(str(target), str(source))
        check("optional driver FSR4 dlls imported", imported["ok"], imported.get("imported"))
        check("driver FSR4 dlls detected after import",
              installer.fsr4_status(str(target))["ready"])

        # The FSR 4 builds the panel offers, without the network: they are pinned
        # by hash, so the table can be checked and a local file identified
        # without a download, and installing one is a copy either way.
        print("\nFSR 4 upscaler builds")
        from optiscaler import fsr4build  # noqa: E402

        # Registered and retired community builds share one table shape; the
        # registry is empty while the bundled 4.1.2 carries the Deck's models.
        table = [*fsr4build.FSR4_BUILDS, *fsr4build.RETIRED_FSR4_BUILDS]
        check("every build pins a 64-character hash for the file it installs",
              all(len(b["file_sha256"]) == 64 for b in [*fsr4build.builds(), *table]))
        check("and one for the archive it arrives in",
              all(len(source["archive_sha256"]) == 64
                  for b in table for source in b["sources"]))
        check("every build names the one file these packages hold",
              all(b["file"] == installer.FFX_UPSCALER_DLL for b in [*fsr4build.builds(), *table]))
        check("every build names the release to fetch it from",
              all(source.get("repo") and source.get("tag") and source.get("asset")
                  for b in table for source in b["sources"]))
        check("and no two builds install the same file",
              len({b["file_sha256"] for b in table}) == len(table))
        check("4.1.1b is retired: recognised, no longer offered",
              fsr4build.retired("4.1.1b") and fsr4build.find("4.1.1b") is None)
        check("which setting reaches FSR 4 follows the version, not the GPU",
              fsr4build.reaches_fsr4_by([4, 1, 1, 2740]) == "int8"
              and fsr4build.reaches_fsr4_by([4, 0, 2, 0]) == "upgrade")
        check("the released upscaler is identified by its bytes",
              (fsr4build.identify(target / installer.FFX_UPSCALER_DLL) or {}).get("id")
              == "bundled")
        check("a build that is not one of ours says so",
              (fsr4build.identify(source / "amdxc64.dll") or {}).get("known") is False)

        # Install one from a hand-made "download": the same call the service
        # makes once `fetch` has verified a real one.
        fake_build = dict(fsr4build.RETIRED_FSR4_BUILDS[0], id="community", file_sha256="0" * 64)
        fake = root / "fake-community.dll"
        fake.write_bytes(b"NOT A REAL FSR4 DLL" * 1024)
        fake_build["file_sha256"] = hashlib.sha256(fake.read_bytes()).hexdigest()
        installed = installer.install_fsr4_build(str(target), fake, fake_build, None)
        check("a build installs over the released one",
              installed["build"]["id"] == fake_build["id"], installed)
        check("and the folder now reports an unrecognised build rather than the released one",
              (installer.fsr4_status(str(target))["build"] or {}).get("known") is False)
        check("the manifest records which build is in place",
              (installer.read_manifest(target) or {}).get("fsr4_build", {}).get("id")
              == fake_build["id"])

        restored = installer.restore_fsr4_build(
            str(target), str(await service.ensure_payload()), None
        )
        check("restoring puts the released build back",
              restored["restored"] == installer.FFX_UPSCALER_DLL
              and (restored["build"] or {}).get("id") == "bundled", restored)
        check("and clears the record of the imported one",
              not (installer.read_manifest(target) or {}).get("fsr4_build"))

        print("\nConfiguration")
        config = await service.read_config(str(target))
        # Every section of the bundled nightly's ini (37 on 2026-10-30, 38 with
        # [FlipMeter] from 2026-11-16; 34 in 0.9.4). Nightlies add sections, and
        # the Advanced tab shows any section, so compare with the ini actually
        # bundled rather than a count.
        reference = ROOT / "defaults" / "OptiScaler.reference.ini"
        sections = re.findall(r"^\[([^\]]+)\]", reference.read_text(encoding="utf-8"), re.M)
        check("ini parsed: every section of the bundled ini",
              config["ok"] and sorted(config["values"]) == sorted(sections) and len(sections) >= 37,
              f"{len(config['values'])} parsed, {len(sections)} in the bundled ini")
        before = (target / "OptiScaler.ini").read_text(encoding="utf-8")

        write = await service.write_config(str(target), [
            {"section": "FrameGen", "key": "Enabled", "value": "true"},
            {"section": "FrameGen", "key": "FGInput", "value": "fsrfg"},
            {"section": "Upscalers", "key": "Dx12Upscaler", "value": "ffx"},
            {"section": "FrameGen", "key": "FGOutput", "value": "NOT_A_VALUE"},
            {"section": "Sharpness", "key": "Sharpness", "value": "9.9"},
        ])
        check("valid changes applied", len(write["applied"]) == 3, write["applied"])
        check("invalid enum rejected",
              any(r["key"] == "FGOutput" for r in write["rejected"]))
        check("out-of-range float rejected",
              any(r["key"] == "Sharpness" for r in write["rejected"]))

        after_ini = IniFile(target / "OptiScaler.ini")
        values = after_ini.to_dict()
        check("values persisted",
              values["FrameGen"]["Enabled"] == "true"
              and values["Upscalers"]["Dx12Upscaler"] == "ffx")
        after = (target / "OptiScaler.ini").read_text(encoding="utf-8")
        check("every comment preserved",
              before.count(";") == after.count(";"),
              f"{before.count(';')} -> {after.count(';')}")

        print("\nManual wiki selection")
        if wiki_entries:
            found = await service.search_wiki("forza")
            names = [r["name"] for r in found["results"]]
            check("search finds Forza entries", "Forza Horizon 5" in names, names[:3])
            found = await service.search_wiki("expedition")
            check("search finds Expedition 33",
                  any("Expedition 33" in n for n in
                      [r["name"] for r in found["results"]]))
            await service.set_wiki_entry(cyberpunk["path"], "Forza Horizon 5")
            pinned = await service.get_recommendation(
                "Cyberpunk 2077", None, False, cyberpunk["path"]
            )
            check("pinned entry overrides name matching",
                  pinned["game"] == "Forza Horizon 5" and pinned.get("manual"),
                  pinned["game"])
            await service.set_wiki_entry(cyberpunk["path"], "")
            unpinned = await service.get_recommendation(
                "Cyberpunk 2077", None, False, cyberpunk["path"]
            )
            check("unpinning restores automatic matching",
                  unpinned["game"] == "Cyberpunk 2077", unpinned["game"])

            missing = await service.get_recommendation("Totally Made Up Game 9000")
            check("a miss reports the list as available, not broken",
                  missing["list_available"] and missing["entry_count"] > 0)
            check("a miss reports what was searched", missing["searched"] == ["Totally Made Up Game 9000"])
            check("a miss offers near misses to pick from", len(missing["near_misses"]) > 0)
        else:
            print("  (offline — skipped)")

        print("\nWiki refresh behind the answer")
        from optiscaler import wiki as wiki_mod
        real_get = wiki_mod._http_get
        served = []
        try:
            # A list already on disk, and a network that is slow enough that
            # waiting for it would be obvious in the answer's timing.
            service.wiki.list_cache.write_text(json.dumps({
                "fetched_at": time.time() - COMPAT_CACHE_TTL - 60,
                "entries": [{"name": "Cached Game", "key": "cachedgame", "page": None,
                             "compatibility": "OK", "inputs": "DLSS",
                             "optipatcher": False, "notes": ""}],
            }))

            def slow(url, *a, **k):
                served.append(url)
                time.sleep(0.4)
                return ("| Game | Compatibility | Inputs |\n|---|---|---|\n"
                        "| [Cached Game](Cached-Game) | OK | DLSS |\n"
                        "| [Brand New Game](Brand-New-Game) | OK | DLSS |\n")

            wiki_mod._http_get = slow
            started = time.monotonic()
            answer = await service.get_recommendation("Cached Game")
            elapsed = time.monotonic() - started
            check("a stale list still answers immediately", elapsed < 0.3, f"{elapsed:.2f}s")
            check("and answers from the cache it had", answer["matched"], answer.get("game"))
            check("the answer says the list behind it is old",
                  answer["list_meta"].get("stale") is True)
            before = answer["list_meta"].get("revision")

            # And the refresh really did run behind it.
            for _ in range(40):
                await asyncio.sleep(0.05)
                if not service._wiki_jobs:
                    break
            check("a refresh was started behind the answer", bool(served), served)
            after = await service.wiki_status()
            check("and the newer list replaced the cached one",
                  after["entry_count"] == 2 and after["revision"] != before,
                  f"{before} -> {after['revision']}")
            check("which is how the UI knows to redraw", bool(after["revision"]))

            # Single flight: three questions in a row are not three downloads.
            service.wiki.list_cache.write_text(json.dumps({
                "fetched_at": time.time() - COMPAT_CACHE_TTL - 60,
                "entries": after["entry_count"] * [
                    {"name": "Cached Game", "key": "cachedgame", "page": None,
                     "compatibility": "OK", "inputs": "DLSS",
                     "optipatcher": False, "notes": ""}],
            }))
            served.clear()
            await asyncio.gather(*(service.get_recommendation("Cached Game") for _ in range(3)))
            for _ in range(40):
                await asyncio.sleep(0.05)
                if not service._wiki_jobs:
                    break
            check("three questions in a row are one download, not three",
                  len(served) == 1, len(served))

            # The Forza Horizon 5 case: a game whose own wiki page has never
            # been cached, on a Deck whose route to the wiki stalls rather than
            # refusing. The page used to be fetched in front of the answer, so
            # the setup tab sat on "Checking the OptiScaler wiki…" for ever —
            # while a game whose page happened to be cached answered fine.
            shutil.rmtree(service.wiki.page_cache, ignore_errors=True)
            service.wiki.page_cache.mkdir(parents=True, exist_ok=True)
            service.wiki.list_cache.write_text(json.dumps({
                "fetched_at": time.time(),
                "entries": [{"name": "Cached Game", "key": "cachedgame",
                             "page": "Cached-Game", "compatibility": "OK",
                             "inputs": "DLSS", "optipatcher": False, "notes": ""}],
            }))

            def stalls(url, *a, **k):
                served.append(url)
                # Long enough that answering at all proves nothing waited for
                # it, short enough that the attempt finishes inside this test.
                time.sleep(1)
                raise TimeoutError("stalled")

            wiki_mod._http_get = stalls
            started = time.monotonic()
            answer = await service.get_recommendation("Cached Game")
            elapsed = time.monotonic() - started
            check("a game whose wiki page was never cached still answers at once",
                  elapsed < 0.3, f"{elapsed:.2f}s")
            check("and says the fuller answer is still coming",
                  answer["matched"] and answer["detail_pending"] is True)
            for _ in range(200):
                await asyncio.sleep(0.05)
                if not service._wiki_jobs:
                    break
            # And having failed once, it stops claiming a refresh is running —
            # a page the wiki will not serve kept one permanently in flight,
            # which is how "Reading this game's entry…" became permanent.
            await service.get_recommendation("Cached Game")
            await asyncio.sleep(0.1)
            check("a page that will not download stops being re-attempted",
                  not service._wiki_jobs, service._wiki_jobs)

            # A pinned entry's answer has to be comparable with the status the
            # UI watches. It used to invent its own metadata with no revision
            # in it, so every check read as "something changed" and the plan
            # reloaded every two seconds for ever.
            await service.set_wiki_entry(str(target), "Cached Game")
            pinned = await service.get_recommendation("Cached Game", game_path=str(target))
            state = await service.wiki_status()
            check("a pinned entry carries the same revision the watch compares",
                  pinned["list_meta"].get("revision") == state["revision"],
                  f"{pinned['list_meta'].get('revision')} vs {state['revision']}")
            check("and still says it was pinned by hand",
                  pinned["list_meta"]["source"] == "manual")
            await service.set_wiki_entry(str(target), None)
        finally:
            wiki_mod._http_get = real_get

        print("\nMonitoring")
        (target / "OptiScaler.log").write_text(
            "[12:00:01.1] [I] Running on Wine 9.0-GE!\n"
            "[12:00:01.2] [I] OptiScaler working as winmm.dll, system dll loaded\n"
            "[12:00:02.0] [I] Adapter Desc: AMD Custom GPU 0405\n"
            "[12:00:05.0] [I] Creating new FSR 4.0.2 upscaler\n"
            "[12:00:06.0] [I] XeFG swapchain created\n"
            "[12:00:07.0] [E] Something broke\n"
        )
        report = await service.get_monitor(str(target))
        check("upscaler read from log", report["state"]["upscaler"] == "FSR 4.0.2",
              report["state"]["upscaler"])
        check("frame generation path detected", report["frame_generation"] == ["XeFG"],
              report["frame_generation"])
        check("errors counted", report["counts"].get("error") == 1)
        check("configured values reported", report["configured"]["fg_enabled"] == "true")

        print("\nUninstall")
        removal = await service.uninstall(str(target))
        check("uninstall succeeded", removal["ok"])
        check("no install remains", not installer.detect(str(target))["installed"])
        check("game folder restored to original contents",
              sorted(p.name for p in target.iterdir())
              == ["Cyberpunk2077.exe", "Licenses", "amd_fidelityfx_upscaler_dx12.dll", "dxgi.dll"],
              sorted(p.name for p in target.iterdir()))
        check("original dll byte-identical", (target / "dxgi.dll").read_bytes() == original)
        check("game's own FidelityFX dll restored byte-identical",
              (target / "amd_fidelityfx_upscaler_dx12.dll").read_bytes() == game_upscaler)
        check("game's own Licenses folder restored",
              (target / "Licenses" / "game.txt").read_text() == "GAME LICENSE")
        check("backup folder cleaned up", not stash.exists())
        check("the launch-options record goes with the install",
              not installer.detect(str(target))["launch_record"]["recorded"])
        check("imported FSR4 files removed too",
              not (target / "amdxcffx64.dll").exists())
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _accepts(field, value):
    try:
        field.encode(value)
        return True
    except (TypeError, ValueError):
        return False


def check_live_control():
    """The live-control channel: field mapping, wire format, install/removal.

    The mapping check matters most -- the ASI writes into OptiScaler's memory at
    offsets derived from the member names listed here, so a name that no longer
    exists in the generated mirror would be a silent no-op at best.
    scripts/test_live_asi.py runs the same channel against the real OptiScaler.
    """
    import json
    import re
    from unittest import mock
    from optiscaler import live

    print("\nLive in-game control")

    mirror = ROOT / "asi" / "generated" / "config_mirror.h"
    check("generated config mirror exists", mirror.is_file())
    kinds = dict(re.findall(r"^    X\((\w+), \w+, OPTI_(\w+)\)", mirror.read_text(), re.M)) \
        if mirror.is_file() else {}
    missing = sorted(f.member for f in live.LIVE_FIELDS.values() if f.member not in kinds)
    check("every live field exists in the generated mirror", not missing, missing)
    wire = {"BOOL": "bool", "I32": "i32", "U32": "u32", "U8": "u8", "FLOAT": "f32"}
    mistyped = sorted(f.member for f in live.LIVE_FIELDS.values()
                      if f.member in kinds and wire.get(kinds[f.member]) != f.wire_type)
    check("every live field is sent as the type the ASI writes it with", not mistyped, mistyped)

    ini_map = json.loads((ROOT / "py_modules" / "optiscaler" / "live_fields.json").read_text())["fields"]
    mismatched = sorted(
        key for key, field in live.LIVE_FIELDS.items()
        if ini_map.get(key, {}).get("member") != field.member
    )
    check("every live field maps to the ini key OptiScaler reads it from",
          not mismatched, mismatched)
    # Switching to another backend or FFX frame generator takes OptiScaler's
    # State internals, which the ASI does not touch: those stay restart-only.
    check("the upscaler backends and the ffx frame generator are not live",
          not any(key in live.LIVE_FIELDS for key in (
              "Upscalers.Dx12Upscaler", "Upscalers.Dx11Upscaler", "Upscalers.VulkanUpscaler",
              live.FFX_FG_ID)))

    root = Path(tempfile.mkdtemp(prefix="optiscaler-live-"))
    try:
        target = root / "game"
        target.mkdir()
        state = live.status(str(target))
        check("no live channel before install", not state["attached"] and state["state"] == "absent")

        def consume():
            """The ASI applying the command file: its status names that seq."""
            seq = (target / live.CMD_FILE).read_text().split()[1]
            (target / live.STATUS_FILE).write_text(
                f"schema {live.STATUS_SCHEMA}\nstatus ready\nseq {seq}\napplied 1\nerror \n")

        fake_asi = root / "fake.asi"
        fake_asi.write_bytes(b"MZ fake")
        check("asi installs into the plugins folder",
              live.install_asi(str(target), fake_asi)["ok"]
              and (target / "plugins" / live.ASI_NAME).is_file())

        result = live.apply_changes(str(target), [
            {"section": "FrameGen", "key": "Enabled", "value": "false"},
            {"section": "Sharpness", "key": "Sharpness", "value": "0.5"},
            {"section": "Upscalers", "key": "Dx12Upscaler", "value": "fsr31"},
            {"section": "Hotfix", "key": "DisableOverlays", "value": "true"},
        ])
        body = (target / live.CMD_FILE).read_text().splitlines()
        check("live-settable keys are sent", result["applied"] == ["FGEnabled", "Sharpness"],
              result["applied"])
        check("restart-only keys are reported as deferred",
              result["deferred"] == ["Upscalers.Dx12Upscaler", "Hotfix.DisableOverlays"],
              result["deferred"])
        check("command file is the documented wire format",
              body[0].startswith("seq ") and body[1:] == ["set FGEnabled bool 0", "set Sharpness f32 0.5"],
              body)
        consume()
        # On is OptiScaler's own FG key: its menu pairs the write with State's
        # fgChanged, which (re)activates a generator fed by the game.
        fg_on = live.apply_changes(str(target), [{"section": "FrameGen", "key": "Enabled", "value": "true"}])
        check("frame generation on is left to OptiScaler's FG key, not written",
              fg_on["keys"] == [{"ini_key": "FrameGen.Enabled", "shortcut": "FGShortcutKey"}]
              and fg_on["applied"] == [] and not fg_on["deferred"], fg_on)
        check("HUD fix is not live (OptiScaler's menu also rebuilds the generator for it)",
              "OptiFG.HUDFix" not in live.LIVE_FIELDS)
        check("a plain setting asks for no upscaler re-init", not result["reinit"])

        # The FSR version is read when the upscaler creates its context: written,
        # then re-created through OptiPluginApi::ReInitUpscalers.
        ups = live.apply_changes(str(target), [
            {"section": live.FFX_UPSCALER_SECTION, "key": live.FFX_UPSCALER_KEY, "value": "1"},
            {"section": "Menu", "key": "ShowFps", "value": "true"},
        ])
        ups_body = (target / live.CMD_FILE).read_text().splitlines()
        check("an ffx upscaler version change is written, then the upscaler re-created",
              ups_body[1:] == ["set FfxUpscalerIndex i32 1", "set ShowFps bool 1", "reinit"]
              and ups["reinit"], ups_body)
        consume()
        check("without ReInitUpscalers it waits for the next launch",
              live.apply_changes(str(target), [
                  {"section": live.FFX_UPSCALER_SECTION, "key": live.FFX_UPSCALER_KEY, "value": "1"}],
                  can_reinit=False)["deferred"] == [live.FFX_UPSCALER_ID])
        check("an index OptiScaler could not have is refused rather than clamped",
              not _accepts(live.LIVE_FIELDS[live.FFX_UPSCALER_ID], "99")
              and not _accepts(live.LIVE_FIELDS[live.FFX_UPSCALER_ID], "-1"))
        check("'auto' is left for OptiScaler to resolve at the next launch",
              live.apply_changes(str(target), [
                  {"section": "FrameGen", "key": "Enabled", "value": "auto"}])["deferred"]
              == ["FrameGen.Enabled"])
        # The FSR 4 model and the init flags: what the overlay changes live by
        # writing the value and rebuilding the upscaler on the same backend.
        model = live.apply_changes(str(target), [
            {"section": "FSR", "key": "Fsr4Model", "value": "3"},
            {"section": "InitFlags", "key": "HDR", "value": "auto"},
        ])
        model_body = (target / live.CMD_FILE).read_text().splitlines()
        check("an FSR 4 model change is written, then the upscaler re-created",
              model_body[1:] == ["set Fsr4Model u8 3", "unset HDR", "reinit"]
              and model["applied"] == ["Fsr4Model", "HDR"] and not model["deferred"], model_body)
        consume()
        check("'auto' for a resettable setting unsets it, as the overlay's Auto does",
              live.apply_changes(str(target), [
                  {"section": "FSR", "key": "Fsr4Model", "value": "auto"}])["applied"] == ["Fsr4Model"])
        check("an FSR 4 model OptiScaler does not have is refused",
              not _accepts(live.LIVE_FIELDS["FSR.Fsr4Model"], "4"))
        check("every setting that needs the upscaler rebuilt waits without ReInitUpscalers",
              live.apply_changes(str(target), [
                  {"section": "FSR", "key": "Fsr4Model", "value": "auto"},
                  {"section": "InitFlags", "key": "AutoExposure", "value": "true"}],
                  can_reinit=False)["deferred"] == ["FSR.Fsr4Model", "InitFlags.AutoExposure"])
        check("a change with nothing live-settable sends nothing",
              live.apply_changes(str(target), [
                  {"section": "Hotfix", "key": "DisableOverlays", "value": "true"}])["sent"] is False)

        # Two pushes inside one ASI poll (100 ms): the ASI applies only the
        # newest file, so the first must travel inside the second (upstream's
        # audit CD-03: it used to be dropped while both reported sent).
        consume()
        first = live.apply_changes(str(target), [
            {"section": "Sharpness", "key": "Sharpness", "value": "0.3"},
            {"section": "FSR", "key": "Fsr4Model", "value": "2"}])
        second = live.apply_changes(str(target), [
            {"section": "Sharpness", "key": "Sharpness", "value": "0.7"},
            {"section": "Menu", "key": "ShowFps", "value": "true"}])
        merged = (target / live.CMD_FILE).read_text().splitlines()
        check("a command the game has not applied yet is carried into the next one",
              merged[1:] == ["set Sharpness f32 0.7", "set Fsr4Model u8 2", "set ShowFps bool 1", "reinit"],
              merged)
        check("and the newer command has the higher seq", second["seq"] > first["seq"],
              (first["seq"], second["seq"]))
        consume()
        live.apply_changes(str(target), [{"section": "Menu", "key": "ShowFps", "value": "false"}])
        check("once the game has applied it, the next command holds only its own change",
              (target / live.CMD_FILE).read_text().splitlines()[1:] == ["set ShowFps bool 0"])
        consume()
        with mock.patch.object(live.time, "time_ns", return_value=5_000_000_000_000):
            a = live._send(str(target), ["set Sharpness f32 0.1"])
            consume()
            b = live._send(str(target), ["set Sharpness f32 0.2"])
        check("two commands in the same millisecond still get different seqs", b > a, (a, b))
        consume()
        import threading as _threading
        errors = []

        def push(index):
            try:
                live._send(str(target), [f"set Member{index} i32 {index}"])
            except Exception as exc:  # what a shared temp file used to raise
                errors.append(repr(exc))
        workers = [_threading.Thread(target=push, args=(i,)) for i in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        held = (target / live.CMD_FILE).read_text().splitlines()[1:]
        check("concurrent pushes neither fail nor lose each other",
              not errors and sorted(held) == sorted(f"set Member{i} i32 {i}" for i in range(8)),
              errors or held)
        check("and leave no temporary files behind",
              not [p.name for p in target.iterdir() if p.name.endswith(".tmp")])
        consume()

        (target / live.DUMP_FILE).write_text(
            "fields 2\nSharpness f32 1 0.5 0.400000006 -\nDx12Upscaler i32 0 - 0 -\n")
        dumped = live.read_dump(str(target))
        check("the field dump is read back per member",
              dumped["Sharpness"] == ("f32", True, "0.5", "0.400000006", "-")
              and dumped["Dx12Upscaler"][1] is False, dumped)

        live.remove_asi(str(target))
        check("removal takes the asi and its control files",
              not (target / "plugins" / live.ASI_NAME).exists()
              and not (target / live.CMD_FILE).exists()
              and not (target / live.DUMP_FILE).exists()
              and not (target / "plugins").exists())
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_live_diagnostics():
    """The three separate reasons live control can be silently absent.

    "Not connected" on its own is useless to the user, and each of these has a
    different fix: the plugin is missing, OptiScaler is not set to load plugins
    at all (it defaults to off), or it loaded and could not find what it needed.
    """
    import tempfile
    from optiscaler import live

    print("\nLive control diagnostics")
    root = Path(tempfile.mkdtemp(prefix="optiscaler-diag-"))
    try:
        target = root / "game"
        target.mkdir()

        check("no ini means the load switch is unknown", live.load_enabled(str(target)) is None)
        (target / "OptiScaler.ini").write_text("[Plugins]\nLoadAsiPlugins=auto\n")
        check("the switch defaults to off, and is reported as off",
              live.load_enabled(str(target)) is False)
        (target / "OptiScaler.ini").write_text("[Plugins]\nLoadAsiPlugins=true\n")
        check("the switch is seen once it is on", live.load_enabled(str(target)) is True)
        # It must not match the same key in another section.
        (target / "OptiScaler.ini").write_text("[Menu]\nLoadAsiPlugins=true\n")
        check("the switch is only read from the Plugins section",
              live.load_enabled(str(target)) is False)

        check("no log means it is unknown whether OptiScaler loaded it",
              live.loaded_by_optiscaler(str(target)) is None)
        (target / "OptiScaler.log").write_text("[info] Checking C:\\game\\plugins for *.asi\n")
        check("a searched-but-empty plugins folder is reported as not loaded",
              live.loaded_by_optiscaler(str(target)) is False)
        (target / "OptiScaler.log").write_text(
            f"[info] Loaded: C:\\game\\plugins\\{live.ASI_NAME}\n")
        check("OptiScaler's own log confirms the load",
              live.loaded_by_optiscaler(str(target)) is True)

        # The heartbeat is what makes staleness mean "the game is gone" rather
        # than "nobody changed a setting recently".
        check("the staleness window is short enough to track a live game",
              live.STATUS_STALE_SECONDS <= 30, live.STATUS_STALE_SECONDS)

        status_file = target / live.STATUS_FILE
        status_file.write_text(
            f"schema {live.STATUS_SCHEMA}\nstatus ready\napi 3\nreinit_available 1\nseq 7\n"
            "applied 2\nreinit -1\nconfig 0x1\nfields 377\ndir C:\\game\nerror \n")
        report = live.status(str(target))
        check("a fresh heartbeat reads as attached and ready", report["attached"] and report["ready"])
        check("the plugin API version is carried through", report["api"] == 3, report["api"])
        check("ReInitUpscalers makes the FSR version live", report["can_reinit"]
              and report["can_change_ffx_upscaler"])
        check("the last applied command is carried through", report["seq"] == "7", report["seq"])

        old = time.time() - (live.STATUS_STALE_SECONDS + 30)
        os.utime(status_file, (old, old))
        check("a stale heartbeat reads as gone", not live.status(str(target))["attached"])

        status_file.write_text(f"schema {live.STATUS_SCHEMA}\nstatus searching\napi 3\nerror \n")
        searching = live.status(str(target))
        check("still looking for Config is attached but not ready",
              searching["attached"] and not searching["ready"])
        status_file.write_text(f"schema {live.STATUS_SCHEMA}\nstatus failed\napi 3\n"
                               "error the ASI's layout does not match this build\n")
        failed = live.status(str(target))
        check("a layout mismatch is neither attached nor ready, and says why",
              not failed["attached"] and not failed["ready"] and "layout" in failed["error"])
        status_file.write_text(f"schema {live.STATUS_SCHEMA}\nstatus ready\napi 0\nreinit_available 0\n")
        old_api = live.status(str(target))
        check("an OptiScaler without the plugin API is ready, without re-init",
              old_api["ready"] and not old_api["can_reinit"])
        # A status file from upstream's 0.9.x plugin (schema 5 and earlier).
        status_file.write_text("schema 5\nstatus ready\nseq 1\nbackends 0x3\nerror \n")
        legacy = live.status(str(target))
        check("another plugin build's status file is not taken as ready",
              legacy["attached"] and not legacy["ready"])
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_reported_folder():
    """The folder the in-game plugin reports, against the one being managed.

    The plugin reports a Windows path and Proton's drive letters are what make
    this more than a string compare. ``Z:`` is the filesystem root, so that case
    shares a tail outright -- but a library on the SD card gets its own letter,
    and ``S:\\steamapps\\common\\...`` shares no tail at all with
    ``/run/media/.../steamapps/common/...``. A healthy install on a card
    therefore reported itself as writing to the wrong folder, in a warning that
    told the user to reinstall something that was working.
    """
    from optiscaler.live import _same_dir

    print("\nThe folder the in-game plugin reports")
    check("a library on the SD card matches through its drive letter",
          _same_dir("S:\\steamapps\\common\\Expedition 33\\Sandfall\\Binaries\\Win64",
                    "/run/media/mmcblk0p1/steamapps/common/Expedition 33/"
                    "Sandfall/Binaries/Win64") is True)
    check("and so does internal storage, where Z: is the root",
          _same_dir("Z:\\home\\deck\\.steam\\steam\\steamapps\\common\\Game\\Bin",
                    "/home/deck/.steam/steam/steamapps/common/Game/Bin") is True)
    check("a genuinely different game is still a mismatch",
          _same_dir("S:\\steamapps\\common\\Other\\Binaries\\Win64",
                    "/run/media/mmcblk0p1/steamapps/common/Expedition 33/"
                    "Sandfall/Binaries/Win64") is False)
    # Whole components only: "Win64" is every Unreal game's last folder, and
    # one shared name is not a shared path.
    check("one shared folder name is not a match",
          _same_dir("S:\\somewhere\\Win64", "/games/Expedition 33/Win64") is False)
    check("a path that says nothing gives no answer",
          _same_dir("", "/games/x") is None)


def check_asi_staleness():
    """A game keeps the in-game plugin it was set up with, for ever.

    Updating the Decky plugin copies a new ASI into ``bin/`` and nothing else:
    every game still holds the build it was set up with, which attaches,
    heartbeats and answers exactly like a current one -- it simply has nothing
    to say about fields added since. That is indistinguishable from a feature
    that was never built, which is precisely how it was reported.
    """
    import tempfile
    from optiscaler import live

    print("\nAn out-of-date in-game plugin")
    root = Path(tempfile.mkdtemp(prefix="optiscaler-asi-"))
    try:
        target = root / "game"
        (target / live.PLUGIN_SUBDIR).mkdir(parents=True)
        shipped = root / "bin" / live.ASI_NAME
        shipped.parent.mkdir()
        shipped.write_bytes(b"MZ" + b"new" * 40)

        check("nothing to compare against is unknown, not out of date",
              live.asi_current(str(target), None) is None)
        check("and neither is a game that has no plugin at all",
              live.asi_current(str(target), str(shipped)) is None)

        installed = live.asi_path(str(target))
        installed.write_bytes(b"MZ" + b"old" * 40)
        check("a same-sized older build is still caught",
              live.asi_current(str(target), str(shipped)) is False)
        installed.write_bytes(b"MZ" + b"old" * 10)
        check("so is one of a different size",
              live.asi_current(str(target), str(shipped)) is False)
        installed.write_bytes(shipped.read_bytes())
        check("the shipped build compares equal",
              live.asi_current(str(target), str(shipped)) is True)

        # The shipped bytes are cached against the source file's stat, so a
        # rebuilt ASI has to invalidate it -- otherwise every game reads as
        # current for the rest of the session.
        import os
        shipped.write_bytes(b"MZ" + b"newer" * 40)
        os.utime(shipped, ns=(0, 0))
        check("a rebuilt plugin is noticed rather than served from the cache",
              live.asi_current(str(target), str(shipped)) is False)

        report = live.status(str(target), str(shipped))
        check("and the status report carries the answer",
              report["asi_current"] is False, report["asi_current"])
        check("a report asked without a source says nothing either way",
              live.status(str(target))["asi_current"] is None)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_asi_reporting():
    """The in-game plugin's shape: the plugin API, and nothing of State.

    The 0.9.x plugin located and wrote OptiScaler's State by memory patterns
    that each nightly can move. This one must not: it gets OptiScaler's module
    and the upscaler re-init from OptiScaler's own plugin API, and writes only
    Config, whose layout is generated from the bundled nightly's Config.h.
    """
    print("\nASI shape")
    source = (ROOT / "asi" / "live.cpp").read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("//"))
    check("it exports OptiPluginInit, the plugin API entry point",
          'extern "C" __declspec(dllexport) bool OptiPluginInit(' in code)
    check("it re-creates the upscaler through the API", "api->ReInitUpscalers(g_plugin)" in code
          and "OPTI_API_HAS(api, ReInitUpscalers)" in code)
    check("it touches nothing of OptiScaler's State",
          not any(word in code for word in ("changeBackend", "newBackend", "FGchanged", "state_tail")))
    check("the status file carries the plugin API version and the re-init capability",
          '"api %u\\n"' in code and '"reinit_available %d\\n"' in code)
    check("it gives up, reporting why, when Config does not validate",
          'strcpy(g_status, "failed");' in code and "layout does not match" in code)
    sdk = (ROOT / "asi" / "plugin_sdk" / "optiscaler_plugin.h").read_text(encoding="utf-8")
    check("the vendored plugin SDK has ReInitUpscalers", "ReInitUpscalers" in sdk)


def check_remembered_choices():
    """Answers the user asked to be remembered have to survive a restart.

    The launch-options question is the one that matters: without the override
    Proton loads its own DLL and OptiScaler never runs, so the install offers to
    set it - but only until the user says "remember my choice".
    """
    import tempfile
    from optiscaler.settings import Settings

    print("\nRemembered choices")
    root = Path(tempfile.mkdtemp(prefix="optiscaler-prefs-"))
    try:
        path = root / "settings.json"
        settings = Settings(path)
        check("an unanswered question has no stored answer",
              settings.get_pref("launch_options") is None)
        settings.set_pref("launch_options", "always")
        check("the answer survives a reload",
              Settings(path).get_pref("launch_options") == "always")
        settings.set_pref("launch_options", None)
        check("forgetting it puts the question back",
              Settings(path).get_pref("launch_options") is None)
        check("prefs do not disturb the rest of the file",
              Settings(path).get("custom_libraries") == [])
        # "Remember my choices" is stored as a boolean and defaults to on, so
        # False has to survive as an answer rather than reading as "unset" —
        # otherwise turning remembering off would silently turn itself back on.
        settings.set_pref("remember_choices", False)
        check("switching remembering off is itself remembered",
              Settings(path).get_pref("remember_choices", True) is False)
        check("an untouched switch defaults to on",
              Settings(path).get_pref("never_asked", True) is True)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_launch_record():
    """Removing OptiScaler has to be able to undo the launch options too.

    Three states have to stay distinguishable, because each means a different
    thing on the removal dialog: no record at all (an older install, or a Steam
    build that would not report them - take out only our own override), a
    recorded empty string (the game genuinely had none - the field can be
    cleared outright), and a recorded value (put exactly that back).
    """
    import tempfile

    print("\nLaunch-options record")
    root = Path(tempfile.mkdtemp(prefix="optiscaler-launch-"))
    try:
        check("nothing recorded reads as nothing recorded",
              installer.read_launch_record(root) == {"recorded": False, "value": ""})

        installer.record_launch_options(root, "")
        check("an empty record is still a record — the game had none",
              installer.read_launch_record(root) == {"recorded": True, "value": ""})

        installer.record_launch_options(root, "mangohud %command%")
        check("write-once: the first answer is the one kept",
              installer.read_launch_record(root)["value"] == "",
              installer.read_launch_record(root)["value"])

        installer.forget_launch_options(root)
        installer.record_launch_options(root, 'PROTON_LOG=1 %command% -dx12')
        check("a real value round trips verbatim",
              installer.read_launch_record(root)["value"] == 'PROTON_LOG=1 %command% -dx12')
        check("the file is plain text next to the install",
              (root / installer.LAUNCH_RECORD_NAME).is_file())

        check("forgetting reports whether there was anything to forget",
              installer.forget_launch_options(root) is True
              and installer.forget_launch_options(root) is False)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_launch_options_read():
    """Steam's own config is the answer when the client will not give one.

    `SteamClient.Apps.GetAppLaunchOptions` is missing from some client builds
    outright, and the first version of the removal dialog leaned on it alone:
    on those Decks it recorded nothing at install time and offered nothing at
    removal, so OptiScaler's own override was left behind and the whole feature
    looked like it had never shipped. This is the source that does not depend
    on an undocumented method existing.
    """
    import tempfile
    from optiscaler import steam as steam_mod

    print("\nLaunch options from Steam's config")
    root = Path(tempfile.mkdtemp(prefix="optiscaler-localconfig-"))
    try:
        home = root / "home"
        config = home / ".local" / "share" / "Steam" / "userdata" / "12345" / "config"
        config.mkdir(parents=True)
        (home / ".local" / "share" / "Steam" / "steamapps").mkdir(parents=True)
        (home / ".steam").mkdir(parents=True, exist_ok=True)
        os.symlink(home / ".local" / "share" / "Steam", home / ".steam" / "steam")

        check("nothing readable is not the same as nothing set",
              steam_mod.launch_options(home, "1091500") == {"found": False, "value": "",
                                                            "source": None})

        (config / "localconfig.vdf").write_text(
            '"UserLocalConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n'
            '\t\t\t"Steam"\n\t\t\t{\n\t\t\t\t"apps"\n\t\t\t\t{\n'
            '\t\t\t\t\t"1091500"\n\t\t\t\t\t{\n'
            '\t\t\t\t\t\t"LaunchOptions"\t\t"mangohud %command% -dx12"\n'
            '\t\t\t\t\t}\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n'
        )
        found = steam_mod.launch_options(home, "1091500")
        check("the launch options are read out of localconfig.vdf",
              found["found"] and found["value"] == "mangohud %command% -dx12", found["value"])
        absent = steam_mod.launch_options(home, "2358720")
        check("a game with no entry has none, which is an answer not a gap",
              absent["found"] and absent["value"] == "", absent)

        # Older and newer clients disagree on the capitalisation of the path,
        # and a lookup that guesses wrong reads as "this user has no games".
        (config / "localconfig.vdf").write_text(
            '"userlocalconfigstore" { "software" { "valve" { "steam" { "apps" {'
            ' "1091500" { "launchoptions" "PROTON_LOG=1 %command%" } } } } } }'
        )
        check("the path is matched whatever its capitalisation",
              steam_mod.launch_options(home, "1091500")["value"] == "PROTON_LOG=1 %command%")

        # An override with quotes in it is the exact case this has to survive.
        (config / "localconfig.vdf").write_text(
            '"UserLocalConfigStore" { "Software" { "Valve" { "Steam" { "apps" { "1091500" {'
            ' "LaunchOptions" "WINEDLLOVERRIDES=\\"dxgi=n,b\\" %command%" } } } } } }'
        )
        check("an escaped quote in the value survives the round trip",
              steam_mod.launch_options(home, "1091500")["value"]
              == 'WINEDLLOVERRIDES="dxgi=n,b" %command%',
              steam_mod.launch_options(home, "1091500")["value"])
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_non_steam_shortcut():
    """A game Steam runs but has no install folder for.

    Everything else here starts from an app manifest, and a shortcut added by
    hand has none: Steam stores a command line and nothing more. Asking it for
    the install directory got no answer, so the library context menu's
    OptiScaler Settings entry ended on "Steam did not report an install folder"
    for every non-Steam game — the panel, Now Playing and the page all failed
    on it identically, because all three come through one lookup. The folder
    the shortcut's target sits in is the answer.
    """
    import struct
    import tempfile
    import zlib
    from optiscaler import steam as steam_mod

    print("\nNon-Steam shortcuts")

    def encode(entries):
        """shortcuts.vdf as Steam writes it: binary KeyValues."""
        def field(marker, key, payload):
            return bytes([marker]) + key.encode() + b"\x00" + payload

        out = b""
        for index, entry in enumerate(entries):
            body = b""
            for key, value in entry.items():
                if isinstance(value, int):
                    body += field(0x02, key, struct.pack("<i", value))
                else:
                    body += field(0x01, key, value.encode() + b"\x00")
            out += field(0x00, str(index), body + b"\x08")
        return field(0x00, "shortcuts", out + b"\x08") + b"\x08"

    # Resolved, because the folder is: a shortcut's target is canonicalised
    # before it is handed back, so the temp dir has to be compared the same way.
    root = Path(tempfile.mkdtemp(prefix="optiscaler-shortcut-")).resolve()
    try:
        home = root / "home"
        steam_root = home / ".local" / "share" / "Steam"
        config = steam_root / "userdata" / "12345" / "config"
        config.mkdir(parents=True)
        (steam_root / "steamapps").mkdir(parents=True)
        (home / ".steam").mkdir(parents=True, exist_ok=True)
        os.symlink(steam_root, home / ".steam" / "steam")

        game = root / "Games" / "Some Launcher Game" / "Binaries" / "Win64"
        game.mkdir(parents=True)
        (game / "Game-Win64-Shipping.exe").write_bytes(b"MZ")

        # Steam stores the id signed and uses it unsigned; every shortcut it
        # has ever made has the top bit set, so the two always disagree.
        signed = -1234567890
        unsigned = str(signed & 0xFFFFFFFF)
        (config / "shortcuts.vdf").write_bytes(encode([
            {"appid": signed,
             "AppName": "Some Launcher Game",
             "Exe": f'"{game / "Game-Win64-Shipping.exe"}"',
             "StartDir": f'"{game}"'},
        ]))

        found = steam_mod.find_by_appid(home, unsigned)
        check("a shortcut is found by the id the library shows, not the stored one",
              found and found["path"] == str(game), found)
        check("and keeps the name Steam displays rather than the folder's",
              found and found["name"] == "Some Launcher Game", found)
        check("and is marked as a shortcut, not a Steam install",
              found and found["source"] == "shortcut", found)
        check("the signed form is not what the library asks with",
              steam_mod.find_by_appid(home, str(signed)) is None)

        # The client answers before the file does: shortcuts.vdf is flushed on
        # Steam's schedule, so a shortcut added this session is not in it yet.
        other = root / "Games" / "Added Just Now"
        other.mkdir(parents=True)
        (other / "game.exe").write_bytes(b"MZ")
        hinted = steam_mod.find_by_appid(home, "4200000000",
                                         exe=f'"{other / "game.exe"}"', name="Added Just Now")
        check("a target the client supplied resolves with nothing on disk to read",
              hinted and hinted["path"] == str(other), hinted)

        # A quoted path is how Steam stores every one of them, and a target
        # that has moved is not a folder to go writing into.
        check("a target that no longer exists is refused rather than guessed at",
              steam_mod.shortcut_folder('"/nowhere/at/all/game.exe"') is None)
        check("but its start directory is taken when that does exist",
              steam_mod.shortcut_folder('"/nowhere/at/all/game.exe"', f'"{game}"')
              == str(game))

        # Launch options for a shortcut are in this file, not localconfig.vdf,
        # where it has no entry at all. Reading the wrong one answers "this
        # game has none" — the state that lets removal clear the field — so a
        # wrapper command would have been deleted along with our override.
        (config / "shortcuts.vdf").write_bytes(encode([
            {"appid": signed,
             "AppName": "Some Launcher Game",
             "Exe": f'"{game / "Game-Win64-Shipping.exe"}"',
             "StartDir": f'"{game}"',
             "LaunchOptions": 'WINEDLLOVERRIDES="dxgi=n,b" mangohud %command%'},
        ]))
        options = steam_mod.launch_options(home, unsigned)
        check("a shortcut's launch options come out of shortcuts.vdf",
              options["found"] and options["value"]
              == 'WINEDLLOVERRIDES="dxgi=n,b" mangohud %command%', options)
        check("and an id that is in no shortcuts file is not called empty",
              steam_mod.launch_options(home, "4200000000")
              == {"found": False, "value": "", "source": None},
              steam_mod.launch_options(home, "4200000000"))

        # Very old entries predate the appid field; Steam derives one.
        legacy_exe = f'"{game / "Game-Win64-Shipping.exe"}"'
        legacy_id = str((zlib.crc32((legacy_exe + "Legacy Game").encode()) & 0xFFFFFFFF)
                        | 0x80000000)
        (config / "shortcuts.vdf").write_bytes(encode([
            {"AppName": "Legacy Game", "Exe": legacy_exe, "StartDir": f'"{game}"'},
        ]))
        check("an entry with no stored appid is matched by the one Steam derives",
              (steam_mod.find_by_appid(home, legacy_id) or {}).get("path") == str(game))

        # Fails closed: a file that is not this format must not half-decode
        # into a folder this plugin then writes OptiScaler into.
        (config / "shortcuts.vdf").write_bytes(b"\x00shortcuts\x00\x09junk")
        check("an unreadable shortcuts file yields nothing rather than a guess",
              steam_mod.find_by_appid(home, unsigned) is None)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_wiki_failure_reporting():
    """A list that will not download must not read as "your game is not on it".

    Both produced an empty result and the UI showed the same sentence for each,
    so a network fault presented as every game in the library being unknown to
    the wiki — with nothing anywhere saying otherwise and no way to retry.
    `status()` is the answer to which of the two it is.
    """
    import tempfile
    from optiscaler import wiki as wiki_mod

    print("\nWiki failure reporting")
    root = Path(tempfile.mkdtemp(prefix="optiscaler-wiki-"))
    real_get = wiki_mod._http_get
    try:
        client = wiki_mod.WikiClient(root / "cache")

        wiki_mod._http_get = lambda *a, **k: (_ for _ in ()).throw(
            urllib.error.URLError("Network is unreachable"))
        entries, meta = client.load_entries(True)
        check("an unreachable wiki yields no entries", entries == [])
        check("and says so in the words of what failed",
              "Network is unreachable" in (meta["error"] or ""), meta["error"])
        state = client.status()
        check("status reports the list as unavailable, not empty",
              not state["available"] and state["entry_count"] == 0)
        check("status carries the error the UI has to print",
              "Network is unreachable" in (state["error"] or ""))
        check("status names the address it could not reach",
              state["url"].endswith("Compatibility-List.md"), state["url"])

        # A list that does download is the other answer, and the difference is
        # the whole point.
        table = ("| Game | Compatibility | Inputs |\n|---|---|---|\n"
                 "| [Cyberpunk 2077](Cyberpunk-2077) | OK | DLSS |\n")
        wiki_mod._http_get = lambda *a, **k: table
        state = client.status(True)
        check("a list that downloads reports as available",
              state["available"] and state["entry_count"] == 1, state)
        check("and carries no error", state["error"] is None)

        # A fetch that succeeds but parses to nothing is a third case, and must
        # not be cached as if it were a real answer.
        wiki_mod._http_get = lambda *a, **k: "not a table at all"
        empty, meta = client.load_entries(True)
        check("a page that parses to nothing is reported, not cached",
              "zero rows" in (meta["error"] or ""), meta["error"])
        check("and the last good list is served instead of nothing",
              len(empty) == 1, len(empty))
    finally:
        wiki_mod._http_get = real_get
        shutil.rmtree(root, ignore_errors=True)


def check_wiki_cache():
    """Answers come from cache; the network is never in front of a question.

    A handheld is regularly asleep, offline, or on a network that resolves and
    does not route. Waiting for the wiki before answering meant every one of
    those cost a full timeout, and a cache older than a day was thrown away
    rather than used. Reading is stale-while-revalidate now: whatever is on
    disk is returned at once, and the refresh happens behind it.
    """
    import tempfile
    from optiscaler import wiki as wiki_mod

    print("\nWiki cache")
    root = Path(tempfile.mkdtemp(prefix="optiscaler-swr-"))
    real_get = wiki_mod._http_get
    calls = []

    def serve(table):
        def get(url, *a, **k):
            calls.append(url)
            return table
        return get

    one = ("| Game | Compatibility | Inputs |\n|---|---|---|\n"
           "| [Cyberpunk 2077](Cyberpunk-2077) | OK | DLSS |\n")
    two = one + "| [Black Myth: Wukong](Black-Myth-Wukong) | OK | DLSS |\n"

    try:
        seed = root / "seed.json"
        seed.write_text(json.dumps({
            "fetched_at": time.time(),
            "entries": [{"name": "Seeded Game", "key": "seededgame", "page": None,
                         "compatibility": "OK", "inputs": "DLSS", "optipatcher": False,
                         "notes": ""}],
        }))

        # A Deck that has never had a connection still has a list.
        wiki_mod._http_get = lambda *a, **k: (_ for _ in ()).throw(
            urllib.error.URLError("Network is unreachable"))
        offline = wiki_mod.WikiClient(root / "a", seed)
        entries, meta = offline.load_entries()
        check("an offline Deck falls back to the bundled list",
              len(entries) == 1 and meta["source"] == "bundled", meta["source"])
        check("and reports the list as available, not as a missing game",
              offline.status()["available"])

        # With no seed and no network there is genuinely nothing.
        bare = wiki_mod.WikiClient(root / "b")
        check("no cache and no network is still an empty list, reported as such",
              bare.load_entries()[0] == [] and not bare.status()["available"])

        # A cached list answers without touching the network at all.
        wiki_mod._http_get = serve(one)
        client = wiki_mod.WikiClient(root / "c", seed)
        client.revalidate()
        calls.clear()
        entries, meta = client.load_entries()
        check("a cached list is served with no request at all",
              len(entries) == 1 and calls == [], calls)
        check("and says which list it came from", meta["source"] == "cache")

        # Even when it is older than the TTL: stale is a reason to refresh
        # behind the answer, never a reason to wait for one.
        payload = json.loads(client.list_cache.read_text())
        payload["fetched_at"] = time.time() - (COMPAT_TTL := 60 * 60 * 24) - 60
        client.list_cache.write_text(json.dumps(payload))
        calls.clear()
        entries, meta = client.load_entries()
        check("a stale list is still served without waiting",
              len(entries) == 1 and calls == [], calls)
        check("and is flagged stale so a refresh gets started",
              meta["stale"] and client.is_stale())

        # The fingerprint is what the UI watches to know something arrived.
        before = client.revision()
        wiki_mod._http_get = serve(one)
        same = client.revalidate()
        check("a refresh that brings back the same list changes nothing",
              same["ok"] and not same["changed"] and client.revision() == before)
        wiki_mod._http_get = serve(two)
        changed = client.revalidate()
        check("a refresh that brings something new says so",
              changed["ok"] and changed["changed"] and changed["count"] == 2)
        check("and the fingerprint moves with it", client.revision() != before)

        # The one rule that keeps this from ever making things worse.
        good = client.revision()
        wiki_mod._http_get = lambda *a, **k: (_ for _ in ()).throw(
            urllib.error.URLError("Network is unreachable"))
        failed = client.revalidate()
        check("a failed refresh leaves the working cache alone",
              not failed["ok"] and client.revision() == good and
              len(client.load_entries()[0]) == 2)
        check("but the failure is remembered for the UI to show",
              "unreachable" in (client.last_error or ""), client.last_error)
        wiki_mod._http_get = serve(two)
        client.revalidate()
        check("and a later success clears it", client.last_error is None)

        # Detail pages follow the same rule: a stale one used to mean two HTTP
        # attempts in front of the answer, the second only after the first
        # timed out.
        wiki_mod._http_get = serve("== Cyberpunk\n|**Filename**\n|`dxgi.dll`\n")
        client.fetch_page("Cyberpunk-2077")
        os.utime(client._page_file("Cyberpunk-2077"),
                 (time.time() - COMPAT_TTL - 60,) * 2)
        calls.clear()
        page = client.load_page("Cyberpunk-2077")
        check("a stale detail page is served from disk, not waited for",
              page is not None and calls == [], calls)
        check("and is reported stale so it can be refreshed behind the answer",
              client.page_is_stale("Cyberpunk-2077"))

        # The hole the stale rule left: a page never downloaded at all was
        # still fetched in front of the answer, two URLs deep with the second
        # attempt only starting once the first had timed out. On a Deck with a
        # bad route to the wiki that is a lookup that never returns, and the
        # setup tab sat on "Checking the OptiScaler wiki…" for ever — for
        # exactly the games whose page had not happened to be cached already.
        calls.clear()
        missing = client.load_page("Never-Seen", allow_fetch=False)
        check("a page never downloaded is not fetched in front of the answer",
              missing is None and calls == [], calls)
        check("but it counts as stale, so it is fetched behind one",
              client.page_is_stale("Never-Seen"))

        # And the arrival has to be visible to the same watch that notices a
        # changed list, or the better answer never reaches the screen.
        before_page = client.revision()
        wiki_mod._http_get = serve("== Never seen\n|**Filename**\n|`winmm.dll`\n")
        client.fetch_page("Never-Seen")
        check("a page arriving moves the revision the UI watches",
              client.revision() != before_page,
              f"{before_page} -> {client.revision()}")
        after_page = client.revision()
        client.fetch_page("Never-Seen")
        check("re-fetching the same page moves nothing",
              client.revision() == after_page)

        # A page the wiki will not serve must not be asked for on every
        # question. Each attempt is two URLs deep, so re-attempting kept a
        # refresh permanently in flight — and the UI reads "a refresh is
        # running" as "the answer may still change", so it never stopped
        # waiting for one that was never going to arrive.
        wiki_mod._http_get = lambda *a, **k: (_ for _ in ()).throw(
            urllib.error.URLError("Network is unreachable"))
        check("a page that has never been seen is worth one attempt",
              client.page_needs_fetch("Refused-Page"))
        check("which is made", client.fetch_page("Refused-Page") is None)
        check("and not repeated on the next question",
              not client.page_needs_fetch("Refused-Page"))
        check("a page already cached is not asked for at all",
              not client.page_needs_fetch("Never-Seen"))
        check("nor is a game with no wiki page", not client.page_needs_fetch(None))
        # Coming back onto a network has to be noticed eventually.
        client._page_failed_at["Refused-Page"] = time.time() - wiki_mod.PAGE_RETRY_AFTER - 1
        check("but the cooldown does expire", client.page_needs_fetch("Refused-Page"))
    finally:
        wiki_mod._http_get = real_get
        shutil.rmtree(root, ignore_errors=True)


def check_wiki_transport():
    """The fetch has to survive the ways one Deck's network differs.

    A router that advertises IPv6 it cannot route is the classic case: the
    address resolves, nothing connects, and urllib has no Happy Eyeballs to
    fall back the way a browser would — so it sits on the first address until
    the timeout, every time, and the wiki never loads on that network while
    everything else works.
    """
    import socket as socket_mod
    from optiscaler import wiki as wiki_mod

    print("\nWiki transport")
    real_open = wiki_mod._open
    attempts = []

    def stalls_once(request, timeout, context, binary=False):
        attempts.append(socket_mod.getaddrinfo)
        if len(attempts) == 1:
            raise urllib.error.URLError(TimeoutError("timed out"))
        return "| Game | Compatibility |\n|---|---|\n| [X](X) | OK |\n"

    try:
        wiki_mod._open = stalls_once
        body = wiki_mod._http_get("https://example.invalid/x")
        check("a stalled connection is retried rather than given up on",
              body.startswith("| Game"), len(attempts))
        check("and the retry is the one that forces IPv4",
              attempts[1] is not attempts[0])
        check("the resolver is put back afterwards",
              socket_mod.getaddrinfo is attempts[0])


        # A server that answered is not retried: asking again says the same.
        attempts.clear()

        def refuses(request, timeout, context, binary=False):
            attempts.append(1)
            raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)

        wiki_mod._open = refuses
        try:
            wiki_mod._http_get("https://example.invalid/x")
            check("an HTTP error is not retried", False, "no error raised")
        except urllib.error.HTTPError:
            check("an HTTP error is not retried", len(attempts) == 1, len(attempts))
    finally:
        wiki_mod._open = real_open

    kinds = [kind for kind, _ in wiki_mod._candidate_contexts()]
    check("an unverified context is always the last resort",
          kinds and kinds[-1] == "unverified", kinds)


def check_wiki_tls():
    """TLS failures must fall through to the next CA source, not abort.

    urlopen wraps an SSLError in a URLError, so an `except ssl.SSLError` around
    it never fires -- the fallback contexts were unreachable and every lookup
    reported the game as absent from the compatibility list. No network needed
    to catch that: it is entirely about which exception is recognised.
    """
    import ssl
    import urllib.error
    from optiscaler import wiki

    print("\nWiki TLS")
    verify_failed = ssl.SSLCertVerificationError(
        "certificate verify failed: unable to get local issuer certificate")
    check("a bare SSLError counts as a tls failure", wiki._is_tls_error(verify_failed))
    check("the URLError urlopen actually raises counts too",
          wiki._is_tls_error(urllib.error.URLError(verify_failed)))
    check("an ordinary network error does not",
          not wiki._is_tls_error(urllib.error.URLError(OSError("unreachable"))))
    check("a timeout does not", not wiki._is_tls_error(TimeoutError()))

    kinds = [kind for kind, _ in wiki._candidate_contexts()]
    check("more than one CA source is tried", len(kinds) > 1, kinds)
    check("an unverified attempt is the last resort, never the first",
          kinds[-1] == "unverified" and kinds[0] != "unverified", kinds)


def check_version_pin():
    """The vendored headers must describe the OptiScaler build actually shipped.

    This is not a formality. The ASI's view of Config is generated from
    asi/optiscaler_ref/Config.h, and the offsets it writes to come from that
    mirror. Vendoring headers from a newer OptiScaler than the release in bin/
    produces a mirror that describes a struct the shipped DLL does not have --
    every validation check then fails and live control silently never attaches.
    That is exactly what happened once already.
    """
    from optiscaler import constants

    print("\nVersion pinning")
    ref = ROOT / "asi" / "optiscaler_ref" / "SOURCE_COMMIT.txt"
    check("vendored OptiScaler headers are recorded", ref.is_file())
    if not ref.is_file():
        return
    text = ref.read_text(encoding="utf-8")
    sys.path.insert(0, str(ROOT / "scripts"))
    import sync_optiscaler_headers
    archive = ROOT / "bin" / constants.PAYLOAD_ARCHIVE
    if archive.is_file():
        version, commit, built = sync_optiscaler_headers.build_info(
            sync_optiscaler_headers.dll_from_archive(archive))
        check("the vendored headers are the commit the bundled OptiScaler.dll was built from",
              text.startswith(commit), f"dll names {commit} ({built}), headers {text.split()[0]}")
    # The plugin version, release tags and "already released?" all use it.
    check("the pinned OptiScaler version carries the nightly's date",
          bool(re.search(r"-20\d{6}$", constants.OPTISCALER_VERSION)), constants.OPTISCALER_VERSION)
    check("the shipped archive matches the pinned version",
          constants.OPTISCALER_VERSION in constants.PAYLOAD_ARCHIVE,
          constants.PAYLOAD_ARCHIVE)

    # And the archive is the one the hash names. This pins more than OptiScaler
    # itself: the FidelityFX libraries inside it are what decide the FSR
    # versions a game reports, and the panel names the newest one by reading
    # that library rather than by trusting the reference ini, which documents
    # whichever build *it* shipped with (4.0.2, where this carries 4.1.1).
    archive = ROOT / "bin" / constants.PAYLOAD_ARCHIVE
    check("the shipped archive is present", archive.is_file(),
          f"{archive} -- run scripts/bundle_nightly.py --latest first")
    if archive.is_file():
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        check("the shipped archive is the one the hash pins",
              digest == constants.PAYLOAD_SHA256, digest)

    # The header must be the one the mirror was generated from.
    config_h = (ROOT / "asi" / "optiscaler_ref" / "Config.h").read_text(encoding="utf-8")
    mirror = (ROOT / "asi" / "generated" / "config_mirror.h")
    check("the generated mirror is present", mirror.is_file())
    if mirror.is_file():
        members = re.findall(r"^    X\((\w+),", mirror.read_text(encoding="utf-8"), re.M)
        absent = [m for m in members if f" {m} " not in config_h and f" {m};" not in config_h
                  and f" {m}\n" not in config_h]
        check("every mirrored member still exists in the vendored header",
              not absent, absent[:5])



def check_bundled_payload_upgrade():
    """The archive actually in bin/ installs, upgrades a 0.9.x folder and uninstalls.

    Everything else here runs against a synthetic payload. This one unpacks the
    real bundled OptiScaler through the same Payload code the Deck runs, so a
    nightly that changes its layout (10.0 moved every dependency into an
    OptiScaler/ subfolder) or adds a DLL fails here rather than on the Deck.
    """
    from optiscaler import constants
    from optiscaler.payload import Payload

    print("\nBundled payload: install, upgrade from 0.9.x, uninstall")
    archive = ROOT / "bin" / constants.PAYLOAD_ARCHIVE
    if not archive.is_file():
        check("the bundled archive is present", False,
              f"{archive} -- run scripts/bundle_nightly.py --latest first")
        return

    work = Path(tempfile.mkdtemp(prefix="optiscaler-payload-"))
    try:
        payload = Payload(archive, work / "payload")
        root = payload.ensure()
        check("the bundled archive extracts through Payload", payload.ready, str(root))

        # The name the plugin shows must be the build inside: 10.0.0-pre1 is
        # file version 10.0.0.1.
        dll_version = installer.pe_file_version(root / "OptiScaler.dll")
        major, minor, hotfix, build = (dll_version[0], dll_version[1], dll_version[2], dll_version[3]) \
            if dll_version else (None,) * 4
        expected = re.match(r"(\d+)\.(\d+)\.(\d+)(?:-pre(\d+))?", constants.OPTISCALER_VERSION)
        check("OptiScaler.dll is the version the plugin names",
              expected and (major, minor, hotfix) == tuple(int(g) for g in expected.groups()[:3])
              and (expected.group(4) is None or build == int(expected.group(4))),
              f"{dll_version} vs {constants.OPTISCALER_VERSION}")

        # Flat layout: no OptiScaler/ subfolder, so OptiScaler 10 resolves its
        # dependencies and plugins/ from the game folder (dllmain.cpp falls back
        # to the exe folder when .\OptiScaler is missing).
        check("the payload is flattened (no OptiScaler/ subfolder)",
              not (root / "OptiScaler").exists())
        top_dlls = {p.name for p in root.iterdir() if p.is_file() and p.suffix.lower() == ".dll"}
        missing = sorted(set(constants.PAYLOAD_FILES) - {p.name for p in root.iterdir()})
        unlisted = sorted(top_dlls - set(constants.PAYLOAD_FILES) - {"OptiScaler.dll"})
        check("every payload file is in the archive", not missing, missing)
        check("every DLL in the archive is installed", not unlisted, unlisted)
        check("every payload dir is in the archive",
              all((root / d).is_dir() for d in constants.PAYLOAD_DIRS), constants.PAYLOAD_DIRS)
        check("the bundled FSR upscaler is the one restore puts back",
              hashlib.sha256((root / constants.FFX_UPSCALER_DLL).read_bytes()).hexdigest()
              == constants.FSR4_BUNDLED_FILE_SHA256)

        # A game folder as the 0.9.x plugin left it, one of whose files had
        # displaced something of the game's.
        game = work / "game"
        game.mkdir()
        (game / "Game.exe").write_bytes(b"MZ")
        old_files = ["dxgi.dll", INI_NAME, "libxess.dll"] + list(constants.RETIRED_PAYLOAD_FILES)
        for name in old_files:
            (game / name).write_bytes(b"0.9.x")
        shutil.copy2(root / "OptiScaler.dll", game / "dxgi.dll")
        for name in constants.RETIRED_PAYLOAD_DIRS:
            (game / name).mkdir()
            (game / name / "D3D12Core.dll").write_bytes(b"0.9.x")
        displaced = constants.RETIRED_PAYLOAD_FILES[0] if constants.RETIRED_PAYLOAD_FILES else None
        backups = {}
        if displaced:
            (game / constants.BACKUP_DIR).mkdir()
            (game / constants.BACKUP_DIR / displaced).write_bytes(b"the game's own")
            backups[displaced] = True
        (game / constants.MANIFEST_NAME).write_text(json.dumps({
            "plugin": constants.PLUGIN_ID, "optiscaler_version": "0.9.4", "filename": "dxgi.dll",
            "files": old_files, "dirs": list(constants.RETIRED_PAYLOAD_DIRS) + ["Licenses"],
            "backups": backups, "fsr4_build": {},
        }), encoding="utf-8")

        installer.install(game, root, "dxgi.dll", preserve_ini=True)
        leftovers = [n for n in list(constants.RETIRED_PAYLOAD_FILES) + list(constants.RETIRED_PAYLOAD_DIRS)
                     if (game / n).exists() and n != displaced]
        check("upgrading removes what this build no longer ships", not leftovers, leftovers)
        if displaced:
            check("and puts back the game file one of those had displaced",
                  (game / displaced).read_bytes() == b"the game's own")
        check("the proxy is this build's OptiScaler",
              (game / "dxgi.dll").read_bytes() == (root / "OptiScaler.dll").read_bytes())
        check("the 0.9.x ini is kept", (game / INI_NAME).read_bytes() == b"0.9.x")
        verify = installer.verify_install(game, root)
        check("verify calls the upgraded folder complete", verify["complete"], verify["problems"])
        check("and finds an FSR 4 capable upscaler", verify["ffx_upscaler"]["fsr4_capable"],
              verify["ffx_upscaler"])
        check("the manifest records this build",
              installer.read_manifest(game)["optiscaler_version"] == constants.OPTISCALER_VERSION)

        installer.uninstall(game, remove_ini=True)
        remaining = sorted(p.name for p in game.iterdir())
        check("uninstall leaves only the game's files",
              remaining == sorted(["Game.exe"] + ([displaced] if displaced else [])), remaining)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def check_menu_key():
    """The OptiScaler menu button: the right key, on a uinput keyboard, set up properly.

    No /dev/uinput in CI, so the device is a recorder: every ioctl and every
    event written is captured and compared with what the kernel expects.
    """
    import struct
    from unittest import mock
    from optiscaler import keys

    print("\nOptiScaler menu key")
    check("auto means Insert", keys.parse_shortcut("auto") == 0x2D)
    check("hex and decimal shortcuts parse",
          keys.parse_shortcut("0x24") == 0x24 and keys.parse_shortcut("45") == 0x2D)
    check("junk falls back to Insert", keys.parse_shortcut("insert!") == 0x2D)
    check("ioctl numbers match linux/uinput.h",
          (keys.UI_DEV_CREATE, keys.UI_DEV_DESTROY, keys.UI_DEV_SETUP, keys.UI_SET_EVBIT,
           keys.UI_SET_KEYBIT) == (0x5501, 0x5502, 0x405C5503, 0x40045564, 0x40045565))
    check("input_event is 24 bytes", struct.calcsize(keys.INPUT_EVENT_FORMAT) == 24)

    ioctls, writes = [], []
    keys._fd = None
    with mock.patch.object(keys.os, "open", return_value=99), \
            mock.patch.object(keys.os, "write", side_effect=lambda fd, data: writes.append(data)), \
            mock.patch.object(keys.os, "close"), \
            mock.patch.object(keys.fcntl, "ioctl", side_effect=lambda fd, req, arg=0: ioctls.append((req, arg))), \
            mock.patch.object(keys.time, "sleep"):
        first = keys.press(0x2D)
        keys.press(0x2D)
        keys.close()
    requests = [req for req, _ in ioctls]
    check("the device is created once and destroyed on close",
          requests.count(keys.UI_DEV_CREATE) == 1 and requests[-1] == keys.UI_DEV_DESTROY)
    check("it is a keyboard that can press Insert",
          (keys.UI_SET_EVBIT, keys.EV_KEY) in ioctls and (keys.UI_SET_KEYBIT, 110) in ioctls)
    events = [struct.unpack(keys.INPUT_EVENT_FORMAT, w)[2:] for w in writes]
    check("a press is down, sync, up, sync",
          events[:4] == [(keys.EV_KEY, 110, 1), (0, 0, 0), (keys.EV_KEY, 110, 0), (0, 0, 0)], events[:4])
    check("and the press reports the key", first == {"vk": 0x2D, "evdev": 110})

    keys._fd = None
    with mock.patch.object(keys.os, "open", side_effect=PermissionError(13, "denied")):
        try:
            keys.press(0x2D)
            denied = None
        except PermissionError as exc:
            denied = str(exc)
    check("no access to /dev/uinput is reported plainly", denied and "/dev/uinput" in denied, denied)

    # The service reads the game's shortcut and passes it on.
    root = Path(tempfile.mkdtemp(prefix="optiscaler-menukey-"))
    try:
        (root / INI_NAME).write_text("[Menu]\nShortcutKey=0x24\n", encoding="utf-8")
        service = OptiScalerService(ROOT, root / "s", root / "r", root / "h", logging.getLogger("t"))
        with mock.patch.object(keys, "press", return_value={"vk": 0x24, "evdev": 102}) as press:
            result = asyncio.run(service.toggle_menu(str(root)))
        check("the game's own shortcut is the one pressed",
              result.get("ok") and press.call_args[0][0] == 0x24, result)
        with mock.patch.object(keys, "press", side_effect=FileNotFoundError("no uinput")):
            result = asyncio.run(service.toggle_menu(str(root)))
        check("a failure comes back as an error, not an exception",
              result == {"ok": False, "error": "no uinput"}, result)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_debug_report():
    """The panel's live-control diagnostics, and the report it saves to ~/Downloads.

    Built so a user on a Deck can see why live editing does nothing and send
    one file. Each fact must point at the cause it names.
    """
    import tempfile
    from optiscaler import live
    from optiscaler.service import OptiScalerService

    print("\nLive-control diagnostics report")
    root = Path(tempfile.mkdtemp(prefix="optiscaler-debug-"))
    try:
        target = root / "Some Game (2026)"
        (target / live.PLUGIN_SUBDIR).mkdir(parents=True)
        (target / live.PLUGIN_SUBDIR / live.ASI_NAME).write_bytes(b"MZ" + b"\0" * 64)
        (target / "OptiScaler.ini").write_text("[Plugins]\nLoadAsiPlugins=true\n[Log]\nLogToFile=true\n")
        (target / "OptiScaler.log").write_text(
            "[info] OptiScaler v10.0.0-dev (1e568bb3) (20261116_122736)\n"
            f"[info] Loaded: C:\\game\\plugins\\{live.ASI_NAME}\n"
            "[I] StreamlineHooks::streamlineLogCallback Looking for plugins in S:/sl\n"
            "[I] FSRFG_Dx12::CreateContext XeFG context created\n")
        (target / live.STATUS_FILE).write_text(
            f"schema {live.STATUS_SCHEMA}\nbuilt_for 1e568bb3\nstatus ready\napi 3\n"
            "reinit_available 1\nseq 4\napplied 1\nreinit -1\nconfig 0x1\nfields 377\nerror \n")
        (target / live.CMD_FILE).write_text("seq 4\nset Menu.ShowFps bool 1\n")
        install = {"version": "10.0.0-pre1", "bundled_version": "10.0.0-pre1"}

        st = live.status(str(target))
        check("the status carries the commit the in-game plugin was built for",
              st.get("built_for") == "1e568bb3", st.get("built_for"))
        check("the running build is read from OptiScaler's banner",
              live.running_build(str(target)) == ("10.0.0-dev", "1e568bb3", "20261116_122736"))

        result = live.diagnostics(str(target), install=install)
        facts = {label: (value, tone) for label, value, tone in result["facts"]}
        tones = {label: tone for label, (_, tone) in facts.items()}
        check("a working setup has no warning or failure in it",
              all(t == "ok" for t in tones.values()), tones)
        for label in ("OptiScaler installed here", "OptiScaler that last ran",
                      "In-game plugin built for", "In-game plugin file",
                      "LoadAsiPlugins in OptiScaler.ini", "OptiScaler loaded it",
                      "In-game plugin status", "Last command from the panel"):
            check(f"the diagnostics show: {label}", label in facts, sorted(facts))

        (target / live.STATUS_FILE).write_text(
            f"schema {live.STATUS_SCHEMA}\nbuilt_for e7e3872d\nstatus failed\napi 3\n"
            "error OptiScaler's Config was not found\n")
        (target / "OptiScaler.ini").write_text("[Plugins]\nLoadAsiPlugins=false\n")
        (target / live.CMD_FILE).write_text("seq 9\nset Menu.ShowFps bool 1\n")
        facts = {label: (value, tone) for label, value, tone in
                 live.diagnostics(str(target), install={**install, "version": "10.0.0-pre1-old"})["facts"]}
        check("a plugin built for another OptiScaler than the one that ran is flagged",
              facts["In-game plugin built for"][1] == "bad"
              and "1e568bb3" in facts["In-game plugin built for"][0], facts.get("In-game plugin built for"))
        check("plugins switched off in the ini is flagged",
              facts["LoadAsiPlugins in OptiScaler.ini"][1] == "bad")
        check("a failed in-game plugin shows as failed, with its error",
              facts["In-game plugin status"][1] == "bad"
              and "Config was not found" in facts["Its last error"][0])
        check("an install older than the bundle is flagged",
              facts["OptiScaler installed here"][1] == "warn")
        check("a command the in-game plugin has not applied is flagged",
              facts["Last command from the panel"][1] == "warn")

        home = root / "home"
        service = OptiScalerService(ROOT, root / "s", root / "r", home, logging.getLogger("t"))
        shown = asyncio.run(service.live_diagnostics(str(target)))
        check("the panel gets the facts and whether OptiScaler is logging",
              shown["ok"] and shown["facts"] and shown["logging"] is True, shown.get("logging"))
        saved = asyncio.run(service.save_debug_report(str(target)))
        path = Path(saved.get("path") or "")
        check("the debug report is saved to ~/Downloads",
              saved["ok"] and path.parent == home / "Downloads" and path.is_file(), saved)
        text = path.read_text() if path.is_file() else ""
        check("the report's file name is safe and names the game",
              path.name.startswith("decky-optiscaler-nightly-debug-Some_Game_2026_"), path.name)
        fg = text.split("frame generation and upscaler lines")[-1]
        check("the report carries OptiScaler's frame-generation lines", "XeFG context created" in fg)
        check("Streamline's own plugin chatter stays out of the plugin lines",
              "Looking for plugins in S:/sl" not in text.split("plugin lines")[-1].split("=====")[1])
        for needle in (str(target), "built_for e7e3872d", "OptiScaler v10.0.0-dev (1e568bb3)",
                       "LoadAsiPlugins=false", "seq 9\nset Menu.ShowFps", live.ASI_NAME,
                       "[ bad] In-game plugin built for"):
            check(f"the report includes {needle!r}", needle in text)

        empty = root / "empty"
        empty.mkdir()
        bare = asyncio.run(service.live_diagnostics(str(empty)))
        check("a folder with nothing in it still gets a report, logging off",
              bare["ok"] and bare["logging"] is False and bare["report"], bare.get("logging"))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_live_push_only_what_moved():
    """A preset rewrites keys it shares with the current one; only what moved is pushed.

    Picking FSR 3.1 over FSR 4 rewrites the backend with the value it already
    has. Pushed as a change, that unchanged restart-only key asked for a
    restart the user did not need: only the FSR version had moved, and that
    one applies live.
    """
    import tempfile
    from optiscaler import live, running
    from optiscaler.service import OptiScalerService

    print("\nLive push: only what moved")
    root = Path(tempfile.mkdtemp(prefix="optiscaler-moved-"))
    try:
        target = root / "game"
        target.mkdir()
        (target / INI_NAME).write_text("[Upscalers]\nDx12Upscaler=ffx\n[FSR]\nUpscalerIndex=0\n")
        (target / live.STATUS_FILE).write_text(
            f"schema {live.STATUS_SCHEMA}\nbuilt_for 03b37db7\nstatus ready\napi 3\n"
            "reinit_available 1\nseq 0\napplied 0\nreinit -1\nconfig 0x1\nfields 380\nerror \n")
        service = OptiScalerService(ROOT, root / "s", root / "r", root / "h", logging.getLogger("t"))
        preset = [{"section": "Upscalers", "key": "Dx12Upscaler", "value": "ffx"},
                  {"section": "FSR", "key": "UpscalerIndex", "value": "1"}]
        pushed = asyncio.run(service.write_config(str(target), preset)).get("live") or {}
        check("only the FSR version is pushed, and nothing waits for a restart",
              pushed.get("sent") and pushed.get("applied") == ["FfxUpscalerIndex"]
              and pushed.get("deferred") == [] and pushed.get("reinit"), pushed)
        again = asyncio.run(service.write_config(str(target), preset)).get("live") or {}
        check("writing the same values again sends nothing and asks for no restart",
              again.get("unchanged") and not again.get("sent"), again)
        # The game holds another value than the ini (OptiScaler's key turned FG
        # off); the panel shows the game's, the user picks the ini's: it is
        # unchanged in the ini but has to reach the game.
        (target / INI_NAME).write_text("[Upscalers]\nDx12Upscaler=ffx\n[FSR]\nUpscalerIndex=1\n"
                                       "[Menu]\nShowFps=false\n")
        (target / running.VALUES_FILE).write_text("fields 2\nShowFps bool 1 1 0 - 0\nFfxUpscalerIndex i32 1 1 0 - 0\n")
        (target / running.LOADED_FILE).write_text("fields 2\nShowFps bool 1 0 0 - 0\nFfxUpscalerIndex i32 1 1 0 - 0\n")
        again = asyncio.run(service.write_config(str(target), [
            {"section": "Menu", "key": "ShowFps", "value": "false"},
            {"section": "FSR", "key": "UpscalerIndex", "value": "1"}])).get("live") or {}
        check("a live key the ini already says is sent when the game holds another value",
              again.get("sent") and again.get("applied") == ["ShowFps"], again)
        (target / running.VALUES_FILE).unlink()
        (target / running.LOADED_FILE).unlink()
        switch = asyncio.run(service.write_config(str(target), [
            {"section": "Upscalers", "key": "Dx12Upscaler", "value": "xess"}])).get("live") or {}
        check("a real backend switch still waits for the next launch",
              switch.get("deferred") == ["Upscalers.Dx12Upscaler"], switch)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_running_config():
    """The running game's settings as the panel shows them.

    Built so a setting changed in OptiScaler's own menu shows in the panel,
    marked as not saved. Wrongly marked settings would be worse than none: an
    "auto" OptiScaler resolved at startup, a volatile value it never saves, or
    a value the panel itself just wrote are not changes the user made.
    """
    import tempfile
    from optiscaler import live, running
    from optiscaler.service import OptiScalerService

    print("\nRunning config (OptiScaler's settings in the game)")
    enums = running.ENUMS
    check("the backend tokens come from OptiScaler's own UpscalerToCode",
          "ffx" in enums.get("Upscaler", {}).get("codes", {}).values()
          and "xess" in enums.get("Upscaler", {}).get("codes", {}).values(), enums.get("Upscaler"))
    check("the FG output tokens come from its EnumConfig table",
          "XeFG" in enums.get("FGOutput", {}).get("codes", {}).values(), enums.get("FGOutput"))
    mapped = {k: running.FIELDS.get(k, {}).get("member") for k in (
        "Upscalers.Dx12Upscaler", "FSR.Fsr4GpuSupport", "Upscalers.VulkanUpscaler")}
    check("ini keys read through a local map to the member that local feeds",
          mapped == {"Upscalers.Dx12Upscaler": "Dx12Upscaler", "FSR.Fsr4GpuSupport": "Fsr4GpuSupport",
                     "Upscalers.VulkanUpscaler": "VulkanUpscaler"}, mapped)

    def code(enum, token):
        return next(int(n) for n, c in enums[enum]["codes"].items() if c.lower() == token)

    root = Path(tempfile.mkdtemp(prefix="optiscaler-running-"))
    try:
        target = root / "game"
        target.mkdir()
        (target / INI_NAME).write_text(
            "[Upscalers]\nDx12Upscaler=ffx\n[FrameGen]\nEnabled=true\nFGOutput=fsrfg\n"
            "[FSR]\nFsr4Model=auto\nUpscalerIndex=0\n[Sharpness]\nSharpness=0.55\n"
            "[Menu]\nShowFps=false\n")
        (target / live.STATUS_FILE).write_text(
            f"schema {live.STATUS_SCHEMA}\nbuilt_for 03b37db7\nstatus ready\napi 3\n"
            "reinit_available 1\nseq 0\napplied 0\nreinit -1\nconfig 0x1\nfields 380\nerror \n")

        def fields(**over):
            base = {
                "Dx12Upscaler": f"i32 1 {code('Upscaler', 'ffx')} 0 - 0",
                "FGEnabled": "bool 1 1 0 1 0",
                "FGOutput": f"u32 1 {code('FGOutput', 'fsrfg')} 0 - 0",
                "Fsr4Model": "u8 1 3 0 - 0",          # "auto" resolved by OptiScaler at load
                "FfxUpscalerIndex": "i32 1 0 0 0 0",
                "Sharpness": "f32 1 0.550000012 0.300000012 0.550000012 0",
                "ShowFps": "bool 1 0 0 0 0",
                "HDR": "bool 1 1 0 - 1",               # volatile: OptiScaler's own override
            }
            base.update(over)
            return "fields %d\n" % len(base) + "".join(f"{k} {v}\n" for k, v in base.items())

        (target / running.LOADED_FILE).write_text(fields())
        (target / running.VALUES_FILE).write_text(fields())
        snap = running.snapshot(str(target))
        check("as loaded, nothing is marked as changed in-game", snap["ready"] and snap["unsaved"] == [],
              snap.get("unsaved"))
        values = snap["values"]
        check("live values come back as ini values",
              values.get("Upscalers.Dx12Upscaler") == "ffx" and values.get("FrameGen.Enabled") == "true"
              and values.get("FrameGen.FGOutput") == "fsrfg" and values.get("Sharpness.Sharpness") == "0.55",
              {k: values.get(k) for k in ("Upscalers.Dx12Upscaler", "FrameGen.Enabled",
                                          "FrameGen.FGOutput", "Sharpness.Sharpness")})

        # Changed in OptiScaler's menu: XeFG output, XeSS, FPS overlay on.
        (target / running.VALUES_FILE).write_text(fields(
            FGOutput=f"u32 1 {code('FGOutput', 'xefg')} 0 - 0",
            Dx12Upscaler=f"i32 1 {code('Upscaler', 'xess')} 0 - 0",
            ShowFps="bool 1 1 0 0 0",
            HDR="bool 1 0 0 - 1"))
        snap = running.snapshot(str(target))
        check("settings changed in OptiScaler's menu are marked, and only those",
              snap["unsaved"] == ["FrameGen.FGOutput", "Menu.ShowFps", "Upscalers.Dx12Upscaler"],
              snap["unsaved"])
        check("their live values are what the panel shows",
              snap["values"]["FrameGen.FGOutput"] == "xefg" and snap["values"]["Upscalers.Dx12Upscaler"] == "xess")

        (target / "OptiScaler.log").write_text(
            "[18:18:13.439232] [I] TryCreateOptiFeature Creating OptiScaler feature, HandleId: 1000001\n"
            "[18:18:13.439248] [I] TryCreateOptiFeature Creating FSR 3.X upscaler feature\n"
            "[18:25:01.000000] [I] TryCreateOptiFeature Creating XeSS upscaler feature\n")
        check("the upscaler OptiScaler last created is read from its log",
              running.snapshot(str(target))["created"] == {"upscaler": "XeSS upscaler", "at": "18:25:01"})

        service = OptiScalerService(ROOT, root / "s", root / "r", root / "h", logging.getLogger("t"))
        kept = asyncio.run(service.keep_running_changes(str(target), []))
        check("keeping writes them to the ini as they are now",
              kept["ok"] and sorted(kept["kept"]) == ["FrameGen.FGOutput", "Menu.ShowFps",
                                                       "Upscalers.Dx12Upscaler"], kept)
        check("and nothing is marked any more", running.snapshot(str(target))["unsaved"] == [])
        # The ini rewritten since the game loaded it (a restart-only change from
        # the panel): the ini is the newer intent, nothing is marked.
        (target / running.VALUES_FILE).write_text(fields(ShowFps="bool 1 1 0 0 0"))
        (target / INI_NAME).write_text((target / INI_NAME).read_text().replace("ShowFps=true", "ShowFps=false")
                                       .replace("FGOutput=xefg", "FGOutput=dlssg"))
        snap = running.snapshot(str(target))
        check("a value the ini was rewritten for since the game loaded it is not marked",
              "FrameGen.FGOutput" not in snap["unsaved"], snap["unsaved"])
        rows = {row[0]: row for row in running.differences(str(target))}
        check("the debug report lists what the game holds other than the ini, with what it loaded",
              rows.get("FrameGen.FGOutput", ())[1:4] == ("dlssg", "fsrfg", "fsrfg")
              and rows.get("FSR.Fsr4Model", ())[2:4] == ("3", "3"),
              {k: rows.get(k) for k in ("FrameGen.FGOutput", "FSR.Fsr4Model")})
        check("keeping pushes nothing back into the game",
              not (target / live.CMD_FILE).exists())

        (target / live.STATUS_FILE).write_text(f"schema {live.STATUS_SCHEMA}\nstatus failed\napi 3\n")
        check("without a ready in-game plugin there is no snapshot",
              running.snapshot(str(target))["ready"] is False)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_live_fields_match_optiscalers_menu():
    """Every live setting is changed the way OptiScaler's own menu changes it.

    For each LIVE_FIELDS member, every place OptiScaler's menu writes it
    (asi/optiscaler_ref/menu_common.cpp, synced at the bundled dll's commit) is
    found, with what the menu does next to it: State flags and upscaler
    rebuilds. The plugin can do a write, an upscaler rebuild on the same
    backend (ReInitUpscalers), and press OptiScaler's own key; anything else
    the menu does means the panel's live change would be half of OptiScaler's,
    so a nightly that adds one fails here, naming the line.
    """
    from optiscaler import keys, live

    print("\nLive settings against OptiScaler's own menu")
    ref = ROOT / "asi" / "optiscaler_ref"
    menu = (ref / "menu_common.cpp").read_text(encoding="utf-8", errors="replace")
    lines = menu.splitlines()
    rebuild = {"newBackend", "MARK_ALL_BACKENDS_CHANGED", "ReInitUpscaler"}
    # What the menu does next to a write that the panel's write still matches,
    # read in OptiScaler's own consumers, with why.
    benign = {
        # XeFG applies the count per frame (XeFG_Dx12 Dispatch, SetNumInterpolatedFrames);
        # fgChanged only pauses the generator for a few frames.
        "FGXeFGInterpolationCount": {"fgChanged"},
        # FSR FG reads the flag per frame (FSRFG_Dx12 Dispatch, fgConfig.flags);
        # fgChanged is the same pause.
        "FGDebugView": {"fgChanged"},
        # XeFG sets it per frame (XeFG_Dx12 Dispatch, EnableDebugFeature); the same pause.
        "FGXeFGDebugView": {"fgChanged"},
    }

    def effects_in(text):
        found = set(re.findall(r"\b(?:state|State::Instance\(\))\.(\w+)\s*=(?!=)", text))
        return found | {name for name in ("MARK_ALL_BACKENDS_CHANGED", "ReInitUpscaler") if name + "(" in text}

    def effects_from(site):
        """What the menu does with the write: the block it sits in, when that is
        a small one (a checkbox's or a combo entry's), else its own statement."""
        depth, opener = 0, None
        for j in range(site, max(-1, site - 40), -1):
            depth += lines[j].count("}") - lines[j].count("{")
            if depth < 0:
                opener = j
                break
        if opener is not None:
            depth, closer = 0, None
            for k in range(opener, min(len(lines), opener + 41)):
                depth += lines[k].count("{") - lines[k].count("}")
                if depth <= 0 and k > opener:
                    closer = k
                    break
            if closer is not None:
                return effects_in("\n".join(lines[opener:closer + 1]))
        statement = lines[site]
        k = site
        while ";" not in statement and k + 1 < len(lines) and k < site + 6:
            k += 1
            statement += lines[k]
        return effects_in(statement)

    def function_body(name):
        start = next((i for i, line in enumerate(lines)
                      if re.match(r"^\S.*\bMenuCommon::" + re.escape(name) + r"\s*\(", line)), None)
        if start is None:
            return set()
        depth, found, opened = 0, set(), False
        for line in lines[start:start + 400]:
            found |= set(re.findall(r"\b(?:state|State::Instance\(\))\.(\w+)\s*=(?!=)", line))
            depth += line.count("{") - line.count("}")
            opened = opened or "{" in line
            if opened and depth <= 0:
                break
        return found

    for ini_key, field in sorted(live.LIVE_FIELDS.items()):
        member = re.escape(field.member)
        sites, effects = [], set()
        # `auto& selectedModel = config->Fsr4Model;` and its writes, within its scope.
        alias_at = {}
        for i, line in enumerate(lines):
            alias = re.search(r"auto&\s+(\w+)\s*=\s*config->" + member + r"(?:For\([^)]*\))?\s*;", line)
            if alias:
                for k in range(i + 1, min(len(lines), i + 80)):
                    alias_at.setdefault(k, set()).add(alias.group(1))
        writes = [r"config->" + member + r"\s*=(?!=)", r"config->" + member + r"\.(?:reset|set_volatile_value)\(",
                  r"Config::Instance\(\)->" + member + r"\s*=(?!=)"]
        for i, line in enumerate(lines):
            mine = writes + [r"(?<![\w.>])" + re.escape(a) + r"\s*(?:=(?!=)|\.reset\()" for a in alias_at.get(i, ())]
            if any(re.search(w, line) for w in mine):
                sites.append(i + 1)
                effects |= effects_from(i)
            call = re.search(r"(\w+)\s*\([^;]*&config->" + member + r"\b", line)
            if call:
                sites.append(i + 1)
                effects |= function_body(call.group(1))
        allowed = set(benign.get(field.member, ()))
        if field.needs_reinit:
            allowed |= rebuild
        if field.on_by_key:
            allowed |= {"fgChanged"}  # turned on by OptiScaler's own key, which does it
        extra = sorted(effects - allowed)
        check(f"{ini_key}: OptiScaler's menu changes it ({len(sites)} place(s)) with nothing the panel "
              f"cannot do", sites and not extra,
              f"no write found in menu_common.cpp" if not sites else f"also {extra}, near line(s) {sites[:6]}")

    # The keys the panel presses are OptiScaler's own, with its defaults.
    header = (ref / "Config.h").read_text(encoding="utf-8", errors="replace")
    vk = {"VK_INSERT": 0x2D, "VK_END": 0x23, "VK_PRIOR": 0x21, "VK_NEXT": 0x22, "VK_HOME": 0x24}
    for name, code in keys.DEFAULT_SHORTCUTS.items():
        found = re.search(r"CustomOptional<int>\s+" + name + r"\s*\{\s*(\w+)\s*\}", header)
        check(f"{name} defaults to OptiScaler's own key", found and vk.get(found.group(1)) == code,
              found.group(1) if found else "not in Config.h")
    for name in keys.DEFAULT_SHORTCUTS:
        check(f"OptiScaler's menu reacts to {name} like to its menu key (CheckShortcut)",
              re.search(r"CheckShortcut\(config->" + name + r"\.value_or_default\(\)", menu) is not None)
    # The overlay types the panel cycles through are OptiScaler's, in its order.
    check("the FPS overlay types are OptiScaler's own names, in its order",
          live.FPS_OVERLAY_TYPES[:2] == ["Just FPS", "Simple"] and "Custom" in live.FPS_OVERLAY_TYPES,
          live.FPS_OVERLAY_TYPES)
    check("its cycle key cycles FpsOverlayType, as the panel's button does",
          re.search(r"config->FpsOverlayType\s*=\s*\(FpsOverlay\)\s*\(\(config->FpsOverlayType", menu)
          is not None)


def check_live_keys():
    """Frame generation on through OptiScaler's own FG key, and which key that is.

    A key toggles, so it may only be pressed when the running game has frame
    generation off, and the key is the one the game holds now: it can be
    rebound in OptiScaler's menu without saving.
    """
    import tempfile
    from unittest import mock
    from optiscaler import keys, live, running
    from optiscaler.service import OptiScalerService

    print("\nOptiScaler's own keys")
    check("an unreadable shortcut falls back to the key it is for",
          keys.parse_shortcut("auto", keys.DEFAULT_SHORTCUTS["FGShortcutKey"]) == 0x23
          and keys.parse_shortcut("junk", 0x22) == 0x22 and keys.parse_shortcut("0x24", 0x23) == 0x24)
    enums = running.ENUMS

    def code(enum, token):
        return next(int(n) for n, c in enums[enum]["codes"].items() if c.lower() == token)

    root = Path(tempfile.mkdtemp(prefix="optiscaler-keys-"))
    try:
        target = root / "game"
        target.mkdir()
        (target / INI_NAME).write_text("[Menu]\nShortcutKey=auto\nFGShortcutKey=auto\n")
        (target / live.STATUS_FILE).write_text(
            f"schema {live.STATUS_SCHEMA}\nbuilt_for 03b37db7\nstatus ready\napi 3\n"
            "reinit_available 1\nseq 0\napplied 0\nreinit -1\nconfig 0x1\nfields 380\nerror \n")

        def values(fg_on, output="xefg", fg_key="i32 0 - 35 - 0", menu_key="i32 1 36 45 - 0"):
            body = {
                "FGEnabled": f"bool 1 {1 if fg_on else 0} 0 - 0",
                "FGInput": f"u32 1 {code('FGInput', 'fsrfg')} 0 - 0",
                "FGOutput": f"u32 1 {code('FGOutput', output)} 0 - 0",
                "FGShortcutKey": fg_key,
                "ShortcutKey": menu_key,
            }
            text = "fields %d\n" % len(body) + "".join(f"{k} {v}\n" for k, v in body.items())
            (target / running.VALUES_FILE).write_text(text)
            (target / running.LOADED_FILE).write_text(text)

        service = OptiScalerService(ROOT, root / "s", root / "r", root / "h", logging.getLogger("t"))
        action = [{"ini_key": "FrameGen.Enabled", "shortcut": "FGShortcutKey"}]
        values(fg_on=False)
        with mock.patch.object(keys, "press", return_value={}) as press:
            result = asyncio.run(service.press_live_keys(str(target), action))
        check("FG off in the game: OptiScaler's FG key is pressed (End, its default)",
              result["pressed"] == ["FrameGen.Enabled"] and press.call_args[0][0] == 0x23, result)
        values(fg_on=False, fg_key="i32 1 36 35 - 0")
        with mock.patch.object(keys, "press", return_value={}) as press:
            asyncio.run(service.press_live_keys(str(target), action))
        check("a key rebound in OptiScaler's menu is the one pressed",
              press.call_args[0][0] == 0x24, press.call_args)
        values(fg_on=True)
        with mock.patch.object(keys, "press", return_value={}) as press:
            result = asyncio.run(service.press_live_keys(str(target), action))
        check("FG already on in the game: nothing is pressed (the key would turn it off)",
              not press.called and result["already"] == ["FrameGen.Enabled"], result)
        values(fg_on=False, output="nofg")
        with mock.patch.object(keys, "press", return_value={}) as press:
            result = asyncio.run(service.press_live_keys(str(target), action))
        check("no frame generator set up: nothing is pressed, it waits for a restart",
              not press.called and result["deferred"] == ["FrameGen.Enabled"], result)
        values(fg_on=False)
        with mock.patch.object(keys, "press", return_value={"vk": 36}) as press:
            asyncio.run(service.toggle_menu(str(target)))
        check("the menu button presses the menu key the game holds", press.call_args[0][0] == 36,
              press.call_args)
        (target / live.STATUS_FILE).write_text(f"schema {live.STATUS_SCHEMA}\nstatus failed\napi 3\n")
        with mock.patch.object(keys, "press", return_value={}) as press:
            result = asyncio.run(service.press_live_keys(str(target), action))
        check("without the in-game plugin nothing is pressed", not press.called and not result["ok"], result)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_optipatcher():
    """OptiPatcher ships alongside and installs into the same plugin folder."""
    import tempfile
    from optiscaler import constants, live

    print("\nOptiPatcher")
    bundled = ROOT / "bin" / constants.OPTIPATCHER_NAME
    check("optipatcher is bundled", bundled.is_file())
    if bundled.is_file():
        check("bundled optipatcher is a windows dll", bundled.read_bytes()[:2] == b"MZ")
        digest = hashlib.sha256(bundled.read_bytes()).hexdigest()
        check("bundled optipatcher matches the recorded build",
              digest == constants.OPTIPATCHER_SHA256, digest)

    root = Path(tempfile.mkdtemp(prefix="optiscaler-patcher-"))
    try:
        target = root / "game"
        target.mkdir()
        source = root / "fake.asi"
        source.write_bytes(b"MZ fake")
        result = live.install_plugin_asi(str(target), source, constants.OPTIPATCHER_NAME)
        landed = target / live.PLUGIN_SUBDIR / constants.OPTIPATCHER_NAME
        # The README's instructions are: plugins/OptiPatcher.asi plus
        # LoadAsiPlugins=true. The second half is service.install's job.
        check("optipatcher lands in the plugins folder", result["ok"] and landed.is_file())
        check("it sits next to the live-control plugin",
              landed.parent.name == live.PLUGIN_SUBDIR)
        requirements = {(r["section"], r["key"]): r["value"] for r in live.ini_requirements()}
        check("asi loading is switched on for it",
              requirements.get(("Plugins", "LoadAsiPlugins")) == "true", requirements)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_asi_wide_formats():
    """No wide format string in the ASI may use a bare %s.

    zig cc builds the plugin against mingw's C99-conformant printf, where "%s"
    in a *wide* format string consumes a char*, not a wchar_t*. Passing a UTF-16
    path to it reads the first byte and stops at the NUL that follows, so
    swprintf(L"%s\\x", path) produced a one-character relative path. Every
    control file then landed in the game's working directory, the plugin looked
    completely dead from outside, and nothing anywhere said why. Use %ls, or
    build the path by concatenation.
    """
    print("\nASI wide format strings")
    source = (ROOT / "asi" / "live.cpp").read_text(encoding="utf-8")
    # Wide literals only: L"..." with no escaped quotes inside is enough here.
    wide = re.findall(r'L"((?:[^"\\]|\\.)*)"', source)
    offenders = [text for text in wide if re.search(r"%[-+ #0-9.]*s", text)]
    check("no wide format string passes a path through %s", not offenders, offenders[:3])
    check("the status file reports the folder the plugin chose",
          '"dir %s\\n"' in source)

    built = ROOT / "bin" / "decky_optiscaler_live.asi"
    check("the built plugin is present", built.is_file())
    if built.is_file():
        data = built.read_bytes()
        check("the built plugin is a windows dll", data[:2] == b"MZ")
        check("the built plugin exports what OptiScaler calls",
              b"InitializeASI" in data and b"OptiPluginInit" in data)


def check_auto_plan():
    """Reading a wiki entry back out as instructions.

    The compatibility list is community prose, not a specification, so the whole
    value of automatic set-up rests on two properties: what it takes is real,
    and what it cannot take is reported rather than dropped. Both are checked
    here against the shapes real entries actually have — every fixture below is
    lifted from a live entry on the list.
    """
    from optiscaler import autoplan

    print("\nAutomatic set-up plan")

    def plan_for(**recommendation):
        base = {"matched": True, "game": "Test Game", "filename": "dxgi.dll",
                "filename_source": "wiki entry", "optipatcher": False,
                "compatibility": "✅", "notes": None, "detail": {}}
        base.update(recommendation)
        return autoplan.build(base)

    check("no match means no plan", not autoplan.build({"matched": False})["available"])
    check("no recommendation at all is survivable", not autoplan.build(None)["available"])

    # -- ini settings mined out of prose ---------------------------------
    plan = plan_for(detail={"Settings": "`Dxgi=false` since spoofing isn't required to see "
                                        "DLSS inputs _(autoapplied by Opti)_"})
    check("a setting the wiki states is taken",
          [(s["section"], s["key"], s["value"]) for s in plan["settings"]]
          == [("Spoofing", "Dxgi", "false")], plan["settings"])
    check("each setting says which part of the entry asked for it",
          plan["settings"][0]["source"] == "wiki entry, “Settings”")

    plan = plan_for(detail={"Settings": "`DontCreateD3D12DeviceForLuma=true`"})
    check("a key that is not an OptiScaler setting is refused",
          not plan["settings"] and plan["unresolved"], plan)
    check("and is reported rather than dropped",
          "DontCreateD3D12DeviceForLuma" in plan["unresolved"][0]["text"])

    # A dozen key names exist in several sections. Guessing one would write a
    # real setting the wiki never asked for, in the wrong place.
    plan = plan_for(detail={"Settings": "`Enabled = true`"})
    check("a key name several sections share is not guessed at",
          not plan["settings"] and "ambiguous" in plan["unresolved"][0]["text"],
          plan["unresolved"])
    plan = plan_for(detail={"Settings": "`[OutputScaling]` `Enabled = true` `Multiplier = 2.0`"})
    check("a stated section resolves the same key",
          ("OutputScaling", "Enabled", "true") in
          [(s["section"], s["key"], s["value"]) for s in plan["settings"]], plan["settings"])
    # A real entry, quoting the pre-0.9 layout: [OptiFG] had an Enabled key and
    # no longer does. Resolving it to some other section's Enabled would write a
    # setting nobody asked for, so it has to fail and say so.
    plan = plan_for(detail={"Settings": "[FrameGen] `FGType = optifg` [OptiFG] `Enabled = true`"})
    check("a section that no longer has that key is not quietly re-homed",
          not any(item["key"] == "Enabled" for item in plan["settings"]), plan["settings"])

    plan = plan_for(detail={"Settings": "`Dx12Upscaler = fsr31` had the highest performance"})
    check("the upscaler is left to the user even when an entry names one",
          not plan["settings"], plan["settings"])
    plan = plan_for(detail={"Settings": "`Sharpness = 9.5`"})
    check("a value outside the option's range is refused",
          not plan["settings"] and plan["unresolved"], plan)
    plan = plan_for(detail={"Settings": "`ShortcutKey=0x24`"})
    check("a hex key code is a valid keycode",
          [(s["key"], s["value"]) for s in plan["settings"]] == [("ShortcutKey", "0x24")],
          plan["settings"])

    # -- launch options --------------------------------------------------
    plan = plan_for(notes="Use -dx12 launch option.")
    check("a launch flag the wiki asks for is picked up", plan["launch_flags"] == ["-dx12"])
    check("and lands after %command%, where the game's own arguments go",
          plan["launch_options"] == 'WINEDLLOVERRIDES="dxgi=n,b" %command% -dx12',
          plan["launch_options"])
    # This one read as an instruction for every Luma Unreal Engine entry.
    plan = plan_for(detail={"Known Issues": "If seeing the non-DX11 device error, set ..."})
    check("a flag spelled inside a word is not an instruction",
          plan["launch_flags"] == [], plan["launch_flags"])
    plan = plan_for(notes="Don't use -dx12, the DX11 renderer is the working one.")
    check("a flag the entry warns against is not applied",
          plan["launch_flags"] == [], plan["launch_flags"])
    plan = plan_for(filename="OptiScaler.asi")
    check("an .asi build needs no dll override",
          plan["launch_options"] == "%command%", plan["launch_options"])

    # -- frame generation ------------------------------------------------
    plan = plan_for(detail={"FG Inputs": "DLSSG via Streamline"})
    check("the recommended FG input becomes an ini value",
          (plan["framegen"]["input"], plan["framegen"]["output"]) == ("dlssg", "fsrfg"),
          plan["framegen"])
    check("and is labelled the way OptiScaler's overlay labels it",
          plan["framegen"]["input_label"] == "DLSSG via Streamline")
    # "Nukem's DLSSG" contains "DLSSG", so order of matching decides this one.
    plan = plan_for(detail={"FG Inputs": "Nukem's DLSSG"})
    check("Nukem's is not mistaken for Streamline DLSSG",
          plan["framegen"]["input"] == "nukems", plan["framegen"])
    plan = plan_for(detail={"FG-Settings": "OptiFG (Upscaler) -> XeFG"})
    check("an arrow states the output as well as the input",
          (plan["framegen"]["input"], plan["framegen"]["output"]) == ("upscaler", "xefg"),
          plan["framegen"])
    plan = plan_for(detail={"FG Inputs": "None"})
    check("an entry reporting no working FG says so rather than picking one",
          plan["framegen"]["input"] == "nofg", plan["framegen"])
    check("and that produces no FG changes to apply",
          autoplan.framegen_changes(plan) == [], autoplan.framegen_changes(plan))
    plan = plan_for(detail={"Notes": "FSR3.1 inputs cannot be hooked."})
    check("a passing mention of FG is not a recommendation",
          plan["framegen"] is None, plan["framegen"])

    # -- the whole thing -------------------------------------------------
    plan = plan_for(
        game="007 First Light", filename="dxgi.dll", optipatcher=True,
        notes="Use -dx12 launch option.",
        detail={
            "FG Inputs": "DLSSG via Streamline",
            "Known Issues": "`Dxgi=false` to disable spoofing. "
                            "`RestoreComputeSignature=true` for DLSS inputs.",
        })
    check("a complete entry produces a complete plan",
          plan["available"] and plan["optipatcher"] and plan["source"] == "wiki entry"
          and plan["launch_flags"] == ["-dx12"] and plan["framegen"]["input"] == "dlssg"
          and len(plan["settings"]) == 2, plan)
    changes = autoplan.setting_changes(plan) + autoplan.framegen_changes(plan)
    check("every planned change is a writable ini change",
          all(set(c) == {"section", "key", "value"} for c in changes) and len(changes) == 4,
          changes)
    check("known issues are surfaced as a warning to read them",
          any("known issues" in w.lower() for w in plan["warnings"]), plan["warnings"])


def check_reframework():
    """The games that cannot host OptiScaler without REFramework.

    Nine entries on the compatibility list say, in prose, that OptiScaler does
    nothing in this game unless REFramework is already in the folder — and for
    three releases the plugin read straight past it. Those games got an install
    that reported success, set the launch options, and changed nothing on
    screen, which is the worst failure available because there is no error
    anywhere to go looking for.

    Everything below is driven by the text real entries actually carry.
    """
    from optiscaler import autoplan, reframework
    from optiscaler.constants import (
        REFRAMEWORK_AUTO_INSTALL, REFRAMEWORK_DLL, REFRAMEWORK_PD_ASSETS,
    )

    print("\nREFramework")
    # Automatic installation is switched off (see constants), and everything
    # else about the feature is deliberately kept: which games need it, which
    # of the two builds, the launch-options override, the status read and the
    # removal path. So the checks below assert against the flag rather than
    # against a constant — turning it back on has to make the fetch work again,
    # and turning it off must not stop the requirement being *read*, which is
    # the half that keeps these games from silently doing nothing.

    def plan_for(**recommendation):
        base = {"matched": True, "game": "Test Game", "filename": "dxgi.dll",
                "filename_source": "wiki entry", "optipatcher": False,
                "compatibility": "✅", "notes": None, "detail": {}}
        base.update(recommendation)
        return autoplan.build(base)

    # -- reading the requirement out of the entry ------------------------
    plan = plan_for(game="Monster Hunter Wilds",
                    notes="Requires REFramework, set Dxgi=false in the OptiScaler.ini "
                          "to avoid crashes.")
    ref = plan["reframework"]
    check("a list row stating the requirement is read as one", bool(ref), plan["reframework"])
    check("praydog's unified nightly is the default build",
          ref["variant"] == "nightly" and ref["asset"] == "REFramework.zip", ref)
    check("and whether it is fetched from here follows the flag, nothing else",
          ref["automatic"] == REFRAMEWORK_AUTO_INSTALL, ref)
    check("with a reason to show the user whenever it is not",
          ref["automatic"] or bool(ref["reason"]), ref)
    check("the entry that said so is cited", "notes" in ref["source"].lower(), ref["source"])

    # The pd entries name a different mod that happens to share a filename.
    plan = plan_for(game="Resident Evil 2 (2019)",
                    notes="Requires REFramework (pd-upscaler branch) + PDUpscaler plugin "
                          "+ OptiScaler, check Wiki")
    ref = plan["reframework"]
    check("an entry naming the pd-upscaler branch gets the pd build",
          ref["variant"] == "pd-upscaler" and ref["asset"] == "RE2.zip", ref)
    check("and is told it needs UpscalerBasePlugin as well",
          ref["plugin"] == "PDPerfPlugin.dll" and ref["plugin_url"], ref)
    check("which is reported as the user's job, since Nexus needs a login",
          any("Nexus" in w for w in plan["warnings"]), plan["warnings"])

    plan = plan_for(game="Devil May Cry 5",
                    notes="Requires REFramework PDUpscaler branch + PDPerfPlugin mod and "
                          "carefully reading the compatibility entry")
    check("the branch spelled without a hyphen is the same branch",
          plan["reframework"]["asset"] == "DMC5.zip", plan["reframework"])

    # The detail page states it a different way than the list row does.
    plan = plan_for(game="Resident Evil 8 Village", detail={
        "Notes": "To get OptiScaler to work with REFramework + UpscalerBasePlugin: "
                 "Download REFramework's pd-upscaler branch."})
    check("the wiki page's phrasing is read too",
          plan["reframework"]["variant"] == "pd-upscaler", plan["reframework"])

    # -- what must not be read as a requirement --------------------------
    # These sentences are on the same pages, about the same mod, and none of
    # them is an instruction to install anything.
    for text in ("XeFG seems to disable the REF overlay",
                 "REF + XeFG currently cause heavy intermittent stuttering",
                 "Might also have to disable Vignette through REF (under Camera options)",
                 "OptiScaler depends on REF bypassing the anti-modding DRM"):
        check("a passing mention of REF is not a requirement",
              plan_for(detail={"Known Issues": text})["reframework"] is None, text)
    check("and an entry that no longer needs it is not given it anyway",
          plan_for(notes="This no longer requires REFramework as of 0.9.")["reframework"]
          is None)
    check("a game the entry says nothing about gets nothing",
          plan_for(notes="Use -dx12 launch option.")["reframework"] is None)

    # -- Proton's half, which the wiki never mentions ---------------------
    # The wiki is written for Windows, where the game folder wins. On Proton it
    # does not: REFramework's dll needs an override of its own or the mod sits
    # in the folder doing nothing, and following the entry to the letter is
    # exactly how that happens.
    plan = plan_for(game="PRAGMATA", notes="Requires REFramework to work.")
    check("REFramework gets a WINEDLLOVERRIDES entry of its own",
          plan["launch_options"] == 'WINEDLLOVERRIDES="dxgi=n,b;dinput8=n,b" %command%',
          plan["launch_options"])
    plan = plan_for(game="PRAGMATA", notes="Requires REFramework to work. Use -dx12.")
    check("and the game's own arguments still land after %command%",
          plan["launch_options"].endswith("%command% -dx12"), plan["launch_options"])
    check("a game with no REFramework requirement is unchanged",
          plan_for()["launch_options"] == 'WINEDLLOVERRIDES="dxgi=n,b" %command%')
    # -- a game with no known build --------------------------------------
    plan = plan_for(game="Some Unlisted RE Engine Game",
                    notes="Requires REFramework (pd-upscaler branch) to work.")
    ref = plan["reframework"]
    check("a pd game with no known asset refuses rather than guessing one",
          ref["required"] and not ref["automatic"] and ref["asset"] is None, ref)
    check("and says where to get it by hand", ref["page"] and ref["reason"], ref)
    check("which is raised as a warning, not buried",
          any("REFramework" in w for w in plan["warnings"]), plan["warnings"])

    # -- the archive is unpacked by name, never on trust -------------------
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        archive = root / "REFramework.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("dinput8.dll", b"REF" * 100)
            zf.writestr("reframework_revision.txt", "abc123")
            zf.writestr("readme.txt", "not ours")
            zf.writestr("../../evil.dll", b"nope")
            zf.writestr("nested/dinput8.dll", b"a second one")
        taken = reframework._extract(archive, root / "out")
        check("the files REFramework ships are taken",
              sorted(taken) == ["dinput8.dll", "reframework_revision.txt"], taken)
        check("and nothing else in the archive is",
              not (root / "out" / "readme.txt").exists()
              and sorted(p.name for p in (root / "out").iterdir())
              == ["dinput8.dll", "reframework_revision.txt"],
              sorted(p.name for p in (root / "out").iterdir()))
        check("a path that climbs out of the folder lands nowhere",
              not (root.parent / "evil.dll").exists() and not (root / "evil.dll").exists())
        check("the first copy of a name wins, so a nested one cannot overwrite it",
              (root / "out" / "dinput8.dll").read_bytes() == b"REF" * 100)

        empty = root / "empty.zip"
        with zipfile.ZipFile(empty, "w") as zf:
            zf.writestr("something_else.dll", b"x")
        try:
            reframework._extract(empty, root / "out2")
            check("an archive without the dll is refused", False, "no error raised")
        except ValueError as exc:
            check("an archive without the dll is refused", REFRAMEWORK_DLL in str(exc), exc)

    # -- installing it, and taking it back out ---------------------------
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        game = root / "game"
        game.mkdir()
        payload = root / "payload"
        payload.mkdir()
        (payload / "OptiScaler.dll").write_bytes(b"OptiScaler" + b"\0" * (5 << 20))
        (payload / INI_NAME).write_text("[Upscalers]\nDx12Upscaler=auto\n")
        source = root / "ref"
        source.mkdir()
        (source / REFRAMEWORK_DLL).write_bytes(b"the REFramework build")
        # The game already ships a dinput8 of its own, which is the case the
        # whole backup contract exists for.
        (game / REFRAMEWORK_DLL).write_bytes(b"the game's own dinput8")

        result = installer.install(str(game), str(payload), "dxgi.dll", False, None,
                                   None, None, [source / REFRAMEWORK_DLL])
        check("REFramework lands next to the executable",
              result["reframework"]["installed"]
              and (game / REFRAMEWORK_DLL).read_bytes() == b"the REFramework build", result)
        check("and the game's own file is set aside rather than lost",
              REFRAMEWORK_DLL in result["backups"], result["backups"])
        found = installer.detect(str(game))
        check("detect reports it, and that we are the ones who installed it",
              found["reframework"]["installed"] and found["reframework"]["managed"], found)

        installer.uninstall(str(game))
        check("removing OptiScaler removes REFramework with it",
              (game / REFRAMEWORK_DLL).read_bytes() == b"the game's own dinput8",
              (game / REFRAMEWORK_DLL).read_bytes())

    # -- adding it to a game already set up -------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        game = root / "game"
        game.mkdir()
        payload = root / "payload"
        payload.mkdir()
        (payload / "OptiScaler.dll").write_bytes(b"OptiScaler" + b"\0" * (5 << 20))
        (payload / INI_NAME).write_text("[Upscalers]\nDx12Upscaler=auto\n")
        installer.install(str(game), str(payload), "dxgi.dll", False)
        source = root / "ref"
        source.mkdir()
        (source / REFRAMEWORK_DLL).write_bytes(b"added later")

        added = installer.add_files(str(game), [source / REFRAMEWORK_DLL])
        check("REFramework can be added without reinstalling OptiScaler",
              added["ok"] and (game / REFRAMEWORK_DLL).is_file(), added)
        manifest = installer.read_manifest(game)
        check("and joins the manifest, so it is removed with everything else",
              REFRAMEWORK_DLL in manifest["files"] and manifest["reframework"], manifest)
        installer.uninstall(str(game))
        check("which it is", not (game / REFRAMEWORK_DLL).exists())

    # -- the curated asset table ------------------------------------------
    seed = json.loads((ROOT / "defaults" / "compat-list.json").read_text())
    keys = {entry["key"] for entry in seed["entries"]}
    unknown = sorted(k for k in REFRAMEWORK_PD_ASSETS if k not in keys)
    check("every curated asset key is a real compatibility-list entry",
          not unknown, unknown)

    # The point of the whole feature: the games on the real list that need it
    # are found, and no others are.
    found = {}
    for entry in seed["entries"]:
        recommendation = dict(entry, matched=True, game=entry["name"], detail={},
                              filename="dxgi.dll", filename_source="x")
        ref = autoplan.build(recommendation)["reframework"]
        if ref:
            found[entry["name"]] = ref["variant"]
    check("the whole bundled list yields exactly the entries that ask for it",
          sorted(found) == [
              "Devil May Cry 5", "Monster Hunter Wilds",
              "Onimusha: Way of the Sword", "PRAGMATA",
              "Resident Evil 2 (2019)", "Resident Evil 3 (2020)",
              "Resident Evil 4 (2023)", "Resident Evil 7 Biohazard",
              "Resident Evil 8 Village", "Resident Evil 9 Requiem",
          ], sorted(found))
    check("with the right build for each",
          [found[name] for name in sorted(found)] ==
          ["pd-upscaler", "nightly", "nightly", "nightly", "pd-upscaler",
           "pd-upscaler", "pd-upscaler", "pd-upscaler", "pd-upscaler",
           "nightly"], found)
    # The asset is what the checklist names when it sends somebody to fetch
    # the file, so it has to resolve whether or not the download is on.
    check("and every one of them resolves to a named build",
          all(autoplan.build(dict(e, matched=True, game=e["name"], detail={},
                                  filename="dxgi.dll", filename_source="x"))
              ["reframework"]["asset"]
              for e in seed["entries"] if e["name"] in found))


def check_value_lists_that_go_on():
    """Numbered value lists written across lines, or with an entry missing its "=".

    Menu.FpsOverlayType reads "... | 3 = Detailed + Graph | 4 Full | 5 Full + Graph
    | 6 Low latency timings" and then "7 = Custom" on the next line; the list
    ended at 3, so schema.valid() refused the overlay types the panel's FPS
    overlay button cycles through, and the DLSS render presets stopped at G
    (K, L, M are DLSS 4's).
    """
    from optiscaler.schema import SCHEMA

    print("\nValue lists over several lines")
    overlay = SCHEMA.get(("Menu", "FpsOverlayType"), {})
    check("every FPS overlay type is a value", overlay.get("options") == [str(i) for i in range(8)],
          overlay.get("options"))
    check("with OptiScaler's names", (overlay.get("optionLabels") or {}).get("4") == "Full"
          and (overlay.get("optionLabels") or {}).get("7") == "Custom", overlay.get("optionLabels"))
    position = SCHEMA.get(("Menu", "FpsOverlayPos"), {})
    check("every FPS overlay position is a value", position.get("options") == [str(i) for i in range(7)],
          position.get("options"))
    preset = SCHEMA.get(("DLSS", "RenderPresetForAll"), {})
    check("the DLSS presets go on past G", "11" in (preset.get("options") or [])
          and (preset.get("optionLabels") or {}).get("11") == "K", preset.get("options"))


def check_prose_is_not_a_specification():
    """Two ways a wiki page says something it is not asking you to do.

    Both of these shipped, and both broke a real game on a real Deck.

    Monster Hunter Wilds stopped booting. Its page lays out three *mutually
    exclusive* install methods, and the miner read all of them: `LoadReshade=true`
    belongs to METHOD 3, where REF's dll has been renamed to `ReShade64.dll` so
    OptiScaler loads it. Applied on top of METHOD 1 — which is what this plugin
    installs — it tells OptiScaler to load a file that is not there while REF is
    already loaded through dinput8.

    Resident Evil 2 rendered nothing useful. "If applying Output Scaling is
    crashing the game, then ... set `Enabled=true` and the desired multiplier
    (e.g. `Multiplier=2.0`)" is remediation advice with an example number, and it
    was applied unconditionally — 2x output scaling, on a Steam Deck.
    """
    from optiscaler import autoplan

    print("\nProse that is not an instruction")

    def plan_for(**recommendation):
        base = {"matched": True, "game": "Test Game", "filename": "dxgi.dll",
                "filename_source": "wiki entry", "optipatcher": False,
                "compatibility": "✅", "notes": None, "detail": {}}
        base.update(recommendation)
        return autoplan.build(base)

    # -- alternative install methods -------------------------------------
    wilds = ("*Requires REFramework to work* * *Download* - REF Nightly --- "
             "*INSTALL METHOD 1* * Keep *REFramework's* default `dinput8.dll` "
             "* Either install *OptiScaler* as `dxgi.dll` --- "
             "*METHOD 3 - loading REF through OptiScaler* "
             "* Rename *REF's* `dinput8.dll` to `ReShade64.dll` "
             "* In `OptiScaler.ini`, set `LoadReshade=true`")
    plan = plan_for(detail={"Notes": wilds})
    check("a setting from an alternative install method is not applied",
          not any(item["key"] == "LoadReshade" for item in plan["settings"]),
          plan["settings"])
    check("but the method this plugin does perform is still read",
          plan["reframework"] and plan["reframework"]["variant"] == "nightly",
          plan["reframework"])
    check("“INSTALL METHOD 1” is not itself a boundary",
          autoplan._this_method_only("a INSTALL METHOD 1 b").strip().endswith("b"))
    check("but METHOD 2 is",
          "gone" not in autoplan._this_method_only("keep this --- METHOD 2 gone"))
    check("and so is an Alternate Method",
          "gone" not in autoplan._this_method_only("keep --- Alternate Method 1 gone"))

    # -- conditional and illustrative values ------------------------------
    re2 = ("If applying Output Scaling is crashing the game, then manually enable it in "
           "*OptiScaler.ini* * Find `[OutputScaling]` in the config, set `Enabled=true` "
           "and the desired multiplier (e.g. `Multiplier=2.0`)")
    plan = plan_for(detail={"Known Issues": re2})
    check("a setting offered as a fix for a problem you may not have is refused",
          not plan["settings"], plan["settings"])
    check("both halves of it, including the one after the “e.g.”",
          len(plan["unresolved"]) == 2, plan["unresolved"])
    check("and the user is told which word made it a maybe",
          all("conditionally" in item["text"] for item in plan["unresolved"]),
          plan["unresolved"])

    # A refusal is not free either: this one is an instruction that happens to
    # name its consequence, and refusing it would leave the crash it prevents.
    # "otherwise" and "instead" are excluded from the hedge words for this.
    plan = plan_for(notes="Requires disabling spoofing, otherwise it crashes - "
                          "set Dxgi=false in OptiScaler.ini.")
    check("“do X, otherwise it crashes” is an instruction, not a condition",
          [(i["key"], i["value"]) for i in plan["settings"]] == [("Dxgi", "false")],
          plan["settings"])
    plan = plan_for(notes="If crashing on certain screen openings, try setting "
                          "`OverlayMenu=false` in OptiScaler.ini")
    check("“if crashing, try setting X” is a condition, not an instruction",
          not plan["settings"] and plan["unresolved"], plan)

    # An unhedged statement in the same field is still taken.
    plan = plan_for(detail={"Settings": "`Dxgi=false`, `OverlayMenu=false` "
                                        "(required for OptiScaler menu to function)"})
    check("a plain statement of two settings is unaffected",
          len(plan["settings"]) == 2, plan["settings"])

    # -- the overlay key it moves ----------------------------------------
    plan = plan_for(detail={"Notes": "*OptiScaler*: set `ShortcutKey=0x24` (changes to "
                                     "home key) in `OptiScaler.ini`."})
    check("the shortcut the entry asks for is still applied",
          any(i["key"] == "ShortcutKey" for i in plan["settings"]), plan["settings"])
    check("and the plan says which key that is, in words",
          plan["hotkey"] and plan["hotkey"]["name"] == "Home", plan["hotkey"])
    check("a game whose entry moves nothing has no hotkey note",
          plan_for()["hotkey"] is None)
    check("a code with no name is reported rather than invented",
          autoplan.key_name("0x9f") is None and autoplan.key_name("0x2D") == "Insert")

    # -- the two games this came from, end to end -------------------------
    plan = plan_for(game="Monster Hunter Wilds",
                    notes="Requires REFramework, set Dxgi=false in the OptiScaler.ini "
                          "to avoid crashes.",
                    detail={"Notes": wilds})
    applied = {(i["key"], i["value"]) for i in plan["settings"]}
    check("Monster Hunter Wilds gets the one setting its entry actually asks for",
          applied == {("Dxgi", "false")}, applied)

    plan = plan_for(game="Resident Evil 2 (2019)",
                    notes="Requires REFramework (pd-upscaler branch) + PDUpscaler plugin",
                    detail={"Settings": "`Dxgi=false`, `OverlayMenu=false` (required for "
                                        "OptiScaler menu to function correctly)",
                            "Known Issues": re2})
    applied = {(i["key"], i["value"]) for i in plan["settings"]}
    check("Resident Evil 2 gets its two, and no output scaling",
          applied == {("Dxgi", "false"), ("OverlayMenu", "false")}, applied)


def check_parenthesised_pages():
    """A wiki page name with brackets in it has to survive being parsed.

    `[Resident Evil 2 (2019)](Resident-Evil-2-(2019))` is an ordinary row on the
    compatibility list, and a link target of `[^)]+` stops at the inner bracket:
    the page came out as "Resident-Evil-2-(2019", the wiki does not serve that,
    and the detail page for every parenthesised title silently never downloaded.
    Those are exactly the Resident Evil entries whose settings and REFramework
    instructions live on the page rather than in the notes column.
    """
    from optiscaler import wiki as wiki_mod

    print("\nParenthesised wiki pages")
    markdown = (
        "| Game | Compatibility | FG Inputs | OptiPatcher | Notes |\n"
        "|---|---|---|---|---|\n"
        "| [Resident Evil 2 (2019)](Resident-Evil-2-(2019)) | ✅ | DLSS |  | Requires REF |\n"
        "| [AI Limit](AI-Limit) | ✅ | DLSS |  |  |\n"
    )
    entries = {e["name"]: e for e in wiki_mod.parse_compat_list(markdown)}
    check("a page name carrying brackets is kept whole",
          entries["Resident Evil 2 (2019)"]["page"] == "Resident-Evil-2-(2019)",
          entries["Resident Evil 2 (2019)"]["page"])
    check("and an ordinary one is unaffected",
          entries["AI Limit"]["page"] == "AI-Limit", entries["AI Limit"]["page"])
    check("the game's own name keeps its brackets too",
          "Resident Evil 2 (2019)" in entries)

    seed = json.loads((ROOT / "defaults" / "compat-list.json").read_text())
    truncated = [e["name"] for e in seed["entries"]
                 if e["page"] and e["page"].count("(") != e["page"].count(")")]
    check("the bundled seed has no half-a-bracket page names left",
          not truncated, truncated)


def check_compat_list_shapes():
    """The compatibility list is hand-edited prose, and its shape moves.

    Two things went wrong on the same page, and neither is visible in a browser:

    A row saved without its leading pipe ended the table for the parser, which
    dropped every row after it — 297 of 695 games, each reported to the user as
    "no wiki entry matched". GitHub renders that line as prose and closes the
    table over the gap, so the page looks right and the plugin quietly forgets
    40% of the list.

    And the page holds three tables, not one: the main table has an OptiPatcher
    column, while "Upscaler mods support" and "Luma Unreal Engine" do not. Read
    by position, their notes column was taken for OptiPatcher support — every
    such game claimed the patcher — and their images column was served to the
    user, and to the plan builder, as the entry's notes.
    """
    from optiscaler import wiki as wiki_mod

    print("\nCompatibility list shapes")
    markdown = (
        "| Game | Compatibility | Upscaler <br>Inputs | OptiPatcher <br>Support"
        " | Notes | Images |\n"
        "| ---- | :---: | :---: | :---: | ---- | :---: |\n"
        "| [Alpha](Alpha) | \u2705 | DLSS | \u2728 | Needs -dx12 | [1](u) |\n"
        "[Bravo](Bravo) | \u2705 | DLSS |  | Saved without its first pipe | [1](u) |\n"
        "| [Charlie](Charlie) | \u2705 | XeSS |  | After the broken row | [1](u) |\n"
        "\n"
        "## Upscaler mods support\n"
        "\n"
        "| Game | Compatibility | Upscaler <br>Inputs | Notes  | Images |\n"
        "| ---- | :---: | :---: | ---- | :---: |\n"
        "| [Delta](Delta) | \u2705 | DLSS | Requires a mod | [1](u) |\n"
    )
    entries = {e["name"]: e for e in wiki_mod.parse_compat_list(markdown)}

    check("a row that lost its leading pipe is still read", "Bravo" in entries)
    check("and it does not take the rest of the table with it", "Charlie" in entries)
    check("the recovered row keeps its own columns",
          entries.get("Bravo", {}).get("notes") == "Saved without its first pipe",
          entries.get("Bravo", {}).get("notes"))
    check("a table with no OptiPatcher column claims none",
          entries["Delta"]["optipatcher"] is False)
    check("and its notes are its notes, not its images",
          entries["Delta"]["notes"] == "Requires a mod", entries["Delta"]["notes"])
    check("while the main table still reads the patcher column",
          entries["Alpha"]["optipatcher"] is True and entries["Bravo"]["optipatcher"] is False)

    # Prose is allowed to contain a pipe; only something shaped like a row is
    # recovered as one.
    prose = (
        "| Game | Compatibility |\n|---|---|\n| [Echo](Echo) | \u2705 |\n"
        "See the table above | it explains the columns\n"
        "| [Foxtrot](Foxtrot) | \u2705 |\n"
    )
    names = [e["name"] for e in wiki_mod.parse_compat_list(prose)]
    check("a sentence carrying a pipe is not read as a row", names == ["Echo"], names)

    seed = json.loads((ROOT / "defaults" / "compat-list.json").read_text())
    # The seed is regenerated per release, and a truncated one is how this
    # reaches a Deck that is offline: the list it falls back to is this file.
    check("the bundled seed is the whole list, not a truncated one",
          len(seed["entries"]) > 600, len(seed["entries"]))


def check_library_labels():
    """Only the SD card is called an SD card.

    The label used to be a path test — anything under /run/media, or with
    "mmcblk" anywhere in the string — so every USB stick and external SSD came
    back as "SD Card". It is now the mount's own block device, which is the only
    thing that actually knows, and this fakes /proc/mounts to drive it.
    """
    print("\nLibrary labels")
    from optiscaler.service import OptiScalerService

    mounts = {
        "/": "/dev/nvme0n1p8",                 # internal SSD, as on most Decks
        "/run/media/mmcblk0p1": "/dev/mmcblk0p1",   # the SD card
        "/run/media/deck/USB": "/dev/sda1",         # a USB stick
        "/run/media/deck/EXT": "/dev/nvme1n1p1",    # an external NVMe drive
        "/home/deck": "/dev/nvme0n1p8",
    }

    def fake_device(path):
        path = str(path)
        best, best_len = None, -1
        for mount, device in mounts.items():
            if path == mount or path.startswith(mount.rstrip("/") + "/"):
                if len(mount) > best_len:
                    best, best_len = device, len(mount)
        return best

    service = OptiScalerService.__new__(OptiScalerService)
    service.home = Path("/home/deck")
    original = OptiScalerService._mount_device
    OptiScalerService._mount_device = staticmethod(fake_device)
    try:
        card = service._library_label("/run/media/mmcblk0p1/steamapps/common")
        usb = service._library_label("/run/media/deck/USB/games")
        ext = service._library_label("/run/media/deck/EXT/games")
        internal = service._library_label("/home/deck/.local/share/Steam")
        check("the sd card is called an sd card", card.startswith("SD Card"), card)
        check("a usb stick is not", not usb.startswith("SD Card"), usb)
        check("nor is an external ssd", not ext.startswith("SD Card"), ext)
        check("internal storage is still internal", internal == "Internal Storage", internal)

        # The 64GB Deck boots from eMMC, so its internal drive is an mmcblk
        # device too - being MMC cannot be the whole test.
        mounts["/"] = "/dev/mmcblk0p8"
        mounts["/run/media/mmcblk1p1"] = "/dev/mmcblk1p1"
        emmc_root = service._library_label("/run/media/mmcblk0p1/steamapps/common")
        real_card = service._library_label("/run/media/mmcblk1p1/steamapps/common")
        check("the drive the system booted from is never the card",
              not emmc_root.startswith("SD Card"), emmc_root)
        check("the other mmc drive still is", real_card.startswith("SD Card"), real_card)
    finally:
        OptiScalerService._mount_device = original

    check("partition suffixes are stripped from either drive naming scheme",
          (OptiScalerService._disk_name("/dev/mmcblk0p1"),
           OptiScalerService._disk_name("/dev/nvme0n1p8"),
           OptiScalerService._disk_name("/dev/sda2")) == ("mmcblk0", "nvme0n1", "sda"))


def check_option_validation():
    """Values the config writer accepts, for the keys whose list is not fixed.

    A write the writer refuses is dropped *and* never pushed to the running
    game, and the panel is left showing a choice that reached neither. That is
    only correct when the refusal is: FSR.FGIndex is documented in the shipped
    ini as the two generators the reference build had, but each game's
    FidelityFX runtime reports its own list, and the plugin lists what the game
    reports. Validating a pick against the ini's two entries therefore refused
    every generator past the second one that a game actually offered.

    FSR.UpscalerIndex is the same snapshot one step over -- the ini names the
    three FSR versions the reference build shipped, and the game in the
    screenshot this was written from reports 4.1.1, which is in none of them.
    """
    from optiscaler.live import FFX_FG_MAX, FFX_UPSCALER_MAX
    from optiscaler.schema import SCHEMA, valid

    print("\nOption validation")

    fg_index = SCHEMA[("FSR", "FGIndex")]
    check("the reference ini only documents two FFX generators",
          fg_index["options"] == ["0", "1"], fg_index["options"])
    check("but a third one the game reports is accepted", valid(fg_index, "2"))
    check("up to the ceiling the live channel enforces",
          valid(fg_index, str(FFX_FG_MAX - 1)) and not valid(fg_index, str(FFX_FG_MAX)))
    check("an index that is not one is still refused",
          not valid(fg_index, "-1") and not valid(fg_index, "fsr"))
    check("auto still means auto", valid(fg_index, "auto"))

    ups_index = SCHEMA[("FSR", "UpscalerIndex")]
    check("the reference ini only documents three FSR versions",
          ups_index["options"] == ["0", "1", "2"], ups_index["options"])
    check("but a fourth the game reports is accepted", valid(ups_index, "3"))
    check("up to the same ceiling",
          valid(ups_index, str(FFX_UPSCALER_MAX - 1))
          and not valid(ups_index, str(FFX_UPSCALER_MAX)))
    check("an index that is not one is still refused",
          not valid(ups_index, "-1") and not valid(ups_index, "fsr4"))

    # FSR4Preset is a closed set of six, and the reference ini writes it as one
    # comma-separated list rather than the "0 = A / 1 = B" form the generator was
    # built around. It read that as two entries with the other four buried in the
    # labels, which made "1", "3", "4" and "5" unwritable — the Quality and
    # Performance models among them. OptiScaler clamps the same range, so six is
    # the truth rather than one build's snapshot.
    fsr4_preset = SCHEMA[("FSR", "Fsr4Preset")]
    check("the FSR4 preset names all six presets",
          fsr4_preset["options"] == ["0", "1", "2", "3", "4", "5"],
          fsr4_preset["options"])
    check("so Quality and Performance can be picked",
          valid(fsr4_preset, "1") and valid(fsr4_preset, "3"))
    check("and one OptiScaler does not have is refused", not valid(fsr4_preset, "6"))

    # Everything else keeps the closed-set treatment: these lists are the
    # backends OptiScaler has, not a snapshot of one runtime's answer.
    dx12 = SCHEMA[("Upscalers", "Dx12Upscaler")]
    check("a real backend id is accepted", valid(dx12, "ffx"))
    check("an id OptiScaler does not have is not", not valid(dx12, "ffx_12"))

    # Basic mode's presets all have to survive the writer, or picking one would
    # half-apply: some keys written, others silently dropped.
    from optiscaler.schema import option as schema_option
    presets = {
        # Mirrors src/config/basic.ts for OptiScaler 10: "ffx" backend ids and
        # Fsr4GpuSupport in place of Fsr4Update / Fsr4ForceEnableInt8.
        "fsr4": [("Upscalers", "Dx12Upscaler", "ffx"), ("Upscalers", "Dx11Upscaler", "ffx_12"),
                 ("Upscalers", "VulkanUpscaler", "ffx_12"),
                 ("FSR", "UpscalerIndex", "0"), ("FSR", "Fsr4GpuSupport", "auto")],
        "fsr4-lite": [("Upscalers", "Dx12Upscaler", "ffx"), ("Upscalers", "Dx11Upscaler", "ffx_12"),
                      ("Upscalers", "VulkanUpscaler", "ffx_12"), ("FSR", "Fsr4GpuSupport", "4"),
                      ("FSR", "Fsr4Model", "3"), ("FSR", "UpscalerIndex", "0"), ("FSR", "Fsr4Preset", "auto")],
        "fsr4-rdna2": [("Upscalers", "Dx12Upscaler", "ffx"), ("Upscalers", "Dx11Upscaler", "ffx_12"),
                       ("Upscalers", "VulkanUpscaler", "ffx_12"), ("FSR", "Fsr4GpuSupport", "3"),
                       ("FSR", "Fsr4Model", "2"), ("FSR", "UpscalerIndex", "0"), ("FSR", "Fsr4Preset", "auto")],
        "fsr31": [("Upscalers", "Dx12Upscaler", "ffx"), ("Upscalers", "Dx11Upscaler", "fsr31"),
                  ("Upscalers", "VulkanUpscaler", "ffx"),
                  ("FSR", "UpscalerIndex", "1"), ("FSR", "Fsr4GpuSupport", "auto")],
        "xess": [("Upscalers", "Dx12Upscaler", "xess"), ("Upscalers", "Dx11Upscaler", "xess_12"),
                 ("Upscalers", "VulkanUpscaler", "xess")],
    }
    for name, changes in presets.items():
        bad = [f"{s}.{k}={v}" for s, k, v in changes
               if not (schema_option(s, k) and valid(schema_option(s, k), v))]
        check(f"every key the {name} preset writes is accepted", not bad, bad)


def _ts_fields(path, interface):
    """Field names of a TypeScript interface, as declared in the frontend."""
    text = (ROOT / path).read_text(encoding="utf-8")
    start = text.index(f"export interface {interface} {{")
    body, depth = [], 0
    for line in text[start:].splitlines()[1:]:
        if depth == 0 and line.startswith("}"):
            break
        if depth == 0:
            match = re.match(r"\s+(\w+)\??:", line)
            if match:
                body.append(match.group(1))
        depth += line.count("{") - line.count("}")
    return body


def check_frontend_types_match_backend():
    """The panel reads only fields the backend really sends.

    Fields from OptiScaler's State (frame rates, the backend in use, the
    generator list) stayed in the frontend's types after the in-game plugin
    stopped reading State; the backend sent them as null forever, and the only
    things that ever filled them were hand-written harness fixtures. Each
    declared field has to be a key the backend's reply holds.
    """
    import tempfile
    from optiscaler import live, running
    from optiscaler.service import OptiScalerService

    print("\nFrontend types against the backend")
    root = Path(tempfile.mkdtemp(prefix="optiscaler-types-"))
    try:
        target = root / "game"
        target.mkdir()
        (target / INI_NAME).write_text("[Plugins]\nLoadAsiPlugins=true\n")
        (target / live.STATUS_FILE).write_text(
            f"schema {live.STATUS_SCHEMA}\nbuilt_for test\nstatus ready\napi 3\nreinit_available 1\n"
            "seq 0\napplied 0\nreinit -1\nconfig 0x1\nfields 1\nerror \n")
        (target / running.VALUES_FILE).write_text("fields 1\nSharpness f32 1 0.5 0.3 - 0\n")
        service = OptiScalerService(ROOT, root / "settings", root / "runtime", root / "home",
                                    logging.getLogger("types"))
        replies = {
            "LiveStatus": asyncio.run(service.get_live_status(str(target))),
            "RunningConfig": asyncio.run(service.get_running_config(str(target))),
        }
        where = {"LiveStatus": "src/types.ts", "RunningConfig": "src/api.ts"}
        for interface, reply in replies.items():
            declared = _ts_fields(where[interface], interface)
            missing = [name for name in declared if name not in reply]
            check(f"every {interface} field the panel declares is one the backend sends",
                  declared and not missing, missing or declared)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(run())
    check_live_control()
    check_live_diagnostics()
    check_debug_report()
    check_live_push_only_what_moved()
    check_running_config()
    check_frontend_types_match_backend()
    check_live_fields_match_optiscalers_menu()
    check_live_keys()
    check_remembered_choices()
    check_launch_record()
    check_launch_options_read()
    check_non_steam_shortcut()
    check_asi_reporting()
    check_asi_staleness()
    check_reported_folder()
    check_wiki_failure_reporting()
    check_wiki_cache()
    check_wiki_transport()
    check_wiki_tls()
    check_version_pin()
    check_bundled_payload_upgrade()
    check_menu_key()
    check_optipatcher()
    check_asi_wide_formats()
    check_auto_plan()
    check_reframework()
    check_prose_is_not_a_specification()
    check_value_lists_that_go_on()
    check_parenthesised_pages()
    check_compat_list_shapes()
    check_option_validation()
    check_library_labels()
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  FAILED: {name}")
        sys.exit(1)


if __name__ == "__main__":
    main()
