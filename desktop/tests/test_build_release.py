"""Linux checks of build preconditions and actual source-backed smoke contracts, not Mac validation."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

DESKTOP=Path(__file__).resolve().parents[1]
ROOT=DESKTOP.parent


def load(name):
    spec=importlib.util.spec_from_file_location(name,DESKTOP/(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


build=load('build')
smoke=load('verify_release_bundle')


class ReleaseTests(unittest.TestCase):
    def test_linux_cli_guard_leaves_output_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            target=Path(temporary)/'new-output'
            with patch.object(build.platform,'system',return_value='Linux'):
                with self.assertRaisesRegex(SystemExit,'macOS is required'):
                    build.build_app(target,'arm64',overwrite=True)
            self.assertFalse(target.exists())

    def test_architecture_mismatch_precedes_any_build(self):
        with patch.object(build.platform,'system',return_value='Darwin'),patch.object(build.platform,'machine',return_value='x86_64'),patch.object(build,'run') as command:
            with self.assertRaisesRegex(SystemExit,'architecture'):
                build.preflight('arm64')
            command.assert_not_called()

    def test_lipo_input_precedes_variadic_verify_arch_arguments(self):
        binary=Path('/tmp/Noon Studio.app/Contents/MacOS/NoonStudio')
        with patch.object(build,'run') as command:
            build.verify_arch(binary,'arm64')
            command.assert_called_once_with('lipo',binary,'-verify_arch','arm64')

    def test_release_metadata_matches_installer(self):
        release=build.release_info()
        self.assertEqual(release['version'],'0.43.0')
        self.assertEqual(release['build'],'43')
        self.assertTrue((DESKTOP/('INSTALL-'+release['version']+'.md')).is_file())

    def test_smoke_environment_does_not_inherit_provider_config(self):
        with patch.dict(os.environ,{'NOON_STUDIO_DATA':'forbidden','TEXT_TOKEN':'forbidden','OPENAI_API_KEY':'forbidden','PYTHONPATH':'forbidden','PATH':'developer-runtime'}):
            clean=smoke.clean_environment()
        self.assertEqual(clean['PATH'],'/usr/bin:/bin')
        self.assertTrue(all(key not in clean for key in ('NOON_STUDIO_DATA','TEXT_TOKEN','OPENAI_API_KEY','PYTHONPATH')))

    def test_missing_backend_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(SystemExit,'missing'):
                smoke.verify(Path(folder)/'Noon Studio.app')

    def test_real_smoke_contract_using_source_wrapper_not_macos(self):
        with tempfile.TemporaryDirectory() as folder:
            app=Path(folder)/'Noon Studio.app'
            executable=app/'Contents/Resources/backend/noon-backend'
            executable.parent.mkdir(parents=True)
            # This invokes the current source, not a frozen/Mach-O executable.
            executable.write_text('#!'+sys.executable+'\nimport runpy,sys\nsys.path.insert(0,'+repr(str(ROOT/'workbench'))+')\nrunpy.run_path('+repr(str(ROOT/'workbench/server.py'))+',run_name="__main__")\n')
            executable.chmod(0o755)
            report=smoke.verify(app)
            self.assertEqual(report['module_surfaces'],21)
            self.assertTrue(report['bundled_ffmpeg_verified'])
            self.assertTrue(report['stock_roundtrip_verified'])
            self.assertFalse(report['native_window_verified'])
            self.assertFalse(report['real_noon_verified'])


if __name__=='__main__':unittest.main()
