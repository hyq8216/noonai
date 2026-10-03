"""Build a standalone macOS app with its native Python runtime and resources."""
import argparse
import importlib.metadata
import json
import os
import platform
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / 'desktop'
PY = HERE / '.venv/bin/python'


def run(*args):
    subprocess.run([str(x) for x in args], check=True, cwd=ROOT)


def verify_arch(path, arch):
    run('lipo', path, '-verify_arch', arch)


def preflight(arch):
    if platform.system() != 'Darwin':
        raise SystemExit('macOS is required to build this app; Linux cannot compile or package the macOS runtime.')
    if arch not in ('arm64', 'x86_64') or platform.machine() != arch:
        raise SystemExit('Build architecture must match this Mac and its native Python interpreter.')
    if not PY.is_file():
        raise SystemExit('Create desktop/.venv and install desktop/requirements-build.txt first.')
    actual = subprocess.check_output([str(PY), '-c', 'import platform;print(platform.machine())'], text=True).strip()
    if actual != arch:
        raise SystemExit('desktop/.venv uses a different architecture; create it with native Python.')
    for tool in ('xcrun', 'codesign', 'lipo'):
        if not shutil.which(tool):
            raise SystemExit(f'Required macOS build tool unavailable: {tool}')
    run('xcrun', '--find', 'swiftc')
    for name in ('manifest.json', 'parser.js', 'popup.html', 'popup.css', 'popup.js'):
        source = ROOT / 'browser-extension/domestic-capture' / name
        if not source.is_file() or source.is_symlink():
            raise SystemExit(f'Domestic capture resource missing or unsafe: {name}')


def release_info():
    config = json.loads((HERE / 'release.json').read_text(encoding='utf-8'))
    import re
    if not re.fullmatch(r'\d+\.\d+\.\d+', config['version']) or not str(config['build']).isdigit():
        raise SystemExit('Invalid desktop release metadata')
    return config


def write_notices(resources):
    notices = resources / 'ThirdParty'
    notices.mkdir()
    for package in ('imageio-ffmpeg', 'Pillow', 'cryptography', 'boto3', 'botocore', 's3transfer',
                    'jmespath', 'python-dateutil', 'urllib3', 'six'):
        dist = importlib.metadata.distribution(package)
        for file in dist.files or []:
            if ('LICENSE' in str(file).upper() or 'COPYING' in str(file).upper()) and '.dist-info/' in str(file):
                source = Path(dist.locate_file(file))
                if source.is_file():
                    shutil.copyfile(source, notices / (package + '-' + source.name))
    import imageio_ffmpeg
    for name, flag in [('FFmpeg-license.txt', '-L'), ('FFmpeg-build.txt', '-version')]:
        result = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), flag], capture_output=True, text=True, check=True)
        (notices / name).write_text(result.stdout + result.stderr, encoding='utf-8')
    (notices / 'README.txt').write_text(
        'Local macOS development build. FFmpeg is supplied by imageio-ffmpeg as a separate subprocess.\n'
        'https://github.com/imageio/imageio-ffmpeg\nhttps://ffmpeg.org\n'
        'Dependency license notices and FFmpeg build/license reports are included.\n'
        'Public distribution requires completing any applicable corresponding-source obligations.\n', encoding='utf-8')


def build_app(dist, arch, overwrite=False):
    # Check the platform before creating or deleting anything.
    preflight(arch)
    info = release_info()
    dist = Path(dist).expanduser().resolve()
    app = dist / 'Noon Studio.app'
    if app.exists() and not overwrite:
        raise SystemExit('App already exists; choose another output directory or explicitly pass --overwrite.')
    if app.is_symlink():
        raise SystemExit('Refusing to replace an app symlink.')
    dist.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='noon-build-', dir=dist) as temporary:
        stage = Path(temporary)
        built = stage / 'Noon Studio.app'
        run(PY, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--name', 'noon-backend',
            '--distpath', stage / 'backend-dist', '--workpath', stage / 'pyinstaller', '--specpath', stage,
            '--add-data', str(ROOT / 'workbench/static') + ':static',
            '--add-data', str(ROOT / 'workbench/examples') + ':examples',
            '--add-data', str(ROOT / 'browser-extension/domestic-capture') + ':domestic-extension',
            '--collect-all', 'imageio_ffmpeg', ROOT / 'workbench/server.py')
        mac = built / 'Contents/MacOS'
        resources = built / 'Contents/Resources'
        mac.mkdir(parents=True)
        resources.mkdir()
        shutil.copytree(stage / 'backend-dist/noon-backend', resources / 'backend', symlinks=True)
        write_notices(resources)
        run('xcrun', 'swiftc', '-O', '-target', f'{arch}-apple-macosx{info["minimum_macos"]}',
            '-framework', 'Cocoa', '-framework', 'WebKit', HERE / 'NoonStudio.swift', '-o', mac / 'NoonStudio')
        bundle = {'CFBundleName': 'Noon Studio', 'CFBundleDisplayName': 'Noon Studio',
                  'CFBundleIdentifier': 'com.noonstudio.desktop', 'CFBundleExecutable': 'NoonStudio',
                  'CFBundlePackageType': 'APPL', 'CFBundleShortVersionString': info['version'],
                  'CFBundleVersion': str(info['build']), 'LSMinimumSystemVersion': info['minimum_macos'],
                  'NSHighResolutionCapable': True, 'NSPrincipalClass': 'NSApplication',
                  'NSAppTransportSecurity': {'NSAllowsLocalNetworking': True},
                  'NSHumanReadableCopyright': 'Noon Studio · Local development build'}
        with (built / 'Contents/Info.plist').open('wb') as handle:
            plistlib.dump(bundle, handle)
        verify_arch(mac / 'NoonStudio', arch)
        verify_arch(resources / 'backend/noon-backend', arch)
        run('codesign', '--force', '--deep', '--sign', '-', built)
        run('codesign', '--verify', '--deep', '--strict', built)
        # Verify the actual frozen runtime before replacing a previous app.
        run(PY, HERE / 'verify_release_bundle.py', '--app', built)
        previous = stage / 'previous.app'
        if app.exists():
            app.rename(previous)
        try:
            built.rename(app)
        except BaseException:
            if previous.exists():
                previous.rename(app)
            raise
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dist', type=Path, default=Path(os.environ.get('NOON_BUILD_DIST', HERE / 'dist')))
    parser.add_argument('--arch', default=platform.machine())
    parser.add_argument('--overwrite', action='store_true')
    args = parser.parse_args()
    print(build_app(args.dist, args.arch, args.overwrite))


if __name__ == '__main__':
    main()
