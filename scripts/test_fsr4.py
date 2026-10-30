#!/usr/bin/env python3
"""Offline integration tests for the Steam Deck FSR 4 presets' DLL/settings lifecycle.

The presets select a model inside the bundled FidelityFX upscaler (INT8 Lite or
INT8 RDNA2). Downloading a community build is still supported for registered
builds, so that path is tested against a fixture build: the one community build
the plugin used to ship, 4.1.1b, is retired.
"""
import asyncio
import hashlib
import logging
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'py_modules'))
from optiscaler import installer, fsr4build
from optiscaler.constants import (FFX_UPSCALER_DLL, FSR4_DECK_MODELS, INI_NAME,
                                  MANIFEST_NAME, RETIRED_FSR4_BUILDS)
from optiscaler.inifile import IniFile
from optiscaler.service import OptiScalerService

ROOT = Path(__file__).resolve().parents[1]

# A registered community build for the download/verify tests, shaped like the
# 4.1.1b entry that used to ship (its sources and hashes are replaced per test).
COMMUNITY = dict(RETIRED_FSR4_BUILDS[0], id='community', label='Community test build')


class BuildFetchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fsr4build._digests.clear()
        self.dll_bytes = b'verified community DLL'
        self.build = dict(COMMUNITY,
                          file_bytes=len(self.dll_bytes),
                          file_sha256=hashlib.sha256(self.dll_bytes).hexdigest())
        self.build['sources'] = [dict(source, archive_sha256=hashlib.sha256(
            source['repo'].encode()).hexdigest()) for source in self.build['sources']]

    def download(self, url, archive, **kwargs):
        source = next(s for s in self.build['sources'] if fsr4build.url_for(s) == url)
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(source['repo'].encode())

    def extract(self, archive, target, logger=None):
        target.mkdir(parents=True, exist_ok=True)
        (target / self.build['file']).write_bytes(self.dll_bytes)

    def test_two_files_remain_cached_and_changes_are_rehashed(self):
        paths = [self.root / name for name in ('game.dll', 'cached.dll')]
        for path in paths:
            path.write_bytes(self.dll_bytes)
        with patch.object(fsr4build.payload, 'sha256', wraps=fsr4build.payload.sha256) as sha:
            for _ in range(3):
                for path in paths:
                    fsr4build.digest(path)
            self.assertEqual(sha.call_count, 2)
            paths[0].write_bytes(b'changed DLL')
            self.assertNotEqual(fsr4build.digest(paths[0]), self.build['file_sha256'])
            self.assertEqual(sha.call_count, 3)
            for i in range(10):
                path = self.root / str(i)
                path.write_bytes(b'other DLL')
                fsr4build.digest(path)
            self.assertLessEqual(len(fsr4build._digests), 8)

    def test_identification_skips_unknown_sizes_but_checks_known_sizes(self):
        path = self.root / 'game.dll'
        path.write_bytes(b'unknown size')
        with patch.object(fsr4build, 'FSR4_BUILDS', [self.build]), \
                patch.object(fsr4build.payload, 'sha256', wraps=fsr4build.payload.sha256) as sha:
            self.assertFalse(fsr4build.identify(path)['known'])
            sha.assert_not_called()
            path.write_bytes(self.dll_bytes)
            self.assertEqual(fsr4build.identify(path)['id'], self.build['id'])
            path.write_bytes(b'x' * len(self.dll_bytes))
            self.assertFalse(fsr4build.identify(path)['known'])

    def test_primary_failure_uses_mirror_and_reuses_verified_cache(self):
        def download(url, archive, **kwargs):
            if url == fsr4build.url_for(self.build['sources'][0]):
                raise OSError('release deleted')
            self.download(url, archive, **kwargs)
        with patch.object(fsr4build.wiki, 'download', side_effect=download) as get, \
                patch.object(fsr4build.payload, 'extract_archive', side_effect=self.extract):
            path = fsr4build.fetch(self.build, self.root)
            self.assertEqual(path.read_bytes(), self.dll_bytes)
            self.assertEqual(get.call_count, 2)
            self.assertEqual(fsr4build.fetch(self.build, self.root), path)
            self.assertEqual(get.call_count, 2)

    def test_corrupt_primary_archive_falls_back(self):
        def download(url, archive, **kwargs):
            self.download(url, archive, **kwargs)
            if url == fsr4build.url_for(self.build['sources'][0]):
                archive.write_bytes(b'corrupt archive')
        with patch.object(fsr4build.wiki, 'download', side_effect=download), \
                patch.object(fsr4build.payload, 'extract_archive', side_effect=self.extract) as extract:
            self.assertEqual(fsr4build.fetch(self.build, self.root).read_bytes(), self.dll_bytes)
            self.assertEqual(extract.call_count, 1)

    def test_wrong_dll_from_either_source_is_rejected(self):
        def extract(archive, target, logger=None):
            self.extract(archive, target, logger)
            (target / self.build['file']).write_bytes(b'x' * len(self.dll_bytes))
        with patch.object(fsr4build.wiki, 'download', side_effect=self.download) as get, \
                patch.object(fsr4build.payload, 'extract_archive', side_effect=extract):
            with self.assertRaisesRegex(RuntimeError, 'DLL checksum mismatch'):
                fsr4build.fetch(self.build, self.root)
            self.assertEqual(get.call_count, 2)
            self.assertFalse(fsr4build.cached_path(self.root, self.build).exists())

    def test_failed_extraction_cannot_leave_a_dll_for_the_mirror(self):
        attempts = 0
        def extract(archive, target, logger=None):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                self.extract(archive, target, logger)
                raise RuntimeError('incomplete extraction')
            target.mkdir(parents=True, exist_ok=True)
        with patch.object(fsr4build.wiki, 'download', side_effect=self.download), \
                patch.object(fsr4build.payload, 'extract_archive', side_effect=extract):
            with self.assertRaisesRegex(RuntimeError, 'did not contain'):
                fsr4build.fetch(self.build, self.root)


