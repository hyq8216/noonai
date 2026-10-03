"""Mocked orchestration checks; these do not validate macOS or real disk images."""
import hashlib
import importlib.util
import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('package_dmg', Path(__file__).resolve().parents[1] / 'package_dmg.py')
pack = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pack)


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.app = self.root / 'Noon Studio.app'
        self.output = self.root / 'out'
        self.guide = self.root / 'INSTALL-0.43.0.md'
        self.guide.write_text('Drag Noon Studio.app into Applications.\n')
        info = {'CFBundleShortVersionString': '0.43.0', 'CFBundleVersion': '43',
                'CFBundleIdentifier': 'com.noonstudio.desktop',
                'CFBundleExecutable': 'NoonStudio', 'CFBundlePackageType': 'APPL'}
        (self.app / 'Contents').mkdir(parents=True)
        (self.app / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
        for relative in ('Contents/MacOS/NoonStudio', 'Contents/Resources/backend/noon-backend'):
            executable = self.app / relative
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_bytes(bytes.fromhex('cffaedfe') + b'fake executable')
            executable.chmod(0o755)
        self.calls = []
        self.fail = None
        self.mount_bad = False
        self.stage = None

    def fake_run(self, *arguments):
        args = [str(arg) for arg in arguments]
        self.calls.append(args)
        if self.fail == tuple(args[:2]):
            raise subprocess.CalledProcessError(1, args)
        stdout = b''
        if args[0] == 'lipo':
            stdout = b'arm64\n'
        elif args[0] == 'ditto' and args[1] != '-c':
            shutil.copytree(args[1], args[2], symlinks=True)
        elif args[:2] == ['hdiutil', 'create']:
            self.stage = Path(args[args.index('-srcfolder') + 1])
            Path(args[-1]).write_bytes(b'mocked disk image')
        elif args[:2] == ['hdiutil', 'attach']:
            mount = Path(args[args.index('-mountpoint') + 1])
            shutil.copytree(self.stage, mount, dirs_exist_ok=True, symlinks=True)
            if self.mount_bad:
                (mount / 'Applications').unlink()
            stdout = plistlib.dumps({'system-entities': [{'mount-point': str(mount)}]})
        elif args[0] == 'ditto' and args[1] == '-c':
            Path(args[-1]).write_bytes(b'mocked zip')
        return subprocess.CompletedProcess(args, 0, stdout, b'')

    def package(self, **kwargs):
        # Keep the historical fake 0.43 app fixture independent of the current release default.
        kwargs.setdefault('version', '0.43.0')
        with patch.object(pack.platform, 'system', return_value='Darwin'), patch.object(pack, 'run', self.fake_run):
            return pack.package_dmg(self.app, self.output, guide=self.guide, **kwargs)

    def test_linux_guard_changes_nothing(self):
        with patch.object(pack.platform, 'system', return_value='Linux'), patch.object(pack, 'run') as run:
            with self.assertRaisesRegex(pack.PackagingError, 'requires macOS'):
                pack.package_dmg(self.app, self.output, guide=self.guide)
            run.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_dmg_zip_hashes_and_readonly_mount(self):
        outputs = self.package(include_zip=True)
        self.assertEqual(len(outputs), 4)
        for output in outputs:
            self.assertTrue(output.is_file())
            if output.suffix == '.sha256':
                artifact = output.with_suffix('')
                self.assertEqual(output.read_text(), hashlib.sha256(artifact.read_bytes()).hexdigest() + '  ' + artifact.name + '\n')
        attach = next(args for args in self.calls if args[:2] == ['hdiutil', 'attach'])
        self.assertIn('-readonly', attach)
        self.assertIn('-nobrowse', attach)
        self.assertTrue(any(args[:2] == ['hdiutil', 'verify'] for args in self.calls))
        self.assertTrue(any(args[:2] == ['hdiutil', 'detach'] for args in self.calls))
        self.assertFalse(list(self.output.glob('.noon-dmg-*')))
        self.assertFalse((self.app / 'Applications').exists())

    def test_mount_check_failure_detaches_and_cleans_without_outputs(self):
        self.mount_bad = True
        with self.assertRaisesRegex(pack.PackagingError, 'shortcut'):
            self.package()
        self.assertTrue(any(args[:2] == ['hdiutil', 'detach'] for args in self.calls))
        self.assertEqual(list(self.output.iterdir()), [])

    def test_create_failure_cleans_temporary_stage(self):
        self.fail = ('hdiutil', 'create')
        with self.assertRaises(subprocess.CalledProcessError):
            self.package()
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertTrue(self.app.exists())

    def test_attach_failure_cleans_without_detaching_unmounted_directory(self):
        self.fail = ('hdiutil', 'attach')
        with self.assertRaises(subprocess.CalledProcessError):
            self.package()
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertFalse(any(args[:2] == ['hdiutil', 'detach'] for args in self.calls))

    def test_detach_failure_retains_mount_for_recovery(self):
        self.fail = ('hdiutil', 'detach')
        with self.assertRaises(subprocess.CalledProcessError):
            self.package()
        retained = list(self.output.glob('.noon-dmg-*'))
        self.assertEqual(len(retained), 1)
        self.assertTrue((retained[0] / 'mount/Noon Studio.app').is_dir())
        self.assertFalse(list(self.output.glob('*.dmg')))

    def test_existing_output_refused_and_preserved(self):
        self.output.mkdir()
        existing = self.output / 'Noon-Studio-0.43.0-macOS-arm64.dmg'
        existing.write_bytes(b'previous release')
        with self.assertRaisesRegex(pack.PackagingError, 'already exists'):
            self.package()
        self.assertEqual(existing.read_bytes(), b'previous release')
        self.assertEqual(self.calls, [])

    def test_overwrite_explicit_after_validation(self):
        self.package()
        existing = self.output / 'Noon-Studio-0.43.0-macOS-arm64.dmg'
        existing.write_bytes(b'old')
        self.package(overwrite=True)
        self.assertEqual(existing.read_bytes(), b'mocked disk image')

    def test_wrong_version_architecture_or_unsigned_app(self):
        with self.assertRaisesRegex(pack.PackagingError, 'version'):
            self.package(version='0.44.0')
        with self.assertRaisesRegex(pack.PackagingError, 'architecture'):
            self.package(arch='x86_64')
        self.fail = ('codesign', '--verify')
        with self.assertRaises(subprocess.CalledProcessError):
            self.package()
        self.assertFalse(self.output.exists())

    def test_business_material_rejected_before_output_mutation(self):
        for name in ('.env', 'credentials.json', 'orders.sqlite3', 'private.key', 'auth.json'):
            with self.subTest(name=name):
                private = self.app / 'Contents/Resources' / name
                private.write_text('synthetic private material')
                with self.assertRaisesRegex(pack.PackagingError, 'Forbidden'):
                    self.package()
                private.unlink()
                self.assertFalse(self.output.exists())

    def test_public_ca_bundle_is_allowed(self):
        (self.app / 'Contents/Resources/cacert.pem').write_text('synthetic public CA bundle')
        self.package()

    def test_private_pem_key_is_rejected(self):
        (self.app / 'Contents/Resources/secret.pem').write_text('-----BEGIN RSA PRIVATE KEY-----\nsynthetic\n')
        with self.assertRaisesRegex(pack.PackagingError, 'private key'):
            self.package()
        self.assertFalse(self.output.exists())

    def test_escape_symlinks_and_output_in_app_rejected(self):
        link = self.app / 'Contents/Resources/escape'
        link.symlink_to(self.root)
        with self.assertRaisesRegex(pack.PackagingError, 'escapes'):
            self.package()
        link.unlink()
        self.output = self.app / 'output'
        with self.assertRaisesRegex(pack.PackagingError, 'inside'):
            self.package()
        self.assertFalse(self.output.exists())

    def test_output_symlink_and_atomic_no_clobber(self):
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        self.output.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaisesRegex(pack.PackagingError, 'symlinks'):
            self.package()
        source = self.root / 'source'
        target = self.root / 'target'
        source.write_bytes(b'new')
        target.write_bytes(b'old')
        with self.assertRaises(FileExistsError):
            pack._publish(source, target, False)
        self.assertEqual(target.read_bytes(), b'old')


if __name__ == '__main__':
    unittest.main()
