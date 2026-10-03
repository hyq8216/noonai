"""Build a standalone Apple Silicon application using a project-local build environment."""
import os
import importlib.metadata
import plistlib
import shutil
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
HERE=ROOT/'desktop'
BUILD=HERE/'build'
DIST=Path(os.environ.get('NOON_BUILD_DIST',str(HERE/'dist'))).resolve()
APP=DIST/'Noon Studio.app'
PY=HERE/'.venv/bin/python'

def run(*args): subprocess.run([str(x) for x in args],check=True,cwd=ROOT)

if not PY.is_file(): raise SystemExit('Create desktop/.venv and install desktop/requirements-build.txt first')
run(PY,'-m','PyInstaller','--noconfirm','--clean','--onedir','--name','noon-backend',
    '--distpath',BUILD/'backend-dist','--workpath',BUILD/'pyinstaller','--specpath',BUILD,
    '--add-data',str(ROOT/'workbench/static')+':static', '--add-data',str(ROOT/'workbench/examples')+':examples',
    '--collect-all','imageio_ffmpeg',
    ROOT/'workbench/server.py')
if APP.exists(): shutil.rmtree(APP)
mac=APP/'Contents/MacOS'; resources=APP/'Contents/Resources'; mac.mkdir(parents=True); resources.mkdir()
shutil.copytree(BUILD/'backend-dist/noon-backend',resources/'backend',symlinks=True)
notices=resources/'ThirdParty';notices.mkdir()
import imageio_ffmpeg
dist=importlib.metadata.distribution('imageio-ffmpeg')
(notices/'imageio-ffmpeg-LICENSE.txt').write_text(dist.read_text('LICENSE'))
for package in ('boto3','botocore','s3transfer','jmespath','python-dateutil','urllib3','six'):
    package_dist=importlib.metadata.distribution(package)
    for file in package_dist.files or []:
        if 'LICENSE' in str(file).upper() and '.dist-info/' in str(file):
            source=Path(package_dist.locate_file(file))
            if source.is_file():shutil.copyfile(source,notices/(package+'-'+source.name))
binary=imageio_ffmpeg.get_ffmpeg_exe()
for name,flag in [('FFmpeg-license.txt','-L'),('FFmpeg-build.txt','-version')]:
    result=subprocess.run([binary,flag],capture_output=True,text=True,check=True)
    (notices/name).write_text(result.stdout+result.stderr)
(notices/'README.txt').write_text('Local development build. FFmpeg executable supplied by imageio-ffmpeg 0.6.0.\nhttps://github.com/imageio/imageio-ffmpeg\nhttps://ffmpeg.org\nThe executable is a separate subprocess; its configuration and license notice are included.\nPublic distribution requires completing the corresponding-source and dependency-license package.\n')
run('xcrun','swiftc','-O','-target','arm64-apple-macosx12.0','-framework','Cocoa','-framework','WebKit',HERE/'NoonStudio.swift','-o',mac/'NoonStudio')
info={'CFBundleName':'Noon Studio','CFBundleDisplayName':'Noon Studio','CFBundleIdentifier':'com.noonstudio.desktop',
      'CFBundleExecutable':'NoonStudio','CFBundlePackageType':'APPL','CFBundleShortVersionString':'0.42.0','CFBundleVersion':'42',
      'LSMinimumSystemVersion':'12.0','NSHighResolutionCapable':True,'NSPrincipalClass':'NSApplication',
      'NSAppTransportSecurity':{'NSAllowsLocalNetworking':True},'NSHumanReadableCopyright':'Noon Studio · Local development build'}
with (APP/'Contents/Info.plist').open('wb') as f: plistlib.dump(info,f)
run('codesign','--force','--deep','--sign','-',APP)
run('codesign','--verify','--deep','--strict',APP)
print(APP)