class DeckFsr4Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = OptiScalerService(ROOT, self.root/'settings', self.root/'runtime',
                                        self.root/'home', logging.getLogger('test'))
        self.payload = asyncio.run(self.service.ensure_payload())
        self.target = self.root/'game'
        self.target.mkdir()
        (self.target/FFX_UPSCALER_DLL).write_bytes(b'original game DLL')
        installer.install(self.target, self.payload)
        ini = IniFile(self.target/INI_NAME)
        ini.set('FrameGen', 'FGOutput', 'fsr3')
        ini.save()
        self.dll = self.root/'community.dll'
        self.dll.write_bytes(b'test community DLL')
        self.build = dict(COMMUNITY,
                          file_sha256=hashlib.sha256(self.dll.read_bytes()).hexdigest(),
                          file_bytes=self.dll.stat().st_size)
        mock = patch.object(fsr4build, 'FSR4_BUILDS', [self.build])
        mock.start()
        self.addCleanup(mock.stop)

    def enable(self):
        with patch.object(fsr4build, 'fetch', return_value=self.dll):
            return asyncio.run(self.service.set_fsr4_build(str(self.target), 'community'))

    def snapshot(self):
        return {name: (self.target/name).read_bytes()
                for name in [FFX_UPSCALER_DLL, INI_NAME, MANIFEST_NAME]}

    def test_enable_verify_reinstall_restore_uninstall(self):
        self.assertTrue(self.enable()['ok'])
        config = asyncio.run(self.service.read_config(str(self.target)))
        self.assertEqual(config['fsr4_build'], 'community')
        # OptiScaler 10: Fsr4GpuSupport=2 (INT8) replaced Fsr4ForceEnableInt8,
        # and the ffx backend ids replaced fsr31/fsr31_12.
        self.assertEqual(config['values']['FSR']['Fsr4GpuSupport'], '2')
        self.assertEqual(config['values']['FSR']['Fsr4Model'], '1')
        self.assertNotIn('Fsr4ForceEnableInt8', config['values']['FSR'])
        self.assertEqual(config['values']['Upscalers']['Dx12Upscaler'], 'ffx')
        self.assertEqual(config['values']['Upscalers']['Dx11Upscaler'], 'ffx_12')
        self.assertEqual(config['values']['FSR']['UpscalerIndex'], '0')
        self.assertEqual(config['values']['FrameGen']['FGOutput'], 'fsr3')
        self.assertTrue(installer.verify_install(self.target, self.payload)['complete'])
        installer.install(self.target, self.payload)
        self.assertEqual((self.target/FFX_UPSCALER_DLL).read_bytes(), self.dll.read_bytes())
        self.assertEqual(installer.read_manifest(self.target)['fsr4_build']['id'], 'community')
        installer.restore_fsr4_build(self.target, self.payload)
        self.assertEqual(fsr4build.identify(self.target/FFX_UPSCALER_DLL)['id'], 'bundled')
        self.assertTrue(installer.verify_install(self.target, self.payload)['complete'])
        self.assertTrue(self.enable()['ok'])
        installer.uninstall(self.target)
        self.assertEqual((self.target/FFX_UPSCALER_DLL).read_bytes(), b'original game DLL')

    def test_other_registered_build_is_accepted_and_unknown_is_rejected(self):
        self.build['id'] = 'future-build'
        with patch.object(fsr4build, 'fetch', return_value=self.dll) as fetch:
            result = asyncio.run(self.service.set_fsr4_build(str(self.target), 'future-build'))
            self.assertTrue(result['ok'])
            fetch.reset_mock()
            result = asyncio.run(self.service.set_fsr4_build(str(self.target), 'unknown'))
            self.assertFalse(result['ok'])
            fetch.assert_not_called()

    def test_invalid_preset_rolls_back_settings_and_dll(self):
        before = self.snapshot()
        with patch.object(installer.schema, 'valid', return_value=False):
            self.assertFalse(self.enable()['ok'])
        self.assertEqual(before, self.snapshot())

    def test_download_failure_preserves_settings_and_dll(self):
        before = self.snapshot()
        with patch.object(fsr4build, 'fetch', side_effect=ValueError('checksum mismatch')):
            result = asyncio.run(self.service.set_fsr4_build(str(self.target), 'community'))
        self.assertFalse(result['ok'])
        self.assertEqual(before, self.snapshot())

    def test_same_size_corruption_fails_verification_and_reinstall(self):
        self.assertTrue(self.enable()['ok'])
        (self.target/FFX_UPSCALER_DLL).write_bytes(b'x'*self.build['file_bytes'])
        self.assertIn(FFX_UPSCALER_DLL, installer.verify_install(self.target, self.payload)['problems'])
        before = self.snapshot()
        with self.assertRaises(ValueError):
            installer.install(self.target, self.payload)
        self.assertEqual(before, self.snapshot())
        self.assertTrue(self.enable()['ok'])
        self.assertTrue(installer.verify_install(self.target, self.payload)['complete'])

    def test_running_game_rejected_before_download_and_after_download(self):
        before = self.snapshot()
        with patch.object(installer.live, 'status', return_value={'attached': True}), \
                patch.object(fsr4build, 'fetch') as fetch:
            result = asyncio.run(self.service.set_fsr4_build(str(self.target), 'community'))
            self.assertFalse(result['ok'])
            fetch.assert_not_called()
        with patch.object(installer.live, 'status', side_effect=[{}, {'attached': True}]):
            self.assertFalse(self.enable()['ok'])
        self.assertEqual(before, self.snapshot())

    def test_unmanaged_install_rejected(self):
        (self.target/MANIFEST_NAME).unlink()
        before = (self.target/FFX_UPSCALER_DLL).read_bytes()
        self.assertFalse(self.enable()['ok'])
        self.assertEqual(before, (self.target/FFX_UPSCALER_DLL).read_bytes())

    def test_manifest_write_failure_rolls_back_dll_and_ini(self):
        before = self.snapshot()
        real_copy = installer.atomic_copy
        def fail_once(source, destination):
            if Path(source).name == 'new.json':
                raise OSError('simulated disk failure')
            real_copy(source, destination)
        with patch.object(installer, 'atomic_copy', side_effect=fail_once):
            self.assertFalse(self.enable()['ok'])
        self.assertEqual(before, self.snapshot())



