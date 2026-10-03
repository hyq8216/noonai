"""Package an already built, signed macOS application. No business data is included."""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import plistlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERSION = '0.44.0'
MACHO_MAGICS = {bytes.fromhex(value) for value in (
    'feedface', 'cefaedfe', 'feedfacf', 'cffaedfe', 'cafebabe', 'bebafeca',
    'cafebabf', 'bfbafeca')}


class PackagingError(RuntimeError):
    pass


def run(*args: object) -> subprocess.CompletedProcess:
    return subprocess.run([str(arg) for arg in args], check=True, capture_output=True)


def validate_app(app: Path, version: str, arch: str) -> None:
    if app.is_symlink() or not app.is_dir() or app.name != 'Noon Studio.app':
        raise PackagingError('Input must be a real Noon Studio.app directory')
    app = app.resolve()
    plist = app / 'Contents/Info.plist'
    try:
        info = plistlib.loads(plist.read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        raise PackagingError('Application Info.plist is missing or invalid') from exc
    expected = {'CFBundleShortVersionString': version,
                'CFBundleIdentifier': 'com.noonstudio.desktop',
                'CFBundleExecutable': 'NoonStudio', 'CFBundlePackageType': 'APPL'}
    if any(info.get(key) != value for key, value in expected.items()):
        raise PackagingError('Application version or bundle identity does not match release')
    if info.get('CFBundleVersion') != str(int(version.split('.')[1])):
        raise PackagingError('Application build number does not match release')
    required = (app / 'Contents/MacOS/NoonStudio',
                app / 'Contents/Resources/backend/noon-backend')
    if not all(path.is_file() and os.access(path, os.X_OK) for path in required):
        raise PackagingError('Application launcher or bundled backend is missing/not executable')
    machos = set()
    for path in app.rglob('*'):
        relative = path.relative_to(app)
        name = path.name.lower()
        if (name == '.env' or name.startswith('.env.') or name == 'auth.json' or 'credentials' in name
                or name.endswith(('.sqlite', '.sqlite3', '.db', '.key'))
                or '.sqlite' in name or 'workbench/data' in relative.as_posix().lower()):
            raise PackagingError(f'Forbidden private/business material in bundle: {relative}')
        if path.is_symlink() and not path.resolve().is_relative_to(app):
            raise PackagingError(f'Bundle symlink escapes application: {relative}')
        if path.is_file():
            with path.open('rb') as source:
                header = source.read(256)
            if re.search(br'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----', header):
                raise PackagingError(f'Forbidden private key in bundle: {relative}')
            magic = header[:4]
            if magic in MACHO_MAGICS:
                machos.add(path.resolve())
    if not all(path.resolve() in machos for path in required):
        raise PackagingError('Launcher and backend must be native Mach-O executables')
    for path in sorted(machos):
        architectures = run('lipo', '-archs', path).stdout.decode().strip().split()
        if arch not in architectures:
            raise PackagingError(f'Expected {arch} architecture: {path.relative_to(app)} ({architectures})')
    run('codesign', '--verify', '--deep', '--strict', app)


def _publish(source: Path, target: Path, overwrite: bool) -> None:
    if overwrite:
        os.replace(source, target)
    else:
        # Atomic no-clobber creation, including races after the initial preflight.
        os.link(source, target)


def package_dmg(app: Path, output_dir: Path = HERE / 'dist', *, version: str = VERSION,
                arch: str = 'arm64', include_zip: bool = False, overwrite: bool = False,
                guide: Path | None = None) -> list[Path]:
    if platform.system() != 'Darwin':
        raise PackagingError('DMG packaging requires macOS; no files were changed')
    if not re.fullmatch(r'0\.\d+\.\d+', version) or arch not in ('arm64', 'x86_64'):
        raise PackagingError('Invalid release version or architecture')
    app = Path(app).absolute()
    if app.is_symlink():
        raise PackagingError('Application input cannot be a symlink')
    app = app.resolve()
    raw_output = Path(output_dir).absolute()
    if any(path.is_symlink() for path in (raw_output, *raw_output.parents)):
        raise PackagingError('Output directory cannot traverse symlinks')
    output_dir = raw_output.resolve()
    if output_dir == app or output_dir.is_relative_to(app):
        raise PackagingError('Output directory cannot be inside the application')
    guide = Path(guide) if guide is not None else HERE / f'INSTALL-{version}.md'
    if guide.is_symlink() or not guide.is_file():
        raise PackagingError('Release installation guide is missing or is a symlink')
    stem = f'Noon-Studio-{version}-macOS-{arch}'
    names = [stem + '.dmg'] + ([stem + '.zip'] if include_zip else [])
    targets = [output_dir / name for name in names]
    all_targets = [path for target in targets for path in (target, target.with_suffix(target.suffix + '.sha256'))]
    for target in all_targets:
        if target.is_symlink() or (target.exists() and (not overwrite or not target.is_file())):
            raise PackagingError(f'Output already exists or is unsafe: {target}; choose another directory or --overwrite')
    validate_app(app, version, arch)
    output_dir.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.noon-dmg-', dir=output_dir))
    mounted = False
    try:
        stage = temporary / 'stage'
        stage.mkdir()
        run('ditto', app, stage / app.name)
        (stage / 'Applications').symlink_to('/Applications', target_is_directory=True)
        shutil.copyfile(guide, stage / guide.name)
        validate_app(stage / app.name, version, arch)
        dmg = temporary / names[0]
        run('hdiutil', 'create', '-volname', f'Noon Studio {version}', '-srcfolder', stage,
            '-format', 'UDZO', '-ov', dmg)
        run('hdiutil', 'verify', dmg)
        mount = temporary / 'mount'
        mount.mkdir()
        # Detach even if attach returns a malformed plist or post-mount checks fail.
        try:
            attached = run('hdiutil', 'attach', '-readonly', '-nobrowse', '-mountpoint', mount, '-plist', dmg)
            mounted = True
            entities = plistlib.loads(attached.stdout).get('system-entities', [])
            if not any(entity.get('mount-point') == str(mount) for entity in entities):
                raise PackagingError('DMG did not mount at the requested verification path')
            validate_app(mount / app.name, version, arch)
            shortcut = mount / 'Applications'
            if not shortcut.is_symlink() or os.readlink(shortcut) != '/Applications':
                raise PackagingError('DMG Applications drag shortcut is missing or invalid')
            if (mount / guide.name).read_bytes() != guide.read_bytes():
                raise PackagingError('DMG installation guide does not match release')
        finally:
            if mounted or mount.is_mount():
                mounted = True
                run('hdiutil', 'detach', mount)
                mounted = False
        if include_zip:
            run('ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', stage / app.name, temporary / names[1])
        for name in names:
            artifact = temporary / name
            with artifact.open('rb') as source:
                digest = hashlib.file_digest(source, 'sha256').hexdigest()
            (temporary / (name + '.sha256')).write_text(f'{digest}  {name}\n', encoding='utf-8')
        for target in all_targets:
            _publish(temporary / target.name, target, overwrite)
        return all_targets
    finally:
        if not mounted:
            shutil.rmtree(temporary)
        # A failed detach retains the temporary mount directory for manual recovery.


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, default=HERE / 'dist/Noon Studio.app')
    parser.add_argument('--output-dir', type=Path, default=HERE / 'dist')
    parser.add_argument('--version', default=VERSION)
    parser.add_argument('--arch', choices=('arm64', 'x86_64'),
                        default='arm64' if platform.machine() == 'arm64' else 'x86_64')
    parser.add_argument('--zip', dest='include_zip', action='store_true', help='Also create a fallback ZIP')
    parser.add_argument('--overwrite', action='store_true', help='Replace existing release artifacts after verification')
    args = parser.parse_args()
    try:
        for path in package_dmg(**vars(args)):
            print(path)
    except (PackagingError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'Packaging failed: {exc}\n')


if __name__ == '__main__':
    main()