class DeckModelTests(unittest.TestCase):
    """The Steam Deck presets: the bundled 4.1.2 upscaler and one of its models."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = OptiScalerService(ROOT, self.root/'settings', self.root/'runtime',
                                        self.root/'home', logging.getLogger('test'))
        self.payload = asyncio.run(self.service.ensure_payload())
        self.target = self.root/'game'
        self.target.mkdir()
        (self.target/FFX_UPSCALER_DLL).write_bytes(b'original game DLL')
        installer.install(self.target, self.payload)
        ini = IniFile(self.target/INI_NAME)
        ini.set('FrameGen', 'FGOutput', 'fsr3')
        ini.save()

    def enable(self, model):
        return asyncio.run(self.service.set_fsr4_build(str(self.target), model))

    def values(self):
        return asyncio.run(self.service.read_config(str(self.target)))['values']

    def snapshot(self):
        return {name: (self.target/name).read_bytes()
                for name in [FFX_UPSCALER_DLL, INI_NAME, MANIFEST_NAME]}

    def test_the_bundled_upscaler_carries_both_models(self):
        # Provider classes FidelityFX registers per model (FSR4Models.cpp).
        data = (Path(self.payload)/FFX_UPSCALER_DLL).read_bytes()
        self.assertIn(b'ffxProvider_FSR4Lite_Int8', data)
        self.assertIn(b'ffxProvider_FSR4_Int8_Navi2', data)

    def test_lite_and_rdna2_write_their_model_and_keep_the_bundled_dll(self):
        for model, support, index in (('lite', '4', '3'), ('rdna2', '3', '2')):
            with self.subTest(model=model):
                result = self.enable(model)
                self.assertTrue(result['ok'], result)
                values = self.values()
                self.assertEqual(values['Upscalers']['Dx12Upscaler'], 'ffx')
                self.assertEqual(values['Upscalers']['Dx11Upscaler'], 'ffx_12')
                self.assertEqual(values['Upscalers']['VulkanUpscaler'], 'ffx_12')
                self.assertEqual(values['FSR']['Fsr4GpuSupport'], support)
                self.assertEqual(values['FSR']['Fsr4Model'], index)
                self.assertEqual(values['FSR']['UpscalerIndex'], '0')
                self.assertEqual(values['FrameGen']['FGOutput'], 'fsr3')
                self.assertEqual(fsr4build.identify(self.target/FFX_UPSCALER_DLL)['id'], 'bundled')
                self.assertFalse(installer.read_manifest(self.target)['fsr4_build'])
                self.assertTrue(installer.verify_install(self.target, self.payload)['complete'])
        installer.uninstall(self.target)
        self.assertEqual((self.target/FFX_UPSCALER_DLL).read_bytes(), b'original game DLL')

    def test_a_deck_preset_replaces_a_community_build(self):
        dll = self.root/'community.dll'
        dll.write_bytes(b'test community DLL')
        build = dict(COMMUNITY, file_sha256=hashlib.sha256(dll.read_bytes()).hexdigest(),
                     file_bytes=dll.stat().st_size)
        with patch.object(fsr4build, 'FSR4_BUILDS', [build]):
            installer.install_fsr4_build(self.target, dll, build, enable_int8=True)
            self.assertEqual(installer.read_manifest(self.target)['fsr4_build']['id'], 'community')
            self.assertTrue(self.enable('lite')['ok'])
        self.assertEqual(fsr4build.identify(self.target/FFX_UPSCALER_DLL)['id'], 'bundled')
        self.assertFalse(installer.read_manifest(self.target)['fsr4_build'])
        self.assertEqual(self.values()['FSR']['Fsr4Model'], '3')

    def test_retired_4_1_1b_is_recognised_and_reinstall_brings_back_the_bundled_dll(self):
        retired = RETIRED_FSR4_BUILDS[0]
        self.assertIsNone(fsr4build.find(retired['id']))
        self.assertFalse(self.enable(retired['id'])['ok'])
        # A folder the 0.0.7 plugin set up with 4.1.1b, as far as the manifest says.
        manifest = installer.read_manifest(self.target)
        manifest['fsr4_build'] = {'id': retired['id'], 'label': retired['label'],
                                  'file': FFX_UPSCALER_DLL, 'sha256': retired['file_sha256']}
        (self.target/MANIFEST_NAME).write_text(__import__('json').dumps(manifest))
        (self.target/FFX_UPSCALER_DLL).write_bytes(b'stand-in for the 4.1.1b DLL')
        installer.install(self.target, self.payload)
        self.assertEqual(fsr4build.identify(self.target/FFX_UPSCALER_DLL)['id'], 'bundled')
        self.assertFalse(installer.read_manifest(self.target)['fsr4_build'])
        self.assertTrue(installer.verify_install(self.target, self.payload)['complete'])

    def test_unknown_model_is_rejected_without_touching_the_folder(self):
        before = self.snapshot()
        self.assertFalse(self.enable('fp8')['ok'])
        self.assertEqual(before, self.snapshot())

    def test_running_game_is_rejected(self):
        before = self.snapshot()
        with patch.object(installer.live, 'status', return_value={'attached': True}):
            self.assertFalse(self.enable('lite')['ok'])
        self.assertEqual(before, self.snapshot())

    def test_invalid_setting_rolls_back(self):
        before = self.snapshot()
        with patch.object(installer.schema, 'valid', return_value=False):
            self.assertFalse(self.enable('rdna2')['ok'])
        self.assertEqual(before, self.snapshot())

    def test_every_offered_model_is_a_real_one(self):
        self.assertEqual({k for k in FSR4_DECK_MODELS if k != 'int8'}, {'lite', 'rdna2'})


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    unittest.main()
